"""AIO extension settings through typed inputs and configured Sites."""

import json
import re
import shlex
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops.arm_resources import ArmResourceObservation
from siteops.cli import main
from siteops.command_context import CommandContext
from siteops.compilation import TemplateCompilationSession
from siteops.guided_inputs import GuidedInputError, load_contract
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    PlanDisposition,
    PlanIntent,
    PlanStatus,
    resolve_plan_value,
)
from siteops.results import RunResult
from tests.test_orchestrator_planning import _arm_template, _RecordingToolRunner

MAPS = {
    "aioExtensionSettings": "aioConfigurationOverrides",
    "secretStoreExtensionSettings": "secretStoreConfigurationOverrides",
    "certManagerExtensionSettings": "certManagerConfigurationOverrides",
}
_SCOPE = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-first/providers"
INSTANCE_ID = f"{_SCOPE}/Microsoft.IoTOperations/instances/external-instance"
LOCATION_ID = f"{_SCOPE}/Microsoft.ExtendedLocation/customLocations/external-location"
CLUSTER_ID = f"{_SCOPE}/Microsoft.Kubernetes/connectedClusters/arc-first"
MANUAL = [
    "siteName=plant-one", "subscription=00000000-0000-0000-0000-000000000001",
    "resourceGroup=rg-existing", "location=eastus", "clusterName=existing-arc",
]


def _release(workspace: Path, release: str) -> dict:
    return yaml.safe_load((workspace / "parameters" / "aio-releases" / f"{release}.yaml").read_text(encoding="utf-8"))


def _session(tmp_path: Path) -> TemplateCompilationSession:
    runner = _RecordingToolRunner()

    def compile_locally(argv, timeout):
        result = runner(argv, timeout)
        if argv[1:3] == ("bicep", "build"):
            source = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
            parameters = {
                name: {
                    "type": kind if kind in {"string", "bool", "int", "object", "array"} else "object",
                    "defaultValue": None,
                }
                for name, kind in re.findall(r"^param\s+(\w+)\s+(\w+)", source, re.MULTILINE)
            }
            Path(argv[argv.index("--outfile") + 1]).write_text(
                json.dumps(_arm_template(parameters)), encoding="utf-8",
            )
        return result

    return TemplateCompilationSession(
        command_runner=compile_locally, tool_resolver=lambda name: str(tmp_path / name),
    )


def _plan(workspace: Path, tmp_path: Path, manifest: str, monkeypatch, **selection):
    session = _session(tmp_path)
    monkeypatch.setattr("siteops.orchestrator.TemplateCompilationSession", lambda: session)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=AssertionError("No real tools")))
    orchestrator = Orchestrator(workspace, site_config_root=selection.pop("site_config_root", None))
    result = orchestrator.build_plan(
        workspace / "manifests" / manifest / "manifest.yaml", intent=PlanIntent.EXECUTABLE, **selection,
    )
    assert result.status is PlanStatus.PLANNED, result.diagnostics
    return result.plan


def _parameters(plan, step: str) -> dict:
    operation = next(op for op in plan.targets[0].operations if op.identity.step == step)
    assert operation.disposition is PlanDisposition.EXECUTE
    assert isinstance(operation.details, DeploymentOperation)
    return {
        entry.key.value: entry.value for entry in operation.details.parameters.entries
        if isinstance(entry.key, LiteralValue)
    }


def _reader():
    calls = []
    related = {
        INSTANCE_ID: {"extendedLocation": LOCATION_ID},
        LOCATION_ID: {"customLocations.hostResourceId": CLUSTER_ID},
        CLUSTER_ID: {},
    }

    def read(ref, *, facts=frozenset(), references=frozenset()):
        calls.append((ref.resource_id, facts))
        assert set(related[ref.resource_id]) == references
        return ArmResourceObservation(
            ref.resource_id, ref.resource_type, "eastus", ref.name, {}, related[ref.resource_id],
        )

    return SimpleNamespace(identity=SimpleNamespace(name="fixture", version="1"), read=read), calls


def _observations() -> dict:
    return {
        "instance": ArmResourceObservation(
            INSTANCE_ID, "Microsoft.IoTOperations/instances", "eastus", "external-instance", {},
            {"extendedLocation": LOCATION_ID},
        ),
        "customLocation": ArmResourceObservation(
            LOCATION_ID, "Microsoft.ExtendedLocation/customLocations", "eastus", "external-location", {},
            {"customLocations.hostResourceId": CLUSTER_ID},
        ),
        "cluster": ArmResourceObservation(
            CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters", "eastus", "arc-first", {},
        ),
    }


def _invoke(argv) -> int:
    with patch.object(sys, "argv", ["siteops", *argv]):
        with pytest.raises(SystemExit) as stopped:
            main()
    return stopped.value.code


@pytest.mark.parametrize("manifest", ["aio-install", "aio-upgrade"])
def test_map_inputs_write_the_documented_site_maps(workspace, manifest):
    contract = load_contract(workspace / "manifests" / manifest / "manifest.yaml")
    fields = {field.name: field for field in contract.fields}
    for name, parameter in MAPS.items():
        field = fields[name]
        assert (field.type, field.required, field.site_path) == ("stringMap", False, ("parameters", parameter))
    assert fields["certManagerExtensionSettings"].when.input == "enableCertManager"
    assert fields["certManagerExtensionSettings"].when.equals is True
    assert fields["aioExtensionSettings"].when is None
    assert fields["secretStoreExtensionSettings"].when is None


def test_install_example_stays_minimal_and_settings_follow_their_extensions(workspace):
    contract = load_contract(workspace / "manifests" / "aio-install" / "manifest.yaml")
    assert contract.example()["values"] == {"cluster": None}
    site = contract.resolve(inline=[
        *MANUAL, "enableCertManager=false", "secretStoreExtensionSettings=rotationPollIntervalInSeconds=240",
    ])
    assert site.properties["deployOptions"]["enableSecretSync"] is False
    assert site.parameters["secretStoreConfigurationOverrides"] == {"rotationPollIntervalInSeconds": "240"}
    assert "certManagerConfigurationOverrides" not in site.parameters
    with pytest.raises(GuidedInputError, match="'certManagerExtensionSettings' was supplied while its condition is inactive"):
        contract.resolve(inline=[*MANUAL, "enableCertManager=false", "certManagerExtensionSettings=k=v"])
    defaults = contract.resolve(inline=MANUAL)
    assert not set(MAPS.values()) & set(defaults.parameters)


@pytest.mark.parametrize("release", ["2607", "2608", "2609"])
def test_typed_install_settings_match_a_site_and_keep_bundled_cert_manager_values(
    workspace, tmp_path, monkeypatch, release,
):
    bundled = _release(workspace, release)["certManagerConfigurationOverrides"]
    replaced = sorted(bundled)[0]
    contract = load_contract(workspace / "manifests" / "aio-install" / "manifest.yaml")
    answers = tmp_path / "answers.yaml"
    answers.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
        "values": {
            "aioRelease": release,
            "aioExtensionSettings": {"observability.metrics.exportIntervalSeconds": "60"},
            "certManagerExtensionSettings": {replaced: "site"},
        },
    }), encoding="utf-8")
    site = contract.resolve(values_file=answers, inline=[
        *MANUAL, "secretStoreExtensionSettings=rotationPollIntervalInSeconds=240",
    ])
    expected = {
        "aioConfigurationOverrides": {"observability.metrics.exportIntervalSeconds": "60"},
        "secretStoreConfigurationOverrides": {"rotationPollIntervalInSeconds": "240"},
        "certManagerConfigurationOverrides": {replaced: "site"},
    }
    assert {name: site.parameters[name] for name in expected} == expected
    plan = _plan(workspace, tmp_path, "aio-install", monkeypatch, sites=[site])
    assert resolve_plan_value(_parameters(plan, "aio-instance")["aioConfigurationOverrides"], {}) == (
        expected["aioConfigurationOverrides"]
    )
    enablement = _parameters(plan, "aio-enablement")
    assert resolve_plan_value(enablement["certManagerConfigurationOverrides"], {}) == {**bundled, replaced: "site"}
    assert resolve_plan_value(enablement["secretStoreConfigurationOverrides"], {}) == (
        expected["secretStoreConfigurationOverrides"]
    )


def test_upgrade_contract_requires_the_release_and_the_cert_manager_answer(workspace, tmp_path):
    contract = load_contract(workspace / "manifests" / "aio-upgrade" / "manifest.yaml")
    assert contract.example()["values"] == {"instance": None, "aioRelease": None, "enableCertManager": None}
    rows = {row["name"]: row for row in contract.describe()["inputs"]}
    for name in ("aioRelease", "enableCertManager"):
        assert rows[name]["status"] == "required" and "default" not in rows[name]
    assert rows["enableCertManager"]["description"].startswith(
        "Include the existing cert-manager Arc extension in this upgrade?"
    )
    assert all(not field.resource.requires for field in contract.fields if field.resource is not None)
    example = tmp_path / "example.yaml"
    example.write_text(yaml.safe_dump(contract.example()), encoding="utf-8")
    with pytest.raises(GuidedInputError) as missing:
        contract.bind(values_file=example)
    assert str(missing.value) == (
        "Missing required input 'instance'. Supply `--input instance=<AIO-instance-resource-ID>`."
    )
    instance = f"instance={INSTANCE_ID}"
    with pytest.raises(GuidedInputError) as missing:
        contract.bind(inline=[instance, "aioRelease=2609"])
    assert str(missing.value) == (
        "Missing required input 'enableCertManager'. Supply `--input enableCertManager=<true-or-false>`."
    )
    with pytest.raises(GuidedInputError) as missing:
        contract.bind(inline=[instance, "enableCertManager=true"])
    assert str(missing.value) == "Missing required input 'aioRelease'. Supply `--input aioRelease=<value>`."
    with pytest.raises(GuidedInputError, match="'certManagerExtensionSettings' was supplied while its condition is inactive"):
        contract.bind(inline=[instance, "aioRelease=2609", "enableCertManager=false", "certManagerExtensionSettings=k=v"])
    excluded = contract.build_site(
        contract.bind(inline=[instance, "aioRelease=2609", "enableCertManager=false"]), _observations(),
    )
    assert excluded.properties == {"aioRelease": "2609", "deployOptions": {"enableCertManager": False}}
    assert (excluded.name, excluded.parameters) == (
        "arc-first", {"aioInstanceName": "external-instance", "clusterName": "arc-first"},
    )


@pytest.mark.parametrize("release", ["2607", "2608", "2609"])
def test_typed_upgrade_applies_settings_over_release_values_at_the_actual_instance(
    workspace, tmp_path, monkeypatch, release,
):
    bundled = _release(workspace, release)["certManagerConfigurationOverrides"]
    replaced = sorted(bundled)[0]
    contract = load_contract(workspace / "manifests" / "aio-upgrade" / "manifest.yaml")
    site = contract.build_site(contract.bind(inline=[
        f"instance={INSTANCE_ID}", f"aioRelease={release}", "enableCertManager=true",
        "aioExtensionSettings=observability.metrics.exportIntervalSeconds=60",
        "secretStoreExtensionSettings=rotationPollIntervalInSeconds=240",
        f"certManagerExtensionSettings={replaced}=site",
    ]), _observations())
    plan = _plan(workspace, tmp_path, "aio-upgrade", monkeypatch, sites=[site])
    assert [op.identity.step for op in plan.targets[0].operations] == [
        "resolve-aio", "resolve-extensions", "update-extensions", "deploy-release-resources",
    ]
    assert resolve_plan_value(_parameters(plan, "resolve-aio")["aioInstanceName"], {}) == "external-instance"
    update = _parameters(plan, "update-extensions")
    assert resolve_plan_value(update["aioConfigurationOverrides"], {}) == {
        "observability.metrics.exportIntervalSeconds": "60",
    }
    assert resolve_plan_value(update["secretStoreConfigurationOverrides"], {}) == {
        "rotationPollIntervalInSeconds": "240",
    }
    assert resolve_plan_value(update["certManagerConfigurationOverrides"], {}) == {**bundled, replaced: "site"}
    assert resolve_plan_value(update["enableCertManager"], {}) is True


def test_typed_upgrade_reads_the_instance_route_without_identity_prerequisites(workspace, tmp_path, capsys):
    reader, calls = _reader()
    with (
        patch("siteops.cli.new_arm_reader", return_value=reader),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=_session(tmp_path)),
        patch.object(subprocess, "Popen", MagicMock(side_effect=AssertionError("No real tools"))),
    ):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-upgrade", "--input", f"instance={INSTANCE_ID}",
            "--input", "aioRelease=2609", "--input", "enableCertManager=false", "--output", "json",
        ]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in document["plan"]["targets"]] == ["arc-first"]
    assert document["plan"]["manifest"]["targetSelection"] == "explicit-site"
    assert calls == [(INSTANCE_ID, frozenset()), (LOCATION_ID, frozenset()), (CLUSTER_ID, frozenset())]


def test_configured_and_site_file_upgrade_routes_read_no_contract(workspace, tmp_path, capsys):
    site_file = tmp_path / "plant.yaml"
    site_file.write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "plant",
        "subscription": "00000000-0000-0000-0000-000000000001", "resourceGroup": "rg-existing",
        "location": "eastus", "properties": {"aioRelease": "2609", "deployOptions": {"enableCertManager": True}},
        "parameters": {"aioConfigurationOverrides": {"example.setting": "site"}},
    }), encoding="utf-8")
    with (
        patch("siteops.cli.load_contract", side_effect=AssertionError("No contract read")),
        patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")),
    ):
        for selection in (["-l", "name=munich-dev"], ["--site-file", str(site_file)]):
            assert _invoke([
                "-w", str(workspace), "plan", "aio-upgrade", "--describe", *selection, "--output", "json",
            ]) == 0
            document = json.loads(capsys.readouterr().out)
            assert len(document["plan"]["targets"]) == 1


def test_inherited_and_overlaid_site_maps_reach_install_and_upgrade(workspace, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "sites").mkdir(parents=True)
    (project / "sites.local").mkdir()
    (project / "sites" / "fleet.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "SiteTemplate", "name": "fleet",
        "properties": {"aioRelease": "2609", "deployOptions": {
            "enableGlobalSite": False, "enableEdgeSite": False, "enableSecretSync": False,
            "enableCertManager": True,
        }},
        "parameters": {
            "aioConfigurationOverrides": {"shared": "fleet", "replaced": "fleet"},
            "secretStoreConfigurationOverrides": {"rotationPollIntervalInSeconds": "120"},
            "certManagerConfigurationOverrides": {"trust-manager.secretTargets.enabled": "true"},
        },
    }), encoding="utf-8")
    (project / "sites" / "plant.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "plant", "inherits": "fleet.yaml",
        "subscription": "00000000-0000-0000-0000-000000000001", "resourceGroup": "rg-existing",
        "location": "eastus", "parameters": {
            "clusterName": "existing-arc", "aioConfigurationOverrides": {"replaced": "site", "added": "site"},
        },
    }), encoding="utf-8")
    (project / "sites.local" / "plant.yaml").write_text(yaml.safe_dump({
        "parameters": {"secretStoreConfigurationOverrides": {"rotationPollIntervalInSeconds": "240"}},
    }), encoding="utf-8")
    aio = {"shared": "fleet", "replaced": "site", "added": "site"}
    cert_manager = {**_release(workspace, "2609")["certManagerConfigurationOverrides"],
                    "trust-manager.secretTargets.enabled": "true"}
    secret_store = {"rotationPollIntervalInSeconds": "240"}
    install = _plan(workspace, tmp_path, "aio-install", monkeypatch, selector="name=plant", site_config_root=project)
    assert resolve_plan_value(_parameters(install, "aio-instance")["aioConfigurationOverrides"], {}) == aio
    enablement = _parameters(install, "aio-enablement")
    assert resolve_plan_value(enablement["certManagerConfigurationOverrides"], {}) == cert_manager
    assert resolve_plan_value(enablement["secretStoreConfigurationOverrides"], {}) == secret_store
    upgrade = _plan(workspace, tmp_path, "aio-upgrade", monkeypatch, selector="name=plant", site_config_root=project)
    update = _parameters(upgrade, "update-extensions")
    assert resolve_plan_value(update["aioConfigurationOverrides"], {}) == aio
    assert resolve_plan_value(update["certManagerConfigurationOverrides"], {}) == cert_manager
    assert resolve_plan_value(update["secretStoreConfigurationOverrides"], {}) == secret_store


_ROOT = Path(__file__).resolve().parents[2]
_DOCUMENTS = (
    "docs/guided-inputs.md",
    "workspaces/iot-operations/manifests/aio-install/README.md",
    "workspaces/iot-operations/manifests/aio-upgrade/README.md",
)


@pytest.mark.parametrize("document", ["docs/guided-inputs.md", "workspaces/iot-operations/manifests/aio-upgrade/README.md"])
def test_documented_upgrade_commands_resolve_through_the_cli(workspace, tmp_path, monkeypatch, capsys, document):
    for name in ("CI", "GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "yes\n")
    commands = [
        line for line in (_ROOT / document).read_text(encoding="utf-8").splitlines()
        if line.startswith("siteops ") and " aio-upgrade " in line and "<AIO-instance-resource-ID>" in line
    ]
    assert commands
    for line in commands:
        reader, calls = _reader()
        with (
            patch("siteops.cli.open_command_context", side_effect=lambda **kwargs: nullcontext(
                CommandContext(workspace, workspace),
            )),
            patch("siteops.cli.new_arm_reader", return_value=reader),
            patch("siteops.orchestrator.TemplateCompilationSession", return_value=_session(tmp_path)),
            patch.object(subprocess, "Popen", MagicMock(side_effect=AssertionError("No real tools"))),
            patch.object(Orchestrator, "execute_plan", return_value=RunResult.from_sites((), elapsed=0)) as execute,
        ):
            assert _invoke(shlex.split(line.replace("<AIO-instance-resource-ID>", INSTANCE_ID))[1:]) == 0
        plan = execute.call_args.args[0].plan
        assert [op.identity.step for op in plan.targets[0].operations][1:3] == [
            "resolve-extensions", "update-extensions",
        ]
        assert [resource for resource, _ in calls] == [INSTANCE_ID, LOCATION_ID, CLUSTER_ID]
        capsys.readouterr()


def _answer_examples(document: str) -> list[dict]:
    text = (_ROOT / document).read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    return [yaml.safe_load(block) for block in blocks if "kind: SiteInputValues" in block]


@pytest.mark.parametrize("document", _DOCUMENTS)
def test_documented_answer_files_with_settings_bind_to_their_contract(workspace, tmp_path, document):
    examples = [example for example in _answer_examples(document) if set(MAPS) & set(example["values"])]
    assert examples
    for number, example in enumerate(examples):
        manifest = "aio-upgrade" if "instance" in example["values"] else "aio-install"
        values = {
            name: (INSTANCE_ID if name == "instance" else CLUSTER_ID.replace("rg-first", "rg-second"))
            if name in {"instance", "cluster"} else value
            for name, value in example["values"].items()
        }
        path = tmp_path / f"answers-{number}.yaml"
        path.write_text(yaml.safe_dump({**example, "values": values}), encoding="utf-8")
        bound = load_contract(workspace / "manifests" / manifest / "manifest.yaml").bind(values_file=path)
        for name in set(MAPS) & set(values):
            assert dict(bound.active_values[name]) == values[name]
            assert all(isinstance(value, str) for value in values[name].values())

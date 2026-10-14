"""Release behavior that differs across AIO versions and API generations."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from siteops.compilation import TemplateCompilationSession
from siteops.models import Site
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    PlanDisposition,
    PlanIntent,
    resolve_plan_value,
)
from tests.test_orchestrator_planning import _arm_template, _RecordingToolRunner


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


def _release(workspace: Path, release: str) -> dict:
    path = workspace / "parameters" / "aio-releases" / f"{release}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_aio_install_forwards_site_extension_settings_over_release_values(workspace, tmp_path, monkeypatch):
    overrides = {
        "aioConfigurationOverrides": {"example.setting": "site-aio"},
        "certManagerConfigurationOverrides": {"trust-manager.secretTargets.enabled": "true"},
        "secretStoreConfigurationOverrides": {"rotationPollIntervalInSeconds": "240"},
    }
    site = Site(
        name="settings", subscription="sub", resource_group="rg", location="eastus",
        properties={"aioRelease": "2608", "deployOptions": {
            "enableGlobalSite": False, "enableEdgeSite": False, "enableSecretSync": False, "enableCertManager": True,
        }},
        parameters={"clusterName": "cluster", **overrides},
    )
    runner = _RecordingToolRunner()

    def compile_locally(argv, timeout):
        result = runner(argv, timeout)
        if argv[1:3] == ("bicep", "build"):
            source = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
            declarations = re.findall(r"^param\s+(\w+)\s+(\S+)", source, re.MULTILINE)
            parameters = {
                name: {"type": kind if kind in {"string", "bool", "int", "array", "object"} else "object",
                       "defaultValue": None}
                for name, kind in declarations
            }
            Path(argv[argv.index("--outfile") + 1]).write_text(json.dumps(_arm_template(parameters)), encoding="utf-8")
        return result

    session = TemplateCompilationSession(command_runner=compile_locally, tool_resolver=lambda name: str(tmp_path / name))
    monkeypatch.setattr("siteops.orchestrator.TemplateCompilationSession", lambda: session)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=AssertionError("No real tools")))
    result = Orchestrator(workspace).build_plan(
        workspace / "manifests" / "aio-install" / "manifest.yaml", sites=[site], intent=PlanIntent.EXECUTABLE,
    )
    assert result.executable, result.diagnostics
    operations = {op.identity.step: op for op in result.plan.targets[0].operations}
    release = _release(workspace, "2608")
    for step, keys in (
        ("aio-instance", ("aioConfigurationOverrides",)),
        ("aio-enablement", ("certManagerConfigurationOverrides", "secretStoreConfigurationOverrides")),
    ):
        operation = operations[step]
        assert operation.disposition is PlanDisposition.EXECUTE
        assert isinstance(operation.details, DeploymentOperation)
        values = {entry.key.value: entry.value for entry in operation.details.parameters.entries
                  if isinstance(entry.key, LiteralValue)}
        for key in keys:
            expected = {**release.get(key, {}), **overrides[key]}
            assert key in values
            assert resolve_plan_value(values[key], {}) == expected


def test_schema_registry_namespace_fits_the_service_limit_for_every_guided_site_name(workspace):
    # DeviceRegistry rejects a schema registry namespace longer than 32 characters at preflight.
    template = _read(workspace, "templates/deps/schema-registry.bicep")
    contract = yaml.safe_load(_read(workspace, "manifests/aio-install/inputs.yaml"))
    site = next(item for item in contract["inputs"] if item["name"] == "siteName")
    common = yaml.safe_load(_read(workspace, "parameters/common/common.yaml"))
    suffix = common["schemaRegistryName"].removeprefix("{{ site.name }}")
    # The longest guided Site name produces a registry name over the limit, so the long form is reachable.
    assert site["maxLength"] + len(suffix) > 32
    assert re.search(r"(?m)^\s*namespace: schemaRegistryNamespace$", template)
    assert not re.search(r"(?m)^\s*namespace: schemaRegistryName$", template)
    # A name that fits stays unchanged, so existing registries keep their namespace.
    assert "length(schemaRegistryName) <= 32\n  ? schemaRegistryName" in template
    long_form = "'${toLower(take(schemaRegistryName, 26))}${take(uniqueString(resourceGroup().id, schemaRegistryName), 6)}'"
    assert long_form in template
    assert 26 + 6 == 32


def test_security_pki_defaults_follow_released_aio_behavior(workspace):
    create_2025 = _read(
        workspace,
        "templates/aio/modules/instance-2025-10-01.bicep",
    )
    create_2026_03 = _read(
        workspace,
        "templates/aio/modules/instance-2026-03-01.bicep",
    )
    create_2026_07 = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    application_uri = "connectors.values.securityPki.applicationUri"
    subject_name = "connectors.values.securityPki.subjectName"

    # The application URI starts with the 2026-03-01 generation. Subject name
    # starts in releases 2605 and 2606, then becomes a 2026-07-01 baseline.
    assert application_uri not in create_2025
    assert subject_name not in create_2025
    assert application_uri in create_2026_03
    assert subject_name in create_2026_03
    assert (
        "var configureSecurityPkiSubjectName = "
        "bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in create_2026_03
    )
    assert application_uri in create_2026_07
    assert subject_name in create_2026_07
    assert "deriveAioExtensionSuffix(clusterResourceId)" in create_2026_03

    for release in ("2603", "2604"):
        assert _release(workspace, release)["aioReleaseConfiguration"] == {}
    for release in ("2605", "2606"):
        extension = _release(workspace, release)["aioReleaseConfiguration"]["extension"]
        assert extension["securityPkiSubjectName"] is True
    for release in ("2607", "2608"):
        extension = (
            _release(workspace, release)["aioReleaseConfiguration"].get("extension", {})
        )
        assert "securityPkiSubjectName" not in extension

    assert (
        "var aioApplicationUriDefault = aioApiVersion == '2025-10-01'\n  ? {}"
        in update_extensions
    )
    assert (
        "var configureSecurityPkiSubjectName = aioApiVersion == '2026-07-01' "
        "|| bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in update_extensions
    )
    assert application_uri in update_extensions
    assert subject_name in update_extensions
    assert "contains(aio.configurationSettings" in update_extensions
    assert "aio.extensionSuffix" in update_extensions


def test_release_contract_does_not_opt_in_to_gds_manager(workspace):
    release_files = sorted(
        (workspace / "parameters" / "aio-releases").glob("*.yaml")
    )
    instance_modules = sorted(
        (workspace / "templates" / "aio" / "modules").glob("instance-*.bicep")
    )
    checked = release_files + instance_modules + [
        workspace / "templates" / "aio" / "upgrade" / "update-extensions.bicep"
    ]
    assert release_files and instance_modules

    for path in checked:
        assert "gdsManager" not in path.read_text(encoding="utf-8"), (
            f"{path}: Scale Kit configures published release requirements and "
            "does not opt in to GDS Manager"
        )

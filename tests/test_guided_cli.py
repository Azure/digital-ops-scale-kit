"""Exercise guided target selection through the public command path."""

import json
import shlex
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from siteops.arm_resources import ArmResourceError, ArmResourceObservation
from siteops.cli import main
from siteops.command_context import CommandContext
from siteops.compilation import TemplateCompilationSession
from siteops.guided_inputs import contract_path, load_contract
from siteops.models import Site
from siteops.orchestrator import Orchestrator
from siteops.package_builder import _source_files
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    OutputValue,
    PlanDisposition,
    PlanIntent,
    PlanStatus,
    resolve_plan_value,
)
from siteops.results import RunResult


@pytest.fixture
def guided_workspace(complete_workspace):
    contract = {
        "apiVersion": "siteops.inputs/v1",
        "kind": "SiteInputContract",
        "inputs": [
            {
                "name": "siteName",
                "type": "string",
                "description": "Site identity.",
                "sitePath": "name",
            },
            {
                "name": "subscription",
                "type": "string",
                "description": "Subscription identity.",
                "sitePath": "subscription",
            },
            {
                "name": "location",
                "type": "string",
                "description": "Deployment region.",
                "sitePath": "location",
            },
        ],
    }
    path = contract_path(complete_workspace / "manifests" / "test-manifest.yaml")
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return complete_workspace


def _invoke(argv):
    with patch.object(sys, "argv", ["siteops", *argv]):
        with pytest.raises(SystemExit) as stopped:
            main()
    return stopped.value.code


@pytest.mark.parametrize("output", ["plain", "json"])
def test_input_inspection_reads_the_manifest_header_in_every_format(
    guided_workspace, tmp_path, capsys, output,
):
    manifest = guided_workspace / "manifests" / "test-manifest.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["steps"].append({"include": "missing-partial.yaml"})
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    selection = ["-w", str(guided_workspace), "inputs", str(manifest), "--output", output]
    assert _invoke(selection) == 0
    inspected = capsys.readouterr().out
    if output == "json":
        assert [field["name"] for field in json.loads(inspected)["inputs"]] == [
            "siteName", "subscription", "location",
        ]
    else:
        assert inspected.startswith(f"Inputs for {document['name']}:\n")
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([*selection, "--input-file", str(answers)]) == 1
    assert "missing-partial.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("redacted", [False, True])
def test_unknown_input_is_named_with_the_nearest_input_unless_redacted(
    guided_workspace, capsys, monkeypatch, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace), "--input", "locaton=eastus",
    ]) == 1
    error = capsys.readouterr().err
    assert error.startswith("Error: Inline inputs contain an unknown input")
    assert ("'locaton'. Did you mean 'location'?" in error) is not redacted


def test_example_onto_an_existing_file_reports_a_plain_message(
    guided_workspace, tmp_path, capsys, monkeypatch,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    target = tmp_path / "answers.yaml"
    target.write_text("keep\n", encoding="utf-8")
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace), "--example", str(target),
    ]) == 1
    error = capsys.readouterr().err
    assert error == f"Error: {target} already exists. Choose a new file name.\n"
    assert target.read_text(encoding="utf-8") == "keep\n"


def _interactive_console(monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "yes\n")


def _manifest(workspace: Path) -> str:
    return str(workspace / "manifests" / "test-manifest.yaml")


def _input_file(path: Path, *, name: str = "one") -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops.inputs/v1",
                "kind": "SiteInputValues",
                "values": {
                    "siteName": name,
                    "subscription": "00000000-0000-0000-0000-000000000001",
                    "location": "eastus",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


_CLUSTER_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/"
    "resourceGroups/rg-first/providers/Microsoft.Kubernetes/"
    "connectedClusters/arc-first"
)


def _resource_workspace(workspace: Path) -> Path:
    path = contract_path(workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["inputs"].extend([
        {"name": "resourceGroup", "type": "string", "description": "RG.", "sitePath": "resourceGroup"},
        {"name": "clusterName", "type": "string", "description": "Arc name.",
         "sitePath": "parameters.clusterName"},
        {
            "name": "cluster", "type": "azureResourceId", "required": False,
            "description": "Existing cluster.",
            "resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                         "apiVersion": "2024-07-15-preview"},
            "derive": {"subscription": "subscription", "resourceGroup": "resourceGroup",
                       "location": "location", "name": "clusterName"},
        },
    ])
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return workspace


def _resource_answers(path: Path) -> Path:
    path.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
        "values": {"siteName": "one", "subscription": None, "location": None,
                   "resourceGroup": None, "clusterName": None, "cluster": _CLUSTER_ID},
    }), encoding="utf-8")
    return path


def test_top_level_help_leads_with_single_site_answers(capsys):
    with patch.object(sys, "argv", ["siteops", "--help"]):
        with pytest.raises(SystemExit) as stopped:
            main()
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert help_text.index("siteops source enroll official") < help_text.index(
        'deploy aio-install --source "official@<release>"'
    )
    assert help_text.index('deploy aio-install --source "official@<release>"') < help_text.index(
        "plan aio-install -l name=plant-two,name=plant-three"
    )
    for verb in ("plan", "deploy"):
        example = next(
            line for line in help_text.splitlines()
            if f" {verb} aio-install " in line and "--input" in line
        )
        assert "--read-resources" not in example
        assert '--input "cluster=<Arc-cluster-resource-ID>"' in example
        assert "--input-file" not in example
    assert "--project ./factory plan aio-install -l name=plant-two,name=plant-three" in help_text


def test_inputs_help_explains_read_only_preview(capsys):
    with patch.object(sys, "argv", ["siteops", "inputs", "--help"]):
        with pytest.raises(SystemExit) as stopped:
            main()
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "preview" in help_text.lower()
    assert "without writing" in help_text.lower()
    assert "--save-site" in help_text


def test_resource_read_consent_matches_command_purpose(capsys):
    for command in ("inputs", "plan", "deploy"):
        with patch.object(sys, "argv", ["siteops", command, "--help"]):
            with pytest.raises(SystemExit) as stopped:
                main()
        assert stopped.value.code == 0
        assert ("--read-resources" in capsys.readouterr().out) is (command == "inputs")
    with patch.object(sys, "argv", ["siteops", "validate", "--help"]):
        with pytest.raises(SystemExit) as stopped:
            main()
    assert "--read-resources" not in capsys.readouterr().out


@pytest.mark.parametrize(("command", "option"), [
    ("inputs", "--example"),
    ("inputs", "--input-file"),
    ("inputs", "--save-site"),
    ("plan", "--input-file"),
    ("plan", "--site-file"),
    ("deploy", "--input-file"),
    ("deploy", "--site-file"),
    ("validate", "--input-file"),
    ("validate", "--site-file"),
])
def test_single_file_options_reject_repetition_before_content_use(
    guided_workspace, tmp_path, capsys, command, option,
):
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    with (
        patch("siteops.cli.resolve_manifest_path", side_effect=AssertionError("No content read")),
        patch("siteops.cli.write_yaml_exclusive", side_effect=AssertionError("No file write")),
    ):
        assert _invoke([
            "-w", str(guided_workspace), command, _manifest(guided_workspace),
            option, str(first), option, str(second),
        ]) == 2
    output = capsys.readouterr()
    assert option in output.err
    assert "only once" in output.err
    assert str(first) not in output.err and str(second) not in output.err
    assert not first.exists() and not second.exists()


def test_inputs_inspection_and_example_are_not_executable(
    guided_workspace, tmp_path, capsys,
):
    example = tmp_path / "answers.yaml"
    args = [
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(example), "--output", "json",
    ]
    assert _invoke(args) == 0
    document = json.loads(capsys.readouterr().out)
    assert {item["name"] for item in document["inputs"]} == {
        "siteName", "subscription", "location",
    }
    assert yaml.safe_load(example.read_text(encoding="utf-8"))["values"] == {
        "siteName": None,
        "subscription": None,
        "location": None,
    }
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(example),
    ]) == 1
    assert "required" in capsys.readouterr().out.lower()
    assert _invoke(args) == 1
    assert "exists" in capsys.readouterr().err.lower()


def test_aio_example_exposes_resource_first_route_without_a_read(tmp_path, capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    example = tmp_path / "aio-inputs.yaml"
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")):
        assert _invoke([
            "-w", str(workspace), "inputs", "aio-install", "--example", str(example),
        ]) == 0
    plain = capsys.readouterr().out
    assert "Resource route:" in plain and "--read-resources" in plain
    assert plain.index("Resource route:") < plain.index("  siteName")
    values = yaml.safe_load(example.read_text(encoding="utf-8"))["values"]
    assert values == {"cluster": None}
    assert all(value is None for value in values.values())
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install",
            "--describe", "--input-file", str(example),
        ]) == 1
    assert "required" in capsys.readouterr().out.lower()


def test_inputs_preview_from_complete_answers_is_read_only(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    original = answers.read_bytes()
    with patch(
        "siteops.cli.write_yaml_exclusive",
        side_effect=AssertionError("Inspection cannot write files."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
            "--input-file", str(answers), "--input", "siteName=preview",
            "--output", "json",
        ]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["resolution"]["status"] == "ready"
    assert document["resolution"]["site"]["name"] == "preview"
    assert document["resolution"]["site"]["location"] == "eastus"
    assert answers.read_bytes() == original
    assert not (guided_workspace / "sites" / "preview.yaml").exists()


def test_inputs_preview_redacts_site_in_automation(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml", name="PRIVATE_TARGET_NAME")
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--output", "json",
    ]) == 0

    output = capsys.readouterr()
    document = json.loads(output.out)
    assert document["resolution"] == {"status": "ready", "site": None}
    assert "PRIVATE_TARGET_NAME" not in output.out + output.err
    assert "00000000-0000-0000-0000-000000000001" not in output.out + output.err


def test_resource_plan_reads_only_selected_role_before_planning(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    calls = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            calls.append((ref.resource_id, facts))
            return ArmResourceObservation(
                _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters",
                "eastus", "arc-first", {},
            )

    with patch("siteops.cli.new_arm_reader", return_value=Reader()):
        assert _invoke([
            "-w", str(workspace), "plan", _manifest(workspace),
            "--describe", "--input-file", str(answers),
             "--output", "json",
        ]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in document["plan"]["targets"]] == ["one"]
    assert calls == [(_CLUSTER_ID, frozenset())]


def test_invalid_manifest_fails_before_any_resource_read(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    (workspace / "manifests" / "test-manifest.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nsteps: [\n",
        encoding="utf-8",
    )
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")):
        assert _invoke([
            "-w", str(workspace), "plan", _manifest(workspace),
            "--describe", "--input-file", str(answers),
             "--output", "json",
        ]) == 1
    assert "invalid" in capsys.readouterr().out


def test_input_inspection_without_read_consent_cannot_use_manual_fallback(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _input_file(tmp_path / "answers.yaml")
    data = yaml.safe_load(answers.read_text(encoding="utf-8"))
    data["values"].update(
        resourceGroup="rg-first", clusterName="arc-first", cluster=_CLUSTER_ID,
    )
    answers.write_text(yaml.safe_dump(data), encoding="utf-8")
    with patch(
        "siteops.cli.new_arm_reader",
        side_effect=AssertionError("No ARM reader may be created."),
    ):
        assert _invoke([
            "-w", str(workspace), "inputs", _manifest(workspace),
            "--input-file", str(answers), "--output", "json",
        ]) == 1
    error = capsys.readouterr().err
    assert "inputs --read-resources" in error
    assert "before planning" not in error


def test_validate_resource_inputs_points_to_an_actual_read_command(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    with patch(
        "siteops.cli.new_arm_reader",
        side_effect=AssertionError("validate must not read Azure"),
    ):
        assert _invoke([
            "-w", str(workspace), "validate", _manifest(workspace),
            "--input-file", str(answers),
        ]) == 1
    error = capsys.readouterr().err
    assert "siteops plan" in error
    assert "--describe" in error and "--read-resources" not in error


def test_read_flag_requires_a_typed_resource_target(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    with patch(
        "siteops.cli.new_arm_reader",
        side_effect=AssertionError("No reader may be constructed."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
            "--read-resources", "--input-file", str(answers),
            "--output", "json",
        ]) == 1
    assert "inputs.resource.nothing-to-read" in capsys.readouterr().err


def test_read_flag_alone_never_selects_manifest_fleet(
    guided_workspace, capsys,
):
    with patch.object(
        Orchestrator, "build_plan",
        side_effect=AssertionError("No fleet plan may be built."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
            "--read-resources", "--output", "json",
        ]) == 1
    assert "inputs.resource.nothing-to-read" in capsys.readouterr().err


def test_redacted_resource_failure_does_not_reveal_id_or_target(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            raise ArmResourceError("FORBIDDEN")

    with patch("siteops.cli.new_arm_reader", return_value=Reader()):
        assert _invoke([
            "-w", str(workspace), "plan", _manifest(workspace),
            "--describe", "--input-file", str(answers),
             "--output", "json",
        ]) == 1
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert document["diagnostics"][0]["code"] == "inputs.resource.forbidden"
    assert _CLUSTER_ID not in output.out + output.err
    assert "00000000-0000-0000-0000-000000000001" not in output.out + output.err
    assert "rg-first" not in output.out + output.err


@pytest.mark.parametrize("command", ["inputs", "plan", "deploy"])
@pytest.mark.parametrize(
    ("read_error", "expected_exit"), [("CANCELLED", 130), ("FORBIDDEN", 1)],
)
def test_guided_resource_read_failure_stops_before_plan_or_execution(
    guided_workspace, tmp_path, capsys, command, read_error, expected_exit,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    reads = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            reads.append(ref.resource_id)
            raise ArmResourceError(read_error)

    args = [
        "-w", str(workspace), command, _manifest(workspace),
        "--input-file", str(answers), "--output", "json",
    ]
    if command == "inputs":
        args.append("--read-resources")
    if command == "deploy":
        args.append("--yes")
    if command == "plan":
        args.append("--describe")
    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")),
        patch.object(Orchestrator, "execute_plan", side_effect=AssertionError("No execution")),
        patch("siteops.cli.write_yaml_exclusive", side_effect=AssertionError("No write")),
    ):
        assert _invoke(args) == expected_exit
    assert reads == [_CLUSTER_ID]
    output = capsys.readouterr()
    assert _CLUSTER_ID not in output.out + output.err
    code = f"inputs.resource.{read_error.lower()}"
    if command == "inputs":
        assert not output.out
        assert code in output.err
    else:
        result = json.loads(output.out)
        assert result["diagnostics"][0]["code"] == code
        if command == "deploy":
            assert result["status"] == ("cancelled" if read_error == "CANCELLED" else "invalid")
            assert result["summary"]["interrupted"] is (read_error == "CANCELLED")
            assert result["exitCode"] == expected_exit
            assert result["sites"] == []
        else:
            assert result["status"] == "invalid"


@pytest.mark.parametrize("command", ["inputs", "plan", "deploy"])
@pytest.mark.parametrize("output", ["plain", "json"])
@pytest.mark.parametrize(
    ("read_error", "remedy"),
    [
        ("TOOL_MISSING", "https://aka.ms/installazurecli"),
        ("NOT_LOGGED_IN", "Run `az login`"),
        ("FORBIDDEN", "read access"),
        ("SUBSCRIPTION_MISSING", "not visible to the account signed in to Azure CLI"),
    ],
)
def test_resource_read_failure_shows_its_remedy(
    guided_workspace, tmp_path, capsys, command, output, read_error, remedy,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            raise ArmResourceError(read_error)

    args = ["-w", str(workspace), command, _manifest(workspace), "--input-file", str(answers)]
    if output == "json":
        args.extend(["--output", "json"])
    if command == "inputs":
        args.append("--read-resources")
    if command == "deploy":
        args.append("--yes")
    if command == "plan":
        args.append("--describe")
    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")),
        patch.object(Orchestrator, "execute_plan", side_effect=AssertionError("No execution")),
    ):
        assert _invoke(args) == 1
    captured = capsys.readouterr()
    assert f"inputs.resource.{read_error.lower().replace('_', '-')}" in captured.out + captured.err
    assert remedy in captured.out + captured.err
    assert _CLUSTER_ID not in captured.out + captured.err


def test_cancelled_reader_setup_is_not_reported_as_provider_unavailable(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    with (
        patch("siteops.cli.new_arm_reader", side_effect=ArmResourceError("CANCELLED")),
        patch.object(Orchestrator, "execute_plan", side_effect=AssertionError("No execution")),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", "--yes", _manifest(workspace),
            "--input-file", str(answers),  "--output", "json",
        ]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "cancelled"
    assert result["diagnostics"][0]["code"] == "inputs.resource.cancelled"


def test_incomplete_input_preview_fails_without_writing(
    guided_workspace, tmp_path, capsys,
):
    site_file = tmp_path / "one.yaml"
    with patch(
        "siteops.cli.write_yaml_exclusive",
        side_effect=AssertionError("An incomplete Site must not be saved."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
            "--input", "siteName=one", "--output", "json",
        ]) == 1
        assert _invoke([
            "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
            "--input", "siteName=one", "--save-site", str(site_file),
        ]) == 1
    assert not site_file.exists()
    assert "subscription" in capsys.readouterr().err


def test_plain_input_inspection_escapes_authored_terminal_controls(
    guided_workspace, capsys,
):
    path = contract_path(guided_workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["inputs"][0]["description"] = "A name.\x1b[31m"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
    ]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert r"\u001b" in output


def test_plain_input_inspection_wraps_long_authored_descriptions(guided_workspace, capsys):
    path = contract_path(guided_workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["inputs"][0]["description"] = (
        "A long operator explanation about selecting the intended Site name "
        "and checking its identity before any deployment is attempted."
    )
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
    ]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "selecting the intended Site name" in " ".join(lines)
    assert max(map(len, lines)) <= 72


def test_input_file_and_inline_override_manifest_fleet_target(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--input-file", str(answers), "--input", "siteName=two",
        "--describe", "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["two"]
    assert result["plan"]["manifest"]["targetSelection"] == "explicit-site"


def test_inline_answers_can_complete_generated_example_file(
    guided_workspace, tmp_path, capsys,
):
    example = tmp_path / "example.yaml"
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(example),
    ]) == 0
    capsys.readouterr()

    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(example),
        "--input", "siteName=one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "location=eastus",
        "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["one"]


def test_deploy_passes_one_explicit_site_to_existing_executor_path(
    guided_workspace, tmp_path,
):
    answers = _input_file(tmp_path / "answers.yaml")
    with (
        patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
        patch.object(
            Orchestrator, "execute_plan", return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "deploy", "--yes", _manifest(guided_workspace),
            "--input-file", str(answers),
        ]) == 0

    deploy.assert_called_once()
    assert build.call_args.kwargs["selector"] is None
    sites = build.call_args.kwargs["sites"]
    assert len(sites) == 1
    assert isinstance(sites[0], Site)
    assert sites[0].name == "one"


def test_complete_standalone_site_replaces_manifest_target(
    guided_workspace, tmp_path, capsys,
):
    standalone = tmp_path / "standalone.yaml"
    standalone.write_text(
        yaml.safe_dump({
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "explicit",
            "subscription": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }),
        encoding="utf-8",
    )
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--site-file", str(standalone), "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == ["explicit"]


def test_validate_checks_explicit_site_without_configured_selection(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "validate", _manifest(guided_workspace),
        "--input-file", str(answers),
    ]) == 0
    assert "Manifest is valid" in capsys.readouterr().out


def _with_site_defaults(workspace: Path, defaults: dict) -> Path:
    path = contract_path(workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["siteDefaults"] = defaults
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return workspace


def _explicit_route(route: str, workspace: Path, tmp_path: Path) -> list[str]:
    """Supply Site 'one', labeled environment=dev, through one explicit route."""
    if route == "site-file":
        site_file = tmp_path / "one.yaml"
        site_file.write_text(yaml.safe_dump({
            "apiVersion": "siteops/v1", "kind": "Site", "name": "one",
            "subscription": "00000000-0000-0000-0000-000000000001", "location": "eastus",
            "labels": {"environment": "dev"},
        }), encoding="utf-8")
        return ["--site-file", str(site_file)]
    _with_site_defaults(workspace, {"labels": {"environment": "dev"}})
    if route == "input-file":
        return ["--input-file", str(_input_file(tmp_path / "answers.yaml"))]
    return [
        "--input", "siteName=one", "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "location=eastus",
    ]


def _command_args(command: str) -> list[str]:
    return {"plan": ["--describe"], "deploy": ["--yes"], "validate": []}[command]


@pytest.mark.parametrize("command", ["plan", "deploy", "validate"])
@pytest.mark.parametrize("route", ["site-file", "input-file", "input"])
def test_label_requirement_admits_a_matching_explicit_site(
    guided_workspace, tmp_path, capsys, command, route,
):
    explicit = _explicit_route(route, guided_workspace, tmp_path)
    with (
        patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
        patch.object(Orchestrator, "execute_plan", return_value=SimpleNamespace(exit_code=0)) as execute,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(guided_workspace), command, _manifest(guided_workspace),
            *_command_args(command), *explicit, "-l", "environment=dev", "-l", "name=two,name=one",
        ]) == 0
    output = capsys.readouterr()
    if command == "validate":
        assert "Manifest is valid" in output.out
        return
    assert [site.name for site in build.call_args.kwargs["sites"]] == ["one"]
    assert execute.called is (command == "deploy")


@pytest.mark.parametrize("command", ["plan", "deploy", "validate"])
@pytest.mark.parametrize("route", ["site-file", "input-file", "input"])
@pytest.mark.parametrize(("selectors", "message"), [
    (["environment=prod"], "Site 'one' does not match -l environment=prod (its environment label is dev)."),
    (["name=two,name=three"], "Site 'one' does not match -l name=two,name=three."),
    (
        ["environment=dev", "region=eu", "name=one"],
        "Site 'one' does not match -l region=eu (it has no region label).",
    ),
    (
        ["name=two,environment=prod,region=eu"],
        "Site 'one' does not match -l name=two,environment=prod,region=eu "
        "(its environment label is dev, it has no region label).",
    ),
])
def test_label_requirement_rejects_a_mismatch_before_preparation(
    guided_workspace, tmp_path, capsys, monkeypatch, command, route, selectors, message,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    explicit = _explicit_route(route, guided_workspace, tmp_path)
    with (
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No preparation")),
        patch.object(Orchestrator, "validate", side_effect=AssertionError("No preparation")),
        patch.object(Orchestrator, "execute_plan", side_effect=AssertionError("No execution")),
    ):
        assert _invoke([
            "-w", str(guided_workspace), command, _manifest(guided_workspace),
            *_command_args(command), *explicit,
            *[argument for selector in selectors for argument in ("-l", selector)],
        ]) == 1
    output = capsys.readouterr()
    assert message in output.out + output.err
    if command == "deploy":
        assert output.err == f"Error: {message}\n"


@pytest.mark.parametrize("redacted", [False, True])
def test_label_requirement_parses_before_reading_the_site(
    guided_workspace, tmp_path, capsys, monkeypatch, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    with patch("siteops.cli.load_direct_site", side_effect=AssertionError("No Site read")):
        assert _invoke([
            "-w", str(guided_workspace), "deploy", "--yes", _manifest(guided_workspace),
            "--site-file", str(tmp_path / "absent.yaml"), "-l", "environment=dev", "-l", "environment=prod",
        ]) == 1
    error = capsys.readouterr().err
    assert error == (
        "Error: Invalid Site selector.\n" if redacted
        else "Error: Selector key `environment` may only appear once. Selectors AND across keys, "
        "so duplicating a key would always match zero Sites. Only `name=` supports multiple "
        "values (OR-combined).\n"
    )


@pytest.mark.parametrize("command", ["plan", "deploy"])
def test_redacted_label_requirement_mismatch_names_no_site_values(
    guided_workspace, tmp_path, capsys, monkeypatch, command,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    site_file = tmp_path / "private.yaml"
    site_file.write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "PRIVATE_SITE_NAME",
        "subscription": "00000000-0000-0000-0000-000000000001", "location": "eastus",
        "labels": {"environment": "PRIVATE_LABEL_VALUE"},
    }), encoding="utf-8")
    with patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No preparation")):
        assert _invoke([
            "-w", str(guided_workspace), command, _manifest(guided_workspace),
            *_command_args(command), "--site-file", str(site_file),
            "-l", "environment=PRIVATE_SELECTOR_VALUE", "--output", "json",
        ]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["diagnostics"] == [{
        "code": "plan.targeting.conflict",
        "severity": "error",
        "summary": (
            "The Site does not match the -l label requirement. "
            "Check its name and labels against each -l term."
        ),
    }]
    assert "PRIVATE" not in output.out + output.err


@pytest.mark.parametrize(("defaults", "selector"), [
    ({}, "name=two"),
    ({"labels": {"environment": "dev"}}, "environment=prod"),
])
def test_label_requirement_fails_before_any_resource_read(
    guided_workspace, tmp_path, capsys, monkeypatch, defaults, selector,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    workspace = _with_site_defaults(_resource_workspace(guided_workspace), defaults)
    answers = _resource_answers(tmp_path / "answers.yaml")
    with (
        patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No preparation")),
    ):
        assert _invoke([
            "-w", str(workspace), "plan", _manifest(workspace), "--describe",
            "--input-file", str(answers), "-l", selector,
        ]) == 1
    assert "Site 'one' does not match -l " + selector in capsys.readouterr().out


def _region_label_workspace(workspace: Path) -> Path:
    """Derive a `region` label from the cluster's observed location."""
    path = contract_path(workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract["inputs"].extend([
        {"name": "region", "type": "string", "description": "Region label.", "sitePath": "labels.region"},
        {
            "name": "cluster", "type": "azureResourceId", "required": False,
            "description": "Existing cluster.",
            "resource": {"type": "Microsoft.Kubernetes/connectedClusters",
                         "apiVersion": "2024-07-15-preview"},
            "derive": {"location": "region"},
        },
    ])
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return workspace


@pytest.mark.parametrize(("region", "expected"), [("eastus", 0), ("westus", 1)])
def test_label_supplied_by_a_read_is_required_before_preparation(
    guided_workspace, tmp_path, capsys, monkeypatch, region, expected,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    workspace = _region_label_workspace(guided_workspace)
    reads = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            reads.append(ref.resource_id)
            return ArmResourceObservation(
                _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters", "eastus", "arc-first", {},
            )

    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
    ):
        assert _invoke([
            "-w", str(workspace), "plan", _manifest(workspace), "--describe",
            "--input", "siteName=one", "--input", "subscription=00000000-0000-0000-0000-000000000001",
            "--input", "location=eastus", "--input", f"cluster={_CLUSTER_ID}",
            "-l", "name=one", "-l", f"region={region}",
        ]) == expected
    assert reads == [_CLUSTER_ID]
    assert build.called is (expected == 0)
    if expected:
        assert (
            "Site 'one' does not match -l region=westus (its region label is eastus)."
            in capsys.readouterr().out
        )


def test_missing_inputs_have_actionable_machine_diagnostic(
    guided_workspace, capsys,
):
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input", "siteName=one", "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "siteops inputs" in output["diagnostics"][0]["summary"]


def test_ci_missing_input_reports_safe_field_name(
    guided_workspace, monkeypatch, capsys,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input", "siteName=one",
        "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "subscription" in output["diagnostics"][0]["summary"]


def test_ci_deploy_missing_input_reports_safe_field_name(
    guided_workspace, monkeypatch, capsys,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    assert _invoke([
        "-w", str(guided_workspace), "deploy", "--yes", _manifest(guided_workspace),
        "--input", "siteName=one", "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.invalid"
    assert "subscription" in output["diagnostics"][0]["summary"]


@pytest.mark.parametrize("redacted", [False, True])
def test_saved_site_is_normal_config_and_not_overwritten(
    guided_workspace, tmp_path, capsys, monkeypatch, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    answers = _input_file(tmp_path / "answers.yaml")
    site_file = guided_workspace / "sites" / "one.yaml"
    args = [
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(site_file),
    ]
    assert _invoke(args) == 0
    capsys.readouterr()
    site = Site.from_file(site_file)
    assert site.name == "one"
    assert site.location == "eastus"
    saved_bytes = site_file.read_bytes()
    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "-l", "name=one", "--describe",
    ]) == 0
    capsys.readouterr()
    assert _invoke(args) == 1
    error = capsys.readouterr().err.lower()
    assert "already exists" in error
    assert (str(site_file).lower() in error) is not redacted
    assert site_file.read_bytes() == saved_bytes


@pytest.mark.parametrize("project_mode", [False, True])
@pytest.mark.parametrize("redacted", [False, True])
def test_saved_site_in_inventory_requires_discoverable_suffix(
    guided_workspace, tmp_path, capsys, monkeypatch, project_mode, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    project = tmp_path / "operator-project" if project_mode else guided_workspace
    sites_dir = project / "sites"
    sites_dir.mkdir(parents=True, exist_ok=True)
    answers = _input_file(tmp_path / "answers.yaml")
    selected = (["--project", str(project)] if project_mode else []) + [
        "-w", str(guided_workspace),
    ]
    invalid = sites_dir / "one.txt"
    assert _invoke([
        *selected, "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(invalid),
    ]) == 1
    assert not invalid.exists()
    output = capsys.readouterr()
    assert ("Site configuration could not be loaded" if redacted else ".yaml or .yml") in output.err

    valid = sites_dir / "one.yml"
    assert _invoke([
        *selected, "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(valid),
    ]) == 0
    capsys.readouterr()
    assert Site.from_file(valid).name == "one"
    assert _invoke([
        *selected, "plan", _manifest(guided_workspace),
        "-l", "name=one", "--describe",
    ]) == 0


def test_save_site_outside_inventory_keeps_explicit_file_route(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    standalone = tmp_path / "standalone.txt"
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(standalone),
    ]) == 0
    capsys.readouterr()
    assert Site.from_file(standalone).name == "one"


@pytest.mark.parametrize(("name", "filename"), [
    ("taken", "fresh.yaml"),
    ("fresh", "taken.yaml"),
    ("fresh", "existing.yml"),
])
@pytest.mark.parametrize("project_mode", [False, True])
@pytest.mark.parametrize("redacted", [False, True])
def test_save_site_rejects_inventory_identity_collisions_before_writing(
    guided_workspace, tmp_path, capsys, monkeypatch, name, filename, project_mode, redacted,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    root = tmp_path / "factory" if project_mode else guided_workspace
    sites_dir = root / "sites"
    sites_dir.mkdir(exist_ok=True, parents=True)
    existing = sites_dir / "existing.yaml"
    existing.write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "taken",
        "subscription": "00000000-0000-0000-0000-000000000001",
        "location": "eastus",
    }), encoding="utf-8")
    answers = _input_file(tmp_path / "answers.yaml", name=name)
    destination = sites_dir / filename
    selection = (["--project", str(root)] if project_mode else []) + [
        "-w", str(guided_workspace),
    ]
    assert _invoke([
        *selection, "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(destination),
    ]) == 1
    assert not destination.exists()
    output = capsys.readouterr()
    if redacted:
        assert "Site configuration could not be loaded" in output.err
        assert "taken" not in output.out + output.err
        assert str(existing) not in output.out + output.err
    else:
        assert "another configured Site" in output.err
    available = _input_file(tmp_path / "available.yaml", name="available")
    assert _invoke([
        *selection, "inputs", _manifest(guided_workspace),
        "--input-file", str(available), "--save-site", str(sites_dir / "available.yaml"),
    ]) == 0
    capsys.readouterr()
    assert _invoke([
        *selection, "plan", _manifest(guided_workspace), "-l", "name=available", "--describe",
    ]) == 0


def test_save_site_rejects_relative_path_matching_existing_internal_name(
    guided_workspace, tmp_path, capsys,
):
    sites_dir = guided_workspace / "sites"
    (sites_dir / "regions" / "eu").mkdir(parents=True)
    (sites_dir / "existing.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "regions/eu/plant",
        "subscription": "00000000-0000-0000-0000-000000000001",
        "location": "eastus",
    }), encoding="utf-8")
    destination = sites_dir / "regions" / "eu" / "plant.yaml"
    answers = _input_file(tmp_path / "answers.yaml", name="plant-new")
    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(destination),
    ]) == 1
    assert not destination.exists()
    assert "another configured Site" in capsys.readouterr().err


def test_guided_secret_sync_does_not_carry_into_fresh_fleet_sites(tmp_path, capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    project = tmp_path / "factory"
    (project / "sites").mkdir(parents=True)
    answers = tmp_path / "aio-inputs.yaml"
    vault_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-vault/providers/Microsoft.KeyVault/vaults/vault-example"
    )
    answers.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
        "values": {
            "siteName": "plant-one", "subscription": None, "resourceGroup": None,
            "location": None, "clusterName": None, "environment": "dev", "country": "US",
            "cluster": _CLUSTER_ID, "enableSecretSync": True, "existingVault": vault_id,
        },
    }), encoding="utf-8")
    fleet_answers = tmp_path / "fleet-inputs.yaml"
    fleet_answers.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
        "values": {"environment": "dev", "country": "US", "enableSecretSync": False},
    }), encoding="utf-8")
    second_id = _CLUSTER_ID.replace("rg-first", "rg-second").replace("arc-first", "arc-second")
    third_id = _CLUSTER_ID.replace("rg-first", "rg-third").replace("arc-first", "arc-third")
    fourth_id = _CLUSTER_ID.replace("rg-first", "rg-fourth").replace("arc-first", "arc-fourth")
    calls = []
    sync_facts = frozenset({
        "connectedClusters.workloadIdentityEnabled",
        "connectedClusters.oidcIssuerAvailable",
    })

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            calls.append((ref.resource_id, facts))
            assert ref.resource_id in {_CLUSTER_ID, second_id, third_id, fourth_id, vault_id}
            return ArmResourceObservation(
                ref.resource_id,
                "Microsoft.KeyVault/vaults" if ref.resource_id == vault_id
                else "Microsoft.Kubernetes/connectedClusters",
                "eastus", ref.resource_id.rsplit("/", 1)[-1],
                {key: True for key in facts},
            )

    with patch("siteops.cli.new_arm_reader", return_value=Reader()):
        for name, resource in (
            ("plant-one", _CLUSTER_ID), ("plant-two", second_id), ("plant-three", third_id),
            ("plant-four", fourth_id),
        ):
            command = [
                "--project", str(project), "-w", str(workspace),
                "inputs", "aio-install",
                "--input-file", str(answers if name == "plant-one" else fleet_answers),
                "--read-resources", "--save-site", str(project / "sites" / f"{name}.yaml"),
            ]
            if name != "plant-one":
                command.extend([
                    "--input", f"siteName={name}", "--input", f"cluster={resource}",
                ])
            assert _invoke(command) == 0
            capsys.readouterr()

    assert calls == [
        (_CLUSTER_ID, sync_facts), (vault_id, frozenset()),
        (second_id, frozenset()), (third_id, frozenset()),
        (fourth_id, frozenset()),
    ]
    for name, cluster in (
        ("plant-one", "arc-first"), ("plant-two", "arc-second"), ("plant-three", "arc-third"),
        ("plant-four", "arc-fourth"),
    ):
        site = Site.from_file(project / "sites" / f"{name}.yaml")
        assert site.name == name
        assert site.labels["environment"] == "dev"
        assert site.parameters["clusterName"] == cluster
        assert site.properties["deployOptions"]["enableSecretSync"] is (name == "plant-one")
        assert ("existingKeyVaultResourceId" in site.parameters) is (name == "plant-one")

    assert _invoke([
        "--project", str(project), "-w", str(workspace),
        "plan", "aio-install", "-l", "name=plant-two,name=plant-three,name=plant-four",
        "--describe", "--output", "json",
    ]) == 0
    document = json.loads(capsys.readouterr().out)
    plan = document["plan"]
    assert {target["name"] for target in plan["targets"]} == {
        "plant-two", "plant-three", "plant-four",
    }
    assert document["summary"]["targetCount"] == 3
    assert plan["parallel"]["maxSites"] == 3


def test_generated_answers_cannot_modify_the_content_cache(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache))
    destination = cache / "answers.yaml"

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--example", str(destination),
    ]) == 1
    assert not destination.exists()
    assert "cache" in capsys.readouterr().err.lower()


def test_explicit_target_cannot_use_cached_package_inputs(
    guided_workspace, tmp_path, monkeypatch, capsys,
):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache))
    answers = _input_file(cache / "answers.yaml")

    assert _invoke([
        "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
        "--describe", "--input-file", str(answers),
    ]) == 1
    assert "cache" in capsys.readouterr().out.lower()


def test_protected_contract_cannot_emit_a_site(
    guided_workspace, tmp_path, capsys,
):
    input_contract = contract_path(guided_workspace / "manifests" / "test-manifest.yaml")
    contract = yaml.safe_load(input_contract.read_text(encoding="utf-8"))
    contract["inputs"].append({
        "name": "password",
        "type": "string",
        "description": "Protected value.",
        "sitePath": "properties.password",
        "required": False,
        "sensitive": True,
    })
    input_contract.write_text(yaml.safe_dump(contract), encoding="utf-8")
    answers = _input_file(tmp_path / "answers.yaml")
    site_file = tmp_path / "site.yaml"

    assert _invoke([
        "-w", str(guided_workspace), "inputs", _manifest(guided_workspace),
        "--input-file", str(answers), "--save-site", str(site_file),
    ]) == 1
    assert "protected" in capsys.readouterr().err.lower()
    assert not site_file.exists()


def test_manifest_without_contract_accepts_complete_site_only(
    complete_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(complete_workspace), "plan", _manifest(complete_workspace),
        "--describe", "--input-file", str(answers),
    ]) == 1
    assert "no typed input contract" in capsys.readouterr().out.lower()
    site_file = tmp_path / "existing.yaml"
    site_file.write_text(
        yaml.safe_dump({
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "standalone",
            "subscription": "00000000-0000-0000-0000-000000000001",
            "location": "eastus",
        }),
        encoding="utf-8",
    )
    assert _invoke([
        "-w", str(complete_workspace), "plan", _manifest(complete_workspace),
        "--describe", "--site-file", str(site_file), "--output", "json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in result["plan"]["targets"]] == [
        "standalone"
    ]


def test_plan_shows_the_first_description_paragraph_as_prose(complete_workspace, capsys):
    path = Path(_manifest(complete_workspace))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["description"] = "First line.\nSecond line.\n\nLater paragraph."
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert _invoke(["-w", str(complete_workspace), "plan", str(path), "--describe"]) == 0
    output = capsys.readouterr().out
    assert "\n  First line. Second line.\n" in output
    assert "Later paragraph." not in output


@pytest.mark.parametrize("entry", ["aio-install", "secretsync"])
def test_aio_contract_is_included_in_workspace_package_source(entry):
    root = Path(__file__).resolve().parents[1]
    authored = (
        f"workspaces/iot-operations/manifests/{entry}/inputs.yaml"
    )
    assert authored in _source_files(root, "workspaces/iot-operations", ())


def test_aio_input_inspection_distinguishes_required_and_defaulted(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke([
        "-w", str(workspace), "inputs", "aio-install", "--output", "json",
    ]) == 0
    document = json.loads(capsys.readouterr().out)
    fields = {field["name"]: field for field in document["inputs"]}
    assert fields["clusterName"]["status"] == "required"
    assert fields["environment"]["status"] == "optional"
    assert fields["country"]["status"] == "optional"
    assert fields["siteName"]["defaultFromResource"] == "cluster"
    assert fields["cluster"]["type"] == "azureResourceId"
    assert fields["subscription"]["derivableFrom"] == ["cluster"]
    assert fields["enableSecretSync"]["default"] is False
    assert fields["aioRelease"]["status"] == "defaulted"
    assert fields["aioRelease"]["default"] == "2608"
    assert fields["enableCertManager"]["default"] is True


def test_aio_boolean_defaults_use_answer_file_spelling(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke(["-w", str(workspace), "inputs", "aio-install"]) == 0
    output = capsys.readouterr().out
    assert "Default: true" in output
    assert "Default: True" not in output


def test_aio_input_inspection_explains_resource_alternative(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke(["-w", str(workspace), "inputs", "aio-install"]) == 0
    output = capsys.readouterr().out
    assert "derived from cluster" in output
    assert "Microsoft.Kubernetes/connectedClusters" in output
    assert "read-resources" in output
    normalized = " ".join(output.split())
    assert "inputs --read-resources" in normalized
    assert (
        "Resource route: supply cluster. Site Ops reads cluster to derive the other required inputs. "
        "Preview or save the Site with inputs --read-resources. "
        "The plan and deploy commands read the ID themselves."
    ) in normalized
    assert "Leave " not in normalized
    assert "workload identity" in output
    assert "Active when enableSecretSync=true" in output


def test_aio_plain_preview_shows_effective_defaults(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke([
        "-w", str(workspace), "inputs", "aio-install",
        "--input", "siteName=plant-one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "resourceGroup=rg-existing",
        "--input", "location=eastus",
        "--input", "clusterName=existing-arc",
        "--input", "environment=dev",
        "--input", "country=US",
    ]) == 0
    output = capsys.readouterr().out
    assert "name: plant-one" in output
    assert "enableSecretSync: false" in output
    assert "enableCertManager: true" in output
    assert "siteName (string, required)" not in output
    assert "Preview only. Add --save-site FILE to keep this Site." in output


def test_aio_inputs_prepare_one_explicit_target_without_example_site(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    assert _invoke([
        "-w", str(workspace), "plan", "aio-install", "--describe",
        "--input", "siteName=plant-one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "resourceGroup=rg-existing",
        "--input", "location=eastus",
        "--input", "clusterName=existing-arc",
        "--input", "environment=dev",
        "--input", "country=US",
        "--output", "json",
    ]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert [target["name"] for target in document["plan"]["targets"]] == [
        "plant-one"
    ]
    assert document["plan"]["manifest"]["manifestSelector"] is None
    assert document["plan"]["manifest"]["targetSelection"] == "explicit-site"
    assert "plant-one" not in output.err and "Target:" not in output.err
    assert _invoke([
        "-w", str(workspace), "plan", "aio-install", "--describe",
        "--input", "siteName=plant-one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "resourceGroup=rg-existing",
        "--input", "location=eastus",
        "--input", "clusterName=existing-arc",
    ]) == 0
    plain = capsys.readouterr()
    assert "  Site selection: explicit Site (replaces manifest targeting)\n" in plain.out
    assert "Target" not in plain.out + plain.err
    dispositions = {
        operation["identity"]["step"]: operation["disposition"]
        for operation in document["plan"]["targets"][0]["operations"]
    }
    assert dispositions["aio-instance"] == "execute"
    assert dispositions["resolve-aio"] == "skip"
    assert dispositions["secretsync"] == "skip"


def test_aio_enabled_without_cluster_observation_fails_before_planning(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    with (
        patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No provider read")),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")),
    ):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install", "--describe",
            "--input", "siteName=plant-one",
            "--input", "subscription=00000000-0000-0000-0000-000000000001",
            "--input", "resourceGroup=rg-first",
            "--input", "location=eastus",
            "--input", "clusterName=arc-first",
            "--input", "environment=dev",
            "--input", "country=US",
            "--input", "enableSecretSync=true",
            "--output", "json",
        ]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["diagnostics"][0]["code"] == "inputs.resource.requirement-unverified"
    assert document["diagnostics"][0]["summary"] == (
        "inputs.resource.requirement-unverified: With `enableSecretSync=true`, supply "
        "`--input cluster=<Arc-cluster-resource-ID>` so Site Ops can read the cluster's "
        "OIDC issuer and workload identity settings."
    )


def test_secretsync_names_its_missing_resource_input_before_prerequisites(capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    with (
        patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No provider read")),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")),
    ):
        assert _invoke([
            "-w", str(workspace), "plan", "secretsync", "--describe", "--input", "siteName=plant-one",
        ]) == 1
        output = capsys.readouterr()
        assert _invoke([
            "-w", str(workspace), "plan", "secretsync", "--describe", "--input", "siteName=plant-one",
            "--output", "json",
        ]) == 1
    message = (
        "Missing required input 'instance'. Supply `--input instance=<AIO-instance-resource-ID>` "
        "so Site Ops can read the cluster's OIDC issuer and workload identity settings."
    )
    assert f"Error: {message}" in output.out
    assert "connectedClusters." not in output.out + output.err
    diagnostic = json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert diagnostic["code"] == "inputs.invalid"
    assert diagnostic["summary"].startswith(message)


def test_missing_manual_answer_names_the_resource_route(capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    with patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install", "--describe", "--input", "enableSecretSync=true",
        ]) == 1
    assert (
        "Error: Missing required input 'siteName'. Supply it, or supply "
        "`--input cluster=<Arc-cluster-resource-ID>` to derive it."
    ) in capsys.readouterr().out


@pytest.mark.parametrize("workload_ready", [False, True])
def test_aio_enabled_observes_prerequisites_before_one_plan(
    capsys, workload_ready,
):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    facts_requested = frozenset({
        "connectedClusters.workloadIdentityEnabled",
        "connectedClusters.oidcIssuerAvailable",
    })
    calls = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            calls.append((ref.resource_id, facts))
            return ArmResourceObservation(
                _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters",
                "eastus", "arc-first",
                {
                    "connectedClusters.workloadIdentityEnabled": workload_ready,
                    "connectedClusters.oidcIssuerAvailable": True,
                },
            )

    with patch("siteops.cli.new_arm_reader", return_value=Reader()):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install", "--describe",
            "--input", "siteName=plant-one",
            "--input", "environment=dev",
            "--input", "country=US",
            "--input", "enableSecretSync=true",
            "--input", f"cluster={_CLUSTER_ID}",

            "--output", "json",
        ]) == (0 if workload_ready else 1)
    document = json.loads(capsys.readouterr().out)
    assert calls == [(_CLUSTER_ID, facts_requested)]
    if workload_ready:
        dispositions = {
            operation["identity"]["step"]: operation["disposition"]
            for operation in document["plan"]["targets"][0]["operations"]
        }
        assert dispositions["aio-instance"] == "execute"
        assert dispositions["resolve-aio"] == "execute"
        assert dispositions["secretsync"] == "execute"
    else:
        assert document["diagnostics"][0]["code"] == "inputs.resource.requirement-unmet"
        assert document["diagnostics"][0]["summary"] == (
            "inputs.resource.requirement-unmet: The cluster read for input 'cluster' "
            "does not report workload identity enabled."
        )


@pytest.mark.parametrize(
    ("cluster", "release"),
    [("arc-2608", "2608"), ("arc-2607", "2607")],
)
def test_inline_aio_release_selects_one_cluster_for_each_deploy(
    cluster, release, capsys, tmp_path,
):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    cluster_id = _CLUSTER_ID.replace("arc-first", cluster)
    observed = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            observed.append((ref.resource_id, facts))
            return ArmResourceObservation(
                cluster_id, "Microsoft.Kubernetes/connectedClusters",
                "eastus", cluster, {},
            )

    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=_aio_template_session(tmp_path)),
        patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
        patch.object(
            Orchestrator, "execute_plan", return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", "--yes", "aio-install",
            "--input", f"siteName=plant-{release}",
            "--input", f"cluster={cluster_id}",
            "--input", "environment=dev",
            "--input", "country=US",
            "--input", f"aioRelease={release}",

        ]) == 0
    capsys.readouterr()
    assert observed == [(cluster_id, frozenset())]
    deploy.assert_called_once()
    assert build.call_args.kwargs["selector"] is None
    sites = build.call_args.kwargs["sites"]
    assert len(sites) == 1
    assert sites[0].name == f"plant-{release}"
    assert sites[0].parameters["clusterName"] == cluster
    assert sites[0].properties["aioRelease"] == release


@pytest.mark.parametrize("workload_ready", [False, True])
def test_enabled_deploy_read_gate_precedes_executor(
    capsys, workload_ready, tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    observed = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            observed.append(ref.resource_type)
            return ArmResourceObservation(
                _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters",
                "eastus", "arc-first",
                {
                    "connectedClusters.workloadIdentityEnabled": workload_ready,
                    "connectedClusters.oidcIssuerAvailable": True,
                },
            )

    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=_aio_template_session(tmp_path)),
        patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
        patch.object(
            Orchestrator, "execute_plan",
            return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", "--yes", "aio-install",
            "--input", "siteName=plant-one", "--input", "environment=dev",
            "--input", "country=US", "--input", "enableSecretSync=true",
            "--input", f"cluster={_CLUSTER_ID}",
        ]) == (0 if workload_ready else 1)
    capsys.readouterr()
    assert observed == ["Microsoft.Kubernetes/connectedClusters"]
    if workload_ready:
        deploy.assert_called_once()
        sites = build.call_args.kwargs["sites"]
        assert len(sites) == 1
        assert sites[0].parameters["clusterName"] == "arc-first"
        assert sites[0].properties["deployOptions"]["enableSecretSync"] is True
    else:
        deploy.assert_not_called()


def test_aio_existing_vault_reads_second_role_without_retargeting_site(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    vault_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-vault/providers/Microsoft.KeyVault/vaults/vault-one"
    )
    calls = []

    class Reader:
        identity = SimpleNamespace(name="azure-cli", version=None)

        def read(self, ref, *, facts=frozenset()):
            calls.append((ref.resource_type, facts))
            if ref.resource_type == "Microsoft.KeyVault/vaults":
                return ArmResourceObservation(
                    vault_id, "Microsoft.KeyVault/vaults", "westus", "vault-one", {},
                )
            return ArmResourceObservation(
                _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters",
                "eastus", "arc-first",
                {
                    "connectedClusters.workloadIdentityEnabled": True,
                    "connectedClusters.oidcIssuerAvailable": True,
                },
            )

    with patch("siteops.cli.new_arm_reader", return_value=Reader()):
        assert _invoke([
            "-w", str(workspace), "inputs", "aio-install",
            "--input", "siteName=plant-one",
            "--input", "environment=dev",
            "--input", "country=US",
            "--input", "enableSecretSync=true",
            "--input", f"cluster={_CLUSTER_ID}",
            "--input", f"existingVault={vault_id}",
            "--read-resources", "--output", "json",
        ]) == 0
    result = json.loads(capsys.readouterr().out)
    site = result["resolution"]["site"]
    assert site["resourceGroup"] == "rg-first"
    assert site["location"] == "eastus"
    assert site["parameters"]["existingKeyVaultResourceId"] == vault_id
    assert result["resolution"]["resourceReads"]["resourceCount"] == 2
    assert [name for name, _ in calls] == [
        "Microsoft.Kubernetes/connectedClusters", "Microsoft.KeyVault/vaults",
    ]
    assert calls[1][1] == frozenset()


def test_aio_existing_vault_sub_mismatch_stops_before_any_read(capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    vault_other_sub = (
        "/subscriptions/00000000-0000-0000-0000-000000000002/"
        "resourceGroups/rg-vault/providers/Microsoft.KeyVault/vaults/vault-one"
    )
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("No Azure read")):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install", "--describe",
            "--input", "siteName=plant-one", "--input", "environment=dev",
            "--input", "country=US", "--input", "enableSecretSync=true",
            "--input", f"cluster={_CLUSTER_ID}",
            "--input", f"existingVault={vault_other_sub}",
             "--output", "json",
        ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"][0]["code"] == "inputs.resource.subscription-mismatch"


def _aio_template_session(
    tmp_path: Path, *, include_aio_version: bool = False,
) -> TemplateCompilationSession:
    def runner(argv: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(argv, 0, '{"azure-cli":"2.87.0"}', "")
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(argv, 0, "Bicep CLI version test", "")
        if argv[1:3] != ("bicep", "build"):
            raise AssertionError(f"Unexpected local tool invocation: {argv}")
        source = Path(argv[argv.index("--file") + 1])
        target = Path(argv[argv.index("--outfile") + 1])
        parameters = (
            {"tags": {"type": "object"}}
            if source.name in {"schema-registry.bicep", "adr-ns.bicep"}
            else {"existingKeyVaultResourceId": {"type": "string", "defaultValue": ""}}
            if source.name == "enable-secretsync.bicep" else {}
        )
        if include_aio_version and source.name == "instance.bicep":
            parameters = {"aioVersion": {"type": "string"}}
        if source.name in {"resolve-aio.bicep", "enable-secretsync.bicep"}:
            parameters.update({
                "aioInstanceName": {"type": "string"},
                "aioApiVersion": {"type": "string"},
            })
        if source.name == "enable-secretsync.bicep":
            parameters.update({
                "instanceTags": {"type": "object", "defaultValue": {}},
                "userAssignedIdentities": {"type": "object", "defaultValue": {}},
                "features": {"type": "object", "defaultValue": {}},
                "identityType": {"type": "string", "defaultValue": "None"},
                "instanceDescription": {"type": "string", "defaultValue": ""},
                "existingSpcResourceId": {"type": "string", "defaultValue": ""},
            })
        outputs = {
            name: {"type": "string"} for name in (
                "customLocationId", "customLocationName", "customLocationNamespace",
                "connectedClusterName", "oidcIssuerUrl", "instanceLocation",
                "identityType", "schemaRegistryResourceId", "adrNamespaceResourceId",
                "instanceDescription", "defaultSecretProviderClassResourceId",
            )
        }
        outputs.update({
            "schemaRegistry": {"type": "object"},
            "adrNamespace": {"type": "object"},
            "clExtensionIds": {"type": "array"},
            "instanceTags": {"type": "object"},
            "userAssignedIdentities": {"type": "object"},
            "features": {"type": "object"},
        })
        template = {
            "$schema": (
                "https://schema.management.azure.com/schemas/"
                "2019-04-01/deploymentTemplate.json#"
            ),
            "contentVersion": "1.0.0.0",
            "parameters": parameters,
            "resources": [],
            "outputs": outputs,
        }
        target.write_text(json.dumps(template), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: str(tmp_path / "tools" / name),
    )


def test_aio_preparation_selects_each_site_release_configuration(tmp_path):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    manifest = workspace / "manifests" / "aio-install" / "manifest.yaml"
    contract = load_contract(manifest)
    sites = [
        contract.resolve(inline=[
            f"siteName=plant-{release}",
            "subscription=00000000-0000-0000-0000-000000000001",
            f"resourceGroup=rg-{release}",
            "location=eastus",
            f"clusterName=arc-{release}",
            "environment=dev", "country=US",
            f"aioRelease={release}",
        ])
        for release in ("2608", "2607")
    ]
    session = _aio_template_session(tmp_path, include_aio_version=True)
    with patch("siteops.orchestrator.TemplateCompilationSession", return_value=session):
        result = Orchestrator(workspace).build_plan(
            manifest, sites=sites, intent=PlanIntent.EXECUTABLE,
        )
    assert result.status is PlanStatus.PLANNED, result.diagnostics
    assert result.plan is not None
    selected = {}
    for target in result.plan.targets:
        instance = next(
            operation for operation in target.operations
            if operation.identity.step == "aio-instance"
        )
        assert isinstance(instance.details, DeploymentOperation)
        assert instance.details.parameters is not None
        value = next(
            entry.value for entry in instance.details.parameters.entries
            if isinstance(entry.key, LiteralValue) and entry.key.value == "aioVersion"
        )
        selected[target.name] = resolve_plan_value(value, {})
    assert selected == {"plant-2608": "1.4.73", "plant-2607": "1.4.41"}


def test_aio_executable_preparation_resolves_country_and_environment_tags(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    manifest = workspace / "manifests" / "aio-install" / "manifest.yaml"
    contract = load_contract(manifest)
    site = contract.resolve(inline=[
        "siteName=plant-one",
        "subscription=00000000-0000-0000-0000-000000000001",
        "resourceGroup=rg-existing",
        "location=eastus",
        "clusterName=existing-arc",
        "environment=dev",
        "country=US",
    ])
    session = _aio_template_session(tmp_path)
    with patch("siteops.orchestrator.TemplateCompilationSession", return_value=session):
        result = Orchestrator(workspace).build_plan(
            manifest, sites=[site], intent=PlanIntent.EXECUTABLE,
        )
        unlabelled = Orchestrator(workspace).build_plan(
            manifest, sites=[replace(site, labels={})], intent=PlanIntent.EXECUTABLE,
        )
    assert result.status is PlanStatus.PLANNED, result.diagnostics
    assert unlabelled.status is PlanStatus.PLANNED, unlabelled.diagnostics
    assert result.plan is not None
    operations = {
        operation.identity.step: operation
        for operation in result.plan.targets[0].operations
    }
    for step in ("schema-registry", "adr-ns"):
        operation = operations[step]
        assert operation.disposition is PlanDisposition.EXECUTE
        assert isinstance(operation.details, DeploymentOperation)
        assert operation.details.parameters is not None
        tags = next(
            entry.value for entry in operation.details.parameters.entries
            if isinstance(entry.key, LiteralValue) and entry.key.value == "tags"
        )
        assert resolve_plan_value(tags, {})["environment"] == "dev"
        assert resolve_plan_value(tags, {})["country"] == "US"
        unlabelled_operation = next(
            operation for operation in unlabelled.plan.targets[0].operations
            if operation.identity.step == step
        )
        unlabelled_tags = next(
            entry.value for entry in unlabelled_operation.details.parameters.entries
            if isinstance(entry.key, LiteralValue) and entry.key.value == "tags"
        )
        assert resolve_plan_value(unlabelled_tags, {}) == {
            "site": "plant-one", "managedBy": "siteops",
        }


@pytest.mark.parametrize("file_based", [False, True])
def test_aio_cluster_only_answers_prepare_without_optional_labels(tmp_path, capsys, file_based):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    calls = []

    def read(ref, *, facts):
        calls.append(ref.resource_id)
        return ArmResourceObservation(ref.resource_id, ref.resource_type, "eastus", ref.name, {})

    reader = SimpleNamespace(
        identity=SimpleNamespace(name="fixture", version="1"), read=read,
    )
    if file_based:
        path = tmp_path / "answers.yaml"
        path.write_text(yaml.safe_dump({
            "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
            "values": {"cluster": _CLUSTER_ID},
        }), encoding="utf-8")
        answers = ["--input-file", str(path)]
    else:
        answers = ["--input", f"cluster={_CLUSTER_ID}"]
    session = _aio_template_session(tmp_path)
    with (
        patch("siteops.cli.new_arm_reader", return_value=reader),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
    ):
        assert _invoke([
            "-w", str(workspace), "plan", "aio-install",
            *answers,  "--output", "json",
        ]) == 0
    document = json.loads(capsys.readouterr().out)
    assert len(document["plan"]["targets"]) == 1
    assert document["plan"]["targets"][0]["name"].startswith("arc-first-")
    assert calls == [_CLUSTER_ID]


def test_aio_generated_sites_save_reload_and_obey_fleet_selection(tmp_path, capsys):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    project = tmp_path / "factory"
    (project / "sites").mkdir(parents=True)
    resources = [_CLUSTER_ID, _CLUSTER_ID.replace("rg-first", "rg-second")]
    contract = load_contract(workspace / "manifests" / "aio-install" / "manifest.yaml")
    names = []

    def read(ref, *, facts):
        return ArmResourceObservation(ref.resource_id, ref.resource_type, "eastus", ref.name, {})

    reader = SimpleNamespace(identity=SimpleNamespace(name="fixture", version="1"), read=read)
    with patch("siteops.cli.new_arm_reader", return_value=reader):
        for resource in resources:
            bound = contract.bind(inline=[f"cluster={resource}"])
            site = contract.build_site(bound, {"cluster": read(bound.resources[0].ref, facts=frozenset())})
            path = project / "sites" / f"{site.name}.yaml"
            assert _invoke([
                "--project", str(project), "-w", str(workspace), "inputs", "aio-install",
                "--input", f"cluster={resource}", "--read-resources", "--save-site", str(path),
            ]) == 0
            assert Site.from_file(path).name == site.name
            assert Site.from_file(path).labels == {}
            names.append(site.name)
            capsys.readouterr()
        assert names[0] != names[1]
        assert _invoke([
            "--project", str(project), "-w", str(workspace), "inputs", "aio-install",
            "--input", f"cluster={resources[0].upper()}", "--read-resources",
            "--save-site", str(project / "sites" / f"{names[0]}.yaml"),
        ]) == 1
        capsys.readouterr()
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("Saved Sites do not read resources")):
        assert _invoke([
            "--project", str(project), "-w", str(workspace),
            "plan", "aio-install", "-l", f"name={names[0]}", "--describe", "--output", "json",
        ]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in document["plan"]["targets"]] == [names[0]]
    orchestrator = Orchestrator(workspace, site_config_root=project)
    assert orchestrator.resolve_sites(orchestrator.load_manifest(
        workspace / "manifests" / "aio-install" / "manifest.yaml",
    )) == []


@pytest.mark.parametrize("field", ["environment", "country"])
def test_aio_optional_labels_reject_empty_supplied_values_before_resource_read(capsys, field):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    with patch("siteops.cli.new_arm_reader", side_effect=AssertionError("Invalid input cannot read Azure")):
        assert _invoke([
            "-w", str(workspace), "inputs", "aio-install",
            "--input", f"cluster={_CLUSTER_ID}", "--input", f"{field}=", "--read-resources",
        ]) == 1
    assert f"Input '{field}' must be a nonempty string." in capsys.readouterr().err


@pytest.mark.parametrize("document", ["README.md", "docs/guided-inputs.md"])
def test_documented_cluster_only_commands_resolve_through_cli(
    tmp_path, capsys, document, monkeypatch,
):
    _interactive_console(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    lines = (root / document).read_text(encoding="utf-8").splitlines()
    cluster_facts = {
        "connectedClusters.workloadIdentityEnabled": True,
        "connectedClusters.oidcIssuerAvailable": True,
    }
    reader = SimpleNamespace(
        identity=SimpleNamespace(name="fixture", version="1"),
        read=lambda ref, *, facts: ArmResourceObservation(
            ref.resource_id, ref.resource_type, "eastus", ref.name,
            {name: cluster_facts[name] for name in facts},
        ),
    )
    session = _aio_template_session(tmp_path)
    commands = [
        line for line in lines if line.startswith("siteops ")
        and any(f" {verb} aio-install " in line for verb in ("plan", "deploy"))
        and "cluster=<Arc-cluster-resource-ID>" in line
    ]
    assert commands and any(" deploy " in line for line in commands)
    for line in commands:
        verb = "deploy" if " deploy " in line else "plan"
        args = shlex.split(line.replace("<Arc-cluster-resource-ID>", _CLUSTER_ID))[1:]
        with (
            patch("siteops.cli.open_command_context", side_effect=lambda **kwargs: nullcontext(
                CommandContext(workspace, workspace),
            )),
            patch("siteops.cli.new_arm_reader", return_value=reader),
            patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
            patch.object(Orchestrator, "build_plan", autospec=True, side_effect=Orchestrator.build_plan) as build,
            patch.object(Orchestrator, "execute_plan", return_value=RunResult.from_sites((), elapsed=0)) as deploy,
        ):
            assert _invoke(args) == 0
        if verb == "deploy":
            assert len(build.call_args.kwargs["sites"]) == 1
            assert build.call_args.kwargs["sites"][0].labels == {}
            assert build.call_args.kwargs["sites"][0].parameters["clusterName"] == "arc-first"
            deploy.assert_called_once()
            build.assert_called_once()
            operations = {
                operation.identity.step: operation.disposition
                for operation in deploy.call_args.args[0].plan.targets[0].operations
            }
            assert operations["secretsync"] is (
                PlanDisposition.EXECUTE if "enableSecretSync=true" in args else PlanDisposition.SKIP
            )
        else:
            deploy.assert_not_called()
        capsys.readouterr()


_EXISTING_INSTANCE_ID = _CLUSTER_ID.replace(
    "Microsoft.Kubernetes/connectedClusters/arc-first",
    "Microsoft.IoTOperations/instances/external-instance",
)
_EXISTING_LOCATION_ID = _CLUSTER_ID.replace(
    "Microsoft.Kubernetes/connectedClusters/arc-first",
    "Microsoft.ExtendedLocation/customLocations/external-location",
)


def _existing_instance_reader(*, identity_enabled=True, location_id=_EXISTING_LOCATION_ID, vault_id=None):
    calls = []

    def read(ref, *, facts=frozenset(), references=frozenset()):
        calls.append((ref, facts, references))
        if ref.resource_id == _EXISTING_INSTANCE_ID:
            related = {"extendedLocation": location_id}
        elif ref.resource_id == _EXISTING_LOCATION_ID:
            related = {"customLocations.hostResourceId": _CLUSTER_ID}
        elif ref.resource_id == _CLUSTER_ID:
            related = {}
        elif vault_id is not None and ref.resource_id == vault_id:
            related = {}
        else:
            raise AssertionError("Unexpected resource read.")
        assert set(related) == references
        return ArmResourceObservation(
            ref.resource_id, ref.resource_type, "eastus", ref.name,
            {fact: identity_enabled for fact in facts}, related,
        )

    return SimpleNamespace(identity=SimpleNamespace(name="fixture", version="1"), read=read), calls


@pytest.mark.parametrize("verb", ["plan", "deploy"])
def test_existing_instance_secret_sync_uses_actual_identity_without_install(tmp_path, capsys, verb):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    reader, calls = _existing_instance_reader()
    session = _aio_template_session(tmp_path)
    with (
        patch("siteops.cli.new_arm_reader", return_value=reader),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
        patch.object(Orchestrator, "execute_plan", return_value=RunResult.from_sites((), elapsed=0)) as execute,
    ):
        assert _invoke([
            "-w", str(workspace), verb, "secretsync",
            "--input", f"instance={_EXISTING_INSTANCE_ID}", "--output", "json",
            *(["--yes"] if verb == "deploy" else []),
        ]) == 0
    output = capsys.readouterr().out
    if verb == "plan":
        document = json.loads(output)
        operations = document["plan"]["targets"][0]["operations"]
        assert [operation["identity"]["step"] for operation in operations] == ["resolve-aio", "secretsync"]
        execute.assert_not_called()
    else:
        result = execute.call_args.args[0]
        assert result.executable
        assert [operation.identity.step for operation in result.plan.targets[0].operations] == [
            "resolve-aio", "secretsync",
        ]
        for operation in result.plan.targets[0].operations:
            parameters = {
                entry.key.value: entry.value for entry in operation.details.parameters.entries
                if isinstance(entry.key, LiteralValue)
            }
            assert resolve_plan_value(parameters["aioInstanceName"], {}) == "external-instance"
            assert resolve_plan_value(parameters["aioApiVersion"], {}) == "2026-07-01"
            if operation.identity.step == "secretsync":
                for name in (
                    "instanceTags", "userAssignedIdentities", "features", "identityType",
                    "instanceDescription", "existingSpcResourceId",
                ):
                    assert isinstance(parameters[name], OutputValue)
                    assert parameters[name].reference.source.step == "resolve-aio"
    assert [ref.resource_id for ref, _, _ in calls] == [
        _EXISTING_INSTANCE_ID, _EXISTING_LOCATION_ID, _CLUSTER_ID,
    ]
    assert [ref.api_version for ref, _, _ in calls] == [
        "2026-07-01", "2021-08-31-preview", "2024-07-15-preview",
    ]


@pytest.mark.parametrize("fault", ["no-consent", "scope", "prerequisite"])
def test_existing_secret_sync_fails_before_writes_on_read_boundaries(tmp_path, capsys, fault):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    reader, calls = _existing_instance_reader(
        identity_enabled=fault != "prerequisite",
        location_id=_EXISTING_LOCATION_ID.replace("rg-first", "private-other-rg") if fault == "scope"
        else _EXISTING_LOCATION_ID,
    )
    args = [
        "-w", str(workspace), "deploy", "secretsync",
        "--input", f"instance={_EXISTING_INSTANCE_ID}",
    ]
    if fault != "no-consent":
        args.append("--yes")
    with (
        patch("siteops.cli.new_arm_reader", return_value=reader),
        patch.object(Orchestrator, "execute_plan", side_effect=AssertionError("No deployment may start")),
    ):
        assert _invoke(args) == (2 if fault == "no-consent" else 1)
    error = capsys.readouterr().err
    assert "private-other-rg" not in error
    assert {
        "no-consent": "--yes", "scope": "resource-group-mismatch",
        "prerequisite": "requirement-unmet",
    }[fault] in error
    assert len(calls) == {"no-consent": 0, "scope": 1, "prerequisite": 3}[fault]


def test_existing_secret_sync_example_needs_only_instance_and_reuses_cluster_site_name(capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    contract = load_contract(workspace / "manifests" / "secretsync" / "manifest.yaml")
    assert contract is not None
    assert contract.example()["values"] == {"instance": None}
    assert _invoke(["-w", str(workspace), "inputs", "secretsync"]) == 0
    text = capsys.readouterr().out
    assert "Resource route: supply instance." in text
    assert "supply siteName" not in text


def test_existing_secret_sync_file_route_keeps_optional_vault_and_cluster_site_identity(tmp_path, capsys):
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    vault_id = _CLUSTER_ID.replace(
        "resourceGroups/rg-first/providers/Microsoft.Kubernetes/connectedClusters/arc-first",
        "resourceGroups/vault-rg/providers/Microsoft.KeyVault/vaults/vault-one",
    )
    answers = tmp_path / "secretsync.yaml"
    answers.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
        "values": {"instance": _EXISTING_INSTANCE_ID, "existingVault": vault_id},
    }), encoding="utf-8")
    reader, calls = _existing_instance_reader(vault_id=vault_id)
    with patch("siteops.cli.new_arm_reader", return_value=reader):
        assert _invoke([
            "-w", str(workspace), "inputs", "secretsync",
            "--input-file", str(answers), "--read-resources", "--output", "json",
        ]) == 0
    site = json.loads(capsys.readouterr().out)["resolution"]["site"]
    assert site["parameters"]["aioInstanceName"] == "external-instance"
    assert site["parameters"]["existingKeyVaultResourceId"] == vault_id
    assert len(calls) == 4
    aio = load_contract(workspace / "manifests" / "aio-install" / "manifest.yaml")
    bound = aio.bind(inline=[f"cluster={_CLUSTER_ID}"])
    fresh_site = aio.build_site(bound, {
        "cluster": ArmResourceObservation(
            _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters", "eastus", "arc-first", {},
        ),
    })
    assert site["name"] == fresh_site.name


@pytest.mark.parametrize("document", [
    "docs/guided-inputs.md", "workspaces/iot-operations/manifests/secretsync/README.md",
])
def test_documented_existing_secret_sync_commands_use_one_instance_input(
    tmp_path, capsys, document, monkeypatch,
):
    _interactive_console(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    lines = (root / document).read_text(encoding="utf-8").splitlines()
    reader, calls = _existing_instance_reader()
    session = _aio_template_session(tmp_path)
    commands = [
        line for line in lines if line.startswith("siteops ")
        and any(f" {verb} secretsync " in line for verb in ("plan", "deploy"))
        and "instance=<AIO-instance-resource-ID>" in line
    ]
    assert commands and any(" deploy " in line for line in commands)
    for command in commands:
        verb = "deploy" if " deploy " in command else "plan"
        args = shlex.split(command.replace("<AIO-instance-resource-ID>", _EXISTING_INSTANCE_ID))[1:]
        with (
            patch("siteops.cli.open_command_context", side_effect=lambda **kwargs: nullcontext(
                CommandContext(workspace, workspace),
            )),
            patch("siteops.cli.new_arm_reader", return_value=reader),
            patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
            patch.object(Orchestrator, "execute_plan", return_value=RunResult.from_sites((), elapsed=0)) as execute,
        ):
            assert _invoke(args) == 0
        if verb == "deploy":
            plan = execute.call_args.args[0].plan
            assert [operation.identity.step for operation in plan.targets[0].operations] == [
                "resolve-aio", "secretsync",
            ]
        else:
            execute.assert_not_called()
        capsys.readouterr()
    assert len(calls) == 3 * len(commands)


@pytest.mark.parametrize("mode", ["review", "unattended", "redacted"])
def test_resource_id_disclosure_is_limited_to_private_deployment_review(tmp_path, monkeypatch, capsys, mode):
    _interactive_console(monkeypatch)
    if mode == "redacted":
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    workspace = Path(__file__).resolve().parents[1] / "workspaces" / "iot-operations"
    reader, _ = _existing_instance_reader()
    arguments = [
        "-w", str(workspace), "deploy", "secretsync",
        "--input", f"instance={_EXISTING_INSTANCE_ID}",
    ]
    if mode != "review":
        arguments.append("--yes")

    def execute(prepared, **kwargs):
        output = capsys.readouterr().err
        if mode == "review":
            assert f"Resource instance: {_EXISTING_INSTANCE_ID}" in output
            assert "Subscription: 00000000-0000-0000-0000-000000000001" in output
            assert "Resource group: rg-first" in output
        else:
            assert _EXISTING_INSTANCE_ID not in output
            assert _EXISTING_LOCATION_ID not in output
            assert _CLUSTER_ID not in output
        return RunResult.from_sites((), elapsed=0)

    with (
        patch("siteops.cli.new_arm_reader", return_value=reader),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=_aio_template_session(tmp_path)),
        patch.object(Orchestrator, "execute_plan", side_effect=execute),
    ):
        assert _invoke(arguments) == 0


def test_aio_executable_preparation_binds_existing_vault_from_second_resource(
    tmp_path,
):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    manifest = workspace / "manifests" / "aio-install" / "manifest.yaml"
    contract = load_contract(manifest)
    vault_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-vault/providers/Microsoft.KeyVault/vaults/vault-one"
    )
    bound = contract.bind(inline=[
        "siteName=plant-one", "environment=dev", "country=US",
        "enableSecretSync=true", f"cluster={_CLUSTER_ID}",
        f"existingVault={vault_id}",
    ])
    site = contract.build_site(bound, {
        "cluster": ArmResourceObservation(
            _CLUSTER_ID, "Microsoft.Kubernetes/connectedClusters",
            "eastus", "arc-first",
            {
                "connectedClusters.workloadIdentityEnabled": True,
                "connectedClusters.oidcIssuerAvailable": True,
            },
        ),
        "existingVault": ArmResourceObservation(
            vault_id, "Microsoft.KeyVault/vaults", "westus", "vault-one", {},
        ),
    })
    session = _aio_template_session(tmp_path)
    with patch("siteops.orchestrator.TemplateCompilationSession", return_value=session):
        result = Orchestrator(workspace).build_plan(
            manifest, sites=[site], intent=PlanIntent.EXECUTABLE,
        )
    assert result.status is PlanStatus.PLANNED, result.diagnostics
    assert result.plan is not None
    operations = {
        operation.identity.step: operation
        for operation in result.plan.targets[0].operations
    }
    assert operations["resolve-aio"].disposition is PlanDisposition.EXECUTE
    secret_sync = operations["secretsync"]
    assert secret_sync.disposition is PlanDisposition.EXECUTE
    assert isinstance(secret_sync.details, DeploymentOperation)
    assert secret_sync.details.parameters is not None
    linked_vault = next(
        entry.value for entry in secret_sync.details.parameters.entries
        if isinstance(entry.key, LiteralValue)
        and entry.key.value == "existingKeyVaultResourceId"
    )
    assert resolve_plan_value(linked_vault, {}) == vault_id

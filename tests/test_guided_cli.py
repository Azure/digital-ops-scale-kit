"""Exercise guided target selection through the public command path."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from siteops.arm_resources import ArmResourceError, ArmResourceObservation
from siteops.cli import main
from siteops.compilation import TemplateCompilationSession
from siteops.guided_inputs import contract_path, load_contract
from siteops.models import Site
from siteops.orchestrator import Orchestrator
from siteops.package_builder import _source_files
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    PlanDisposition,
    PlanIntent,
    PlanStatus,
    resolve_plan_value,
)


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
    assert help_text.index("inputs aio-install --example") < help_text.index(
        "plan aio-install --input-file"
    )
    assert help_text.index("plan aio-install --input-file") < help_text.index(
        "plan aio-install -l name=plant-two,name=plant-three"
    )
    for verb in ("plan", "deploy"):
        example = next(
            line for line in help_text.splitlines() if f" {verb} aio-install --input-file" in line
        )
        assert "--read-resources" in example
    assert "deploy aio-install -l name=plant-two,name=plant-three" in help_text


def test_inputs_help_explains_read_only_preview(capsys):
    with patch.object(sys, "argv", ["siteops", "inputs", "--help"]):
        with pytest.raises(SystemExit) as stopped:
            main()
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "preview" in help_text.lower()
    assert "without writing" in help_text.lower()
    assert "--save-site" in help_text


def test_resource_reads_are_explicit_optional_command_options(capsys):
    for command in ("inputs", "plan", "deploy"):
        with patch.object(sys, "argv", ["siteops", command, "--help"]):
            with pytest.raises(SystemExit) as stopped:
                main()
        assert stopped.value.code == 0
        assert "--read-resources" in capsys.readouterr().out
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
    assert set(values) == {
        "siteName", "subscription", "resourceGroup", "location",
        "clusterName", "environment", "country", "cluster",
    }
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
            "--read-resources", "--output", "json",
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
            "--read-resources", "--output", "json",
        ]) == 1
    assert "invalid" in capsys.readouterr().out


def test_resource_id_without_read_flag_cannot_use_manual_fallback(
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
            "-w", str(workspace), "plan", _manifest(workspace),
            "--describe", "--input-file", str(answers), "--output", "json",
        ]) == 1
    document = json.loads(capsys.readouterr().out)
    assert "read-resources" in document["diagnostics"][0]["summary"]


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
    assert "--describe --read-resources" in error


def test_read_flag_requires_a_typed_resource_target(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    with patch(
        "siteops.cli.new_arm_reader",
        side_effect=AssertionError("No reader may be constructed."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
            "--describe", "--input-file", str(answers), "--read-resources",
            "--output", "json",
        ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.resource.nothing-to-read"


def test_read_flag_alone_never_selects_manifest_fleet(
    guided_workspace, capsys,
):
    with patch.object(
        Orchestrator, "build_plan",
        side_effect=AssertionError("No fleet plan may be built."),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "plan", _manifest(guided_workspace),
            "--describe", "--read-resources", "--output", "json",
        ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "inputs.resource.nothing-to-read"


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
            "--read-resources", "--output", "json",
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
        "--input-file", str(answers), "--read-resources", "--output", "json",
    ]
    if command == "plan":
        args.append("--describe")
    with (
        patch("siteops.cli.new_arm_reader", return_value=Reader()),
        patch.object(Orchestrator, "build_plan", side_effect=AssertionError("No plan")),
        patch.object(Orchestrator, "deploy", side_effect=AssertionError("No execution")),
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


def test_cancelled_reader_setup_is_not_reported_as_provider_unavailable(
    guided_workspace, tmp_path, capsys,
):
    workspace = _resource_workspace(guided_workspace)
    answers = _resource_answers(tmp_path / "answers.yaml")
    with (
        patch("siteops.cli.new_arm_reader", side_effect=ArmResourceError("CANCELLED")),
        patch.object(Orchestrator, "deploy", side_effect=AssertionError("No execution")),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", _manifest(workspace),
            "--input-file", str(answers), "--read-resources", "--output", "json",
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
        patch.object(
            Orchestrator, "deploy", return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
            "--input-file", str(answers),
        ]) == 0

    deploy.assert_called_once()
    assert deploy.call_args.kwargs["selector"] is None
    sites = deploy.call_args.kwargs["sites"]
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


def test_mixed_explicit_and_fleet_target_fails_before_execution(
    guided_workspace, tmp_path, capsys,
):
    answers = _input_file(tmp_path / "answers.yaml")
    assert _invoke([
        "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
        "--input-file", str(answers), "-l", "name=test-site",
        "--output", "json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] != "succeeded"
    assert output["diagnostics"][0]["code"] == "plan.targeting.conflict"
    assert "selector" in output["diagnostics"][0]["summary"].lower()


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
        "-w", str(guided_workspace), "deploy", _manifest(guided_workspace),
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
    assert ("could not be loaded" if redacted else "exists") in error
    assert site_file.read_bytes() == saved_bytes


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


def test_aio_contract_is_included_in_workspace_package_source():
    root = Path(__file__).resolve().parents[1]
    authored = (
        "workspaces/iot-operations/manifests/aio-install/inputs.yaml"
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
    assert fields["environment"]["status"] == "required"
    assert fields["country"]["status"] == "required"
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
    assert "replaces manifest targeting" in output.err
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
            "--read-resources",
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


@pytest.mark.parametrize(
    ("cluster", "release"),
    [("arc-2608", "2608"), ("arc-2607", "2607")],
)
def test_inline_aio_release_selects_one_cluster_for_each_deploy(
    cluster, release, capsys,
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
        patch.object(
            Orchestrator, "deploy", return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", "aio-install",
            "--input", f"siteName=plant-{release}",
            "--input", f"cluster={cluster_id}",
            "--input", "environment=dev",
            "--input", "country=US",
            "--input", f"aioRelease={release}",
            "--read-resources",
        ]) == 0
    capsys.readouterr()
    assert observed == [(cluster_id, frozenset())]
    deploy.assert_called_once()
    assert deploy.call_args.kwargs["selector"] is None
    sites = deploy.call_args.kwargs["sites"]
    assert len(sites) == 1
    assert sites[0].name == f"plant-{release}"
    assert sites[0].parameters["clusterName"] == cluster
    assert sites[0].properties["aioRelease"] == release


@pytest.mark.parametrize("workload_ready", [False, True])
def test_enabled_deploy_read_gate_precedes_executor(
    capsys, workload_ready,
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
        patch.object(
            Orchestrator, "deploy",
            return_value=SimpleNamespace(exit_code=0),
        ) as deploy,
        patch("siteops.cli._write_run_result"),
    ):
        assert _invoke([
            "-w", str(workspace), "deploy", "aio-install",
            "--input", "siteName=plant-one", "--input", "environment=dev",
            "--input", "country=US", "--input", "enableSecretSync=true",
            "--input", f"cluster={_CLUSTER_ID}", "--read-resources",
        ]) == (0 if workload_ready else 1)
    capsys.readouterr()
    assert observed == ["Microsoft.Kubernetes/connectedClusters"]
    if workload_ready:
        deploy.assert_called_once()
        sites = deploy.call_args.kwargs["sites"]
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
            "--read-resources", "--output", "json",
        ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"][0]["code"] == "inputs.resource.subscription-mismatch"


def _aio_template_session(
    tmp_path: Path, *, include_aio_version: bool = False,
) -> TemplateCompilationSession:
    def runner(argv: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(argv, 0, '{"azure-cli":"test"}', "")
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
    assert unlabelled.status is PlanStatus.INVALID
    assert any(
        diagnostic.code == "operation-preparation.invalid"
        for diagnostic in unlabelled.diagnostics
    )
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

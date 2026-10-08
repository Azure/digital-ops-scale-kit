"""Shared terminal presentation: markers, sanitizing, width and progress lines."""

import io
import json
import time
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from siteops import cli, terminal
from siteops.orchestrator import Orchestrator
from siteops.planning import PlanStatus

_HOSTILE = "\x1b[2J\x1b[31m\u202e\x07"


class _Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_sanitize_escapes_controls_once_and_keeps_text():
    escaped = terminal.sanitize(f"caf\u00e9 {_HOSTILE}\n")

    assert escaped == "caf\u00e9 \\u001b[2J\\u001b[31m\\u202e\\u0007\\u000a"
    assert terminal.sanitize(escaped) == escaped
    assert terminal.sanitize_lines("one\x1b\r\ntwo") == ["one\\u001b", "two"]


def test_markers_are_one_ascii_set():
    markers = (
        terminal.SUCCEEDED, terminal.FAILED, terminal.NOT_RUN,
        terminal.UNKNOWN, terminal.WARNING, terminal.BULLET,
    )
    assert "".join(markers).isascii()
    assert len({terminal.SUCCEEDED, terminal.FAILED, terminal.NOT_RUN, terminal.UNKNOWN}) == 4


@pytest.mark.parametrize(("columns", "expected"), [(80, 80), (160, 100), (20, 40)])
def test_terminal_width_is_capped_on_a_tty(monkeypatch, columns, expected):
    monkeypatch.setattr(
        terminal.shutil, "get_terminal_size", lambda fallback: MagicMock(columns=columns),
    )
    assert terminal.line_width(_Stream(tty=True)) == expected


def test_redirected_output_uses_a_fixed_width(monkeypatch):
    monkeypatch.setattr(
        terminal.shutil, "get_terminal_size", MagicMock(side_effect=AssertionError("Not a terminal")),
    )
    assert terminal.line_width(_Stream(tty=False)) == terminal.REDIRECTED_WIDTH
    assert terminal.line_width(object()) == terminal.REDIRECTED_WIDTH
    wrapped = terminal.wrap("word " * 40)
    assert max(map(len, wrapped)) <= terminal.REDIRECTED_WIDTH


def test_validation_errors_escape_authored_text(complete_workspace, capsys):
    orchestrator = Orchestrator(complete_workspace)
    args = Namespace(
        manifest=complete_workspace / "manifests" / "test-manifest.yaml",
        workspace=complete_workspace, selector=None,
    )
    with patch.object(orchestrator, "validate", return_value=[f"Step 'a{_HOSTILE}' failed.\nDetail"]):
        assert cli.cmd_validate(args, orchestrator) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "x Validation failed with 1 error(s):" in captured.err
    assert "  - Step 'a\\u001b[2J\\u001b[31m\\u202e\\u0007' failed.\n    Detail\n" in captured.err
    assert all(character == "\n" or character.isprintable() for character in captured.err)


def test_named_manifest_is_reported_by_workspace_path(complete_workspace, capsys):
    args = Namespace(manifest="test-manifest", workspace=complete_workspace)

    path = cli._command_manifest(args)

    assert path is not None
    error = capsys.readouterr().err
    assert error == "Manifest: manifests/test-manifest.yaml\n"


def test_manifest_lookup_errors_escape_content_text(complete_workspace, capsys):
    from siteops.browse import BrowseError

    args = Namespace(manifest="test-manifest", workspace=complete_workspace)
    failure = BrowseError("browse.invalid", f"Entry 'a{_HOSTILE}' is invalid.")

    with patch("siteops.cli.resolve_manifest_path", side_effect=failure):
        assert cli._command_manifest(args) is None

    error = capsys.readouterr().err
    assert error == "Error: Entry 'a\\u001b[2J\\u001b[31m\\u202e\\u0007' is invalid.\n"


def test_plan_describe_escapes_an_authored_description(complete_workspace, capsys):
    import yaml

    manifest = complete_workspace / "manifests" / "test-manifest.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["description"] = f"Install{_HOSTILE} things."
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    args = Namespace(manifest=manifest, workspace=complete_workspace, selector=None, describe=True)

    assert cli.cmd_plan(args, Orchestrator(complete_workspace)) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output and "\u202e" not in output and "\x07" not in output
    assert "Install\\u001b[2J\\u001b[31m" in output


def _planning_args(workspace, **overrides):
    values = {
        "manifest": workspace / "manifests" / "test-manifest.yaml",
        "workspace": workspace, "selector": None, "verbose": False,
        "output": "json", "projection": "local-private", "parallel": None,
    }
    values.update(overrides)
    return Namespace(**values)


def _slow_plan(*args, **kwargs):
    time.sleep(0.2)
    result = MagicMock()
    result.status = PlanStatus.PLANNED
    return result


def test_executable_preparation_reports_a_heartbeat_on_stderr(complete_workspace, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_HEARTBEAT_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(cli, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    orchestrator = MagicMock()
    orchestrator.build_plan.side_effect = _slow_plan

    with patch("siteops.cli.serialize_plan_json", return_value='{"kind": "DeploymentPlan"}'):
        assert cli.cmd_plan(_planning_args(complete_workspace), orchestrator) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"kind": "DeploymentPlan"}
    assert "Preparing executable deployment plan..." in captured.err
    assert "Still preparing: compiling templates and checking local tools" in captured.err
    assert not any(
        thread.name == "siteops-preparation-heartbeat" for thread in __import__("threading").enumerate()
    )


def test_describe_and_fast_preparation_print_no_heartbeat(complete_workspace, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_HEARTBEAT_FIRST_SECONDS", 0.05)
    orchestrator = MagicMock()
    orchestrator.build_plan.side_effect = _slow_plan

    with patch("siteops.cli.serialize_plan_json", return_value="{}"):
        assert cli.cmd_plan(_planning_args(complete_workspace, describe=True), orchestrator) == 0
    assert "Still preparing" not in capsys.readouterr().err

    monkeypatch.setattr(cli, "_HEARTBEAT_FIRST_SECONDS", 10.0)
    with patch("siteops.cli.serialize_plan_json", return_value="{}"):
        assert cli.cmd_plan(_planning_args(complete_workspace), orchestrator) == 0
    assert "Still preparing" not in capsys.readouterr().err


def test_heartbeat_stops_when_preparation_is_interrupted(complete_workspace, monkeypatch):
    monkeypatch.setattr(cli, "_HEARTBEAT_FIRST_SECONDS", 0.01)
    args = _planning_args(complete_workspace)

    with pytest.raises(KeyboardInterrupt):
        with cli._preparation_heartbeat(args):
            time.sleep(0.05)
            raise KeyboardInterrupt
    assert not any(
        thread.name == "siteops-preparation-heartbeat" for thread in __import__("threading").enumerate()
    )


def test_standard_enrollment_announces_the_trusted_root_read(capsys):
    args = Namespace(
        project=None, workspace=None, approved_source=None, source_command="enroll",
        trust_policy=None, trusted_root=None, source="github:example/content", name="example",
    )

    with patch("siteops.source_profiles.enroll_standard_source", side_effect=OSError("stopped")) as enroll:
        assert cli.cmd_source(args) == 1

    enroll.assert_called_once()
    error = capsys.readouterr().err
    assert error.startswith("Reading the current GitHub trusted root with the GitHub CLI...\n")


def test_run_summary_and_progress_escape_target_and_step_text():
    from siteops.planning import OperationIdentity, OperationKind, TargetKind
    from siteops.reporting import TextProgressReporter, render_plain_run
    from siteops.results import (
        OperationResult,
        OperationStatus,
        OutcomeReason,
        OutcomeReasonCode,
        ProgressEvent,
        ProgressEventKind,
        RunResult,
        SiteResult,
    )

    target = f"site{_HOSTILE}"
    identity = OperationIdentity(target=target, step=f"deploy{_HOSTILE}")
    operation = OperationResult(
        identity=identity, kind=OperationKind.DEPLOYMENT, status=OperationStatus.UNKNOWN,
        elapsed=1.0, reason=OutcomeReason(OutcomeReasonCode.COMPLETION_UNKNOWN),
        _deployment_name=f"name{_HOSTILE}",
    )
    site = SiteResult.from_operations(
        target=target, kind=TargetKind.RESOURCE_GROUP, operations=(operation,), elapsed=1.0,
    )
    rendered = render_plain_run(RunResult.from_sites((site,), elapsed=1.0, run_id="run"), redacted=False)

    stream = io.StringIO()
    reporter = TextProgressReporter(stream, redacted=False)
    reporter(ProgressEvent(
        kind=ProgressEventKind.OPERATION_FINISHED, operation=identity,
        operation_status=OperationStatus.UNKNOWN,
        reason=OutcomeReason(OutcomeReasonCode.COMPLETION_UNKNOWN),
    ))
    reporter.message(f"note{_HOSTILE}")

    for text in (rendered, stream.getvalue()):
        assert all(character == "\n" or character.isprintable() for character in text)
        assert "site\\u001b[2J" in text or "deploy\\u001b[2J" in text
    assert "? site\\u001b[2J" in rendered
    flat = " ".join(rendered.split())
    assert "deploy\\u001b[2J\\u001b[31m\\u202e\\u0007: unconfirmed deployment name\\u001b[2J" in flat

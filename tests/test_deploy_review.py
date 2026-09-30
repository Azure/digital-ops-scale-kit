"""Review and automation consent are checked at the actual deployment call boundary."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from siteops import cli
from siteops.planning import PlanStatus
from siteops.results import RunResult


def invoke(arguments):
    with patch.object(sys, "argv", ["siteops", *arguments]):
        with pytest.raises(SystemExit) as stopped:
            cli.main()
    return stopped.value.code


@pytest.fixture
def prepared(complete_workspace, monkeypatch):
    for name in ("CI", "GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    result = SimpleNamespace(
        executable=True, status=PlanStatus.PLANNED,
        plan=SimpleNamespace(targets=(object(),)),
    )
    engine = Mock()
    engine.build_plan.return_value = result
    engine.execute_plan.return_value = RunResult.from_sites((), elapsed=0)
    engine.deploy.return_value = engine.execute_plan.return_value
    monkeypatch.setattr(cli, "Orchestrator", Mock(return_value=engine))
    monkeypatch.setattr(cli, "render_plain_plan", Mock(return_value="Reviewed target\n"))
    manifest = complete_workspace / "manifests" / "test-manifest.yaml"
    return complete_workspace, manifest, result, engine


@pytest.mark.parametrize("answer", ["y\n", "YES\n", "no\n", "\n", ""])
def test_interactive_deploy_reviews_and_executes_the_same_plan(prepared, monkeypatch, capsys, answer):
    workspace, manifest, result, engine = prepared
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    def respond():
        assert engine.build_plan.call_count == 1
        engine.execute_plan.assert_not_called()
        assert "Reviewed target" in capsys.readouterr().err
        return answer

    monkeypatch.setattr(sys.stdin, "readline", respond)
    accepted = answer.strip().lower() in {"y", "yes"}
    assert invoke(["-w", str(workspace), "deploy", str(manifest)]) == (0 if accepted else 130)
    engine.build_plan.assert_called_once()
    engine.deploy.assert_not_called()
    if accepted:
        assert engine.execute_plan.call_args.args[0] is result
    else:
        engine.execute_plan.assert_not_called()
        assert "No operations were submitted" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["pipe", "json", "ci"])
def test_noninteractive_deploy_requires_yes_before_source_or_target_access(
    prepared, monkeypatch, capsys, mode,
):
    workspace, manifest, _, engine = prepared
    monkeypatch.setattr(sys.stdin, "isatty", lambda: mode != "pipe")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    if mode == "ci":
        monkeypatch.setenv("CI", "true")
    arguments = ["-w", str(workspace), "deploy", str(manifest)]
    if mode == "json":
        arguments += ["--output", "json"]
    with patch.object(cli, "open_command_context", side_effect=AssertionError("No content access")):
        assert invoke(arguments) == 2
    output = capsys.readouterr()
    assert "--yes" in output.err
    assert output.out == ""
    engine.build_plan.assert_not_called()


def test_yes_executes_once_without_prompt_or_private_plan_in_json_streams(prepared, monkeypatch, capsys):
    workspace, manifest, result, engine = prepared
    monkeypatch.setattr(sys.stdin, "readline", Mock(side_effect=AssertionError("No prompt")))
    assert invoke(["-w", str(workspace), "deploy", str(manifest), "--yes", "--output", "json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["kind"] == "DeploymentRun"
    assert "Reviewed target" not in output.err
    cli.render_plain_plan.assert_not_called()
    engine.build_plan.assert_called_once()
    assert engine.execute_plan.call_args.args[0] is result
    engine.deploy.assert_not_called()


def test_authority_change_after_confirmation_blocks_execution(prepared, monkeypatch):
    workspace, manifest, _, engine = prepared
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "yes\n")
    from contextlib import nullcontext

    from siteops.command_context import CommandContext
    from siteops.project import ProjectError

    context = CommandContext(
        workspace, workspace,
        revalidate=Mock(side_effect=ProjectError("Source approval changed during review.")),
    )
    with patch.object(cli, "open_command_context", return_value=nullcontext(context)):
        assert invoke(["-w", str(workspace), "deploy", str(manifest)]) == 1
    engine.execute_plan.assert_not_called()


@pytest.mark.parametrize("phase", ["preparation", "review"])
def test_interrupted_preparation_or_review_submits_nothing(prepared, monkeypatch, capsys, phase):
    workspace, manifest, _, engine = prepared
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", Mock(side_effect=KeyboardInterrupt))
    if phase == "preparation":
        engine.build_plan.side_effect = KeyboardInterrupt
    assert invoke(["-w", str(workspace), "deploy", str(manifest)]) == 130
    engine.execute_plan.assert_not_called()
    assert "No operations were submitted" in capsys.readouterr().err


def test_terminal_controls_in_reviewed_content_are_not_executed(prepared, monkeypatch, capsys):
    workspace, manifest, _, _ = prepared
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "readline", lambda: "no\n")
    cli.render_plain_plan.return_value = "Target\x1b[2Jhidden\u202e\nNext\n"
    assert invoke(["-w", str(workspace), "deploy", str(manifest)]) == 130
    text = capsys.readouterr().err
    assert "\x1b" not in text and "\u202e" not in text
    assert "\\u001b" in text and "\\u202e" in text


def test_execution_interrupt_does_not_claim_no_submission(prepared, capsys):
    workspace, manifest, _, engine = prepared
    engine.execute_plan.side_effect = KeyboardInterrupt
    assert invoke(["-w", str(workspace), "deploy", str(manifest), "--yes"]) == 130
    output = capsys.readouterr().err
    assert "No operations were submitted" not in output
    assert "Inspect the targets" in output


def test_late_authority_failure_is_a_single_failed_json_result(prepared, capsys):
    from contextlib import nullcontext

    from siteops.command_context import CommandContext
    from siteops.project import ProjectError

    workspace, manifest, _, engine = prepared
    context = CommandContext(
        workspace, workspace,
        revalidate=Mock(side_effect=ProjectError("Source approval changed during preparation.")),
    )
    with patch.object(cli, "open_command_context", return_value=nullcontext(context)):
        assert invoke([
            "-w", str(workspace), "deploy", str(manifest), "--yes", "--output", "json",
        ]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "DeploymentRun" and result["status"] == "invalid"
    engine.execute_plan.assert_not_called()


@pytest.mark.parametrize("phase", ["acquisition", "cleanup"])
def test_context_interruption_reports_whether_deployment_could_have_started(prepared, capsys, phase):
    from contextlib import contextmanager

    from siteops.command_context import CommandContext

    workspace, manifest, _, engine = prepared

    @contextmanager
    def context(**kwargs):
        if phase == "acquisition":
            raise KeyboardInterrupt
        yield CommandContext(workspace, workspace)
        raise KeyboardInterrupt

    with patch.object(cli, "open_command_context", side_effect=context):
        try:
            status = invoke(["-w", str(workspace), "deploy", str(manifest), "--yes"])
        except KeyboardInterrupt:
            pytest.fail("CLI context interruption escaped controlled cancellation.")
    assert status == 130
    error = capsys.readouterr().err
    if phase == "acquisition":
        engine.build_plan.assert_not_called()
        engine.execute_plan.assert_not_called()
        assert "No operations were submitted" in error
    else:
        engine.execute_plan.assert_called_once()
        assert "No operations were submitted" not in error
        assert "Inspect" in error

"""Execute deploy template bodies with the reviewed standalone adapter."""

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from siteops.diagnostics import ProviderFailure
from siteops.orchestrator import Orchestrator
from siteops.planning import PlanBuildResult, PlanIntent, PlanProjection, PlanStatus, serialize_plan
from siteops.reporting import serialize_run_json
from siteops.results import OperationStatus
from tests.shell_helpers import bash_path, run_script, write_executable
from tests.test_provider_diagnostics import _operation, _run
from tests.yaml_documents import cached_safe_load

ROOT = Path(__file__).resolve().parents[1]


def _body(platform):
    if platform == "github":
        data = cached_safe_load((ROOT / ".github" / "workflows" / "_siteops-deploy.yaml").read_text())
        steps = data["jobs"]["deploy"]["steps"]
        return next(s["run"] for s in steps if s.get("name") == "Prepare executable deployment plan"), \
            next(s["run"] for s in steps if s.get("name") == "Deploy")
    data = cached_safe_load((ROOT / ".pipelines" / "templates" / "siteops-deploy.yaml").read_text())

    def find(value):
        if isinstance(value, dict):
            if str(value.get("task", "")).startswith("AzureCLI@"):
                return value["inputs"]["inlineScript"]
            for child in value.values():
                found = find(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = find(child)
                if found:
                    return found
        return None

    return find(data), None


@pytest.mark.parametrize("platform", ["github", "ado"])
@pytest.mark.parametrize("code", [1, 17, 130])
def test_real_deploy_templates_render_failure_groups_without_private_streams(tmp_path, platform, code):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "render-siteops-output.py", tmp_path / "scripts" / "render-siteops-output.py")
    python = bash_path(Path(sys.executable), preserve_symlinks=True)
    write_executable(tmp_path / "bin" / "python3", f'#!/usr/bin/env bash\nexec "{python}" "$@"\n')
    # Reuse an old supported plan to exercise the template's executable gate.
    plan = serialize_plan(PlanBuildResult(PlanStatus.INVALID, False, None, intent=PlanIntent.EXECUTABLE),
                          PlanProjection.PUBLISHABLE, engine_version="future")
    plan.update(status="planned", executable=True)
    run = json.loads(serialize_run_json(_run((
        _operation(OperationStatus.FAILED, "one", ProviderFailure("authorization-failed", "AuthorizationFailed")),
        _operation(OperationStatus.UNKNOWN, "two", ProviderFailure("timeout")),
    ), interrupted=code == 130), PlanProjection.PUBLISHABLE, engine_version="future"))
    run["exitCode"] = code
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    (tmp_path / "run.json").write_text(json.dumps(run))
    write_executable(tmp_path / "bin" / "siteops", """#!/usr/bin/env bash
echo "$*" >> arguments
echo 'FactoryBlue PRIVATE_PROVIDER_STDERR' >&2
if [[ "$2" == * ]]; then :; fi
for word in "$@"; do
  if [[ "$word" == plan ]]; then cat "$CASE_ROOT/plan.json"; exit 0; fi
  if [[ "$word" == deploy ]]; then cat "$CASE_ROOT/run.json"; exit "$RUN_EXIT"; fi
done
exit 99
""")
    state = tmp_path / "state"
    state.mkdir()
    summary = tmp_path / "summary.md"
    summary.write_text("")
    exports = {
        "CASE_ROOT": bash_path(tmp_path), "RUN_EXIT": str(code), "SITEOPS_REDACT_OUTPUT": "1",
        "GITHUB_WORKSPACE": bash_path(tmp_path), "TOOL_ROOT": bash_path(tmp_path),
        "GITHUB_STEP_SUMMARY": bash_path(summary), "RUNNER_TEMP": bash_path(state),
        "PLAN_TEMP_DIRECTORY": bash_path(state), "PLAN_SUMMARY_DIRECTORY": bash_path(tmp_path),
        "WORKSPACE": "caller workspace", "INPUT_WORKSPACE": "caller workspace",
        "MANIFEST": "manifests/private.yaml", "INPUT_MANIFEST": "manifests/private.yaml",
        "SELECTOR": "", "INPUT_SELECTOR": "", "SITE_FILE": "", "DRY_RUN": "False",
    }
    plan_body, run_body = _body(platform)
    if platform == "github":
        prepared = run_script(plan_body, tmp_path, exports)
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        result = run_script(run_body, tmp_path, exports)
        text = summary.read_text()
    else:
        result = run_script(plan_body, tmp_path, exports, shell_options=())
        text = (tmp_path / "deployment-result.md").read_text()
    assert result.returncode == code
    combined = result.stdout + result.stderr + text
    assert "failed: authorization-failed" in combined
    assert "unknown: timeout" in combined
    assert "AuthorizationFailed" in combined and "Confirm the provider state" in combined
    assert "FactoryBlue" not in combined and "PRIVATE_PROVIDER_STDERR" not in combined
    assert not list(state.iterdir())
    assert "--yes" in (tmp_path / "arguments").read_text()


@pytest.mark.parametrize("platform", ["github", "ado"])
@pytest.mark.parametrize("helper", [False, True])
@pytest.mark.parametrize("phase", ["plan", "deploy"])
def test_deploy_template_code_two_reports_fixed_mismatch_without_retry(tmp_path, platform, helper, phase):
    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts").mkdir()
    if helper or phase == "deploy":
        shutil.copyfile(ROOT / "scripts" / "render-siteops-output.py", tmp_path / "scripts" / "render-siteops-output.py")
    python = bash_path(Path(sys.executable), preserve_symlinks=True)
    write_executable(tmp_path / "bin" / "python3", f'#!/usr/bin/env bash\nexec "{python}" "$@"\n')
    plan = serialize_plan(PlanBuildResult(PlanStatus.INVALID, False, None, intent=PlanIntent.EXECUTABLE),
                          PlanProjection.PUBLISHABLE, engine_version="future")
    plan.update(status="planned", executable=True)
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    write_executable(tmp_path / "bin" / "siteops", """#!/usr/bin/env bash
echo call >> calls
echo FactoryBlue >&2
if [[ "$FAILURE_PHASE" == deploy ]]; then
  for word in "$@"; do
    if [[ "$word" == plan ]]; then cat "$CASE_ROOT/plan.json"; exit 0; fi
  done
  if [[ "$HELPER_PRESENT" == no ]]; then rm "$CASE_ROOT/scripts/render-siteops-output.py"; fi
fi
exit 2
""")
    (tmp_path / "state").mkdir()
    exports = {
        "CASE_ROOT": bash_path(tmp_path), "FAILURE_PHASE": phase, "HELPER_PRESENT": "yes" if helper else "no",
        "GITHUB_WORKSPACE": bash_path(tmp_path), "TOOL_ROOT": bash_path(tmp_path),
        "GITHUB_STEP_SUMMARY": bash_path(tmp_path / "summary.md"),
        "RUNNER_TEMP": bash_path(tmp_path / "state"), "PLAN_TEMP_DIRECTORY": bash_path(tmp_path / "state"),
        "PLAN_SUMMARY_DIRECTORY": bash_path(tmp_path), "WORKSPACE": "workspace", "INPUT_WORKSPACE": "workspace",
        "MANIFEST": "manifest.yaml", "INPUT_MANIFEST": "manifest.yaml", "SELECTOR": "", "INPUT_SELECTOR": "",
        "SITE_FILE": "", "DRY_RUN": "False",
    }
    plan_body, run_body = _body(platform)
    if platform == "github" and phase == "deploy":
        prepared = run_script(plan_body, tmp_path, exports, shell_options=())
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    result = run_script(run_body if platform == "github" and phase == "deploy" else plan_body,
                        tmp_path, exports, shell_options=())
    assert result.returncode == 2
    assert "Site Ops engine and template releases do not match." in result.stdout
    annotations = [line for line in result.stdout.splitlines() if re.match(r"::error::|##vso\[task.logissue", line)]
    assert len(annotations) == 1
    assert "FactoryBlue" not in result.stdout + result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == ["call"] * (2 if phase == "deploy" else 1)


@pytest.mark.parametrize("platform", ["github", "ado"])
def test_deploy_templates_render_preparation_roots_not_raw_json(tmp_path, platform):
    from siteops.diagnostics import ContentDiagnostic

    (tmp_path / "bin").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "state").mkdir()
    shutil.copyfile(ROOT / "scripts" / "render-siteops-output.py", tmp_path / "scripts" / "render-siteops-output.py")
    python = bash_path(Path(sys.executable), preserve_symlinks=True)
    write_executable(tmp_path / "bin" / "python3", f'#!/usr/bin/env bash\nexec "{python}" "$@"\n')
    result = Orchestrator._invalid_preparation(
        (ContentDiagnostic("content.duplicate-key", "FactoryBlue", "site"),), PlanIntent.EXECUTABLE)
    plan = serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version="future")
    plan["customerA"] = "PRIVATE_PROVIDER_STDERR"
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    write_executable(tmp_path / "bin" / "siteops", """#!/usr/bin/env bash
echo call >> calls
cat "$CASE_ROOT/plan.json"
echo FactoryBlue >&2
exit 1
""")
    exports = {
        "CASE_ROOT": bash_path(tmp_path), "GITHUB_WORKSPACE": bash_path(tmp_path), "TOOL_ROOT": bash_path(tmp_path),
        "GITHUB_STEP_SUMMARY": bash_path(tmp_path / "summary.md"), "RUNNER_TEMP": bash_path(tmp_path / "state"),
        "PLAN_TEMP_DIRECTORY": bash_path(tmp_path / "state"), "PLAN_SUMMARY_DIRECTORY": bash_path(tmp_path),
        "WORKSPACE": "caller workspace", "INPUT_WORKSPACE": "caller workspace", "MANIFEST": "private.yaml",
        "INPUT_MANIFEST": "private.yaml", "SELECTOR": "", "INPUT_SELECTOR": "", "SITE_FILE": "", "DRY_RUN": "False",
    }
    invocation = run_script(_body(platform)[0], tmp_path, exports, shell_options=())
    summary = (tmp_path / ("summary.md" if platform == "github" else "deployment-plan.md")).read_text()
    assert invocation.returncode == 1
    assert (tmp_path / "calls").read_text().splitlines() == ["call"]
    text = invocation.stdout + invocation.stderr + summary
    assert "content.duplicate-key" in text and "Merge the repeated entries" in text
    assert all(value not in text for value in ("FactoryBlue", "customerA", "PRIVATE_PROVIDER_STDERR", "```json"))
    assert not list((tmp_path / "state").iterdir())

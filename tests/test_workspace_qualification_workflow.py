"""Require the complete frozen engine matrix before workspace candidate review."""

import hashlib
import json
import sys

import pytest
import yaml

from tests.release_helpers import ROOT
from tests.shell_helpers import run_script, write_executable

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/_release-candidate.yaml").read_text())


def test_engine_selection_and_qualification_hold_only_read_permissions():
    for name in ("engine-input", "workspace-qualify", "workspace-qualified"):
        job = WORKFLOW["jobs"][name]
        assert job["permissions"] == {"contents": "read", "actions": "read"}
        assert "environment" not in job
    selected = WORKFLOW["jobs"]["engine-input"]
    assert selected["needs"] == ["prepare", "distribution"]
    assert "needs.prepare.outputs.workspaces == 'true'" in selected["if"]
    qualification = WORKFLOW["jobs"]["workspace-qualify"]
    assert qualification["strategy"]["matrix"] == "${{ fromJSON(needs.engine-input.outputs.matrix) }}"
    assert qualification["runs-on"] == "${{ matrix.os }}"
    assert qualification["strategy"]["max-parallel"] == 4
    assert qualification["env"]["PROJECT_WORKSPACE"] == "${{ needs.prepare.outputs.project-workspace }}"
    script = next(step["run"] for step in qualification["steps"]
                  if step.get("name") == "Qualify through the installed engine")
    assert '--project-workspace "$PROJECT_WORKSPACE"' in script
    assert '--plan "$temp/qualification-project-plan/plan.json"' in script
    assert "workspace-qualified" in WORKFLOW["jobs"]["review"]["needs"]
    assert "needs.workspace-qualified.result == 'success'" in WORKFLOW["jobs"]["review"]["if"]
    for row in qualification["steps"]:
        if row.get("uses", "").startswith("actions/download-artifact@"):
            assert row["with"]["run-id"] == "${{ github.run_id }}"
            assert row["with"]["digest-mismatch"] == "error"


@pytest.mark.parametrize("fault", [
    None, "missing", "extra", "engine", "workspace", "plan", "target", "count", "deployment",
    "project-missing", "project-workspace", "project-pin", "project-publication",
])
def test_complete_qualification_results_are_bound_to_exact_inputs(tmp_path, fault):
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "python3", '#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n')
    temporary = tmp_path / "runner"
    identities = temporary / "qualification-identity"
    plans = temporary / "qualification-plan"
    results = temporary / "qualification-results"
    for path in (identities, plans, results):
        path.mkdir(parents=True)
    source = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
    plan = {
        "source": source,
        "workspaces": [{
            "workspace": "workspace", "id": "fixture", "package": "workspace.zip",
            "compatibility": {"siteops": ">=1.0.0b1,<2"}, "licenses": ["LICENSE"],
        }],
    }
    plan_raw = json.dumps(plan).encode()
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    targets = [{"python": "3.11", "platform": platform} for platform in ("linux-x86_64", "windows-x86_64")]
    engine = {"candidate": source, "planSha256": plan_sha, "version": "1.0.0b1", "targets": targets}
    raw = json.dumps(engine).encode()
    engine_sha = hashlib.sha256(raw).hexdigest()
    (identities / "workspace-engine.json").write_bytes(raw)
    (plans / "plan.json").write_bytes(plan_raw)
    for index, target in enumerate(targets):
        if fault == "missing" and index == 1:
            continue
        path = results / f"workspace-qualification-42-3-{target['platform']}-{target['python']}"
        path.mkdir()
        report = {
            "apiVersion": "siteops.release.qualification/v1", "kind": "WorkspaceEngineQualification",
            "engineVersion": "1.0.0b1", "engineSelectionSha256": engine_sha,
            "workspaceInventorySha256": "c" * 64, "planSha256": plan_sha,
            "target": target, "packages": 1, "catalogManifests": 2,
            "deployment": "not-run", "workloadHealth": "not-checked",
            "project": {
                "workspace": "workspace", "pinSha256": "f" * 64,
                "sourceReleaseObservation": "not-performed",
            },
        }
        if index == 0:
            if fault in {"engine", "workspace", "plan"}:
                key = {"engine": "engineSelectionSha256", "workspace": "workspaceInventorySha256", "plan": "planSha256"}[fault]
                report[key] = "d" * 64
            elif fault == "target":
                report["target"] = {"python": "3.12", "platform": target["platform"]}
            elif fault == "count":
                report["packages"] = True
            elif fault == "deployment":
                report["deployment"] = "succeeded"
            elif fault == "project-missing":
                del report["project"]
            elif fault == "project-workspace":
                report["project"]["workspace"] = "other"
            elif fault == "project-pin":
                report["project"]["pinSha256"] = "a" * 64
            elif fault == "project-publication":
                report["project"]["sourceReleaseObservation"] = "published"
        (path / "workspace-qualification.json").write_text(json.dumps(report))
    if fault == "extra":
        (results / "unexpected").mkdir()
    body = next(
        step["run"] for step in WORKFLOW["jobs"]["workspace-qualified"]["steps"]
        if step["name"] == "Require the complete frozen qualification matrix"
    )
    result = run_script(body, tmp_path, {
        "RUNNER_TEMP": temporary.as_posix(), "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3",
        "EXPECTED_ENGINE_SHA": engine_sha, "EXPECTED_WORKSPACE_SHA": "c" * 64,
        "EXPECTED_PLAN_SHA": plan_sha,
        "TEST_PYTHON": sys.executable.replace("\\", "/"),
    })
    assert result.returncode == (0 if fault is None else 1), result.stdout + result.stderr
    if fault is not None:
        assert "::error::" in result.stdout + result.stderr

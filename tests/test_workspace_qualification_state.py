"""Select and protect qualification state before installed workspace use."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from siteops.artifacts import ArtifactError
from tests.release_helpers import ROOT, SCRIPTS

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/_release-candidate.yaml").read_text())
STEP = next(
    row for row in WORKFLOW["jobs"]["workspace-qualify"]["steps"]
    if row.get("name") == "Select private qualification state"
)


def controller():
    spec = importlib.util.spec_from_file_location("qualification_state", SCRIPTS / "qualify-workspace-engine.py")
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as context:
        context.syspath_prepend(str(SCRIPTS))
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "runner,key,existing",
    [("Windows", "LOCALAPPDATA", False), ("Linux", "RUNNER_TEMP", False), ("Windows", "LOCALAPPDATA", True)],
)
def test_workflow_selects_a_new_platform_private_root(tmp_path, monkeypatch, runner, key, existing):
    output = tmp_path / "environment"
    roots = {"LOCALAPPDATA": tmp_path / "local", "RUNNER_TEMP": tmp_path / "temporary"}
    for root in roots.values():
        root.mkdir()
    platform = "windows-x86_64" if runner == "Windows" else "linux-x86_64"
    selected = roots[key] / f"siteops-qualification-42-3-{platform}-3.11"
    if existing:
        selected.mkdir()
    values = {
        "RUNNER_OS": runner, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3",
        "QUALIFICATION_PLATFORM": platform, "QUALIFICATION_PYTHON": "3.11",
        "GITHUB_ENV": str(output), **{name: str(value) for name, value in roots.items()},
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    if existing:
        with pytest.raises(SystemExit, match="::error::"):
            exec(compile(STEP["run"], "<qualification-state>", "exec"), {})
        assert not output.exists()
    else:
        exec(compile(STEP["run"], "<qualification-state>", "exec"), {})
        assert Path(output.read_text().strip().split("=", 1)[1]) == selected
        assert not selected.exists()


@pytest.mark.parametrize("reject", [False, True])
def test_controller_protects_state_and_preflights_future_cache(tmp_path, monkeypatch, reject):
    module = controller()
    engine, workspaces, state = (tmp_path / name for name in ("engine", "workspaces", "state"))
    engine.mkdir()
    workspaces.mkdir()
    checked = []

    def observe(path):
        checked.append(path)
        if reject:
            raise ArtifactError("Controlled unsafe ancestor.")

    monkeypatch.setattr(module, "check_cache_ancestors", observe)
    if reject:
        with pytest.raises(ArtifactError, match="unsafe ancestor"):
            module.create_qualification_state(state, (engine, workspaces))
        assert not state.exists()
    else:
        module.create_qualification_state(state, (engine, workspaces))
        assert state.is_dir()
    assert checked == [state / "probe-state"]


@pytest.mark.parametrize("fault", [None, "digest", "source", "missing-workspace", "duplicate-workspace", "tag"])
def test_project_selection_is_bound_to_the_reviewed_plan_before_state_creation(tmp_path, fault):
    module = controller()
    source = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True,
        "source": dict(source), "release": {"tag": "v1.0.0b7"},
        "workspaces": [{"workspace": "workspace"}],
    }
    if fault == "source":
        plan["source"]["commit"] = "b" * 40
    elif fault == "missing-workspace":
        plan["workspaces"] = []
    elif fault == "duplicate-workspace":
        plan["workspaces"].append({"workspace": "workspace"})
    elif fault == "tag":
        plan["release"]["tag"] = "invalid tag"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    digest = "0" * 64 if fault == "digest" else hashlib.sha256(path.read_bytes()).hexdigest()
    if fault:
        with pytest.raises(ArtifactError):
            module.project_release(path, digest, source, "workspace")
    else:
        assert module.project_release(path, digest, source, "workspace") == "v1.0.0b7"
    assert list(tmp_path.iterdir()) == [path]

"""Exercise the hosted smoke entry with real caller files and an installed engine."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.ado_helpers import TEMPLATES, nodes
from tests.installed_runtime import build_engine_wheel, install_engine
from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".pipelines" / "validate-pipelines.yaml"
GITHUB_CI = ROOT / ".github" / "workflows" / "ci.yaml"
GIT_SOURCE = "siteops @ git+https://github.com/${{ github.repository }}@${{ github.sha }}"
SMOKE_STAGES = TEMPLATES / "consumer-smoke.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "ado-consumer"
RELEASED = "tests/fixtures/compatibility/v1.0.0b7/ado-consumer"
STAGES = ["validate_selector", "validate_site_file", "validate_b7_selector", "validate_b7_site_file"]


def smoke_cases():
    return yaml.safe_load(SMOKE_STAGES.read_text(encoding="utf-8"))["stages"]


def test_smoke_pipeline_reuses_the_consumer_validation_template():
    document = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    assert document["trigger"] == document["pr"] == "none"
    assert document["pool"]["vmImage"] == "ubuntu-24.04"
    assert document["variables"] == {"SITE_OVERRIDES": ""}
    assert {"template": "templates/consumer-smoke.yaml"} in document["stages"]
    cases = smoke_cases()
    assert [case["parameters"]["stageName"] for case in cases] == STAGES
    for case, fixture in zip(cases, ["tests/fixtures/ado-consumer"] * 2 + [RELEASED] * 2, strict=True):
        assert case["template"] == "siteops-validate.yaml"
        options = case["parameters"]
        assert options["siteopsSource"] == "$(Build.SourcesDirectory)"
        assert options["workspace"] == f"{fixture}/workspace"
        assert options["manifest"] == "manifests/smoke.yaml"
        assert bool(options.get("selector")) != bool(options.get("siteFile"))
    # The frozen fixture runs the same selections as the current one.
    current, released = cases[:2], cases[2:]
    for now, then in zip(current, released, strict=True):
        assert {key: value.replace(RELEASED, "tests/fixtures/ado-consumer")
                for key, value in then["parameters"].items() if key != "stageName"} == {
            key: value for key, value in now["parameters"].items() if key != "stageName"
        }
    selector, site_file = (case["parameters"] for case in current)
    assert selector["selector"] == "environment=smoke"
    assert site_file["siteFile"] == "tests/fixtures/ado-consumer/operator/site.yaml"
    assert (ROOT / RELEASED / "operator" / "site.yaml").is_file()
    all_nodes = list(nodes(SMOKE_STAGES))
    assert not any(
        node.get("task", "").startswith("AzureCLI")
        or "environment" in node
        or "group" in node
        or node.get("continueOnError")
        for node in all_nodes
    )
    assert not any("SYSTEM_ACCESSTOKEN" in node.get("env", {}) for node in all_nodes)


def test_preview_failure_does_not_schedule_or_gate_the_consumer_lane():
    document = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    stages = document["stages"]
    assert stages[0] == {"template": "templates/consumer-smoke.yaml"}
    preview = next(stage for stage in stages if stage.get("stage") == "preview")
    assert preview["dependsOn"] == []
    consumer = yaml.safe_load((TEMPLATES / "siteops-validate.yaml").read_text(encoding="utf-8"))
    assert "dependsOn" not in consumer["stages"][0]
    assert [case["parameters"]["stageName"] for case in smoke_cases()] == STAGES
    report = next(stage for stage in stages if stage.get("stage") == "qualification_report")
    assert set(report["dependsOn"]) == {"preview", *STAGES}
    assert report["condition"] == "always()"


@pytest.fixture(scope="module")
def installed_smoke_engine(tmp_path_factory):
    root = tmp_path_factory.mktemp("ado-smoke")
    wheel = build_engine_wheel(root)
    return install_engine(root / "installed", wheel)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("fault", [None, "missing-template", "invalid-site"])
def test_smoke_inputs_validate_with_the_installed_engine(
    installed_smoke_engine, tmp_path, stage, fault,
):
    caller = tmp_path / "caller"
    for fixture in ("tests/fixtures/ado-consumer", RELEASED):
        shutil.copytree(ROOT / fixture, caller / fixture)
    assert not (caller / "pyproject.toml").exists()
    assert not (caller / "siteops").exists()
    assert not (caller / "scripts").exists()
    options = next(case["parameters"] for case in smoke_cases()
                   if case["parameters"]["stageName"] == stage)
    workspace = caller / options["workspace"]
    selected_site = (
        caller / options["siteFile"]
        if options.get("siteFile") else workspace / "sites" / "selected.yaml"
    )
    if fault == "missing-template":
        (workspace / "templates" / "empty.template.json").unlink()
    elif fault == "invalid-site":
        selected_site.write_text("name: [invalid]\n", encoding="utf-8")
    arguments = ["-w", str(workspace), "validate", options["manifest"]]
    if options.get("siteFile"):
        arguments += ["--site-file", str(selected_site)]
    else:
        arguments += ["-l", options["selector"]]
    installed_smoke_engine.run(*arguments, expected=1 if fault else 0)


def _pip_consumer():
    return yaml.safe_load(GITHUB_CI.read_text(encoding="utf-8"))["jobs"]["pip-consumer"]


def _pip_step(name):
    return next(step for step in _pip_consumer()["steps"] if step["name"] == name)


def test_github_pip_consumer_installs_this_commit_from_its_git_url():
    job = _pip_consumer()
    ci = yaml.safe_load(GITHUB_CI.read_text(encoding="utf-8"))["jobs"]
    assert job["permissions"] == {"contents": "read"}
    assert job["if"] == ci["linux-bootstrap"]["if"]
    checkout, setup, *_ = job["steps"]
    assert checkout["uses"] == ci["test"]["steps"][0]["uses"]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["sparse-checkout"].split() == [
        ".github/actions", "tests/fixtures/ado-consumer", "tests/fixtures/compatibility",
    ]
    assert setup["uses"] == "./.github/actions/setup-siteops"
    assert setup["with"] == {"siteops-source": GIT_SOURCE}
    check = _pip_step("Check the installed source")
    assert check["env"] == {
        "SOURCE_URL": "https://github.com/${{ github.repository }}", "SOURCE_SHA": "${{ github.sha }}",
    }


def test_github_pip_consumer_runs_each_ado_smoke_selection_through_validate_and_describe(tmp_path):
    (tmp_path / RELEASED).mkdir(parents=True)
    expected = []
    current = [case for case in smoke_cases() if case["parameters"]["workspace"].startswith("tests/fixtures/ado-")]
    for fixture in ("tests/fixtures/ado-consumer", RELEASED):
        for case in current:
            options = {key: value.replace("tests/fixtures/ado-consumer", fixture)
                       for key, value in case["parameters"].items()}
            selection = (["--site-file", options["siteFile"]] if options.get("siteFile")
                         else ["-l", options["selector"]])
            expected.append(["-w", options["workspace"], "validate", options["manifest"], *selection])
    expected += [[*command[:2], "plan", command[3], "--describe", *command[4:]] for command in expected]
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "siteops", (
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$(printf \'%s\\x1f\' "$@")" >> "$CALLS"\n'
        'count=$(wc -l < "$CALLS")\n[[ "$count" != "$FAIL_AT" ]]\n'
    ))
    body = _pip_step("Validate and describe consumer content")["run"]
    calls = tmp_path / "calls"
    result = run_script(body, tmp_path, {"CALLS": bash_path(calls), "FAIL_AT": "0"})
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = [line.rstrip("\x1f").split("\x1f") for line in calls.read_text().splitlines()]
    assert sorted(recorded) == sorted(expected)
    calls.unlink()
    failed = run_script(body, tmp_path, {"CALLS": bash_path(calls), "FAIL_AT": "2"})
    assert failed.returncode != 0
    assert len(calls.read_text().splitlines()) == 2


@pytest.mark.parametrize("case", ["exact", "other-commit", "editable", "other-url", "no-origin", "checkout"])
def test_github_pip_consumer_accepts_only_the_exact_git_installation(tmp_path, case):
    body = _pip_step("Check the installed source")["run"]
    script = body.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / "check").mkdir()
    (tmp_path / "check" / "check.py").write_text(script, encoding="utf-8")
    checkout = tmp_path / "checkout"
    packages = checkout / "site" if case == "checkout" else tmp_path / "site"
    (packages / "siteops").mkdir(parents=True)
    (packages / "siteops" / "__init__.py").write_text('__version__ = "1.0.0b1"\n', encoding="utf-8")
    metadata = packages / "siteops-1.0.0b1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: siteops\nVersion: 1.0.0b1\n", encoding="utf-8")
    origin = {
        "url": "https://github.com/example/scale-kit",
        "vcs_info": {"vcs": "git", "requested_revision": "a" * 40, "commit_id": "a" * 40},
    }
    if case == "other-commit":
        origin["vcs_info"]["commit_id"] = "b" * 40
    elif case == "editable":
        origin = {"url": "file:///checkout", "dir_info": {"editable": True}}
    elif case == "other-url":
        origin["url"] = "https://github.com/other/scale-kit"
    if case != "no-origin":
        (metadata / "direct_url.json").write_text(json.dumps(origin), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "check" / "check.py")], cwd=tmp_path, capture_output=True, text=True,
        env={"PYTHONPATH": str(packages), "SOURCE_URL": "https://github.com/example/scale-kit",
             "SOURCE_SHA": "a" * 40, "GITHUB_WORKSPACE": str(checkout),
             **{key: os.environ[key] for key in ("SYSTEMROOT",) if key in os.environ}},
        timeout=30,
    )
    assert (result.returncode == 0) is (case in ("exact",)), result.stdout + result.stderr
    if case != "exact":
        assert "::error::Site Ops was not installed from this commit's Git URL." in result.stdout + result.stderr

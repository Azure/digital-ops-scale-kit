"""Current templates and engine keep the Azure Pipelines contract of released callers."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from siteops import cli
from tests.ado_helpers import TEMPLATES

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "v1.0.0b7"
RELEASE_COMMIT = "19c8fefb1080f89eba31325449c77c4f00f93890"
BASELINE = ROOT / "tests" / "fixtures" / "compatibility" / RELEASE
SNAPSHOT = json.loads((BASELINE / "ado-templates.json").read_text(encoding="utf-8"))
CALLER = ROOT / "tests" / "fixtures" / "ado-consumer"
SAMPLE = CALLER / "azure-pipelines.yaml"
TYPES = {bool: "boolean", str: "string"}


def _declared(path):
    return {row["name"]: row for row in yaml.safe_load(path.read_text(encoding="utf-8"))["parameters"]}


def _released(template):
    return {row["name"]: row for row in SNAPSHOT["templates"][template]}


def test_snapshot_records_the_supported_caller_templates():
    assert SNAPSHOT["release"] == RELEASE
    assert sorted(SNAPSHOT["templates"]) == ["setup-siteops.yaml", "siteops-deploy.yaml", "siteops-validate.yaml"]
    assert sum(len(rows) for rows in SNAPSHOT["templates"].values()) == 33


def _tagged_commit(root=ROOT):
    tag = subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", f"refs/tags/{RELEASE}^{{commit}}"],
                         capture_output=True, text=True, timeout=30)
    if tag.returncode:
        # Both CI unit jobs fetch the tag and set this flag, so a missing baseline fails there.
        if os.environ.get("SITEOPS_REQUIRE_COMPATIBILITY_TAG") == "1":
            pytest.fail(f"Fetch {RELEASE} from Azure/digital-ops-scale-kit to check the compatibility baseline.")
        pytest.skip(f"{RELEASE} is not fetched in this checkout.")
    return tag.stdout.strip()


def test_snapshot_matches_the_release_tag():
    assert _tagged_commit() == RELEASE_COMMIT

    def show(path):
        return subprocess.run(["git", "-C", str(ROOT), "show", f"{RELEASE_COMMIT}:{path}"],
                              capture_output=True, check=True, timeout=30).stdout

    for template, rows in SNAPSHOT["templates"].items():
        tagged = yaml.safe_load(show(f".pipelines/templates/{template}"))["parameters"]
        assert rows == [{key: row[key] for key in ("name", "type", "default", "values") if key in row}
                        for row in tagged]
    for frozen, tagged, count in (
        (BASELINE / "ado-consumer", "tests/fixtures/ado-consumer", 4),
        (BASELINE / "saved-data" / "index", "tests/fixtures/browse-workspace", 6),
    ):
        copied = sorted(path for path in frozen.rglob("*") if path.is_file() and not path.name.startswith("siteops-index"))
        assert len(copied) == count
        for path in copied:
            relative = path.relative_to(frozen).as_posix()
            assert path.read_bytes() == show(f"{tagged}/{relative}"), relative


@pytest.mark.parametrize("platform", ["github", "ado"])
def test_both_unit_jobs_fetch_the_release_tag_and_require_it(platform):
    if platform == "github":
        steps = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
        names = [step.get("name") for step in steps]
        bodies = {step.get("name"): step.get("run", "") for step in steps}
        environment = steps[names.index("Run unit tests")].get("env", {})
    else:
        jobs = yaml.safe_load((ROOT / ".pipelines/ci.yaml").read_text(encoding="utf-8"))["jobs"]
        steps = next(job for job in jobs if job["job"] == "test")["steps"]
        names = [step.get("displayName") for step in steps]
        bodies = {step.get("displayName"): step.get("script", "") for step in steps}
        environment = steps[names.index("Run unit tests")].get("env", {})
    fetch = f"Fetch the {RELEASE} compatibility baseline"
    assert names.index(fetch) < names.index("Run unit tests")
    command = shlex.split(bodies[fetch].replace("\\\n", " "))
    assert command[command.index("git"):] == [
        "git", "fetch", "--quiet", "--no-tags", "--depth=1", "https://github.com/Azure/digital-ops-scale-kit",
        f"+refs/tags/{RELEASE}:refs/tags/{RELEASE}",
    ]
    assert bodies[fetch].startswith("set -euo pipefail\n")
    assert environment == {"SITEOPS_REQUIRE_COMPATIBILITY_TAG": "1"}


def test_a_missing_release_tag_fails_only_where_ci_requires_it(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True, timeout=30)
    monkeypatch.delenv("SITEOPS_REQUIRE_COMPATIBILITY_TAG", raising=False)
    with pytest.raises(pytest.skip.Exception):
        _tagged_commit(checkout)
    monkeypatch.setenv("SITEOPS_REQUIRE_COMPATIBILITY_TAG", "1")
    with pytest.raises(pytest.fail.Exception, match=f"Fetch {RELEASE}"):
        _tagged_commit(checkout)


def _incompatibilities(template, released, current):
    """Name each change that could break a caller that passes only the released parameters."""
    problems = []
    for name, row in released.items():
        if name not in current:
            problems.append(f"{template} no longer accepts the {RELEASE} parameter {name}.")
            continue
        now = current[name]
        if now["type"] != row["type"]:
            problems.append(f"{template} changed the type of {name}.")
        # A released parameter without a default may gain one, because every caller passes it.
        if "default" in row and ("default" not in now or now["default"] != row["default"]):
            problems.append(f"{template} changed the default of {name}.")
        if "values" in now and not ("values" in row and set(row["values"]) <= set(now["values"])):
            problems.append(f"{template} narrowed {name}.")
    for name in sorted(current.keys() - released.keys()):
        if "default" not in current[name]:
            problems.append(f"{template} added {name} without a default.")
    return problems


@pytest.mark.parametrize("template", sorted(SNAPSHOT["templates"]))
def test_current_templates_accept_every_released_parameter(template):
    assert _incompatibilities(template, _released(template), _declared(TEMPLATES / template)) == []


@pytest.mark.parametrize("change,problem", [
    ("add-default", None),
    ("add-value", None),
    ("add-optional", None),
    ("remove", "no longer accepts the v1.0.0b7 parameter manifest."),
    ("retype", "changed the type of manifest."),
    ("change-default", "changed the default of dryRun."),
    ("drop-default", "changed the default of dryRun."),
    ("narrow", "narrowed stage."),
    ("restrict", "narrowed manifest."),
    ("add-required", "added extra without a default."),
])
def test_released_parameter_rules_accept_only_compatible_changes(change, problem):
    released = {
        "manifest": {"name": "manifest", "type": "string"},
        "dryRun": {"name": "dryRun", "type": "boolean", "default": True},
        "stage": {"name": "stage", "type": "string", "default": "a", "values": ["a", "b"]},
    }
    current = {name: dict(row) for name, row in released.items()}
    if change == "add-default":
        current["manifest"]["default"] = "manifests/smoke.yaml"
    elif change == "add-value":
        current["stage"]["values"] = ["a", "b", "c"]
    elif change == "add-optional":
        current["extra"] = {"name": "extra", "type": "string", "default": ""}
    elif change == "remove":
        del current["manifest"]
    elif change == "retype":
        current["manifest"]["type"] = "object"
    elif change == "change-default":
        current["dryRun"]["default"] = False
    elif change == "drop-default":
        del current["dryRun"]["default"]
    elif change == "narrow":
        current["stage"]["values"] = ["a"]
    elif change == "restrict":
        current["manifest"]["values"] = ["manifests/smoke.yaml"]
    else:
        current["extra"] = {"name": "extra", "type": "string"}
    problems = _incompatibilities("siteops-validate.yaml", released, current)
    assert problems == ([] if problem is None else [f"siteops-validate.yaml {problem}"])


@pytest.fixture
def released_caller(tmp_path):
    root = tmp_path / "caller"
    shutil.copytree(BASELINE / "ado-consumer", root)
    return root


def _run(monkeypatch, capsys, workspace, command, *options):
    argv = ["siteops", "-w", str(workspace), command, "manifests/smoke.yaml",
            "--output", "json", "--projection", "publishable", *options]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    return caught.value.code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("command", ["validate", "plan"])
@pytest.mark.parametrize("selection", ["selector", "site-file"])
def test_engine_validates_and_plans_the_released_caller(
    released_caller, deterministic_local_tools, monkeypatch, capsys, command, selection,
):
    options = (["-l", "environment=smoke"] if selection == "selector"
               else ["--site-file", str(released_caller / "operator" / "site.yaml")])
    code, document = _run(monkeypatch, capsys, released_caller / "workspace", command, *options)
    assert code == 0, document
    assert document["diagnostics"] == []
    if command == "validate":
        assert document["status"] == "valid"
        assert document["summary"]["targetResolution"] == "complete"
    else:
        assert document["status"] == "planned" and document["executable"] is True
        assert document["summary"]["targetCount"] == 1
        assert document["summary"]["dispositions"] == {"execute": 1, "skip": 0, "blocked": 0}


def _documented_caller():
    guide = (ROOT / "docs" / "ci-cd-setup.md").read_text(encoding="utf-8")
    section = guide.split("### Reference the deployment template from another repository", 1)[1]
    return yaml.safe_load(re.search(r"```yaml\n(.*?)```", section, re.DOTALL)[1])


def _callers():
    return {"sample": yaml.safe_load(SAMPLE.read_text(encoding="utf-8")), "guide": _documented_caller()}


@pytest.mark.parametrize("caller", ["sample", "guide"])
def test_external_caller_references_the_templates_at_a_release_tag(caller):
    document = _callers()[caller]
    assert document["trigger"] == document["pr"] == "none"
    (repository,) = document["resources"]["repositories"]
    assert repository["type"] == "github" and repository["name"] == "Azure/digital-ops-scale-kit"
    assert repository["endpoint"] == "scalekit-github"
    assert repository["ref"] == "refs/tags/<reviewed-release>"
    assert {"name": "SITE_OVERRIDES", "value": ""} in document["variables"]
    alias, stages = repository["repository"], document["stages"]
    for stage in stages:
        path, _, owner = stage["template"].partition("@")
        assert owner == alias and path.startswith("/.pipelines/templates/")
        assert (ROOT / path.lstrip("/")).is_file()
        assert stage["parameters"]["templateRepository"] == alias
    names = [stage["parameters"].get("stageName", Path(stage["template"]).stem) for stage in stages]
    assert len(names) == len(set(names))
    assert {Path(stage["template"].partition("@")[0]).name for stage in stages} == {
        "siteops-validate.yaml", "siteops-deploy.yaml",
    }


@pytest.mark.parametrize("caller", ["sample", "guide"])
@pytest.mark.parametrize("baseline", ["current", RELEASE])
def test_external_caller_arguments_fit_each_template_contract(caller, baseline):
    document = _callers()[caller]
    own = {row["name"]: row["type"] for row in document.get("parameters", [])}
    checked = 0
    for stage in document["stages"]:
        template = Path(stage["template"].partition("@")[0]).name
        declared = _declared(TEMPLATES / template) if baseline == "current" else _released(template)
        arguments = stage["parameters"]
        assert set(arguments) <= set(declared)
        assert {name for name, row in declared.items() if "default" not in row} <= set(arguments)
        for name, value in arguments.items():
            reference = re.fullmatch(r"\$\{\{ parameters\.(\w+) \}\}", str(value))
            assert (own[reference[1]] if reference else TYPES[type(value)]) == declared[name]["type"], name
            checked += 1
    assert checked >= 10


@pytest.mark.parametrize("stage", range(2))
def test_sample_caller_validation_stages_accept_their_inputs(monkeypatch, capsys, stage):
    arguments = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))["stages"][stage]["parameters"]
    options = ["--site-file", str(CALLER / arguments["siteFile"])] if "siteFile" in arguments else []
    options += ["-l", arguments["selector"]] if "selector" in arguments else []
    code, document = _run(monkeypatch, capsys, CALLER / arguments["workspace"], "validate", *options)
    assert code == 0 and document["status"] == "valid", document

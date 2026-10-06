"""Exercise the hosted smoke entry with real caller files and an installed engine."""

import shutil
from pathlib import Path

import pytest
import yaml

from tests.ado_helpers import TEMPLATES, nodes
from tests.installed_runtime import build_engine_wheel, install_engine

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / ".pipelines" / "validate-pipelines.yaml"
SMOKE_STAGES = TEMPLATES / "consumer-smoke.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "ado-consumer"


def smoke_cases():
    return yaml.safe_load(SMOKE_STAGES.read_text(encoding="utf-8"))["stages"]


def test_smoke_pipeline_reuses_the_consumer_validation_template():
    document = yaml.safe_load(PIPELINE.read_text(encoding="utf-8"))
    assert document["trigger"] == document["pr"] == "none"
    assert document["pool"]["vmImage"] == "ubuntu-24.04"
    assert document["variables"] == {"SITE_OVERRIDES": ""}
    assert {"template": "templates/consumer-smoke.yaml"} in document["stages"]
    cases = smoke_cases()
    assert len(cases) == 2
    assert {case["parameters"]["stageName"] for case in cases} == {
        "validate_selector", "validate_site_file",
    }
    for case in cases:
        assert case["template"] == "siteops-validate.yaml"
        options = case["parameters"]
        assert options["siteopsSource"] == "$(Build.SourcesDirectory)"
        assert options["workspace"] == "tests/fixtures/ado-consumer/workspace"
        assert options["manifest"] == "manifests/smoke.yaml"
        assert bool(options.get("selector")) != bool(options.get("siteFile"))
    selector, site_file = (case["parameters"] for case in cases)
    assert selector["selector"] == "environment=smoke"
    assert site_file["siteFile"] == "tests/fixtures/ado-consumer/operator/site.yaml"
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
    assert [case["parameters"]["stageName"] for case in smoke_cases()] == [
        "validate_selector", "validate_site_file",
    ]
    report = next(stage for stage in stages if stage.get("stage") == "qualification_report")
    assert set(report["dependsOn"]) == {"preview", "validate_selector", "validate_site_file"}
    assert report["condition"] == "always()"


@pytest.fixture(scope="module")
def installed_smoke_engine(tmp_path_factory):
    root = tmp_path_factory.mktemp("ado-smoke")
    wheel = build_engine_wheel(root)
    return install_engine(root / "installed", wheel)


@pytest.mark.parametrize("stage", ["validate_selector", "validate_site_file"])
@pytest.mark.parametrize("fault", [None, "missing-template", "invalid-site"])
def test_smoke_inputs_validate_with_the_installed_engine(
    installed_smoke_engine, tmp_path, stage, fault,
):
    caller = tmp_path / "caller"
    content = caller / "tests" / "fixtures" / "ado-consumer"
    shutil.copytree(FIXTURE, content)
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

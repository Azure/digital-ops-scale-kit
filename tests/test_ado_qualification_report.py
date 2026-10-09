"""Keep the consolidated maintainer result complete, source-bound and value-safe."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.installed_runtime import isolated_environment

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
NAMES = ["ci", "deploy-dev-plan"]


@pytest.fixture
def qualification():
    spec = importlib.util.spec_from_file_location(
        "ado_qualification", ROOT / "scripts" / "summarize-ado-qualification.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview_receipt(tmp_path):
    path = tmp_path / "preview.json"
    path.write_text(json.dumps({
        "sourceCommit": SOURCE, "status": "passed", "expectedCases": NAMES,
        "cases": [
            {"case": name, "status": "passed", "inputSha256": "b" * 64, "expandedSha256": "c" * 64}
            for name in NAMES
        ],
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("job", ["preview", "validate_selector", "validate_site_file"])
@pytest.mark.parametrize("result", ["Succeeded", "SucceededWithIssues", "Failed", "Skipped", "Canceled", ""])
def test_every_required_job_must_succeed(qualification, preview_receipt, job, result):
    results = dict.fromkeys(qualification.CHECKS, "Succeeded")
    results[job] = result
    report = qualification.summarize(SOURCE, results, preview_receipt, "Succeeded")
    assert report["status"] == ("passed" if result == "Succeeded" else "failed")
    assert report["sourceCommit"] == SOURCE
    assert report["engineSelection"] == {"kind": "source-checkout", "sourceCommit": SOURCE}
    assert {item["case"] for item in report["checks"]} == set(qualification.CHECKS)
    assert next(item for item in report["checks"] if item["case"] == job)["result"] == (result or "Missing")
    assert bool(report["templatePreviews"]) is (results["preview"] == "Succeeded")
    assert "verified-release-installation" in report["notExercised"]


@pytest.mark.parametrize("fault", [
    "missing", "source", "status", "empty", "partial", "duplicate", "digest",
    "extra-field", "private-name", "malformed", "oversized", "duplicate-field",
])
def test_an_invalid_preview_receipt_cannot_make_a_green_report(
    qualification, preview_receipt, fault,
):
    document = json.loads(preview_receipt.read_text())
    if fault == "source":
        document["sourceCommit"] = "d" * 40
    elif fault == "status":
        document["status"] = "failed"
    elif fault == "empty":
        document["cases"] = []
        document["expectedCases"] = []
    elif fault == "partial":
        document["cases"].pop()
    elif fault == "duplicate":
        document["expectedCases"][1] = document["expectedCases"][0]
        document["cases"][1] = document["cases"][0]
    elif fault == "digest":
        document["cases"][0]["expandedSha256"] = "private-marker"
    elif fault == "extra-field":
        document["cases"][0]["private"] = "private-marker"
    elif fault == "private-name":
        document["expectedCases"][0] = document["cases"][0]["case"] = "private marker\n##vso[command]"
    preview_receipt.write_text(json.dumps(document), encoding="utf-8")
    if fault == "missing":
        preview_receipt.unlink()
    elif fault == "malformed":
        preview_receipt.write_text("private-marker", encoding="utf-8")
    elif fault == "oversized":
        preview_receipt.write_bytes(
            json.dumps(document).encode().ljust(qualification.MAX_RECEIPT + 1, b" "),
        )
    elif fault == "duplicate-field":
        preview_receipt.write_text(
            json.dumps(document)[:-1] + ', "sourceCommit": "' + SOURCE + '"}',
            encoding="utf-8",
        )
    report = qualification.summarize(
        SOURCE, dict.fromkeys(qualification.CHECKS, "Succeeded"), preview_receipt, "Succeeded",
    )
    assert report["status"] == "failed"
    assert report["diagnostics"] == ["preview-receipt-invalid"]
    assert report["templatePreviews"] == []
    assert "private" not in json.dumps(report)
    assert "private" not in qualification.render_markdown(report)


@pytest.mark.parametrize("fault", [
    None, "failed-job", "missing-result", "invalid-result", "source", "existing", "preparation",
])
def test_reporting_command_publishes_only_its_own_safe_result(
    qualification, preview_receipt, tmp_path, monkeypatch, capsys, fault,
):
    output = tmp_path / "report"
    for variable in qualification.CHECKS.values():
        monkeypatch.setenv(variable, "Succeeded")
    monkeypatch.setenv("BUILD_SOURCEVERSION", "private-marker" if fault == "source" else SOURCE)
    monkeypatch.setenv("REPORT_PREPARATION_RESULT", "Failed" if fault == "preparation" else "Succeeded")
    if fault == "failed-job":
        monkeypatch.setenv("SELECTOR_RESULT", "Failed")
    elif fault == "missing-result":
        monkeypatch.delenv("SITE_FILE_RESULT")
    elif fault == "invalid-result":
        monkeypatch.setenv("SITE_FILE_RESULT", "private-marker")
    elif fault == "existing":
        output.mkdir()
        (output / "operator.txt").write_text("operator-owned")
    monkeypatch.setattr(sys, "argv", [
        "summarize-ado-qualification.py", "--preview", str(preview_receipt), "--output", str(output),
    ])
    assert qualification.main() == (0 if fault is None else 1)
    captured = capsys.readouterr()
    assert "private-marker" not in captured.out + captured.err
    if fault in {"source", "existing", "invalid-result"}:
        assert "ADO_QUALIFICATION_REPORT_READY" not in captured.out
        assert not (output / "qualification.json").exists()
        if fault == "existing":
            assert (output / "operator.txt").read_text() == "operator-owned"
    else:
        report = json.loads((output / "qualification.json").read_text())
        assert report["status"] == ("passed" if fault is None else "failed")
        assert "##vso[task.setvariable variable=ADO_QUALIFICATION_REPORT_READY]true" in captured.out
        assert "##vso[task.uploadsummary]" in captured.out
        assert "Not exercised by this run" in (output / "qualification.md").read_text()


def test_pipeline_collects_native_job_results_and_retains_failure_reporting():
    document = yaml.safe_load((ROOT / ".pipelines/validate-pipelines.yaml").read_text())
    stage = next(stage for stage in document["stages"] if stage.get("stage") == "qualification_report")
    assert set(stage["dependsOn"]) == {"preview", "validate_selector", "validate_site_file"}
    assert stage["condition"] == "always()"
    job = stage["jobs"][0]
    assert job["condition"] == "always()"
    assert job["variables"] == {
        "ADO_QUALIFICATION_REPORT_READY": "false",
        "PREVIEW_RESULT": "$[ stageDependencies.preview.preview.result ]",
        "SELECTOR_RESULT": "$[ stageDependencies.validate_selector.siteops_validate.result ]",
        "SITE_FILE_RESULT": "$[ stageDependencies.validate_site_file.siteops_validate.result ]",
    }
    steps = job["steps"]
    download = next(step for step in steps if step.get("task") == "DownloadPipelineArtifact@2")
    assert download["condition"] == "eq(variables['PREVIEW_RESULT'], 'Succeeded')"
    assert download["inputs"]["buildType"] == "current"
    assert download["inputs"]["artifactName"] == "pipeline-preview"
    summarize = next(step for step in steps if step.get("displayName") == "Summarize qualification")
    assert summarize["condition"] == "always()"
    assert "python3 -I -S -B scripts/summarize-ado-qualification.py" in summarize["script"]
    assert summarize["env"]["BUILD_SOURCEVERSION"] == "$(Build.SourceVersion)"
    assert summarize["env"]["REPORT_PREPARATION_RESULT"] == "$(Agent.JobStatus)"
    assert "SYSTEM_ACCESSTOKEN" not in summarize["env"]
    publish = next(step for step in steps if step.get("task") == "PublishPipelineArtifact@1")
    assert publish["condition"] == "and(always(), eq(variables['ADO_QUALIFICATION_REPORT_READY'], 'true'))"
    assert publish["inputs"]["artifact"] == "ado-qualification"


@pytest.mark.parametrize("result", ["Succeeded", "Failed"])
def test_native_reporter_needs_no_installed_engine_or_third_party_packages(
    preview_receipt, tmp_path, result,
):
    output = tmp_path / "native-report"
    environment = {
        **isolated_environment(tmp_path / "state"),
        "BUILD_SOURCEVERSION": SOURCE, "PREVIEW_RESULT": "Succeeded",
        "SELECTOR_RESULT": result, "SITE_FILE_RESULT": "Succeeded",
        "REPORT_PREPARATION_RESULT": "Succeeded",
    }
    process = subprocess.run([
        sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/summarize-ado-qualification.py"),
        "--preview", str(preview_receipt), "--output", str(output),
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert process.returncode == (0 if result == "Succeeded" else 1), process.stdout + process.stderr
    report = json.loads((output / "qualification.json").read_text())
    assert report["status"] == ("passed" if result == "Succeeded" else "failed")
    assert "ADO_QUALIFICATION_REPORT_READY" in process.stdout


@pytest.mark.parametrize("preparation", ["Failed", "Canceled", "Skipped", "SucceededWithIssues", ""])
def test_report_preparation_failure_cannot_reuse_an_old_preview_receipt(
    qualification, preview_receipt, preparation,
):
    report = qualification.summarize(
        SOURCE, dict.fromkeys(qualification.CHECKS, "Succeeded"), preview_receipt, preparation,
    )
    assert report["status"] == "failed"
    assert report["diagnostics"] == ["report-preparation-failed"]
    assert report["templatePreviews"] == []


def test_preview_receipt_accepts_the_exact_size_boundary(qualification, preview_receipt):
    raw = preview_receipt.read_bytes().ljust(qualification.MAX_RECEIPT, b" ")
    preview_receipt.write_bytes(raw)
    report = qualification.summarize(
        SOURCE, dict.fromkeys(qualification.CHECKS, "Succeeded"), preview_receipt, "Succeeded",
    )
    assert report["status"] == "passed"

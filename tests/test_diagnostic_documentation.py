"""Output guide examples use the engine's actual publishable contracts."""

import json
import re
from pathlib import Path

import pytest

from siteops import __version__
from siteops.diagnostics import ProviderFailure
from siteops.orchestrator import Orchestrator
from siteops.planning import OperationIdentity, OperationKind, PlanProjection, TargetKind
from siteops.reporting import render_plain_run, serialize_run_json
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    RunResult,
    SiteResult,
)
from tests.test_content_diagnostics import workspace

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def _examples():
    return [json.loads(block) for block in re.findall(
        r"```json\n(.*?)\n```", (DOCS / "run-output.md").read_text(encoding="utf-8"), re.DOTALL,
    )]


def _operation(site, step, status, failure=None):
    reason = None if status is OperationStatus.SUCCEEDED else OutcomeReason(
        OutcomeReasonCode.CONDITION_FALSE if status is OperationStatus.SKIPPED else OutcomeReasonCode.OPERATION_FAILED,
    )
    return OperationResult(OperationIdentity(site, step), OperationKind.DEPLOYMENT, status, 0,
                           reason=reason, failure=failure)


def _document(sites):
    return json.loads(serialize_run_json(RunResult.from_sites(tuple(sites), elapsed=0),
                                        PlanProjection.PUBLISHABLE, engine_version=__version__))


@pytest.mark.parametrize("engine_version", [__version__, "9.8.7"])
def test_documented_validation_and_successful_run_match_real_results(tmp_path, monkeypatch, engine_version):
    monkeypatch.setitem(globals(), "__version__", engine_version)
    root = workspace(tmp_path, "valid")
    validation = Orchestrator(root).validate(root / "manifests" / "smoke.yaml").document("publishable", __version__)
    assert validation["summary"]["targetResolution"] == "not-requested"
    sites = [
        SiteResult.from_operations(target=name, kind=TargetKind.RESOURCE_GROUP, elapsed=0, operations=(
            _operation(name, "first", OperationStatus.SUCCEEDED),
            _operation(name, "second", OperationStatus.SKIPPED if index else OperationStatus.SUCCEEDED),
        ))
        for index, name in enumerate(("one", "two"))
    ]
    examples = {item["kind"]: item for item in _examples() if "kind" in item}
    for example in examples.values():
        example["engine"]["version"] = __version__
    assert examples["ValidationResult"] == validation
    assert examples["DeploymentRun"] == _document(sites)


def test_documented_plain_failure_guidance_matches_rendered_output(monkeypatch):
    monkeypatch.setattr("siteops.terminal.line_width", lambda *args: 72)
    operation = _operation("example", "first", OperationStatus.FAILED)
    site = SiteResult.from_operations(target="example", kind=TargetKind.RESOURCE_GROUP, operations=(operation,), elapsed=0)
    output = render_plain_run(RunResult.from_sites((site,), elapsed=0), redacted=False)
    section = re.search(r"  Failure diagnostics\n  -+\n.*?(?=\n\n)", output, re.DOTALL)
    assert section is not None
    text = (DOCS / "run-output.md").read_text(encoding="utf-8")
    assert section.group() in text


def test_documented_failure_aggregate_matches_logical_operations_and_registry():
    operations = (
        _operation("example", "first", OperationStatus.FAILED,
                   ProviderFailure("subscription-not-visible", "SubscriptionNotFound")),
        _operation("example", "second", OperationStatus.UNKNOWN, ProviderFailure("timeout")),
    )
    site = SiteResult.from_operations(target="example", kind=TargetKind.RESOURCE_GROUP, operations=operations, elapsed=0)
    expected = _document((site,))["summary"]["operations"]
    examples = [item for item in _examples() if "failureDiagnostics" in item]
    assert examples == [expected]


@pytest.mark.parametrize("name", ["run-output.md", "plan-output.md", "ci-cd-setup.md", "migrating.md"])
def test_diagnostic_guide_links_resolve_to_permanent_entries(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\((diagnostics\.md(?:#[^\)]+)?)\)", text)
    assert links, f"{name} must link to the diagnostic index."
    headings = (DOCS / "diagnostics.md").read_text(encoding="utf-8")
    anchors = {
        re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
        for title in re.findall(r"^#{1,6} (.+)$", headings, re.MULTILINE)
    }
    for link in links:
        _, _, anchor = link.partition("#")
        if anchor:
            assert anchor in anchors
    assert "blob/siteops%2Fv" not in text

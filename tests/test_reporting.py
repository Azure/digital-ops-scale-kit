"""Final run serialization, plain rendering, and progress ownership tests."""

from __future__ import annotations

import io
import json
import threading

import pytest

from siteops.planning import (
    OperationIdentity,
    OperationKind,
    PlanProjection,
    TargetKind,
)
from siteops.reporting import (
    TextProgressReporter,
    render_plain_run,
    serialize_run_json,
)
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    ProgressEvent,
    ProgressEventKind,
    RunDiagnostic,
    RunDiagnosticSeverity,
    RunResult,
    SiteResult,
    SiteStatus,
)
from siteops.sanitize import REDACT_ENV, is_redaction_enabled

_PRIVATE_SENTINELS = (
    "private-run-id",
    "private-site",
    "private-step",
    "private-deployment",
    "private-output-key",
    "private-output-value",
    "private-template-hash",
    "private-provider-error",
    "private/path",
    "private-diagnostic-code",
    "private-diagnostic-summary",
    "private-diagnostic-detail",
    "private-diagnostic-serialized",
)


def _flat(rendered: str) -> str:
    """Collapse layout whitespace so a phrase assertion survives wrapping."""
    return " ".join(rendered.split())


_LONG_TARGET = "contoso-global-subscription-scope-site"


def _private_run() -> RunResult:
    succeeded = OperationResult(
        identity=OperationIdentity(
            target="private-site",
            step="private-step",
        ),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.SUCCEEDED,
        elapsed=1.0,
        _private_outputs={
            "private-output-key": {
                "value": "private-output-value",
                "templateHash": "private-template-hash",
            }
        },
        _deployment_name="private-deployment",
    )
    reason = OutcomeReason(
        OutcomeReasonCode.OPERATION_FAILED,
        private_detail=(
            "private-provider-error for private-site at private/path"
        ),
        serialized_detail="The provider rejected the operation.",
    )
    failed = OperationResult(
        identity=OperationIdentity(
            target="private-site",
            step="later-step",
        ),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.FAILED,
        elapsed=2.0,
        reason=reason,
    )
    site = SiteResult.from_operations(
        target="private-site",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(succeeded, failed),
        elapsed=3.0,
        reason=reason,
    )
    return RunResult.from_sites(
        (site,),
        elapsed=3.5,
        run_id="private-run-id",
        diagnostics=(
            RunDiagnostic(
                code="private-diagnostic-code",
                severity=RunDiagnosticSeverity.WARNING,
                summary="private-diagnostic-summary for private-site",
                private_detail="private-diagnostic-detail at private/path",
                serialized_detail="private-diagnostic-serialized",
            ),
        ),
    )


@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_ci_marker_selects_publishable_document_without_deleting_fixture_env(
    monkeypatch,
    marker,
):
    monkeypatch.setenv(REDACT_ENV, "")
    monkeypatch.setenv(marker, "1")
    assert is_redaction_enabled() is True

    document = serialize_run_json(
        _private_run(),
        (
            PlanProjection.PUBLISHABLE
            if is_redaction_enabled()
            else PlanProjection.LOCAL_PRIVATE
        ),
        engine_version="9.8.7",
    )

    for sentinel in _PRIVATE_SENTINELS:
        assert sentinel not in document


def test_publishable_json_is_an_allowlist_with_complete_counts():
    serialized = serialize_run_json(
        _private_run(),
        PlanProjection.PUBLISHABLE,
        engine_version="9.8.7",
    )
    document = json.loads(serialized)

    assert document == {
        "apiVersion": "siteops/v1alpha1",
        "diagnostics": [
            {
                "code": "run.diagnostic",
                "severity": "warning",
                "summary": "The deployment run reported a diagnostic.",
            }
        ],
        "engine": {"name": "siteops", "version": "9.8.7"},
        "exitCode": 1,
        "kind": "DeploymentRun",
        "projection": "publishable",
        "status": "failed",
        "summary": {
            "interrupted": False,
            "operations": {
                "counts": {
                    "cancelled": 0,
                    "failed": 1,
                    "not-run": 0,
                    "skipped": 0,
                    "succeeded": 1,
                    "unknown": 0,
                },
                "total": 2,
            },
            "sites": {
                "counts": {
                    "cancelled": 0,
                    "failed": 1,
                    "not-run": 0,
                    "skipped": 0,
                    "succeeded": 0,
                    "unknown": 0,
                },
                "total": 1,
            },
        },
    }
    for sentinel in _PRIVATE_SENTINELS:
        assert sentinel not in serialized
    assert serialized == serialize_run_json(
        _private_run(),
        PlanProjection.PUBLISHABLE,
        engine_version="9.8.7",
    )


def test_publishable_diagnostics_use_fixed_run_categories():
    known = RunResult.from_sites(
        (),
        elapsed=0.0,
        diagnostics=(
            RunDiagnostic(
                code="run-interrupted",
                severity=RunDiagnosticSeverity.WARNING,
                summary="private summary text",
                serialized_detail="private serialized text",
            ),
            RunDiagnostic(
                code="validation.failed",
                severity=RunDiagnosticSeverity.ERROR,
                summary="private summary text",
            ),
        ),
    )

    document = json.loads(
        serialize_run_json(
            known,
            PlanProjection.PUBLISHABLE,
            engine_version="9.8.7",
        )
    )

    assert document["diagnostics"] == [
        {
            "code": "run.interrupted",
            "severity": "warning",
            "summary": (
                "Execution was interrupted before every operation finished."
            ),
        },
        {
            "code": "run.validation-failed",
            "severity": "error",
            "summary": "Manifest validation failed.",
        },
    ]
    assert "private" not in json.dumps(document)


def test_local_private_diagnostics_keep_value_free_serialized_detail():
    serialized = serialize_run_json(
        _private_run(),
        PlanProjection.LOCAL_PRIVATE,
        engine_version="9.8.7",
    )

    document = json.loads(serialized)

    assert document["diagnostics"] == [
        {
            "code": "private-diagnostic-code",
            "severity": "warning",
            "summary": "private-diagnostic-summary for private-site",
            "serializedDetail": "private-diagnostic-serialized",
        }
    ]
    assert "private-diagnostic-detail" not in serialized


def test_local_private_json_includes_identity_but_not_outputs_or_raw_error():
    serialized = serialize_run_json(
        _private_run(),
        PlanProjection.LOCAL_PRIVATE,
        engine_version="9.8.7",
    )

    assert "private-run-id" in serialized
    assert "private-site" in serialized
    assert "private-step" in serialized
    assert "private-deployment" in serialized
    assert "private-output-key" not in serialized
    assert "private-output-value" not in serialized
    assert "private-provider-error" not in serialized
    assert "private/path" not in serialized
    assert "The provider rejected the operation." in serialized


def test_json_exit_code_matches_success_and_interruption_outcomes():
    skipped = RunResult.from_sites((), elapsed=0.0, run_id="skipped-run")
    unknown_reason = OutcomeReason(
        OutcomeReasonCode.COMPLETION_UNKNOWN
    )
    unknown_operation = OperationResult(
        identity=OperationIdentity(
            target="site-a",
            step="deploy",
        ),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.UNKNOWN,
        elapsed=1.0,
        reason=unknown_reason,
    )
    unknown_site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(unknown_operation,),
        elapsed=1.0,
        reason=unknown_reason,
    )
    interrupted = RunResult.from_sites(
        (unknown_site,),
        elapsed=1.0,
        interrupted=True,
        run_id="interrupted-run",
    )

    assert json.loads(
        serialize_run_json(
            skipped,
            PlanProjection.PUBLISHABLE,
            engine_version="9.8.7",
        )
    )["exitCode"] == 0
    assert json.loads(
        serialize_run_json(
            interrupted,
            PlanProjection.PUBLISHABLE,
            engine_version="9.8.7",
        )
    )["exitCode"] == 130


def _succeeded_run(*, interrupted: bool) -> RunResult:
    operation = OperationResult(
        identity=OperationIdentity(target="site-a", step="deploy"),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.SUCCEEDED,
        elapsed=1.0,
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=1.0,
    )
    return RunResult.from_sites(
        (site,),
        elapsed=1.0,
        interrupted=interrupted,
        run_id="run-a",
    )


def test_interruption_keeps_exit_130_when_observed_work_succeeded():
    document = json.loads(
        serialize_run_json(
            _succeeded_run(interrupted=True),
            PlanProjection.PUBLISHABLE,
            engine_version="9.8.7",
        )
    )

    assert document["status"] == "succeeded"
    assert document["exitCode"] == 130
    assert document["summary"]["interrupted"] is True
    assert json.loads(
        serialize_run_json(
            _succeeded_run(interrupted=False),
            PlanProjection.PUBLISHABLE,
            engine_version="9.8.7",
        )
    )["exitCode"] == 0


@pytest.mark.parametrize("redacted", [False, True])
def test_a_run_with_no_executed_work_says_so(redacted):
    skipped_operation = OperationResult(
        identity=OperationIdentity(target="site-a", step="deploy"),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.SKIPPED,
        elapsed=0.0,
        reason=OutcomeReason(OutcomeReasonCode.CONDITION_FALSE),
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(skipped_operation,),
        elapsed=0.0,
    )
    all_skipped = RunResult.from_sites((site,), elapsed=0.0, run_id="run-a")
    no_targets = RunResult.from_sites((), elapsed=0.0, run_id="run-b")

    assert "No operation ran." in render_plain_run(
        all_skipped,
        redacted=redacted,
    )
    assert "No target matched" in render_plain_run(
        no_targets,
        redacted=redacted,
    )
    assert "No operation ran." not in render_plain_run(
        _succeeded_run(interrupted=False),
        redacted=redacted,
    )


def test_local_plain_names_an_unconfirmed_deployment_without_claiming_cancellation():
    unknown_reason = OutcomeReason(
        OutcomeReasonCode.COMPLETION_UNKNOWN,
        private_detail="polling stopped before the provider answered",
    )
    unknown_operation = OperationResult(
        identity=OperationIdentity(target="site-a", step="deploy"),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.UNKNOWN,
        elapsed=4.0,
        reason=unknown_reason,
        _deployment_name="siteops-site-a-deploy-1",
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(unknown_operation,),
        elapsed=4.0,
    )
    result = RunResult.from_sites(
        (site,),
        elapsed=4.0,
        interrupted=True,
        run_id="run-a",
    )

    rendered = _flat(render_plain_run(result, redacted=False))

    assert "polling stopped before the provider answered" in rendered
    assert "unconfirmed deployment siteops-site-a-deploy-1" in rendered
    assert "was not cancelled" in rendered
    assert "cancelled the deployment" not in rendered
    assert "siteops-site-a-deploy-1" not in render_plain_run(
        result,
        redacted=True,
    )


def test_redacted_plain_renders_allowed_fields_without_private_diagnostics():
    result = _private_run()

    public = _flat(render_plain_run(result, redacted=True))

    assert "Result: failed" in public
    assert "1 failed" in public
    assert "The deployment run reported a diagnostic." in public
    assert "private-diagnostic-summary" not in public
    assert "private-diagnostic-detail" not in public
    assert "private-diagnostic-serialized" not in public


def test_plain_rendering_separates_local_and_publishable_details():
    result = _private_run()

    local = render_plain_run(result, redacted=False)
    public = render_plain_run(result, redacted=True)

    assert "private-site" in local
    assert "private-provider-error" in local
    assert "1 failed" in local
    assert "private-site" not in public
    assert "private-provider-error" not in public
    assert "1 failed" in public


def test_text_progress_reporter_serializes_complete_lines():
    stream = io.StringIO()
    reporter = TextProgressReporter(stream, redacted=False)
    events = [
        ProgressEvent(
            kind=ProgressEventKind.OPERATION_STARTED,
            target=f"site-{index}",
            operation=OperationIdentity(
                target=f"site-{index}",
                step=f"step-{index}",
            ),
            operation_kind=OperationKind.DEPLOYMENT,
        )
        for index in range(20)
    ]
    threads = [
        threading.Thread(target=reporter, args=(event,))
        for event in events
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = stream.getvalue().splitlines()
    assert len(lines) == len(events)
    assert {
        line.split("]")[0] + "]"
        for line in lines
    } == {f"[site-{index}]" for index in range(20)}


def _mixed_run(*, interrupted: bool = False) -> RunResult:
    """A run with one of every ending an operator has to read at once."""
    failed_reason = OutcomeReason(
        OutcomeReasonCode.OPERATION_FAILED,
        private_detail="BadRequest: the template was rejected",
    )
    unknown_reason = OutcomeReason(OutcomeReasonCode.COMPLETION_UNKNOWN)
    cancelled_reason = OutcomeReason(
        OutcomeReasonCode.CANCELLED_BEFORE_START
    )

    def operation(target, step, status, reason=None, name=None):
        return OperationResult(
            identity=OperationIdentity(target=target, step=step),
            kind=OperationKind.DEPLOYMENT,
            status=status,
            elapsed=1.0,
            reason=reason,
            _deployment_name=name,
        )

    healthy = SiteResult.from_operations(
        target="seattle-dev",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(
            operation("seattle-dev", "install", OperationStatus.SUCCEEDED),
        ),
        elapsed=1.0,
    )
    broken = SiteResult.from_operations(
        target="munich-prod",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(
            operation(
                "munich-prod",
                "install",
                OperationStatus.FAILED,
                failed_reason,
            ),
            operation(
                "munich-prod",
                "assets",
                OperationStatus.NOT_RUN,
                OutcomeReason(OutcomeReasonCode.EARLIER_OPERATION_FAILED),
            ),
        ),
        elapsed=2.0,
        reason=failed_reason,
    )
    unconfirmed = SiteResult.from_operations(
        target=_LONG_TARGET,
        kind=TargetKind.SUBSCRIPTION,
        operations=(
            operation(
                _LONG_TARGET,
                "resource-groups",
                OperationStatus.UNKNOWN,
                unknown_reason,
                "siteops-contoso-global-resource-groups-20260909",
            ),
        ),
        elapsed=61.2,
    )
    queued = SiteResult.from_operations(
        target="tokyo-lab",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(
            operation(
                "tokyo-lab",
                "install",
                OperationStatus.CANCELLED,
                cancelled_reason,
            ),
        ),
        elapsed=0.0,
    )
    return RunResult.from_sites(
        (healthy, broken, unconfirmed, queued),
        elapsed=64.2,
        interrupted=interrupted,
        run_id="run-a",
    )


@pytest.mark.parametrize("redacted", [False, True])
def test_plain_output_carries_no_terminal_control_sequences(redacted):
    """Plain output is as likely to be redirected as displayed."""
    rendered = render_plain_run(_mixed_run(interrupted=True), redacted=redacted)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    assert all(
        character == "\n" or character.isprintable()
        for character in rendered
    )
    assert rendered.isascii()


def test_a_long_target_name_keeps_its_identity_and_its_own_line():
    rendered = render_plain_run(_mixed_run(), redacted=False)

    assert _LONG_TARGET in rendered
    assert f"? {_LONG_TARGET}" in rendered
    assert "..." not in rendered
    assert "siteops-contoso-global-resource-groups-20260909" in rendered


def test_every_summary_line_stays_within_a_basic_terminal_width():
    rendered = render_plain_run(_mixed_run(interrupted=True), redacted=False)

    over_width = [
        line
        for line in rendered.splitlines()
        if len(line) > 80 or (len(line) > 74 and _LONG_TARGET not in line)
    ]

    assert not over_width, over_width


def test_unknown_work_takes_priority_over_failure_in_recovery_guidance():
    result = _mixed_run()

    local = _flat(render_plain_run(result, redacted=False))
    public = _flat(render_plain_run(result, redacted=True))

    assert "Result: failed" in local
    assert "Next: verify unconfirmed operations at their targets" in local
    assert "unconfirmed work before starting another deployment" in public
    assert "rerun" not in local + public
    assert "munich-prod" not in public


def test_an_unconfirmed_run_is_told_to_confirm_before_redeploying():
    unknown_reason = OutcomeReason(OutcomeReasonCode.COMPLETION_UNKNOWN)
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(
            OperationResult(
                identity=OperationIdentity(target="site-a", step="deploy"),
                kind=OperationKind.DEPLOYMENT,
                status=OperationStatus.UNKNOWN,
                elapsed=1.0,
                reason=unknown_reason,
            ),
        ),
        elapsed=1.0,
    )
    result = RunResult.from_sites((site,), elapsed=1.0, run_id="run-a")

    rendered = _flat(render_plain_run(result, redacted=False))

    assert "Next: verify unconfirmed operations at their targets" in rendered


def test_a_cancelled_operation_says_it_never_started():
    rendered = _flat(render_plain_run(_mixed_run(), redacted=False))

    assert "tokyo-lab" in rendered
    assert "The operation was cancelled before it started." in rendered


def test_a_finished_run_offers_no_next_action():
    operation = OperationResult(
        identity=OperationIdentity(target="site-a", step="deploy"),
        kind=OperationKind.DEPLOYMENT,
        status=OperationStatus.SUCCEEDED,
        elapsed=1.0,
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=1.0,
    )
    result = RunResult.from_sites((site,), elapsed=1.0, run_id="run-a")

    for redacted in (False, True):
        assert "Next:" not in render_plain_run(result, redacted=redacted)


def test_progress_lines_share_the_summary_markers_and_stay_plain():
    stream = io.StringIO()
    reporter = TextProgressReporter(stream, redacted=False)
    finished = [
        ProgressEvent(
            kind=ProgressEventKind.TARGET_FINISHED,
            target="site-a",
            site_status=SiteStatus.FAILED,
            elapsed=2.0,
        ),
        ProgressEvent(
            kind=ProgressEventKind.TARGET_FINISHED,
            target="site-b",
            site_status=SiteStatus.SUCCEEDED,
            elapsed=1.0,
        ),
    ]

    for event in finished:
        reporter(event)
    written = stream.getvalue()

    assert "[site-a] x failed in 2.0s" in written
    assert "[site-b] + succeeded in 1.0s" in written
    assert "\x1b" not in written
    assert written.isascii()

    redacted_stream = io.StringIO()
    redacted_reporter = TextProgressReporter(redacted_stream, redacted=True)
    redacted_reporter(finished[0])

    assert "site-a" not in redacted_stream.getvalue()
    assert "[<site>] x failed in 2.0s" in redacted_stream.getvalue()

"""Typed runtime outcome model tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from siteops.planning import (
    DiagnosticSeverity,
    OperationIdentity,
    OperationKind,
    PlanBuildResult,
    PlanDiagnostic,
    PlanIntent,
    PlanStatus,
    TargetKind,
)
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    RunResult,
    RunStatus,
    SiteResult,
    SiteStatus,
    preparation_failure_result,
)


def _operation(
    target: str,
    step: str,
    status: OperationStatus,
    *,
    reason: OutcomeReason | None = None,
    outputs: dict | None = None,
    deployment_name: str | None = None,
) -> OperationResult:
    return OperationResult(
        identity=OperationIdentity(target=target, step=step),
        kind=OperationKind.DEPLOYMENT,
        status=status,
        elapsed=0.25,
        reason=reason,
        _private_outputs=outputs or {},
        _deployment_name=deployment_name,
    )


def test_private_outputs_are_copied_frozen_and_hidden_from_repr():
    outputs = {"nested": {"value": ["private-output"]}}
    operation = _operation(
        "site-a",
        "deploy",
        OperationStatus.SUCCEEDED,
        outputs=outputs,
        deployment_name="private-deployment",
    )

    outputs["nested"]["value"].append("changed")

    assert "private-output" not in repr(operation)
    assert "private-deployment" not in repr(operation)
    assert operation.deployment_name == "private-deployment"
    copied = operation.copy_outputs()
    assert copied == {
        "nested": {"value": ["private-output"]}
    }
    copied["nested"]["value"].append("caller-change")
    assert operation.copy_outputs() == {
        "nested": {"value": ["private-output"]}
    }
    with pytest.raises(TypeError):
        operation._private_outputs["new"] = "value"
    with pytest.raises(FrozenInstanceError):
        operation.elapsed = 3.0


def test_all_skipped_run_is_successful_without_claiming_deployment():
    reason = OutcomeReason(OutcomeReasonCode.CONDITION_FALSE)
    operation = _operation(
        "site-a",
        "deploy",
        OperationStatus.SKIPPED,
        reason=reason,
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=0.25,
    )

    result = RunResult.from_sites((site,), elapsed=0.5, run_id="run-a")

    assert site.status is SiteStatus.SKIPPED
    assert result.status is RunStatus.SKIPPED
    assert result.exit_code == 0
    assert result.sites == (site,)


def test_failed_run_retains_success_outputs_and_not_run_records():
    failure = OutcomeReason(
        OutcomeReasonCode.OPERATION_FAILED,
        private_detail="provider rejected the request",
    )
    not_run = OutcomeReason(OutcomeReasonCode.EARLIER_OPERATION_FAILED)
    operations = (
        _operation(
            "site-a",
            "first",
            OperationStatus.SUCCEEDED,
            outputs={"value": {"type": "String", "value": "retained"}},
        ),
        _operation(
            "site-a",
            "second",
            OperationStatus.FAILED,
            reason=failure,
        ),
        _operation(
            "site-a",
            "third",
            OperationStatus.NOT_RUN,
            reason=not_run,
        ),
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=operations,
        elapsed=1.0,
        reason=failure,
    )

    result = RunResult.from_sites((site,), elapsed=1.0, run_id="run-b")
    assert result.status is RunStatus.FAILED
    assert result.exit_code == 1
    assert site.status is SiteStatus.FAILED
    assert site.failure_reason() is failure
    assert [operation.status for operation in site.operations] == [
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.NOT_RUN,
    ]
    assert (
        site.operations[0].copy_outputs()["value"]["value"]
        == "retained"
    )
    assert sum(
        operation.status is OperationStatus.NOT_RUN
        for operation in site.operations
    ) == 1


def test_all_not_run_sites_have_explicit_nonzero_run_status():
    reason = OutcomeReason(OutcomeReasonCode.DEPENDENCY_UNAVAILABLE)
    operation = _operation(
        "site-a",
        "deploy",
        OperationStatus.NOT_RUN,
        reason=reason,
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=0.0,
        reason=reason,
    )

    result = RunResult.from_sites((site,), elapsed=0.0, run_id="run-c")

    assert result.status is RunStatus.NOT_RUN
    assert result.exit_code == 1


def test_interrupted_run_exit_code_overrides_aggregate_status():
    reason = OutcomeReason(OutcomeReasonCode.OPERATION_FAILED)
    operation = _operation(
        "site-a",
        "deploy",
        OperationStatus.FAILED,
        reason=reason,
    )
    site = SiteResult.from_operations(
        target="site-a",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=0.25,
        reason=reason,
    )

    result = RunResult.from_sites(
        (site,),
        elapsed=0.5,
        interrupted=True,
        run_id="run-interrupted",
    )

    assert result.status is RunStatus.FAILED
    assert result.interrupted is True
    assert result.exit_code == 130


def test_preparation_failure_is_invalid_and_nonzero():
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        intent=PlanIntent.EXECUTABLE,
        diagnostics=(
            PlanDiagnostic(
                code="private-path-failed",
                severity=DiagnosticSeverity.ERROR,
                summary="Deployment preparation failed.",
                detail=f"Could not read {Path('private/site.yaml')}.",
                serialized_detail="A deployment input could not be read.",
            ),
        ),
    )

    failure = preparation_failure_result(result)

    assert failure.status is RunStatus.INVALID
    assert failure.exit_code == 1
    assert failure.sites == ()
    assert failure.diagnostics[0].private_detail.endswith(
        "private\\site.yaml."
    ) or failure.diagnostics[0].private_detail.endswith(
        "private/site.yaml."
    )

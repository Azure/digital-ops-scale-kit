# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Immutable runtime outcome models for Site Ops deployments."""

from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from siteops.planning import (
    DiagnosticSeverity,
    OperationIdentity,
    OperationKind,
    PlanBuildResult,
    PlanDisposition,
    SkipReasonCode,
    TargetKind,
)


class OperationStatus(str, Enum):
    """Runtime outcome of one prepared operation."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not-run"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class SiteStatus(str, Enum):
    """Aggregate runtime outcome of one prepared target."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not-run"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class RunStatus(str, Enum):
    """Aggregate runtime outcome of one deployment run."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not-run"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class OutcomeReasonCode(str, Enum):
    """Stable, value-free category explaining a non-success outcome."""

    CONDITION_FALSE = "condition-false"
    SCOPE_MISMATCH = "scope-mismatch"
    PLAN_BLOCKED = "plan-blocked"
    DEPENDENCY_UNAVAILABLE = "dependency-unavailable"
    OPERATION_FAILED = "operation-failed"
    EARLIER_OPERATION_FAILED = "earlier-operation-failed"
    EARLIER_OPERATION_UNKNOWN = "earlier-operation-unknown"
    CANCELLED_BEFORE_START = "cancelled-before-start"
    RUN_INTERRUPTED = "run-interrupted"
    COMPLETION_UNKNOWN = "completion-unknown"
    PREPARATION_INVALID = "preparation-invalid"


_REASON_SUMMARIES = {
    OutcomeReasonCode.CONDITION_FALSE: "The operation condition was not met.",
    OutcomeReasonCode.SCOPE_MISMATCH: "The operation does not apply to this target.",
    OutcomeReasonCode.PLAN_BLOCKED: "The prepared operation is blocked.",
    OutcomeReasonCode.DEPENDENCY_UNAVAILABLE: (
        "A required prior operation output is unavailable."
    ),
    OutcomeReasonCode.OPERATION_FAILED: "The operation failed.",
    OutcomeReasonCode.EARLIER_OPERATION_FAILED: (
        "The operation was not run after an earlier operation failed."
    ),
    OutcomeReasonCode.EARLIER_OPERATION_UNKNOWN: (
        "The operation was not run because an earlier operation's final "
        "state is unknown."
    ),
    OutcomeReasonCode.CANCELLED_BEFORE_START: (
        "The operation was cancelled before it started."
    ),
    OutcomeReasonCode.RUN_INTERRUPTED: (
        "The operation was not run because execution was interrupted."
    ),
    OutcomeReasonCode.COMPLETION_UNKNOWN: (
        "The operation may have completed, but its final state could not be "
        "confirmed."
    ),
    OutcomeReasonCode.PREPARATION_INVALID: (
        "The operation was not run because preparation was invalid."
    ),
}


class RunDiagnosticSeverity(str, Enum):
    """Severity of a safe final-run diagnostic."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ProgressEventKind(str, Enum):
    """Small set of progress notifications emitted by the runtime core."""

    PHASE_STARTED = "phase-started"
    BATCH_STARTED = "batch-started"
    TARGET_STARTED = "target-started"
    OPERATION_STARTED = "operation-started"
    OPERATION_FINISHED = "operation-finished"
    TARGET_FINISHED = "target-finished"
    TARGET_BLOCKED = "target-blocked"
    RUN_INTERRUPTED = "run-interrupted"


class ProgressPhase(str, Enum):
    """Execution phase named by a progress event."""

    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resource-group"
    TARGETS = "targets"


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty.")


def _require_elapsed(value: float, label: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number.")


def _freeze_private(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                copy.deepcopy(key): _freeze_private(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_private(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_private(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_private(item) for item in value)
    return copy.deepcopy(value)


def _thaw_private(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            copy.deepcopy(key): _thaw_private(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_private(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw_private(item) for item in value}
    return copy.deepcopy(value)


@dataclass(frozen=True)
class OutcomeReason:
    """Typed reason with private local detail and explicit safe detail."""

    code: OutcomeReasonCode
    private_detail: str | None = field(default=None, repr=False, compare=False)
    serialized_detail: str | None = None

    def __post_init__(self) -> None:
        if self.private_detail is not None:
            _require_text(self.private_detail, "Outcome private detail")
        if self.serialized_detail is not None:
            _require_text(self.serialized_detail, "Outcome serialized detail")

    @property
    def summary(self) -> str:
        """Return the fixed publication-safe summary for this category."""
        return _REASON_SUMMARIES[self.code]

    def local_message(self) -> str:
        """Return local detail when available, otherwise the safe summary."""
        return self.private_detail or self.summary


@dataclass(frozen=True)
class RunDiagnostic:
    """Final diagnostic whose raw detail is never structurally serialized."""

    code: str
    severity: RunDiagnosticSeverity
    summary: str
    private_detail: str | None = field(default=None, repr=False, compare=False)
    serialized_detail: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.code, "Run diagnostic code")
        _require_text(self.summary, "Run diagnostic summary")
        if self.private_detail is not None:
            _require_text(self.private_detail, "Run diagnostic private detail")
        if self.serialized_detail is not None:
            _require_text(
                self.serialized_detail,
                "Run diagnostic serialized detail",
            )


@dataclass(frozen=True)
class ProgressEvent:
    """Bounded structured progress notification."""

    kind: ProgressEventKind
    target: str | None = None
    operation: OperationIdentity | None = None
    operation_kind: OperationKind | None = None
    operation_status: OperationStatus | None = None
    site_status: SiteStatus | None = None
    reason: OutcomeReason | None = None
    phase: ProgressPhase | None = None
    target_count: int | None = None
    worker_count: int | None = None
    elapsed: float | None = None

    def __post_init__(self) -> None:
        if self.target is not None:
            _require_text(self.target, "Progress target")
        if self.target_count is not None and self.target_count < 0:
            raise ValueError("Progress target count must be non-negative.")
        if self.worker_count is not None and self.worker_count <= 0:
            raise ValueError("Progress worker count must be positive.")
        if self.elapsed is not None:
            _require_elapsed(self.elapsed, "Progress elapsed time")


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class OperationResult:
    """Completed record for one canonical prepared operation."""

    identity: OperationIdentity
    kind: OperationKind
    status: OperationStatus
    elapsed: float
    reason: OutcomeReason | None = None
    _private_outputs: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _deployment_name: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.status, OperationStatus):
            raise TypeError("Operation status must be an OperationStatus.")
        _require_elapsed(self.elapsed, "Operation elapsed time")
        if self.status is not OperationStatus.SUCCEEDED and self._private_outputs:
            raise ValueError("Only observed successful operations can supply outputs.")
        object.__setattr__(
            self,
            "_private_outputs",
            _freeze_private(self._private_outputs),
        )
        if self._deployment_name is not None:
            _require_text(self._deployment_name, "Deployment name")
        if self.status is OperationStatus.SUCCEEDED:
            if self.reason is not None:
                raise ValueError("Succeeded operations cannot carry a reason.")
        elif self.reason is None:
            raise ValueError("Non-success operations require a typed reason.")

    @property
    def deployment_name(self) -> str | None:
        """Return private deployment metadata for authorized local reporting."""
        return self._deployment_name

    def copy_outputs(self) -> dict[str, Any]:
        """Copy outputs for trusted in-process consumers, never publication."""
        outputs = _thaw_private(self._private_outputs)
        if not isinstance(outputs, dict):
            raise TypeError("Operation outputs must be a mapping.")
        return outputs

def _aggregate_site_status(
    operations: tuple[OperationResult, ...],
    reason: OutcomeReason | None = None,
) -> SiteStatus:
    if (
        reason is not None
        and reason.code is OutcomeReasonCode.OPERATION_FAILED
    ):
        return SiteStatus.FAILED
    statuses = {operation.status for operation in operations}
    if OperationStatus.FAILED in statuses:
        return SiteStatus.FAILED
    if OperationStatus.UNKNOWN in statuses:
        return SiteStatus.UNKNOWN
    if OperationStatus.CANCELLED in statuses:
        return SiteStatus.CANCELLED
    if OperationStatus.NOT_RUN in statuses:
        return SiteStatus.NOT_RUN
    if not operations or statuses == {OperationStatus.SKIPPED}:
        return SiteStatus.SKIPPED
    return SiteStatus.SUCCEEDED


@dataclass(frozen=True)
class SiteResult:
    """Completed record for one canonical prepared target."""

    target: str
    kind: TargetKind
    status: SiteStatus
    operations: tuple[OperationResult, ...]
    elapsed: float
    reason: OutcomeReason | None = None

    def __post_init__(self) -> None:
        _require_text(self.target, "Site result target")
        _require_elapsed(self.elapsed, "Site elapsed time")
        object.__setattr__(self, "operations", tuple(self.operations))
        identities: set[OperationIdentity] = set()
        for operation in self.operations:
            if operation.identity.target != self.target:
                raise ValueError(
                    "Operation result target must match its site result."
                )
            if operation.identity in identities:
                raise ValueError(
                    f"Duplicate operation result identity: {operation.identity}."
                )
            identities.add(operation.identity)
        expected = _aggregate_site_status(self.operations, self.reason)
        if self.status is not expected:
            raise ValueError(
                f"Site status {self.status.value!r} does not match "
                f"operation aggregate {expected.value!r}."
            )

    @classmethod
    def from_operations(
        cls,
        *,
        target: str,
        kind: TargetKind,
        operations: tuple[OperationResult, ...],
        elapsed: float,
        reason: OutcomeReason | None = None,
    ) -> SiteResult:
        """Build a site result with a derived aggregate status."""
        owned = tuple(operations)
        return cls(
            target=target,
            kind=kind,
            status=_aggregate_site_status(owned, reason),
            operations=owned,
            elapsed=elapsed,
            reason=reason,
        )

    def failure_reason(self) -> OutcomeReason | None:
        """Return the reason for the first incomplete operation or target."""
        if self.reason is not None:
            return self.reason
        for operation in self.operations:
            if (
                operation.status
                not in {OperationStatus.SUCCEEDED, OperationStatus.SKIPPED}
                and operation.reason is not None
            ):
                return operation.reason
        return None

def _aggregate_run_status(
    sites: tuple[SiteResult, ...],
    *,
    interrupted: bool,
) -> RunStatus:
    statuses = {site.status for site in sites}
    if SiteStatus.FAILED in statuses:
        return RunStatus.FAILED
    if SiteStatus.UNKNOWN in statuses:
        return RunStatus.UNKNOWN
    if SiteStatus.CANCELLED in statuses:
        return RunStatus.CANCELLED
    if SiteStatus.NOT_RUN in statuses:
        return RunStatus.CANCELLED if interrupted else RunStatus.NOT_RUN
    if not sites or statuses == {SiteStatus.SKIPPED}:
        return RunStatus.SKIPPED
    return RunStatus.SUCCEEDED


@dataclass(frozen=True)
class RunResult:
    """Typed private return for one prepared deployment run."""

    run_id: str
    sites: tuple[SiteResult, ...]
    status: RunStatus
    elapsed: float
    interrupted: bool = False
    diagnostics: tuple[RunDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if type(self.interrupted) is not bool:
            raise TypeError("Run interrupted must be a boolean.")
        _require_text(self.run_id, "Run ID")
        _require_elapsed(self.elapsed, "Run elapsed time")
        object.__setattr__(self, "sites", tuple(self.sites))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        targets = [site.target for site in self.sites]
        if len(targets) != len(set(targets)):
            raise ValueError("Run result target names must be unique.")
        if self.status is not RunStatus.INVALID:
            expected = _aggregate_run_status(
                self.sites,
                interrupted=self.interrupted,
            )
            if self.status is not expected:
                raise ValueError(
                    f"Run status {self.status.value!r} does not match "
                    f"site aggregate {expected.value!r}."
                )
        elif any(
            operation.status not in {OperationStatus.NOT_RUN, OperationStatus.SKIPPED}
            for site in self.sites
            for operation in site.operations
        ):
            raise ValueError("Invalid preparation cannot contain executed operations.")

    @classmethod
    def from_sites(
        cls,
        sites: tuple[SiteResult, ...],
        *,
        elapsed: float,
        interrupted: bool = False,
        diagnostics: tuple[RunDiagnostic, ...] = (),
        run_id: str | None = None,
    ) -> RunResult:
        """Build a run result with generated identity and aggregate status."""
        owned = tuple(sites)
        return cls(
            run_id=run_id or str(uuid.uuid4()),
            sites=owned,
            status=_aggregate_run_status(
                owned,
                interrupted=interrupted,
            ),
            elapsed=elapsed,
            interrupted=interrupted,
            diagnostics=tuple(diagnostics),
        )

    @property
    def exit_code(self) -> int:
        """Return the process exit code associated with this outcome."""
        if self.interrupted:
            return 130
        if self.status in {RunStatus.SUCCEEDED, RunStatus.SKIPPED}:
            return 0
        return 1

def _reason_for_prepared_operation() -> OutcomeReason:
    return OutcomeReason(OutcomeReasonCode.PREPARATION_INVALID)


def preparation_failure_result(result: PlanBuildResult) -> RunResult:
    """Create a non-executed typed result for expected preparation failure."""
    if result.executable:
        raise ValueError("Executable preparation is not a preparation failure.")
    sites: tuple[SiteResult, ...] = ()
    if result.plan is not None:
        sites = tuple(
            SiteResult.from_operations(
                target=target.name,
                kind=target.kind,
                operations=tuple(
                    OperationResult(
                        identity=operation.identity,
                        kind=operation.kind,
                        status=OperationStatus.NOT_RUN,
                        elapsed=0.0,
                        reason=_reason_for_prepared_operation(),
                    )
                    for operation in target.operations
                ),
                elapsed=0.0,
                reason=_reason_for_prepared_operation(),
            )
            for target in result.plan.targets
        )
    severity_by_plan = {
        DiagnosticSeverity.INFO: RunDiagnosticSeverity.INFO,
        DiagnosticSeverity.WARNING: RunDiagnosticSeverity.WARNING,
        DiagnosticSeverity.ERROR: RunDiagnosticSeverity.ERROR,
    }
    diagnostics = tuple(
        RunDiagnostic(
            code=diagnostic.code,
            severity=severity_by_plan[diagnostic.severity],
            summary=diagnostic.summary,
            private_detail=diagnostic.detail,
            serialized_detail=diagnostic.serialized_detail,
        )
        for diagnostic in result.diagnostics
    )
    return RunResult(
        run_id=str(uuid.uuid4()),
        sites=sites,
        status=RunStatus.INVALID,
        elapsed=0.0,
        diagnostics=diagnostics,
    )


def reason_from_plan_skip(
    code: SkipReasonCode,
    detail: str,
) -> OutcomeReason:
    """Adapt a planning skip reason without reusing its disposition enum."""
    runtime_code = {
        SkipReasonCode.CONDITION_FALSE: OutcomeReasonCode.CONDITION_FALSE,
        SkipReasonCode.SCOPE_MISMATCH: OutcomeReasonCode.SCOPE_MISMATCH,
        SkipReasonCode.DEPENDENCY_BLOCKED: (
            OutcomeReasonCode.DEPENDENCY_UNAVAILABLE
        ),
        SkipReasonCode.TARGET_PREPARATION_FAILED: (
            OutcomeReasonCode.PLAN_BLOCKED
        ),
        SkipReasonCode.COMPILATION_FAILED: OutcomeReasonCode.PLAN_BLOCKED,
        SkipReasonCode.CAPABILITY_UNAVAILABLE: OutcomeReasonCode.PLAN_BLOCKED,
    }[code]
    return OutcomeReason(runtime_code, private_detail=detail)


def operation_result_for_plan_disposition(
    *,
    identity: OperationIdentity,
    kind: OperationKind,
    disposition: PlanDisposition,
    skip_code: SkipReasonCode | None,
    skip_detail: str | None,
) -> OperationResult:
    """Create the runtime record for a statically non-executable operation."""
    if disposition is PlanDisposition.EXECUTE:
        raise ValueError("Executable operations need an observed runtime outcome.")
    if skip_code is None or skip_detail is None:
        raise ValueError("A non-executable operation requires a typed plan reason.")
    reason = reason_from_plan_skip(skip_code, skip_detail)
    return OperationResult(
        identity=identity,
        kind=kind,
        status=(
            OperationStatus.SKIPPED
            if disposition is PlanDisposition.SKIP
            else OperationStatus.NOT_RUN
        ),
        elapsed=0.0,
        reason=reason,
    )

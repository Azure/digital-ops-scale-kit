# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deterministic final rendering and serialized text progress."""

from __future__ import annotations

import json
import threading
from collections import Counter
from typing import Any, TextIO

from siteops import terminal
from siteops.planning import (
    BICEP_COMPILER_SUMMARY,
    PUBLISHABLE_CAPABILITY_SUMMARIES,
    PlanProjection,
)
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    ProgressEvent,
    ProgressEventKind,
    ProgressPhase,
    RunDiagnostic,
    RunResult,
    RunStatus,
    SiteResult,
    SiteStatus,
)
from siteops.terminal import heading as _heading
from siteops.terminal import sanitize
from siteops.terminal import wrap as _wrap

_API_VERSION = "siteops/v1alpha1"
_KIND = "DeploymentRun"

# Publication categories for the diagnostic codes a run can carry. A run
# diagnostic's own code, summary, and serialized detail are producer text, so
# publishing them directly would export whatever a producer happened to write.
# Preparation diagnostics enter a run result with their planning codes, so both
# families are named here. An unlisted code publishes the generic entry below.
_PUBLISHABLE_RUN_DIAGNOSTICS = {
    "run-interrupted": (
        "run.interrupted",
        "Execution was interrupted before every operation finished.",
    ),
    "progress-reporting-failed": (
        "run.progress-reporting-failed",
        "Progress reporting stopped before execution completed.",
    ),
    **{
        code: ("run.capability-unavailable", summary)
        for code, summary in PUBLISHABLE_CAPABILITY_SUMMARIES.items()
    },
    "composition.invalid": (
        "run.preparation-invalid",
        "Preparation failed before execution started.",
    ),
    "compilation.failed": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.input-changed": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.module-unavailable": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.output-invalid": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.source-unreadable": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.timeout": (
        "run.compilation-failed",
        "Template compilation failed.",
    ),
    "compilation.tool-missing": (
        "run.capability-unavailable",
        BICEP_COMPILER_SUMMARY,
    ),
    "compilation.tool-unavailable": (
        "run.capability-unavailable",
        BICEP_COMPILER_SUMMARY,
    ),
    "operation-preparation.invalid": (
        "run.preparation-invalid",
        "Preparation failed before execution started.",
    ),
    "operation.dependency-blocked": (
        "run.dependency-unavailable",
        "A required prior operation is unavailable.",
    ),
    "parameter-selection.invalid": (
        "run.preparation-invalid",
        "Preparation failed before execution started.",
    ),
    "plan.target-set-incomplete": (
        "run.targeting-incomplete",
        "The selected target set is incomplete.",
    ),
    "plan.targeting.empty": (
        "run.targeting-empty",
        "No Sites matched the selected criteria.",
    ),
    "plan.targeting.required": (
        "run.targeting-required",
        "The manifest needs a target set or a selector.",
    ),
    "subscription-target.missing": (
        "run.preparation-invalid",
        "Preparation failed before execution started.",
    ),
    "validation.failed": (
        "run.validation-failed",
        "Manifest validation failed.",
    ),
}
_GENERIC_RUN_DIAGNOSTIC = (
    "run.diagnostic",
    "The deployment run reported a diagnostic.",
)


def _reason_document(reason: OutcomeReason) -> dict[str, Any]:
    document: dict[str, Any] = {
        "code": reason.code.value,
        "summary": reason.summary,
    }
    if reason.serialized_detail is not None:
        document["serializedDetail"] = reason.serialized_detail
    return document


def _publishable_diagnostic_document(
    diagnostic: RunDiagnostic,
) -> dict[str, str]:
    if diagnostic.public_summary is not None and (
        diagnostic.code in {"inputs.invalid", "site.invalid", "plan.targeting.conflict"}
        or diagnostic.code.startswith("inputs.resource.")
    ):
        return {
            "code": diagnostic.code,
            "severity": diagnostic.severity.value,
            "summary": diagnostic.public_summary,
        }
    code, summary = _PUBLISHABLE_RUN_DIAGNOSTICS.get(
        diagnostic.code,
        _GENERIC_RUN_DIAGNOSTIC,
    )
    return {
        "code": code,
        "severity": diagnostic.severity.value,
        "summary": summary,
    }


def _local_diagnostic_document(diagnostic: RunDiagnostic) -> dict[str, Any]:
    document: dict[str, Any] = {
        "code": diagnostic.code,
        "severity": diagnostic.severity.value,
        "summary": diagnostic.summary,
    }
    if diagnostic.serialized_detail is not None:
        document["serializedDetail"] = diagnostic.serialized_detail
    return document


def _enum_counts(
    values: list[OperationStatus] | list[SiteStatus],
    members: type[OperationStatus] | type[SiteStatus],
) -> dict[str, int]:
    counts = Counter(values)
    return {member.value: counts[member] for member in members}


def _summary_document(result: RunResult) -> dict[str, Any]:
    operations = [
        operation
        for site in result.sites
        for operation in site.operations
    ]
    return {
        "interrupted": result.interrupted,
        "operations": {
            "counts": _enum_counts(
                [operation.status for operation in operations],
                OperationStatus,
            ),
            "total": len(operations),
        },
        "sites": {
            "counts": _enum_counts(
                [site.status for site in result.sites],
                SiteStatus,
            ),
            "total": len(result.sites),
        },
    }


def _base_document(
    result: RunResult,
    projection: PlanProjection,
    engine_version: str,
) -> dict[str, Any]:
    return {
        "apiVersion": _API_VERSION,
        "engine": {
            "name": "siteops",
            "version": engine_version,
        },
        "exitCode": result.exit_code,
        "kind": _KIND,
        "projection": projection.value,
        "status": result.status.value,
        "summary": _summary_document(result),
    }


def _operation_document(operation: OperationResult) -> dict[str, Any]:
    document: dict[str, Any] = {
        "elapsedSeconds": operation.elapsed,
        "identity": {
            "step": operation.identity.step,
            "target": operation.identity.target,
        },
        "kind": operation.kind.value,
        "status": operation.status.value,
    }
    if operation.reason is not None:
        document["reason"] = _reason_document(operation.reason)
    if operation.deployment_name is not None:
        document["deploymentName"] = operation.deployment_name
    return document


def _site_document(site: SiteResult) -> dict[str, Any]:
    document: dict[str, Any] = {
        "elapsedSeconds": site.elapsed,
        "kind": site.kind.value,
        "operations": [
            _operation_document(operation)
            for operation in site.operations
        ],
        "status": site.status.value,
        "target": site.target,
    }
    if site.reason is not None:
        document["reason"] = _reason_document(site.reason)
    return document


def _local_private_document(
    result: RunResult,
    engine_version: str,
) -> dict[str, Any]:
    document = _base_document(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version,
    )
    document.update(
        {
            "diagnostics": [
                _local_diagnostic_document(diagnostic)
                for diagnostic in result.diagnostics
            ],
            "elapsedSeconds": result.elapsed,
            "runId": result.run_id,
            "sites": [_site_document(site) for site in result.sites],
        }
    )
    return document


def _publishable_document(
    result: RunResult,
    engine_version: str,
) -> dict[str, Any]:
    document = _base_document(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version,
    )
    document["diagnostics"] = [
        _publishable_diagnostic_document(diagnostic)
        for diagnostic in result.diagnostics
    ]
    return document


def serialize_run_json(
    result: RunResult,
    projection: PlanProjection,
    *,
    engine_version: str,
) -> str:
    """Serialize one explicit final-result projection."""
    if projection is PlanProjection.PUBLISHABLE:
        document = _publishable_document(result, engine_version)
    elif projection is PlanProjection.LOCAL_PRIVATE:
        document = _local_private_document(result, engine_version)
    else:  # pragma: no cover - PlanProjection is closed
        raise ValueError(f"Unsupported run projection: {projection!r}")
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


_NAME_COLUMN_LIMIT = 24
_STATUS_COLUMN = 9

# Status markers come from the shared ASCII set. No color or cursor control
# is emitted, because this text is as likely to be redirected into a log or
# an artifact as it is to reach a terminal.
_SITE_MARKERS = {
    SiteStatus.SUCCEEDED: terminal.SUCCEEDED,
    SiteStatus.FAILED: terminal.FAILED,
    SiteStatus.SKIPPED: terminal.NOT_RUN,
    SiteStatus.NOT_RUN: terminal.NOT_RUN,
    SiteStatus.CANCELLED: terminal.NOT_RUN,
    SiteStatus.UNKNOWN: terminal.UNKNOWN,
}
_OPERATION_MARKERS = {
    OperationStatus.SUCCEEDED: terminal.SUCCEEDED,
    OperationStatus.FAILED: terminal.FAILED,
    OperationStatus.SKIPPED: terminal.NOT_RUN,
    OperationStatus.NOT_RUN: terminal.NOT_RUN,
    OperationStatus.CANCELLED: terminal.NOT_RUN,
    OperationStatus.UNKNOWN: terminal.UNKNOWN,
}


def _status_word(value: str) -> str:
    """Render a status value as prose without inventing new vocabulary."""
    return value.replace("-", " ")


def _plural(count: int | None, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _packed_lines(
    head: str,
    items: list[str],
    *,
    indent: str = "  ",
    hanging: str = "    ",
) -> list[str]:
    """Pack comma separated items into lines without splitting one item.

    `textwrap` breaks on any space, which would strand a count away from the
    status it counts. Packing whole items keeps `1 not run` readable.
    """
    width = terminal.line_width()
    lines: list[str] = []
    current = f"{indent}{head}"
    for item in items:
        piece = f", {item}"
        if len(current) + len(piece) <= width:
            current = f"{current}{piece}"
            continue
        lines.append(f"{current},")
        current = f"{hanging}{item}"
    lines.append(current)
    return lines


def _counts_line(label: str, section: dict[str, Any]) -> list[str]:
    return _packed_lines(
        f"{label}: {section['total']} total",
        [
            f"{count} {_status_word(name)}"
            for name, count in section["counts"].items()
            if count
        ],
    )


def _totals_lines(result: RunResult, *, elapsed: bool) -> list[str]:
    """Render the aggregate counts from the same summary the JSON publishes."""
    summary = _summary_document(result)
    outcome = (
        "all deployment operations succeeded"
        if _succeeded(result)
        else _status_word(result.status.value)
    )
    headline = f"  Result: {outcome}"
    if elapsed:
        headline = f"{headline} in {result.elapsed:.1f}s"
    return [
        headline,
        *_counts_line("Sites", summary["sites"]),
        *_counts_line("Operations", summary["operations"]),
    ]


def _succeeded(result: RunResult) -> bool:
    return result.status is RunStatus.SUCCEEDED and not result.interrupted


def _no_work_line(result: RunResult) -> str | None:
    """Say plainly that nothing ran, so an exit code 0 is not read as work."""
    if result.status is not RunStatus.SKIPPED:
        return None
    if not result.sites:
        return "  No target matched, so no operation ran."
    return "  No operation ran. Every prepared operation was skipped."


def _has_unconfirmed(result: RunResult) -> bool:
    return any(
        operation.status is OperationStatus.UNKNOWN
        for site in result.sites
        for operation in site.operations
    )


def _next_action(result: RunResult, *, redacted: bool) -> str | None:
    """Name the one thing worth doing next, or say nothing.

    A run that finished its work needs no instruction. Every other ending has
    a different next step, and guessing wrong wastes an operator's time, so
    the uncertain case never says to redeploy first.
    """
    if result.status is RunStatus.INVALID:
        return (
            "Next: run the same manifest through `siteops plan` for the full "
            "preparation detail."
        )
    if _has_unconfirmed(result) or result.status is RunStatus.UNKNOWN:
        if redacted:
            return (
                "Next: inspect the affected targets for unconfirmed work "
                "before starting another deployment."
            )
        return (
            "Next: verify unconfirmed operations at their targets before "
            "deciding whether to deploy again."
        )
    if result.status is RunStatus.FAILED:
        if redacted:
            return (
                "Next: inspect the affected targets and private diagnostics, "
                "then review a new plan before deploying again."
            )
        return (
            "Next: inspect affected resources, correct the error, and review "
            "a new plan before deploying again."
        )
    if result.interrupted or result.status in {
        RunStatus.CANCELLED,
        RunStatus.NOT_RUN,
    }:
        return (
            "Next: review completed and unstarted operations before planning "
            "another deployment. A new run does not resume this one."
        )
    return None


def _interrupted_lines(result: RunResult, *, redacted: bool) -> list[str]:
    if not result.interrupted:
        return []
    return [
        "",
        *_wrap(
            "Execution was interrupted. Work already accepted by Azure was "
            "not cancelled."
            if not redacted
            else "Execution was interrupted."
        ),
    ]


def _render_publishable_plain(result: RunResult) -> str:
    document = _publishable_document(result, engine_version="")
    lines = _heading("Deployment summary")
    lines.extend(_totals_lines(result, elapsed=False))
    no_work = _no_work_line(result)
    if no_work is not None:
        lines.append(no_work)
    lines.extend(_interrupted_lines(result, redacted=True))
    if document["diagnostics"]:
        lines.extend(_heading("Diagnostics", blank_after=False))
        for diagnostic in document["diagnostics"]:
            lines.extend(
                _wrap(
                    sanitize(f"{diagnostic['severity'].capitalize()}: {diagnostic['summary']}"),
                    indent="    ",
                    hanging="      ",
                )
            )
    next_action = _next_action(result, redacted=True)
    if next_action is not None:
        lines.extend(["", *_wrap(next_action)])
    lines.append("")
    return "\n".join(sanitize(line) for line in lines) + "\n"


def _operation_progress(site: SiteResult) -> str:
    counts = Counter(operation.status for operation in site.operations)
    extras = ", ".join(
        f"{counts[status]} {_status_word(status.value)}"
        for status in (
            OperationStatus.FAILED,
            OperationStatus.SKIPPED,
            OperationStatus.NOT_RUN,
            OperationStatus.CANCELLED,
            OperationStatus.UNKNOWN,
        )
        if counts[status]
    )
    suffix = f" ({extras})" if extras else ""
    completed = counts[OperationStatus.SUCCEEDED]
    return f"{completed}/{len(site.operations)} ops{suffix}"


def _name_column_width(sites: tuple[SiteResult, ...]) -> int:
    """Size the name column to the sites that fit, never by truncating one."""
    lengths = [
        len(sanitize(site.target))
        for site in sites
        if len(sanitize(site.target)) <= _NAME_COLUMN_LIMIT
    ]
    return max(lengths, default=0)


def _site_row(site: SiteResult, width: int) -> list[str]:
    detail = (
        f"{_status_word(site.status.value):<{_STATUS_COLUMN}}  "
        f"{_operation_progress(site)}  {site.elapsed:.1f}s"
    )
    marker = _SITE_MARKERS[site.status]
    target = sanitize(site.target)
    if len(target) > width:
        return [f"  {marker} {target}", f"      {detail}"]
    return [f"  {marker} {target:<{width}}  {detail}"]


def _incomplete_lines(result: RunResult) -> list[str]:
    incomplete = [
        site
        for site in result.sites
        if site.status not in {SiteStatus.SUCCEEDED, SiteStatus.SKIPPED}
    ]
    if not incomplete:
        return []
    lines = _heading("Incomplete", blank_after=False)
    for site in incomplete:
        reason = site.failure_reason()
        detail = (
            reason.local_message()
            if reason is not None
            else _status_word(site.status.value)
        )
        lines.append(f"  {_SITE_MARKERS[site.status]} {sanitize(site.target)}")
        lines.extend(_wrap(sanitize(detail), indent="      "))
        for operation in site.operations:
            if (
                operation.status is OperationStatus.UNKNOWN
                and operation.deployment_name is not None
            ):
                lines.extend(
                    _wrap(
                        sanitize(
                            f"{operation.identity.step}: unconfirmed deployment "
                            f"{operation.deployment_name}"
                        ),
                        indent="      ",
                    )
                )
    return lines


def render_plain_run(result: RunResult, *, redacted: bool) -> str:
    """Render a deterministic final deployment summary.

    Rows size themselves to the sites in hand instead of a fixed wide table,
    and a name longer than the column keeps its own line rather than being
    cut, because a truncated target name is no longer an identity an operator
    can act on. Target, step and reason text is shown with control
    characters escaped.
    """
    if redacted:
        return _render_publishable_plain(result)

    lines = _heading("Deployment summary")
    width = _name_column_width(result.sites)
    for site in result.sites:
        lines.extend(_site_row(site, width))
    if result.sites:
        lines.append("")
    lines.extend(_totals_lines(result, elapsed=True))
    no_work = _no_work_line(result)
    if no_work is not None:
        lines.append(no_work)
    lines.extend(_interrupted_lines(result, redacted=False))
    lines.extend(_incomplete_lines(result))
    if result.diagnostics:
        lines.extend(_heading("Diagnostics", blank_after=False))
        for diagnostic in result.diagnostics:
            lines.extend(
                _wrap(
                    sanitize(
                        f"{diagnostic.severity.value.capitalize()}: "
                        f"{diagnostic.private_detail or diagnostic.summary}"
                    ),
                    indent="    ",
                    hanging="      ",
                )
            )
    next_action = _next_action(result, redacted=False)
    if next_action is not None:
        lines.extend(["", *_wrap(next_action)])
    lines.append("")
    return "\n".join(sanitize(line) for line in lines) + "\n"


class TextProgressReporter:
    """Serialize human progress writes to one text stream."""

    def __init__(self, stream: TextIO, *, redacted: bool):
        self._stream = stream
        self._redacted = redacted
        self._lock = threading.Lock()
        self._last_phase: ProgressPhase | None = None

    def _target(self, event: ProgressEvent) -> str:
        if self._redacted:
            return "<site>"
        return sanitize(event.target or (
            event.operation.target
            if event.operation is not None
            else "<site>"
        ))

    def _step(self, event: ProgressEvent) -> str:
        if self._redacted:
            return "<step>"
        return sanitize(
            event.operation.step
            if event.operation is not None
            else "<step>"
        )

    def message(self, text: str) -> None:
        """Write one fixed status line, serialized with semantic progress."""
        with self._lock:
            self._stream.write(sanitize(text) + "\n")
            self._stream.flush()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            if event.kind is ProgressEventKind.PHASE_STARTED and event.phase in {
                ProgressPhase.RESOLUTION, ProgressPhase.DOWNLOAD, ProgressPhase.VERIFICATION,
            }:
                if self._last_phase is event.phase:
                    return
            self._last_phase = event.phase if event.kind is ProgressEventKind.PHASE_STARTED else None
            line = self._render(event)
            if line is None:
                return
            self._stream.write(line)
            self._stream.flush()

    def _render(self, event: ProgressEvent) -> str | None:
        if event.kind is ProgressEventKind.PHASE_STARTED:
            preparation = {
                ProgressPhase.RESOLUTION: "Resolving the selected release...",
                ProgressPhase.DOWNLOAD: "Downloading missing content...",
                ProgressPhase.VERIFICATION: "Checking source approval and content integrity...",
                ProgressPhase.PREPARATION: "Preparing executable deployment plan...",
            }
            if event.phase in preparation:
                return preparation[event.phase] + "\n"
            label = {
                ProgressPhase.SUBSCRIPTION: (
                    "[Phase 1] Steps at subscription scope"
                ),
                ProgressPhase.RESOURCE_GROUP: (
                    "[Phase 2] Steps in resource groups"
                ),
                ProgressPhase.TARGETS: "[Execution] Prepared targets",
            }[event.phase]
            return f"\n  {label}: {_plural(event.target_count, 'target')}\n"
        if event.kind is ProgressEventKind.BATCH_STARTED:
            return (
                f"\n  [Parallel] Deploying to {event.target_count} targets "
                f"({event.worker_count} concurrent)\n"
            )
        if event.kind is ProgressEventKind.TARGET_STARTED:
            return f"[{self._target(event)}] starting\n"
        if event.kind is ProgressEventKind.OPERATION_STARTED:
            kind = (
                event.operation_kind.value
                if event.operation_kind is not None
                else "operation"
            )
            return (
                f"[{self._target(event)}] > {self._step(event)} "
                f"({kind})...\n"
            )
        if event.kind is ProgressEventKind.OPERATION_FINISHED:
            symbol = _OPERATION_MARKERS[event.operation_status]
            suffix = ""
            if event.reason is not None:
                detail = (
                    event.reason.summary
                    if self._redacted
                    else event.reason.local_message()
                )
                suffix = f": {sanitize(detail)}"
            return (
                f"[{self._target(event)}] {symbol} {self._step(event)}"
                f"{suffix}\n"
            )
        if event.kind is ProgressEventKind.OPERATION_WAITING:
            return (
                f"[{self._target(event)}] {self._step(event)}: "
                f"waiting, {(event.elapsed or 0):.0f}s elapsed\n"
            )
        if event.kind is ProgressEventKind.TARGET_BLOCKED:
            return (
                f"[{self._target(event)}] {terminal.NOT_RUN} blocked: "
                f"{sanitize(event.reason.summary) if event.reason else 'not run'}\n"
            )
        if event.kind is ProgressEventKind.TARGET_FINISHED:
            status = event.site_status
            marker = _SITE_MARKERS[status] if status is not None else terminal.NOT_RUN
            word = (
                _status_word(status.value)
                if status is not None
                else "finished"
            )
            return (
                f"[{self._target(event)}] {marker} {word} "
                f"in {(event.elapsed or 0.0):.1f}s\n"
            )
        if event.kind is ProgressEventKind.RUN_INTERRUPTED:
            return (
                "Execution was interrupted. Unfinished outcomes are "
                "retained.\n"
            )
        return None

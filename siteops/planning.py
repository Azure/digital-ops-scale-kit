# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Immutable models for prepared Site Ops plans."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, TypeAlias

STEP_OUTPUT_PATTERN = re.compile(
    r"\{\{\s*steps\.([a-zA-Z0-9_-]+)\.outputs\."
    r"([a-zA-Z0-9_.-]+)\s*\}\}"
)
_MALFORMED_TEMPLATE_PATTERN = re.compile(
    r"(?<!\{)\{\s*(?:site|steps)\.[^{}]*\}\}"
)
_SINGLE_BRACE_TEMPLATE_PATTERN = re.compile(
    r"(?<!\{)\{\s*(?:site|steps)\.[^{}]*\}(?!\})"
)
_EXTRA_CLOSER_AFTER_TEMPLATE_PATTERN = re.compile(
    STEP_OUTPUT_PATTERN.pattern + r"\s*\}"
)


class PlanIntent(str, Enum):
    """How completely a plan must prepare an operation."""

    DESCRIBE = "describe"
    EXECUTABLE = "executable"


class PlanStatus(str, Enum):
    """Result of building a plan."""

    PLANNED = "planned"
    INVALID = "invalid"


class PlanProjection(str, Enum):
    """Structured view of a private plan."""

    LOCAL_PRIVATE = "local-private"
    PUBLISHABLE = "publishable"


class DiagnosticSeverity(str, Enum):
    """Importance of a planning diagnostic."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PlanDisposition(str, Enum):
    """Static treatment of an operation."""

    EXECUTE = "execute"
    SKIP = "skip"
    BLOCKED = "blocked"


class SkipReasonCode(str, Enum):
    """Stable category explaining why an operation will not execute."""

    CONDITION_FALSE = "condition-false"
    SCOPE_MISMATCH = "scope-mismatch"
    TARGET_PREPARATION_FAILED = "target-preparation-failed"
    DEPENDENCY_BLOCKED = "dependency-blocked"


class OperationKind(str, Enum):
    """Prepared operation implementation."""

    DEPLOYMENT = "deployment"
    KUBECTL = "kubectl"
    WAIT = "wait"


class OperationScope(str, Enum):
    """Control-plane boundary where an operation executes."""

    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resourceGroup"
    TARGET = "target"


class TargetKind(str, Enum):
    """Azure scope represented by a prepared target."""

    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resourceGroup"


class InputStatus(str, Enum):
    """Preparation state of deployment inputs."""

    DESCRIBED = "described"
    PREPARED = "prepared"
    INVALID = "invalid"


class PlanExecutionMode(str, Enum):
    """Runtime treatment of deferred operation outputs."""

    APPLY = "apply"
    PREVIEW = "preview"


class ResourceDisposition(str, Enum):
    """How a composed resource participates in the plan."""

    APPLY = "apply"
    EXTERNAL = "external"


class PlanNotExecutableError(ValueError):
    """Raised when execution receives an invalid or describe-only plan."""

    def __init__(self, result: PlanBuildResult):
        self.result = result
        super().__init__(self.message(redacted=False))

    def message(self, *, redacted: bool) -> str:
        """Return the diagnostic text permitted for the output destination."""
        if self.result.diagnostics:
            diagnostic = self.result.diagnostics[0]
            if redacted:
                return diagnostic.summary
            return diagnostic.detail or diagnostic.summary
        return "The deployment plan is not executable."


class PlanValueResolutionError(ValueError):
    """Runtime failure with separate local and publishable messages."""

    def __init__(self, detail: str, public_message: str):
        self.public_message = public_message
        super().__init__(detail)


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty.")


@dataclass(frozen=True)
class OperationIdentity:
    """Logical identity that remains stable across plan details."""

    target: str
    step: str

    def __post_init__(self) -> None:
        _require_text(self.target, "Operation target")
        _require_text(self.step, "Operation step")


@dataclass(frozen=True)
class DataReference:
    """Runtime read from a prior logical operation output."""

    source: OperationIdentity
    output_path: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", tuple(self.output_path))
        if not self.output_path:
            raise ValueError("Data reference output path must be non-empty.")
        for segment in self.output_path:
            _require_text(segment, "Data reference output path segment")

    @classmethod
    def from_dotted_path(
        cls,
        source: OperationIdentity,
        output_path: str,
    ) -> DataReference:
        """Build a reference from the manifest's dotted output syntax."""
        return cls(source=source, output_path=tuple(output_path.split(".")))


PlanScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True)
class LiteralValue:
    """Known scalar value."""

    value: PlanScalar

    def __post_init__(self) -> None:
        if not (
            self.value is None
            or isinstance(self.value, (str, int, float, bool))
        ):
            raise TypeError("Literal plan values must be scalar.")


@dataclass(frozen=True)
class OutputValue:
    """Value supplied entirely by a runtime data reference."""

    reference: DataReference


StringPart: TypeAlias = str | DataReference


@dataclass(frozen=True)
class InterpolatedValue:
    """String assembled from literal text and runtime data references."""

    parts: tuple[StringPart, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if not self.parts:
            raise ValueError("Interpolated value must contain at least one part.")
        if not any(isinstance(part, DataReference) for part in self.parts):
            raise ValueError(
                "Interpolated value must contain a data reference."
            )


@dataclass(frozen=True)
class ListValue:
    """Ordered recursive plan values."""

    items: tuple[PlanValue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class MappingEntry:
    """One ordered mapping entry, including a potentially deferred key."""

    key: PlanValue
    value: PlanValue


@dataclass(frozen=True)
class MappingValue:
    """Ordered recursive mapping that can represent deferred keys."""

    entries: tuple[MappingEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


PlanValue: TypeAlias = (
    LiteralValue
    | OutputValue
    | InterpolatedValue
    | ListValue
    | MappingValue
)


def classify_plan_value(
    value: Any,
    available_sources: Mapping[str, OperationIdentity],
    path: str = "",
) -> PlanValue:
    """Classify known and runtime-deferred values recursively.

    Site expressions must already be resolved. The caller supplies only steps
    that execute before the operation being classified, so an unknown, current,
    or later step is rejected through the same boundary.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return LiteralValue(value)
    if isinstance(value, str):
        return _classify_plan_string(value, available_sources, path)
    if isinstance(value, list):
        return ListValue(
            tuple(
                classify_plan_value(
                    item,
                    available_sources,
                    f"{path}[{index}]",
                )
                for index, item in enumerate(value)
            )
        )
    if isinstance(value, dict):
        entries: list[MappingEntry] = []
        for key, item in value.items():
            key_path = f"{path}.<key>" if path else "<key>"
            value_path = (
                f"{path}.{key}"
                if path
                else str(key)
            )
            entries.append(
                MappingEntry(
                    key=classify_plan_value(
                        key,
                        available_sources,
                        key_path,
                    ),
                    value=classify_plan_value(
                        item,
                        available_sources,
                        value_path,
                    ),
                )
            )
        return MappingValue(tuple(entries))
    raise TypeError(
        f"{_value_location(path)} has unsupported type "
        f"{type(value).__name__}."
    )


def _classify_plan_string(
    value: str,
    available_sources: Mapping[str, OperationIdentity],
    path: str,
) -> PlanValue:
    if (
        _MALFORMED_TEMPLATE_PATTERN.search(value)
        or _SINGLE_BRACE_TEMPLATE_PATTERN.search(value)
        or _EXTRA_CLOSER_AFTER_TEMPLATE_PATTERN.search(value)
    ):
        raise ValueError(
            f"{_value_location(path)} contains a malformed template: "
            f"{value!r}."
        )

    stripped = value.strip()
    full_match = STEP_OUTPUT_PATTERN.fullmatch(stripped)
    if full_match:
        return OutputValue(
            _data_reference_from_match(
                full_match,
                available_sources,
                path,
            )
        )

    matches = list(STEP_OUTPUT_PATTERN.finditer(value))
    if matches:
        parts: list[StringPart] = []
        cursor = 0
        for match in matches:
            if match.start() > cursor:
                parts.append(value[cursor:match.start()])
            parts.append(
                _data_reference_from_match(
                    match,
                    available_sources,
                    path,
                )
            )
            cursor = match.end()
        if cursor < len(value):
            parts.append(value[cursor:])
        remainder = STEP_OUTPUT_PATTERN.sub("", value)
        if "{{" in remainder:
            raise ValueError(
                f"{_value_location(path)} contains an unsupported or "
                f"malformed template: {value!r}."
            )
        return InterpolatedValue(tuple(parts))

    if "{{" in value:
        raise ValueError(
            f"{_value_location(path)} contains an unresolved or unsupported "
            f"template: {value!r}."
        )
    return LiteralValue(value)


def _data_reference_from_match(
    match: re.Match[str],
    available_sources: Mapping[str, OperationIdentity],
    path: str,
) -> DataReference:
    step_name = match.group(1)
    source = available_sources.get(step_name)
    if source is None:
        raise ValueError(
            f"{_value_location(path)} references step {step_name!r}, which "
            "is not an available prior operation."
        )
    return DataReference.from_dotted_path(source, match.group(2))


def _value_location(path: str) -> str:
    return f"Plan value at {path!r}" if path else "Plan value"


def collect_data_references(value: PlanValue) -> tuple[DataReference, ...]:
    """Return unique runtime references in first-appearance order."""
    found: list[DataReference] = []

    def collect(current: PlanValue) -> None:
        if isinstance(current, OutputValue):
            if current.reference not in found:
                found.append(current.reference)
        elif isinstance(current, InterpolatedValue):
            for part in current.parts:
                if isinstance(part, DataReference) and part not in found:
                    found.append(part)
        elif isinstance(current, ListValue):
            for item in current.items:
                collect(item)
        elif isinstance(current, MappingValue):
            for entry in current.entries:
                collect(entry.key)
                collect(entry.value)

    collect(value)
    return tuple(found)


def resolve_plan_value(
    value: PlanValue,
    outputs: Mapping[OperationIdentity, Mapping[str, Any]],
    *,
    mode: PlanExecutionMode = PlanExecutionMode.APPLY,
) -> Any:
    """Resolve a prepared value from completed operation outputs."""
    if isinstance(value, LiteralValue):
        return value.value
    if isinstance(value, OutputValue):
        return _resolve_data_reference(value.reference, outputs, mode)
    if isinstance(value, InterpolatedValue):
        parts: list[str] = []
        for part in value.parts:
            if isinstance(part, str):
                parts.append(part)
                continue
            resolved = _resolve_data_reference(part, outputs, mode)
            if isinstance(resolved, (dict, list)):
                raise PlanValueResolutionError(
                    detail=(
                        "A complex operation output cannot be embedded in a "
                        "string."
                    ),
                    public_message=(
                        "A runtime output could not be used in a string."
                    ),
                )
            parts.append(str(resolved))
        return "".join(parts)
    if isinstance(value, ListValue):
        return [
            resolve_plan_value(item, outputs, mode=mode)
            for item in value.items
        ]

    resolved_mapping: dict[Any, Any] = {}
    key_sources: dict[Any, PlanValue] = {}
    for entry in value.entries:
        key = resolve_plan_value(entry.key, outputs, mode=mode)
        if not isinstance(entry.key, LiteralValue) and not isinstance(key, str):
            raise PlanValueResolutionError(
                detail=(
                    "A deferred mapping key must resolve to a string, got "
                    f"{type(key).__name__}."
                ),
                public_message=(
                    "A deferred mapping key resolved to an unsupported type."
                ),
            )
        if key in resolved_mapping:
            raise PlanValueResolutionError(
                detail=(
                    f"Two prepared mapping keys both resolve to {key!r}."
                ),
                public_message=(
                    "Deferred mapping keys resolved to the same name."
                ),
            )
        key_sources[key] = entry.key
        resolved_mapping[key] = resolve_plan_value(
            entry.value,
            outputs,
            mode=mode,
        )
    return resolved_mapping


def _resolve_data_reference(
    reference: DataReference,
    outputs: Mapping[OperationIdentity, Mapping[str, Any]],
    mode: PlanExecutionMode,
) -> Any:
    operation_outputs = outputs.get(reference.source)
    if operation_outputs is None:
        if mode is PlanExecutionMode.PREVIEW:
            return _render_data_reference(reference)
        raise PlanValueResolutionError(
            detail=(
                f"Step '{reference.source.step}' on site "
                f"'{reference.source.target}' has no available outputs."
            ),
            public_message=(
                "A required prior-operation output is unavailable."
            ),
        )

    current: Any = operation_outputs
    for segment in reference.output_path:
        current = _unwrap_arm_output(current)
        if not isinstance(current, Mapping) or segment not in current:
            if mode is PlanExecutionMode.PREVIEW:
                return _render_data_reference(reference)
            raise PlanValueResolutionError(
                detail=(
                    f"Step '{reference.source.step}' on site "
                    f"'{reference.source.target}' has no output at "
                    f"{'.'.join(reference.output_path)!r}."
                ),
                public_message=(
                    "A required prior-operation output is unavailable."
                ),
            )
        current = current[segment]
    return _unwrap_arm_output(current)


def _unwrap_arm_output(value: Any) -> Any:
    if (
        isinstance(value, Mapping)
        and "type" in value
        and "value" in value
    ):
        return value["value"]
    return value


@dataclass(frozen=True)
class PlanSkipReason:
    """Typed local-private explanation for a static disposition."""

    code: SkipReasonCode
    detail: str

    def __post_init__(self) -> None:
        _require_text(self.detail, "Plan skip detail")


@dataclass(frozen=True)
class PlanDiagnostic:
    """Diagnostic with local detail and optional value-free serialized detail."""

    code: str
    severity: DiagnosticSeverity
    summary: str
    detail: str | None = None
    serialized_detail: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.code, "Diagnostic code")
        _require_text(self.summary, "Diagnostic summary")
        if self.detail is not None:
            _require_text(self.detail, "Diagnostic detail")
        if self.serialized_detail is not None:
            _require_text(
                self.serialized_detail,
                "Serialized diagnostic detail",
            )


@dataclass(frozen=True)
class ResourceIdentity:
    """Provider-shaped resource identity with declaration field names."""

    collection: str
    components: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))
        _require_text(self.collection, "Resource identity collection")
        if not self.components:
            raise ValueError(
                "Resource identity must contain at least one component."
            )
        for path, value in self.components:
            _require_text(path, "Resource identity path")
            _require_text(value, "Resource identity value")


@dataclass(frozen=True)
class CompositionSource:
    """Selected parameter source and the site path that selected it."""

    path: Path
    selected_by: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        _require_text(self.selected_by, "Composition source origin")


@dataclass(frozen=True)
class CompositionResource:
    """Applied or externally asserted composed resource."""

    identity: ResourceIdentity
    disposition: ResourceDisposition
    source: Path
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))
        if self.disposition is ResourceDisposition.EXTERNAL:
            if self.reason is None:
                raise ValueError(
                    "External composition resources require a reason."
                )
            _require_text(self.reason, "External resource reason")
        elif self.reason is not None:
            raise ValueError(
                "Applied composition resources cannot carry an external reason."
            )


@dataclass(frozen=True)
class CompositionReference:
    """Resolved or recorded provider reference."""

    rule_id: str
    source: ResourceIdentity
    source_bindings: tuple[tuple[str, str], ...]
    target: ResourceIdentity | None
    target_member_name: str | None = None
    target_member_identity: str | None = None
    external: bool = False
    unverified_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_bindings",
            tuple(self.source_bindings),
        )
        _require_text(self.rule_id, "Composition reference rule")
        for name, value in self.source_bindings:
            _require_text(name, "Composition reference binding name")
            _require_text(value, "Composition reference binding value")
        if self.unverified_reason is not None:
            _require_text(
                self.unverified_reason,
                "Unverified reference reason",
            )
            if self.target is not None:
                raise ValueError(
                    "Unverified composition references cannot carry a target."
                )
        elif self.target is None:
            raise ValueError(
                "Verified composition references require a target."
            )
        if (self.target_member_name is None) != (
            self.target_member_identity is None
        ):
            raise ValueError(
                "Reference target member name and identity must be paired."
            )


@dataclass(frozen=True)
class CompositionRequirement:
    """Source-local co-presence requirement."""

    identity: ResourceIdentity
    source: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))


@dataclass(frozen=True)
class PlanComposition:
    """Effective resource composition for one target."""

    sources: tuple[CompositionSource, ...]
    resources: tuple[CompositionResource, ...]
    references: tuple[CompositionReference, ...]
    requirements: tuple[CompositionRequirement, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "requirements", tuple(self.requirements))


@dataclass(frozen=True)
class DeploymentOperation:
    """Prepared Bicep or ARM deployment details."""

    template: Path
    input_status: InputStatus
    parameters: MappingValue | None = None
    accepted_parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "template", Path(self.template))
        object.__setattr__(
            self,
            "accepted_parameters",
            tuple(self.accepted_parameters),
        )
        if len(self.accepted_parameters) != len(
            set(self.accepted_parameters)
        ):
            raise ValueError("Accepted deployment parameters must be unique.")
        for name in self.accepted_parameters:
            _require_text(name, "Accepted deployment parameter")
        if self.input_status is InputStatus.PREPARED and self.parameters is None:
            raise ValueError(
                "Prepared deployment inputs require a parameter mapping."
            )


@dataclass(frozen=True)
class KubectlOperation:
    """Prepared kubectl operation details."""

    input_status: InputStatus
    operation: str
    cluster_name: PlanValue
    cluster_resource_group: PlanValue
    files: tuple[PlanValue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        _require_text(self.operation, "Kubectl operation")
        if not self.files:
            raise ValueError(
                "Prepared kubectl operation must contain at least one file."
            )


@dataclass(frozen=True)
class ArmTagWaitOperation:
    """Prepared ARM tag wait details."""

    input_status: InputStatus
    resource_id: PlanValue
    tag_key: PlanValue
    expected_value: PlanValue
    failure_pattern: PlanValue | None
    timeout_minutes: int
    poll_interval_seconds: int

    def __post_init__(self) -> None:
        if self.timeout_minutes <= 0:
            raise ValueError("Wait timeout must be positive.")
        if self.poll_interval_seconds <= 0:
            raise ValueError("Wait poll interval must be positive.")
        if self.poll_interval_seconds > self.timeout_minutes * 60:
            raise ValueError("Wait poll interval must not exceed its timeout.")


OperationDetails: TypeAlias = (
    DeploymentOperation | KubectlOperation | ArmTagWaitOperation
)


def _validate_operation_details(
    kind: OperationKind,
    scope: OperationScope,
    details: OperationDetails,
) -> None:
    expected_details = {
        OperationKind.DEPLOYMENT: DeploymentOperation,
        OperationKind.KUBECTL: KubectlOperation,
        OperationKind.WAIT: ArmTagWaitOperation,
    }[kind]
    if not isinstance(details, expected_details):
        raise TypeError(
            f"{kind.value} operation requires "
            f"{expected_details.__name__} details."
        )

    if kind is OperationKind.DEPLOYMENT and scope is OperationScope.TARGET:
        raise ValueError(
            "Deployment operations require an Azure deployment scope."
        )
    if kind is not OperationKind.DEPLOYMENT and scope is not OperationScope.TARGET:
        raise ValueError("Kubectl and wait operations use target scope.")


@dataclass(frozen=True)
class PlanStep:
    """Immutable authored step definition in manifest order."""

    name: str
    sequence: int
    kind: OperationKind
    scope: OperationScope
    details: OperationDetails
    condition: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.name, "Plan step name")
        if self.sequence <= 0:
            raise ValueError("Plan step sequence must be positive.")
        if self.condition is not None:
            _require_text(self.condition, "Plan step condition")
        _validate_operation_details(self.kind, self.scope, self.details)


@dataclass(frozen=True)
class PreparedOperation:
    """One statically classified operation in manifest order."""

    identity: OperationIdentity
    step: PlanStep
    disposition: PlanDisposition
    details: OperationDetails
    skip_reason: PlanSkipReason | None = None
    data_references: tuple[DataReference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "data_references",
            tuple(self.data_references),
        )
        if self.identity.step != self.step.name:
            raise ValueError(
                "Operation identity step must match its plan step."
            )
        if self.disposition is PlanDisposition.EXECUTE:
            if self.skip_reason is not None:
                raise ValueError(
                    "Executable operations cannot carry a skip reason."
                )
        elif self.skip_reason is None:
            raise ValueError(
                "Skipped or blocked operations require a typed reason."
            )
        _validate_operation_details(
            self.step.kind,
            self.step.scope,
            self.details,
        )

    @property
    def sequence(self) -> int:
        """Return the authored manifest sequence."""
        return self.step.sequence

    @property
    def kind(self) -> OperationKind:
        """Return the authored operation kind."""
        return self.step.kind

    @property
    def scope(self) -> OperationScope:
        """Return the authored operation scope."""
        return self.step.scope

    @property
    def condition(self) -> str | None:
        """Return the authored condition text."""
        return self.step.condition


@dataclass(frozen=True)
class PreparedTarget:
    """Resolved private execution target and its ordered operations."""

    name: str
    kind: TargetKind
    subscription: str
    resource_group: str | None
    location: str
    operations: tuple[PreparedOperation, ...]
    composition: PlanComposition | None = None
    diagnostics: tuple[PlanDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        _require_text(self.name, "Target name")
        _require_text(self.subscription, "Target subscription")
        _require_text(self.location, "Target location")
        if self.kind is TargetKind.RESOURCE_GROUP:
            if self.resource_group is None:
                raise ValueError(
                    "Resource-group targets require a resource group."
                )
            _require_text(self.resource_group, "Target resource group")
        elif self.resource_group is not None:
            raise ValueError(
                "Subscription targets cannot carry a resource group."
            )

        identities: set[OperationIdentity] = set()
        sequences: set[int] = set()
        for operation in self.operations:
            if operation.identity.target != self.name:
                raise ValueError(
                    "Operation identity target must match its prepared target."
                )
            if operation.identity in identities:
                raise ValueError(
                    f"Duplicate operation identity: {operation.identity}."
                )
            if operation.sequence in sequences:
                raise ValueError(
                    f"Duplicate operation sequence: {operation.sequence}."
                )
            identities.add(operation.identity)
            sequences.add(operation.sequence)


@dataclass(frozen=True)
class DeploymentPlan:
    """Private prepared deployment plan."""

    manifest_name: str
    source_path: Path
    intent: PlanIntent
    description: str | None
    max_parallel_sites: int
    steps: tuple[PlanStep, ...]
    targets: tuple[PreparedTarget, ...]
    cli_selector: str | None = None
    manifest_selector: str | None = None
    composition_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "targets", tuple(self.targets))
        _require_text(self.manifest_name, "Manifest name")
        if self.max_parallel_sites < 0:
            raise ValueError("Maximum parallel sites must be non-negative.")
        step_names = [step.name for step in self.steps]
        if len(step_names) != len(set(step_names)):
            raise ValueError("Prepared plan step names must be unique.")
        step_sequences = [step.sequence for step in self.steps]
        if len(step_sequences) != len(set(step_sequences)):
            raise ValueError("Prepared plan step sequences must be unique.")
        known_steps = {step.name: step for step in self.steps}
        names = [target.name for target in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("Prepared plan target names must be unique.")
        for target in self.targets:
            for operation in target.operations:
                if known_steps.get(operation.step.name) != operation.step:
                    raise ValueError(
                        "Prepared operation must use a plan step definition."
                    )


@dataclass(frozen=True)
class PlanBuildResult:
    """Plan plus typed status and diagnostics."""

    status: PlanStatus
    executable: bool
    plan: DeploymentPlan | None
    diagnostics: tuple[PlanDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if self.status is PlanStatus.PLANNED and self.plan is None:
            raise ValueError("A planned result requires a plan.")
        if self.status is PlanStatus.INVALID and self.executable:
            raise ValueError("An invalid plan cannot be executable.")
        if self.executable:
            if self.plan is None:
                raise ValueError("An executable result requires a plan.")
            if self.plan.intent is not PlanIntent.EXECUTABLE:
                raise ValueError(
                    "Only executable preparation can produce an executable plan."
                )
            if any(
                diagnostic.severity is DiagnosticSeverity.ERROR
                for diagnostic in self.diagnostics
            ):
                raise ValueError(
                    "An executable plan cannot carry error diagnostics."
                )


def render_plain_plan(
    result: PlanBuildResult,
    *,
    redacted: bool,
) -> str:
    """Render the plain deployment plan deterministically."""
    plan = result.plan
    if plan is None:
        lines = ["Deployment plan is unavailable."]
        for diagnostic in result.diagnostics:
            message = diagnostic.summary if redacted else (
                diagnostic.detail or diagnostic.summary
            )
            lines.append(f"  {diagnostic.severity.value}: {message}")
        lines.append("")
        return "\n".join(lines) + "\n"

    if not plan.targets:
        lines = [f"⚠ No sites matched for manifest '{plan.manifest_name}'"]
        if plan.cli_selector and not redacted:
            lines.append(f"  Selector: {plan.cli_selector}")
        elif plan.manifest_selector and not redacted:
            lines.append(f"  Manifest selector: {plan.manifest_selector}")
        lines.append("")
        return "\n".join(lines) + "\n"

    border = "═" * 60
    lines = [
        border,
        f"  DEPLOYMENT PLAN: {plan.manifest_name}",
    ]
    if plan.cli_selector and not redacted:
        lines.append(f"  (filtered by: {plan.cli_selector})")
    lines.append(border)

    if plan.description:
        lines.extend(("", f"  {plan.description}"))

    if redacted:
        lines.extend(("", f"  Sites: {len(plan.targets)} selected"))
    else:
        lines.extend(("", f"  Sites ({len(plan.targets)}):"))
        lines.extend(
            f"    • {target.name} ({target.location})"
            for target in plan.targets
        )

    lines.extend(
        (
            "",
            f"  Parallel: {_format_parallel(plan.max_parallel_sites)}",
        )
    )

    if plan.composition_enabled:
        lines.extend(("", "  Resource composition:"))
        if redacted:
            _render_redacted_composition(lines, plan.targets)
        else:
            _render_local_composition(lines, plan.targets)

    lines.extend(("", f"  Steps ({len(plan.steps)}):"))
    for step in plan.steps:
        _render_plan_step(lines, step)

    lines.extend(("", border))
    total = sum(
        operation.disposition is PlanDisposition.EXECUTE
        for target in plan.targets
        for operation in target.operations
    )
    lines.append(f"  Total: {total} operation(s)")

    if len(plan.targets) > 1:
        if plan.max_parallel_sites == 1:
            lines.append("  Execution: Sequential (one site at a time)")
        elif plan.max_parallel_sites == 0:
            lines.append("  Execution: Parallel (all sites concurrently)")
        else:
            lines.append(
                "  Execution: Parallel "
                f"(max {plan.max_parallel_sites} concurrent)"
            )
    lines.extend((border, ""))
    return "\n".join(lines) + "\n"


def _format_parallel(max_parallel_sites: int) -> str:
    if max_parallel_sites == 0:
        return "unlimited"
    if max_parallel_sites == 1:
        return "sequential"
    return f"max {max_parallel_sites}"


def _render_redacted_composition(
    lines: list[str],
    targets: tuple[PreparedTarget, ...],
) -> None:
    compositions = [
        target.composition
        for target in targets
        if target.composition is not None
    ]
    source_count = sum(
        len(composition.sources) for composition in compositions
    )
    applied_count = sum(
        resource.disposition is ResourceDisposition.APPLY
        for composition in compositions
        for resource in composition.resources
    )
    external_count = sum(
        resource.disposition is ResourceDisposition.EXTERNAL
        for composition in compositions
        for resource in composition.resources
    )
    verified_count = sum(
        reference.unverified_reason is None
        for composition in compositions
        for reference in composition.references
    )
    unverified_count = sum(
        reference.unverified_reason is not None
        for composition in compositions
        for reference in composition.references
    )
    lines.append(
        f"    Across {len(targets)} site(s): "
        f"{source_count} selected source(s), "
        f"{applied_count} applied resource(s), "
        f"{external_count} external assertion(s)"
    )
    lines.append(
        f"    {verified_count} verified reference(s), "
        f"{unverified_count} recorded reference(s)"
    )

    error_counts: dict[str, int] = {}
    for target in targets:
        for diagnostic in target.diagnostics:
            error_counts[diagnostic.summary] = (
                error_counts.get(diagnostic.summary, 0) + 1
            )
    for message, count in error_counts.items():
        lines.append(f"    {count} site(s): {message}")
    lines.append(
        "    apply semantics: only listed definitions are applied. "
        "Deselecting a set does not delete existing resources"
    )


def _render_local_composition(
    lines: list[str],
    targets: tuple[PreparedTarget, ...],
) -> None:
    for target in targets:
        lines.append(f"    {target.name}:")
        if target.diagnostics:
            for diagnostic in target.diagnostics:
                lines.append(
                    f"      error: {diagnostic.detail or diagnostic.summary}"
                )
            continue

        composition = target.composition
        if composition is None:
            continue
        if composition.sources:
            lines.append("      sets:")
            for source in composition.sources:
                lines.append(
                    f"        {source.path.as_posix()} "
                    f"[selected by {source.selected_by}]"
                )
        else:
            lines.append("      sets: none selected")

        collections: dict[str, list[CompositionResource]] = {}
        for resource in composition.resources:
            collections.setdefault(
                resource.identity.collection,
                [],
            ).append(resource)
        for collection, resources in collections.items():
            lines.append(f"      {collection}:")
            for resource in resources:
                identity = _format_resource_identity(resource.identity)
                if resource.disposition is ResourceDisposition.APPLY:
                    lines.append(
                        f"        apply     {identity} "
                        f"({resource.source.as_posix()})"
                    )
                else:
                    lines.append(
                        f"        external  {identity} "
                        f"({resource.source.as_posix()}): {resource.reason}"
                    )

        for reference in composition.references:
            source = _format_resource_identity(reference.source)
            if reference.unverified_reason is not None:
                bindings = ", ".join(
                    f"{name}={value!r}"
                    for name, value in reference.source_bindings
                )
                binding_suffix = f" [{bindings}]" if bindings else ""
                lines.append(
                    f"      reference [{reference.rule_id}]: "
                    f"{source}{binding_suffix} recorded, not verified: "
                    f"{reference.unverified_reason}"
                )
                continue

            if reference.target is None:
                raise ValueError(
                    "Verified composition reference has no target."
                )
            target = _format_resource_identity(reference.target)
            suffix = " (external)" if reference.external else ""
            member = ""
            if reference.target_member_name is not None:
                member = (
                    f"/{reference.target_member_name}"
                    f"[key={reference.target_member_identity!r}]"
                )
            lines.append(
                f"      reference [{reference.rule_id}]: "
                f"{source} -> {target}{member}{suffix}"
            )

        for requirement in composition.requirements:
            lines.append(
                "      requires: "
                f"{_format_resource_identity(requirement.identity)} "
                f"({requirement.source.as_posix()})"
            )
        lines.append(
            "      apply semantics: only listed definitions are applied. "
            "Deselecting a set does not delete existing resources"
        )


def _render_plan_step(lines: list[str], step: PlanStep) -> None:
    condition = f" [when: {step.condition}]" if step.condition else ""
    details = step.details
    if isinstance(details, KubectlOperation):
        lines.append(
            f"    {step.sequence}. {step.name} "
            f"(kubectl:{details.operation}){condition}"
        )
        lines.append(
            f"       ├─ cluster: {_render_plan_value(details.cluster_name)}"
        )
        for index, file_value in enumerate(details.files):
            prefix = "└─" if index == len(details.files) - 1 else "├─"
            lines.append(
                f"       {prefix} {_render_plan_value(file_value)}"
            )
    elif isinstance(details, ArmTagWaitOperation):
        lines.append(f"    {step.sequence}. {step.name} (wait){condition}")
        lines.append(
            "       ├─ resource: "
            f"{_render_plan_value(details.resource_id)}"
        )
        lines.append(
            f"       ├─ tag: {_render_plan_value(details.tag_key)} == "
            f"{_render_plan_value(details.expected_value)}"
        )
        if details.failure_pattern is not None:
            lines.append(
                "       ├─ failurePattern: "
                f"{_render_plan_value(details.failure_pattern)}"
            )
        lines.append(
            f"       └─ timeout {details.timeout_minutes}m, "
            f"poll {details.poll_interval_seconds}s"
        )
    else:
        lines.append(
            f"    {step.sequence}. {step.name} "
            f"({step.scope.value}){condition}"
        )
        lines.append(f"       └─ {details.template.as_posix()}")


def _format_resource_identity(identity: ResourceIdentity) -> str:
    fields = ", ".join(
        f"{path}={value!r}"
        for path, value in identity.components
    )
    return f"{identity.collection}[{fields}]"


def _render_plan_value(value: PlanValue) -> str:
    if isinstance(value, LiteralValue):
        if value.value is None:
            return "null"
        if isinstance(value.value, bool):
            return str(value.value).lower()
        return str(value.value)
    if isinstance(value, OutputValue):
        return _render_data_reference(value.reference)
    if isinstance(value, InterpolatedValue):
        return "".join(
            part if isinstance(part, str) else _render_data_reference(part)
            for part in value.parts
        )
    if isinstance(value, ListValue):
        return "[" + ", ".join(
            _render_plan_value(item) for item in value.items
        ) + "]"
    return "{" + ", ".join(
        f"{_render_plan_value(entry.key)}: "
        f"{_render_plan_value(entry.value)}"
        for entry in value.entries
    ) + "}"


def _render_data_reference(reference: DataReference) -> str:
    return (
        "{{ steps."
        f"{reference.source.step}.outputs."
        f"{'.'.join(reference.output_path)}"
        " }}"
    )


_PUBLISHABLE_DIAGNOSTICS = {
    "composition.invalid": (
        "Resource composition failed. Set SITEOPS_REDACT_OUTPUT=0, then rerun "
        "the command locally for source and identity details."
    ),
    "operation-preparation.invalid": "Operation preparation failed.",
    "parameter-selection.invalid": (
        "Parameter file selection failed. Set SITEOPS_REDACT_OUTPUT=0, then "
        "rerun the command locally for site and path details."
    ),
    "plan.targeting.required": (
        "Add `sites:` or `selector:` to the manifest, or pass "
        "`-l <key>=<value>`."
    ),
    "subscription-target.missing": (
        "A required subscription target is missing."
    ),
    "validation.failed": "Manifest validation failed.",
}


def serialize_plan(
    result: PlanBuildResult,
    projection: PlanProjection,
    *,
    engine_version: str,
) -> dict[str, Any]:
    """Build one explicit structured projection of a plan result."""
    if projection is PlanProjection.PUBLISHABLE:
        return _publishable_plan_document(result, engine_version)
    return _local_private_plan_document(result, engine_version)


def serialize_plan_json(
    result: PlanBuildResult,
    projection: PlanProjection,
    *,
    engine_version: str,
) -> str:
    """Serialize one deterministic plan document."""
    return json.dumps(
        serialize_plan(
            result,
            projection,
            engine_version=engine_version,
        ),
        indent=2,
        sort_keys=True,
    )


def _plan_envelope(
    result: PlanBuildResult,
    projection: PlanProjection,
    engine_version: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "siteops/v1alpha1",
        "kind": "DeploymentPlan",
        "projection": projection.value,
        "status": result.status.value,
        "executable": result.executable,
        "engine": {
            "name": "siteops",
            "version": engine_version,
        },
    }


def _plan_summary(result: PlanBuildResult) -> dict[str, Any]:
    plan = result.plan
    targets = plan.targets if plan is not None else ()
    operations = tuple(
        operation
        for target in targets
        for operation in target.operations
    )
    compositions = tuple(
        target.composition
        for target in targets
        if target.composition is not None
    )
    return {
        "targetCount": len(targets),
        "operationCount": len(operations),
        "dispositions": {
            disposition.value: sum(
                operation.disposition is disposition
                for operation in operations
            )
            for disposition in PlanDisposition
        },
        "composition": {
            "selectedSourceCount": sum(
                len(composition.sources)
                for composition in compositions
            ),
            "appliedResourceCount": sum(
                resource.disposition is ResourceDisposition.APPLY
                for composition in compositions
                for resource in composition.resources
            ),
            "externalAssertionCount": sum(
                resource.disposition is ResourceDisposition.EXTERNAL
                for composition in compositions
                for resource in composition.resources
            ),
            "verifiedReferenceCount": sum(
                reference.unverified_reason is None
                for composition in compositions
                for reference in composition.references
            ),
            "recordedReferenceCount": sum(
                reference.unverified_reason is not None
                for composition in compositions
                for reference in composition.references
            ),
            "requirementCount": sum(
                len(composition.requirements)
                for composition in compositions
            ),
        },
        "diagnosticCount": len(result.diagnostics),
    }


def _publishable_plan_document(
    result: PlanBuildResult,
    engine_version: str,
) -> dict[str, Any]:
    document = _plan_envelope(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version,
    )
    document["summary"] = _plan_summary(result)
    document["diagnostics"] = [
        {
            "code": (
                diagnostic.code
                if diagnostic.code in _PUBLISHABLE_DIAGNOSTICS
                else "plan.diagnostic"
            ),
            "severity": diagnostic.severity.value,
            "summary": _PUBLISHABLE_DIAGNOSTICS.get(
                diagnostic.code,
                "Plan processing reported a diagnostic.",
            ),
        }
        for diagnostic in result.diagnostics
    ]
    return document


def _local_private_plan_document(
    result: PlanBuildResult,
    engine_version: str,
) -> dict[str, Any]:
    document = _plan_envelope(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version,
    )
    document["summary"] = _plan_summary(result)
    document["diagnostics"] = [
        _local_diagnostic_document(diagnostic)
        for diagnostic in result.diagnostics
    ]
    if result.plan is not None:
        document["plan"] = _local_plan_document(result.plan)
    return document


def _local_diagnostic_document(
    diagnostic: PlanDiagnostic,
) -> dict[str, Any]:
    document = {
        "code": diagnostic.code,
        "severity": diagnostic.severity.value,
        "summary": diagnostic.summary,
    }
    if diagnostic.serialized_detail is not None:
        document["detail"] = diagnostic.serialized_detail
    return document


def _local_plan_document(plan: DeploymentPlan) -> dict[str, Any]:
    return {
        "intent": plan.intent.value,
        "manifest": {
            "name": plan.manifest_name,
            "description": plan.description,
            "sourcePath": plan.source_path.as_posix(),
            "cliSelector": plan.cli_selector,
            "manifestSelector": plan.manifest_selector,
        },
        "parallel": {
            "maxSites": plan.max_parallel_sites,
        },
        "steps": [
            _local_step_document(step)
            for step in plan.steps
        ],
        "targets": [
            _local_target_document(target)
            for target in plan.targets
        ],
    }


def _local_step_document(step: PlanStep) -> dict[str, Any]:
    return {
        "name": step.name,
        "sequence": step.sequence,
        "kind": step.kind.value,
        "scope": step.scope.value,
        "condition": step.condition,
        "details": _local_operation_details(
            step.details,
            include_parameter_descriptors=False,
        ),
    }


def _local_target_document(target: PreparedTarget) -> dict[str, Any]:
    return {
        "name": target.name,
        "kind": target.kind.value,
        "subscription": target.subscription,
        "resourceGroup": target.resource_group,
        "location": target.location,
        "operations": [
            _local_operation_document(operation)
            for operation in target.operations
        ],
        "composition": (
            _local_composition_document(target.composition)
            if target.composition is not None
            else None
        ),
        "diagnostics": [
            _local_diagnostic_document(diagnostic)
            for diagnostic in target.diagnostics
        ],
    }


def _local_operation_document(
    operation: PreparedOperation,
) -> dict[str, Any]:
    return {
        "identity": {
            "target": operation.identity.target,
            "step": operation.identity.step,
        },
        "sequence": operation.sequence,
        "kind": operation.kind.value,
        "scope": operation.scope.value,
        "disposition": operation.disposition.value,
        "condition": operation.condition,
        "skipReason": (
            {
                "code": operation.skip_reason.code.value,
                "detail": operation.skip_reason.detail,
            }
            if operation.skip_reason is not None
            else None
        ),
        "dataReferences": [
            _data_reference_document(reference)
            for reference in operation.data_references
        ],
        "details": _local_operation_details(
            operation.details,
            include_parameter_descriptors=True,
        ),
    }


def _local_operation_details(
    details: OperationDetails,
    *,
    include_parameter_descriptors: bool,
) -> dict[str, Any]:
    if isinstance(details, DeploymentOperation):
        document: dict[str, Any] = {
            "type": "deployment",
            "templatePath": details.template.as_posix(),
            "inputStatus": details.input_status.value,
        }
        if include_parameter_descriptors:
            document["parameters"] = (
                _parameter_descriptors(details.parameters)
                if details.parameters is not None
                else []
            )
            document["valuesSerialized"] = False
        return document
    if isinstance(details, KubectlOperation):
        return {
            "type": "kubectl",
            "inputStatus": details.input_status.value,
            "operation": details.operation,
            "clusterName": _plan_value_descriptor(
                details.cluster_name,
                details.input_status,
            ),
            "clusterResourceGroup": _plan_value_descriptor(
                details.cluster_resource_group,
                details.input_status,
            ),
            "files": [
                _plan_value_descriptor(
                    file_value,
                    details.input_status,
                )
                for file_value in details.files
            ],
            "valuesSerialized": False,
        }
    return {
        "type": "wait",
        "inputStatus": details.input_status.value,
        "conditionType": "arm-tag",
        "resourceId": _plan_value_descriptor(
            details.resource_id,
            details.input_status,
        ),
        "tagKey": _plan_value_descriptor(
            details.tag_key,
            details.input_status,
        ),
        "expectedValue": _plan_value_descriptor(
            details.expected_value,
            details.input_status,
        ),
        "failurePattern": (
            _plan_value_descriptor(
                details.failure_pattern,
                details.input_status,
            )
            if details.failure_pattern is not None
            else None
        ),
        "timeoutMinutes": details.timeout_minutes,
        "pollIntervalSeconds": details.poll_interval_seconds,
        "valuesSerialized": False,
    }


def _parameter_descriptors(
    parameters: MappingValue,
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for entry in parameters.entries:
        references = tuple(
            dict.fromkeys(
                (
                    *collect_data_references(entry.key),
                    *collect_data_references(entry.value),
                )
            )
        )
        name = (
            entry.key.value
            if isinstance(entry.key, LiteralValue)
            and isinstance(entry.key.value, str)
            else None
        )
        descriptors.append(
            {
                "name": name,
                "expectedType": None,
                "resolution": "deferred" if references else "known",
                "dataReferences": [
                    _data_reference_document(reference)
                    for reference in references
                ],
                "serialized": False,
            }
        )
    return descriptors


def _plan_value_descriptor(
    value: PlanValue,
    input_status: InputStatus,
) -> dict[str, Any]:
    references = collect_data_references(value)
    return {
        "resolution": (
            "described"
            if input_status is InputStatus.DESCRIBED
            else "deferred"
            if references
            else "known"
        ),
        "dataReferences": [
            _data_reference_document(reference)
            for reference in references
        ],
        "serialized": False,
    }


def _data_reference_document(
    reference: DataReference,
) -> dict[str, Any]:
    return {
        "source": {
            "target": reference.source.target,
            "step": reference.source.step,
        },
        "outputPath": list(reference.output_path),
    }


def _local_composition_document(
    composition: PlanComposition,
) -> dict[str, Any]:
    return {
        "sources": [
            {
                "path": source.path.as_posix(),
                "selectedBy": source.selected_by,
            }
            for source in composition.sources
        ],
        "resources": [
            {
                "identity": _resource_identity_document(resource.identity),
                "disposition": resource.disposition.value,
                "sourcePath": resource.source.as_posix(),
                "reason": resource.reason,
            }
            for resource in composition.resources
        ],
        "references": [
            {
                "rule": reference.rule_id,
                "source": _resource_identity_document(reference.source),
                "sourceBindings": [
                    {"name": name, "value": value}
                    for name, value in reference.source_bindings
                ],
                "target": (
                    _resource_identity_document(reference.target)
                    if reference.target is not None
                    else None
                ),
                "targetMember": (
                    {
                        "name": reference.target_member_name,
                        "identity": reference.target_member_identity,
                    }
                    if reference.target_member_name is not None
                    else None
                ),
                "external": reference.external,
                "unverifiedReason": reference.unverified_reason,
            }
            for reference in composition.references
        ],
        "requirements": [
            {
                "identity": _resource_identity_document(
                    requirement.identity
                ),
                "sourcePath": requirement.source.as_posix(),
            }
            for requirement in composition.requirements
        ],
    }


def _resource_identity_document(
    identity: ResourceIdentity,
) -> dict[str, Any]:
    return {
        "collection": identity.collection,
        "components": [
            {"path": path, "value": value}
            for path, value in identity.components
        ],
    }

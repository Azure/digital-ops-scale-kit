# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Closed public diagnostics and immutable content validation results."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, NamedTuple

_INDEX = "https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md"
LOCAL_REPRODUCTION = (
    "Run `siteops -w <workspace> validate <manifest>` locally for the file and line."
)
BICEP_COMPILER_SUMMARY = (
    "Azure CLI (`az`) with Bicep is required for Bicep template steps. Install "
    "Azure CLI from https://aka.ms/installazurecli, run `az bicep install`, "
    "then rerun the command."
)
CAPABILITY_SUMMARIES = {
    "capability.arm-control-plane.missing": (
        "Azure CLI (`az`) is required for deployment and wait steps. Install "
        "it from https://aka.ms/installazurecli, then rerun the command."
    ),
    "capability.bicep-compiler.missing": BICEP_COMPILER_SUMMARY,
    "capability.kubectl.missing": (
        "kubectl is required for kubectl steps. Install it from "
        "https://kubernetes.io/docs/tasks/tools/, then rerun the command."
    ),
    "capability.arc-proxy.missing": (
        "Azure CLI (`az`) and its connectedk8s extension are required for "
        "kubectl steps. Install Azure CLI from https://aka.ms/installazurecli, "
        "run `az extension add --name connectedk8s`, then rerun the command."
    ),
}
_SUBJECTS = frozenset({
    "site", "manifest", "parameters", "include", "reference", "composition",
    "inputs", "template", "targeting", "workspace",
})
_MODEL_FIELDS = {
    "site": frozenset({
        "apiVersion", "kind", "name", "description", "inherits", "subscription",
        "resourceGroup", "location", "labels", "properties", "parameters", "metadata", "spec",
    }),
    "manifest": frozenset({
        "apiVersion", "kind", "name", "description", "metadata", "spec", "sites",
        "selector", "siteSelector", "parallel", "parameters", "parameterCompositions", "steps",
    }),
}


@dataclass(frozen=True)
class RegistryCode:
    """A registered member, not producer supplied public text."""

    value: str
    source_alias: str | None = None


@dataclass(frozen=True)
class PublicEntry:
    code: str
    summary: str
    guidance: str
    documentation: str


@dataclass(frozen=True)
class _Definition:
    member: RegistryCode
    summary: str
    guidance: str
    content: bool = False
    addenda: tuple[tuple[str, str], ...] = ()


def _definition(
    code: str, summary: str, guidance: str, *, content: bool = False,
    addenda: tuple[tuple[str, str], ...] = (),
) -> _Definition:
    return _Definition(RegistryCode(code), summary, guidance, content, addenda)


_CONTENT = (
    ("duplicate-key", "A mapping contains a duplicate key.", "Merge the repeated entries or rename one."),
    ("unknown-field", "A configuration document contains an unrecognized key.",
     "Use a supported field in the correct container."),
    ("site-selector-misplaced", "A Site contains Manifest targeting fields.",
     "Move targeting to selector on the Manifest, or pass -l to the command. "
     "Neither selector nor siteSelector belongs in a Site."),
    ("invalid-type", "A configuration field has an invalid value type.",
     "Use the type required by the configuration schema. Quote values that must be text."),
    ("required-value-empty", "A required configuration field has no value.",
     "Provide a value for the required field."),
    ("required-field-missing", "A required configuration field is missing.", "Supply the required field."),
    ("field-placement-invalid", "A configuration field is in the wrong container.",
     "Move the field to its supported container."),
    ("legacy-resource-selection", "A resource set selection uses the legacy scalar form.",
     "Use an ordered list of resource set names. Omit the selection for none, or use [] to clear inheritance."),
    ("condition-invalid", "An operation or include condition is invalid.",
     "Use a supported when expression. Omit when or use null for an unconditional operation."),
    ("path-invalid", "A configuration path is not permitted.",
     "Use a path accepted by the selected workspace boundary. Keep dynamic paths within the "
     "workspace and use HTTPS where remote inputs are permitted."),
    ("file-missing", "A required configuration file was not found.",
     "Restore the required file or correct its reference."),
    ("syntax-invalid", "A configuration document could not be parsed.",
     "Correct the document syntax and encoding."),
    ("file-unreadable", "A configuration file could not be read.",
     "Check local file access and encoding, then validate again."),
    ("schema-unsupported", "A configuration document uses an unsupported schema or kind.",
     "Use the supported document apiVersion and kind."),
    ("other", "Configuration validation reported an unclassified problem.",
     "Inspect the detailed error locally, correct the content, and validate again."),
)
_ADDENDA = {
    "unknown-field": (("site", "Put operator metadata in labels or properties."),),
    "site-selector-misplaced": (
        ("site", "siteSelector was renamed to selector on Manifests in v1.0.0b4."),
    ),
    "required-value-empty": (("site", "Remove an empty scalar when the value should be inherited."),),
    "required-field-missing": (("site", "Inherit it from a SiteTemplate where supported."),),
    "field-placement-invalid": (
        ("site", "Use one Site shape and keep inherits at the top level."),
        ("composition", "Composition metadata and governed resource definitions belong in Manifest parameter sources."),
    ),
    "schema-unsupported": (("site", "Use SiteTemplate only as an inheritance parent."),),
}
_definitions = [
    _definition(f"content.{code}", summary, guidance, content=True, addenda=_ADDENDA.get(code, ()))
    for code, summary, guidance in _CONTENT
]
_PROVIDERS = (
    ("subscription-not-visible", "The target subscription is not visible to the current Azure account.",
     "Check the subscriptions visible to the current account and use an identity with access to the target subscription."),
    ("authorization-failed", "The current identity is not authorized for the requested operation.",
     "Check the identity and its access at the required Azure scope. For unknown work, confirm the provider state before another deployment."),
    ("template-invalid", "The deployment template or its parameters were rejected.",
     "Correct the template or required parameter inputs, then review a new plan. Inspect the full provider detail locally or in the Azure activity log."),
    ("tool-unavailable", "A required local tool is unavailable.",
     "Install or repair the tools required by the selected steps, then plan again."),
    ("timeout", "The local execution or observation deadline was reached.",
     "Confirm the provider state before another deployment. A local deadline does not establish whether cloud work completed."),
    ("other", "The operation failed or its completion could not be established.",
     "Inspect the detailed error locally or in the Azure activity log. Confirm unknown work before another deployment."),
)
_definitions.extend(_definition("provider." + code, summary, guidance) for code, summary, guidance in _PROVIDERS)
_PROVIDER_CATEGORIES = frozenset(code for code, _, _ in _PROVIDERS)
_AZURE_CODES = {
    "SubscriptionNotFound": "subscription-not-visible",
    "AuthorizationFailed": "authorization-failed",
    "InvalidTemplate": "template-invalid",
}
_LEGACY = {
    "plan.diagnostic": "Plan processing reported a diagnostic.",
    "run.diagnostic": "The deployment run reported a diagnostic.",
    "plan.composition-invalid": "Resource composition failed.",
    "plan.operation-preparation-failed": "Operation preparation failed.",
    "plan.parameter-selection-invalid": "Parameter file selection failed.",
    "plan.validation-failed": "Manifest validation failed.",
    "run.validation-failed": "Manifest validation failed.",
    "run.preparation-invalid": "Preparation failed before execution started.",
    "plan.target-set-incomplete": "The selected target set is incomplete.",
    "run.targeting-incomplete": "The selected target set is incomplete.",
    "plan.targeting-empty": "No Sites matched the selected criteria.",
    "run.targeting-empty": "No Sites matched the selected criteria.",
    "plan.targeting-required": "The manifest has no targeting.",
    "run.targeting-required": "The manifest has no targeting.",
    "plan.subscription-target-missing": "A required subscription target is missing.",
    "plan.dependency-blocked": "A required prior operation is unavailable.",
    "run.dependency-unavailable": "A required prior operation is unavailable.",
    "site.invalid": "The supplied Site file is invalid.",
    "inputs.invalid": "Typed Site inputs are unavailable or invalid.",
    "plan.targeting.conflict": "The supplied Site conflicts with the selected target requirements.",
    "lookup.missing": "Manifest not found.",
    "lookup.incomplete": "Name lookup requires a complete inventory.",
    "lookup.ambiguous": "Manifest selection is ambiguous.",
    "plan.compilation-failed": "Template compilation failed.",
    "run.compilation-failed": "Template compilation failed.",
    "plan.capability-unavailable": "A required local tool is unavailable.",
    "run.capability-unavailable": "A required local tool is unavailable.",
    "run.interrupted": "Execution was interrupted before every operation finished.",
    "run.progress-reporting-failed": "Progress reporting stopped before execution completed.",
}
_TOOL_GUIDANCE = (
    "Install the tools required by the selected steps. Azure CLI is available at "
    "https://aka.ms/installazurecli. For Bicep, run `az bicep install`. For Arc proxy access, "
    "run `az extension add --name connectedk8s`. Install kubectl from "
    "https://kubernetes.io/docs/tasks/tools/, then plan again."
)
_GUIDANCE = {
    "plan.targeting-required": "Add sites or selector to the Manifest, or pass -l <key>=<value>.",
    "run.targeting-required": "Add sites or selector to the Manifest, or pass -l <key>=<value>.",
    "plan.target-set-incomplete": "Correct the Site loading diagnostics or select explicit valid Sites.",
    "run.targeting-incomplete": "Correct the Site loading diagnostics or select explicit valid Sites.",
    "inputs.invalid": "Run `siteops inputs` for the declared input contract, then correct the answers.",
    "lookup.missing": "Run `siteops browse` to list Manifests, then use an exact name or path.",
}
for _code, _summary in _LEGACY.items():
    _tool = _code.endswith("capability-unavailable")
    _definitions.append(_definition(
        _code, _summary,
        _TOOL_GUIDANCE if _tool else _GUIDANCE.get(
            _code, "Inspect the detailed error locally and correct the reported problem.",
        ),
        content=not _tool and "compilation" not in _code and _code not in {
            "run.interrupted", "run.progress-reporting-failed", "run.diagnostic", "plan.diagnostic",
        },
    ))
_RESOURCE_SUMMARIES = {
    "invalid-id": "The resource ID is invalid.",
    "invalid-type": "The resource type is invalid.",
    "type-mismatch": "The resource ID does not have the required type.",
    "invalid-api-version": "The resource API version is invalid.",
    "invalid-observation": "The resource observation is invalid.",
    "unsupported-fact": "The requested resource fact is unsupported.",
    "unsupported-reference": "The requested resource relationship is unsupported.",
    "unsupported-provider": "The resource reader is unavailable.",
    "tool-missing": "The resource reader's local tool is unavailable.",
    "timeout": "The resource read reached its deadline.",
    "response-limit": "The resource read exceeded its output limit.",
    "not-found": "The resource was not found.",
    "forbidden": "The current identity cannot read the resource.",
    "not-logged-in": "The current Azure CLI session is not signed in.",
    "subscription-missing": "The subscription is not visible to the current Azure account.",
    "cancelled": "The resource read was cancelled.",
    "failed": "The resource read failed.",
    "subscription-mismatch": "The resource conflicts with the Site subscription.",
    "resource-group-mismatch": "The resource conflicts with the required resource group.",
    "conflict": "Resource inputs conflict with other supplied inputs.",
    "requirement-unmet": "A required resource fact was not observed.",
    "requirement-unverified": "A required resource fact has not been verified.",
    "nothing-to-read": "No active resource input was supplied.",
    "read-required": "Validation does not read Azure resources.",
    "provider-unavailable": "The selected resource reader is unavailable.",
}
_RESOURCE_GUIDANCE = {
    "tool-missing": "Install Azure CLI from https://aka.ms/installazurecli, then read the declared resource again.",
    "not-logged-in": "Run `az login` locally with the intended identity, then read the declared resource again.",
    "subscription-missing": (
        "The subscription is not visible to the account signed in to Azure CLI. "
        "Use an identity with read access to the required subscription."
    ),
}
for _code, _summary in _RESOURCE_SUMMARIES.items():
    _definitions.append(_definition(
        f"inputs.resource.{_code}", _summary,
        _RESOURCE_GUIDANCE.get(
            _code, "Check the declared input and required read access. Use `siteops inputs` for the input contract.",
        ),
        content=True,
    ))
_base_definitions = MappingProxyType({entry.member.value: entry for entry in _definitions})
_PUBLIC_NAMES = {
    "composition.invalid": ("plan.composition-invalid", "run.preparation-invalid"),
    "operation-preparation.invalid": ("plan.operation-preparation-failed", "run.preparation-invalid"),
    "operation.dependency-blocked": ("plan.dependency-blocked", "run.dependency-unavailable"),
    "parameter-selection.invalid": ("plan.parameter-selection-invalid", "run.preparation-invalid"),
    "validation.failed": ("plan.validation-failed", "run.validation-failed"),
    "plan.target-set-incomplete": ("plan.target-set-incomplete", "run.targeting-incomplete"),
    "plan.targeting.empty": ("plan.targeting-empty", "run.targeting-empty"),
    "plan.targeting.required": ("plan.targeting-required", "run.targeting-required"),
    "subscription-target.missing": ("plan.subscription-target-missing", "run.preparation-invalid"),
    "run-interrupted": ("run.interrupted", "run.interrupted"),
    "progress-reporting-failed": ("run.progress-reporting-failed", "run.progress-reporting-failed"),
    **{code: ("plan.capability-unavailable", "run.capability-unavailable") for code in CAPABILITY_SUMMARIES},
    **{
        f"compilation.{suffix}": (
            ("plan.capability-unavailable", "run.capability-unavailable")
            if suffix in {"tool-missing", "tool-unavailable"}
            else ("plan.compilation-failed", "run.compilation-failed")
        )
        for suffix in (
            "failed", "input-changed", "module-unavailable", "output-invalid",
            "source-unreadable", "timeout", "tool-missing", "tool-unavailable",
        )
    },
}
_ALIAS_SUMMARIES = {
    **CAPABILITY_SUMMARIES,
    "compilation.tool-missing": BICEP_COMPILER_SUMMARY,
    "compilation.tool-unavailable": BICEP_COMPILER_SUMMARY,
}
for _code, _aliases in _PUBLIC_NAMES.items():
    if _code not in _base_definitions:
        _base = _base_definitions[_aliases[0]]
        _definitions.append(_definition(
            _code, _ALIAS_SUMMARIES.get(_code, _base.summary), _base.guidance,
            content=_base.content, addenda=_base.addenda,
        ))
REGISTRY = MappingProxyType({entry.member.value: entry for entry in _definitions})
_PUBLIC_MEMBERS = MappingProxyType({
    (source, kind): RegistryCode(_PUBLIC_NAMES[source][index], source_alias=source)
    for source in _ALIAS_SUMMARIES
    for index, kind in enumerate(("DeploymentPlan", "DeploymentRun"))
})


def resolve_member(code: str, kind: str = "ValidationResult", *, private: bool = False) -> RegistryCode:
    """Resolve producer codes without admitting producer text or prefix matches."""
    if type(code) is str:
        if not private:
            member = _PUBLIC_MEMBERS.get((code, kind))
            if member is not None:
                return member
        aliases = _PUBLIC_NAMES.get(code)
        if not private and aliases is not None and kind in {"ValidationResult", "DeploymentPlan", "DeploymentRun"}:
            code = aliases[kind == "DeploymentRun"]
        entry = REGISTRY.get(code)
        if entry is not None:
            return entry.member
    fallback = {"DeploymentPlan": "plan.diagnostic", "DeploymentRun": "run.diagnostic"}.get(kind, "content.other")
    return REGISTRY[fallback].member


def public_entry(member: RegistryCode, subject: str | None) -> PublicEntry:
    """Look up every public text field from an actual registry member."""
    definition = REGISTRY.get(member.source_alias or member.value) if type(member) is RegistryCode else None
    registered = definition is not None and (
        definition.member is member or any(
            _PUBLIC_MEMBERS.get((member.source_alias, kind)) is member
            for kind in ("DeploymentPlan", "DeploymentRun")
        )
    )
    if not registered:
        raise ValueError("Publication requires a registered diagnostic member.")
    guidance = definition.guidance
    addendum = dict(definition.addenda).get(subject) if type(subject) is str else None
    if addendum:
        guidance = f"{guidance} {addendum}"
    if definition.content:
        guidance = f"{guidance}\n{LOCAL_REPRODUCTION}"
    return PublicEntry(member.value, definition.summary, guidance, f"{_INDEX}#{member.value.replace('.', '')}")


def public_document(
    member: RegistryCode, severity: str, *,
    subject: str | None = None, schema_field: str | None = None,
) -> dict[str, str]:
    """Publish only registry text and independently allowed structural fields."""
    subject = subject if type(subject) is str and subject in _SUBJECTS else None
    entry = public_entry(member, subject)
    document = {
        "code": entry.code, "severity": severity, "summary": entry.summary,
        "guidance": entry.guidance, "documentation": entry.documentation,
    }
    if subject is not None:
        document["subject"] = subject
    fields = _MODEL_FIELDS.get(subject, frozenset())
    if member.value == "content.site-selector-misplaced":
        fields = frozenset({"siteSelector"}) if subject == "site" else frozenset()
    field_codes = {
        "content.unknown-field", "content.site-selector-misplaced", "content.invalid-type",
        "content.required-value-empty", "content.required-field-missing",
        "content.field-placement-invalid", "content.path-invalid", "content.schema-unsupported",
    }
    if member.value in field_codes and schema_field in fields:
        document["schemaField"] = schema_field
    return document


@dataclass(frozen=True)
class ProviderFailure:
    category: str = "other"
    azure_code: str | None = None


OTHER_FAILURE = ProviderFailure()


def normalize_provider_failure(value: Any) -> ProviderFailure:
    if type(value) is not ProviderFailure or type(value.category) is not str or value.category not in _PROVIDER_CATEGORIES:
        return OTHER_FAILURE
    if value.azure_code is None:
        return value
    if type(value.azure_code) is str and _AZURE_CODES.get(value.azure_code) == value.category:
        return value
    return OTHER_FAILURE


def provider_document(value: Any) -> dict[str, str]:
    failure = normalize_provider_failure(value)
    entry = public_entry(resolve_member("provider." + failure.category), None)
    document = {"category": failure.category, "summary": entry.summary,
                "guidance": entry.guidance, "documentation": entry.documentation}
    if failure.azure_code is not None:
        document["azureCode"] = failure.azure_code
    return document


def arm_failure(nodes: Any) -> ProviderFailure:
    """Reduce only documented error nodes, with bounded structural admission."""
    if type(nodes) not in (list, tuple) or not nodes or len(nodes) > 64:
        return OTHER_FAILURE
    pending = [(node, 0) for node in nodes]
    seen = set()
    causes = []
    try:
        while pending:
            node, depth = pending.pop()
            if type(node) is not dict or depth > 8 or id(node) in seen or len(seen) >= 64:
                return OTHER_FAILURE
            seen.add(id(node))
            code = node.get("code")
            if type(code) is not str or not code or len(code) > 64:
                return OTHER_FAILURE
            children = []
            if "details" in node:
                if type(node["details"]) is not list or len(node["details"]) > 64:
                    return OTHER_FAILURE
                children.extend(node["details"])
            if "innererror" in node:
                if type(node["innererror"]) is not dict:
                    return OTHER_FAILURE
                children.append(node["innererror"])
            if len(seen) + len(pending) + len(children) > 64:
                return OTHER_FAILURE
            if code != "DeploymentFailed" or not children:
                category = _AZURE_CODES.get(code)
                if category is None:
                    return OTHER_FAILURE
                causes.append((category, code))
            pending.extend((child, depth + 1) for child in children)
        return ProviderFailure(*causes[0]) if causes and len(set(causes)) == 1 else OTHER_FAILURE
    except Exception:
        return OTHER_FAILURE


@dataclass(frozen=True)
class ContentDiagnostic:
    code: str
    private_detail: str
    subject: str | None = None
    schema_field: str | None = None
    source_path: Path | None = None
    line: int | None = None
    column: int | None = None
    severity: str = "error"

    def document(self, *, private: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = public_document(
            resolve_member(self.code, private=private), self.severity,
            subject=self.subject, schema_field=self.schema_field,
        )
        if private:
            result["code"] = self.code
            context = {}
            if self.source_path is not None:
                context["sourcePath"] = str(self.source_path)
            for name in ("line", "column"):
                value = getattr(self, name)
                if type(value) is int and value > 0:
                    context[name] = value
            if context:
                result["context"] = context
        return result


class ContentError(ValueError):
    """An expected content rejection carrying its cause across wrappers."""

    diagnostic_code = "content.other"
    diagnostic_subject: str | None = None

    def __init__(
        self, message: str, *, code: str | None = None,
        diagnostic: ContentDiagnostic | None = None, subject: str | None = None,
        schema_field: str | None = None, source: Path | None = None,
    ):
        super().__init__(message)
        self.diagnostic = replace(diagnostic, private_detail=message) if diagnostic else ContentDiagnostic(
            code or self.diagnostic_code, message, subject or self.diagnostic_subject, schema_field, source,
        )


def diagnostic_from_error(
    error: Exception, *, subject: str | None = None, source: Path | None = None,
) -> ContentDiagnostic:
    """Normalize only at an expected parse/load/content error boundary."""
    import yaml

    from siteops.yamlio import DuplicateKeyError, YamlStructureLimitError

    existing = getattr(error, "diagnostic", None)
    if isinstance(existing, ContentDiagnostic):
        path = existing.source_path
        if source is not None and (path is None or not path.is_absolute()):
            path = source
        return replace(
            existing, subject=existing.subject or subject,
            source_path=path,
            private_detail=getattr(error, "private_message", None) or existing.private_detail,
        )
    if isinstance(error, DuplicateKeyError):
        code = "content.duplicate-key"
    elif isinstance(error, YamlStructureLimitError):
        code = "content.other"
    elif isinstance(error, (yaml.YAMLError, UnicodeError, json.JSONDecodeError)):
        code = "content.syntax-invalid"
    elif isinstance(error, FileNotFoundError):
        code = "content.file-missing"
    elif isinstance(error, OSError):
        code = "content.file-unreadable"
    else:
        code = "content.other"
    mark = getattr(error, "problem_mark", None)
    if mark is not None and type(mark.name) is str and Path(mark.name).is_absolute():
        source = Path(mark.name)
    return ContentDiagnostic(
        code, str(error) or type(error).__name__, subject, source_path=source,
        line=mark.line + 1 if mark is not None else None,
        column=mark.column + 1 if mark is not None else None,
    )


class SiteLoadFailure(NamedTuple):
    name: str
    diagnostic: ContentDiagnostic


@dataclass(frozen=True)
class DiagnosticCollector:
    """Collect independent issues without shared mutable state or fail-fast behavior."""

    diagnostics: tuple[ContentDiagnostic, ...] = ()

    def add(self, detail: str, *, code: str = "content.other", subject: str | None = None) -> DiagnosticCollector:
        return self.extend((ContentDiagnostic(code, detail, subject),))

    def error(
        self, error: Exception, *, subject: str | None = None,
        source: Path | None = None, detail: str | None = None,
        fallback_code: str | None = None,
    ) -> DiagnosticCollector:
        diagnostic = diagnostic_from_error(error, subject=subject, source=source)
        if diagnostic.code == "content.other" and fallback_code is not None:
            diagnostic = replace(diagnostic, code=fallback_code)
        if detail is not None:
            diagnostic = replace(diagnostic, private_detail=detail)
        return self.extend((diagnostic,))

    def extend(self, diagnostics: Iterable[ContentDiagnostic]) -> DiagnosticCollector:
        return DiagnosticCollector(self.diagnostics + tuple(diagnostics))


@dataclass(frozen=True)
class ValidationResult:
    diagnostics: tuple[ContentDiagnostic, ...] = ()
    target_resolution: str = "not-requested"
    manifest_name: str | None = None
    source_path: Path | None = None

    @property
    def status(self) -> str:
        return "invalid" if any(issue.severity == "error" for issue in self.diagnostics) else "valid"

    @property
    def exit_code(self) -> int:
        return int(self.status == "invalid")

    def local_messages(self) -> list[str]:
        return [diagnostic.private_detail for diagnostic in self.diagnostics]

    def document(self, projection: str, engine_version: str) -> dict[str, Any]:
        private = projection == "local-private"
        result = {
            "apiVersion": "siteops/v1alpha1", "kind": "ValidationResult",
            "projection": projection, "engine": {"name": "siteops", "version": engine_version},
            "status": self.status, "exitCode": self.exit_code,
            "summary": {"diagnosticCount": len(self.diagnostics), "targetResolution": self.target_resolution},
            "diagnostics": [diagnostic.document(private=private) for diagnostic in self.diagnostics],
        }
        if private and self.manifest_name is not None:
            result["manifest"] = {"name": self.manifest_name, "sourcePath": str(self.source_path)}
        return result


def grouped_diagnostics(documents: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Count published occurrences without changing records or private context."""
    records: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for document in documents:
        public = {key: value for key, value in document.items() if key != "context"}
        key = json.dumps(public, sort_keys=True)
        records.setdefault(key, public)
        counts[key] += 1
    return [(document, counts[key]) for key, document in records.items()]


def render_diagnostics(documents: Iterable[dict[str, Any]], *, indent: str = "  ") -> list[str]:
    """Render fixed published text with presentation occurrence counts."""
    from siteops import terminal

    lines = []
    for document, count in grouped_diagnostics(documents):
        occurrence = f" ({count} occurrences)" if count > 1 else ""
        head = f"{document['severity'].capitalize()}: {document['code']}{occurrence}: {document['summary']}"
        lines.extend(terminal.wrap(head, indent=indent, hanging=indent + "  "))
        if document.get("schemaField"):
            lines.extend(terminal.wrap(f"Field: {document['schemaField']}.", indent=indent + "  "))
        for text in document.get("guidance", "").splitlines():
            lines.extend(terminal.wrap(text, indent=indent + "  "))
        if document.get("documentation"):
            lines.extend(terminal.wrap(document["documentation"], indent=indent + "  "))
    return lines

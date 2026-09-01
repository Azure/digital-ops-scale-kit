# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Core data models for Azure Site Ops.

This module defines the core resource types:
- Site: A deployment target (subscription, resource group, location)
- Manifest: Orchestrates deployment steps across sites
- DeploymentStep: A single Bicep/ARM template deployment
- KubectlStep: A kubectl operation against an Arc-connected cluster

Resources support K8s-style apiVersion/kind validation:
- apiVersion defaults to 'siteops/v1' if not specified
- kind is validated if present, but optional
"""

import re
from dataclasses import MISSING, dataclass, field, fields
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar

from siteops import yamlio


class DataclassInstance(Protocol):
    """Any dataclass instance, which is what `fields()` accepts."""

    __dataclass_fields__: ClassVar[dict[str, Any]]


# A `list` or `dict`, tying `_require_collection`'s return to the type asked for.
_CollectionT = TypeVar("_CollectionT", list, dict)

VALID_SCOPES = {"subscription", "resourceGroup"}
DEFAULT_API_VERSION = "siteops/v1"
SUPPORTED_API_VERSIONS = {"siteops/v1"}

# Maximum depth of recursive `include:` resolution. Anything deeper is a smell;
# the cap exists to surface mistakes early rather than to bound real designs.
MAX_INCLUDE_DEPTH = 8

# Reserved keys for the `include:` step shape. Any other key on an include step
# is an authoring error.
_INCLUDE_ALLOWED_KEYS = {"include", "when"}
_PARAMETER_SOURCE_KEYS = {"path", "forEach", "collections"}

# Allowed top-level keys on a flat-shape Manifest (most common form). Any
# other key triggers a parse-time error with a "did you mean?" hint when the
# unknown key is close to a known one. Catches typos like `site:` (singular)
# or `selctor:` that today silently degrade to "missing field".
_MANIFEST_FLAT_KNOWN_KEYS = {
    "apiVersion",
    "kind",
    "name",
    "description",
    "sites",
    "selector",
    "siteSelector",
    "parallel",
    "parameters",
    "parameterCompositions",
    "steps",
}

# K8s-style nested envelope. Top-level allows only the four envelope keys.
# `metadata` carries name/description/labels. `spec` carries everything else.
_MANIFEST_NESTED_TOP_KEYS = {"apiVersion", "kind", "metadata", "spec"}
_MANIFEST_NESTED_METADATA_KEYS = {"name", "description", "labels"}
_MANIFEST_NESTED_SPEC_KEYS = _MANIFEST_FLAT_KNOWN_KEYS - {"apiVersion", "kind", "name", "description"}

# Site files, both shapes. The set is what `Site.from_file` and the
# orchestrator's merged-data path read, so it cannot drift from the parser.
#
# Only the envelope is closed. `labels`, `properties`, and `parameters` hold
# operator-defined content and stay open, so a key inside them is checked
# against workspace usage instead, which `siteops validate` reports.
#
# `description` is accepted and not read. Manifests carry one, so a site that
# already has it should not start failing now that unknown keys are rejected.
# Nothing else is added on that basis. A key the engine does not read is
# rejected, which is the whole point of closing the envelope, and `annotations`
# in particular is rejected on a manifest already.
_SITE_FLAT_KNOWN_KEYS = {
    "apiVersion",
    "kind",
    "name",
    "description",
    "inherits",
    "subscription",
    "resourceGroup",
    "location",
    "labels",
    "properties",
    "parameters",
}
_SITE_NESTED_TOP_KEYS = {"apiVersion", "kind", "metadata", "spec"}
_SITE_NESTED_METADATA_KEYS = {"name", "description", "labels"}
# `inherits` is absent here on purpose. Only the flat shape is read for it, so
# admitting it in a spec would accept a key that silently does nothing.
_SITE_NESTED_SPEC_KEYS = _SITE_FLAT_KNOWN_KEYS - {
    "apiVersion",
    "kind",
    "name",
    "description",
    "labels",
    "inherits",
}

# Required on a resolved site. Checked after any inherit and overlay merge, so
# a child may leave one to its parent template.
_SITE_REQUIRED_SPEC_KEYS = ("subscription", "location")

# Keys that hold operator-defined content. Open as to which keys they carry,
# closed as to being mappings, since every reader indexes into them.
_SITE_MAPPING_KEYS = ("labels", "properties", "parameters")

# Keys whose value is used as text: an identity, a path, or a command-line
# argument. Checked as a set rather than one at a time, so a field added to the
# envelope is covered by being listed rather than by remembering to guard it.
# A name is the one that fails worst, since it becomes a dictionary key and a
# list value that is not hashable raises before any message can name the file.
_SITE_STRING_KEYS = (
    "name",
    "description",
    "inherits",
    "subscription",
    "resourceGroup",
    "location",
)

# Guidance for a key that is rejected for a reason the closest spelling cannot
# express. A real field in the wrong container has a correct answer, and a
# fuzzy match sends the reader to a different field that is also wrong:
# `description` in a spec suggested `subscription`, which is neither where it
# belongs nor what it means.
_SITE_UNREAD_ADVICE = {
    # Recognizable to anyone who writes Kubernetes manifests, and read by
    # nothing here. Named so the answer is where operator metadata goes rather
    # than the nearest spelling.
    "annotations": (
        "not read by siteops. Put operator metadata in `labels`, which "
        "selectors match on, or in `properties`, which templates read"
    ),
    "namespace": (
        "not read by siteops. A site targets a subscription and a resource "
        "group, which `subscription` and `resourceGroup` name"
    ),
}
_SITE_TOP_LEVEL_ADVICE = {
    **_SITE_UNREAD_ADVICE,
    **{key: "belongs under `metadata`" for key in _SITE_NESTED_METADATA_KEYS},
    **{key: "belongs under `spec`" for key in _SITE_NESTED_SPEC_KEYS},
}
_SITE_METADATA_ADVICE = {
    **_SITE_UNREAD_ADVICE,
    **{key: "belongs under `spec`" for key in _SITE_NESTED_SPEC_KEYS},
}
_SITE_SPEC_ADVICE = {
    **_SITE_UNREAD_ADVICE,
    **{key: "belongs under `metadata`" for key in _SITE_NESTED_METADATA_KEYS},
    "inherits": "is read at the top level of the file, in either shape",
}


def validate_site_required(spec: dict, source: Path | str) -> None:
    """Reject a site whose required field is absent or carries no value.

    Presence alone is not enough. A key written with nothing after the colon
    parses as `None` and reaches a command line as the string `None` rather
    than failing here.

    Args:
        spec: The site's spec mapping, after any inherit and overlay merge.
        source: Path or label naming the file to report.

    Raises:
        ValueError: A required field is absent or empty.
    """
    for key in _SITE_REQUIRED_SPEC_KEYS:
        if key not in spec:
            raise ValueError(f"Missing required field '{key}' in site: {source}")
        value = spec[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"Required field '{key}' in site '{source}' has no value. "
                f"Give it one, inherit it from a parent template, or remove "
                f"the key entirely, since a key written with no value "
                f"overrides the inherited one."
            )


def validate_site_keys(data: dict, source: Path | str) -> None:
    """Reject a site key that no parser reads.

    A misspelled envelope key is silent otherwise: `paramaters` contributes
    nothing and the site deploys with defaults. Applied to merged data, so it
    covers a key an overlay contributes as well as one the base file carries.

    Args:
        data: Site data, either shape, after any inherit and overlay merge.
        source: Path or label naming the file to report.

    Raises:
        ValueError: A key is not one the site parser reads.
    """
    path = source if isinstance(source, Path) else Path(str(source))
    if "spec" in data:
        # A file carrying `spec` and also carrying flat-shape fields at the top
        # level is two shapes at once. Reported as the shape problem it is,
        # since listing the fields as unknown keys says three real field names
        # are not real, which is the opposite of what the reader needs.
        misplaced = sorted(
            (set(data) - _SITE_NESTED_TOP_KEYS)
            & (_SITE_NESTED_METADATA_KEYS | _SITE_NESTED_SPEC_KEYS)
        )
        if misplaced:
            named = ", ".join(f"`{key}`" for key in misplaced)
            raise ValueError(
                f"Site '{source}' mixes the two site shapes. It declares `spec`, "
                f"which is the envelope shape, and also carries {named} at the "
                f"top level, where only the flat shape reads them. Move them "
                f"under `metadata` or `spec`, or remove `spec` and put every "
                f"field at the top level."
            )
        _validate_known_keys(
            data, _SITE_NESTED_TOP_KEYS, path, "top-level", "Site", _SITE_TOP_LEVEL_ADVICE
        )
        metadata = _require_envelope_mapping(data, "metadata", source, "Site")
        if metadata:
            _validate_known_keys(
                metadata,
                _SITE_NESTED_METADATA_KEYS,
                path,
                "metadata",
                "Site",
                _SITE_METADATA_ADVICE,
            )
        spec = _require_envelope_mapping(data, "spec", source, "Site")
        if "inherits" in spec:
            # Named rather than reported as an unknown key, since `inherits` is
            # a real field and the reader needs to know it is the placement
            # that is wrong rather than the spelling.
            raise ValueError(
                f"Site '{source}' declares `inherits` inside `spec`, which is "
                f"never read. Move it to the top level of the file, alongside "
                f"`apiVersion` and `kind`. Both site shapes inherit that way."
            )
        _validate_known_keys(
            spec, _SITE_NESTED_SPEC_KEYS, path, "spec", "Site", _SITE_SPEC_ADVICE
        )
        containers = [metadata, spec]
    else:
        _validate_known_keys(
            data, _SITE_FLAT_KNOWN_KEYS, path, "top-level", "Site", _SITE_UNREAD_ADVICE
        )
        containers = [data]

    # An open container still has to be a mapping. A scalar here fails much
    # later, at the first read, with an error naming neither the file nor the
    # key.
    for container in containers:
        for key in _SITE_MAPPING_KEYS:
            if key in container and container[key] is not None:
                if not isinstance(container[key], dict):
                    raise ValueError(
                        f"'{key}' in site '{source}' must be a mapping, got "
                        f"{type(container[key]).__name__}."
                    )
        for key in _SITE_STRING_KEYS:
            if key in container and container[key] is not None:
                if not isinstance(container[key], str):
                    raise ValueError(
                        f"'{key}' in site '{source}' must be text, got "
                        f"{type(container[key]).__name__}. Quote the value if "
                        f"it is meant to be text."
                    )

        # A label value is compared against a selector, which is text, so a
        # value of any other type matches nothing at all. Rejected rather than
        # coerced: coercing would make a site start matching a selector it
        # never matched, which changes what a deployment targets.
        labels = container.get("labels")
        if isinstance(labels, dict):
            for label, value in labels.items():
                if not isinstance(value, str):
                    raise ValueError(
                        f"Label '{label}' in site '{source}' must be text, got "
                        f"{type(value).__name__}. Selectors compare text, so "
                        f"this label matches nothing. Quote the value."
                    )


def _normalize_null_collections(instance: DataclassInstance) -> None:
    """Replace a `None` collection field with its declared empty default.

    A YAML key written with nothing after the colon parses as `None`, and
    `dict.get(key, default)` returns that `None`, since the default applies
    only when the key is absent. The `None` then reaches a reader promised a
    collection and fails there, far from the file, naming neither.

    Scope is deliberately narrow. A field declaring a default factory is
    asserting it is never `None`, so that declaration is the rule, and a field
    where `None` is meaningful declares no factory and is left alone. This does
    not type-check scalars. `validate_site_required` covers the two that
    matter, and rejecting rather than normalizing is right for those, since an
    empty required scalar is not a request for a default.
    """
    for spec in fields(instance):
        if spec.default_factory is MISSING:
            continue
        if getattr(instance, spec.name) is None:
            setattr(instance, spec.name, spec.default_factory())


def _require_collection(
    data: dict[str, Any],
    key: str,
    source: Path | str,
    kind: type[_CollectionT],
    element_kind: type | None = None,
) -> _CollectionT:
    """Read an optional collection, treating a null value as absent.

    Used where a value is consumed before any model is constructed, so
    `_normalize_null_collections` has not run yet. Reports a wrong type against
    the file and key rather than letting it surface later as a bare
    `TypeError` from whatever first iterates it.

    Args:
        data: The mapping to read from.
        key: The key to read.
        source: Path or label naming the file to report.
        kind: The collection type the caller requires, `list` or `dict`.
        element_kind: When given, the type every element must be. A list of
            paths or identifiers is unusable if one entry is a number, and
            YAML produces one from an unquoted version or release.

    Returns:
        The value, or an empty collection of `kind` when absent or null.

    Raises:
        ValueError: The key holds a value of another type, or an element does.
    """
    value = data.get(key)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise ValueError(
            f"'{key}' in '{source}' must be a {kind.__name__}, got "
            f"{type(value).__name__}."
        )
    if element_kind is not None:
        for index, element in enumerate(value):
            if not isinstance(element, element_kind):
                raise ValueError(
                    f"Entry {index} of '{key}' in '{source}' must be "
                    f"{element_kind.__name__}, got {type(element).__name__}: "
                    f"{element!r}. Quote the value if it is meant to be text."
                )
    return value


def _require_envelope_mapping(
    data: dict[str, Any], key: str, source: Path | str, kind: str
) -> dict[str, Any]:
    """Read a K8s envelope container, rejecting a wrong type by name.

    `metadata` and `spec` are indexed immediately after they are read, so a
    scalar here surfaces as `'str' object has no attribute 'get'`, naming
    neither the file nor the key. Absent or null yields an empty mapping,
    which the callers already treat as "nothing supplied".

    Raises:
        ValueError: The key holds something other than a mapping.
    """
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"'{key}' in {kind.lower()} '{source}' must be a mapping, got "
            f"{type(value).__name__}."
        )
    return value


def _suggest_known_key(unknown: str, known: set[str]) -> str | None:
    """Return a 'did you mean X?' suggestion for a typo if there is a close match."""
    import difflib
    matches = difflib.get_close_matches(unknown, sorted(known), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _validate_known_keys(
    actual: dict,
    allowed: set[str],
    path: Path,
    context: str,
    kind: str = "Manifest",
    advice: dict[str, str] | None = None,
) -> None:
    """Reject any keys in `actual` that are not in `allowed`.

    Args:
        actual: The dict whose keys to validate.
        allowed: The closed set of permitted keys.
        path: Source file path, used in the error message.
        context: Where in the file this dict lives (e.g. "top-level",
            "spec", "metadata"), used to disambiguate the error.
        kind: Resource kind named in the error, so a site does not report
            itself as a manifest.
        advice: Per-key guidance that replaces the closest-spelling
            suggestion. A key that is real but sits in the wrong container
            has a correct answer, and offering the nearest spelling instead
            sends the reader to a different field that is also wrong.
    """
    unknown = sorted(set(actual.keys()) - allowed)
    if not unknown:
        return
    parts = []
    for key in unknown:
        hint = (advice or {}).get(key)
        if hint:
            parts.append(f"`{key}` ({hint})")
            continue
        suggestion = _suggest_known_key(key, allowed)
        if suggestion:
            parts.append(f"`{key}` (did you mean `{suggestion}`?)")
        else:
            parts.append(f"`{key}`")
    raise ValueError(
        f"{kind} '{path}' has unknown {context} key(s): {', '.join(parts)}. "
        f"Allowed: {sorted(allowed)}."
    )


class IncludeError(ValueError):
    """Raised when a manifest `include:` directive cannot be resolved.

    Subclass of ValueError so existing callers that catch ValueError still work.
    """


# Pattern for condition expressions in 'when' clauses
# Supports:
#   - Comparison: site.labels.<key> == 'value' or site.properties.<path> != 'value'
#   - Boolean shorthand: site.properties.<path> (truthy check)
# Values can be quoted strings ('value' or "value") or unquoted booleans (true/false)
CONDITION_PATTERN = re.compile(
    r"\{\{\s*site\.(labels\.[a-zA-Z0-9_-]+|properties\.[a-zA-Z0-9_.\[\]-]+)"
    r"(?:\s*(==|!=)\s*(?:['\"]([^'\"]*?)['\"]|(true|false)))?\s*\}\}"
)


@dataclass(frozen=True)
class AnyCondition:
    """A structured gate that passes when any atomic condition passes."""

    expressions: tuple[str, ...]


WhenCondition = str | AnyCondition


def parse_when_condition(
    when: WhenCondition | dict[str, Any] | None,
    context: str,
) -> WhenCondition | None:
    """Parse one atomic condition or the structured `any` form."""
    if when is None:
        return None
    if isinstance(when, AnyCondition):
        expressions = when.expressions
    elif isinstance(when, str):
        expression = when.strip()
        if not CONDITION_PATTERN.fullmatch(expression):
            raise ConditionSyntaxError(
                f"Invalid 'when' condition syntax on {context}: {when}. "
                "Expected: {{ site.labels.X == 'value' }}, "
                "{{ site.properties.path == true }}, "
                "{{ site.properties.path }} (truthy check), or "
                "a mapping with one `any` list."
            )
        return expression
    elif isinstance(when, dict):
        if set(when) != {"any"}:
            raise ConditionSyntaxError(
                f"Invalid structured 'when' condition on {context}: "
                f"expected only `any`, got "
                f"{sorted(str(key) for key in when)}."
            )
        raw_expressions = when.get("any")
        if not isinstance(raw_expressions, list) or not raw_expressions:
            raise ConditionSyntaxError(
                f"Invalid structured 'when' condition on {context}: "
                "`any` must be a non-empty list of condition strings."
            )
        expressions = tuple(raw_expressions)
    else:
        raise ConditionSyntaxError(
            f"Invalid 'when' condition on {context}: expected a string or "
            "a mapping with one `any` list."
        )

    parsed: list[str] = []
    for index, expression in enumerate(expressions):
        if not isinstance(expression, str) or not expression.strip():
            raise ConditionSyntaxError(
                f"Invalid structured 'when' condition on {context}: "
                f"`any[{index}]` must be a non-empty string."
            )
        parsed_expression = parse_when_condition(
            expression,
            f"{context} `any[{index}]`",
        )
        assert isinstance(parsed_expression, str)
        parsed.append(parsed_expression)
    return AnyCondition(tuple(parsed))


# Supported kubectl operations. Add provider operations such as `delete` here
# only when their execution and validation contracts are implemented.
KUBECTL_OPERATIONS = {"apply"}


def validate_condition_syntax(
    when: WhenCondition | dict[str, Any] | None,
    context: str,
) -> None:
    """Reject a `when:` expression the condition evaluator cannot parse.

    Condition evaluation fails OPEN, returning True for an expression it cannot
    parse, so an unvalidated typo turns a gate that should exclude into one that
    includes everywhere. Every path that assigns `when:` has to come through
    here, including an include's `when:` propagated onto a spliced step.

    Args:
        when: The expression, or None to skip.
        context: What carries the expression, for the error message.

    Raises:
        ValueError: If the expression does not match the supported grammar.
    """
    parse_when_condition(when, context)


def format_when_condition(when: WhenCondition | None) -> str:
    """Render a parsed condition for plans and skip diagnostics."""
    if isinstance(when, AnyCondition):
        return "any(" + ", ".join(when.expressions) + ")"
    return when or ""

# Supported wait-step condition types. The first is `arm-tag` (poll an Azure
# tag on an ARM resource). The dispatch shape accommodates future condition
# types (e.g. arm-resource-property, kubectl-resource-ready) without a manifest
# contract change.
VALID_WAIT_CONDITION_TYPES = {"arm-tag"}


class NoTargetingError(ValueError):
    """Raised when neither the manifest nor the CLI provides any targeting.

    Distinct from generic `ValueError` so callers can differentiate the
    "generic library manifest with no CLI selector" case from selector
    parse errors. `validate()` treats this as structurally OK and skips
    site-dependent checks. `cmd_deploy` surfaces it as a hard error.
    """


class SelectorParseError(ValueError):
    """Raised when a `-l/--selector` string fails to parse.

    Distinct from generic `ValueError` so `validate()` can attribute
    the failure to selector input (and skip the redundant no-match
    diagnostic) without substring-matching the error message.
    """


class MultipleSubscriptionSitesError(ValueError):
    """Raised when one subscription resolves more than one subscription-level site.

    Subscription-scoped steps run once per subscription and their outputs feed
    every resource-group site under it, so two candidates have no correct
    resolution. `validate()` reports this among its errors. `deploy` raises it,
    since `deploy` does not run `validate` and choosing one candidate would
    deploy the rest of the fleet against outputs from a site the operator did
    not name.
    """


class ParameterSelectionError(ValueError):
    """Raised for malformed, duplicated, legacy, or unresolved selections."""


class ConditionSyntaxError(ValueError):
    """Raised when a `when:` expression does not match the supported grammar.

    Manifest parsing rejects unsupported syntax before the evaluator's
    defensive fallback can run it. The distinct type lets callers separate an
    authored condition error from unrelated model validation.
    """


def parse_selector(selector: str | None) -> dict[str, list[str]]:
    """Parse a label selector string into key to value-list pairs.

    Within a single selector string, comma-separated `key=value` pairs are
    AND-combined across distinct keys. Duplicate keys follow these rules:

    - The special `name` key may repeat. Repeated values OR-combine and
      duplicates are deduped (preserving first-seen order).
    - Any non-name key may only appear once. Duplicate non-name keys
      raise `SelectorParseError`. This matches kubectl, Terraform, and
      Ansible label-selector grammars where AND across distinct keys is
      the rule.

    Args:
        selector: Comma-separated `key=value` pairs (e.g.,
            `environment=prod,region=eastus`), or None/empty for no
            filtering.

    Returns:
        Dict mapping each key to a list of allowed values. Non-name keys
        always map to a single-element list. The `name` key may map to
        multiple values (OR-combined). Empty dict if `selector` is None
        or empty.

    Raises:
        SelectorParseError: If a term is not in `key=value` form, or a
            non-name key appears more than once.

    Example:
        >>> parse_selector('environment=prod,region=eastus')
        {'environment': ['prod'], 'region': ['eastus']}
        >>> parse_selector('name=a,name=b,name=a')
        {'name': ['a', 'b']}
        >>> parse_selector(None)
        {}
    """
    if not selector:
        return {}

    labels: dict[str, list[str]] = {}
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SelectorParseError(
                f"Selector term `{part}` is not in `key=value` form. "
                f"Did you mean `name={part}`?"
            )
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise SelectorParseError(
                f"Selector term `{part}` has empty key. Use `key=value` form."
            )
        if not value:
            raise SelectorParseError(
                f"Selector key `{key}` has empty value. Use `{key}=<value>`."
            )
        if key in labels:
            if key != "name":
                raise SelectorParseError(
                    f"Selector key `{key}` may only appear once. Selectors "
                    f"AND across keys, so duplicating a key would always "
                    f"match zero sites. Only `name=` supports multiple "
                    f"values (OR-combined)."
                )
            if value not in labels[key]:
                labels[key].append(value)
        else:
            labels[key] = [value]
    return labels


def _merge_selector_strings(strings: list[str] | None) -> str | None:
    """Merge multiple selector strings into a single comma-separated string.

    Used by the CLI to flatten repeated `-l/--selector` flags into a single
    string before parsing. The grammar is associative under comma joining:
    `parse_selector(",".join(parts))` enforces the same name-OR /
    non-name-error rules across the merged input.
    """
    if not strings:
        return None
    merged = ",".join(s for s in strings if s)
    return merged or None


def _normalize_site_identifier(identifier: str) -> str:
    """Validate and normalize a site identifier or path-form identifier.

    Accepts:
    - Bare basename (`munich-dev`)
    - Forward-slash relative path (`regions/eu/munich-dev`)
    - Backslash relative path (normalized to forward slashes)

    Rejects (raises `ValueError`):
    - Empty string
    - Leading `./`
    - Leading `/` (absolute path)
    - Trailing `/`
    - `..` path segments (path traversal)
    - `.` path segments
    - Empty path segments (e.g., `a//b`)

    Returns the normalized form (forward-slash separators, no leading or
    trailing slash).
    """
    if not identifier:
        raise ValueError("Site identifier must not be empty")
    normalized = identifier.replace("\\", "/")
    if normalized.startswith("./"):
        raise ValueError(
            f"Site identifier '{identifier}' must not start with `./`. "
            f"Use the relative form (e.g., `regions/eu/munich`)."
        )
    if normalized.startswith("/"):
        raise ValueError(
            f"Site identifier '{identifier}' must be relative (no leading `/`)."
        )
    if normalized.endswith("/"):
        raise ValueError(
            f"Site identifier '{identifier}' must not end with `/`."
        )
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain `..` segments."
        )
    if any(p == "." for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain `.` segments."
        )
    if any(not p for p in parts):
        raise ValueError(
            f"Site identifier '{identifier}' must not contain empty path segments."
        )
    return normalized


def _validate_resource(data: dict[str, Any], expected_kind: str | list[str], path: Path) -> str:
    """Validate apiVersion and kind for a resource file.

    Args:
        data: Parsed YAML data
        expected_kind: The expected kind(s) (e.g., 'Site' or ['Site', 'SiteTemplate'])
        path: File path for error messages

    Returns:
        The validated apiVersion string

    Raises:
        ValueError: If kind doesn't match expected or apiVersion is unsupported

    Note:
        - apiVersion defaults to 'siteops/v1' if not specified
        - kind is only validated if present; if omitted, the resource type
          is determined by the calling context
    """
    api_version = data.get("apiVersion", DEFAULT_API_VERSION)
    kind = data.get("kind")

    # Normalize expected_kind to a list for consistent handling
    expected_kinds = [expected_kind] if isinstance(expected_kind, str) else list(expected_kind)

    if api_version not in SUPPORTED_API_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_API_VERSIONS))
        raise ValueError(f"Unsupported apiVersion '{api_version}' in {path}. Supported: {supported}")

    if kind is not None and kind not in expected_kinds:
        if len(expected_kinds) == 1:
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected '{expected_kinds[0]}'")
        else:
            expected_str = ", ".join(f"'{k}'" for k in expected_kinds)
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected one of: {expected_str}")

    return api_version


@dataclass(frozen=True)
class ParallelConfig:
    """Configuration for parallel site execution.

    Controls how many sites are deployed concurrently during manifest execution.

    Attributes:
        sites: Maximum concurrent sites.
            - 0 means unlimited (all sites run concurrently)
            - 1 means sequential (one site at a time)
            - N means at most N sites run concurrently

    Examples:
        >>> ParallelConfig.from_value(3)
        ParallelConfig(sites=3)
        >>> ParallelConfig.from_value(True)
        ParallelConfig(sites=0)
        >>> ParallelConfig.from_value({"sites": 2})
        ParallelConfig(sites=2)
    """

    sites: int = 1

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.sites < 0:
            raise ValueError(f"parallel.sites must be >= 0, got {self.sites}")

    @classmethod
    def from_value(cls, value: Any) -> "ParallelConfig":
        """Parse parallel config from a manifest value.

        Args:
            value: One of:
                - None: Returns default (sequential)
                - bool: True = unlimited, False = sequential
                - int: Max concurrent sites (0 = unlimited)
                - dict: Object form with 'sites' key

        Returns:
            Configured ParallelConfig instance

        Raises:
            ValueError: If value is invalid type or out of range

        Examples:
            parallel: 3           -> ParallelConfig(sites=3)
            parallel: 0           -> ParallelConfig(sites=0)  # unlimited
            parallel: true        -> ParallelConfig(sites=0)  # unlimited
            parallel: false       -> ParallelConfig(sites=1)  # sequential
            parallel:
              sites: 3            -> ParallelConfig(sites=3)
        """
        if value is None:
            return cls()

        if isinstance(value, bool):
            return cls(sites=0 if value else 1)

        if isinstance(value, int):
            return cls(sites=value)

        if isinstance(value, dict):
            sites = value.get("sites", 1)
            if not isinstance(sites, int):
                raise ValueError(f"parallel.sites must be an integer, got {type(sites).__name__}")
            return cls(sites=sites)

        raise ValueError(f"Invalid parallel value: expected bool, int, or dict, " f"got {type(value).__name__}")

    @property
    def is_sequential(self) -> bool:
        """Return True if deployment runs one site at a time."""
        return self.sites == 1

    @property
    def is_unlimited(self) -> bool:
        """Return True if all sites run concurrently."""
        return self.sites == 0

    @property
    def max_workers(self) -> int | None:
        """Return max workers for ThreadPoolExecutor, or None for unlimited."""
        return None if self.sites == 0 else self.sites

    def __str__(self) -> str:
        """Return human-readable description."""
        if self.is_unlimited:
            return "unlimited"
        if self.is_sequential:
            return "sequential"
        return f"max {self.sites}"


@dataclass
class Site:
    """Deployment target representing an Azure subscription/resource group.

    Attributes:
        name: Unique identifier for the site
        subscription: Azure subscription ID
        resource_group: Azure resource group name
        location: Azure region (e.g., 'eastus', 'westus2')
        labels: Key-value string pairs for filtering with selectors
        properties: Structured data for complex site-specific configuration
        parameters: Default parameters to include in all deployments to this site
    """

    name: str
    subscription: str
    resource_group: str
    location: str
    labels: dict[str, str] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Hold the optional mappings to the types this class declares."""
        _normalize_null_collections(self)

    def matches_selector(self, selector: dict[str, list[str]]) -> bool:
        """Check if site matches all selector criteria.

        Supports:
        - `name`: site name must be one of the listed values (OR-combined)
        - any other `<label>`: site label value must equal the single
          listed value

        Args:
            selector: Dict mapping each key to a list of allowed values.
                Non-name keys must map to a single-element list (enforced
                by `parse_selector`).

        Returns:
            True if all selector criteria match.
        """
        for key, values in selector.items():
            if key == "name":
                if self.name not in values:
                    return False
            else:
                # Non-name keys carry a single value (enforced upstream).
                # Use list containment so a malformed multi-value list still
                # produces deterministic match behavior.
                if self.labels.get(key) not in values:
                    return False
        return True

    @classmethod
    def from_file(cls, path: Path) -> "Site":
        """Load a site from a YAML file.

        Supports two formats:
        1. Flat format (recommended):
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            name: dev-eastus
            subscription: "..."
            resourceGroup: "..."
            location: eastus
            labels:
              environment: dev
            properties:
              deviceEndpoints:
                - host: 10.0.1.100
                  port: 4840
            ```

        2. K8s-style nested format:
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            metadata:
              name: dev-eastus
              labels:
                environment: dev
            spec:
              subscription: "..."
              resourceGroup: "..."
              location: eastus
              properties:
                deviceEndpoints:
                  - host: 10.0.1.100
                    port: 4840
            ```

        Args:
            path: Path to the YAML file

        Returns:
            Site instance

        Raises:
            ValueError: If file is empty, invalid, or missing required fields

        Note:
            This is a low-level loader. It does NOT apply `inherits:` chains
            or overlays from `sites.local/` / extras dirs. Use
            `Orchestrator.load_site(name)` for fully-resolved sites.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = yamlio.load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML file: {path}")

        _validate_resource(data, "Site", path)
        return cls.from_data(data, source=path, default_name=path.stem)

    @classmethod
    def from_data(
        cls,
        data: dict[str, Any],
        *,
        source: Path | str,
        default_name: str,
    ) -> "Site":
        """Build a site from already-parsed data in either shape.

        The single parse for both entry points. `from_file` reads one file and
        calls this. The orchestrator merges a file with its inherit chain and
        any overlay, then calls this. One implementation is what stops the two
        paths drifting, since a rule added to one of two copies holds for only
        one of them.

        Does not validate `apiVersion` and `kind`. Both callers do that before
        calling, against the source they want named in that error.

        Args:
            data: Site data, either the flat shape or the K8s envelope, after
                any inherit and overlay merge.
            source: Path or label naming the file to report in an error.
            default_name: Name to use when neither shape supplies one.

        Returns:
            Site instance.

        Raises:
            ValueError: A key is unknown, a required field is absent or empty,
                or an open container is not a mapping.
        """
        validate_site_keys(data, source)

        if "spec" in data:
            spec = data.get("spec") or {}
            metadata = data.get("metadata") or {}
            # `or default_name` rather than a `get` default, since a bare
            # `name:` supplies the key with a null value. That reached `Site`
            # as `None` and broke sorting and interpolation far from here.
            name = metadata.get("name") or default_name
            labels = metadata.get("labels")
        else:
            spec = data
            name = data.get("name") or default_name
            labels = data.get("labels")

        validate_site_required(spec, source)

        # `None` reaching any of these is normalized by `__post_init__`, which
        # holds them to the types this class declares.
        return cls(
            name=name,
            subscription=spec["subscription"],
            resource_group=spec.get("resourceGroup") or "",
            location=spec["location"],
            labels=labels,
            properties=spec.get("properties"),
            parameters=spec.get("parameters"),
        )

    @property
    def is_subscription_level(self) -> bool:
        """Check if this is a subscription-level site (no resource group).

        Subscription-level sites are used for deploying shared resources
        once per subscription (e.g., Azure Edge Sites). They have only
        subscription + location, no resourceGroup.

        Returns:
            True if site has no resource_group (subscription-level)
            False if site has a resource_group (RG-level)
        """
        return not self.resource_group

    def get_all_parameters(self) -> dict[str, Any]:
        """Get a copy of site-level parameters.

        Returns:
            Copy of the parameters dict (modifications won't affect the site)
        """
        return dict(self.parameters)

    def __repr__(self) -> str:
        return f"Site(name={self.name!r}, location={self.location!r})"


@dataclass
class DeploymentStep:
    """A single Bicep/ARM deployment step within a manifest.

    Attributes:
        name: Unique name for the step (used in deployment names and output references)
        template: Path to the Bicep/ARM template file (relative to workspace)
        parameters: List of parameter file paths (relative to workspace)
        scope: Deployment scope - 'resourceGroup' or 'subscription'
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")
    """

    name: str
    template: str
    parameters: list[str] = field(default_factory=list)
    scope: str = "resourceGroup"
    when: WhenCondition | dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _normalize_null_collections(self)

        if self.scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{self.scope}'. Must be one of: {VALID_SCOPES}")

        self.when = parse_when_condition(self.when, f"step '{self.name}'")


@dataclass
class ArcCluster:
    """Arc-connected Kubernetes cluster configuration.

    Attributes:
        name: Cluster name (supports template variables like {{ site.labels.clusterName }})
        resource_group: Resource group containing the cluster (supports template variables)
    """

    name: str
    resource_group: str


@dataclass
class KubectlStep:
    """A kubectl operation step within a manifest.

    Executes kubectl commands against an Arc-connected Kubernetes cluster.
    Site Ops automatically manages the `az connectedk8s proxy` lifecycle.

    Attributes:
        name: Unique name for the step
        operation: Kubectl operation ('apply' is currently supported)
        arc: Arc cluster configuration (name and resourceGroup)
        files: List of file paths (relative to workspace) or HTTPS URLs to apply
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")

    Example manifest usage:
        ```yaml
        - name: apply-config
          type: kubectl
          operation: apply
          arc:
            name: "{{ site.labels.clusterName }}"
            resourceGroup: "{{ site.resourceGroup }}"
          files:
            - https://example.com/manifest.yaml
            - configs/local-config.yaml
          when: "{{ site.labels.enableConfig == 'true' }}"
        ```
    """

    name: str
    operation: str
    arc: ArcCluster
    files: list[str] = field(default_factory=list)
    when: WhenCondition | dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _normalize_null_collections(self)

        if self.operation not in KUBECTL_OPERATIONS:
            raise ValueError(
                f"Invalid kubectl operation '{self.operation}'. " f"Supported: {', '.join(sorted(KUBECTL_OPERATIONS))}"
            )

        if not self.files:
            raise ValueError(f"KubectlStep '{self.name}' must specify at least one file")

        self.when = parse_when_condition(self.when, f"step '{self.name}'")


@dataclass(frozen=True)
class ArmTagCondition:
    """Wait condition that polls a tag on an ARM resource.

    The condition is satisfied when the resource's `tag_key` tag reaches
    `expected_value`. When `failure_pattern` is set, a tag value matching that
    glob aborts the wait immediately instead of waiting for the timeout.

    This is pure data. Evaluation (running `az` and classifying the result)
    lives in the executor.

    Attributes:
        type: Condition discriminator. Always `arm-tag` for this class.
        resource_id: Full ARM resource ID whose tags are polled. Supports
            template variables and step-output references, resolved per site.
        tag_key: Name of the tag to read.
        expected_value: Tag value that satisfies the wait. Azure tag values are
            strings, so this is compared as a string.
        failure_pattern: Optional fnmatch glob. A tag value matching it aborts
            the wait fast. Omit for a plain wait-until-expected-or-timeout.
    """

    type: str
    resource_id: str
    tag_key: str
    expected_value: str
    failure_pattern: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("resourceId", self.resource_id),
            ("tagKey", self.tag_key),
            ("expectedValue", self.expected_value),
        ):
            if not value or not str(value).strip():
                raise ValueError(f"arm-tag condition requires a non-empty '{field_name}'")

        # A failure glob that also matches the success value would classify a
        # satisfied wait as failed. Catch the authoring error at parse time.
        if self.failure_pattern and fnmatchcase(str(self.expected_value), self.failure_pattern):
            raise ValueError(
                f"arm-tag condition expectedValue '{self.expected_value}' also matches "
                f"failurePattern '{self.failure_pattern}'. The success value must not match the "
                f"failure glob."
            )


# Union type for wait conditions. One concrete type today. Future condition
# types join this union.
WaitCondition = ArmTagCondition


@dataclass
class WaitStep:
    """A wait step that gates downstream steps on an Azure condition.

    Blocks the per-site step sequence until `condition` is satisfied, polling
    every `poll_interval_seconds` up to `timeout_minutes`. A gate produces no
    outputs. A timeout or a terminal failure fails the step, which skips the
    site's remaining steps (the standard step-failure behavior).

    Attributes:
        name: Unique name for the step.
        condition: The wait condition (currently ArmTagCondition).
        timeout_minutes: Maximum minutes to wait before failing the step.
        poll_interval_seconds: Seconds between condition checks.
        when: Optional condition expression (same syntax as other steps).

    Example manifest usage:
        ```yaml
        - name: wait-for-bootstrap
          type: wait
          condition:
            type: arm-tag
            resourceId: "/subscriptions/{{ site.subscription }}/resourceGroups/{{ site.resourceGroup }}/providers/Microsoft.HybridCompute/machines/{{ site.parameters.aksee.machineName }}"
            tagKey: "siteops.bootstrap.state"
            expectedValue: "succeeded"
            failurePattern: "failed-*"
          timeoutMinutes: 45
          pollIntervalSeconds: 30
        ```
    """

    name: str
    condition: WaitCondition
    timeout_minutes: int = 30
    poll_interval_seconds: int = 30
    when: WhenCondition | dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.timeout_minutes <= 0:
            raise ValueError(
                f"WaitStep '{self.name}' timeoutMinutes must be positive, got {self.timeout_minutes}"
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"WaitStep '{self.name}' pollIntervalSeconds must be positive, got {self.poll_interval_seconds}"
            )
        if self.poll_interval_seconds > self.timeout_minutes * 60:
            raise ValueError(
                f"WaitStep '{self.name}' pollIntervalSeconds ({self.poll_interval_seconds}) exceeds "
                f"timeoutMinutes ({self.timeout_minutes} = {self.timeout_minutes * 60}s). The condition "
                f"would be checked only once."
            )

        self.when = parse_when_condition(self.when, f"step '{self.name}'")


# Union type for manifest steps - allows type checking to distinguish step types
ManifestStep = DeploymentStep | KubectlStep | WaitStep


@dataclass(frozen=True)
class ParameterSource:
    """A manifest parameter file, optionally expanded over a site list."""

    path: str
    for_each: str | None = None
    collections: tuple[str, ...] = ()
    declared_in: Path | None = field(default=None, compare=False, repr=False)


ManifestParameter = str | ParameterSource


@dataclass
class Manifest:
    """Deployment manifest that orchestrates templates across sites.

    A manifest defines:
    - Which sites to deploy to (explicit list or label selector)
    - What steps to execute (Bicep/ARM deployments, kubectl operations, or waits)
    - The order of deployment (steps execute sequentially per site)
    - Whether to deploy to sites in parallel
    - Shared parameters applied to all steps (with auto-filtering)

    Attributes:
        name: Unique identifier for the manifest
        description: Human-readable description
        sites: Explicit list of site names to deploy to
        steps: Ordered list of deployment, kubectl, and wait steps
        site_selector: Label selector string (e.g., 'environment=prod')
        parallel: Parallelization config (int, bool, or object with 'sites' key)
        parameters: Manifest-level parameter sources, each a path string or a
            typed source with `path`, `forEach`, and `collections`
        parameter_compositions: Workspace contracts governing collection
            identity and reference validation

    Parallel Configuration:
        - parallel: 0           # Unlimited concurrency (all sites at once)
        - parallel: 1           # Sequential (one site at a time, default)
        - parallel: 3           # Max 3 sites concurrently
        - parallel: true        # Unlimited concurrency
        - parallel: false       # Sequential
        - parallel:
            sites: 3            # Object form, max 3 sites concurrently
    """

    name: str
    description: str
    sites: list[str]
    steps: list[ManifestStep]
    site_selector: str | None = None
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    parameters: list[ManifestParameter] = field(default_factory=list)
    parameter_compositions: list[str] = field(default_factory=list)
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Hold the optional fields to the types this class declares."""
        _normalize_null_collections(self)

    @classmethod
    def from_file(cls, path: Path, *, workspace_root: Path) -> "Manifest":
        """Load a manifest from a YAML file.

        Resolves any `- include: <path>` steps recursively, splicing the
        included manifests' steps into this one's step list at the include's
        position. See docs/manifest-includes.md for the full include contract.

        Example manifest:
            ```yaml
            apiVersion: siteops/v1
            kind: Manifest
            name: iot-operations
            description: Deploy Azure IoT Operations
            parallel: 2  # Max 2 sites concurrently

            sites:
              - dev-eastus

            steps:
              - name: aio-enablement
                template: templates/enablement.bicep
                scope: subscription
                parameters:
                  - parameters/enablement.yaml

              - name: configure-cluster
                type: kubectl
                operation: apply
                arc:
                  name: "{{ site.labels.clusterName }}"
                  resourceGroup: "{{ site.resourceGroup }}"
                files:
                  - https://example.com/config.yaml
                when: "{{ site.labels.enableConfig == 'true' }}"
            ```

        Args:
            path: Path to the YAML file.
            workspace_root: Workspace root directory. Required, keyword-only.
                Used as the anti-traversal boundary when resolving any
                `include:` step paths and to scope all workspace-relative
                references. In production this is `Orchestrator.workspace`;
                in tests, pass the workspace fixture (or `manifest_path.parent`
                for a self-contained synthetic manifest).

        Returns:
            Manifest instance with all includes resolved into a flat step list.

        Raises:
            ValueError: If file is empty, invalid, or steps are misconfigured.
            IncludeError: If an include cycles, exceeds depth, escapes the
                workspace root, names a missing or non-Manifest file,
                conflicts with a step's own `when:`, or contributes zero steps.
        """
        path = Path(path)
        root = Path(workspace_root)

        spec, name, description = _read_manifest_spec(path)

        sites = []
        for item in _require_collection(spec, "sites", path, list, str):
            try:
                sites.append(_normalize_site_identifier(item))
            except ValueError as e:
                raise ValueError(
                    f"Invalid site identifier in `{path}` `sites:` list: {e}"
                ) from e

        # `selector:` is the preferred manifest field. `siteSelector:` is
        # accepted for backward compatibility but logs a one-time deprecation
        # notice per file. Both refer to the same label expression.
        if "selector" in spec and "siteSelector" in spec:
            raise ValueError(
                f"Manifest '{path}' declares both `selector:` and "
                f"`siteSelector:`. Use `selector:` only."
            )
        if "siteSelector" in spec:
            import logging as _logging
            _logging.getLogger("siteops.models").warning(
                "%s uses deprecated `siteSelector:`. Rename to `selector:`.",
                path,
            )
            site_selector = spec.get("siteSelector")
        else:
            site_selector = spec.get("selector")
        parallel = ParallelConfig.from_value(spec.get("parallel"))

        # Recursive include resolution. The recursion stack tracks the current
        # DFS path so a fragment shared by two siblings is not flagged as a
        # cycle. The include chain captures the full provenance for diagnostics.
        steps, parameters, parameter_compositions = _resolve_steps_and_params(
            spec=spec,
            manifest_path=path,
            workspace_root=root.resolve(),
            recursion_stack=[path.resolve()],
            include_chain=[path],
            depth=0,
        )

        _validate_no_step_name_collisions(steps)

        return cls(
            name=name,
            description=description,
            sites=sites,
            steps=steps,
            site_selector=site_selector,
            parallel=parallel,
            parameters=parameters,
            parameter_compositions=parameter_compositions,
            source_path=path.resolve(),
        )

    def resolve_parameter_path(self, param_path: str, site: "Site") -> str:
        """Resolve template variables in a parameter file path.

        Supports:
        - {{ site.name }} - Site name
        - {{ site.location }} - Site location
        - {{ site.resourceGroup }} - Site resource group
        - {{ site.subscription }} - Site subscription
        - {{ site.labels.<key> }} - Site label value
        - {{ site.properties.<path> }} - Site property value (nested paths supported)

        Args:
            param_path: Parameter file path with optional template variables
            site: Site to resolve variables from

        Returns:
            Resolved path string
        """
        result = param_path
        result = result.replace("{{ site.name }}", site.name)
        result = result.replace("{{ site.location }}", site.location)
        result = result.replace("{{ site.resourceGroup }}", site.resource_group)
        result = result.replace("{{ site.subscription }}", site.subscription)

        for key, value in site.labels.items():
            result = result.replace(f"{{{{ site.labels.{key} }}}}", value)

        # Resolve {{ site.properties.<path> }} templates
        for match in re.finditer(r"\{\{\s*site\.properties\.(\S+?)\s*\}\}", result):
            prop_path = match.group(1)
            value = site.properties
            for part in prop_path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    value = None
                    break
            if value is not None:
                if isinstance(value, list):
                    item_path = result.replace(match.group(0), "{{ item }}")
                    raise ParameterSelectionError(
                        f"Parameter path '{param_path}' reads "
                        f"site.properties.{prop_path} as an ordered list. "
                        "Replace the scalar source with an object using "
                        f"path: {item_path!r} and forEach: "
                        f"'{{{{ site.properties.{prop_path} }}}}'."
                    )
                result = result.replace(match.group(0), str(value))

        return result


# ---------------------------------------------------------------------------
# Manifest loading helpers (include resolution, step parsing)
# ---------------------------------------------------------------------------


def _read_manifest_spec(path: Path) -> tuple[dict[str, Any], str, str]:
    """Read a manifest YAML file and return (spec, name, description).

    Validates apiVersion + kind, rejects unknown top-level keys with a
    "did you mean?" hint, and unwraps the K8s-style `spec:` envelope when
    present. Raises ValueError on empty files, wrong kind, or unknown
    top-level keys.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yamlio.load(f)

    if not data:
        raise ValueError(f"Empty or invalid YAML file: {path}")

    _validate_resource(data, "Manifest", path)

    if "spec" in data:
        _validate_known_keys(data, _MANIFEST_NESTED_TOP_KEYS, path, "top-level")
        metadata = _require_envelope_mapping(data, "metadata", path, "Manifest")
        if metadata:
            _validate_known_keys(metadata, _MANIFEST_NESTED_METADATA_KEYS, path, "metadata")
        spec = _require_envelope_mapping(data, "spec", path, "Manifest")
        _validate_known_keys(spec, _MANIFEST_NESTED_SPEC_KEYS, path, "spec")
        name = metadata.get("name", path.stem)
        description = metadata.get("description", "")
    else:
        _validate_known_keys(data, _MANIFEST_FLAT_KNOWN_KEYS, path, "top-level")
        spec = data
        name = data.get("name", path.stem)
        description = data.get("description", "")

    return spec, name, description


def _is_include_step(step_data: dict[str, Any]) -> bool:
    return isinstance(step_data, dict) and "include" in step_data


def _format_include_chain(chain: list[Path]) -> str:
    return " -> ".join(str(p) for p in chain)


def _validate_include_step(step_data: dict[str, Any], parent_path: Path, index: int) -> str:
    """Validate an `include:` step shape and return the path string."""
    extra = set(step_data.keys()) - _INCLUDE_ALLOWED_KEYS
    if extra:
        raise IncludeError(
            f"Step {index + 1} in '{parent_path}' has unexpected keys alongside "
            f"`include:`: {sorted(extra)}. Only `include` and `when` are allowed."
        )
    target = step_data.get("include")
    if not isinstance(target, str) or not target.strip():
        raise IncludeError(
            f"Step {index + 1} in '{parent_path}' must provide a non-empty "
            f"string path for `include`."
        )
    return target


def _resolve_include_path(raw: str, parent_path: Path, workspace_root: Path) -> Path:
    """Resolve a relative include path under the workspace root.

    The resolved absolute path must be a descendant of workspace_root.
    Site-driven (Mustache) include paths are not supported in v1 and will
    fail the workspace-root check or the file-exists check.
    """
    candidate = (parent_path.parent / raw).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        raise IncludeError(
            f"Include path '{raw}' in '{parent_path}' resolves outside the "
            f"workspace root '{workspace_root}'."
        ) from None
    if not candidate.exists():
        raise IncludeError(
            f"Include path '{raw}' in '{parent_path}' does not exist "
            f"(resolved to '{candidate}')."
        )
    return candidate


def _propagate_when(
    step: "ManifestStep",
    include_when: WhenCondition | dict[str, Any] | None,
    source: Path,
) -> None:
    """Apply an include's `when:` to a spliced step.

    Raises IncludeError if the step already has its own `when:`. Combining
    two expressions is not supported in v1.
    """
    if include_when is None:
        return
    if step.when:
        raise IncludeError(
            f"Step '{step.name}' from '{source}' already has a `when:` "
            f"and the parent include also sets one. Consolidate into a "
            f"single condition on either the include or the step."
        )
    # Assignment here bypasses the step's own __post_init__ validation, so
    # validate explicitly. An include's gate is the same contract as a step's.
    step.when = parse_when_condition(
        include_when,
        f"include of '{source.name}'",
    )


def _parse_inline_step(step_data: dict[str, Any], source_path: Path, index: int) -> "ManifestStep":
    """Parse one deployment, kubectl, or wait step."""
    if "name" not in step_data:
        raise ValueError(f"Step {index + 1} missing required field 'name' in manifest: {source_path}")

    step_type = step_data.get("type", "deployment")

    if step_type == "kubectl":
        if "operation" not in step_data:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'operation' in manifest: {source_path}"
            )
        if "arc" not in step_data:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'arc' configuration in manifest: {source_path}"
            )
        arc_data = step_data["arc"]
        if "name" not in arc_data or "resourceGroup" not in arc_data:
            raise ValueError(
                f"Step '{step_data['name']}' arc config must have 'name' and 'resourceGroup': {source_path}"
            )
        if "files" not in step_data or not step_data["files"]:
            raise ValueError(
                f"Step '{step_data['name']}' (type: kubectl) missing 'files' in manifest: {source_path}"
            )
        return KubectlStep(
            name=step_data["name"],
            operation=step_data["operation"],
            arc=ArcCluster(
                name=arc_data["name"],
                resource_group=arc_data["resourceGroup"],
            ),
            files=_require_collection(step_data, "files", source_path, list, str),
            when=step_data.get("when"),
        )

    if step_type == "wait":
        return _parse_wait_step(step_data, source_path)

    if "template" not in step_data:
        raise ValueError(f"Step '{step_data['name']}' missing 'template' in manifest: {source_path}")
    return DeploymentStep(
        name=step_data["name"],
        template=step_data["template"],
        parameters=_require_collection(step_data, "parameters", source_path, list, str),
        scope=step_data.get("scope", "resourceGroup"),
        when=step_data.get("when"),
    )


def _coerce_step_int(value: Any, field_name: str, step_name: str, source_path: Path) -> int:
    """Coerce a manifest numeric field to int with a clear error on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Step '{step_name}' (type: wait) field '{field_name}' must be an integer, got {value!r} "
            f"in manifest: {source_path}"
        )


def _parse_wait_step(step_data: dict[str, Any], source_path: Path) -> "WaitStep":
    """Parse a `type: wait` step into a WaitStep with its condition.

    Dispatches on `condition.type`. Unknown condition types fail here so the
    error surfaces at manifest-load time rather than at deploy time.
    """
    name = step_data["name"]
    condition_data = step_data.get("condition")
    if not isinstance(condition_data, dict):
        raise ValueError(
            f"Step '{name}' (type: wait) requires a 'condition' mapping in manifest: {source_path}"
        )

    condition_type = condition_data.get("type")
    if not condition_type:
        raise ValueError(
            f"Step '{name}' (type: wait) condition requires a 'type' field in manifest: {source_path}"
        )
    if condition_type not in VALID_WAIT_CONDITION_TYPES:
        raise ValueError(
            f"Step '{name}' (type: wait) has unknown condition type '{condition_type}'. "
            f"Supported: {', '.join(sorted(VALID_WAIT_CONDITION_TYPES))} (manifest: {source_path})"
        )

    condition = _parse_arm_tag_condition(name, condition_data, source_path)
    return WaitStep(
        name=name,
        condition=condition,
        timeout_minutes=_coerce_step_int(
            step_data.get("timeoutMinutes", 30), "timeoutMinutes", name, source_path
        ),
        poll_interval_seconds=_coerce_step_int(
            step_data.get("pollIntervalSeconds", 30), "pollIntervalSeconds", name, source_path
        ),
        when=step_data.get("when"),
    )


def _parse_arm_tag_condition(step_name: str, condition_data: dict[str, Any], source_path: Path) -> "ArmTagCondition":
    """Parse the body of an arm-tag wait condition."""
    for required in ("resourceId", "tagKey", "expectedValue"):
        if required not in condition_data:
            raise ValueError(
                f"Step '{step_name}' arm-tag condition missing '{required}' in manifest: {source_path}"
            )

    failure_pattern = condition_data.get("failurePattern")
    return ArmTagCondition(
        type="arm-tag",
        resource_id=condition_data["resourceId"],
        tag_key=condition_data["tagKey"],
        # Azure tag values are strings. YAML may parse `expectedValue: true` to a
        # bool, so coerce to str for a consistent comparison.
        expected_value=str(condition_data["expectedValue"]),
        failure_pattern=str(failure_pattern) if failure_pattern is not None else None,
    )


def _parse_parameter_source(
    value: Any,
    manifest_path: Path,
    index: int,
) -> ManifestParameter:
    """Parse one string or object entry from manifest `parameters`."""
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(
                f"Manifest '{manifest_path}' parameters[{index}] must be a "
                "non-empty path string."
            )
        return value

    if not isinstance(value, dict):
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}] must be a path "
            "string or a mapping with `path`."
        )

    extra = sorted(set(value) - _PARAMETER_SOURCE_KEYS)
    if extra:
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}] has unknown "
            f"key(s): {extra}. Allowed: {sorted(_PARAMETER_SOURCE_KEYS)}."
        )

    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}].path must be a "
            "non-empty string."
        )

    for_each = value.get("forEach")
    if for_each is not None and (
        not isinstance(for_each, str) or not for_each.strip()
    ):
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}].forEach must be "
            "a non-empty string."
        )
    if for_each is not None and "{{ item }}" not in path:
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}] declares "
            "`forEach` but its path contains no `{{ item }}` placeholder."
        )
    if for_each is None and "{{ item }}" in path:
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}].path contains "
            "`{{ item }}` but declares no `forEach` expression."
        )

    raw_collections = value.get("collections", [])
    if not isinstance(raw_collections, list):
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}].collections "
            "must be a list of names."
        )
    collections: list[str] = []
    for collection_index, collection in enumerate(raw_collections):
        if not isinstance(collection, str) or not collection.strip():
            raise ValueError(
                f"Manifest '{manifest_path}' "
                f"parameters[{index}].collections[{collection_index}] must "
                "be a non-empty string."
            )
        name = collection.strip()
        if name in collections:
            raise ValueError(
                f"Manifest '{manifest_path}' parameters[{index}].collections "
                f"contains duplicate name '{name}'."
            )
        collections.append(name)

    if collections and for_each is None and "{{" in path:
        raise ValueError(
            f"Manifest '{manifest_path}' parameters[{index}] selects governed "
            "collections through a dynamic path without `forEach`. Use a fixed "
            "path or explicit `path` plus `forEach` expansion."
        )

    return ParameterSource(
        path=path.strip(),
        for_each=for_each.strip() if for_each is not None else None,
        collections=tuple(collections),
        declared_in=manifest_path,
    )


def _parameter_source_key(source: ManifestParameter) -> tuple[str, str | None]:
    if isinstance(source, str):
        return Path(source).as_posix(), None
    return Path(source.path).as_posix(), source.for_each


def _parameter_source_collections(source: ManifestParameter) -> tuple[str, ...]:
    return source.collections if isinstance(source, ParameterSource) else ()


def _merge_parameters(
    parent: list[ManifestParameter],
    fragment: list[ManifestParameter],
) -> list[ManifestParameter]:
    """Append fragment parameters after the parent's parameter sources.

    Sources deduplicate by normalized path and `forEach`, preserving the
    parent's position. A repeated source with different `collections` metadata
    is rejected. Different files still load in order, so the fragment's value
    wins for an ordinary parameter key both files set.
    """
    seen = {_parameter_source_key(source): source for source in parent}
    merged = list(parent)
    for source in fragment:
        key = _parameter_source_key(source)
        if key not in seen:
            merged.append(source)
            seen[key] = source
            continue
        if _parameter_source_collections(seen[key]) != _parameter_source_collections(
            source
        ):
            raise IncludeError(
                "Manifest parameter source "
                f"{key[0]!r} is declared more than once with different "
                "`collections` metadata."
            )
    return merged


def _parse_parameter_sources(
    spec: dict[str, Any],
    manifest_path: Path,
) -> list[ManifestParameter]:
    raw = _require_collection(spec, "parameters", manifest_path, list)
    return [
        _parse_parameter_source(value, manifest_path, index)
        for index, value in enumerate(raw)
    ]


def _parse_parameter_compositions(
    spec: dict[str, Any],
    manifest_path: Path,
    workspace_root: Path,
) -> list[str]:
    raw_paths = _require_collection(
        spec,
        "parameterCompositions",
        manifest_path,
        list,
        str,
    )
    result: list[str] = []
    for index, raw in enumerate(raw_paths):
        if not raw.strip():
            raise ValueError(
                f"Manifest '{manifest_path}' "
                f"parameterCompositions[{index}] must be a non-empty path."
            )
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"Manifest '{manifest_path}' parameter composition path "
                f"'{raw}' must be relative and contain no '..' segments."
            )
        resolved = (workspace_root / path).resolve()
        try:
            relative = resolved.relative_to(workspace_root)
        except ValueError:
            raise ValueError(
                f"Manifest '{manifest_path}' parameter composition path "
                f"'{raw}' resolves outside the workspace."
            ) from None
        if not resolved.is_file():
            raise ValueError(
                f"Manifest '{manifest_path}' parameter composition path "
                f"'{raw}' does not exist."
            )
        normalized = relative.as_posix()
        if normalized not in result:
            result.append(normalized)
    return result


def _resolve_steps_and_params(
    spec: dict[str, Any],
    manifest_path: Path,
    workspace_root: Path,
    recursion_stack: list[Path],
    include_chain: list[Path],
    depth: int,
) -> tuple[list["ManifestStep"], list[ManifestParameter], list[str]]:
    """Resolve includes into flat steps, parameters, and composition contracts.

    Args:
        spec: The current manifest's parsed `spec` dict (i.e., the body
            holding `steps:` and `parameters:`).
        manifest_path: Path of the manifest whose spec is being processed.
        workspace_root: Resolved absolute workspace root for traversal checks.
        recursion_stack: Resolved paths of manifests on the current DFS path.
            Used for cycle detection (NOT a global visited set).
        include_chain: Human-readable include chain for diagnostics.
        depth: Current recursion depth, capped by MAX_INCLUDE_DEPTH.
    """
    if depth > MAX_INCLUDE_DEPTH:
        raise IncludeError(
            f"Include depth exceeded {MAX_INCLUDE_DEPTH} levels at "
            f"{_format_include_chain(include_chain)}."
        )

    steps: list[ManifestStep] = []
    parameters = _parse_parameter_sources(spec, manifest_path)
    parameter_compositions = _parse_parameter_compositions(
        spec,
        manifest_path,
        workspace_root,
    )

    raw_steps = _require_collection(spec, "steps", manifest_path, list)
    for index, step_data in enumerate(raw_steps):
        if not isinstance(step_data, dict):
            raise ValueError(
                f"Step {index + 1} in '{manifest_path}' is not a mapping."
            )

        if not _is_include_step(step_data):
            steps.append(_parse_inline_step(step_data, manifest_path, index))
            continue

        raw_target = _validate_include_step(step_data, manifest_path, index)
        include_when = step_data.get("when")

        target_path = _resolve_include_path(raw_target, manifest_path, workspace_root)

        if target_path in recursion_stack:
            cycle = include_chain + [target_path]
            raise IncludeError(
                f"Include cycle detected: {_format_include_chain(cycle)}."
            )

        try:
            sub_spec, _, _ = _read_manifest_spec(target_path)
        except ValueError as exc:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' could not be loaded as a Manifest: {exc}"
            ) from exc

        sub_steps, sub_params, sub_compositions = _resolve_steps_and_params(
            spec=sub_spec,
            manifest_path=target_path,
            workspace_root=workspace_root,
            recursion_stack=recursion_stack + [target_path],
            include_chain=include_chain + [target_path],
            depth=depth + 1,
        )

        # Manifest-level parameters merge unconditionally into every parent
        # step. A gated include that contributes parameters would silently
        # affect ungated parent steps. Check sub_params (post-recursion) so a
        # fragment that only includes another fragment with parameters is
        # still caught.
        if include_when and sub_params:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' has a `when:` "
                f"but its include subtree contributes manifest-level "
                f"`parameters:`. Drop the `when:` or move the parameters onto "
                f"individual fragment steps."
            )

        if not sub_steps:
            raise IncludeError(
                f"Include '{raw_target}' in '{manifest_path}' contributed "
                f"zero steps. An include must define at least one step."
            )

        for sub_step in sub_steps:
            _propagate_when(sub_step, include_when, target_path)

        steps.extend(sub_steps)
        parameters = _merge_parameters(parameters, sub_params)
        for composition in sub_compositions:
            if composition not in parameter_compositions:
                parameter_compositions.append(composition)

    return steps, parameters, parameter_compositions


def _validate_no_step_name_collisions(steps: list["ManifestStep"]) -> None:
    """Reject duplicate step names in the post-flatten step list."""
    seen: set[str] = set()
    for step in steps:
        if step.name in seen:
            raise ValueError(
                f"Duplicate step name '{step.name}' after include flattening. "
                f"Step names must be unique across the entire flattened "
                f"pipeline (parent steps and all included fragments)."
            )
        seen.add(step.name)

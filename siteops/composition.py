# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Content-agnostic parameter composition and reference validation."""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siteops import yamlio
from siteops.sanitize import is_redaction_enabled


class CompositionError(ValueError):
    """Raised when parameter sources cannot form one valid composition."""


def report_composition_error(error: Exception) -> str:
    """Keep resource identities and source paths out of published diagnostics."""
    if is_redaction_enabled():
        return (
            "Resource composition failed. Re-run locally with output "
            "redaction disabled for source and identity details."
        )
    return str(error)


@dataclass(frozen=True)
class IdentityField:
    name: str
    path: str
    default: str | None = None


@dataclass(frozen=True)
class MemberSpec:
    name: str
    path: str
    shape: str


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    path: str
    identity: tuple[IdentityField, ...]
    seeds: tuple[tuple[str, ...], ...]
    members: dict[str, MemberSpec]


@dataclass(frozen=True)
class BindingSpec:
    name: str
    path: str
    default: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class ReferenceSource:
    collection: str
    select: str | None
    bindings: tuple[BindingSpec, ...]


@dataclass(frozen=True)
class MemberTarget:
    name: str
    match: dict[str, str]


@dataclass(frozen=True)
class ReferenceTarget:
    collection: str
    match: dict[str, str]
    member: MemberTarget | None = None


@dataclass(frozen=True)
class ReferenceRule:
    id: str
    source: ReferenceSource
    target: ReferenceTarget | None
    unverified: str | None


@dataclass(frozen=True)
class CompositionContract:
    collections: dict[str, CollectionSpec]
    references: tuple[ReferenceRule, ...]


@dataclass(frozen=True)
class LoadedParameterSource:
    path: Path
    data: dict[str, Any]
    collections: tuple[str, ...]


@dataclass(frozen=True)
class SelectedSource:
    path: Path
    collections: tuple[str, ...]


@dataclass(frozen=True)
class ComposedEntry:
    identity: tuple[str, ...]
    value: dict[str, Any]
    source: Path


@dataclass(frozen=True)
class ExternalEntry:
    identity: tuple[str, ...]
    expects: dict[str, Any]
    reason: str
    source: Path


@dataclass(frozen=True)
class Requirement:
    collection: str
    identity: tuple[str, ...]
    source: Path


@dataclass(frozen=True)
class ReferenceResult:
    rule_id: str
    source_collection: str
    source_identity: tuple[str, ...]
    source_path: Path
    target_collection: str | None
    target_identity: tuple[str, ...] | None
    target_source: Path | None
    target_member_name: str | None = None
    target_member_identity: str | None = None
    external: bool = False
    unverified_reason: str | None = None


@dataclass(frozen=True)
class CompositionResult:
    parameters: dict[str, Any]
    sources: tuple[SelectedSource, ...]
    entries: dict[str, tuple[ComposedEntry, ...]]
    external: dict[str, tuple[ExternalEntry, ...]]
    requirements: tuple[Requirement, ...]
    references: tuple[ReferenceResult, ...]


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompositionError(f"{label} must be a mapping.")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CompositionError(f"{label} must be a list.")
    return value


def _known_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise CompositionError(
            f"{label} has unknown key(s): {unknown}. "
            f"Allowed: {sorted(allowed)}."
        )


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompositionError(f"{label} must be a non-empty string.")
    return value.strip()


def _read_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    if not path:
        return True, current
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _select(value: Any, path: str | None, label: str) -> list[Any]:
    if path is None:
        return [value]
    current = [value]
    for segment in path.split("."):
        wildcard = segment.endswith("[*]")
        key = segment[:-3] if wildcard else segment
        selected: list[Any] = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                raise CompositionError(
                    f"{label} requires selector path '{path}'."
                )
            child = item[key]
            if wildcard:
                if not isinstance(child, list):
                    raise CompositionError(
                        f"Reference selector '{path}' expected '{key}' to be "
                        "a list."
                    )
                selected.extend(child)
            else:
                selected.append(child)
        current = selected
    return current


def _identity(
    spec: CollectionSpec,
    entry: dict[str, Any],
    label: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for field in spec.identity:
        found, value = _read_path(entry, field.path)
        if not found:
            value = field.default
        if not isinstance(value, str) or not value.strip():
            raise CompositionError(
                f"{label} identity field '{field.path}' must resolve to a "
                "non-empty string."
            )
        if value != value.strip():
            raise CompositionError(
                f"{label} identity field '{field.path}' must not contain "
                "leading or trailing whitespace."
            )
        resolved = value
        if "{{" in resolved or "}}" in resolved:
            raise CompositionError(
                f"{label} identity field '{field.path}' is unresolved: "
                f"{resolved!r}."
            )
        values.append(resolved)
    return tuple(values)


def _identity_root_keys(spec: CollectionSpec) -> set[str]:
    return {field.path.split(".", 1)[0] for field in spec.identity}


def _identity_label(
    spec: CollectionSpec,
    identity: tuple[str, ...],
) -> str:
    fields = ", ".join(
        f"{field.path}={value!r}"
        for field, value in zip(spec.identity, identity, strict=True)
    )
    return f"{spec.name}[{fields}]"


def format_identity(
    spec: CollectionSpec,
    identity: tuple[str, ...],
) -> str:
    """Format an identity with the declaration field names operators write."""
    return _identity_label(spec, identity)


def resolve_identity(
    spec: CollectionSpec,
    entry: dict[str, Any],
    label: str,
) -> tuple[str, ...]:
    """Resolve one entry identity using the contract's declared fields."""
    return _identity(spec, entry, label)


def _parse_identity(
    value: Any,
    label: str,
) -> tuple[IdentityField, ...]:
    mapping = _require_mapping(value, label)
    if not mapping:
        raise CompositionError(f"{label} must declare at least one field.")
    result: list[IdentityField] = []
    for name, raw in mapping.items():
        component = _non_empty_string(name, f"{label} component")
        if isinstance(raw, str):
            result.append(
                IdentityField(
                    name=component,
                    path=_non_empty_string(raw, f"{label}.{component}"),
                )
            )
            continue
        body = _require_mapping(raw, f"{label}.{component}")
        _known_keys(body, {"path", "default"}, f"{label}.{component}")
        path = _non_empty_string(
            body.get("path"),
            f"{label}.{component}.path",
        )
        default = body.get("default")
        if default is not None:
            default = _non_empty_string(
                default,
                f"{label}.{component}.default",
            )
        result.append(
            IdentityField(
                name=component,
                path=path,
                default=default,
            )
        )
    return tuple(result)


def _parse_members(
    value: Any,
    label: str,
) -> dict[str, MemberSpec]:
    mapping = _require_mapping(value, label)
    result: dict[str, MemberSpec] = {}
    for name, raw in mapping.items():
        member_name = _non_empty_string(name, f"{label} member")
        body = _require_mapping(raw, f"{label}.{member_name}")
        _known_keys(body, {"path", "shape"}, f"{label}.{member_name}")
        shape = _non_empty_string(
            body.get("shape"),
            f"{label}.{member_name}.shape",
        )
        if shape != "map":
            raise CompositionError(
                f"{label}.{member_name}.shape must be 'map' in this version."
            )
        result[member_name] = MemberSpec(
            name=member_name,
            path=_non_empty_string(
                body.get("path"),
                f"{label}.{member_name}.path",
            ),
            shape=shape,
        )
    return result


def _parse_collections(value: Any, label: str) -> dict[str, CollectionSpec]:
    mapping = _require_mapping(value, label)
    if not mapping:
        raise CompositionError(f"{label} must declare at least one collection.")
    result: dict[str, CollectionSpec] = {}
    paths: dict[str, str] = {}
    for name, raw in mapping.items():
        collection_name = _non_empty_string(name, f"{label} collection")
        body = _require_mapping(raw, f"{label}.{collection_name}")
        _known_keys(
            body,
            {"path", "identity", "members", "seeds"},
            f"{label}.{collection_name}",
        )
        path = _non_empty_string(
            body.get("path"),
            f"{label}.{collection_name}.path",
        )
        if "." in path or "[*]" in path:
            raise CompositionError(
                f"{label}.{collection_name}.path must name one top-level "
                "parameter in this version."
            )
        if path in paths:
            raise CompositionError(
                f"Collections '{paths[path]}' and '{collection_name}' both "
                f"govern parameter path '{path}'."
            )
        identity = _parse_identity(
            body.get("identity"),
            f"{label}.{collection_name}.identity",
        )
        seed_values: list[tuple[str, ...]] = []
        raw_seeds = (
            _require_list(
                body["seeds"],
                f"{label}.{collection_name}.seeds",
            )
            if "seeds" in body
            else []
        )
        for index, seed in enumerate(raw_seeds):
            seed_mapping = _require_mapping(
                seed,
                f"{label}.{collection_name}.seeds[{index}]",
            )
            seed_values.append(
                _identity(
                    CollectionSpec(
                        name=collection_name,
                        path=path,
                        identity=identity,
                        seeds=(),
                        members={},
                    ),
                    seed_mapping,
                    f"{label}.{collection_name}.seeds[{index}]",
                )
            )
        if len(set(seed_values)) != len(seed_values):
            raise CompositionError(
                f"{label}.{collection_name}.seeds contains a duplicate "
                "identity."
            )
        result[collection_name] = CollectionSpec(
            name=collection_name,
            path=path,
            identity=identity,
            seeds=tuple(seed_values),
            members=(
                _parse_members(
                    body["members"],
                    f"{label}.{collection_name}.members",
                )
                if "members" in body
                else {}
            ),
        )
        paths[path] = collection_name
    return result


def _parse_bindings(value: Any, label: str) -> tuple[BindingSpec, ...]:
    mapping = _require_mapping(value, label)
    if not mapping:
        raise CompositionError(f"{label} must declare at least one binding.")
    result: list[BindingSpec] = []
    for name, raw in mapping.items():
        binding_name = _non_empty_string(name, f"{label} binding")
        if isinstance(raw, str):
            result.append(
                BindingSpec(
                    name=binding_name,
                    path=_non_empty_string(raw, f"{label}.{binding_name}"),
                )
            )
            continue
        body = _require_mapping(raw, f"{label}.{binding_name}")
        _known_keys(
            body,
            {"path", "default", "optional"},
            f"{label}.{binding_name}",
        )
        default = body.get("default")
        if default is not None:
            default = _non_empty_string(
                default,
                f"{label}.{binding_name}.default",
            )
        optional = body.get("optional", False)
        if not isinstance(optional, bool):
            raise CompositionError(
                f"{label}.{binding_name}.optional must be a boolean."
            )
        if default is not None and optional:
            raise CompositionError(
                f"{label}.{binding_name} cannot declare both `default` and "
                "`optional`."
            )
        result.append(
            BindingSpec(
                name=binding_name,
                path=_non_empty_string(
                    body.get("path"),
                    f"{label}.{binding_name}.path",
                ),
                default=default,
                optional=optional,
            )
        )
    return tuple(result)


def _parse_match(value: Any, label: str) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    if not mapping:
        raise CompositionError(f"{label} must not be empty.")
    return {
        _non_empty_string(component, f"{label} component"): _non_empty_string(
            binding,
            f"{label}.{component}",
        )
        for component, binding in mapping.items()
    }


def _parse_references(
    value: Any,
    collections: dict[str, CollectionSpec],
    label: str,
) -> tuple[ReferenceRule, ...]:
    raw_rules = [] if value is None else _require_list(value, label)
    result: list[ReferenceRule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        rule_label = f"{label}[{index}]"
        body = _require_mapping(raw, rule_label)
        _known_keys(body, {"id", "source", "target", "unverified"}, rule_label)
        rule_id = _non_empty_string(body.get("id"), f"{rule_label}.id")
        if rule_id in seen_ids:
            raise CompositionError(f"{label} contains duplicate id '{rule_id}'.")
        seen_ids.add(rule_id)

        source_body = _require_mapping(
            body.get("source"),
            f"{rule_label}.source",
        )
        _known_keys(
            source_body,
            {"collection", "select", "bind"},
            f"{rule_label}.source",
        )
        source_collection = _non_empty_string(
            source_body.get("collection"),
            f"{rule_label}.source.collection",
        )
        if source_collection not in collections:
            raise CompositionError(
                f"{rule_label}.source.collection names unknown collection "
                f"'{source_collection}'."
            )
        select = source_body.get("select")
        if select is not None:
            select = _non_empty_string(select, f"{rule_label}.source.select")
        source = ReferenceSource(
            collection=source_collection,
            select=select,
            bindings=_parse_bindings(
                source_body.get("bind"),
                f"{rule_label}.source.bind",
            ),
        )

        has_target = "target" in body
        has_unverified = "unverified" in body
        if has_target == has_unverified:
            raise CompositionError(
                f"{rule_label} must declare exactly one of `target` or "
                "`unverified`."
            )

        target: ReferenceTarget | None = None
        unverified: str | None = None
        if has_unverified:
            unverified = _non_empty_string(
                body["unverified"],
                f"{rule_label}.unverified",
            )
        else:
            target_body = _require_mapping(
                body["target"],
                f"{rule_label}.target",
            )
            _known_keys(
                target_body,
                {"collection", "match", "member"},
                f"{rule_label}.target",
            )
            target_collection = _non_empty_string(
                target_body.get("collection"),
                f"{rule_label}.target.collection",
            )
            if target_collection not in collections:
                raise CompositionError(
                    f"{rule_label}.target.collection names unknown collection "
                    f"'{target_collection}'."
                )
            member: MemberTarget | None = None
            if "member" in target_body:
                member_body = _require_mapping(
                    target_body["member"],
                    f"{rule_label}.target.member",
                )
                _known_keys(
                    member_body,
                    {"name", "match"},
                    f"{rule_label}.target.member",
                )
                member_name = _non_empty_string(
                    member_body.get("name"),
                    f"{rule_label}.target.member.name",
                )
                if member_name not in collections[target_collection].members:
                    raise CompositionError(
                        f"{rule_label}.target.member names unknown member "
                        f"'{member_name}' on collection '{target_collection}'."
                    )
                member = MemberTarget(
                    name=member_name,
                    match=_parse_match(
                        member_body.get("match"),
                        f"{rule_label}.target.member.match",
                    ),
                )
            target = ReferenceTarget(
                collection=target_collection,
                match=_parse_match(
                    target_body.get("match"),
                    f"{rule_label}.target.match",
                ),
                member=member,
            )

        binding_names = {binding.name for binding in source.bindings}
        if target is not None:
            target_spec = collections[target.collection]
            expected_components = {
                field.name for field in target_spec.identity
            }
            if set(target.match) != expected_components:
                raise CompositionError(
                    f"{rule_label}.target.match must map identity components "
                    f"{sorted(expected_components)}, got "
                    f"{sorted(target.match)}."
                )
            referenced_bindings = set(target.match.values())
            if target.member is not None:
                if set(target.member.match) != {"key"}:
                    raise CompositionError(
                        f"{rule_label}.target.member.match must map only `key` "
                        "for a map member."
                    )
                referenced_bindings.update(target.member.match.values())
            missing_bindings = referenced_bindings - binding_names
            if missing_bindings:
                raise CompositionError(
                    f"{rule_label} target references unknown binding(s): "
                    f"{sorted(missing_bindings)}."
                )
        result.append(
            ReferenceRule(
                id=rule_id,
                source=source,
                target=target,
                unverified=unverified,
            )
        )
    return tuple(result)


def load_contract(path: Path) -> CompositionContract:
    """Load one strict `ParameterComposition` contract."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yamlio.load(handle)
    body = _require_mapping(data, f"Parameter composition '{path}'")
    _known_keys(
        body,
        {"apiVersion", "kind", "name", "collections", "references"},
        f"Parameter composition '{path}'",
    )
    if body.get("apiVersion") != "siteops/v1":
        raise CompositionError(
            f"Parameter composition '{path}' must use apiVersion "
            "'siteops/v1'."
        )
    if body.get("kind") != "ParameterComposition":
        raise CompositionError(
            f"Parameter composition '{path}' must use kind "
            "'ParameterComposition'."
        )
    _non_empty_string(body.get("name"), f"Parameter composition '{path}'.name")
    collections = _parse_collections(
        body.get("collections"),
        f"Parameter composition '{path}'.collections",
    )
    return CompositionContract(
        collections=collections,
        references=_parse_references(
            body.get("references"),
            collections,
            f"Parameter composition '{path}'.references",
        ),
    )


def merge_contracts(contracts: list[CompositionContract]) -> CompositionContract:
    """Merge contracts whose collection names and paths are disjoint."""
    collections: dict[str, CollectionSpec] = {}
    paths: dict[str, str] = {}
    references: list[ReferenceRule] = []
    reference_ids: set[str] = set()
    for contract in contracts:
        for name, spec in contract.collections.items():
            if name in collections:
                raise CompositionError(
                    f"Parameter composition collection '{name}' is declared "
                    "by more than one contract."
                )
            if spec.path in paths:
                raise CompositionError(
                    f"Parameter composition collections '{paths[spec.path]}' "
                    f"and '{name}' both govern path '{spec.path}'."
                )
            collections[name] = spec
            paths[spec.path] = name
        for rule in contract.references:
            if rule.id in reference_ids:
                raise CompositionError(
                    f"Parameter composition reference id '{rule.id}' is "
                    "declared by more than one contract."
                )
            reference_ids.add(rule.id)
            references.append(rule)
    return CompositionContract(collections, tuple(references))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _contains_reserved_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        return "_siteops" in value or any(
            _contains_reserved_metadata(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_reserved_metadata(item) for item in value)
    return False


def contains_composition_metadata(value: Any) -> bool:
    """Return whether a value contains the reserved `_siteops` envelope."""
    return _contains_reserved_metadata(value)


def _metadata(
    source: LoadedParameterSource,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data = copy.deepcopy(source.data)
    raw = data.pop("_siteops", {})
    if raw is None:
        raise CompositionError(
            f"{source.path}: `_siteops` must be a mapping, not null."
        )
    metadata = _require_mapping(raw, f"{source.path}: `_siteops`")
    _known_keys(
        metadata,
        {"requires", "external"},
        f"{source.path}: `_siteops`",
    )
    if _contains_reserved_metadata(data):
        raise CompositionError(
            f"{source.path}: `_siteops` is allowed only at the parameter "
            "source root."
        )
    return data, metadata


def compose_sources(
    contract: CompositionContract,
    sources: list[LoadedParameterSource],
) -> CompositionResult:
    """Compose loaded and site-resolved parameter sources."""
    by_path = {spec.path: spec for spec in contract.collections.values()}
    entries: dict[str, list[ComposedEntry]] = {
        name: [] for name in contract.collections
    }
    entry_index: dict[str, dict[tuple[str, ...], ComposedEntry]] = {
        name: {} for name in contract.collections
    }
    external: dict[str, list[ExternalEntry]] = {
        name: [] for name in contract.collections
    }
    external_index: dict[str, dict[tuple[str, ...], ExternalEntry]] = {
        name: {} for name in contract.collections
    }
    requirements: list[Requirement] = []
    parameters: dict[str, Any] = {}

    for source in sources:
        data, metadata = _metadata(source)
        allowed = set(source.collections)
        has_requirements = False
        unknown_allowed = allowed - set(contract.collections)
        if unknown_allowed:
            raise CompositionError(
                f"{source.path}: parameter source lists unknown governed "
                f"collection(s): {sorted(unknown_allowed)}."
            )
        contributed: set[str] = set()

        for path, spec in by_path.items():
            if path not in data:
                continue
            contributed.add(spec.name)
            if spec.name not in allowed:
                raise CompositionError(
                    f"{source.path}: parameter source contributes governed "
                    f"collection '{spec.name}' without listing it in "
                    "`collections`."
                )
            raw_entries = _require_list(
                data.pop(path),
                f"{source.path}: collection '{spec.name}'",
            )
            for index, raw_entry in enumerate(raw_entries):
                entry = _require_mapping(
                    raw_entry,
                    f"{source.path}: {spec.name}[{index}]",
                )
                identity = _identity(
                    spec,
                    entry,
                    f"{source.path}: {spec.name}[{index}]",
                )
                label = _identity_label(spec, identity)
                if identity in spec.seeds:
                    raise CompositionError(
                        f"{source.path}: {label} is provider-owned and cannot "
                        "be written by a resource set."
                    )
                previous = entry_index[spec.name].get(identity)
                if previous is not None:
                    raise CompositionError(
                        f"{source.path} and {previous.source} both write "
                        f"{label}. Duplicate composed identities are not "
                        "allowed."
                    )
                previous_external = external_index[spec.name].get(identity)
                if previous_external is not None:
                    raise CompositionError(
                        f"{source.path}: writer {label} conflicts with external "
                        f"assertion from {previous_external.source}."
                    )
                composed = ComposedEntry(
                    identity=identity,
                    value=copy.deepcopy(entry),
                    source=source.path,
                )
                entries[spec.name].append(composed)
                entry_index[spec.name][identity] = composed

        external_mapping = _require_mapping(
            metadata.get("external", {}),
            f"{source.path}: `_siteops.external`",
        )
        for collection_name, raw_entries in external_mapping.items():
            if collection_name not in contract.collections:
                raise CompositionError(
                    f"{source.path}: `_siteops.external` names unknown "
                    f"collection '{collection_name}'."
                )
            contributed.add(collection_name)
            if collection_name not in allowed:
                raise CompositionError(
                    f"{source.path}: external collection '{collection_name}' "
                    "is not listed in this parameter source's `collections`."
                )
            spec = contract.collections[collection_name]
            for index, raw_entry in enumerate(
                _require_list(
                    raw_entries,
                    f"{source.path}: `_siteops.external.{collection_name}`",
                )
            ):
                body = _require_mapping(
                    raw_entry,
                    f"{source.path}: external {collection_name}[{index}]",
                )
                _known_keys(
                    body,
                    _identity_root_keys(spec) | {"reason", "expects"},
                    f"{source.path}: external {collection_name}[{index}]",
                )
                reason = _non_empty_string(
                    body.get("reason"),
                    f"{source.path}: external "
                    f"{collection_name}[{index}].reason",
                )
                expects = body.get("expects", {})
                expects = _require_mapping(
                    expects,
                    f"{source.path}: external "
                    f"{collection_name}[{index}].expects",
                )
                identity = _identity(
                    spec,
                    body,
                    f"{source.path}: external {collection_name}[{index}]",
                )
                label = _identity_label(spec, identity)
                if identity in spec.seeds:
                    raise CompositionError(
                        f"{source.path}: external {label} conflicts with a "
                        "provider-owned seed."
                    )
                writer = entry_index[collection_name].get(identity)
                if writer is not None:
                    raise CompositionError(
                        f"{source.path}: external {label} conflicts with "
                        f"writer {writer.source}."
                    )
                previous_external = external_index[collection_name].get(identity)
                if previous_external is not None:
                    raise CompositionError(
                        f"{source.path} and {previous_external.source} both "
                        f"assert external {label}."
                    )
                external_entry = ExternalEntry(
                    identity=identity,
                    expects=copy.deepcopy(expects),
                    reason=reason,
                    source=source.path,
                )
                external[collection_name].append(external_entry)
                external_index[collection_name][identity] = external_entry

        requirement_mapping = _require_mapping(
            metadata.get("requires", {}),
            f"{source.path}: `_siteops.requires`",
        )
        for collection_name, raw_requirements in requirement_mapping.items():
            if collection_name not in contract.collections:
                raise CompositionError(
                    f"{source.path}: `_siteops.requires` names unknown "
                    f"collection '{collection_name}'."
                )
            spec = contract.collections[collection_name]
            requirement_entries = _require_list(
                raw_requirements,
                f"{source.path}: `_siteops.requires.{collection_name}`",
            )
            if requirement_entries:
                has_requirements = True
            for index, raw_requirement in enumerate(requirement_entries):
                body = _require_mapping(
                    raw_requirement,
                    f"{source.path}: requirement "
                    f"{collection_name}[{index}]",
                )
                _known_keys(
                    body,
                    _identity_root_keys(spec),
                    f"{source.path}: requirement "
                    f"{collection_name}[{index}]",
                )
                identity = _identity(
                    spec,
                    body,
                    f"{source.path}: requirement "
                    f"{collection_name}[{index}]",
                )
                requirement = Requirement(
                    collection=collection_name,
                    identity=identity,
                    source=source.path,
                )
                requirements.append(requirement)

        undeclared = contributed - allowed
        if undeclared:
            raise CompositionError(
                f"{source.path}: source contributes undeclared governed "
                f"collection(s): {sorted(undeclared)}."
            )
        if allowed and not contributed and not has_requirements:
            raise CompositionError(
                f"{source.path}: typed parameter source lists governed "
                f"collection(s) {sorted(allowed)} but contributes no governed "
                "collection, external assertion, or requirement. Add the "
                "intended collection, or declare an explicit empty array."
            )
        parameters = _deep_merge(parameters, data)

    for name, collection_entries in entries.items():
        parameters[contract.collections[name].path] = [
            copy.deepcopy(entry.value) for entry in collection_entries
        ]

    for requirement in requirements:
        spec = contract.collections[requirement.collection]
        if (
            requirement.identity not in entry_index[requirement.collection]
            and requirement.identity not in external_index[requirement.collection]
            and requirement.identity not in spec.seeds
        ):
            available = sorted(
                [
                    _identity_label(spec, identity)
                    for identity in (
                        set(entry_index[requirement.collection])
                        | set(external_index[requirement.collection])
                        | set(spec.seeds)
                    )
                ]
            )
            raise CompositionError(
                f"{requirement.source}: requirement "
                f"{_identity_label(spec, requirement.identity)} is not "
                "provided by any selected resource set, external assertion, "
                f"or provider seed. Available: {available}."
            )

    reference_results: list[ReferenceResult] = []
    referenced_by_source: dict[
        Path,
        set[tuple[str, tuple[str, ...]]],
    ] = {}
    for rule in contract.references:
        source_spec = contract.collections[rule.source.collection]
        for source_entry in entries[rule.source.collection]:
            for selected in _select(
                source_entry.value,
                rule.source.select,
                f"{source_entry.source}: rule '{rule.id}'",
            ):
                if not isinstance(selected, dict):
                    raise CompositionError(
                        f"{source_entry.source}: rule '{rule.id}' selected a "
                        "non-mapping value."
                    )
                bindings: dict[str, str] = {}
                skip = False
                for binding in rule.source.bindings:
                    found, value = _read_path(selected, binding.path)
                    if not found:
                        if binding.default is not None:
                            value = binding.default
                        elif binding.optional:
                            skip = True
                            break
                        else:
                            raise CompositionError(
                                f"{source_entry.source}: rule '{rule.id}' "
                                f"requires binding path '{binding.path}'."
                            )
                    if not isinstance(value, str) or not value.strip():
                        raise CompositionError(
                            f"{source_entry.source}: rule '{rule.id}' binding "
                            f"'{binding.name}' must resolve to a non-empty "
                            "string."
                        )
                    if value != value.strip():
                        raise CompositionError(
                            f"{source_entry.source}: rule '{rule.id}' binding "
                            f"'{binding.name}' must not contain leading or "
                            "trailing whitespace."
                        )
                    bindings[binding.name] = value
                if skip:
                    continue

                if rule.unverified is not None:
                    reference_results.append(
                        ReferenceResult(
                            rule_id=rule.id,
                            source_collection=rule.source.collection,
                            source_identity=source_entry.identity,
                            source_path=source_entry.source,
                            target_collection=None,
                            target_identity=None,
                            target_source=None,
                            unverified_reason=rule.unverified,
                        )
                    )
                    continue

                assert rule.target is not None
                target_spec = contract.collections[rule.target.collection]
                target_identity = tuple(
                    bindings[rule.target.match[field.name]]
                    for field in target_spec.identity
                )
                target_entry = entry_index[rule.target.collection].get(
                    target_identity
                )
                target_external = external_index[rule.target.collection].get(
                    target_identity
                )
                is_seed = target_identity in target_spec.seeds
                if target_entry is None and target_external is None and not is_seed:
                    requested = _identity_label(
                        target_spec,
                        target_identity,
                    )
                    available = sorted(
                        [
                            _identity_label(target_spec, identity)
                            for identity in (
                                set(entry_index[rule.target.collection])
                                | set(external_index[rule.target.collection])
                                | set(target_spec.seeds)
                            )
                        ]
                    )
                    closest = difflib.get_close_matches(
                        requested,
                        available,
                        n=1,
                    )
                    hint = (
                        f" Closest available identity: {closest[0]}."
                        if closest
                        else ""
                    )
                    raise CompositionError(
                        f"{source_entry.source}: rule '{rule.id}' reference "
                        f"from {_identity_label(source_spec, source_entry.identity)} "
                        f"does not resolve to {requested}. Available: "
                        f"{available}.{hint} If the target is managed outside "
                        "this composition, select a source that declares "
                        f"`_siteops.external.{rule.target.collection}`."
                    )

                target_value: dict[str, Any] | None = None
                target_source: Path | None = None
                is_external = False
                if target_entry is not None:
                    target_value = target_entry.value
                    target_source = target_entry.source
                elif target_external is not None:
                    target_value = target_external.expects
                    target_source = target_external.source
                    is_external = True

                if rule.target.member is not None:
                    member = target_spec.members[rule.target.member.name]
                    binding_name = rule.target.member.match["key"]
                    member_identity = bindings[binding_name]
                    if target_value is None:
                        raise CompositionError(
                            f"{source_entry.source}: rule '{rule.id}' cannot "
                            f"resolve member '{member.name}' on provider seed "
                            f"{_identity_label(target_spec, target_identity)}."
                        )
                    found, member_values = _read_path(target_value, member.path)
                    if not found and is_external:
                        reference_results.append(
                            ReferenceResult(
                                rule_id=rule.id,
                                source_collection=rule.source.collection,
                                source_identity=source_entry.identity,
                                source_path=source_entry.source,
                                target_collection=rule.target.collection,
                                target_identity=target_identity,
                                target_source=target_source,
                                target_member_name=member.name,
                                target_member_identity=member_identity,
                                external=True,
                                unverified_reason=(
                                    "External expectation does not cover "
                                    f"member path '{member.path}'."
                                ),
                            )
                        )
                        continue
                    if not found or not isinstance(member_values, dict):
                        raise CompositionError(
                            f"{target_source}: "
                            f"{_identity_label(target_spec, target_identity)} "
                            f"does not declare map member '{member.path}'."
                        )
                    if member_identity not in member_values:
                        available = sorted(str(key) for key in member_values)
                        raise CompositionError(
                            f"{source_entry.source}: rule '{rule.id}' "
                            f"references member {member_identity!r} on "
                            f"{_identity_label(target_spec, target_identity)}, "
                            f"but available keys are {available}."
                        )

                referenced_by_source.setdefault(source_entry.source, set()).add(
                    (rule.target.collection, target_identity)
                )
                reference_results.append(
                    ReferenceResult(
                        rule_id=rule.id,
                        source_collection=rule.source.collection,
                        source_identity=source_entry.identity,
                        source_path=source_entry.source,
                        target_collection=rule.target.collection,
                        target_identity=target_identity,
                        target_source=target_source,
                        target_member_name=(
                            rule.target.member.name
                            if rule.target.member is not None
                            else None
                        ),
                        target_member_identity=(
                            bindings[rule.target.member.match["key"]]
                            if rule.target.member is not None
                            else None
                        ),
                        external=is_external,
                    )
                )

    for requirement in requirements:
        if (
            requirement.collection,
            requirement.identity,
        ) in referenced_by_source.get(requirement.source, set()):
            spec = contract.collections[requirement.collection]
            raise CompositionError(
                f"{requirement.source}: requirement "
                f"{_identity_label(spec, requirement.identity)} duplicates a "
                "dependency already expressed by a reference rule."
            )

    return CompositionResult(
        parameters=parameters,
        sources=tuple(
            SelectedSource(source.path, source.collections)
            for source in sources
            if source.collections
        ),
        entries={
            name: tuple(collection_entries)
            for name, collection_entries in entries.items()
        },
        external={
            name: tuple(collection_entries)
            for name, collection_entries in external.items()
        },
        requirements=tuple(requirements),
        references=tuple(reference_results),
    )

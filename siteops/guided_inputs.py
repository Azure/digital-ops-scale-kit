# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Resolve one declarative set of typed answers into an ordinary Site."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml

from siteops import yamlio
from siteops.artifacts import open_regular_file
from siteops.cache_layout import write_new
from siteops.models import Site, _validate_resource
from siteops.workspace_package import MaterializedPackageBinding

if TYPE_CHECKING:
    from siteops.arm_resources import ArmResourceObservation, ArmResourceRef

_VERSION = "siteops.inputs/v1"
_MAX_YAML_BYTES = 128 * 1024
_MAX_INPUTS = 64
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_ROOT_FIELDS = {"name", "subscription", "resourceGroup", "location"}
_MAPPING_FIELDS = {"labels", "parameters", "properties"}
_FIELD_KEYS = {
    "name", "type", "description", "sitePath", "required", "default", "sensitive",
    "when", "resource", "derive", "requires",
}
_MISSING = object()
_RESOURCE_FACTS = {
    "Microsoft.Kubernetes/connectedClusters": frozenset({
        "connectedClusters.workloadIdentityEnabled",
        "connectedClusters.oidcIssuerAvailable",
    }),
}
_OBSERVED_FIELDS = frozenset({"id", "subscription", "resourceGroup", "name", "location"})


class GuidedInputError(ValueError):
    """A guided-input validation failure with no supplied values or local paths."""


class ResourceInputError(GuidedInputError):
    """A value-safe input error with a stable resource observation code."""

    def __init__(self, category: str, message: str):
        if category not in {
            "invalid-id", "type-mismatch", "subscription-mismatch",
            "conflict", "requirement-unmet", "requirement-unverified",
            "invalid-observation",
        }:
            raise ValueError("Unsupported resource input failure category.")
        self.code = f"inputs.resource.{category}"
        super().__init__(f"{self.code}: {message}")


def contract_path(manifest_path: Path) -> Path:
    """Choose one manifest-specific sidecar without inspecting the filesystem."""
    if manifest_path.suffix not in {".yaml", ".yml"}:
        raise GuidedInputError("The manifest must have a .yaml or .yml filename.")
    if manifest_path.stem == "manifest":
        return manifest_path.with_name("inputs.yaml")
    return manifest_path.with_name(f"{manifest_path.stem}.inputs.yaml")


def _read_yaml(path: Path, *, label: str) -> Any:
    if not path.exists() and not path.is_symlink():
        raise GuidedInputError(f"{label} was not found.") from None
    with open_regular_file(path) as stream:
        raw = stream.read(_MAX_YAML_BYTES + 1)
    if len(raw) > _MAX_YAML_BYTES:
        raise GuidedInputError(f"{label} exceeds the 128 KiB YAML limit.")
    try:
        return yamlio.load_bounded(raw.decode("utf-8"))
    except yamlio.DuplicateKeyError:
        raise GuidedInputError(f"{label} contains a duplicate YAML key.") from None
    except (UnicodeError, yaml.YAMLError, RecursionError):
        raise GuidedInputError(f"{label} must be valid UTF-8 YAML within structural limits.") from None


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GuidedInputError(f"{label} must be a mapping.")
    if any(not isinstance(key, str) for key in value):
        raise GuidedInputError(f"{label} must have string keys.")
    return value


def _shape(value: dict[str, Any], *, allowed: set[str], required: set[str], label: str) -> None:
    if value.keys() - allowed:
        raise GuidedInputError(f"{label} contains an unknown key.")
    if required - value.keys():
        raise GuidedInputError(f"{label} is missing a required key.")


def _site_defaults(value: Any) -> dict[str, Any]:
    defaults = _mapping(value, "siteDefaults")
    _shape(defaults, allowed=_MAPPING_FIELDS, required=set(), label="siteDefaults")
    for name, content in defaults.items():
        _mapping(content, f"siteDefaults.{name}")
    pending = [defaults]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            _mapping(item, "siteDefaults nested mapping")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise GuidedInputError("siteDefaults contains an unsupported YAML value.")
    return defaults


def _typed_value(value: Any, kind: str, label: str, *, required: bool) -> None:
    if kind == "string":
        if not isinstance(value, str):
            raise GuidedInputError(f"{label} must be a string.")
        if required and not value.strip():
            raise GuidedInputError(f"{label} is required and must be a nonempty string.")
    elif kind == "boolean" and type(value) is not bool:
        raise GuidedInputError(f"{label} must be a boolean (true or false).")
    elif kind == "azureResourceId" and (
        not isinstance(value, str) or not value.strip()
    ):
        raise GuidedInputError(f"{label} must be one nonempty ARM resource ID.")


def _site_path(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise GuidedInputError("Input sitePath must be a dotted Site field.")
    try:
        size = len(value.encode("ascii"))
    except UnicodeEncodeError:
        raise GuidedInputError("Input sitePath must use ASCII identifiers.") from None
    if size > 256:
        raise GuidedInputError("Input sitePath exceeds the 256-byte limit.")
    parts = tuple(value.split("."))
    if (
        parts[0] in _ROOT_FIELDS and len(parts) == 1
        or parts[0] == "labels" and len(parts) == 2
        or parts[0] in {"parameters", "properties"} and len(parts) >= 2
    ) and len(parts) <= 8 and all(
        len(part) <= 64 and _IDENTIFIER.fullmatch(part) for part in parts
    ):
        return parts
    raise GuidedInputError(
        "Input sitePath must name a supported Site field with at most "
        "8 segments of 64 characters, without indexes or wildcards."
    )


@dataclass(frozen=True)
class InputCondition:
    input: str
    equals: str | bool


@dataclass(frozen=True)
class ResourceRequirement:
    fact: str
    description: str
    when: InputCondition | None = None


@dataclass(frozen=True)
class ResourceBinding:
    resource_type: str
    api_version: str
    derive: tuple[tuple[str, str], ...]
    subscription: str | None = None
    requires: tuple[ResourceRequirement, ...] = ()


@dataclass(frozen=True)
class InputField:
    name: str
    type: str
    description: str
    site_path: tuple[str, ...] | None
    required: bool
    sensitive: bool
    default: str | bool | object = _MISSING
    when: InputCondition | None = None
    resource: ResourceBinding | None = None

    @property
    def has_default(self) -> bool:
        return self.default is not _MISSING


def _condition(
    value: Any, label: str, earlier: dict[str, InputField],
) -> InputCondition:
    row = _mapping(value, label)
    _shape(row, allowed={"input", "equals"}, required={"input", "equals"}, label=label)
    controller = row["input"]
    if not isinstance(controller, str) or controller not in earlier:
        raise GuidedInputError(f"{label} must reference an earlier declared input.")
    field = earlier[controller]
    if field.when is not None or field.resource is not None:
        raise GuidedInputError(f"{label} cannot reference a conditional or resource input.")
    _typed_value(row["equals"], field.type, f"{label} equals", required=False)
    return InputCondition(controller, row["equals"])


def _resource_binding(
    row: dict[str, Any], label: str, earlier: dict[str, InputField],
) -> ResourceBinding:
    resource = _mapping(row["resource"], f"{label} resource")
    _shape(
        resource,
        allowed={"type", "apiVersion", "subscription"},
        required={"type", "apiVersion"},
        label=f"{label} resource",
    )
    resource_type = resource["type"]
    if not isinstance(resource_type, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.-]{0,127}/[A-Za-z][A-Za-z0-9_.-]{0,127}",
        resource_type,
        re.ASCII,
    ):
        raise GuidedInputError(f"{label} resource type must be one ARM provider/type.")
    api_version = resource["apiVersion"]
    if not isinstance(api_version, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:-preview)?", api_version, re.ASCII,
    ):
        raise GuidedInputError(f"{label} resource apiVersion must be a pinned date.")
    try:
        date.fromisoformat(api_version[:10])
    except ValueError:
        raise GuidedInputError(f"{label} resource apiVersion must be a real date.") from None
    subscription = resource.get("subscription")
    if subscription is not None and subscription != "site":
        raise GuidedInputError(f"{label} resource subscription must be `site` when supplied.")
    derive = _mapping(row.get("derive", {}), f"{label} derive")
    if derive.keys() - _OBSERVED_FIELDS or any(
        not isinstance(target, str) or not _IDENTIFIER.fullmatch(target)
        for target in derive.values()
    ):
        raise GuidedInputError(f"{label} derive must map known resource facts to input names.")
    if subscription == "site" and "subscription" in derive:
        raise GuidedInputError(f"{label} cannot constrain and derive the Site subscription.")
    checks = row.get("requires", [])
    if not isinstance(checks, list) or len(checks) > 8:
        raise GuidedInputError(f"{label} requires must be a list of at most eight checks.")
    allowed_facts = next(
        (facts for kind, facts in _RESOURCE_FACTS.items()
         if kind.casefold() == resource_type.casefold()),
        frozenset(),
    )
    requirements: list[ResourceRequirement] = []
    for number, item in enumerate(checks, start=1):
        requirement_label = f"{label} requires {number}"
        check = _mapping(item, requirement_label)
        _shape(
            check,
            allowed={"fact", "description", "when"},
            required={"fact", "description"},
            label=requirement_label,
        )
        fact, description = check["fact"], check["description"]
        if not isinstance(fact, str) or fact not in allowed_facts:
            raise GuidedInputError(f"{requirement_label} names an unsupported resource fact.")
        if not isinstance(description, str) or not description.strip() or len(description) > 256:
            raise GuidedInputError(f"{requirement_label} description must be bounded text.")
        if any(existing.fact == fact for existing in requirements):
            raise GuidedInputError(f"{label} contains a duplicate resource requirement.")
        when = (
            _condition(check["when"], f"{requirement_label} when", earlier)
            if "when" in check else None
        )
        requirements.append(ResourceRequirement(fact, description, when))
    return ResourceBinding(
        resource_type, api_version, tuple(derive.items()), subscription, tuple(requirements),
    )


def _parse_fields(value: Any) -> tuple[InputField, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_INPUTS:
        raise GuidedInputError("Contract inputs must be a nonempty list of at most 64 fields.")
    fields: list[InputField] = []
    by_name: dict[str, InputField] = {}
    for index, item in enumerate(value, start=1):
        label = f"Input declaration {index}"
        row = _mapping(item, label)
        _shape(
            row,
            allowed=_FIELD_KEYS,
            required={"name", "type", "description"},
            label=label,
        )
        name = row["name"]
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise GuidedInputError(f"{label} name must be a simple ASCII identifier.")
        if name in by_name:
            raise GuidedInputError(f"Contract inputs contain a duplicate name: {name}.")
        kind = row["type"]
        if kind not in ("string", "boolean", "azureResourceId"):
            raise GuidedInputError(f"{label} type must be string, boolean or azureResourceId.")
        description = row["description"]
        if not isinstance(description, str) or not description.strip():
            raise GuidedInputError(f"{label} description must be nonempty text.")
        resource_role = kind == "azureResourceId"
        if not resource_role and (
            "sitePath" not in row or any(key in row for key in ("resource", "derive", "requires"))
        ):
            raise GuidedInputError(f"{label} requires sitePath and cannot declare a resource read.")
        if resource_role and ("resource" not in row or "default" in row):
            raise GuidedInputError(f"{label} requires resource and cannot declare a default.")
        path = _site_path(row["sitePath"]) if "sitePath" in row else None
        if resource_role and path is None and not row.get("derive"):
            raise GuidedInputError(f"{label} requires sitePath or derived input fields.")
        if resource_role and path is not None and path[0] not in {"parameters", "properties"}:
            raise GuidedInputError(f"{label} resource ID sitePath must be a parameter or property.")
        if path is not None and any(
            field.site_path is not None and (
                path[: len(field.site_path)] == field.site_path
                or field.site_path[: len(path)] == path
            )
            for field in fields
        ):
            raise GuidedInputError(f"{label} sitePath overlaps another input writer path.")
        default = row.get("default", _MISSING)
        required = row.get("required", default is _MISSING)
        sensitive = row.get("sensitive", False)
        if type(required) is not bool:
            raise GuidedInputError(f"{label} required must be a boolean.")
        if type(sensitive) is not bool:
            raise GuidedInputError(f"{label} sensitive must be a boolean.")
        if sensitive:
            raise GuidedInputError(
                f"{label} sensitive inputs are unsupported until protected Site output is available."
            )
        if default is not _MISSING:
            _typed_value(default, kind, f"{label} default", required=required)
        when = _condition(row["when"], f"{label} when", by_name) if "when" in row else None
        resource = _resource_binding(row, label, by_name) if resource_role else None
        field = InputField(name, kind, description, path, required, sensitive, default, when, resource)
        fields.append(field)
        by_name[name] = field
    roles = [field for field in fields if field.resource is not None]
    if len(roles) > 4:
        raise GuidedInputError("A contract supports at most four resource inputs.")
    if any(role.resource.subscription == "site" for role in roles) and not any(
        field.site_path == ("subscription",) for field in fields
    ):
        raise GuidedInputError(
            "A resource requiring the Site subscription needs a declared Site subscription input."
        )
    controllers = {
        field.when.input for field in fields if field.when is not None
    } | {
        check.when.input for field in roles for check in field.resource.requires
        if check.when is not None
    }
    derived_targets: set[str] = set()
    for role in roles:
        for fact, target_name in role.resource.derive:
            target = by_name.get(target_name)
            if (
                target is None or target.type != "string"
                or target.has_default or target_name in controllers
                or target.when != role.when or target_name in derived_targets
            ):
                raise GuidedInputError(
                    f"Input '{role.name}' has an invalid or conflicting derive target."
                )
            derived_targets.add(target_name)
            if role.site_path is not None and target.site_path is not None and (
                role.site_path[: len(target.site_path)] == target.site_path
                or target.site_path[: len(role.site_path)] == role.site_path
            ):
                raise GuidedInputError(
                    f"Input '{role.name}' would write a derived Site path twice."
                )
    return tuple(fields)


def _validate_default_writers(
    defaults: dict[str, Any], fields: tuple[InputField, ...],
) -> None:
    for field in fields:
        if field.site_path is None:
            continue
        current: Any = defaults
        for part in field.site_path:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            if isinstance(current, dict):
                raise GuidedInputError(
                    f"Input '{field.name}' sitePath would replace a siteDefaults mapping."
                )


def _same_observed_value(fact: str, supplied: str, observed: str) -> bool:
    if fact == "location":
        return "".join(supplied.split()).casefold() == "".join(observed.split()).casefold()
    return supplied.casefold() == observed.casefold()


@dataclass(frozen=True)
class BoundResource:
    """A declared resource role and one syntactically validated supplied ID."""

    field: InputField
    ref: ArmResourceRef = dataclass_field(repr=False)
    required_facts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BoundInputs:
    """Resolved local answers and their origins, before any provider read."""

    active_values: Mapping[str, str | bool] = dataclass_field(repr=False)
    sources: Mapping[str, str]
    resources: tuple[BoundResource, ...] = dataclass_field(default=(), repr=False)


@dataclass(frozen=True)
class InputContract:
    """A closed, versioned contract that resolves file and inline answers into one Site."""

    fields: tuple[InputField, ...]
    _site_defaults: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_site_defaults", copy.deepcopy(self._site_defaults))

    @property
    def inputs(self) -> tuple[InputField, ...]:
        return self.fields

    def describe(self) -> dict[str, Any]:
        """Return safe metadata, without protected defaults or supplied values."""
        controllers = {field.name: field for field in self.fields}
        derivations: dict[str, list[str]] = {}
        for field in self.fields:
            if field.resource is not None:
                for _, target in field.resource.derive:
                    derivations.setdefault(target, []).append(field.name)
        rows = []
        for field in self.fields:
            status = (
                "conditional" if field.when else
                "defaulted" if field.has_default else
                "required" if field.required else "optional"
            )
            row: dict[str, Any] = {
                "name": field.name,
                "type": field.type,
                "description": field.description,
                "required": field.required,
                "sensitive": field.sensitive,
                "status": status,
            }
            if field.site_path is not None:
                row["sitePath"] = ".".join(field.site_path)
            if field.name in derivations:
                row["derivableFrom"] = derivations[field.name]
            if field.resource is not None:
                row["resource"] = {
                    "type": field.resource.resource_type,
                    "apiVersion": field.resource.api_version,
                }
                if field.resource.subscription is not None:
                    row["resource"]["subscription"] = field.resource.subscription
                if field.resource.derive:
                    row["derive"] = dict(field.resource.derive)
                if field.resource.requires:
                    row["requires"] = [
                        {
                            "fact": check.fact,
                            "description": check.description,
                            **(
                                {"when": {"input": check.when.input, "equals": check.when.equals}}
                                if check.when else {}
                            ),
                        }
                        for check in field.resource.requires
                    ]
            if field.has_default and not field.sensitive:
                row["default"] = field.default
            if field.when:
                row["when"] = {"input": field.when.input}
                if not controllers[field.when.input].sensitive:
                    row["when"]["equals"] = field.when.equals
            rows.append(row)
        return {"apiVersion": _VERSION, "kind": "SiteInputContract", "inputs": rows}

    def example(self) -> dict[str, Any]:
        """Provide incomplete answers, never a deployable placeholder Site."""
        required = {
            field.name for field in self.fields
            if field.required and not field.has_default and field.when is None
        }
        return {
            "apiVersion": _VERSION,
            "kind": "SiteInputValues",
            "values": {
                field.name: None for field in self.fields
                if field.name in required
                or (
                    field.resource is not None and not field.required
                    and field.when is None
                    and any(target in required for _, target in field.resource.derive)
                )
            },
        }

    def bind(
        self, values_file: Path | None = None, inline: list[str] | None = None,
    ) -> BoundInputs:
        """Validate and merge local answers without constructing a Site."""
        fields = {field.name: field for field in self.fields}
        inline_values: dict[str, str | bool] = {}
        for answer in inline or []:
            if not isinstance(answer, str) or "=" not in answer:
                raise GuidedInputError("Inline inputs must use NAME=VALUE format.")
            name, value = answer.split("=", 1)
            if name not in fields:
                raise GuidedInputError("Inline inputs contain an unknown input name.")
            if name in inline_values:
                raise GuidedInputError(f"Duplicate inline input '{name}'.")
            field = fields[name]
            if field.sensitive:
                raise GuidedInputError(f"Sensitive input '{name}' cannot be supplied inline.")
            if field.type == "boolean":
                if value not in ("true", "false"):
                    raise GuidedInputError(f"Input '{name}' must be a boolean (true or false).")
                inline_values[name] = value == "true"
            else:
                _typed_value(value, "string", f"Input '{name}'", required=field.required)
                inline_values[name] = value

        file_values: dict[str, Any] = {}
        if values_file is not None:
            document = _mapping(_read_yaml(values_file, label="Input values file"), "Input values file")
            _shape(
                document,
                allowed={"apiVersion", "kind", "values"},
                required={"apiVersion", "kind", "values"},
                label="Input values file",
            )
            if document["apiVersion"] != _VERSION:
                raise GuidedInputError("Input values file has an unsupported apiVersion.")
            if document["kind"] != "SiteInputValues":
                raise GuidedInputError("Input values file kind must be SiteInputValues.")
            file_values = _mapping(document["values"], "Input values")
            for name, value in file_values.items():
                if name not in fields:
                    raise GuidedInputError("Input values contain an unknown input name.")
                field = fields[name]
                if value is None and (
                    field.required or (field.resource is not None and field.when is None)
                ):
                    continue
                _typed_value(value, field.type, f"Input '{name}'", required=field.required)

        effective = {
            field.name: field.default for field in self.fields if field.has_default
        }
        effective.update(file_values)
        effective.update(inline_values)
        active_values: dict[str, str | bool] = {}
        sources: dict[str, str] = {}
        missing_required: list[str] = []
        for field in self.fields:
            if field.when is not None:
                controller = active_values.get(field.when.input, _MISSING)
                if controller is _MISSING:
                    raise GuidedInputError(
                        f"Input '{field.when.input}' is missing; it controls conditional "
                        f"input '{field.name}'."
                    )
                if controller != field.when.equals:
                    if field.name in file_values or field.name in inline_values:
                        raise GuidedInputError(
                            f"Input '{field.name}' was supplied while its condition is inactive."
                        )
                    continue
            value = effective.get(field.name, _MISSING)
            if value is _MISSING or value is None:
                if field.required:
                    missing_required.append(field.name)
                continue
            active_values[field.name] = value
            sources[field.name] = (
                "inline" if field.name in inline_values
                else "input file" if field.name in file_values else "default"
            )
        resource_refs: list[BoundResource] = []
        for field in self.fields:
            if field.resource is None or field.name not in active_values:
                continue
            from siteops.arm_resources import ArmResourceError, parse_arm_resource_id

            try:
                ref = parse_arm_resource_id(
                    active_values[field.name],
                    expected_type=field.resource.resource_type,
                    api_version=field.resource.api_version,
                )
            except ArmResourceError as error:
                category = "type-mismatch" if error.code == "TYPE_MISMATCH" else "invalid-id"
                raise ResourceInputError(
                    category, f"Resource input '{field.name}' has an invalid ID."
                ) from None
            for fact, target_name in field.resource.derive:
                if fact == "location":
                    continue
                observed = {
                    "id": ref.resource_id,
                    "subscription": ref.subscription,
                    "resourceGroup": ref.resource_group,
                    "name": ref.name,
                }[fact]
                existing = active_values.get(target_name)
                if existing is not None and not _same_observed_value(fact, existing, observed):
                    raise ResourceInputError(
                        "conflict",
                        f"Input '{target_name}' ({sources[target_name]}) conflicts "
                        f"with resource input '{field.name}'.",
                    )
                if existing is None:
                    active_values[target_name] = observed
                    sources[target_name] = f"resource '{field.name}'"
            required_facts = frozenset(
                check.fact for check in field.resource.requires
                if check.when is None
                or active_values.get(check.when.input) == check.when.equals
            )
            resource_refs.append(BoundResource(field, ref, required_facts))
        site_subscription_field = next(
            (field for field in self.fields if field.site_path == ("subscription",)),
            None,
        )
        for resource in resource_refs:
            binding = resource.field.resource
            if binding is None or binding.subscription != "site":
                continue
            if site_subscription_field is None:
                raise GuidedInputError("A Site subscription input is required by this contract.")
            site_subscription = active_values.get(site_subscription_field.name)
            if not isinstance(site_subscription, str):
                raise GuidedInputError(
                    f"Resource input '{resource.field.name}' requires a Site subscription."
                )
            if site_subscription.casefold() != resource.ref.subscription.casefold():
                raise ResourceInputError(
                    "subscription-mismatch",
                    f"Resource input '{resource.field.name}' conflicts with the Site subscription.",
                )
        derivable = {
            target for bound_resource in resource_refs
            for _, target in bound_resource.field.resource.derive
        }
        selected_roles = {resource.field.name for resource in resource_refs}
        for field in self.fields:
            if field.resource is None:
                continue
            if (
                field.when is not None
                and active_values.get(field.when.input) != field.when.equals
            ):
                continue
            for check in field.resource.requires:
                if (
                    check.when is None
                    or active_values.get(check.when.input) == check.when.equals
                ) and field.name not in selected_roles:
                    raise ResourceInputError(
                        "requirement-unverified",
                        f"Resource input '{field.name}' must be read for '{check.fact}'.",
                    )
        for name in missing_required:
            if name not in active_values and name not in derivable:
                raise GuidedInputError(f"Missing required input '{name}'.")
        return BoundInputs(
            MappingProxyType(active_values),
            MappingProxyType(sources),
            tuple(resource_refs),
        )

    def build_site(
        self,
        bound: BoundInputs,
        observations: Mapping[str, ArmResourceObservation] | None = None,
    ) -> Site:
        """Construct one ordinary Site after all selected resource reads succeed."""
        resource_names = {resource.field.name for resource in bound.resources}
        if resource_names and observations is None:
            raise GuidedInputError(
                "Resource ID inputs require `--read-resources` on inputs, plan or deploy."
            )
        if set(observations or {}) != resource_names:
            raise GuidedInputError("The selected resource observations are incomplete or unexpected.")
        values = dict(bound.active_values)
        sources = dict(bound.sources)
        data = copy.deepcopy(self._site_defaults)
        for resource in bound.resources:
            from siteops.arm_resources import ArmResourceError, validate_arm_observation

            field = resource.field
            observation = observations[field.name]
            try:
                validate_arm_observation(resource.ref, observation)
            except ArmResourceError as error:
                raise ResourceInputError(
                    "invalid-observation",
                    f"Resource input '{field.name}' returned invalid metadata ({error.code}).",
                ) from None
            for requirement in field.resource.requires:
                if requirement.fact in resource.required_facts and (
                    observation.facts.get(requirement.fact) is not True
                ):
                    raise ResourceInputError(
                        "requirement-unmet",
                        f"Resource input '{field.name}' requirement "
                        f"'{requirement.fact}' was not reported by Azure.",
                    )
            for fact, target_name in field.resource.derive:
                observed = (
                    observation.location if fact == "location"
                    else observation.resource_id if fact == "id"
                    else {
                        "subscription": resource.ref.subscription,
                        "resourceGroup": resource.ref.resource_group,
                        "name": resource.ref.name,
                    }[fact]
                )
                existing = values.get(target_name)
                if existing is not None and not _same_observed_value(fact, existing, observed):
                    raise ResourceInputError(
                        "conflict",
                        f"Input '{target_name}' ({sources[target_name]}) conflicts "
                        f"with resource input '{field.name}'.",
                    )
                if existing is None:
                    values[target_name] = observed
                    sources[target_name] = f"resource '{field.name}'"
            if field.site_path is not None:
                _assign(data, field.site_path, observation.resource_id)
        for field in self.fields:
            if field.resource is not None:
                continue
            if field.name not in values:
                if field.required and (
                    field.when is None
                    or values.get(field.when.input) == field.when.equals
                ):
                    raise GuidedInputError(f"Missing required input '{field.name}'.")
                continue
            if field.site_path is None:
                raise GuidedInputError("A mapped input needs a Site field.")
            _assign(data, field.site_path, values[field.name])
        return Site.from_data(data, source="guided inputs", default_name="guided-site")

    def resolve(
        self, values_file: Path | None = None, inline: list[str] | None = None,
    ) -> Site:
        """Build a Site from local file and inline answers."""
        return self.build_site(self.bind(values_file=values_file, inline=inline))


def _assign(data: dict[str, Any], path: tuple[str, ...], value: str | bool) -> None:
    current = data
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise GuidedInputError(
                f"Cannot map input to {'.'.join(path)}: '{part}' is not a mapping in siteDefaults."
            )
        current = child
    current[path[-1]] = value


def load_contract(
    manifest_path: Path, *, binding: MaterializedPackageBinding | None = None,
) -> InputContract | None:
    """Load this manifest's contract, if present; verify acquired bytes before reading."""
    path = contract_path(manifest_path)
    if binding is not None:
        binding.validate()
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    if binding is not None:
        verified, _ = binding.require_workspace_file(path)
    else:
        verified = path
    document = _mapping(_read_yaml(verified, label="Input contract"), "Input contract")
    if binding is not None:
        binding.require_workspace_file(path)
    _shape(
        document,
        allowed={"apiVersion", "kind", "siteDefaults", "inputs"},
        required={"apiVersion", "kind", "inputs"},
        label="Input contract",
    )
    if document["apiVersion"] != _VERSION:
        raise GuidedInputError("Input contract has an unsupported apiVersion.")
    if document["kind"] != "SiteInputContract":
        raise GuidedInputError("Input contract kind must be SiteInputContract.")
    defaults = _site_defaults(document.get("siteDefaults", {}))
    fields = _parse_fields(document["inputs"])
    _validate_default_writers(defaults, fields)
    return InputContract(fields, defaults)


def load_direct_site(path: Path) -> Site:
    """Read a bounded standalone Site, without access to configured inheritance."""
    data = _mapping(_read_yaml(path, label="Site file"), "Site file")
    if "inherits" in data or (
        isinstance(data.get("spec"), dict) and "inherits" in data["spec"]
    ):
        raise GuidedInputError("A direct Site file cannot use inherits; provide a complete Site.")
    if "apiVersion" in data and not isinstance(data["apiVersion"], str):
        raise GuidedInputError("Site file apiVersion must be text.")
    if data.get("kind") is not None and not isinstance(data["kind"], str):
        raise GuidedInputError("Site file kind must be text.")
    try:
        _validate_resource(data, "Site", path)
    except ValueError:
        raise GuidedInputError("Site file has an unsupported apiVersion or kind.") from None
    return Site.from_data(data, source=path, default_name=path.stem)


def write_yaml_exclusive(path: Path, data: dict[str, Any]) -> None:
    """Write UTF-8 YAML only to a new file in an existing directory."""
    _mapping(data, "YAML output")
    try:
        raw = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, yaml.YAMLError):
        raise GuidedInputError("YAML output must contain only supported plain data.") from None
    if len(raw) > _MAX_YAML_BYTES:
        raise GuidedInputError("YAML output exceeds the 128 KiB limit.")
    write_new(path, raw)

"""Specifications and discovery helpers shared by the catalog family tests.

A catalog deployment family is a directory of Bicep under
`templates/aio/<family>/` and a partial the catalog entry point includes. One
family may serve several public resource areas and definition directories.
Every family carries the same authoring hazards, because its templates pass
each entry's `properties` to the resource provider unchanged: Bicep type-checks
nothing inside `properties`, an unknown key at any other level is dropped by
name filtering, and both failures deploy clean.

The contracts those hazards need are held once, in
`test_catalog_family_contracts.py`, parameterized over `CATALOG_FAMILIES` below.
A family registers one `FamilySpec` here and inherits all of them. What stays in
a family's own module is what only that family means: for dataflows, that a
reference resolves to something declared, and that an endpoint type carries the
settings object it names.

This module is deliberately not named `test_*`, so pytest imports it as a plain
module rather than collecting it. Test modules import from here rather than from
each other.

A spec carries enough for a family whose shape differs from dataflows:

- Its API version parameter, since a family may route on `adrApiVersion` rather
  than on `aioApiVersion`.
- Its parent resource type, since a family may create resources under a
  namespace rather than under the AIO instance.
- Its name rules, at family level and overridable per kind, since resource
  providers do not agree on what a name may contain.
- Its probe fields, since a resource type whose ARM definition requires a
  top-level property other than `name` and `properties` needs that property
  present before Bicep will type-check the rest.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from siteops.composition import load_contract
from siteops.models import Manifest, ParameterSource

# The catalog entry point. A family is wired by having a declaration path here.
CATALOG_MANIFEST = "manifests/aio-resources.yaml"
CATALOG_PARTIAL = "manifests/_aio-resources.yaml"
CATALOG_CONTRACT = "contracts/aio-catalog.yaml"
_RESOURCE_SET_EXPRESSION = re.compile(
    r"\{\{\s*site\.properties\.resourceSets\.(?P<key>[\w-]+)\s*\}\}"
)

# A declaration may carry a site variable anywhere, including in a name. A name
# rule applies to the resolved value, so a name is checked with each variable
# replaced by a representative site name. Site names follow the same shape, so a
# template that resolves cleanly for this one resolves for any.
SITE_VARIABLE = re.compile(r"\{\{[^}]*\}\}")
SAMPLE_SITE_NAME = "seattle-dev"

# The keys an entry may carry before a kind adds its own. Nothing types this
# level, so an unknown key here is read by no template and discarded.
# `properties` reaches the resource provider untyped, and the compile probe owns
# what is inside it.
COMMON_ENTRY_KEYS = frozenset({"name", "properties"})

# The name pattern the AIO resource provider's own ARM types declare, which Bicep
# reports as BCP416 on a literal. Bicep applies it to a literal leaf name, but a
# declaration supplies names through a parameter array, which Bicep cannot
# inspect. A resource whose name is built as a fully-qualified multi-segment
# string gets no pattern check from Bicep at all.
AIO_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# The longest name the resource provider accepts, which Bicep reports as BCP332
# on a literal. A composed name grows with the site values it interpolates, so
# this is checked against the resolved name rather than the declared one.
AIO_MAX_NAME_LENGTH = 63

# The default shortest name, meaning "the family states no minimum". Bicep
# reports a name below a provider's minimum as BCP333 on a literal, and a
# declaration's names are again out of its reach. A family whose provider
# publishes a minimum states it rather than inheriting this.
NO_MINIMUM_NAME_LENGTH = 1

# ARM accepts a broader device name shape, including uppercase and a trailing
# hyphen. The device is projected to Kubernetes under the same name, where
# metadata names must be lowercase and end in an alphanumeric character. Hold
# declarations to the intersection so an ARM-successful device reaches the edge.
ADR_DEVICE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
ADR_MIN_NAME_LENGTH = 3
ADR_MAX_NAME_LENGTH = 63


@dataclass(frozen=True)
class KindSpec:
    """One resource kind a family deploys.

    Everything a kind needs travels together, so a kind cannot be half
    registered: the declaration key, which is also the entry-point array
    parameter, the ARM resource type the modules write, the keys an entry may
    carry, the name the instance template already owns, and the properties the
    resource provider requires.

    `required_properties` pairs a property name with why it matters, so a
    failure names the consequence rather than only the missing key.

    `name_pattern`, `min_name_length`, `max_name_length`, and `probe_fields` are
    overrides. Left unset, the family's own values apply. A kind whose resource
    provider constrains names differently, or whose ARM type requires a
    top-level property the rest of the family does not, sets them here rather
    than forking the checks.
    """

    key: str
    resource_type: str
    entry_keys: frozenset[str]
    required_properties: tuple[tuple[str, str], ...] = ()
    name_pattern: re.Pattern[str] | None = None
    min_name_length: int | None = None
    max_name_length: int | None = None
    probe_fields: tuple[tuple[str, str], ...] = ()
    resource_set_key: str | None = None
    parameters_dir: str | None = None


@dataclass(frozen=True)
class FamilySpec:
    """One catalog family, and everything the shared contracts need to check it.

    `module_prefix`, `template_dir`, `parameters_dir`, and `resource_set_key`
    default to `family`, which is what the dataflow family does. A kind whose
    public resource area differs overrides the latter two on `KindSpec`.

    `probe_fields` maps a top-level resource property to the Bicep source the
    compile probe emits for it. `name` and `properties` are always emitted, so
    this is for a resource type whose ARM definition requires something else
    before it will type-check the properties object, such as an
    `extendedLocation`. The value is Bicep source rather than a Python value,
    since a probe field is usually a small object literal written once.
    """

    family: str
    api_version_param: str
    parent_resource_type: str
    kinds: tuple[KindSpec, ...]
    name_pattern: re.Pattern[str] = AIO_NAME_PATTERN
    min_name_length: int = NO_MINIMUM_NAME_LENGTH
    max_name_length: int = AIO_MAX_NAME_LENGTH
    module_prefix: str | None = None
    template_dir: str | None = None
    parameters_dir: str | None = None
    resource_set_key: str | None = None
    probe_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for attribute in ("module_prefix", "template_dir", "parameters_dir", "resource_set_key"):
            if getattr(self, attribute) is None:
                object.__setattr__(self, attribute, self.family)

    @property
    def kind_keys(self) -> tuple[str, ...]:
        """Declaration keys, which are also the entry point's array parameters."""
        return tuple(kind.key for kind in self.kinds)

    @property
    def resource_set_keys(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                kind.resource_set_key or self.resource_set_key
                for kind in self.kinds
            )
        )

    def parameters_dir_for(self, kind: KindSpec) -> str:
        return kind.parameters_dir or self.parameters_dir

    def kind(self, key: str) -> KindSpec:
        for kind in self.kinds:
            if kind.key == key:
                return kind
        raise KeyError(f"{self.family} declares no kind '{key}'")

    def name_rules(self, kind: KindSpec) -> tuple[re.Pattern[str], int, int]:
        """The pattern and the length bounds that apply to one kind's names."""
        return (
            kind.name_pattern or self.name_pattern,
            kind.min_name_length or self.min_name_length,
            kind.max_name_length or self.max_name_length,
        )

    def probe_fields_for(self, kind: KindSpec) -> tuple[tuple[str, str], ...]:
        """Top-level probe properties for one kind, with the kind's own last.

        A kind-level field of the same name replaces the family-level one, so a
        family sets what every kind shares and a kind states only its difference.
        """
        merged = dict(self.probe_fields)
        merged.update(dict(kind.probe_fields))
        return tuple(merged.items())


# AIO creates a `default` endpoint and a `default` profile alongside the
# instance, so a reference to one resolves without the declaration supplying it,
# and a declaration must not claim the name.
DATAFLOWS = FamilySpec(
    family="dataflows",
    api_version_param="aioApiVersion",
    parent_resource_type="Microsoft.IoTOperations/instances",
    kinds=(
        KindSpec(
            key="dataflowEndpoints",
            resource_type="Microsoft.IoTOperations/instances/dataflowEndpoints",
            entry_keys=COMMON_ENTRY_KEYS,
            required_properties=(
                ("endpointType", "which the resource provider requires"),
            ),
        ),
        KindSpec(
            key="dataflowProfiles",
            resource_type="Microsoft.IoTOperations/instances/dataflowProfiles",
            entry_keys=COMMON_ENTRY_KEYS,
        ),
        KindSpec(
            key="dataflows",
            resource_type="Microsoft.IoTOperations/instances/dataflowProfiles/dataflows",
            entry_keys=COMMON_ENTRY_KEYS | {"profileRef"},
            required_properties=(
                (
                    "operations",
                    "which the resource provider requires and which is the pipeline itself",
                ),
            ),
        ),
    ),
)

# Devices and assets are Azure Device Registry resources created under the ADR
# namespace the instance is bound to, not under the instance, so this family
# routes on `adrApiVersion` and reads a different parent. Nothing under the
# namespace is created alongside the instance, so no name here is instance owned.
ASSETS = FamilySpec(
    family="assets",
    api_version_param="adrApiVersion",
    parent_resource_type="Microsoft.DeviceRegistry/namespaces",
    min_name_length=ADR_MIN_NAME_LENGTH,
    max_name_length=ADR_MAX_NAME_LENGTH,
    kinds=(
        KindSpec(
            key="devices",
            resource_type="Microsoft.DeviceRegistry/namespaces/devices",
            entry_keys=COMMON_ENTRY_KEYS,
            # The projectable subset is narrower than the ARM name pattern.
            name_pattern=ADR_DEVICE_NAME_PATTERN,
            resource_set_key="devices",
            parameters_dir="devices",
        ),
        KindSpec(
            key="assets",
            resource_type="Microsoft.DeviceRegistry/namespaces/assets",
            entry_keys=COMMON_ENTRY_KEYS,
            required_properties=(
                (
                    "deviceRef",
                    "which names the device and inbound endpoint the asset reads "
                    "through, and which the resource provider requires",
                ),
            ),
            resource_set_key="assets",
            parameters_dir="assets",
        ),
    ),
    # A device and an asset both require `location` and `extendedLocation`, and
    # Bicep reports a missing required property rather than type-checking what is
    # present, so the probe supplies both before it can check `properties`.
    probe_fields=(
        ("location", "'eastus2'"),
        ("extendedLocation", "{ type: 'CustomLocation', name: 'probe-custom-location' }"),
    ),
)

# Every family the shared contracts run against. A family added here inherits
# all of them. `TestTheFamilyRegistryIsComplete` holds this against what the
# catalog manifest actually wires, so a family cannot be deployed unregistered
# and a spec cannot outlive the family it describes.
CATALOG_FAMILIES: tuple[FamilySpec, ...] = (ASSETS, DATAFLOWS)


def family_spec(name: str) -> FamilySpec:
    for spec in CATALOG_FAMILIES:
        if spec.family == name:
            return spec
    raise KeyError(f"No catalog family spec registered for '{name}'")


def family_id(spec: FamilySpec) -> str:
    """Parameterization id, so a failure names the family rather than an index."""
    return spec.family


@lru_cache(maxsize=None)
def load_yaml(path: Path):
    """Parse a workspace YAML file, returning None for anything unreadable.

    Cached because discovery re-reads every YAML in the workspace and most
    checks need the parsed result. The committed workspace is read-only for the
    duration of a run, and the `workspace` fixture yields one constant path.
    """
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def entries(data, key: str) -> list[dict]:
    """The mapping entries a declaration carries under one kind key."""
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def family_dir(workspace: Path, spec: FamilySpec) -> Path:
    return workspace / "templates" / "aio" / spec.template_dir


def entry_point(workspace: Path, spec: FamilySpec) -> Path:
    return family_dir(workspace, spec) / "main.bicep"


def parameter_dirs(workspace: Path, spec: FamilySpec) -> tuple[Path, ...]:
    """Distinct selectable directories that feed one deployment family."""
    return tuple(
        workspace / "parameters" / directory
        for directory in dict.fromkeys(
            spec.parameters_dir_for(kind) for kind in spec.kinds
        )
    )


def version_module(workspace: Path, spec: FamilySpec, api_version: str) -> Path:
    return family_dir(workspace, spec) / "modules" / f"{spec.module_prefix}-{api_version}.bicep"


def supported_api_versions(workspace: Path, spec: FamilySpec) -> list[str]:
    """API generations the family entry point routes on, in declared order."""
    text = entry_point(workspace, spec).read_text(encoding="utf-8")
    match = re.search(
        rf"@allowed\(\[([^\]]*)\]\)\s*param\s+{re.escape(spec.api_version_param)}\s+string",
        text,
    )
    assert match, (
        f"templates/aio/{spec.template_dir}/main.bicep does not constrain "
        f"{spec.api_version_param}, so a release the family has no module for "
        f"would create nothing and report success."
    )
    versions = re.findall(r"'([^']+)'", match.group(1))
    assert versions, (
        f"templates/aio/{spec.template_dir}/main.bicep constrains "
        f"{spec.api_version_param} to an empty set."
    )
    return versions


def entry_point_array_params(workspace: Path, spec: FamilySpec) -> set[str]:
    """Array parameters the family entry point accepts, which are its kinds."""
    text = entry_point(workspace, spec).read_text(encoding="utf-8")
    return set(re.findall(r"^\s*param\s+(\w+)\s+array\b", text, re.MULTILINE))


@lru_cache(maxsize=None)
def _declaration_files(workspace: Path, family: str) -> tuple[Path, ...]:
    spec = family_spec(family)
    matches: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        for path in workspace.rglob(pattern):
            if not path.is_file():
                continue
            data = load_yaml(path)
            if not isinstance(data, dict):
                continue
            metadata = data.get("_siteops")
            external = (
                metadata.get("external", {})
                if isinstance(metadata, dict)
                else {}
            )
            if any(key in data or key in external for key in spec.kind_keys):
                matches.append(path)
    return tuple(sorted(matches))


def declaration_files(workspace: Path, spec: FamilySpec) -> tuple[Path, ...]:
    """Every committed file that declares resources for one family.

    Walks the workspace and selects structurally, so a declaration added under
    `samples/` or `parameters/<area>/` is picked up without a list being
    maintained here. Both YAML extensions are matched, since a file named `.yml`
    would otherwise be skipped with no signal.
    """
    return _declaration_files(workspace, spec.family)


def declared_entry_count(workspace: Path, spec: FamilySpec) -> int:
    """Total entries across every discovered declaration for one family."""
    return sum(
        len(entries(load_yaml(path) or {}, key))
        for path in declaration_files(workspace, spec)
        for key in spec.kind_keys
    )


def declarations_by_family(workspace: Path) -> list[tuple[FamilySpec, tuple[Path, ...]]]:
    """Each registered family paired with the declarations it owns."""
    return [(spec, declaration_files(workspace, spec)) for spec in CATALOG_FAMILIES]


def resolved_declarations(workspace: Path, orchestrator, spec: FamilySpec) -> dict[Path, dict]:
    """Resolve one family's declarations for every site that could deploy them.

    All definition files, not only the sets under `parameters/`, since a sample
    attaches one the same way and its variables resolve the same way.

    All committed sites, not only those the catalog's own selector matches. A
    selector is overridable from the CLI, so any site can be handed any
    declaration, and a variable that resolves for the dev sites while leaving a
    literal `{{ ... }}` on another is the failure this exists to expose.
    """
    sites = orchestrator.load_all_sites()
    assert sites, "No sites loaded from the workspace."

    resolutions: dict[Path, dict] = {}
    for set_file in declaration_files(workspace, spec):
        declaration = load_yaml(set_file) or {}
        resolutions[set_file] = {
            site.name: orchestrator._resolve_template_strings(declaration, site)
            for site in sites
        }
    return resolutions


def manifest_resource_sets(workspace: Path) -> dict[str, tuple[str, ...]]:
    """Map each public resource-set key to the collections its source permits."""
    manifest = Manifest.from_file(
        workspace / CATALOG_MANIFEST,
        workspace_root=workspace,
    )
    result: dict[str, tuple[str, ...]] = {}
    for source in manifest.parameters:
        if not isinstance(source, ParameterSource) or source.for_each is None:
            continue
        match = _RESOURCE_SET_EXPRESSION.fullmatch(source.for_each)
        if not match:
            continue
        result[match.group("key")] = source.collections
    assert result, (
        f"No resource-set parameter sources found in {CATALOG_MANIFEST}. If "
        "the selection mechanism changed, update this helper rather than "
        "deleting it."
    )
    return result


def composition_contract(workspace: Path):
    return load_contract(workspace / CATALOG_CONTRACT)


def template_family_dirs(workspace: Path) -> list[str]:
    """Family directories present on disk, discovered by their composing template."""
    return sorted(p.parent.name for p in (workspace / "templates" / "aio").glob("*/main.bicep"))


def resolved_name_candidate(name: str) -> str:
    """A declared name with every site variable replaced by a sample site name."""
    return SITE_VARIABLE.sub(SAMPLE_SITE_NAME, name)


def bicep_literal(value, indent: int = 1) -> str:
    """Render a Python value as a Bicep literal.

    Bicep is not JSON. Strings are single quoted, object keys are bare when they
    are valid identifiers, and members are newline separated rather than comma
    separated. Emitting JSON here produces a parse error rather than the type
    check the compile probe exists for.
    """
    pad = "  " * indent
    closing = "  " * (indent - 1)

    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for key, item in value.items():
            if re.fullmatch(r"[A-Za-z_]\w*", str(key)):
                rendered_key = str(key)
            else:
                # A quoted key is still a Bicep string, so it needs the same
                # escaping a string value gets. An apostrophe in an unescaped key
                # terminates the key early and the probe fails to parse.
                rendered_key = bicep_literal(str(key), indent)
            lines.append(f"{pad}{rendered_key}: {bicep_literal(item, indent + 1)}")
        lines.append(f"{closing}}}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            lines.append(f"{pad}{bicep_literal(item, indent + 1)}")
        lines.append(f"{closing}]")
        return "\n".join(lines)

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Bicep has no float type, so a fractional literal is a parse error rather
        # than a type error. Raising names the declaration instead of surfacing as
        # an unrelated syntax diagnostic against the generated probe.
        raise ValueError(
            f"Cannot render {value!r} as a Bicep literal. Bicep has no floating "
            f"point type, so a declaration cannot carry a fractional number."
        )

    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("${", "\\${")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"

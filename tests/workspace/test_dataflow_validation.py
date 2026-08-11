"""Workspace tests for the dataflow catalog declaration contract.

The dataflow templates pass each entry's `properties` to the resource
provider unchanged, which keeps the declaration in AIO's own vocabulary and
lets a new provider property flow through without a template edit. The cost
is that Bicep type-checks nothing inside `properties`, so a malformed
declaration compiles clean and fails at deploy.

Committed declarations are held to the contract here instead:

- Names are unique per resource kind, and no declaration claims `default`
  for a profile, which the instance template owns.
- Every `endpointRef` resolves to a declared endpoint or to `default`, and
  every `profileRef` resolves to a declared profile or to `default`. A
  mistyped reference deploys clean and never moves data.
- Each entry carries the fields the resource provider requires.
- `TestDeclarationCompiles` renders every declaration as a typed Bicep
  literal at the pinned API version and compiles it, so a property that does
  not exist at that generation fails here rather than at ARM. That test is
  the active trigger for introducing per-version dispatch: when a
  declaration needs a property a newer generation added, this is what names
  it.
"""

import difflib
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import yaml

from siteops.models import Manifest
from tests.workspace.conftest import az_path


# Resource kinds the catalog deploys. One entry per kind, so a family added to
# this map cannot be half-registered: the declaration key, the ARM resource type,
# the template that writes it, and the name the instance template already owns all
# travel together. Keep them in one map rather than in parallel ones, so a kind
# cannot be added to one and missed in another, which narrows coverage silently.
class _Kind:
    __slots__ = ("resource_type", "instance_owned_name", "entry_keys")

    def __init__(
        self,
        resource_type: str,
        instance_owned_name: str | None,
        entry_keys: frozenset[str],
    ):
        self.resource_type = resource_type
        self.instance_owned_name = instance_owned_name
        self.entry_keys = entry_keys


# AIO creates these alongside the instance, so a reference to one resolves without
# the declaration supplying it, and a declaration must not claim the name.
_INSTANCE_OWNED_ENDPOINT = "default"
_INSTANCE_OWNED_PROFILE = "default"

# The keys an entry may carry. Nothing types this level, so an unknown key here
# is read by no template and discarded. `properties` reaches the resource
# provider untyped, and the compile probe owns what is inside it.
_COMMON_ENTRY_KEYS = frozenset({"name", "properties"})

_KIND_SPECS = {
    "dataflowEndpoints": _Kind(
        "Microsoft.IoTOperations/instances/dataflowEndpoints",
        _INSTANCE_OWNED_ENDPOINT,
        _COMMON_ENTRY_KEYS,
    ),
    "dataflowProfiles": _Kind(
        "Microsoft.IoTOperations/instances/dataflowProfiles",
        _INSTANCE_OWNED_PROFILE,
        _COMMON_ENTRY_KEYS,
    ),
    "dataflows": _Kind(
        "Microsoft.IoTOperations/instances/dataflowProfiles/dataflows",
        None,
        _COMMON_ENTRY_KEYS | {"profileRef"},
    ),
}

# Views onto the one map above, so a kind cannot appear in one and not another.
_KINDS = {key: spec.resource_type for key, spec in _KIND_SPECS.items()}
_INSTANCE_OWNED_NAMES = {
    key: spec.instance_owned_name
    for key, spec in _KIND_SPECS.items()
    if spec.instance_owned_name
}

# Matches the near-miss threshold rationale in test_secretsync_validation.py.
_NEAR_MATCH_RATIO = 0.92

# The name pattern the resource provider's own ARM type declares, which Bicep
# reports as BCP416 on a literal. Bicep applies it to a literal leaf name, but a
# declaration supplies names through a parameter array, which Bicep cannot
# inspect. The dataflows resource also builds a fully-qualified three-segment
# name, and Bicep applies no pattern at all to a multi-segment name, so this is
# the only check a dataflow name gets.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# The longest name the resource provider accepts, which Bicep reports as BCP332
# on a literal. A composed name grows with the site values it interpolates, so
# this is checked against the resolved name rather than the declared one.
_MAX_NAME_LENGTH = 63

# A declaration may carry a site variable anywhere, including in a name. The
# pattern above applies to the resolved value, so a name is checked with each
# variable replaced by a representative site name. Site names follow the same
# shape, so a template that resolves cleanly for this one resolves for any.
_SITE_VARIABLE = re.compile(r"\{\{[^}]*\}\}")
_SAMPLE_SITE_NAME = "seattle-dev"

# `endpointType` is the discriminator that selects which settings shape the
# resource provider validates. Derived from the settings variants Bicep lists for
# `DataflowEndpointProperties`: dataExplorerSettings, dataLakeStorageSettings,
# fabricOneLakeSettings, kafkaSettings, localStorageSettings, mqttSettings,
# openTelemetrySettings.
_ENDPOINT_TYPES = frozenset(
    {
        "DataExplorer",
        "DataLakeStorage",
        "FabricOneLake",
        "Kafka",
        "LocalStorage",
        "Mqtt",
        "OpenTelemetry",
    }
)


def _settings_key(endpoint_type: str) -> str:
    """The settings property paired with an endpoint type.

    The provider names them mechanically, lowercasing the first character of
    the type and appending `Settings`.
    """
    return f"{endpoint_type[0].lower()}{endpoint_type[1:]}Settings"


@lru_cache(maxsize=None)
def _declaration_files(workspace: Path) -> tuple[Path, ...]:
    """Every committed file that declares dataflow catalog resources.

    Walks the workspace and selects structurally, so a declaration added
    under `samples/` or `parameters/dataflows/` is picked up without this
    list being maintained. Both YAML extensions are matched, since a file
    named `.yml` would otherwise be skipped with no signal.

    Cached because each call re-parses every YAML in the workspace and most
    tests here need the list. The committed workspace is read-only for the
    duration of a run, and the `workspace` fixture yields one constant path.
    """
    matches: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        for path in workspace.rglob(pattern):
            if not path.is_file():
                continue
            data = _load_yaml(path)
            if not isinstance(data, dict):
                continue
            if any(key in data for key in _KINDS):
                matches.append(path)
    return tuple(sorted(matches))


def _declared_entry_count(workspace: Path) -> int:
    """Total catalog entries across every discovered declaration."""
    return sum(
        len(_entries(_load_yaml(path) or {}, key))
        for path in _declaration_files(workspace)
        for key in _KINDS
    )


@lru_cache(maxsize=None)
def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _entries(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


def _family_dir(workspace: Path) -> Path:
    return workspace / "templates" / "aio" / "dataflows"


def _supported_api_versions(workspace: Path) -> list[str]:
    """API generations the family entry point routes on, in declared order."""
    text = (_family_dir(workspace) / "main.bicep").read_text(encoding="utf-8")
    match = re.search(r"@allowed\(\[([^\]]*)\]\)\s*param\s+aioApiVersion\s+string", text)
    assert match, "main.bicep does not constrain aioApiVersion, so a release the family has no module for would create nothing and report success."
    versions = re.findall(r"'([^']+)'", match.group(1))
    assert versions, "main.bicep constrains aioApiVersion to an empty set."
    return versions


def _version_module(workspace: Path, api_version: str) -> Path:
    return _family_dir(workspace) / "modules" / f"dataflows-{api_version}.bicep"


class TestDataflowDeclarationContract:
    """Committed dataflow declarations satisfy the authoring contract."""

    def test_at_least_one_declaration_entry_is_discovered(self, workspace):
        """Guard on entries, not files.

        `parameters/dataflows/none.yaml` declares empty arrays, so it always
        satisfies a file-level guard while contributing nothing to check. Every
        other test in this class iterates entries, so counting files would let
        all of them go vacuous if the only non-empty declaration moved.
        """
        files = _declaration_files(workspace)
        assert files, (
            "No workspace YAML declares dataflow catalog resources. If the "
            "sample or the parameters/dataflows sets moved, update the "
            "discovery filter."
        )
        count = _declared_entry_count(workspace)
        assert count > 0, (
            f"Discovered {len(files)} declaration file(s) but zero entries "
            f"across them, so every check in this class would pass without "
            f"examining anything. Files: {[str(f.name) for f in files]}"
        )

    def test_every_committed_set_is_discovered_as_a_declaration(self, workspace):
        """Files that a site can select are checked, not merely those that look right.

        Discovery is content-shaped: a file counts as a declaration because it
        already carries a known kind. That makes a file whose keys are *all*
        wrong invisible to every test here, while a deploy still loads it,
        drops the unrecognized keys by name filtering, and creates nothing on
        every site that selected it.

        Anything under a family's `parameters/` directory is selectable by a
        site, so it is checked by path rather than by what it happens to
        contain.
        """
        from tests.workspace.test_catalog_gating import _catalog_families

        discovered = {p.resolve() for p in _declaration_files(workspace)}
        missing: list[str] = []

        for family in _catalog_families(workspace):
            family_dir = workspace / "parameters" / family
            assert family_dir.is_dir(), (
                f"Catalog family '{family}' has no parameters/{family}/ "
                f"directory. If the layout changed, update this test."
            )
            for set_file in sorted(family_dir.glob("*.yaml")):
                if set_file.resolve() not in discovered:
                    missing.append(
                        f"{set_file.relative_to(workspace)} is selectable by a "
                        f"site but declares none of the kinds this family "
                        f"deploys ({sorted(_KINDS)}), so no check in this "
                        f"module reads it. A deploy would drop its keys and "
                        f"create nothing."
                    )

        assert not missing, "\n".join(missing)

    def test_names_are_unique_per_kind(self, workspace):
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for key in _KINDS:
                seen: dict[str, int] = {}
                for i, entry in enumerate(_entries(data, key)):
                    name = entry.get("name")
                    if not name:
                        failures.append(
                            f"{path}: {key}[{i}] has no `name`. Every catalog "
                            f"entry needs a name, which is the resource name "
                            f"and what a reference resolves against."
                        )
                        continue
                    if name in seen:
                        failures.append(
                            f"{path}: {key} name '{name}' appears at indices "
                            f"{seen[name]} and {i}. Two entries with one name "
                            f"deploy as a single resource, and which one wins "
                            f"depends on template loop order."
                        )
                        continue
                    seen[name] = i
        assert not failures, "\n\n".join(failures)

    def test_kind_specs_match_the_family_entry_point(self, workspace):
        """The kinds this file checks are the kinds the family deploys.

        `_KIND_SPECS` is hand-maintained. A resource kind added to the family
        templates and missed here loses every check in this module silently,
        and a declaration key that no template accepts is dropped by name
        filtering and deploys nothing while reporting success.
        """
        entry_point = workspace / "templates" / "aio" / "dataflows" / "main.bicep"
        accepted = set(
            re.findall(r"^\s*param\s+(\w+)\s+array\b", entry_point.read_text(encoding="utf-8"), re.MULTILINE)
        )
        assert accepted, f"{entry_point.name} declares no array parameters."

        declared = set(_KIND_SPECS)
        assert declared == accepted, (
            "The declaration kinds this module checks disagree with what the "
            "family entry point accepts.\n"
            f"  Checked but not accepted: {sorted(declared - accepted)}\n"
            f"  Accepted but not checked: {sorted(accepted - declared)}"
        )

    def test_every_supported_version_has_a_module_declaring_every_kind(self, workspace):
        """A generation's module writes all three kinds at its own version.

        A module that omitted a kind would deploy a fraction of a declaration on
        the releases routed to it, silently and only on those releases. A module
        carrying a stray literal from the generation it was copied from would
        write that kind at the wrong version, which no compile check catches
        because both versions are real.
        """
        failures: list[str] = []
        for api_version in _supported_api_versions(workspace):
            module = _version_module(workspace, api_version)
            if not module.is_file():
                failures.append(
                    f"main.bicep allows '{api_version}' but "
                    f"{module.name} does not exist, so a site on that release "
                    f"would deploy nothing."
                )
                continue

            text = module.read_text(encoding="utf-8")
            for key, spec in _KIND_SPECS.items():
                pattern = rf"resource\s+\w+\s+'{re.escape(spec.resource_type)}@([\d-]+(?:-preview)?)'"
                found = re.findall(pattern, text)
                if not found:
                    failures.append(f"{module.name} declares no '{key}' resource.")
                    continue
                wrong = [v for v in found if v != api_version]
                if wrong:
                    failures.append(
                        f"{module.name} writes '{key}' at {wrong}, but the "
                        f"module is the {api_version} generation. A literal was "
                        f"missed when this module was copied."
                    )
        assert not failures, "\n".join(failures)

    def test_every_module_reports_the_generation_it_writes(self, workspace):
        """A module's reported generation matches the version it writes at.

        The family entry point reads this output rather than echoing its own
        input, so a deploy reports the generation that actually ran. That only
        holds if each module reports itself honestly. A copied module keeping
        the source generation's literal reports one version while writing at
        another.

        The `existing` instance reference is checked with it, since reading the
        parent at a different generation is the divergence the platform layer
        routes for.
        """
        failures: list[str] = []
        for api_version in _supported_api_versions(workspace):
            module = _version_module(workspace, api_version)
            if not module.is_file():
                continue
            text = module.read_text(encoding="utf-8")

            reported = re.findall(
                r"output\s+deployedApiVersion\s+string\s*=\s*'([^']+)'", text
            )
            if reported != [api_version]:
                failures.append(
                    f"{module.name} reports deployedApiVersion {reported}, but "
                    f"it is the {api_version} generation. The entry point "
                    f"reports this value as the generation that ran."
                )

            instance = re.findall(
                r"resource\s+\w+\s+'Microsoft\.IoTOperations/instances@([\d-]+(?:-preview)?)'\s+existing",
                text,
            )
            # Asserted as equality rather than by looking for wrong entries. A
            # reference that stopped being `existing`, or moved, matches nothing
            # and would otherwise pass with an empty result.
            if instance != [api_version]:
                failures.append(
                    f"{module.name} reads the instance at {instance}, but writes "
                    f"at {api_version}. The module needs exactly one `existing` "
                    f"instance reference, at its own generation, since read and "
                    f"write route on the same version."
                )
        assert not failures, "\n".join(failures)

    def test_every_module_reads_every_entry_key(self, workspace):
        """A module reads exactly the entry keys the declaration contract allows.

        The contract has two halves. `test_every_entry_key_is_one_a_template_reads`
        holds the declaration side, that a set uses only keys a template reads.
        This holds the template side, that the templates still read them.

        Without it a module can stop honoring `profileRef` while every offline
        check passes, and the family entry point still reports the declared
        profile in `dataflowProfileRefs`, so the deploy names a placement it did
        not make. A module serves one generation, so a live run on a single
        release would not show it either.
        """
        failures: list[str] = []
        for api_version in _supported_api_versions(workspace):
            module = _version_module(workspace, api_version)
            if not module.is_file():
                continue
            text = module.read_text(encoding="utf-8")

            for kind, spec in _KIND_SPECS.items():
                loop = re.search(rf"for\s+(\w+)\s+in\s+{kind}\s*:", text)
                if not loop:
                    failures.append(
                        f"{module.name} has no loop over '{kind}', so the "
                        f"declaration keys it reads cannot be checked."
                    )
                    continue
                read = set(re.findall(rf"\b{loop.group(1)}\.\??(\w+)", text))
                if read != spec.entry_keys:
                    failures.append(
                        f"{module.name} reads {sorted(read)} from each '{kind}' "
                        f"entry, but the contract is {sorted(spec.entry_keys)}. "
                        f"Missing: {sorted(spec.entry_keys - read)}. "
                        f"Unexpected: {sorted(read - spec.entry_keys)}."
                    )
        assert not failures, "\n".join(failures)

    def test_the_reported_profile_is_the_one_the_name_is_built_from(self, workspace):
        """The entry point reports placement using the expression it deploys with.

        `dataflowProfileRefs` documents itself as the same expression the
        resource name is built from. Two copies of an expression can drift, and
        a report that no longer matches the deploy is worse than no report.
        """
        main = (_family_dir(workspace) / "main.bicep").read_text(encoding="utf-8")
        module = _version_module(
            workspace, _supported_api_versions(workspace)[0]
        ).read_text(encoding="utf-8")

        placement = r"dataflow\.\?profileRef\s*\?\?\s*defaultProfileName"
        assert re.search(placement, main), (
            "main.bicep no longer reports the profile as "
            "`dataflow.?profileRef ?? defaultProfileName`, so its "
            "dataflowProfileRefs output may not describe where a dataflow "
            "was placed."
        )
        assert re.search(placement, module), (
            "A generation module no longer builds the dataflow name from "
            "`dataflow.?profileRef ?? defaultProfileName`, so the profile the "
            "entry point reports is not the one the resource is created under."
        )

    def test_the_default_profile_is_the_one_the_instance_owns(self, workspace):
        """The fallback profile names a profile that exists without being declared.

        A dataflow entry may omit `profileRef`, and the templates then place it
        in the profile AIO creates alongside the instance. A typo in that
        default sends every such dataflow to a profile nothing creates, which
        includes the worked example the documentation points at.

        Checked in the entry point and in every module, since each carries its
        own copy of the default.
        """
        templates = [_family_dir(workspace) / "main.bicep"] + [
            _version_module(workspace, version)
            for version in _supported_api_versions(workspace)
        ]
        failures: list[str] = []
        for template in templates:
            if not template.is_file():
                continue
            defaults = re.findall(
                r"param\s+defaultProfileName\s+string\s*=\s*'([^']*)'",
                template.read_text(encoding="utf-8"),
            )
            if not defaults:
                failures.append(
                    f"{template.name} declares no defaultProfileName default, so "
                    f"an entry omitting profileRef has no profile to fall back to."
                )
                continue
            wrong = [d for d in defaults if d != _INSTANCE_OWNED_PROFILE]
            if wrong:
                failures.append(
                    f"{template.name} falls back to {wrong}, but the profile the "
                    f"instance owns is '{_INSTANCE_OWNED_PROFILE}'. A dataflow "
                    f"declaring no profileRef would name a profile nothing creates."
                )
        assert not failures, "\n".join(failures)

    def test_the_generation_modules_differ_only_in_their_version(self, workspace):
        """Every generation module is the same template at a different version.

        The family has no per-generation behavior, so a module that drifts from
        its siblings is a copy edit rather than an intended difference. Any
        drift is visible as a diff against the first module once each file's
        own version literal is normalized.

        This is what catches a fix applied to one module and not the others, on
        a family where a live run exercises one generation.
        """
        modules = [
            (version, _version_module(workspace, version))
            for version in _supported_api_versions(workspace)
        ]
        present = [(v, p) for v, p in modules if p.is_file()]
        assert len(present) > 1, (
            "Fewer than two generation modules were found, so parity cannot be "
            "checked. If the family stopped dispatching, update this test."
        )

        def _normalized(version: str, path: Path) -> list[str]:
            text = path.read_text(encoding="utf-8").replace(version, "<version>")
            return text.splitlines()

        base_version, base_path = present[0]
        base = _normalized(base_version, base_path)
        failures: list[str] = []
        for version, path in present[1:]:
            current = _normalized(version, path)
            if current == base:
                continue
            diff = list(
                difflib.unified_diff(
                    base, current, base_path.name, path.name, lineterm="", n=1
                )
            )
            failures.append(
                f"{path.name} differs from {base_path.name} beyond its version "
                f"literal:\n" + "\n".join(f"    {line}" for line in diff[:24])
            )
        assert not failures, "\n".join(failures)

    def test_every_declared_key_is_a_kind_some_template_deploys(self, workspace):
        """A declaration file carries only keys a family actually deploys.

        A misspelled or invented key is dropped by name-based parameter
        filtering, so the deploy succeeds and creates nothing for it.
        """
        known = set(_KIND_SPECS)
        failures: list[str] = []

        for path in _declaration_files(workspace):
            data = _load_yaml(path) or {}
            for key, value in data.items():
                if not isinstance(value, list):
                    continue
                if key not in known:
                    suggestion = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.7)
                    hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
                    failures.append(
                        f"{path.name} declares '{key}', which no catalog "
                        f"template deploys.{hint} Known kinds: {sorted(known)}"
                    )

        assert not failures, "\n".join(failures)

    def test_every_declared_kind_is_a_list_of_entries(self, workspace):
        """A kind key carries a list of mappings, not some other shape.

        `_entries` returns nothing for a value that is not a list and drops
        items that are not mappings, so a kind written as a mapping, left with
        no value, or given a bare string makes every per-entry check below pass
        vacuously. The declaration then reaches the template as `null` or as an
        object where an array belongs, and ARM rejects it after the fleet has
        already been dispatched.

        Generic across kinds, so a family adding one inherits the check.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path) or {}
            for key in _KIND_SPECS:
                if key not in data:
                    continue
                value = data[key]
                where = f"{path.relative_to(workspace)} '{key}'"
                if value is None:
                    failures.append(
                        f"{where} has no value, which reaches the template as "
                        f"null rather than as an empty array. Write it as `[]` "
                        f"or remove the key."
                    )
                    continue
                if not isinstance(value, list):
                    failures.append(
                        f"{where} is a {type(value).__name__} where a list of "
                        f"entries belongs, so every check on it is skipped. "
                        f"Each entry is a `- name:` item."
                    )
                    continue
                for i, entry in enumerate(value):
                    if not isinstance(entry, dict):
                        failures.append(
                            f"{where}[{i}] is a {type(entry).__name__} where a "
                            f"mapping belongs, so it is skipped by every check. "
                            f"Each entry needs at least a `name`."
                        )
        assert not failures, "\n".join(failures)

    def test_every_entry_key_is_one_a_template_reads(self, workspace):
        """An entry carries only keys the family template reads.

        Nothing types this level. A template reads `name`, `properties`, and
        the references its own kind defines, and an unknown key is discarded in
        silence. `profileref` for `profileRef` is the costly shape: the
        dataflow is created under the instance-owned profile while the profile
        the operator declared is created empty, and every offline check passes.

        `properties` is deliberately not descended into. It reaches the
        resource provider untyped and the compile probe owns what is inside it.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path) or {}
            for key, spec in _KIND_SPECS.items():
                for entry in _entries(data, key):
                    for entry_key in entry:
                        if entry_key in spec.entry_keys:
                            continue
                        suggestion = difflib.get_close_matches(
                            entry_key, sorted(spec.entry_keys), n=1, cutoff=0.7
                        )
                        hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
                        failures.append(
                            f"{path.relative_to(workspace)} '{key}' entry "
                            f"'{entry.get('name', '?')}' declares '{entry_key}', "
                            f"which no template reads, so it is discarded.{hint} "
                            f"Keys for this kind: {sorted(spec.entry_keys)}"
                        )
        assert not failures, "\n".join(failures)

    def test_every_workspace_yaml_is_well_formed(self, workspace):
        """Every YAML file declaration discovery walks parses cleanly.

        Discovery is content-shaped: it reads each YAML in the workspace and
        keeps the ones carrying a known kind. `_load_yaml` returns `None` for a
        file it cannot parse, so a declaration with a syntax error is not
        reported as broken, it is simply not discovered, and every check in this
        module goes quiet on it. A duplicated key behaves the same way, keeping
        the last block and dropping the first.

        Asserted over the whole walk rather than over discovered files, since a
        file that fails to parse is exactly the one discovery cannot see. Uses
        the engine's loader, so committed content is held to the rule the engine
        applies at deploy time.
        """
        from siteops import yamlio

        failures: list[str] = []
        for pattern in ("*.yaml", "*.yml"):
            for path in sorted(workspace.rglob(pattern)):
                if not path.is_file():
                    continue
                try:
                    yamlio.load(path.read_text(encoding="utf-8"))
                except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
                    # The whole message, indented. A duplicate key reports the
                    # key, the consequence, and both positions across several
                    # lines, and the first line alone is only the context.
                    detail = "\n".join(
                        f"    {line}" for line in str(exc).strip().splitlines()
                    )
                    failures.append(f"{path.relative_to(workspace)}:\n{detail}")
        assert not failures, "\n".join(failures)

    def test_no_declaration_carries_a_secret_looking_value(self, workspace):
        """A declaration is committed content, so it must not carry a secret.

        `properties` passes to the resource provider untyped, so a declaration
        can hold any value and the template parameter is not `@secure()`. A
        secret placed here would sit in version control and reach deployment
        logs. Values a site supplies stay templates here and resolve at deploy
        time, which is the supported route for anything sensitive.

        Keys are matched with the same substrings the CLI uses to redact site
        output, so the two surfaces agree on what counts as a secret.
        """
        from siteops.cli import _is_sensitive_key

        failures: list[str] = []

        def _walk(node, path: str, source: Path) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    here = f"{path}.{key}" if path else str(key)
                    if (
                        _is_sensitive_key(str(key))
                        and isinstance(value, str)
                        and "{{" not in value
                    ):
                        failures.append(
                            f"{source.name}: '{here}' holds a literal value "
                            f"under a secret-named key. Reference a site value "
                            f"with a template so it resolves at deploy time."
                        )
                    _walk(value, here, source)
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    _walk(item, f"{path}[{i}]", source)

        for path in _declaration_files(workspace):
            _walk(_load_yaml(path) or {}, "", path)

        assert not failures, "\n".join(failures)

    def test_names_match_the_resource_provider_pattern(self, workspace):
        """A declared name is a resource name, and the provider constrains its shape.

        Bicep enforces this pattern on a literal name, but a declaration supplies
        names through a parameter array that Bicep cannot inspect, and
        the dataflows resource builds a fully-qualified three-segment name, which
        Bicep does not pattern-check at all. A `/` in a name would silently add a
        segment to that path. An uppercase or underscored name is accepted by
        some tooling and rejected when the resource projects to the cluster.

        A name carrying a site variable is checked with the variable replaced by
        a representative site name, since the pattern applies to the resolved
        value rather than the template.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for key in _KINDS:
                for i, entry in enumerate(_entries(data, key)):
                    name = entry.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    candidate = _SITE_VARIABLE.sub(_SAMPLE_SITE_NAME, name)
                    if not _NAME_PATTERN.match(candidate):
                        detail = (
                            f" (resolves to '{candidate}')" if candidate != name else ""
                        )
                        failures.append(
                            f"{path.name}: {key}[{i}] is named '{name}'{detail}, "
                            f"which does not match {_NAME_PATTERN.pattern}. Use "
                            f"lowercase letters, digits, and hyphens, starting "
                            f"and ending with a letter or digit."
                        )
        assert not failures, "\n\n".join(failures)

    def test_no_declaration_claims_an_instance_owned_name(self, workspace):
        """`default` endpoints and profiles are created by the instance template.

        Reusing one of those names makes two templates full-PUT the same
        resource, so each deploy silently discards what the other set. For the
        endpoint that is worse than a lost setting: every dataflow sourcing
        `endpointRef: default` stops moving data, and the value flaps as install
        and dataflow deploys overwrite each other in turn.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for key, owned in _INSTANCE_OWNED_NAMES.items():
                for i, entry in enumerate(_entries(data, key)):
                    if entry.get("name") == owned:
                        failures.append(
                            f"{path}: {key}[{i}] is named '{owned}', which the "
                            f"AIO instance template creates and owns. Name the "
                            f"resource for its workload instead, and set any "
                            f"instance-level sizing on the site."
                        )
        assert not failures, "\n\n".join(failures)

    def test_endpoint_and_profile_references_resolve(self, workspace):
        """A reference to a name nothing declares deploys clean and never runs.

        References resolve against the declarations in the same file plus the
        resources AIO creates with the instance. That is the right scope while
        one file is one attachment unit, which
        `test_no_manifest_attaches_two_declarations_for_one_family` holds true.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            endpoints = {e["name"] for e in _entries(data, "dataflowEndpoints") if e.get("name")}
            endpoints.add(_INSTANCE_OWNED_ENDPOINT)
            profiles = {p["name"] for p in _entries(data, "dataflowProfiles") if p.get("name")}
            profiles.add(_INSTANCE_OWNED_PROFILE)

            for i, flow in enumerate(_entries(data, "dataflows")):
                profile_ref = flow.get("profileRef", _INSTANCE_OWNED_PROFILE)
                if profile_ref not in profiles:
                    failures.append(
                        f"{path}: dataflows[{i}] ('{flow.get('name')}') sets "
                        f"profileRef '{profile_ref}', which no declaration in "
                        f"this file provides. Known profiles: {sorted(profiles)}."
                    )
                for op in flow.get("properties", {}).get("operations", []) or []:
                    if not isinstance(op, dict):
                        continue
                    for settings_key in ("sourceSettings", "destinationSettings"):
                        settings = op.get(settings_key)
                        if not isinstance(settings, dict):
                            continue
                        ref = settings.get("endpointRef")
                        if ref is not None and ref not in endpoints:
                            failures.append(
                                f"{path}: dataflows[{i}] "
                                f"('{flow.get('name')}') {settings_key} "
                                f"references endpoint '{ref}', which no "
                                f"declaration in this file provides. Known "
                                f"endpoints: {sorted(endpoints)}."
                            )
        assert not failures, "\n\n".join(failures)

    def test_no_manifest_attaches_two_declarations_for_one_family(self, workspace):
        """One declaration file is one attachment unit.

        Reference resolution above checks each file on its own, which is correct
        only while a manifest attaches at most one declaration per family. Two
        attached files would let a dataflow in one reference an endpoint in the
        other, and that reference would stop being checked while every test here
        stayed green.

        A templated path such as `parameters/dataflows/{{ ... }}.yaml` selects
        one of a set, so it is expanded to every file it can resolve to and
        counted once. Comparing the literal template would make this guard blind
        to the catalog manifest, which is the primary route.
        """
        declarations = {
            str(path.relative_to(workspace)).replace("\\", "/")
            for path in _declaration_files(workspace)
        }
        failures: list[str] = []

        for manifest_path in sorted(workspace.rglob("*.yaml")):
            raw = _load_yaml(manifest_path)
            if not isinstance(raw, dict) or raw.get("kind") != "Manifest":
                continue
            try:
                manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            except Exception:
                continue

            attached: list[str] = []
            declared_paths = list(manifest.parameters or [])
            for step in manifest.steps:
                declared_paths.extend(getattr(step, "parameters", []) or [])

            for entry in declared_paths:
                if "{{" in entry:
                    # A path variable selects one of a set. Any of them is one
                    # attachment, so the whole pattern counts once.
                    pattern = _SITE_VARIABLE.sub("*", entry)
                    if any(
                        str(p.relative_to(workspace)).replace("\\", "/") in declarations
                        for p in workspace.glob(pattern)
                    ):
                        attached.append(entry)
                elif entry in declarations:
                    attached.append(entry)

            if len(attached) > 1:
                failures.append(
                    f"{manifest_path.relative_to(workspace)} attaches "
                    f"{len(attached)} declaration files: {attached}. Reference "
                    f"resolution checks one file at a time, so a cross-file "
                    f"reference would go unchecked. Merge them, or widen "
                    f"test_endpoint_and_profile_references_resolve to resolve "
                    f"across everything a manifest attaches."
                )

        assert not failures, "\n\n".join(failures)

    def test_required_fields_are_present(self, workspace):
        """Each kind carries what the resource provider requires.

        `endpointType` on an endpoint and `operations` on a dataflow are
        required by the API. A profile requires nothing, so it is checked only
        for a properties object.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for key in _KINDS:
                for i, entry in enumerate(_entries(data, key)):
                    props = entry.get("properties")
                    if not isinstance(props, dict):
                        failures.append(
                            f"{path}: {key}[{i}] ('{entry.get('name')}') has no "
                            f"`properties` object. The template passes this "
                            f"through to the resource provider."
                        )
                        continue
                    if key == "dataflowEndpoints" and not props.get("endpointType"):
                        failures.append(
                            f"{path}: dataflowEndpoints[{i}] "
                            f"('{entry.get('name')}') has no `endpointType`, "
                            f"which the resource provider requires."
                        )
                    if key == "dataflows" and not props.get("operations"):
                        failures.append(
                            f"{path}: dataflows[{i}] ('{entry.get('name')}') "
                            f"has no `operations`, which the resource provider "
                            f"requires and which is the pipeline itself."
                        )
        assert not failures, "\n\n".join(failures)

    def test_endpoint_types_are_real_and_carry_matching_settings(self, workspace):
        """`endpointType` selects which settings shape the provider validates.

        Bicep types `properties` as `DataflowEndpointProperties` and rejects an
        unknown property inside it, but only once `endpointType` names a real
        variant. An endpoint whose `endpointType` is `NotARealEndpointType`
        compiles with no diagnostic at all, so a typo there turns off validation
        for everything else in that entry.

        The paired settings key is checked with it. `endpointType: Mqtt`
        alongside `kafkaSettings` names a variant whose settings are absent,
        which the compile probe cannot catch either.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for i, entry in enumerate(_entries(data, "dataflowEndpoints")):
                props = entry.get("properties")
                if not isinstance(props, dict):
                    continue
                endpoint_type = props.get("endpointType")
                if not endpoint_type:
                    continue
                label = f"{path}: dataflowEndpoints[{i}] ('{entry.get('name')}')"

                if endpoint_type not in _ENDPOINT_TYPES:
                    failures.append(
                        f"{label} declares endpointType '{endpoint_type}', "
                        f"which is not one of {sorted(_ENDPOINT_TYPES)}. An "
                        f"unknown value compiles clean and stops the resource "
                        f"provider's own property validation from running."
                    )
                    continue

                settings_key = _settings_key(endpoint_type)
                if settings_key not in props:
                    failures.append(
                        f"{label} declares endpointType '{endpoint_type}' but "
                        f"carries no `{settings_key}`. The endpoint would be "
                        f"created with none of the settings that type needs."
                    )
        assert not failures, "\n\n".join(failures)

    def test_no_near_miss_names(self, workspace):
        """Names close enough to suggest a typo are worth a second look.

        A mistyped endpoint name paired with a matching mistyped reference
        deploys clean and moves nothing, so name similarity is the earliest
        signal available.
        """
        failures: list[str] = []
        for path in _declaration_files(workspace):
            data = _load_yaml(path)
            for key in _KINDS:
                names = sorted({e["name"] for e in _entries(data, key) if e.get("name")})
                if len(names) < 2:
                    continue
                for name in names:
                    others = [n for n in names if n != name]
                    for match in difflib.get_close_matches(
                        name, others, n=len(others), cutoff=_NEAR_MATCH_RATIO
                    ):
                        if match < name:
                            continue
                        failures.append(
                            f"{path}: {key} names '{name}' and '{match}' are "
                            f"near-matches (difflib ratio >= {_NEAR_MATCH_RATIO}). "
                            f"Rename one if the similarity is accidental."
                        )
        assert not failures, "\n\n".join(failures)



# `WARNING: <file>(LINE,COL) : Warning BCP037: ...`
_DIAGNOSTIC = re.compile(r"\((\d+),\d+\)\s*:\s*(Error|Warning)\s+([\w-]+):\s*(.*)$")

# Diagnostics that are safe to ignore in the probe. Everything else fails,
# including any code Bicep adds later.
#
# The probe is a generated file containing only resource declarations, so linter
# rules about parameters, outputs, and interpolation cannot fire. Keeping this
# list empty until something proves benign means a new diagnostic class fails
# loudly rather than being silently discarded.
_IGNORABLE_CODES: frozenset[str] = frozenset()

# Bicep emits this at Warning severity when it has no type definition for a
# resource type and API version pair, and then skips property validation
# entirely. Treating it as anything but fatal would let this whole test pass
# while checking nothing, which is the defect class scripts/validate-bicep.ps1
# exists to prevent.
_NO_TYPES_CODE = "BCP081"


def _bicep_literal(value, indent: int = 1) -> str:
    """Render a Python value as a Bicep literal.

    Bicep is not JSON. Strings are single quoted, object keys are bare when they
    are valid identifiers, and members are newline separated rather than comma
    separated. Emitting JSON here produces a parse error rather than the type
    check this test exists for.
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
                rendered_key = _bicep_literal(str(key), indent)
            lines.append(f"{pad}{rendered_key}: {_bicep_literal(item, indent + 1)}")
        lines.append(f"{closing}}}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            lines.append(f"{pad}{_bicep_literal(item, indent + 1)}")
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


class TestDeclarationCompiles:
    """Every committed declaration is valid at the API version the templates pin.

    The templates take `properties` as an untyped object, so Bicep validates
    nothing inside it. Rendering each declaration as a typed literal and
    compiling it restores that check. A property that exists only in a newer API
    generation fails here by name, which is the signal to introduce a per-version
    module for that one resource rather than to discover the gap during a deploy.

    Every entry is emitted into one file and compiled once. Bicep compilation is
    the expensive part of this suite, so the count of `az` invocations stays at
    one no matter how many declarations the workspace grows.
    """

    def _resource_block(self, symbol: str, resource_type: str, api_version: str, props: dict) -> str:
        """Render one declaration entry as a Bicep resource.

        The symbolic name and resource name are unique placeholders, so many
        entries of one type coexist in a single file. Only `properties` is under
        test, and it is emitted as a literal so Bicep type-checks it.
        `extendedLocation` is optional on these types, so omitting it is clean.
        """
        levels = len(resource_type.split("/")) - 1
        placeholder = "/".join(f"{symbol}s{i}" for i in range(levels))
        literal = _bicep_literal(props)
        return (
            f"resource {symbol} '{resource_type}@{api_version}' = {{\n"
            f"  name: '{placeholder}'\n"
            f"  properties: {literal}\n"
            f"}}\n"
        )

    def _build_probe(self, workspace: Path) -> tuple[str, list[tuple[int, int, str]]]:
        """Return the combined probe source and a line range per entry.

        Every declaration is rendered at every generation the family supports,
        not only at one. A declaration file is shared fleet-wide and any site may
        select it, so it has to be valid at each generation a site could be
        running. Rendering only the newest would pass here and fail live on a
        site that has not upgraded.

        The ranges let a diagnostic reported against the combined file be traced
        back to the declaration file, entry, and generation that produced it.
        """
        source_lines: list[str] = []
        spans: list[tuple[int, int, str]] = []
        counter = 0

        for api_version in _supported_api_versions(workspace):
            for path in _declaration_files(workspace):
                data = _load_yaml(path)
                for key, spec in _KIND_SPECS.items():
                    for i, entry in enumerate(_entries(data, key)):
                        props = entry.get("properties")
                        if not isinstance(props, dict):
                            continue
                        block = self._resource_block(
                            f"probe{counter}", spec.resource_type, api_version, props
                        )
                        start = len(source_lines) + 1
                        source_lines.extend(block.splitlines())
                        spans.append(
                            (
                                start,
                                len(source_lines),
                                f"{path.name}: {key}[{i}] ('{entry.get('name')}') "
                                f"at {api_version}",
                            )
                        )
                        counter += 1

        return "\n".join(source_lines) + "\n", spans

    def test_declared_properties_exist_at_every_supported_api_version(self, workspace):
        source, spans = self._build_probe(workspace)
        assert spans, "No declaration entries were rendered, so nothing was checked"

        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "declarations.bicep"
            probe.write_text(source, encoding="utf-8")
            # Build to a discarded outfile rather than `--stdout`. Compiled ARM
            # JSON goes to the console under `--stdout`, and `az` raises on
            # content the console encoding cannot represent, so a declaration
            # carrying a non-Latin-1 character would fail as a tooling error.
            result = subprocess.run(
                [
                    az_path(),
                    "bicep",
                    "build",
                    "--file",
                    str(probe),
                    "--outfile",
                    str(Path(tmp) / "declarations.json"),
                ],
                capture_output=True,
                text=True,
            )

        failures: list[str] = []
        untyped: list[str] = []
        for line in result.stderr.splitlines():
            match = _DIAGNOSTIC.search(line)
            if not match:
                continue
            reported_line, severity, code, message = match.groups()
            label = next(
                (lbl for start, end, lbl in spans if start <= int(reported_line) <= end),
                f"line {reported_line}",
            )
            if code == _NO_TYPES_CODE:
                untyped.append(f"{label}: {message.strip()}")
                continue
            # Fail on everything not explicitly cleared. Bicep reports an
            # unknown property under more than one code, emitting BCP089 ("Did
            # you mean ...?") instead of BCP037 when the name is close to a real
            # one, so a denylist of known property codes lets realistic typos
            # through.
            if code in _IGNORABLE_CODES:
                continue
            failures.append(f"{label}: {code} {message.strip()}")

        assert not untyped, (
            "Bicep has no type definitions for a pinned resource type and API version, "
            "so it skipped property validation entirely and this test checked nothing. "
            "Upgrade Bicep (`az bicep upgrade`) so it carries types for the pinned "
            "generation, or correct the pin.\n  " + "\n  ".join(untyped)
        )

        # A run that produced no parsed diagnostics AND failed is an `az` or
        # toolchain failure rather than a clean compile, so a bicep download
        # failure in CI fails here instead of reporting success.
        assert result.returncode == 0 or failures, (
            "`az bicep build` failed without emitting a parseable diagnostic, so "
            "nothing was validated.\n"
            f"exit={result.returncode}\n{result.stderr.strip()[:2000]}"
        )

        assert not failures, (
            "A committed declaration names a property the pinned API version does "
            "not have, or omits one it requires. Correct the declaration, or if the "
            "property exists only in a newer generation, introduce a per-version "
            "module for that resource and route the template through it.\n  "
            + "\n  ".join(failures)
        )

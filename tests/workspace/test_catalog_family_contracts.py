"""Contracts every catalog family satisfies, checked against each registered family.

A family passes its declarations to the resource provider as untyped
`properties`, which keeps the declaration in the provider's own vocabulary and
lets a new provider property flow through without a template edit. The cost is
that nothing type-checks a declaration on the way in, so a malformed one
compiles clean and fails at deploy, and an unknown key at any other level is
dropped by name filtering and creates nothing while the deploy reports success.

Every family carries those hazards identically, so the checks live here once and
run against each spec in `catalog_harness.CATALOG_FAMILIES`. A family registers
one spec and inherits all of them:

- Names are unique per kind, match the provider's name rules once resolved, and
  never claim a name the instance template already owns.
- A declaration carries only kinds a template deploys and only entry keys a
  module reads, and each module still reads every key the contract promises.
- Every generation the entry point allows has a module that writes every kind at
  that generation and reads its parent there.
- `TestDeclarationCompiles` renders every declaration as a typed Bicep literal at
  every supported generation and compiles it, so a property that does not exist
  at a generation fails here rather than at ARM.

What stays in a family's own module is what only that family means. For
dataflows that is reference resolution and endpoint types, held in
`test_dataflow_validation.py`.
"""

import difflib
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from siteops.composition import format_identity, resolve_identity
from siteops.models import Manifest, ParameterSource
from tests.workspace import catalog_harness as harness
from tests.workspace.catalog_harness import CATALOG_FAMILIES, family_id
from tests.workspace.conftest import az_path

# Matches the near-miss threshold rationale in test_secretsync_validation.py.
_NEAR_MATCH_RATIO = 0.92


class TestTheFamilyRegistryIsComplete:
    """The registry names exactly the families the workspace ships.

    Every contract below is parameterized from the registry, so an unregistered
    family is not checked at all while it still deploys to any site that selects
    a set for it. A stale spec is the mirror image: it keeps reporting green for
    a family that no longer exists, and the coverage it appears to provide is
    gone.
    """

    def test_the_registry_matches_the_families_the_catalog_wires(self, workspace):
        wired = harness.manifest_resource_sets(workspace)
        registered: dict[str, tuple[str, ...]] = {}
        for spec in CATALOG_FAMILIES:
            for key in spec.resource_set_keys:
                registered[key] = tuple(
                    kind.key
                    for kind in spec.kinds
                    if (kind.resource_set_key or spec.resource_set_key) == key
                )

        assert registered == wired, (
            "The catalog family registry disagrees with the resource-set sources in "
            f"{harness.CATALOG_MANIFEST}.\n"
            f"  Manifest: {wired}\n"
            f"  Registry: {registered}"
        )

    def test_the_registry_names_the_selection_key_each_family_is_gated_on(self, workspace):
        wired = set(harness.manifest_resource_sets(workspace))
        registered = {
            key
            for spec in CATALOG_FAMILIES
            for key in spec.resource_set_keys
        }
        assert registered == wired

    def test_the_registry_matches_the_family_templates_on_disk(self, workspace):
        """A template directory with no spec deploys unchecked.

        Discovery by layout is what `test_catalog_templates_compile.py` uses, so
        holding the registry against it keeps the two views of "what a family is"
        from drifting apart.
        """
        on_disk = set(harness.template_family_dirs(workspace))
        registered = {spec.template_dir for spec in CATALOG_FAMILIES}
        assert registered == on_disk, (
            "The catalog family registry disagrees with the family templates "
            "under templates/aio/*/main.bicep.\n"
            f"  On disk but unregistered: {sorted(on_disk - registered)}\n"
            f"  Registered but absent: {sorted(registered - on_disk)}"
        )


def test_every_workspace_yaml_is_well_formed(workspace):
    """Every YAML file declaration discovery walks parses cleanly.

    Discovery is content-shaped: it reads each YAML in the workspace and keeps
    the ones carrying a known kind. `load_yaml` returns `None` for a file it
    cannot parse, so a declaration with a syntax error is not reported as
    broken, it is simply not discovered, and every check in this module goes
    quiet on it. A duplicated key behaves the same way, keeping the last block
    and dropping the first.

    Asserted over the whole walk rather than over discovered files, since a file
    that fails to parse is exactly the one discovery cannot see. Uses the
    engine's loader, so committed content is held to the rule the engine applies
    at deploy time.

    Run once for the workspace rather than per family. The walk is family
    independent, and repeating it would only repeat the same failure.
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
                # The whole message, indented. A duplicate key reports the key,
                # the consequence, and both positions across several lines, and
                # the first line alone is only the context.
                detail = "\n".join(f"    {line}" for line in str(exc).strip().splitlines())
                failures.append(f"{path.relative_to(workspace)}:\n{detail}")
    assert not failures, "\n".join(failures)


def test_composition_engine_contains_no_aio_reference_vocabulary():
    root = Path(__file__).parents[2] / "siteops"
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("composition.py", "models.py", "orchestrator.py")
    )
    forbidden = (
        "deviceRef",
        "endpointRef",
        "profileRef",
        "Microsoft.IoTOperations",
        "Microsoft.DeviceRegistry",
    )
    found = [value for value in forbidden if value in source]
    assert not found, (
        "AIO reference vocabulary reached the generic Site Ops engine: "
        f"{found}"
    )


def test_external_only_resource_set_is_discovered(tmp_path):
    path = tmp_path / "parameters" / "devices" / "external-plant.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
_siteops:
  external:
    devices:
      - name: plant-opc
        reason: Managed elsewhere.
"""
    )
    harness._declaration_files.cache_clear()
    try:
        discovered = harness.declaration_files(tmp_path, harness.ASSETS)
    finally:
        harness._declaration_files.cache_clear()

    assert discovered == (path,)


@pytest.mark.parametrize("spec", CATALOG_FAMILIES, ids=family_id)
class TestFamilyDeclarationContract:
    """Committed declarations satisfy the authoring contract for their family."""

    def test_at_least_one_declaration_entry_is_discovered(self, workspace, spec):
        """Guard on entries, not files.

        Every other test in this class iterates entries, so counting files would
        let all of them go vacuous if each selectable source became empty.
        """
        files = harness.declaration_files(workspace, spec)
        assert files, (
            f"No workspace YAML declares {spec.family} catalog resources. If the "
            "sample or selectable set directories moved, update the discovery "
            "filter."
        )
        count = harness.declared_entry_count(workspace, spec)
        assert count > 0, (
            f"Discovered {len(files)} declaration file(s) for '{spec.family}' but "
            f"zero entries across them, so every check in this class would pass "
            f"without examining anything. Files: {[str(f.name) for f in files]}"
        )

    def test_every_committed_set_is_discovered_as_a_declaration(self, workspace, spec):
        """Files that a site can select are checked, not merely those that look right.

        Discovery is content-shaped: a file counts as a declaration because it
        already carries a known kind. That makes a file whose keys are *all*
        wrong invisible to every test here, while a deploy still loads it, drops
        the unrecognized keys by name filtering, and creates nothing on every
        site that selected it.

        Anything under a family's `parameters/` directory is selectable by a
        site, so it is checked by path rather than by what it happens to
        contain.
        """
        discovered = {p.resolve() for p in harness.declaration_files(workspace, spec)}
        missing: list[str] = []
        for family_dir in harness.parameter_dirs(workspace, spec):
            assert family_dir.is_dir(), (
                f"Catalog family '{spec.family}' has no "
                f"{family_dir.relative_to(workspace)}/ directory. If the "
                "layout changed, update the spec."
            )
            directory_name = family_dir.name
            allowed = sorted(
                kind.key
                for kind in spec.kinds
                if spec.parameters_dir_for(kind) == directory_name
            )
            for set_file in sorted(family_dir.glob("*.yaml")):
                if set_file.resolve() not in discovered:
                    missing.append(
                        f"{set_file.relative_to(workspace)} is selectable by a "
                        f"site but declares none of its resource-area kinds "
                        f"({allowed}), so no check in this module reads it."
                    )

        assert not missing, "\n".join(missing)

    def test_names_are_unique_per_kind(self, workspace, spec):
        failures: list[str] = []
        contract = harness.composition_contract(workspace)
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path)
            for key in spec.kind_keys:
                collection = contract.collections[key]
                seen: dict[tuple[str, ...], int] = {}
                for i, entry in enumerate(harness.entries(data, key)):
                    try:
                        identity = resolve_identity(
                            collection,
                            entry,
                            f"{path}: {key}[{i}]",
                        )
                    except ValueError as error:
                        failures.append(str(error))
                        continue
                    if identity in seen:
                        failures.append(
                            f"{path}: {format_identity(collection, identity)} "
                            f"appears at indices {seen[identity]} and {i}. "
                            "Two entries with one identity deploy as one "
                            "resource."
                        )
                    seen[identity] = i
        assert not failures, "\n\n".join(failures)

    def test_kind_specs_match_the_family_entry_point(self, workspace, spec):
        """The kinds a spec declares are the kinds the family deploys.

        A spec is hand-maintained. A resource kind added to the family templates
        and missed there loses every check in this module silently, and a
        declaration key that no template accepts is dropped by name filtering
        and deploys nothing while reporting success.
        """
        accepted = harness.entry_point_array_params(workspace, spec)
        assert accepted, (
            f"templates/aio/{spec.template_dir}/main.bicep declares no array "
            f"parameters."
        )

        declared = set(spec.kind_keys)
        assert declared == accepted, (
            f"The declaration kinds registered for '{spec.family}' disagree with "
            f"what the family entry point accepts.\n"
            f"  Checked but not accepted: {sorted(declared - accepted)}\n"
            f"  Accepted but not checked: {sorted(accepted - declared)}"
        )

    def test_every_supported_version_has_a_module_declaring_every_kind(self, workspace, spec):
        """A generation's module writes every kind at its own version.

        A module that omitted a kind would deploy a fraction of a declaration on
        the releases routed to it, silently and only on those releases. A module
        carrying a stray literal from the generation it was copied from would
        write that kind at the wrong version, which no compile check catches
        because both versions are real.
        """
        failures: list[str] = []
        for api_version in harness.supported_api_versions(workspace, spec):
            module = harness.version_module(workspace, spec, api_version)
            if not module.is_file():
                failures.append(
                    f"main.bicep allows '{api_version}' but {module.name} does "
                    f"not exist, so a site on that release would deploy nothing."
                )
                continue

            text = module.read_text(encoding="utf-8")
            for kind in spec.kinds:
                pattern = (
                    rf"resource\s+\w+\s+'{re.escape(kind.resource_type)}"
                    rf"@([\d-]+(?:-preview)?)'"
                )
                found = re.findall(pattern, text)
                if not found:
                    failures.append(f"{module.name} declares no '{kind.key}' resource.")
                    continue
                wrong = [v for v in found if v != api_version]
                if wrong:
                    failures.append(
                        f"{module.name} writes '{kind.key}' at {wrong}, but the "
                        f"module is the {api_version} generation. A literal was "
                        f"missed when this module was copied."
                    )
        assert not failures, "\n".join(failures)

    def test_every_module_reads_its_parent_at_the_generation_it_writes(
        self, workspace, spec
    ):
        """Each module reads and writes through one provider generation."""
        failures: list[str] = []
        for api_version in harness.supported_api_versions(workspace, spec):
            module = harness.version_module(workspace, spec, api_version)
            if not module.is_file():
                continue
            text = module.read_text(encoding="utf-8")

            parent = re.findall(
                rf"resource\s+\w+\s+'{re.escape(spec.parent_resource_type)}"
                rf"@([\d-]+(?:-preview)?)'\s+existing",
                text,
            )
            # Asserted as equality rather than by looking for wrong entries. A
            # reference that stopped being `existing`, or moved, matches nothing
            # and would otherwise pass with an empty result.
            if parent != [api_version]:
                failures.append(
                    f"{module.name} reads {spec.parent_resource_type} at "
                    f"{parent}, but writes at {api_version}. The module needs "
                    f"exactly one `existing` parent reference, at its own "
                    f"generation, since read and write route on the same version."
                )
        assert not failures, "\n".join(failures)

    def test_catalog_templates_expose_no_test_only_outputs(self, workspace, spec):
        """Catalog tests observe provider state rather than declaration echoes."""
        paths = [harness.entry_point(workspace, spec)]
        paths.extend(
            harness.version_module(workspace, spec, api_version)
            for api_version in harness.supported_api_versions(workspace, spec)
        )
        failures: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            outputs = re.findall(
                r"^\s*output\s+([A-Za-z_][A-Za-z0-9_]*)\b",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            if outputs:
                failures.append(
                    f"{path.name} exposes output(s) {outputs}. Catalog "
                    "integration tests must read provider state instead of "
                    "expanding the deployment contract for test assertions."
                )
        assert not failures, "\n".join(failures)

    def test_every_module_reads_every_entry_key(self, workspace, spec):
        """A module reads exactly the entry keys the declaration contract allows.

        The contract has two halves. `test_every_entry_key_is_one_a_template_reads`
        holds the declaration side, that a set uses only keys a template reads.
        This holds the template side, that the templates still read them.

        Without it a module can stop honoring an optional key such as
        `profileRef` while every structural check over resource presence still
        passes. A module serves one generation, so a live run on a different
        release would not show it either.
        """
        failures: list[str] = []
        for api_version in harness.supported_api_versions(workspace, spec):
            module = harness.version_module(workspace, spec, api_version)
            if not module.is_file():
                continue
            text = module.read_text(encoding="utf-8")

            for kind in spec.kinds:
                loop = re.search(rf"for\s+(\w+)\s+in\s+{kind.key}\s*:", text)
                if not loop:
                    failures.append(
                        f"{module.name} has no loop over '{kind.key}', so the "
                        f"declaration keys it reads cannot be checked."
                    )
                    continue
                read = set(re.findall(rf"\b{loop.group(1)}\.\??(\w+)", text))
                if read != kind.entry_keys:
                    failures.append(
                        f"{module.name} reads {sorted(read)} from each "
                        f"'{kind.key}' entry, but the contract is "
                        f"{sorted(kind.entry_keys)}. "
                        f"Missing: {sorted(kind.entry_keys - read)}. "
                        f"Unexpected: {sorted(read - kind.entry_keys)}."
                    )
        assert not failures, "\n".join(failures)

    def test_the_generation_modules_differ_only_in_their_version(self, workspace, spec):
        """Every generation module is the same template at a different version.

        A family with no per-generation behavior has modules that differ only in
        their version literal, so a module that drifts from its siblings is a
        copy edit rather than an intended difference. Any drift is visible as a
        diff against the first module once each file's own version literal is
        normalized.

        This is what catches a fix applied to one module and not the others, on
        a family where a live run exercises one generation.
        """
        modules = [
            (version, harness.version_module(workspace, spec, version))
            for version in harness.supported_api_versions(workspace, spec)
        ]
        present = [(v, p) for v, p in modules if p.is_file()]
        assert len(present) > 1, (
            f"Fewer than two generation modules were found for '{spec.family}', "
            f"so parity cannot be checked. If the family stopped dispatching, "
            f"update this test."
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
                difflib.unified_diff(base, current, base_path.name, path.name, lineterm="", n=1)
            )
            failures.append(
                f"{path.name} differs from {base_path.name} beyond its version "
                f"literal:\n" + "\n".join(f"    {line}" for line in diff[:24])
            )
        assert not failures, "\n".join(failures)

    def test_every_declared_key_is_a_kind_some_template_deploys(self, workspace, spec):
        """A declaration file carries only keys a family actually deploys.

        A misspelled or invented key is dropped by name-based parameter
        filtering, so the deploy succeeds and creates nothing for it.

        Known keys are taken across every registered family rather than from
        this one, so a file declaring two families' kinds is not reported as
        wrong by either.
        """
        known = {key for other in CATALOG_FAMILIES for key in other.kind_keys}
        failures: list[str] = []

        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path) or {}
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

    def test_every_declared_kind_is_a_list_of_entries(self, workspace, spec):
        """A kind key carries a list of mappings, not some other shape.

        `entries` returns nothing for a value that is not a list and drops items
        that are not mappings, so a kind written as a mapping, left with no
        value, or given a bare string makes every per-entry check below pass
        vacuously. The declaration then reaches the template as `null` or as an
        object where an array belongs, and ARM rejects it after the fleet has
        already been dispatched.
        """
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path) or {}
            for key in spec.kind_keys:
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

    def test_every_entry_key_is_one_a_template_reads(self, workspace, spec):
        """An entry carries only keys the family template reads.

        Nothing types this level. A template reads `name`, `properties`, and the
        references its own kind defines, and an unknown key is discarded in
        silence. `profileref` for `profileRef` is the costly shape: the dataflow
        is created under the instance-owned profile while the profile the
        operator declared is created empty, and every offline check passes.

        `properties` is deliberately not descended into. It reaches the resource
        provider untyped and the compile probe owns what is inside it.
        """
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path) or {}
            for kind in spec.kinds:
                for entry in harness.entries(data, kind.key):
                    for entry_key in entry:
                        if entry_key in kind.entry_keys:
                            continue
                        suggestion = difflib.get_close_matches(
                            entry_key, sorted(kind.entry_keys), n=1, cutoff=0.7
                        )
                        hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
                        failures.append(
                            f"{path.relative_to(workspace)} '{kind.key}' entry "
                            f"'{entry.get('name', '?')}' declares '{entry_key}', "
                            f"which no template reads, so it is discarded.{hint} "
                            f"Keys for this kind: {sorted(kind.entry_keys)}"
                        )
        assert not failures, "\n".join(failures)

    def test_no_declaration_carries_a_secret_looking_value(self, workspace, spec):
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

        for path in harness.declaration_files(workspace, spec):
            _walk(harness.load_yaml(path) or {}, "", path)

        assert not failures, "\n".join(failures)

    def test_names_match_the_resource_provider_rules(self, workspace, spec):
        """A declared name is a resource name, and the provider constrains its shape.

        Bicep enforces a name pattern on a literal name, but a declaration
        supplies names through a parameter array that Bicep cannot inspect, and
        a resource that builds a fully-qualified multi-segment name is not
        pattern-checked at all. A `/` in a name would silently add a segment to
        that path. An uppercase or underscored name is accepted by some tooling
        and rejected when the resource projects to the cluster.

        A name carrying a site variable is checked with the variable replaced by
        a representative site name, since the rules apply to the resolved value
        rather than to the template. Length is checked at both ends, since a
        provider that publishes a minimum rejects a two-character name as firmly
        as an over-long one. Every committed site renders the same declaration in
        `test_catalog_gating.py`, which is where a name that resolves differently
        per site is caught.
        """
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path)
            for kind in spec.kinds:
                pattern, min_length, max_length = spec.name_rules(kind)
                for i, entry in enumerate(harness.entries(data, kind.key)):
                    name = entry.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    candidate = harness.resolved_name_candidate(name)
                    detail = f" (resolves to '{candidate}')" if candidate != name else ""
                    if not pattern.match(candidate):
                        failures.append(
                            f"{path.name}: {kind.key}[{i}] is named "
                            f"'{name}'{detail}, which does not match "
                            f"{pattern.pattern}. Use lowercase letters, digits, "
                            f"and hyphens, starting and ending with a letter or "
                            f"digit."
                        )
                    elif len(candidate) < min_length:
                        failures.append(
                            f"{path.name}: {kind.key}[{i}] is named "
                            f"'{name}'{detail}, which is {len(candidate)} "
                            f"characters, under the {min_length} the resource "
                            f"provider requires."
                        )
                    elif len(candidate) > max_length:
                        failures.append(
                            f"{path.name}: {kind.key}[{i}] is named "
                            f"'{name}'{detail}, which is {len(candidate)} "
                            f"characters, over the {max_length} the resource "
                            f"provider allows."
                        )
        assert not failures, "\n\n".join(failures)

    def test_no_declaration_claims_an_instance_owned_name(self, workspace, spec):
        """Some names are created by the instance template rather than declared.

        Reusing one of those names makes two templates full-PUT the same
        resource, so each deploy silently discards what the other set. For a
        dataflow endpoint that is worse than a lost setting: every dataflow
        sourcing `endpointRef: default` stops moving data, and the value flaps as
        install and family deploys overwrite each other in turn.
        """
        contract = harness.composition_contract(workspace)
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path)
            for key in spec.kind_keys:
                collection = contract.collections[key]
                if not collection.seeds:
                    continue
                for i, entry in enumerate(harness.entries(data, key)):
                    try:
                        identity = resolve_identity(
                            collection,
                            entry,
                            f"{path}: {key}[{i}]",
                        )
                    except ValueError:
                        continue
                    if identity in collection.seeds:
                        failures.append(
                            f"{path}: {format_identity(collection, identity)} "
                            "is provider-owned. Name the resource for its "
                            "workload instead."
                        )
        assert not failures, "\n\n".join(failures)

    def test_manifest_declaration_sources_name_the_collections_they_may_write(
        self,
        workspace,
        spec,
    ):
        """Governed declarations use typed sources instead of plain strings."""
        declarations = {
            str(path.relative_to(workspace)).replace("\\", "/")
            for path in harness.declaration_files(workspace, spec)
        }
        failures: list[str] = []

        for manifest_path in sorted(workspace.rglob("*.yaml")):
            raw = harness.load_yaml(manifest_path)
            if not isinstance(raw, dict) or raw.get("kind") != "Manifest":
                continue
            try:
                manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            except Exception:
                continue

            for source in manifest.parameters:
                path = source.path if isinstance(source, ParameterSource) else source
                if "{{" in path:
                    pattern = harness.SITE_VARIABLE.sub("*", path)
                    pattern = pattern.replace("{{ item }}", "*")
                    if any(
                        str(p.relative_to(workspace)).replace("\\", "/") in declarations
                        for p in workspace.glob(pattern)
                    ):
                        if not isinstance(source, ParameterSource):
                            failures.append(
                                f"{manifest_path.relative_to(workspace)} "
                                f"attaches governed declaration '{path}' as a "
                                "plain string."
                            )
                        elif not set(source.collections) <= set(spec.kind_keys):
                            failures.append(
                                f"{manifest_path.relative_to(workspace)} source "
                                f"'{path}' names collections "
                                f"{source.collections} outside family "
                                f"'{spec.family}'."
                            )
                elif path in declarations and not isinstance(source, ParameterSource):
                    failures.append(
                        f"{manifest_path.relative_to(workspace)} attaches "
                        f"governed declaration '{path}' as a plain string."
                    )

        assert not failures, "\n\n".join(failures)

    def test_required_fields_are_present(self, workspace, spec):
        """Each kind carries what the resource provider requires.

        Every entry needs a `properties` object, which the template passes
        through untouched. What must be inside it is per kind, so a kind that
        requires nothing is checked only for the object.
        """
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path)
            for kind in spec.kinds:
                for i, entry in enumerate(harness.entries(data, kind.key)):
                    props = entry.get("properties")
                    if not isinstance(props, dict):
                        failures.append(
                            f"{path}: {kind.key}[{i}] ('{entry.get('name')}') has "
                            f"no `properties` object. The template passes this "
                            f"through to the resource provider."
                        )
                        continue
                    for required, reason in kind.required_properties:
                        if not props.get(required):
                            failures.append(
                                f"{path}: {kind.key}[{i}] "
                                f"('{entry.get('name')}') has no `{required}`, "
                                f"{reason}."
                            )
        assert not failures, "\n\n".join(failures)

    def test_no_near_miss_names(self, workspace, spec):
        """Names close enough to suggest a typo are worth a second look.

        A mistyped resource name paired with a matching mistyped reference
        deploys clean and moves nothing, so name similarity is the earliest
        signal available.
        """
        failures: list[str] = []
        for path in harness.declaration_files(workspace, spec):
            data = harness.load_yaml(path)
            for key in spec.kind_keys:
                names = sorted(
                    {e["name"] for e in harness.entries(data, key) if e.get("name")}
                )
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


@pytest.mark.parametrize("spec", CATALOG_FAMILIES, ids=family_id)
class TestDeclarationCompiles:
    """Every committed declaration is valid at each API version its family pins.

    The templates take `properties` as an untyped object, so Bicep validates
    nothing inside it. Rendering each declaration as a typed literal and
    compiling it restores that check. A property that exists only in a newer API
    generation fails here by name, which is the signal to introduce a
    per-version module for that one resource rather than to discover the gap
    during a deploy.

    Every entry of a family is emitted into one file and compiled once. Bicep
    compilation is the expensive part of this suite, so the count of `az`
    invocations stays at one per family no matter how many declarations that
    family grows.
    """

    def _resource_block(
        self,
        symbol: str,
        resource_type: str,
        api_version: str,
        props: dict,
        probe_fields: tuple[tuple[str, str], ...],
    ) -> str:
        """Render one declaration entry as a Bicep resource.

        The symbolic name and resource name are unique placeholders, so many
        entries of one type coexist in a single file. Only `properties` is under
        test, and it is emitted as a literal so Bicep type-checks it. Any other
        top-level property the ARM type requires comes from the spec's probe
        fields, since Bicep reports a missing required property rather than
        type-checking what is present.
        """
        levels = len(resource_type.split("/")) - 1
        placeholder = "/".join(f"{symbol}s{i}" for i in range(levels))
        extra = "".join(f"  {name}: {source}\n" for name, source in probe_fields)
        literal = harness.bicep_literal(props)
        return (
            f"resource {symbol} '{resource_type}@{api_version}' = {{\n"
            f"  name: '{placeholder}'\n"
            f"{extra}"
            f"  properties: {literal}\n"
            f"}}\n"
        )

    def _build_probe(
        self, workspace: Path, spec: harness.FamilySpec
    ) -> tuple[str, list[tuple[int, int, str]]]:
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

        for api_version in harness.supported_api_versions(workspace, spec):
            for path in harness.declaration_files(workspace, spec):
                data = harness.load_yaml(path)
                for kind in spec.kinds:
                    for i, entry in enumerate(harness.entries(data, kind.key)):
                        props = entry.get("properties")
                        if not isinstance(props, dict):
                            continue
                        block = self._resource_block(
                            f"probe{counter}",
                            kind.resource_type,
                            api_version,
                            props,
                            spec.probe_fields_for(kind),
                        )
                        start = len(source_lines) + 1
                        source_lines.extend(block.splitlines())
                        spans.append(
                            (
                                start,
                                len(source_lines),
                                f"{path.name}: {kind.key}[{i}] "
                                f"('{entry.get('name')}') at {api_version}",
                            )
                        )
                        counter += 1

        return "\n".join(source_lines) + "\n", spans

    def test_declared_properties_exist_at_every_supported_api_version(self, workspace, spec):
        source, spans = self._build_probe(workspace, spec)
        assert spans, (
            f"No '{spec.family}' declaration entries were rendered, so nothing "
            f"was checked"
        )

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

"""Tests for the catalog's family gating and per-family completeness.

`manifests/aio-resources.yaml` deploys a family only when the site selects a set
for it, so the gate is what keeps an unconfigured site from deploying every
family. Two properties matter:

- The gate has to evaluate the way the design intends. `_evaluate_condition`
  fails open, returning True for a malformed condition or a missing property, so
  a broken gate deploys a family to every site rather than none.
- A family has several pieces that must all be present. Adding one and missing a
  piece degrades quietly: a missing site key or set file makes the manifest
  invalid for every site, and an ungated include deploys unconditionally.
"""

import copy
import re
from pathlib import Path

import pytest
import yaml

from siteops.models import Manifest

CATALOG_MANIFEST = "manifests/aio-resources.yaml"

# `parameters/dataflows/{{ site.properties.resourceSets.dataflows }}.yaml`
_SET_PATH = re.compile(
    r"parameters/(?P<family>[\w-]+)/\{\{\s*site\.properties\.resourceSets\.(?P<key>[\w-]+)\s*\}\}"
)

# The value every site inherits, meaning "deploy nothing for this family".
_EMPTY_SET = "none"

# Step-level chaining every family step reads, carrying values resolved from an
# upstream step rather than derived by naming convention.
_CATALOG_CHAINING = "parameters/inputs/catalog.yaml"


@pytest.fixture
def selectable_site(orchestrator):
    """A site whose `resourceSets` a test may set, restored afterwards.

    `load_site` caches, so every test in this module receives the same `Site`
    object. A test that set a selection and left it there would change what the
    next one resolves, and the failure would depend on execution order.
    `tests/integration/conftest.py` restores the same way and for the same
    reason.
    """
    site = orchestrator.load_site("seattle-dev")
    original = copy.deepcopy(site.properties)
    site.properties.setdefault("resourceSets", {})
    try:
        yield site
    finally:
        site.properties.clear()
        site.properties.update(original)


def _unresolved_paths(value, path: str = "") -> list[str]:
    """Locate any `{{ ... }}` left in a resolved structure, by path."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_unresolved_paths(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found.extend(_unresolved_paths(item, f"{path}[{i}]"))
    elif isinstance(value, str) and "{{" in value:
        found.append(f"{path} = {value!r}")
    return found


def _catalog_families(workspace) -> dict[str, str]:
    """Map each family directory to the `resourceSets` key that selects it."""
    raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))
    families = {}
    for entry in raw.get("parameters") or []:
        match = _SET_PATH.search(entry) if isinstance(entry, str) else None
        if match:
            families[match.group("family")] = match.group("key")
    assert families, (
        f"No catalog family declaration paths found in {CATALOG_MANIFEST}. If "
        f"the selection mechanism changed, update this test rather than "
        f"deleting it."
    )
    return families


@pytest.fixture(scope="module")
def catalog_families(workspace) -> dict[str, str]:
    return _catalog_families(workspace)


class TestCatalogGateSemantics:
    """The gate deploys a family when a site selects a set, and not otherwise.

    `_evaluate_condition` fails open, so a gate that is subtly wrong is invisible
    at authoring time and shows up as every site deploying every family.
    """

    def _gated_steps(self, workspace):
        manifest = Manifest.from_file(workspace / CATALOG_MANIFEST, workspace_root=workspace)
        return [step for step in manifest.steps if step.when]

    def test_every_catalog_step_is_gated(self, workspace, catalog_families):
        """Only the resolve step runs unconditionally.

        An ungated family step deploys to every site, including sites that
        selected no set, which is the failure the gate exists to prevent.
        """
        manifest = Manifest.from_file(workspace / CATALOG_MANIFEST, workspace_root=workspace)
        ungated = [s.name for s in manifest.steps if not s.when]
        assert ungated == ["resolve-aio"], (
            "Every catalog family step must be gated on its `resourceSets` key. "
            f"Ungated steps: {ungated}"
        )

    def test_gate_skips_a_site_that_selected_no_set(self, workspace, orchestrator, selectable_site):
        """`none` evaluates false, so the family contributes no deployment."""
        site = selectable_site
        for family, key in _catalog_families(workspace).items():
            site.properties["resourceSets"][key] = _EMPTY_SET

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is False, (
                f"Step '{step.name}' would deploy for a site that selected "
                f"'{_EMPTY_SET}'. Its gate is `{step.when}`."
            )

    def test_gate_runs_a_site_that_selected_a_set(self, workspace, orchestrator, selectable_site):
        """A named set evaluates true, so the family deploys."""
        site = selectable_site
        for family, key in _catalog_families(workspace).items():
            site.properties["resourceSets"][key] = "some-committed-set"

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is True, (
                f"Step '{step.name}' would be skipped for a site that selected "
                f"a set. Its gate is `{step.when}`."
            )

    def test_gate_opens_when_the_key_is_absent(self, workspace, orchestrator, selectable_site):
        """An absent key opens the gate, so the entry point must attach the set file.

        `_evaluate_condition` reads a missing property as empty, and empty is not
        `none`, so a site carrying no `resourceSets` key selects the family rather
        than skipping it. Every committed site inherits the key from
        `base-site.yaml`, and the manifest-level declaration path names the same
        key, so `_require_selected_parameter_file` stops such a site before any
        family deploys. Both of those are load-bearing: a family whose entry point
        omits the declaration path would deploy to every site with template
        defaults. Asserting the polarity here keeps that dependency visible.
        """
        site = selectable_site
        site.properties["resourceSets"] = {}

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is True, (
                f"Step '{step.name}' no longer opens for a site missing its "
                f"`resourceSets` key. Its gate is `{step.when}`. If the gate now "
                f"closes instead, the manifest may no longer need to attach the "
                f"declaration file to catch this case."
            )


class TestCatalogFamilyCompleteness:
    """Every family carries all the pieces a family needs."""

    def test_every_family_ships_an_empty_set(self, workspace, catalog_families):
        missing = [
            family
            for family in catalog_families
            if not (workspace / "parameters" / family / f"{_EMPTY_SET}.yaml").is_file()
        ]
        assert not missing, (
            f"A family has no `{_EMPTY_SET}.yaml`. The declaration path resolves "
            f"for every site regardless of the gate, so a site that selected no "
            f"set would fail to resolve.\n{missing}"
        )

    def test_the_empty_set_declares_every_kind_as_empty(self, workspace, catalog_families):
        """`none.yaml` declares each of the family's kinds, each an empty list.

        Every site inherits this file and it loads regardless of the gate, so a
        real entry here deploys to the whole fleet. A key present but null, or
        a kind omitted entirely, is the quieter failure: the family's template
        falls back to its own default and the file stops meaning "nothing".

        The kinds come from the family template's array parameters, so adding a
        resource kind to a family without extending its empty set fails here.
        """
        array_param = re.compile(r"^\s*param\s+(\w+)\s+array\b", re.MULTILINE)
        failures: list[str] = []

        for family in catalog_families:
            entry_point = workspace / "templates" / "aio" / family / "main.bicep"
            if not entry_point.is_file():
                entry_point = workspace / "templates" / "aio" / family / "main.bicep"
            assert entry_point.is_file(), (
                f"No family entry point found for '{family}'. If the layout "
                f"changed, update this test rather than deleting it."
            )

            kinds = set(array_param.findall(entry_point.read_text(encoding="utf-8")))
            assert kinds, f"{entry_point.name} declares no array parameters."

            empty_set = workspace / "parameters" / family / f"{_EMPTY_SET}.yaml"
            declared = yaml.safe_load(empty_set.read_text(encoding="utf-8")) or {}

            for kind in sorted(kinds):
                if kind not in declared:
                    failures.append(
                        f"{empty_set.relative_to(workspace)} omits '{kind}', "
                        f"which {entry_point.name} accepts."
                    )
                elif declared[kind] != []:
                    failures.append(
                        f"{empty_set.relative_to(workspace)} sets '{kind}' to "
                        f"{declared[kind]!r}, expected an empty list. Every "
                        f"site inherits this file."
                    )

        assert not failures, "\n".join(failures)

    def test_base_site_declares_every_family_key(self, workspace, catalog_families):
        base = yaml.safe_load((workspace / "sites" / "base-site.yaml").read_text(encoding="utf-8"))
        declared = (base.get("properties") or {}).get("resourceSets") or {}
        missing = sorted(set(catalog_families.values()) - set(declared))
        assert not missing, (
            "base-site.yaml does not default every catalog family, so a site "
            "that never sets one cannot resolve its declaration path.\n"
            f"  Missing keys: {missing}"
        )

    def test_base_site_defaults_every_family_to_the_empty_set(self, workspace, catalog_families):
        base = yaml.safe_load((workspace / "sites" / "base-site.yaml").read_text(encoding="utf-8"))
        declared = (base.get("properties") or {}).get("resourceSets") or {}
        wrong = {k: v for k, v in declared.items() if v != _EMPTY_SET}
        assert not wrong, (
            f"Every catalog family defaults to '{_EMPTY_SET}' in base-site.yaml, "
            f"so inheriting a site deploys nothing until it opts in.\n{wrong}"
        )

    def test_every_family_partial_is_composed_by_the_catalog(self, workspace, catalog_families):
        """A family partial that nothing includes is dead content."""
        raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))
        included = {
            step["include"]
            for step in raw.get("steps") or []
            if isinstance(step, dict) and "include" in step
        }
        missing = [
            family
            for family in catalog_families
            if f"_{family.rstrip('s')}.yaml" not in included
            and f"_{family}.yaml" not in included
        ]
        assert not missing, (
            "A family has a declaration path in the catalog manifest but no "
            f"matching include, so its declaration loads and nothing deploys "
            f"it.\n{missing}\n  Includes present: {sorted(included)}"
        )


class TestPerSiteResolution:
    """A site variable inside a declaration resolves to each site's own value.

    One committed file deployed fleet-wide with per-site values is the whole
    point of the selection mechanism. An unresolved or hard-coded value deploys
    clean and puts every site on the same topic, which no ARM-level assertion
    catches.

    Asserted here rather than only against a live cluster, since it is pure
    resolution and costs a fraction of a second.
    """

    def _resolved_declarations(self, workspace, orchestrator):
        """Resolve every committed declaration for every site that could deploy it.

        All declaration files, not only the sets under `parameters/`, since a
        sample attaches one the same way and its variables resolve the same way.

        All committed sites, not only those the catalog's own selector matches.
        A selector is overridable from the CLI, so any site can be handed any
        declaration, and a variable that resolves for the dev sites while
        leaving a literal `{{ ... }}` on another is the failure this catches.
        """
        from tests.workspace.test_dataflow_validation import _declaration_files

        sites = orchestrator.load_all_sites()
        assert sites, "No sites loaded from the workspace."

        resolutions: dict[Path, dict[str, dict]] = {}
        for set_file in _declaration_files(workspace):
            if set_file.stem == _EMPTY_SET:
                continue
            declaration = yaml.safe_load(set_file.read_text(encoding="utf-8")) or {}
            resolutions[set_file] = {
                site.name: orchestrator._resolve_template_strings(declaration, site)
                for site in sites
            }
        return resolutions

    def test_no_committed_set_leaves_an_unresolved_variable(self, workspace, orchestrator):
        """Every `{{ ... }}` in a declaration resolves for every site.

        A variable naming a label only some sites carry resolves for the rest
        and ships the literal `{{ ... }}` to the resource provider on the
        others, which deploys clean and puts those sites on a topic nobody
        reads.
        """
        failures: list[str] = []
        for set_path, per_site in self._resolved_declarations(workspace, orchestrator).items():
            for site_name, resolved in per_site.items():
                leftovers = _unresolved_paths(resolved)
                if leftovers:
                    failures.append(
                        f"{set_path.relative_to(workspace)} for site "
                        f"'{site_name}' keeps unresolved templates at: {leftovers}"
                    )
        assert not failures, "\n".join(failures)

    def test_a_set_using_a_site_variable_differs_per_site(self, workspace, orchestrator):
        """A set that reads a site value produces a different result per site.

        This is what proves the fan-out claim. A set whose variables all
        resolved to the same thing would put the whole fleet on one topic.
        """
        checked: list[Path] = []
        for set_path, per_site in self._resolved_declarations(workspace, orchestrator).items():
            source = yaml.safe_load(set_path.read_text(encoding="utf-8"))
            if "{{ site." not in yaml.safe_dump(source):
                continue
            checked.append(set_path)
            rendered = {name: yaml.safe_dump(v) for name, v in per_site.items()}
            # More than one distinct rendering, not one per site. A set keyed on
            # a label several sites share, such as a per-country topic, is a
            # normal fleet shape and renders identically within each group.
            assert len(set(rendered.values())) > 1, (
                f"{set_path.relative_to(workspace)} reads a site value but "
                f"resolved identically for every site, so the fleet would share "
                f"one destination."
            )

        # Named rather than counted. Another declaration carrying a site value
        # would keep a count above zero while this one quietly stopped using one.
        worked_example = workspace / "parameters" / "dataflows" / "site-telemetry.yaml"
        assert worked_example in checked, (
            f"{worked_example.relative_to(workspace)} is the worked example for "
            f"per-site values and no longer carries one, so the fan-out claim is "
            f"unproven by the file the documentation points at. Checked: "
            f"{[str(p.relative_to(workspace)) for p in checked]}"
        )

    def test_rendered_names_are_valid_for_every_site(self, workspace, orchestrator):
        """A name carrying a site variable is valid once resolved, on every site.

        The contract test matches the name pattern against the declared string
        with variables replaced by a representative site name, which is the
        shape a site name has. A name may interpolate any site value, and a
        label carries whatever the operator wrote: every committed site spells
        `labels.city` with a capital, so `flow-{{ site.labels.city }}` renders
        `flow-Seattle` and the resource provider rejects it. Length behaves the
        same way, since a composed name grows with the site values it reads.

        Checked here because this is where every declaration is already
        rendered for every committed site.
        """
        from tests.workspace.test_dataflow_validation import (
            _KINDS,
            _MAX_NAME_LENGTH,
            _NAME_PATTERN,
            _entries,
        )

        failures: list[str] = []
        for set_path, per_site in self._resolved_declarations(workspace, orchestrator).items():
            for site_name, resolved in per_site.items():
                for kind in _KINDS:
                    for entry in _entries(resolved, kind):
                        name = entry.get("name")
                        if not isinstance(name, str):
                            continue
                        where = (
                            f"{set_path.relative_to(workspace)} {kind} name "
                            f"{name!r} on site '{site_name}'"
                        )
                        if not _NAME_PATTERN.match(name):
                            failures.append(
                                f"{where} does not match "
                                f"{_NAME_PATTERN.pattern} once resolved. Site "
                                f"values reach the name verbatim, so lowercase "
                                f"the label or use a value that is already "
                                f"lowercase, such as {{{{ site.name }}}}."
                            )
                        elif len(name) > _MAX_NAME_LENGTH:
                            failures.append(
                                f"{where} resolves to {len(name)} characters, "
                                f"over the {_MAX_NAME_LENGTH} the resource "
                                f"provider allows. Shorten the literal part or "
                                f"read a shorter site value."
                            )
        assert not failures, "\n".join(failures)

    def test_every_site_selects_a_family_the_catalog_knows(self, workspace, orchestrator):
        """A `resourceSets` key on a site names a family the catalog deploys.

        The gate reads one key per family. A key that names no family is inert:
        the site keeps the inherited empty set, the family is skipped, and the
        deploy reports success having created nothing the operator asked for.
        Neither the gate nor the parameter path can catch it, since both resolve
        correctly for the key they name.
        """
        import difflib

        known = _catalog_families(workspace)
        failures: list[str] = []
        for site in orchestrator.load_all_sites():
            selections = (site.properties or {}).get("resourceSets") or {}
            if not isinstance(selections, dict):
                failures.append(
                    f"Site '{site.name}' declares `resourceSets` as "
                    f"{type(selections).__name__}, which selects nothing. It is "
                    f"a mapping of family key to set name."
                )
                continue
            for key in selections:
                if key in known.values():
                    continue
                hint = difflib.get_close_matches(str(key), sorted(known.values()), n=1)
                suggestion = f" Did you mean '{hint[0]}'?" if hint else ""
                failures.append(
                    f"Site '{site.name}' selects a set for '{key}', which no "
                    f"family reads, so the selection is discarded and that "
                    f"family deploys nothing.{suggestion} Families: "
                    f"{sorted(known.values())}."
                )
        assert not failures, "\n".join(failures)


    def test_every_family_step_attaches_the_catalog_chaining_file(self, workspace):
        """A family step reads the resolved custom location, not a guessed one.

        `parameters/common/common.yaml` supplies `customLocationName` by naming
        convention for the steps that create it. A catalog step needs the name
        the resolve step actually read back, which arrives through the step-level
        chaining file. Dropping that file is silent, because the convention
        default satisfies the template's required parameter, and the family then
        deploys against a guessed name that works on every site following the
        convention and fails on any that does not.
        """
        raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))
        family_includes = [
            step["include"]
            for step in raw.get("steps") or []
            if isinstance(step, dict)
            and step.get("include", "").startswith("_")
            and step["include"] != "_resolve-aio.yaml"
        ]
        assert family_includes, "No family partials found in the catalog manifest."

        failures: list[str] = []
        for include in family_includes:
            manifest = Manifest.from_file(
                workspace / "manifests" / include, workspace_root=workspace
            )
            for step in manifest.steps:
                attached = set(getattr(step, "parameters", []) or [])
                if _CATALOG_CHAINING not in attached:
                    failures.append(
                        f"{include} step '{step.name}' does not attach "
                        f"{_CATALOG_CHAINING}. Attached: {sorted(attached)}"
                    )

        assert not failures, "\n".join(failures)

    def test_the_catalog_chaining_file_supplies_the_resolved_custom_location(
        self, workspace
    ):
        """The chaining file carries a step output, not a restatement of the default."""
        chaining = yaml.safe_load(
            (workspace / _CATALOG_CHAINING).read_text(encoding="utf-8")
        )
        value = (chaining or {}).get("customLocationName")
        assert value and "{{ steps." in str(value), (
            f"{_CATALOG_CHAINING} should chain `customLocationName` from the "
            f"resolve step. Found {value!r}. A literal here would deploy the "
            f"family against a guessed name rather than the resolved one."
        )


class TestCatalogStepOrder:
    """Resources within a family are ordered before anything that references them.

    A family deploys as one step, so the ordering lives in the family template's
    `dependsOn` rather than in manifest step order. ARM enforces it there, but it
    is out of reach of a manifest-level check, so it is asserted against the
    template.
    """

    def test_dataflow_family_is_one_step(self, workspace):
        """The partial contributes a single step per family.

        One deployment per family rather than one per resource kind is what keeps
        a fleet deploy from paying a round trip per kind per site.
        """
        manifest = Manifest.from_file(
            workspace / "manifests" / "_dataflows.yaml", workspace_root=workspace
        )
        assert [s.name for s in manifest.steps] == ["dataflow-resources"]

    def test_every_family_step_deploys_the_family_entry_point(self, workspace):
        """A family step points at its `main.bicep`, not at a version module.

        Each module under `modules/` creates every resource kind, but at one
        fixed API generation. A step pointing at a module directly succeeds and
        writes every site at that generation, ignoring the release each site
        actually runs.
        """
        raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))
        includes = [
            step["include"]
            for step in raw.get("steps") or []
            if isinstance(step, dict) and step.get("include", "").startswith("_")
            and step["include"] != "_resolve-aio.yaml"
        ]
        assert includes, (
            "No family partials are included by the catalog manifest, so nothing "
            "was checked."
        )

        for include in includes:
            manifest = Manifest.from_file(
                workspace / "manifests" / include, workspace_root=workspace
            )
            for step in manifest.steps:
                assert step.template.endswith("/main.bicep"), (
                    f"{include} step '{step.name}' deploys "
                    f"'{step.template}'. A family step deploys the family entry "
                    f"point, which routes to the module for the site's release. "
                    f"Pointing at a module pins every site to one generation."
                )

    def test_dataflow_family_orders_dataflows_last(self, workspace):
        """Every generation's module creates dataflows after their references.

        A dataflow names its endpoint and profile by string. ARM does not model a
        reference by name, so a dataflow deployed first succeeds and never moves
        data. Losing this `dependsOn` would be invisible until a live run, and it
        has to hold in each module rather than in one of them.
        """
        modules = sorted(
            (workspace / "templates" / "aio" / "dataflows" / "modules").glob("dataflows-*.bicep")
        )
        assert modules, (
            "No per-generation dataflow modules found, so this check covers "
            "nothing. Either the family moved or its layout changed."
        )

        for module in modules:
            text = module.read_text(encoding="utf-8")
            dataflow_block = text.split(
                "resource dataflowResources 'Microsoft.IoTOperations/instances/dataflowProfiles/dataflows"
            )
            assert len(dataflow_block) == 2, (
                f"{module.name} declares no dataflows resource, so ordering "
                f"cannot be checked."
            )
            depends_on = dataflow_block[1]

            for prerequisite in ("endpoints", "profiles"):
                assert re.search(rf"dependsOn:[^\]]*\b{prerequisite}\b", depends_on, re.DOTALL), (
                    f"{module.name} does not make its dataflows depend on "
                    f"{prerequisite}. A dataflow would deploy before the "
                    f"{prerequisite[:-1]} it references exists."
                )

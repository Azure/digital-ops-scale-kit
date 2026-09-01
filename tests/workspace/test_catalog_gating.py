"""Tests for catalog gating and deployment-family completeness.

`manifests/aio-resources.yaml` deploys a family only when the site selects a
resource set it serves, so the gate keeps an unconfigured site from running
empty deployments. These properties matter:

- The manifest parser rejects malformed gates, and the evaluator treats an
  absent or empty selection as false.
- A family has several pieces that must all be present. Adding one and missing a
  piece can otherwise leave definitions loaded without a deployment step.
"""

import copy
import re
from pathlib import Path

import pytest
import yaml

from siteops.models import Manifest
from tests.workspace import catalog_harness as harness
from tests.workspace.catalog_harness import (
    CATALOG_FAMILIES,
    CATALOG_MANIFEST,
    DATAFLOWS,
)

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


def _selection_keys() -> list[str]:
    """The public `resourceSets` keys the catalog reads.

    Registered rather than rediscovered here. `catalog_harness.CATALOG_FAMILIES`
    is the one list, and `test_catalog_family_contracts.py` holds it against the
    declaration paths in the catalog manifest, so a family cannot be wired
    without a spec or described by a stale one.
    """
    return sorted(
        {
            key
            for spec in CATALOG_FAMILIES
            for key in spec.resource_set_keys
        }
    )


class TestCatalogGateSemantics:
    """The gate deploys a family when a site selects a set, and not otherwise.

    Manifest parsing holds the gate to the evaluator's supported grammar.
    """

    def _gated_steps(self, workspace):
        manifest = Manifest.from_file(workspace / CATALOG_MANIFEST, workspace_root=workspace)
        return [step for step in manifest.steps if step.when]

    def test_every_catalog_step_is_gated(self, workspace):
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
        """Empty lists evaluate false, so no family deployment runs."""
        site = selectable_site
        for key in _selection_keys():
            site.properties["resourceSets"][key] = []

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is False, (
                f"Step '{step.name}' would deploy for a site that selected "
                f"no resource set. Its gate is `{step.when}`."
            )

    def test_gate_runs_a_site_that_selected_a_set(self, workspace, orchestrator, selectable_site):
        """A named set evaluates true, so the family deploys."""
        site = selectable_site
        for key in _selection_keys():
            site.properties["resourceSets"][key] = ["some-committed-set"]

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is True, (
                f"Step '{step.name}' would be skipped for a site that selected "
                f"a set. Its gate is `{step.when}`."
            )

    @pytest.mark.parametrize("selected_key", ["devices", "assets"])
    def test_device_registry_gate_runs_for_either_public_selection(
        self,
        workspace,
        orchestrator,
        selectable_site,
        selected_key,
    ):
        site = selectable_site
        site.properties["resourceSets"] = {
            "devices": [],
            "assets": [],
            "dataflows": [],
        }
        site.properties["resourceSets"][selected_key] = ["selected"]
        steps = {
            step.name: step
            for step in Manifest.from_file(
                workspace / CATALOG_MANIFEST,
                workspace_root=workspace,
            ).steps
        }

        assert orchestrator._evaluate_condition(
            steps["asset-resources"].when,
            site,
        )
        assert not orchestrator._evaluate_condition(
            steps["dataflow-resources"].when,
            site,
        )

    def test_gate_closes_when_the_key_is_absent(self, workspace, orchestrator, selectable_site):
        """An omitted resource area selects nothing."""
        site = selectable_site
        site.properties["resourceSets"] = {}

        for step in self._gated_steps(workspace):
            assert orchestrator._evaluate_condition(step.when, site) is False, (
                f"Step '{step.name}' would run for a site missing its "
                f"`resourceSets` key. Its gate is `{step.when}`."
            )


class TestCatalogFamilyCompleteness:
    """Every family carries all the pieces a family needs."""

    def test_no_resource_area_ships_a_none_sentinel(self, workspace):
        stale = [
            str((directory / "none.yaml").relative_to(workspace))
            for spec in CATALOG_FAMILIES
            for directory in harness.parameter_dirs(workspace, spec)
            if (directory / "none.yaml").exists()
        ]
        assert not stale, (
            "Empty resource-set lists load no files, so `none.yaml` is a stale "
            f"sentinel rather than a no-op declaration: {stale}"
        )

    def test_base_site_omits_resource_set_selections(self, workspace):
        base = yaml.safe_load((workspace / "sites" / "base-site.yaml").read_text(encoding="utf-8"))
        properties = base.get("properties") or {}
        assert "resourceSets" not in properties, (
            "Omitting resourceSets is the no-selection default. Keep explicit "
            "[] for a child site that clears an inherited selection rather "
            "than enumerating every resource area on the base template."
        )

    def test_every_family_partial_is_composed_by_the_catalog(self, workspace):
        """A family partial that nothing includes is dead content."""
        raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))
        included = {
            step["include"]
            for step in raw.get("steps") or []
            if isinstance(step, dict) and "include" in step
        }
        missing = [
            spec.family
            for spec in CATALOG_FAMILIES
            if f"_{spec.family.rstrip('s')}.yaml" not in included
            and f"_{spec.family}.yaml" not in included
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
        """Every committed declaration, resolved for every site, with its family.

        Sourced from `catalog_harness`, so this covers each registered family
        rather than one, and a family added later is rendered here without an
        edit. The family travels with each result, since the name rules and the
        kinds a declaration carries are per family.
        """
        resolved: list[tuple[harness.FamilySpec, Path, dict[str, dict]]] = []
        for spec in CATALOG_FAMILIES:
            rendered = harness.resolved_declarations(workspace, orchestrator, spec)
            resolved.extend((spec, set_path, per_site) for set_path, per_site in rendered.items())
        return resolved

    def test_no_committed_set_leaves_an_unresolved_variable(self, workspace, orchestrator):
        """Every `{{ ... }}` in a declaration resolves for every site.

        A variable naming a label only some sites carry fails at deployment
        resolution for the others. Committed definitions should fail here
        first, before reaching that runtime boundary.
        """
        failures: list[str] = []
        for _, set_path, per_site in self._resolved_declarations(workspace, orchestrator):
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

        Non-vacuity is held twice. Here, that some committed declaration reads a
        site value at all. In a family's own module, that the worked example the
        documentation points at is the one still doing it.
        """
        checked: list[Path] = []
        for _, set_path, per_site in self._resolved_declarations(workspace, orchestrator):
            source = harness.load_yaml(set_path)
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

        assert checked, (
            "No committed declaration reads a site value, so this check "
            "compared nothing and the fan-out the selection mechanism is sold "
            "on is unproven."
        )

    def test_rendered_names_are_valid_for_every_site(self, workspace, orchestrator):
        """A name carrying a site variable is valid once resolved, on every site.

        The contract test matches the name rules against the declared string
        with variables replaced by a representative site name, which is the
        shape a site name has. A name may interpolate any site value, and a
        label carries whatever the operator wrote: every committed site spells
        `labels.city` with a capital, so `flow-{{ site.labels.city }}` renders
        `flow-Seattle` and the resource provider rejects it. Length behaves the
        same way at both ends, since a composed name grows and shrinks with the
        site values it reads.

        Checked here because this is where every declaration is already
        rendered for every committed site.
        """
        failures: list[str] = []
        for spec, set_path, per_site in self._resolved_declarations(workspace, orchestrator):
            for site_name, resolved in per_site.items():
                for kind in spec.kinds:
                    pattern, min_length, max_length = spec.name_rules(kind)
                    for entry in harness.entries(resolved, kind.key):
                        name = entry.get("name")
                        if not isinstance(name, str):
                            continue
                        where = (
                            f"{set_path.relative_to(workspace)} {kind.key} name "
                            f"{name!r} on site '{site_name}'"
                        )
                        if not pattern.match(name):
                            failures.append(
                                f"{where} does not match "
                                f"{pattern.pattern} once resolved. Site "
                                f"values reach the name verbatim, so lowercase "
                                f"the label or use a value that is already "
                                f"lowercase, such as {{{{ site.name }}}}."
                            )
                        elif len(name) < min_length:
                            failures.append(
                                f"{where} resolves to {len(name)} characters, "
                                f"under the {min_length} the resource provider "
                                f"requires. Lengthen the literal part or read a "
                                f"longer site value."
                            )
                        elif len(name) > max_length:
                            failures.append(
                                f"{where} resolves to {len(name)} characters, "
                                f"over the {max_length} the resource "
                                f"provider allows. Shorten the literal part or "
                                f"read a shorter site value."
                            )
        assert not failures, "\n".join(failures)

    def test_every_site_selects_a_family_the_catalog_knows(self, workspace, orchestrator):
        """A `resourceSets` key on a site names a public resource area.

        The gate reads each public resource area. An unknown key is inert:
        known areas remain absent, the deployment family is skipped, and the
        deploy reports success without creating what the operator requested.
        Neither the gate nor the parameter path can catch it, since both resolve
        correctly for the key they name.
        """
        import difflib

        known = _selection_keys()
        failures: list[str] = []
        for site in orchestrator.load_all_sites():
            selections = (site.properties or {}).get("resourceSets") or {}
            if not isinstance(selections, dict):
                failures.append(
                    f"Site '{site.name}' declares `resourceSets` as "
                    f"{type(selections).__name__}, which selects nothing. It is "
                    "a mapping of resource area to an ordered set-name list."
                )
                continue
            for key in selections:
                if key in known:
                    continue
                hint = difflib.get_close_matches(str(key), known, n=1)
                suggestion = f" Did you mean '{hint[0]}'?" if hint else ""
                failures.append(
                    f"Site '{site.name}' selects a set for '{key}', which no "
                    f"resource area reads, so the selection is discarded and "
                    f"deploys nothing.{suggestion} Areas: {known}."
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

    def test_the_catalog_chaining_file_supplies_every_discovered_name(self, workspace):
        """A name the resolve step discovers reaches the family through the chain.

        `parameters/common/common.yaml` derives several names from the site name
        by convention, and those attach at manifest level to every catalog step.
        A family template that accepts such a name and is not handed the resolved
        one deploys against the convention default. That succeeds on every site
        following the convention and writes to the wrong resource, or fails, on
        any that does not, and nothing else in the suite can see it: the
        parameter is supplied, so the required-parameter sweep is satisfied, and
        the template compiles either way.

        The at-risk set is derived rather than listed, so a family accepting a
        new discovered name is covered on arrival. An output that merely echoes
        an input of the same name is excluded, since chaining it would return
        the value the site already supplied.
        """
        resolve = (workspace / "templates" / "aio" / "resolve-aio.bicep").read_text(
            encoding="utf-8"
        )
        discovered = {
            name
            for name, expression in re.findall(
                r"^output\s+(\w+)\s+\w+\s*=\s*(.+)$", resolve, re.MULTILINE
            )
            if expression.strip() != name
        }
        assert discovered, (
            "resolve-aio.bicep emits no output that is more than an echo of its "
            "input, so this check covers nothing."
        )

        common = yaml.safe_load(
            (workspace / "parameters" / "common" / "common.yaml").read_text(encoding="utf-8")
        ) or {}
        chained = set(
            yaml.safe_load((workspace / _CATALOG_CHAINING).read_text(encoding="utf-8")) or {}
        )

        failures: list[str] = []
        for spec in CATALOG_FAMILIES:
            entry_point = harness.entry_point(workspace, spec)
            if not entry_point.is_file():
                continue
            accepted = set(
                re.findall(
                    r"^\s*param\s+(\w+)\s+", entry_point.read_text(encoding="utf-8"), re.MULTILINE
                )
            )
            for name in sorted(accepted & set(common) & discovered):
                if name not in chained:
                    failures.append(
                        f"templates/aio/{spec.template_dir}/main.bicep accepts "
                        f"`{name}`, which common.yaml derives by convention and "
                        f"the resolve step reads back. {_CATALOG_CHAINING} does "
                        f"not chain it, so the family would deploy against the "
                        f"derived name rather than the resolved one."
                    )

        assert not failures, "\n".join(failures)

    def test_every_catalog_chaining_value_comes_from_a_step(self, workspace):
        """Nothing in the chaining file is a literal or a site variable.

        The file exists to carry values an upstream step read back. A key that
        stopped being a step reference would still satisfy the template's
        required parameter, so the family would deploy against whatever the
        chaining file now says with no other signal.
        """
        chaining = yaml.safe_load(
            (workspace / _CATALOG_CHAINING).read_text(encoding="utf-8")
        ) or {}
        assert chaining, f"{_CATALOG_CHAINING} carries no keys."

        wrong = {
            key: value for key, value in chaining.items() if "{{ steps." not in str(value)
        }
        assert not wrong, (
            f"{_CATALOG_CHAINING} carries values that are not step outputs, so "
            f"a family would deploy against a guessed name rather than a "
            f"resolved one.\n{wrong}"
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

    def test_asset_family_is_one_step(self, workspace):
        """Devices and assets deploy together, in one round trip per site.

        The asset family groups two resource kinds with an ordering between them.
        Splitting them into two steps would express that ordering in the manifest
        rather than in `dependsOn`, and would double the deployments a fleet run
        issues for this family.
        """
        manifest = Manifest.from_file(
            workspace / "manifests" / "_assets.yaml", workspace_root=workspace
        )
        assert [s.name for s in manifest.steps] == ["asset-resources"]

    def test_assets_deploy_before_dataflows(self, workspace):
        """A dataflow may name an asset as its source, so the asset comes first.

        Family order in the catalog manifest is the only thing sequencing two
        families, since each deploys as its own step and ARM cannot see across
        them. A dataflow whose source names an asset that does not exist yet
        deploys clean and moves nothing, which is the same silent failure the
        within-family `dependsOn` exists to prevent.

        Asserted on both surfaces the manifest carries. The include order is what
        sequences the deploy, and the declaration paths load in the same order, so
        keeping them aligned keeps the file readable as one ordering rather than
        two.
        """
        raw = yaml.safe_load((workspace / CATALOG_MANIFEST).read_text(encoding="utf-8"))

        includes = [
            step["include"]
            for step in raw.get("steps") or []
            if isinstance(step, dict) and "include" in step
        ]
        for family in ("_assets.yaml", "_dataflows.yaml"):
            assert family in includes, (
                f"The catalog manifest no longer includes {family}, so the "
                f"ordering between the asset and dataflow families is unchecked. "
                f"Includes present: {includes}"
            )
        assert includes.index("_assets.yaml") < includes.index("_dataflows.yaml"), (
            "The catalog manifest includes _dataflows.yaml before _assets.yaml, "
            "so a dataflow sourcing an asset would deploy before the asset "
            f"exists. Includes in order: {includes}"
        )

        paths = [
            entry["path"] if isinstance(entry, dict) else entry
            for entry in raw.get("parameters") or []
        ]
        devices_path = next(
            (i for i, p in enumerate(paths) if "parameters/devices/" in p),
            None,
        )
        assets_path = next((i for i, p in enumerate(paths) if "parameters/assets/" in p), None)
        dataflows_path = next(
            (i for i, p in enumerate(paths) if "parameters/dataflows/" in p), None
        )
        assert (
            devices_path is not None
            and assets_path is not None
            and dataflows_path is not None
        ), (
            "The catalog manifest no longer loads every resource declaration path.\n"
            f"  Paths: {paths}"
        )
        assert devices_path < assets_path < dataflows_path, (
            "The catalog manifest lists the dataflow declaration path before the "
            "device or asset path. Keep the source list in dependency order and "
            f"the family includes in deployment order.\n  Paths: {paths}"
        )

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
        modules = [
            harness.version_module(workspace, DATAFLOWS, version)
            for version in harness.supported_api_versions(workspace, DATAFLOWS)
        ]
        modules = sorted(module for module in modules if module.is_file())
        assert modules, (
            "No per-generation dataflow modules found, so this check covers "
            "nothing. Either the family moved or its layout changed."
        )

        for module in modules:
            text = module.read_text(encoding="utf-8")
            dataflow_block = text.split(
                f"resource dataflowResources "
                f"'{DATAFLOWS.kind('dataflows').resource_type}"
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

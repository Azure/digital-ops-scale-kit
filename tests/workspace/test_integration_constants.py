"""The names integration tests assert are the names the workspace declares.

An integration module hard-codes the resource names it expects on the cluster,
because it reads them back through `kubectl` rather than deriving them. Renaming
a declared resource and leaving those constants behind costs a live deployment
to discover, and the failure reads like a product defect rather than a stale
test.

Checked here rather than in the integration lane, which runs only against a real
subscription.
"""

import ast
import re
from pathlib import Path

import yaml

from tests.workspace import catalog_harness as harness

INTEGRATION_DIR = Path(__file__).parent.parent / "integration"


def _module_constants(path: Path) -> dict[str, str]:
    """Every module-level `NAME = "literal"` assignment in a test module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _selected_area_sets(path: Path) -> list[tuple[str, str]]:
    """The resource-area and set pairs applied to each resolved site.

    Read as names rather than as literals, then resolved through the module's
    own constants. A pair built from something other than a module constant is
    reported rather than skipped, since the point of the tuple is that every
    value it carries is checkable from here.
    """
    constants = _module_constants(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    selections: list[tuple[str, str]] | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CATALOG_SELECTIONS"
            for target in node.targets
        ):
            continue
        assert isinstance(node.value, (ast.Tuple, ast.List)), (
            f"{path.name} declares CATALOG_SELECTIONS as "
            f"{type(node.value).__name__}, where a tuple of (family, set) pairs "
            f"belongs."
        )
        selections = []
        for element in node.value.elts:
            assert isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) == 2, (
                f"{path.name}: every CATALOG_SELECTIONS entry is a (family, set) "
                f"pair."
            )
            resolved: list[str] = []
            for item in element.elts:
                assert isinstance(item, ast.Name) and item.id in constants, (
                    f"{path.name}: CATALOG_SELECTIONS carries a value that is "
                    f"not a module constant, so this check cannot resolve it. "
                    f"Name each family and set as a constant."
                )
                resolved.append(constants[item.id])
            selections.append((resolved[0], resolved[1]))

    assert selections is not None, (
        f"{path.name} no longer defines CATALOG_SELECTIONS. Update this check "
        f"rather than deleting it."
    )
    return selections


def _declared_names(workspace: Path) -> set[str]:
    """Every resource name any committed catalog declaration carries."""
    names: set[str] = set()
    for spec, paths in harness.declarations_by_family(workspace):
        for path in paths:
            data = harness.load_yaml(path) or {}
            for key in spec.kind_keys:
                for entry in harness.entries(data, key):
                    name = entry.get("name")
                    if isinstance(name, str):
                        names.add(name)
    return names


class TestIntegrationConstantsMatchTheWorkspace:
    """Names an integration module expects are names something declares."""

    # Constants naming a declared catalog resource. Others in those modules
    # name instance-owned resources or steps, which are checked elsewhere.
    _DECLARED_NAME_CONSTANTS = {
        "test_dataflow_sample_manifest.py": (
            "SAMPLE_ENDPOINT_NAME",
            "SAMPLE_ALERTS_ENDPOINT_NAME",
            "SAMPLE_PROFILE_NAME",
            "SAMPLE_ALERTS_PROFILE_NAME",
            "SAMPLE_DATAFLOW_NAME",
            "SAMPLE_ALERTS_DATAFLOW_NAME",
        ),
        "test_aio_resources_manifest.py": (
            "SET_ENDPOINT_NAME",
            "SET_DATAFLOW_NAME",
            "SET_DEVICE_NAME",
            "SET_ASSET_NAME",
        ),
        "test_resource_set_samples_manifest.py": (
            "_BASIC_DATAFLOW",
            "_MANAGED_DEVICE",
            "_MANAGED_OVEN",
            "_MANAGED_BOILER",
            "_EXTERNAL_OVEN",
            "_ADVANCED_ENDPOINT",
            "_ADVANCED_PROFILE",
            "_ADVANCED_DATAFLOW",
        ),
    }

    def test_expected_resource_names_are_declared_somewhere(self, workspace):
        declared = _declared_names(workspace)
        assert declared, "No declared resource names found in the workspace."

        checked = 0
        failures: list[str] = []
        for module_name, constant_names in self._DECLARED_NAME_CONSTANTS.items():
            module = INTEGRATION_DIR / module_name
            assert module.is_file(), (
                f"{module_name} does not exist. If integration modules were "
                f"renamed, update this map rather than deleting the check."
            )
            constants = _module_constants(module)
            for constant in constant_names:
                assert constant in constants, (
                    f"{module_name} no longer defines {constant}. Update this "
                    f"map so the check keeps covering that module."
                )
                checked += 1
                expected = constants[constant]
                if expected not in declared:
                    failures.append(
                        f"{module_name}: {constant} = '{expected}', which no "
                        f"committed declaration declares. A live run would "
                        f"fail looking for it. Declared: {sorted(declared)}"
                    )

        assert checked > 0, "No constants were checked."
        assert not failures, "\n".join(failures)

    def test_the_selected_set_constants_name_real_sets(self, workspace):
        """Every set the `aio-resources` phase selects names a committed file."""
        conftest = INTEGRATION_DIR / "conftest.py"
        selections = _selected_area_sets(conftest)
        assert selections, (
            "tests/integration/conftest.py selects no catalog set, so the "
            "`aio-resources` phase would deploy nothing and report success."
        )

        registered = {
            key: spec
            for spec in harness.CATALOG_FAMILIES
            for key in spec.resource_set_keys
        }
        for area, selected in selections:
            set_file = workspace / "parameters" / area / f"{selected}.yaml"
            assert set_file.is_file(), (
                f"The integration fixture selects set '{selected}' for resource "
                f"area '{area}', but parameters/{area}/{selected}.yaml does not "
                f"exist. A live run would deploy nothing and report success."
            )

            assert area in registered, (
                f"The integration fixture selects resource area '{area}', "
                "which no catalog family spec describes. A live run would "
                "select content nothing else in this suite checks. Registered: "
                f"{sorted(registered)}"
            )

            spec = registered[area]
            declaration = harness.load_yaml(set_file) or {}
            # Counted over the kinds a template deploys, not over every key. A
            # file whose only entries sit under an unknown key deploys nothing,
            # which is the outcome this guard exists to reject.
            selected_kinds = [
                kind.key
                for kind in spec.kinds
                if (kind.resource_set_key or spec.resource_set_key) == area
            ]
            declared = sum(
                len(harness.entries(declaration, key))
                for key in selected_kinds
            )
            assert declared > 0, (
                f"The integration fixture selects set '{selected}' for resource "
                f"area '{area}', which declares no resources that its deployment "
                "family handles. The live run would deploy nothing, and every "
                "assertion that reads a name from this set would pass against a "
                "run that created nothing. Select a set that declares at least "
                f"one resource. Known kinds: {sorted(spec.kind_keys)}"
            )

    def test_each_named_resource_area_and_set_pair_is_selected(self):
        """The named resource-area constants are the pairs the fixture applies.

        The fixture loops `CATALOG_SELECTIONS`, and each area also carries its
        own named constants so an assertion elsewhere can read one. A constant
        left out of the tuple names a set nothing selects, and the assertions
        keyed on it would run against resources that never deployed.
        """
        constants = _module_constants(INTEGRATION_DIR / "conftest.py")
        selected = set(_selected_area_sets(INTEGRATION_DIR / "conftest.py"))

        pairs = {
            "dataflow": ("CATALOG_FAMILY", "CATALOG_SET"),
            "device": ("DEVICE_CATALOG_FAMILY", "DEVICE_CATALOG_SET"),
            "asset": ("ASSET_CATALOG_FAMILY", "ASSET_CATALOG_SET"),
        }
        for label, (family_constant, set_constant) in pairs.items():
            for constant in (family_constant, set_constant):
                assert constant in constants, (
                    f"tests/integration/conftest.py no longer defines "
                    f"{constant}. Update this check rather than deleting it."
                )
            pair = (constants[family_constant], constants[set_constant])
            assert pair in selected, (
                f"The {label} pair {pair} is named in conftest but is not in "
                f"CATALOG_SELECTIONS, so the fixture never selects it. "
                f"Selected: {sorted(selected)}"
            )

    def test_every_registered_family_is_exercised_by_the_phase(self):
        """Every deployment family is reached by a selected resource area.

        The phase deploys one entry point whose steps are gated by public
        resource areas. A family with no selected area would pass every offline
        contract without being written to a real provider.
        """
        selected_areas = {
            area
            for area, _ in _selected_area_sets(
                INTEGRATION_DIR / "conftest.py"
            )
        }
        missing = sorted(
            spec.family
            for spec in harness.CATALOG_FAMILIES
            if not selected_areas.intersection(spec.resource_set_keys)
        )
        assert not missing, (
            f"These catalog families are never selected by the `aio-resources` "
            f"integration phase, so nothing deploys them against a real "
            f"subscription: {missing}. Add a (resource area, set) pair to "
            f"CATALOG_SELECTIONS in tests/integration/conftest.py."
        )


class TestIntegrationStepNamesMatchTheManifests:
    """A step name an integration module asserts is a step some manifest has."""

    _STEP_CONSTANTS = {
        "test_dataflow_sample_manifest.py": ("CATALOG_STEP",),
        "test_aio_resources_manifest.py": ("CATALOG_STEP", "ASSET_CATALOG_STEP"),
    }

    def test_expected_step_names_exist(self, workspace):
        from tests.workspace.test_manifest_validation import _all_manifest_files

        step_names: set[str] = set()
        for manifest_path in _all_manifest_files(workspace):
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            for step in raw.get("steps") or []:
                if isinstance(step, dict) and "name" in step:
                    step_names.add(step["name"])
        assert step_names, "No step names found across the workspace manifests."

        failures: list[str] = []
        for module_name, constant_names in self._STEP_CONSTANTS.items():
            constants = _module_constants(INTEGRATION_DIR / module_name)
            for constant in constant_names:
                expected = constants.get(constant)
                assert expected, f"{module_name} no longer defines {constant}."
                if expected not in step_names:
                    failures.append(
                        f"{module_name}: {constant} = '{expected}', which no "
                        f"manifest declares as a step. A live run would assert "
                        f"against a step that never executes."
                    )

        assert not failures, "\n".join(failures)


class TestIntegrationApiVersionsMatchTheWorkspace:
    """API versions used by live reads match the templates they inspect."""

    def test_extension_read_uses_the_deployed_resource_api(self, workspace):
        constants = _module_constants(
            INTEGRATION_DIR / "test_aio_install_manifest.py"
        )
        expected = constants.get("EXTENSION_API_VERSION")
        assert expected, (
            "test_aio_install_manifest.py no longer defines "
            "EXTENSION_API_VERSION"
        )

        versions: set[str] = set()
        for template in (workspace / "templates").rglob("*.bicep"):
            source = template.read_text(encoding="utf-8")
            versions.update(
                re.findall(
                    r"Microsoft\.KubernetesConfiguration/extensions@"
                    r"(\d{4}-\d{2}-\d{2}(?:-preview)?)'",
                    source,
                )
            )

        assert versions, "No Arc extension API versions were found in templates."
        assert versions == {expected}, (
            "The live extension read API does not match the workspace "
            f"templates. Integration: {expected!r}. Templates: "
            f"{sorted(versions)}"
        )


class TestKubernetesResourceTypesAreConsistent:
    """The projected resource types are spelled the same in every module."""

    # AIO workload resources project into `<kind>.connectivity.iotoperations.azure.com`,
    # and namespace-scoped Device Registry resources into
    # `<kind>.namespaces.deviceregistry.microsoft.com`. Both groups are matched, since
    # a module spelling either one differently reads a resource type nothing serves.
    _TYPE_PATTERN = re.compile(
        r"['\"]([a-z]+\.(?:connectivity\.iotoperations\.azure\.com"
        r"|namespaces\.deviceregistry\.microsoft\.com))['\"]"
    )

    def test_no_two_spellings_of_one_resource_type(self):
        by_kind: dict[str, set[str]] = {}
        for module in sorted(INTEGRATION_DIR.glob("test_*.py")):
            for match in self._TYPE_PATTERN.finditer(module.read_text(encoding="utf-8")):
                full = match.group(1)
                by_kind.setdefault(full.split(".", 1)[0], set()).add(full)

        assert by_kind, "No AIO custom resource types found in the integration lane."
        conflicts = {k: sorted(v) for k, v in by_kind.items() if len(v) > 1}
        assert not conflicts, (
            f"One resource kind is spelled more than one way, so a rename "
            f"would be applied inconsistently.\n{conflicts}"
        )


class TestAssertedOutputsAreDeclared:
    """An output an integration module reads is one a template emits.

    `assert_output_exists(step, "X")` fails at deploy time, so a renamed or
    mistyped output name costs a live run to discover and reads like a product
    defect. The name is checked against the whole template set rather than
    against the one template behind the step, since a test module does not
    record which template its step deploys.

    Two shapes are collected, because both are in use: the name passed straight
    to the call, and a tuple of names the call is looped over.
    """

    @staticmethod
    def _asserted_output_names(module: Path) -> set[str]:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names: set[str] = set()

        for node in ast.walk(tree):
            # `assert_output_exists(step, "X")`
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called == "assert_output_exists" and len(node.args) >= 2:
                    second = node.args[1]
                    if isinstance(second, ast.Constant) and isinstance(second.value, str):
                        names.add(second.value)

            # `stable_outputs = ("X", "Y")`, looped over by the call above.
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if any(t == "outputs" or t.endswith("_outputs") for t in targets):
                    for element in node.value.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            names.add(element.value)
        return names

    def test_every_asserted_output_name_is_emitted_somewhere(self, workspace):
        emitted: set[str] = set()
        for area in ("templates", "samples"):
            for template in (workspace / area).rglob("*.bicep"):
                emitted.update(
                    match.group(1)
                    for match in re.finditer(
                        r"^output\s+(\w+)\s",
                        template.read_text(encoding="utf-8"),
                        re.MULTILINE,
                    )
                )
        assert emitted, "No Bicep outputs found, so this check covers nothing."

        asserted: dict[str, set[str]] = {}
        for module in sorted(INTEGRATION_DIR.glob("test_*.py")):
            for name in self._asserted_output_names(module):
                asserted.setdefault(name, set()).add(module.name)
        assert len(asserted) >= 5, (
            f"Only {len(asserted)} asserted output name(s) found in the "
            f"integration lane, which is fewer than the modules that read "
            f"outputs. The call or tuple shape this collects may have changed."
        )

        missing = sorted(
            f"{name} (in {', '.join(sorted(modules))})"
            for name, modules in asserted.items()
            if name not in emitted
        )
        assert not missing, (
            "An integration test reads an output no template emits, which fails "
            "only once a deployment has already run:\n  " + "\n  ".join(missing)
        )

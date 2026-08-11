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


def _declared_names(workspace: Path) -> set[str]:
    """Every resource name any committed dataflow declaration carries."""
    from tests.workspace.test_dataflow_validation import (
        _KINDS,
        _declaration_files,
        _entries,
        _load_yaml,
    )

    names: set[str] = set()
    for path in _declaration_files(workspace):
        data = _load_yaml(path) or {}
        for key in _KINDS:
            for entry in _entries(data, key):
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

    def test_the_selected_set_constant_names_a_real_set(self, workspace):
        """`CATALOG_SET` in the integration conftest names a committed file."""
        conftest = INTEGRATION_DIR / "conftest.py"
        constants = _module_constants(conftest)

        family = constants.get("CATALOG_FAMILY")
        selected = constants.get("CATALOG_SET")
        assert family and selected, (
            "tests/integration/conftest.py no longer defines CATALOG_FAMILY "
            "and CATALOG_SET. Update this check rather than deleting it."
        )

        set_file = workspace / "parameters" / family / f"{selected}.yaml"
        assert set_file.is_file(), (
            f"The integration fixture selects set '{selected}' for family "
            f"'{family}', but {set_file.relative_to(workspace)} does not "
            f"exist. A live run would deploy nothing and report success."
        )

        from tests.workspace.test_dataflow_validation import _KINDS, _entries, _load_yaml

        declaration = _load_yaml(set_file) or {}
        # Counted over the kinds a template deploys, not over every key. A file
        # whose only entries sit under an unknown key deploys nothing, which is
        # the outcome this guard exists to reject.
        declared = sum(len(_entries(declaration, key)) for key in _KINDS)
        assert declared > 0, (
            f"The integration fixture selects set '{selected}' for family "
            f"'{family}', which declares no resources of any kind the family "
            f"deploys. The live run would deploy nothing, and every assertion "
            f"that reads a name from this set would pass against a run that "
            f"created nothing. Select a set that declares at least one "
            f"resource. Known kinds: {sorted(_KINDS)}"
        )


class TestIntegrationStepNamesMatchTheManifests:
    """A step name an integration module asserts is a step some manifest has."""

    _STEP_CONSTANTS = {
        "test_dataflow_sample_manifest.py": "CATALOG_STEP",
        "test_aio_resources_manifest.py": "CATALOG_STEP",
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
        for module_name, constant in self._STEP_CONSTANTS.items():
            constants = _module_constants(INTEGRATION_DIR / module_name)
            expected = constants.get(constant)
            assert expected, f"{module_name} no longer defines {constant}."
            if expected not in step_names:
                failures.append(
                    f"{module_name}: {constant} = '{expected}', which no "
                    f"manifest declares as a step. A live run would assert "
                    f"against a step that never executes."
                )

        assert not failures, "\n".join(failures)


class TestKubernetesResourceTypesAreConsistent:
    """The projected resource types are spelled the same in every module."""

    _TYPE_PATTERN = re.compile(r"['\"]([a-z]+\.connectivity\.iotoperations\.azure\.com)['\"]")

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

"""Tests that every list of integration test phases agrees with the others.

Four operator-facing surfaces name the same set of phases:

- the E2E workflow's phase map and its documented list of valid values
- the GitHub integration workflow's `manifest` choice list
- the Azure Pipelines integration definition's `manifest` values

Each is hand-maintained, and each derives a test filename from the phase name by
the same convention, so a phase that does not follow it selects a file that does
not exist and the run passes having collected nothing. Both integration lists
have drifted before: one omitted a phase that was unexpressible because its test
file did not follow the convention.

The integration test files are the authoritative set. Everything else is checked
against them.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e-test.yaml"
GITHUB_INTEGRATION = REPO_ROOT / ".github" / "workflows" / "integration-test.yaml"
ADO_INTEGRATION = REPO_ROOT / ".pipelines" / "integration-test.yaml"
E2E_TESTING_DOC = REPO_ROOT / "docs" / "e2e-testing.md"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
INTEGRATION_CONFTEST = INTEGRATION_DIR / "conftest.py"
AIO_UPGRADE_TEST = INTEGRATION_DIR / "test_aio_upgrade_manifest.py"

# `"opc-ua-solution": "tests/integration/test_opc_ua_solution_manifest.py",`
_MAP_ENTRY = re.compile(r'"([a-z0-9-]+)":\s*"(tests/integration/test_\w+\.py)"')

# `Valid values: aio-install, enable-secretsync, ... .`
_VALID_VALUES = re.compile(r"Valid values: ([^.]+)\.")

# `all` selects the whole suite rather than one phase, so it is not a phase name.
_NOT_A_PHASE = {"all"}

# Phase names the E2E workflow hard-codes inside guard logic rather than reading
# from its own map. A rename that updated the map would leave these behind, and
# every other check here would stay green while the guard silently stopped
# matching. `secret_sync_tests = {"enable-secretsync", "sync-secrets"}`
_GUARD_SET = re.compile(r"secret_sync_tests\s*=\s*\{([^}]*)\}")

# `if upgrade_to and "aio-upgrade" not in ordered:`
_GUARD_LITERAL = re.compile(r'"([a-z0-9-]+)"\s+not in ordered')
_ISOLATION_PAIR = re.compile(
    r"profile_heavy\s*=\s*\{\s*"
    r'"dataflow-sample",\s*"resource-set-samples",?\s*\}',
    re.DOTALL,
)

_TEST_CLASS = re.compile(r"^class\s+(Test\w+)\b", re.MULTILINE)


def _class_set(name: str) -> set[str]:
    """Read a frozenset of quoted class names from integration conftest."""
    text = INTEGRATION_CONFTEST.read_text(encoding="utf-8")
    match = re.search(
        rf"{name}\s*=\s*frozenset\(\{{(?P<body>.*?)\}}\)",
        text,
        re.DOTALL,
    )
    assert match, f"No {name} frozenset found in {INTEGRATION_CONFTEST.name}"
    return set(re.findall(r'"(Test\w+)"', match.group("body")))


def _phase_to_file(phase: str) -> str:
    """The test file a phase name derives to, per the documented convention."""
    return f"tests/integration/test_{phase.replace('-', '_')}_manifest.py"


def _authoritative_phases() -> set[str]:
    """Phase names implied by the integration test files on disk."""
    phases = set()
    for path in sorted(INTEGRATION_DIR.glob("test_*_manifest.py")):
        stem = path.stem.removeprefix("test_").removesuffix("_manifest")
        phases.add(stem.replace("_", "-"))
    assert phases, f"No integration test files found under {INTEGRATION_DIR}"
    return phases


def _e2e_phase_map() -> dict[str, str]:
    entries = dict(_MAP_ENTRY.findall(E2E_WORKFLOW.read_text(encoding="utf-8")))
    assert entries, "No phase-to-test-file entries found in the E2E workflow."
    return entries


def _e2e_documented_phases() -> set[str]:
    match = _VALID_VALUES.search(E2E_WORKFLOW.read_text(encoding="utf-8"))
    assert match, "No `Valid values:` list found in the E2E workflow `tests` input."
    return {v.strip() for v in match.group(1).split(",") if v.strip()}


def _docs_documented_phases() -> set[str]:
    """Phases listed in the operator-facing E2E guide.

    A fifth surface naming the same set. It is prose rather than configuration,
    so nothing else keeps it honest.
    """
    text = E2E_TESTING_DOC.read_text(encoding="utf-8")
    match = re.search(r"Valid values: ([^.|]+)\.", text)
    assert match, f"No `Valid values:` list found in {E2E_TESTING_DOC.name}"
    return {v.strip().strip("`") for v in match.group(1).split(",") if v.strip()}


def _choice_options(path: Path, marker: str) -> set[str]:
    """Read a YAML choice list following a marker line, without a YAML parse.

    The ADO definition carries `${{ }}` template expressions elsewhere in the
    file, so a targeted scan is enough here rather than loading the whole document.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    options: set[str] = set()
    collecting = False
    for line in lines:
        if marker in line:
            collecting = True
            continue
        if not collecting:
            continue
        item = re.match(r"^\s+-\s+([a-z0-9-]+)\s*$", line)
        if item:
            options.add(item.group(1))
        elif options:
            break
    phases = options - _NOT_A_PHASE
    # Assert after the subtraction. A list holding only `all` would otherwise
    # satisfy the guard and then compare as an empty set against another empty
    # set, passing while covering nothing.
    assert phases, f"No phase options found after '{marker}' in {path.name}"
    return phases


class TestIntegrationPhaseConsistency:
    """Every phase list agrees with the integration test files."""

    def test_e2e_map_paths_match_the_naming_convention(self):
        """Each mapped path equals what its phase name derives to.

        Both integration pipelines compute the filename from the phase name
        rather than reading this map, so a map entry that points somewhere else
        is reachable from E2E and unreachable from either integration pipeline.
        That is exactly how a phase became unselectable on Azure Pipelines.
        """
        violations = {
            phase: {"declared": declared, "derived": _phase_to_file(phase)}
            for phase, declared in _e2e_phase_map().items()
            if declared != _phase_to_file(phase)
        }
        assert not violations, (
            "An E2E phase maps to a file other than the one its name derives "
            "to. The integration pipelines derive the name, so that phase "
            "cannot be selected there.\n"
            + "\n".join(
                f"  {p}: declared={v['declared']} derived={v['derived']}"
                for p, v in violations.items()
            )
        )

    def test_e2e_map_matches_the_test_files(self):
        mapped = _e2e_phase_map()
        missing = {p: f for p, f in mapped.items() if not (REPO_ROOT / f).is_file()}
        assert not missing, (
            f"An E2E phase names a test file that does not exist.\n{missing}"
        )
        assert set(mapped) == _authoritative_phases(), (
            "The E2E phase map disagrees with the integration test files.\n"
            f"  In map only:   {sorted(set(mapped) - _authoritative_phases())}\n"
            f"  On disk only:  {sorted(_authoritative_phases() - set(mapped))}"
        )

    def test_e2e_documented_values_match_its_map(self):
        documented = _e2e_documented_phases()
        mapped = set(_e2e_phase_map())
        assert documented == mapped, (
            "The operator-facing `Valid values` list disagrees with the E2E "
            "phase map, so a documented phase is rejected or a working one is "
            "undiscoverable.\n"
            f"  Documented only: {sorted(documented - mapped)}\n"
            f"  Mapped only:     {sorted(mapped - documented)}"
        )

    def test_github_and_ado_integration_offer_the_same_phases(self):
        """The two integration pipelines are peers and must stay aligned.

        This is the drift that has actually happened: a phase offered on one
        platform and not the other is untestable from the missing one.
        """
        github = _choice_options(GITHUB_INTEGRATION, 'description: "Test phase"')
        ado = _choice_options(ADO_INTEGRATION, 'displayName: "Test phase"')
        assert github == ado, (
            "The integration phase lists have drifted between platforms.\n"
            f"  Only in .github/workflows/integration-test.yaml: {sorted(github - ado)}\n"
            f"  Only in .pipelines/integration-test.yaml:        {sorted(ado - github)}"
        )

    def test_integration_phases_all_resolve_to_test_files(self):
        """Every offered phase names a real test file.

        The integration pipelines offer a subset of the phases E2E runs, since
        some flows need the E2E harness. A subset is fine. A phase that resolves
        to nothing is not, because selecting it collects zero tests and reports
        success.
        """
        offered = _choice_options(GITHUB_INTEGRATION, 'description: "Test phase"')
        unknown = sorted(offered - _authoritative_phases())
        assert not unknown, (
            "An integration phase does not correspond to any test file, so "
            f"selecting it would collect nothing.\n{unknown}"
        )

    def test_e2e_guide_documents_the_same_phases(self):
        """The operator guide is a fifth surface naming the same set.

        It is prose, so nothing else keeps it honest, and it already listed a
        stale set once.
        """
        documented = _docs_documented_phases()
        mapped = set(_e2e_phase_map())
        assert documented == mapped, (
            f"{E2E_TESTING_DOC.name} lists different E2E phases than the "
            "workflow offers.\n"
            f"  Documented only: {sorted(documented - mapped)}\n"
            f"  Workflow only:   {sorted(mapped - documented)}"
        )

    def test_workflow_guards_name_live_phases(self):
        """Guard logic in the workflow names phases that still exist.

        The workflow decides whether a selection is valid by comparing against
        phase names written directly into its script, not by reading its own
        map. Those literals are a sixth surface. A rename that updated the map
        and the choice lists would leave a guard comparing against a name
        nothing produces, so the guard would stop rejecting the combination it
        exists to reject while every other check here stayed green.
        """
        text = E2E_WORKFLOW.read_text(encoding="utf-8")
        mapped = set(_e2e_phase_map())
        guarded: set[str] = set()

        secret_sync = _GUARD_SET.search(text)
        assert secret_sync, (
            "No `secret_sync_tests` guard set found in the E2E workflow. If the "
            "Secret Sync guard moved, point this test at its new form rather "
            "than deleting it."
        )
        guarded |= {
            value.strip().strip("\"'")
            for value in secret_sync.group(1).split(",")
            if value.strip()
        }

        upgrade = _GUARD_LITERAL.findall(text)
        assert upgrade, (
            "No `\"<phase>\" not in ordered` guard found in the E2E workflow. "
            "The upgrade-phase guard prevents a run that collects zero tests "
            "and reports success."
        )
        guarded |= set(upgrade)

        stale = sorted(guarded - mapped)
        assert not stale, (
            "E2E workflow guard logic names phases that are not in its phase "
            "map, so those guards no longer match anything an operator can "
            f"select.\n  Stale in guards: {stale}\n"
            f"  Known phases:    {sorted(mapped)}"
        )

    def test_profile_heavy_phases_enable_cleanup_when_selected_together(self):
        text = E2E_WORKFLOW.read_text(encoding="utf-8")
        assert _ISOLATION_PAIR.search(text), (
            "The E2E workflow no longer isolates the dataflow sample before "
            "the advanced resource-set sample when both phases share one "
            "cluster."
        )
        assert (
            "SITEOPS_E2E_ISOLATE_DATAFLOW_SAMPLE: "
            "${{ needs.prep.outputs.isolate-dataflow-sample }}"
        ) in text
        assert (
            'ordered[first_profile_phase:first_profile_phase] = [\n'
            '                      "dataflow-sample",\n'
            '                      "resource-set-samples",\n'
            "                  ]"
        ) in text

    def test_aio_upgrade_classes_have_an_explicit_phase(self):
        """Every upgrade test class is allowlisted or intentionally install-only.

        A class omitted from both sets is collected and silently skipped during
        the upgrade phase. That can leave new upgrade behavior covered only by
        a same-version install-phase reapply.
        """
        classes = set(
            _TEST_CLASS.findall(AIO_UPGRADE_TEST.read_text(encoding="utf-8"))
        )
        allowed = _class_set("_UPGRADE_PHASE_ALLOWED_CLASSES")
        install_only = _class_set("_UPGRADE_PHASE_INSTALL_ONLY_CLASSES")

        assert allowed.isdisjoint(install_only), (
            "An AIO upgrade test class is both upgrade-phase and install-only: "
            f"{sorted(allowed & install_only)}"
        )
        assert classes == allowed | install_only, (
            "AIO upgrade test classes do not have an explicit phase.\n"
            f"  Unclassified: {sorted(classes - allowed - install_only)}\n"
            f"  Stale names:  {sorted((allowed | install_only) - classes)}"
        )


@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_supported_ci_markers_fail_closed(monkeypatch, marker):
    from tests.integration.conftest import _in_ci

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("TF_BUILD", raising=False)
    monkeypatch.setenv(marker, "true")

    assert _in_ci()


def test_e2e_masks_the_site_name_before_publishing_it():
    text = E2E_WORKFLOW.read_text(encoding="utf-8")

    mask = 'echo "::add-mask::$SN"'
    output = 'echo "site_name=$SN" >> "$GITHUB_OUTPUT"'
    assert mask in text
    assert text.index(mask) < text.index(output)

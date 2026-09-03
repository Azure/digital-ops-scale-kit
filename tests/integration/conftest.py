"""Fixtures for integration tests.

Integration tests deploy real manifests against Azure and assert outputs.
The test framework is site-agnostic: it deploys to whatever sites match
the manifest's selector (or a user-provided override), just like production.

Configuration is provided via:
  - Local: sites.local/ overlay files (hand-written YAML, one per site)
  - CI integration suite: SITE_OVERRIDES env var (JSON → auto-generates sites.local/ overlays)
  - E2E suite: SITEOPS_EXTRA_SITES_DIRS env var (os.pathsep-joined dirs
    containing rendered site files, orthogonal to sites.local/)

Behavior when no site config is present:
  - Tests are skipped at collection time (`has_config` check).
Behavior when site config is present but the selector resolves to zero sites:
  - Tests ERROR at fixture time with a diagnostic message. A zero-site
    deployment is never a legitimate integration-test outcome. Silent
    vacuous passes would mask real misconfigurations (wrong selector,
    broken inherits chain, mismatched labels) that were discovered
    previously in exactly this way.

Cluster-side reads (direct kubectl) require a kubeconfig that routes to
the cluster the AIO instance was deployed onto:
  - Local: standard kubectl discovery (~/.kube/config or KUBECONFIG)
  - E2E suite: SITEOPS_TEST_KUBECONFIG env var. Required because the
    siteops orchestrator's `arc:` kubectl steps mutate ~/.kube/config
    via `az connectedk8s proxy` (adding a proxy-context entry that
    points at a local port and switching current-context to it). The
    proxy process exits after each deploy step, leaving the kubeconfig
    pointing at a dead URL. Point SITEOPS_TEST_KUBECONFIG at the k3s
    admin file (mode 0644 from create-k3s-cluster) and helpers in
    tests/integration/helpers/kube.py inject --kubeconfig=<path> on
    every kubectl invocation.
"""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from siteops.models import Manifest
from siteops.orchestrator import Orchestrator
from siteops.sanitize import scrub_site_for_output, site_name_for_output

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"
SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "generate-site-overrides.py"

_EXTRA_SITES_DIRS_ENV = "SITEOPS_EXTRA_SITES_DIRS"
_UPGRADE_PHASE_ENV = "SITEOPS_E2E_UPGRADE_PHASE"

# The catalog resource areas and committed sets the `aio-resources` phase deploys. Each
# set ships at parameters/<area>/<set>.yaml, so a rename there has to reach
# these constants or the deploy resolves nothing.
CATALOG_FAMILY = "dataflows"
CATALOG_SET = "site-telemetry"
DEVICE_CATALOG_FAMILY = "devices"
DEVICE_CATALOG_SET = "site-devices"
ASSET_CATALOG_FAMILY = "assets"
ASSET_CATALOG_SET = "site-assets"

# Every (resource area, set) pair the phase selects on each resolved site. Named as a
# pair rather than applied inline, so `tests/workspace/test_integration_constants.py`
# can hold each one against the committed sets.
CATALOG_SELECTIONS = (
    (DEVICE_CATALOG_FAMILY, DEVICE_CATALOG_SET),
    (ASSET_CATALOG_FAMILY, ASSET_CATALOG_SET),
    (CATALOG_FAMILY, CATALOG_SET),
)

# Sentinel returned by `aio_install_result` in upgrade phase. Shape is
# deliberately not a real deploy result so any leaked consumer fails loudly.
_UPGRADE_PHASE_INSTALL_SENTINEL = {"_upgrade_phase_sentinel": True}

# Upgrade-phase allowlist: classes whose tests read only upgrade-step outputs.
# Allowlisted classes must not consume `aio_install_result` content (the
# sentinel has no `sites`/`summary` keys, so direct access would KeyError
# with a non-obvious traceback). Depending on the fixture for ordering only
# is fine. Reading from it is not.
_UPGRADE_PHASE_ALLOWED_CLASSES = frozenset({
    "TestAioUpgradeDeployment",
    "TestAioUpgradeOidcOptionality",
    "TestAioUpgradeResolveExtensions",
    "TestAioUpgradeSelfConsistency",
    "TestAioUpgradeIdempotency",
    "TestAioUpgradeTargetVersion",
    "TestAioExtensionInvariants",
    "TestOpcUaConnectorTemplateUpgrade",
    "TestSecretStoreExtensionInvariants",
    "TestCertManagerExtensionInvariants",
    "TestExtensionAdditiveOverrides",
})

# Classes in test_aio_upgrade_manifest.py that deliberately need install-phase
# state and therefore stay skipped when the upgrade phase stubs aio_install_result.
_UPGRADE_PHASE_INSTALL_ONLY_CLASSES = frozenset({
    "TestAioUpgradePreservation",
})


def _is_upgrade_phase() -> bool:
    return os.environ.get(_UPGRADE_PHASE_ENV, "").strip() in ("1", "true", "yes")


def _in_ci() -> bool:
    """True when running on a supported CI runner.

    Used to turn a skip into a failure: locally a missing prerequisite is a
    clean skip, while in CI it means a misconfigured workflow that would
    otherwise report success after provisioning real resources.
    """
    return (
        os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
        or os.environ.get("TF_BUILD", "").lower() == "true"
    )


def _extra_sites_dirs() -> list[Path]:
    """Parse `SITEOPS_EXTRA_SITES_DIRS` into a list of paths (os.pathsep-delimited)."""
    raw = os.environ.get(_EXTRA_SITES_DIRS_ENV, "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def _extra_sites_have_yaml(dirs: list[Path]) -> bool:
    """Return True if any extra-sites dir contains at least one `*.yaml` or `*.yml` file."""
    return any(
        d.is_dir() and (any(d.glob("*.yaml")) or any(d.glob("*.yml")))
        for d in dirs
    )


def _generate_overlays_from_site_overrides() -> bool:
    """Generate sites.local/ overlays by calling the shared script.

    Returns True if overlays were generated.
    """
    raw = os.environ.get("SITE_OVERRIDES", "")
    if not raw.strip():
        return False

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(WORKSPACE_PATH)],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"generate-site-overrides.py failed: {result.stderr}", file=sys.stderr)
        return False

    return True


_pre_existing_overlays: set[str] = set()
_generated_overlays = False


def pytest_collection_finish(session):
    """Prepare site config and gate the integration suite.

    Runs after marker deselection, so `session.items` holds exactly what this
    session will execute. The unit lane collects this file and then deselects
    every integration item, and a session that reaches Azure has to be told
    apart from one that never will. Doing this in `pytest_collection_modifyitems`
    would judge the unit lane, since deselection has not happened yet.
    """
    global _generated_overlays, _pre_existing_overlays

    items = [item for item in session.items if "integration" in item.keywords]
    if not items:
        return

    sites_local = WORKSPACE_PATH / "sites.local"
    if sites_local.is_dir():
        _pre_existing_overlays = {f.name for f in sites_local.glob("*.yaml")}

    _generated_overlays = _generate_overlays_from_site_overrides()

    extra_dirs = _extra_sites_dirs()
    has_config = (
        _generated_overlays
        or (sites_local.is_dir() and any(sites_local.glob("*.yaml")))
        or _extra_sites_have_yaml(extra_dirs)
    )

    if not has_config:
        # In CI a missing site config means the workflow is misconfigured, not
        # that there is nothing to run. Skipping would exit 0 after the run has
        # already provisioned a cluster and an AIO instance, reporting success
        # while asserting nothing. Fail where that is unambiguous, and keep the
        # clean skip for local runs.
        if _in_ci():
            pytest.exit(
                "Integration tests require sites.local/ overlays, SITE_OVERRIDES, "
                "or SITEOPS_EXTRA_SITES_DIRS with site files, and none were found. "
                "Skipping the whole suite in CI is not allowed, since it would "
                "report success without asserting anything.",
                returncode=1,
            )
        skip = pytest.mark.skip(
            reason="Integration tests require sites.local/ overlays, "
            "SITE_OVERRIDES, or SITEOPS_EXTRA_SITES_DIRS with site files"
        )
        for item in items:
            item.add_marker(skip)

    if _is_upgrade_phase():
        skip_upgrade = pytest.mark.skip(
            reason=f"{_UPGRADE_PHASE_ENV} active: only upgrade-step tests run "
            f"in this phase (install fixtures are stubbed)"
        )
        seen_classes: set[str] = set()
        kept = 0
        for item in items:
            cls = getattr(item, "cls", None)
            cls_name = cls.__name__ if cls is not None else None
            if cls_name is not None:
                seen_classes.add(cls_name)
            if cls_name not in _UPGRADE_PHASE_ALLOWED_CLASSES:
                item.add_marker(skip_upgrade)
            else:
                kept += 1

        # The allowlist is matched by name, so a rename or a moved test drops
        # coverage with no other signal. Both checks below turn that into a
        # fast failure rather than an expensive green run.
        missing = _UPGRADE_PHASE_ALLOWED_CLASSES - seen_classes
        if missing:
            pytest.exit(
                f"{_UPGRADE_PHASE_ENV} allowlist names classes that were not "
                f"collected: {sorted(missing)}. Update "
                f"_UPGRADE_PHASE_ALLOWED_CLASSES to match the tests, since an "
                f"unmatched name runs nothing.",
                returncode=1,
            )
        if kept == 0:
            pytest.exit(
                f"{_UPGRADE_PHASE_ENV} is set but no test matched the allowlist, "
                f"so the upgrade phase would assert nothing.",
                returncode=1,
            )


def pytest_sessionfinish(session, exitstatus):
    """Clean up generated overlays unless skip-cleanup is set."""
    skip_cleanup = os.environ.get("INTEGRATION_SKIP_CLEANUP", "").lower() in ("true", "1", "yes")
    if _generated_overlays and not skip_cleanup:
        sites_local = WORKSPACE_PATH / "sites.local"
        if sites_local.is_dir():
            for f in sites_local.glob("*.yaml"):
                if f.name not in _pre_existing_overlays:
                    f.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def workspace() -> Path:
    """Path to the IoT Operations workspace."""
    assert WORKSPACE_PATH.is_dir(), f"Workspace not found: {WORKSPACE_PATH}"
    return WORKSPACE_PATH


@pytest.fixture(scope="session")
def selector() -> str | None:
    """Site selector from INTEGRATION_SELECTOR env var, or None for manifest default."""
    return os.environ.get("INTEGRATION_SELECTOR") or None


@pytest.fixture(scope="session")
def orchestrator(workspace: Path) -> Orchestrator:
    """Orchestrator configured for the real workspace.

    `SITEOPS_EXTRA_SITES_DIRS` (os.pathsep-joined) is honored so the E2E
    workflow can inject a rendered site without touching `sites.local/`.
    """
    return Orchestrator(workspace, extra_trusted_sites_dirs=_extra_sites_dirs())


def _resolve_or_fail(
    orchestrator: Orchestrator, manifest_path: Path, selector: str | None
) -> tuple[Manifest, list]:
    """Resolve sites for a manifest, raising a diagnostic error on zero matches.

    The historical failure mode was a silent vacuous pass: selector resolved
    to an empty list, `deploy()` short-circuited with `sites={}`, and every
    test body's `for name in result["sites"]:` loop became a no-op. This
    helper makes that impossible at the fixture boundary.
    """
    manifest = Manifest.from_file(manifest_path, workspace_root=WORKSPACE_PATH)
    sites = orchestrator.resolve_sites(manifest, selector)
    if not sites:
        raise RuntimeError(
            f"Integration fixture resolved zero sites for manifest "
            f"'{manifest_path.name}' (selector={selector!r}, "
            f"manifest.selector={manifest.site_selector!r}, "
            f"manifest.sites={manifest.sites!r}, "
            f"extra_trusted_sites_dirs={[str(p) for p in _extra_sites_dirs()]}). "
            f"A zero-site integration run indicates a configuration mismatch "
            f"(missing overlay, wrong selector, broken inherits chain, or "
            f"label mismatch) and is treated as a hard failure rather than "
            f"a silent pass."
        )
    return manifest, sites


def _assert_deployed(result: dict, label: str) -> dict:
    """Fail with the scrubbed diagnostic fields rather than the whole result.

    A result carries fully-qualified resource ids on every site row, and a
    failing assertion in CI writes its message to a published job log and to the
    JUnit artifact. The engine already scrubs each site's `error` when the
    destination is published, so reporting those fields keeps the failure
    actionable without republishing what the scrub removed.
    """
    summary = result.get("summary", {})
    if summary.get("failed"):
        errors = [
            "  "
            f"{site_name_for_output(name)}: "
            f"{scrub_site_for_output(site.get('error', 'no error reported'), name)}"
            for name, site in (result.get("sites") or {}).items()
            if site.get("error")
        ]
        raise AssertionError(
            f"{label} deployment failed. Summary: {summary}\n" + "\n".join(errors)
        )
    return result


@pytest.fixture(scope="session")
def aio_install_result(orchestrator: Orchestrator, selector: str | None) -> dict:
    """Deploy aio-install.yaml once, shared by all dependent tests.

    Upgrade phase short-circuits to a sentinel: aio-install is desired-state,
    so re-running it at a new release against an existing instance can
    overwrite operator config on the live instance.
    """
    if _is_upgrade_phase():
        return _UPGRADE_PHASE_INSTALL_SENTINEL

    manifest_path = WORKSPACE_PATH / "manifests" / "aio-install.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    return _assert_deployed(result, "aio-install")

@pytest.fixture(scope="session")
def secretsync_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy secretsync.yaml after AIO is installed."""
    manifest_path = WORKSPACE_PATH / "manifests" / "secretsync.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )


@pytest.fixture(scope="session")
def opc_ua_solution_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy samples/opc-ua-solution/manifest.yaml after AIO is installed."""
    manifest_path = WORKSPACE_PATH / "samples" / "opc-ua-solution" / "manifest.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )


@pytest.fixture(scope="session")
def dataflow_sample_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy samples/dataflow-sample/manifest.yaml after AIO is installed.

    The sample attaches its own declaration at manifest level, so this covers
    the catalog templates without depending on a site carrying a
    `resourceSets` selection.
    """
    manifest_path = WORKSPACE_PATH / "samples" / "dataflow-sample" / "manifest.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    return _assert_deployed(result, "dataflow-sample")


@pytest.fixture(scope="session")
def aio_resources_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy `aio-resources.yaml` with one set selected per resource area.

    This is the fleet route: a site names a committed set through
    `properties.resourceSets.<area>`, the catalog resolves that to a
    definition file, and the deployment-family gate opens. `dataflow_sample_result`
    covers the dataflow templates through a manifest-attached declaration, so
    this fixture exists for the selection mechanism rather than for the
    templates.

    Every resource area is selected in one deploy. The
    catalog is a single entry point whose steps are independently gated, so
    selecting every area shows both deployment families opening and ordering
    correctly.

    The sets are applied to the resolved sites here rather than in the site
    template, so a default run omits every resource area and pays
    nothing for a phase it did not select. `Orchestrator` caches and returns the same
    `Site` objects to every fixture in the session, so the original properties
    are restored afterwards. Without that, a later fixture would resolve a site
    that permanently selects these sets.
    """
    manifest_path = WORKSPACE_PATH / "manifests" / "aio-resources.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)

    original = {id(site): copy.deepcopy(site.properties) for site in sites}
    for site in sites:
        selections = site.properties.setdefault("resourceSets", {})
        for family, selected in CATALOG_SELECTIONS:
            selections[family] = [selected]

    try:
        result = orchestrator.deploy(
            manifest_path=manifest_path,
            manifest=manifest,
            sites=sites,
        )
        return _assert_deployed(result, "aio-resources")
    finally:
        for site in sites:
            site.properties.clear()
            site.properties.update(original[id(site)])


@pytest.fixture(scope="session")
def aio_upgrade_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy aio-upgrade.yaml after AIO is installed.

    Without an aioRelease bump, the upgrade is a no-op same-version re-PUT
    that exercises the resolve-then-update round-trip and asserts that
    extension identity, configurationSettings, and releaseNamespace are
    preserved.
    """
    manifest_path = WORKSPACE_PATH / "manifests" / "aio-upgrade.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    return _assert_deployed(result, "aio-upgrade")


# Test override keys injected by aio_upgrade_with_overrides_result. Exposed at
# module scope so TestExtensionAdditiveOverrides can assert against them
# without re-declaring values.
TEST_OVERRIDE_AIO_KEY = "siteopsTestOverrideAio"
TEST_OVERRIDE_AIO_VALUE = "siteops-test-aio-value"
TEST_OVERRIDE_SECRET_STORE_KEY = "siteopsTestOverrideSecretStore"
TEST_OVERRIDE_SECRET_STORE_VALUE = "siteops-test-secretstore-value"
TEST_OVERRIDE_CERT_MANAGER_KEY = "siteopsTestOverrideCertManager"
TEST_OVERRIDE_CERT_MANAGER_VALUE = "siteops-test-certmanager-value"


@pytest.fixture(scope="session")
def aio_upgrade_with_overrides_result(
    orchestrator: Orchestrator,
    selector: str | None,
    aio_install_result: dict,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    """Deploy aio-upgrade.yaml with non-empty `configurationOverrides` on every
    extension. Exercises the `union(existing, overrides)` additive path in
    update-extensions.bicep so tests can assert pre-PUT keys are preserved
    AND override keys are added.

    Implementation: write a tmp parameter file with known override keys, load
    aio-upgrade.yaml, append the tmp file to update-extensions' parameter
    chain. No production manifest mutation, no fixture-manifest duplication.

    Independent of aio_upgrade_result so test ordering does not matter. Both
    fixtures use additive `union()` semantics, so cross-test contamination on
    the shared cluster is safe.
    """
    overrides_dir = tmp_path_factory.mktemp("siteops-aio-upgrade-test-overrides")
    overrides_path = overrides_dir / "extension-overrides.yaml"
    overrides_path.write_text(
        yaml.safe_dump(
            {
                "aioConfigurationOverrides": {
                    TEST_OVERRIDE_AIO_KEY: TEST_OVERRIDE_AIO_VALUE,
                },
                "secretStoreConfigurationOverrides": {
                    TEST_OVERRIDE_SECRET_STORE_KEY: TEST_OVERRIDE_SECRET_STORE_VALUE,
                },
                "certManagerConfigurationOverrides": {
                    TEST_OVERRIDE_CERT_MANAGER_KEY: TEST_OVERRIDE_CERT_MANAGER_VALUE,
                },
            }
        ),
        encoding="utf-8",
    )

    manifest_path = WORKSPACE_PATH / "manifests" / "aio-upgrade.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)

    # Append the tmp overrides file to update-extensions' parameter list.
    # Absolute path bypasses workspace-relative resolution.
    injected = False
    for step in manifest.steps:
        if step.name == "update-extensions":
            step.parameters.append(str(overrides_path))
            injected = True
            break
    if not injected:
        raise RuntimeError(
            "aio_upgrade_with_overrides_result: aio-upgrade.yaml has no "
            "step named 'update-extensions' to inject overrides into. "
            "Manifest structure changed. Update the fixture."
        )

    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    _assert_deployed(result, "aio-upgrade-with-overrides")
    return result


@pytest.fixture(scope="session")
def sync_secret_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy samples/secretsync-sample/manifest.yaml after AIO is installed.

    The sample composes resolve-aio + enable-secretsync + sync-secrets,
    exercising the full secret-sync data path through to the cluster.
    Cluster-side assertions live in test_sync_secrets_manifest.py and
    individually depend on the `kubectl_available` fixture so a missing
    kubectl on local runs only skips the cluster reads, not the deploy.
    """
    manifest_path = WORKSPACE_PATH / "samples" / "secretsync-sample" / "manifest.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )


@pytest.fixture(scope="session")
def kubectl_available() -> None:
    """Skip (or hard-fail in CI) if `kubectl` cannot reach a cluster.

    Tests that read from the cluster (custom resources, materialized
    Secrets, pod readiness) depend on this fixture. Local development
    without a cluster gets a clean skip. CI must never silently skip
    because a misconfigured workflow could otherwise drop the entire
    new test surface without anyone noticing.
    """
    from tests.integration.helpers.kube import is_available

    if is_available():
        return
    if _in_ci():
        pytest.fail(
            "kubectl is required in CI but is unavailable or cannot reach a "
            "cluster. Check the runner's kubeconfig and that k3s is running. "
            "Skipping these tests in CI is not allowed."
        )
    pytest.skip("kubectl unavailable, skipping cluster-dependent tests")


@pytest.fixture(scope="session")
def aio_namespace(aio_install_result: dict) -> str:
    """The namespace where AIO operators and SecretSync targets live.

    Extracted from the resolve-aio step's customLocationNamespace output
    so the fixture tracks the actual deployment rather than a hardcoded
    constant. Falls back to `azure-iot-operations` (the AIO RP convention)
    when resolve-aio did not run (e.g., enableSecretSync=false sites or
    upgrade-phase sentinel).
    """
    from tests.integration.helpers.assertions import find_step

    DEFAULT = "azure-iot-operations"
    sites = aio_install_result.get("sites", {})
    if not sites:
        return DEFAULT
    site_name = next(iter(sites))
    try:
        resolve_step = find_step(aio_install_result, site_name, "resolve-aio")
    except (ValueError, KeyError):
        return DEFAULT
    outputs = resolve_step.get("outputs", {})
    ns = outputs.get("customLocationNamespace")
    if isinstance(ns, dict):
        ns = ns.get("value")
    return ns or DEFAULT

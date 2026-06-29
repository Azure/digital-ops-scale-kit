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

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from siteops.models import Manifest
from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"
SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "generate-site-overrides.py"

_EXTRA_SITES_DIRS_ENV = "SITEOPS_EXTRA_SITES_DIRS"
_UPGRADE_PHASE_ENV = "SITEOPS_E2E_UPGRADE_PHASE"

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
    "TestAioUpgradeResolveExtensions",
    "TestAioUpgradeSelfConsistency",
    "TestAioUpgradeIdempotency",
    "TestAioExtensionInvariants",
    "TestSecretStoreExtensionInvariants",
    "TestCertManagerExtensionInvariants",
    "TestExtensionAdditiveOverrides",
})


def _is_upgrade_phase() -> bool:
    return os.environ.get(_UPGRADE_PHASE_ENV, "").strip() in ("1", "true", "yes")


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
    )
    if result.returncode != 0:
        print(f"generate-site-overrides.py failed: {result.stderr}", file=sys.stderr)
        return False

    return True


_pre_existing_overlays: set[str] = set()
_generated_overlays = False


def pytest_collection_modifyitems(config, items):
    """Generate overlays from SITE_OVERRIDES and skip if no config available."""
    global _generated_overlays, _pre_existing_overlays

    # Snapshot existing overlay files before generation
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
        skip = pytest.mark.skip(
            reason="Integration tests require sites.local/ overlays, "
            "SITE_OVERRIDES, or SITEOPS_EXTRA_SITES_DIRS with site files"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)

    if _is_upgrade_phase():
        skip_upgrade = pytest.mark.skip(
            reason=f"{_UPGRADE_PHASE_ENV} active: only upgrade-step tests run "
            f"in this phase (install fixtures are stubbed)"
        )
        for item in items:
            if "integration" not in item.keywords:
                continue
            cls = getattr(item, "cls", None)
            cls_name = cls.__name__ if cls is not None else None
            if cls_name not in _UPGRADE_PHASE_ALLOWED_CLASSES:
                item.add_marker(skip_upgrade)


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
    assert result["summary"]["failed"] == 0, (
        f"aio-install deployment failed: {result}"
    )
    return result


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
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )


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
            "Manifest structure changed; update the fixture."
        )

    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    assert result["summary"]["failed"] == 0, (
        f"aio-upgrade-with-overrides deployment failed: {result}"
    )
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
    in_ci = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    if in_ci:
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


"""Integration tests for the aio-upgrade.yaml manifest.

The fixture chain is `aio_install_result` -> `aio_upgrade_result` so the
upgrade runs against a freshly installed instance. Without an aioRelease
bump the upgrade is a same-version re-PUT, which is the right shape for a
round-trip test: it must preserve extension identity, configurationSettings,
and releaseNamespace rather than trigger a full-replace.
"""

from pathlib import Path

import pytest

from siteops.models import Manifest
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_output_starts_with,
    assert_step_succeeded,
    find_step,
)

pytestmark = [pytest.mark.integration]

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"


class TestAioUpgradeDeployment:
    """Validate that aio-upgrade.yaml deploys successfully end-to-end."""

    def test_no_failures(self, aio_upgrade_result):
        assert aio_upgrade_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, aio_upgrade_result):
        for name, site in aio_upgrade_result["sites"].items():
            assert site["status"] == "success", (
                f"Site '{name}' failed: {site.get('error')}"
            )
            assert site["steps_completed"] == 3

    def test_all_phases_run(self, aio_upgrade_result):
        expected = ("resolve-aio", "resolve-extensions", "update-extensions")
        for name in aio_upgrade_result["sites"]:
            for step_name in expected:
                assert_step_succeeded(aio_upgrade_result, name, step_name)


class TestAioUpgradeResolveExtensions:
    """Validate the snapshot outputs from resolve-extensions."""

    def test_aio_snapshot_fields(self, aio_upgrade_result):
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            aio = assert_output_exists(step, "aio")
            for key in ("id", "name", "extensionType", "version", "releaseTrain",
                        "configurationSettings", "identity", "releaseNamespace"):
                assert key in aio, f"Site '{name}': aio snapshot missing '{key}': {aio}"
            assert aio["id"].startswith("/subscriptions/"), aio["id"]
            # configurationSettings must be non-empty: union(empty, overrides)
            # would silently wipe operator-applied config on the upgrade PUT.
            assert aio["configurationSettings"], (
                f"Site '{name}': aio.configurationSettings is empty; "
                f"upgrade would wipe operator config"
            )

    def test_aio_release_namespace_non_empty(self, aio_upgrade_result):
        """Snapshot must populate releaseNamespace; empty risks destructive PUT."""
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            aio = assert_output_exists(step, "aio")
            assert aio["releaseNamespace"], (
                f"Site '{name}': aio.releaseNamespace must be non-empty "
                f"(got {aio['releaseNamespace']!r})"
            )

    def test_secret_store_snapshot_fields(self, aio_upgrade_result):
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            secret_store = assert_output_exists(step, "secretStore")
            for key in ("id", "name", "extensionType", "version", "releaseTrain",
                        "configurationSettings", "identity"):
                assert key in secret_store, (
                    f"Site '{name}': secretStore snapshot missing '{key}'"
                )
            assert secret_store["configurationSettings"], (
                f"Site '{name}': secretStore.configurationSettings is empty; "
                f"upgrade would wipe operator config"
            )

    def test_cert_manager_snapshot_shape(self, aio_upgrade_result):
        """resolve-extensions returns a uniform certManager snapshot whether
        the extension is installed or not. When `enableCertManager` is true
        the snapshot is populated; when false it is the zero-valued shape."""
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            cert_manager = assert_output_exists(step, "certManager")
            for key in ("id", "name", "extensionType", "version", "releaseTrain",
                        "configurationSettings", "identity"):
                assert key in cert_manager, (
                    f"Site '{name}': certManager snapshot missing '{key}'"
                )


class TestAioUpgradePreservation:
    """Round-trip: install -> upgrade must preserve extension identity and config."""

    def test_aio_extension_id_preserved(self, aio_install_result, aio_upgrade_result):
        """A full-replace would mint a new resource id. Same id = in-place PUT."""
        for name in aio_upgrade_result["sites"]:
            install_step = assert_step_succeeded(aio_install_result, name, "aio-instance")
            install_aio = assert_output_exists(install_step, "aioExtension")
            install_id = install_aio["id"]

            upgrade_step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            upgrade_id = assert_output_exists(upgrade_step, "aioExtensionId")

            assert install_id == upgrade_id, (
                f"Site '{name}': AIO extension id changed across upgrade "
                f"({install_id!r} -> {upgrade_id!r}); upgrade is replacing not patching"
            )

    def test_secret_store_extension_id_preserved(self, aio_install_result, aio_upgrade_result):
        for name in aio_upgrade_result["sites"]:
            install_step = assert_step_succeeded(aio_install_result, name, "aio-enablement")
            install_extensions = assert_output_exists(install_step, "extensions")
            install_id = install_extensions["secretStore"]["id"]

            upgrade_step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            upgrade_id = assert_output_exists(upgrade_step, "secretStoreExtensionId")

            assert install_id == upgrade_id, (
                f"Site '{name}': secret store extension id changed across upgrade "
                f"({install_id!r} -> {upgrade_id!r})"
            )

    def test_aio_version_preserved_when_no_release_bump(self, aio_install_result, aio_upgrade_result):
        """Same aioRelease across install and upgrade means the applied
        version equals the resolved snapshot version."""
        for name in aio_upgrade_result["sites"]:
            resolve_step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            resolved_version = assert_output_exists(resolve_step, "aio")["version"]

            update_step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            applied_version = assert_output_exists(update_step, "aioVersionApplied")

            assert applied_version == resolved_version, (
                f"Site '{name}': aioVersionApplied {applied_version!r} != "
                f"resolved version {resolved_version!r}"
            )

    def test_update_extensions_outputs(self, aio_upgrade_result):
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            assert_output_starts_with(step, "aioExtensionId", "/subscriptions/")
            assert_output_starts_with(step, "secretStoreExtensionId", "/subscriptions/")
            assert_output_exists(step, "aioVersionApplied")
            assert_output_exists(step, "secretStoreVersionApplied")


class TestAioUpgradeSelfConsistency:
    """Cross-step consistency checks on upgrade outputs alone.

    Allowlisted for upgrade-phase E2E (cross-version: install on release A,
    then upgrade to release B in a separate run). These tests must not read
    `aio_install_result` content because the install was a separate run; the
    fixture is replaced by a sentinel during upgrade phase.
    """

    def test_update_extensions_aio_id_matches_resolve(self, aio_upgrade_result):
        """update-extensions writes back the AIO extension id it patched.
        It must equal the id resolve-extensions discovered in the same run.
        """
        for name in aio_upgrade_result["sites"]:
            resolve_step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            resolved_id = assert_output_exists(resolve_step, "aio")["id"]

            update_step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            applied_id = assert_output_exists(update_step, "aioExtensionId")

            assert resolved_id == applied_id, (
                f"Site '{name}': update-extensions patched {applied_id!r} but "
                f"resolve-extensions discovered {resolved_id!r}; upgrade is "
                f"writing to the wrong extension"
            )

    def test_update_extensions_secret_store_id_matches_resolve(self, aio_upgrade_result):
        for name in aio_upgrade_result["sites"]:
            resolve_step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            resolved_id = assert_output_exists(resolve_step, "secretStore")["id"]

            update_step = assert_step_succeeded(aio_upgrade_result, name, "update-extensions")
            applied_id = assert_output_exists(update_step, "secretStoreExtensionId")

            assert resolved_id == applied_id, (
                f"Site '{name}': update-extensions patched secret store "
                f"{applied_id!r} but resolve discovered {resolved_id!r}"
            )

    def test_resolve_snapshots_non_empty(self, aio_upgrade_result):
        """Snapshots feed `union(snapshot, overrides)` in update-extensions.
        Empty snapshots would silently wipe operator config on the PUT.
        """
        for name in aio_upgrade_result["sites"]:
            step = assert_step_succeeded(aio_upgrade_result, name, "resolve-extensions")
            aio = assert_output_exists(step, "aio")
            secret_store = assert_output_exists(step, "secretStore")
            assert aio["configurationSettings"], (
                f"Site '{name}': aio.configurationSettings empty in upgrade snapshot"
            )
            assert secret_store["configurationSettings"], (
                f"Site '{name}': secretStore.configurationSettings empty in upgrade snapshot"
            )


class TestAioUpgradeIdempotency:
    """Re-running the upgrade against an already-upgraded instance is a no-op."""

    def test_redeploy_succeeds_with_stable_ids_and_versions(
        self, orchestrator, selector, aio_upgrade_result
    ):
        manifest_path = WORKSPACE_PATH / "manifests" / "aio-upgrade.yaml"
        manifest = Manifest.from_file(manifest_path, workspace_root=WORKSPACE_PATH)
        sites = orchestrator.resolve_sites(manifest, selector)
        result2 = orchestrator.deploy(
            manifest_path=manifest_path,
            manifest=manifest,
            sites=sites,
        )
        assert result2["summary"]["failed"] == 0

        for name in aio_upgrade_result["sites"]:
            step1 = find_step(aio_upgrade_result, name, "update-extensions")
            step2 = find_step(result2, name, "update-extensions")
            for output_name in (
                "aioExtensionId",
                "secretStoreExtensionId",
                "aioVersionApplied",
                "secretStoreVersionApplied",
            ):
                v1 = assert_output_exists(step1, output_name)
                v2 = assert_output_exists(step2, output_name)
                assert v1 == v2, (
                    f"Site '{name}': {output_name} changed on re-upgrade "
                    f"({v1!r} -> {v2!r})"
                )

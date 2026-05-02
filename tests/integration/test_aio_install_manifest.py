"""Integration tests for the aio-install.yaml manifest."""

import pytest

from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_skipped,
    assert_step_succeeded,
    find_step,
)
from tests.integration.conftest import WORKSPACE_PATH

pytestmark = [pytest.mark.integration]


class TestAioInstallDeployment:
    """Validate that aio-install.yaml deploys successfully."""

    def test_no_failures(self, aio_install_result):
        assert aio_install_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, aio_install_result):
        for name in aio_install_result["sites"]:
            site = aio_install_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"

    def test_schema_registry_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "schema-registry")
            assert_output_exists(step, "schemaRegistry")

    def test_adr_ns_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "adr-ns")
            assert_output_exists(step, "adrNamespace")

    def test_aio_enablement_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-enablement")
            assert_output_exists(step, "clExtensionIds")

    def test_aio_instance_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-instance")
            assert_output_exists(step, "aio")
            assert_output_exists(step, "customLocation")
            assert_output_exists(step, "aioExtension")

    def test_schema_registry_role_succeeds(self, aio_install_result):
        for name in aio_install_result["sites"]:
            assert_step_succeeded(aio_install_result, name, "schema-registry-role")


class TestAioInstallConditionalSteps:
    """Validate that conditional steps are gated correctly."""

    def test_global_edge_site_skipped_for_rg_sites(self, aio_install_result):
        """RG-level sites should skip the subscription-scoped edge site step."""
        for name in aio_install_result["sites"]:
            step = find_step(aio_install_result, name, "global-edge-site")
            assert step["status"] == "skipped", (
                f"Site '{name}': global-edge-site should be skipped for RG-level sites"
            )

    def test_secretsync_steps_skipped_when_disabled(
        self, aio_install_result, orchestrator
    ):
        """Sites with deployOptions.enableSecretSync=false should skip both
        secretsync steps embedded in aio-install.yaml (a regression guard for
        the E2E site template and anyone reusing the same deployOptions)."""
        for name in aio_install_result["sites"]:
            site = orchestrator.load_site(name)
            enabled = site.properties.get("deployOptions", {}).get("enableSecretSync", True)
            if enabled:
                continue
            assert_step_skipped(aio_install_result, name, "resolve-aio")
            assert_step_skipped(aio_install_result, name, "secretsync")


class TestAioInstallVersioning:
    """Validate that the AIO extension Azure actually deployed matches the
    versioned-templates contract (requested aioRelease selects a template dir
    that pins the extension version)."""

    def test_aio_extension_version_matches_version_config(
        self, aio_install_result, orchestrator
    ):
        """The bicep output `aioExtension.version` reflects
        `Microsoft.KubernetesConfiguration/extensions/.../properties/version`
        from Azure. Cross-check it against the `aioVersion` declared in the
        site's aio-releases config file (selected by the site's aioRelease).
        This is the primary regression guard for versioned-templates wiring.
        A drift here means the wrong template dispatched, even if everything
        else looks green.
        """
        import yaml

        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-instance")
            aio_extension = assert_output_exists(step, "aioExtension")
            assert isinstance(aio_extension, dict), (
                f"Site '{name}': aioExtension output is not an object: {aio_extension!r}"
            )
            deployed_version = aio_extension.get("version")
            assert deployed_version, (
                f"Site '{name}': aioExtension.version missing "
                f"(keys: {sorted(aio_extension.keys())})"
            )

            site = orchestrator.load_site(name)
            aio_release_key = site.properties.get("aioRelease")
            assert aio_release_key, f"Site '{name}': missing properties.aioRelease"

            version_config = (
                WORKSPACE_PATH / "parameters" / "aio-releases" / f"{aio_release_key}.yaml"
            )
            assert version_config.is_file(), (
                f"Site '{name}': version config not found: {version_config}"
            )
            expected = yaml.safe_load(version_config.read_text(encoding="utf-8"))["aioVersion"]
            assert deployed_version == expected, (
                f"Site '{name}': aio extension version drift. "
                f"expected {expected!r} (from {version_config.name}), "
                f"deployed {deployed_version!r}. The versioned-templates dispatch "
                f"selected the wrong API version or the version YAML is stale."
            )


class TestAioInstallIdempotency:
    """Validate that re-deploying produces the same results."""

    def test_redeploy_preserves_resource_ids(
        self, orchestrator, selector, aio_install_result
    ):
        """Re-deploying must not recreate core AIO resources. Recreation would
        break every downstream step (secretsync, opc-ua) that captured the
        original IDs. Mirrors the stability guard in the secretsync suite."""
        result2 = orchestrator.deploy(
            manifest_path=WORKSPACE_PATH / "manifests" / "aio-install.yaml",
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0

        for name in aio_install_result["sites"]:
            step1 = find_step(aio_install_result, name, "aio-instance")
            step2 = find_step(result2, name, "aio-instance")
            for output_name in ("aio", "customLocation", "aioExtension"):
                v1 = assert_output_exists(step1, output_name)
                v2 = assert_output_exists(step2, output_name)
                id1 = v1.get("id") if isinstance(v1, dict) else v1
                id2 = v2.get("id") if isinstance(v2, dict) else v2
                assert id1 == id2, (
                    f"Site '{name}': {output_name} resource ID changed on redeploy "
                    f"({id1!r} -> {id2!r})"
                )

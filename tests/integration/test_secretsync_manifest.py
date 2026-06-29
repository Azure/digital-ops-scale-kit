"""Integration tests for the secretsync.yaml manifest."""

import pytest

from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_output_starts_with,
    assert_step_succeeded,
    find_step,
)
from tests.integration.helpers.kube import KubectlError, kubectl_json

pytestmark = [pytest.mark.integration]


class TestSecretSyncDeployment:
    """Validate that secretsync.yaml deploys successfully."""

    def test_no_failures(self, secretsync_result):
        assert secretsync_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, secretsync_result):
        for name in secretsync_result["sites"]:
            site = secretsync_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"
            assert site["steps_completed"] == 2


class TestSecretSyncResolveAio:
    """Validate resolve-aio step outputs across all sites."""

    def test_resolve_aio_succeeds(self, secretsync_result):
        for name in secretsync_result["sites"]:
            assert_step_succeeded(secretsync_result, name, "resolve-aio")

    def test_infrastructure_outputs(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_exists(step, "customLocationName")
            assert_output_exists(step, "customLocationNamespace")
            assert_output_exists(step, "connectedClusterName")
            assert_output_starts_with(step, "customLocationId", "/subscriptions/")

    def test_oidc_issuer_url(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_starts_with(step, "oidcIssuerUrl", "https://")

    def test_instance_properties_forwarded(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_exists(step, "instanceLocation")
            assert_output_starts_with(step, "schemaRegistryResourceId", "/subscriptions/")
            assert_output_exists(step, "identityType")


class TestSecretSyncEnablement:
    """Validate secretsync step outputs across all sites."""

    def test_secretsync_succeeds(self, secretsync_result):
        for name in secretsync_result["sites"]:
            assert_step_succeeded(secretsync_result, name, "secretsync")

    def test_spc_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_starts_with(step, "spcResourceId", "/subscriptions/")
            assert_output_exists(step, "spcResourceName")

    def test_managed_identity_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "managedIdentityPrincipalId")
            assert_output_exists(step, "managedIdentityClientId")
            assert_output_starts_with(step, "managedIdentityResourceId", "/subscriptions/")

    def test_key_vault_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "keyVaultName")
            assert_output_starts_with(step, "keyVaultResourceId", "/subscriptions/")

    def test_federated_credential_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "federatedCredentialName")


class TestSecretSyncIdempotency:
    """Validate that re-deploying produces consistent results."""

    def test_redeploy_succeeds_with_same_outputs(self, orchestrator, selector, secretsync_result):
        """Every resource secretsync creates is expected to be idempotent. A
        regression where the MI, KV, or SPC silently gets recreated would
        break workload-identity federation and any dependent site."""
        result2 = orchestrator.deploy(
            manifest_path=WORKSPACE_PATH / "manifests" / "secretsync.yaml",
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0

        stable_outputs = (
            "spcResourceId",
            "managedIdentityResourceId",
            "keyVaultResourceId",
        )
        for name in secretsync_result["sites"]:
            step1 = find_step(secretsync_result, name, "secretsync")
            step2 = find_step(result2, name, "secretsync")
            for output_name in stable_outputs:
                v1 = assert_output_exists(step1, output_name)
                v2 = assert_output_exists(step2, output_name)
                assert v1 == v2, (
                    f"Site '{name}': {output_name} changed on redeploy "
                    f"({v1!r} -> {v2!r})"
                )


class TestSecretSyncInstanceAdoption:
    """Validate that the AIO instance adopted the deployed SPC as its
    default secret provider class.

    The enable-secretsync template updates the AIO instance's
    `defaultSecretProviderClassRef` after creating the SPC. Reading the
    AIO instance custom resource on the cluster proves the instance
    update path took effect (not just the SPC creation, which
    `TestSecretSyncEnablement` already covers).
    """

    def test_aio_instance_default_spc_ref(
        self,
        secretsync_result,
        aio_install_result,
        aio_namespace,
        kubectl_available,
    ):
        for name in secretsync_result["sites"]:
            install_step = find_step(aio_install_result, name, "aio-instance")
            aio = assert_output_exists(install_step, "aio")
            instance_name = aio.get("name") if isinstance(aio, dict) else None
            assert instance_name, (
                f"Site '{name}': aio-install aio-instance.aio has no `name` "
                f"field (got {aio!r})"
            )

            secretsync_step = find_step(secretsync_result, name, "secretsync")
            expected_spc = assert_output_exists(secretsync_step, "spcResourceName")

            try:
                instance = kubectl_json(
                    [
                        "get",
                        "instance.iotoperations.azure.com",
                        instance_name,
                        "-n",
                        aio_namespace,
                    ]
                )
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': could not read AIO instance custom "
                    f"resource '{instance_name}' in '{aio_namespace}': {e}"
                )

            ref = instance.get("spec", {}).get("defaultSecretProviderClassRef")
            actual = _extract_spc_name_from_ref(ref)
            assert actual == expected_spc, (
                f"Site '{name}': AIO instance defaultSecretProviderClassRef "
                f"resolved to ({actual!r}) but the deployed SPC was "
                f"({expected_spc!r}). Raw ref field: {ref!r}. "
                f"The enable-secretsync instance update did not take effect."
            )


def _extract_spc_name_from_ref(ref: object) -> str | None:
    """Extract the SPC short name from a defaultSecretProviderClassRef projection.

    AIO operator versions vary in how they project the ARM
    `defaultSecretProviderClassRef.resourceId` onto the cluster CR. Some
    versions expose a direct `name` field, others leave the full ARM
    `resourceId` (or just `id`) for callers to parse. This helper accepts
    either shape and returns just the trailing resource name, so the
    assertion is resilient across operator versions.
    """
    if not isinstance(ref, dict):
        return None
    name = ref.get("name")
    if name:
        return name
    resource_id = ref.get("resourceId") or ref.get("id")
    if isinstance(resource_id, str) and "/" in resource_id:
        return resource_id.rstrip("/").split("/")[-1]
    return None

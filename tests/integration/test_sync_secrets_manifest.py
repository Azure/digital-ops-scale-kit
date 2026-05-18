"""Integration tests for the secretsync-sample manifest.

Drives end-to-end coverage of the secret-sync data path. Scalekit writes
N Key Vault secrets, updates the default SPC's `properties.objects` to
include all of them, and creates one SecretSync ARM resource per entry.
The cluster-side SecretSync controller reads each Key Vault secret using
the managed identity and materializes a Kubernetes Secret on the cluster.
The canonical assertion in TestSyncSecretsMaterialize iterates over every
configured secret and asserts exact-bytes equality with the value supplied
to Bicep.
"""

import base64

import pytest

from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_succeeded,
)
from tests.integration.helpers.kube import (
    KubectlError,
    assert_secret_value_equals,
    dump_secretsync_status,
    get_secret,
    kubectl_json,
    wait_for_secret,
)

pytestmark = [pytest.mark.integration]

# Fixed sample values from
# `workspaces/iot-operations/parameters/inputs/sync-secrets.yaml`. Every
# materialized Kubernetes Secret is asserted to carry the matching value.
# Order matches the chaining file's `secrets` array.
SAMPLE_SECRETS = [
    {
        "secretName": "secretsync-sample-secret-a",
        "kubernetesSecretName": "secretsync-sample-secret-a",
        "kubernetesSecretKey": "secretsync-sample-secret-a",
        "value": "secretsync-sample-value-a",
    },
    {
        "secretName": "secretsync-sample-secret-b",
        "kubernetesSecretName": "secretsync-sample-app-b",
        "kubernetesSecretKey": "token",
        "value": "secretsync-sample-value-b",
    },
]


class TestSyncSecretsDeployment:
    """Validate that the secretsync-sample manifest deploys successfully."""

    def test_no_failures(self, sync_secret_result):
        assert sync_secret_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, sync_secret_result):
        for name in sync_secret_result["sites"]:
            site = sync_secret_result["sites"][name]
            assert site["status"] == "success", (
                f"Site '{name}' failed: {site.get('error')}"
            )
            # Manifest composes resolve-aio + secretsync + sync-secrets.
            assert site["steps_completed"] == 3


class TestSyncSecretsArmOutputs:
    """Validate the ARM-side outputs of the sync-secrets step."""

    def test_sync_secrets_step_succeeds(self, sync_secret_result):
        for name in sync_secret_result["sites"]:
            assert_step_succeeded(sync_secret_result, name, "sync-secrets")

    def test_outputs_present(self, sync_secret_result):
        for name in sync_secret_result["sites"]:
            step = assert_step_succeeded(sync_secret_result, name, "sync-secrets")
            assert_output_exists(step, "materializedSecrets")
            assert_output_exists(step, "secretCount")

    def test_secret_count_matches_sample(self, sync_secret_result):
        for name in sync_secret_result["sites"]:
            step = assert_step_succeeded(sync_secret_result, name, "sync-secrets")
            count = assert_output_exists(step, "secretCount")
            assert count == len(SAMPLE_SECRETS), (
                f"Expected {len(SAMPLE_SECRETS)} secrets, got {count}"
            )

    def test_materialized_secrets_match_sample(self, sync_secret_result):
        """Per-entry output metadata matches what the chaining file asks for."""
        expected_by_name = {s["secretName"]: s for s in SAMPLE_SECRETS}
        for name in sync_secret_result["sites"]:
            step = assert_step_succeeded(sync_secret_result, name, "sync-secrets")
            materialized = assert_output_exists(step, "materializedSecrets")
            actual_names = {entry["secretName"] for entry in materialized}
            assert actual_names == set(expected_by_name), (
                f"Site '{name}': materialized secret-name set mismatch. "
                f"Missing: {set(expected_by_name) - actual_names}. "
                f"Unexpected: {actual_names - set(expected_by_name)}."
            )
            for entry in materialized:
                expected = expected_by_name[entry["secretName"]]
                assert entry["kubernetesSecretName"] == expected["kubernetesSecretName"]
                assert entry["kubernetesSecretKey"] == expected["kubernetesSecretKey"]
                assert entry["secretSyncName"] == expected["kubernetesSecretName"]


class TestSyncSecretsCustomResources:
    """Validate the SecretSync custom resources are on the cluster.

    The SPC and SecretSync CRs are intermediaries that the controller
    reconciles. Their presence is a useful localizing signal when
    `TestSyncSecretsMaterialize` fails. Uses kubectl by resource
    shortname so the test is resilient to API-version changes in the
    SecretSync controller.
    """

    def test_secret_sync_crs_present(
        self, sync_secret_result, aio_namespace, kubectl_available
    ):
        for name in sync_secret_result["sites"]:
            step = assert_step_succeeded(sync_secret_result, name, "sync-secrets")
            materialized = assert_output_exists(step, "materializedSecrets")
            for entry in materialized:
                cr_name = entry["secretSyncName"]
                try:
                    kubectl_json(["get", "secretsync", cr_name, "-n", aio_namespace])
                except KubectlError as e:
                    pytest.fail(
                        f"SecretSync CR '{cr_name}' not retrievable in namespace "
                        f"'{aio_namespace}': {e}"
                    )

    def test_spc_present(
        self, sync_secret_result, aio_namespace, kubectl_available
    ):
        """The default Secret Provider Class (created by enable-secretsync
        and updated by sync-secrets to include all configured object names)
        backs SecretSync reconciliation. The Azure SecretSyncController
        extension projects the ARM
        `Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses`
        resource to a stock upstream `SecretProviderClass` CR in the
        `secrets-store.csi.x-k8s.io` group on the cluster."""
        for name in sync_secret_result["sites"]:
            step = assert_step_succeeded(sync_secret_result, name, "secretsync")
            spc_name = assert_output_exists(step, "spcResourceName")
            try:
                kubectl_json(
                    [
                        "get",
                        "secretproviderclass",
                        spc_name,
                        "-n",
                        aio_namespace,
                    ]
                )
            except KubectlError as e:
                pytest.fail(
                    f"SPC '{spc_name}' not retrievable in namespace "
                    f"'{aio_namespace}': {e}"
                )


class TestSyncSecretsMaterialize:
    """The canonical end-to-end assertion: every configured Key Vault value
    lands on the cluster as a Kubernetes Secret with exact-bytes equality."""

    def test_all_secrets_materialize_with_value(
        self, sync_secret_result, aio_namespace, kubectl_available
    ):
        """Wait for every configured SecretSync to materialize and assert
        each one carries the value supplied via Bicep. Proves the full data
        path: scalekit's KV writes, the SPC objects update, the federated
        identity exchange, the controller reads, and the Secret writes are
        all working end to end.

        Note: value comparison goes through `assert_secret_value_equals`
        so the failure message never echoes the materialized value. Do
        not replace with `assert actual == expected` in test variants
        that read real customer values from a real Key Vault.
        """
        expected_by_name = {s["secretName"]: s for s in SAMPLE_SECRETS}
        for site_name in sync_secret_result["sites"]:
            step = assert_step_succeeded(
                sync_secret_result, site_name, "sync-secrets"
            )
            materialized = assert_output_exists(step, "materializedSecrets")
            actual_names = {entry["secretName"] for entry in materialized}
            assert actual_names == set(expected_by_name), (
                f"Site '{site_name}': materialized secret-name set mismatch. "
                f"Missing: {set(expected_by_name) - actual_names}. "
                f"Unexpected: {actual_names - set(expected_by_name)}."
            )
            secretsync_step = assert_step_succeeded(
                sync_secret_result, site_name, "secretsync"
            )
            spc_name = assert_output_exists(secretsync_step, "spcResourceName")
            for entry in materialized:
                expected = expected_by_name[entry["secretName"]]
                k8s_name = entry["kubernetesSecretName"]
                k8s_key = entry["kubernetesSecretKey"]
                secretsync_name = entry["secretSyncName"]
                try:
                    secret = wait_for_secret(
                        k8s_name,
                        aio_namespace,
                        expected_key=k8s_key,
                        timeout=600,
                        interval=10,
                    )
                except TimeoutError as e:
                    diagnostic = dump_secretsync_status(
                        secretsync_name, spc_name, aio_namespace
                    )
                    pytest.fail(f"{e}\n\n{diagnostic}")
                encoded = secret["data"][k8s_key]
                actual = base64.b64decode(encoded).decode("utf-8")
                assert_secret_value_equals(
                    actual,
                    expected["value"],
                    context=(
                        f"Site='{site_name}' Secret='{k8s_name}' Key='{k8s_key}'"
                    ),
                )


class TestSyncSecretsIdempotency:
    """Re-deploying the sample preserves every materialized Secret value.

    A regression where the controller silently re-creates a Secret on
    every reconcile would create observable gaps for dependent workloads.
    Redeploy with the same inputs and assert exact-bytes equality on
    every materialized Secret.
    """

    def test_redeploy_preserves_secret_values(
        self,
        orchestrator,
        selector,
        sync_secret_result,
        aio_namespace,
        kubectl_available,
    ):
        manifest_path = (
            WORKSPACE_PATH / "samples" / "secretsync-sample" / "manifest.yaml"
        )
        result2 = orchestrator.deploy(
            manifest_path=manifest_path,
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0
        expected_by_name = {s["secretName"]: s for s in SAMPLE_SECRETS}
        for site_name in sync_secret_result["sites"]:
            step = assert_step_succeeded(result2, site_name, "sync-secrets")
            materialized = assert_output_exists(step, "materializedSecrets")
            actual_names = {entry["secretName"] for entry in materialized}
            assert actual_names == set(expected_by_name), (
                f"Site '{site_name}': materialized secret-name set mismatch on "
                f"redeploy. Missing: {set(expected_by_name) - actual_names}. "
                f"Unexpected: {actual_names - set(expected_by_name)}."
            )
            for entry in materialized:
                expected = expected_by_name[entry["secretName"]]
                k8s_name = entry["kubernetesSecretName"]
                k8s_key = entry["kubernetesSecretKey"]
                secret = get_secret(k8s_name, aio_namespace)
                assert secret is not None, (
                    f"Secret '{k8s_name}' missing in '{aio_namespace}' after redeploy"
                )
                encoded = secret.get("data", {}).get(k8s_key)
                assert encoded is not None, (
                    f"Secret '{k8s_name}' missing key '{k8s_key}' after redeploy"
                )
                actual = base64.b64decode(encoded).decode("utf-8")
                assert_secret_value_equals(
                    actual,
                    expected["value"],
                    context=(
                        f"Site='{site_name}' Secret='{k8s_name}' Key='{k8s_key}' "
                        f"(after redeploy)"
                    ),
                )


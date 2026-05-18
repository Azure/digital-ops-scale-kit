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
import json
import subprocess
import sys
import uuid

import pytest

from siteops.models import Manifest
from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_succeeded,
)
from tests.integration.helpers.kube import (
    KubectlError,
    assert_secret_value_equals,
    delete_resource,
    get_secret,
    kubectl_json,
    wait_for_secret,
)
from tests.integration.helpers.secretsync import dump_secretsync_status

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


class TestSyncSecretsExistingKvSecret:
    """Cover the `createInKv: false` branch of sync-secrets.bicep.

    The default sample exercises only `createInKv: true` (write to Key Vault
    then sync to the cluster). Customers who already manage Key Vault
    secrets out of band need the inverse path: scalekit must update the SPC
    objects list and create a SecretSync ARM resource pointing at the
    pre-existing Key Vault secret without re-writing it. This test
    pre-creates the Key Vault secret directly, then re-deploys
    sync-secrets.bicep with the full sample set plus the new entry marked
    `createInKv: false`, and asserts the value materializes on the cluster.

    The sample manifest cannot exercise this branch because siteops
    resolves chaining parameter files workspace-relative and we do not put
    test-only fixtures into the customer-facing workspace. The deploy is
    therefore driven via `az deployment group create` against the same
    bicep the customer-facing path uses.

    Cluster-state contract: this test runs the SPC through two PUTs. The
    first PUT writes SAMPLE_SECRETS + the new test entry. The second
    (cleanup) PUT writes SAMPLE_SECRETS only, restoring baseline before
    the test-only KV secret is purged so the SPC never carries a dangling
    objectName referencing a deleted secret. Existing tags on the SPC are
    read upfront and round-tripped through both PUTs so they are not
    wiped. Not safe to run under pytest-xdist alongside other secret-sync
    tests because the SPC name is global.
    """

    def test_existing_kv_secret_materializes(
        self,
        orchestrator,
        selector,
        sync_secret_result,
        aio_namespace,
        kubectl_available,
        tmp_path,
    ):
        manifest_path = (
            WORKSPACE_PATH / "samples" / "secretsync-sample" / "manifest.yaml"
        )
        manifest = Manifest.from_file(manifest_path, workspace_root=WORKSPACE_PATH)
        sites = orchestrator.resolve_sites(manifest, selector)
        site_by_name = {s.name: s for s in sites}

        # First site only. Multi-site materialization is already covered by
        # TestSyncSecretsMaterialize. A per-site loop would double the
        # deploy cost without adding coverage of the createInKv branch.
        site_name = next(iter(sync_secret_result["sites"]))
        site = site_by_name[site_name]

        resolve_aio_step = assert_step_succeeded(
            sync_secret_result, site_name, "resolve-aio"
        )
        custom_location_name = assert_output_exists(
            resolve_aio_step, "customLocationName"
        )
        instance_location = assert_output_exists(resolve_aio_step, "instanceLocation")

        secretsync_step = assert_step_succeeded(
            sync_secret_result, site_name, "secretsync"
        )
        kv_name = assert_output_exists(secretsync_step, "keyVaultName")
        spc_name = assert_output_exists(secretsync_step, "spcResourceName")
        mi_client_id = assert_output_exists(
            secretsync_step, "managedIdentityClientId"
        )

        # Round-trip the SPC's current tags so the PUT does not strip
        # whatever the prior siteops deploy stamped. The bicep applies one
        # tags object to the SPC, the KV writes, and every SecretSync, so
        # the SPC's tags are a faithful representation of baseline state.
        spc_resource_id = (
            f"/subscriptions/{site.subscription}"
            f"/resourceGroups/{site.resource_group}"
            f"/providers/Microsoft.SecretSyncController"
            f"/azureKeyVaultSecretProviderClasses/{spc_name}"
        )
        spc_show = subprocess.run(
            [
                "az",
                "resource",
                "show",
                "--ids",
                spc_resource_id,
                "--api-version",
                "2024-08-21-preview",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        spc_tags = json.loads(spc_show.stdout).get("tags") or {}

        suffix = uuid.uuid4().hex[:8]
        kv_secret_name = f"existing-test-{suffix}"
        k8s_secret_name = f"existing-test-{suffix}"
        k8s_secret_key = "value"
        secret_value = f"existing-value-{suffix}"

        bicep_path = (
            WORKSPACE_PATH / "templates" / "secretsync" / "sync-secrets.bicep"
        )
        sample_secrets_input = [
            {
                "secretName": s["secretName"],
                "kubernetesSecretName": s["kubernetesSecretName"],
                "kubernetesSecretKey": s["kubernetesSecretKey"],
            }
            for s in SAMPLE_SECRETS
        ]
        sample_values_input = {
            s["secretName"]: s["value"] for s in SAMPLE_SECRETS
        }

        def _az_deploy_sync_secrets(secrets, values, label):
            params = {
                "$schema": (
                    "https://schema.management.azure.com/schemas/2019-04-01/"
                    "deploymentParameters.json#"
                ),
                "contentVersion": "1.0.0.0",
                "parameters": {
                    "keyVaultName": {"value": kv_name},
                    "customLocationName": {"value": custom_location_name},
                    "spcName": {"value": spc_name},
                    "managedIdentityClientId": {"value": mi_client_id},
                    "instanceLocation": {"value": instance_location},
                    "secrets": {"value": secrets},
                    "secretValues": {"value": values},
                    "tags": {"value": spc_tags},
                },
            }
            params_path = tmp_path / f"sync-secrets-{label}.params.json"
            params_path.write_text(json.dumps(params))
            subprocess.run(
                [
                    "az",
                    "deployment",
                    "group",
                    "create",
                    "-g",
                    site.resource_group,
                    "--subscription",
                    site.subscription,
                    "-f",
                    str(bicep_path),
                    "-p",
                    f"@{params_path}",
                    "-o",
                    "none",
                    "--name",
                    f"sync-secrets-test-{suffix}-{label}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )

        try:
            subprocess.run(
                [
                    "az",
                    "keyvault",
                    "secret",
                    "set",
                    "--vault-name",
                    kv_name,
                    "--name",
                    kv_secret_name,
                    "--value",
                    secret_value,
                    "--subscription",
                    site.subscription,
                    "-o",
                    "none",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # PUT the SPC with SAMPLE_SECRETS plus the new createInKv:false
            # entry. The full union preserves the sample SecretSyncs already
            # established by sync_secret_result so they are not orphaned mid
            # test.
            test_secrets = sample_secrets_input + [
                {
                    "secretName": kv_secret_name,
                    "kubernetesSecretName": k8s_secret_name,
                    "kubernetesSecretKey": k8s_secret_key,
                    "createInKv": False,
                }
            ]
            _az_deploy_sync_secrets(
                test_secrets, sample_values_input, "with-existing"
            )

            try:
                secret = wait_for_secret(
                    k8s_secret_name,
                    aio_namespace,
                    expected_key=k8s_secret_key,
                    timeout=600,
                    interval=10,
                )
            except TimeoutError as e:
                diagnostic = dump_secretsync_status(
                    k8s_secret_name, spc_name, aio_namespace
                )
                pytest.fail(f"{e}\n\n{diagnostic}")
            encoded = secret["data"][k8s_secret_key]
            actual = base64.b64decode(encoded).decode("utf-8")
            assert_secret_value_equals(
                actual,
                secret_value,
                context=(
                    f"Site='{site_name}' Secret='{k8s_secret_name}' "
                    f"Key='{k8s_secret_key}' (createInKv:false)"
                ),
            )
        finally:
            # Restore the SPC objects list to baseline BEFORE deleting the
            # KV secret so the SPC never references a missing object name
            # (the SecretSync controller would error on the dangling ref
            # and pollute subsequent test status reads).
            try:
                _az_deploy_sync_secrets(
                    sample_secrets_input, sample_values_input, "restore"
                )
            except subprocess.CalledProcessError as e:
                sys.stderr.write(
                    f"[cleanup] baseline SPC restore failed (exit "
                    f"{e.returncode}): {e.stderr}\n"
                )

            secretsync_resource_id = (
                f"/subscriptions/{site.subscription}"
                f"/resourceGroups/{site.resource_group}"
                f"/providers/Microsoft.SecretSyncController"
                f"/secretSyncs/{k8s_secret_name}"
            )
            try:
                subprocess.run(
                    [
                        "az",
                        "resource",
                        "delete",
                        "--ids",
                        secretsync_resource_id,
                        "-o",
                        "none",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.CalledProcessError as e:
                sys.stderr.write(
                    f"[cleanup] SecretSync ARM delete failed (exit "
                    f"{e.returncode}): {e.stderr}\n"
                )

            try:
                delete_resource("secret", k8s_secret_name, aio_namespace)
            except KubectlError as e:
                sys.stderr.write(
                    f"[cleanup] K8s Secret delete failed: {e}\n"
                )

            try:
                subprocess.run(
                    [
                        "az",
                        "keyvault",
                        "secret",
                        "delete",
                        "--vault-name",
                        kv_name,
                        "--name",
                        kv_secret_name,
                        "--subscription",
                        site.subscription,
                        "-o",
                        "none",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.CalledProcessError as e:
                sys.stderr.write(
                    f"[cleanup] KV secret delete failed (exit "
                    f"{e.returncode}): {e.stderr}\n"
                )

            # Purge is best-effort. Without the
            # Microsoft.KeyVault/vaults/secrets/purge/action permission the
            # secret stays soft-deleted for the vault's retention period.
            # The uuid suffix makes collision on a re-run effectively
            # impossible regardless.
            try:
                subprocess.run(
                    [
                        "az",
                        "keyvault",
                        "secret",
                        "purge",
                        "--vault-name",
                        kv_name,
                        "--name",
                        kv_secret_name,
                        "--subscription",
                        site.subscription,
                        "-o",
                        "none",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.CalledProcessError as e:
                sys.stderr.write(
                    f"[cleanup] KV secret purge failed (exit "
                    f"{e.returncode}): {e.stderr}\n"
                )


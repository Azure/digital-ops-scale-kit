# secretsync-sample

Reference scenario that synchronizes a set of Key Vault secrets to Kubernetes Secrets on the target AIO cluster. Demonstrates the full secret-sync data path end to end, including multiple secrets in one deploy and the pattern for syncing a secret that already exists in the Key Vault.

## What this sample does

1. **resolve-aio**: reads instance and custom-location names from the existing AIO instance.
2. **secretsync** (`enable-secretsync`): provisions the secretsync infrastructure on the resource group: a user-assigned managed identity, a Key Vault, role assignments, a federated identity credential, and the default Secret Provider Class. Updates the AIO instance to point at the new SPC as its default.
3. **sync-secrets** (`sync-secrets.bicep`): writes the configured Key Vault secrets, updates the default SPC's `properties.objects` to include every entry, and creates one SecretSync ARM resource per entry.

The cluster-side SecretSync controller resolves each SecretSync, exchanges its OIDC token for an Azure AD token via the federated identity credential, reads the Key Vault secret using the managed identity, and writes the value into a Kubernetes Secret on the cluster. Materialized Secrets are consumable by AIO workloads in the AIO namespace.

## Prerequisites

- AIO must be installed on the target cluster. Run `aio-install` first.
- The site's `aioRelease` must point to a release config under `parameters/aio-releases/`.

## Configure before deploying

The sync-secrets template treats the `secrets` array as the desired state. Each deploy PUTs the SPC with the union of all entries and creates one SecretSync per entry. Edit `parameters/inputs/sync-secrets.yaml` (or override in a `sites.local/` overlay) to declare the secrets you want synced and supply their values.

```yaml
# parameters/inputs/sync-secrets.yaml (or sites.local/ overlay)
secrets:
  - secretName: db-password
  - secretName: api-key
    kubernetesSecretName: my-app-credentials
    kubernetesSecretKey: key
  - secretName: license-token
    createInKv: false  # already in the Key Vault, sync only

secretValues:
  db-password: "{{ env.DB_PASSWORD }}"
  api-key: "{{ env.API_KEY }}"
  # license-token omitted because createInKv is false
```

Per-entry fields:

- **`secretName`** (required): the Key Vault secret name. Also the default Kubernetes Secret name and key. Must be unique within the array.
- **`kubernetesSecretName`** (optional): override when the consuming workload expects a different Kubernetes Secret name.
- **`kubernetesSecretKey`** (optional): override when the consuming workload expects a different key inside the Secret.
- **`createInKv`** (optional, default `true`): set `false` to sync a secret that already exists in the Key Vault. Skip the corresponding entry in `secretValues`.

Supply `secretValues` via a `sites.local/` overlay or a CI/CD secret store. Do not commit real values to source control.

## Deploy

```bash
siteops -w workspaces/iot-operations deploy samples/secretsync-sample/manifest.yaml -l environment=dev
```

The defaults shipped in `parameters/inputs/sync-secrets.yaml` are placeholder values intended for a first-run smoke test against a throwaway environment. Override them per the section above before deploying anywhere you care about.

## Verifying the result

After deploy, inspect a materialized Kubernetes Secret with `kubectl`:

```bash
kubectl get secret <kubernetesSecretName> -n azure-iot-operations -o yaml
```

The integration test `tests/integration/test_sync_secrets_manifest.py` asserts every configured secret materializes with the value supplied at deploy time.

## Removing a secret

Remove its entry from `secrets` and re-deploy. The SPC will be PUT without that entry, so the cluster-side controller stops syncing it. Note that Bicep Incremental mode does NOT delete the corresponding `Microsoft.SecretSyncController/secretSyncs` ARM resource. To fully clean up:

```bash
az resource delete --ids <secretSyncResourceId>
```

## Authoritative writes to the SPC

`sync-secrets.bicep` is authoritative for the default SPC's `properties.objects` field. Each deploy PUTs the SPC with the union of every entry in `secrets`, replacing whatever was there before. Two implications worth knowing for day-2 operations:

- **Re-running enablement clears the SPC objects.** Redeploying `enable-secretsync` (or composing manifests like `secretsync.yaml` standalone, or `aio-install.yaml` with `enableSecretSync` true) PUTs the SPC without an `objects` field, so the controller stops materializing every Kubernetes Secret with the error `the secretproviderclass parameters does not have a valid objects field`. Re-run the sample after any enablement redeploy.
- **CLI-managed entries are dropped on Bicep redeploy.** Entries added out of band via `az iot ops secretsync secret set` are removed the next time this sample runs. Pick one source of truth per cluster.

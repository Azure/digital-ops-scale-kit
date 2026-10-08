# secretsync-sample

Reference sample that synchronizes a set of Key Vault secrets to Kubernetes Secrets on the AIO cluster. Demonstrates the full Secret Sync data path end to end, including multiple secrets in one deploy and the pattern for syncing a secret that already exists in the Key Vault.

## What this sample does

1. **resolve-aio**: reads instance and custom location names from the existing AIO instance.
2. **secretsync** (`enable-secretsync`): provisions the Secret Sync infrastructure on the resource group: a user-assigned managed identity, a Key Vault, role assignments, a federated identity credential, and the default Secret Provider Class. Updates the AIO instance to point at the new SPC as its default.
3. **sync-secrets** (`sync-secrets.bicep`): writes the configured Key Vault secrets, updates the default SPC's `properties.objects` to include every entry, and creates one SecretSync ARM resource per distinct `kubernetesSecretName`. Entries that share a `kubernetesSecretName` are grouped into one Kubernetes Secret with multiple keys.

The SecretSync controller on the cluster resolves each SecretSync, exchanges its OIDC token for an Azure AD token via the federated identity credential, reads the Key Vault secret using the managed identity, and writes the value into a Kubernetes Secret on the cluster. Materialized Secrets are consumable by AIO workloads in the AIO namespace.

## Prerequisites

- AIO must be installed on the cluster. Run `aio-install` first.
- The connected cluster must already have an OIDC issuer and workload identity enabled. This sample runs Secret Sync enablement, which depends on both.
- The Site's `aioRelease` must point to an AIO release config under `parameters/aio-releases/`.

## Configure before deploying

The sync-secrets template treats the `secrets` array as the desired state. Each deploy PUTs the SPC with the union of all entries and emits one SecretSync per distinct `kubernetesSecretName`. Edit `secrets.yaml` (or override in a `sites.local/` overlay) to declare the secrets you want synced and supply their values.

```yaml
# samples/secretsync-sample/secrets.yaml (or sites.local/ overlay)
secrets:
  # Single-key Secret: one Key Vault secret -> one Kubernetes Secret with one key.
  - secretName: db-password

  # Renamed Kubernetes Secret + renamed key: one Key Vault secret ->
  # Kubernetes Secret `my-app-credentials` with one key `key`.
  - secretName: api-key
    kubernetesSecretName: my-app-credentials
    kubernetesSecretKey: key

  # Bring-your-own Key Vault secret: skip the Key Vault write, sync only.
  - secretName: license-token
    createInKv: false

  # Multi-key Secret: three Key Vault secrets grouped into one Kubernetes
  # Secret `database-credentials` with keys `host`, `username`, `password`.
  - secretName: my-db-host-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: host
  - secretName: my-db-username-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: username
  - secretName: my-db-password-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: password

secretValues:
  # Placeholders only. Real values come from an overlay or CI, see
  # "Supplying real values" below.
  db-password: "replace-me"
  api-key: "replace-me"
  # license-token omitted because createInKv is false
  my-db-host-kv: "replace-me"
  my-db-username-kv: "replace-me"
  my-db-password-kv: "replace-me"
```

Fields of each item:

- **`secretName`** (required): the Key Vault secret name. Also the default Kubernetes Secret name and key. Must be unique within the array.
- **`kubernetesSecretName`** (optional): override when the consuming workload expects a different Kubernetes Secret name. Multiple entries that set the same value are grouped into one Secret with multiple keys.
- **`kubernetesSecretKey`** (optional): override when the consuming workload expects a different key inside the Secret. Must be unique within a group of entries that share a `kubernetesSecretName`.
- **`createInKv`** (optional, default `true`): set `false` to sync a secret that already exists in the Key Vault. Skip the corresponding entry in `secretValues`.

Workspace tests enforce the uniqueness rules for names and for Secret name and
key pairs in the committed declaration. A `sites.local/` overlay or another
array that a caller provides must preserve them because the template does not reject duplicates at
deployment time.

Supply `secretValues` via a `sites.local/` overlay or a CI/CD secret store. Do not commit real values to source control.

### Supplying real values

`secrets.yaml` attaches at manifest level, which sits below Site parameters in the merge order, so a Site overrides it. Locally, drop a gitignored overlay next to the Site you deploy to:

```yaml
# workspaces/iot-operations/sites.local/munich-dev.yaml
parameters:
  secretValues:
    db-password: "the-real-password"
    api-key: "the-real-api-key"
```

In CI, set the `SITE_OVERRIDES` secret. Keys in dot notation expand into the same overlay:

```json
{
  "munich-dev": {
    "parameters.secretValues.db-password": "the-real-password",
    "parameters.secretValues.api-key": "the-real-api-key"
  }
}
```

Two behaviors are worth knowing when you override:

- `secretValues` is a map, so an overlay merges key by key. Supply only the values you want to replace and the rest of the declared defaults stay in place.
- `secrets` is a list, so an overlay replaces it wholesale. Restate the entries you want when you override it.

`siteops sites <name> --output yaml` masks the whole `secretValues` map.
The remaining configuration is still private inspection output.

## Deploy

```bash
siteops -w workspaces/iot-operations plan samples/secretsync-sample/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy samples/secretsync-sample/manifest.yaml -l name=<site>
```

Replace `<site>` with the configured Site. The explicit selector replaces the
manifest's `environment=dev` default.

The defaults shipped in `secrets.yaml` are placeholder values intended for a first smoke test against a disposable environment. Override them as the section above describes before deploying anywhere you care about.

## Verifying the result

After deploy, inspect a materialized Kubernetes Secret with `kubectl`:

```bash
kubectl get secret <kubernetesSecretName> -n azure-iot-operations -o yaml
```

The integration test `tests/integration/test_sync_secrets_manifest.py` asserts every configured secret materializes with the value supplied at deploy time.

## Removing a secret

Remove its entry from `secrets` and redeploy. The SPC will be PUT without that entry, so the controller on the cluster stops syncing it. Bicep incremental mode does not delete the corresponding `Microsoft.SecretSyncController/secretSyncs` ARM resource. To fully clean up:

```bash
az resource delete --ids <secretSyncResourceId>
```

## Writes to the SPC

The default SPC is written by both `enable-secretsync.bicep` and `sync-secrets.bicep`, and an ARM PUT replaces the fields it omits. Enablement does not own `properties.objects`. It writes the shared derivation when the Site declares secrets, and otherwise reads the current value off the class and writes it back, so a platform install never drops an object list it did not set.

Two implications worth knowing for ongoing operations:

- **Declaring `secrets` where both writers see it keeps them in agreement.** Manifest level, or a Site's `parameters`, puts the array in front of both templates, and both derive the object list from `templates/secretsync/spc-objects.bicep` so the documents are identical. An array attached to a single step reaches only that step.
- **The declaration is the source of truth.** Entries added out of band via `az iot ops secretsync secret set` are replaced the next time a template runs with declared secrets. Pick one source of truth per cluster.

## Writing your own sample

See `../README.md` for sample conventions and how to add a new sample to this workspace.

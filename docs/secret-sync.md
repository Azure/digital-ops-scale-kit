# Secret Sync

Enable [secret synchronization](https://learn.microsoft.com/azure/iot-operations/secure-iot-ops/howto-manage-secrets) for Azure IoT Operations instances, fully declarative with no CLI commands required.

Secret sync bridges Azure Key Vault and your Arc-enabled Kubernetes cluster. Once enabled, you can synchronize Key Vault secrets to Kubernetes secrets that AIO workloads consume directly.

## What gets deployed

The enablement template (`enable-secretsync.bicep`) creates:

| Resource | Purpose |
|----------|---------|
| User-Assigned Managed Identity | Authenticates the cluster to Key Vault |
| Key Vault (optional) | Stores secrets, skipped if you bring your own |
| Key Vault role assignments | Grants the MI `Key Vault Secrets User` + `Key Vault Reader` |
| Federated Identity Credential | Binds the MI to the cluster's secret sync service account via OIDC |
| SecretProviderClass (SPC) | Cluster-side resource linking the MI, Key Vault, and tenant |
| Instance update | Sets the SPC as the instance's default secret provider |

## Prerequisites

- Azure IoT Operations instance deployed and running
- Connected cluster with **OIDC issuer** and **workload identity** enabled
- Contributor + Key Vault Administrator (or equivalent) permissions on the target resource group

## How it works

Secret sync enablement uses a two-step pipeline:

```
resolve-aio                          enable-secretsync
┌──────────────────────────┐         ┌──────────────────────────────────┐
│ Read-only instance lookup │────────▶│ Create MI, KV, FIC, SPC,        │
│                           │ output  │ role assignments, instance update│
│ Outputs:                  │ chain   │                                  │
│  • CL name, namespace    │         │ Receives all values as params;   │
│  • Cluster name, OIDC    │         │ no cross-directory dependencies  │
│  • Instance properties   │         │                                  │
└──────────────────────────┘         └──────────────────────────────────┘
```

`resolve-aio.bicep` is read-only and outputs everything downstream needs. `enable-secretsync.bicep` receives those values via [output chaining](parameter-resolution.md#output-chaining) and provisions the secret sync resources. The split keeps `enable-secretsync.bicep` portable across naming conventions.

### Output chaining

The parameter file `parameters/inputs/secretsync.yaml` maps outputs from the resolve step to the enablement step's inputs:

```yaml
# Resolved infrastructure names
customLocationId: "{{ steps.resolve-aio.outputs.customLocationId }}"
customLocationName: "{{ steps.resolve-aio.outputs.customLocationName }}"
customLocationNamespace: "{{ steps.resolve-aio.outputs.customLocationNamespace }}"
connectedClusterName: "{{ steps.resolve-aio.outputs.connectedClusterName }}"
oidcIssuerUrl: "{{ steps.resolve-aio.outputs.oidcIssuerUrl }}"

# Instance properties for safe PUT forwarding
instanceLocation: "{{ steps.resolve-aio.outputs.instanceLocation }}"
schemaRegistryResourceId: "{{ steps.resolve-aio.outputs.schemaRegistryResourceId }}"
# ... additional properties forwarded for safe instance update
```

## Enabling secret sync

### Option 1: Integrated deployment (new instances)

Set `enableSecretSync: true` in your site configuration:

```yaml
# sites/my-site.yaml (or base-site.yaml for all sites)
properties:
  deployOptions:
    enableSecretSync: true
```

Then deploy with `aio-install.yaml` as usual. The resolve-aio and secretsync steps run automatically after the AIO instance is configured:

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l "name=my-site"
```

Both steps are gated by a `when` condition and only run for sites that have `enableSecretSync: true`.

### Option 2: Standalone day-2 enablement (existing instances)

Use the standalone manifest to enable secret sync on instances that are already deployed:

```bash
siteops -w workspaces/iot-operations deploy manifests/secretsync.yaml -l "name=my-site"
```

The standalone `secretsync.yaml` manifest runs the same two steps (resolve-aio → enable-secretsync) without the full AIO installation pipeline.

### CI/CD

In CI, enable secret sync per-site via the `SITE_OVERRIDES` secret:

```json
{
  "munich-dev": {
    "subscription": "...",
    "resourceGroup": "...",
    "properties.deployOptions.enableSecretSync": true
  }
}
```

## Bringing your own Key Vault

By default, the enablement template creates a new Key Vault in the deployment resource group. To use an existing Key Vault, including one in a different resource group, pass its resource ID:

```yaml
# parameters/secretsync-overrides.yaml (or in sites.local/)
existingKeyVaultResourceId: "/subscriptions/.../resourceGroups/shared-rg/providers/Microsoft.KeyVault/vaults/my-keyvault"
```

When an existing Key Vault is provided:
- No new Key Vault is created
- Role assignments are scoped to the Key Vault's resource group (cross-RG supported)
- The Key Vault must have RBAC authorization enabled (`enableRbacAuthorization: true`)

## Syncing secrets to the cluster

After enablement, use `sync-secrets.bicep` to synchronize one or more Key Vault secrets to Kubernetes Secrets in a single deploy:

```
az deployment group create -g <rg> \
  -f templates/secretsync/sync-secrets.bicep \
  -p keyVaultName=<kv> customLocationName=<cl> spcName=<spc> \
     managedIdentityClientId=<clientId> instanceLocation=<region> \
     secrets='[{"secretName":"my-secret"},{"secretName":"existing","createInKv":false}]' \
     secretValues='{"my-secret":"<value>"}'
```

The template treats the `secrets` array as the desired state. Each deploy PUTs the SPC with the union of all entries' object names and creates one SecretSync per entry.

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `keyVaultName` | Yes | Key Vault name (from enablement outputs) |
| `customLocationName` | Yes | Custom location name (from `resolve-aio` outputs) |
| `spcName` | Yes | Default SPC name (from enablement outputs) |
| `managedIdentityClientId` | Yes | Secretsync MI client ID (from enablement outputs) |
| `instanceLocation` | Yes | AIO instance location (from `resolve-aio` outputs) |
| `secrets` | Yes | Array of per-secret metadata, see below |
| `secretValues` | No | **`@secure()`** object keyed by `secretName`, required for entries with `createInKv` true |
| `tags` | No | Tags applied to the SPC, KV secrets, and SecretSync resources |

Per-entry fields in `secrets`:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `secretName` | Yes | | Key Vault secret name (must be unique within the array) |
| `kubernetesSecretName` | No | `secretName` | Kubernetes Secret name when the consuming workload expects a different name |
| `kubernetesSecretKey` | No | `secretName` | Key inside the Kubernetes Secret |
| `createInKv` | No | `true` | Set `false` to sync a secret already present in the Key Vault |

### Security model

The `secretValues` parameter is decorated with `@secure()` so ARM never logs values in deployment history or outputs. Provide values via:

- **`sites.local/`** parameter overrides (gitignored), the standard siteops pattern for local development
- **CI/CD secrets** such as GitHub Actions secrets or Azure DevOps variable groups
- **CLI `--parameters`** at deployment time

### Adding as a manifest step

To sync secrets as part of a manifest, add a step after enablement:

```yaml
- name: sync-secrets
  template: templates/secretsync/sync-secrets.bicep
  scope: resourceGroup
  parameters:
    - parameters/inputs/sync-secrets.yaml
    # secretValues come from sites.local/ or CI secrets
  when: "{{ site.properties.deployOptions.enableSecretSync }}"
```

### Removing a secret

Remove its entry from `secrets` and re-deploy. The SPC will be PUT without that entry, so the cluster-side controller stops syncing it. Bicep Incremental mode does NOT delete the corresponding `Microsoft.SecretSyncController/secretSyncs` ARM resource. To fully clean up, run `az resource delete --ids <secretSyncResourceId>` after the redeploy.

### Authoritative writes to the SPC

`sync-secrets.bicep` is authoritative for the default SPC's `properties.objects` field: each deploy PUTs the SPC with the union of every entry in the `secrets` array, replacing whatever was there before.

Two implications worth knowing:

- **Re-running enablement clears the SPC objects.** `enable-secretsync.bicep` (composed by `manifests/secretsync.yaml` and by `aio-install.yaml` when `enableSecretSync` is true) PUTs the same SPC without an `objects` field. If you redeploy enablement after `sync-secrets`, the cluster controller stops materializing every Kubernetes Secret with the controller error `the secretproviderclass parameters does not have a valid objects field`. Re-run `sync-secrets` after any enablement redeploy to restore the field.
- **CLI-managed entries are dropped on Bicep redeploy.** Entries added out of band via `az iot ops secretsync secret set` are removed from the SPC the next time `sync-secrets` deploys. Prefer one source of truth per cluster.

## Template reference

```
templates/
├── aio/
│   ├── resolve-aio.bicep                    # Read-only instance → CL → cluster resolution (dispatcher)
│   └── modules/
│       ├── resolve-instance-2025-10-01.bicep  # Per-API-version instance read
│       ├── resolve-instance-2026-03-01.bicep  # Per-API-version instance read
│       └── update-instance.bicep            # Shared safe instance PUT (dispatcher) used by the secretsync flow
├── common/
│   └── modules/
│       ├── resolve-custom-location.bicep    # CL resource ID → name, namespace, hostResourceId
│       └── resolve-cluster.bicep            # Cluster resource ID → name, OIDC issuer URLs
└── secretsync/
    ├── enable-secretsync.bicep              # Creates MI, KV, roles, FIC, SPC, instance update
    ├── sync-secrets.bicep                   # Syncs N KV secrets to K8s secrets in one deploy
    └── modules/
        └── keyvault-roles.bicep             # KV role assignments (cross-RG capable)
```

### Resolve modules

`resolve-aio.bicep` is the entry point. It is a dispatcher on `aioApiVersion` (sourced from `parameters/aio-releases/<release>.yaml`) that dispatches the instance read to a per-API-version inner module, then chains the (version-stable) custom-location and connected-cluster lookups:

| Module | Input | Outputs |
|--------|-------|---------|
| `aio/resolve-aio.bicep` | `aioInstanceName`, `aioApiVersion` | All infrastructure names + instance properties |
| `aio/modules/resolve-instance-<v>.bicep` | `aioInstanceName` | Instance fields read at API version `<v>` |
| `common/modules/resolve-custom-location.bicep` | CL resource ID | `name`, `namespace`, `hostResourceId` |
| `common/modules/resolve-cluster.bicep` | Cluster resource ID | `name`, `oidcIssuerUrl`, `selfHostedIssuerUrl` |

These modules use Bicep's **module boundary** pattern: runtime resource IDs passed as module parameters become compile-time values inside the module, enabling chained `existing` resource lookups.

### Enablement modules

| Module | Purpose |
|--------|---------|
| `aio/modules/update-instance.bicep` | Safe instance PUT that forwards all writable properties for the pinned API version, with conditional identity handling |
| `secretsync/modules/keyvault-roles.bicep` | Key Vault role assignments via module scope, supporting cross-resource-group Key Vaults |

## Troubleshooting

### "condition not met" (steps skipped)

The resolve-aio and secretsync steps have `when: "{{ site.properties.deployOptions.enableSecretSync }}"`. Ensure your site (or its base template) sets this to `true`:

```yaml
properties:
  deployOptions:
    enableSecretSync: true
```

For CI, set it in `SITE_OVERRIDES`:

```json
{ "my-site": { "properties.deployOptions.enableSecretSync": true } }
```

### DeploymentOutputEvaluationFailed

If `resolve-aio` fails with an error about a property not existing on the instance resource, this is an ARM limitation with `existing` resource references. Properties accessed via safe navigation (`instance.?tags ?? {}`) handle this correctly. If you see this error on a new API version, check that the resolve template uses `?.` for optional properties.

### Role assignment conflicts

Role assignments use deterministic names via `guid(keyVault.id, principalId, roleId)`. Re-running the deployment is idempotent. Existing assignments are confirmed in place, not duplicated.

### Key Vault RBAC not enabled

The enablement template creates Key Vaults with `enableRbacAuthorization: true`. If you bring your own Key Vault, role assignments will still be created successfully regardless of the Key Vault's authorization mode, but they will not take effect until RBAC authorization is enabled. Ensure `enableRbacAuthorization: true` is set on the Key Vault for the managed identity to authenticate.

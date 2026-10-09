# Secret Sync

Enable [secret synchronization](https://learn.microsoft.com/azure/iot-operations/secure-iot-ops/howto-manage-secrets) for Azure IoT Operations instances through declarative Site Ops manifests. No imperative `az iot ops secretsync` commands are required.

Secret Sync bridges Azure Key Vault and your Kubernetes cluster connected to Azure Arc. Once enabled, you can synchronize Key Vault secrets to Kubernetes secrets that AIO workloads consume directly.

## What gets deployed

The enablement template (`enable-secretsync.bicep`) creates:

| Resource | Purpose |
|----------|---------|
| User-Assigned Managed Identity | Authenticates the cluster to Key Vault |
| Key Vault (optional) | Stores secrets, skipped if you bring your own |
| Key Vault role assignments | Grants the MI `Key Vault Secrets User` + `Key Vault Reader` |
| Federated Identity Credential | Binds the MI to the cluster's Secret Sync service account via OIDC |
| SecretProviderClass (SPC) | Resource on the cluster linking the MI, Key Vault, and tenant, carrying the secrets the Site declares |
| Instance update | Sets the SPC as the instance's default secret provider |

## Prerequisites

- Existing Azure IoT Operations instance
- Connected cluster with **OIDC issuer** and **workload identity** enabled
- Contributor on the deployment resource group, plus permission to create role assignments at the Key Vault scope (for example, Owner, User Access Administrator, or Role Based Access Control Administrator at an applicable scope). Set `skipRoleAssignments: true` only when the Secret Sync managed identity already has the required Key Vault roles.

## How it works

Secret Sync enablement uses a pipeline of two steps, named `resolve-aio` and `secretsync`:

```
resolve-aio                          secretsync
┌──────────────────────────┐         ┌──────────────────────────────────┐
│ Read-only instance lookup │────────▶│ Create MI, KV, FIC, SPC,        │
│                           │ output  │ role assignments, instance update│
│ Outputs:                  │ chain   │                                  │
│  • CL name, namespace    │         │ Receives all values as params;   │
│  • Cluster name, OIDC    │         │ shared workspace modules         │
│  • Instance properties   │         │                                  │
└──────────────────────────┘         └──────────────────────────────────┘
```

`resolve-aio.bicep` only reads resources and outputs everything downstream needs. The `secretsync` step deploys `enable-secretsync.bicep`, which receives those values via [output chaining](parameter-resolution.md#output-chaining) and provisions the Secret Sync resources. Later steps read its outputs as `{{ steps.secretsync.outputs.<name> }}`. The split keeps `enable-secretsync.bicep` portable across naming conventions.

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

### Declaring the secrets to sync

The set of Key Vault secrets a Site synchronizes is declared once, as a `secrets` array, and read by both templates that write the SPC.

`enable-secretsync.bicep` and `sync-secrets.bicep` both PUT the default Secret Provider Class, and a full PUT replaces `properties.objects`. Both therefore derive that field from the same declaration through the shared `templates/secretsync/spc-objects.bicep` library, so the two writers always agree on what the controller on the cluster materializes.

Declare the array at **manifest level**, or in a Site's `parameters` section:

```yaml
# sites/my-site.yaml, or a sites.local/ overlay
parameters:
  secrets:
    - secretName: db-password
    - secretName: api-key
      kubernetesSecretName: my-app-credentials
      kubernetesSecretKey: key
```

Attachment at manifest level sits below Site parameters in the [merge order](parameter-resolution.md#merge-order), so a Site overrides the declared default. It also applies to every step in the pipeline, and each step receives only the parameters its own template declares. `secretValues` is `@secure()` and declared only by `sync-secrets.bicep`, so values reach the template that writes them to Key Vault and no other deployment.

A Site that declares no secrets keeps whatever object list the cluster already carries. Enablement reads the current value from the class the instance is bound to and writes it back, so running the platform install on a cluster whose secrets came from elsewhere leaves them in place. On a first install there is nothing to read, and the class is written with `objects` set to an empty string.

The read requires the bound class to exist. When an instance points at a class that was deleted out of band, the read fails and the deployment stops rather than writing over the reference. Set `preserveExistingSpcObjects: false` in the Site's `parameters` to skip the read and let enablement create the class fresh. It belongs on the Site rather than in a parameter file, because the chaining file that supplies the class reference attaches at step level and outranks a Site value.

## Enabling Secret Sync

### Option 1: Integrated deployment (new instances)

Set `enableSecretSync: true` in your Site configuration:

```yaml
# sites/my-site.yaml (or base-site.yaml for all sites)
properties:
  deployOptions:
    enableSecretSync: true
```

Then deploy with `aio-install` as usual. The resolve-aio and secretsync steps run automatically after the AIO instance is configured:

```bash
siteops -w workspaces/iot-operations deploy aio-install -l "name=my-site"
```

Both steps are gated by a `when` condition on their includes in `aio-install` and only run for Sites that have `enableSecretSync: true`.

### Option 2: Standalone enablement on existing instances

For AIO 2607 or 2608, the [guided route with an instance ID](guided-inputs.md#enable-secret-sync-on-an-existing-instance)
needs only the existing instance resource ID and explicit read permission.
It resolves the related cluster and checks identity prerequisites before
deployment. Optional answers select an existing vault or override Site labels.

Use the standalone manifest to enable Secret Sync on instances that are already deployed:

```bash
siteops -w workspaces/iot-operations deploy manifests/secretsync/manifest.yaml -l "name=my-site"
```

The standalone `secretsync` manifest runs the same `resolve-aio` and `secretsync` steps without the full AIO installation pipeline. It has no `enableSecretSync` gate.

### CI/CD

In CI, enable Secret Sync for each Site via the `SITE_OVERRIDES` secret:

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
# workspaces/iot-operations/sites.local/my-site.yaml
parameters:
  existingKeyVaultResourceId: "/subscriptions/.../resourceGroups/shared-rg/providers/Microsoft.KeyVault/vaults/my-keyvault"
```

When an existing Key Vault is provided:
- No new Key Vault is created
- Role assignments are deployed from the Key Vault's resource group and scoped to the Key Vault itself (another resource group is supported)
- The Key Vault must have RBAC authorization enabled (`enableRbacAuthorization: true`)

## Syncing secrets to the cluster

After enablement, use the `sync-secrets.bicep` step to configure one or more Key Vault secrets for synchronization to Kubernetes Secrets. The workspace sample composes that step and accepts secret values from a local overlay with the same name:

```yaml
# workspaces/iot-operations/sites.local/my-site.yaml
parameters:
  secrets:
    - secretName: my-secret
    - secretName: existing
      createInKv: false
  secretValues:
    my-secret: "<secret-value>"
```

```bash
siteops -w workspaces/iot-operations deploy samples/secretsync-sample/manifest.yaml -l "name=my-site"
```

The template treats the `secrets` array as the desired state. Each deploy PUTs the SPC with the union of all entries' object names and creates one SecretSync per distinct `kubernetesSecretName` (defaulting to `secretName`). Entries that share a `kubernetesSecretName` are grouped into one Kubernetes Secret with multiple keys. See [Secrets with multiple keys](#secrets-with-multiple-keys) below.

### Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `keyVaultName` | Yes | Key Vault name (from enablement outputs) |
| `customLocationName` | Yes | Custom location name (from `resolve-aio` outputs) |
| `spcName` | Yes | Default SPC name (from enablement outputs) |
| `managedIdentityClientId` | Yes | Secret Sync MI client ID (from enablement outputs) |
| `instanceLocation` | Yes | AIO instance location (from `resolve-aio` outputs) |
| `secrets` | Yes | Array of metadata for each secret, see below |
| `secretValues` | No | **`@secure()`** object keyed by `secretName`, required for entries with `createInKv` true |
| `tags` | No | Tags applied to the SPC, KV secrets, and SecretSync resources |

Fields of each item in `secrets`:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `secretName` | Yes | | Key Vault secret name. Must be unique within the array. |
| `kubernetesSecretName` | No | `secretName` | Kubernetes Secret name. Multiple entries that set the same value are grouped into one Secret with multiple keys. |
| `kubernetesSecretKey` | No | `secretName` | Key inside the Kubernetes Secret. Must be unique within a group of entries that share a `kubernetesSecretName`. |
| `createInKv` | No | `true` | Set `false` to sync a secret already present in the Key Vault |

### Secrets with multiple keys

Workloads often consume related credentials as a single Kubernetes Secret with multiple keys (for example, a `database-credentials` Secret with `host`, `username`, and `password` keys). Express this by setting the same `kubernetesSecretName` on each entry and a distinct `kubernetesSecretKey`:

```yaml
secrets:
  - secretName: my-db-host-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: host
  - secretName: my-db-username-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: username
  - secretName: my-db-password-kv
    kubernetesSecretName: database-credentials
    kubernetesSecretKey: password
```

This produces:

- Three Key Vault secrets (`my-db-host-kv`, `my-db-username-kv`, `my-db-password-kv`)
- One SecretSync ARM resource named `database-credentials` with three `objectSecretMapping` entries
- One Kubernetes Secret `database-credentials` on the cluster with three keys (`host`, `username`, `password`)

Constraints:

- Each `secretName` must be unique across the array. Each entry corresponds to one Key Vault secret.
- Within a group of entries sharing a `kubernetesSecretName`, each `kubernetesSecretKey` must also be unique. Like any duplicate key in YAML, two entries claiming the same `(kubernetesSecretName, kubernetesSecretKey)` pair both write to the same Kubernetes Secret slot, and the reconcile order on the cluster decides which value wins.

Workspace tests enforce these constraints for committed declarations. A
`sites.local/` overlay or another array that a caller provides must preserve the same
uniqueness because the template does not reject duplicates at deployment time.

### Security model

The `secretValues` parameter is decorated with `@secure()` so ARM does not record values in deployment history or outputs. This protection does not make shell arguments safe. Provide values via:

- **`sites.local/`** parameter overrides (gitignored), the standard Site Ops pattern for local development
- **The `SITE_OVERRIDES` secret** populated from GitHub Actions secrets or Azure DevOps variable groups

### Adding as a manifest step

Syncing secrets from a manifest takes two parameter files, split by what each one holds.

The chaining file wires upstream step outputs into the sync step, so it attaches at step level:

```yaml
# samples/secretsync-sample/inputs.yaml
keyVaultName: "{{ steps.secretsync.outputs.keyVaultName }}"
spcName: "{{ steps.secretsync.outputs.spcResourceName }}"
managedIdentityClientId: "{{ steps.secretsync.outputs.managedIdentityClientId }}"
customLocationName: "{{ steps.resolve-aio.outputs.customLocationName }}"
instanceLocation: "{{ steps.resolve-aio.outputs.instanceLocation }}"
```

```yaml
- name: sync-secrets
  template: templates/secretsync/sync-secrets.bicep
  scope: resourceGroup
  parameters:
    - samples/secretsync-sample/inputs.yaml
```

Gate the step when the composition makes Secret Sync optional. `aio-install` puts the `when:` on the `_secretsync.yaml` include rather than on individual steps, so every spliced step inherits one condition.

The declaration file holds `secrets` and `secretValues` and attaches at manifest level, which puts it below Site parameters in the [merge order](parameter-resolution.md#merge-order) so a Site or a `sites.local/` overlay overrides it:

```yaml
parameters:
  - samples/secretsync-sample/secrets.yaml
```

Attachment at manifest level also reaches every step, so the enablement step and the sync step PUT the SPC from the same array. See [Declaring the secrets to sync](#declaring-the-secrets-to-sync).

### Removing a secret

See [secretsync-sample/README.md](../workspaces/iot-operations/samples/secretsync-sample/README.md#removing-a-secret) for the operational steps. The SPC PUT semantics and SecretSync ARM resource cleanup are documented there alongside the sample they apply to.

## Template reference

```
templates/
├── aio/
│   ├── resolve-aio.bicep                    # Read-only instance → CL → cluster resolution (dispatcher)
│   └── modules/
│       ├── resolve-instance-<api-version>.bicep  # Per-API-version instance read, one per supported version
│       └── update-instance.bicep            # Shared safe instance PUT (dispatcher) used by the secretsync flow
├── common/
│   └── modules/
│       ├── resolve-custom-location.bicep    # CL resource ID → name, namespace, hostResourceId
│       └── resolve-cluster.bicep            # Cluster resource ID → name, OIDC issuer URLs
└── secretsync/
    ├── enable-secretsync.bicep              # Creates MI, KV, roles, FIC, SPC, instance update
    ├── sync-secrets.bicep                   # Syncs N KV secrets to K8s secrets in one deploy
    ├── spc-objects.bicep                    # Shared SPC objects derivation, imported by both writers
    └── modules/
        ├── keyvault-roles.bicep             # KV role assignments (cross-RG capable)
        └── read-spc-objects.bicep           # Reads `objects` off an existing SPC so enablement preserves it
```

The modules for each API version track the supported AIO releases, so read the directory rather than this tree for the current set.

### Resolve modules

Callers deploy `resolve-aio.bicep`. It is a dispatcher on `aioApiVersion` (sourced from `parameters/aio-releases/<release>.yaml`) that sends the instance read to an inner module for that API version, then chains the custom location and connected cluster lookups, which do not vary by version:

| Module | Input | Outputs |
|--------|-------|---------|
| `aio/resolve-aio.bicep` | `aioInstanceName`, `aioApiVersion` | All infrastructure names + instance properties |
| `aio/modules/resolve-instance-<v>.bicep` | `aioInstanceName` | Instance fields read at API version `<v>` |
| `common/modules/resolve-custom-location.bicep` | CL resource ID | `name`, `namespace`, `hostResourceId` |
| `common/modules/resolve-cluster.bicep` | Cluster resource ID | `name`, `oidcIssuerUrl`, `selfHostedIssuerUrl` |

These modules use Bicep's **module boundary** pattern: runtime resource IDs passed as module parameters become values known at compile time inside the module, enabling chained `existing` resource lookups.

### Enablement modules

| Module | Purpose |
|--------|---------|
| `aio/modules/update-instance.bicep` | Safe instance PUT that forwards all writable properties for the pinned API version, with conditional identity handling |
| `secretsync/modules/keyvault-roles.bicep` | Key Vault role assignments via module scope, supporting Key Vaults in other resource groups |

## Troubleshooting

### "Condition not met" (steps skipped)

In `aio-install`, the includes that add the resolve-aio and secretsync steps carry `when: "{{ site.properties.deployOptions.enableSecretSync }}"`. The standalone `secretsync` manifest has no such gate. Ensure your Site (or its base template) sets this to `true`:

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

Role assignments use deterministic names via `guid(keyVault.id, principalId, roleId)`, so rerunning assignments created by this template is idempotent. Assignments created elsewhere are not necessarily reused. Set `skipRoleAssignments: true` when the required grants are already configured.

### Key Vault RBAC not enabled

The enablement template creates Key Vaults with `enableRbacAuthorization: true`. If you bring your own Key Vault, role assignments will still be created successfully regardless of the Key Vault's authorization mode, but they will not take effect until RBAC authorization is enabled. Ensure `enableRbacAuthorization: true` is set on the Key Vault for the managed identity to authenticate.

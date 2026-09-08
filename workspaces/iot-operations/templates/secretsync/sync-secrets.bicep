// sync-secrets.bicep
// -------------------------------------------------------------------------------------
// Multi-secret synchronization template.
//
// Takes an array of secrets and:
//   1. Writes Key Vault secrets for entries with createInKv true (default true).
//   2. Updates the default Secret Provider Class (SPC) to include EVERY entry's
//      objectName in `properties.objects`. The SPC must list every secret name that
//      any SecretSync references, otherwise the SecretSync controller errors with
//      "the secretproviderclass parameters does not have a valid objects field".
//   3. Creates one Microsoft.SecretSyncController/secretSyncs ARM resource per
//      distinct kubernetesSecretName (defaulting to secretName), with one
//      objectSecretMapping entry per input entry that targets that name.
//      Multiple input entries sharing a kubernetesSecretName produce one
//      multi-key Kubernetes Secret (the common pattern for credential bundles
//      like `database-credentials` with host/username/password keys).
//
// Single source of truth pattern: the `secrets` array IS the desired state.
// Each deploy PUTs the SPC with the union of all entries. To stop syncing a
// secret, remove its entry from the array and re-deploy. Note that the
// corresponding `Microsoft.SecretSyncController/secretSyncs` resource is NOT
// auto-deleted by Bicep Incremental mode and must be removed separately
// (e.g., `az resource delete --ids <resourceId>`).
//
// Existing Key Vault secrets: set `createInKv: false` on an entry to skip the
// Key Vault write and just sync an already-present value. The entry still
// participates in the SPC objects list and gets a SecretSync mapping.
//
// Usage:
//   Compose this template as a Site Ops manifest step and supply secret values
//   through a same-name sites.local overlay or the CI SITE_OVERRIDES secret.
//   See docs/secret-sync.md.
// -------------------------------------------------------------------------------------

import { aioSecretSyncServiceAccountName } from '../common/extension-names.bicep'
import { spcObjectsYaml as renderSpcObjects } from './spc-objects.bicep'

// =====================================================================================
// Parameters chained from upstream steps
// =====================================================================================

@description('Name of the Key Vault (from secretsync.outputs.keyVaultName).')
param keyVaultName string

@description('Name of the custom location (from resolve-aio.outputs.customLocationName).')
param customLocationName string

@description('Name of the default Secret Provider Class (from secretsync.outputs.spcResourceName).')
param spcName string

@description('Client ID of the secretsync managed identity (from secretsync.outputs.managedIdentityClientId).')
param managedIdentityClientId string

@description('Location of the AIO instance (from resolve-aio.outputs.instanceLocation). The SPC and SecretSync resources must use the AIO instance location.')
param instanceLocation string

// =====================================================================================
// Per-deploy parameters
// =====================================================================================

@description('Per-secret metadata. Each entry: { secretName: string, kubernetesSecretName?: string (defaults to secretName), kubernetesSecretKey?: string (defaults to secretName), createInKv?: bool (default true) }. secretName values and resolved (kubernetesSecretName, kubernetesSecretKey) pairs must be unique. Workspace tests check committed declarations. Other callers must enforce the same contract. Entries sharing a kubernetesSecretName form one multi-key Kubernetes Secret. An empty array writes an empty SPC objects string and creates no Key Vault secret or SecretSync resources. This parameter is required.')
param secrets array

@secure()
@description('Secret values keyed by secretName. An entry must be present for every secret with createInKv true (or unset, since the default is true).')
param secretValues object = {}

@description('Resource tags applied to newly-created Key Vault secrets, the SPC, and the SecretSync resources.')
param tags object = {}

// =====================================================================================
// Existing Resources
// =====================================================================================

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

// =====================================================================================
// Variables
// =====================================================================================

// Synthesize the SPC.objects YAML string from all entries. The format matches what
// `az iot ops secretsync secret add` produces: the value is a literal YAML document
// with an `array:` of literal-block-scalar entries, each carrying objectName and
// objectType. The SecretSync controller parses this string to know which Key Vault
// objects to fetch. Workspace tests keep committed declarations duplicate-free.
// Other callers must supply unique secret names and resolved target pairs.
var spcObjectsYaml = renderSpcObjects(secrets)

// Distinct Kubernetes Secret names referenced by the array. `union(..., [])`
// is the Bicep idiom for deduplicating a list. One SecretSync ARM resource
// is emitted per name.
var k8sSecretNames = union(map(secrets, s => s.?kubernetesSecretName ?? s.secretName), [])

// =====================================================================================
// Key Vault Secrets (one per entry that asks for createInKv)
// =====================================================================================

resource kvSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in secrets: if (s.?createInKv ?? true) {
  parent: keyVault
  name: s.secretName
  tags: tags
  properties: {
    value: secretValues[s.secretName]
  }
}]

// =====================================================================================
// SPC update. PUT with the union of all secret object names
// =====================================================================================

resource spc 'Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses@2024-08-21-preview' = {
  name: spcName
  location: instanceLocation
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  tags: tags
  // Object literal rather than `union()`, matching enable-secretsync.bicep. A
  // whole-object expression is handed to the resource provider unevaluated when
  // any part of it is not known at preflight.
  properties: {
    clientId: managedIdentityClientId
    keyvaultName: keyVaultName
    tenantId: tenant().tenantId
    objects: spcObjectsYaml
  }
}

// =====================================================================================
// SecretSync resources (one per distinct kubernetesSecretName)
// =====================================================================================

resource secretSyncs 'Microsoft.SecretSyncController/secretSyncs@2024-08-21-preview' = [for k8sName in k8sSecretNames: {
  name: k8sName
  location: instanceLocation
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  tags: tags
  properties: {
    secretProviderClassName: spc.name
    serviceAccountName: aioSecretSyncServiceAccountName
    kubernetesSecretType: 'Opaque'
    objectSecretMapping: [for s in filter(secrets, s => (s.?kubernetesSecretName ?? s.secretName) == k8sName): {
      sourcePath: s.secretName
      targetKey: s.?kubernetesSecretKey ?? s.secretName
    }]
    forceSynchronization: 'no'
  }
  dependsOn: [
    kvSecrets
  ]
}]

// =====================================================================================
// Outputs
// =====================================================================================

@description('Configured mapping metadata. One entry per input secret, in the same order. Each carries the target Kubernetes Secret name, the key inside that Secret, and the SecretSync ARM resource name. Entries that share a kubernetesSecretName report the same secretSyncName. This output does not confirm cluster-side materialization.')
output materializedSecrets array = [for s in secrets: {
  secretName: s.secretName
  kubernetesSecretName: s.?kubernetesSecretName ?? s.secretName
  kubernetesSecretKey: s.?kubernetesSecretKey ?? s.secretName
  secretSyncName: s.?kubernetesSecretName ?? s.secretName
}]

@description('Number of secrets configured by this deploy.')
output secretCount int = length(secrets)

@description('Number of distinct Kubernetes Secret target names configured by this deploy. Equals secretCount unless entries are grouped by kubernetesSecretName. This output does not confirm cluster-side materialization.')
output kubernetesSecretCount int = length(k8sSecretNames)

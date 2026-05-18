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
//      entry, mapping the Key Vault secret to a Kubernetes Secret on the cluster.
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
// participates in the SPC objects list and gets a SecretSync resource.
//
// Usage:
//   az deployment group create -g <rg> -f sync-secrets.bicep \
//     -p keyVaultName=<kv> customLocationName=<cl> spcName=<spc> \
//        managedIdentityClientId=<clientId> instanceLocation=<region> \
//        secrets='[{"secretName":"foo"},{"secretName":"bar","createInKv":false}]' \
//        secretValues='{"foo":"foo-value"}'
// -------------------------------------------------------------------------------------

import { aioSecretSyncServiceAccountName } from '../common/extension-names.bicep'

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

@description('Per-secret metadata. Each entry: { secretName: string, kubernetesSecretName?: string (defaults to secretName), kubernetesSecretKey?: string (defaults to secretName), createInKv?: bool (default true) }. secretName values must be unique. The array must be non-empty.')
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
// objects to fetch.
var spcObjectsYaml = 'array:\n${join(map(secrets, s => '  - |\n    objectName: ${s.secretName}\n    objectType: secret'), '\n')}\n'

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
  properties: {
    clientId: managedIdentityClientId
    keyvaultName: keyVaultName
    tenantId: tenant().tenantId
    objects: spcObjectsYaml
  }
}

// =====================================================================================
// SecretSync resources (one per entry)
// =====================================================================================

resource secretSyncs 'Microsoft.SecretSyncController/secretSyncs@2024-08-21-preview' = [for (s, i) in secrets: {
  name: s.?kubernetesSecretName ?? s.secretName
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
    objectSecretMapping: [
      {
        sourcePath: s.secretName
        targetKey: s.?kubernetesSecretKey ?? s.secretName
      }
    ]
    forceSynchronization: 'no'
  }
  dependsOn: [
    kvSecrets
  ]
}]

// =====================================================================================
// Outputs
// =====================================================================================

@description('Per-secret materialization metadata. One entry per input secret, in the same order. Each carries the resolved Kubernetes Secret name, the key inside that Secret, and the SecretSync ARM resource name.')
output materializedSecrets array = [for (s, i) in secrets: {
  secretName: s.secretName
  kubernetesSecretName: s.?kubernetesSecretName ?? s.secretName
  kubernetesSecretKey: s.?kubernetesSecretKey ?? s.secretName
  secretSyncName: secretSyncs[i].name
}]

@description('Number of secrets configured by this deploy.')
output secretCount int = length(secrets)

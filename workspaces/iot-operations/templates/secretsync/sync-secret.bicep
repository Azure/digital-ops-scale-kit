// sync-secret.bicep
// -------------------------------------------------------------------------------------
// Generic template: syncs a Key Vault secret to a Kubernetes secret via SecretSync.
//
// Creates a secret in the Key Vault and a SecretSync resource that maps it to a
// Kubernetes secret on the cluster. The SecretSync references the default SPC
// created by enable-secretsync.
//
// Security: The secret value is a @secure() parameter and is never logged in ARM
// deployment history or Bicep outputs. Provide the value via:
//   - sites.local/ parameter overrides (gitignored)
//   - CI/CD pipeline secrets
//   - CLI --parameters at deployment time
//
// Usage:
//   az deployment group create -g <rg> -f sync-secret.bicep \
//     -p keyVaultName=<kv> customLocationName=<cl> spcName=<spc> \
//        secretName=<name> secretValue=<value>
// -------------------------------------------------------------------------------------

import { aioSecretSyncServiceAccountName } from '../common/extension-names.bicep'

// =====================================================================================
// Parameters
// =====================================================================================

@description('Name of the Key Vault (from enable-secretsync outputs).')
param keyVaultName string

@description('Name of the custom location.')
param customLocationName string

@description('Name of the default Secret Provider Class (from enable-secretsync outputs).')
param spcName string

@description('Name of the Key Vault secret to create.')
param secretName string

@secure()
@description('Secret value. Provide at deployment time. Never store in source control.')
param secretValue string

@description('Name for the Kubernetes secret. Defaults to the Key Vault secret name.')
param kubernetesSecretName string = ''

@description('Key within the Kubernetes secret. Defaults to the Key Vault secret name.')
#disable-next-line secure-secrets-in-params // This is a key name, not a secret value
param kubernetesSecretKey string = ''

@description('Location for the SecretSync resource. Defaults to the resource group location.')
param location string = resourceGroup().location

// =====================================================================================
// Variables
// =====================================================================================

var resolvedK8sSecretName = !empty(kubernetesSecretName) ? kubernetesSecretName : secretName
var resolvedK8sSecretKey = !empty(kubernetesSecretKey) ? kubernetesSecretKey : secretName

// =====================================================================================
// Existing Resources
// =====================================================================================

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

resource spc 'Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses@2024-08-21-preview' existing = {
  name: spcName
}

// =====================================================================================
// Key Vault Secret
// =====================================================================================

resource kvSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: secretName
  properties: {
    value: secretValue
  }
}

// =====================================================================================
// SecretSync Resource
//   Maps the Key Vault secret to a Kubernetes secret via the default SPC.
//   The resource name becomes the Kubernetes secret name on the cluster.
// =====================================================================================

resource secretSync 'Microsoft.SecretSyncController/secretSyncs@2024-08-21-preview' = {
  name: resolvedK8sSecretName
  location: location
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  properties: {
    secretProviderClassName: spc.name
    serviceAccountName: aioSecretSyncServiceAccountName
    kubernetesSecretType: 'Opaque'
    objectSecretMapping: [
      {
        sourcePath: secretName
        targetKey: resolvedK8sSecretKey
      }
    ]
    forceSynchronization: 'no'
  }
  dependsOn: [
    kvSecret
  ]
}

// =====================================================================================
// Outputs
// =====================================================================================

@description('Name of the Kubernetes secret that will be created on the cluster.')
output kubernetesSecretName string = resolvedK8sSecretName

@description('Key within the Kubernetes secret.')
output kubernetesSecretKey string = resolvedK8sSecretKey

@description('Name of the SecretSync resource.')
output secretSyncName string = secretSync.name

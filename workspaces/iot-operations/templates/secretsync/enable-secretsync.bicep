// enable-secretsync.bicep
// -------------------------------------------------------------------------------------
// Enables secret synchronization for an Azure IoT Operations instance.
// Mirrors the behavior of `az iot ops secretsync enable`.
//
// All resolved infrastructure values (CL name, cluster name, OIDC issuer, namespace,
// instance properties) are received as parameters, typically via output chaining from
// the resolve-aio step. This template has no cross-directory module dependencies.
//
// Resources provisioned/managed:
//   1. User-Assigned Managed Identity (idempotent PUT)
//   2. Key Vault with RBAC authorization (idempotent PUT)
//   3. Key Vault role assignments: Key Vault Secrets User + Key Vault Reader
//   4. Federated Identity Credential on the managed identity
//   5. AzureKeyVaultSecretProviderClass (SPC) on the custom location
//   6. IoT Operations instance update: sets defaultSecretProviderClassRef to the SPC
//
// Usage (with siteops output chaining from resolve-aio):
//   The resolve-aio step outputs all required values. The inputs/secretsync.yaml
//   parameter file maps those outputs to this template's parameters.
//
// Usage (standalone):
//   az deployment group create -g <rg> -f enable-secretsync.bicep \
//     -p aioInstanceName=<name> customLocationId=<clId> customLocationName=<cl> \
//        customLocationNamespace=<ns> connectedClusterName=<cluster> \
//        oidcIssuerUrl=<issuer> instanceLocation=<location> \
//        schemaRegistryResourceId=<srId>
// -------------------------------------------------------------------------------------

// =====================================================================================
// Parameters: resolved infrastructure (from resolve-aio output chaining)
// =====================================================================================

import { aioSecretSyncServiceAccountName } from '../common/extension-names.bicep'

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('Full ARM resource ID of the custom location.')
param customLocationId string

@description('Custom location name.')
param customLocationName string

@description('Kubernetes namespace associated with the custom location.')
param customLocationNamespace string

@description('Name of the Arc-connected cluster.')
param connectedClusterName string

@description('OIDC issuer URL for workload identity federation.')
param oidcIssuerUrl string

// =====================================================================================
// Parameters: instance properties (from resolve-aio, forwarded to instance update)
// =====================================================================================

@description('Instance location.')
param instanceLocation string

@description('Instance tags (forwarded to instance update).')
param instanceTags object = {}

@description('Instance identity type (forwarded to instance update).')
param identityType string = 'None'

@description('Instance user-assigned identities map (forwarded to instance update).')
param userAssignedIdentities object = {}

@description('Schema registry resource ID (forwarded to instance update).')
param schemaRegistryResourceId string

@description('ADR namespace resource ID (forwarded to instance update).')
param adrNamespaceResourceId string = ''

@description('Instance features map (forwarded to instance update).')
param features object = {}

@description('Instance description (forwarded to instance update).')
param instanceDescription string = ''

// =====================================================================================
// Parameters: AIO API version (drives update-instance dispatch)
// =====================================================================================

@description('IoT Operations API version for the instance PUT. Must match the version the instance was created with.')
@allowed([
  '2025-10-01'
  '2026-03-01'
])
param aioApiVersion string

// =====================================================================================
// Parameters: secret sync configuration
// =====================================================================================

@description('Name for the user-assigned managed identity. Auto-generated if empty.')
param managedIdentityName string = ''

@description('Resource ID of an existing Key Vault. If provided, no Key Vault is created and this one is used instead. Supports cross-resource-group references.')
param existingKeyVaultResourceId string = ''

@description('Name for a new Key Vault. Ignored if existingKeyVaultResourceId is provided. Auto-generated if empty.')
param keyVaultName string = ''

@description('Name override for the Secret Provider Class. Auto-generated if empty.')
param spcName string = ''

@description('Skip Key Vault role assignments (use when roles are already configured).')
param skipRoleAssignments bool = false

@description('Tags to apply to created resources.')
param tags object = {}

// =====================================================================================
// Variables
// =====================================================================================

var resolvedMiName = !empty(managedIdentityName)
  ? managedIdentityName
  : 'mi-aio-${uniqueString(resourceGroup().id, aioInstanceName)}'

// Key Vault: use existing or create new
var useExistingKv = !empty(existingKeyVaultResourceId)
var kvRgName = useExistingKv ? split(existingKeyVaultResourceId, '/')[4] : resourceGroup().name
var resolvedKvName = useExistingKv
  ? last(split(existingKeyVaultResourceId, '/'))
  : !empty(keyVaultName) ? keyVaultName : 'kvaio${uniqueString(resourceGroup().id, aioInstanceName)}'

var resolvedSpcName = !empty(spcName)
  ? spcName
  : 'spc-ops-${uniqueString(connectedClusterName, resourceGroup().name, aioInstanceName)}'

var fedCredName = 'fc-${uniqueString(connectedClusterName, customLocationName, aioInstanceName)}'

// Kubernetes service account subject for the secret sync controller
var credSubject = 'system:serviceaccount:${customLocationNamespace}:${aioSecretSyncServiceAccountName}'

// =====================================================================================
// Existing Resources
// =====================================================================================

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

// =====================================================================================
// User-Assigned Managed Identity
//   Idempotent PUT: if an MI with this name already exists, it is confirmed in place.
// =====================================================================================

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: resolvedMiName
  location: instanceLocation
  tags: tags
}

// =====================================================================================
// Key Vault
//   Conditional: only created when no existing Key Vault is provided.
//   Idempotent PUT: if a KV with this name already exists in the RG, it is confirmed
//   in place with RBAC authorization enabled.
// =====================================================================================

resource newKeyVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (!useExistingKv) {
  name: resolvedKvName
  location: instanceLocation
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenant().tenantId
    enableRbacAuthorization: true
  }
}

// =====================================================================================
// Key Vault Role Assignments
//   Deployed as a module to support cross-resource-group Key Vaults.
//   The module scope targets the Key Vault's resource group.
// =====================================================================================

module kvRoles './modules/keyvault-roles.bicep' = if (!skipRoleAssignments) {
  name: 'kv-roles-${uniqueString(resolvedKvName, aioInstanceName)}'
  scope: resourceGroup(kvRgName)
  params: {
    keyVaultName: resolvedKvName
    principalId: managedIdentity.properties.principalId
  }
  dependsOn: [
    newKeyVault
  ]
}

// =====================================================================================
// Federated Identity Credential
//   Links the MI to the connected cluster's OIDC issuer via the aio-ssc-sa
//   Kubernetes service account, enabling workload identity federation.
// =====================================================================================

resource federatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: managedIdentity
  name: fedCredName
  properties: {
    issuer: oidcIssuerUrl
    subject: credSubject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// =====================================================================================
// Secret Provider Class (SPC)
// =====================================================================================

resource spc 'Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses@2024-08-21-preview' = {
  name: resolvedSpcName
  location: instanceLocation
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  tags: tags
  properties: {
    clientId: managedIdentity.properties.clientId
    keyvaultName: resolvedKvName
    tenantId: tenant().tenantId
  }
  dependsOn: [
    federatedCredential
    newKeyVault
    kvRoles
  ]
}

// =====================================================================================
// Instance Update
//   Dispatched via ../aio/modules/update-instance.bicep (the shared UPDATE primitive
//   for Microsoft.IoTOperations/instances) to the correct API-versioned module based
//   on aioApiVersion. All known writable properties for the pinned API version are
//   forwarded to prevent data loss.
// =====================================================================================

module instanceUpdate '../aio/modules/update-instance.bicep' = {
  name: 'update-instance-spc-${uniqueString(aioInstanceName, spc.id)}'
  params: {
    aioApiVersion: aioApiVersion
    instanceName: aioInstanceName
    instanceLocation: instanceLocation
    extendedLocationName: customLocationId
    instanceTags: instanceTags
    identityType: identityType
    userAssignedIdentities: userAssignedIdentities
    schemaRegistryResourceId: schemaRegistryResourceId
    adrNamespaceResourceId: adrNamespaceResourceId
    features: features
    instanceDescription: instanceDescription
    spcResourceId: spc.id
  }
}

// =====================================================================================
// Outputs
// =====================================================================================

@description('Resource ID of the created Secret Provider Class.')
output spcResourceId string = spc.id

@description('Name of the created Secret Provider Class.')
output spcResourceName string = spc.name

@description('Principal ID of the managed identity.')
output managedIdentityPrincipalId string = managedIdentity.properties.principalId

@description('Client ID of the managed identity.')
output managedIdentityClientId string = managedIdentity.properties.clientId

@description('Resource ID of the managed identity.')
output managedIdentityResourceId string = managedIdentity.id

@description('Name of the Key Vault.')
output keyVaultName string = resolvedKvName

@description('Resource ID of the Key Vault.')
output keyVaultResourceId string = useExistingKv ? existingKeyVaultResourceId : newKeyVault!.id

@description('Name of the federated identity credential.')
output federatedCredentialName string = fedCredName

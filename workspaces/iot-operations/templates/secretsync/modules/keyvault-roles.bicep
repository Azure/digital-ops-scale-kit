// keyvault-roles.bicep
// -------------------------------------------------------------------------------------
// Module: Key Vault role assignments for secret sync.
//
// Declares the Key Vault as an existing resource and assigns the required roles
// to a managed identity principal. Deployed as a module so that cross-resource-group
// Key Vaults are supported. The parent template sets the module scope to the
// Key Vault's resource group.
// -------------------------------------------------------------------------------------

@description('Name of the Key Vault.')
param keyVaultName string

@description('Principal ID of the managed identity to grant access.')
param principalId string

// Well-known role definition IDs
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var kvReaderRoleId = '21090545-7ca7-4776-b22c-e363652d74d2'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, principalId, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, principalId, kvReaderRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvReaderRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Key Vault name.')
output name string = keyVault.name

@description('Key Vault resource ID.')
output id string = keyVault.id

// schema-registry-role.bicep
// -------------------------------------------------------------------------------------
// Grants the AIO extension's system-assigned MI Contributor on an existing
// Schema Registry. Run after both schema-registry and the AIO instance exist.
//
// Inputs:  schemaRegistryName, aioExtensionPrincipalId.
// Outputs: none (role assignment is idempotent on the same triple of scope/principal/role).
// -------------------------------------------------------------------------------------

metadata description = 'Assigns Contributor role to AIO extension on Schema Registry.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the existing schema registry.')
param schemaRegistryName string

@description('Principal ID of the AIO extension system-assigned identity.')
param aioExtensionPrincipalId string

/*****************************************************************************/
/*                          Existing Resources                               */
/*****************************************************************************/

resource schemaRegistry 'Microsoft.DeviceRegistry/schemaRegistries@2025-10-01' existing = {
  name: schemaRegistryName
}

/*****************************************************************************/
/*                          Role Assignment                                  */
/*****************************************************************************/

// Contributor role for AIO Extension MI on Schema Registry
// Role ID: b24988ac-6180-42a0-ab88-20f7382dd24c
resource aioExtensionSchemaRegistryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(schemaRegistry.id, aioExtensionPrincipalId, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  scope: schemaRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
    principalId: aioExtensionPrincipalId
    principalType: 'ServicePrincipal'
  }
}

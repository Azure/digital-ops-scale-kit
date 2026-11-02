// schema-registry-role.bicep
// -------------------------------------------------------------------------------------
// Grants the AIO extension's system assigned MI Azure Device Registry Administrator
// on an existing Schema Registry. The role grants Microsoft.DeviceRegistry/* only.
// Run after both schema-registry and the AIO instance exist.
//
// Inputs:  schemaRegistryName, aioExtensionPrincipalId.
// Outputs: none (role assignment is idempotent on the same triple of scope/principal/role).
// -------------------------------------------------------------------------------------

metadata description = 'Assigns Azure Device Registry Administrator role to AIO extension on Schema Registry.'

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

// Azure Device Registry Administrator role for AIO Extension MI on Schema Registry
// Role ID: 12675fd7-7f59-493f-9201-f7944860a2f1
// The name includes the role ID, so changing the role creates a new assignment and
// leaves the earlier one in place.
resource aioExtensionSchemaRegistryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(schemaRegistry.id, aioExtensionPrincipalId, '12675fd7-7f59-493f-9201-f7944860a2f1')
  scope: schemaRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '12675fd7-7f59-493f-9201-f7944860a2f1')
    principalId: aioExtensionPrincipalId
    principalType: 'ServicePrincipal'
  }
}

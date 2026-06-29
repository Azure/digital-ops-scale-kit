// schema-registry.bicep
// -------------------------------------------------------------------------------------
// Creates an AIO Schema Registry with a backing storage account and blob container.
// Hardens the storage account (RBAC-only, deny by default, schema-registry MI granted
// Storage Blob Data Contributor on the container).
//
// Inputs:  schemaRegistryName, optional storageAccountName/containerName/location/tags.
// Outputs: schemaRegistry { id, name, principalId }, storageAccount { id, name, containerUrl }.
// -------------------------------------------------------------------------------------

metadata description = 'Creates a Schema Registry with supporting storage infrastructure for Azure IoT Operations.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the schema registry to create.')
param schemaRegistryName string

@description('Name of the storage account. If not provided, a unique name will be generated.')
param storageAccountName string = ''

@description('Name of the blob container for schema storage.')
param containerName string = 'schemas'

@description('Location for all resources. Defaults to resource group location.')
param location string = resourceGroup().location

@description('Tags to apply to resources')
param tags object = {}

/*****************************************************************************/
/*                          Storage Account                                  */
/*****************************************************************************/

var generatedStorageAccountName = !empty(storageAccountName)
  ? storageAccountName
  : take('sr${uniqueString(resourceGroup().id, schemaRegistryName)}', 24)

// Uses resourceId() instead of schemaRegistry.id to avoid a circular dependency
// (storage account → schema registry → storage account)
var schemaRegistryResourceId = resourceId('Microsoft.DeviceRegistry/schemaRegistries', schemaRegistryName)

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: generatedStorageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      resourceAccessRules: [
        {
          resourceId: schemaRegistryResourceId
          tenantId: tenant().tenantId
        }
      ]
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: containerName
}

/*****************************************************************************/
/*                          Schema Registry                                  */
/*****************************************************************************/

resource schemaRegistry 'Microsoft.DeviceRegistry/schemaRegistries@2025-10-01' = {
  name: schemaRegistryName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    namespace: schemaRegistryName
    // Explicitly construct URL to avoid any trailing slash issues
    storageAccountContainerUrl: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${containerName}'
  }
  dependsOn: [
    container
  ]
}

/*****************************************************************************/
/*                          Role Assignments                                 */
/*****************************************************************************/

// Storage Blob Data Contributor role for Schema Registry MI on the container
// Role ID: ba92f5b4-2d11-453d-a403-e96b0029c9fe
resource schemaRegistryStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, schemaRegistry.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: container
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: schemaRegistry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output schemaRegistry object = {
  id: schemaRegistry.id
  name: schemaRegistry.name
  principalId: schemaRegistry.identity.principalId
}

output storageAccount object = {
  id: storageAccount.id
  name: storageAccount.name
  containerUrl: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${containerName}'
}

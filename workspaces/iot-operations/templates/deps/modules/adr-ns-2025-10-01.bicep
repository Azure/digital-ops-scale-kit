// adr-ns-2025-10-01.bicep
// -------------------------------------------------------------------------------------
// Per-API-version implementation of the ADR namespace resource. Selected by
// templates/deps/adr-ns.bicep when the resolved aio-releases YAML declares
// adrApiVersion '2025-10-01'.
// -------------------------------------------------------------------------------------

metadata description = 'ADR namespace deployment using Microsoft.DeviceRegistry@2025-10-01.'

@description('Name of the ADR namespace to create.')
param adrNamespaceName string

@description('Location for the namespace.')
param location string

@description('Tags to apply to resources.')
param tags object = {}

resource adrNamespace 'Microsoft.DeviceRegistry/namespaces@2025-10-01' = {
  name: adrNamespaceName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
  tags: tags
}

output id string = adrNamespace.id
output name string = adrNamespace.name
output principalId string = adrNamespace.identity.principalId

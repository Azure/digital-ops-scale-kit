// assets-2026-04-01.bicep
// -------------------------------------------------------------------------------------
// Every asset catalog resource kind, written at the 2026-04-01 ADR API generation.
//
// Selected by `main.bicep` from the site's release. The two kinds share a module
// because they share an ordering: an asset names its device and inbound endpoint by
// string, and ARM does not model a reference by name, so `dependsOn` supplies it.
// -------------------------------------------------------------------------------------

@description('Name of the existing Azure Device Registry namespace the resources are created under.')
param adrNamespaceName string

@description('Name of the custom location bound to the existing AIO instance.')
param customLocationName string

@description('Location for the created devices and assets.')
param location string

@description('Tags to apply to the created devices and assets.')
param tags object = {}

@description('Devices to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param devices array = []

@description('Assets to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param assets array = []

resource adrNamespace 'Microsoft.DeviceRegistry/namespaces@2026-04-01' existing = {
  name: adrNamespaceName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

// `properties` reads an item from a parameter array, which ARM evaluates before
// preflight. A properties expression that reads a resource or a module output
// instead can reach the provider unevaluated.
resource deviceResources 'Microsoft.DeviceRegistry/namespaces/devices@2026-04-01' = [
  for device in devices: {
    parent: adrNamespace
    name: device.name
    location: location
    tags: tags
    extendedLocation: {
      name: customLocation.id
      type: 'CustomLocation'
    }
    properties: device.properties
  }
]

// An asset binds to one inbound endpoint on one device through
// `properties.deviceRef`, so every device in the effective composition is created first.
resource assetResources 'Microsoft.DeviceRegistry/namespaces/assets@2026-04-01' = [
  for asset in assets: {
    parent: adrNamespace
    name: asset.name
    location: location
    tags: tags
    extendedLocation: {
      name: customLocation.id
      type: 'CustomLocation'
    }
    properties: asset.properties
    dependsOn: [
      deviceResources
    ]
  }
]

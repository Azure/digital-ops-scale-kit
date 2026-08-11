// dataflows-2026-03-01.bicep
// -------------------------------------------------------------------------------------
// Every dataflow resource kind, written at the 2026-03-01 AIO API generation.
//
// Selected by `main.bicep` from the site's release. The three kinds share a module
// because they share an ordering: a dataflow names its endpoint and profile by string,
// and ARM does not model a reference by name, so `dependsOn` supplies it.
// -------------------------------------------------------------------------------------

@description('Name of the existing AIO instance the resources are created under.')
param aioInstanceName string

@description('Name of the custom location bound to the existing AIO instance.')
param customLocationName string

@description('Dataflow endpoints to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param dataflowEndpoints array = []

@description('Dataflow profiles to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param dataflowProfiles array = []

@description('Dataflows to create. Each entry is { name: string, profileRef?: string, properties: object }. An empty array creates nothing.')
param dataflows array = []

@description('Profile a dataflow runs in when its entry declares no profileRef. AIO creates this profile alongside the instance.')
param defaultProfileName string = 'default'

resource aioInstance 'Microsoft.IoTOperations/instances@2026-03-01' existing = {
  name: aioInstanceName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

// `properties` reads an item from a parameter array, which ARM evaluates before
// preflight. A properties expression that reads a resource or a module output
// instead can reach the provider unevaluated.
resource endpoints 'Microsoft.IoTOperations/instances/dataflowEndpoints@2026-03-01' = [
  for endpoint in dataflowEndpoints: {
    parent: aioInstance
    name: endpoint.name
    extendedLocation: {
      name: customLocation.id
      type: 'CustomLocation'
    }
    properties: endpoint.properties
  }
]

resource profiles 'Microsoft.IoTOperations/instances/dataflowProfiles@2026-03-01' = [
  for profile in dataflowProfiles: {
    parent: aioInstance
    name: profile.name
    extendedLocation: {
      name: customLocation.id
      type: 'CustomLocation'
    }
    properties: profile.properties
  }
]

// The profile a dataflow belongs to varies per entry, so each resource names its full
// instance, profile, and dataflow path rather than binding to one symbolic parent.
resource dataflowResources 'Microsoft.IoTOperations/instances/dataflowProfiles/dataflows@2026-03-01' = [
  for dataflow in dataflows: {
    name: '${aioInstanceName}/${dataflow.?profileRef ?? defaultProfileName}/${dataflow.name}'
    extendedLocation: {
      name: customLocation.id
      type: 'CustomLocation'
    }
    properties: dataflow.properties
    dependsOn: [
      endpoints
      profiles
    ]
  }
]

@description('The generation this module writes at. main.bicep selects it so a deploy reports the module that actually ran.')
output deployedApiVersion string = '2026-03-01'

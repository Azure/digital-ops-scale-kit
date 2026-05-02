// resolve-custom-location.bicep
// -------------------------------------------------------------------------------------
// Reusable module: resolves a custom location from its full ARM resource ID.
//
// Accepts the full resource ID (e.g., from instance.extendedLocation.name),
// parses the name, declares the custom location as an existing resource, and
// outputs its key properties.
//
// The module boundary converts the runtime resource ID into a compile-time
// parameter, allowing the existing resource lookup that Bicep otherwise
// prohibits on runtime values.
// -------------------------------------------------------------------------------------

@description('Full ARM resource ID of the custom location.')
param customLocationResourceId string

var customLocationName = last(split(customLocationResourceId, '/'))

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

@description('Custom location name (parsed from resource ID).')
output name string = customLocation.name

@description('Full resource ID of the custom location.')
output id string = customLocation.id

@description('Kubernetes namespace associated with the custom location.')
output namespace string = customLocation.properties.namespace

@description('Full ARM resource ID of the host connected cluster.')
output hostResourceId string = customLocation.properties.hostResourceId

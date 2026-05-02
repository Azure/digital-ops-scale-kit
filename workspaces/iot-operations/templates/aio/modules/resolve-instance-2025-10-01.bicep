// resolve-instance-2025-10-01.bicep
// -------------------------------------------------------------------------------------
// API-version-bound read of an existing AIO instance. Routed by
// templates/aio/resolve-aio.bicep.
//
// Why version-bound: Microsoft.IoTOperations is an Arc-mapped RP. The ARM
// API version on `existing` is not just a Bicep schema lens. It issues
// `GET ?api-version=<pinned>` and the RP/bridge projects through that
// version's CRD generation. Mismatching the cluster's CRD generation can
// drift or fail the read. The router selects this module when the site's
// version config declares aioApiVersion '2025-10-01'.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

resource instance 'Microsoft.IoTOperations/instances@2025-10-01' existing = {
  name: aioInstanceName
}

@description('Full ARM resource ID of the custom location bound to the instance.')
output customLocationResourceId string = instance.extendedLocation.name

@description('Instance location.')
output instanceLocation string = instance.location

@description('Instance tags. ARM does not expose tags on existing resource references in all cases; defaults to empty.')
output instanceTags object = instance.?tags ?? {}

@description('Instance identity type.')
output identityType string = instance.?identity.?type ?? 'None'

@description('Instance user-assigned identities map.')
output userAssignedIdentities object = instance.?identity.?userAssignedIdentities ?? {}

@description('Schema registry resource ID.')
output schemaRegistryResourceId string = instance.properties.schemaRegistryRef.resourceId

@description('ADR namespace resource ID.')
output adrNamespaceResourceId string = instance.properties.?adrNamespaceRef.?resourceId ?? ''

@description('Instance features map.')
output features object = instance.properties.?features ?? {}

@description('Instance description.')
output instanceDescription string = instance.properties.?description ?? ''

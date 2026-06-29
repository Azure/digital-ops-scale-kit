// resolve-instance-2026-03-01.bicep
// -------------------------------------------------------------------------------------
// API-version-bound read of an existing AIO instance. Routed by
// templates/aio/resolve-aio.bicep.
//
// See resolve-instance-2025-10-01.bicep header for the rationale on
// version-bound reads against Arc-mapped RPs.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

resource instance 'Microsoft.IoTOperations/instances@2026-03-01' existing = {
  name: aioInstanceName
}

@description('Full ARM resource ID of the custom location bound to the instance.')
output customLocationResourceId string = instance.extendedLocation.name

@description('Instance location.')
output instanceLocation string = instance.location

@description('Instance tags. ARM does not expose tags on existing resource references in all cases. Defaults to empty.')
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

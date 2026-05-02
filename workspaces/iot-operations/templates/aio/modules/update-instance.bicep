// update-instance.bicep
// -------------------------------------------------------------------------------------
// Router: dispatches IoT Operations instance PUT to the correct API-versioned module.
//
// Use this when a capability needs to update an existing Microsoft.IoTOperations/
// instances resource (e.g., setting a default secret provider class, updating
// features, changing the default dataflow endpoint). All writable properties for
// the target API version must be forwarded to prevent data loss, so callers are
// expected to chain from resolve-aio outputs.
//
// Mirrors the pattern in templates/aio/instance.bicep (CREATE). This module is
// the matching UPDATE primitive. Lives in templates/aio/modules/ so it is
// available to any capability, not only secretsync.
//
// --- Adding a new API version -------------------------------------------------------
// Convention: the newest API version is always the else-branch (falsy fallback);
// every older version is an explicit positive equality check. See
// templates/aio/instance.bicep for the detailed restructuring example.
//   1. Extend @allowed on aioApiVersion below.
//   2. Add a new `module update_<YYYY>` conditional block.
//   3. Push the previously-newest output into an explicit equality and make the
//      new version the else in the instanceResourceId ternary at the bottom.
// -------------------------------------------------------------------------------------

@description('IoT Operations API version selecting the update module to deploy.')
@allowed([
  '2025-10-01'
  '2026-03-01'
])
param aioApiVersion string

@description('IoT Operations instance name.')
param instanceName string

@description('Instance location (from existing instance).')
param instanceLocation string

@description('Extended location resource ID (the custom location ID).')
param extendedLocationName string

@description('Instance tags.')
param instanceTags object = {}

@description('Identity type (None, UserAssigned, SystemAssigned, SystemAssigned,UserAssigned).')
param identityType string = 'None'

@description('User-assigned managed identities map (resource ID to empty object).')
param userAssignedIdentities object = {}

@description('Schema registry resource ID (required by the IoT Operations instance).')
param schemaRegistryResourceId string

@description('ADR namespace resource ID.')
param adrNamespaceResourceId string = ''

@description('Instance features map (component mode/settings). Forwarded to prevent data loss.')
param features object = {}

@description('Instance description.')
param instanceDescription string = ''

@description('Secret Provider Class resource ID to set as the default. Empty clears the SPC reference on PUT.')
param spcResourceId string = ''

module update_2025 './update-instance-2025-10-01.bicep' = if (aioApiVersion == '2025-10-01') {
  name: 'update-instance-2025-${uniqueString(instanceName, spcResourceId)}'
  params: {
    instanceName: instanceName
    instanceLocation: instanceLocation
    extendedLocationName: extendedLocationName
    instanceTags: instanceTags
    identityType: identityType
    userAssignedIdentities: userAssignedIdentities
    schemaRegistryResourceId: schemaRegistryResourceId
    adrNamespaceResourceId: adrNamespaceResourceId
    features: features
    instanceDescription: instanceDescription
    spcResourceId: spcResourceId
  }
}

module update_2026 './update-instance-2026-03-01.bicep' = if (aioApiVersion == '2026-03-01') {
  name: 'update-instance-2026-${uniqueString(instanceName, spcResourceId)}'
  params: {
    instanceName: instanceName
    instanceLocation: instanceLocation
    extendedLocationName: extendedLocationName
    instanceTags: instanceTags
    identityType: identityType
    userAssignedIdentities: userAssignedIdentities
    schemaRegistryResourceId: schemaRegistryResourceId
    adrNamespaceResourceId: adrNamespaceResourceId
    features: features
    instanceDescription: instanceDescription
    spcResourceId: spcResourceId
  }
}

output instanceResourceId string = aioApiVersion == '2025-10-01'
  ? update_2025!.outputs.instanceResourceId
  : update_2026!.outputs.instanceResourceId

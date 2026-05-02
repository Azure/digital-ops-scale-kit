// resolve-aio.bicep
// -------------------------------------------------------------------------------------
// Read-only router: resolves an Azure IoT Operations instance and its
// associated infrastructure (custom location, connected cluster) into a
// complete set of outputs.
//
// This template performs no resource creation or modification. It dispatches
// the AIO instance read to an API-versioned inner module, then chains the
// custom-location and connected-cluster lookups (those API versions are not
// AIO-coupled and stay shared).
//
// Resolution chain:
//   1. Instance read via modules/resolve-instance-<aioApiVersion>.bicep
//      (compile-time dispatch on aioApiVersion).
//   2. Custom Location parsed from the instance's extendedLocation.name.
//   3. Connected Cluster parsed from the CL's hostResourceId.
//
// Why route on aioApiVersion: Microsoft.IoTOperations is an Arc-mapped RP;
// the ARM API version on `existing` issues a real GET against that version's
// CRD generation. The site's selected release config (aio-releases/<release>.yaml)
// is the source of truth for which API version to read with. During an
// upgrade, this is the *target* API version; the GET against a source-version
// instance relies on RP forward-compat for read shape.
//
// Usage (siteops manifest step):
//   parameters:
//     - "parameters/aio-releases/{{ site.properties.aioRelease }}.yaml"
// -------------------------------------------------------------------------------------

// =====================================================================================
// Parameters
// =====================================================================================

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('IoT Operations API version for the instance read. Sourced from parameters/aio-releases/<release>.yaml; pin must match the version the site is currently on.')
@allowed([
  '2025-10-01'
  '2026-03-01'
])
param aioApiVersion string

@description('Use the self-hosted OIDC issuer URL instead of the public one.')
param useSelfHostedIssuer bool = false

// =====================================================================================
// Instance read: dispatched per API version
//
// Adding a new API version:
//   1. Extend @allowed on aioApiVersion above.
//   2. Add a new `module resolve_<YYYY>` block mirroring the existing two.
//   3. Push the previously-newest branch into an explicit equality and make
//      the new version the else-branch in `activeInstance` below.
//   4. Mirror the same change in templates/aio/instance.bicep and
//      templates/aio/modules/update-instance.bicep.
// =====================================================================================

module resolve_2025 './modules/resolve-instance-2025-10-01.bicep' = if (aioApiVersion == '2025-10-01') {
  name: 'resolve-instance-2025-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
  }
}

module resolve_2026 './modules/resolve-instance-2026-03-01.bicep' = if (aioApiVersion == '2026-03-01') {
  name: 'resolve-instance-2026-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
  }
}

// Select outputs from the active module. The @allowed constraint guarantees
// exactly one module deploys. Convention: newest API version is the
// else-branch; every older version is an explicit positive equality check.
var activeInstance = aioApiVersion == '2025-10-01'
  ? {
      customLocationResourceId: resolve_2025!.outputs.customLocationResourceId
      instanceLocation: resolve_2025!.outputs.instanceLocation
      instanceTags: resolve_2025!.outputs.instanceTags
      identityType: resolve_2025!.outputs.identityType
      userAssignedIdentities: resolve_2025!.outputs.userAssignedIdentities
      schemaRegistryResourceId: resolve_2025!.outputs.schemaRegistryResourceId
      adrNamespaceResourceId: resolve_2025!.outputs.adrNamespaceResourceId
      features: resolve_2025!.outputs.features
      instanceDescription: resolve_2025!.outputs.instanceDescription
    }
  : {
      customLocationResourceId: resolve_2026!.outputs.customLocationResourceId
      instanceLocation: resolve_2026!.outputs.instanceLocation
      instanceTags: resolve_2026!.outputs.instanceTags
      identityType: resolve_2026!.outputs.identityType
      userAssignedIdentities: resolve_2026!.outputs.userAssignedIdentities
      schemaRegistryResourceId: resolve_2026!.outputs.schemaRegistryResourceId
      adrNamespaceResourceId: resolve_2026!.outputs.adrNamespaceResourceId
      features: resolve_2026!.outputs.features
      instanceDescription: resolve_2026!.outputs.instanceDescription
    }

// =====================================================================================
// Chained Resolution: version-stable
//   Custom location and connected cluster live under different RPs whose
//   ARM API versions are not coupled to AIO releases. They stay single-pinned
//   and shared.
// =====================================================================================

module resolvedCl '../common/modules/resolve-custom-location.bicep' = {
  name: 'resolve-cl-${uniqueString(aioInstanceName)}'
  params: {
    customLocationResourceId: activeInstance.customLocationResourceId
  }
}

module resolvedCluster '../common/modules/resolve-cluster.bicep' = {
  name: 'resolve-cluster-${uniqueString(aioInstanceName)}'
  params: {
    connectedClusterResourceId: resolvedCl.outputs.hostResourceId
  }
}

// =====================================================================================
// Outputs: resolved infrastructure
// =====================================================================================

@description('Full ARM resource ID of the custom location.')
output customLocationId string = activeInstance.customLocationResourceId

@description('Custom location name.')
output customLocationName string = resolvedCl.outputs.name

@description('Kubernetes namespace associated with the custom location.')
output customLocationNamespace string = resolvedCl.outputs.namespace

@description('Connected cluster name.')
output connectedClusterName string = resolvedCluster.outputs.name

@description('Full ARM resource ID of the connected cluster. Used by the upgrade flow to compute the AIO Arc extension name via aioExtensionName(clusterResourceId), mirroring install-time derivation.')
output connectedClusterResourceId string = resolvedCluster.outputs.id

@description('OIDC issuer URL for workload identity federation.')
output oidcIssuerUrl string = useSelfHostedIssuer
  ? resolvedCluster.outputs.selfHostedIssuerUrl
  : resolvedCluster.outputs.oidcIssuerUrl

// =====================================================================================
// Outputs: instance properties (forwarded for safe PUT by downstream templates)
// =====================================================================================

@description('Instance location.')
output instanceLocation string = activeInstance.instanceLocation

@description('Instance tags. Defaults to empty object if unavailable.')
output instanceTags object = activeInstance.instanceTags

@description('Instance identity type.')
output identityType string = activeInstance.identityType

@description('Instance user-assigned identities map.')
output userAssignedIdentities object = activeInstance.userAssignedIdentities

@description('Schema registry resource ID.')
output schemaRegistryResourceId string = activeInstance.schemaRegistryResourceId

@description('ADR namespace resource ID.')
output adrNamespaceResourceId string = activeInstance.adrNamespaceResourceId

@description('ADR namespace name (parsed from resource ID; empty if instance has no ADR namespace bound).')
output adrNamespaceName string = empty(activeInstance.adrNamespaceResourceId) ? '' : last(split(activeInstance.adrNamespaceResourceId, '/'))

@description('Instance features map.')
output features object = activeInstance.features

@description('Instance description.')
output instanceDescription string = activeInstance.instanceDescription

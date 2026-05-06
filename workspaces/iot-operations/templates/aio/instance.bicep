// instance.bicep
// -------------------------------------------------------------------------------------
// Deploys the core Azure IoT Operations instance, including the AIO extension,
// custom location, broker, dataflow profile, and default endpoints onto an
// Arc-enabled Kubernetes cluster.
//
// This template is version-agnostic: callers supply the AIO extension version
// and release train explicitly, so the same template works across AIO releases
// without embedding version constants.
// -------------------------------------------------------------------------------------

import * as types from './modules/types.bicep'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

/*                          Cluster Parameters                               */
///////////////////////////////////////////////////////////////////////////////

@description('Name of the existing arc-enabled cluster where AIO will be deployed.')
param clusterName string

@description('The namespace on the cluster to deploy to.')
param clusterNamespace string = 'azure-iot-operations'

@description('Location of the existing arc-enabled cluster where AIO will be deployed. AIO RP enforces region support on PUT.')
param clusterLocation string = any(resourceGroup().location)

/*                          Custom Location Parameters                       */
///////////////////////////////////////////////////////////////////////////////

@description('Name of the custom location where AIO will be deployed.')
param customLocationName string?

@description('List of cluster extension IDs for the custom location.')
param clExtensionIds string[]

/*                             Instance Parameters                           */
///////////////////////////////////////////////////////////////////////////////

@description('Name of the AIO instance to be created.')
param aioInstanceName string?

@description('User assigned identity resource id to assign to the AIO instance.')
param userAssignedIdentity string?

@description('Schema Registry resource ID assigned to the AIO instance.')
param schemaRegistryId string

@description('Existing Azure Device Registry namespace resource ID to be passed in to the AIO Instance.')
param adrNamespaceId string?

@description('AIO Instance features.')
param features types.Features?

/*                              Broker Parameters                            */
///////////////////////////////////////////////////////////////////////////////

@description('Configuration for the AIO Broker services deployed for AIO')
param brokerConfig types.BrokerConfig?

/*                                TLS Parameters                             */
///////////////////////////////////////////////////////////////////////////////

@description('Trust bundle config for AIO.')
param trustConfig types.TrustConfig = {
  source: 'SelfSigned'
}

/*                               Other Parameters                            */
///////////////////////////////////////////////////////////////////////////////

@description('Instance count for the default dataflow profile. The default is 1.')
param defaultDataflowInstanceCount int = 1

@description('Enable observability metrics collection.')
param observabilityEnabled bool = false

@description('OpenTelemetry collector address for metrics export.')
param otelCollectorAddress string = ''

/*                          Extension Version Parameters                     */
///////////////////////////////////////////////////////////////////////////////

@description('Version of the AIO extension to install.')
param aioVersion string

@description('Release train of the AIO extension.')
param aioTrain string = 'stable'

@description('IoT Operations API version for resource deployment.')
@allowed([
  '2025-10-01'
  '2026-03-01'
])
param aioApiVersion string

@description('Additional configuration settings for the AIO extension. Merged with defaults.')
param aioConfigurationOverrides object = {}

/*****************************************************************************/
/*                                Variables                                     */
/********************************************************************************/

var HASH = take(uniqueString(resourceGroup().id, clusterName, clusterNamespace), 5)

var hasIdentity = !empty(userAssignedIdentity) && userAssignedIdentity != ''
var instanceIdentity = !hasIdentity
  ? { type: 'None' }
  : { type: 'UserAssigned', userAssignedIdentities: { '${userAssignedIdentity!}': {} } }

/*****************************************************************************/
/*         Existing Arc-enabled cluster where AIO will be deployed.          */
/*****************************************************************************/

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: clusterName
}

/*****************************************************************************/
/*     IoT Operations Resources (API-versioned module deployment)            */
/*****************************************************************************/

module resources_2025 './modules/instance-2025-10-01.bicep' = if (aioApiVersion == '2025-10-01') {
  name: 'aio-resources-2025-10-01'
  params: {
    clusterName: clusterName
    clusterNamespace: clusterNamespace
    clusterLocation: clusterLocation
    clusterResourceId: cluster.id
    customLocationName: customLocationName ?? 'location-${HASH}'
    clExtensionIds: clExtensionIds
    instanceName: aioInstanceName ?? 'aio-${HASH}'
    instanceIdentity: instanceIdentity
    schemaRegistryId: schemaRegistryId
    adrNamespaceId: adrNamespaceId
    features: features
    defaultDataflowInstanceCount: defaultDataflowInstanceCount
    aioVersion: aioVersion
    aioTrain: aioTrain
    observabilityEnabled: observabilityEnabled
    otelCollectorAddress: otelCollectorAddress
    aioConfigurationOverrides: aioConfigurationOverrides
    brokerConfig: brokerConfig
    trustConfig: trustConfig
  }
}

module resources_2026 './modules/instance-2026-03-01.bicep' = if (aioApiVersion == '2026-03-01') {
  name: 'aio-resources-2026-03-01'
  params: {
    clusterName: clusterName
    clusterNamespace: clusterNamespace
    clusterLocation: clusterLocation
    clusterResourceId: cluster.id
    customLocationName: customLocationName ?? 'location-${HASH}'
    clExtensionIds: clExtensionIds
    instanceName: aioInstanceName ?? 'aio-${HASH}'
    instanceIdentity: instanceIdentity
    schemaRegistryId: schemaRegistryId
    adrNamespaceId: adrNamespaceId
    features: features
    defaultDataflowInstanceCount: defaultDataflowInstanceCount
    aioVersion: aioVersion
    aioTrain: aioTrain
    observabilityEnabled: observabilityEnabled
    otelCollectorAddress: otelCollectorAddress
    aioConfigurationOverrides: aioConfigurationOverrides
    brokerConfig: brokerConfig
    trustConfig: trustConfig
  }
}

// Select outputs from the active module.
// The @allowed constraint on aioApiVersion guarantees exactly one module deploys.
//
// --- Adding a new API version ----------------------------------------------
// Convention: the newest API version is always the else-branch (falsy fallback);
// every older version is an explicit positive equality check.
//
// When 2027-01-01 lands:
//   1. Extend @allowed on aioApiVersion above.
//   2. Add a new `module resources_<YYYY>` conditional block mirroring the
//      existing two.
//   3. Push the previously-newest branch into an explicit equality and make
//      the new version the else:
//
//        var activeResources = aioApiVersion == '2025-10-01' ? { ...2025 }
//                            : aioApiVersion == '2026-03-01' ? { ...2026 }
//                            : { ...2027 }   // newest, no equality check
//
//   4. Mirror the same change in templates/aio/modules/update-instance.bicep.
// ---------------------------------------------------------------------------
var activeResources = aioApiVersion == '2025-10-01'
  ? {
      instanceName: resources_2025!.outputs.instanceName
      brokerName: resources_2025!.outputs.brokerName
      brokerListenerName: resources_2025!.outputs.brokerListenerName
      brokerAuthnName: resources_2025!.outputs.brokerAuthnName
      brokerSettings: resources_2025!.outputs.brokerSettings
      aioExtensionId: resources_2025!.outputs.aioExtensionId
      aioExtensionName: resources_2025!.outputs.aioExtensionName
      aioExtensionVersion: resources_2025!.outputs.aioExtensionVersion
      aioExtensionReleaseTrain: resources_2025!.outputs.aioExtensionReleaseTrain
      aioExtensionPrincipalId: resources_2025!.outputs.aioExtensionPrincipalId
      customLocationId: resources_2025!.outputs.customLocationId
      customLocationName: resources_2025!.outputs.customLocationName
    }
  : {
      instanceName: resources_2026!.outputs.instanceName
      brokerName: resources_2026!.outputs.brokerName
      brokerListenerName: resources_2026!.outputs.brokerListenerName
      brokerAuthnName: resources_2026!.outputs.brokerAuthnName
      brokerSettings: resources_2026!.outputs.brokerSettings
      aioExtensionId: resources_2026!.outputs.aioExtensionId
      aioExtensionName: resources_2026!.outputs.aioExtensionName
      aioExtensionVersion: resources_2026!.outputs.aioExtensionVersion
      aioExtensionReleaseTrain: resources_2026!.outputs.aioExtensionReleaseTrain
      aioExtensionPrincipalId: resources_2026!.outputs.aioExtensionPrincipalId
      customLocationId: resources_2026!.outputs.customLocationId
      customLocationName: resources_2026!.outputs.customLocationName
    }

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output aioExtension object = {
  name: activeResources.aioExtensionName
  id: activeResources.aioExtensionId
  version: activeResources.aioExtensionVersion
  releaseTrain: activeResources.aioExtensionReleaseTrain
  config: {
    trustConfig: trustConfig
  }
  identityPrincipalId: activeResources.aioExtensionPrincipalId
}

output aio object = {
  name: activeResources.instanceName
  broker: {
    name: activeResources.brokerName
    listener: activeResources.brokerListenerName
    authn: activeResources.brokerAuthnName
    settings: activeResources.brokerSettings
  }
}

output customLocation object = {
  id: activeResources.customLocationId
  name: activeResources.customLocationName
}

output location string = clusterLocation

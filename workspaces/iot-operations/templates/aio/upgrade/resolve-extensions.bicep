// resolve-extensions.bicep
// -------------------------------------------------------------------------------------
// Resolves the AIO, azure-secret-store, and (conditionally) cert-manager Arc
// extensions on the cluster hosting the AIO instance, returning uniform
// snapshots that update-extensions consumes.
//
// Discovery: direct `existing` lookups by names sourced from
// `templates/common/extension-names.bicep`, the same module the install path
// uses to STAMP these names. Drift between install and upgrade is structurally
// impossible because both sides import the same authoritative deriver/constants.
//
// cert-manager ownership: the conditional `existing` is gated on the deploy-time
// `enableCertManager` parameter (sourced from `site.properties.deployOptions.
// enableCertManager`). This is the same flag that gates the install in
// `enablement.bicep`, so the upgrade's view of cert-manager ownership matches
// the install's. Sites that delegate cert-manager to a customer-managed install
// pass `enableCertManager: false` and the snapshot is returned zero-valued.
//
// Why not iterate `customLocation.clusterExtensionIds` and filter by extensionType?
//   BCP138 forces duplicating the filter predicate per extension, `filter(...)[0]`
//   produces opaque ARM errors when an entry is missing, and cert-manager is
//   outside the custom-location boundary so a CL-scoped lookup would not cover
//   it uniformly. Direct lookups through the shared name deriver give equivalent
//   authority with simpler Bicep and clearer "resource not found" diagnostics.
// -------------------------------------------------------------------------------------

import {
  aioExtensionName as deriveAioExtensionName
  secretStoreExtensionName
  certManagerExtensionName
  certManagerExtensionType
} from '../../common/extension-names.bicep'

// =====================================================================================
// Parameters
// =====================================================================================

@description('Name of the Arc-connected cluster hosting the AIO instance. Chained from resolve-aio.outputs.connectedClusterName.')
param connectedClusterName string

@description('Full ARM resource ID of the connected cluster. Chained from resolve-aio.outputs.connectedClusterResourceId. Used to derive the AIO Arc extension name via the same uniqueString algebra the install path uses.')
param connectedClusterResourceId string

@description('Whether scalekit owns cert-manager on this cluster. Sourced from `site.properties.deployOptions.enableCertManager`. False skips cert-manager resolution entirely so externally-managed cert-manager installs are never read by this template.')
param enableCertManager bool

// =====================================================================================
// Direct existing lookups via shared source-of-truth names.
// =====================================================================================

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: connectedClusterName
}

resource aioExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = {
  scope: cluster
  name: deriveAioExtensionName(connectedClusterResourceId)
}

resource secretStoreExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = {
  scope: cluster
  name: secretStoreExtensionName
}

resource certManagerExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = if (enableCertManager) {
  scope: cluster
  name: certManagerExtensionName
}

// =====================================================================================
// Outputs: uniform snapshot shape consumed by update-extensions.
// =====================================================================================

@description('AIO Arc extension snapshot (id, name, extensionType, version, releaseTrain, configurationSettings, identity, releaseNamespace). releaseNamespace is forwarded into update-extensions so the upgrade PUT preserves the cluster namespace stamped by the install path.')
output aio object = {
  id: aioExtension.id
  name: aioExtension.name
  extensionType: aioExtension.properties.extensionType
  version: aioExtension.properties.?version ?? ''
  releaseTrain: aioExtension.properties.?releaseTrain ?? ''
  configurationSettings: aioExtension.properties.?configurationSettings ?? {}
  identity: aioExtension.?identity ?? { type: 'None' }
  releaseNamespace: aioExtension.properties.?scope.?cluster.?releaseNamespace ?? 'azure-iot-operations'
}

@description('Secret store Arc extension snapshot.')
#disable-next-line outputs-should-not-contain-secrets
output secretStore object = {
  id: secretStoreExtension.id
  name: secretStoreExtension.name
  extensionType: secretStoreExtension.properties.extensionType
  version: secretStoreExtension.properties.?version ?? ''
  releaseTrain: secretStoreExtension.properties.?releaseTrain ?? ''
  configurationSettings: secretStoreExtension.properties.?configurationSettings ?? {}
  identity: secretStoreExtension.?identity ?? { type: 'None' }
}

@description('cert-manager Arc extension snapshot. Populated when enableCertManager is true; otherwise zero-valued with the canonical name and type so update-extensions can consume a uniform shape.')
output certManager object = enableCertManager
  ? {
      id: certManagerExtension!.id
      name: certManagerExtension!.name
      extensionType: certManagerExtension!.properties.extensionType
      version: certManagerExtension!.properties.?version ?? ''
      releaseTrain: certManagerExtension!.properties.?releaseTrain ?? ''
      configurationSettings: certManagerExtension!.properties.?configurationSettings ?? {}
      identity: certManagerExtension!.?identity ?? { type: 'None' }
    }
  : {
      id: ''
      name: certManagerExtensionName
      extensionType: certManagerExtensionType
      version: ''
      releaseTrain: ''
      configurationSettings: {}
      identity: { type: 'None' }
    }

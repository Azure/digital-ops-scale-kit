// resolve-extensions.bicep
// -------------------------------------------------------------------------------------
// Resolves the AIO and azure-secret-store Arc extensions on the cluster hosting
// the AIO instance and surfaces the `trustSource` recorded in the AIO extension's
// configurationSettings so a follow-up step can decide whether cert-manager is
// present on the cluster.
//
// Discovery: direct `existing` lookups by names sourced from
// `templates/common/extension-names.bicep`, the same module the install path
// uses to STAMP these names. Drift between install and upgrade is structurally
// impossible because both sides import the same authoritative deriver/constants.
//
// What this template does NOT resolve:
//   cert-manager. Its presence depends on the runtime trustSource read here,
//   and Bicep does not allow `existing if(cond)` where `cond` is produced at
//   deployment runtime by a sibling module/resource (BCP182). cert-manager
//   resolution is therefore the next manifest step (`resolve-cert-manager.bicep`),
//   gated by `detectedTrustSource` chained from this template's outputs.
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
} from '../../common/extension-names.bicep'

// =====================================================================================
// Parameters
// =====================================================================================

@description('Name of the Arc-connected cluster hosting the AIO instance. Chained from resolve-aio.outputs.connectedClusterName.')
param connectedClusterName string

@description('Full ARM resource ID of the connected cluster. Chained from resolve-aio.outputs.connectedClusterResourceId. Used to derive the AIO Arc extension name via the same uniqueString algebra the install path uses.')
param connectedClusterResourceId string

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

// =====================================================================================
// Outputs: uniform snapshot shape consumed by update-extensions alongside
// resolve-cert-manager.outputs.snapshot.
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

@description('Trust source detected from the AIO extension configurationSettings.trustSource (set at install time by instance-*.bicep). Chained into resolve-cert-manager.bicep to gate whether cert-manager is resolved on the cluster.')
output detectedTrustSource string = aioExtension.properties.?configurationSettings.?trustSource ?? ''

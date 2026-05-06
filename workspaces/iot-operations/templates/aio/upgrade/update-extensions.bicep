// update-extensions.bicep
// -------------------------------------------------------------------------------------
// Bumps Arc extension versions for AIO, secret-store, and (conditionally) cert-manager
// while preserving each extension's existing configurationSettings, releaseTrain, and
// identity. Inputs are typically chained from `resolve-extensions.bicep` outputs.
//
// Per-extension target inputs (`<ext>TargetVersion`, `<ext>TargetTrain`,
// `<ext>ConfigurationOverrides`) are all optional. Empty target version = no bump
// for that extension (the resolved current version is preserved on PUT, which is a
// no-op idempotent re-PUT). Empty train = preserve resolved train.
// configurationOverrides are unioned over the existing configurationSettings so
// the PUT cannot wipe operator state.
//
// cert-manager is gated by `enableCertManager`: when false, no cert-manager PUT
// is emitted. (Conditional resource declaration; the existing extension is left
// untouched.)
//
// API version: `Microsoft.KubernetesConfiguration/extensions@2023-05-01` is fixed
// across AIO releases and is not driven by the AIO API version dispatcher. If a
// future AIO release requires a different extensions API, apply the versioned
// router pattern (see `update-instance.bicep`) rather than mutating this template
// in place.
//
// IMPORTANT: `union()` is ADDITIVE-ONLY:
//   `union(existing, overrides)` cannot delete or rename keys in existing. If a
//   future AIO release renames a `configurationSettings` key (e.g.,
//   `trustSource` -> `trust.source`), this template will preserve BOTH the old
//   and new keys on PUT, which the RP may reject. When such a schema migration
//   actually arrives, choose between (a) adding an `excludeKeys` parameter that
//   filters keys out of `existing` before the union, or (b) introducing a
//   versioned `update-extensions-<apiVersion>.bicep` behind a router. Do NOT
//   pre-build either mechanism for hypothetical migrations.
//
// IMPORTANT: `scope.cluster.releaseNamespace` handling is per-extension:
//   - AIO: install path parameterizes `releaseNamespace: clusterNamespace` (default
//     `azure-iot-operations` but overridable). The upgrade PUT MUST forward whatever
//     the install stamped to avoid a full-replace dropping the field. Snapshotted
//     in resolve-extensions and forwarded as `aio.releaseNamespace` below.
//   - secret-store: install path does NOT set `scope` (lets the RP default apply).
//     The upgrade PUT mirrors the install by omitting `scope` entirely.
//   - cert-manager: install path hardcodes `'cert-manager'`; the PUT below hardcodes
//     the same value rather than reading from the snapshot. ARM treats
//     `releaseNamespace` as immutable post-create, so the hardcode is functionally
//     equivalent for scalekit-managed installs and avoids a snapshot field for a
//     value scalekit owns. If Microsoft ever changes the default cert-manager
//     namespace, both install and this template must be updated together.
// -------------------------------------------------------------------------------------

// =====================================================================================
// Parameters: connected cluster + resolved snapshots (from resolve-extensions chaining)
// =====================================================================================

@description('Name of the Arc-connected cluster hosting the AIO instance.')
param connectedClusterName string

@description('AIO extension snapshot from resolve-extensions.outputs.aio. Carries name, version, releaseTrain, configurationSettings, identityType.')
param aio object

@description('Secret store extension snapshot from resolve-extensions.outputs.secretStore.')
#disable-next-line secure-secrets-in-params
param secretStore object

@description('cert-manager extension snapshot from resolve-extensions.outputs.certManager. Ignored when enableCertManager is false.')
param certManager object

@description('Whether scalekit owns cert-manager on this cluster. Sourced from `site.properties.deployOptions.enableCertManager`. False skips the cert-manager extension PUT and assumes cert-manager is externally managed.')
param enableCertManager bool

// =====================================================================================
// Parameters: target versions (all optional; empty = preserve resolved).
// Names mirror the keys in `parameters/aio-releases/<release>.yaml` so the release config
// can be wired in directly via the manifest's `parameters:` list (same source the
// install path consumes).
// =====================================================================================

@description('Target version for the AIO Arc extension. Empty preserves the resolved current version.')
param aioVersion string = ''

@description('Target release train for the AIO Arc extension. Empty preserves the resolved current train.')
param aioTrain string = ''

@description('Configuration overrides to merge over the AIO extension\'s existing configurationSettings on PUT. Empty preserves config exactly.')
param aioConfigurationOverrides object = {}

@description('Target version for the secret store Arc extension. Empty preserves the resolved current version.')
#disable-next-line secure-secrets-in-params
param secretStoreVersion string = ''

@description('Target release train for the secret store Arc extension. Empty preserves the resolved current train.')
#disable-next-line secure-secrets-in-params
param secretStoreTrain string = ''

@description('Configuration overrides to merge over the secret store extension\'s existing configurationSettings on PUT.')
#disable-next-line secure-secrets-in-params
param secretStoreConfigurationOverrides object = {}

@description('Target version for the cert-manager Arc extension. Ignored when enableCertManager is false. Empty preserves the resolved current version.')
param certManagerVersion string = ''

@description('Target release train for the cert-manager Arc extension. Ignored when enableCertManager is false. Empty preserves the resolved current train.')
param certManagerTrain string = ''

@description('Configuration overrides to merge over the cert-manager extension\'s existing configurationSettings on PUT. Ignored when enableCertManager is false.')
param certManagerConfigurationOverrides object = {}

// =====================================================================================
// Effective values: empty target preserves the resolved current value.
// =====================================================================================

var effectiveAioVersion = !empty(aioVersion) ? aioVersion : aio.version
var effectiveAioTrain = !empty(aioTrain) ? aioTrain : aio.releaseTrain

var effectiveSecretStoreVersion = !empty(secretStoreVersion) ? secretStoreVersion : secretStore.version
var effectiveSecretStoreTrain = !empty(secretStoreTrain) ? secretStoreTrain : secretStore.releaseTrain

var effectiveCertManagerVersion = !empty(certManagerVersion) ? certManagerVersion : certManager.version
var effectiveCertManagerTrain = !empty(certManagerTrain) ? certManagerTrain : certManager.releaseTrain

// =====================================================================================
// Cluster reference (extensions are scoped to the connected cluster).
// =====================================================================================

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: connectedClusterName
}

// =====================================================================================
// AIO Extension: PUT with target version, preserving config + identity.
// =====================================================================================

resource aioExtensionUpdate 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = {
  scope: cluster
  name: aio.name
  identity: aio.identity
  properties: {
    extensionType: aio.extensionType
    version: effectiveAioVersion
    releaseTrain: effectiveAioTrain
    autoUpgradeMinorVersion: false
    scope: {
      cluster: {
        releaseNamespace: aio.releaseNamespace
      }
    }
    configurationSettings: union(aio.configurationSettings, aioConfigurationOverrides)
  }
}

// =====================================================================================
// Secret Store Extension: PUT with target version, preserving config + identity.
// =====================================================================================

resource secretStoreExtensionUpdate 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = {
  scope: cluster
  name: secretStore.name
  identity: secretStore.identity
  properties: {
    extensionType: secretStore.extensionType
    version: effectiveSecretStoreVersion
    releaseTrain: effectiveSecretStoreTrain
    autoUpgradeMinorVersion: false
    // union() preserves existing keys and overlays overrides. Keys removed in
    // newer schemas are not pruned, so they accumulate across multi-hop upgrades.
    configurationSettings: union(secretStore.configurationSettings, secretStoreConfigurationOverrides)
  }
  // Conditional dependency must stay in sync with the `if (enableCertManager)`
  // guard on certManagerExtensionUpdate below.
  dependsOn: enableCertManager ? [certManagerExtensionUpdate] : []
}

// =====================================================================================
// cert-manager Extension: conditional PUT only when present on the cluster.
// =====================================================================================

resource certManagerExtensionUpdate 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = if (enableCertManager) {
  scope: cluster
  name: certManager.name
  identity: certManager.identity
  properties: {
    extensionType: certManager.extensionType
    version: effectiveCertManagerVersion
    releaseTrain: effectiveCertManagerTrain
    autoUpgradeMinorVersion: false
    scope: {
      cluster: {
        releaseNamespace: 'cert-manager'
      }
    }
    configurationSettings: union(certManager.configurationSettings, certManagerConfigurationOverrides)
  }
}

// =====================================================================================
// Outputs: post-upgrade state, useful for E2E/integration assertions.
// =====================================================================================

@description('Resource ID of the (updated) AIO Arc extension.')
output aioExtensionId string = aioExtensionUpdate.id

@description('Resource ID of the (updated) secret store Arc extension.')
output secretStoreExtensionId string = secretStoreExtensionUpdate.id

@description('Resource ID of the (updated) cert-manager Arc extension. Empty when enableCertManager is false.')
output certManagerExtensionId string = enableCertManager ? certManagerExtensionUpdate!.id : ''

@description('Effective version applied to the AIO Arc extension.')
output aioVersionApplied string = effectiveAioVersion

@description('Effective version applied to the secret store Arc extension.')
output secretStoreVersionApplied string = effectiveSecretStoreVersion

@description('Effective version applied to the cert-manager Arc extension. Empty when enableCertManager is false.')
output certManagerVersionApplied string = enableCertManager ? effectiveCertManagerVersion : ''

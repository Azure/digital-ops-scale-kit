// enablement.bicep
// -------------------------------------------------------------------------------------
// Deploys the AIO platform enablement extensions (cert-manager and secret store)
// onto an Arc-enabled Kubernetes cluster.
//
// This template is version-agnostic: callers supply the extension versions and
// release trains explicitly, so the same template works across AIO releases
// without embedding version constants.
// -------------------------------------------------------------------------------------

import { certManagerExtensionName, secretStoreExtensionName, certManagerExtensionType, secretStoreExtensionType } from '../common/extension-names.bicep'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

/*                          Cluster Parameters                               */
///////////////////////////////////////////////////////////////////////////////

@description('Name of the existing arc-enabled cluster where AIO will be deployed.')
param clusterName string

/*                          Capability Toggles                               */
///////////////////////////////////////////////////////////////////////////////

@description('Whether scalekit owns the cert-manager Arc extension on this cluster. When false, the install assumes cert-manager is provided externally and skips both the install and the dependsOn wiring on the secret store extension. Mirrors `site.properties.deployOptions.enableCertManager`.')
param enableCertManager bool = true

/*                          Extension Version Parameters                     */
///////////////////////////////////////////////////////////////////////////////

@description('Version of the cert-manager extension to install.')
param certManagerVersion string

@description('Release train of the cert-manager extension.')
param certManagerTrain string = 'stable'

@description('Version of the secret store extension to install.')
#disable-next-line secure-secrets-in-params
param secretStoreVersion string

@description('Release train of the secret store extension.')
#disable-next-line secure-secrets-in-params
param secretStoreTrain string = 'stable'

@description('Additional configuration settings for the cert-manager extension.')
param certManagerConfigurationOverrides object = {}

@description('Additional configuration settings for the secret store extension.')
#disable-next-line secure-secrets-in-params // Configuration overrides, not a secret
param secretStoreConfigurationOverrides object = {}

/*****************************************************************************/
/*         Existing Arc-enabled cluster where AIO will be deployed.          */
/*****************************************************************************/

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: clusterName
}

/*****************************************************************************/
/*                      Azure IoT Operations Dependencies.                   */
/*****************************************************************************/

resource certManagerExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = if (enableCertManager) {
  scope: cluster
  name: certManagerExtensionName
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    extensionType: certManagerExtensionType
    releaseTrain: certManagerTrain
    version: certManagerVersion
    autoUpgradeMinorVersion: false
    scope: {
      cluster: {
        releaseNamespace: 'cert-manager'
      }
    }
    configurationSettings: union({
        AgentOperationTimeoutInMinutes: '20'
        'global.telemetry.enabled': 'true'
      }, certManagerConfigurationOverrides)
  }
}

resource secretStoreExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' = {
  scope: cluster
  name: secretStoreExtensionName
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    extensionType: secretStoreExtensionType
    version: secretStoreVersion
    releaseTrain: secretStoreTrain
    autoUpgradeMinorVersion: false
    configurationSettings: union({
        rotationPollIntervalInSeconds: '120'
        'validatingAdmissionPolicies.applyPolicies': 'false'
      }, secretStoreConfigurationOverrides)
  }
  dependsOn: enableCertManager ? [certManagerExtension] : []
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output clExtensionIds string[] = [
  secretStoreExtension.id
]

output extensions object = {
  certManager: {
    name: certManagerExtension.?name
    id: certManagerExtension.?id
    version: certManagerExtension.?properties.version
    releaseTrain: certManagerExtension.?properties.releaseTrain
  }
  secretStore: {
    name: secretStoreExtension.name
    id: secretStoreExtension.id
    version: secretStoreExtension.properties.version
    releaseTrain: secretStoreExtension.properties.releaseTrain
  }
}

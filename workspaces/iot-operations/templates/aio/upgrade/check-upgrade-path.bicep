// check-upgrade-path.bicep
// -------------------------------------------------------------------------------------
// First step of aio-upgrade. Reads the installed AIO Arc extension's version
// and release train, and decides with upgrade-path.bicep whether aio-upgrade
// may move it to the target AIO version and train. A refused move fails this
// deployment before any later step reads the instance or changes anything.
// update-extensions.bicep repeats the decision on the target it applies.
//
// The read uses the cluster name from the Site, so it does not depend on the
// target's IoT Operations API version. Without `clusterName`, the check reports
// that it could not read the installed version, and update-extensions still
// refuses an unsupported move before it writes.
//
// Read only: every resource is `existing`. Nothing of cert-manager is read.
// -------------------------------------------------------------------------------------

import { aioExtensionName } from '../../common/extension-names.bicep'
import { targetDecision, upgradePaths } from 'upgrade-path.bicep'

@description('Name of the Arc-connected cluster hosting the AIO instance, from the Site\'s `parameters.clusterName`. The cluster is in the Site\'s resource group.')
param clusterName string = ''

@description('Target AIO version, from the Site\'s AIO release file or its `aioVersion` override. Empty keeps the installed version, as the extension update does.')
param aioVersion string = ''

@description('Target AIO release train, from the Site\'s AIO release file or its `aioTrain` override. Empty keeps the installed train.')
param aioTrain string = ''

var canRead = !empty(clusterName)

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = if (canRead) {
  name: clusterName
}

resource aioExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = if (canRead) {
  scope: cluster
  name: aioExtensionName(resourceId('Microsoft.Kubernetes/connectedClusters', clusterName))
}

var installedVersion = canRead ? (aioExtension!.properties.?version ?? '') : ''
var installedTrain = canRead ? (aioExtension!.properties.?releaseTrain ?? '') : ''
var decision = targetDecision(installedVersion, installedTrain, aioVersion, aioTrain, upgradePaths)

@description('Decision: unchanged, supported or unverified. A refused move fails the deployment with the reason.')
output status string = decision.status == 'refused' ? fail(decision.message) : decision.status

@description('AIO version installed before this run, empty when it could not be read.')
output installedVersion string = installedVersion

@description('Warnings for Site Ops to show: why the path was not checked, when it was not.')
output warnings array = decision.status == 'unverified' ? [{ message: decision.message }] : []

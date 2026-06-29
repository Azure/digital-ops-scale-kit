// extension-names.bicep
// -------------------------------------------------------------------------------------
// Import-only library of cluster-side names referenced across the workspace.
//
// Two categories:
//   1. Names scalekit owns (cert-manager, azure-secret-store, AIO extension). Stamped
//      by the install path and read back by the upgrade path. Defining them here once
//      makes drift between paths structurally impossible.
//   2. Names fixed by upstream contracts that scalekit must reference (the AIO
//      secret-sync service account, deployed by the AIO Arc extension). Scalekit uses
//      this name to federate a UAMI with the cluster OIDC issuer for workload identity.
//
// Auto-discovery assumes scalekit-owned defaults. Clusters where extensions were
// installed out-of-band with non-default names are supported via parameter override.
// -------------------------------------------------------------------------------------

@description('Fixed name of the cert-manager Arc extension installed by scalekit.')
@export()
var certManagerExtensionName = 'cert-manager'

@description('Fixed name of the azure-secret-store Arc extension installed by scalekit.')
@export()
var secretStoreExtensionName = 'azure-secret-store'

@description('Derives the per-cluster AIO Arc extension name from the connected cluster resource ID. Mirrors the install-time name computation.')
@export()
func aioExtensionName(clusterResourceId string) string => 'azure-iot-operations-${take(uniqueString(clusterResourceId), 5)}'

@description('Authoritative extensionType discriminator for the AIO Arc extension.')
@export()
var aioExtensionType = 'microsoft.iotoperations'

@description('Authoritative extensionType discriminator for the azure-secret-store Arc extension.')
@export()
var secretStoreExtensionType = 'microsoft.azure.secretstore'

@description('Authoritative extensionType discriminator for the cert-manager Arc extension.')
@export()
var certManagerExtensionType = 'microsoft.certmanagement'

@description('Fixed name of the Kubernetes service account that the AIO Arc extension deploys on the cluster. Scalekit references this name when federating a UAMI with the cluster OIDC issuer so SecretSync can pull secrets via workload identity. The name is owned by the AIO extension contract, not by scalekit.')
@export()
var aioSecretSyncServiceAccountName = 'aio-ssc-sa'

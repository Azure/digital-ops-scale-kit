// resolve-cluster.bicep
// -------------------------------------------------------------------------------------
// Reusable module: resolves a connected cluster from its full ARM resource ID.
//
// Accepts the full resource ID (e.g., from customLocation.properties.hostResourceId),
// parses the name, declares the cluster as an existing resource, and outputs
// its OIDC issuer URLs for workload identity federation.
//
// The module boundary converts the runtime resource ID into a compile-time
// parameter, allowing the existing resource lookup that Bicep otherwise
// prohibits on runtime values.
// -------------------------------------------------------------------------------------

@description('Full ARM resource ID of the Arc-connected cluster.')
param connectedClusterResourceId string

var connectedClusterName = last(split(connectedClusterResourceId, '/'))

resource connectedCluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: connectedClusterName
}

@description('Connected cluster name (parsed from resource ID).')
output name string = connectedCluster.name

@description('Full ARM resource ID of the connected cluster. Used by upgrade flow to compute the AIO Arc extension name via aioExtensionName(clusterResourceId), mirroring the install-time derivation.')
output id string = connectedCluster.id

@description('Public OIDC issuer URL for workload identity federation.')
output oidcIssuerUrl string = connectedCluster.properties.oidcIssuerProfile.issuerUrl

@description('Self-hosted OIDC issuer URL (empty string if not configured).')
output selfHostedIssuerUrl string = connectedCluster.properties.oidcIssuerProfile.?selfHostedIssuerUrl ?? ''

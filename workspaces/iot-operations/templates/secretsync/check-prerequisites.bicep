// check-prerequisites.bicep
// -------------------------------------------------------------------------------------
// Secret Sync prerequisite guard. The shared Secret Sync partial runs it after
// resolve-aio and before enable-secretsync, so integrated and standalone
// enablement stop before any Secret Sync write when the Arc cluster cannot
// federate workload identity.
//
// It reads the cluster's OIDC issuer and workload identity flags, and checks
// the issuer that resolve-aio selected, which enablement writes to the
// federated identity credential. The issuer is checked as is, never changed.
//
// Read only: the cluster is `existing`. resolve-aio stays free of Secret Sync
// requirements, because upgrade and other consumers support clusters without
// workload identity.
// -------------------------------------------------------------------------------------

@description('Name of the Arc-connected cluster. Chained from resolve-aio.outputs.connectedClusterName.')
param connectedClusterName string

@description('OIDC issuer URL that enablement federates with. Chained from resolve-aio.outputs.oidcIssuerUrl.')
param oidcIssuerUrl string

@description('Whether resolve-aio selected the self-hosted issuer. Names the cluster field in the guidance.')
param useSelfHostedIssuer bool = false

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: connectedClusterName
}

var problem = prerequisiteProblem(
  cluster.properties.?oidcIssuerProfile.?enabled ?? false,
  cluster.properties.?securityProfile.?workloadIdentity.?enabled ?? false,
  oidcIssuerUrl,
  useSelfHostedIssuer
)

@description('The issuer that enablement federates with. A missing prerequisite fails the deployment with guidance.')
output oidcIssuerUrl string = empty(problem) ? oidcIssuerUrl : fail(problem)

@export()
@description('Returns guidance for the first missing Secret Sync prerequisite, or an empty string.')
func prerequisiteProblem(oidcIssuerEnabled bool, workloadIdentityEnabled bool, issuer string, selfHosted bool) string =>
  !oidcIssuerEnabled
    ? 'Secret Sync needs the OIDC issuer on the Arc cluster, and oidcIssuerProfile.enabled is not true. ${preparation()}'
    : !workloadIdentityEnabled
      ? 'Secret Sync needs workload identity on the Arc cluster, and securityProfile.workloadIdentity.enabled is not true. ${preparation()}'
      : empty(issuer)
        ? 'Secret Sync needs an OIDC issuer URL, and the Arc cluster\'s ${issuerField(selfHosted)} is empty. ${preparation()}'
        : !startsWith(toLower(issuer), 'https://')
          ? 'Secret Sync needs an HTTPS OIDC issuer URL, and the Arc cluster\'s ${issuerField(selfHosted)} does not start with https://. ${preparation()}'
          : ''

func issuerField(selfHosted bool) string => selfHosted ? 'oidcIssuerProfile.selfHostedIssuerUrl' : 'oidcIssuerProfile.issuerUrl'

func preparation() string =>
  'Enable the OIDC issuer and workload identity on the cluster, and configure its API server to issue tokens with that issuer, then run Secret Sync again. See https://learn.microsoft.com/azure/iot-operations/secure-iot-ops/howto-enable-secure-settings.'

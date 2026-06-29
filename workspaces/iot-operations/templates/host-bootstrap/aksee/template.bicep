// Delivers the AKS EE bootstrap launcher to an Arc-connected Windows VM via
// `Microsoft.HybridCompute/machines/runCommands`. The Connected Machine agent
// on the VM polls Azure, picks up the runCommand, executes the launcher
// script body locally with the supplied parameters, and reports back into
// the resource's instanceView.
//
// The launcher writes the worker + AKS EE config template to disk, registers a
// Scheduled Task that drives the worker (running as SYSTEM by default, or as a
// created local admin when runAsDedicatedAdmin is set), starts the task, and
// returns `REGISTERED`. ARM sees the runCommand succeed at
// that point. The actual bootstrap (Hyper-V enable, reboot, cluster deploy,
// Arc-connect) happens inside the Scheduled Task asynchronously. The worker
// writes a `siteops.bootstrap.state` tag on the Arc machine when it finishes,
// and a siteops `type: wait` step gates downstream steps on that tag (see the
// `aio-with-aksee-bootstrap` sample).
//
// Prerequisites on the target VM (one-time per VM, outside this Bicep):
//   1. Server is Arc-connected (e.g., via `OnboardingScript.ps1`).
//   2. The Arc machine's system-assigned managed identity has access on the
//      resource group. The worker authenticates as this identity for the
//      Phase 3 Arc-connect, the AIO feature enablement, and the Phase 99 tag
//      write. No service principal is used (Phase 2 is an AioDeploy cluster-
//      only build). Use `Contributor` (simplest), or `Kubernetes Cluster -
//      Azure Arc Onboarding` plus `Tag Contributor` for least privilege.
//
// Usage as a scalekit step:
//   - name: aksee-bootstrap
//     template: templates/host-bootstrap/aksee/template.bicep
//     scope: resourceGroup
//     parameters:
//       - parameters/inputs/aksee-bootstrap.yaml

@description('Name of the existing Arc-enabled Windows machine resource (Microsoft.HybridCompute/machines).')
param machineName string

@description('Name to assign the runCommands child resource. Use a stable name so re-deploys overwrite the existing command rather than accumulating history entries.')
param runCommandName string = 'aksee-bootstrap'

@description('Location for the runCommands resource. Defaults to the resource group location, which typically matches the machine location.')
param location string = resourceGroup().location

@description('Name of the Arc-connected Kubernetes cluster that AKS EE will register inside the worker. Must match the cluster name the scalekit site overlay expects.')
param clusterName string

@description('Resource group that holds the Arc-connected server and will receive the new connectedClusters resource. Typically the same RG that holds this runCommand.')
param targetResourceGroup string = resourceGroup().name

@description('Subscription ID where the cluster will be Arc-registered.')
param targetSubscription string = subscription().subscriptionId

@description('Azure region for the connectedClusters and custom-location resources the worker creates inside the VM.')
param targetLocation string = resourceGroup().location

@description('Tenant-wide object ID for the Custom Locations RP service principal. Use `az ad sp show --id bc313c14-388c-4e7d-a58e-70017303ee3b --query id -o tsv` to retrieve.')
param customLocationsOid string

@description('URL of the AKS Edge Essentials MSI to install. Default is the official latest-K3s aka.ms shortcut. To pin a version, host the MSI yourself and point this at it.')
param aksEdgeMsiUrl string = 'https://aka.ms/aks-edge/k3s-msi'

@description('When true, Phase 3 enables the OIDC issuer and workload identity on the Arc-connected cluster and patches the K3s apiserver `service-account-issuer`. Required only when downstream AIO components use workload-identity-backed secret sync. Defaults to false.')
param enableWorkloadIdentity bool = false

@description('When true, the worker Scheduled Task runs as a created local admin account with an on-box generated password instead of the built-in SYSTEM account. Leave false unless a hardened environment forbids SYSTEM-context tasks. Defaults to false.')
param runAsDedicatedAdmin bool = false

@description('Timeout in seconds for the runCommand. It bounds only the synchronous launcher, which returns quickly after registering the Scheduled Task. The bootstrap itself runs asynchronously inside that task.')
param runCommandTimeoutSeconds int = 600

resource machine 'Microsoft.HybridCompute/machines@2024-11-10-preview' existing = {
  name: machineName
}

resource bootstrapCommand 'Microsoft.HybridCompute/machines/runCommands@2024-11-10-preview' = {
  parent: machine
  name: runCommandName
  location: location
  properties: {
    source: {
      // loadTextContent inlines the launcher at compile time, so we inline the
      // minified launcher (comments, blank lines, and leading whitespace
      // stripped) to stay within the runCommands inline-script size limit. Each
      // added feature narrows the margin. scriptUri delivery (a blob URL) is
      // the durable fix when the inline body no longer fits.
      script: loadTextContent('./scripts/Install-AksEeBootstrap.min.ps1')
    }
    // asyncExecution=false makes ARM block until the script body exits. The
    // launcher returns quickly. The long-running bootstrap is the Scheduled
    // Task it registers, which runs after ARM has already seen success, so
    // the runCommand result reflects only whether the launcher itself ran.
    asyncExecution: false
    timeoutInSeconds: runCommandTimeoutSeconds
    parameters: [
      { name: 'ClusterName',        value: clusterName }
      { name: 'ResourceGroup',      value: targetResourceGroup }
      { name: 'Subscription',       value: targetSubscription }
      { name: 'Location',           value: targetLocation }
      { name: 'CustomLocationsOid', value: customLocationsOid }
      { name: 'AksEdgeMsiUrl',      value: aksEdgeMsiUrl }
      // The launcher param is [string]. string() yields 'true' or 'false',
      // which the launcher parses case-insensitively. A bool value here
      // would be rejected by the runCommand's string-typed parameter.
      { name: 'EnableWorkloadIdentity', value: string(enableWorkloadIdentity) }
      { name: 'RunAsDedicatedAdmin',    value: string(runAsDedicatedAdmin) }
    ]
  }
}

@description('Final execution state of the launcher script (typically `Succeeded` when the launcher registered the Scheduled Task without error). Independent of the actual bootstrap outcome, which the Scheduled Task drives asynchronously after this resource completes.')
output executionState string = bootstrapCommand.properties.instanceView.executionState

@description('Exit code from the launcher script. 0 = launcher returned REGISTERED. Non-zero = launcher failed before registering the Scheduled Task. Check `stdout` and `errorOutput` for diagnostics.')
output exitCode int = bootstrapCommand.properties.instanceView.exitCode

@description('Stdout captured from the launcher. Truncated by ARM, so blob upload is the alternative for large output. Typically contains the per-step launcher log lines and the final REGISTERED marker.')
output stdout string = bootstrapCommand.properties.instanceView.output

@description('Stderr captured from the launcher. Truncated by ARM. Typically empty on success, populated on launcher failure.')
output errorOutput string = bootstrapCommand.properties.instanceView.error

@description('Fully qualified resource ID of the Arc machine that hosts the bootstrap. Useful for chaining downstream steps that target the same machine, such as the wait step that polls the bootstrap-state tag.')
output machineId string = machine.id

@description('Name of the runCommand resource. Re-deploys with the same name overwrite this resource. Use a different name (for example timestamped) to keep history.')
output runCommandName string = bootstrapCommand.name

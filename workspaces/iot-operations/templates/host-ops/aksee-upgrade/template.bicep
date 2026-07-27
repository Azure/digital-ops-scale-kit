// Delivers the AKS EE upgrade launcher to an Arc-connected Windows VM via
// `Microsoft.HybridCompute/machines/runCommands`. The Connected Machine agent on
// the VM polls Azure, picks up the runCommand, executes the launcher locally,
// and reports back into the resource's instanceView.
//
// The launcher writes the worker to disk, registers a Scheduled Task that drives
// it (running as NT AUTHORITY\SYSTEM), sets the in-progress completion tag, starts
// the task, and returns `REGISTERED`. ARM sees the runCommand succeed at that point.
// The actual upgrade (stage, apply, inner node-VM reboot, verify) happens inside
// the Scheduled Task asynchronously. The worker writes a
// `siteops.aksee.upgrade.state` tag on the Arc machine when it finishes, and a
// siteops `type: wait` step gates downstream steps on that tag.
//
// Two upgrade modes are supported via `allowKubernetesMinorUpgrade`:
//   false (default): patch updates within the current Kubernetes minor version.
//   true: sequential minor-version hops. `AcceptUpgrade` is set true only for
//   the run and re-pinned false after successful completion. A failed run
//   preserves the staged update cache. Use
//   `targetKubernetesVersion` to stop at a specific minor version. The wait
//   step timeout should be raised for multi-hop runs.
//
// Prerequisites on the target VM (one-time per VM, outside this Bicep):
//   1. An AKS Edge Essentials single-node cluster is already deployed and
//      Arc-connected (e.g., by the host-bootstrap/aksee bootstrap).
//   2. The Arc machine's system-assigned managed identity can write tags on the
//      Arc machine resource. The worker uses it only for the completion tag. The
//      post-upgrade verification runs on-box (Test-AksEdgeArcConnection, kubectl)
//      and needs no Azure permission. No service principal is used. The single
//      permission is `Microsoft.Resources/tags/write` on the Arc machine resource,
//      via `Tag Contributor` scoped to the machine or its resource group, or
//      `Contributor`. A VM bootstrapped by host-bootstrap/aksee already holds a
//      broader resource-group grant, so no extra assignment is needed there.
//
// Usage as a scalekit step:
//   - name: aksee-upgrade
//     template: templates/host-ops/aksee-upgrade/template.bicep
//     scope: resourceGroup
//     parameters:
//       - parameters/inputs/aksee-upgrade.yaml

@description('Name of the existing Arc-enabled Windows machine resource (Microsoft.HybridCompute/machines).')
param machineName string

@description('Name to assign the runCommands child resource. Use a stable name so re-deploys overwrite the existing command rather than accumulating history entries.')
param runCommandName string = 'aksee-upgrade'

@description('Location for the runCommands resource. Defaults to the resource group location, which typically matches the machine location.')
param location string = resourceGroup().location

@description('Resource group that holds the Arc-connected server and the connected cluster. Typically the same RG that holds this runCommand.')
param targetResourceGroup string = resourceGroup().name

@description('Subscription ID where the Arc machine and connected cluster live.')
param targetSubscription string = subscription().subscriptionId

@description('Opaque per-deploy identifier recorded in the completion tag (siteops.aksee.upgrade.runId). Defaults to the deploy time so each deploy is correlatable. Re-deploys with a fresh value re-run the worker, which no-ops when no newer patch is available.')
param runId string = utcNow()

@description('When false (default), the worker applies patch updates within the current Kubernetes minor version. When true, the worker performs sequential minor-version hops. `AcceptUpgrade` is scoped to the run and re-pinned false after successful completion. A failed run preserves the staged update cache.')
param allowKubernetesMinorUpgrade bool = false

@description('Optional target Kubernetes minor version for minor-mode upgrades, e.g. `1.33` or `v1.33.5+k3s1`. The worker normalizes to major.minor and stops hopping once the deployed minor reaches this value. Empty string means no explicit target.')
param targetKubernetesVersion string = ''

@description('Timeout in seconds for the runCommand. It bounds only the synchronous launcher, which returns quickly after registering the Scheduled Task. The upgrade itself runs asynchronously inside that task.')
param runCommandTimeoutSeconds int = 600

resource machine 'Microsoft.HybridCompute/machines@2024-11-10-preview' existing = {
  name: machineName
}

resource upgradeCommand 'Microsoft.HybridCompute/machines/runCommands@2024-11-10-preview' = {
  parent: machine
  name: runCommandName
  location: location
  properties: {
    source: {
      // loadTextContent inlines the launcher at compile time. The minified
      // launcher (comments, blank lines, leading whitespace stripped) keeps the
      // inline script body within the runCommands size limit. scriptUri delivery
      // (a blob URL) is the durable fix when the inline body no longer fits.
      script: loadTextContent('./scripts/Install-AksEeUpgrade.min.ps1')
    }
    // asyncExecution=false makes ARM block until the launcher exits. The launcher
    // returns quickly. The long-running upgrade is the Scheduled Task it
    // registers, which runs after ARM has already seen success.
    asyncExecution: false
    timeoutInSeconds: runCommandTimeoutSeconds
    parameters: [
      { name: 'ResourceGroup', value: targetResourceGroup }
      { name: 'Subscription',  value: targetSubscription }
      { name: 'MachineName',   value: machineName }
      { name: 'RunId',         value: runId }
      // The launcher params are [string]. string() yields 'true'/'false', which
      // the launcher parses case-insensitively. A bool value would be rejected
      // by the runCommand's string-typed parameter.
      { name: 'AllowKubernetesMinorUpgrade', value: string(allowKubernetesMinorUpgrade) }
      { name: 'TargetKubernetesVersion',    value: targetKubernetesVersion }
    ]
  }
}

@description('Final execution state of the launcher script (typically `Succeeded` when the launcher registered the Scheduled Task). Independent of the actual upgrade outcome, which the Scheduled Task drives asynchronously and reports via the siteops.aksee.upgrade.state tag.')
output executionState string = upgradeCommand.properties.instanceView.executionState

@description('Exit code from the launcher script. 0 = launcher returned REGISTERED. Non-zero = launcher failed before registering the Scheduled Task.')
output exitCode int = upgradeCommand.properties.instanceView.exitCode

@description('Stdout captured from the launcher. Typically contains the per-step launcher log lines and the final REGISTERED marker.')
output stdout string = upgradeCommand.properties.instanceView.output

@description('Stderr captured from the launcher. Typically empty on success, populated on launcher failure.')
output errorOutput string = upgradeCommand.properties.instanceView.error

@description('Fully qualified resource ID of the Arc machine that hosts the upgrade. Useful for chaining the wait step that polls the upgrade-state tag.')
output machineId string = machine.id

@description('The runId recorded in the completion tag for this deploy.')
output runId string = runId

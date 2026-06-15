# AKS Edge Essentials host bootstrap

Takes a freshly Arc-onboarded Windows VM through:

- AKS Edge Essentials install (MSI + Hyper-V, including the reboot)
- Single-node K3s cluster deployment
- Cluster Arc-connect with custom locations enabled (OIDC issuer + workload identity opt-in)

After this completes, the cluster satisfies the AIO prerequisites and the existing AIO deploy chain runs against it.

Delivered remotely from Azure via Arc Run Command. The launcher writes a worker state machine + supporting files to the VM, registers a Scheduled Task that drives the worker through all phases (including survival of the Hyper-V reboot), and returns once the task is started. The operator never RDPs to the VM.

## How it composes

Three entry shapes:

| Entry | Use |
|---|---|
| `manifests/aksee-bootstrap.yaml` | Standalone host bootstrap. Stops at "cluster Arc-connected + AIO-ready". |
| `templates/host-bootstrap/aksee/_partial.yaml` | Internal partial co-located with this implementation. Composed by the standalone above and by compositions like the next row. |
| `samples/aio-with-aksee-bootstrap/manifest.yaml` | End-to-end bare VM to AIO in one deploy. Composes the partial above plus `_aio-fundamentals.yaml`. |

## Prerequisites per target VM (one-time)

### 1. Server is Arc-connected

The VM must already be Arc-onboarded as a `Microsoft.HybridCompute/machines` resource before the bootstrap runs. Use the [official onboarding flow](https://learn.microsoft.com/azure/azure-arc/servers/onboard-portal) or your existing onboarding script. The bootstrap targets the VM by its Arc machine name.

### 2. Grant the Arc machine identity access to the resource group

The bootstrap uses no service principal. The worker authenticates as the Arc machine's system-assigned managed identity for everything it does in Azure: the Phase 3 Arc-connect, the AIO feature enablement, and the Phase 99 state-tag write. Grant that identity access on the target resource group, either `Contributor` (simplest) or, for least privilege, `Kubernetes Cluster - Azure Arc Onboarding` (connect + enable-features) plus `Tag Contributor` (for the `Microsoft.Resources/tags/write` the tag needs).

```bash
ARC_PRINCIPAL_ID=$(az resource show -g <rg> -n <vm-name> --resource-type Microsoft.HybridCompute/machines --query "identity.principalId" -o tsv)
# Simplest: one Contributor grant on the resource group.
az role assignment create --assignee-object-id $ARC_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Contributor" --scope "/subscriptions/<sub>/resourceGroups/<rg>"
```

### 3. Resource providers registered on the subscription

```bash
az provider register --namespace Microsoft.HybridCompute
az provider register --namespace Microsoft.Kubernetes
az provider register --namespace Microsoft.KubernetesConfiguration
az provider register --namespace Microsoft.ExtendedLocation
az provider register --namespace Microsoft.IoTOperations
```

## Site configuration

Add an `aksee` section under your site's `parameters`. The bootstrap uses no secret, so the whole config lives in the committable `sites/` tree:

```yaml
# sites/<site>.yaml (committable)
name: my-site
subscription: <subscription-id>
resourceGroup: <rg-name>
location: westus2
labels:
  environment: dev
parameters:
  aksee:
    machineName: my-arc-windows-vm
    clusterName: my-aksee-cluster
    customLocationsOid: <custom-locations RP object id>
```

Required fields:

| Field | Source |
|---|---|
| `machineName` | The Arc-onboarded VM's machine resource name. |
| `clusterName` | Name to register the new K3s cluster as in Arc. New per site. |
| `customLocationsOid` | `az ad sp show --id bc313c14-388c-4e7d-a58e-70017303ee3b --query id -o tsv`. Tenant-wide. |

## Run

```bash
# Standalone host bootstrap (stops at cluster Arc-connected + AIO-ready)
siteops -w workspaces/iot-operations deploy manifests/aksee-bootstrap.yaml -l environment=dev

# Or bootstrap + AIO install in one deploy
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev
```

The deploy returns the moment the launcher returns `REGISTERED` (typically 30 to 90 seconds after the Arc agent picks up the run command). The actual bootstrap (25 to 40 minutes wall time) runs asynchronously on the VM inside the Scheduled Task. Use the monitor commands below from RDP to track phase progression.

## Monitor

From an admin PowerShell session on the VM (non-admin sessions cannot read the working directory, which is ACL-locked to Administrators + SYSTEM):

```powershell
$dir = 'C:\ProgramData\siteops\aksee-bootstrap'

# State (re-run every 30 to 60 seconds)
Get-Content (Join-Path $dir 'state.json') | ConvertFrom-Json | Format-List

# Worker log tail
$log = Get-ChildItem (Join-Path $dir 'worker-*.log') | Sort-Object LastWriteTime | Select-Object -Last 1
if ($log) { Get-Content $log.FullName -Tail 30 }
```

Phase progression to expect:

| Phase | Status | What's happening |
|---|---|---|
| 0 | running | Pre-flight checks (admin, OS, memory, disk, NuGet provider) |
| 1 | running | MSI install, Hyper-V enable (may reboot) |
| 2 | pending-reboot | Hyper-V reboot imminent or in progress |
| 2 | running | Cluster deployment (10 to 15 minutes) |
| 3 | running | Azure CLI install, Arc operations, custom locations enablement (5 to 10 minutes) |
| 99 | succeeded | Done |

Live-follow form for the latest worker log:

```powershell
$log = Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\worker-*.log' | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 50 -Wait
```

## Verify

On the VM after `state.json` shows `phase=99 status=succeeded`. The bootstrap copies the cluster kubeconfig to the shared ACL-locked path below (the original under the task account's profile is purged in Phase 99). Open an admin PowerShell and point `KUBECONFIG` at the shared copy:

```powershell
$env:KUBECONFIG = 'C:\ProgramData\siteops\aksee-bootstrap\kubeconfig'

kubectl get nodes
# Expect: one node, status Ready

az connectedk8s show --name <cluster-name> --resource-group <rg> --query connectivityStatus
# Expect: Connected
```

If you bootstrapped with workload identity enabled (see "Optional flags" below), additionally verify the OIDC issuer and workload identity surface:

```powershell
az connectedk8s show --name <cluster-name> --resource-group <rg> --query "{oidc:oidcIssuerProfile.enabled, wi:securityProfile.workloadIdentity.enabled}"
# Expect: oidc=true, wi=true
```

## If something goes wrong

### Monitoring shells need admin

`C:\ProgramData\siteops\aksee-bootstrap\` has ACLs locked to Administrators + SYSTEM at launcher time (the directory holds the cluster kubeconfig and the az token cache). A non-admin PowerShell session cannot read the state file, the worker transcripts, or the msiexec log. Open monitoring shells as Administrator.

### Bootstrap stuck on a phase

```powershell
# Read the error from state.json
$state = Get-Content 'C:\ProgramData\siteops\aksee-bootstrap\state.json' | ConvertFrom-Json
$state.error

# Read the latest transcript
$log = Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\worker-*.log' | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 100

# MSI install errors (Phase 1)
Get-Content 'C:\ProgramData\siteops\aksee-bootstrap\msiexec.log' -Tail 100

# AKS EE deployment errors (Phase 2). Worker captures the cmdlet's stdout
# and stderr from the child PowerShell process to separate files.
Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\aksee-deploy-*.log*' | Sort-Object LastWriteTime | Select-Object -Last 2 | ForEach-Object {
    Write-Host "===== $($_.Name) ====="
    Get-Content $_.FullName -Tail 20
}
```

### Scheduled Task is not firing

```powershell
Get-ScheduledTask -TaskName SiteOpsAksEeBootstrap | Get-ScheduledTaskInfo
# Look at LastRunTime and LastTaskResult. Result 0 = success.

Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 50 |
    Where-Object { $_.Message -like '*SiteOpsAksEeBootstrap*' }
```

### Re-apply against an already-bootstrapped host

Re-running the bootstrap against a host that already finished is a safe no-op. The launcher sees `state.json` at `status=succeeded`, leaves the cluster and scheduled task untouched, and returns `ALREADY-BOOTSTRAPPED`. In a composition the wait step then passes immediately on the existing `succeeded` tag. Pass `-Force` to the launcher (or use the clean-restart block below) to re-bootstrap from scratch.

### Re-run a failed phase (keeps existing task and user)

Use when a transient failure hit a single phase (network blip, az CLI download timeout) and you want to retry the same phase without re-running the launcher.

```powershell
# Reset state to re-attempt a specific phase. Each phase is idempotent.
@{ phase = 2; status = 'running'; lastUpdated = (Get-Date).ToString('o'); error = $null } |
    ConvertTo-Json | Set-Content 'C:\ProgramData\siteops\aksee-bootstrap\state.json'
Start-ScheduledTask -TaskName SiteOpsAksEeBootstrap
```

### Full clean restart (Phase 0 or 1 failed, no cluster yet)

Use when something fundamental needs to change (different SP, different cluster name) and you want to re-deploy from scratch. Skips the cluster cleanup because no cluster exists yet at Phase 0 or 1.

```powershell
Stop-ScheduledTask       -TaskName SiteOpsAksEeBootstrap -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName SiteOpsAksEeBootstrap -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force 'C:\ProgramData\siteops\aksee-bootstrap\' -ErrorAction SilentlyContinue
Remove-LocalUser -Name siteops-bootstrap -ErrorAction SilentlyContinue

# Then re-run `siteops deploy ...`
```

### Wipe and redo from scratch (Phase 2 or 3 failed, cluster exists)

```powershell
# Stop and remove the partial cluster (only valid once AKS EE is installed)
Import-Module AksEdge -ErrorAction SilentlyContinue
Stop-AksEdgeDeployment   -Confirm:$false -ErrorAction SilentlyContinue
Remove-AksEdgeDeployment -Confirm:$false -ErrorAction SilentlyContinue

# Then run the Full clean restart block above to wipe task + working dir + user.
```

## Phases reference

| Phase | Action | May reboot? |
|---|---|---|
| 0  | Pre-flight: admin, OS, memory, disk, nested virt | No |
| 1  | MSI install + `Install-AksEdgeHostFeatures` | Yes (Hyper-V enable) |
| 2  | Render the cluster config (AioDeploy cluster-only, no service principal) and create the single-node K3s cluster | No |
| 3  | Install Azure CLI if missing, authenticate with the Arc machine managed identity, Arc-connect the cluster, enable `cluster-connect` and `custom-locations`, and (when `enableWorkloadIdentity` is requested) wire the OIDC issuer through the K3s apiserver | No |
| 99 | Cleanup (unregister scheduled task, purge the task account's kubeconfig and az token cache, remove the rendered config, and remove the bootstrap user if one was created). Write `siteops.bootstrap.state` tag on the Arc machine. | No |

Each phase is idempotent so a worker re-run from any state is safe. Phase 1 writes the next phase to `state.json` BEFORE calling `Install-AksEdgeHostFeatures` so the at-startup scheduled-task trigger resumes at Phase 2 after the reboot.

Phase 3 layers AIO-specific features on top of the basic Arc-connected cluster. The reason for layering instead of doing everything in Phase 2: the inner `aksedge-config.json` schema does not recognize OIDC issuer, workload identity, or custom-locations fields. Phase 3 handles them explicitly through `az connectedk8s` commands.

## Bootstrap state tag

The worker writes a tag on the Arc machine resource that signals terminal bootstrap state:

- `siteops.bootstrap.state=succeeded` on Phase 99 success.
- `siteops.bootstrap.state=failed-phase-N` on any phase failure. N is the failing phase number.

Downstream automation reads this tag to gate on actual bootstrap completion. A siteops `type: wait` step is the intended primary consumer. A CI script polling via `az tag list` works the same way.

The worker writes the tag using the Arc machine managed identity (the same identity it uses for all Phase 3 Azure operations). The required permission is `Microsoft.Resources/tags/write` on the Arc machine resource.

- If you granted the identity `Contributor` in [prereq #2](#2-grant-the-arc-machine-identity-access-to-the-resource-group), no extra grant is needed.
- If you scoped it to the narrow `Kubernetes Cluster - Azure Arc Onboarding` role instead, add a `Tag Contributor` assignment on the Arc machine resource:

```bash
az role assignment create \
  --assignee-object-id $ARC_PRINCIPAL_ID \
  --assignee-principal-type ServicePrincipal \
  --role "Tag Contributor" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.HybridCompute/machines/<vm-name>"
```

A failed tag write does not fail the bootstrap. The cluster is still up and Arc-connected. Verify or set the tag manually:

```bash
az tag list --resource-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.HybridCompute/machines/<vm-name>" \
    --query "properties.tags" -o json
```

## Optional flags

| Flag | Default | Effect |
|---|---|---|
| `enableWorkloadIdentity` | false | When true, Phase 3 enables the OIDC issuer and workload identity on the Arc connection and patches the K3s apiserver `service-account-issuer`. Required when downstream AIO components use workload-identity-backed secret sync. |

Set it per site via `deployOptions.enableWorkloadIdentity: true` (paired with `enableSecretSync`). For direct launcher invocation, pass `-EnableWorkloadIdentity true`.

## Security

- **No secret:** the bootstrap uses no service principal. The worker authenticates with the Arc machine's system-assigned managed identity (short-lived HIMDS tokens), so there is no credential to deliver, encrypt, store, or clean up.
- **ACLs:** `C:\ProgramData\siteops\aksee-bootstrap\` has inherited ACLs removed and re-granted to Administrators + SYSTEM only.
- **Task identity:** by default the worker Scheduled Task runs as `NT AUTHORITY\SYSTEM`, so no local account or password is created. With `runAsDedicatedAdmin` the launcher instead creates a local admin with an on-box generated password, written only to the task registration via `Register-ScheduledTask -User -Password` and never persisted to any file the worker reads.
- **az token cache:** Phase 3 scopes `AZURE_CONFIG_DIR` into the ACL-locked working directory so the az tokens stay behind the Administrators + SYSTEM ACL. Phase 99 removes the cache on success.
- **Cluster kubeconfig:** Phase 2 copies the cluster kubeconfig (a long-lived bearer token) into the ACL-locked working directory and Phase 99 keeps it there for the operator, behind the Administrators + SYSTEM ACL.

## Installer integrity

The worker Authenticode-verifies each installer MSI (AKS Edge Essentials, Azure CLI) before running it. The signature must be `Valid` (signed, untampered, chain-trusted) and the signer organization must be Microsoft, so a poisoned `aka.ms` redirect to a differently-signed binary is rejected. The default revocation check reaches the CRL or OCSP endpoint over the same network used for the download, so a fully air-gapped host may report a non-`Valid` status and need a revocation exception.

## Run directly (advanced)

The scalekit path delivers the launcher via Bicep + Arc Run Command. For debugging or one-off use without scalekit, the full launcher script can run directly on the VM. See `scripts/README.md` for the dev workflow.

## Known limitations

- The worker Scheduled Task runs as `NT AUTHORITY\SYSTEM` by default, so no local account or password is created. For hardened environments that disallow SYSTEM-context tasks, the Bicep `runAsDedicatedAdmin` parameter (launcher `-RunAsDedicatedAdmin`) runs it as a launcher-created local admin instead.
- The Run Command resource returns `executionState=Succeeded` the moment the launcher returns `REGISTERED`, NOT when the worker reaches `phase=99 status=succeeded`. Use the [bootstrap state tag](#bootstrap-state-tag) to gate downstream pipeline steps on actual bootstrap completion.

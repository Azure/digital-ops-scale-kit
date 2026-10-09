# AKS Edge Essentials upgrade (host-ops)

Upgrades an existing AKS EE cluster with a single node in place, delivered
remotely from Azure via an Arc Run Command. It follows the host bootstrap's
pattern of a Run Command launcher and a worker, because AKS EE has no upgrade
channel driven from the cloud: the only way to drive an upgrade remotely is to
invoke the PowerShell cmdlets on the host, which this worker does, wrapped with
idempotency, a snapshot before the upgrade, a mandatory verification gate, and
a completion tag.

For how the launcher and worker pattern works (the launcher, the phase state
machine, managed identity authentication, and the tag gate), see the bootstrap README
in the [bootstrap operator guide](../aksee-bootstrap/README.md).

## Scope

Two upgrade modes are supported, selected by `allowKubernetesMinorUpgrade` under
`deployOptions` in the Site config. The mode toggle is a property, while the
optional `targetKubernetesVersion` that bounds a run in minor mode is a value under
`parameters.aksee`:

**Patch mode** (default, `false`): applies AKS EE patch updates within the
current Kubernetes minor version on a cluster with a single node. `AcceptUpgrade`
stays false throughout.

**Minor mode** (`true`): performs sequential upgrades over several hops,
advancing one Kubernetes minor version per hop (for example k3s 1.31 -> 1.32 ->
1.33). Each hop runs the full stage/apply/verify cycle. `AcceptUpgrade` is set
true for the run. Successful finalization attempts to set it back to false. A
failed run leaves it set so the staged update cache survives for a redeploy to
resume.

Set `site.parameters.aksee.targetKubernetesVersion` (e.g. `"1.33"`) to stop at
a specific minor version. Leave it empty to upgrade to the latest available
version (up to the maximum of six hops).

The worker verifies the cluster after each hop and attempts to report the
outcome through the completion tag:

- The node VM update can intermittently fail to finalize (the node cannot find
  `/EFI/AZLB/bootx64.efi` after it reboots). The worker surfaces this as the tag
  value `failed-needs-remediation`. The fix is a manual step in the VM console (see
  [Trident remediation](#trident-remediation)).
- The worker rechecks node health and the Arc connection after the update and
  fails the deploy if either regressed.

The worker verifies the cluster platform. If the cluster runs AIO, confirm AIO
health after the upgrade.

## How it works

The upgrade is delivered as a `Microsoft.HybridCompute/machines/runCommands`
resource that inlines the minified launcher. The Connected Machine Agent runs
the launcher, which registers a Scheduled Task running as
`NT AUTHORITY\SYSTEM`, then returns `REGISTERED`. The worker runs
asynchronously:

| Phase | What it does | Inner reboot |
|---|---|---|
| 0 | Preflight and snapshot: administrator, AKS EE installed, topology with a single node, install az if missing (after verifying its signature), `az login --identity`, set shared kubeconfig + pin AKS EE kubectl, detect AIO namespace presence, capture the snapshot before the upgrade (deployed Kubernetes version, host AKS EE version, node count, Arc state, and AIO namespace presence), validate target version if set, initialize `progress.json`, set `AcceptUpgrade` for the run | No |
| 1 | Stage one hop: check whether the target minor is already met, then stage the next AKS EE update from Microsoft Update (a Windows Update scan, download, and install that extracts itself into the update cache) and install the cached MSI with `Start-AksEdgeUpdate -Force`. Goes to Phase 2 when staged, Phase 3 to verify and finalize when Microsoft Update offers nothing | No |
| 2 | Apply: `Import-Module AksEdge -Force` then `Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force` | Yes (node VM) |
| 3 | Verify hop + decide: deployed Kubernetes version, `/readyz`, nodes Ready, `Test-AksEdgeArcConnection`. Decide: target reached -> Phase 99, patch mode -> Phase 99, max hops exceeded -> fail, else loop back to Phase 1 | No |
| 99 | Finalize: set `AcceptUpgrade $false` again (best effort), write `siteops.aksee.upgrade.state=succeeded` (with `appliedVersion`, `fromVersion`, `runId`), remove the az token cache | No |

The host does not reboot during the upgrade (only the inner node VM does), so
the worker normally runs straight through. The Scheduled Task trigger at startup
is kept as a safety net for an unrelated host reboot, which the phase state
machine resumes from.

**Idempotent rerun.** Reapplying the manifest resets local state and reruns
the worker. When no newer patch is available, Phase 1 records that nothing
changed, the verify gate checks the current platform state, and Phase 99
attempts to tag `succeeded`. The launcher resets the tag before the run on a
best effort basis, and the shipped
wait checks only the state tag rather than the run identifier.

## Prerequisites

1. **An AKS EE cluster with a single node is already deployed and connected to
   Azure Arc** on the VM (for example by the `host-bootstrap/aksee` bootstrap).
2. **The Arc machine's system-assigned managed identity can write tags on the
   Arc machine resource.** The worker authenticates as this identity only to write
   the completion tag. The verification after the upgrade runs on the host through AKS EE
   cmdlets (`Test-AksEdgeArcConnection`) and `kubectl`, so it needs no Azure
   permission. There is no service principal fallback. The single permission the
   upgrade needs is `Microsoft.Resources/tags/write` on the Arc machine resource,
   granted by `Tag Contributor` scoped to the machine or its resource group.
   `Contributor` also works. A VM bootstrapped by `host-bootstrap/aksee` already
   holds a broader grant (it connected the cluster to Azure Arc), so no extra assignment is
   needed there.

```bash
ARC_ID=$(az resource show -g <rg> -n <vm-name> --resource-type Microsoft.HybridCompute/machines --query id -o tsv)
PRINCIPAL_ID=$(az resource show --ids "$ARC_ID" --query "identity.principalId" -o tsv)
# Least privilege: Tag Contributor scoped to the Arc machine resource.
az role assignment create --assignee-object-id $PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Tag Contributor" --scope "$ARC_ID"
```

3. **The host can reach Microsoft Update, with Microsoft Update enabled for
   other Microsoft products.** Each hop is staged from Microsoft Update. The worker starts the
   Windows Update service and scans for the next AKS EE update, so enable
   "receive updates for other Microsoft products" on the host. Staging
   requires that network path.

## Site configuration

The upgrade reuses the same `aksee` parameter section the bootstrap uses:

```yaml
# sites/<site>.yaml
name: my-site
subscription: <subscription-id>
resourceGroup: <rg-name>
location: <region>
parameters:
  aksee:
    machineName: my-arc-windows-vm
    # Optional. Set for minor-mode upgrades to stop at a specific version.
    targetKubernetesVersion: "1.33"
properties:
  deployOptions:
    # Set true to enable sequential minor-version hops. Default is false (patch only).
    allowKubernetesMinorUpgrade: true
```

## Run

```bash
siteops -w workspaces/iot-operations deploy manifests/aksee-upgrade/manifest.yaml -l name=<site>
```

The deploy blocks on the wait step until the state tag reaches a terminal
value. A green deploy means the worker reported a verified applied upgrade or
a verified run that changed nothing. A failed deploy can carry `failed-phase-N` or
`failed-needs-remediation`. The wait checks only the state, not the current
run identifier. Runs in minor mode with several hops use a 240-minute wait
timeout.

## Monitor

On the VM from an administrator PowerShell session (the working directory's ACL
admits only Administrators and SYSTEM):

```powershell
$dir = 'C:\ProgramData\siteops\aksee-upgrade'
Get-Content (Join-Path $dir 'state.json') | ConvertFrom-Json | Format-List
Get-Content (Join-Path $dir 'snapshot.json') | ConvertFrom-Json | Format-List
$log = Get-ChildItem (Join-Path $dir 'worker-*.log') | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 40 -Wait
```

The AKS EE cmdlet output is captured per call in `aksee-install-msi-*.log`,
`aksee-apply-update-*.log`, `aksee-set-accept-upgrade-*.log`, and their `.err`
siblings. The Windows Update staging runs inline and is logged in the
`worker-*.log`.

## Verify

The worker already verifies, but to confirm by hand after the deploy:

```powershell
$env:KUBECONFIG = 'C:\ProgramData\siteops\aksee-bootstrap\kubeconfig'
kubectl get nodes -o wide          # node at the new Kubernetes version, Ready
Test-AksEdgeArcConnection
```

Read the applied/from versions from the Arc machine tags:

```bash
az tag list --resource-id "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.HybridCompute/machines/<vm-name>" --query "properties.tags" -o json
```

## Trident remediation

If the deploy fails with the tag value `failed-needs-remediation`, the node VM
hit the known Trident/EFI finalize failure and needs a manual step. Console into
the Linux node VM and rerun `trident`:

```powershell
# On the host, find the node VM id and console in
hcsdiag list
hcsdiag console <node-vm-id>
# Inside the VM, re-run the finalize
sudo trident
```

After the node recovers, rerun the manifest. The worker rechecks the version
and health and tags `succeeded` once the cluster is healthy.

## Rerun a failed phase

The launcher resets state on a normal redeploy, so rerunning the manifest is
the supported retry. To drive the worker again on the VM without redeploying, set
`state.json` back to the phase to resume from and start the task:

```powershell
$dir = 'C:\ProgramData\siteops\aksee-upgrade'
'{ "phase": 0, "status": "running", "error": null }' | Set-Content (Join-Path $dir 'state.json')
Start-ScheduledTask -TaskName SiteOpsAksEeUpgrade
```

## Known limitations

- **Clusters with a single node only.** The worker fails preflight on a cluster with several nodes.
- **Workloads are down during each apply.** The A/B update in place stops the
  node VM, updates its OS partition, and restarts it. AIO does not support live
  upgrades and expects downtime.
- **The worker checks the cluster platform.** It checks that nodes are Ready
  and the Arc connection holds. Confirm AIO health after the upgrade.
- **The runCommand returns early.** `executionState=Succeeded` means the
  launcher registered the task, not that the upgrade finished. Gate on the
  `wait` step, never on the runCommand result.
- **State tags are written on a best effort basis.** The reset before the run and terminal writes can
  warn without failing local worker completion. The shipped wait checks state,
  not `runId`, so inspect the host state and worker log if the wait result does
  not match the current operation.
- **Minor upgrades are sequential.** AKS EE cannot skip a minor version. An
  upgrade over three hops (1.31 -> 1.32 -> 1.33) takes three full
  stage/apply/verify cycles, each with a reboot of the inner node VM. Plan for extended downtime.

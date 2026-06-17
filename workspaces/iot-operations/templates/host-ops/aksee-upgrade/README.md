# AKS Edge Essentials patch update (host-ops)

In-place patch update of an existing single-node AKS Edge Essentials cluster,
delivered remotely from Azure via an Arc Run Command. It mirrors the host
bootstrap's runCommand-worker pattern because AKS EE has no cloud-driven upgrade
channel: the only way to drive an upgrade remotely is to invoke the on-box
PowerShell cmdlets, which this worker does, wrapped with idempotency, a
pre-upgrade snapshot, a mandatory verification gate, and a completion tag.

For how the runCommand-worker pattern works (the launcher, the phase state
machine, the managed-identity auth, and the tag gate), see the bootstrap README
at [`../../host-bootstrap/aksee/README.md`](../../host-bootstrap/aksee/README.md).

## Scope

Two upgrade modes are supported, selected by `allowKubernetesMinorUpgrade` in the site config:

**Patch mode** (default, `false`): applies AKS EE patch updates within the
current Kubernetes minor version on a single-node cluster. `AcceptUpgrade`
stays false throughout.

**Minor mode** (`true`): performs sequential multi-hop upgrades, advancing one
Kubernetes minor version per hop (e.g. k3s 1.31 -> 1.32 -> 1.33). Each hop
runs the full stage/apply/verify cycle. `AcceptUpgrade` is set true only for
the duration of the run and is re-pinned false on completion or failure.

Set `site.parameters.aksee.targetKubernetesVersion` (e.g. `"1.33"`) to stop at
a specific minor version. Leave it empty to upgrade to the latest available
version (up to the 10-hop maximum).

The worker verifies the cluster after each hop and reports the outcome through
the completion tag:

- The node-VM update can intermittently fail to finalize (the node cannot find
  `/EFI/AZLB/bootx64.efi` after it reboots). The worker surfaces this as the tag
  value `failed-needs-remediation`. The fix is a manual VM-console step (see
  [Trident remediation](#trident-remediation)).
- The worker re-checks node health and the Arc connection after the update and
  fails the deploy if either regressed.

AIO health is not verified by the worker. Confirm AIO after the upgrade if the
cluster runs it.

## How it works

The upgrade is delivered as a `Microsoft.HybridCompute/machines/runCommands`
resource that inlines the minified launcher. The Connected Machine Agent runs
the launcher, which registers a Scheduled Task (running as `NT AUTHORITY\SYSTEM`
by default) that drives the worker, then returns `REGISTERED` (~90 seconds). The
worker runs asynchronously:

| Phase | What it does | Inner reboot |
|---|---|---|
| 0 | Preflight + snapshot: admin, AKS EE installed, single-node topology, install az if missing (signature-verified), `az login --identity`, set shared kubeconfig + pin AKS EE kubectl, detect AIO, capture the pre-upgrade snapshot (deployed Kubernetes version, host AKS EE version, node count, Arc + AIO state), validate target version if set, initialize `progress.json`, set `AcceptUpgrade` for the run | No |
| 1 | Stage one hop: check whether the target minor is already met, then stage `Start-AksEdgeUpdate -Force` via the `Invoke-ChildStage` classifier. Goes to Phase 2 when staged, Phase 99 when nothing is available | No |
| 2 | Apply: `Import-Module AksEdge -Force` then `Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force` | Yes (node VM) |
| 3 | Verify hop + decide: deployed Kubernetes version, `/readyz`, nodes Ready, `Test-AksEdgeArcConnection`. Decide: target reached -> Phase 99, patch mode -> Phase 99, max hops exceeded -> fail, else loop back to Phase 1 | No |
| 99 | Finalize: re-pin `AcceptUpgrade $false` (best-effort), write `siteops.aksee.upgrade.state=succeeded` (with `appliedVersion`, `fromVersion`, `hopCount`, `runId`), remove the az token cache | No |

The host does not reboot during the upgrade (only the inner node VM does), so
the worker normally runs straight through. The at-startup Scheduled Task trigger
is kept as a safety net for an unrelated host reboot, which the phase state
machine resumes from.

**Idempotent re-run.** Re-applying the manifest resets state and re-runs the
worker. When no newer patch is available, Phase 1 records a no-op, the verify
gate confirms the cluster is healthy at its current version, and Phase 99 tags
`succeeded`. The launcher sets the tag to `running` synchronously before the
wait step polls, so a stale `succeeded` from a previous run cannot pass the gate
early.

## Prerequisites

1. **An AKS EE single-node cluster is already deployed and Arc-connected** on
   the target VM (for example by the `host-bootstrap/aksee` bootstrap).
2. **The Arc machine's system-assigned managed identity has a role on the
   resource group.** The worker authenticates as this identity for the
   post-upgrade verification and the completion tag. There is no service
   principal fallback. Grant `Contributor` (simplest), or `Kubernetes Cluster -
   Azure Arc Onboarding` plus `Tag Contributor` (for the
   `Microsoft.Resources/tags/write` the tag needs) for least privilege.

```bash
ARC_PRINCIPAL_ID=$(az resource show -g <rg> -n <vm-name> --resource-type Microsoft.HybridCompute/machines --query "identity.principalId" -o tsv)
az role assignment create --assignee-object-id $ARC_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Contributor" --scope "/subscriptions/<sub>/resourceGroups/<rg>"
```

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
    allowAkseeMinorUpgrade: true
```

## Run

```bash
siteops -w workspaces/iot-operations deploy manifests/aksee-upgrade.yaml -l name=<site>
```

The deploy blocks on the wait step until the worker reaches its terminal state,
so a green deploy means the patch update applied and verified, and a failed
deploy carries the tag value that failed (`failed-phase-N` or
`failed-needs-remediation`).

## Monitor

On the VM from an admin PowerShell (the working dir is ACL-locked to
Administrators + SYSTEM):

```powershell
$dir = 'C:\ProgramData\siteops\aksee-upgrade'
Get-Content (Join-Path $dir 'state.json') | ConvertFrom-Json | Format-List
Get-Content (Join-Path $dir 'snapshot.json') | ConvertFrom-Json | Format-List
$log = Get-ChildItem (Join-Path $dir 'worker-*.log') | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 40 -Wait
```

The staged/applied AKS EE cmdlet output is captured per call in
`aksee-stage-update-*.log`, `aksee-apply-update-*.log`, and their `.err`
siblings.

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
the Linux node VM and re-run `trident`:

```powershell
# On the host, find the node VM id and console in
hcsdiag list
hcsdiag console <node-vm-id>
# Inside the VM, re-run the finalize
sudo trident
```

After the node recovers, re-run the manifest. The worker re-checks the version
and health and tags `succeeded` once the cluster is healthy.

## Re-run a failed phase

The launcher resets state on a normal re-deploy, so re-running the manifest is
the supported retry. To re-drive the worker on the VM without re-deploying, set
`state.json` back to the phase to resume from and start the task:

```powershell
$dir = 'C:\ProgramData\siteops\aksee-upgrade'
'{ "phase": 0, "status": "running", "error": null }' | Set-Content (Join-Path $dir 'state.json')
Start-ScheduledTask -TaskName SiteOpsAksEeUpgrade
```

## Known limitations

- **Single-node only.** The worker fails preflight on a multi-node cluster.
- **Workloads are down during each apply.** The in-place A/B update stops the
  node VM, updates its OS partition, and restarts it. AIO does not support live
  upgrades and expects downtime.
- **AIO health is not verified.** The worker checks the cluster platform (nodes
  Ready, Arc connected) but not AIO. Confirm AIO after the upgrade.
- **The runCommand returns early.** `executionState=Succeeded` means the
  launcher registered the task, not that the upgrade finished. Gate on the
  `wait` step, never on the runCommand result.
- **Minor upgrades are sequential.** AKS EE cannot skip a minor version. A
  three-hop upgrade (1.31 -> 1.32 -> 1.33) takes three full stage/apply/verify
  cycles, each with an inner node-VM reboot. Plan for extended downtime.

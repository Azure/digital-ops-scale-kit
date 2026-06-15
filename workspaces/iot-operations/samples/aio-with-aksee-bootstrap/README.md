# aio-with-aksee-bootstrap

Composes the AKS Edge Essentials host bootstrap with the AIO fundamentals install. Demonstrates the composed shape for bare-Arc-onboarded-Windows-VM to AIO in a single deploy.

## Single deploy

The bootstrap step returns shortly after the Connected Machine Agent picks up the launcher (~90 seconds). The cluster then comes up on the VM asynchronously for 25 to 40 minutes. AIO fundamentals' first cluster-dependent step (`aio-enablement`) deploys an Arc extension that requires the `connectedClusters` resource to exist, which is absent until the bootstrap finishes.

A `type: wait` step sits between the bootstrap and AIO fundamentals. It polls the `siteops.bootstrap.state` tag the worker writes on the Arc machine resource and releases the downstream steps only once the tag reads `succeeded`. The whole chain runs from one command:

```bash
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev
```

The deploy blocks at `wait-for-bootstrap` for the 25 to 40 minutes the cluster takes to come up (timeout 60 minutes, poll every 30 seconds), then AIO fundamentals proceeds against the ready cluster. If the bootstrap fails, the worker writes `failed-phase-N` and the wait step fails fast instead of blocking for the full timeout. See [`../../templates/host-bootstrap/aksee/README.md`](../../templates/host-bootstrap/aksee/README.md) for the VM-side monitor commands to watch progress during the wait.

## What this sample does

1. **aksee-bootstrap**: delivers and runs the bootstrap launcher on the target Windows VM via Arc Run Command. The launcher registers a Scheduled Task that drives a state machine through preflight, MSI install + Hyper-V enable (may reboot), single-node K3s cluster create, Arc-connect with custom locations, and cleanup. Survives the Hyper-V reboot via the at-startup task trigger and `state.json`. Phase 99 writes `siteops.bootstrap.state=succeeded` on the Arc machine resource.
2. **wait-for-bootstrap**: polls that tag until it reads `succeeded`, gating the cluster-dependent steps below on the cluster actually being ready.
3. **aio-fundamentals**: Arc extensions, custom location, AIO instance, schema registry, ADR namespace. Runs on the cluster the bootstrap produced.

After the deploy completes, the cluster is registered with Arc, has custom locations enabled, and is running AIO. Add secret sync, OPC UA, or other workload samples on top via additional `include:` directives or by composing into a larger manifest.

## Prerequisites

The bootstrap prerequisites apply (Arc-onboarded VM, the Arc machine managed identity granted access on the resource group, resource providers registered). See [`../../templates/host-bootstrap/aksee/README.md`](../../templates/host-bootstrap/aksee/README.md) for the one-time setup walkthrough, including the tag-write permission the wait step depends on.

The site must carry both the `aksee` parameter section the bootstrap needs and any per-release AIO parameters the fundamentals expect (`properties.aioRelease` pointing at a file under `parameters/aio-releases/`).

The wait step targets the Arc machine resource named by `site.parameters.aksee.machineName`. The worker tags the machine whose name is the VM's computer name, so `machineName` must match the Arc machine resource name (the `azcmagent connect` default uses the computer name).

## Variants

- **Bootstrap only:** `siteops deploy manifests/aksee-bootstrap.yaml` stops at "cluster Arc-connected + AIO-ready", without installing AIO.
- **Bootstrap + AIO + sample workload:** add another `include:` to a sample partial (e.g., `../opc-ua-solution/_partial.yaml`) to land an OPC UA solution on top.
- **Bootstrap + AIO + secret sync:** add `../../manifests/_resolve-aio.yaml` and `../../manifests/_secretsync.yaml` after the fundamentals step.

# aio-with-aksee-bootstrap

Composes the AKS Edge Essentials host bootstrap with the AIO fundamentals install. Demonstrates the composed shape that takes a Windows VM onboarded to Azure Arc to AIO in a single deploy.

## Single deploy

The bootstrap launcher registers a Scheduled Task, and the cluster then comes
up asynchronously on the VM. The first AIO fundamentals step that depends on the cluster
(`aio-enablement`) deploys an Arc extension that requires the
`connectedClusters` resource to exist, which is absent until the bootstrap
finishes.

A `type: wait` step sits between the bootstrap and AIO fundamentals. It polls
the `siteops.bootstrap.state` tag on the Arc machine resource. The wait checks
state only and does not compare the bootstrap run ID. Review the plan, then one
deploy command runs the whole chain:

```bash
siteops -w workspaces/iot-operations plan samples/aio-with-aksee-bootstrap/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l name=<site>
```

Replace `<site>` with the configured Site. The deploy blocks at
`wait-for-bootstrap` until the state tag reports success or failure. The
timeout is 60 minutes and the poll interval is 30 seconds. See the
[bootstrap monitor commands](../../manifests/aksee-bootstrap/README.md#monitor)
for progress on the VM.

## What this sample does

1. **aksee-bootstrap**: delivers and runs the bootstrap launcher on the Windows VM via Arc Run Command. The launcher registers a Scheduled Task that drives a state machine through preflight, MSI install + Hyper-V enable (may reboot), K3s cluster creation on a single node, connection to Azure Arc with custom locations, and cleanup. Survives the Hyper-V reboot through the task trigger at startup and `state.json`. Phase 99 writes `siteops.bootstrap.state=succeeded` on the Arc machine resource.
2. **wait-for-bootstrap**: polls that tag until it reads `succeeded`, gating the steps below that depend on the cluster on the worker's platform checks.
3. **the AIO platform steps**, composed from `_aio-fundamentals.yaml`: `global-edge-site`, `edge-site`, `schema-registry`, `adr-ns`, `aio-enablement`, `aio-instance`, and `schema-registry-role`. Arc extensions, custom location, AIO instance, schema registry, and ADR namespace, on the cluster the bootstrap produced.

After the deploy completes, the cluster is registered with Arc, custom
locations are enabled, and the AIO deployment steps have completed. Verify AIO
health separately. Add Secret Sync, OPC UA, or other workload samples through
additional `include:` directives or a larger composition.

## Prerequisites

The bootstrap prerequisites apply (VM onboarded to Azure Arc, the Arc machine managed identity granted access on the resource group, resource providers registered). See the [bootstrap prerequisites](../../manifests/aksee-bootstrap/README.md#prerequisites-for-each-vm-once) for the setup you do once, and the [bootstrap state tag](../../manifests/aksee-bootstrap/README.md#bootstrap-state-tag) for the permission to write tags that the wait step depends on.

The Site must carry both the `aksee` parameter section the bootstrap needs and any AIO parameters for its AIO release the fundamentals expect (`properties.aioRelease` pointing at a file under `parameters/aio-releases/`).

The wait step and worker both target the Arc machine resource named by
`site.parameters.aksee.machineName`.

## Variants

- **Bootstrap only:** `siteops -w workspaces/iot-operations deploy manifests/aksee-bootstrap/manifest.yaml -l name=<site>` stops after the cluster is connected to Azure Arc and prepared for an AIO deployment. It does not install AIO.
- **Bootstrap + AIO + sample workload:** add another `include:` to a sample partial (e.g., `../opc-ua-solution/_partial.yaml`) to land an OPC UA solution on top.
- **Bootstrap + AIO + Secret Sync:** add `../../manifests/_partials/_resolve-aio.yaml` and `../../manifests/_partials/_secretsync.yaml` after the fundamentals step.

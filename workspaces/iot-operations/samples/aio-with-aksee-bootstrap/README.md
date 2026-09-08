# aio-with-aksee-bootstrap

Composes the AKS Edge Essentials host bootstrap with the AIO fundamentals install. Demonstrates the composed shape for bare-Arc-onboarded-Windows-VM to AIO in a single deploy.

## Single deploy

The bootstrap launcher registers a Scheduled Task, and the cluster then comes
up asynchronously on the VM. AIO fundamentals' first cluster-dependent step
(`aio-enablement`) deploys an Arc extension that requires the
`connectedClusters` resource to exist, which is absent until the bootstrap
finishes.

A `type: wait` step sits between the bootstrap and AIO fundamentals. It polls
the `siteops.bootstrap.state` tag on the Arc machine resource. The wait checks
state only and does not compare the bootstrap run ID. The whole chain runs from
one command:

```bash
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev
```

The deploy blocks at `wait-for-bootstrap` until the state tag reports success
or failure. The timeout is 60 minutes and the poll interval is 30 seconds. See
[`../../templates/host-bootstrap/aksee/README.md`](../../templates/host-bootstrap/aksee/README.md)
for VM-side monitor commands.

## What this sample does

1. **aksee-bootstrap**: delivers and runs the bootstrap launcher on the target Windows VM via Arc Run Command. The launcher registers a Scheduled Task that drives a state machine through preflight, MSI install + Hyper-V enable (may reboot), single-node K3s cluster create, Arc-connect with custom locations, and cleanup. Survives the Hyper-V reboot via the at-startup task trigger and `state.json`. Phase 99 writes `siteops.bootstrap.state=succeeded` on the Arc machine resource.
2. **wait-for-bootstrap**: polls that tag until it reads `succeeded`, gating the cluster-dependent steps below on the worker's platform checks.
3. **the AIO platform steps**, composed from `_aio-fundamentals.yaml`: `global-edge-site`, `edge-site`, `schema-registry`, `adr-ns`, `aio-enablement`, `aio-instance`, and `schema-registry-role`. Arc extensions, custom location, AIO instance, schema registry, and ADR namespace, on the cluster the bootstrap produced.

After the deploy completes, the cluster is registered with Arc, custom
locations are enabled, and the AIO deployment steps have completed. Verify AIO
health separately. Add secret sync, OPC UA, or other workload samples through
additional `include:` directives or a larger composition.

## Prerequisites

The bootstrap prerequisites apply (Arc-onboarded VM, the Arc machine managed identity granted access on the resource group, resource providers registered). See [`../../templates/host-bootstrap/aksee/README.md`](../../templates/host-bootstrap/aksee/README.md) for the one-time setup walkthrough, including the tag-write permission the wait step depends on.

The site must carry both the `aksee` parameter section the bootstrap needs and any per-release AIO parameters the fundamentals expect (`properties.aioRelease` pointing at a file under `parameters/aio-releases/`).

The wait step and worker both target the Arc machine resource named by
`site.parameters.aksee.machineName`.

## Variants

- **Bootstrap only:** `siteops deploy manifests/aksee-bootstrap.yaml` stops after the cluster is Arc-connected and prepared for an AIO deployment. It does not install AIO.
- **Bootstrap + AIO + sample workload:** add another `include:` to a sample partial (e.g., `../opc-ua-solution/_partial.yaml`) to land an OPC UA solution on top.
- **Bootstrap + AIO + secret sync:** add `../../manifests/_resolve-aio.yaml` and `../../manifests/_secretsync.yaml` after the fundamentals step.

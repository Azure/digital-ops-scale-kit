# aio-with-opc-ua

Composed example that installs the AIO platform and the OPC UA sample in
one deploy. Useful as a starting point with one command on a fresh cluster.

The manifest composes existing partials, so there is no template or input
file under this directory. See `manifest.yaml` for the flattened step
sequence and `samples/README.md` for the rules every composition follows.

## Prerequisites

- An existing cluster connected to Azure Arc and a configured Site, as described in the
  [install guide](../../manifests/aio-install/README.md#configure-the-site).
- The permissions to create role assignments and the Kubernetes RBAC listed in the
  [OPC UA sample prerequisites](../opc-ua-solution/README.md#prerequisites).
  This composition installs AIO itself, so the existing installation
  requirement there does not apply.

## Deploy

```bash
siteops -w workspaces/iot-operations plan samples/aio-with-opc-ua/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy samples/aio-with-opc-ua/manifest.yaml -l name=<site>
```

Replace `<site>` with the configured Site. The explicit selector replaces the
manifest's `environment=dev` default.

## Verifying the result

The sample's dataflow projects to the cluster as a CR:

```bash
kubectl get dataflows.connectivity.iotoperations.azure.com -n azure-iot-operations
```

Telemetry lags the deploy. The OPC UA connector reconciles the asset,
establishes its session, and warms up polling before the first message
reaches the broker, after which the dataflow forwards it to Event Hub.

AIO release `2608`, which Sites inherit by default, deploys the template and
creates the connector pod on demand. AIO releases before 2607 deploy
the connector statically. AIO release `2607` has the documented limitation
of a missing connector template. See
[samples/opc-ua-solution/README.md](../opc-ua-solution/README.md#releases-this-data-path-reaches).

To add a declaratively authored dataflow over the same telemetry, deploy
`samples/dataflow-sample/manifest.yaml` afterwards. See
[docs/resource-catalog.md](../../../../docs/resource-catalog.md).

See `../README.md` (samples authoring guide) for the composition pattern and the conventions every sample follows.

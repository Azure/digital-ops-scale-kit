# aio-with-opc-ua

Composed example that installs the AIO platform and the OPC UA sample in
one deploy. Useful as a single-command starting point on a fresh cluster.

The manifest composes existing partials, so there is no template or input
file under this directory. See `manifest.yaml` for the post-flatten step
sequence and `samples/README.md` for the rules every composition follows.

## Deploy

```bash
siteops -w workspaces/iot-operations deploy samples/aio-with-opc-ua/manifest.yaml -l environment=dev
```

## Verifying the result

The sample's dataflow projects to the cluster as a CR:

```bash
kubectl get dataflows.connectivity.iotoperations.azure.com -n azure-iot-operations
```

Telemetry lags the deploy. The OPC UA connector reconciles the asset,
establishes its session, and warms up polling before the first message
reaches the broker, after which the dataflow forwards it to Event Hub.

From AIO API version `2026-07-01`, which includes the default `2607`, AIO runs
the connector as a pod it creates on demand from a `ConnectorTemplate`. A
future release adds that resource. See
[samples/opc-ua-solution/README.md](../opc-ua-solution/README.md#releases-this-data-path-reaches).

To add a declaratively authored dataflow over the same telemetry, deploy
`samples/dataflow-sample/manifest.yaml` afterwards. See
[docs/resource-catalog.md](../../../../docs/resource-catalog.md).

See `../README.md` (samples authoring guide) for the composition pattern and the conventions every sample follows.

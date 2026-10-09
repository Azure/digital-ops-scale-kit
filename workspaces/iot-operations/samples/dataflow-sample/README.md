# dataflow-sample

Reference sample that declares dataflows in YAML and deploys them onto an existing AIO instance. It carries no Bicep of its own. `dataflows.yaml` is the whole declaration, and the shared catalog templates under `templates/aio/dataflows/` deploy it.

Compare this directory with `../opc-ua-solution/`, which expresses its dataflow as ARM resources in `template.bicep`. Both approaches are supported, and both deploy the same kind of resource.

## What this sample deploys

1. **resolve-aio**: reads the custom location name from the existing AIO instance.
2. **dataflow-resources**: everything `dataflows.yaml` declares, in one deployment.
   - two MQTT endpoints (`dataflow-sample-mqtt-out`, `dataflow-sample-alerts-out`) pointing at the local AIO broker
   - two execution pools (`dataflow-sample-profile`, `dataflow-sample-alerts-pool`) so neither path shares the default pool
   - two dataflows. `dataflow-sample-passthrough` subscribes to every asset topic under `azure-iot-operations/data/`, passes each message through, and republishes it. `dataflow-sample-alerts` reads the narrower `azure-iot-operations/data/alerts/` prefix through its own pool and its own endpoint.

Each key is a list, so all six resources come from one file and deploy in a single round trip. Adding a seventh is adding a list item, not adding a step. Nothing in this sample is a template.

### Values for each Site

The declaration reads Site values, so one committed file gives every Site its own topics and its own broker client id:

- destination topics carry `{{ site.labels.country }}`, `{{ site.labels.city }}`, and `{{ site.name }}`, so a central subscriber can tell Sites apart
- each endpoint sets `clientIdPrefix` from `{{ site.name }}`, which resolves two levels below `properties`

A Site value resolves at any depth in a declaration. Use one that every selected Site carries. A Site that leaves a `{{ ... }}` unresolved fails the step before it deploys. `tests/workspace/test_catalog_gating.py` checks every committed definition against every committed Site earlier in CI.

## Prerequisites

- AIO must be installed on the cluster. Run `aio-install` first.
- The Site's `aioRelease` must point to an AIO release config under `parameters/aio-releases/`.

The deployment creates no supporting cloud service outside the existing AIO
instance and uses the normal Site Ops deployment identity. Exercising the data
path separately requires an authenticated MQTT client in the cluster.

## Seeing data move

A successful deployment provisions the dataflow resources but does not
establish dataflow health or data movement. A stock AIO install has no assets,
so use one of these routes to give the dataflows something to carry:

- **Deploy `samples/opc-ua-solution/manifest.yaml` first.** It brings up a simulated OPC UA server, a device, and an oven asset publishing under `azure-iot-operations/data/`, which this dataflow's source subscribes to. Both samples deploy against the same existing AIO install, so running them in sequence is all it takes. On AIO release `2607` the MQTT client below is the quickest route, for the reason [that sample's README](../opc-ua-solution/README.md#releases-this-data-path-reaches) gives.
- **Publish a message yourself** with an MQTT client in the cluster (see Microsoft's [`mqtt-client.yaml` reference](https://learn.microsoft.com/azure/iot-operations/manage-mqtt-broker/howto-test-connection)), targeting a topic under `azure-iot-operations/data/`, and subscribe to `dataflow-sample/<country>/<site>/output` to watch it arrive.

`dataflow-sample-alerts` reads the narrower `azure-iot-operations/data/alerts/#`, which neither route publishes to, so it stays idle until something publishes under that prefix. Publish there to exercise it, and subscribe to `dataflow-sample/alerts/<city>/<site>`.

## Configure before deploying

Edit `dataflows.yaml` for this sample, which you deploy once. Composed resource
arrays are owned by definition sources at manifest level, so `site.parameters` and step
parameter files cannot replace them. The endpoint host and trust bundle name in
`dataflows.yaml` assume an install in the default `azure-iot-operations`
namespace. Adjust both when the install uses a different namespace.

## Deploy

```bash
siteops -w workspaces/iot-operations plan samples/dataflow-sample/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy samples/dataflow-sample/manifest.yaml -l name=<site>
```

Replace `<site>` with a configured Site that carries the `country` and `city`
labels the destination topics read. The explicit selector replaces the
manifest's `environment=dev` default.

The dataflow carries whatever assets publish. For real telemetry through it, deploy `samples/opc-ua-solution/manifest.yaml` first to bring up a simulated asset, then deploy this sample.

## Sharing a declaration across Sites

This sample keeps its declaration next to itself. A fleet usually wants the opposite: one declaration shared by every Site of a class. Move the file to `resource-sets/dataflows/<set>.yaml`, point Sites at it, and deploy `manifests/aio-resources/manifest.yaml`:

```yaml
# sites/<site>.yaml
properties:
  resourceSets:
    dataflows:
      - my-set
```

The declaration is unchanged by the move. Only its location and the manifest that attaches it differ. See `docs/resource-catalog.md`.

## Verifying the result

Check the dataflow reached the cluster:

```bash
kubectl get dataflows.connectivity.iotoperations.azure.com -n azure-iot-operations
kubectl get dataflowendpoints.connectivity.iotoperations.azure.com -n azure-iot-operations
```

## Removing the sample

The Bicep deploy is incremental, so removing a declaration from `dataflows.yaml` and redeploying leaves the resource in place. Delete it explicitly:

```bash
az resource delete --ids <dataflowResourceId> <endpointResourceId> <profileResourceId>
```

## Writing your own sample

See `../README.md` for sample conventions and how to add a new sample to this workspace.

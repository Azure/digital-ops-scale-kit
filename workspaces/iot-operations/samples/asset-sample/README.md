# asset-sample

Reference sample that declares Azure Device Registry devices and assets in YAML and deploys them onto an existing AIO instance. It carries no Bicep of its own. The shared templates deploy `parameters/devices/site-devices.yaml` and `parameters/assets/site-assets.yaml` as one internally ordered Device Registry step.

Compare this directory with `../opc-ua-solution/`, which expresses its device and asset as ARM resources in `template.bicep`. Both approaches are supported, and both deploy the same kind of resource.

## What this sample deploys

1. **resolve-aio**: reads the custom location name and the ADR namespace name from the existing AIO instance.
2. **asset-resources**: the selected device and asset definitions, in one deployment.
   - one device, `site-opc-ua`, enabled, carrying a single inbound OPC UA endpoint named `opc-ua-connector-0` at `opc.tcp://opcplc-000000:50000`
   - one asset, `site-oven`, enabled, bound to that device and endpoint through `properties.deviceRef`, publishing an `oven-telemetry` dataset of two data points to MQTT

Both keys are lists, so a second device or a tenth asset is a list entry rather than a step. The device is created before the asset, so the endpoint the asset names exists when the asset lands.

### Per-site values

The declaration reads site values, so one committed file gives every site its own topic and its own operator-visible labels:

- the dataset destination topic is `azure-iot-operations/data/{{ site.name }}/oven`, so a central subscriber can tell sites apart
- the asset's `displayName` reads `{{ site.name }}`, and its `attributes.site` carries the same value, so the portal shows which site an asset belongs to

A site value resolves at any depth in a declaration. Use one every target site carries. A site that leaves a `{{ ... }}` unresolved fails the step before it deploys. `tests/workspace/test_catalog_gating.py` checks every committed definition against every committed site earlier in CI.

## Prerequisites

- AIO must be installed on the target cluster, with an ADR namespace bound to the instance. Run `aio-install` first.
- The site's `aioRelease` must point to a release config under `parameters/aio-releases/`, which is where `adrApiVersion` comes from.

No cloud infrastructure and no credentials beyond the deployment identity.

## Seeing data move

The device and the asset deploy on their own. Telemetry needs an OPC UA server answering at the address the device declares, and the address points at the OPC PLC simulator service, `opcplc-000000`, in the `azure-iot-operations` namespace.

- **Deploy `samples/opc-ua-solution/manifest.yaml` first** to bring the simulator up. Both samples deploy against the same existing AIO install, so running them in sequence is all it takes. That sample also creates its own device and asset under different names, so the two coexist.
- **Or point the address at a server the site already runs.** Copy the device set, change `address` under its inbound endpoint, and select the new set for that site.

On releases where the OPC UA supervisor serves an asset only after adopting a `ConnectorTemplate`, see [the OPC UA sample's README](../opc-ua-solution/README.md#releases-this-data-path-reaches) for which releases carry the connector.

Watch the data arrive by subscribing to `azure-iot-operations/data/<site>/oven` on the broker with an in-cluster MQTT client (see Microsoft's [`mqtt-client.yaml` reference](https://learn.microsoft.com/azure/iot-operations/manage-mqtt-broker/howto-test-connection)).

## Configure before deploying

Edit the worked sets, or point this manifest's `parameters:` entries at another
device or asset set. Composed resource arrays are owned by manifest-level
definition sources, so `site.parameters` and step parameter files cannot
replace them.

## Deploy

```bash
siteops -w workspaces/iot-operations deploy samples/asset-sample/manifest.yaml -l environment=dev
```

## Selecting the same set per site instead

This sample attaches both definition sources on its own manifest, which suits a one-off or a demo. A fleet selects the same sets per site and deploys the catalog entry point:

```yaml
# sites/<site>.yaml
properties:
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
```

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-resources.yaml -l environment=dev
```

The definitions are identical on both routes. Only the manifest that selects
or attaches them differs. See `docs/resource-catalog.md`.

## Verifying the result

Check the device and the asset reached the cluster:

```bash
kubectl get devices.namespaces.deviceregistry.microsoft.com -n azure-iot-operations
kubectl get assets.namespaces.deviceregistry.microsoft.com -n azure-iot-operations
```

A device reports `spec.enabled: true` and lists its inbound endpoints. An asset reports the `spec.deviceRef` it was created with.

## Removing the sample

The Bicep deploy is incremental, so removing an entry from the declaration and redeploying leaves the resource in place. Delete it explicitly, assets before devices, since an asset refers to a device:

```bash
az resource delete --ids <assetResourceId>
az resource delete --ids <deviceResourceId>
```

See `docs/resource-catalog.md` for why a deploy writes only what the declaration names.

## Writing your own sample

See `../README.md` for sample conventions and how to add a new sample to this workspace.

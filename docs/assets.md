# Assets

An asset is a piece of equipment the edge reads data from: an oven, a pump, a press. A device is what the connector talks to on its behalf, carrying the endpoints an asset's data comes through. Both are Azure Device Registry resources, created under the ADR namespace the AIO instance is bound to. Sites select device and asset sets independently, while one deployment step creates devices before assets.

For what a declaration is, how to attach one, when to write Bicep instead, and the API-version policy, see [resource-catalog.md](resource-catalog.md). Those rules apply to every resource area.

## Device and asset definitions

A resource set carries the array for its resource area:

| Key | Creates | Template |
|---|---|---|
| `devices` | Endpoints the connector reads through, one entry per device | `templates/aio/assets/modules/assets-<api-version>.bicep` |
| `assets` | The equipment itself, each bound to one endpoint on one device | same module |

`templates/aio/assets/main.bicep` is what a manifest step points at. It routes to the module for the ADR API version the site's release ships, so a site's devices and assets are written at the same API version as the namespace holding them.

Each entry is `{ name, properties }`. The template adds the location, the tags, the `extendedLocation`, and the namespace parent, so a declaration carries only what is specific to the resource.

A site composes the device and asset sets:

```yaml
properties:
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
```

The device set:

```yaml
devices:
  - name: line-3-opc-ua
    properties:
      enabled: true
      endpoints:
        inbound:
          opc-ua-connector-0:
            endpointType: Microsoft.OpcUa
            address: "opc.tcp://opcplc-000000:50000"
            authentication:
              method: Anonymous
```

The asset set:

```yaml
assets:
  - name: line-3-oven
    properties:
      enabled: true
      displayName: Line 3 oven
      deviceRef:
        deviceName: line-3-opc-ua
        endpointName: opc-ua-connector-0
      datasets:
        - name: oven-telemetry
          dataPoints:
            - name: Temperature
              dataSource: "ns=3;s=SpikeData"
              dataPointConfiguration: '{"samplingInterval":500,"queueSize":1}'
          destinations:
            - target: Mqtt
              configuration:
                topic: azure-iot-operations/data/line-3-oven
                retain: Never
                qos: Qos1
```

The contents of each `properties` block are the resource provider's own schema. See the [Device Registry REST API reference](https://learn.microsoft.com/rest/api/deviceregistry/) for the full property set of each type.

## How an asset finds its device

`properties.deviceRef` is the binding, and it has two halves:

- `deviceName` names a device in the effective composition.
- `endpointName` names a key under that device's `properties.endpoints.inbound`.

Site Ops composes every selected device set before validating the asset sets. A
device may therefore be shared by several independently selected asset sets.
Both halves are matched before deployment. Local validation names the source
set and available identities when a device or endpoint is missing. Published CI
output reports the failure without resource identities.

An externally supplied device is selected through a device set whose
`_siteops.external.devices` entry declares its identity, reason, and optional
expected endpoint shape. The asset set stays unchanged whether the device is
applied by this catalog or supplied elsewhere.

## Enabled state

Both kinds carry `properties.enabled`, and both matter:

- A device that is not enabled presents no endpoint, so the connector skips every asset bound to it.
- An asset that is not enabled is created and never served.

State both explicitly. A deploy succeeds either way, and the difference shows up only as data that never arrives.

## Names

Device and asset names are resource names, and the provider constrains them:

| Kind | Length | Pattern |
|---|---|---|
| `devices` | 3 to 63 | `^[a-z0-9][a-z0-9-]*[a-z0-9]$` |
| `assets` | 3 to 63 | `^[a-z0-9][a-z0-9-]*[a-z0-9]$` |

The Device Registry ARM API accepts a broader device-name shape, but the device
is projected to Kubernetes under the same name. The catalog therefore requires
the lowercase, alphanumeric-ended subset that is valid on both surfaces.

A name may interpolate site values, and the rules apply to the resolved value. The workspace tests render every committed declaration against every committed site and check the result, so a name that is valid for one site and too long or wrongly cased for another fails in CI.

## Where a declaration's values come from

Every `{{ ... }}` in a declaration resolves per site, so one committed file deploys across a fleet and each site receives its own values. `parameters/devices/site-devices.yaml` and `parameters/assets/site-assets.yaml` ship as the worked pair. The asset carries `{{ site.name }}` in the display name, in an attribute, and in the MQTT topic its dataset publishes to, so a hundred sites deploying one file each land on their own topic and stay tellable apart in the portal.

Interpolate into string-valued properties such as topics, display names, attributes, and endpoint addresses. The workspace tests compile each committed declaration against every supported API version, and they read a declaration as written rather than as resolved, so an interpolation in a numeric or boolean property is reported as a type error. State those values directly.

Preview the site values a declaration reads before deploying:

```bash
siteops -w workspaces/iot-operations sites munich-dev --render
```

## Step order

The Device Registry deployment family runs as one step, `asset-resources`.
Inside it, every selected device is created before any selected asset. An asset
refers to its device by name through `deviceRef`, and ARM does not model that
relationship, so each per-version module expresses the ordering with
`dependsOn`.

Across families, `manifests/aio-resources.yaml` runs the asset step before the dataflow step, so a dataflow whose source names an asset finds it already there.

## Composing with other steps

`manifests/_assets.yaml` is a partial, so a manifest that already installs AIO can add devices and assets without a second deploy. `manifests/aio-resources.yaml` gates it when either selection list is non-empty, and `samples/asset-sample/` composes it alongside `_resolve-aio.yaml` as a standalone deploy.

`_assets.yaml` carries no manifest-level parameters, which is what lets a composing manifest gate it and supply the declaration.

The step reads two chained values from `resolve-aio` through `parameters/inputs/catalog.yaml`: the custom location name, and the ADR namespace name the instance is actually bound to. That is why the family reads a namespace it discovered rather than one derived from the site name.

## Seeing data move

A device and an asset deploy on their own. Telemetry needs a server answering at the address the device declares. `samples/opc-ua-solution/` brings up the OPC PLC simulator as `opcplc-000000` in the AIO namespace, which is what the worked example addresses. Deploy that sample first, or point the address at a server the site already runs.

## Verifying a deploy

```bash
kubectl get devices.namespaces.deviceregistry.microsoft.com -n azure-iot-operations
kubectl get assets.namespaces.deviceregistry.microsoft.com -n azure-iot-operations
```

A device reports `spec.enabled` and its inbound endpoints. An asset reports the `spec.deviceRef` the provider stored.

## Removing an asset

Removing an entry from a declaration and redeploying leaves the resource in place. Delete it explicitly, and delete assets before the devices they bind to:

```bash
az resource delete --ids <assetResourceId>
az resource delete --ids <deviceResourceId>
```

See [resource-catalog.md](resource-catalog.md) for why a deploy writes only what the declaration names.

## See also

- [resource-catalog.md](resource-catalog.md) for the authoring contract every resource area shares
- [dataflows.md](dataflows.md) for moving what an asset publishes to a destination
- [aio-releases.md](aio-releases.md) for where `adrApiVersion` comes from

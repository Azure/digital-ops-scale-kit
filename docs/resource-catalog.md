# Resource catalog

Declaring AIO workload resources in YAML, so reviewable definitions compose and deploy across a fleet. A declaration is a plain file you can diff and hold in version control, and the same file reaches one site or a hundred through a site selector, with each site's own values substituted in.

The workspace ships templates that turn a declaration into resources, so a solution supplies what it wants rather than the scaffolding around it.

Both routes ship and both are supported. Declarations remove the ARM scaffolding for the resources the templates cover. Bicep remains available for everything else, including resources outside AIO.

Resource areas available today:

| Area | Declaration keys | Page |
|---|---|---|
| Devices | `devices` | [assets.md](assets.md) |
| Assets | `assets` | [assets.md](assets.md) |
| Dataflows | `dataflowEndpoints`, `dataflowProfiles`, `dataflows` | [dataflows.md](dataflows.md) |

## What the templates supply

To add one dataflow endpoint as an ARM resource, a template needs the AIO instance and custom location resolved, an `extendedLocation` block on every resource, parent wiring, and a literal API version:

```bicep
resource aioInstance 'Microsoft.IoTOperations/instances@2025-10-01' existing = {
  name: aioInstanceName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

resource endpoint 'Microsoft.IoTOperations/instances/dataflowEndpoints@2025-10-01' = {
  parent: aioInstance
  name: 'my-endpoint'
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  properties: {
    endpointType: 'Kafka'
    kafkaSettings: { ... }
  }
}
```

The same endpoint as a declaration:

```yaml
dataflowEndpoints:
  - name: my-endpoint
    properties:
      endpointType: Kafka
      kafkaSettings: { ... }
```

The `properties` block is identical in both. That is deliberate. A declaration keeps AIO's own vocabulary, so the resource provider documentation applies unchanged and there is no second schema to learn. The templates supply the scaffolding: instance and custom location resolution, `extendedLocation`, parent wiring, and the API version.

## When to write Bicep instead

A declaration can carry any value the site knows. Site variables resolve inside a declaration at any depth, so a resource pointing at shared cloud infrastructure reads its address from site configuration:

```yaml
dataflowEndpoints:
  - name: fabric-out
    properties:
      endpointType: Kafka
      kafkaSettings:
        host: "{{ site.parameters.eventHubHost }}:9093"
```

Reach for Bicep when a value is not knowable until the deployment is already running, or when the resource is one part of a larger solution that needs its own infrastructure anyway.

- **A value comes from a resource the same deployment creates.** `samples/opc-ua-solution/template.bicep` derives its dataflow endpoint host from the Event Hub namespace it creates in the same template. A governed resource definition cannot move to a step-level parameter file because composition owns those arrays at manifest level. Use Bicep when the value does not exist until the deployment runs.
- **The solution ships resources outside AIO.** The same sample creates an Event Hub namespace, an event hub, and a role assignment. Those never become catalog resources, so keeping the dataflow beside them in one template keeps the solution readable.
- **A resource needs per-item conditional deployment, per-item outputs, or ordering the array loop does not express.**
- **The declaration needs a property only some supported API versions have.** A declaration is shared fleet-wide and any site may select it, so it has to be valid at every API version a site could be running.

Both routes deploy through `siteops` and produce the same kind of resource on the same instance. `samples/opc-ua-solution/` writes its device, asset, and dataflow as ARM resources alongside the Event Hub it also needs, while `samples/dataflow-sample/` and `samples/asset-sample/` declare theirs in YAML. Deploying them in sequence against the same instance runs one of each, under different names, and neither disturbs the other.

## Step names

A family deploys as one step, named for the family: `asset-resources`, `dataflow-resources`. Inside it, `templates/aio/<family>/main.bicep` routes to a module for the API version the site's release ships, and that module creates every resource kind in order, so a resource exists before anything that references it.

One step per family rather than one per resource kind keeps a fleet deploy to one round trip per family per site, and keeps the step namespace small. Step names are a flat global namespace after include flattening, and a collision is a parse error.

Families run in the order `manifests/aio-resources.yaml` lists them, which is how a reference that crosses families is satisfied. Assets deploy before dataflows, so a dataflow naming an asset as its source finds it already there.

## Select resource sets per site

Each list under `properties.resourceSets` names ordered YAML sources from the
matching `parameters/<area>/` directory:

```yaml
properties:
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
    dataflows:
      - site-telemetry
```

The site above loads:

```text
parameters/devices/site-devices.yaml
parameters/assets/site-assets.yaml
parameters/dataflows/site-telemetry.yaml
```

Omit an area for no selection. Use `[]` when a child site must clear a list
inherited from its parent. Selection lists replace as a whole during site
inheritance, while the resource definitions from their selected files compose
by identity.

Deploy the fleet entry point:

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-resources.yaml -l environment=dev
```

Site values resolve inside each definition at any depth.

For runnable examples, start with
[`resource-set-basic`](../workspaces/iot-operations/samples/resource-set-basic/README.md),
which selects one dataflow set. Then use
[`resource-set-composition`](../workspaces/iot-operations/samples/resource-set-composition/README.md)
for inherited device selections, independent asset sets, an externally managed
device, and separately selected dataflow endpoint, profile, and route sources.

## Compose sets by resource identity

Distinct resources append in selection order. Two selected sources writing the
same resolved identity are rejected rather than merged implicitly.

Provider references resolve against the whole effective composition. An asset
set can therefore refer to a device from a separately selected device set, and
a dataflow set can refer to an endpoint or profile from another dataflow set.
A local plan names the source file for each applied resource and shows each
resolved reference. Published CI plans show aggregate counts without resource
identities or definition paths.

Some prerequisites have no provider field. Advanced sets declare those under
the reserved `_siteops` envelope:

```yaml
_siteops:
  requires:
    dataflows:
      - profileRef: production
        name: normalize
```

`_siteops.requires` asserts co-presence. It does not invent a provider
relationship or ordering inside one deployment.

An externally supplied resource is selected on its own resource area:

```yaml
# parameters/devices/external-plant-opc.yaml
_siteops:
  external:
    devices:
      - name: plant-opc
        reason: Created with the plant gateway infrastructure.
        expects:
          properties:
            endpoints:
              inbound:
                opc: {}
```

The consuming asset set remains unchanged whether `plant-opc` is applied by
another selected set or asserted as external. `_siteops` is Site Ops metadata,
is allowed only at the source root, and never reaches Bicep.

Prefix a source containing only external assertions with `external-` so its
selection reads clearly in a site file.

## Attach a definition directly

A sample or one-off manifest can attach a fixed definition source:

```yaml
parameters:
  - path: samples/dataflow-sample/dataflows.yaml
    collections: [dataflowEndpoints, dataflowProfiles, dataflows]
```

The object form names which composed collections the source may contribute.
Attach governed resource definitions at manifest level. Site and step parameter
tiers cannot replace composed collections.

Secret Sync predates this mechanism and remains a directly attached
declaration. See [secret-sync.md](secret-sync.md).

## What a deploy does

A deploy creates or updates every resource the declaration names, and leaves everything it does not name alone. Redeploying an unchanged declaration is a reconcile rather than a recreate.

Removing an entry and redeploying therefore leaves that resource in place. A deploy carries no record of what an earlier one created, so it has nothing to compare against. Delete the resource explicitly:

```bash
az resource delete --ids <resourceId>
```

Writing only the names the declaration carries is what lets a declaration run beside hand-written Bicep on the same instance. `samples/opc-ua-solution/` creates a dataflow from its own template, and a catalog deploy leaves it alone.

Selecting a set applies its definitions. Deselecting it stops applying those
definitions and does not delete resources created by an earlier deployment.

## API versions

Catalog templates route on the API version the site's release ships, the same way the platform templates do. Each family follows the release key its own resource provider is versioned by: dataflows route on `aioApiVersion`, and assets route on `adrApiVersion`. `main.bicep` dispatches on that key to one module per API version under the family's `modules/` directory, so a site's resources are written through the same API version as the platform serving them. Both keys come from the same `parameters/aio-releases/<release>.yaml` file, and they move independently across releases.

The writable surface of these resources happens to be identical across every supported API version today. That is not why they dispatch. Schema equality does not imply the resource provider handles a request the same way at every API version, and neither a compile check nor a schema comparison can see that difference, so a resource is written at its own release's version rather than through an older one.

The API version is not something a declaration names. That is the point: the site's `aioRelease` governs the platform, and the catalog resources follow without the author choosing a version.

During an upgrade a site names the new API version before the cluster runs it, so reapply a family once the upgrade finishes. See [aio-releases.md](aio-releases.md).

Declarations are compiled against **every** supported API version by the workspace tests, so a property missing from any of them fails in CI with the property and the API version named, rather than at deploy. A declaration is shared fleet-wide and any site may select it, so validating only the newest would pass in CI and fail live on a site that has not upgraded.

## Adding a resource area

A public resource area may use an existing deployment family or add one.
Devices and assets are separate site selections and share one Device Registry
step. Dataflow endpoints, profiles, and dataflows share the `dataflows` area
and step.

1. Add the resource collection and its identity to
   `contracts/aio-catalog.yaml`, plus any provider reference rules.
2. Add `parameters/<area>/` for reusable sets.
3. Add a typed parameter source to `manifests/aio-resources.yaml`, naming the
   collections that area may contribute.
4. Add or update the gated family partial and its versioned Bicep entry point.
   Keep `parameters/inputs/catalog.yaml` attached at step level so resolved
   parent names reach the deployment.
5. Put the provider deployment step before its consumer. Source order does not
   affect reference resolution, which runs over the whole effective
   composition.
6. Register only test-specific provider facts in
   `tests/workspace/catalog_harness.py`. Identity and provider seeds come from
   the runtime composition contract.

## Adding a set

1. Create `parameters/<area>/<set>.yaml` with the definitions that area accepts.
2. Add its name to the matching ordered list under
   `properties.resourceSets.<area>`.
3. Preview the steps with `siteops validate manifests/aio-resources.yaml -l <selector> --plan`.
4. Run the workspace tests with `pytest tests/workspace/ -q`, which check name uniqueness, reference resolution, required fields, and validity at every supported API version.

## See also

- [assets.md](assets.md) for the asset family's keys and behavior
- [dataflows.md](dataflows.md) for the dataflow family's keys and behavior
- [parameter-resolution.md](parameter-resolution.md) for the merge order and attachment tiers
- [site-configuration.md](site-configuration.md) for where `resourceSets` lives on a site
- [aio-releases.md](aio-releases.md) for the API-version policy

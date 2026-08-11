# Resource catalog

Declaring AIO workload resources in YAML, so one definition deploys across a fleet. A declaration is a plain file you can review, diff, and hold in version control, and the same file reaches one site or a hundred through a site selector, with each site's own values substituted in.

The workspace ships templates that turn a declaration into resources, so a solution supplies what it wants rather than the scaffolding around it.

Both routes ship and both are supported. Declarations remove the ARM scaffolding for the resources the templates cover. Bicep remains available for everything else, including resources outside AIO.

Families available today:

| Family | Declaration keys | Page |
|---|---|---|
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

- **A value comes from a resource the same deployment creates.** `samples/opc-ua-solution/template.bicep` derives its dataflow endpoint host from the Event Hub namespace it creates in the same template. A manifest-level declaration cannot carry that, because manifest-level parameters apply to every step including ones that run before the producer. Wiring a step output in is possible through a step-level file, at the cost of a site no longer being able to override the declaration. See [parameter-resolution.md](parameter-resolution.md).
- **The solution ships resources outside AIO.** The same sample creates an Event Hub namespace, an event hub, and a role assignment. Those never become catalog resources, so keeping the dataflow beside them in one template keeps the solution readable.
- **A resource needs per-item conditional deployment, per-item outputs, or ordering the array loop does not express.**
- **The declaration needs a property only some supported API generations have.** A declaration is shared fleet-wide and any site may select it, so it has to be valid at every generation a site could be running.

Both routes deploy through `siteops` and produce the same kind of resource on the same instance. `samples/opc-ua-solution/` writes its dataflow as ARM resources alongside the Event Hub it also needs, while `samples/dataflow-sample/` declares its dataflows in YAML. Deploying them in sequence against the same instance runs one of each.

## Step names

A family deploys as one step, named for the family: `dataflow-resources`. Inside it, `templates/aio/<family>/main.bicep` routes to a module for the AIO API generation the site's release ships, and that module creates every resource kind in order, so a resource exists before anything that references it.

One step per family rather than one per resource kind keeps a fleet deploy to one round trip per family per site, and keeps the step namespace small. Step names are a flat global namespace after include flattening, and a collision is a parse error.

## Two ways to attach a declaration

**Per site class, from a shared set.** Put the declaration in `parameters/<family>/<set>.yaml` and point sites at it. This is the fleet case, where one definition is shared by every site of a class:

```yaml
# sites/<site>.yaml
properties:
  resourceSets:
    dataflows: site-telemetry
```

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-resources.yaml -l environment=dev
```

The value of `resourceSets.<family>` names a file in the matching `parameters/` subdirectory, so the key and the directory always match. Every site inherits `none`, which declares empty arrays and deploys nothing.

`parameters/dataflows/site-telemetry.yaml` ships as a worked example. Its destination topic carries `{{ site.name }}`, so every site selecting it publishes under its own topic from one committed file. Any site value resolves the same way, at any depth in the declaration:

```yaml
dataflows:
  - name: site-telemetry
    properties:
      operations:
        - operationType: Destination
          destinationSettings:
            endpointRef: site-telemetry-out
            dataDestination: "telemetry/{{ site.name }}"
```

**Alongside a sample or a one-off manifest.** Attach the file directly, as `samples/dataflow-sample/` does:

```yaml
parameters:
  - samples/dataflow-sample/dataflows.yaml
```

Either way the declaration content is identical, and both run the same templates.

Secret sync predates this mechanism and supports only the second route. Its declaration lives beside the sample in `samples/secretsync-sample/secrets.yaml` and there is no `resourceSets.secrets` key. See [secret-sync.md](secret-sync.md).

Attach at **manifest level**, not step level. Manifest level sits below site parameters in the merge order, so a site or a `sites.local/` overlay can override it, and every step in the pipeline reads one source. A step-level file sits in the highest tier where a site cannot reach it. See [parameter-resolution.md](parameter-resolution.md) for the full rule.

Because lists replace wholesale rather than merging, an override supplies the complete array rather than the entries it wants to change.

## What a deploy does

A deploy creates or updates every resource the declaration names, and leaves everything it does not name alone. Redeploying an unchanged declaration is a reconcile rather than a recreate.

Removing an entry and redeploying therefore leaves that resource in place. A deploy carries no record of what an earlier one created, so it has nothing to compare against. Delete the resource explicitly:

```bash
az resource delete --ids <resourceId>
```

Writing only the names the declaration carries is what lets a declaration run beside hand-written Bicep on the same instance. `samples/opc-ua-solution/` creates a dataflow from its own template, and a catalog deploy leaves it alone.

## API versions

Catalog templates route on the AIO API generation the site's release ships, the same way the platform templates do. `main.bicep` dispatches on `aioApiVersion` to one module per generation under the family's `modules/` directory, so a site's dataflow resources are written through the same generation as the instance serving them.

The writable surface of these resources happens to be identical across every supported generation today. That is not why they dispatch. Schema equality does not imply the resource provider handles a request the same way at every generation, and neither a compile check nor a schema comparison can see that difference, so a resource is written at its own release's version rather than through an older one.

The API version is not something a declaration names. That is the point: the site's `aioRelease` governs the platform, and the catalog resources follow without the author choosing a version.

Declarations are compiled against **every** supported generation by the workspace tests, so a property missing from any of them fails in CI with the property and the generation named, rather than at deploy. A declaration is shared fleet-wide and any site may select it, so validating only the newest would pass in CI and fail live on a site that has not upgraded.

## Adding a family

Each family adds one partial and one declaration directory, and registers in one place. One name identifies the family everywhere: the directory under `templates/aio/`, the partial under `manifests/`, the `parameters/` subdirectory, and the `resourceSets` key all use it.

1. Add `templates/aio/<family>/main.bicep` routing on `aioApiVersion`, plus one module under `modules/` per supported AIO API generation, each creating every resource kind the family owns and ordering them with `dependsOn`.
2. Add `manifests/_<family>.yaml` with one step pointing at that `main.bicep`. Attach `parameters/inputs/catalog.yaml` at **step level**, which is what supplies the custom location the resolve step read back. Carry **no** manifest-level `parameters:`, otherwise the family cannot be gated.
3. Add `parameters/<family>/none.yaml` declaring an empty array for every key the family's `main.bicep` accepts.
4. Add a `resourceSets.<family>: "none"` default to `sites/base-site.yaml`.
5. Register the family in `manifests/aio-resources.yaml`: one `parameters:` line for the declaration path and one gated `include:`.

Step 2 matters more than it looks. `parameters/common/common.yaml` supplies a `customLocationName` derived from the site name, so a family that skips the chaining file still deploys, against a guessed name rather than the resolved one. That works on every site following the naming convention and fails on any that does not. `tests/workspace/test_catalog_gating.py` rejects a family step that omits it.

The declaration path loads for every site regardless of the gate, so steps 3 and 4 are what keep an unconfigured site valid. `siteops validate` reports a missing set rather than deploying nothing.

## Adding a set

1. Create `parameters/<family>/<set>.yaml` with the keys the family accepts.
2. Point one or more sites at it through `properties.resourceSets.<family>`.
3. Preview the steps with `siteops validate manifests/aio-resources.yaml -l <selector> -v`.
4. Run the workspace tests with `pytest tests/workspace/ -q`, which check name uniqueness, reference resolution, required fields, and validity at every supported API generation.

## See also

- [dataflows.md](dataflows.md) for the dataflow family's keys and behavior
- [parameter-resolution.md](parameter-resolution.md) for the merge order and attachment tiers
- [site-configuration.md](site-configuration.md) for where `resourceSets` lives on a site
- [aio-releases.md](aio-releases.md) for the API-version policy

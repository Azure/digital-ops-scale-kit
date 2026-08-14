# Dataflows

Dataflows move data between endpoints: from the MQTT broker to Event Hubs, from an asset to Fabric OneLake, from one broker topic to another. This page covers the dataflow family's declaration keys and behavior.

For what a declaration is, how to attach one, when to write Bicep instead, and the API-version policy, see [resource-catalog.md](resource-catalog.md). Those rules apply to every family.

## The three keys

A declaration file carries up to three arrays. All three are created by one module.

| Key | Creates | Template |
|---|---|---|
| `dataflowEndpoints` | Sources and destinations a dataflow can reference | `templates/aio/dataflows/modules/dataflows-<api-version>.bicep` |
| `dataflowProfiles` | Execution pools, sized by `instanceCount` | same module |
| `dataflows` | The pipelines themselves | same module |

`templates/aio/dataflows/main.bicep` is what a manifest step points at. It routes to the module for the AIO API version the site's release ships, so a site's dataflow resources are written at the same API version as the instance serving them.

Every key is optional. An omitted or empty array creates nothing.

Each entry is `{ name, properties }`. A `dataflows` entry also accepts `profileRef` to select its execution pool, defaulting to `default`.

A declaration using all three keys:

```yaml
dataflowEndpoints:
  - name: my-mqtt-out
    properties:
      endpointType: Mqtt
      mqttSettings:
        host: "aio-broker.azure-iot-operations:18883"
        authentication:
          method: ServiceAccountToken
          serviceAccountTokenSettings:
            audience: aio-internal
        tls:
          mode: Enabled
          trustedCaCertificateConfigMapRef: azure-iot-operations-aio-ca-trust-bundle

dataflowProfiles:
  - name: my-profile
    properties:
      instanceCount: 1

dataflows:
  - name: my-passthrough
    profileRef: my-profile
    properties:
      mode: Enabled
      operations:
        - operationType: Source
          sourceSettings:
            endpointRef: default
            dataSources:
              - "azure-iot-operations/data/#"
        - operationType: Destination
          destinationSettings:
            endpointRef: my-mqtt-out
            dataDestination: my-topic/output
```

The contents of each `properties` block are the resource provider's own schema. See the [AIO dataflow REST API reference](https://learn.microsoft.com/rest/api/iotoperations/) for the full property set of each type.

AIO creates a `default` endpoint and a `default` profile alongside the instance, so a declaration references those without supplying them. Do not declare an endpoint or a profile named `default`. The instance template owns both, and a second writer full-PUTs the resource, so each deploy discards what the other set. For the endpoint that also stops every dataflow sourcing `endpointRef: default` from moving data. The workspace tests reject either name.

## Where a declaration's values come from

Every `{{ ... }}` in a declaration resolves per site, so one committed file deploys across a fleet and each site receives its own values. A declaration names a value rather than the tier that defines it, so a fleet can move a value between tiers and every declaration keeps working unchanged.

`samples/dataflow-sample/dataflows.yaml` reads two tiers of the workspace fleet:

| Value | Defined in | Applies to |
|---|---|---|
| `site.labels.country` | `sites/shared/germany.yaml` | Every German site |
| `site.labels.city`, `site.name` | `sites/munich-dev.yaml` | One site |

`sites/base-site.yaml` sits above both and holds the fleet-wide defaults every site inherits.

Interpolate into string-valued properties such as topic paths, host names, and client id prefixes. The workspace tests compile each committed declaration against every supported API version, and they read a declaration as written rather than as resolved, so an interpolation in a numeric property such as `instanceCount` is reported as a type error. State those values directly.

Preview the site values a declaration reads before deploying:

```bash
siteops -w workspaces/iot-operations sites munich-dev --render
```

## Step order

The family deploys as one step, `dataflow-resources`. Inside it, endpoints and profiles are created before the dataflows that name them. A dataflow references an endpoint by name through `endpointRef`, and ARM does not model that relationship, so the ordering is what guarantees the target exists. Each per-version module expresses it with `dependsOn`, so ARM enforces it.

A reference to a name nothing declares deploys clean and never moves data. The workspace tests check every `endpointRef` and `profileRef` against the declarations in the same file.

## Composing with other steps

`manifests/_dataflows.yaml` is a partial, so a manifest that already installs AIO can add dataflows without a second deploy. `manifests/aio-resources.yaml` composes it that way, gated on the site's selected set.

`_dataflows.yaml` carries no manifest-level parameters, which is what lets a composing manifest gate it and supply the declaration. A sample composes it the same way, attaching its declaration on its own manifest, as `samples/dataflow-sample/` does.

## Verifying a deploy

```bash
kubectl get dataflows.connectivity.iotoperations.azure.com -n azure-iot-operations
kubectl get dataflowendpoints.connectivity.iotoperations.azure.com -n azure-iot-operations
```

A dataflow reports `mode: Enabled` once the resource projects to the cluster. Data movement lags that, since a source has to be publishing before anything flows.

## Removing a dataflow

Removing an entry from a declaration and redeploying leaves the resource in place. Delete it explicitly:

```bash
az resource delete --ids <dataflowResourceId>
```

See [resource-catalog.md](resource-catalog.md) for why a deploy writes only what the declaration names.

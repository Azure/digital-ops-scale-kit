# Parameter Resolution

Parameters flow from multiple sources and are automatically filtered for each template.

## Merge order

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (lowest) | Manifest parameters | `manifest.parameters` list: shared defaults |
| 2 | Site parameters | `site.parameters` section: overrides for one Site |
| 3 (highest) | Step parameters | `step.parameters` list: overrides for one step |

Later values override earlier values. Nested objects merge recursively. Lists are replaced rather than merged. This order follows the principle of specificity: manifest provides shared defaults, Sites override with specific values.

## Choosing an attachment tier

A parameter file's tier follows from what the file holds, not from which step consumes it.

| The file holds | Attach at | Why |
|---|---|---|
| **Chaining**: `{{ steps.X.outputs.Y }}` wiring | Step level | Wiring belongs to one consumer and must not be overridable. Step level is the tier with the highest precedence. |
| **Declaration**: values that operators author | Manifest level | Sits below Site parameters, so a Site or a `sites.local/` overlay overrides ordinary values. Applies to every step, so several steps can read one source. Composed resource collections are the exception below. |

The quick test is whether the file contains `{{ steps.`. If it does, it is a chaining file.

Attaching a declaration at step level makes it unoverridable, because step level outranks Site level and lists are replaced wholesale. Keep the two kinds in separate files even when the same step consumes both. The workspace test `test_manifest_level_parameters_carry_no_step_output_refs` enforces the chaining half of the rule.

Schema filtering keeps only the parameter names a consuming template accepts.
It is not a confidentiality classifier or permission to publish values.
An attached binding can also be filtered out, so input guidance must
distinguish authored wiring from the inputs actually retained by preparation.

Resource collections governed by a `ParameterComposition` contract are the
exception to ordinary tier precedence. They compose only from sources at manifest
level. Change them by selecting resource sets under `site.properties`, not by
writing the collection array in `site.parameters` or a step parameter file.

When a manifest pulls in others via `include:` (see [manifest-includes.md](manifest-includes.md)), each included manifest's `parameters:` at manifest level are appended after the parent's. Duplicate strings compare by normalized path. Source objects compare by normalized `path` and `forEach`, and their `collections` metadata must agree. The first occurrence keeps its position. That is not the same as the parent winning on a parameter key: different files still load in order, so the later file's scalar or ungoverned list value survives.

## Template variables

| Variable | Example |
|----------|---------|
| `{{ site.name }}` | `munich-dev` |
| `{{ site.location }}` | `germanywestcentral` |
| `{{ site.resourceGroup }}` | `rg-iot-munich-dev` |
| `{{ site.subscription }}` | `00000000-...` |
| `{{ site.labels.X }}` | Any label value |
| `{{ site.parameters.X.Y }}` | Nested parameter value |
| `{{ site.properties.X.Y }}` | Nested property |
| `{{ site.properties.X[0] }}` | Array indexing |
| `{{ steps.X.outputs.Y }}` | Output from step X |

Write each Site variable with one space inside each pair of braces, as shown.
`{{site.name}}` is not substituted, and a value the template accepts then fails
preparation with `contains an unresolved or unsupported template`.

When a whole value is a single `{{ site.properties.X }}` or
`{{ site.parameters.X }}` reference, it keeps the referenced type, so a list or
mapping reaches the template as a list or mapping. Embedded in a longer string,
the reference is converted to text.

An optional label can be the complete value of a parameter mapping member:

```yaml
tags:
  environment: "{{ site.labels.environment? }}"
  site: "{{ site.name }}"
```

When the label is absent, that member is omitted. When present, it uses the
same string conversion as an ordinary label reference. This syntax applies
only to complete mapping values, not names, list elements, embedded strings
or conditions. An ordinary `{{ site.labels.environment }}` remains required
during preparation. Unrelated null, false and zero values retain their meaning.

### Dynamic parameter paths

A parameter file path containing a Site template is selected by each Site:

```yaml
parameters:
  - "parameters/aio-releases/{{ site.properties.aioRelease }}.yaml"
```

After substitution, the path must be relative to the workspace, contain no `..` path segments, and
resolve to a file inside the workspace. A fixed path without a template may still be absolute when
a trusted runtime supplies the file.

Every attached parameter file must exist. A missing fixed file fails validation with
`Manifest parameter file not found` or, at step level, `Parameter file not found`. A path selected
by the Site fails with an error naming the Site, either because the Site does not carry the property the
path reads or because the resolved file does not exist.

## Output chaining

Reference outputs from previous steps:

```yaml
# parameters/inputs/aio-instance.yaml
schemaRegistryId: "{{ steps.schema-registry.outputs.schemaRegistry.id }}"
clExtensionIds: "{{ steps.aio-enablement.outputs.clExtensionIds }}"
```

> **Note**: Outputs of prior steps exist only during deployment. `siteops plan`
> records them as typed deferred references rather than resolving them to
> values.

## Parameter sources and resource sets

Shared defaults and step wiring live under `parameters`. Reusable workload
declarations live in the separate library of resource sets:

| Location | Role | Example |
|---|---|---|
| `parameters/common/` | Shared values derived from the Site, applied to all steps | `common.yaml` |
| `parameters/inputs/` | Consumer inputs (a step pulls outputs from upstream producers) | `inputs/aio-instance.yaml` pulls from `schema-registry`, `adr-ns`, `aio-enablement` |
| `parameters/outputs/` | Producer outputs (a single step's outputs feed multiple downstream consumers) | `outputs/aio-instance.yaml` feeds `schema-registry-role` |
| `parameters/aio-releases/` | Version pin files for each AIO release (selected via `site.properties.aioRelease`) | `aio-releases/2607.yaml` |
| `resource-sets/devices/` | Device definition sets selected through `site.properties.resourceSets.devices` | `devices/site-devices.yaml` |
| `resource-sets/assets/` | Asset definition sets selected through `site.properties.resourceSets.assets` | `assets/site-assets.yaml` |
| `resource-sets/dataflows/` | Dataflow definition sets selected through `site.properties.resourceSets.dataflows` | `dataflows/site-telemetry.yaml` |

A step that both consumes upstream outputs and feeds downstream consumers gets two files: one under `inputs/`, one under `outputs/`, named after the step (e.g. `inputs/aio-instance.yaml` and `outputs/aio-instance.yaml`).

A file may instead be named for a class of steps when they all read the same upstream values. `inputs/catalog.yaml` is the input file every resource catalog family step reads, so one file serves each family a workspace adds. See [resource-catalog.md](resource-catalog.md).

When one chaining file would be shared by multiple consumer steps **within the same manifest**, prefer one file per consumer step named `<manifest>-<step>.yaml` (e.g. `inputs/aio-upgrade-resolve-extensions.yaml`, `inputs/aio-upgrade-update-extensions.yaml`, and `inputs/aio-upgrade-deploy-release-resources.yaml`). A single shared file ends up with `{{ steps.X.outputs.Y }}` references that look forward from the perspective of the earliest consumer, which structural validation correctly rejects.

Samples keep their input and output files inside `samples/<name>/` rather than `parameters/`. The roles are the same. Only the location differs.

## Output chaining across scopes

Sites at resource group level can reference outputs from steps scoped to the subscription. Subscription outputs are keyed by subscription ID and resolved automatically. A consumer names the producing step the same way it would name a step scoped to a resource group:

```yaml
# An input file for a step that consumes the subscription-scoped producer
edgeSiteId: "{{ steps.global-edge-site.outputs.site.id }}"
```

`global-edge-site` is a step scoped to the subscription in `manifests/_partials/_aio-fundamentals.yaml`, deployed once for each subscription. `munich-dev` and `munich-prod` are Sites at resource group level in that same subscription, so both resolve this reference from the one set of outputs that step produced.

The consuming template has to declare the parameter. Automatic filtering removes a
chained value whose name the template does not accept. Executable preparation
also reports any required template parameter that remains absent.

Step names are unique across the flattened manifest, so each reference names
one producer. A reference to a step scoped to the subscription resolves from
that step's run for the Site's subscription. A reference to a step scoped to
a resource group resolves from the same Site's run. The Site at subscription
level must be selected in the same command. See
[Deployment in two phases](manifest-reference.md#deployment-in-two-phases).

## Automatic filtering

Parameters are automatically filtered to include only values accepted by each
template. Executable preparation then requires every parameter that is not
nullable and has no default. Nullable parameters and parameters with explicit defaults
may be omitted. A name at the top level derived from a prior operation stays deferred
until the output resolves, when the same schema check runs again. This enables
shared parameter files without postponing known missing inputs:

```yaml
# parameters/common/common.yaml - works with ANY template
location: "{{ site.location }}"
customLocationName: "{{ site.name }}-cl"
aioInstanceName: "{{ site.name }}-aio"
schemaRegistryName: "{{ site.name }}-sr"
adrNamespaceName: "{{ site.name }}-ns"
tags:
  environment: "{{ site.labels.environment }}"
```

When deploying:

- **schema-registry template**: Receives `location`, `tags`, `schemaRegistryName`
- **aio-instance template**: Receives `customLocationName`, `aioInstanceName`
- Extra parameters are omitted

## Best practices

| Parameter type | Where to define |
|----------------|-----------------|
| Sizing for one Site (replicas, memory) | `site.parameters` |
| Derived from Site variables | `parameters/common/common.yaml` |
| Output chaining (inputs) | `parameters/inputs/<step>.yaml` |
| Output chaining (outputs) | `parameters/outputs/<step>.yaml` |

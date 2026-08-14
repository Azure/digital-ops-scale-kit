# Parameter Resolution

Parameters flow from multiple sources and are automatically filtered per template.

## Merge order

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (lowest) | Manifest parameters | `manifest.parameters` list - shared defaults |
| 2 | Site parameters | `site.parameters` section - site-specific overrides |
| 3 (highest) | Step parameters | `step.parameters` list - step-specific overrides |

Later values override earlier values. Nested objects merge recursively. Lists are replaced rather than merged. This order follows the principle of specificity: manifest provides shared defaults, sites override with specific values.

## Choosing an attachment tier

A parameter file's tier follows from what the file holds, not from which step consumes it.

| The file holds | Attach at | Why |
|---|---|---|
| **Chaining**: `{{ steps.X.outputs.Y }}` wiring | Step level | Wiring belongs to one consumer and must not be overridable. Step level is the highest-precedence tier. |
| **Declaration**: operator-authored values | Manifest level | Sits below site parameters, so a site or a `sites.local/` overlay overrides it. Applies to every step, so several steps can read one source. |

The quick test is whether the file contains `{{ steps.`. If it does, it is a chaining file.

Attaching a declaration at step level makes it unoverridable, because step level outranks site level and lists are replaced wholesale. Keep the two kinds in separate files even when the same step consumes both. The workspace test `test_manifest_level_parameters_carry_no_step_output_refs` enforces the chaining half of the rule.

Manifest-level attachment is safe to use broadly because parameters are filtered per template: a step receives only the keys its own template declares. A `@secure()` value therefore reaches only the template that declares it.

When a manifest pulls in others via `include:` (see [manifest-includes.md](manifest-includes.md)), each included manifest's manifest-level `parameters:` are appended after the parent's. Duplicate paths (normalized POSIX strings) are dropped on a first-wins basis, so the same file declared by both is loaded once, in the parent's position. That is not the same as the parent winning on a *key*: when the parent and an included manifest attach different files that both set one key, the files load in list order and the included manifest's value survives. A parent that needs to override an included default sets it on the site instead.

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

## Output chaining

Reference outputs from previous steps:

```yaml
# parameters/inputs/aio-instance.yaml
schemaRegistryId: "{{ steps.schema-registry.outputs.schemaRegistry.id }}"
clExtensionIds: "{{ steps.aio-enablement.outputs.clExtensionIds }}"
```

> **Note**: Output chaining only works during real deployments. In `--dry-run` mode, output templates remain unresolved.

## `parameters/` layout

The directory groups files by the role they play in the parameter merge:

| Subdir | Role | Example |
|---|---|---|
| `parameters/common/` | Site-derived shared values applied to all steps | `common.yaml` |
| `parameters/inputs/` | Consumer fan-in (a step pulls outputs from upstream producers) | `inputs/aio-instance.yaml` pulls from `schema-registry`, `adr-ns`, `aio-enablement` |
| `parameters/outputs/` | Producer fan-out (a single step's outputs feed multiple downstream consumers) | `outputs/aio-instance.yaml` feeds `schema-registry-role` |
| `parameters/aio-releases/` | Per-release version pin files (selected via `site.properties.aioRelease`) | `aio-releases/2607.yaml` |
| `parameters/dataflows/` | Dataflow declaration sets (selected via `site.properties.resourceSets.dataflows`) | `dataflows/none.yaml` |

A step that has both fan-in inputs and fan-out outputs gets two files: one under `inputs/`, one under `outputs/`, named after the step (e.g. `inputs/aio-instance.yaml` and `outputs/aio-instance.yaml`).

A file may instead be named for a class of steps when they all read the same upstream values. `inputs/catalog.yaml` is the fan-in every resource catalog family step reads, so one file serves each family a workspace adds. See [resource-catalog.md](resource-catalog.md).

When one chaining file would be shared by multiple consumer steps **within the same manifest**, prefer one file per consumer step named `<manifest>-<step>.yaml` (e.g. `inputs/aio-upgrade-resolve-extensions.yaml`, `inputs/aio-upgrade-update-extensions.yaml`). A single shared file ends up with `{{ steps.X.outputs.Y }}` references that look forward from the perspective of the earliest consumer, which structural validation correctly rejects.

Samples co-locate their input and output files inside `samples/<name>/` rather than `parameters/`. The roles are the same. Only the location differs.

## Cross-scope output chaining

RG-level sites can reference outputs from subscription-scoped steps. Subscription outputs are keyed by subscription ID and resolved automatically. A consumer names the producing step the same way it would name an RG-scoped one:

```yaml
# An input file for a step that consumes the subscription-scoped producer
edgeSiteId: "{{ steps.global-edge-site.outputs.site.id }}"
```

`global-edge-site` is a subscription-scoped step in `manifests/_aio-fundamentals.yaml`, deployed once per subscription. `munich-dev` and `munich-prod` are RG-level sites in that same subscription, so both resolve this reference from the one set of outputs that step produced.

The consuming template has to declare the parameter. Auto-filtering drops anything a template does not accept, so a chained value whose name is not a declared parameter is removed before the deployment.

**Resolution priority:**

1. Per-site step outputs (from RG-scoped steps)
2. Subscription outputs (from subscription-scoped steps, matched by site's subscription)

## Auto-filtering

Parameters are automatically filtered to only include values accepted by each template. This enables shared parameter files:

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
- Extra parameters are silently filtered out

## Best practices

| Parameter type | Where to define |
|----------------|-----------------|
| Site-specific sizing (replicas, memory) | `site.parameters` |
| Derived from site variables | `parameters/common/common.yaml` |
| Output chaining (fan-in) | `parameters/inputs/<step>.yaml` |
| Output chaining (fan-out) | `parameters/outputs/<step>.yaml` |

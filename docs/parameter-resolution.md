# Parameter Resolution

Parameters flow from multiple sources and are automatically filtered per template.

## Merge order

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (lowest) | Manifest parameters | `manifest.parameters` list - shared defaults |
| 2 | Site parameters | `site.parameters` section - site-specific overrides |
| 3 (highest) | Step parameters | `step.parameters` list - step-specific overrides |

Later values override earlier values. Nested objects merge recursively. This order follows the principle of specificity: manifest provides shared defaults, sites override with specific values.

When a manifest pulls in others via `include:` (see [manifest-includes.md](manifest-includes.md)), each included manifest's manifest-level `parameters:` are appended after the parent's. Duplicate paths (normalized POSIX strings) are dropped on a first-wins basis, so the parent always wins on conflict.

## Template variables

| Variable | Example |
|----------|---------|
| `{{ site.name }}` | `munich-dev` |
| `{{ site.location }}` | `germanywestcentral` |
| `{{ site.resourceGroup }}` | `rg-iot-munich-dev` |
| `{{ site.subscription }}` | `00000000-...` |
| `{{ site.labels.X }}` | Any label value |
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
| `parameters/outputs/` | Producer fan-out (a single step's outputs feed multiple downstream consumers) | `outputs/aio-instance.yaml` feeds `schema-registry-role` and the OPC UA sample |
| `parameters/aio-releases/` | Per-release version pin files (selected via `site.properties.aioRelease`) | `aio-releases/2603.yaml` |

A step that has both fan-in inputs and fan-out outputs gets two files: one under `inputs/`, one under `outputs/`, named after the step (e.g. `inputs/aio-instance.yaml` and `outputs/aio-instance.yaml`).

When one chaining file would be shared by multiple consumer steps **within the same manifest**, prefer one file per consumer step named `<manifest>-<step>.yaml` (e.g. `inputs/aio-upgrade-resolve-extensions.yaml`, `inputs/aio-upgrade-update-extensions.yaml`). A single shared file ends up with `{{ steps.X.outputs.Y }}` references that look forward from the perspective of the earliest consumer, which structural validation correctly rejects.

Samples co-locate their input and output files inside `samples/<name>/` rather than `parameters/`. The roles are the same; only the location differs.

## Cross-scope output chaining

RG-level sites can reference outputs from subscription-scoped steps. Subscription outputs are keyed by subscription ID and resolved automatically:

```yaml
# parameters/inputs/aio-instance.yaml
edgeSiteId: "{{ steps.global-edge-site.outputs.site.id }}"
```

For `munich-line-1` (subscription: sub-123):
→ Resolves from subscription outputs for sub-123

For `munich-line-2` (subscription: sub-123):
→ Resolves from the same subscription outputs

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
- **aio-instance template**: Receives `location`, `tags`, `customLocationName`, `aioInstanceName`
- Extra parameters are silently filtered out

## Best practices

| Parameter type | Where to define |
|----------------|-----------------|
| Site-specific sizing (replicas, memory) | `site.parameters` |
| Derived from site variables | `parameters/common/common.yaml` |
| Output chaining (fan-in) | `parameters/inputs/<step>.yaml` |
| Output chaining (fan-out) | `parameters/outputs/<step>.yaml` |

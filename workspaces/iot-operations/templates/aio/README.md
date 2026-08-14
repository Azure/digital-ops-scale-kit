# templates/aio/

Templates for the Azure IoT Operations resource provider. This is the largest
template area, and it holds three structurally different kinds of subdirectory.
Knowing which kind you are in tells you how to add to it.

| Kind | Example | Shape |
|---|---|---|
| **Platform lifecycle** | `enablement.bicep`, `instance.bicep`, `resolve-aio.bicep` | Top-level templates a manifest step points at directly. `instance.bicep` and `resolve-aio.bicep` are dispatchers that switch on the AIO API version. |
| **Per-API-version modules** | `modules/` | Inner modules a dispatcher routes to, one per API version. Added only where an API version genuinely diverges. |
| **Resource catalog families** | `dataflows/` | A `main.bicep` routing on the AIO API version to one module per API version under the family's own `modules/`. Deployed through `manifests/aio-resources.yaml`, gated per site. |
| **Lifecycle phases** | `upgrade/` | Templates for one operation that spans several steps, kept together rather than at the top level. |

## Platform lifecycle and dispatchers

A resource type and API version is a string literal in Bicep and cannot be
computed, which is why an API version that diverges needs its own module
rather than a parameter. Adding an `aioApiVersion` means updating every
consumer that routes on it. See [aio-releases.md](../../../../docs/aio-releases.md).

The read side routes on the same API version as the write side. For an
Arc-mapped resource provider the pinned API version selects the resource shape
the provider projects through, so `resolve-aio.bicep` is a dispatcher rather
than a single `existing` reference.

## Resource catalog families

A family is a group of related resource kinds deployed as one step. Each family
directory ships a `main.bicep` that routes on `aioApiVersion` to one module per
supported AIO API version, and each module creates every kind the family owns,
ordered with `dependsOn`.

One name identifies a family everywhere: the directory here, the partial at
`manifests/_<family>.yaml`, the declaration directory at `parameters/<family>/`,
and the `resourceSets.<family>` key on a site.

A family writes at the API version the site's release ships, matching the platform
templates. The writable surface of these resources is identical across supported
API versions today, but schema equality does not imply the provider behaves the
same way, so the API version is not assumed to be interchangeable. Adding an API
version means extending `@allowed` on `main.bicep` and copying the newest
module with its API version literals changed.

See [resource-catalog.md](../../../../docs/resource-catalog.md) for the
authoring contract and [dataflows.md](../../../../docs/dataflows.md) for the
dataflow family's keys.

## Adding to this area

- A new platform resource that AIO owns goes at the top level.
- A new workload resource an operator configures goes in a family directory.
- A new API version that diverges goes in `modules/`, reached by a dispatcher.

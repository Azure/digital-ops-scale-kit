# Reusable resource sets

Author named device, asset and dataflow definitions here, then select them
through `properties.resourceSets.<area>` on a Site.

| Area | Contains |
|---|---|
| `devices/` | Device definitions and external-device assertions |
| `assets/` | Asset definitions and their provider requirements |
| `dataflows/` | Endpoints, profiles and dataflow routes |

For example, `properties.resourceSets.dataflows: [basic-routing]` selects
`dataflows/basic-routing.yaml`. Apply selections with
[aio-resources](../manifests/aio-resources/README.md). Keep source order when
composing several sets. An empty list clears an inherited selection.

These files describe desired workload resources. Shared defaults and
step-output wiring remain under `parameters/`. A declaration used only by one
sample can stay beside that sample. Promote it here when it becomes an
intentionally shared selection, rather than maintaining two copies.

Device and asset selections are separate public areas even though one
internally ordered deployment family writes both. Collection identity,
references and provider-owned seeds are defined by
[`contracts/aio-catalog.yaml`](../contracts/aio-catalog.yaml).

Selection applies resources, not a separate deployment package. Deselecting
a set does not delete its resources. See the
[resource catalog reference](../../../docs/resource-catalog.md) for authoring,
composition and removal semantics.

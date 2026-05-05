# Manifest Includes

A manifest can splice another manifest's steps into its own step list using a step-level `include:` directive. This makes one manifest viewable two ways: standalone, or as a partial composed into a larger pipeline.

```yaml
# scenarios/aio-with-opc-ua.yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-with-opc-ua
description: Compose AIO fundamentals with the OPC UA sample.

steps:
  - include: ../manifests/_aio-fundamentals.yaml
  - include: ../manifests/_resolve-aio.yaml
  - include: ../samples/opc-ua-solution/_partial.yaml
```

After resolution, the parent's step list is a flat sequence of every step the included manifests contribute, in declared order, interleaved with any inline steps the parent defines.

The standalone-vs-partial distinction matters for composition: scenarios should include the leaf partials, not standalone manifests. See [Standalone manifests vs partials](#standalone-manifests-vs-partials) and `scenarios/README.md`.

## Step shape

```yaml
- include: <path>           # required, string, workspace-relative
  when: "{{ ... }}"         # optional condition; see "Conditional includes"
```

No other keys are allowed alongside `include:`. Adding `name`, `template`, `type`, `arc`, `files`, `operation`, `parameters`, or `scope` to an include step is a parse error.

## Path resolution

- Paths are resolved relative to the **including manifest's directory**.
- The resolved path must stay inside the workspace root. `../` traversal that escapes the workspace is rejected.
- The path is static. Site-driven include paths (e.g. `samples/{{ site.properties.preferredSample }}/manifest.yaml`) are not supported.

## Conditional includes

A `when:` on the include step propagates to every spliced step:

```yaml
steps:
  - include: ../samples/opc-ua-solution/_partial.yaml
    when: "{{ site.properties.deployOptions.enableOpcUa }}"
```

If a spliced step already has its own `when:`, the include cannot also set one. Combining two `when:` expressions is not supported. Consolidate into a single condition on either side.

If the included manifest defines manifest-level `parameters:`, the include cannot set `when:`. Manifest-level parameters apply unconditionally to every parent step at deploy time, so a gated include contributing parameters would silently affect ungated parent steps. Either drop the include's `when:` or move parameters onto the included manifest's individual steps.

## Recursive includes

Includes may include further includes. Cycles are detected (a manifest cannot, directly or indirectly, include itself) and reported with the full include chain. Maximum include depth is 8.

A partial shared by two siblings (A includes B and C, both B and C include D) is allowed. Cycle detection tracks the current depth-first path, not a global visited set. Step-name collisions in the resulting flat list are still rejected. Ensure shared partials contribute uniquely-named steps. This is the main reason scenarios should compose `_partial.yaml` files rather than two standalone manifests that each include the same partial.

## Step name uniqueness

Step names must be unique across the entire flattened pipeline (parent and all included partials). A duplicate name is a parse error.

## Parameter merge

Manifest-level `parameters:` lists merge across includes:

- The parent's `parameters:` come first.
- Each include's manifest-level `parameters:` are appended after, in include order.
- Duplicate paths (compared as normalized POSIX strings) are dropped on the first wins basis. The parent therefore wins on conflict.

Step-level `parameters:` (on individual steps) are not affected by include resolution. They follow the existing per-step rules.

## Standalone manifests vs partials

Any manifest can be included. When it is, top-level fields that only make sense for standalone deployment are silently ignored:

- `name`, `description`, `selector`, `sites`, and `parallel` flow no further than the included file.
- Only `steps:` and manifest-level `parameters:` are spliced into the parent.

The convention for files authored primarily to be included is the `_` filename prefix (e.g., `_aio-fundamentals.yaml`, `_partial.yaml`). Standalone manifests such as `manifests/aio-install.yaml` exist as convenience entry points for `siteops deploy`. **Compositions should include the `_` partials, not the standalone manifests**, so that two siblings can share a common preamble without colliding on step names.

## Empty includes

An include must contribute at least one step after recursion. Including a manifest with `steps: []` is a parse error.

## Output chaining across includes

Step output references (`{{ steps.<name>.outputs.<field> }}`) are resolved against the post-flatten step list. A consumer can reference any other step's outputs as long as the producing step appears earlier than the consumer in the flat post-include order.

## See also

- [manifest-reference.md](manifest-reference.md): step shapes, conditional steps, parallel execution.
- [parameter-resolution.md](parameter-resolution.md): how parameters merge across manifest, site, and step levels.
- [targeting.md](targeting.md): how a composed manifest's sites are selected. Partials inherit the parent's targeting.

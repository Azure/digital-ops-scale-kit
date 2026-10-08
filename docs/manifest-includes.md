# Manifest Includes

A manifest can splice another manifest's steps into its own step list using an `include:` step. This makes one manifest viewable two ways: standalone, or as a partial composed into a larger pipeline.

```yaml
# Simplified from samples/aio-with-opc-ua/manifest.yaml, which also sets a
# selector, attaches parameters at manifest level, and adds a gated Secret Sync include.
apiVersion: siteops/v1
kind: Manifest
name: aio-with-opc-ua
description: Compose AIO fundamentals with the OPC UA sample.

steps:
  - include: ../../manifests/_partials/_aio-fundamentals.yaml
  - include: ../../manifests/_partials/_resolve-aio.yaml
  - include: ../opc-ua-solution/_partial.yaml
```

Include paths are resolved relative to the including manifest's directory. From `samples/<name>/manifest.yaml`, partials under `manifests/` are two levels up (`../../manifests/`) and sibling samples are one level up (`../<other-sample>/`).

After resolution, the parent's step list is a flat sequence of every step the included manifests contribute, in declared order, interleaved with any inline steps the parent defines.

The difference between standalone manifests and partials matters for composition. Compositions should include the leaf `_partial.yaml`s, not standalone `manifest.yaml`s. Standalone manifests can repeat prerequisites such as `resolve-aio`, which gives two flattened steps the same name. See [Standalone manifests vs partials](#standalone-manifests-vs-partials) and [Composing samples](../workspaces/iot-operations/samples/README.md#composing-samples).

## Step shape

```yaml
- include: <path>           # required, string, file-relative
  when: "{{ ... }}"         # optional condition. See "Conditional includes"
```

No other keys are allowed alongside `include:`. Adding `name`, `template`, `type`, `arc`, `files`, `operation`, `parameters`, or `scope` to an include step is a parse error.

## Path resolution

- Paths are resolved relative to the **including manifest's directory**.
- The resolved path must stay inside the workspace root. `../` traversal that escapes the workspace is rejected.
- The path is static. It cannot depend on Site values, such as `samples/{{ site.properties.preferredSample }}/manifest.yaml`.

## Conditional includes

A `when:` on the include step propagates to every spliced step:

```yaml
# Illustrative manifests/custom/manifest.yaml. Define this flag in the Site.
steps:
  - include: ../../samples/opc-ua-solution/_partial.yaml
    when: "{{ site.properties.deployOptions.enableOpcUa }}"
```

If a spliced step already has its own `when:`, the include cannot also set one. Combining two `when:` expressions is not supported. Consolidate into a single condition on either side.

If the included manifest, or any manifest it includes, defines `parameters:` at manifest level, the include cannot set `when:`. Parameters at manifest level apply unconditionally to every parent step at deploy time, so a gated include contributing parameters would silently affect ungated parent steps. Either drop the include's `when:` or move parameters onto the included manifest's individual steps.

## Recursive includes

Includes may include further includes. Cycles are detected (a manifest cannot, directly or indirectly, include itself) and reported with the full include chain. Maximum include depth is 8.

A partial shared by two siblings (A includes B and C, both B and C include D) is allowed. Cycle detection tracks the current include chain, not a global visited set. Duplicate step names in the resulting flat list are still rejected. Ensure shared partials contribute steps with unique names. This is the main reason compositions should compose `_partial.yaml` files rather than two standalone manifests that each include the same partial.

## Step name uniqueness

Step names must be unique across the entire flattened pipeline (parent and all included partials). A duplicate name is a parse error.

## Parameter merge

`parameters:` lists at manifest level merge across includes:

- The parent's `parameters:` come first.
- Each include's `parameters:` at manifest level are appended after, in include order.
- Duplicate string paths are compared as normalized POSIX strings. Parameter
  source objects are compared by normalized `path` and `forEach`. The first
  occurrence keeps its position, and duplicate sources must declare the same
  `collections` metadata.
- That is path deduplication, not key precedence. When the parent and an include
  attach different files that both set one key, the files load in list order.
  The later file's scalar or ungoverned list value survives. A collection
  governed by a `ParameterComposition` contract composes by identity instead,
  and two sources writing one identity are rejected.

`parameters:` on individual steps are not affected by include resolution. They follow the existing rules for steps.

`parameterCompositions:` paths also merge across includes. They are
relative to the workspace, canonicalized, and deduplicated. A gated include contributes
its contracts unconditionally because the contract describes parameter
identity and references, while rules over unselected collections bind nothing.

## Standalone manifests vs partials

Any manifest can be included. When it is, fields at the top level that only make sense for standalone deployment are silently ignored:

- `name`, `description`, `selector`, `sites`, and `parallel` flow no further than the included file.
- Only `steps:`, `parameters:` at manifest level, and
  `parameterCompositions:` are spliced into the parent.

The convention for files authored primarily to be included is the `_` filename prefix (e.g., `_aio-fundamentals.yaml`, `_partial.yaml`). Standalone manifests such as `manifests/aio-install/manifest.yaml` exist as convenient starting points for `siteops deploy`. **Compositions should include the `_` partials, not the standalone manifests**, so that two siblings can share a common preamble without colliding on step names.

## Empty includes

An include must contribute at least one step after recursion. Including a manifest with `steps: []` is a parse error.

## Output chaining across includes

Step output references (`{{ steps.<name>.outputs.<field> }}`) are resolved against the flattened step list. A consumer can reference any other step's outputs as long as the producing step appears earlier than the consumer in that flat order.

## See also

- [manifest-reference.md](manifest-reference.md): step shapes, conditional steps, parallel execution.
- [parameter-resolution.md](parameter-resolution.md): how parameters merge across manifest, Site, and step levels.
- [targeting.md](targeting.md): how a composed manifest's Sites are selected. Partials inherit the parent's targeting.

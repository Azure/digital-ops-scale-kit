# Site targeting

How Site Ops decides which Sites a manifest applies to. You can supply one
explicit Site with `--site-file`, `--input-file`, or `--input`. Otherwise,
the manifest's `sites:` list, its `selector:`, and the CLI `-l/--selector`
flag determine the set of configured Sites.

## Precedence

An explicit Site replaces the manifest's `sites:` list or selector, and cannot
be combined with `-l`. For configured Sites, CLI `-l/--selector` overrides
the manifest. Inside a manifest, `sites:` and `selector:` are mutually
exclusive. Resolution chooses the first present source in this order:

1. **CLI `-l`** if provided. Replaces manifest targeting entirely.
2. **Manifest `sites:`** explicit list of Site names.
3. **Manifest `selector:`** label expression filter.

Resource ID answers on `plan` and `deploy` authorize the declared, bounded
Azure reads needed to construct one Site. `inputs` requires
`--read-resources` to read resources while previewing or saving a Site. Typed answers
cannot be combined with `--site-file` or `-l`. See
[guided inputs](guided-inputs.md) for the provider read and privacy boundary.

A manifest with all three configured targeting sources empty is allowed as a
library or partial. Ordinary validation needs no Site. Planning and
deployment require `-l` or an explicit Site.

```yaml
# manifests/aio-install/manifest.yaml
selector: "environment=dev"    # shipped default scope
```

The examples on this page use `plan`, which previews the selection without
deploying. `deploy` accepts the same selectors.

```bash
siteops plan manifests/aio-install/manifest.yaml                    # all env=dev sites
siteops plan manifests/aio-install/manifest.yaml -l name=munich-dev # only munich-dev
```

The default `environment=dev` selects every matching Site, including one
that already runs AIO. To install on a separate set of new Sites, review a plan
with explicit `-l name=...` values and use the same selector for deploy.
Reapplying `aio-install` to an existing AIO Site can overwrite settings
managed there.

For AIO fleets, use one resource group per AIO instance. Each Site deploys
into its `resourceGroup`, which is its Arc cluster's resource group, so
connect each cluster in its own resource group.

## Selector grammar

A selector is one or more `key=value` pairs joined by commas. Pairs with distinct keys combine with AND.

The following label examples are illustrative. Define the referenced labels
on your Sites, or substitute keys and values from your own inventory.

```bash
siteops plan manifests/aio-install/manifest.yaml -l environment=prod,region=eu
# Selects sites where labels.environment == "prod" AND labels.region == "eu".
```

`-l` is repeatable. Each invocation contributes `key=value` pairs that combine with the others using AND.

```bash
siteops plan manifests/aio-install/manifest.yaml -l environment=prod -l region=eu
# Equivalent to the comma-joined form above.
```

### The `name` key

`name=` is the one selector key whose duplicate values combine with OR. Select several Sites by repeating `name=` values.

```bash
siteops plan manifests/aio-install/manifest.yaml -l name=munich-dev,name=seattle-dev
# Targets exactly munich-dev OR seattle-dev.
```

Duplicate values for any other key raise an error pointing at the conflict, since this is almost always a typo:

```bash
siteops plan manifests/aio-install/manifest.yaml -l env=dev -l env=prod
# Deployment plan is unavailable.
#   Error: Selector key `env` may only appear once. Selectors AND across
#   keys, so duplicating a key would always match zero Sites. Only `name=`
#   supports multiple values (OR-combined).
```

### Path names

For Sites under nested `sites/` subdirectories, `name=` accepts both the basename (filename without extension) and the relative path under the trusted directory. Both forms resolve to the same Site.

```bash
siteops plan manifests/aio-install/manifest.yaml -l name=munich-dev
siteops plan manifests/aio-install/manifest.yaml -l name=regions/eu/munich-dev
# Both target the file at `sites/regions/eu/munich-dev.yaml`.
```

## Site identity

Each deployable Site is reachable by three identifiers, all of which work in `-l name=`, in manifest `sites:` lists, and in `siteops sites <name>`:

| Form | Example | Notes |
|---|---|---|
| Basename | `munich-dev` | The filename without extension. The orchestrator enforces basename uniqueness across each trusted dir at load time. |
| Relative path | `regions/eu/munich-dev` | The path under the owning trusted dir, no extension. |
| Internal `name:` | `contoso-munich` | The value of the `name:` field if it differs from the basename. Must be unique across the workspace. |

**Basename uniqueness.** Within any one trusted directory, every Site basename must be unique across all subdirectories. The orchestrator rejects collisions at load time so `-l name=<basename>` always resolves to one file. Collisions across directories are valid only when the relative path also matches (the overlay pattern).

**Path normalization.** Path identifiers are normalized: backslashes become forward slashes, `..` and `./` segments are rejected, leading or trailing `/` is rejected. These rules apply to both manifest `sites:` entries and `-l name=` values.

## Library and partial manifests

A manifest with no `sites:` and no `selector:` is a library or partial.
Standalone planning or deployment requires `-l` or an explicit Site to supply
the Site. See [guided inputs](guided-inputs.md) for the path with one Site.

```yaml
# manifests/diagnostics.yaml
apiVersion: siteops/v1
kind: Manifest
name: diagnostics
description: Capture diagnostic snapshots from a single Site on demand.
steps:
  - name: capture
    template: templates/diagnostics/capture.bicep
    scope: resourceGroup
```

```bash
siteops plan manifests/diagnostics.yaml -l name=munich-prod
# Works. CLI supplies the targeting the manifest deferred.

siteops plan manifests/diagnostics.yaml
# Deployment plan is unavailable.
#   Error: Manifest 'diagnostics' has no targeting. Add `sites:` or
#   `selector:` to the manifest, or pass `-l <key>=<value>` on the CLI.
```

Partials (filename prefixed `_`) compose into other manifests via `include:`. They almost always omit targeting on the assumption that the parent manifest sets it. See [manifest-includes.md](manifest-includes.md).

## Diagnostic when nothing matches

When a CLI selector matches zero Sites, `plan` and `deploy` exit nonzero with a diagnostic that lists what the workspace actually contains for each requested key. The diagnostic catches typos at the moment the operator runs the command.

```bash
siteops plan manifests/aio-install/manifest.yaml -l environment=prdo
# Deployment plan is unavailable.
#   Error: CLI selector `-l environment=prdo` matched no Sites.
#   `environment=prdo` requested. Workspace `environment` values: 'dev',
#   'prod', 'sample', 'staging'.
```

```bash
siteops plan manifests/aio-install/manifest.yaml -l name=does-not-exist
# Deployment plan is unavailable.
#   Error: CLI selector `-l name=does-not-exist` matched no Sites.
#   `name=does-not-exist` not found. Workspace Site names: <site-1>,
#   <site-2>.
```

When the Site name matches but another selector key knocks it out, the diagnostic says so:

```bash
siteops plan manifests/aio-install/manifest.yaml -l name=munich-dev,environment=prod
# Deployment plan is unavailable.
#   Error: CLI selector `-l name=munich-dev,environment=prod` matched no
#   Sites. `name=munich-dev` matched a workspace Site but another selector
#   key filtered it out. `environment=prod` requested. Workspace
#   `environment` values: 'dev', 'prod', 'sample', 'staging'.
```

Both manifest selectors and CLI selectors that match zero Sites return a
nonzero exit code from planning and deployment.

## Validation

`siteops validate <manifest>` performs the shared structural checks used
before planning and deployment, without compiling. Executable `plan` and
`deploy` then add template acquisition, checks for required inputs, and local
capability preflight.

- **Unknown manifest keys are rejected** with a `did you mean` hint sourced
  from the canonical list (`apiVersion`, `kind`, `name`, `description`,
  `sites`, `selector`, `siteSelector`, `parallel`, `parameters`,
  `parameterCompositions`, `steps`). `siteSelector` is the deprecated
  spelling of `selector` and is still accepted.
- **Selector parse errors** (duplicate non-`name` keys, malformed pairs) are surfaced as validation errors alongside other manifest issues, so you see every problem in one pass.
- **Library manifests pass validation** because no targeting is structurally OK. Add `-l` when running `validate` to exercise the resolve path against real Sites.

## Pitfalls

- **`-l env=prod -l env=dev` errors.** Selectors AND across keys, so duplicating any key other than `name` would always match zero Sites. To target several fleets, run two commands or add a label that spans them.
- **`-l name=path/to/site` works but is rare in practice.** The basename form is shorter and just as unambiguous when the basename invariant holds.
- **Adding a nested Site that collides on basename fails the workspace load.** Rename one of the colliding files. The error message names both paths.
- **An overlay in `sites.local/` cannot rename a Site.** It may restate the same `name:` (common when the overlay mirrors the base shape) but cannot change it. The same rule applies to a file in an extras directory that overlays a base file at the same path under `sites/`. Use `inherits:` or rename the base file instead.

## Related

- [site-configuration.md](site-configuration.md). The Site object, inheritance, overlays, extras directories.
- [manifest-reference.md](manifest-reference.md). Manifest shape, step types, conditions.
- [manifest-includes.md](manifest-includes.md). Partials and `include:` composition.

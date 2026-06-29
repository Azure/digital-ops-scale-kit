# Site targeting

How `siteops` decides which sites a manifest applies to. Three sources contribute: the manifest's `sites:` list, the manifest's `selector:`, and the CLI `-l/--selector` flag. This page covers the precedence between them, the grammar of selectors, the site-identity model, and the no-match diagnostic.

## Precedence

CLI `-l/--selector` overrides the manifest. Inside a manifest, `sites:` and `selector:` are mutually exclusive. Resolution chooses the first present source in this order:

1. **CLI `-l`** if provided. Replaces manifest targeting entirely.
2. **Manifest `sites:`** explicit list of site names.
3. **Manifest `selector:`** label expression filter.

A manifest with all three sources empty is allowed (a "library" or partial manifest). Such a manifest requires `-l` at deploy time.

```yaml
# manifests/aio-install.yaml
selector: "environment=prod"   # default scope
```

```bash
siteops deploy manifests/aio-install.yaml                    # all env=prod sites
siteops deploy manifests/aio-install.yaml -l name=munich-dev # only munich-dev
```

Same precedence model as `kubectl`, `terraform`, and `helm`.

## Selector grammar

A selector is one or more `key=value` pairs joined by commas. Pairs AND-combine across distinct keys.

```bash
siteops deploy manifests/aio-install.yaml -l environment=prod,region=eu
# Selects sites where labels.environment == "prod" AND labels.region == "eu".
```

`-l` is repeatable. Each invocation contributes `key=value` pairs that AND-combine with the others.

```bash
siteops deploy manifests/aio-install.yaml -l environment=prod -l region=eu
# Equivalent to the comma-joined form above.
```

### The `name` key

`name=` is the one selector key whose duplicate values OR-combine. Multi-site selection happens through repeated `name=` values.

```bash
siteops deploy manifests/aio-install.yaml -l name=munich-dev,name=seattle-dev
# Targets exactly munich-dev OR seattle-dev.
```

Duplicate values for any other key raise an error pointing at the conflict, since this is almost always a typo:

```bash
siteops deploy manifests/aio-install.yaml -l env=dev -l env=prod
# Error: Selector key `env` may only appear once. Selectors AND across
# keys, so duplicating a key would always match zero sites. Only `name=`
# supports multiple values (OR-combined).
```

### Path-form names

For sites under nested `sites/` subdirectories, `name=` accepts both the basename (filename without extension) and the relative path under the trusted dir. Both forms resolve to the same site.

```bash
siteops deploy manifests/aio-install.yaml -l name=munich-dev
siteops deploy manifests/aio-install.yaml -l name=regions/eu/munich-dev
# Both target the file at `sites/regions/eu/munich-dev.yaml`.
```

## Site identity

Each deployable site is reachable by three identifiers, all of which work in `-l name=`, in manifest `sites:` lists, and in `siteops sites <name>`:

| Form | Example | Notes |
|---|---|---|
| Basename | `munich-dev` | The filename without extension. The orchestrator enforces basename uniqueness across each trusted dir at load time. |
| Relative path | `regions/eu/munich-dev` | The path under the owning trusted dir, no extension. |
| Internal `name:` | `contoso-munich` | The value of the `name:` field if it differs from the basename. Must be unique workspace-wide. |

**Basename uniqueness.** Within any one trusted directory, every site basename must be unique across all subdirectories. The orchestrator rejects collisions at load time so `-l name=<basename>` always resolves to one file. Cross-dir collisions are valid only when the relative path also matches (the overlay pattern).

**Path normalization.** Path-form identifiers are normalized: backslashes become forward slashes, `..` and `./` segments are rejected, leading or trailing `/` is rejected. These rules apply to both manifest `sites:` entries and `-l name=` values.

## Library and partial manifests

A manifest with no `sites:` and no `selector:` is a library or partial. Standalone deployment requires `-l` to supply the target.

```yaml
# manifests/diagnostics.yaml
apiVersion: siteops/v1
kind: Manifest
name: diagnostics
description: Capture diagnostic snapshots from a single site on demand.
steps:
  - name: capture
    template: templates/diagnostics/capture.bicep
    scope: resourceGroup
```

```bash
siteops deploy manifests/diagnostics.yaml -l name=munich-prod
# Works. CLI supplies the targeting the manifest deferred.

siteops deploy manifests/diagnostics.yaml
# Error: declares no `sites:` or `selector:`, and no `-l/--selector`
# was provided. Either add targeting to the manifest, or pass
# `-l <key>=<value>` at deploy time.
```

Partials (filename prefixed `_`) compose into other manifests via `include:`. They almost always omit targeting on the assumption that the parent manifest sets it. See [manifest-includes.md](manifest-includes.md).

## No-match diagnostic

When a CLI selector matches zero sites, `deploy` exits non-zero with a diagnostic that lists what the workspace actually contains for each requested key. The diagnostic catches typos at the moment the operator runs the command.

```bash
siteops deploy manifests/aio-install.yaml -l environment=prdo
# Error: CLI selector `-l environment=prdo` matched no sites.
# `environment=prdo` requested. Workspace `environment` values: dev, prod, staging.
```

```bash
siteops deploy manifests/aio-install.yaml -l name=does-not-exist
# Error: CLI selector `-l name=does-not-exist` matched no sites.
# `name=does-not-exist` not found. Workspace site names:
# chicago-staging, contoso-global, munich-dev, munich-prod, seattle-dev, seattle-prod.
```

When the site name matches but another selector key knocks it out, the diagnostic says so:

```bash
siteops deploy manifests/aio-install.yaml -l name=munich-dev,environment=prod
# `name=munich-dev` matched a workspace site but another selector key
# filtered it out.
```

Manifest selectors that match zero sites warn but still exit zero.

## Validation

`siteops validate <manifest>` exercises the same parse and resolve paths as `deploy` without executing any steps.

- **Unknown manifest keys are rejected** with a `did you mean` hint sourced from the canonical list (`apiVersion`, `kind`, `name`, `description`, `sites`, `selector`, `parallel`, `parameters`, `steps`).
- **Selector parse errors** (duplicate non-`name` keys, malformed pairs) are surfaced as validation errors. They no longer mask other manifest issues. The operator sees every problem in one pass.
- **Library manifests pass validation** because no targeting is structurally OK. Add `-l` when running `validate` to exercise the resolve path against real sites.

## Pitfalls

- **`-l env=prod -l env=dev` errors.** Selectors AND across keys, so duplicating a non-name key would always match zero sites. To target multiple cohorts, run two commands or add a label that spans them.
- **`-l name=path/to/site` works but is rare in practice.** The basename form is shorter and just as unambiguous when the basename invariant holds.
- **Adding a nested site that collides on basename fails the workspace load.** Rename one of the colliding files. The error message names both paths.
- **An overlay in `sites.local/` cannot rename a site.** It may restate the same `name:` (common when the overlay mirrors the base shape) but cannot change it. The same rule applies to a file in an extras dir that overlays a base file at the same path under `sites/`. Use `inherits:` or rename the base file instead.

## Related

- [site-configuration.md](site-configuration.md). The site object, inheritance, overlays, extras directories.
- [manifest-reference.md](manifest-reference.md). Manifest shape, step types, conditions.
- [manifest-includes.md](manifest-includes.md). Partials and `include:` composition.

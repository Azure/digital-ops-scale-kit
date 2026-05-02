# manifests/

Standalone manifests and their internal partials for the AIO platform.

## Files

| File | Kind | Deployable | Purpose |
|------|------|------------|---------|
| `aio-install.yaml` | Standalone | yes | Day-1 AIO platform install. Composes `_aio-fundamentals.yaml` and conditionally adds secret sync. |
| `aio-upgrade.yaml` | Standalone | yes | In-place AIO upgrade to the site's current `aioRelease`. |
| `secretsync.yaml` | Standalone | yes | Day-2 enable secret sync on an existing AIO install. |
| `_aio-fundamentals.yaml` | Partial | no | Arc extensions, custom location, instance, schema registry, ADR namespace, plus optional global/edge sites. |
| `_resolve-aio.yaml` | Partial | no | Reads instance and custom-location names from the existing AIO instance for downstream chaining. |
| `_secretsync.yaml` | Partial | no | Workload-identity-backed secret sync step. |

## Conventions

- **`_` prefix** marks an internal partial. Not deployed directly. Composed via `include:`. The `test_partial_files_use_underscore_prefix` workspace test enforces this (a manifest authored to be included must start with `_`).
- **Standalone manifests** are convenience entry points for `siteops deploy`. They re-include the partials they depend on.
- **Composed manifests live in `scenarios/`**, not here. A scenario that pulls in two standalone manifests will collide on shared step names (e.g. `resolve-aio`). Compose the underlying `_partial.yaml` files instead.

## Authoring a new partial

1. Name the file `_<topic>.yaml` (leading underscore).
2. Set `kind: Manifest`. The engine has no separate Partial kind.
3. Include only the steps that ARE the topic. Do not pull prerequisites; the parent decides ordering.
4. If the partial needs values from upstream steps, reference them as `{{ steps.<name>.outputs.<key> }}` and document the expected upstream step in the description.

See `docs/manifest-includes.md` for the full include contract.

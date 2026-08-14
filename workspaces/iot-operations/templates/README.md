# templates/

Bicep templates referenced by manifest steps via the `template:` field.

## Subdirs

| Subdir | What it holds |
|--------|---------------|
| `aio/` | The AIO resource provider. Platform lifecycle (enablement, instance, resolve-aio, upgrade phases) plus workload configuration subareas such as `aio/dataflows/`. Platform templates are versioned: per-release modules under `aio/modules/` and a top-level dispatcher that switches on the AIO API version. |
| `common/` | Shared bicep modules used across multiple top-level templates (e.g. `extension-names.bicep`, the single source of truth for AIO/cert-manager/secret-store extension naming). |
| `deps/` | AIO dependencies: schema registry, ADR namespace, role assignments. |
| `edge-site/` | Edge site resources (subscription-scoped global site, RG-scoped per-cluster site). |
| `host-bootstrap/` | Host provisioning delivered through Arc Run Command, one subdir per implementation (`aksee/`). |
| `host-ops/` | Day-2 host operations, one subdir per operation (`aksee-upgrade/`). |
| `secretsync/` | Workload-identity-backed secret sync enablement and the secret sync data path. |

## Conventions

- **Family entry points.** A workload-configuration subarea such as `aio/dataflows/` ships a `main.bicep` that routes on `aioApiVersion` to one module per supported AIO API version. Manifest steps point at `main.bicep`, so a site pays one deployment round trip per family and its resources are written at the API version its release ships. Each module declares its resources with literal type and API version strings, which is what makes Bicep resolve them against the provider schema. `properties` passes through as an object, so the workspace tests are what validate its contents.
- **Versioned dispatchers** are introduced only when an API version actually diverges. Default to a single template. On the first breaking API change, split the area into a top-level dispatcher (e.g. `aio/instance.bicep`) plus per-API-version inner modules under `<area>/modules/<api-version>.bicep`.
- **`existing` resource lookups** must use the shared deriver from `common/extension-names.bicep` so install and upgrade resolve to the same names.
- **API version pins** for samples follow the policy in `docs/aio-releases.md` ("Sample template API-version policy"): pin to the oldest supported API version. The `test_samples_pin_to_oldest_api_version` workspace test enforces this for `samples/<name>/template.bicep`.
- **Outputs** declared in a Bicep template have to cover the `{{ steps.<step>.outputs.<key> }}` references that the matching `parameters/outputs/<step>.yaml` makes (when the step's outputs flow downstream). That file is a flat mapping of the consumer's parameter name to the producer's output path. The chaining tests in `tests/workspace/test_parameter_chaining.py` check that each consumer reference resolves to an output a producing step actually emits.

## Authoring a new template

1. Pick the smallest existing subdir that fits the resource type.
2. Declare `@description(...)` on every `param`. It documents the contract for anyone reading or calling the template.
3. If the template needs to identify an existing AIO/cert-manager/secret-store extension, import `common/extension-names.bicep` rather than re-deriving the name.

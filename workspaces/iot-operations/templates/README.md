# templates/

Bicep templates referenced by manifest steps via the `template:` field.

## Subdirs

| Subdir | What it holds |
|--------|---------------|
| `aio/` | The AIO platform (enablement, instance, resolve-aio, upgrade phases). Versioned: per-release modules under `aio/modules/` and a top-level dispatcher that switches on the AIO API version. |
| `common/` | Shared bicep modules used across multiple top-level templates (e.g. `extension-names.bicep`, the single source of truth for AIO/cert-manager/secret-store extension naming). |
| `deps/` | AIO dependencies: schema registry, ADR namespace, role assignments. |
| `edge-site/` | Edge site resources (subscription-scoped global site, RG-scoped per-cluster site). |
| `secretsync/` | Workload-identity-backed secret sync enablement. |

## Conventions

- **Versioned dispatchers** are introduced only when an API version actually diverges. Default to a single template. On the first breaking API change, split the area into a top-level dispatcher (e.g. `aio/instance.bicep`) plus per-API-version inner modules under `<area>/modules/<api-version>.bicep`.
- **`existing` resource lookups** must use the shared deriver from `common/extension-names.bicep` so install and upgrade resolve to the same names.
- **API version pins** for samples follow the policy in `docs/aio-releases.md` ("Sample template API-version policy"): pin to the oldest supported API version. The `test_samples_pin_to_oldest_api_version` workspace test enforces this for `samples/<name>/template.bicep`.
- **Outputs** declared in a Bicep template must match the `outputs:` section of the matching `parameters/outputs/<step>.yaml` (when the step's outputs flow downstream). The `test_step_output_shape` workspace test enforces this.

## Authoring a new template

1. Pick the smallest existing subdir that fits the resource type.
2. Declare `@description(...)` on every `param`. The CLI prints these in `siteops validate` output and `--render` previews.
3. If the template needs to identify an existing AIO/cert-manager/secret-store extension, import `common/extension-names.bicep` rather than re-deriving the name.

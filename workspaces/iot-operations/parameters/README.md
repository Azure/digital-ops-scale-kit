# parameters/

Parameter YAML files referenced by manifest steps. All paths in this dir are workspace-relative when listed in a manifest's `parameters:` or a step's `parameters:`.

## Subdirs

| Subdir | What it holds |
|--------|---------------|
| `common/` | Workspace-wide defaults applied to every step (e.g. `parameters/common/common.yaml`). |
| `inputs/` | Per-step **fan-in** files: `<step>.yaml` wires upstream step outputs into the named step's parameters. |
| `outputs/` | Per-step **fan-out** files: `<step>.yaml` exposes a step's outputs for downstream consumers. |
| `aio-releases/` | Release pinning. One YAML per AIO release (e.g. `2603.yaml`) with the API and extension versions for that release. The site's `properties.aioRelease` selects which file is loaded. |

## Conventions

- **Auto-filtering**: the engine drops any parameter key the target Bicep template does not declare. This lets a single `inputs/<step>.yaml` cover multiple template versions without per-version duplication.
- **Filename = step name**: `parameters/inputs/<step>.yaml` and `parameters/outputs/<step>.yaml` are conventionally named after the step they wire, even when the file is consumed at the manifest level.
- **Header comments**: each parameters file should declare in a header what it produces or consumes ("Fan-in for X step", "Fan-out from X step consumed by Y").
- **Common dedup**: values already provided by `base-site.yaml` (e.g. `managedBy: siteops`) should not be re-declared in `common/common.yaml`.

See `docs/parameter-resolution.md` for the full merge precedence (manifest → site → step) and the auto-filtering algorithm.

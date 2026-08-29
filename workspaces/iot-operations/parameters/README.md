# parameters/

Parameter YAML files referenced by manifest steps. All paths in this dir are workspace-relative when listed in a manifest's `parameters:` or a step's `parameters:`.

## Subdirs

| Subdir | What it holds |
|--------|---------------|
| `common/` | Workspace-wide defaults applied to every step (e.g. `parameters/common/common.yaml`). |
| `inputs/` | Per-step **fan-in** files: `<step>.yaml` wires upstream step outputs into the named step's parameters. |
| `outputs/` | Per-step **fan-out** files: `<step>.yaml` exposes a step's outputs for downstream consumers. |
| `aio-releases/` | Release pinning. One YAML per AIO release (e.g. `2608.yaml`) with the API, extension versions, and release-specific AIO configuration. The site's `properties.aioRelease` selects which file is loaded. |
| `devices/` | Device definition sets selected by `properties.resourceSets.devices`. `site-devices.yaml` is the worked OPC UA device set. |
| `assets/` | Asset definition sets selected by `properties.resourceSets.assets`. `site-assets.yaml` is the worked per-site asset set. |
| `dataflows/` | Endpoint, profile, and dataflow definition sets selected by `properties.resourceSets.dataflows`. `site-telemetry.yaml` is the worked per-site routing set. |

## Conventions

- **Auto-filtering**: the engine drops any parameter key the target Bicep template does not declare. This lets a single `inputs/<step>.yaml` cover multiple template versions without per-version duplication.
- **Filename = step name**: `parameters/inputs/<step>.yaml` and `parameters/outputs/<step>.yaml` are conventionally named after the step they wire, even when the file is consumed at the manifest level. One file may instead serve a class of steps that read the same upstream values, named for what they share: `inputs/catalog.yaml` is the fan-in every resource catalog family step reads.
- **Header comments**: each parameters file should declare in a header what it produces or consumes ("Fan-in for X step", "Fan-out from X step consumed by Y").
- **Common dedup**: values already provided by `base-site.yaml` (e.g. `managedBy: siteops`) should not be re-declared in `common/common.yaml`.

See `docs/parameter-resolution.md` for the full merge precedence (manifest → site → step) and the auto-filtering algorithm.

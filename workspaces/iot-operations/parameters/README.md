# parameters/

Parameter YAML files referenced by manifest steps. All paths in this directory are relative to the workspace when listed in a manifest's `parameters:` or a step's `parameters:`.

## Subdirs

| Subdir | What it holds |
|--------|---------------|
| `common/` | Defaults for the whole workspace, applied to every step (for example `parameters/common/common.yaml`). |
| `inputs/` | **Input** files for each step: `<step>.yaml` wires upstream step outputs into the named step's parameters. |
| `outputs/` | **Output** files for each step: `<step>.yaml` exposes a step's outputs for downstream consumers. |
| `aio-releases/` | AIO release pinning. One YAML per AIO release (for example `2608.yaml`) with the API, extension versions, and AIO configuration specific to that AIO release. The Site's `properties.aioRelease` selects which file is loaded. |

Reusable workload declarations live in the
[resource set library](../resource-sets/README.md). `inputs` and `outputs`
both contain bindings consumed by steps. Their names distinguish the values a
step consumes from the values it shares, not separate execution channels or
stored runtime outputs.

## Conventions

- **Automatic filtering**: the engine drops any parameter key the receiving Bicep template does not declare. This lets a single `inputs/<step>.yaml` cover multiple template versions without a copy for each version.
- **Filename = step name**: `parameters/inputs/<step>.yaml` and `parameters/outputs/<step>.yaml` are conventionally named after the step they wire. Both attach at the consuming step. One file may instead serve a class of steps that read the same upstream values, named for what they share: `inputs/catalog.yaml` is the input file every resource catalog family step reads.
- **Header comments**: each parameters file should declare in a header what it produces or consumes ("Inputs to the X step", "Outputs of the X step, consumed by Y").
- **Shared defaults**: reuse a parameter's existing default instead of copying it into each file. Site labels and resource tags are separate fields, even when they carry similar information.

See `docs/parameter-resolution.md` for the full merge precedence (manifest → Site → step) and the automatic filtering algorithm.

# Samples

Self-contained workload bundles deployable on top of an existing Azure IoT Operations install. Each sample is its own directory with a fixed file convention.

## Directory layout

```
samples/<name>/
├── manifest.yaml     User entry point. Standalone deployable.
├── _partial.yaml     Internal partial. Composed by the manifest above and by scenarios.
├── template.bicep    The sample's Bicep template.
├── inputs.yaml       Step output to step input wiring (consumer fan-in).
└── outputs.yaml      Optional. Sample step outputs forwarded to downstream consumers.
```

## File conventions

- **`manifest.yaml`** is the user-facing entry point. Deploy with `siteops -w workspaces/iot-operations deploy samples/<name>/manifest.yaml`. Composes `_partial.yaml` plus any prerequisite steps the standalone deployment needs (e.g., `_resolve-aio.yaml` reads names from an existing AIO instance).
- **`_partial.yaml`** holds only the steps that ARE the sample. The leading `_` marks it as an internal partial not intended for direct deployment. Composed by `manifest.yaml` and by scenarios under `scenarios/`.
- **`template.bicep`** is the sample's deployment template. Pinned to the oldest supported AIO and ADR API versions per `docs/aio-releases.md` (Sample template API-version policy).
- **`inputs.yaml`** wires upstream step outputs into the sample's step parameters. Co-located with the sample (not in the workspace-root `parameters/inputs/` dir).
- **`outputs.yaml`** (optional) is the producer-side fan-out file when the sample's step outputs are consumed elsewhere. Same shape as `parameters/outputs/`.

## Adding a new sample

1. Create `samples/<name>/`.
2. Add `template.bicep` with your sample's resources. Pin Microsoft.IoTOperations and Microsoft.DeviceRegistry references to the oldest supported API version (the workspace test `test_samples_pin_to_oldest_api_version` enforces this).
3. Add `inputs.yaml` with `{{ steps.X.outputs.Y }}` references for any values the template needs from upstream steps.
4. Add `_partial.yaml` containing the sample steps (no `resolve-aio`, no other prerequisites).
5. Add `manifest.yaml`. For a sample that needs `resolve-aio`, include `_resolve-aio.yaml` from `manifests/` and then include `_partial.yaml`.
6. Optionally add an integration test under `tests/integration/test_<name>_manifest.py`.
7. Optionally compose into `scenarios/<combo>.yaml` to demonstrate the sample alongside other deployments.

### Scaling beyond a single file

Real samples may exceed the shape above. Conventions:

- **Multiple Bicep files**: `template.bicep` is the entry template called by `_partial.yaml`. Helper templates go under `samples/<name>/modules/`, mirroring `templates/<area>/modules/`.
- **Multiple input files**: prefer one shared `inputs.yaml` for the whole sample; auto-filtering routes the right keys to each step. If steps need genuinely disjoint inputs, name them `samples/<name>/<step>.yaml` to mirror `parameters/inputs/`.
- **Sample-local outputs**: `outputs.yaml` next to `inputs.yaml`, consumed by other samples or scenarios via `{{ steps.<sample-step>.outputs.<key> }}`.

## Composing samples in scenarios

Scenarios under `scenarios/` should `include:` the sample's `_partial.yaml` (not the standalone `manifest.yaml`). Standalone manifests re-include `_resolve-aio.yaml` so they can be deployed on their own. Composing two standalone manifests in one scenario will collide on the `resolve-aio` step name. See `scenarios/README.md` for the full composition rules.


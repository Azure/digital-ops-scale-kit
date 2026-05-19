# Samples

Deployable examples for Azure IoT Operations. Two shapes are supported.

- **Self-contained workload bundle.** A sample directory carries its own Bicep template, input wiring, and a manifest that composes them on top of an existing AIO install. Example: `secretsync-sample`.
- **Composition.** A sample directory carries just a manifest (and a small README) that `include:`s leaf partials from `manifests/` and other samples into one end-to-end deploy. Example: `aio-with-opc-ua`.

Both shapes are deployable from the same path convention:

```bash
siteops -w workspaces/iot-operations deploy samples/<name>/manifest.yaml -l environment=dev
```

## Bundle layout (self-contained shape)

```
samples/<name>/
├── manifest.yaml     User entry point. Standalone deployable.
├── _partial.yaml     Internal partial. Composed by the manifest above and by other samples.
├── template.bicep    The sample's Bicep template.
├── inputs.yaml       Step output to step input wiring (consumer fan-in).
└── outputs.yaml      Optional. Sample step outputs forwarded to downstream consumers.
```

## File conventions

- **`manifest.yaml`** is the user-facing entry point. Composes `_partial.yaml` plus any prerequisite steps the standalone deployment needs (e.g., `_resolve-aio.yaml` reads names from an existing AIO instance).
- **`_partial.yaml`** holds only the steps that ARE the sample. The leading `_` marks it as an internal partial not intended for direct deployment. Composed by `manifest.yaml` and by compositional samples.
- **`template.bicep`** is the sample's deployment template. Pinned to the oldest supported AIO and ADR API versions per `docs/aio-releases.md` (Sample template API-version policy).
- **`inputs.yaml`** wires upstream step outputs into the sample's step parameters. Co-located with the sample (not in the workspace-root `parameters/inputs/` dir).
- **`outputs.yaml`** (optional) is the producer-side fan-out file when the sample's step outputs are consumed elsewhere. Same shape as `parameters/outputs/`.

## Adding a new self-contained sample

1. Create `samples/<name>/`.
2. Add `template.bicep` with your sample's resources. Pin Microsoft.IoTOperations and Microsoft.DeviceRegistry references to the oldest supported API version (the workspace test `test_samples_pin_to_oldest_api_version` enforces this).
3. Add `inputs.yaml` with `{{ steps.X.outputs.Y }}` references for any values the template needs from upstream steps.
4. Add `_partial.yaml` containing the sample steps (no `resolve-aio`, no other prerequisites).
5. Add `manifest.yaml`. For a sample that needs `resolve-aio`, include `_resolve-aio.yaml` from `manifests/` and then include `_partial.yaml`.
6. Optionally add an integration test under `tests/integration/test_<name>_manifest.py`.
7. Optionally compose into `samples/<combo>/manifest.yaml` to demonstrate the sample alongside other deployments. See the next section.

### Scaling beyond a single file

Real samples may exceed the shape above. Conventions:

- **Multiple Bicep files**: `template.bicep` is the entry template called by `_partial.yaml`. Helper templates go under `samples/<name>/modules/`, mirroring `templates/<area>/modules/`.
- **Multiple input files**: prefer one shared `inputs.yaml` for the whole sample. Auto-filtering routes the right keys to each step. If steps need genuinely disjoint inputs, name them `samples/<name>/<step>.yaml` to mirror `parameters/inputs/`.
- **Sample-local outputs**: `outputs.yaml` next to `inputs.yaml`, consumed by other samples via `{{ steps.<sample-step>.outputs.<key> }}`.

## Composing samples

A compositional sample is its own `samples/<name>/` directory with one `manifest.yaml` (and a short README) that pulls in leaf partials. Example:

```yaml
# samples/aio-with-opc-ua/manifest.yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-with-opc-ua
description: AIO platform + OPC UA sample.
selector: "environment=dev"
steps:
  - include: ../../manifests/_aio-fundamentals.yaml
  - include: ../../manifests/_resolve-aio.yaml
  - include: ../opc-ua-solution/_partial.yaml
```

Omit `_resolve-aio.yaml` when the composition has no downstream consumer of the resolved instance and custom-location names. The OPC UA sample needs them, so it stays in.

### Composition rules

1. **Compose partials, not standalone manifests.** `manifests/aio-install.yaml` and `samples/<name>/manifest.yaml` are standalone entry points that re-include `_resolve-aio.yaml` so they can be deployed on their own. Composing two of them in one parent will collide on the `resolve-aio` step name. Compose the underlying `_partial.yaml` files instead.
2. **Step names must be unique** across the post-include flat step list. Collision is a parse-time error.
3. **Site selectors and parallel settings** declared on the composing manifest apply at the composition level. The same fields on included partials are silently ignored.

## Samples in this workspace

| Sample | Shape | Composes |
|---|---|---|
| `secretsync-sample/` | Bundle | template + inputs + partial |
| `opc-ua-solution/` | Bundle | template + inputs + partial |
| `aio-with-opc-ua/` | Composition | `_aio-fundamentals` + `_resolve-aio` + `opc-ua-solution/_partial` |

See each sample's own `README.md` for what it deploys, prerequisites, and how to configure before deploying.

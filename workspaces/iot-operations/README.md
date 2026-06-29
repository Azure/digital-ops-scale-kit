# IoT Operations workspace

This workspace deploys [Azure IoT Operations](https://learn.microsoft.com/en-us/azure/iot-operations/) at fleet scale through the `siteops` engine.

The workspace is one tenant of the engine. The engine itself is workspace-agnostic. The field names under `properties.deployOptions.*`, the label keys (`environment`, `country`, `scope`), and the AIO-specific manifests are conventions of this workspace.

## Layout

| Directory | Purpose |
|---|---|
| `sites/` | Per-cluster deployment targets (subscription, resource group, labels, parameters, properties). Sites can live at any depth. Basenames must be unique within this directory. |
| `manifests/` | Ordered step lists keyed off site selection. `aio-install.yaml`, `aio-upgrade.yaml`, `secretsync.yaml`, `aksee-bootstrap.yaml` are the standalone entry points. Files prefixed `_` are partials composed via `include:`. Partials live in `manifests/` when they are cross-cutting (reused by multiple standalones) or under `templates/<area>/<impl>/_partial.yaml` when they are the wire-up for a single self-contained implementation. |
| `samples/` | Deployable examples. Each `samples/<name>/` is a single example. Two shapes are supported: self-contained workload bundles (manifest + partial + template + inputs) and compositions (a manifest that `include:`s leaf partials from `manifests/` and other samples). See `samples/README.md`. |
| `parameters/` | Template variables, fan-in/fan-out chaining, AIO release pins. See `parameters/README.md`. |
| `templates/` | Bicep templates organized by area (`aio/`, `secretsync/`, `edge-site/`, `deps/`, `common/`, `host-bootstrap/`). Versioned per AIO release via dispatchers under `<area>/modules/`. Implementations under `templates/host-bootstrap/<impl>/` may also include host-runtime scripts and a `_partial.yaml` alongside the Bicep template. |

## Common tasks

```bash
# Run from the repo root. siteops auto-discovers this workspace
# under `./workspaces/`. Pass `-w workspaces/iot-operations` to be
# explicit.
siteops -w workspaces/iot-operations sites
siteops -w workspaces/iot-operations validate manifests/aio-install.yaml
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l environment=dev
```

See the [repo README](../../README.md) for a full command tour and [docs/](../../docs/) for the engine reference.

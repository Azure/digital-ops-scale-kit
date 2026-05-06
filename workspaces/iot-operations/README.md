# IoT Operations workspace

This workspace deploys [Azure IoT Operations](https://learn.microsoft.com/en-us/azure/iot-operations/) at fleet scale through the `siteops` engine.

The workspace is one tenant of the engine. The engine itself is workspace-agnostic; the field names under `properties.deployOptions.*`, the label keys (`environment`, `country`, `scope`), and the AIO-specific manifests are conventions of this workspace.

## Layout

| Directory | Purpose |
|---|---|
| `sites/` | Per-cluster deployment targets (subscription, resource group, labels, parameters, properties). Sites can live at any depth; basenames must be unique within this directory. |
| `manifests/` | Ordered step lists keyed off site selection. `aio-install.yaml`, `aio-upgrade.yaml`, `secretsync.yaml` are the standalone entry points. Files prefixed `_` are partials composed via `include:`. |
| `scenarios/` | Compositions of partials. Each scenario is a deployable manifest assembled from `_partial.yaml` files in `manifests/` and `samples/`. |
| `samples/` | Self-contained workload bundles (manifest + partial + inputs + Bicep template). One bundle per directory. |
| `parameters/` | Template variables, fan-in/fan-out chaining, AIO release pins. See `parameters/README.md`. |
| `templates/` | Bicep templates organized by area (`aio/`, `secretsync/`, `edge-site/`, `deps/`, `common/`). Versioned per AIO release via dispatchers under `<area>/modules/`. |

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

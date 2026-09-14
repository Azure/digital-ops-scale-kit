# Host bootstrap

Implementations of host-side bootstrap for Azure IoT Operations targets. Each implementation takes a bare host through the install, cluster, and Arc enablement work that AIO needs as a prerequisite, then hands off to the standard AIO deploy chain.

## Layout

| Path | Role |
|---|---|
| `<impl>/template.bicep` | The entry template invoked by the implementation's `_partial.yaml`. |
| `<impl>/_partial.yaml` | Partial that wires the template into a deployable step. Composed by `manifests/<name>-bootstrap/manifest.yaml` and examples such as `samples/aio-with-<name>-bootstrap/`. |
| `<impl>/scripts/` | Host-runtime artifacts the template delivers (e.g., PowerShell scripts inlined via `loadTextContent`). Empty for implementations that deliver only ARM resources. |
| `<impl>/README.md` | Implementation ownership and a link to the public entry's operator guide. |
| `<impl>/scripts/README.md` | Dev workflow for the scripts (regeneration, local testing). Present only when an implementation ships scripts. |

## Implementations

| Implementation | Target | Cluster | Status |
|---|---|---|---|
| [`aksee/`](aksee) | Windows host | AKS Edge Essentials single-node K3s | Available |

## Adding a new implementation

1. Create `<impl>/`.
2. Add `template.bicep` (the entry template invoked by the partial below).
3. Add `_partial.yaml` that wires the template into a deployable step.
4. If the implementation delivers host-runtime artifacts, add `<impl>/scripts/` with the artifacts and a `<impl>/scripts/README.md` that documents the dev workflow.
5. Keep implementation guidance beside the source, and give the public entry its operator guide covering prerequisites, configuration, deployment, monitoring and recovery.
6. Add `manifests/<name>-bootstrap/manifest.yaml` with the implementation partial and a `type: wait` step for the worker's completion tag. Keep input wiring under `parameters/inputs/<name>-bootstrap.yaml`. Launcher registration alone does not establish cluster readiness.
7. Optionally add a composition sample at `samples/aio-with-<name>-bootstrap/` that demonstrates bootstrap + AIO install in one deploy.
8. Add this row to the implementations table above.

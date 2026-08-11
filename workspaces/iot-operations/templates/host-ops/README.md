# Host operations

Day-2 operations against a host that is already bootstrapped and Arc-enabled.
Where `host-bootstrap/` takes a bare host to a ready cluster, an operation here
changes a host that is already running a workload, so each one has to survive
the AIO installation already on the cluster.

One subdirectory per operation, laid out the same way as a bootstrap
implementation.

## Layout

| Path | Role |
|---|---|
| `<operation>/template.bicep` | The entry template invoked by the operation's `_partial.yaml`. |
| `<operation>/_partial.yaml` | Partial that wires the template into a deployable step. Composed by the standalone `manifests/<operation>.yaml`. |
| `<operation>/scripts/` | Host-runtime artifacts the template delivers, inlined with `loadTextContent`. |
| `<operation>/README.md` | Operator-facing docs for the operation. |
| `<operation>/scripts/README.md` | Dev workflow for the scripts, including launcher regeneration. |

## Operations

| Operation | What it does | Status |
|---|---|---|
| [`aksee-upgrade/`](aksee-upgrade) | Upgrades an AKS Edge Essentials node in place, hop by hop, and reconnects it to Arc. | Validated on a nested-virtualization Arc Windows host. |

## What an operation has to get right

- **Run Command success is not workload completion.** It proves the launcher
  returned. Gate later steps on the asynchronous worker's own completion signal.
- **Clear the terminal tag synchronously before starting**, and record a
  per-deploy run identifier. A stale `succeeded` or `failed-*` tag from an
  earlier run otherwise satisfies a later wait.
- **Generated launchers are built, not edited.** `Build-Launcher.ps1` embeds
  `worker.ps1` into the installer and a minified variant. Edit the source and
  rebuild. CI rebuilds and fails on a difference.
- **Platform health is not application health.** An upgrade that reports a
  Ready node and a connected Arc agent has not shown that AIO survived. Verify
  the workload separately.

## Adding an operation

1. Create `<operation>/` with `template.bicep` and `_partial.yaml`.
2. Add `scripts/` with the host-runtime artifacts and a `scripts/README.md`.
3. Add `<operation>/README.md` covering prerequisites, configuration, run,
   monitor, verify, and troubleshoot.
4. Add a standalone entry point at `manifests/<operation>.yaml` that includes
   the partial and then waits on the completion tag the worker writes.
5. Register the manifest in the deploy dropdowns on both CI platforms.
6. Add a row to the operations table above.

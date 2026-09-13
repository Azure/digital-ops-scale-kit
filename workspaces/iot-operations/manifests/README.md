# manifests/

Core operations for the AIO workspace. Each named directory contains its
`manifest.yaml` and operator guide. Examples use the same entry shape under
[`samples`](../samples/README.md).

Optional `entry.yaml` describes the operator contract for `siteops browse`.
Other manifest filenames use `<stem>.entry.yaml`. Keep those facts with their
entry, rather than duplicating an inventory of cards. See the
[authoring contract](../../../docs/browse-content.md#author-an-entry).

## Files

| Operation | Purpose |
|---|---|
| [aio-install](aio-install/README.md) | Install AIO on an existing Arc-connected cluster |
| [aio-upgrade](aio-upgrade/README.md) | Upgrade an existing installation to its selected AIO release |
| [aio-resources](aio-resources/README.md) | Apply Site-selected devices, assets and dataflows |
| [secretsync](secretsync/README.md) | Enable Secret Sync on an existing instance |
| [aksee-bootstrap](aksee-bootstrap/README.md) | Bootstrap an AKS Edge Essentials host and wait for completion |
| [aksee-upgrade](aksee-upgrade/README.md) | Upgrade an existing AKS Edge Essentials cluster |

Shared orchestration fragments live under `_partials/`. They are composed by
entries, rather than presented as standalone operator choices. Sample-local
and host-implementation partials stay beside the material they compose.

## Conventions

- **`_` prefix** marks an internal partial. Not deployed directly. Composed via `include:`. The `test_partial_manifests_use_underscore_prefix` workspace test enforces this (a manifest authored to be included must start with `_`).
- **Standalone manifests** live at `<name>/manifest.yaml`. Give each one an explicit `name`, its own operator guide, and references to shared implementation.
- **Composed manifests live in `samples/<name>/manifest.yaml`**, next to the partials they compose. A composition that pulls in two standalone manifests will collide on shared step names (e.g. `resolve-aio`). Compose the underlying `_partial.yaml` files instead. See `samples/README.md` for the full composition rules.

## Authoring a new partial

1. Put a shared fragment at `_partials/_<topic>.yaml`. Keep implementation-local fragments with their owner.
2. Set `kind: Manifest`. The engine has no separate Partial kind.
3. Include only the steps that ARE the topic. Do not pull prerequisites. The parent decides ordering.
4. If the partial needs values from upstream steps, reference them as `{{ steps.<name>.outputs.<key> }}` and document the expected upstream step in the description.

See [manifest includes](../../../docs/manifest-includes.md) for the full contract.

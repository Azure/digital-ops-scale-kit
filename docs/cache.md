# Inspect and remove cached content

Use cache commands to see retained storage, reclaim space or remove a corrupt
entry before restoring its exact content. They operate on the
[Site Ops cache root](workspace-packages.md#internal-workspace-cache), selected
by `SITEOPS_CACHE_DIR` or the platform default.

```text
siteops cache list
siteops cache list --kind package
siteops cache list --output json
```

Inspection makes no source requests and does not run a provenance verifier.
It reports storage layout, not content integrity or publisher trust. A
`stored` entry still needs ordinary verification before use.

## Read the inventory

| Kind | ID | Included bytes |
|---|---|---|
| `package` | Expected package SHA-256 | Retained ZIP, extracted content and that package's verification receipts |
| `proof` | Expected proof SHA-256 | Retained detached proof |
| `metadata` | Hash of the provider/access/kind/identity key | One source observation, tree or blob record |

IDs are complete lowercase values from the inventory. Metadata IDs identify
records, not their payload digests or a project name. The commands do not
accept filesystem paths as entry IDs.

Storage states are `stored`, `incomplete`, `busy`, `missing` or `unavailable`.
Byte counts are logical file sizes, not filesystem allocation measurements.
Busy or unavailable entries have unknown size. Inspection reports an entry
as busy rather than racing a running operation or receipt update.

The default list limit is 100. Narrow a large inventory or inspect one entry:

```text
siteops cache list --kind proof --limit 20
siteops cache list --kind metadata --id <complete-entry-id>
```

The result reports matching and shown counts, with `hasMore` in JSON.
Inspection has explicit entry, node and depth bounds. An unavailable entry
reports its issue code and makes the command return nonzero. The cache root
is not created merely to inspect an empty cache.

## Remove one selected entry

Copy the complete ID from the inventory and replace the placeholder:

```text
siteops cache remove package <complete-package-sha256>
siteops cache remove proof <complete-proof-sha256>
siteops cache remove metadata <complete-metadata-entry-id>
```

Removal requires the entry's exclusive lease. An active operation reports
`cache.busy` and leaves the entry untouched. Package removal includes its
receipts, while shared proofs and source metadata remain separate entries.
Proof or metadata removal does not remove a package.

Project pins, Site configuration, trust inputs and deployed resources remain
unchanged. Lock files remain in place so later operations use the same lock
identity. There is no automatic pruning or whole-cache removal command.

Removal checks the complete selected entry before deleting any of it.
Filesystem aliases, links, shared access, special files and detected mount
boundaries are refused rather than followed or repaired. These controls
coordinate ordinary Site Ops operations, not another process acting as the
same user or administrator.

A filesystem failure during deletion reports `cache.remove-incomplete`.
Resolve the file access issue and repeat removal of that same kind and ID.
An incomplete entry is not accepted for ordinary package use. The command
never silently repairs or merges content into an existing entry.

## Restore after removal

For a package or proof, repeat the project command without `--offline`.
Site Ops restores only the exact selection recorded by the workspace pin,
under current consumer policy. If the release no longer matches or the bytes
are unavailable, restoration fails rather than choosing different content.

For descriptive metadata, repeat the corresponding source browse without
`--offline`. Removing a record does not change any workspace pin.

Removal can make a previously usable offline project require source access.
Keep needed entries when their source may be unavailable, and avoid removing
retained evidence you need for independent auditing.

Cache output contains local storage details and requires an authorized private
destination. Cache commands do not use `--project`, `-w`, trust inputs or
extra Site directory arguments. They use the cache directory setting alone.

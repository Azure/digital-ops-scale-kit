# Browse a published source

Browse a source's published deployment descriptions without cloning its
repository. The source must contain a generated Site Ops index.
Replace `<owner>` and `<repository>` in these examples:

```bash
siteops browse --source github:<owner>/<repository>
siteops browse aio-install --source github:<owner>/<repository>
siteops browse --source github:<owner>/<repository> --category core
siteops browse --source github:<owner>/<repository> --category sample --tag mqtt
```

A root GitHub repository URL also works. Use `--ref` to select a branch, tag
or commit, or append `@<ref>` to the `github:` locator. Without a ref, Site Ops
resolves the repository's default branch rather than assuming its name.

```bash
siteops browse --source https://github.com/<owner>/<repository> --ref <branch-or-tag>
```

The command resolves one exact commit, then reads that commit's tree and
immutable index blobs. The source and revision remain visible in plain and
JSON output. Documentation links point at that same revision.

## Choose a workspace and entry

A source with one index is selected automatically. If several workspaces
publish indexes, the command lists their source-relative paths and asks for
an explicit selection:

```bash
siteops -w workspaces/iot-operations browse --source github:<owner>/<repository>
```

With `--source`, `-w` selects a path inside that source, not a local directory.
Names, paths, filters, category labels and partial visibility follow
[local browsing](browse-content.md). The source owns categories such as
`core` and `sample`. They do not establish trust or qualification.

An index can serve a large collection without downloading every manifest.
Source validation uses a pinned tree and index blobs, not a separate content
request for every entry.

## Use cached metadata with `browse`

Remote browsing retains source observations, trees and index blobs in the
private Site Ops cache. Repeat the same browse command to reuse them.
Branch, tag and default branch observations can be reused for five minutes.
After that interval, an ordinary browse resolves the reference again.

Use `--refresh` to resolve it immediately, or `--offline` to select retained
metadata without contacting the source:

```bash
siteops browse --source github:<owner>/<repository>
siteops browse --source github:<owner>/<repository> --refresh
siteops browse --source github:<owner>/<repository> --offline
```

Keep the same `--ref`, `--auth` and source workspace selection across these
commands. A fully cached commit can also be selected directly:

```bash
siteops browse --source github:<owner>/<repository> --ref <full-commit-sha> --offline
```

Selecting the complete commit displayed by an earlier browse reuses that
snapshot without resolving its branch again. A reference containing exactly
40 hexadecimal characters is a commit identity and must resolve to that same
commit. Qualify a named Git reference explicitly when needed, for example
`--ref refs/heads/<branch>`.

Plain and JSON output report whether the reference observation came from the
source or cache, its observation time, and when a mutable reference needs
refresh. Offline mode may use an expired reference observation and labels it
as overdue. Its index is still checked against that exact cached revision.
The displayed revision is not a claim about the branch's current head.

For `browse --source`, `--refresh` and `--offline` are mutually exclusive.
Refreshing a reference reuses unchanged immutable trees and
blobs. A network, authorization or quota failure is reported rather than
silently selecting old data or another access mode. Offline cache misses
report `cache.metadata-missing`. Fetch that source/workspace without
`--offline` first. Inconsistent cache records and observations later than the
system clock fail explicitly. Use [targeted cache maintenance](cache.md) to
remove an inconsistent record before fetching it again.

Metadata is stored separately from workspace packages and verification
receipts, under `metadata/records/` in the
[Site Ops cache root](workspace-packages.md#internal-workspace-cache).
`SITEOPS_CACHE_DIR` remains the one absolute cache directory override.
Records contain private source context and bindings, so keep them out of
repositories and galleries. Cache refresh does not edit operator Sites or
workspace pins.

With `--source`, these options control descriptive browsing and do not acquire
an executable workspace. With a selected [project](projects.md),
`browse --offline` instead requires its package and proof already in cache.
Neither mode relaxes provenance policy expiry for package use. `--refresh`
applies only to source index browsing and never changes a workspace pin.

## Public and authorized source access

Public reads use anonymous HTTPS by default. Private repositories and higher
authenticated rate limits can use an already configured GitHub CLI:

```bash
siteops browse --source github:<owner>/<repository> --auth cli
```

This mode sends required source requests through `gh api` using configured
authentication. It does not extract tokens, inspect personal credential
stores, change login or fall back to another credential source after failure.

`--auth cli` applies only to descriptive metadata browsing. Package
acquisition for `project pin` uses anonymous source access. Authenticated
package acquisition is not implemented.

Anonymous and CLI access use separate cache scopes. Retained data belongs to
the current operating system user and stays available locally after the
original read. Reuse does not confirm the current GitHub login or repository
permission. Use `--refresh` when a fresh source access check is required.
No credential or token is stored in the metadata cache.

The GitHub CLI executable is resolved only from absolute PATH entries, not
implicitly from the content directory. Because `gh api` can follow redirects,
authenticated reads also confirm the repository's returned identity. A moved
repository requires its current owner and name rather than a silent switch.

Source access is separate from permission to deploy Azure or Kubernetes
resources. Error responses and tool output are not echoed as raw diagnostics.
Oversized or truncated responses fail explicitly.

## Understand what the preview establishes

A current index means its manifest/guidance input identities match the pinned
source. It does not establish package provenance, executable compatibility,
effective Site inputs, deployment authorization or workload health.

Remote cards intentionally omit authored targeting from the public index.
They report that targeting is unavailable rather than claiming no targets
were declared. They also omit executable plan/deploy suggestions because this
command has not acquired a complete deployable workspace.

Read the operator guide at the displayed revision. Use an
[operator project](projects.md) to acquire an approved complete workspace
package and execute it with configured Sites or, where the verified package
contains a declared input contract, [guided inputs](guided-inputs.md).
You can also select a reviewed local workspace. Descriptive index browsing
remains separate from acquisition and execution.

Remote inspection still uses a private output destination:

```bash
siteops browse --source github:<owner>/<repository> --output json
```

A private repository's identity and source selection can be private even
when its index format contains only approved descriptive fields. Destination
redaction refuses browsing before source access.

## Publish descriptions from a workspace

Content authors generate the index using the same bounded reader as local
browsing:

```bash
siteops -w workspaces/iot-operations index --public --for-source github
```

`--public` explicitly approves the workspace's declared entry names and
guidance for a publication projection. Review that text before publishing. Read access
or a public repository is not automatic publication approval.

The command writes two generated files:

| File | Audience and purpose |
|---|---|
| `siteops-index.json` | Approved descriptive entries. Suitable as input to a separately implemented read-only gallery |
| `siteops-index.inputs.json` | Source-private input paths and freshness digests. Keep with the authorized source, not in the gallery |

On POSIX, both generated files allow at most owner read/write access.
Refreshing preserves existing permissions only within that limit.
On Windows, files inherit access controls from the workspace directory.
Protect that directory according to the source's access requirements.

The public index is built from an allowlist. It excludes the raw manifest
description, selectors, named Sites, absolute workspace paths, diagnostics
and source pointers. It contains no effective input values. Unclassified
candidates are omitted rather than silently promoted to published choices.

The binding file records the exact candidate set, present and absent guidance
inputs, and the public index's content identity. UTF-8 source identities
normalize uniform LF/CRLF line endings. The GitHub adapter adds optional Git
object identities for both forms. Other source adapters do not need those
Git-specific identities.

The index contains no self-referential commit or archive digest. GitHub pins
the commit containing the generated files and compares the recorded inputs
against that commit's tree. Changes to headers, metadata, extra paths or the
conventional candidate set make an old index stale. Template behavior and
guide contents still require their normal authoring and preparation coverage.

Keep the files alongside their workspace and regenerate after changing
discovery inputs. CI can compare them without writing:

```bash
siteops -w workspaces/iot-operations index --public --for-source github --check
```

Commit both generated files with the source. The binding file follows the
repository's access boundary, while only the public index belongs in a
gallery. When refreshing a GitHub index, keep `--for-source github`.
Removing that setting would remove the adapter's freshness evidence, so the
producer reports the mismatch rather than silently downgrading the output.

Generation performs no upload, commit, branch push or release operation.
Publishing those files requires the source owner's normal review and
publication process. A missing or stale index produces an actionable error,
not a silent fallback to floating remote YAML.

Keep generated outputs unchanged when publishing them. The input bindings
can describe private source paths, so a gallery should receive only the
public index and separately approved presentation assets.

## Boundaries

The first adapter supports GitHub.com. The common index, entry model,
filtering and rendering do not require GitHub fields. An approved artifact
source or another Git host can supply the same model through its own
identity and authorization boundary.

A gallery consumes the public index, not the source binding file or private
`browse --output json`. Gallery hosting, artifact storage, source provenance
and deployment credentials remain separate choices.

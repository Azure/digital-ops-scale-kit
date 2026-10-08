# Browse a published source

Browse a source's published manifest descriptions without cloning its
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
`--source NAME` also accepts the name of an approved source that you enrolled
independently, optionally with `@<ref>`. Its repository reference may
be resolved even after the enrollment expires. This passive read
does not renew the enrollment or allow use of a workspace package.

```bash
siteops browse --source https://github.com/<owner>/<repository> --ref <branch-or-tag>
```

The command resolves one exact commit, then reads that commit's tree and
immutable index blobs. The source and revision remain visible in plain and
JSON output. Documentation links point at that same revision.

## Choose a workspace and manifest

A source with one index is selected automatically. If several workspaces
publish indexes, the command lists their paths within the source and asks for
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
request for every manifest.

## Use cached metadata with `browse`

Remote browsing retains source observations, trees and index blobs in the
private Site Ops cache. Repeat the same browse command to reuse them.
Branch, tag and default branch observations can be reused for five minutes.
After that interval, an ordinary browse resolves the reference again.

Use `--refresh` to resolve it immediately, or `--offline-content` to
select retained metadata without contacting the source:

```bash
siteops browse --source github:<owner>/<repository>
siteops browse --source github:<owner>/<repository> --refresh
siteops browse --source github:<owner>/<repository> --offline-content
```

Keep the same `--ref`, `GH_TOKEN` setting and source workspace selection
across these commands. A fully cached commit can also be selected
directly:

```bash
siteops browse --source github:<owner>/<repository> --ref <full-commit-sha> --offline-content
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

For `browse --source`, `--refresh` and `--offline-content` are mutually
exclusive. Refreshing a reference reuses unchanged immutable trees and
blobs. A network, authorization or quota failure is reported rather than
silently selecting old data or another access mode. Offline cache misses
report `cache.metadata-missing`. Fetch that source/workspace without
`--offline-content` first. Inconsistent cache records and observations
later than the system clock fail explicitly. Use [targeted cache maintenance](cache.md) to
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
`browse --offline-content` instead requires its package and proof already
in cache.
Neither mode relaxes provenance policy expiry for package use. `--refresh`
applies only to source index browsing and never changes a workspace pin.
The `--offline-content` switch limits source content acquisition, not Azure
resource reads or deployment operations in other commands. Direct executable
`--source SOURCE@RELEASE` resolves a published release online on each
invocation and cannot be combined with this switch. A project pin is the
route for offline content and repeatable fleets.

## Public and authorized source access

Site Ops reads public sources over HTTPS without credentials by default.
GitHub limits API requests without credentials for each IP address, and
everyone behind the same address shares that limit, for example on a
corporate network, Cloud Shell or Codespaces. To use the higher limit of
your own account, set `GH_TOKEN` before running Site Ops:

```bash
export GH_TOKEN=$(gh auth token)
```

```powershell
$env:GH_TOKEN = gh auth token
```

Any GitHub token that can read public repositories is enough for public
sources. Site Ops sends the `GH_TOKEN` value only on its requests to
`https://api.github.com`, never to another host or on a redirect, and does
not store or print it. A rejected token fails with an error that names
`GH_TOKEN` rather than retrying without credentials. Unset `GH_TOKEN` to
read without credentials.

Site Ops does not read tokens that GitHub CLI stores. It also ignores
`GITHUB_TOKEN`, which Codespaces and other tools set automatically. To use
the token in `GITHUB_TOKEN`, choose it explicitly:

```bash
export GH_TOKEN="$GITHUB_TOKEN"
```

See GitHub's
[rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Direct `--source SOURCE@RELEASE` and `project pin` read release metadata
through the API, then download the workspace descriptor, package and proof
from the release download URL,
`https://github.com/<owner>/<repository>/releases/download/<tag>/<file>`.
These downloads send no credentials and do not count toward the API limit.
Each file must match the size and SHA-256 that the API reports for that
release asset before Site Ops uses it.

To browse a private repository, set `GH_TOKEN` to a token that can read it.
Package downloads send no credentials, so an acquired package must come from
a public repository.

Reads without credentials and reads with `GH_TOKEN` use separate cache
scopes. Retained data belongs to the current operating system user and stays
available locally after the original read. Reuse relies on that earlier read.
Use `--refresh` when a fresh source access check is required. No credential
or token is stored in the metadata cache.

Site Ops rejects API redirects, so a moved repository requires its current
owner and name.

Source access is separate from permission to deploy Azure or Kubernetes
resources. Error responses are not echoed as raw diagnostics.
Oversized or truncated responses fail explicitly.

## Understand what the preview establishes

A current index means its manifest/guidance input identities match the pinned
source. It does not establish package provenance, executable compatibility,
effective Site inputs, deployment authorization or workload health.

Remote cards intentionally omit authored targeting from the public index.
They report that targeting is unavailable rather than claiming that no
selector or Sites were declared. They also omit plan and deploy commands,
because remote browsing reads published descriptions only. Deployment
downloads and verifies the package.

Read the operator guide at the displayed revision. With an approved source
that you enrolled independently and a published release,
[guided inputs](guided-inputs.md) can use direct `--source SOURCE@RELEASE`
for one explicit Site. An
[operator project](projects.md) can instead pin the verified package for
configured Sites and repeat use.
You can also select a reviewed local workspace. Descriptive index browsing
remains separate from acquisition and execution.

Remote inspection still uses a private output destination:

```bash
siteops browse --source github:<owner>/<repository> --output json
```

A private repository's identity and source selection can be private even
when its index format contains only published descriptive fields. Destination
redaction refuses browsing before source access.

## Publish descriptions from a workspace

Content authors generate the index using the same bounded reader as local
browsing:

```bash
siteops -w workspaces/iot-operations index --public --for-source github
```

`--public` explicitly publishes the workspace's declared manifest names and
guidance by marking them public. Review that text first. Read access
or a public repository does not publish descriptions automatically.

The command writes two generated files:

| File | Audience and purpose |
|---|---|
| `siteops-index.json` | Published manifest descriptions. Suitable as input to a separate gallery that only reads them |
| `siteops-index.inputs.json` | Input paths private to the source and freshness digests. Keep with the authorized source, not in the gallery |

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
identities, which are specific to Git.

The index contains no digest of the commit or archive that contains it. GitHub pins
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
public index and separately reviewed presentation assets.

## Boundaries

The GitHub adapter reads GitHub.com. The common index, manifest description
model, filtering and rendering do not require GitHub fields, so another
artifact source or Git host can supply the same model through its own
identity and authorization boundary.

A gallery consumes the public index, not the source binding file or private
`browse --output json`. Gallery hosting, artifact storage, source provenance
and deployment credentials remain separate choices.

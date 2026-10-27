# Files Site Ops keeps

Site Ops keeps no deployment state between commands. Engine commands save
only the files below, each in a named format. Reading a file leaves it
unchanged. Only the command named under **Written by** changes it. The
bootstrap keeps its own [installation storage](#installation-storage).

## Locations and formats

| File | Location | Format | Written by | Read by |
|---|---|---|---|---|
| Workspace pin | `<project>/siteops.pin` | JSON `siteops/v1alpha1` `WorkspacePin` | `project pin` | Commands run with `--project`, and `project show` |
| Project state | `<project>/.siteops/` | A `.gitignore` that holds `*`, and an empty `project.lock` | `project pin` | `project pin` |
| Approved source | `<config>/siteops/sources/<name>/source.json` | JSON `siteops.source/v1` `ApprovedSource` | `source enroll` | Commands that use the source, `source list` and `source show` |
| Source policy | `<config>/siteops/sources/<name>/policy.json` | JSON `siteops/v1alpha1` `ArtifactVerificationPolicy`, pinned by its SHA-256 in `source.json` | `source enroll` | Package verification |
| Trusted root | `<config>/siteops/sources/<name>/trusted-root.jsonl` | Sigstore trusted root, pinned by its SHA-256 in `source.json` and the policy | `source enroll` | Package verification |
| Cache marker | `<cache>/cache.json` | The exact bytes `{"apiVersion":"siteops/v1alpha1","kind":"WorkspaceCache"}` and a newline | First cache use | Every cache use |
| Package | `<cache>/objects/sha256/<digest>/` | `package.zip` and its extracted `content/`, described by `siteops-package.json` (`siteops/v1alpha1` `WorkspacePackage`) | Package acquisition | Every use, after verification |
| Package proof | `<cache>/proofs/sha256/<digest>/proof.bin` | Proof bytes, named by their SHA-256 | Package acquisition | Package verification |
| Source metadata | `<cache>/metadata/records/<key digest>.json` | JSON `siteops/v1alpha1` `SourceMetadataRecord` | Release resolution | Release resolution, including `--offline-content` |
| Verification receipt | `<cache>/receipts/<digest>/<key>.json` | JSON `siteops/v1alpha1` `ArtifactVerification` | Every package use | No command. Verification decides trust at each use, and a receipt only records it. |
| Cache locks and staging | `<cache>/locks/`, `<cache>/staging/` | Empty lock files and incomplete writes | Any cache use | Any cache use |
| Answer file | The `inputs --example` path | YAML `siteops.inputs/v1` `SiteInputValues` | `inputs --example` | `--input-file` |
| Saved Site | The `inputs --save-site` path | YAML `siteops/v1` `Site` | `inputs --save-site` | `--site-file`, or as a configured Site |
| Content index | `<workspace>/siteops-index.json` and `siteops-index.inputs.json` | JSON `siteops/v1alpha1` `ContentIndex` and `ContentIndexBindings` | `index` | `browse`, and `index --check` |

`<config>` is `%LOCALAPPDATA%` on Windows, and `$XDG_CONFIG_HOME` or
`~/.config` elsewhere. [Cache maintenance](cache.md) describes `<cache>`.

## Installation storage

The bootstrap scripts keep these directories under their data root, `<data>`.
It is `%LOCALAPPDATA%\siteops` on Windows, and `$XDG_DATA_HOME/siteops` or
`~/.local/share/siteops` elsewhere. Only the bootstrap writes them. It checks
each one again before reuse, and stops with a message to inspect a directory
that differs rather than rewriting it.

| Directory | Location | Contents | Kept when |
|---|---|---|---|
| Retained release download | `<data>/install-downloads/<selection>/` | `siteops-install.zip` and its `.attestation.jsonl` proof | An installation succeeds. The next run with the same selection verifies them again instead of downloading. |
| Engine reference | `<data>/engine-references/<selection>/` | `siteops-engine.json`, JSON `siteops.release.engine/v1` `EngineReference`, and its `.attestation.jsonl` proof | A content release selects its engine |
| Installed bundle | `<data>/bundles/<bundle SHA-256>/` | The extracted bundle, described by `bundle.json` (`siteops.install/v1` `SiteOpsBundle`), with its wheels and `pylock.toml` | An installation succeeds. uv installs Site Ops from it. |
| Native uv copy | `<data>/tools/uv/0.12.20/` | The pinned uv executable. Windows also keeps its archive. | No qualified uv 0.12.20 is on `PATH` |
| Installation staging | `<data>/install-staging/` | One private directory for each run | A run is interrupted. A later run removes directories older than a day. |

A `<selection>` is the SHA-256 of the repository, release, source commit,
source ref and calling workflow, each followed by a NUL byte.
Read [retained files](install-siteops.md#retained-files-and-private-diagnostics)
before you change them.

## Compatibility

Each reader accepts only its format identifier and known fields. Unit tests
keep files that v1.0.0b7 saved, under
`tests/fixtures/compatibility/v1.0.0b7/saved-data`, and check both directions:

- The current engine reads each file, keeps its meaning, and leaves its bytes
  and modification time unchanged.
- The current engine writes the same bytes for the same content, so
  v1.0.0b7 reads what the current engine saves. You can return to that
  release without converting these files.

These tests cover the pin, approved source, cache marker, a package that
v1.0.0b7 published in its cache with its extracted content, proof, source
metadata, answer file, saved Site and content index. A filled in v1.0.0b7
answer file saves the same Site as the values given on the command line.
No command reads verification receipts, so the tests leave them out. The
bootstrap tests cover the release download selection.

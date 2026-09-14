# Build a workspace package

Content authors can produce one ZIP containing a complete workspace, its
approved companion documentation, and licensing files. The package preserves
source-relative paths, so a guide outside the workspace can remain in its
canonical location.

This is a content artifact, separate from the Site Ops installation bundle.
Building it performs no upload, signing, release operation or deployment.
The producer reports `provenance: not-established`. A checksum and a valid
package structure do not authenticate the publisher.

## Produce from a reviewed commit

Use a clean source checkout and the repository's development environment.
Git and the declared Python dependencies must be installed. Choose an
existing output directory outside the source checkout, or a gitignored
directory. The output filename must be new.

From the repository root, substitute your kit version and output directory:

```powershell
$commit = git rev-parse HEAD
python scripts\build-workspace-package.py `
  --root . `
  --expected-source-sha $commit `
  --workspace workspaces/iot-operations `
  --id azure.iot-operations `
  --version <kit-version> `
  --requires-siteops '>=1.0.0b1,<2' `
  --require-feature manifest/v1 `
  --require-feature composition/v1 `
  --include docs `
  --include README.md `
  --license LICENSE `
  --license ThirdPartyNotices.txt `
  --output '<absolute-output-directory>\iot-operations.zip'
```

Use forward slashes for source-relative `--workspace`, `--include` and
`--license` values on every platform. `--root` and `--output` are native
filesystem paths. The script also runs on Linux using its ordinary Python
invocation and shell continuation syntax.

The producer checks the expected source commit and clean checkout, then reads
an export of that exact commit. Ignored local tools and secrets are excluded.
It includes every tracked workspace file and each selected companion.
An export that omits selected tracked files, such as through `export-ignore`,
is rejected. Submodules, symbolic links and Git LFS pointers require explicit
source preparation rather than automatic downloads.

The output is a JSON summary with the ZIP's SHA-256, size, kit identity and
workspace path. Keep those exact bytes for separate provenance signing and
qualification. Reconstructing another ZIP is a different artifact.

## Package identities

The first member, `siteops-package.json`, uses `siteops/v1alpha1` and kind
`WorkspacePackage`. It records:

| Field | Meaning |
|---|---|
| `kit` | Author-supplied identifier and version, independent of the engine version |
| `source.revision` | Opaque source revision, without a required repository or hosting provider |
| `workspace.root` | Package-relative workspace directory, or `.` for a root workspace |
| `workspace.tree` | SHA-256 over the sorted workspace-relative file inventory |
| `compatibility` | Bounded PEP 440 Site Ops version range and required engine features |
| `files` | Exact package-relative payload paths, raw-byte SHA-256 digests and sizes |

File hashes preserve raw bytes, including line endings. The workspace tree
uses the domain prefix `siteops.workspace-tree/v1` followed by a NUL byte and
compact UTF-8 JSON file records ordered by path, with sorted object keys.
Each record contains `path`, `sha256` and `size`. Companion files outside the
workspace affect the archive identity, not the workspace tree identity.
The metadata file is excluded from its own payload inventory.

Supported required features are `manifest/v1`, `composition/v1` and
`manifest-selection/v1`. Unsupported required features and incompatible
engine versions are rejected. Compatibility describes the declared engine
contract, not successful compilation, deployment or workload qualification.

## Bounded materialization

Package inspection compares the expected archive SHA-256 before parsing the
ZIP. It checks the metadata, every payload digest and current-engine
compatibility. Materialization writes only into a newly created staging
directory. Existing files and directories are preserved.

| Limit | Maximum |
|---|---|
| Payload files | 10,000 |
| File and directory path nodes | 20,000 |
| One payload file | 64 MiB |
| Total payload | 512 MiB |
| Archive | 128 MiB |
| Metadata | 4 MiB |
| ZIP central directory | 8 MiB |
| Path | 512 characters, 32 components, 255 UTF-8 bytes per component |
| Expansion ratio | 200 times compressed member size |

The ZIP32 format supports stored and deflated regular files. It excludes
directory entries, links, special files, duplicate or case-colliding paths,
file/directory conflicts, encrypted members, extra fields, comments,
multi-volume archives and ZIP64. Directories are inferred from file paths.
The producer stores highly compressible files when compression would exceed
the consumer's expansion limit.

Files use owner-only POSIX permissions. On Windows, staging inherits its
parent's access controls, so the caller must provide a protected parent.
Failure removes only paths created by that materialization attempt.

## Trust and execution boundary

The package format, file identities and compatibility model contain no
GitHub-specific requirements. The first producer reads Git snapshots.
Other approved producers can create the same package format.

These integrity and materialization primitives are not a trusted acquisition
command. Remote use requires consumer-owned provenance policy before
materialization, a protected cache with atomic publication, and separate
operator-owned Site configuration. Packaged example Sites must not silently
become deployment targets.

Archive confinement does not validate paths referenced inside Bicep or
manifests, and a complete source-file inventory is not proof of every
execution dependency. Those checks belong to the execution boundary.

There is currently no `plan --source` or `deploy --source` route. Continue to
use a reviewed local workspace with configured Sites for execution.

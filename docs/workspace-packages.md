# Build a workspace package

Content authors can produce one ZIP containing a complete workspace, its
approved companion documentation, licensing files, and producer-compiled ARM
JSON for executable Bicep roots. The package preserves every authored file at
its source-relative path. Generated templates use a separate producer-owned
namespace.

This is a content artifact, separate from the Site Ops installation bundle.
Building it performs no upload, signing, release operation or deployment.
The producer reports `provenance: not-established`. A checksum and a valid
package structure do not authenticate the publisher.

## Produce from a reviewed commit

Use a clean source checkout and the repository's development environment.
Git, Azure CLI, an existing Azure CLI-managed Bicep installation, and the
declared Python dependencies must be installed. Choose an existing output
directory outside the source checkout, or a gitignored directory. The output
filename must be new.

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

Use `--bicep <absolute-path>` when the provisioned Azure CLI-managed Bicep
binary is outside the current Azure CLI configuration directory.

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
Source contents come from raw committed Git blobs. Archive substitutions and
checkout line-ending settings do not rewrite the authored package files.

The producer starts with the existing discovery inventory, including partials,
then recognizes additional manifest documents throughout the complete workspace
using the shared parser. Custom paths and extensionless manifests therefore
receive the same template mappings as conventional entries. It expands includes
and compiles distinct deployment template paths, not every Bicep module.
Name-based browsing keeps its existing inventory rules.
The producer does not construct Sites, apply overlays or resolve runtime
parameter values. Unrelated data files are not deployment entries merely
because they contain a key such as `steps`.
Incomplete manifest discovery blocks production. Optional advisory-guidance
errors do not independently prevent template discovery or compilation.

Native ARM JSON deployment roots are validated and mapped at their authored
path. Bicep roots are compiled to
`.siteops/compiled/v1/<source-path>.json`. Authored content cannot use the
`.siteops/compiled` namespace.

Package compilation uses an absolute Azure CLI path and a copied,
already-provisioned Bicep executable. The invocation runs with isolated Azure
CLI configuration, user cache, and temporary directories. Azure CLI telemetry
and automatic Bicep upgrade checks are disabled. `--no-restore` prevents
implicit module restoration, and Azure CLI is configured to use only the
controlled Bicep binary from `PATH`. A readable `az bicep version` result is
required before the first build.

An authored `bicepconfig.json` must be inside the packaged workspace. The
nearest workspace configuration is recorded by path and digest. When the
workspace supplies none, the producer uses an explicit empty configuration
outside the package source as the nearest boundary and records
`producer-default` with its digest. Every tracked Bicep file in the workspace
must resolve to a configuration inside the workspace or to that producer
default. This accounts for module-level configuration discovery without
claiming a complete module graph.
Configuration filenames must use exactly `bicepconfig.json` so discovery has
the same meaning on case-sensitive and case-insensitive filesystems.

The output is a JSON summary with the ZIP's SHA-256, size, kit identity and
workspace path, plus the number of mapped deployment templates. Keep those
exact bytes for separate provenance signing and qualification. Reconstructing
another ZIP is a different artifact.

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
| `templates` | Exact source-to-ARM-artifact mappings and producer compilation identities |

File hashes preserve raw bytes, including line endings. The workspace tree
uses the domain prefix `siteops.workspace-tree/v1` followed by a NUL byte and
compact UTF-8 JSON file records ordered by path, with sorted object keys.
Each record contains `path`, `sha256` and `size`. Companion files outside the
workspace affect the archive identity, not the workspace tree identity.
The metadata file is excluded from its own payload inventory.

Supported required features are `manifest/v1`, `composition/v1` and
`manifest-selection/v1`. Package production also requires
`compiled-templates/v1`. Unsupported required features and incompatible
engine versions are rejected. Compatibility describes the declared engine
contract, not deployment or workload qualification.

## Template mapping

`templates.artifactRoot` is `.siteops/compiled/v1`. Each entry uses
workspace-relative paths, so `source.path` is the same canonical path stored in
the manifest.

```json
{
  "templates": {
    "artifactRoot": ".siteops/compiled/v1",
    "entries": [
      {
        "source": {
          "path": "templates/aio/instance.bicep",
          "kind": "bicep",
          "sha256": "<source-sha256>",
          "size": 1234
        },
        "artifact": {
          "path": ".siteops/compiled/v1/templates/aio/instance.bicep.json",
          "kind": "arm-json",
          "sha256": "<artifact-sha256>",
          "size": 5678
        },
        "producer": {
          "mode": "azure-cli-bicep",
          "invocation": ["az", "bicep", "build", "--no-restore"],
          "driver": {
            "provider": "azure-cli",
            "version": "2.87.0"
          },
          "compiler": {
            "provider": "azure-cli-bicep",
            "version": "0.45.15.0"
          },
          "configuration": {
            "discovery": "producer-default",
            "sha256": "<configuration-sha256>"
          },
          "dependencies": {
            "coverage": "compiled-output-only",
            "templateHashes": []
          }
        }
      }
    ]
  }
}
```

A workspace-authored configuration uses `nearest-found` and adds its
workspace-relative `path`. Native ARM JSON uses the same entry shape with
identical source and artifact paths and identities. It uses
`mode: "native-arm-json"`, `invocation: ["read-arm-json"]`, and null tool
and configuration fields.

Source paths and artifact paths are unique. Every identity must match the
exact package file inventory. Every generated namespace file must have one
mapping, and every mapped artifact must be valid ARM deployment JSON. Missing
fields, unsupported toolchain modes, changed identities, malformed output,
or generated files without mappings invalidate the package.

Compiler-emitted nested template hashes are retained with
`compiled-output-only` coverage. They do not claim a complete source module,
file-read, or configuration graph. Native ARM JSON records
`not-applicable`, or `unknown` when it contains a linked template.

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

`PackageInspection` retains the metadata file's SHA-256 and size as well as
the archive identity. A materialized binding uses those values to validate
`siteops-package.json`, every payload file, and the exact file and directory
inventory.

## Trust and execution boundary

The package format, file identities and compatibility model contain no
GitHub-specific requirements. The first producer reads Git snapshots.
Other approved producers can create the same package format.

These integrity and materialization primitives are not a trusted acquisition
command. Remote use requires consumer-owned provenance policy before
materialization, a protected cache with atomic publication, and separate
operator-owned Site configuration. Packaged example Sites must not silently
become deployment targets.

Producer compilation is limited to the selected source snapshot, but Bicep
does not expose an allowed-root switch or a complete file-read graph. The
mapping therefore does not claim complete dependency coverage. Engine-owned
manifest and parameter path confinement remains part of the acquired
execution boundary.

`MaterializedPackageBinding.bind(inspection, package_root, manifest)` provides
the internal execution binding. It revalidates the complete materialization,
resolves the manifest through the shared exact name and path rules, binds every
authored template to its mapped ARM JSON, and verifies package inputs before
engine parsers or providers consume them.

Pass that binding to
`Orchestrator(..., site_config_root=<project>, materialized_package=binding)`.
The Site configuration root is required and must stay outside package content.
Packaged example Sites therefore remain content rather than deployment
targets. Runtime Site values, overlays, selection, and prior-operation outputs
continue through the ordinary planner and executor.

The caller owns source-provenance verification and an immutable cache lease for
the binding's full lifetime. A parsed receipt, package metadata, or a
cache-shaped path does not establish that authority. The engine detects
changed bytes, unexpected paths, links, path aliases, and inventory drift at
its validation points. It does not provide an OS sandbox against another
process running as the same user.

Acquired kubectl inputs must be verified local package paths. Remote HTTPS
manifest URLs fail before tool or proxy mutation, including when a prior
operation produces the URL at runtime. Ordinary trusted local workspaces keep
their existing HTTPS behavior.

There is no public `plan --source` or `deploy --source` route yet. Source
download, provenance-policy orchestration, project pins and release delivery
remain separate acquisition work.

## Private workspace cache

`WorkspaceCache` provides internal package publication and use leases.
`publish` copies an opaque local archive into private staging, verifies its
expected SHA-256 and consumer-owned provenance, then extracts and validates
the complete package before publishing it atomically. Existing valid objects
are reused. Changed or incomplete objects fail rather than being repaired
in place.

`lease` checks the retained archive, materialized inventory, source revision,
current-engine compatibility and consumer verification policy before returning
a `CachedWorkspace`. Keep that shared lease open through browsing, planning
and execution. Its `bind` method uses the same manifest resolver and
`MaterializedPackageBinding` as other acquired execution callers.

| Platform | Default cache root |
|---|---|
| Windows | `%LOCALAPPDATA%\siteops\cache` |
| Linux | `$XDG_CACHE_HOME/siteops`, or `~/.cache/siteops` |

`SITEOPS_CACHE_DIR` is the single cache-root override. It must select an
absolute directory. Cache root resolution is lazy until the storage API is
used. These defaults do not enable acquisition in the CLI.

The cache retains the ZIP and its extracted package beneath
`objects/sha256/<digest>/`, with verification receipts outside those immutable
objects. Every use invokes the caller's trusted verifier. A stored receipt
never authorizes execution by itself. A verifier with retained local proof
and trusted-root inputs can support offline reuse without a source request.
The storage layer performs no downloads and does not refresh project pins.

New directories use private POSIX modes or an explicit Windows DACL for the
current user, SYSTEM and Administrators. Existing ownership and access
controls are checked rather than changed. Choose a local filesystem location
whose ancestors prevent replacement by other users. An existing unmarked
directory, a filesystem alias or a shared cache path is rejected.

Shared process-held leases allow concurrent readers. Publication requires an
exclusive lease for the package digest, and the operating system releases
leases when a process exits. Lease coordination protects cooperating Site Ops
operations, not against another process running as the same user. Corrupt
content and changed access controls are detected during reuse.

Operator Sites, overlays, pins and run state remain outside the cache.
Cache maintenance commands, retention and source-metadata caching are
separate work.

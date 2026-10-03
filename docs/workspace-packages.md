# Build a workspace package

Content authors can build one `WorkspacePackage` ZIP containing a complete
authored workspace, approved companion documentation and licensing files, and
ARM JSON compiled by the producer for executable Bicep roots. Authored files
keep their paths relative to the source. Generated templates use a separate
namespace owned by the producer.

This content artifact is separate from the Site Ops installation wheel or ZIP
bundle. Building it performs no upload, signing, release operation or
deployment. The producer reports `provenance: not-established`. A checksum
and a valid package structure do not authenticate the publisher.
Successful production establishes package integrity and declared compatibility,
not deployment safety or a live AIO outcome.

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
an export of that exact commit. Untracked and ignored local files are
excluded. It includes every tracked workspace file and each selected
companion.
An export that omits selected tracked files, such as through `export-ignore`,
is rejected. Submodules, symbolic links and Git LFS pointers require explicit
source preparation rather than automatic downloads.
Source contents come from raw committed Git blobs. Archive substitutions and
checkout line-ending settings do not rewrite the authored package files.

The producer starts with the conventional entries and declared partials in the
browse inventory. It then uses the engine parser to recognize additional
parseable manifests throughout the workspace, including manifests available
only by explicit path, extensionless manifests and valid manifests without a
`kind`. Name-based browse discovery keeps its existing inventory rules.

The producer expands includes and compiles distinct deployment template paths,
not every Bicep module. It does not construct Sites, apply overlays or resolve
runtime parameter values. Supplemental kindless parameter data that cannot
form a manifest is ignored. Incomplete manifest discovery blocks production.
Optional guidance remains advisory, so its errors do not independently
prevent template discovery or compilation.

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

An authored Bicep configuration must be inside the packaged workspace and use
the exact basename `bicepconfig.json`. The nearest workspace configuration is
recorded by path and digest. When the workspace supplies none, the producer
uses an explicit empty configuration outside the package source as the
nearest boundary and records `producer-default` with its digest. Every tracked
Bicep file in the workspace must resolve to a configuration inside the
workspace or to that producer default. This accounts for module-level
configuration discovery without claiming a complete module graph and keeps
ancestor checks consistent across case-sensitive and case-insensitive
filesystems.

The output is a JSON summary with the ZIP's SHA-256, size, kit identity and
workspace path, plus the number of mapped deployment templates. Keep those
exact bytes for later signing and qualification. Signing creates a detached
attestation proof, a separate file containing signed provenance evidence.
Reconstructing another ZIP is a different artifact.

## Build workspaces declared by a release

A content release file can declare complete workspace builds alongside its
engine choice. The content tag supplies the kit version. Each workspace names
its own package, compatibility range and licensing files:

```json
{
  "tag": "v1.0.0b8",
  "headline": "Verified workspace content",
  "siteops": {"build": true},
  "workspaces": [
    {
      "workspace": "workspaces/iot-operations",
      "id": "azure.iot-operations",
      "package": "iot-operations.zip",
      "compatibility": {
        "siteops": ">=1.0.0b1,<2",
        "requiredFeatures": ["manifest/v1", "composition/v1"]
      },
      "include": ["docs", "README.md"],
      "licenses": ["LICENSE", "ThirdPartyNotices.txt"]
    }
  ]
}
```

Commit this as `releases/workspace-candidate/release.json` with a sibling
`notes.md`. Choose the tag and engine selection according to the
[release policy](releasing.md). The example describes the format rather than
a scheduled release.

From a clean checkout, build the declared unsigned assets into a new directory.
Supply the build number and attempt for the engine build that these workspaces
will accompany:

```powershell
$commit = git rev-parse HEAD
$sourceRef = git symbolic-ref --quiet HEAD
if ($LASTEXITCODE -ne 0) { throw 'Use a named source branch for this example.' }
python scripts\build-workspace-release.py `
  --root . `
  --repository '<owner/repository>' `
  --expected-source-sha $commit `
  --source-ref $sourceRef `
  --release-file releases/workspace-candidate/release.json `
  --build-number <build-number> `
  --build-attempt <build-attempt> `
  --output-dir '<new-absolute-output-directory>'
```

When `siteops.release` selects an existing engine, omit the build number and
attempt. Its exact version is used instead of the development engine version
in the checkout. The command always creates unsigned files. `--dry-run` also
permits a committed file under `.github/release-examples/` and marks the
unsigned build record as a preview. The flag does not sign the output.
`--release-workspace <workspace>` selects one exact workspace from the
declaration while retaining the identity of the complete prepared release
plan.

The directory receives each declared ZIP and `workspace-builds.json`, which
records their exact sizes, hashes, source, content version and selected engine
version. The record also binds the prepared release plan's SHA-256. Production
uses the same complete workspace builder as the individual package command. A
failure removes outputs created by that invocation, with an explicit warning
if cleanup cannot complete.

Automation can supply `--prepared-plan <file>` together with
`--expected-plan-sha <sha256>`. The producer requires both the exact expected
bytes and agreement with the release intent loaded from the selected commit.
Omitting these options derives the release plan directly from that committed
intent.

Declarations allow up to 64 distinct workspace paths, within the release
file's byte limit. Package names are unique portable ZIP filenames and reserve
room for their proof names. Workspace and companion paths must exist
in the selected commit. License paths must identify regular files. `include`
is optional, and omitted `requiredFeatures` defaults to `manifest/v1`.
The producer also records `compiled-templates/v1`.

If a workspace contains either generated index file, both must be present and
current for that exact source. Production preserves the existing generic or
GitHub binding settings and records the public index's exact digest. It never
rewrites a stale index. A workspace without generated indexes remains a valid
package source.

The producer checks declared compatibility against the selected engine
version. This does not establish that the released engine supports the
workspace. Consumer inspection and extraction still enforce the actual
installed engine's version and supported features. For an individual package,
`--target-engine-version` sets the compatibility target explicitly. It does
not acquire an engine or authorize consumer use.

The candidate workflow uses one reusable build and signing path per workspace,
with bounded parallelism. Its build job has read permissions and consumes the
release plan from the same run. It restores the committed Python locks through
the configured feed, verifies the selected Bicep binary against the SHA-256
pinned in workflow source before installing or using it, and builds the
workspace with private configuration and temporary directories.

A separate signing job downloads only that workspace's build artifact.
It admits the exact package and build record before creating a proof for each.
The signing job executes no repository scripts. A
collector with read permissions verifies both proofs under independent workflow
policy, checks the package against the reviewed declaration, and then creates
`siteops-workspaces.json` and the frozen workspace asset inventory.

The local build command produces unsigned files only. The candidate's
workspace proofs and descriptor are retained as Actions artifacts. Workspace
qualification against the selected installed engine and publication happen
later.

## Qualify against the selected engine

Workspace qualification uses the actual selected engine installation. A
combined release reuses the completed engine build. A content release
acquires the exact referenced engine assets without rebuilding them from
the current checkout. The ZIP and standalone wheel are verified before
inspection, and their source, version and identical application wheel bytes
are retained with the qualification inputs.

The qualification transfer path is anonymous and bounded to 128 MiB per
native engine asset and 2 MiB per proof. It uses the existing HTTPS transfer
boundary, restricted to approved origins. This limit applies to workspace
qualification, not to ordinary direct wheel installation.

For each declared Windows or Linux target and Python version, a new application
environment consumes the engine's authenticated `pylock.toml` through stock
pip. The probe runs with isolated Python imports, confirms the exact installed
version and module location, then checks package compatibility, protected
cache publication and reuse, and guarded loading of catalog manifests. Source
checkout imports cannot satisfy this gate.

The matrix also creates an operator project for the first declared workspace
through the installed engine's normal pin API. Its release identity comes
from the independently hashed release plan. Project publication follows
successful checks of the complete declared package set. Every runtime target
must produce the same pin digest. The receipt explicitly records that public
release observation was not performed, since the candidate is not yet published.

For a standalone qualification invocation, pass `--project-workspace PATH`
and `--plan FILE` together to `qualify-workspace-engine.py`. The plan must match
`--expected-plan-sha` and contain that exact workspace. The project is created
under the selected private state at `probe-state/operator`, with its cache
at `probe-state/cache`. Use that cache and the generated workspace policy/root
files when running the installed CLI with `--project` and `--offline-content`.
This seeds prepublication test state, not a new public archive-pinning route.
Its temporary qualification policy does not enroll an operator source or
authorize Azure deployment.

The result reports package and catalog counts separately. It loads no operator
Site values. It does not authorize targets, compare executable deployment
plans, deploy resources, or evaluate workload health. The final matrix gate
requires every declared target's result to name the same frozen engine,
workspace inventory, release plan and candidate project pin.

Native `uv tool install <wheel-url>` and the verified bootstrap are
described in the [installation guide](install-siteops.md). The isolated
qualification environment is release tooling, not another operator installer.
The final candidate payload includes the qualified workspaces and any engine
assets built for that release. After approval, the publisher rechecks those
exact bytes, proofs, and source contracts. The configured approval environment
and applicable content release evidence remain required.

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
GitHub-specific requirements. The current producer reads exact Git commits.
Other approved producers can create the same package format.

These integrity and materialization primitives are not a trusted acquisition
command. Remote use requires consumer-owned provenance policy before
materialization, a protected cache with atomic publication, and separate
operator-owned Site configuration. Packaged example Sites must not silently
become deployment targets. A direct `--source SOURCE@RELEASE` command
selects one verified package online but still requires explicit Site
inputs unless `--project` supplies the operator's configured Sites.
It does not change a project pin. A pin is the repeatable fleet and
`--offline-content` route.

Producer compilation is limited to the selected source snapshot, but Bicep
does not expose an allowed-root switch or a complete file-read graph. The
mapping therefore does not claim complete dependency coverage. Engine-owned
manifest and parameter path confinement remains part of the acquired
execution boundary.

`MaterializedPackageBinding.bind(inspection, package_root, manifest)` provides
the internal execution binding. It revalidates the complete materialization,
resolves the manifest through the shared exact name and path rules, binds every
authored template to its mapped ARM JSON, and verifies package inputs before
engine parsers or providers consume them. Missing mappings or changed source
or artifact bytes fail. The consumer does not compile packaged Bicep as a
fallback.

Pass that binding to
`Orchestrator(..., site_config_root=<project>, materialized_package=binding)`.
The Site configuration root is required and must stay outside package content.
For direct source use without a project, the CLI uses temporary private
operator configuration for one explicit target, not packaged example Sites.
Packaged example Sites therefore remain content rather than deployment
targets. Runtime Site values, overlays, selection, and prior-operation outputs
continue through the ordinary planner and executor.

The caller owns source-provenance verification and an immutable cache lease for
the binding's full lifetime. A parsed receipt, package metadata, or a
cache-shaped path does not establish that authority. The engine detects
unexpected paths, links, path aliases, and inventory drift at its validation
points. It does not provide an OS sandbox against another process running as
the same user.

Acquired kubectl inputs must be verified local package paths. Remote HTTPS
manifest URLs fail before tool or proxy mutation, including when a prior
operation produces the URL at runtime. Ordinary trusted local workspaces keep
their existing HTTPS behavior.

The [source acquisition flow](workspace-sources.md) provides release resolution,
downloads, retained proofs and verified cache use. [Operator projects](projects.md)
connect packaged content and separate configured Sites to ordinary planning
and deployment. Cache maintenance has its own commands. Workspace asset
publication uses the content release workflow after qualification and approval.

## Internal workspace cache

`WorkspaceCache` is an internal storage API for package publication and use
leases. Project commands consume it. Remote metadata browsing shares its
protected storage layout through a separate cache without acquiring executable
packages.

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
used. Cache directory selection does not select a workspace, source or trust
policy.

The cache retains the ZIP and its extracted package beneath
`objects/sha256/<digest>/`, with verification receipts outside those immutable
objects. Every use invokes the caller's trusted verifier. A stored receipt
never authorizes execution by itself. A verifier with retained local proof
and trusted-root inputs can support offline reuse without a source request.
The storage layer performs no downloads and does not refresh workspace pins.

`retain_proof` stores exact opaque verification inputs at
`proofs/sha256/<proof-digest>/proof.bin`. This optional namespace is created
atomically when first needed. Existing marked caches keep their marker and
package objects unchanged. Invalid existing namespace contents are rejected
rather than overwritten.

Proofs remain inputs, not publisher authority. `lease_proof` holds a shared
lease while the trusted verifier reads them and checks their identity again
on return. Proof retention uses a separate exclusive lock. Missing proof bytes
produce `cache.proof-missing`, while changed bytes or access controls fail
without repair. Older builds that do not support this namespace may reject
the cache. Use a separate cache directory when running such a build.

Source acquisition supplies an additional source check to `publish` and
`lease`. It compares the verified package with the selected workspace and kit
before publication or use. See [workspace sources](workspace-sources.md) for
the download, policy and pinned reuse flow.

New directories use private POSIX modes or an explicit Windows DACL for the
current user, SYSTEM and Administrators. Existing ownership and access
controls are checked rather than changed. Choose a local filesystem location
whose ancestors prevent replacement by other users. An existing unmarked
directory, a filesystem alias or a shared cache path is rejected.
Windows native declarations and the current process SID may be cached.
Per-node access-control checks are not cached.

Shared process-held leases allow concurrent readers. Publication requires an
exclusive lease for the package digest, and the operating system releases
leases when a process exits. Lease coordination protects cooperating Site Ops
operations, not against another process running as the same user. Corrupt
content and changed access controls are detected during reuse.

Source observations and index bytes use the separate optional
`metadata/records/` namespace. Each bounded record binds an exact provider,
access scope and metadata identity to payload size, SHA-256 and observation
time. Record filenames are hashes of those keys, never source paths.
The same root marker and native access controls protect every namespace.
See [remote browsing](remote-content.md#use-cached-metadata-with-browse).

Operator Sites, overlays, pins and run state remain outside the cache.
Use [cache maintenance](cache.md) to inspect storage or remove a selected
entry. Automatic pruning is not implemented.

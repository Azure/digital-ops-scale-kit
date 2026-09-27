# Browse deployment content

Find an operation, understand its requirements and effects, then use the
ordinary Site Ops planner and deployer against your configured Sites.

From a repository checkout:

```bash
siteops -w workspaces/iot-operations browse
siteops -w workspaces/iot-operations browse aio-install
siteops -w workspaces/iot-operations browse --tag mqtt
siteops -w workspaces/iot-operations browse --category sample --search "resource set"
```

`browse` reads manifest headers and optional authored guidance. It does not
load Sites or overlays, expand includes, read parameter values, compile
templates, probe deployment tools, contact services or perform deployment.
It works with an explicitly selected workspace that has no configured Sites.
The card's authored Site input guidance is descriptive. For an entry with
an executable typed input contract, use
`siteops -w workspaces/iot-operations inputs aio-install` after browsing
to see the required answers. [Guided inputs](guided-inputs.md) describes
the separate Site construction and planning step.

For a published remote catalog, use `browse --source`. See
[remote browsing](remote-content.md) for pinned GitHub sources, authorized
reads and source-index publication. Remote preview does not acquire
deployable workspace content.

After pinning a verified workspace in an operator project, omit `-w` and
browse the acquired package with the same approved source:

```bash
siteops --approved-source official --project ./factory browse aio-install
siteops --approved-source official --project ./factory inputs aio-install
```

This reads the selected package rather than the checkout or the remote
descriptive index. See [operator projects](projects.md) for the pin and
approval steps.

## Find the right choice

The default inventory lists declared standalone entries and visibly
unclassified candidates. Declared partials require `--include-partials`.
A standalone role describes author intent, not readiness or certification.

The included AIO workspace labels its ordinary deployment paths as `core`
and its instructional examples as `sample`. Use `--category core` or
`--category sample` to focus on either. These categories are separate from
standalone/partial roles and do not establish support, trust or qualification.
Internal fundamental fragments remain partials, not standalone core choices.

Inventory rows are compact and ordered by workspace-relative path. All
matches are returned by default, including for large collections. Use:

| Option | Meaning |
|---|---|
| `--search TEXT` | Every space-separated word must match the name, description, path, category or tags, ignoring case |
| `--tag TAG` | Require an exact authored tag. Repeat to require several tags |
| `--category CATEGORY` | Require an exact authored category |
| `--limit N` | Show at most N matches, with the full match count and a truncation indication |
| `--include-partials` | Include declared reusable fragments |
| `--output json` | Emit one complete private inspection document |

Select an exact visible name or an explicit manifest path:

```bash
siteops -w workspaces/iot-operations browse resource-set-basic
siteops -w workspaces/iot-operations browse samples/resource-set-basic/manifest.yaml
```

`browse`, `validate`, `plan` and `deploy` accept the same exact manifest names.
Names are case-sensitive and scoped to the selected workspace, not a global
registry. Declared partials use explicit paths for execution.

A bare token can identify a manifest name or a file at the workspace root,
including extensionless filenames and names ending in `.yaml`. If the name
and filename identify different files, the command reports the available
paths. Use `./file.yaml` to select a root filename explicitly, or use the
displayed `manifests/...` or `samples/...` path to select another entry.

An incomplete manifest-name inventory cannot establish uniqueness, so use
an explicit path. Broken optional entry guidance retains the known name and
returns a partial card. Guidance is not an execution admission rule.
Inventory filters preserve known name ambiguity even when only one matching
row remains visible. Selection cannot be combined with inventory filters.

## From inspection to deployment

For the first AIO installation-to-workload journey, configure `catalog-basic`
once using the [basic-routing guide](../workspaces/iot-operations/samples/resource-set-basic/README.md).
Use the same explicit Site for both operations:

```bash
siteops -w workspaces/iot-operations browse aio-install
siteops -w workspaces/iot-operations plan aio-install -l name=catalog-basic
siteops -w workspaces/iot-operations deploy aio-install -l name=catalog-basic
```

After assessing AIO readiness, inspect and apply the workload:

```bash
siteops -w workspaces/iot-operations browse resource-set-basic
siteops -w workspaces/iot-operations plan resource-set-basic -l name=catalog-basic
siteops -w workspaces/iot-operations deploy resource-set-basic -l name=catalog-basic
```

For an existing installation, omit installation and configure the correct
instance name and release. The lookup requires the instance name as input.
It reads associated infrastructure during deployment, not during browsing
or planning.

Always review the actual plan before deploying. A CLI selector replaces
manifest targeting. `deploy` prepares again, rather than consuming the
previous preview as a saved plan. Deployment completion, readiness and the
sample's MQTT canary are separate outcomes.

Generated command suggestions are labeled for PowerShell on Windows and a
POSIX shell elsewhere. PowerShell suggestions are not Command Prompt
commands. Paths containing control characters or Windows shell metacharacters
are displayed as data without executable command suggestions. Private JSON
retains canonical paths for callers that construct argument arrays directly.

## Authored guidance and uncertainty

A card distinguishes supported Site inputs from selected step-supplied
values. These are descriptions, not an input form or another parameter
resolver. The planner still owns effective values, filtering by template
schema and actual selected operations.

Missing input or permission guidance means undocumented, not "none required".
The coverage statement identifies the scope the author describes.
Source references and the full manifest description are retained in JSON.
Sensitivity is authored guidance, not permission to publish configuration.
No effective input values are included.

## Author an entry

For `manifest.yaml` or `manifest.yml`, place optional guidance in `entry.yaml`
beside it. Other manifest filenames use `<stem>.entry.yaml`.
Core operations and samples share this convention.

```yaml
apiVersion: siteops/v1alpha1
kind: DeploymentEntry
role: standalone
category: operation
tags: [storage]
documentation: [README.md]
outcome: Create a storage account in the selected resource group.
inputs:
  - field: parameters.storageAccountName
    type: string
    requirement: required
    sensitivity: non-sensitive
    description: The account name supplied by the configured Site.
prerequisites:
  - An existing resource group and permission for the deployment.
effects:
  - Creates or updates the account.
removal:
  - Delete it deliberately after preserving required data.
coverage: Covers this operation, not every provider constraint or effective permission.
```

Supported roles are `standalone`, `partial` and `unclassified`. Categories and
tags are opaque workspace vocabulary. A manifest owns its name, description,
targeting and steps. Guidance must not duplicate those fields.

Input rows accept `field`, `type`, `requirement`, `description`, optional
`sensitivity`, `defaultBehavior` and `source`. Requirements are `required`,
`optional`, `conditional` or `unknown`. Sensitivity is `sensitive`,
`non-sensitive` or `unknown`, with unknown as the default.
Only inputs explicitly marked non-sensitive can carry descriptive default
text. Literal `default` values and choice lists are not supported.

Optional `supplied` rows contain `step`, `input`, `description` and `source`.
Describe only bindings the consumer actually retains. A value in a shared
parameter file may be filtered out by the consumer's template schema.

Documentation paths are relative to the guidance file. Input and supplied
source references are workspace-relative. Both may include a fragment and
must remain inside the workspace. They are references, not files opened by
the inspector.

Conventional discovery covers YAML manifests beneath `manifests` and
`manifest.yaml`, `manifest.yml` and underscore partials beneath `samples`.
In a nested `manifests/` directory, `inputs.yaml` is a discoverable manifest
unless the same directory has `manifest.yaml` or `manifest.yml`; alongside
either of those names, it is reserved for that manifest's typed input
contract. To browse an actual manifest named `inputs.yaml` in that ambiguous
layout, list its path explicitly in `content.yaml`.
Metadata files are excluded. The engine does not crawl arbitrary template or
script trees. To list additional manifests, optionally name them in the
workspace's `content.yaml`:

```yaml
apiVersion: siteops/v1alpha1
kind: WorkspaceContent
entries:
  - operations/custom/action.yaml
```

This file supplies extra paths, not copied cards or a generated index.
Explicit-path inspection also works without it.

## Errors and limits

Malformed headers or guidance produce safe diagnostics and a nonzero exit.
Useful readable entries remain available in a partial result. An unrelated
broken entry does not prevent inspection by explicit path.
Absent metadata is different from malformed metadata. An empty inventory
without errors succeeds.

Discovery excludes hidden paths, Site/configuration and working-state
directories, symlinks, reparse points and hardlinked input files. Explicit
inspection also stays inside the selected workspace.
Path checks use canonical filesystem identity and reject ambiguous Windows
components and reserved device names.

One invocation admits at most 1,000 candidate entries and 10,000 enumerated
directory items, with 12 levels of traversal beneath conventional roots.
Each file is limited to 256 KiB and total reads to 8 MiB. YAML nesting and
expanded alias structure are bounded separately. Exceeding a limit is an
explicit failure, not a silently complete inventory.
`yaml.limit` distinguishes a structure-budget failure from malformed YAML.

## Private output and source trust

Inspection can contain private source identities, names, paths and authored
descriptions. It is unavailable while destination redaction is enabled.
Use `SITEOPS_REDACT_OUTPUT=0` only for an authorized private destination.
JSON contains its preview version, projection, source context, processing
status, counts, entries and diagnostics. It is not a deployment plan.
The `local-private` projection label describes the authorized output
destination, not whether the content source is local or remote.
`nameInventoryComplete` distinguishes complete name discovery from descriptive
metadata quality. A null value means no name inventory was performed.

A local clone or materialized directory does not become a verified package
because of its location. Browsing does not inspect Git remotes or claim
publisher verification. Remote browsing consumes a separately generated
publication projection. It does not implement workspace acquisition or a
privileged gallery.

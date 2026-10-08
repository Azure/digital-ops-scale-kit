# Repository and workspace guide

This guide explains where behavior belongs and how a command moves from
workspace files to provider operations. Start here when extending the engine,
adding workspace content, or reviewing a change that crosses both layers.

## Product layers

| Layer | Location | Responsibility |
|---|---|---|
| Site Ops engine | `siteops/` | Generic workspace loading, validation, planning, execution, and output |
| IoT Operations content | `workspaces/iot-operations/` | AIO releases, manifests, templates, site conventions, resource definitions, and samples |
| Tests | `tests/` | Local engine and workspace assertions, workflow checks, packaging checks, and opt-in live scenarios |
| Delivery | `.github/` and `.pipelines/` | CI, deployment, integration, and release automation |
| Build and release tooling | `scripts/` and `releases/` | Engine and workspace package builders, installation bootstraps, CI helpers, and reviewed release declarations |
| Operator guidance | `README.md` and `docs/` | Getting started, operation, reference, migration, and release guidance |

Keep Azure IoT Operations meaning in the workspace. Add engine behavior only
when another workspace could use the same contract without importing AIO
names, assumptions, or release policy.

## From workspace to outcome

Browsing and execution have separate responsibilities. `browse` reads
descriptive content without loading Site values. A remote browse consumes a
published index tied to one source revision, not a remote execution filesystem.

Each command first selects its content and Site configuration. `-w PATH`
selects a local workspace. `--project DIRECTORY` supplies operator Sites and an
optional workspace pin, and `--source SOURCE@RELEASE` selects a verified
published release. Packaged content is verified, read from the protected
cache, and deployed with the ARM JSON its producer compiled. `inputs` can
instead construct one Site from a manifest's typed input contract.

Preparation and deployment follow one shared command path:

1. Load the workspace, site definitions, overlays, manifest, includes,
   parameter sources, and composition contracts.
2. Resolve targeting and prepare an immutable in-memory operation plan.
3. For executable planning, acquire template schemas, compile Bicep, and
   preflight the local capabilities selected operations require.
4. For deployment, execute the prepared operations per site with bounded
   concurrency and dependency ordering.
5. Render plan or deployment results with the selected local-private or
   publishable projection. Site inspection separately exposes the resolved
   configuration.

`validate` stops after compile-free structural checks. `plan` performs
executable preparation without Azure or Kubernetes mutation. `deploy`
prepares and executes. Deployment completion remains distinct from workload
readiness and functional verification.

Supporting commands manage the state those commands read. `project` inspects
or pins a project's workspace source, `source` inspects, enrolls or removes
approved sources, `index` builds a public content index, and `cache` inspects
cached storage or removes a selected entry.

## Workspace anatomy

| Directory | Operator meaning | Authoring responsibility |
|---|---|---|
| `sites/` | Where to deploy | Committed sites and reusable `SiteTemplate` defaults |
| `sites.local/` | Local target overrides | Gitignored overlays that cannot introduce inheritance |
| `manifests/` | Core operations | Named entry directories with a manifest and operator guide, plus shared `_partials/` |
| `parameters/` | Defaults and step bindings | Shared inputs, output chaining and release pins |
| `resource-sets/` | Reusable workload intent | Site-selected device, asset and dataflow definitions |
| `contracts/` | How governed collections compose | Resource identity, reference, and provider-seed rules |
| `templates/` | How provider resources or operations work | Bicep entry points, modules, and host-delivered content |
| `samples/` | Worked deployments | Self-contained bundles and compositions with their own prerequisites |

The engine recognizes the generic workspace shape. Field names beneath
`site.properties`, label conventions, AIO release keys, and resource-family
semantics belong to the workspace.

Public entries own optional `entry.yaml` guidance beside their manifest and
operator guide. The workspace's optional `content.yaml` names extra discovery
paths rather than repeating entry descriptions. Generated indexes are
publication artifacts, not another authored source or an executable package.
See [browsing](browse-content.md) and [remote indexes](remote-content.md).
The [workspace package producer](workspace-packages.md) keeps complete
content artifacts separate from those descriptive indexes and from the
engine installation bundle.

## Choose the owning layer

| Change | Owner |
|---|---|
| Add an AIO release, resource type, host operation, or sample | `workspaces/iot-operations/` |
| Add a reusable site, manifest, parameter, or composition rule | The workspace that defines its meaning |
| Change site loading, targeting, preparation, execution, or result contracts | `siteops/` |
| Change GitHub Actions or Azure Pipelines behavior | The matching delivery directory, with parity where the feature is shared |
| Document an operator task | `README.md`, `docs/`, or the nearest workspace guide |
| Document implementation and contribution flow | This guide and `CONTRIBUTING.md` |

Avoid adding an engine switch for one workspace convention. Prefer a generic
mechanism in the engine and express the product-specific choice in content.

## Workspace trust and writable state

Workspace files are executable deployment inputs. Review the checkout and any
extra trusted site directories before running with credentials.
`sites.local/` is the ordinary operator-owned override layer. It merges into a
matching committed site but cannot add `inherits:`.

Site inspection and local-private plans contain target identities and
configuration detail. Keep them in authorized destinations. Use explicit
publishable projections for CI artifacts and summaries.

### Separate content and Site configuration

`--project DIRECTORY` selects an operator project. Its `sites/`,
`sites.local/`, inheritance fallback and Site provenance labels then come from
the project, while manifests, templates and parameter libraries come from the
local workspace selected with `-w` or from the project's pinned package.
Without a project, a local workspace supplies both. Direct `--source` use
without a project accepts only an explicit Site from `--site-file`,
`--input-file` or `--input`. See [operator projects](projects.md).

Extra trusted Site directories augment the selected configuration root.
They are not a substitute for separating operator targets from packaged
examples. An empty operator root does not fall back to Sites owned by the content,
and Sites shipped inside a package remain content rather than deployment
targets. Package metadata cannot select the configuration root.

## Contributor route

Use [CONTRIBUTING.md](../CONTRIBUTING.md) for source setup and validation.
Use the [documentation index](README.md) for operator contracts before
changing a command or workspace shape. Changes to examples should preserve
the progression from inspection to validation, planning, deployment, and
outcome verification.

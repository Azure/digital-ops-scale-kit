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
| Operator guidance | `README.md` and `docs/` | Getting started, operation, reference, migration, and release guidance |

Keep Azure IoT Operations meaning in the workspace. Add engine behavior only
when another workspace could use the same contract without importing AIO
names, assumptions, or release policy.

## From workspace to outcome

Site Ops follows one command path:

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

## Workspace anatomy

| Directory | Operator meaning | Authoring responsibility |
|---|---|---|
| `sites/` | Where to deploy | Committed sites and reusable `SiteTemplate` defaults |
| `sites.local/` | Local target overrides | Gitignored overlays that cannot introduce inheritance |
| `manifests/` | What to run and in what order | Standalone entry points and reusable partials |
| `parameters/` | Values and reusable resource definitions | Shared inputs, output chaining, release pins, and resource sets |
| `contracts/` | How governed collections compose | Resource identity, reference, and provider-seed rules |
| `templates/` | How provider resources or operations work | Bicep entry points, modules, and host-delivered content |
| `samples/` | Worked deployments | Self-contained bundles and compositions with their own prerequisites |

The engine recognizes the generic workspace shape. Field names beneath
`site.properties`, label conventions, AIO release keys, and resource-family
semantics belong to the workspace.

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

## Contributor route

Use [CONTRIBUTING.md](../CONTRIBUTING.md) for source setup and validation.
Use the [documentation index](README.md) for operator contracts before
changing a command or workspace shape. Changes to examples should preserve
the progression from inspection to validation, planning, deployment, and
outcome verification.

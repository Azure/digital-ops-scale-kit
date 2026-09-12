# Documentation

Use this page to choose the shortest route for the task in front of you. New
operators should begin with the [one-site quickstart](getting-started.md),
which installs the CLI, obtains workspace content, prepares one target, and
separates validation, planning, deployment, and health verification.

## Begin locally

| Task | Guide |
|---|---|
| Deploy AIO to one prepared target | [First-site quickstart](getting-started.md) |
| Install an identified Site Ops release | [Install Site Ops](install-siteops.md) |
| Configure and inspect a deployment target | [Site configuration](site-configuration.md) |
| Understand the included AIO content | [IoT Operations workspace](../workspaces/iot-operations/README.md) |
| Diagnose a failed command or provider operation | [Troubleshooting](troubleshooting.md) |

Installing the CLI does not acquire a workspace. The included IoT Operations
workspace is currently obtained from this repository. Review both the CLI
release and the content checkout before deploying.

## Prepare and run deployments

Follow the command progression from read-only inspection to provider writes:

1. Use [site configuration](site-configuration.md) to inspect inheritance and
   overlays.
2. Use [site targeting](targeting.md) to select one site or a fleet.
3. Use the [manifest reference](manifest-reference.md) to understand the
   ordered operations.
4. Use [deployment plan output](plan-output.md) to review executable
   preparation without Azure or Kubernetes mutation.
5. Use [deployment run output](run-output.md) to interpret results,
   interruption, temporary files, and publication-safe output.

For advanced authoring:

- [Manifest includes](manifest-includes.md) covers reusable partials and
  composition.
- [Parameter resolution](parameter-resolution.md) covers merge order,
  template variables, and output chaining.

`validate` is compile-free structural checking. `plan` adds compilation and
local capability preflight. `deploy` performs provider operations. Neither a
valid plan nor a successful resource deployment establishes workload
readiness.

## Build Azure IoT Operations content

| Task | Guide |
|---|---|
| Select or upgrade an AIO release | [AIO releases](aio-releases.md) |
| Compose reusable workload definitions | [Resource catalog](resource-catalog.md) |
| Declare Device Registry devices and assets | [Assets](assets.md) |
| Declare endpoints, profiles, and dataflows | [Dataflows](dataflows.md) |
| Enable and operate Secret Sync | [Secret Sync](secret-sync.md) |
| Start from a deployable example | [Workspace samples](../workspaces/iot-operations/samples/README.md) |

These pages describe workspace content. The Site Ops engine remains
content-agnostic.

## Automate and qualify

| Task | Guide |
|---|---|
| Configure OIDC, protected environments, overrides, and deployment workflows | [CI/CD setup](ci-cd-setup.md) |
| Run selected live-subscription scenarios | [End-to-end testing](e2e-testing.md) |
| Publish private and public plan or run output safely | [Plan output](plan-output.md) and [run output](run-output.md) |

Hosted tests establish only the assertions selected by that workflow run.
They do not certify an arbitrary target, deployment, or AIO workload as ready
for production.

## Upgrade, release, or contribute

| Task | Guide |
|---|---|
| Update an existing workspace to the current preview contract | [Migration guide](migrating.md) |
| Prepare and publish a Scale Kit or Site Ops release | [Release guide](releasing.md) |
| Understand repository and workspace boundaries | [Repository and workspace guide](repository-guide.md) |
| Set up a development environment and submit changes | [Contributing](../CONTRIBUTING.md) |

## Core terms

| Term | Meaning |
|---|---|
| **Site Ops** | The generic CLI and orchestration engine under `siteops/` |
| **Workspace** | A directory containing sites, manifests, parameters, and templates, with optional contracts, samples, and local overlays |
| **Site** | A deployable target with subscription, optional resource group, location, labels, parameters, and properties |
| **SiteTemplate** | A reusable site base referenced through `inherits:` and not deployed directly |
| **Manifest** | An ordered set of operations with site targeting and parameter sources |
| **Partial** | A manifest intended for `include:` composition, conventionally named with a leading underscore |
| **Plan** | The prepared operation set produced without Azure or Kubernetes mutation |
| **Run result** | The final account of attempted, skipped, incomplete, and unconfirmed operations |
| **Resource set** | An ordered workspace selection of reusable AIO resource definitions |

For the directory responsibilities and the boundary between generic engine
behavior and AIO content, see the
[repository and workspace guide](repository-guide.md).

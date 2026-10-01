# Documentation

Use this page to choose the shortest route for the task in front of you. New
operators can [install Site Ops](install-siteops.md), then use
[guided inputs](guided-inputs.md) with a compatible published workspace.
For a local checkout and reusable Site
file, use the [configured-Site guide](getting-started.md). A separate plan
is optional: an interactive deploy reviews and confirms its own prepared
plan before execution. Health verification remains separate.

## Start with installed Site Ops

| Task | Guide |
|---|---|
| Install an identified Site Ops release | [Install Site Ops](install-siteops.md) |
| Deploy AIO to one explicit target | [Guided inputs](guided-inputs.md) |
| Install AIO and enable Secret Sync together | [Combined installation](guided-inputs.md#install-aio-with-secret-sync) |
| Enable Secret Sync on an existing AIO instance | [Existing-instance inputs](guided-inputs.md#enable-secret-sync-on-an-existing-instance) |
| Deploy AIO with a configured example Site | [Local checkout guide](getting-started.md) |
| Configure and inspect a deployment target | [Site configuration](site-configuration.md) |
| Understand the included AIO content | [IoT Operations workspace](../workspaces/iot-operations/README.md) |
| Find and inspect deployment choices | [Browse deployment content](browse-content.md) |
| Browse a published source without cloning | [Remote content indexes](remote-content.md) |
| Use typed answers or a complete Site file | [Guided inputs](guided-inputs.md) |
| Use configured Sites with packaged or local content | [Operator projects](projects.md) |
| Inspect storage or remove a cached entry safely | [Cache maintenance](cache.md) |
| Diagnose a failed command or provider operation | [Troubleshooting](troubleshooting.md) |

Installing the CLI does not acquire a workspace or approve a source. Follow
the selected release's installation and source instructions. With a compatible
published workspace and an independently approved consumer alias, begin
without pinning a project or writing an answer file:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

This private interactive command prepares, reviews, confirms and executes
one plan. Use `--yes` only for explicitly authorized unattended execution.
The target read and deployment still require your Azure identity. A local
checkout uses `-w`, while [operator projects](projects.md) add configured
Sites, fleet selection and optional offline-content reuse.

## Prepare and run deployments

For configured Sites, fleets and deeper inspection, choose the reference
you need. These are not prerequisites for the direct deployment above:

1. Use [site configuration](site-configuration.md) to inspect inheritance and
   overlays.
2. Use [site targeting](targeting.md) to select one site or a fleet.
3. Use the [manifest reference](manifest-reference.md) to understand the
   ordered operations.
4. Use [deployment plan output](plan-output.md) for a separate optional
   executable preview without deployment writes.
5. Use [deployment run output](run-output.md) to interpret results,
   interruption, temporary files, and publication-safe output.

For advanced authoring:

- [Manifest includes](manifest-includes.md) covers reusable partials and
  composition.
- [Parameter resolution](parameter-resolution.md) covers merge order,
  template variables, and output chaining.

`validate` is compile-free structural checking without target reads. `plan`
adds compilation and local capability preflight, with authorized prerequisite
reads when you supply typed resource IDs. `deploy` performs provider operations. Neither a
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
| Build a complete workspace content artifact | [Workspace packages](workspace-packages.md) |
| Identify workspace packages within a release | [Workspace release sources](workspace-sources.md) |
| Understand publisher policy and artifact evidence | [Artifact verification](artifact-verification.md) |
| Understand repository and workspace boundaries | [Repository and workspace guide](repository-guide.md) |
| Set up a development environment and submit changes | [Contributing](../CONTRIBUTING.md) |

## Core terms

| Term | Meaning |
|---|---|
| **Site Ops** | The generic CLI and orchestration engine under `siteops/` |
| **Workspace** | A directory containing sites, manifests, parameters, and templates, with optional contracts, samples, and local overlays |
| **Site** | A deployable target with subscription, optional resource group, location, labels, parameters, and properties |
| **Project** | An operator directory containing Site configuration and an optional workspace pin |
| **SiteTemplate** | A reusable site base referenced through `inherits:` and not deployed directly |
| **Manifest** | An ordered set of operations with site targeting and parameter sources |
| **Partial** | A manifest intended for `include:` composition, conventionally named with a leading underscore |
| **Plan** | The prepared operation set produced without Azure or Kubernetes mutation |
| **Run result** | The final account of attempted, skipped, incomplete, and unconfirmed operations |
| **Resource set** | An ordered workspace selection of reusable AIO resource definitions |

For the directory responsibilities and the boundary between generic engine
behavior and AIO content, see the
[repository and workspace guide](repository-guide.md).

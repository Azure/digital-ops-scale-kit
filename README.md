# Digital Operations Scale Kit

**Fleet-scale Azure infrastructure deployment.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in fleet-scale deployment, reach out at <aioteam@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of sites with a single command. Per-site customization, parallel execution, and failure isolation built in.

```bash
# Deploy to all production sites
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l "environment=prod"
```

---

## What this repository provides

Scale Kit has two layers:

- **Site Ops** is the generic, stateless orchestration engine. It loads a
  workspace, selects sites, prepares ordered operations, and executes Bicep,
  ARM, kubectl, and wait steps.
- **The IoT Operations workspace** is curated Azure IoT Operations content
  built on that engine. It owns the AIO manifests, release pins, templates,
  site conventions, and samples.

Installing Site Ops and obtaining workspace content are separate actions.
Site Ops releases provide the CLI. The IoT Operations workspace is currently
obtained from a repository checkout. Site Ops does not download or discover a
verified remote workspace package in this release.

Site Ops runs on demand without a persistent service or reconciliation loop.
ARM and Bicep remain responsible for Azure resource deployment. Site Ops adds
fleet targeting, ordered execution, output chaining, concurrency, and failure
isolation around those providers.

## Quick start

The recommended current route uses an identified Site Ops release with the
matching Scale Kit workspace checkout.

### Install the CLI

Select a Scale Kit release from the
[official releases](https://github.com/Azure/digital-ops-scale-kit/releases).
Its notes identify the compatible Site Ops release or build. Follow
[Install Site Ops from a release](docs/install-siteops.md), then confirm the
selected command. Choose a release that provides installation assets or links
to an engine release:

```bash
siteops --version
```

If you are changing the engine itself, use the source setup in
[CONTRIBUTING.md](CONTRIBUTING.md#development-setup) instead.

### Obtain the workspace content

Replace `<scale-kit-release-tag>` with the content release you selected:

```bash
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit
git checkout <scale-kit-release-tag>
```

The checkout is executable content. Review its manifests, templates, and
release notes before using it with deployment credentials.

### Prepare one target

The first IoT Operations deployment assumes:

- an existing resource group and Arc-connected Kubernetes cluster that meet
  [Azure IoT Operations prerequisites](https://learn.microsoft.com/azure/iot-operations/)
- Azure CLI on `PATH` and an authenticated identity for deployment
- permissions required by the selected templates. The included AIO install
  creates role assignments, so use Owner or User Access Administrator plus
  Contributor at the applicable scope
- Cluster Connect and Kubernetes RBAC when a selected manifest contains
  kubectl operations

The deployment creates or updates Azure resources and can incur charges.
Review the target, permissions, selected release, and cleanup responsibility
before continuing.

Create
`workspaces/iot-operations/sites.local/munich-dev.yaml` with values for an
existing target:

```yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev
subscription: "<subscription-id>"
resourceGroup: "<existing-resource-group>"
location: "<supported-azure-region>"
parameters:
  clusterName: "<existing-arc-cluster-name>"
```

`sites.local/` is gitignored. This overlay selects your target and deployment
region without changing the committed site. Other settings, including the AIO
release, remain inherited defaults. Inspect the resolved result:

```bash
siteops -w workspaces/iot-operations sites munich-dev --output yaml
```

Site inspection contains private configuration. Keep its output in an
authorized local destination.

### Validate and review the plan

```bash
siteops -w workspaces/iot-operations validate manifests/aio-install.yaml -l name=munich-dev
siteops -w workspaces/iot-operations plan manifests/aio-install.yaml -l name=munich-dev
```

`validate` performs compile-free structural checks. `plan` also compiles and
preflights the selected operations. Planning does not submit Azure
deployments or contact Kubernetes clusters, but compiler acquisition and
module restore can use the network. It does not establish Azure authorization,
cluster connectivity, or workload readiness.

### Deploy after review

```bash
az login
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l name=munich-dev
```

`deploy` applies the prepared operations and can leave partial changes when a
provider reports failure or the run is interrupted. It does not provide
automatic rollback. A successful deployment reports resource-operation
completion, not Azure IoT Operations readiness or application health. Verify
the promised outcome with the target's Azure and Kubernetes health signals.

Use `manifests/aio-upgrade.yaml` for an existing AIO installation. Reapplying
the installation manifest can overwrite operator-managed instance settings
and child resources.

## Site Ops command flow

| Command | Purpose | Side effects |
|---|---|---|
| `siteops sites` | List or inspect resolved sites | Reads workspace configuration |
| `siteops validate <manifest>` | Check structure, composition, paths, and static references | Reads workspace configuration |
| `siteops plan <manifest>` | Compile, preflight, and render an executable plan | May acquire local tooling or restore modules. Does not submit Azure or Kubernetes operations |
| `siteops deploy <manifest>` | Prepare and execute the selected operations | Creates or updates provider resources |

CLI selectors such as `-l environment=prod` override a manifest's default
targeting. Site inheritance and overlays provide per-site variation without
copying the deployment logic. See
[Site configuration](docs/site-configuration.md),
[targeting](docs/targeting.md), and
[plan output](docs/plan-output.md) for the complete contracts.

## Navigate by task

| Task | Start here |
|---|---|
| Configure a site or fleet | [Site configuration](docs/site-configuration.md) |
| Select sites safely | [Site targeting](docs/targeting.md) |
| Understand manifests and results | [Documentation by task](docs/README.md) |
| Operate the AIO workspace | [IoT Operations workspace](workspaces/iot-operations/README.md) |
| Move the same commands into automation | [CI/CD setup](docs/ci-cd-setup.md) |
| Extend the engine or workspace | [Repository and workspace guide](docs/repository-guide.md) |
| Contribute changes | [Contributing](CONTRIBUTING.md) |

## Repository map

| Area | Responsibility |
|---|---|
| `siteops/` | Generic workspace loading, preparation, orchestration, execution, and reporting |
| `workspaces/iot-operations/` | Azure IoT Operations product content |
| `docs/` | Operator, reference, migration, and contributor guidance |
| `tests/` | Engine, workspace, workflow, packaging, and live-scenario assertions |
| `.github/` and `.pipelines/` | GitHub Actions and Azure Pipelines delivery surfaces |

Keep AIO-specific behavior in workspace content. Engine behavior belongs in
`siteops/` only when it is reusable by another workspace.

## License

[MIT](LICENSE)

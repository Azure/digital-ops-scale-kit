# Digital Operations Scale Kit

**Fleet-scale Azure infrastructure deployment.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in fleet-scale deployment, reach out at <aioteam@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of sites with a single command. Per-site customization, parallel execution, and failure isolation built in.

---

## Why Scale Kit?

Keep one deployment workflow for your fleet and change only what varies by
site. Scale Kit combines reusable deployment content with Site Ops, the CLI
that runs it across your selected targets.

- **Reuse the same deployment across sites.** Compose templates and ordered
  steps once, then supply each site's subscription, resource group, and settings.
- **Target the right part of your fleet.** Select one site, an environment,
  or a labeled group with the same command.
- **Review before making changes.** Validate configuration and inspect a plan
  before submitting deployments.
- **See what happened at each site.** Run sites concurrently with failure
  isolation, and distinguish completed, failed, and skipped operations.

The same workspace and commands work locally and in CI/CD. Site Ops runs on
demand, with no persistent orchestration service to operate.

## Scale Kit and Site Ops

**Scale Kit** provides the deployment content and examples. Its included
IoT Operations workspace covers AIO installation, upgrades, workload resources,
and host lifecycle operations.

**Site Ops** is the reusable engine. It orchestrates your Bicep, ARM, kubectl,
and wait steps, so you can also use it with infrastructure beyond AIO.

A **site** describes a target and its settings. A **manifest** describes the
steps to run. A **workspace** groups those files with their templates and
parameters. Reusing a manifest across sites keeps deployment logic separate
from environment-specific configuration.

## Quick start

Start with one existing Arc-connected cluster. You do not need a repository
clone, saved Site or project pin.

### Install Site Ops

Follow the selected release's generated installation instructions, using a
compatible engine and complete IoT Operations workspace.

| Your environment | Installation route |
|---|---|
| uv is already available | Use the exact [release-wheel command](docs/install-siteops.md#install-the-release-wheel). Dependencies come from your approved uv package feed. |
| You need installation tooling | Use the generated [Windows or Linux bootstrap](docs/install-siteops.md#bootstrap-from-https). It proposes tool changes and verifies the installation archive. |

For independent script provenance before any installer code runs, use the
[verified bootstrap entry](docs/install-siteops.md#verify-the-bootstrap-script).
When choosing the bootstrap, add `--enroll-source official` or
`-EnrollSource official` to approve the official content source explicitly.
The bootstrap needs `curl` and GitHub CLI 2.95 or newer, and reports when
Azure CLI is missing.
Other installation routes use [independent source enrollment](docs/projects.md#use-an-approved-source).
Installing the CLI alone does not acquire or approve workspace content.

### Deploy AIO

Use an authorized Azure CLI identity and the source/version identified by
your release instructions. `official` is an independently approved consumer
alias, not authority supplied by downloaded content. Replace the cluster
placeholder with its full ARM resource ID:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Deploy prepares an executable plan once, displays its target and operation
scope in a private terminal, asks for confirmation, then executes that same
plan. Declared resource-ID answers authorize bounded Azure reads. Publisher
trust, read access and permission to deploy remain separate. For CI, JSON
output or any unattended invocation, pass `--yes` to approve execution
explicitly. It does not bypass validation or source approval. The command
does not sign you in, and deployment can incur charges. Follow the
[outcome check](docs/guided-inputs.md#check-the-result) after deployment.

To install AIO and enable Secret Sync together, add
`--input enableSecretSync=true` after confirming the cluster prerequisites.
To enable Secret Sync on an existing AIO 2607 or 2608 instance without
reinstalling, use the instance ID with the
[standalone Secret Sync route](docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance).
[Guided inputs](docs/guided-inputs.md) covers optional names and labels,
answer files, manual targets and all three routes. `plan` is available
when you want a separate preview. It is not a prerequisite for deployment.

### Scale out on the same model

Save Sites for repeatable fleet selection
and optionally pin a release in an operator project. Review only the new
targets before deploying them:

```text
siteops --approved-source official project pin factory --release <release>
siteops --approved-source official --project factory plan aio-install -l name=plant-two,name=plant-three
```

Deploy the same selector only after confirming it excludes already installed
clusters. Reapplying `aio-install` can overwrite their settings. A project
pin is an optional repeatability and `--offline-content` route, not a
prerequisite for the single-cluster command. Local workspaces and non-AIO
content also use the same planner and executor.
See [project Sites](docs/projects.md),
[fleet targeting](docs/targeting.md) and the
[local checkout guide](docs/getting-started.md) for experienced workflows.

## Browse deployment choices

With a pinned approved workspace, browse its packaged content without a
checkout:

```bash
siteops --approved-source official --project factory browse aio-install
```

The following `-w` commands require a local checkout. Inspect local
operations and samples before supplying deployment inputs:

```bash
siteops -w workspaces/iot-operations browse
siteops -w workspaces/iot-operations browse aio-install
siteops -w workspaces/iot-operations browse --tag mqtt
```

Browsing reads descriptions and authored guidance without loading Site values
or invoking deployment tools. Use the same name with `plan` and `deploy`,
or select the explicit path shown on the card. See [browse deployment content](docs/browse-content.md)
for filters, private JSON and the installation-to-workload journey.

Use `browse --source` to inspect a source's published index without cloning.
[Remote browsing](docs/remote-content.md) identifies the exact source revision
and links to its pinned guides. It does not acquire deployable workspace
content or grant deployment permissions.

## Learn and extend

| Task | Start here |
|---|---|
| Understand concepts and find a guide | [Documentation by task](docs/README.md) |
| Configure and target a fleet | [Sites](docs/site-configuration.md) and [targeting](docs/targeting.md) |
| Choose an AIO operation or sample | [IoT Operations workspace](workspaces/iot-operations/README.md) |
| Run deployments in automation | [CI/CD setup](docs/ci-cd-setup.md) |
| Build on the engine or contribute content | [Contributing](CONTRIBUTING.md) and [repository guide](docs/repository-guide.md) |

## License

[MIT](LICENSE)

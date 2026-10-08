# Digital Operations Scale Kit

**Azure infrastructure deployment at fleet scale.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in deployment at fleet scale, reach out at <aioteam@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of Sites with a single command. Customization for each Site, parallel execution, and failure isolation are built in.

---

## Why Scale Kit?

Keep one deployment workflow for your fleet and change only what varies by
Site. Scale Kit combines reusable manifests, templates and parameters with
Site Ops, the CLI that runs them across your selected Sites.

- **Reuse the same manifest across Sites.** Compose templates and ordered
  steps once, then supply each Site's subscription, resource group, and settings.
- **Target the right part of your fleet.** Select one Site, an environment,
  or a labeled group with the same command.
- **Review before making changes.** Validate configuration and inspect a plan
  before submitting deployments.
- **See what happened at each Site.** Run Sites concurrently with failure
  isolation, and distinguish completed, failed, and skipped operations.

The same workspace and commands work locally and in CI/CD. Site Ops runs on
demand, with no persistent orchestration service to operate.

## Scale Kit and Site Ops

**Scale Kit** provides the content: manifests, templates, parameters and
samples. Its included
IoT Operations workspace covers AIO installation, upgrades, workload resources,
and host lifecycle operations.

**Site Ops** is the reusable engine. It orchestrates your Bicep, ARM, kubectl,
and wait steps, so you can also use it with infrastructure beyond AIO.

A **Site** describes one deployment target and its settings. A **manifest**
describes the steps to run. A **workspace** groups those files with their
templates and parameters. Reusing a manifest across Sites keeps deployment
logic separate from the configuration of each environment.

## Quick start

Start with one existing cluster connected to Azure Arc. You do not need a repository
clone, saved Site or project pin.

### Install Site Ops

Use the selected release's generated installation instructions. They install
the Site Ops engine only.

| Your environment | Installation route |
|---|---|
| uv is already available | Use the exact [release wheel command](docs/install-siteops.md#install-the-release-wheel). Dependencies come from your approved uv package feed. |
| You need installation tooling | Use the generated [Windows or Linux bootstrap](docs/install-siteops.md#bootstrap-from-https). It proposes tool changes and verifies the installation archive. |

For independent script provenance before any installer code runs, use the
[verified bootstrap route](docs/install-siteops.md#verify-the-bootstrap-script).
Site Ops uses GitHub CLI 2.95 or newer, without a login, to verify published
content. The bootstrap also needs it, along with `curl`, and reports when
Azure CLI is missing.
Installing the CLI alone does not acquire workspace content or enroll a
source.

### Enroll the official content source

Enroll the official publisher once, under a name you choose. This example
uses `official`:

```text
siteops source enroll official
```

The enrollment accepts releases that the official publisher builds from
its main branch, for 30 days. Run the same command again to renew. The bootstrap
does the same when you add `--enroll-source official` or
`-EnrollSource official`. See [approved sources](docs/projects.md#use-an-approved-source)
for other publishers and custom policies.

### Deploy AIO

Use an authorized Azure CLI identity and the release named by your release
instructions. `official` is the approved source you enrolled independently
above, not authority supplied by downloaded content. Replace the cluster
placeholder with its full ARM resource ID:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

The command reads the cluster to fill in its subscription, resource group
and region. In a private terminal, it shows the Sites and operations of
one prepared plan and asks for confirmation before executing that plan. For
CI, JSON output or any other unattended run, pass `--yes` to confirm
execution. Validation and approved source checks still apply. The command
does not sign you in, and deployment can incur charges. Follow the
[outcome check](docs/guided-inputs.md#check-the-result) after deployment.

To install AIO and enable Secret Sync together, add
`--input enableSecretSync=true` after confirming the cluster prerequisites.
To enable Secret Sync on an existing AIO 2607 or 2608 instance without
reinstalling, use the instance ID with the
[standalone Secret Sync route](docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance).
[Guided inputs](docs/guided-inputs.md) covers optional names and labels,
answer files, manually described Sites and all three routes. `plan` is available
when you want a separate preview. It is not a prerequisite for deployment.

### Scale out on the same model

[Save Sites](docs/guided-inputs.md#keep-a-site-for-later) for repeatable
fleet selection and optionally pin a release in an operator project. Review
only the new Sites before deploying them:

```text
siteops --approved-source official project pin ./factory --release <release>
siteops --approved-source official --project ./factory plan aio-install -l name=plant-two,name=plant-three
```

Deploy the same selector only after confirming it excludes already installed
clusters. Reapplying `aio-install` can overwrite their settings. A project
pin is an optional route for repeatability and `--offline-content`, not a
prerequisite for the command that deploys one cluster. Local workspaces and
content beyond AIO also use the same planner and executor.
See [project Sites](docs/projects.md),
[fleet targeting](docs/targeting.md) and the
[local checkout guide](docs/getting-started.md) for experienced workflows.

## Browse deployment choices

With a workspace pinned from an approved source, browse its packaged content
without a checkout:

```bash
siteops --approved-source official --project ./factory browse aio-install
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
or select the explicit path shown on the card. See [browse manifests](docs/browse-content.md)
for filters, private JSON and the journey from installation to workload.

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

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

**[Start with AIO on one site](docs/getting-started.md).**

The quickstart takes an existing Arc-connected Kubernetes cluster through
installing the CLI, filling in one site file, reviewing a plan, and deploying.
It keeps prerequisites and commands together, so you can follow one path
without first reading the reference documentation.

Deployment creates or updates Azure resources and can incur charges.
Once that first site is working, expand the selector to deploy the same
manifest across your fleet.

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

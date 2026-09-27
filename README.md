# Digital Operations Scale Kit

**Fleet-scale Azure infrastructure deployment.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in fleet-scale deployment, reach out at <aioteam@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of sites with a single command. Per-site customization, parallel execution, and failure isolation built in.

With a configured Site inventory in a local checkout, review the production
fleet before deploying:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml -l "environment=prod"
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

Start with one existing Arc-connected Kubernetes cluster and an authorized
Azure identity. Select an identified Site Ops release that publishes the
bootstrap scripts and an approved Scale Kit release containing the complete
IoT Operations workspace. If your selected release has no bootstrap assets,
use the [manual installation routes](docs/install-siteops.md#install-the-release-wheel).
The new bootstrap scripts and typed AIO workspace are not yet published in
official releases. Use the commands below only after both identified releases
list the required assets. Until then, use the compatible released installation
and [local checkout](docs/getting-started.md) routes.

| Install route | Initial script trust |
|---|---|
| [Quick HTTPS bootstrap](docs/install-siteops.md#bootstrap-from-https) | The official HTTPS endpoint. The script then verifies the engine ZIP before installation. |
| [Verify before running](docs/install-siteops.md#verify-the-bootstrap-script) | Check the script's detached proof and exact publisher/source identity first. Requires GitHub CLI, but no GitHub login. |

For Ubuntu 24.04 or Azure Cloud Shell on managed Azure Linux 3, copy the
release's exact tag and full source commit into
the quick route below. Download completes before the script runs. See the
[Windows instructions](docs/install-siteops.md#bootstrap-from-https), or the
verified route above when you need to authenticate the script before running
it. Cloud Shell needs an approved configured Python package index if pipx is
missing. [Managed, direct wheel and manual bundle installation](docs/install-siteops.md)
remain available.

```bash
tag="<approved-Site-Ops-release>"; sha="<full-source-commit>"
(
  umask 077
  script="$(mktemp)"; trap 'rm -f -- "$script"' EXIT
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-redirs 3 --max-time 120 --output "$script" \
    "https://github.com/Azure/digital-ops-scale-kit/releases/download/${tag//\//%2F}/siteops-bootstrap.sh" &&
    bash "$script" --release "$tag" --source-commit "$sha" \
      --with-azure-cli --enroll-source official
)
```

Bootstrap proposes tool changes for consent and explicitly enrolls the
official content source because `--enroll-source official` was selected.
It does not sign in to Azure or deploy. Use your intended authorized Azure
CLI identity before planning with `--read-resources`. If you are starting in
a Codespace or another host without an Azure session, use `az login` and
confirm the selected subscription privately. The cluster and
resource group must already exist, and deployment can incur charges.

Then select the approved workspace release, inspect its typed input
contract, and prepare one target. Fill `siteName`, `environment`, `country`
in the generated file, then replace `cluster: null` with the full Arc
cluster resource ID. The generated example includes `cluster: null` as
the optional resource route. The four manual target fields can stay
`null` because the explicit resource read derives them. Review the Site and selected
operations in the plan before deploying:

```text
siteops --approved-source official project pin factory --release <approved-Scale-Kit-release>
siteops --approved-source official --project factory inputs aio-install --example aio-inputs.yaml
# Edit the generated answer file before proceeding.
siteops --approved-source official --project factory plan aio-install --input-file aio-inputs.yaml --read-resources
siteops --approved-source official --project factory deploy aio-install --input-file aio-inputs.yaml --read-resources
```

If the content release includes more than one workspace, add
`--release-workspace` with the exact path in its descriptor to `project pin`.
See [operator projects](docs/projects.md#run-project-pin).
The first path leaves Secret Sync disabled. [Guided inputs](docs/guided-inputs.md)
explains manual answers without an Azure read, Secret Sync prerequisites and
the same invocation with enablement selected. Planning does not submit
deployments. Deployment success does not establish AIO workload health.

**Scale out on the same model.** Save the first Site for repeat use, then
configure a separate set of new clusters as project Sites. Review only
those new targets with the same `aio-install` manifest:

```text
siteops --approved-source official --project factory plan aio-install -l name=plant-two,name=plant-three
```

Deploy that selection only after reviewing its targets. Reapplying
`aio-install` to the first cluster can overwrite settings there. If its
answer file enabled Secret Sync, use a separate
[fleet answer file](docs/guided-inputs.md#keep-a-site-for-later)
with `enableSecretSync: false` and no conditional vault input for the
new Sites. A label selector is convenient once it matches
only the intended cohort. Local workspaces and non-AIO content also use
the generic Site Ops planner and executor.
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

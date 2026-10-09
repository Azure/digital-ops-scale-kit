# Deploy AIO from a local checkout

This page deploys AIO from a local checkout, the route for authoring or
changing workspace content. To deploy one Site from a published release
without a clone or handwritten Site file, use the
[guided path for one Site](guided-inputs.md). For configured Sites and
repeatable fleet deployments from a pinned release, use an
[operator project](projects.md).

Use one existing Kubernetes cluster connected to Azure Arc to learn the
workflow: configure a Site, review a plan, then deploy Azure IoT Operations.
The same manifest can later run across more Sites by changing the selector.

This guide installs the AIO platform. For a cluster that already runs AIO,
follow [the upgrade guide](aio-releases.md) instead. Reapplying the install
manifest can overwrite instance settings and child resources that operators
manage.

## Before you start

You need:

- Git and an installed Site Ops from one of the [supported routes](install-siteops.md).
- Azure CLI available as `az`.
- An existing resource group and a Kubernetes cluster connected to Azure Arc that meet
  [Azure IoT Operations requirements](https://learn.microsoft.com/azure/iot-operations/).
- Deployment permissions at the applicable scope. The included templates
  create role assignments, so use Owner or User Access Administrator plus
  Contributor.

Deployment creates or updates resources and can incur Azure charges. Choose a
subscription and cluster you are authorized to use and keep track of the resources you create
for later cleanup.

## 1. Get the CLI and workspace

Choose a [Scale Kit release](https://github.com/Azure/digital-ops-scale-kit/releases).
Install the Site Ops engine that its release notes name by following
[Install Site Ops](install-siteops.md).
Then confirm the command is available:

```bash
siteops --version
```

The CLI and workspace content are separate. Obtain the workspace from the
same content release, replacing `<scale-kit-release-tag>`:

```bash
git clone --branch "<scale-kit-release-tag>" --depth 1 https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit
```

Run the remaining commands from this repository directory. Review the release
notes, manifests, and templates before using the checkout with credentials.
If you are developing from source, use
[the contributor setup](../CONTRIBUTING.md#development-setup) instead of the
release installation step.

## 2. Configure your Site

Inspect the installation's purpose, requirements and effects before supplying
Site values:

```bash
siteops -w workspaces/iot-operations browse aio-install
```

The card is authored guidance, not an environment assessment. Browse other
choices with `siteops -w workspaces/iot-operations browse`. See
[content inspection](browse-content.md) for filtering and the `basic-routing`
journey on the same Site.

Create the `workspaces/iot-operations/sites.local/` directory and save the
following as `munich-dev.yaml`. Replace every placeholder with your Site's
values:

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

`munich-dev` is the example Site's name, not the name your cluster must have.
This file overlays the example Site's settings and is gitignored. Other
settings, including the selected AIO release and broker configuration, remain
inherited defaults.

Inspect the resolved configuration:

```bash
siteops -w workspaces/iot-operations sites munich-dev --output yaml
```

Confirm the subscription, resource group, cluster name, region, and
`properties.aioRelease` before continuing. Site inspection contains private
configuration, so keep its output in an authorized local destination.
See [Site configuration](site-configuration.md) when you need to change
inherited settings.

## 3. Review, then deploy

Check the configuration and prepare a plan:

```bash
siteops -w workspaces/iot-operations validate aio-install -l name=munich-dev
siteops -w workspaces/iot-operations plan aio-install -l name=munich-dev
```

`validate` checks structure without compilation. `plan` also compiles templates
and preflights selected local capabilities. Neither command submits Azure
deployments or contacts Kubernetes clusters. Planning can acquire a compiler
or restore modules, so private module sources need their credentials available.
It does not establish Azure authorization or cluster readiness.
If you change the Site or workspace files after planning, review a new plan
before deploying. `deploy` prepares its inputs again when you run it.

After reviewing the plan, authenticate with the intended Azure identity and
deploy only this Site:

```bash
az login
siteops -w workspaces/iot-operations deploy aio-install -l name=munich-dev
```

Deployment applies the selected operations. Failure or interruption can leave
partial changes, with no automatic rollback. Read
[deployment results](run-output.md) before retrying an incomplete run.

## Confirm the outcome

A successful Site Ops result reports that resource operations completed. Check the
AIO extension provisioning state and component health in Azure and Kubernetes
to establish readiness before adding a workload. Additional workload outcomes
have their own checks in the [sample guides](../workspaces/iot-operations/samples/README.md).

Keep your Site file for repeat use. Remove created resources deliberately
when finished, preserving the existing cluster and other resources you still
need.

## Expand to more Sites

Configure the other Sites and give them labels that describe how you want to
target them. You can keep the same manifest and select the fleet with
`-l "environment=prod"`.
Review the wider selection first:

```bash
siteops -w workspaces/iot-operations plan aio-install -l "environment=prod"
```

After review, use `deploy` with the same manifest and selector. See
[targeting](targeting.md) for label matching, [Site configuration](site-configuration.md)
for inheritance, and [CI/CD setup](ci-cd-setup.md) to run the same workflow in
automation.

# Deploy AIO from a local checkout

For an approved workspace package with a typed input contract, use the
[single-Site guided path](guided-inputs.md). It needs no repository clone or
handwritten Site file. This page retains the local checkout route for
operators who want a reusable configured Site and fleet targeting.

Use one existing Arc-connected Kubernetes cluster to learn the workflow:
configure a site, review a plan, then deploy Azure IoT Operations.
The same manifest can later run across more sites by changing the selector.

This guide installs the AIO platform. For a cluster that already runs AIO,
follow [the upgrade guide](aio-releases.md) instead. Reapplying the install
manifest can overwrite operator-managed instance settings and child resources.

## Before you start

You need:

- Git and the Python/pipx prerequisites in [Install Site Ops](install-siteops.md).
- Azure CLI available as `az`.
- An existing resource group and Arc-connected Kubernetes cluster that meet
  [Azure IoT Operations requirements](https://learn.microsoft.com/azure/iot-operations/).
- Deployment permissions at the applicable scope. The included templates
  create role assignments, so use Owner or User Access Administrator plus
  Contributor.

Deployment creates or updates resources and can incur Azure charges. Choose a
target you are authorized to use and keep track of the resources you create
for later cleanup.

## 1. Get the CLI and workspace

Choose a [Scale Kit release](https://github.com/Azure/digital-ops-scale-kit/releases)
that provides installation assets or links to a compatible Site Ops release.
Use an installation route that the selected engine release actually publishes:
the [verified bundle or direct wheel](install-siteops.md), a bootstrap script
with its proof when available, or your managed software channel. A verified
bootstrap route authenticates the script before execution. Match the engine
version required by this content.
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

## 2. Set your target

Inspect the installation's purpose, requirements and effects before supplying
target values:

```bash
siteops -w workspaces/iot-operations browse aio-install
```

The card is authored guidance, not an environment assessment. Browse other
choices with `siteops -w workspaces/iot-operations browse`. See
[content inspection](browse-content.md) for filtering and the same-Site
basic-routing journey.

Create the `workspaces/iot-operations/sites.local/` directory and save the
following as `munich-dev.yaml`. Replace every placeholder with your target's
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

`munich-dev` is the example site's name, not the name your cluster must have.
This file overlays the example's target settings and is gitignored. Other
settings, including the selected AIO release and broker configuration, remain
inherited defaults.

Inspect the resolved configuration:

```bash
siteops -w workspaces/iot-operations sites munich-dev --output yaml
```

Confirm the subscription, resource group, cluster name, region, and
`properties.aioRelease` before continuing. Site inspection contains private
configuration, so keep its output in an authorized local destination.
See [site configuration](site-configuration.md) when you need to change
inherited settings.

## 3. Review, then deploy

Check the configuration and prepare a plan:

```bash
siteops -w workspaces/iot-operations validate manifests/aio-install/manifest.yaml -l name=munich-dev
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml -l name=munich-dev
```

`validate` checks structure without compilation. `plan` also compiles templates
and preflights selected local capabilities. Neither command submits Azure
deployments or contacts Kubernetes clusters. Planning can acquire a compiler
or restore modules, so private module sources need their credentials available.
It does not establish Azure authorization or target readiness.
If you change the site or workspace files after planning, review a new plan
before deploying. `deploy` prepares its inputs again when you run it.

After reviewing the plan, authenticate with the intended Azure identity and
deploy only this site:

```bash
az login
siteops -w workspaces/iot-operations deploy manifests/aio-install/manifest.yaml -l name=munich-dev
```

Deployment applies the selected operations. Failure or interruption can leave
partial changes, with no automatic rollback. Read
[deployment results](run-output.md) before retrying an incomplete run.

## Confirm the outcome

A successful Site Ops result reports resource-operation completion. Check the
AIO extension provisioning state and component health in Azure and Kubernetes
to establish readiness before adding a workload. Additional workload outcomes
have their own checks in the [sample guides](../workspaces/iot-operations/samples/README.md).

Keep your site file for repeat use. Remove created resources deliberately
when finished, preserving the existing cluster and other resources you still
need.

## Expand to more sites

Configure the other sites and give them labels that describe how you want to
target them. You can keep the same manifest and select the fleet with
`-l "environment=prod"`, as in the repository's opening example.
Review the wider selection first:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml -l "environment=prod"
```

After review, use `deploy` with the same manifest and selector. See
[targeting](targeting.md) for label matching, [site configuration](site-configuration.md)
for inheritance, and [CI/CD setup](ci-cd-setup.md) to run the same workflow in
automation.

# Install Azure IoT Operations

Install AIO on an existing Kubernetes cluster connected to Azure Arc. For a
first deployment with a workspace package from an approved source, follow
[guided inputs](../../../../docs/guided-inputs.md) to construct one
explicit Site. For a local checkout or configured fleet, follow
[Deploy AIO from a local checkout](../../../../docs/getting-started.md) to prepare
an authorized Site overlay. Both routes execute this same manifest.

Use [aio-upgrade](../aio-upgrade/README.md) to change versions in place.
Reapplying installation can overwrite instance and child resource settings
that operators manage.

## Configure the Site

For guided inputs, supply only the existing Arc cluster ID and authorize
its read. Site Ops derives the Site values and generates a stable Site name.
`siteName`, `environment` and `country` remain optional overrides on
this resource route. Supplied labels become resource tags, while omitted
labels are not invented. The manual route retains explicit Site fields.

Confirm the Site's subscription, resource group, location and
`parameters.clusterName`. The cluster and resource group must already exist.
Confirm the inherited `properties.aioRelease` and that the cluster meets its
[AIO prerequisites](https://learn.microsoft.com/azure/iot-operations/).

Shared naming defaults derive the names of the instance, custom location,
Schema Registry and ADR namespace from the Site name. Ordinary Site parameters can override
those defaults. Keep an existing explicit Site name when reusing configuration.
IDs produced by earlier operations are supplied to their
consuming steps, rather than entered by the operator.

Optional `properties.deployOptions` settings include `enableEdgeSite`,
`enableSecretSync` and `enableCertManager`. Secret Sync requires OIDC and
workload identity on the existing cluster. Host bootstrap flags do not enable
those features through this manifest. An Edge Site scoped to the subscription
needs a Site at subscription level, not just a flag on a Site at resource
group level.
The guided resource ID route checks the reported identity prerequisites
before writes when Secret Sync is enabled. Configured fleet Sites do not
repeat that guided read, so confirm the prerequisites for each selected
cluster before enabling Secret Sync across a fleet.

## Review and deploy

With a compatible published workspace and an `official` approved source that
you enrolled independently, supply the existing Arc cluster ID:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Use the release identified by its instructions. Deploy prepares, displays
and confirms one plan in a private terminal. Add `--yes` only for explicitly
authorized unattended execution. The resource ID authorizes bounded reads of
the cluster, not additional Azure permissions.

For a local checkout and configured Sites, run from the repository root
and replace `<site>` with the configured Site:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-install/manifest.yaml -l name=<site>
```

The explicit selector replaces the manifest's `environment=dev` default.
Review the plan before deploying. Deployment prepares again rather than
executing a saved copy of that preview.

## Effects and outcome

Installation can create or update Arc extensions, the AIO instance and
children, a custom location, an ADR namespace, Schema Registry storage and
role assignments. Optional capabilities add their own resources.
Deployment can incur Azure charges and needs permission to create role assignments at
the applicable scope, commonly Owner or User Access Administrator plus
Contributor.

An executable plan establishes local preparation, not Azure authorization
or cluster readiness. Deployment success reports provider operations.
Observe AIO readiness before adding a workload, then use that workload's
functional procedure.

For a small workload, use [basic routing](../../samples/resource-set-basic/README.md)
on the same Site. Its guide explains the `catalog-basic` route and reuse of a
differently named existing instance.

## Removal

There is no automatic rollback or general uninstall operation. Remove
created resources deliberately, preserving the existing cluster and resources
you intend to keep. Retain the Site configuration and deployment results
needed to understand partial changes.

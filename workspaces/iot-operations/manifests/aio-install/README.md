# Install Azure IoT Operations

Install AIO on an existing Arc-connected Kubernetes cluster. For a first
deployment with an approved workspace package, follow
[guided inputs](../../../../docs/guided-inputs.md) to construct one
explicit Site. For a local checkout or configured fleet, use the
[configured-Site guide](../../../../docs/getting-started.md) to prepare
an authorized Site overlay. Both routes execute this same manifest.

Use [aio-upgrade](../aio-upgrade/README.md) for in-place version changes.
Reapplying installation can overwrite operator-managed instance and child
resource settings.

## Configure the target

Confirm the Site's subscription, resource group, location and
`parameters.clusterName`. The cluster and resource group must already exist.
Confirm the inherited `properties.aioRelease` and that the target meets its
[AIO prerequisites](https://learn.microsoft.com/azure/iot-operations/).

Shared naming defaults derive instance, custom-location, Schema Registry and
ADR namespace names from the Site name. Ordinary Site parameters can override
those defaults. IDs produced by earlier operations are supplied to their
consuming steps, rather than entered by the operator.

Optional `properties.deployOptions` settings include `enableEdgeSite`,
`enableSecretSync` and `enableCertManager`. Secret Sync requires OIDC and
workload identity on the existing cluster. Host-bootstrap flags do not enable
those features through this manifest. A subscription-scoped Edge Site needs
an applicable subscription target, not just a flag on an RG-scoped Site.
The guided resource ID route checks the reported identity prerequisites
before writes when Secret Sync is enabled. Configured fleet Sites do not
repeat that guided read, so confirm the prerequisites for each selected
cluster before enabling Secret Sync across a fleet.

## Review and deploy

Run from the repository root and replace `<site>` with the configured Site:

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
Deployment can incur Azure charges and needs role-assignment permissions at
the applicable scope, commonly Owner or User Access Administrator plus
Contributor.

An executable plan establishes local preparation, not Azure authorization
or target readiness. Deployment success reports provider operations.
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

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

### Arc extension settings

Three maps of string keys and string values set Arc extension configuration.
Each key replaces the release or template value of that key. These settings
are visible in Azure, so never put secrets in them.

| Typed input | Site parameter | Arc extension |
|---|---|---|
| `aioExtensionSettings` | `aioConfigurationOverrides` | AIO |
| `secretStoreExtensionSettings` | `secretStoreConfigurationOverrides` | Secret store, with Secret Sync enabled or disabled |
| `certManagerExtensionSettings` | `certManagerConfigurationOverrides` | cert-manager, when `enableCertManager` is true |

On the guided route, add one `--input NAME=KEY=VALUE` to the deploy command
for each setting, for example
`--input "aioExtensionSettings=observability.metrics.exportIntervalSeconds=60"`.

An answer file holds the same maps. Quote keys and values, because YAML
reads an unquoted `on` as true and `1.10` as 1.1:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  cluster: "<Arc-cluster-resource-ID>"
  aioExtensionSettings:
    "observability.metrics.exportIntervalSeconds": "60"
```

Inline entries replace answer file entries key by key. A key that contains
`=` is supported only in the answer file. See
[guided inputs](../../../../docs/guided-inputs.md#set-arc-extension-settings)
for the complete grammar.

For a configured Site, set the Site parameters directly:

```yaml
parameters:
  aioConfigurationOverrides:
    "<setting.key>": "<value>"
  certManagerConfigurationOverrides:
    "<setting.key>": "<value>"
  secretStoreConfigurationOverrides:
    "<setting.key>": "<value>"
```

Replace the placeholders with settings supported by each extension.
Inherited Sites and `sites.local` overlays merge these maps key by key, so a
child Site adds or replaces keys but cannot remove one. An omitted or empty
typed map sets nothing. Neither route removes a key that the cluster
already has.

### Settings Scale Kit derives

This installation derives some AIO extension settings from other Site
values. A map key replaces any of them:

| AIO extension settings | Derived from |
|---|---|
| `trustSource`, `trustBundleSettings.issuer.name`, `trustBundleSettings.issuer.kind`, `trustBundleSettings.configMap.name`, `trustBundleSettings.configMap.key` | `parameters.trustConfig` |
| `dataFlows.values.wasmGraphController.mqttBroker.caCertConfigMapRef`, `dataFlows.values.wasmGraphController.mqttBroker.caCertFileName` | `parameters.trustConfig`, for AIO releases 2608 and 2609 |
| `connectors.values.mqttBroker.address`, `connectors.values.mqttBroker.serviceAccountTokenAudience`, `dataFlows.values.tinyKube.mqttBroker.hostName`, `dataFlows.values.tinyKube.mqttBroker.port`, `dataFlows.values.tinyKube.mqttBroker.authentication.serviceAccountTokenAudience`, `schemaRegistry.values.mqttBroker.host`, `schemaRegistry.values.mqttBroker.serviceAccountTokenAudience` | The cluster namespace and the default broker listener |
| `connectors.values.securityPki.applicationUri` | The cluster, for AIO 2603 and later |
| `connectors.values.securityPki.subjectName` | The cluster, for AIO 2605 and later |
| `observability.metrics.enabled`, `observability.metrics.openTelemetryCollectorAddress` | The instance template's observability defaults |

The curated installation keeps the trust, WebAssembly graph trust and broker
settings consistent with each other. Replacing those keys through a map is
outside that recipe. Use the dedicated settings instead:

- `parameters.trustConfig` for a trust bundle you manage
- `parameters.brokerConfig` for broker sizing and logging
- dataflow endpoints in [resource sets](../../resource-sets/README.md) for
  MQTT destinations outside the cluster
- your own manifests and templates for a materially different internal
  topology

For example, select an existing issuer and trust bundle:

```yaml
parameters:
  trustConfig:
    source: CustomerManaged
    settings:
      issuerName: "<existing-issuer-name>"
      issuerKind: ClusterIssuer
      configMapName: "<existing-trust-bundle-configmap>"
      configMapKey: "<certificate-key>"
```

`trustConfig` is a Site parameter, not a typed input. To use it on the guided
route, save the Site as the next paragraph describes. Replacing the OPC UA
certificate and observability settings above is ordinary use, for example to
match values that an OPC UA server already trusts or to send metrics to your
own collector.

To keep the guided answers for later, including settings and parameters
outside the typed inputs,
[save a Site](../../../../docs/guided-inputs.md#keep-a-site-for-later)
with `siteops inputs ... --save-site` into the configured inventory, edit
that file, then deploy with `-l name=<site>`.

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

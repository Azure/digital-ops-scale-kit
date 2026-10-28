# Upgrade Azure IoT Operations

Upgrade an existing AIO installation to a selected AIO release. The manifest
updates the Arc extensions for AIO and the resources that the selected AIO
release explicitly requires. Select the target with the Site's
`properties.aioRelease`, or with typed inputs for one existing instance.

The instance ARM resource has no writable version property. This operation
does not perform general migrations of brokers, dataflow profiles or other
instance children.

## Choose a supported upgrade path

Microsoft supports upgrading an instance to any patch of the same minor
version, or to the next minor version. To downgrade, or to move between a
preview and a generally available version in either direction, uninstall
and install the desired version instead. AIO does not upgrade live, so
expect downtime. See
[Upgrade Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/manage-iot-ops/howto-upgrade).

Each AIO release in this workspace installs one AIO version:

| AIO release | AIO version | Minor version |
|---|---|---|
| 2512 | 1.2.154 | 1.2 |
| 2602 | 1.2.189 | 1.2 |
| 2603 | 1.3.38 | 1.3 |
| 2604 | 1.3.70 | 1.3 |
| 2605 | 1.3.105 | 1.3 |
| 2606 | 1.3.137 | 1.3 |
| 2607 | 1.4.41 | 1.4 |
| 2608 | 1.4.73 | 1.4 |
| 2609 | 1.4.112 | 1.4 |

Before you choose a target, find the version the instance runs. The
instance's Overview page in the Azure portal shows it, and so does
`az iot ops show --name <instance> --resource-group <resource-group> --query properties.version`.
The AIO Arc extension's version, on the cluster's Extensions page or from
`az k8s-extension list --cluster-name <cluster> --resource-group <resource-group> --cluster-type connectedClusters --output table`,
matches the AIO version column exactly.

Move one minor version at a time. From a 1.2 release, upgrade to a 1.3
release before a 1.4 release. Never select an AIO release older than the one
the instance runs, because Microsoft does not support a downgrade.
`aio-upgrade` applies the AIO release you select without checking the path,
so choose a supported transition from the instance's current release.

The MQTT broker upgrades through rolling updates, which need a backend
redundancy factor of 2 or more. Microsoft does not support broker upgrades
with a factor of 1. Scale Kit installs with 2 by default, so keep
`parameters.brokerConfig.backendRedundancyFactor` at 2 or more.

Scale Kit's live tests have exercised selected adjacent upgrades: 2604 to
2605, 2606 to 2607 and 2608 to 2609. These tests support the content in
this workspace. They do not certify another cluster or upgrade path,
which Microsoft's guidance governs.

## Prepare the upgrade

For a configured Site, confirm it identifies the existing instance and
cluster. Update `properties.aioRelease` on the Site or its inherited
configuration. See [AIO releases](../../../../docs/aio-releases.md) for
AIO release selection.

When you saved the installation's Site with `siteops inputs ... --save-site`,
change `properties.aioRelease` in that file. Run this manifest with
`--site-file FILE`, or with `-l name=<site>` when the file is in your
configured Site inventory. Typed answers never change a saved Site. Prefer
the saved Site for an installation you configured, because an instance ID
cannot recover a custom Site name or earlier choices.

The AIO release pin also selects API versions for workload resources. Avoid
applying resource sets between changing the pin and completing the upgrade.
The extension update preserves existing configuration, release train and
identity settings, while applying the changes that the AIO release owns.

## Upgrade one instance with typed inputs

For an existing instance without a saved Site, supply its resource ID, the
target AIO release and your cert-manager answer:

```text
siteops deploy aio-upgrade --source "official@<release>" --input "instance=<AIO-instance-resource-ID>" --input aioRelease=2609 --input "enableCertManager=<true-or-false>"
```

Site Ops reads the instance, its custom location and that location's Arc
cluster with your Azure CLI identity. The related resources must stay in the
instance's subscription and resource group. The upgrade uses the actual
instance name. The Site name defaults to the cluster's name in lowercase.
Supply `siteName` to choose another, and the optional `environment` and
`country` inputs to label the Site. Run `siteops inputs aio-upgrade` with the
same `--source` to list every input.

`aioRelease` has no default, so the upgrade never selects a target for you.
`enableCertManager` has no default either. It answers the question "Include
the existing cert-manager Arc extension in this upgrade?" Answer `true` when
Scale Kit installed cert-manager and manages it, as an installation with the
default `enableCertManager` does, and the upgrade updates its version and
settings. Answer `false` when another owner manages cert-manager, and the
upgrade neither reads nor updates that extension. The instance cannot tell
Site Ops which applies.

The extension settings inputs from
[installation](../aio-install/README.md#arc-extension-settings) also apply
here. `certManagerExtensionSettings` needs `enableCertManager=true`. For
example, this answer file points AIO metrics at an OpenTelemetry collector
that you run separately:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  instance: "<AIO-instance-resource-ID>"
  aioRelease: "2609"
  enableCertManager: <true-or-false>
  aioExtensionSettings:
    "observability.metrics.enabled": "true"
    "observability.metrics.openTelemetryCollectorAddress": "aio-otel-collector.azure-iot-operations.svc.cluster.local:4317"
```

Replace each placeholder, then supply the file with `--input-file` instead
of the inline answers. A setting you made at installation for a key that the
AIO release also sets, such as the cert-manager settings of releases 2607 to
2609, returns to the release value on upgrade unless you supply it again.
[Settings Scale Kit derives](../aio-install/README.md#settings-scale-kit-derives)
lists the AIO settings that the installation computes. Deploy shows
the read resource IDs and the plan, then asks for confirmation. Add `--yes`
only for explicitly authorized unattended execution.

## Change extension settings

Each extension update starts from the settings already on the cluster, so
settings applied at installation stay in place and the same Site applies
them again. A setting from the AIO release replaces the cluster's value on
each upgrade unless the Site sets that key. Removing a key from the Site
never removes it from the cluster. Running installation again can overwrite
settings that you manage, because installation writes the settings without
reading the cluster's.

To change only settings, run `aio-upgrade` with the instance's current AIO
release and the new settings. It applies the same extension versions with
those settings and deploys the release's required resources again. Scale
Kit's live tests do not yet cover this route, so review the plan and check
extension provisioning afterward. Keep the current release for a settings
change rather than selecting a newer one.

## Review and deploy

From the repository root, replace `<site>` with the configured Site:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-upgrade/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-upgrade/manifest.yaml -l name=<site>
```

Use the intended Azure identity and permissions for the selected resource
updates. The plan performs local preparation, not an effective RBAC or
assessment of workload health. The explicit selector replaces the manifest's
default for the development fleet.

## Outcome and recovery

After the upgrade, observe extension provisioning and AIO workload health.
Then reapply the Site's selected workload definitions through
[aio-resources](../aio-resources/README.md).

An interrupted or failed upgrade can leave partial changes. There is no
general rollback. Read the reported operation outcomes and the release's
recovery guidance before retrying. Use the install manifest for a new
installation, not as a substitute for this upgrade path.

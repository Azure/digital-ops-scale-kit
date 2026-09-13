# Upgrade Azure IoT Operations

Upgrade an existing AIO installation using the Site's selected
`properties.aioRelease`. The manifest updates AIO-related Arc extensions and
resources explicitly required by the selected release.

The instance ARM resource has no writable version property. This operation
does not perform general migrations of brokers, dataflow profiles or other
instance children.

## Prepare the upgrade

Confirm the configured Site identifies the existing instance and cluster.
Choose a supported transition and update `properties.aioRelease` on the Site
or its inherited configuration. See the
[release reference](../../../../docs/aio-releases.md#supported-upgrade-paths)
for supported transitions and release selection.

The release pin also selects API versions for workload resources. Avoid
applying resource sets between changing the pin and completing the upgrade.
The extension update preserves existing configuration, release-train and
identity settings, while applying the release-owned changes.

## Review and deploy

From the repository root, replace `<site>` with the configured target:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-upgrade/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-upgrade/manifest.yaml -l name=<site>
```

Use the intended Azure identity and permissions for the selected resource
updates. The plan performs local preparation, not an effective RBAC or
workload-health assessment. The explicit selector replaces the manifest's
development-fleet default.

## Outcome and recovery

After the upgrade, observe extension provisioning and AIO workload health.
Then reapply the Site's selected workload definitions through
[aio-resources](../aio-resources/README.md).

An interrupted or failed upgrade can leave partial changes. There is no
general rollback. Read the reported operation outcomes and the release's
recovery guidance before retrying. Use the install entry for a new
installation, not as a substitute for this upgrade path.

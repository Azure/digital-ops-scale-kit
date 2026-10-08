# AIO Releases

Azure IoT Operations (AIO) ships on a release cadence. Each AIO release pins specific versions of the AIO extension, cert-manager, secret store, and a matching control plane API version. Scale Kit represents every supported AIO release as a release config file under `workspaces/iot-operations/parameters/aio-releases/` and selects one for each Site via `site.properties.aioRelease`.

## How release selection works

```
site.properties.aioRelease: "2608"
            │
            ▼
workspaces/iot-operations/parameters/aio-releases/2608.yaml
            │
            ▼  (siteops auto-forwards matching params to Bicep)
templates/aio/enablement.bicep       ──► cert-manager, secret store extensions
templates/aio/instance.bicep         ──► AIO extension + instance (dispatches on aioApiVersion)
templates/secretsync/enable-secretsync.bicep  ──► instance update (dispatches on aioApiVersion)
templates/deps/adr-ns.bicep          ──► ADR namespace (dispatches on adrApiVersion)
```

Each AIO release YAML has a stable schema at the top level.
`aioReleaseConfiguration` holds behavior owned by the AIO release that can
differ even when two AIO releases use the same ARM API generation:

```yaml
# parameters/aio-releases/2608.yaml
aioVersion: "1.4.73"            # AIO extension version pinned in Arc
aioTrain: stable                # Extension release train
aioApiVersion: "2026-07-01"     # Microsoft.IoTOperations/instances API version
adrApiVersion: "2026-04-01"     # Microsoft.DeviceRegistry/namespaces API version
aioReleaseConfiguration:
  extension:
    wasmGraphControllerMqttTrust: true
  resources:
    opcUaConnector:
      version: "1.4.10"
certManagerVersion: "1.0.0"
certManagerTrain: stable
certManagerConfigurationOverrides:
  trust-manager.secretTargets.enabled: "false"
  trust-manager.secretTargets.authorizedSecretsAll: "false"
secretStoreVersion: "1.5.2"
secretStoreTrain: stable
```

The `aioApiVersion` and `adrApiVersion` values route CREATE and UPDATE operations through their matching versioned modules (for example `templates/aio/modules/instance-2026-07-01.bicep` and `templates/deps/modules/adr-ns-2026-04-01.bicep`). Configuration override objects specific to an AIO release are forwarded automatically to templates that declare them. Bicep cannot parameterize API version strings, so the dispatchers use `@allowed` + conditional modules. See [Adding a new AIO release](#adding-a-new-aio-release) below.

The AIO release 2608 configuration also deploys the OPC UA connector template
and supplies the MQTT trust settings required by its WebAssembly graph
controller. Install and upgrade use the same typed connector template module.
Settings under `aioReleaseConfiguration.extension` belong to the AIO Arc
extension. Settings under `aioReleaseConfiguration.resources` describe child
resources that the AIO release requires. `extension.configurationOverrides` is
available for static extension settings owned by the AIO release. Site or
deployment overrides remain in the `aioConfigurationOverrides` parameter at
the top level and take precedence. Resources that the AIO release requires
have no separate Site override because they describe the components that the
AIO release needs.
`extension.securityPkiSubjectName` enables the OPC UA certificate subject for
AIO releases where it is not yet a default of the API generation.
`extension.wasmGraphControllerMqttTrust` enables the derived MQTT trust settings
required by the WebAssembly graph controller.

## Pinning a Site to an AIO release

Set `properties.aioRelease` on the Site (or on a parent via inheritance). The value must be the filename (without extension) of a YAML under `parameters/aio-releases/`.

```yaml
# sites/munich-prod.yaml
apiVersion: siteops/v1
kind: Site
name: munich-prod
inherits: base-site.yaml

properties:
  aioRelease: "2608"    # must match parameters/aio-releases/2608.yaml
```

If not specified, the Site inherits the value from its parent template. Every committed Site in this workspace inherits `base-site.yaml`, directly or through a shared template, and `base-site.yaml` declares `"2608"`. A manifest that loads the release file fails preparation for a Site that carries no `aioRelease` at all, reporting that the Site does not carry the property the path selects on.

## Available AIO releases

Every file in `workspaces/iot-operations/parameters/aio-releases/` is a shipped AIO release:

| AIO release | `aioApiVersion` | `adrApiVersion` | Notes |
|------|-----------------|-----------------|-------|
| `2512` | `2025-10-01` | `2025-10-01` | |
| `2602` | `2025-10-01` | `2025-10-01` | |
| `2603` | `2026-03-01` | `2026-04-01` | |
| `2604` | `2026-03-01` | `2026-04-01` | |
| `2605` | `2026-03-01` | `2026-04-01` | |
| `2606` | `2026-03-01` | `2026-04-01` | |
| `2607` | `2026-07-01` | `2026-04-01` | OPC connector template affected by [Microsoft known issue 1330](https://learn.microsoft.com/azure/iot-operations/troubleshoot/known-issues#opc-connector-template-missing) |
| `2608` | `2026-07-01` | `2026-04-01` | Default in `base-site.yaml` |

Source of truth for every pinned version number is the YAML itself. Check it against the [IoT Operations release matrix](https://github.com/Azure/azure-iot-ops-cli-extension/wiki/IoT-Operations-versions) before shipping a new one.

## Upgrading an existing Site

Follow the [upgrade operation guide](../workspaces/iot-operations/manifests/aio-upgrade/README.md)
to prepare and deploy an AIO release change in place. The operation updates
extensions and the resources that the AIO release requires rather than reapplying the
greenfield instance configuration. An upgrade to 2608 adds the OPC UA
connector template.

### Catalog families and upgrade order

Bumping `aioRelease` changes the API version a Site writes at, and that takes effect as soon as the Site file changes, before the upgrade manifest has moved the cluster. A catalog deploy in that window writes resources at the new API version while the cluster still runs the old one.

Deploy catalog families outside that window, and reapply them once the upgrade finishes:

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-resources/manifest.yaml -l "name=<site>"
```

This applies to every resource area a Site selects through `properties.resourceSets`. See [resource-catalog.md](resource-catalog.md).

### Supported upgrade paths

Azure IoT Operations supports upgrade to any patch of the same minor version, or to the next minor version. Other transitions (downgrades, jumps across several minor versions, and moves between preview and GA) require uninstall and reinstall. See [Upgrade Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/deploy-iot-ops/howto-upgrade) for the authoritative rules.

The optional E2E workflow can exercise selected upgrades between adjacent AIO releases,
such as `2607` to `2608`. A passing dispatch establishes its selected
integration assertions. It is not a general readiness or support
certification for another cluster or upgrade path.

### Sample template API version policy

Sample templates under `samples/<name>/template.bicep` (e.g. `samples/opc-ua-solution/template.bicep`) pin every `Microsoft.IoTOperations/*` and `Microsoft.DeviceRegistry/*` reference to the **oldest supported** API version in the matrix above. They rely on resource provider backward compatibility so a single file works against every shipped AIO release. Bump these pins only when the oldest supported API version is removed from the matrix, not on every AIO release. The workspace test `test_samples_pin_to_oldest_api_version` enforces this.

This policy applies to samples. The platform fundamentals (the top level of `templates/aio/`, and `templates/deps/`) and the catalog templates driven by configuration under `templates/aio/dataflows/` and `templates/aio/assets/` use the dispatch for each API version described under "Adding a new AIO release", so a Site's resources are written at the API version its AIO release ships. Adding an AIO release that introduces an API version therefore means adding a catalog module too, which `tests/workspace/test_aio_dispatch_shape.py` checks. See [resource-catalog.md](resource-catalog.md).

## Adding a new AIO release

1. **Ship the release YAML.** Create `parameters/aio-releases/<release>.yaml` with the required version, train, API, dependency, and `aioReleaseConfiguration` fields used by the supported release matrix.
2. **If `aioApiVersion` is new**, extend every consumer of the AIO API version:
   - `templates/aio/instance.bicep`: add to `@allowed` on `param aioApiVersion`, add a new conditional `module instance_<YYYY>` block, move the version that was newest from `else` into an explicit equality, make the new version the `else`.
   - `templates/aio/modules/update-instance.bicep`: same pattern. The file header has a checklist.
   - `templates/aio/resolve-aio.bicep`: add the matching read module for that version and its active output branch.
   - `templates/secretsync/enable-secretsync.bicep`: extend the `aioApiVersion` allowlist used by the instance update.
   - `templates/aio/upgrade/update-extensions.bicep`: extend the allowlist used by extension defaults specific to an AIO release.
   - `templates/aio/upgrade/deploy-release-resources.bicep`: add the matching conditional module.
   - Add `templates/aio/modules/instance-<YYYY-MM-DD>.bicep`, `resolve-instance-<YYYY-MM-DD>.bicep`, and `update-instance-<YYYY-MM-DD>.bicep`. Seed them from the previous API version, then apply every verified schema change for the new API.
   - Add `templates/aio/upgrade/modules/deploy-release-resources-<YYYY-MM-DD>.bicep` with the same public parameter surface as the earlier generation modules.
   - `templates/aio/dataflows/main.bicep`: extend `@allowed` on `param aioApiVersion` and add a `module dataflows_<YYYY>` block. Add `templates/aio/dataflows/modules/dataflows-<YYYY-MM-DD>.bicep` by copying the newest module and changing every API version literal in it.
3. **If `adrApiVersion` is new**, extend the ADR dispatch:
   - `templates/deps/adr-ns.bicep`: add to `@allowed` on `param adrApiVersion`, add a new conditional `module ns_<YYYY>` block, fold the version that was newest into an explicit equality.
   - Add `templates/deps/modules/adr-ns-<YYYY-MM-DD>.bicep` by copying the previous version verbatim and changing the API version string.
   - `templates/aio/assets/main.bicep`: extend `@allowed` on `param adrApiVersion` and add a `module assets_<YYYY>` block. Add `templates/aio/assets/modules/assets-<YYYY-MM-DD>.bicep` by copying the newest module and changing every API version literal in it.
4. **If neither API version is new**, no Bicep dispatch changes are needed,
   and automatic parameter filtering forwards the new extension versions. Extend
   `aioReleaseConfiguration` and the existing
   `templates/aio/modules/instance-<generation>.bicep` module when the AIO release
   adds behavior that the API version alone cannot distinguish. A new
   `resources` entry must also be implemented by the matching
   `templates/aio/upgrade/modules/deploy-release-resources-<generation>.bicep`
   module.
5. **Run the workspace suite**: `pytest tests/workspace/ -q`. The relevant checks are:
   - `test_release_api_versions_are_accepted_by_every_consumer`: every API version a release selects appears in the `@allowed` list of every consumer that dispatches on it, discovered rather than listed.
   - `test_allowed_sets_match_across_consumers`: the consumers agree on which versions they accept.
   - `test_version_config_aio_api_versions_have_modules`: every selectable version has its own module file.
   - `test_all_sites_aio_releases_have_config_files`: no Site references a missing YAML file.
   - `TestUpdateInstanceDispatch`: every param of the `update-instance` dispatcher is forwarded by every caller.
6. **Decide the default for new Sites.** If the new AIO release should be the workspace default, update `aioRelease:` in `sites/base-site.yaml`. Sites that don't override `properties.aioRelease` will then pick it up on the next deploy. If Sites should select the new AIO release explicitly, leave the base alone and pin specific Sites individually.
7. **Test live**: dispatch the E2E workflow including the new release in `aio-releases`:
   ```
   gh workflow run e2e-test.yaml -f aio-releases=<existing>,<new>
   ```
   The matrix runs each AIO release in its own fresh resource group and Arc cluster, and the integration suite checks the deployed `aioExtension.version` against the YAML.

## Removing an EOL release

When an AIO release reaches end of life (tied to AIO's official support window), drop it from the workspace.

1. **Remove the release YAML.** Delete `parameters/aio-releases/<release>.yaml`. Git history preserves the values for future reference.
2. **Verify no Site still pins the removed AIO release.** Run `pytest tests/workspace/ -q`. `test_all_sites_aio_releases_have_config_files` fails fast on any Site that references the missing YAML. Update those Sites to a supported AIO release.
3. **Remove orphaned Bicep modules for API versions.** If no remaining AIO release uses a given `aioApiVersion` or `adrApiVersion`, the corresponding modules for that version and their `@allowed` + conditional dispatch entries can be removed. For `aioApiVersion` these are `instance-<YYYY-MM-DD>.bicep`, `resolve-instance-<YYYY-MM-DD>.bicep`, `update-instance-<YYYY-MM-DD>.bicep`, `upgrade/modules/deploy-release-resources-<YYYY-MM-DD>.bicep` and `dataflows/modules/dataflows-<YYYY-MM-DD>.bicep`. For `adrApiVersion` they are `adr-ns-<YYYY-MM-DD>.bicep` and `assets/modules/assets-<YYYY-MM-DD>.bicep`. Leave them if any supported release still uses the API version.
4. **Update sample template API pins if needed.** Samples under `samples/<name>/template.bicep` pin to the **oldest supported** API version. If removing the EOL AIO release leaves a newer oldest supported version, bump the pins. `test_samples_pin_to_oldest_api_version` enforces this.
5. **Remove the release from the E2E matrix.** Update any documentation, CI workflow defaults, or release notes recipes that named the EOL AIO release.

A Site pinned to a removed AIO release fails at workflow preparation (`aio-releases entries without a matching ... Available: [...]`) and at deploy time (validator rejects the missing YAML). The error is clear enough that no deprecation flag is needed.

## Validation summary

Release misconfigurations surface at these points:

| Layer | Check | When it runs |
|-------|-------|--------------|
| Workflow prep job | Every requested `aio-releases` entry has a matching YAML | E2E dispatch (`e2e-test.yaml`) |
| Workspace unit tests | `@allowed` membership, coverage of all Sites and of `base-site.yaml` | Every CI run |
| Workspace unit tests | `TestUpdateInstanceDispatch`: parameter parity between callers and the dispatcher | Every CI run |
| Live integration | Deployed `aioExtension.version` equals YAML's `aioVersion` | E2E matrix (per cell) |

## See also

- [Site configuration](site-configuration.md): the `aioRelease` field lives in `properties:`. Inheritance and overlays apply normally.
- [Parameter resolution](parameter-resolution.md): how AIO release YAML values are forwarded automatically to Bicep.
- [E2E testing](e2e-testing.md): how to dispatch a matrix over multiple AIO releases.
- `templates/aio/instance.bicep`, `templates/aio/resolve-aio.bicep`, `templates/aio/modules/update-instance.bicep`, `templates/aio/upgrade/update-extensions.bicep`, and `templates/aio/upgrade/deploy-release-resources.bicep`: checklists for dispatchers and AIO release defaults.

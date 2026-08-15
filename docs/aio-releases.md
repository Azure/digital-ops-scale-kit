# AIO Releases

Azure IoT Operations (AIO) ships on a release cadence. Each release pins specific versions of the AIO extension, cert-manager, secret store, and a matching control-plane API version. The scalekit represents every supported release as a release config file under `workspaces/iot-operations/parameters/aio-releases/` and selects one per site via `site.properties.aioRelease`.

## How release selection works

```
site.properties.aioRelease: "2607"
            │
            ▼
workspaces/iot-operations/parameters/aio-releases/2607.yaml
            │
            ▼  (siteops auto-forwards matching params to Bicep)
templates/aio/enablement.bicep       ──► cert-manager, secret store extensions
templates/aio/instance.bicep         ──► AIO extension + instance (dispatches on aioApiVersion)
templates/secretsync/enable-secretsync.bicep  ──► instance update (dispatches on aioApiVersion)
templates/deps/adr-ns.bicep          ──► ADR namespace (dispatches on adrApiVersion)
```

Each release YAML is a flat schema:

```yaml
# parameters/aio-releases/2607.yaml
aioVersion: "1.4.41"            # AIO extension version pinned in Arc
aioTrain: stable                # Extension release train
aioApiVersion: "2026-07-01"     # Microsoft.IoTOperations/instances API version
adrApiVersion: "2026-04-01"     # Microsoft.DeviceRegistry/namespaces API version
certManagerVersion: "0.14.0"
certManagerTrain: stable
certManagerConfigurationOverrides:
  trust-manager.secretTargets.enabled: "false"
  trust-manager.secretTargets.authorizedSecretsAll: "false"
secretStoreVersion: "1.5.1"
secretStoreTrain: stable
```

The `aioApiVersion` and `adrApiVersion` values route CREATE and UPDATE operations through their matching versioned modules (for example `templates/aio/modules/instance-2026-07-01.bicep` and `templates/deps/modules/adr-ns-2026-04-01.bicep`). Release-specific configuration override objects are auto-forwarded to templates that declare them. Bicep cannot parameterize API version strings, so the dispatchers use `@allowed` + conditional modules. See [Adding a new AIO release](#adding-a-new-aio-release) below.

## Pinning a site to a release

Set `properties.aioRelease` on the site (or on a parent via inheritance). The value must be the filename (without extension) of a YAML under `parameters/aio-releases/`.

```yaml
# sites/munich-prod.yaml
apiVersion: siteops/v1
kind: Site
name: munich-prod
inherits: base-site.yaml

properties:
  aioRelease: "2607"    # must match parameters/aio-releases/2607.yaml
```

If not specified, the site inherits whatever `base-site.yaml` declares (`"2607"` today).

## Available releases

Every file in `workspaces/iot-operations/parameters/aio-releases/` is a shipped release. At time of writing:

| Release | `aioApiVersion` | `adrApiVersion` | Notes |
|------|-----------------|-----------------|-------|
| `2512` | `2025-10-01` | `2025-10-01` | |
| `2602` | `2025-10-01` | `2025-10-01` | |
| `2603` | `2026-03-01` | `2026-04-01` | |
| `2604` | `2026-03-01` | `2026-04-01` | |
| `2605` | `2026-03-01` | `2026-04-01` | |
| `2606` | `2026-03-01` | `2026-04-01` | |
| `2607` | `2026-07-01` | `2026-04-01` | base-site default |

Source of truth for every pinned version number is the YAML itself. Cross-reference against the [IoT Operations release matrix](https://github.com/Azure/azure-iot-ops-cli-extension/wiki/IoT-Operations-versions) before shipping a new one.

## Upgrading an existing site

Use `aio-upgrade.yaml` to move a site to a newer `aioRelease`. It bumps the Arc extension versions for AIO, secret-store, and (when the site declares `deployOptions.enableCertManager: true`) cert-manager, preserving each extension's existing `configurationSettings`, `releaseTrain`, and identity.

The IoT Operations instance ARM resource has no writable version property and is not mutated by this manifest. New instance child resource types introduced by future AIO releases (broker properties, dataflow profile schema changes, etc.) are out of scope and will need a future tier of upgrade manifests.

```bash
# 1. Bump aioRelease on the site (or its parent) to the new YAML filename (without extension).
# 2. Deploy the upgrade manifest:
siteops -w workspaces/iot-operations deploy manifests/aio-upgrade.yaml -l "name=<site>"
```

`aio-install.yaml` remains the greenfield-install manifest. Running it against an already-deployed site is desired-state and can overwrite operator-applied changes on the AIO instance and its children. Use `aio-upgrade.yaml` for in-place version moves.

### Catalog families and upgrade order

Bumping `aioRelease` changes the API version a site writes at, and that takes effect as soon as the site file changes, before the upgrade manifest has moved the cluster. A catalog deploy in that window writes resources at the new API version while the cluster still runs the old one.

Deploy catalog families outside that window, and reapply them once the upgrade finishes:

```bash
siteops -w workspaces/iot-operations deploy manifests/aio-resources.yaml -l "name=<site>"
```

This applies to every family a site selects through `properties.resourceSets`. See [resource-catalog.md](resource-catalog.md).

### Supported upgrade paths

Azure IoT Operations supports upgrade to any patch of the same minor version, or to the next minor version. Other transitions (downgrades, multi-minor jumps, preview/GA crossings) require uninstall and reinstall. See [Upgrade Azure IoT Operations](https://learn.microsoft.com/en-us/azure/iot-operations/deploy-iot-ops/howto-upgrade) for the authoritative rules.

The scalekit exercises adjacent-release upgrades (e.g. `2606` -> `2607`) in CI per E2E dispatch.

### Sample template API-version policy

Sample templates under `samples/<name>/template.bicep` (e.g. `samples/opc-ua-solution/template.bicep`) pin every `Microsoft.IoTOperations/*` and `Microsoft.DeviceRegistry/*` reference to the **oldest supported** API version in the matrix above. They rely on RP backward-compatibility so a single file works against every shipped release. Bump these pins only when the oldest supported API version is removed from the matrix, not on every release. The workspace test `test_samples_pin_to_oldest_api_version` enforces this.

This policy applies to samples. The platform fundamentals (`templates/aio/` top level, `templates/deps/`) and the config-driven catalog templates under `templates/aio/dataflows/` both use the per-version dispatch described under "Adding a new AIO release", so a site's resources are written at the API version its release ships. Adding a release that introduces an API version therefore means adding a catalog module too, which `tests/workspace/test_aio_dispatch_shape.py` checks. See [resource-catalog.md](resource-catalog.md).

## Adding a new AIO release

1. **Ship the release YAML.** Create `parameters/aio-releases/<release>.yaml` with the required version, train, API, and configuration override fields used by the supported release matrix.
2. **If `aioApiVersion` is new**, extend every AIO API-version consumer:
   - `templates/aio/instance.bicep`: add to `@allowed` on `param aioApiVersion`, add a new conditional `module instance_<YYYY>` block, push the previously-newest version from `else` into an explicit equality, make the new version the `else`.
   - `templates/aio/modules/update-instance.bicep`: same pattern. The file header has a checklist.
   - `templates/aio/resolve-aio.bicep`: add the matching version-bound read module and active-output branch.
   - `templates/secretsync/enable-secretsync.bicep`: extend the `aioApiVersion` allowlist used by the instance update.
   - `templates/aio/upgrade/update-extensions.bicep`: extend the allowlist used by release-specific extension defaults.
   - Add `templates/aio/modules/instance-<YYYY-MM-DD>.bicep`, `resolve-instance-<YYYY-MM-DD>.bicep`, and `update-instance-<YYYY-MM-DD>.bicep`. Seed them from the previous API version, then apply every verified schema change for the new API.
3. **If `adrApiVersion` is new**, extend the ADR dispatch:
   - `templates/deps/adr-ns.bicep`: add to `@allowed` on `param adrApiVersion`, add a new conditional `module ns_<YYYY>` block, fold the previously-newest version into an explicit equality.
   - Add `templates/deps/modules/adr-ns-<YYYY-MM-DD>.bicep` by copying the previous version verbatim and changing the API version string.
4. **If neither API version is new**, no Bicep changes are needed. Siteops forwards the new extension versions via parameter auto-filtering.
5. **Run the workspace suite**: `pytest tests/workspace/ -q`. The relevant checks are:
   - `test_release_api_versions_are_accepted_by_every_consumer`: every API version a release selects appears in the `@allowed` list of every consumer that dispatches on it, discovered rather than listed.
   - `test_allowed_sets_match_across_consumers`: the consumers agree on which versions they accept.
   - `test_version_config_aio_api_versions_have_modules`: every selectable version has a per-version module file.
   - `test_all_sites_aio_releases_have_config_files`: no site references a missing YAML file.
   - `TestUpdateInstanceDispatch`: every param of the update-instance dispatcher is forwarded by every caller.
6. **Decide the default for new sites.** If the new release should be the workspace default, update `aioRelease:` in `sites/base-site.yaml`. Sites that don't override `properties.aioRelease` will then pick it up on the next deploy. If the new release is opt-in only, leave the base alone and pin specific sites individually.
7. **Test live**: dispatch the E2E workflow including the new release in `aio-releases`:
   ```
   gh workflow run e2e-test.yaml -f aio-releases=<existing>,<new>
   ```
   The matrix runs each release in its own fresh RG + Arc cluster, and the integration suite cross-checks the deployed `aioExtension.version` against the YAML.

## Removing an EOL release

When a release reaches end-of-life (tied to AIO's official support window), drop it from the workspace.

1. **Remove the release YAML.** Delete `parameters/aio-releases/<release>.yaml`. Git history preserves the values for future reference.
2. **Verify no site still pins the removed release.** Run `pytest tests/workspace/ -q`. `test_all_sites_aio_releases_have_config_files` fails fast on any site that references the missing YAML. Update those sites to a supported release.
3. **Remove orphaned API-version Bicep modules.** If no remaining release uses a given `aioApiVersion` or `adrApiVersion`, the corresponding per-version modules (`instance-<YYYY-MM-DD>.bicep`, `update-instance-<YYYY-MM-DD>.bicep`, `adr-ns-<YYYY-MM-DD>.bicep`) and their `@allowed` + conditional dispatch entries can be removed. Leave them if any supported release still uses the API version.
4. **Update sample template API pins if needed.** Samples under `samples/<name>/template.bicep` pin to the **oldest supported** API version. If removing the EOL release leaves a newer oldest-supported version, bump the pins. `test_samples_pin_to_oldest_api_version` enforces this.
5. **Remove the release from the E2E matrix.** Update any documentation, CI workflow defaults, or release-notes recipes that named the EOL release.

A site pinned to a removed release now fails at workflow prep (`aio-releases entries without a matching ... Available: [...]`) and at deploy time (validator rejects the missing YAML). The error is clear enough that no soft-deprecation flag is needed.

## Validation summary

Release misconfigurations surface at four points:

| Layer | Check | When it runs |
|-------|-------|--------------|
| Workflow prep job | Every requested `aio-releases` entry has a matching YAML | E2E dispatch (`e2e-test.yaml`) |
| Workspace unit tests | `@allowed` membership, all-sites coverage, base-site coverage | Every CI run |
| Workspace unit tests | `TestUpdateInstanceDispatch`: caller-vs-dispatcher param parity | Every CI run |
| Live integration | Deployed `aioExtension.version` equals YAML's `aioVersion` | E2E matrix (per cell) |

## See also

- [Site configuration](site-configuration.md): the `aioRelease` field lives in `properties:`. Inheritance and overlays apply normally.
- [Parameter resolution](parameter-resolution.md): how release YAML values are auto-forwarded to Bicep.
- [E2E testing](e2e-testing.md): how to dispatch a matrix over multiple releases.
- `templates/aio/instance.bicep`, `templates/aio/resolve-aio.bicep`, `templates/aio/modules/update-instance.bicep`, and `templates/aio/upgrade/update-extensions.bicep`: dispatcher and release-default checklists.

# AIO Releases

Azure IoT Operations (AIO) ships on a release cadence. Each release pins specific versions of the AIO extension, cert-manager, secret store, and a matching control-plane API version. The scalekit represents every supported release as a release config file under `workspaces/iot-operations/parameters/aio-releases/` and selects one per site via `site.properties.aioRelease`.

## How release selection works

```
site.properties.aioRelease: "2603"
            │
            ▼
workspaces/iot-operations/parameters/aio-releases/2603.yaml
            │
            ▼  (siteops auto-forwards matching params to Bicep)
templates/aio/enablement.bicep       ──► cert-manager, secret store extensions
templates/aio/instance.bicep         ──► AIO extension + instance (dispatches on aioApiVersion)
templates/secretsync/enable-secretsync.bicep  ──► instance update (dispatches on aioApiVersion)
templates/deps/adr-ns.bicep          ──► ADR namespace (dispatches on adrApiVersion)
```

Each release YAML is a flat schema:

```yaml
# parameters/aio-releases/2603.yaml
aioVersion: "1.3.38"            # AIO extension version pinned in Arc
aioTrain: stable                # Extension release train
aioApiVersion: "2026-03-01"     # Microsoft.IoTOperations/instances API version
adrApiVersion: "2026-04-01"     # Microsoft.DeviceRegistry/namespaces API version
certManagerVersion: "0.10.2"
certManagerTrain: stable
secretStoreVersion: "1.3.0"
secretStoreTrain: stable
```

The `aioApiVersion` and `adrApiVersion` values route CREATE and UPDATE operations through their matching versioned modules (for example `templates/aio/modules/instance-2026-03-01.bicep` and `templates/deps/modules/adr-ns-2026-04-01.bicep`). Bicep cannot parameterize API version strings, so the dispatchers use `@allowed` + conditional modules. See [Adding a new AIO release](#adding-a-new-aio-release) below.

## Pinning a site to a release

Set `properties.aioRelease` on the site (or on a parent via inheritance). The value must be the filename (without extension) of a YAML under `parameters/aio-releases/`.

```yaml
# sites/munich-prod.yaml
apiVersion: siteops/v1
kind: Site
name: munich-prod
inherits: base-site.yaml

properties:
  aioRelease: "2603"    # must match parameters/aio-releases/2603.yaml
```

If not specified, the site inherits whatever `base-site.yaml` declares (`"2603"` today).

## Available releases

Every file in `workspaces/iot-operations/parameters/aio-releases/` is a shipped release. At time of writing:

| Release | `aioApiVersion` | `adrApiVersion` | Notes |
|------|-----------------|-----------------|-------|
| `2512` | `2025-10-01` | `2025-10-01` | |
| `2602` | `2025-10-01` | `2025-10-01` | |
| `2603` | `2026-03-01` | `2026-04-01` | base-site default |

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

### Supported upgrade paths

In-place upgrades are exercised in CI between adjacent shipped releases (e.g. `2602` -> `2603`). Multi-hop upgrades (e.g. `2512` -> `2603`) are not gated by CI. Perform the hops sequentially through each adjacent release and verify between hops.

Downgrades are not supported by IoT Operations.

### Sample template API-version policy

Sample templates under `samples/<name>/template.bicep` (e.g. `samples/opc-ua-solution/template.bicep`) pin every `Microsoft.IoTOperations/*` and `Microsoft.DeviceRegistry/*` reference to the **oldest supported** API version in the matrix above. They rely on RP backward-compatibility so a single file works against every shipped release. Bump these pins only when the oldest supported API version is removed from the matrix, not on every release. The workspace test `test_samples_pin_to_oldest_api_version` enforces this.

This policy applies only to samples. Fundamentals (`templates/aio/`, `templates/deps/`) use the per-version dispatch described under "Adding a new AIO release".

## Adding a new AIO release

1. **Ship the release YAML.** Create `parameters/aio-releases/<release>.yaml` with all eight fields (`aioVersion`, `aioTrain`, `aioApiVersion`, `adrApiVersion`, `certManagerVersion`, `certManagerTrain`, `secretStoreVersion`, `secretStoreTrain`).
2. **If `aioApiVersion` is new**, extend the dispatch in both Bicep dispatchers:
   - `templates/aio/instance.bicep`: add to `@allowed` on `param aioApiVersion`, add a new conditional `module instance_<YYYY>` block, push the previously-newest version from `else` into an explicit equality, make the new version the `else`.
   - `templates/aio/modules/update-instance.bicep`: same pattern. The file header has a checklist.
   - Add `templates/aio/modules/instance-<YYYY-MM-DD>.bicep` and `update-instance-<YYYY-MM-DD>.bicep`. Start by copying the previous API version's modules verbatim and change only the API version strings. Diverge per-module only when the schema actually changes.
3. **If `adrApiVersion` is new**, extend the ADR dispatch:
   - `templates/deps/adr-ns.bicep`: add to `@allowed` on `param adrApiVersion`, add a new conditional `module ns_<YYYY>` block, fold the previously-newest version into an explicit equality.
   - Add `templates/deps/modules/adr-ns-<YYYY-MM-DD>.bicep` by copying the previous version verbatim and changing the API version string.
4. **If neither API version is new**, no Bicep changes are needed. Siteops forwards the new extension versions via parameter auto-filtering.
5. **Run the workspace suite**: `pytest tests/workspace/ -q`. The relevant checks are:
   - `test_version_config_api_versions_are_allowed_in_bicep`: every `aioApiVersion` must appear in both AIO dispatchers' `@allowed` lists.
   - `test_version_config_adr_api_versions_are_allowed_in_bicep`: every `adrApiVersion` must appear in the ADR dispatcher's `@allowed` list.
   - `test_all_sites_aio_releases_have_config_files`: no site references a missing YAML file.
   - `TestUpdateInstanceDispatch`: every param of the update-instance dispatcher is forwarded by every caller.
6. **Decide the default for new sites.** If the new release should be the workspace default, update `aioRelease:` in `sites/base-site.yaml`. Sites that don't override `properties.aioRelease` will then pick it up on the next deploy. If the new release is opt-in only, leave the base alone and pin specific sites individually.
7. **Test live**: dispatch the E2E workflow including the new release in `aio-releases`:
   ```
   gh workflow run e2e-test.yaml -f aio-releases=<existing>,<new>
   ```
   The matrix runs each release in its own fresh RG + Arc cluster, and `test_aio_extension_version_matches_version_config` cross-checks the deployed `aioExtension.version` against the YAML.

## Validation summary

Four layers catch release misconfigurations before they reach production:

| Layer | Check | When it runs |
|-------|-------|--------------|
| Workflow prep job | Every requested `aio-releases` entry has a matching YAML | E2E dispatch (`e2e-test.yaml`) |
| Workspace unit tests | `@allowed` membership, all-sites coverage, base-site coverage | Every CI run |
| Workspace unit tests | `TestUpdateInstanceDispatch`: caller-vs-dispatcher param parity | Every CI run |
| Live integration | Deployed `aioExtension.version` equals YAML's `aioVersion` | E2E matrix (per cell) |

## See also

- [Site configuration](site-configuration.md): the `aioRelease` field lives in `properties:`; inheritance and overlays apply normally.
- [Parameter resolution](parameter-resolution.md): how release YAML values are auto-forwarded to Bicep.
- [E2E testing](e2e-testing.md): how to dispatch a matrix over multiple releases.
- `templates/aio/instance.bicep` and `templates/aio/modules/update-instance.bicep`: dispatcher checklists embedded at the top of each file.

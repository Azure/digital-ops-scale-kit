## Highlights

### 1. Install Site Ops from a release

Install Site Ops and use published Scale Kit content with your own Site
inputs, without a repository clone. The bootstrap verifies the engine
archive's provenance and payload, provisions native uv and managed Python
with your consent, and installs the recorded dependency wheels.
It runs as your ordinary user on Windows x64 or Linux x64 based on glibc.

Use the generated **Install Site Ops** section below for the exact package
version, Windows and Linux commands, verification choices and source
enrollment. The [README](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/README.md#quick-start)
also provides a short command that selects this release by tag and can
enroll the `official` source in the same invocation.
That route trusts HTTPS for the initial script. Choose the attestation
verification route when your policy requires script provenance before it runs.

Source enrollment records the publisher you trust for 30 days. Azure login
and deployment confirmation remain separate choices.

### 2. Deploy AIO from one cluster ID, with optional Secret Sync

The Arc cluster resource ID is the only required answer for guided AIO
installation. Site Ops reads the cluster to derive the subscription, resource
group, region and cluster name, and names the Site after the cluster.
Environment and country labels are optional.

Use an existing Arc cluster in its own resource group, Azure CLI 2.70.0 or
newer signed in with deployment and role assignment permissions, and
GitHub CLI 2.95 or newer. Enroll `official` using the instructions below,
then replace the cluster placeholder. This command installs AIO and enables
Secret Sync together:

```text
siteops deploy aio-install --source "official@v1.0.0b7" --input "cluster=<Arc-cluster-resource-ID>" --input enableSecretSync=true
```

Secret Sync requires the existing cluster to report an OIDC issuer and
enabled workload identity. Omit `--input enableSecretSync=true` for AIO
alone. Secret Sync is disabled by default.

For an existing AIO 2607, 2608 or 2609 instance, replace the instance
placeholder and enable Secret Sync directly:

```text
siteops deploy secretsync --source "official@v1.0.0b7" --input "instance=<AIO-instance-resource-ID>"
```

This route resolves the instance and its associated cluster, uses the actual
instance name, and performs Secret Sync enablement without reinstalling or
upgrading AIO. Add `existingVault` to use an existing Key Vault.
Answer files, optional name overrides and saved Sites use the same typed
input contract. See [guided inputs](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/guided-inputs.md).

### 3. Review a prepared plan and read deployment outcomes

Prepare an executable plan before deployment:

```text
siteops plan aio-install --source "official@v1.0.0b7" --input "cluster=<Arc-cluster-resource-ID>"
```

`plan` validates inputs, resolves operations, compiles templates and checks
the local tools those operations need. It performs no deployment writes.
Supplying a resource ID authorizes bounded Azure reads to resolve the Site.
Use `--describe` for the plan shape without compilation or tool checks.

In an interactive terminal, `deploy` shows its prepared plan, asks you to
confirm, and executes that same plan. A separate `plan` invocation is a
preview, not a saved execution artifact. Add `--yes` for CI, JSON output or
other unattended deployment.

Verified Packages use ARM JSON compiled by the producer. Local workspaces
retain source Bicep submission and record the compilation observed during
preparation. Progress uses stderr, and final JSON output is one document
on stdout. Results retain each Site's and operation's outcome, including
partial failure, skipped work, cancellation and unconfirmed completion.

A successful deployment reports completed operations. Check AIO readiness
and the workload's functional result separately before proceeding.
See [plan output](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/plan-output.md),
[run output](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/run-output.md)
and [check the result](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/guided-inputs.md#check-the-result).

### 4. Browse, acquire and publish complete workspace Packages

Keep your Site configuration separate from the content you consume.
After enrolling `official`, acquire this release's workspace and inspect
its deployment choices:

```powershell
siteops --approved-source official project pin .\factory --release v1.0.0b7
siteops --approved-source official --project .\factory browse aio-install
```

The project pin selects an exact Package and leaves your Sites unchanged.
Package use verifies provenance, byte integrity, compatibility and current
source policy. Add `--offline-content` to content commands in a pinned
project to use cached Package and proof bytes. This restricts content
acquisition, not Azure reads or deployment.

`browse` shows requirements, effects, guides and typed input availability
without loading Site values or invoking deployment tools. Remote
`browse --source` reads published descriptions rather than deployable
content.

Content authors publish index descriptions with
`siteops -w workspaces\iot-operations index --public` in a reviewed local
checkout. Reviewed release declarations build and sign complete workspace
Packages, including compiled templates and their documentation.
A signed engine reference identifies the exact engine selected for a
content release. Source enrollment remains the consumer's decision.
See [projects](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/projects.md),
[browsing](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/browse-content.md)
and [workspace Packages](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/workspace-packages.md).

### 5. Qualify the exact release candidate before publication

Publication requires passing acceptance for the exact frozen engine and
workspace assets. Acceptance covers native installation, guided AIO alone,
combined AIO and Secret Sync, Secret Sync on an existing instance, and
one deployment across a fleet. Deployment cases check AIO pod readiness
and confirm removal of the resources they created.

The publication approval remains separate, and the publisher verifies the
bound acceptance receipt before creating a tag or release. Official
releases require immutable tags and assets. Windows installation includes
a standard user path, and installation checks include Ubuntu 26.04.

Deployment, infrastructure readiness, workload data delivery and Secret Sync
materialization remain distinct outcomes. Acceptance checks deployment
and readiness. Use the workload and Secret Sync guides for functional checks.
See [release acceptance](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/releasing.md#accept-the-candidate).

### 6. Diagnose failures without publishing private values

Content errors carry stable codes such as `content.duplicate-key` and
`content.unknown-field`, with fixed guidance and documentation links.
Validation also emits structured JSON. From a matching local checkout:

```powershell
siteops -w workspaces\iot-operations validate aio-install --output json --projection publishable
```

Deployment results classify failed and unconfirmed logical operations,
and include failure diagnostics beside the existing operation counts.
Publishable output retains causes and allowlisted Azure codes while keeping
Site values, paths and raw provider messages private.
Azure Pipelines and GitHub Actions render these results with the same helper
and preserve the operation's exit status through reporting and cleanup.
See the [diagnostic index](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/diagnostics.md).

### 7. Choose AIO 2609 or keep an explicit AIO release

AIO 2608 and 2609 configurations join the workspace. AIO 2609, which pins
AIO 1.4.112, is the default for inherited Site settings and guided
installation. AIO 2607 and 2608 remain selectable through
`properties.aioRelease` or the guided `aioRelease` input.
For example, add `--input aioRelease=2608` to the installation command to
select 2608 deliberately.

The 2608 and 2609 configurations supply WebAssembly graph controller MQTT
trust settings and the OPC UA connector template. Install and upgrade
create that template when OPC UA is enabled. With feature mode `Disabled`,
they skip it. The legacy `connectors` feature supplies the mode when
`opcua` is absent, and `opcua` takes precedence when both are present.
Upgrade uses the existing instance's resolved features.

Use `aio-upgrade` to change an existing installation's AIO release.
See [AIO releases](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/aio-releases.md)
for pins, supported transitions and the AIO 2607 connector template limitation.

### 8. Compose catalog resource sets across a fleet

Select ordered resource sets for devices, assets and dataflows on each Site.
Definitions compose by resource identity across the selected files.
Assets can reference independently selected devices, and dataflows can
reference endpoints and profiles from other sets. Duplicate writers and
unresolved references are checked before deployment.

In a matching local checkout, a configured Site can select the shipped sets:

```yaml
properties:
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
    dataflows:
      - shared-mqtt-endpoint
      - shared-dataflow-profile
      - basic-routing
```

For a configured Site named `plant-one` that already runs AIO, review the
effective composition:

```powershell
siteops -w workspaces\iot-operations plan aio-resources -l name=plant-one --describe
```

The example device points at the OPC PLC simulator used by the samples.
Use the sample setup or change its endpoint to your reachable server before
applying it.

Local plans show source provenance, applied resources and resolved
references. Explicit external assertions let a set describe a dependency
managed elsewhere, with its reason retained locally. Those assertions
describe expected resources rather than reading Azure to prove existence.

Catalog deployment creates or updates selected definitions. Deselecting a
set leaves existing resources in place for deliberate removal.
The [resource catalog](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/resource-catalog.md)
and the [basic](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/workspaces/iot-operations/samples/resource-set-basic/README.md)
and [composition](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/workspaces/iot-operations/samples/resource-set-composition/README.md)
samples show the full path from selection to workload checks.

## Automation and operator controls

- Azure Pipelines can select an attested engine from a release tag or explicit
  release and source commit, while keeping caller content in its own checkout.
  Environment service connections and variable groups live in reviewed YAML.
- GitHub Actions uses full commit SHA references for external actions.
- Plain output uses ASCII markers, wraps to the terminal width and shows
  bounded progress while preparation or provider work continues.
- `sites --output yaml`, `sites --output json` and `sites --show-sources`
  support private inspection of resolved configuration and provenance.
- AIO extension configuration overrides belong under the saved Site's
  `parameters` map. See [configure the Site](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/workspaces/iot-operations/manifests/aio-install/README.md#configure-the-site).

See [CI/CD setup](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/ci-cd-setup.md)
for engine selection, fleet targeting and diagnostic rendering in automation.

## Upgrading

Read the [v1.0.0b6 to v1.0.0b7 migration guide](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/migrating.md#to-v100b7)
before updating existing workspaces, scripts or pipelines. It compares this
release with v1.0.0b6.

The breaking changes to check first are Site selected parameter paths and
catalog resource set selections. Keep dynamic parameter paths relative
and inside the workspace, with no `..` segments. Replace scalar resource
set selections with ordered lists, omit an area for no selection, and
use `[]` to clear an inherited list.

The inherited AIO default changes from 2607 to 2609. Pin
`properties.aioRelease: "2607"` to retain the v1.0.0b6 AIO release, or
`"2608"` to select 2608. Use the upgrade operation for an existing instance.

Update automation to use `plan` in place of `deploy --dry-run` and
`plan --describe` in place of `validate --plan`. Unattended deployment
requires `--yes`. Review moved Manifest and resource set paths, the
Azure CLI minimum, and matching pipeline templates and result helpers
in the migration guide.

## Operational notes

- This is a Scale Kit beta release. The Site Ops source version remains
  `1.0.0b1`, and the included engine artifact has a distinct build suffix.
  The generated installation section reports its exact package version.
- Installation and source enrollment do not sign in to Azure or authorize
  resource changes. Planning with a resource ID performs bounded reads.
  Deployment requires its own confirmation and can incur Azure charges.
- Reapplying `aio-install` can overwrite instance settings. Select new
  clusters explicitly for installation, use `aio-upgrade` for upgrades,
  and use `secretsync` for enablement on an existing instance.
- Secret Sync enablement grants Key Vault roles to its managed identity. To
  manage the vault's secrets yourself, for example in the IoT Operations
  portal, assign your account a Key Vault data role such as Key Vault
  Secrets Officer.
- A Site name becomes part of its schema registry namespace, which must be
  unique in the Microsoft Entra tenant. Sites built from typed inputs scope
  that namespace to their resource group. Give a Site file you write
  yourself a name that no other Site in the tenant uses. See
  [Site identity](https://github.com/Azure/digital-ops-scale-kit/blob/v1.0.0b7/docs/site-configuration.md#site-identity).
- Run the OPC UA solution sample from a local checkout of this release, with
  `siteops -w workspaces/iot-operations`. The sample applies an OPC PLC
  simulator from a pinned public URL, and verified packages apply only the
  files they carry.
- Failure or interruption can leave partial changes. Review the deployment
  result and provider state before retrying. Resource removal remains an
  explicit operator action.

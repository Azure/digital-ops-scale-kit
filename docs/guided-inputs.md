# Deploy AIO with guided inputs

Deploy Azure IoT Operations to one existing cluster connected to Azure Arc
with a single command. Site Ops builds one explicit Site in memory from typed
answers, without a project pin, saved Site, generated answer file or
separate plan command.

| Your goal | Start here |
|---|---|
| Install AIO on an existing Arc cluster | [AIO installation](#install-aio-from-an-existing-cluster) |
| Install AIO and enable Secret Sync together | [Combined installation](#install-aio-with-secret-sync) |
| Set Arc extension configuration | [Arc extension settings](#set-arc-extension-settings) |
| Enable Secret Sync without reinstalling AIO | [Enablement on an existing instance](#enable-secret-sync-on-an-existing-instance) |
| Upgrade an existing instance | [Upgrade with typed inputs](#upgrade-an-existing-instance) |

Install a compatible Site Ops build and select a release containing the
complete AIO workspace and its typed input contract. The source and release
come from your selected release instructions. `official` is the approved source you
enrolled with `siteops source enroll official`, not authority supplied by
the package. See [approved sources](projects.md#use-an-approved-source).
Independent `--trust-policy` and `--trusted-root` files can select a
source repository directly instead. Enrolling a source does not sign in to
Azure or confirm deployment.

Existing local `-w` workspaces and configured Sites remain supported. There
is no required migration to a project pin or typed answers. Use
`siteops inputs` when a manifest declares a typed contract and you want one
explicit Site without first configuring it. With `--input-file`, `--input`
or `--site-file`, `-l` selects no configured Sites. It names labels the
explicit Site must carry, as described in
[targeting](targeting.md#label-requirements-for-an-explicit-site). Saved Sites
can later be selected with the same explicit fleet selectors as before.

## Install AIO from an existing cluster

The existing Arc cluster resource ID is the only required answer for the
guided AIO installation. Replace the placeholder and run:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

In a private TTY, deploy shows the validated resource IDs and prepares one
executable plan with each Site's subscription, resource group and operations.
It asks for confirmation, then executes that same plan. For JSON,
CI or any noninteractive invocation, add `--yes` to authorize execution
explicitly. Without it the command stops with a usage error before content
or Azure access. The cluster and resource group must already exist. The
declared resource ID answer authorizes bounded reads using your Azure CLI
identity to derive subscription, resource group, region and cluster name.
An explicit manual value must agree with those facts. Deployment can create
or update resources and incur charges. To check the deployed AIO services
afterward, see [Check the result](#check-the-result).

For a separate executable preview, run:

```text
siteops plan aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Add `--describe` to see the plan shape without compiling. A separate plan never
authorizes a later deploy: deploy builds a fresh plan. Direct `--source`
requires an explicit published release and online source resolution, even
when verified package bytes can be reused from cache. If the release contains
several workspaces, global `-w` selects a relative workspace path inside
that source, not a local directory. Global `--project` can supply your
configured Sites without changing its existing pin. A
[project pin](projects.md#run-project-pin) is optional for this single Site
and useful for repeatable fleets or `--offline-content`.

Without `siteName`, the Site takes the cluster's name in lowercase, so the
cluster `plant-a` deploys as the Site `plant-a`. Clusters in different resource
groups can share a name, and each of their Sites still gets its own schema
registry namespace. To choose a different name, add
`--input siteName=plant-one` to the deploy command (and any separate plan).
When the cluster's name is not a valid Site name, for example because it
contains an underscore, the command stops before deploying and asks for
`siteName`.
Typed Site names use lowercase letters, digits and interior hyphens, with a
maximum of 59 characters. Existing saved Sites retain their names.

Environment and country labels are optional. Add `--input environment=dev`
or `--input country=US` when you want those labels and resource tags.
Omitted values create neither labels nor tags. The default AIO release is
2609, cert-manager is enabled, and Secret Sync is disabled. To list every
optional input, such as `enableCertManager` and `brokerMemoryProfile`, run
`siteops inputs aio-install` with the same `--source`.

## Use an answer file instead

An answer file is optional. To inspect the declared fields and write an
incomplete example for editing:

```text
siteops inputs aio-install --source "official@<release>" --example ./aio-inputs.yaml
```

This command explains the inputs and writes an incomplete example.
Omit `--example` to inspect without writing. The generated file needs one
value:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  cluster: null
```

Replace `cluster: null` with the full cluster ID. Optional overrides may
also be included under `values`. Inline `--input` answers override the
file. The generated example is incomplete and cannot deploy unchanged.

Supply the completed file to deploy with the same interactive review:

```text
siteops deploy aio-install --source "official@<release>" --input-file ./aio-inputs.yaml
```

For an optional preview that only inspects, run
`inputs aio-install --source "official@<release>" --input-file ./aio-inputs.yaml --read-resources`.
Only `inputs` requires that flag to authorize an Azure read while previewing
or saving.
It reads the selected cluster and structurally validates one Site without
writing it, compiling templates or submitting deployments. Plain and local JSON output
include a private display of the Site and its defaults. In CI and other
redacted destinations, the command reports resolution status without
publishing Site values.

For a short command without secrets, supply the same named answers with repeated
`--input NAME=VALUE` options on `inputs`, `plan` or `deploy`. Inline answers override
the input file. `--input-file` selects exactly one answer file. Repeating
that option is an error, not a way to select several Sites. Strings and
strict `true` or `false` booleans are parsed
according to the selected contract. A map input takes one `NAME=KEY=VALUE`
entry for each key, as [Arc extension settings](#set-arc-extension-settings)
describes. Do not put secrets in process arguments
or shell history. Input contracts that declare sensitive inputs are
rejected. Duplicate and unknown answer names fail.

## Use an existing resource ID

The ID must identify an existing
`Microsoft.Kubernetes/connectedClusters` resource. Site Ops checks its
type and identity, reads its region, and derives the four Site values
before calling the normal planner. A supplied manual value must agree
with the observed resource. The read uses your existing Azure CLI
identity and fails if that identity cannot access the cluster. Site Ops
does not sign you in, change accounts or grant permissions. It reads
again for the separate deploy invocation rather than treating a
previous plan's observation as current.
`--offline-content` on a pinned project restricts source content acquisition,
not Azure resource reads or deployment. Direct `--source` instead requires
online release resolution, even if its verified content is already cached.

## Install AIO with Secret Sync

To enable Secret Sync during the guided AIO deployment, set
`enableSecretSync: true` in the answer file, or add
`--input enableSecretSync=true` to the deploy command. This guided route
requires the cluster ID. Its bounded read checks prerequisites without an
additional plan/deploy switch. Before any deployment writes, Azure
must report an OIDC issuer and enabled workload identity on the existing
cluster:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>" --input enableSecretSync=true
```

You may supply an optional `existingVault` resource ID when
enabled. It must be in the same subscription as the Site but may be in
a different resource group. Omit it to create a new vault. A successful
resource read establishes those reported settings at that moment, not
cluster readiness, federation success or secret materialization.

Enablement grants vault roles only to the Secret Sync managed identity. To
add or view the vault's secrets yourself, for example in the operations
experience, give your account a Key Vault data role as
[vault access](secret-sync.md#give-your-account-access-to-the-vault)
describes.

## Set Arc extension settings

`aioExtensionSettings`, `secretStoreExtensionSettings` and
`certManagerExtensionSettings` set the configuration of the AIO, secret
store and cert-manager Arc extensions. Each is an optional map of string
keys and string values. Inline, supply one entry for each key as
`NAME=KEY=VALUE`:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>" --input "aioExtensionSettings=observability.metrics.exportIntervalSeconds=60"
```

The input name ends at the first `=` and the key ends at the second, so the
value can contain `=`. An empty value after the second `=` sets an empty
string. Keys that contain `=` need the answer file, which holds the same
maps. Quote keys and values there, because YAML reads an unquoted `on` as
true and `1.10` as 1.1:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  cluster: "<Arc-cluster-resource-ID>"
  aioExtensionSettings:
    "observability.metrics.exportIntervalSeconds": "60"
```

Inline entries replace answer file entries key by key, and other file
entries remain. A key repeated inline fails, even with the same value.
Keys and values must be text. Numbers, booleans, null, lists and nested maps
fail rather than being converted, so quote each key and value, and write a
nested setting as one dotted key. Keys must also be printable: no control,
format, surrogate, private use or unassigned characters, and no line or
paragraph separators. Which characters are unassigned depends on the Unicode
version of the engine's Python. Each map, from the file, inline or combined,
holds at most 64 entries, with keys of 1 to 256 characters, values of at
most 4,096 characters, and at most 64 KiB of keys and values together.
Errors name the input and, unless output is redacted, the key, never the
value. The settings appear in answer files, shell history, the Site preview
and Azure, so never put secrets in them. The private Site preview shows `***`
for values whose names look sensitive, such as the whole
`secretStoreConfigurationOverrides` map and keys that contain `token`. A
saved Site keeps the real values.

An omitted map or `{}` adds no settings. With `aio-upgrade`, the settings
already on the cluster stay in place and no answer removes a key.
Installation writes each extension's settings without reading the
cluster's, so change the settings of an existing installation with
[aio-upgrade](../workspaces/iot-operations/manifests/aio-upgrade/README.md#change-extension-settings)
rather than by installing again. Typed answers build a new Site each time,
so they never change a saved Site. `certManagerExtensionSettings`
applies only while `enableCertManager` is true. Supplying it otherwise, even
as `{}`, fails like any inactive input. The secret store settings apply
whether or not Secret Sync is enabled. See
[Arc extension settings](../workspaces/iot-operations/manifests/aio-install/README.md#arc-extension-settings)
for the Site parameters these inputs set and the settings the installation
derives.

These typed maps need a Site Ops engine that supports them. An older engine
refuses a package that uses them with
`This package requires unsupported Site Ops features.` Install the engine
that the content release names. Configured Sites and complete Site files
set the same maps under `parameters` without typed inputs.

## Manual Site values without resource reads

Omit `cluster` and provide `siteName`, `subscription`, `resourceGroup`,
`location` and `clusterName` instead. Use the same inline or file route
without an Azure resource read. Name, environment and country overrides follow
the same validation rules, and the labels remain optional. The manual route
does not establish Azure resource existence or enable guided Secret Sync,
which requires observed cluster prerequisites.

```text
siteops deploy aio-install --source "official@<release>" --input siteName=plant-one --input subscription=<subscription-ID> --input resourceGroup=<existing-resource-group> --input location=<region> --input clusterName=<Arc-cluster-name>
```

## Enable Secret Sync on an existing instance

For an existing AIO 2607, 2608 or 2609 instance, supply its resource ID instead
of running installation again:

```text
siteops deploy secretsync --source "official@<release>" --input "instance=<AIO-instance-resource-ID>"
```

`instance` is the only required answer. Site Ops reads the instance, its
custom location and that location's Arc cluster. The related resources
must remain in the instance's subscription and resource group. OIDC issuer
and workload identity must already be enabled. Unsupported relationships,
conflicting Site values or missing prerequisites fail before deployment.
The reads do not enable cluster features or grant roles.

The plan contains only instance resolution and Secret Sync enablement.
It uses the actual instance name and does not reinstall or upgrade AIO.
The shared typed contract supports existing AIO 2607, 2608 and 2609 instances.
Configured Sites retain their release selection for other API generations.

The default Site name comes from the associated cluster, matching the
basic AIO resource route. Supply `siteName` to retain an earlier explicit
name. Optional `environment` and `country` values label enablement resources.
Add `existingVault` to use your existing Key Vault, including one in another
resource group of the same subscription. Otherwise enablement uses its
default vault. Review that choice before updating an existing Secret Sync
configuration.

The existing content forwards instance settings and preserves the bound
Secret Provider Class object list when no secrets are declared. Enablement
still updates the default Secret Provider Class binding. It does not prove
that a secret materialized in Kubernetes. See the [Secret Sync reference](secret-sync.md)
for preservation, vault permissions and functional checks.

## Upgrade an existing instance

To upgrade one existing instance without a saved Site, supply its resource
ID, the target AIO release and whether this upgrade includes the existing
cert-manager Arc extension:

```text
siteops deploy aio-upgrade --source "official@<release>" --input "instance=<AIO-instance-resource-ID>" --input aioRelease=2609 --input "enableCertManager=<true-or-false>"
```

`aioRelease` and `enableCertManager` have no defaults. Answer
`enableCertManager=true` when Scale Kit installed cert-manager and manages
it, as an installation with the default `enableCertManager` does. Answer
`false` when another owner manages cert-manager, and the upgrade neither
reads nor updates that extension. Site Ops reads the instance, its custom
location and that location's Arc cluster, as for Secret Sync enablement,
but checks no OIDC or workload identity prerequisite. The extension
settings inputs apply here too. Choose a supported transition from the
version the instance runs, never an older release, as the
[upgrade guide](../workspaces/iot-operations/manifests/aio-upgrade/README.md#choose-a-supported-upgrade-path)
describes.

If you saved the installation's Site, change `properties.aioRelease` in that
file and upgrade with `--site-file` or `-l name=<site>` instead. Typed
answers never change a saved Site.

## Check the result

A successful Site Ops result means the deployment operations completed.
To check the deployed AIO services, use the
[AIO health check](https://learn.microsoft.com/en-us/cli/azure/iot/ops#az-iot-ops-check).
It needs Azure CLI with the `azure-iot-ops` extension, authorized Kubernetes
access and a kubeconfig context for the cluster you just deployed:

```text
az iot ops check --context "<target-kubeconfig-context>"
```

Choose that context explicitly rather than relying on whichever cluster
is current. Review the reported readiness and runtime health failures
before adding a workload. Keep its diagnostic output private. This check
does not establish that a particular workload delivered data or that a
Key Vault secret materialized. Use the selected workload's functional
checks and the [Secret Sync guidance](secret-sync.md) for those outcomes.

A repeated deploy can perform provider writes. Site Ops does not roll back
or delete resources automatically after failure. Review the retained
deployment result and the current resource state before retrying. For a disposable demo,
plan cleanup of the created Azure resources and Kubernetes objects without
deleting the existing cluster or resources owned by others.

## Deploy a selected AIO release to each cluster

`aioRelease` is a typed answer mapped to the selected Site's
`properties.aioRelease`. The packaged IoT Operations workspace supplies
the [supported AIO release configurations](aio-releases.md#available-aio-releases),
including `2608`. `2609` is the default.
To use different AIO releases without saving Site files, prepare and
deploy each existing Arc cluster in a separate invocation:

```text
siteops deploy aio-install --source "official@<release>" --input siteName=plant-2608 --input "cluster=<Arc-ID-A>" --input environment=dev --input country=US --input aioRelease=2608
siteops deploy aio-install --source "official@<release>" --input siteName=plant-2607 --input "cluster=<Arc-ID-B>" --input environment=dev --input country=US --input aioRelease=2607
```

Replace the two placeholders with the full ARM IDs of two different
connected clusters, each in its own resource group.
Each command constructs one Site in memory and selects the corresponding
AIO release parameters from the verified workspace. Each deploy reviews its own
plan. These examples leave Secret Sync disabled.
Run `aio-upgrade`, not `aio-install`, to change an existing installation's
AIO release.

## Keep a Site for later

For repeatable workflows with configured Sites and fleets, first
[pin a reviewed release](projects.md#run-project-pin) in `./factory`
and select its `official` approved source. To retain resolved configuration, create
the project's `sites` directory
and run `inputs` with a completed answer file. When that file contains an Arc
cluster ID, authorize its resource read while saving:

```text
mkdir -p ./factory/sites
siteops --approved-source official --project ./factory inputs aio-install --input-file ./aio-inputs.yaml --read-resources --save-site ./factory/sites/plant-one.yaml
siteops --approved-source official --project ./factory plan aio-install -l name=plant-one
```

The answer file in this example must resolve `siteName: plant-one` and
its first cluster. The saved document is an ordinary Site. Site Ops does
not overwrite an existing file. When saving into the selected project
Site inventory, it checks the new name and path against configured Sites
before writing. Files saved inside a configured Site directory need a
lowercase `.yaml` or `.yml` suffix so the inventory can discover them.
A Site saved outside that inventory remains available by explicit path.
An inline Site remains in memory unless you explicitly choose `--save-site`.
Saving a Site built from resource observations does not store the
observations or recheck their prerequisites on later `--site-file` use.
If you keep the cluster's name as the Site name, use it as the Site filename
and selector. Two clusters with the same name need different Site names in
one project, so supply `siteName` for the second. Without an environment label, the saved
Site does not match the manifest's default `environment=dev` selector.
Use an explicit name selection or supply intentional fleet labels.
Keep Site and answer files outside the content cache and verified package.
To reuse a complete standalone Site without saving it into a project, pass
`--site-file ./plant-one.yaml` to `plan`, `validate`, or `deploy`. A standalone
Site must be complete and cannot inherit from packaged example Sites.
Configured Sites can continue to use the existing inheritance and overlay
rules.

Settings outside the typed inputs belong in the saved Site, such as a
[trust bundle you manage](../workspaces/iot-operations/manifests/aio-install/README.md#settings-scale-kit-derives).
Typed answers never change a saved Site, so edit the file itself.

Use one resource group per AIO instance. A Site built from a cluster ID
takes that cluster's resource group, so connect each Arc cluster in its own
resource group before you save its Site.

For a separate fleet deployment after plant-one already runs AIO, create
a distinct `fleet-inputs.yaml` answer file. Keep the common answers
there and supply each new Site name and cluster ID inline:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  environment: dev
  country: US
  enableSecretSync: false
```

The original file may have `enableSecretSync: true` and an
`existingVault`. Copying it and overriding only `enableSecretSync=false`
is invalid because `existingVault` is then inactive. The separate
file contains no identity of the first cluster or conditional vault input.
The four required Site fields derived from `cluster` are omitted,
so each authorized read supplies the new cluster's facts:

```text
siteops --approved-source official --project ./factory inputs aio-install --input-file ./fleet-inputs.yaml --input siteName=plant-two --input cluster="<second-Arc-cluster-ID>" --read-resources --save-site ./factory/sites/plant-two.yaml
siteops --approved-source official --project ./factory inputs aio-install --input-file ./fleet-inputs.yaml --input siteName=plant-three --input cluster="<third-Arc-cluster-ID>" --read-resources --save-site ./factory/sites/plant-three.yaml
siteops --approved-source official --project ./factory plan aio-install -l name=plant-two,name=plant-three
```

The selector with two names bounds this plan to new Sites and excludes
plant-one. Review the Site count and names before using the same
selector with `deploy`. Reapplying `aio-install` to a cluster that already
runs AIO can overwrite settings managed by the operator. To target three
or four new clusters instead, save more Sites from the fleet file
and include only their names. The manifest permits
three concurrent Sites by default. For four concurrent Sites, pass
`--parallel 4` to both plan and deploy. Use a label such as
`environment=dev` only after confirming that it selects precisely the
new Sites and excludes plant-one. `parallel` limits concurrent work,
not the number of Sites selected. Saved Sites do not repeat guided
OIDC and workload identity checks when Secret Sync is enabled.

For an established dev fleet, each configured Site keeps its own
`properties.aioRelease`. One plan and one deploy can select all matching
Sites even when some request `2607` and others `2608`:

```text
siteops --approved-source official --project ./factory plan aio-install -l environment=dev
siteops --approved-source official --project ./factory deploy aio-install -l environment=dev
```

`-l environment=dev` selects every configured Site labeled `dev`, including
Sites that already run AIO. Review the exact Site names and operations in
the plan and the `aioRelease` value in each selected Site before deploying.
For only two or three new clusters, use the
bounded `name=` selector above so a previously deployed Site is not
reinstalled.

An explicit Site replaces the manifest's default selector or `sites:` list.
With `--site-file`, `--input-file`, or `--input`, `-l` never adds another
Site. The explicit Site must match every `-l` term, otherwise the command
stops before preparing a plan. For example,
`siteops plan aio-install --source "official@<release>" --input-file ./plant.yaml -l environment=prod`
proceeds only when those answers set the `environment` input to `prod`. If a
manifest does not declare typed inputs,
use a complete Site file or the workflow for configured Sites. `browse` remains
descriptive: it never treats authored guidance as an executable input schema.

For fleet deployments, use [project Sites](projects.md) and
[targeting](targeting.md). For the separate permission and provenance
boundaries, see [workspace packages](workspace-packages.md).

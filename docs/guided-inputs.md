# Supply one Site with typed inputs

One explicit Site can be built in memory from typed answers, without a
project pin, saved Site, generated answer file or separate plan command.
Install a compatible Site Ops build and select a release containing the
complete AIO workspace and its typed input contract. The source/version
comes from your selected release instructions. `official` denotes an
independently enrolled consumer source, not an approval supplied by the
package. See [source enrollment](projects.md#use-an-approved-source).
Independent `--trust-policy` and `--trusted-root` files can select a
provider locator instead. Source approval does not sign in to Azure or
approve deployment.

Existing local `-w` workspaces and configured Sites remain supported. There
is no required migration to a project pin or typed answers. Use
`siteops inputs` when a manifest declares a typed contract and you want one
explicit Site without first configuring it. Do not combine `--input-file`, `--input`
or `--site-file` targeting with a configured-Site `-l` selector. Saved Sites
can later be selected with the same explicit fleet selectors as before.

## Install AIO from an existing cluster

The existing Arc cluster resource ID is the only required target answer
for the guided AIO installation. Replace the placeholder and run:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

In a private TTY, deploy shows the validated resource IDs and prepares one
executable plan with each target's subscription, resource group and operations.
It asks for confirmation, then executes that same plan. For JSON,
CI or any noninteractive invocation, add `--yes` to authorize execution
explicitly. Without it the command stops with a usage error before content
or Azure access. The cluster and resource group must already exist. The
declared resource-ID answer authorizes bounded reads using your Azure CLI
identity to derive subscription, resource group, region and cluster name.
An explicit manual value must agree with those facts. Deployment can create
or update resources and incur charges. A successful deployment is not a
workload health check.

For a separate executable preview, run:

```text
siteops plan aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Add `--describe` for the compile-free plan shape. A separate plan never
authorizes a later deploy: deploy builds a fresh plan. Direct `--source`
requires an explicit published release and online source resolution, even
when verified package bytes can be reused from cache. If the release contains
several workspaces, global `-w` selects a relative workspace path inside
that source, not a local directory. Global `--project` can supply your
configured Sites without changing its existing pin. A
[project pin](projects.md#run-project-pin) is optional for this single target
and useful for repeatable fleets or `--offline-content`.

Site Ops generates a stable name from a lowercase cluster prefix and a hash
of its full resource ID. Review that name in the plan. Add
`--input siteName=plant-one` to the deploy command (and any separate plan)
to choose a different name.
Typed Site names use lowercase letters, digits and interior hyphens, with a
maximum of 59 characters. Existing saved Sites retain their names.

Environment and country labels are optional. Add `--input environment=dev`
or `--input country=US` when you want those labels and resource tags.
Omitted values create neither labels nor tags. The default AIO release is
2608, cert-manager is enabled, and Secret Sync is disabled.

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

For an optional inspection-only preview, run
`inputs aio-install --source "official@<release>" --input-file ./aio-inputs.yaml --read-resources`.
Only `inputs` requires that flag to authorize an Azure read while previewing
or saving.
It reads the selected cluster and structurally validates one Site without
writing it, compiling templates or submitting deployments. Plain and local JSON output
include a private display of the Site and its defaults. In CI and other
redacted destinations, the command reports resolution status without
publishing Site values.

## Use an existing resource ID

The ID must identify an existing
`Microsoft.Kubernetes/connectedClusters` resource. Site Ops checks its
type and identity, reads its region, and derives the four target values
before calling the normal planner. A supplied manual value must agree
with the observed resource. The read uses your existing Azure CLI
identity and fails if that identity cannot access the target. Site Ops
does not sign you in, change accounts or grant permissions. It reads
again for the separate deploy invocation rather than treating a
previous plan's observation as current.
`--offline-content` on a pinned project restricts source content acquisition,
not Azure target reads or deployment. Direct `--source` instead requires
online release resolution, even if its verified content is already cached.

To enable Secret Sync during that same AIO deployment, set
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

For a short non-secret command, supply the same named answers with repeated
`--input NAME=VALUE` options on `inputs`, `plan` or `deploy`. Inline answers override
the input file. `--input-file` selects exactly one answer file. Repeating
that option is an error, not a way to select several Sites. Strings and
strict `true` or `false` booleans are parsed
according to the selected contract. Do not put secrets in process arguments
or shell history. The initial typed route rejects contracts with protected
inputs. Duplicate and unknown answer names fail.

### Manual targets without resource reads

Omit `cluster` and provide `siteName`, `subscription`, `resourceGroup`,
`location` and `clusterName` instead. Use the same inline or file route
without an Azure target read. Name, environment and country overrides follow
the same validation rules, and the labels remain optional. The manual route
does not establish Azure resource existence or enable guided Secret Sync,
which requires observed cluster prerequisites.

```text
siteops deploy aio-install --source "official@<release>" --input siteName=plant-one --input subscription=<subscription-ID> --input resourceGroup=<existing-resource-group> --input location=<region> --input clusterName=<Arc-cluster-name>
```

## Enable Secret Sync on an existing instance

For an existing AIO 2607 or 2608 instance, supply its resource ID instead
of running installation again:

```text
siteops deploy secretsync --source "official@<release>" --input "instance=<AIO-instance-resource-ID>"
```

`instance` is the only required answer. Site Ops reads the instance, its
custom location and that location's Arc cluster. The related resources
must remain in the instance's subscription and resource group. OIDC issuer
and workload identity must already be enabled. Unsupported relationships,
conflicting target values or missing prerequisites fail before deployment.
The reads do not enable cluster features or grant roles.

The plan contains only instance resolution and Secret Sync enablement.
It uses the actual instance name and does not reinstall or upgrade AIO.
The shared typed contract supports existing AIO 2607 and 2608 instances.
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

## Check the result

The final Site Ops result reports deployment outcomes separately from
readiness and functionality, which remain `not-assessed`. With Azure CLI
and its `azure-iot-ops` extension installed, use the existing
[AIO health check](https://learn.microsoft.com/en-us/cli/azure/iot/ops#az-iot-ops-check)
to inspect the deployed services. You need authorized Kubernetes access
and a kubeconfig context for the cluster you just deployed:

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
deployment result and target state before retrying. For a disposable demo,
plan cleanup of the created Azure resources and Kubernetes objects without
deleting the existing cluster or resources owned by others.

## Deploy a selected AIO release to each cluster

`aioRelease` is a typed answer mapped to the selected Site's
`properties.aioRelease`. The bundled IoT Operations workspace supplies
release configurations for `2607` and `2608`, with `2608` as the current
default. To use different releases without saving Site files, prepare and
deploy each existing Arc cluster in a separate invocation:

```text
siteops deploy aio-install --source "official@<release>" --input siteName=plant-2608 --input "cluster=<Arc-ID-A>" --input environment=dev --input country=US --input aioRelease=2608
siteops deploy aio-install --source "official@<release>" --input siteName=plant-2607 --input "cluster=<Arc-ID-B>" --input environment=dev --input country=US --input aioRelease=2607
```

Replace the two placeholders with distinct full connected-cluster ARM IDs.
Each command constructs one Site in memory and selects the corresponding
release parameters from the verified workspace. Each deploy reviews its own
plan. These examples leave Secret Sync disabled.
Run `aio-upgrade`, not `aio-install`, to change an existing installation's
release.

## Keep a Site for later

For repeatable configured-Site and fleet workflows, first
[pin a reviewed release](projects.md#run-project-pin) in `./factory`
and select its `official` approval. To retain resolved configuration, create
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
An inline target remains in memory unless you explicitly choose `--save-site`.
Saving a Site built from resource observations does not store the
observations or re-check their prerequisites on later `--site-file` use.
If you retain the generated name, use the name shown by the input preview
as the Site filename and selector. Without an environment label, the saved
Site does not match the manifest's default `environment=dev` selector.
Use an explicit name selection or supply intentional fleet labels.
Keep Site and answer files outside the content cache and verified package.
To reuse a complete standalone Site without saving it into a project, pass
`--site-file ./plant-one.yaml` to `plan`, `validate`, or `deploy`. A standalone
Site must be complete and cannot inherit from packaged example Sites.
Configured Sites can continue to use the existing inheritance and overlay
rules.

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
file contains no first-cluster identity or conditional vault input.
The four required target fields derived from `cluster` are omitted,
so each authorized read supplies the new cluster's facts:

```text
siteops --approved-source official --project ./factory inputs aio-install --input-file ./fleet-inputs.yaml --input siteName=plant-two --input cluster="<second-Arc-cluster-ID>" --read-resources --save-site ./factory/sites/plant-two.yaml
siteops --approved-source official --project ./factory inputs aio-install --input-file ./fleet-inputs.yaml --input siteName=plant-three --input cluster="<third-Arc-cluster-ID>" --read-resources --save-site ./factory/sites/plant-three.yaml
siteops --approved-source official --project ./factory plan aio-install -l name=plant-two,name=plant-three
```

The two-name selector bounds this plan to new Sites and excludes
plant-one. Review the target count and names before using the same
selector with `deploy`. Reapplying `aio-install` to a cluster that already
runs AIO can overwrite settings managed by the operator. To target three
or four new clusters instead, save more Sites from the fleet file
and include only their names. The manifest permits
three concurrent Sites by default. For four concurrent Sites, pass
`--parallel 4` to both plan and deploy. Use a label such as
`environment=dev` only after confirming that it selects precisely the
new cohort and excludes plant-one. `parallel` limits concurrent work,
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
Sites that already run AIO. Review the exact target names and operations in
the plan and the `aioRelease` value in each selected Site before deploying.
For only two or three new clusters, use the
bounded `name=` selector above so a previously deployed Site is not
reinstalled.

An explicit Site replaces the manifest's default selector or `sites:` list.
Combining `--site-file`, `--input-file`, or `--input` with `-l` fails rather than
joining another target. If an entry does not declare an input contract,
use a complete Site file or the configured-Site workflow. `browse` remains
descriptive: it never treats authored guidance as an executable input schema.

For fleet deployments, use [project Sites](projects.md) and
[targeting](targeting.md). For the separate permission and provenance
boundaries, see [workspace packages](workspace-packages.md).

# Supply one Site with typed inputs

Select a deployment, inspect its input contract, then prepare a plan for one
explicit Site. You can supply answers inline or in a file without adding a
Site to the workspace. The same manifest and planner still support configured
Sites and fleet selectors.

This guide uses `aio-install` in an approved, verified workspace package.
First [install a compatible Site Ops build](install-siteops.md) and
[pin that workspace](projects.md#run-project-pin). The package must
include `aio-install` and its input contract. The examples below use
`--approved-source official`, explicitly selecting the source enrolled
by the [bootstrap](install-siteops.md#choose-an-installation-route).
If you manage independent policy and trusted root files yourself,
use both `--trust-policy policy.json` and
`--trusted-root trusted-root.json` instead, and supply `--source`
when pinning. Never combine those options with an approved source.
The project pin cannot authorize its own source policy. A local
checkout selected with `-w` also works for authoring without a pin
or package trust options.

Existing local `-w` workspaces and configured Sites remain supported. There
is no required migration to a project pin or typed answers. Use
`siteops inputs` when a manifest declares a typed contract and you want one
explicit Site without first configuring it. Do not combine `--input-file`, `--input`
or `--site-file` targeting with a configured-Site `-l` selector. Saved Sites
can later be selected with the same explicit fleet selectors as before.

## Inspect and fill the inputs

Pin the approved content release for your project as described in
[operator projects](projects.md#use-an-approved-source). The following
commands use that project and enrollment:

```text
siteops --approved-source official --project ./factory inputs aio-install --example ./aio-inputs.yaml
```

This one command lists required, derivable, defaulted and conditional inputs
and writes an incomplete example. Omit `--example` to inspect without
writing anything. The example includes a null optional `cluster` ID so
you can choose the resource route without adding a new YAML key:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  siteName: null       # Choose a Site name, such as plant-one.
  subscription: null   # Use the subscription containing the Arc cluster.
  resourceGroup: null  # Use the existing cluster's resource group.
  location: null       # Use the cluster's Azure region.
  clusterName: null    # Use the existing Arc cluster name.
  environment: null    # Choose the environment label, such as dev or prod.
  country: null        # Choose the country label for resource tags.
  cluster: null        # Or supply the full existing Arc cluster resource ID.
```

For the manual route, fill the seven required fields and leave `cluster`
as `null`. For the resource route, fill `siteName`, `environment`, `country`
and `cluster`. Leave `subscription`, `resourceGroup`, `location` and
`clusterName` as `null` so an authorized read derives them.
You can supply the same named answer with `--input`.
Neither route is deployable until its required answers are complete.
The default AIO path selects release 2608,
enables cert-manager, and leaves Secret Sync disabled.
Your existing cluster, resource group, appropriate Azure permissions, and
Azure CLI are prerequisites. Input resolution checks types and the Site
structure. It does not check that those Azure resources exist or that your
identity can deploy. Do not use a plan as evidence of cluster readiness.

Review the plan and target before deploying:

```text
siteops --approved-source official --project ./factory plan aio-install --input-file ./aio-inputs.yaml
siteops --approved-source official --project ./factory deploy aio-install --input-file ./aio-inputs.yaml
```

The ordinary plan performs local preflight and does not read Azure
resources or submit deployments.
The deploy command prepares again before making changes and may update
resources or incur charges. Authenticate explicitly with the identity
authorized for the target. A successful deployment result is not evidence of
AIO component health or a working application.

For an optional preview before planning, run
`siteops inputs aio-install --input-file ./aio-inputs.yaml` with the same
project and trust options.
It resolves and structurally validates one Site without writing it,
compiling templates or reading Azure resources. Plain and local JSON output
include a private display of the Site and its defaults. In CI and other
redacted destinations, the command reports resolution status without
publishing Site values.

## Use an existing resource ID

Give `cluster` the full ARM ID of your existing Arc-connected Kubernetes
cluster in the generated answer file. Keep the four derived fields `null`.
Explicitly allow the read with `--read-resources` on both `plan` and
`deploy`:

```text
siteops --approved-source official --project ./factory plan aio-install --input-file ./aio-inputs.yaml --read-resources
siteops --approved-source official --project ./factory deploy aio-install --input-file ./aio-inputs.yaml --read-resources
```

For this route, also fill `siteName`, `environment` and `country`.
The ID must identify an existing
`Microsoft.Kubernetes/connectedClusters` resource. Site Ops checks its
type and identity, reads its region, and derives the four target values
before calling the normal planner. A supplied manual value must agree
with the observed resource. The read uses your existing Azure CLI
identity and fails if that identity cannot access the target. Site Ops
does not sign you in, change accounts or grant permissions. It reads
again for the separate deploy invocation rather than treating a
previous plan's observation as current.
`--offline` applies to the pinned content source only. It does not
prevent an Azure read explicitly requested with `--read-resources`.

To enable Secret Sync during that same AIO deployment, set
`enableSecretSync: true` in the answer file. This guided route requires
`cluster` and `--read-resources`. Before any deployment writes, Azure
must report an OIDC issuer and enabled workload identity on the existing
cluster. You may supply an optional `existingVault` resource ID when
enabled. It must be in the same subscription as the Site but may be in
a different resource group. Omit it to create a new vault. A successful
resource read establishes those reported settings at that moment, not
cluster readiness, federation success or secret materialization.

For a short non-secret command, supply the same named answers with repeated
`--input NAME=VALUE` options on `plan` or `deploy`. Inline answers override
the input file. `--input-file` selects exactly one answer file. Repeating
that option is an error, not a way to select several Sites. Strings and
strict `true` or `false` booleans are parsed
according to the selected contract. Do not put secrets in process arguments
or shell history. The initial typed route rejects contracts with protected
inputs. Duplicate and unknown answer names fail.

## Deploy a selected AIO release to each cluster

`aioRelease` is a typed answer mapped to the selected Site's
`properties.aioRelease`. The bundled IoT Operations workspace supplies
release configurations for `2607` and `2608`, with `2608` as the current
default. To use different releases without saving Site files, prepare and
deploy each existing Arc cluster in a separate invocation:

```text
siteops --approved-source official --project ./factory plan aio-install --input siteName=plant-2608 --input "cluster=<Arc-ID-A>" --input environment=dev --input country=US --input aioRelease=2608 --read-resources
siteops --approved-source official --project ./factory deploy aio-install --input siteName=plant-2608 --input "cluster=<Arc-ID-A>" --input environment=dev --input country=US --input aioRelease=2608 --read-resources
siteops --approved-source official --project ./factory plan aio-install --input siteName=plant-2607 --input "cluster=<Arc-ID-B>" --input environment=dev --input country=US --input aioRelease=2607 --read-resources
siteops --approved-source official --project ./factory deploy aio-install --input siteName=plant-2607 --input "cluster=<Arc-ID-B>" --input environment=dev --input country=US --input aioRelease=2607 --read-resources
```

Replace the two placeholders with distinct full connected-cluster ARM IDs.
Each command constructs one Site in memory and selects the corresponding
release parameters from the verified workspace. Review each plan before its
deploy command. These examples use the default Secret Sync disabled setting.
Run `aio-upgrade`, not `aio-install`, to change an existing installation's
release.

## Keep a Site for later

To retain the resolved configuration, create the project's `sites` directory
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
before writing. An inline target remains in memory unless you explicitly
choose `--save-site`.
Saving a Site built from resource observations does not store the
observations or re-check their prerequisites on later `--site-file` use.
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

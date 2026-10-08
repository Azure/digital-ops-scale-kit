# Manifest Reference

Manifests define **what** to deploy and in **what order**.

## Basic structure

```yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-install
description: Deploy Azure IoT Operations

# Site selection (choose one)
sites:
  - munich-dev
  - seattle-dev
# OR
selector: "environment=dev"

# Parallel execution
parallel: 3  # Deploy up to 3 sites concurrently

# Manifest-level parameters (applied to all steps)
parameters:
  - parameters/common/common.yaml

steps:
  - name: step-name
    template: templates/resource.bicep
    scope: resourceGroup
    parameters:
      - parameters/step-specific.yaml
    when: "{{ site.labels.condition == 'true' }}"
```

## Manifest fields

| Field | Required | Behavior |
|-------|----------|----------|
| `apiVersion` | no | `siteops/v1`, the default when omitted. |
| `kind` | no | `Manifest` when present. |
| `name` | no | Manifest name. Defaults to the filename without its extension. |
| `description` | no | Free text describing the manifest. |
| `sites` | no | Site names or relative Site paths to target. When both `sites:` and `selector:` are set, `sites:` is used. |
| `selector` | no | Label selector, such as `environment=dev`. `siteSelector` is a deprecated spelling that logs a warning. Declaring both spellings is an error. |
| `parallel` | no | Concurrent Sites. See [Parallel execution](#parallel-execution). |
| `parameters` | no | Parameter sources applied to every step. See [Manifest-level parameters](#manifest-level-parameters). |
| `parameterCompositions` | no | Workspace contracts for composed parameter collections. |
| `steps` | yes | Ordered steps. Validation reports `Manifest has no steps defined` when the list is empty or missing. |

Any other key at the top level is rejected with a `did you mean` hint. A
manifest may instead use the envelope shape: `apiVersion` and `kind` at the
top, `name`, `description` and `labels` under `metadata`, and every other
field under `spec`.

## Site selection

| Method | Behavior |
|--------|----------|
| `sites:` list | Deploy to named sites only |
| `selector:` | Deploy to all sites matching label |
| CLI `-l` flag | Overrides manifest selection. Repeatable. `name=` may carry multiple values (OR-combined) |

```bash
# Overrides manifest selection, deploys to all prod sites.
siteops -w workspaces/iot-operations deploy aio-install -l environment=prod

# Multi-site CLI selection (name OR-combines).
siteops -w workspaces/iot-operations deploy aio-install -l name=munich-dev,name=seattle-dev
```

A manifest with neither `sites:` nor `selector:` is a library or partial.
It can be checked with `validate`, while `plan` and `deploy` require `-l`
or an explicit Site supplied with `--site-file`, `--input-file`, or `--input`.
See [targeting.md](targeting.md) for the full grammar, the no-match diagnostic,
and validation rules.

## Typed input contract

A deployment that supports [guided single-Site inputs](guided-inputs.md)
places `inputs.yaml` next to `manifest.yaml` or `manifest.yml`. A flat manifest
such as `manifests/storage.yaml` uses `manifests/storage.inputs.yaml` so
several flat manifests cannot share one contract. The file is packaged with
the workspace and checked against the acquired package's file inventory
before Site Ops reads it. It is not a manifest, a browsing card, or a source
of permission to deploy. Without a sibling `manifest.yaml` or `manifest.yml`,
a nested `manifests/.../inputs.yaml` is itself conventionally discoverable as
a manifest. If an actual manifest uses that name alongside a sibling
`manifest.yaml` or `manifest.yml`, list it explicitly in `content.yaml`.
Existing entry guidance remains descriptive. A parseable sibling is treated
as a typed contract when it declares `kind: SiteInputContract` or an
`apiVersion` in the `siteops.inputs/` namespace. Unrelated sample wiring
does not become a contract. A declared contract with invalid kind, version
or fields fails rather than being ignored.

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputContract
siteDefaults:
  properties:
    release: "1"
inputs:
  - name: siteName
    type: string
    description: Name for the explicit Site.
    sitePath: name
  - name: subscription
    type: string
    description: Subscription where the resources will be created.
    sitePath: subscription
  - name: location
    type: string
    description: Azure deployment region.
    sitePath: location
  - name: featureEnabled
    type: boolean
    description: Include the optional feature.
    sitePath: properties.featureEnabled
    default: false
```

`siteDefaults` may hold only `labels`, `properties`, and `parameters`.
Each ordinary `inputs` row declares one semantic name, a `string` or
`boolean` type, description, and a destination under `name`,
`subscription`, `resourceGroup`, `location`, `labels`, `parameters`, or
`properties`.
An input without a default is required unless `required: false` is
declared. Defaults, then answer files, then inline answers contribute
values. A conditional row may use
`when: {input: featureEnabled, equals: true}` to require it only when a
previously declared unconditional controller has that value. Contracts
declaring `sensitive: true` are rejected.
Author only mappings that ordinary Site parsing and actual deployment
preparation accept. Do not map two inputs onto the same Site field.

String inputs may declare `format: nonEmpty` or `format: dnsLabel`.
The DNS label format accepts lowercase letters, digits and interior hyphens,
with at most 63 characters. `maxLength` supplies a further string limit.
These constraints apply to defaults, file values and inline overrides.

A named `azureResourceId` input can instead derive values for existing
semantic inputs. This is an optional read, not a source of Azure
credentials or arbitrary provider commands. For example, after declaring
`subscription`, `resourceGroup`, `location` and `clusterName` as string
inputs:

```yaml
- name: cluster
  type: azureResourceId
  description: Existing Arc-connected Kubernetes cluster resource ID.
  required: false
  resource:
    type: Microsoft.Kubernetes/connectedClusters
    apiVersion: "2024-07-15-preview"
  derive:
    subscription: subscription
    resourceGroup: resourceGroup
    location: location
    name: clusterName
```

When an optional resource ID derives required fields, `inputs --example`
includes `cluster: null` alongside required fields left null. Leave the
resource null for manual answers, or fill its ID and use
`--read-resources` with `inputs` to preview or save the resolved Site.
For `plan` and `deploy`, supplying the ID itself authorizes its bounded
read and derives the matching target facts. A null optional
ID does not trigger an Azure read. The example remains incomplete until
the operator supplies every requirement through one route.

An optional top-level `nameFromResource: cluster` selects an unconditional
declared resource to generate the Site name after its authorized read.
The contract must also declare a required unconditional string input mapped
to `name`, without another default, derivation or conditional readers.
An explicit name overrides generation. Without the resource, that name
remains required for the manual route.

The generated name combines a lowercase sanitized resource-name prefix of
at most 18 characters with a hyphen and 12 hexadecimal SHA-256 characters
from the full case-normalized resource ID. Identical IDs retain their names
across case variations, while different resource groups or subscriptions
contribute to the hash. With this declaration, `inputs --example` prefers
the resource route and omits the fields it supplies. The AIO example
therefore contains only `cluster: null`.

A resource role can follow an earlier role through a declared relationship:

```yaml
- name: customLocation
  type: azureResourceId
  description: Custom location referenced by the selected resource.
  fromResource: {input: instance, field: extendedLocation}
  resource:
    type: Microsoft.ExtendedLocation/customLocations
    apiVersion: "2021-08-31-preview"
```

The closed fields are `extendedLocation` for the ARM extended-location
reference and `customLocations.hostResourceId` for a custom location's host.
The source must be an earlier resource role with the same activation
condition. Related roles cannot require or accept separate operator answers.
They must contribute a mapping, prerequisite or dependent read, and count
toward the four-resource limit.

For `inputs`, `--read-resources` also authorizes these declared reads.
`plan` and `deploy` authorize them with the supplied ID. Site Ops validates
each source observation, related ID and declared target type before the
next read. Related resources must stay in the source's subscription and
resource group. This restriction is enforced by the engine, not selectable
by content. No arbitrary property paths, URLs, code or subscription searches
are supported. Explicit independent resources, such as a supplied vault,
keep their declared scope rules.

`nameFromResource` may select a related role. The example then asks for its
explicit source input, while naming waits for the related resource's validated
observation. This lets an instance input resolve its associated cluster and
retain the same Site naming as a directly supplied cluster.

An operator provides `cluster` through the same `--input NAME=VALUE`
or answer file used for strings. Add `--read-resources` only to `inputs`
when previewing or saving. `plan` and `deploy` read declared IDs without
that option, while `validate` stays read-free. If the role is omitted,
ordinary manual answers remain required and no Azure read occurs. The derived values
fill missing answers. Any manually supplied answer must agree with the
resource ID or the read response. A role may also use `sitePath` to bind
its verified ID to one Site parameter, and `resource.subscription: site`
to require the Site's subscription while allowing another resource
group. Each contract admits at most four resource roles and only
top-level resource-group ARM IDs.

Conditions and prerequisites use a closed vocabulary. A connected
cluster role can declare `requires` facts
`connectedClusters.workloadIdentityEnabled` and
`connectedClusters.oidcIssuerAvailable`, optionally gated by an earlier
boolean input. An active prerequisite must be verified by an explicit
read or input resolution fails before deployment. Neither fact proves
readiness or secret materialization. The selected workspace declares
resource types and allowed mappings. Site Ops selects the read provider
and the operator's configured Azure identity.

`siteops inputs <manifest>` shows the contract and can write an incomplete
answer file for the operator. Conditional fields are omitted from the example
until their controller activates them. Complete typed answers let `inputs`
preview the structurally validated Site without writing it. A completed
file has kind `SiteInputValues` and a `values:` mapping. No Site is written
or altered by `plan` or `deploy` with typed answers. Use
`siteops inputs <manifest> --save-site FILE` after
supplying complete non-protected answers to retain an ordinary Site. A
manifest without a contract continues to accept complete Site files and
configured Sites.

`inputs --output json` returns the contract fields. With complete answers,
it also includes `resolution.status: ready` and `resolution.site` for
authorized private output. Redacted destinations set `resolution.site` to
`null`, meaning the Site was resolved but its values were withheld. This
inspection output is not public gallery metadata or an executable plan.

## Manifest-level parameters

A string loads one fixed or site-selected parameter file:

```yaml
parameters:
  - parameters/common/common.yaml
  - "parameters/aio-releases/{{ site.properties.aioRelease }}.yaml"
```

Use the object form when one site property selects an ordered list of files:

```yaml
parameters:
  - path: "resource-sets/devices/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.devices }}"
    collections: [devices]
```

`forEach` must resolve to a list of unique, non-empty strings. Each item
replaces `{{ item }}` in order. An omitted property or `[]` loads no files. A
scalar value reports the list migration rather than iterating its characters.
Every expanded path stays inside the workspace.

`collections` names the composed parameter arrays that source may contribute.
A source carrying a governed collection must use the object form. The
composition rules come from one or more fixed workspace contracts:

```yaml
parameterCompositions:
  - contracts/aio-catalog.yaml
```

Included manifests may contribute the same contract path. Paths are
canonicalized and deduplicated during include flattening. See
[Resource catalog](resource-catalog.md) for resource identity, references,
external assertions, and provenance.

## Step types

### Bicep/ARM steps (default)

```yaml
- name: deploy-resources
  template: templates/my-template.bicep
  scope: resourceGroup  # or 'subscription'
  parameters:
    - parameters/my-params.yaml
```

Executable preparation acquires the template schema, removes supplied
parameters the template does not declare, and requires every non-nullable
parameter that has no default. Nullable parameters may be omitted even when
they declare no default. A top-level parameter name derived from a prior
operation remains deferred until that output resolves.

### Kubectl steps

```yaml
- name: apply-config
  type: kubectl
  operation: apply
  arc:
    name: "{{ site.parameters.clusterName }}"
    resourceGroup: "{{ site.resourceGroup }}"
  files:
    - https://example.com/manifest.yaml
    - configs/local-manifest.yaml
```

| Field | Required | Behavior |
|-------|----------|----------|
| `type` | yes | `kubectl`. |
| `operation` | yes | `apply`, the only supported operation. |
| `arc.name` | yes | Name of the Arc cluster. Supports site variables. |
| `arc.resourceGroup` | yes | Resource group of the cluster. Supports site variables. |
| `files` | yes | Nonempty list of workspace paths or HTTPS URLs to apply. |
| `when` | no | Condition. See [Conditional steps](#conditional-steps). |

Site Ops reaches the cluster through an `az connectedk8s proxy` session that
it opens for the step. `plan` and `deploy` check that `kubectl`, Azure CLI and
its connectedk8s extension are available on the machine running Site Ops. The
cluster also needs cluster connect enabled, and the account signed in to Azure
CLI needs Kubernetes permissions for the step. See
[Azure CLI and az login](install-siteops.md#azure-cli-and-az-login).

Authored local paths must remain inside the workspace, and URLs must use
HTTPS. When the content comes from a verified workspace package, `files` must
name package paths, and HTTPS URLs are rejected before any tool runs.
Site-selected local files are required only for sites where the step's
condition applies. Fully resolved cluster names, resource groups, and file
values are checked during executable preparation. Values derived from prior
operation outputs remain deferred until execution.

### Wait steps

A wait step gates the steps that follow it on an Azure condition. It blocks the
site's step sequence until the condition is met, then lets the remaining steps
run. Use it when a prior step starts asynchronous work whose completion is not
reflected in the deployment's own result. A timeout or a terminal failure fails
the step, which skips the site's remaining steps.

The supported condition type is `arm-tag`: poll a tag on an ARM resource
until it reaches an expected value.

```yaml
- name: wait-for-bootstrap
  type: wait
  condition:
    type: arm-tag
    resourceId: "/subscriptions/{{ site.subscription }}/resourceGroups/{{ site.resourceGroup }}/providers/Microsoft.HybridCompute/machines/{{ site.parameters.aksee.machineName }}"
    tagKey: "siteops.bootstrap.state"
    expectedValue: "succeeded"
    failurePattern: "failed-*"   # optional: abort fast on a matching value
  timeoutMinutes: 45
  pollIntervalSeconds: 30
```

| Field | Required | Behavior |
|-------|----------|----------|
| `condition.type` | yes | Condition kind. Currently `arm-tag`. |
| `condition.resourceId` | yes | Full ARM resource ID to poll. Supports template variables and `{{ steps.X.outputs.Y }}` references to prior steps. |
| `condition.tagKey` | yes | Tag name to read. |
| `condition.expectedValue` | yes | Tag value that satisfies the wait. Compared as a string. |
| `condition.failurePattern` | no | An `fnmatch` glob. A tag value matching it aborts the wait immediately instead of waiting for the timeout. Omit for a plain wait-until-expected. |
| `timeoutMinutes` | no (default 30) | Maximum minutes to wait before failing. |
| `pollIntervalSeconds` | no (default 30) | Seconds between checks. |

Behavior notes:

- The deploying identity reads the tag, so it needs read access on the resource. No extra service is provisioned.
- The wait checks the condition once before sleeping, so an already-satisfied condition returns on the first poll.
- A permanent error (authorization failure, resource not found, malformed `resourceId`) fails the step fast rather than polling for the full timeout. Transient errors (throttling, 5xx, network) keep polling.
- A timeout or failure message reports the last observed tag value and the last underlying error.
- These authoring errors fail when the manifest loads: a `failurePattern` that
  also matches `expectedValue`, and a `pollIntervalSeconds` longer than
  `timeoutMinutes`.
- `siteops plan` never polls. `deploy` waits only during execution, after
  preparing and confirming the plan. Fully resolved values use
  the same scalar and success-versus-failure-pattern checks as execution.
  Prior-operation outputs remain deferred until execution.

### Include steps

Splice another manifest's steps into this one's step list at the include's position:

```yaml
- include: ../samples/opc-ua-solution/_partial.yaml
  when: "{{ site.properties.deployOptions.enableOpcUa }}"  # optional
```

See [manifest-includes.md](manifest-includes.md) for the full include contract (path resolution, cycle detection, parameter merge, standalone-vs-partial conventions).

## Conditional steps

Control step execution based on site labels or properties:

```yaml
# Truthy check on properties (recommended for booleans)
- name: secretsync
  template: templates/secretsync/enable-secretsync.bicep
  scope: resourceGroup
  when: "{{ site.properties.deployOptions.enableSecretSync }}"

# String comparison on labels
- name: prod-only-feature
  template: templates/feature.bicep
  scope: resourceGroup
  when: "{{ site.labels.environment == 'prod' }}"

# Run one shared step when either resource area has a selection
- name: device-registry-resources
  template: templates/device-registry/main.bicep
  when:
    any:
      - "{{ site.properties.resourceSets.devices }}"
      - "{{ site.properties.resourceSets.assets }}"
```

### Supported syntax

| Syntax | Example | Use Case |
|--------|---------|----------|
| Truthy check | `{{ site.properties.path }}` | Boolean properties |
| Equals | `{{ site.labels.env == 'prod' }}` | String comparison |
| Not equals | `{{ site.labels.env != 'dev' }}` | Exclusion |
| Boolean comparison | `{{ site.properties.flag == true }}` | Explicit boolean check |
| Any | `when: { any: [...] }` | Run when any listed atomic condition passes |

Truthy evaluation:

- Runs the step: `true`, a nonempty string other than `false` or `0` in any
  letter case, a nonzero number, or a nonempty list or mapping.
- Skips the step: a missing label or property, `false`, `""`, `"false"`, `"0"`, `0`,
  `[]`, or `{}`.

The structured `any` form takes a non-empty list of the atomic expressions
above. Invalid structured conditions fail manifest loading.

## Parallel execution

| Value | Behavior |
|-------|----------|
| `parallel: 1` or `parallel: false` | Sequential (default) |
| `parallel: true` or `parallel: 0` | Unlimited concurrency |
| `parallel: 5` | Up to 5 sites concurrently |
| `parallel: {sites: 5}` | Object form of the same limit |

CLI override: `-p` accepts a positive integer, or `max`, `auto` or `0` for
unlimited concurrency, and replaces the manifest setting:

```bash
siteops -w workspaces/iot-operations plan aio-install -p 5
siteops -w workspaces/iot-operations deploy aio-install -p 5
```

## Deployment scopes

| Scope | Use case | Azure CLI |
|-------|----------|-----------|
| `resourceGroup` | Deploy resources into RG | `az deployment group create` |
| `subscription` | Shared resources (Edge Sites, policies) | `az deployment sub create` |

### Two-phase deployment

When a manifest contains `scope: subscription` steps, Site Ops uses two-phase deployment:

**Phase 1**: subscription-scoped steps:
- Groups selected sites by subscription
- Finds the subscription-level site among the selected sites for each subscription
- Executes subscription-scoped steps once per subscription
- Caches outputs keyed by subscription ID

**Phase 2**: RG-scoped steps:
- Executes for all RG-level sites (parallelizable)
- Subscription-level sites are skipped (no resource group)
- Can reference Phase 1 outputs via cross-scope chaining

Select the subscription-level site in the same command as the RG-level sites,
for example `-l name=contoso-global,name=munich-dev`. A selector such as
`environment=dev` matches only sites carrying that label. When a
subscription-scoped step would run and no subscription-level site is selected
for that subscription, validation reports this error:

```text
Subscription '<subscription>' has RG-level sites (<sites>) but no subscription-level site for subscription-scoped steps
```

```yaml
steps:
  - name: global-edge-site
    template: templates/edge-site/subscription.bicep
    scope: subscription  # Phase 1: once per subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"

  - name: edge-site
    template: templates/edge-site/main.bicep
    scope: resourceGroup  # Phase 2: per RG-level site
    when: "{{ site.properties.deployOptions.enableEdgeSite }}"

  - name: schema-registry
    template: templates/deps/schema-registry.bicep
    scope: resourceGroup  # Phase 2: per RG-level site
    parameters:
      - parameters/inputs/aio-instance.yaml  # Can reference global-edge-site outputs
```

See [parameter-resolution.md](parameter-resolution.md) for cross-scope output chaining details.

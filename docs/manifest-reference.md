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

## Site selection

| Method | Behavior |
|--------|----------|
| `sites:` list | Deploy to named sites only |
| `selector:` | Deploy to all sites matching label |
| CLI `-l` flag | Overrides manifest selection. Repeatable. `name=` may carry multiple values (OR-combined) |

```bash
# Overrides manifest selection, deploys to all prod sites.
siteops deploy manifest.yaml -l environment=prod

# Multi-site CLI selection (name OR-combines).
siteops deploy manifest.yaml -l name=munich-dev,name=seattle-dev
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
a manifest; if an actual manifest uses that name alongside a sibling
`manifest.yaml` or `manifest.yml`, list it explicitly in `content.yaml`.
Existing entry guidance remains descriptive.

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
declaring `sensitive: true` are rejected in this initial route until protected
values can be preserved across planning and reporting without disclosure.
Author only mappings that ordinary Site parsing and actual deployment
preparation accept. Do not map two inputs onto the same Site field.

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
`--read-resources` to derive the matching target facts. A null optional
ID does not trigger an Azure read. The example remains incomplete until
the operator supplies every requirement through one route.

An operator provides `cluster` through the same `--input NAME=VALUE`
or answer file used for strings, and explicitly adds `--read-resources`
to `inputs`, `plan` or `deploy`. If the role is omitted, ordinary manual
answers remain required and no Azure read occurs. The derived values
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
and the operator's configured Azure identity. A later SDK reader can
use the same contract and pinned ARM API version.

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

Authored local paths must remain inside the workspace, and URLs must use
HTTPS. Site-selected local files are required only for sites where the step's
condition applies. Fully resolved cluster names, resource groups, and file
values are checked during executable preparation. Values derived from prior
operation outputs remain deferred until execution.

### Wait steps

A wait step gates the steps that follow it on an Azure condition. It blocks the
site's step sequence until the condition is met, then lets the remaining steps
run. Use it when a prior step starts asynchronous work whose completion is not
reflected in the deployment's own result. A timeout or a terminal failure fails
the step, which skips the site's remaining steps.

The first supported condition type is `arm-tag`: poll a tag on an ARM resource
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
- `siteops plan` and `deploy --dry-run` never poll. Fully resolved values use
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

- `true` → runs step
- `false`, `""`, `"false"`, `"0"`, `0`, `[]`, `{}` → skips step

The structured `any` form takes a non-empty list of the atomic expressions
above. Invalid structured conditions fail manifest loading.

## Parallel execution

| Value | Behavior |
|-------|----------|
| `parallel: 1` | Sequential (default) |
| `parallel: true` | Unlimited concurrency |
| `parallel: 5` | Up to 5 sites concurrently |

CLI override: `siteops plan manifest.yaml -p 5` or
`siteops deploy manifest.yaml -p 5`

## Deployment scopes

| Scope | Use case | Azure CLI |
|-------|----------|-----------|
| `resourceGroup` | Deploy resources into RG | `az deployment group create` |
| `subscription` | Shared resources (Edge Sites, policies) | `az deployment sub create` |

### Two-phase deployment

When a manifest contains `scope: subscription` steps, Site Ops uses two-phase deployment:

**Phase 1**: subscription-scoped steps:
- Groups selected sites by subscription
- Finds the subscription-level site for each subscription
- Executes subscription-scoped steps once per subscription
- Caches outputs keyed by subscription ID

**Phase 2**: RG-scoped steps:
- Executes for all RG-level sites (parallelizable)
- Subscription-level sites are skipped (no resource group)
- Can reference Phase 1 outputs via cross-scope chaining

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

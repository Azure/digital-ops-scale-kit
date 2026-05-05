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

A manifest with neither `sites:` nor `selector:` is a library or partial. It requires `-l` at deploy time. See [targeting.md](targeting.md) for the full grammar, the no-match diagnostic, and validation rules.

## Step types

### Bicep/ARM steps (default)

```yaml
- name: deploy-resources
  template: templates/my-template.bicep
  scope: resourceGroup  # or 'subscription'
  parameters:
    - parameters/my-params.yaml
```

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
```

### Supported syntax

| Syntax | Example | Use Case |
|--------|---------|----------|
| Truthy check | `{{ site.properties.path }}` | Boolean properties |
| Equals | `{{ site.labels.env == 'prod' }}` | String comparison |
| Not equals | `{{ site.labels.env != 'dev' }}` | Exclusion |
| Boolean comparison | `{{ site.properties.flag == true }}` | Explicit boolean check |

Truthy evaluation:

- `true` → runs step
- `false`, `""`, `"false"`, `"0"`, `0`, `[]`, `{}` → skips step

## Parallel execution

| Value | Behavior |
|-------|----------|
| `parallel: 1` | Sequential (default) |
| `parallel: true` | Unlimited concurrency |
| `parallel: 5` | Up to 5 sites concurrently |

CLI override: `siteops deploy manifest.yaml -p 5`

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

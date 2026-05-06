# CI/CD Setup

This guide covers CI/CD configuration for automated testing and deployments. Site Ops is CI/CD-platform agnostic. It runs anywhere Python and `az` CLI are available. This project provides reference implementations for both GitHub Actions (primary) and Azure DevOps (MVP).

| Platform | Location | Status |
|----------|----------|--------|
| [GitHub Actions](#github-actions) | `.github/workflows/` | Primary |
| [Azure DevOps](#azure-devops) | `.pipelines/` | Reference implementation |

## Prerequisites

1. Azure subscription with resources to deploy
2. GitHub repository with Actions enabled **or** Azure DevOps project with Pipelines enabled
3. Azure AD application for OIDC / Workload Identity Federation

## GitHub Actions

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yaml` | Push, pull request, manual | Validate Bicep templates, run unit tests, and validate manifests |
| `deploy.yaml` | Manual (`workflow_dispatch`) | Deploy infrastructure to Azure |
| `_siteops-deploy.yaml` | Called by deploy.yaml | Reusable deployment logic |
| `integration-test.yaml` | Manual (`workflow_dispatch`) | Run the integration pytest suite against an environment that was previously deployed via `deploy.yaml` |
| `e2e-test.yaml` | Manual (`workflow_dispatch`) | Full-stack E2E: k3s + Arc + AIO deploy + integration tests (see [E2E testing](e2e-testing.md)) |

### Azure OIDC Configuration

OIDC (OpenID Connect) allows GitHub Actions to authenticate to Azure without storing secrets. Examples use bash syntax.

#### 1. Create Azure AD application

```bash
# Create app registration
az ad app create --display-name "siteops-github-actions"

# Note the appId (client ID) from output
APP_ID=$(az ad app list --display-name "siteops-github-actions" --query "[0].appId" -o tsv)

# Create service principal
az ad sp create --id $APP_ID
```

#### 2. Create federated credentials

```bash
# For main branch deployments
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:YOUR-ORG/YOUR-REPO:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# For environment-based deployments (recommended)
for ENV in dev staging prod; do
  az ad app federated-credential create \
    --id $APP_ID \
    --parameters "{
      \"name\": \"github-env-$ENV\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:YOUR-ORG/YOUR-REPO:environment:$ENV\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }"
done
```

Alternatively, configure the subject to match a branch (`ref:refs/heads/main`), pull request (`pull_request`), or tag (`ref:refs/tags/v*`) instead of an environment.

#### 3. Assign Azure roles

For basic deployments, Contributor is sufficient:

```bash
az role assignment create \
  --assignee $APP_ID \
  --role "Contributor" \
  --scope /subscriptions/<subscription-id>
```

**For AIO deployments:** The full installation includes RBAC operations (e.g., granting the AIO extension access to the schema registry). Contributor cannot create role assignments. Use Owner with a condition that prevents privilege escalation:

```bash
az role assignment create \
  --assignee $APP_ID \
  --role "Owner" \
  --scope /subscriptions/<subscription-id> \
  --condition $'((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9, f58310d9-a9f6-439a-9e8d-f62e7b41a168})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9, f58310d9-a9f6-439a-9e8d-f62e7b41a168}))' \
  --condition-version "2.0"
```

This condition allows creating and deleting role assignments but blocks these privileged roles:

| GUID | Role |
| ---- | ---- |
| `8e3af657-a8ff-443c-a75c-2fe8c4bcb635` | Owner |
| `18d7d88d-d35e-4fb5-a5c3-7773c20a72d9` | User Access Administrator |
| `f58310d9-a9f6-439a-9e8d-f62e7b41a168` | Role Based Access Control Administrator |

#### Kubernetes RBAC for Arc proxy operations

If your manifests include `kubectl` steps that execute via Arc proxy (Cluster Connect), the CI/CD service principal needs authorization to perform operations inside the Kubernetes cluster. The Azure roles above control access to Azure resources. They do not grant permissions within Kubernetes itself.

There are two approaches to grant this access:

- **Azure RBAC for Arc-enabled Kubernetes**: Assign Azure roles like `Azure Arc Kubernetes Cluster Admin` or a custom role to the service principal, scoped to the cluster resource. This is managed entirely through Azure and requires [Azure RBAC to be enabled on the cluster](https://learn.microsoft.com/azure/azure-arc/kubernetes/azure-rbac).
- **Kubernetes-native RBAC**: Create a `RoleBinding` or `ClusterRoleBinding` on the cluster itself, referencing the service principal's object ID.

The following is a Kubernetes-native example that grants broad access for development. Replace with a least-privilege role for production:

```bash
# Replace <object-id> with the service principal's object ID
# Replace <namespace> with the target namespace (e.g., azure-iot-operations)

kubectl create namespace <namespace> --dry-run=client -o yaml | kubectl apply -f -

kubectl create rolebinding ci-cluster-admin \
  --clusterrole=cluster-admin \
  --user=<object-id> \
  --namespace=<namespace>
```

> **Note:** `cluster-admin` is convenient for getting started but grants full access to the namespace. For production, create a custom `ClusterRole` scoped to the specific resources your manifests manage, or use Azure RBAC with a narrowly scoped role.

This configuration is per-cluster and must be repeated for each Arc-enabled cluster that the CI/CD pipeline targets.

#### 4. Configure GitHub secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Azure AD application client ID |
| `AZURE_TENANT_ID` | Yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Default subscription for OIDC login |
| `SITE_OVERRIDES` | No | JSON object with per-site overrides (see below) |

#### 5. Configure GitHub environments

Go to **Settings → Environments** and create:

#### `dev` environment

- No protection rules (deploys immediately)

#### `staging` environment

- Required reviewers: 1 person
- Deployment branches: `main` only

#### `prod` environment

- Required reviewers: 2 people
- Deployment branches: `main` only
- Wait timer: 5 minutes (optional)

## SITE_OVERRIDES

Use `SITE_OVERRIDES` when you prefer not to commit configuration values (subscriptions, resource groups, credentials) to the repository. Both GHA and ADO pipelines generate `sites.local/*.yaml` files at runtime from this value using identical logic.

| Platform | Where to store | Type |
|----------|---------------|------|
| GitHub Actions | Repository secret (`Settings → Secrets → Actions`) | Secret |
| Azure DevOps | Variable group `siteops-secrets` (`Pipelines → Library`) | Secret variable |

The JSON format is identical on both platforms.

**When to use:**

- You want to keep committed site files as templates with placeholder values
- Different CI environments target different resources
- Your team prefers separation between code and environment configuration

**When not needed:**

- Site files already contain real values
- You're comfortable committing configuration to the repository

### Format

Override subscription, resource group, and parameters per site. Supports nested paths using dot notation (e.g., `parameters.clusterName`):

```json
{
  "munich-dev": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-iot-munich-dev",
    "parameters.clusterName": "munich-dev-arc"
  },
  "munich-prod": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-iot-munich-prod",
    "parameters.clusterName": "munich-prod-arc"
  },
  "seattle-dev": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-iot-seattle-dev",
    "parameters.clusterName": "arc-sea-dev-01"
  },
  "seattle-prod": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-iot-seattle-prod",
    "parameters.clusterName": "arc-sea-prod-01"
  },
  "chicago-staging": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-iot-chicago-staging",
    "parameters.clusterName": "arc-chi-staging-01"
  }
}
```

> **Note:** `SITE_OVERRIDES` is stored as a secret for access control (admin-only modification).
> Individual override values are masked in pipeline logs to prevent exposure (`::add-mask::` on GHA, `##vso[task.setvariable issecret=true]` on ADO).

## Running Deployments

### CI (automatic, both platforms)

CI runs automatically on pushes to main and PRs that modify:

- `siteops/**`
- `workspaces/**`
- `tests/**`
- `scripts/**`
- `pyproject.toml`

Can also be triggered manually from **Actions → CI → Run workflow** (GHA) or **Pipelines → CI → Run pipeline** (ADO).

### Deploy via GitHub UI

1. Go to **Actions** tab
2. Select **"Deploy Infrastructure"**
3. Click **"Run workflow"**
4. Fill in options:
   - **Git ref**: Branch, tag, or commit (optional)
   - **Workspace**: Workspace name (default: `iot-operations`)
   - **Manifest**: Path to manifest, relative to the workspace root (default: `manifests/aio-install.yaml`)
   - **Environment**: `dev`, `staging`, or `prod`
   - **Selector**: Additional site filter (optional, e.g., `region=eastus`)
   - **Dry run**: Preview only, no actual deployment
5. Click **"Run workflow"**

### Deploy via GitHub CLI

```bash
gh workflow run deploy.yaml \
  -f workspace=iot-operations \
  -f manifest=manifests/aio-install.yaml \
  -f environment=dev
```

Add `-f selector="<value>"` to filter sites further:

- `selector="country=US"`: sites with country label
- `selector="name=seattle-dev"`: specific site by name
- `selector="country=US,name=seattle-dev"`: multiple filters

### Deploy via REST API

```bash
curl -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/YOUR-ORG/YOUR-REPO/actions/workflows/deploy.yaml/dispatches \
  -d '{
    "ref": "main",
    "inputs": {
      "workspace": "iot-operations",
      "manifest": "manifests/aio-install.yaml",
      "environment": "dev",
      "selector": "",
      "dry-run": "false"
    }
  }'
```

## Demo Workflows

The iot-operations workspace demonstrates key Site Ops capabilities:

| Step | Manifest | Environment | Sites | Demonstrates |
|------|----------|-------------|-------|--------------|
| 1 | `manifests/aio-install.yaml` | `staging` | chicago-staging | Base AIO platform only |
| 2 | `manifests/aio-install.yaml` | `dev` | munich-dev, seattle-dev | Parallel deployment |
| 3 | `manifests/aio-install.yaml` | `prod` | munich-prod, seattle-prod | Parallel deployment |
| 4 | `samples/opc-ua-solution/manifest.yaml` | `staging` | chicago-staging | OPC UA sample on existing AIO |
| 5 | `scenarios/aio-with-opc-ua.yaml` | any | any | Composed install + sample in one shot |
| 6 | `manifests/aio-upgrade.yaml` | any | any AIO-installed site | Upgrade an existing AIO instance to the site's current `aioRelease` (bump the site's `aioRelease` first, then dispatch) |

### Site configuration

| Site | Environment | `enableSecretSync` |
|------|-------------|--------------------|
| munich-dev | dev | optional |
| seattle-dev | dev | optional |
| munich-prod | prod | recommended |
| seattle-prod | prod | recommended |
| chicago-staging | staging | off |

### Running the demo

```bash
# Step 1: Deploy base AIO to staging
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install.yaml" -f environment="staging"

# Step 2: Deploy AIO to dev (parallel across sites)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install.yaml" -f environment="dev"

# Step 3: Deploy AIO to prod (parallel across sites)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install.yaml" -f environment="prod"

# Step 4: Add the OPC UA sample on top of the staging install
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="samples/opc-ua-solution/manifest.yaml" -f environment="staging"

# Step 5: Composed install + sample in one shot (alternative to steps 1+4)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="scenarios/aio-with-opc-ua.yaml" -f environment="staging"
```

## Workflow Architecture

### GitHub Actions

```
┌─────────────────────────────────────────────────────────────┐
│                    Trigger Sources                          │
├─────────────┬─────────────┬─────────────┬──────────────────┤
│  GitHub UI  │  REST API   │  GitHub CLI │  Pull Request    │
└──────┬──────┴──────┬──────┴──────┬──────┴────────┬─────────┘
       │             │             │               │
       ▼             ▼             ▼               ▼
┌─────────────────────────┐   ┌─────────────────────────────┐
│     deploy.yaml         │   │          ci.yaml            │
│  (workflow_dispatch)    │   │  (push + pull_request)      │
└───────────┬─────────────┘   ├─────────────────────────────┤
            │                 │  • Unit Tests               │
            │                 │  • Manifest Validation      │
            │                 │  • Deployment Plan Preview  │
            ▼                 └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│               _siteops-deploy.yaml (reusable)               │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops                                          │
│  2. Validate inputs (path traversal protection)             │
│  3. Generate sites.local/ from SITE_OVERRIDES secret        │
│  4. Validate and show deployment plan                       │
│  5. Azure Login (OIDC)                                      │
│  6. Start OIDC token refresh service (background)           │
│  7. Run: siteops deploy                                     │
│  8. Stop OIDC refresh and Azure Logout                      │
└─────────────────────────────────────────────────────────────┘
```

See [ADO architecture](#ado-architecture) for the Azure DevOps equivalent.

## Security

| Feature | GitHub Actions | Azure DevOps |
|---------|---------------|--------------|
| **Authentication** | OIDC (no stored credentials, short-lived tokens) | WIF service connection (token managed by `AzureCLI@2`) |
| **Environment Protection** | Required approvals for staging/prod | Approval checks on ADO environments |
| **Input Validation** | Prevents path traversal and injection attacks | Same validation logic in pipeline scripts |
| **Site Name Sanitization** | `SITE_OVERRIDES` keys validated against `^[a-zA-Z0-9_-]+$` | Same |
| **Override Value Masking** | `::add-mask::` per value | `##vso[task.setvariable issecret=true]` per value |
| **Concurrency Control** | `concurrency` groups (one deploy or integration-test per env, shared `azure-${env}` group) | Exclusive lock on ADO environments |
| **Least Privilege** | `permissions:` block scopes GitHub token | Service connection authorization scopes access |
| **Token Refresh** | Background OIDC refresh every 4 min | Not needed (`AzureCLI@2` manages lifecycle) |
| **Credential Isolation** | `persist-credentials: false` on checkout | `persistCredentials: false` on checkout |
| **Audit Trail** | All runs logged with triggering user | Same |

### Security model

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: CI/CD Platform                                    │
│                                                             │
│  GitHub Actions:                                            │
│  • Environment protection rules (approvals, branch gates)   │
│  • Concurrency prevents parallel deploys or integration-tests│
│    to the same env                                          │
│  • Minimal permissions (contents: read, id-token: write)    │
│                                                             │
│  Azure DevOps:                                              │
│  • Environment approval checks and exclusive locks          │
│  • Service connection authorization (admin-controlled)      │
│  • Variable groups with role-based access                   │
│                                                             │
│  Both:                                                      │
│  • Input validation blocks path traversal                   │
│  • SITE_OVERRIDES values masked in logs                     │
│  • Credential persistence disabled on checkout              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: Identity Federation                               │
│  • No stored Azure credentials on either platform           │
│  • GHA: OIDC token + federated credential subject matching  │
│  • ADO: WIF service connection (automatic token exchange)   │
│  • Token scoped to specific environment/context             │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Azure RBAC                                        │
│  • Service principal has scoped permissions                 │
│  • Can further restrict by subscription/resource group      │
│  • Same identity and roles for both platforms               │
└─────────────────────────────────────────────────────────────┘
```

## Extending

### Adding new manifests

To add a new manifest to the deployment workflows:

1. Create your manifest at the appropriate location in the workspace:
   - Standalone day-2 manifests under `workspaces/<workspace>/manifests/`
   - Self-contained workload bundles under `workspaces/<workspace>/samples/<name>/`
   - Composed (include-based) manifests under `workspaces/<workspace>/scenarios/`
2. Update the workflow/pipeline to add the path to the dropdown:

**GitHub Actions** (`.github/workflows/deploy.yaml`):
```yaml
manifest:
    description: "Manifest to deploy (path relative to the workspace root)"
    required: true
    type: choice
    options:
        - manifests/aio-install.yaml
        - manifests/aio-upgrade.yaml
        - manifests/secretsync.yaml
        - samples/opc-ua-solution/manifest.yaml
        - scenarios/aio-with-opc-ua.yaml
        - manifests/my-new-manifest.yaml  # Add here (full path)
```

**Azure DevOps** (`.pipelines/deploy.yaml`):
```yaml
- name: manifest
  displayName: Manifest (path relative to the workspace root)
  type: string
  default: manifests/aio-install.yaml
  values:
    - manifests/aio-install.yaml
    - manifests/aio-upgrade.yaml
    - manifests/secretsync.yaml
    - samples/opc-ua-solution/manifest.yaml
    - scenarios/aio-with-opc-ua.yaml
    - manifests/my-new-manifest.yaml  # Add here (full path)
```

### Adding new workspaces

To add a new workspace (e.g., `iot-hub`):

1. Create `workspaces/iot-hub/` with `manifests/`, `sites/`, `parameters/`, `templates/`
2. Update the workflow/pipeline to add it to the dropdown:

**GitHub Actions** (`.github/workflows/deploy.yaml`):
```yaml
workspace:
    description: "Workspace to deploy"
    required: true
    type: choice
    options:
        - iot-operations
        - iot-hub  # Add here
```

**Azure DevOps** (`.pipelines/deploy.yaml`):
```yaml
- name: workspace
  displayName: Workspace
  type: string
  default: iot-operations
  values: [iot-operations, iot-hub]  # Add here
```

### Custom deployment workflow

**GitHub Actions**: create a new workflow that calls the reusable workflow:

```yaml
name: Deploy My Service

on:
  push:
    branches: [main]
    paths: ['services/my-service/**']

jobs:
  deploy:
    uses: ./.github/workflows/_siteops-deploy.yaml
    with:
      manifest: manifests/my-service.yaml
      environment: dev
    secrets: inherit
```

**Azure DevOps**: create a new pipeline that uses the stage template:

```yaml
trigger:
  branches:
    include: [main]
  paths:
    include: [services/my-service/**]

pr: none

variables:
  - group: siteops-secrets

stages:
  - template: templates/siteops-deploy.yaml
    parameters:
      serviceConnection: azure-siteops
      manifest: manifests/my-service.yaml
      environment: dev
```

### Setup templates

**GitHub Actions**: the `setup-siteops` composite action:

| Input | Default | Description |
|-------|---------|-------------|
| `python-version` | `3.11` | Python version to install |
| `install-dev` | `false` | Include dev dependencies (pytest, pytest-cov) |
| `siteops-source` | (empty) | pip install spec for siteops. Empty = local editable install. Set to `git+https://github.com/.../digital-ops-scale-kit@<ref>` to pin a release. |

```yaml
- uses: ./.github/actions/setup-siteops
  with:
    install-dev: "true"
```

**Azure DevOps**: the `setup-siteops.yaml` steps template:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pythonVersion` | `'3.11'` | Python version to install |
| `installDev` | `false` | Include dev dependencies (pytest, pytest-cov) |
| `siteopsSource` | (empty) | pip install spec for siteops. Empty = local editable install. |
| `enableCache` | `true` | Cache the pip wheel directory across pipeline runs. Disable in deployment jobs (no cache scope available). |

```yaml
- template: templates/setup-siteops.yaml
  parameters:
    installDev: true
```

---

## Azure DevOps

### Pipelines

| Pipeline file | Purpose | Trigger |
|---------------|---------|---------|
| `.pipelines/ci.yaml` | Bicep validation, unit tests, manifest validation | Push to main, PRs |
| `.pipelines/deploy.yaml` | Manual deploy with environment selection | Manual only |
| `.pipelines/templates/siteops-deploy.yaml` | Stage template: deployment logic | Called by deploy.yaml |
| `.pipelines/templates/setup-siteops.yaml` | Steps template: install Python + siteops | Called by all pipelines |

### ADO project setup

#### 1. Create service connection (Workload Identity Federation)

In ADO → **Project settings → Service connections → New → Azure Resource Manager → Workload Identity federation**.

- **Automatic**: creates the Entra app registration and federated credential for you
- **Manual**: reuse the existing app registration from GitHub Actions OIDC setup (same `APP_ID`)

The service connection name is referenced in the deploy pipeline. Default: `azure-siteops`.

> **Reusing the GitHub Actions app registration:** If you already configured OIDC for GitHub Actions (section above), you can reuse that same app registration. Create a new federated credential for ADO. The issuer and subject claims are different from GitHub's. The Azure roles are shared.

#### 2. Create variable group

In ADO → **Pipelines → Library → + Variable group**:

| Variable group | Variable | Type | Description |
|----------------|----------|------|-------------|
| `siteops-secrets` | `SITE_OVERRIDES` | Secret | JSON object, same format as the GitHub secret (see [SITE_OVERRIDES](#site_overrides)) |

#### 3. Create environments

In ADO → **Pipelines → Environments** → create `dev`, `staging`, `prod`.

| Environment | Approvals | Exclusive lock |
|-------------|-----------|----------------|
| `dev` | None | Yes |
| `staging` | 1 approver | Yes |
| `prod` | 2 approvers | Yes |

Exclusive lock ensures one deployment per environment at a time. The GitHub Actions equivalent is the shared `azure-${env}` `concurrency` group on `deploy.yaml` and `integration-test.yaml`, so a deploy and an integration test against the same environment serialize on both platforms.

To configure: **Environments → (select env) → Approvals and checks → + → Exclusive lock** and **+ → Approvals**.

#### 4. Create pipelines

In ADO → **Pipelines → New pipeline** → **Azure Repos Git** (or GitHub, if the repo is hosted there) → select repository → **Existing Azure Pipelines YAML file**:

- `.pipelines/ci.yaml` → name it **"CI"**
- `.pipelines/deploy.yaml` → name it **"Deploy Infrastructure"**

#### 5. Assign Azure roles

Same as GitHub Actions, see [Assign Azure roles](#3-assign-azure-roles). The service connection's managed identity needs the same Contributor (or Owner with conditions) role assignment.

### Running ADO deployments

#### Deploy via ADO UI

1. Go to **Pipelines** → select **"Deploy Infrastructure"**
2. Click **"Run pipeline"**
3. Select branch/tag from the branch picker
4. Fill in parameters:
   - **Workspace**: `iot-operations`
   - **Manifest**: path relative to the workspace root (e.g., `manifests/aio-install.yaml`, `samples/opc-ua-solution/manifest.yaml`, `scenarios/aio-with-opc-ua.yaml`)
   - **Target environment**: `dev`, `staging`, or `prod`
   - **Additional site selector**: e.g., `country=US,name=seattle-dev` (optional)
   - **Dry run**: Check to preview without deploying
5. Click **"Run"**

#### Deploy via Azure CLI

```bash
az pipelines run \
  --name "Deploy Infrastructure" \
  --parameters workspace=iot-operations manifest=manifests/aio-install.yaml environment=dev

# With additional options
az pipelines run \
  --name "Deploy Infrastructure" \
  --parameters workspace=iot-operations manifest=manifests/aio-install.yaml environment=dev \
               selector="country=US" dryRun=true
```

### ADO architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Trigger Sources                          │
├──────────────┬──────────────┬──────────────────────────────┤
│   ADO UI     │  az CLI      │  Push / PR                   │
└──────┬───────┴──────┬───────┴──────────────┬───────────────┘
       │              │                      │
       ▼              ▼                      ▼
┌──────────────────────────┐   ┌─────────────────────────────┐
│    deploy.yaml           │   │         ci.yaml             │
│    (manual trigger)      │   │    (push + pull_request)    │
└───────────┬──────────────┘   ├─────────────────────────────┤
            │                  │  • Unit Tests               │
            │                  │  • Manifest Validation      │
            │                  │  • Deployment Plan Preview  │
            ▼                  └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│            siteops-deploy.yaml (stage template)             │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops (steps template)                         │
│  2. Validate inputs (path traversal protection)             │
│  3. Generate sites.local/ from SITE_OVERRIDES               │
│  4. Validate and show deployment plan                       │
│  5. AzureCLI@2: siteops deploy (auth scoped to this step)  │
└─────────────────────────────────────────────────────────────┘
```

**Key difference from GitHub Actions:** `AzureCLI@2` handles authentication, token lifecycle, and cleanup in a single task. No separate login, token refresh, or logout steps needed.

### Per-environment migration

The deploy pipeline uses object parameter lookup tables for service connections and variable groups. To split per-environment (separate identities and secrets):

```yaml
# .pipelines/deploy.yaml: edit these defaults:
- name: serviceConnections
  type: object
  default:
    dev: azure-siteops-dev         # ← separate service connection
    staging: azure-siteops-staging
    prod: azure-siteops-prod

- name: secretGroups
  type: object
  default:
    dev: siteops-secrets-dev       # ← separate variable group
    staging: siteops-secrets-staging
    prod: siteops-secrets-prod
```

No structural pipeline changes needed. Just edit defaults and create the corresponding ADO resources.

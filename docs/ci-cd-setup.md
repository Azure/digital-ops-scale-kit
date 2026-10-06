# CI/CD Setup

This guide covers CI/CD configuration for automated testing and deployments.
Site Ops runs anywhere Python and Azure CLI are available. Deployments that
contain kubectl operations also require `kubectl`. This project provides a
primary GitHub Actions implementation and an Azure Pipelines reference.

| Platform | Location | Status |
|----------|----------|--------|
| [GitHub Actions](#github-actions) | `.github/workflows/` | Primary |
| [Azure DevOps](#azure-devops) | `.pipelines/` | Reference implementation |

Azure Pipelines is a consumer surface: install an identified Site Ops engine,
select deployment content and configuration, then plan, deploy and check
outcomes. Its reusable templates can be referenced from another repository.
Building, signing and publishing Site Ops or Scale Kit release assets belongs
to the GitHub Actions release workflows, not these ADO deployment pipelines.

## Prerequisites

1. Azure subscription with resources to deploy
2. GitHub repository with Actions enabled **or** Azure DevOps project with Pipelines enabled
3. Azure AD application for OIDC / Workload Identity Federation

## GitHub Actions

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yaml` | Push, pull request, manual | Lint Python, run unit tests, validate Bicep templates, and validate manifests |
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

## Site overrides

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
- `.github/workflows/**` and `.github/actions/**` on GitHub Actions, or `.pipelines/**` on Azure Pipelines

Can also be triggered manually from **Actions → CI → Run workflow** (GHA) or **Pipelines → CI → Run pipeline** (ADO).

### Deploy via GitHub UI

1. Go to **Actions** tab
2. Select **"Deploy Infrastructure"**
3. Click **"Run workflow"**
4. Fill in options:
   - **Git ref**: Branch, tag, or commit (optional)
   - **Workspace**: Workspace name (default: `iot-operations`)
   - **Manifest**: Path to manifest, relative to the workspace root (default: `manifests/aio-install/manifest.yaml`)
   - **Environment**: `dev`, `staging`, or `prod`
   - **Selector**: Additional site filter (optional, e.g., `region=eastus`)
   - **Dry run**: Have the wrapper prepare an executable plan without invoking `deploy`
5. Click **"Run workflow"**

### Deploy via GitHub CLI

```bash
gh workflow run deploy.yaml \
  -f workspace=iot-operations \
  -f manifest=manifests/aio-install/manifest.yaml \
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
      "manifest": "manifests/aio-install/manifest.yaml",
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
| 1 | `manifests/aio-install/manifest.yaml` | `staging` | chicago-staging | Base AIO platform only |
| 2 | `manifests/aio-install/manifest.yaml` | `dev` | munich-dev, seattle-dev | Parallel deployment |
| 3 | `manifests/aio-install/manifest.yaml` | `prod` | munich-prod, seattle-prod | Parallel deployment |
| 4 | `samples/opc-ua-solution/manifest.yaml` | `staging` | chicago-staging | OPC UA sample on existing AIO |
| 5 | `samples/aio-with-opc-ua/manifest.yaml` | any | any | Composed install + sample in one shot |
| 6 | `manifests/aio-upgrade/manifest.yaml` | any | any AIO-installed site | Upgrade an existing AIO instance to the site's current `aioRelease` (bump the site's `aioRelease` first, then dispatch) |

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
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install/manifest.yaml" -f environment="staging"

# Step 2: Deploy AIO to dev (parallel across sites)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install/manifest.yaml" -f environment="dev"

# Step 3: Deploy AIO to prod (parallel across sites)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="manifests/aio-install/manifest.yaml" -f environment="prod"

# Step 4: Add the OPC UA sample on top of the staging install
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="samples/opc-ua-solution/manifest.yaml" -f environment="staging"

# Step 5: Composed install + sample in one shot (alternative to steps 1+4)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="samples/aio-with-opc-ua/manifest.yaml" -f environment="staging"
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
            │                 │  • Executable-plan Tests    │
            ▼                 └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│               _siteops-deploy.yaml (reusable)               │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops                                          │
│  2. Validate inputs (path traversal protection)             │
│  3. Generate sites.local/ from SITE_OVERRIDES secret        │
│  4. Azure Login (OIDC)                                      │
│  5. Start OIDC token refresh service (background)           │
│  6. Prepare and publish the executable plan                 │
│  7. Run siteops deploy unless dry run                       │
│  8. Stop OIDC refresh and Azure Logout                      │
└─────────────────────────────────────────────────────────────┘
```

Executable planning runs after login because Bicep compiler acquisition and
module restore may use the network and may need the workflow identity. Planning
does not submit Azure deployments or contact Kubernetes clusters. The workflow
sets private file permissions, publishes only a supported executable
`publishable` JSON plan, and removes runner-local stdout and stderr files when
the step finishes. The workflow's **Dry run** input stops here and publishes
no deployment result. It does not call the removed CLI `deploy --dry-run`.

The deploy step explicitly requests `--yes`, `--output json` and
`--projection publishable`: noninteractive or JSON deployment without
`--yes` is a usage
error before content or Azure access. The workflow's environment approval
and Azure OIDC identity remain separate from Site Ops consumer source
approval. `--yes` authorizes execution without bypassing source trust or
target prerequisites, and does not print the private plan to stderr.
The deploy command prepares its own fresh executable plan rather than
executing the separately published preview as a saved plan.
The step captures stdout separately from stderr. It validates the `DeploymentRun` envelope before
publishing anything, including that the reported exit code matches the process
exit code and that exit code `130` appears only with `summary.interrupted`. An
unsupported document is reported as unavailable rather than published, and the
process exit code is always preserved. The published summary carries a short
status line and the allowlisted JSON document. Run stdout and stderr files are
removed when the step finishes. See [run-output.md](run-output.md) for the
result contract.

Reporting and file cleanup preserve a failed or interrupted Site Ops exit
code. If the operation succeeded but its summary or private file cleanup
fails, the task fails too. A warning on stderr alone does not determine the
deployment result.

See [ADO architecture](#ado-architecture) for the Azure DevOps equivalent.

## Security

| Feature | GitHub Actions | Azure DevOps |
|---------|---------------|--------------|
| **Authentication** | OIDC (no stored credentials, short-lived tokens) | WIF service connection (token managed by `AzureCLI@2`) |
| **Environment Protection** | Required approvals for staging/prod | Approval checks on ADO environments |
| **Input Validation** | Rejects traversal markers and unsupported selector characters | Same validation logic in pipeline scripts |
| **Site Name Sanitization** | `SITE_OVERRIDES` keys validated against `^[a-zA-Z0-9_-]+$` | Same |
| **Override Value Masking** | Encoded `::add-mask::` commands | Encoded `##vso[task.setsecret]` commands |
| **Concurrency Control** | `concurrency` groups (one deploy or integration-test per env, shared `azure-${env}` group) | Exclusive lock on ADO environments |
| **Least Privilege** | `permissions:` block scopes GitHub token | Service connection authorization scopes access |
| **Token Refresh** | Background OIDC refresh every 4 min | Opt in with `keepAzSessionActive` for WIF connections |
| **Credential Isolation** | `persist-credentials: false` on checkout | `persistCredentials: false` on checkout |
| **Audit Trail** | All runs logged with triggering user | Same |
| **Output Redaction** | `SITEOPS_REDACT_OUTPUT=1`, and on by default from `GITHUB_ACTIONS` | Same, and on by default from `TF_BUILD` |

### Output redaction

Plan and run summaries publish only the supported `publishable` JSON
projection. Deployment diagnostics in that document use fixed categories
and summaries. The workflows capture progress and diagnostic stderr
separately, then remove it without publishing it. Integration result
assertions likewise use fixed outcome reasons when redaction is enabled.

Redaction follows the destination. Local plain output retains detailed
reasons, while redacted plain output renders the allowed run fields.
`GITHUB_ACTIONS` and `TF_BUILD` enable redaction automatically, and the
shipped workflows also set `SITEOPS_REDACT_OUTPUT=1` explicitly.

Site override generation follows the same setting. CI reports generated
and preserved overlay counts, while private local output includes Site
names and detailed validation errors.

Diagnostic logging also scrubs recognized identifiers and credential
patterns. That heuristic is separate from the publication allowlist and
does not make arbitrary stderr suitable for an artifact.

For a private local diagnostic session, set `SITEOPS_REDACT_OUTPUT=0` and
keep the resulting terminal output or captured stderr operator-local.
See [run-output.md](run-output.md) for projections and recovery guidance.

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
   - Core operations at `workspaces/<workspace>/manifests/<name>/manifest.yaml`
   - Worked examples at `workspaces/<workspace>/samples/<name>/manifest.yaml`
   - Keep the operator guide and optional `entry.yaml` beside each public entry.
2. Update the workflow/pipeline to add the path to the dropdown:

**GitHub Actions** (`.github/workflows/deploy.yaml`): add the path to the `manifest` input's `options:` list.
```yaml
manifest:
    description: "Manifest to deploy (path relative to the workspace root)"
    required: true
    type: choice
    options:
        # ... existing entries ...
        - manifests/my-new-manifest/manifest.yaml
```

**Azure DevOps** (`.pipelines/deploy.yaml`): add the same path to the `manifest` parameter's `values:` list.
```yaml
- name: manifest
  displayName: Manifest (path relative to the workspace root)
  type: string
  default: manifests/aio-install/manifest.yaml
  values:
    # ... existing entries ...
    - manifests/my-new-manifest/manifest.yaml
```

Keep the two lists in step. A manifest offered on one platform and not the other is deployable only from that platform. `tests/workspace/test_deploy_registration.py` derives the expected set from the workspace and fails when either list drifts, so the current entries are whatever those files hold rather than what this page lists.

Regenerate the [content index](remote-content.md#publish-descriptions-from-a-workspace)
after updating discovery inputs. Descriptive metadata does not replace either
platform's environment, credential or approval policy.

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
  - name: SITE_OVERRIDES
    value: ''
  - group: siteops-secrets

pool:
  vmImage: ubuntu-24.04

stages:
  - template: templates/siteops-deploy.yaml
    parameters:
      serviceConnection: azure-siteops
      keepAzSessionActive: true  # WIF connections only.
      manifest: manifests/my-service.yaml
      environment: dev
      release: '<reviewed-release>'
      sourceCommit: '<full-release-source-commit>'
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
| `installDev` | `false` | Explicit editable checkout installation with development dependencies. Used by contributor CI. |
| `siteopsSource` | (empty) | Explicit pip installation spec, such as an exact tagged VCS source or wheel. Not the verified bundle route. |
| `release`, `sourceCommit` | (empty) | An explicit engine/content release and full source commit. Otherwise use the exact tagged template repository selection. |
| `repository` | (empty) | Publisher for an explicit release selection. Defaults to `Azure/digital-ops-scale-kit`. Inferred selection uses the template repository. |
| `templateRepository` | `self` | Automation repository alias. The consumer stage templates supply its runtime ref/version and checkout. |
| `enableCache` | `true` | Pip cache for explicit source/development installation only. Disabled in deployment jobs. |
| `sourceDirectory` | `$(Build.SourcesDirectory)` | Reviewed automation checkout for the shared bootstrap, or the explicitly selected development source directory. |

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
| `.pipelines/ci.yaml` | Contributor checks for engine, workspace and pipeline changes. Does not publish releases. | Push to main, PRs |
| `.pipelines/deploy.yaml` | Manual deploy with environment selection | Manual only |
| `.pipelines/integration-test.yaml` | Integration suite against an environment already deployed by `deploy.yaml` | Manual only |
| `.pipelines/templates/siteops-deploy.yaml` | Stage template: deployment logic | Called by deploy.yaml |
| `.pipelines/templates/siteops-validate.yaml` | Stage template: structural consumer validation without Azure authentication | Referenced by consumer pipelines |
| `.pipelines/templates/setup-siteops.yaml` | Steps template: install Python + siteops | Called by all pipelines |
| `.pipelines/validate-pipelines.yaml` | Maintainer qualification: full template previews, hosted consumer smoke and one combined report | Manual only |
| `.pipelines/templates/consumer-smoke.yaml` | Maintainer stages using the ordinary consumer-validation template with fixture content | Called by maintainer qualification |

### Reference the deployment template from another repository

Keep deployment configuration in your repository and pin the Scale Kit
repository resource to the reviewed template revision. The template's
`templateRepository` parameter names that resource alias. It checks out
your repository into `s/siteops-inputs` and the automation into
`s/siteops-automation`, relative to the agent's pipeline workspace.
`workspace` remains relative to your repository, not the tooling checkout.

Pin one reviewed content release below. Both stages install its exact
selected engine through the verified bootstrap, without a second engine
version pin. The release must include these consumer templates and the
signed engine reference. Configure a GitHub
repository service connection named `scalekit-github`, an Azure WIF service
connection named `azure-siteops`, and the `dev` approval environment.
The validation stage itself needs no Azure service connection.

```yaml
trigger: none
pr: none

resources:
  repositories:
    - repository: scalekit
      type: github
      name: Azure/digital-ops-scale-kit
      endpoint: scalekit-github
      ref: refs/tags/<reviewed-release>

pool:
  vmImage: ubuntu-24.04

variables:
  - name: SITE_OVERRIDES
    value: ''
  # Add your approved variable group here when overrides are needed.

stages:
  - template: /.pipelines/templates/siteops-validate.yaml@scalekit
    parameters:
      templateRepository: scalekit
      workspace: deployment
      manifest: manifests/install/manifest.yaml

  - template: /.pipelines/templates/siteops-deploy.yaml@scalekit
    parameters:
      templateRepository: scalekit
      workspace: deployment
      manifest: manifests/install/manifest.yaml
      selector: environment=dev
      environment: dev
      serviceConnection: azure-siteops
      keepAzSessionActive: true
      dryRun: true
```

Your `deployment` directory contains the ordinary Site Ops workspace,
including manifests and Sites. No content package or project conversion is
required for this caller-owned workspace. Structural validation checks
syntax and static references without preparing an executable plan or
using Azure credentials. Set `dryRun: false` only when deployment is
intended. An Azure-authenticated plan can restore compiler modules, but
does not submit deployment writes.

The pinned automation checkout is reviewed executable code. Its tag and
resolved commit select the content reference. The bootstrap then verifies
the signed engine reference and the engine's own bundle provenance.
ADO only downloads and verifies these assets. It needs no signing key or
release-publication permission. Each job uses fresh installation state
under the agent's temporary directory, separate from any preinstalled
Site Ops tool. Use Ubuntu 24.04 agents for this template route.

The deployment identity can remain scoped to existing target resource
groups. Installation does not log in to Azure, grant permissions, create
groups or approve content sources. Manifest operations determine any
additional scope needed.

For one standalone Site, pass `siteFile: operator/site.yaml` to either
stage instead of `selector`. Its path is relative to the caller checkout.
Supplying both is an error. A manifest remains required. For a fleet, keep
the existing inventory and selector semantics.

With `templateRepository: self`, a tagged checkout can select its release.
A branch checkout or copied template must instead provide `release` and
`sourceCommit`, or explicitly select `siteopsSource`. The release pair
uses the same verified bootstrap. `siteopsSource` remains an ordinary pip
installation through your approved feed, including exact tagged VCS sources.
Do not combine release, source and `installDev` selections.

Older releases without the signed reference require their explicit engine
selection. There is no lookup of the newest compatible engine and no
silent fallback to editable installation. Referenced templates are the
recommended route. If you copy templates, retain their matching shared
templates and helper scripts from the same revision.

### ADO project setup

#### 1. Create service connection (Workload Identity Federation)

In ADO → **Project settings → Service connections → New → Azure Resource Manager → Workload Identity federation**.

- **Automatic**: creates the Entra app registration and federated credential for you
- **Manual**: reuse the existing app registration from GitHub Actions OIDC setup (same `APP_ID`)

The service connection name is referenced in the deploy pipeline. Default: `azure-siteops`.

> **Reusing the GitHub Actions app registration:** If you already configured OIDC for GitHub Actions (section above), you can reuse that same app registration. Create a new federated credential for ADO. The issuer and subject claims are different from GitHub's. The Azure roles are shared.

For a WIF service connection, the deploy stage template, deployment pipeline
and integration pipeline expose `keepAzSessionActive`. Set it to `true` to
request session refresh from `AzureCLI@2`. The examples here enable it for
their WIF connections. The default remains `false` for existing callers.
Leave it off for service connections using a client secret, certificate or
managed-identity authentication: the task rejects refresh for those schemes.

The Azure CLI task currently labels this option **experimental**. It signs
in periodically during the task using a fresh federated assertion and stops
refreshing when the task finishes. A successful initial login alone does
not guarantee that a long deployment can obtain later tokens. An expired
assertion may produce `AADSTS700024`. Use a deployed task version that supports
the option and qualify it with the selected service connection. See the
[Azure CLI task implementation](https://github.com/microsoft/azure-pipelines-tasks/tree/master/Tasks/AzureCLIV2).

Session refresh does not extend the pipeline's job timeout, change Azure
roles or replace environment approvals. The Site Ops templates keep the
task's isolated Azure configuration and do not expose its service-principal
credentials to the inline script. They add no separate login or refresh loop.

#### 2. Create variable group

In ADO → **Pipelines → Library → + Variable group**:

| Variable group | Variable | Type | Description |
|----------------|----------|------|-------------|
| `siteops-secrets` | `SITE_OVERRIDES` | Secret | JSON object, same format as the GitHub secret (see [site overrides](#site-overrides)) |

The top-level pipelines default `SITE_OVERRIDES` to empty before loading
the group, so a missing optional value leaves committed Sites in use.
Reusable-template callers should likewise define an empty default or
provide the secret through their own variable group. Mask registration
encodes percent signs and line breaks before publishing logging commands.
It is a supplementary protection, not permission to print private values.

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
   - **Manifest**: path relative to the workspace root (e.g., `manifests/aio-install/manifest.yaml`, `samples/opc-ua-solution/manifest.yaml`, `samples/aio-with-opc-ua/manifest.yaml`)
   - **Target environment**: `dev`, `staging`, or `prod`
   - **Additional site selector**: e.g., `country=US,name=seattle-dev` (optional)
   - **Dry run**: Prepare the executable plan without deploying
   - **Refresh Azure WIF session**: Enable for the WIF service connection after reviewing the task's experimental setting above
   - **Engine or content release** and **Full source commit**: Required together when running an untagged checkout, unless an explicit pip engine source is selected
5. Click **"Run"**

#### Deploy via Azure CLI

```bash
az pipelines run \
  --name "Deploy Infrastructure" \
  --parameters workspace=iot-operations manifest=manifests/aio-install/manifest.yaml environment=dev \
               release="<reviewed-release>" sourceCommit="<full-release-source-commit>" \
               keepAzSessionActive=true

# With additional options
az pipelines run \
  --name "Deploy Infrastructure" \
  --parameters workspace=iot-operations manifest=manifests/aio-install/manifest.yaml environment=dev \
               release="<reviewed-release>" sourceCommit="<full-release-source-commit>" \
               selector="country=US" dryRun=true keepAzSessionActive=true
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
            │                  │  • Executable-plan Tests    │
            ▼                  └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│            siteops-deploy.yaml (stage template)             │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops (steps template)                         │
│  2. Validate inputs (path traversal protection)             │
│  3. Generate sites.local/ from SITE_OVERRIDES               │
│  4. AzureCLI@2: prepare plan, deploy unless dry run         │
└─────────────────────────────────────────────────────────────┘
```

Dry run stops after the publishable plan. Otherwise, `AzureCLI@2` handles
authentication, plan-time compiler or module access, deployment and session
cleanup in one task. WIF session refresh is the explicit `keepAzSessionActive`
choice described above, not a guarantee from the initial login. The deployment
result is validated and uploaded as a second summary using the same envelope
rules as the GitHub workflow.

Setup and override steps stop on the first failed command. Empty optional
override values remain valid. Unit and integration result publication
requires a results file and reports failed tests as task failures.

### Automated pipeline validation

Use complementary checks for different parts of the pipeline contract:

| Check | What it establishes |
|---|---|
| Repository regression tests | Actual YAML script bodies preserve command failures, interruption, output validation, summary errors and cleanup outcomes. Controlled tools keep these checks independent of Azure. |
| Azure DevOps YAML preview | The service parses templates and parameters for the selected repository revision. This checks Azure expression expansion rather than approximating it with a local YAML parser. |
| Hosted CI and deployment qualification | Real tasks, agent images, cache and result publication work in the selected project. Authenticated planning and deployment require separately scoped service connections and targets. |

Run the local pipeline regressions with:

```text
python -B -m pytest tests/test_ado_preview.py tests/test_ado_consumer_smoke.py tests/test_ado_qualification_report.py
python -B -m pytest tests/test_ado_pipelines.py tests/workspace/test_deploy_registration.py tests/test_site_overrides_script.py
```

These tests run in normal CI, including GitHub PRs that change only
`.pipelines/`. Azure Pipelines and GitHub Actions provision the same pinned
Linux installation fixtures before their unit suites. Required native
inputs fail explicitly when unavailable.

For service validation, the
[Preview API](https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/preview/preview?view=azure-devops-rest-7.1)
accepts `previewRun: true` and returns `finalYaml` without creating a run.
Bind `resources.repositories.self.refName` and `version` to the chosen
branch and commit. Exercise the default parameters, deployment versus dry
run, each environment mapping, sample selectors and setup template options.
`yamlOverride` replaces the entry document, so bind referenced templates
to the same candidate too. Treat a rejected request or absent final YAML
as a failed check. Keep expanded YAML private.

Use a dedicated validation project or explicitly authorized pipeline
definitions. Preview is a separate operation from queueing a build and
does not establish script behavior, runtime authentication, environment
approval or deployment success. A hosted CI run needs no Azure deployment
identity. A deployment dry run still authenticates and can perform planning
reads or compiler acquisition. Qualify that boundary and actual deployment
only with the corresponding approval and scoped targets.

### Run maintainer ADO qualification

This harness is for Scale Kit maintainers qualifying customer-facing
templates. Customers use the consumer pipelines and reusable templates
documented above, with their own content and deployment identity.

1. Register `.pipelines/validate-pipelines.yaml` once using **Existing Azure
   Pipelines YAML file** and the reviewed source branch. Save the definition
   before running it. An existing definition pointing at this path can be
   reused.
2. Configure the agent's approved Python package feed and the real
   `serviceConnections` and `secretGroups` name mappings to preview.
   A `PIP_INDEX_URL` pipeline variable can select the approved feed when
   required by your organization. On this qualification pipeline only,
   allow **Edit build pipeline** for the project build service identity,
   `<project> Build Service (<organization>)`. Template previews submit
   entry YAML through `yamlOverride`, which requires that permission.
   Keep the identity's other permissions inherited. The build identity also
   needs read access to its definition and source repository, plus the
   resource authorization required for template expansion. This
   qualification does not need Azure resource roles.
3. Queue one approved run bound to the selected branch and full commit,
   then inspect its qualification summary and `ado-qualification` artifact.

The preview stage discovers its own definition through `System.DefinitionId`
and verifies the repository and qualification entry path. CI, deployment
and integration definitions do not need to be registered separately.
The controller requires a clean checkout at the selected commit and reads
the entry YAML from committed Git blobs. It submits those documents through
`yamlOverride`, with that same branch and commit bound to every template
expansion. All entry points share the qualification entry's `.pipelines`
directory, preserving relative template resolution.

The full preview matrix covers setup options, environment mappings,
planning versus deployment, selectors, Site files, resource-set samples
and every integration phase. Both default and enabled WIF session refresh
are expanded. These requests use only the dedicated `/preview` endpoint
and never execute the deployment or integration jobs.

The consumer stages run independently of the preview stage, so a preview
failure does not suppress runtime evidence. Available agent capacity
determines whether the lanes execute concurrently. The Site-file case still
follows a successful selector case.

Both consumer stages use the ordinary setup and validation templates.
Each installs the selected checkout with a noneditable pip installation
and runs compile-free `validate` against real fixtures under
`tests/fixtures/ado-consumer`. One case selects a workspace Site by label.
The other uses a standalone Site file outside the workspace inventory.
The fixture includes its referenced ARM JSON and synthetic target values.
These stages have no Azure task, variable group or mapped preview token.

The final stage waits for both lanes and reads native job outcomes without
polling or queueing other pipelines. All required jobs must return
`Succeeded`, and the complete preview receipt must match the selected
source. Failed, canceled, skipped,
missing or partially successful jobs cannot produce a passing report.
The report includes the selected source-install identity and the preview
case inventory with input and expansion digests. Reports are published
only after the reporter creates its own safe output, including failed
qualification results. A canceled run can prevent the reporting stage from
starting, so a missing report is not qualification evidence.

The recorded source commit identifies this pipeline's checkout. Retain a
reviewed source-equivalence mapping for a mirror or snapshot, and keep the
original publisher identity for signed-release verification.

Run only reviewed pipeline source with the explicitly mapped
`System.AccessToken`. The harness has no automatic PR trigger, interactive
login or pipeline-creation behavior. Expanded YAML, resource names and raw
service diagnostics are not published. HTTP failures retain the status code
and an allowlisted service exception category. When the service reports an
unlisted exception type whose key has the form of a .NET class name, the
diagnostic also names that type. Messages and other unknown or unreadable
details remain undisclosed. These categories describe the server response, not a
proven cause, and do not trigger retries or an execution-endpoint fallback.

A complete passing run qualifies template expansion and the explicit
source-install consumer checks for that candidate. Verified-release
installation, separate caller/tooling repository checkouts, executable
planning, deployment and WIF renewal remain separately scoped qualification.
Release production remains in GitHub Actions.

A separate customer-repository rehearsal should use the reusable-template
example above and confirm both checkouts, engine origin, workspace selection
and configured overrides. Local tests exercise those script/path boundaries.
Only a separately approved hosted run qualifies repository service
connections, agent tasks and Azure authentication. A preview success does
not establish those outcomes. For WIF refresh, retain the actual task version
and run long enough to exercise token renewal and a subsequent authorized
Azure operation. Confirm refresh failure is visible and the final task result
preserves any deployment failure. Local controls and template previews do
not establish that live token lifecycle.

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

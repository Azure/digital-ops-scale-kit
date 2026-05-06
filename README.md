# Digital Operations Scale Kit

**Fleet-scale Azure infrastructure deployment, simplified.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in fleet-scale deployment, reach out at <azureiotoperationslicensinghelp@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of sites with a single command. Per-site customization, parallel execution, and failure isolation built in.

```bash
# Deploy to all production sites
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l "environment=prod"
```

---

## What's in this repository

| Project | Description |
|---------|-------------|
| **Site Ops** | A reference implementation of a multi-site IaC orchestration tool. Template-agnostic. Works with any Bicep or ARM templates. |
| **IoT Operations Workspace** | A starter kit demonstrating Site Ops for deploying Azure IoT Operations at scale. |

---

## Why Site Ops?

ARM/Bicep deploys resources. Site Ops orchestrates deployments across your fleet.

> **Site Ops isn't replacing ARM/Bicep. It's the fleet management layer on top.**

| Challenge | Site Ops Solution |
|-----------|-------------------|
| Deploying to 50+ sites manually | One command deploys to all matching sites in parallel |
| Targeting specific sites or environments | Label-based selection filters your fleet (`-l environment=prod`, `-l country=US`) |
| Per-site configuration differences | Template variables (`{{ site.name }}`, `{{ site.labels.X }}`) customize each deployment |
| Multi-step dependencies | Output chaining passes resource IDs between steps automatically |
| Partial failures stopping everything | Failure isolation. One site's failure doesn't block others |
| Environment-specific values mixed with code | Site overlays separate per-environment config from committed files |

### Portability

Site Ops runs anywhere Python runs. No agents, no servers, no state to manage.

- **Run anywhere**: local machine, GitHub Actions, Azure DevOps, GitLab CI, or any CI/CD platform
- **Zero infrastructure**: no servers, agents, or state backends to provision
- **CI/CD agnostic**: included GitHub Actions workflows serve as reference implementations. Adapt to your preferred platform.

### Key capabilities

- **One-command fleet deployment**: deploy to all matching sites with a single command
- **Declarative site inventory**: define your fleet as code. Sites have labels, parameters, and inheritance.
- **Label-based site selection**: target any slice of your fleet with expressions like `-l environment=prod`, `-l country=US,city=Seattle`, or `-l name=munich-dev`
- **Subscription-scoped deployment**: deploy shared resources once per subscription, then deploy per-site resources with automatic output resolution
- **Output chaining**: reference outputs from previous steps, including cross-scope resolution from subscription to resource group deployments
- **Parallel execution**: deploy to multiple sites simultaneously with configurable concurrency
- **Failure isolation**: one site's failure doesn't block others. Subscription failures block only dependent sites.
- **Dry-run validation**: preview the full deployment plan without making Azure calls
- **Flexible step orchestration**: conditional execution, parameter auto-filtering, and mixed step types (Bicep and kubectl via Arc proxy) in a single manifest

### Cloud-first deployment

Site Ops deploys infrastructure through Azure Resource Manager, the native control plane for Azure resources. For Arc-enabled solutions like Azure IoT Operations, this aligns with Azure's cloud-first model: no in-cluster GitOps agents required.

---

## Quick start

### Option 1: Run locally

```bash
# Clone the repository
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit

# Install Site Ops
pip install -e .

# Authenticate with Azure
az login

# Run from the repo root. siteops auto-discovers the workspace
# under `./workspaces/`. Pass `-w <dir>` to point it elsewhere.
siteops -w workspaces/iot-operations sites

# Subsequent commands can drop -w when run from the repo root.
siteops validate manifests/aio-install.yaml
siteops deploy manifests/aio-install.yaml --dry-run
siteops deploy manifests/aio-install.yaml -l environment=dev
```

### Prerequisites

- Python 3.10+
- [Azure CLI](https://docs.microsoft.com/cli/azure/install-azure-cli) installed and authenticated
- For kubectl steps: `kubectl` in PATH

### Option 2: Use as a GitHub template

The local path above proves the tool works. To productionize as a CI/CD pipeline:

1. **Create your repository**:
   - Click **Use this template** → **Create a new repository**
   - Or fork the repository to your organization

2. **Configure GitHub secrets** for Azure OIDC authentication:

   | Secret | Description |
   |--------|-------------|
   | `AZURE_CLIENT_ID` | Azure AD application client ID |
   | `AZURE_TENANT_ID` | Azure AD tenant ID |
   | `AZURE_SUBSCRIPTION_ID` | Default subscription for login |

   See [docs/ci-cd-setup.md](docs/ci-cd-setup.md) for OIDC federation setup.

3. **Configure site overrides** (optional):

   The included sites use placeholder subscription IDs. To deploy to real Azure resources, create a `SITE_OVERRIDES` secret with your actual values. See [docs/ci-cd-setup.md](docs/ci-cd-setup.md#site-overrides) for the JSON shape.

4. **Configure environments** (optional):
   - Create `dev`, `staging`, `prod` environments in repository settings
   - Add approval policies for `staging` and `prod`

5. **Run a deployment**:
   - Go to **Actions** → **Deploy** → **Run workflow**
   - Select a manifest and environment
   - Monitor progress in the workflow logs

---

## Repository structure

```
digital-ops-scale-kit/
├── siteops/                      # Site Ops package
│   ├── cli.py                    # CLI entry point
│   ├── models.py                 # Site, Manifest, Step dataclasses
│   ├── orchestrator.py           # Core orchestration logic
│   └── executor.py               # Azure CLI and kubectl execution
├── tests/                        # Test suite
├── scripts/                      # Utility scripts (Bicep validation, etc.)
├── workspaces/
│   └── iot-operations/           # Reference implementation
│       ├── sites/                # Site definitions
│       ├── manifests/            # Deployment orchestration
│       ├── parameters/           # Parameter files
│       └── templates/            # Bicep templates
├── docs/                         # Extended documentation
│   ├── aio-releases.md           # AIO release pinning, upgrades, adding a new release
│   ├── ci-cd-setup.md            # GitHub Actions, Azure DevOps, OIDC, secrets
│   ├── e2e-testing.md            # End-to-end live-subscription test workflow
│   ├── manifest-includes.md      # Splicing one manifest into another via `include:`
│   ├── manifest-reference.md     # Manifest syntax, step types
│   ├── parameter-resolution.md   # Variables, output chaining
│   ├── secret-sync.md            # Secret sync enablement and usage
│   ├── site-configuration.md     # Sites, inheritance, overlays
│   ├── targeting.md              # Selector grammar, site identity, no-match diagnostic
│   └── troubleshooting.md        # Common issues and solutions
├── .github/                      # GitHub Actions workflows
└── .pipelines/                   # Azure DevOps pipeline definitions
```

### Workspace anatomy

Each workspace follows a consistent structure:

| Directory | Purpose | Contains |
|-----------|---------|----------|
| `sites/` | **Where** to deploy | Site definitions with subscription, resource group, labels |
| `manifests/` | **What** to deploy | Ordered steps with site selection and conditions |
| `parameters/` | **With what values** | Template variables, output chaining |
| `templates/` | **How** to deploy | Bicep/ARM templates |
| `sites.local/` | **Overrides** | Local/CI overrides (gitignored) |

---

## Core concepts

### Sites

A **site** is a deployment target. Define one per row in your fleet
under `workspaces/<workspace>/sites/`:

```yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-munich-dev
location: germanywestcentral
labels:
  environment: dev
  city: Munich
parameters:
  clusterName: munich-dev-arc
```

Sites can inherit shared defaults from a `SiteTemplate`, get overlaid
by `sites.local/` files at runtime, and operate at either RG scope or
subscription scope. See [docs/site-configuration.md](docs/site-configuration.md)
for the full model.

### Manifests

A **manifest** is an ordered list of deployment steps targeted at one
or more sites:

```yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-install
selector: "environment=dev"
steps:
  - name: schema-registry
    template: templates/deps/schema-registry.bicep
    scope: resourceGroup
  - name: aio-instance
    template: templates/aio/instance.bicep
    scope: resourceGroup
    parameters:
      - parameters/inputs/aio-instance.yaml  # outputs from prior steps
```

A manifest can also `include:` other manifests (partials and standalone
manifests) to compose larger pipelines. See
[docs/manifest-reference.md](docs/manifest-reference.md) for the full
step shape, conditions, and parallel options, and
[docs/manifest-includes.md](docs/manifest-includes.md) for the
composition contract.

### Template variables

Site values flow into parameter files via Mustache-style placeholders:

```yaml
# parameters/common/common.yaml (manifest-level, applies to every step)
location: "{{ site.location }}"
aioInstanceName: "{{ site.name }}-aio"
```

```yaml
# parameters/inputs/aio-instance.yaml (step-level chaining)
schemaRegistryId: "{{ steps.schema-registry.outputs.schemaRegistry.id }}"
```

See [docs/parameter-resolution.md](docs/parameter-resolution.md) for
auto-filtering, merge order, and cross-scope output chaining.

---

## Commands

| Command | Description |
|---------|-------------|
| `siteops sites` | List sites in the workspace |
| `siteops sites <name>` | Inspect one site (basename, relative path, or internal `name:`) |
| `siteops sites <name> -v` | Show every value with the source file it came from after inherits and overlays |
| `siteops sites <name> --render` | Show the resolved YAML after inheritance and overlays |
| `siteops validate <manifest>` | Validate manifest and all references |
| `siteops validate <manifest> -v` | Validation plus the deployment plan |
| `siteops deploy <manifest>` | Execute deployment |
| `siteops deploy <manifest> --dry-run` | Show what would deploy without calling Azure |

### Common options

| Option | Description | Default |
|--------|-------------|---------|
| `-w, --workspace` | Workspace directory | current dir, walking upward to the nearest `sites/` ancestor |
| `-l, --selector` | Filter sites by label. Repeatable. `name=` may carry multiple values (OR-combined). | none |
| `-p, --parallel` | Max concurrent sites for `deploy`. Accepts a positive integer, or `max`/`auto`/`0` for unlimited | manifest setting |
| `--extra-sites-dir` | Additional trusted `sites/` directory. Repeatable. Also accepts `SITEOPS_EXTRA_SITES_DIRS`. CLI wins on conflict | none |

See [docs/targeting.md](docs/targeting.md) for the selector grammar and the no-match diagnostic.

---

## Extending

### Create a new workspace

1. Create directory structure:

   ```
   workspaces/my-workspace/
   ├── sites/
   ├── manifests/
   ├── parameters/
   └── templates/
   ```

2. Add site definitions in `sites/`
3. Add or reference Bicep templates
4. Create manifests that orchestrate the deployment

### Add a new site

```yaml
# sites/seattle-prod.yaml
apiVersion: siteops/v1
kind: Site
name: seattle-prod
inherits: base-site.yaml  # Optional: inherit shared config

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-seattle-prod
location: westus2

labels:
  environment: prod
  country: US
  city: Seattle

parameters:
  clusterName: seattle-prod-arc
```

Sites can live at any depth under `sites/`. Use `sites/regions/eu/munich.yaml` to group by region. Basenames must remain unique within the trusted directory tree. See [docs/targeting.md](docs/targeting.md) for the identity model.

### Add conditional steps

```yaml
steps:
  - name: optional-feature
    template: templates/feature.bicep
    scope: resourceGroup
    when: "{{ site.properties.featureOptions.enableFeature }}"
```

---

## CI/CD

This repository includes GitHub Actions workflows for automated deployment:

| Workflow | Description |
|----------|-------------|
| `deploy.yaml` | Manual deployment via GitHub UI |
| `ci.yaml` | CI validation (tests + manifest check) |
| `_siteops-deploy.yaml` | Reusable deployment workflow |

### Required secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Azure AD application client ID |
| `AZURE_TENANT_ID` | Yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Default subscription for OIDC login |
| `SITE_OVERRIDES` | No | JSON object with per-site subscription/resourceGroup overrides |

See [docs/ci-cd-setup.md](docs/ci-cd-setup.md) for detailed configuration.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/site-configuration.md](docs/site-configuration.md) | Site definitions, inheritance, overlays |
| [docs/targeting.md](docs/targeting.md) | Selector grammar, site identity, no-match diagnostic |
| [docs/manifest-reference.md](docs/manifest-reference.md) | Manifest syntax, step types, conditions |
| [docs/manifest-includes.md](docs/manifest-includes.md) | Splicing one manifest into another via `include:` |
| [docs/parameter-resolution.md](docs/parameter-resolution.md) | Template variables, output chaining, auto-filtering |
| [docs/aio-releases.md](docs/aio-releases.md) | Pinning an AIO release per site, in-place upgrades, adding a new release |
| [docs/secret-sync.md](docs/secret-sync.md) | Secret sync enablement and usage |
| [docs/ci-cd-setup.md](docs/ci-cd-setup.md) | GitHub Actions, Azure DevOps, OIDC, secrets configuration |
| [docs/e2e-testing.md](docs/e2e-testing.md) | End-to-end live-subscription test workflow |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues and solutions |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and contribution guidelines.

---

## License

[MIT](LICENSE)

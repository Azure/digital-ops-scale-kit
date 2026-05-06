# Site Configuration

Sites define **where** to deploy: the Azure subscription, resource group, location, and site-specific configuration.

## Quick decision table

| I want to... | Do this |
|---|---|
| Add a new deployable site | Drop `my-site.yaml` under `workspace/sites/` (any subdir) or an extras dir |
| Share a reusable template across sites | Put it in `workspace/sites/<name>.yaml` (same dir) or `workspace/sites/shared/<name>.yaml` (subdir) and reference via `inherits:` |
| Override a committed site at runtime without a PR | Put `my-site.yaml` in `workspace/sites.local/` (overlay merges, `inherits:` stripped) |
| Inject a site from CI without touching the workspace | Register a dir via `SITEOPS_EXTRA_SITES_DIRS` / `--extra-sites-dir` and drop `my-site.yaml` in it |
| Target one specific site at the CLI | `siteops deploy <manifest> -l name=<site-name>` |
| Target multiple specific sites at the CLI | `siteops deploy <manifest> -l name=<a>,name=<b>` |
| Pin the manifest to a labeled cohort | Set `selector:` in the manifest |
| Hard-code the target list for a manifest | Set `sites:` in the manifest |
| Preview a fully-resolved site (post inherit + overlay) | `siteops -w <workspace> sites <name> --render` |
| See where every value in a resolved site came from | `siteops -w <workspace> sites <name> -v` |

The reference material below covers the model in depth. See [targeting.md](targeting.md) for the selector grammar and the no-match diagnostic.

## Site levels

Sites operate at two levels based on whether they have a `resourceGroup`:

| Site has | Site level | Deploys |
|----------|-----------|--------|
| `subscription` + `resourceGroup` | RG-level | Both subscription and RG-scoped steps |
| `subscription` only | Subscription-level | `scope: subscription` steps only |

RG-level sites are the common case. Subscription-level sites deploy shared resources once per subscription (like Azure Edge Sites). RG-level sites in the same subscription pick up those outputs via cross-scope output chaining.

## Site structure

**RG-level site** (most common):

```yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-munich-dev
location: germanywestcentral

labels:
  environment: dev
  country: DE
  city: Munich

parameters:
  clusterName: munich-dev-arc
  brokerConfig:
    memoryProfile: Low

properties:
  deployOptions:
    enableSecretSync: true
```

### Site identity

A site is reachable by its filename basename, its relative path under the trusted directory, or its internal `name:` field. The three forms are symmetric. By convention `name:` matches the basename, but it can differ when a friendlier identifier is needed. See [targeting.md](targeting.md) for the full identity model and the workspace invariants the orchestrator enforces at load time.

**Subscription-level site** (for shared resources):

```yaml
apiVersion: siteops/v1
kind: Site
name: germany-subscription

subscription: "00000000-0000-0000-0000-000000000000"
location: germanywestcentral
# No resourceGroup → subscription-level site

labels:
  environment: dev
  scope: subscription      # Required: lets manifests target with `selector: scope=subscription`

parameters:
  edgeSiteName: germany-edge-site
```

## Labels vs Parameters vs Properties

Sites have three ways to attach data, each serving a different purpose:

| Field | Data Type | Filtering | Conditionals | Template Access |
|-------|-----------|-----------|--------------|-----------------|
| `labels` | Flat strings only | ✅ `-l "key=value"` | ✅ `when:` | `{{ site.labels.X }}` |
| `parameters` | Any structure | ❌ | ❌ | `{{ site.parameters.X }}` |
| `properties` | Any structure | ❌ | ✅ `when:` | `{{ site.properties.X.Y }}` |

### Labels

Simple strings for **filtering** and **conditionals**:

```yaml
labels:
  environment: prod        # Filter: siteops deploy -l "environment=prod"
  city: Seattle            # Template variable: {{ site.labels.city }}
```

Use labels when you need to:

- Select sites with `-l` / `--selector`
- Reference simple string values in templates

### Parameters

Values passed directly to **Bicep templates**:

```yaml
parameters:
  clusterName: arc-seattle-prod      # Infrastructure identifier
  brokerConfig:                      # Complex objects for Bicep
    memoryProfile: Medium
    frontendReplicas: 4
```

Use parameters for:

- Infrastructure configuration (cluster names, sizing)
- Values that vary per-site based on capacity
- Complex objects consumed by Bicep templates

### Properties

Free-form site state read by manifests and templates via
`{{ site.properties.<path> }}` substitution and `when:` conditions.
Open schema. Siteops does not enforce field names or shapes. The
workspace defines its own conventions.

```yaml
properties:
  # Pinned AIO release (workspace convention; selects which
  # `parameters/aio-releases/<release>.yaml` gets loaded).
  aioRelease: "2603"

  # Capability gates evaluated by manifest `when:` conditions
  # (workspace convention; the `enable*` prefix is a workspace style).
  deployOptions:
    enableGlobalSite: false
    enableEdgeSite: false
    enableSecretSync: false
    enableCertManager: true

  # Free-form custom fields. Anything you reference via
  # `{{ site.properties.X }}` in a manifest, parameters file,
  # or `when:` condition belongs here.
  opcUaEndpoints:
    - name: cnc-machine-1
      address: opc.tcp://10.1.1.100:4840
```

Use properties for:

- Capability gates evaluated via `when:` (`deployOptions.*`)
- Workspace-specific orchestration state (release pins, feature toggles)
- Free-form data structures consumed via `{{ site.properties.X }}`

> **Bicep inputs go in `parameters:`, not `properties:`.** Resource tags,
> cluster names, and any other value the engine should hand to a Bicep
> `param` declaration belong in `parameters:` (auto-filtered per template).
> `properties:` is read by the orchestrator and template substitution
> only.

### What siteops enforces vs what the workspace conventions are

The siteops engine has a deliberately narrow contract over a site
file. Knowing where the boundary sits tells you what you can rename
when forking the workspace:

| Layer | Owned by | What it cares about |
|---|---|---|
| YAML mechanics | siteops engine | Top-level fields (`name`, `subscription`, `resourceGroup`, `location`, `labels`, `inherits`, `parameters`, `properties`); the `parameters:` filter against Bicep template params; the `{{ site.X }}` and `{{ site.properties.<path> }}` substitution surface; selector parsing on `labels`. |
| Field semantics | The workspace | The names of fields under `properties:` (`aioRelease`, `deployOptions`, `enable*` prefix, etc.) and the names of label keys used in selectors (`environment`, `country`, `scope`, etc.). |

Anything in the second row is a convention you can rename for your own
workspace. The iot-operations workspace happens to use
`properties.aioRelease`, `properties.deployOptions.enable*`, and
`labels.environment`. A forked workspace could call them
`properties.release`, `properties.featureFlags.*`, or `labels.tier`
without the engine caring. Just keep manifest `when:` conditions and
`{{ site.properties.X }}` references in sync with whatever the
workspace decides.

### Conditionals

Properties support conditionals with truthy syntax:

```yaml
# Truthy check (recommended for booleans)
when: "{{ site.properties.deployOptions.enableSecretSync }}"

# Explicit comparison (also supported)
when: "{{ site.properties.deployOptions.enableSecretSync == true }}"
when: "{{ site.labels.environment == 'prod' }}"
```

### Rule of thumb

- Need to filter sites? → **Labels** (strings only)
- Need in `when` conditionals? → **Labels** (string comparison) or **Properties** (truthy check)
- Goes into Bicep templates? → **Parameters**
- Structured metadata (tags, arrays, deployment options)? → **Properties**

## Site overlays

Sites support layered definitions for separating committed config from local/CI overrides:

```
sites/           # Base definitions (committed to git)
sites.local/     # Overrides (gitignored)
```

**Merge order**: `sites/` → `sites.local/` (later values override earlier)

```yaml
# sites/munich-dev.yaml (committed)
name: munich-dev
subscription: "00000000-0000-0000-0000-000000000000"  # Placeholder
resourceGroup: placeholder
location: germanywestcentral
```

```yaml
# sites.local/munich-dev.yaml (gitignored)
subscription: "real-subscription-id"
resourceGroup: real-resource-group
```

> **Security**: Only base files (in trusted site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance.

## Extra trusted site directories

In addition to the workspace's `sites/` directory, Site Ops can search
one or more extra trusted directories for site files. Files in these
directories are treated exactly like files in `sites/`: they are
discoverable by `siteops sites`, they can declare `inherits`, and they
serve as valid base files for the inheritance chain.

Use cases include:

- **CI / end-to-end tests**: keep test-only sites out of `workspaces/*/sites/`
  (production config) and inject them only when the test workflow runs.
- **Cross-repo site libraries**: pull shared sites from another repository
  checked out alongside the workspace.
- **Blueprint catalogs**: keep opinionated site templates in a central
  location, pointed at from multiple workspaces.

Provide extra directories via the CLI or environment variable:

```bash
# Repeatable flag
siteops -w workspace --extra-sites-dir ./tests/e2e/sites sites

# Environment variable (os.pathsep-separated: ';' on Windows, ':' on Unix)
SITEOPS_EXTRA_SITES_DIRS=/path/to/lib-sites siteops -w workspace sites
```

When both are provided, the CLI flag wins and an INFO log records that
the env var was ignored.

**Merge order (full)**:

```
inherits target → sites/ → <extra dirs, in listed order> → sites.local/
```

Extras cannot collide with the workspace's own `sites/` or `sites.local/` directories. The orchestrator rejects both at construction time. Registering `sites.local/` as trusted is specifically refused because it would let overlays inject inheritance and break the overlay security invariant.

### Discovery walks subdirectories

Every trusted directory (`sites/`, each extras dir, `sites.local/`) is scanned recursively. A site at any depth is reachable by its basename (filename without extension), by its relative path under the trusted dir, or by its internal `name:` field. Basename uniqueness within each trusted dir is enforced at load time so the basename shorthand always resolves unambiguously. Cross-dir basename collisions are valid only when the relative path also matches (the overlay pattern).

| Path | Kind | Reachable via |
|---|---|---|
| `sites/munich-prod.yaml` | `Site` | `munich-prod`, internal `name:` |
| `sites/regions/eu/munich-prod.yaml` | `Site` | `munich-prod`, `regions/eu/munich-prod`, internal `name:` |
| `sites/base-site.yaml` | `SiteTemplate` | `inherits: base-site.yaml` only |
| `sites/shared/usa-west.yaml` | `SiteTemplate` | `inherits: shared/usa-west.yaml` only |

See [targeting.md](targeting.md) for the full identity model and CLI grammar.

## Site inheritance

Sites can inherit from shared templates to reduce duplication:

```yaml
# sites/base-site.yaml
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site

parameters:
  brokerConfig:
    memoryProfile: Medium
    frontendReplicas: 2

properties:
  tags:
    project: iot-operations
    managedBy: siteops
```

```yaml
# sites/munich-dev.yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev
inherits: base-site.yaml

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-munich-dev
location: germanywestcentral

labels:
  environment: dev

parameters:
  brokerConfig:
    memoryProfile: Low  # Overrides inherited value
```

### How `inherits:` paths are resolved

Resolution is relative to the **child file's own directory**. The only
exception is the bare-filename fallback (row 1 below), which lets an
extras-dir site inherit a workspace-owned template without copying it.

| Form | Example | Resolves to |
|---|---|---|
| Bare filename | `inherits: base-site.yaml` | `./base-site.yaml` next to the child, then fallback to `<workspace>/sites/base-site.yaml` |
| Subpath | `inherits: shared/usa-east.yaml` | `<child-dir>/shared/usa-east.yaml` |
| Parent / sibling | `inherits: ../base-site.yaml` | `<child-dir>/../base-site.yaml` |
| Absolute | `inherits: /abs/path/tpl.yaml` | Used as-is |

The fallback searches `<workspace>/sites/` only (never across extras
dirs), so there is no implicit shared-template namespace between trusted
directories.

> **Trust model.** `inherits:` is author-trusted and not filesystem-sandboxed;
> it may point to a sibling `shared/` dir or an absolute path. The control is
> *who may author files in trusted sites locations* (`workspace/sites/` and
> extras dirs); anyone who can write an `inherits:` value can already set any
> other site field. `sites.local/` overlays strip `inherits:`, so runtime
> overlays cannot introduce new inheritance targets.

### SiteTemplate vs Site

| Aspect | `kind: Site` | `kind: SiteTemplate` |
|--------|--------------|----------------------|
| Can be deployed | ✅ Yes | ❌ No |
| Can be inherited from | ✅ Yes | ✅ Yes |
| Requires subscription/location | ✅ Yes | ❌ No |
| Discovered by `siteops sites` | ✅ Yes | ❌ No |

### Merge order with inheritance

`inherits target` → `sites/` → `<extra trusted dirs>` → `sites.local/`

Inherited values are overridden by child site values. Nested objects (labels, parameters, properties) merge recursively. See [Extra trusted site directories](#extra-trusted-site-directories) for how extra dirs participate in the chain.

> **Security**: Only base files (in trusted site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance, even when extra trusted dirs are configured.

## Site selection from a manifest

A manifest's target sites resolve from three sources: CLI `-l/--selector` (overrides everything), manifest `sites:` (explicit name list), and manifest `selector:` (label expression). A manifest with none of the three is a library or partial that requires `-l` at deploy time.

```bash
siteops deploy manifests/aio-install.yaml                           # uses manifest selector
siteops deploy manifests/aio-install.yaml -l environment=dev        # CLI overrides manifest
siteops deploy manifests/aio-install.yaml -l name=munich-dev        # single site
siteops deploy manifests/aio-install.yaml -l name=a,name=b          # multi-site (name OR-combines)
```

`-l` is repeatable. Distinct keys AND-combine. Repeated `name=` values OR-combine. Any other duplicate key is an error. Path-form names (`-l name=regions/eu/munich`) work for nested site files. See [targeting.md](targeting.md) for the full grammar, the no-match diagnostic, and the validation rules.

## Scaling to a fleet

Once you cross a handful of sites, two-axis composition (region × environment) duplicates env config across `<region>-dev.yaml` and `<region>-prod.yaml`. The recommended pattern: introduce intermediate `SiteTemplate` files so each concrete site inherits one chain.

```
sites/
├── base-site.yaml                # workspace defaults
├── shared/
│   ├── env-dev.yaml              # SiteTemplate: dev-only labels + parameters
│   ├── env-prod.yaml             # SiteTemplate: prod-only labels + parameters
│   ├── region-eu.yaml            # SiteTemplate: location, country labels
│   └── region-eu-prod.yaml       # SiteTemplate: inherits region-eu, then env-prod
├── munich-dev.yaml               # Site: inherits shared/region-eu.yaml + dev override
├── munich-prod.yaml              # Site: inherits shared/region-eu-prod.yaml
└── ...
```

Each concrete site declares a single `inherits:` parent. The intermediate `SiteTemplate` files capture the cross-cutting axes so that adding a new region or environment is a one-file change instead of N edits across N regions.

> Validate the resolved shape with `siteops -w <workspace> sites <name> --render` before committing the new template chain.

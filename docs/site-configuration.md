# Site Configuration

Sites define **where** to deploy: the Azure subscription, resource group, location, and configuration specific to each Site.

With an [operator project](projects.md), `--project DIRECTORY` selects its
`sites` and `sites.local` directories independently from the content
workspace. An explicit `-w PATH` selects local development content while
retaining those project Sites. Without a project, the workspace owns Site
configuration as described below. This page calls the directory that supplies
`sites/` and `sites.local/` the configuration root: the project when one is
selected, otherwise the workspace. Extra trusted Site directories add to
whichever configuration root is selected.

## Quick decision table

| I want to... | Do this |
|---|---|
| Supply one complete Site without storing it in the workspace | Pass `--site-file FILE` to `validate`, `plan`, or `deploy` |
| Answer a selected manifest's typed inputs | Use [`siteops inputs`](guided-inputs.md), then pass `--input-file FILE` or repeat `--input NAME=VALUE` |
| Preview a resolved Site without writing it | Use `siteops inputs <manifest> --input-file FILE` with complete answers |
| Add a new deployable Site | Drop `my-site.yaml` under the configuration root's `sites/` (any subdirectory) or an extras directory |
| Share a reusable template across Sites | Put it in `sites/<name>.yaml` (same directory) or `sites/shared/<name>.yaml` (subdirectory) and reference via `inherits:` |
| Override a committed Site at runtime without a PR | Put `my-site.yaml` in the configuration root's `sites.local/` (overlay merges, `inherits:` stripped) |
| Inject a Site from CI without touching the workspace | Register a directory via `SITEOPS_EXTRA_SITES_DIRS` / `--extra-sites-dir` and drop `my-site.yaml` in it |
| Target one specific Site at the CLI | `siteops deploy <manifest> -l name=<site-name>` |
| Target multiple specific Sites at the CLI | `siteops deploy <manifest> -l name=<a>,name=<b>` |
| Pin the manifest to a labeled fleet | Set `selector:` in the manifest |
| Fix the list of Sites for a manifest | Set `sites:` in the manifest |
| List configured Sites with location, resource group and labels | `siteops -w <workspace> sites` |
| Preview a fully resolved Site (after inheritance and overlays) | `siteops -w <workspace> sites <name> --output yaml` |
| Inspect resolved Sites in private automation | `siteops -w <workspace> sites --output json` |
| See where every value in a resolved Site came from | `siteops -w <workspace> sites <name> --show-sources` |

The reference material below covers the model in depth. See [targeting.md](targeting.md) for the selector grammar and the diagnostic when nothing matches.

## Inspect resolved Sites

Check what a Site resolves to after inheritance and overlays:

```bash
siteops -w workspaces/iot-operations sites munich-dev --output yaml
```

The default `--output plain` is a display for people to read. Every format
uses the same resolved Sites and sorts them by name.

| Format | Output shape |
|---|---|
| `plain` | One line per Site (name, location, resource group and labels) when several match. Full resolved detail for one Site, or for every match with `--show-sources` |
| `yaml` | One `Site` document per match, separated by `---` for multiple Sites |
| `json` | One array of `Site` objects, including when only one Site matches |

Run `siteops sites NAME` to see one Site's resolved configuration in plain
output. Values use YAML spelling, such as `false`, `null` and `[a, b]`.

Use `--show-sources` with the default plain output to see where each value
came from:

```bash
siteops -w workspaces/iot-operations sites munich-dev --show-sources
```

These commands show private configuration. For automation, see
[inspection output details](#inspection-output-details).

## Site levels

Sites operate at two levels based on whether they have a `resourceGroup`:

| Site has | Site level | Deploys |
|----------|-----------|--------|
| `subscription` + `resourceGroup` | Resource group | Resource group steps, and kubectl or wait steps for the Site |
| `subscription` only | Subscription | `scope: subscription` steps, and kubectl or wait steps for the Site |

Sites at resource group level are the common case. A Site at subscription
level deploys shared resources once for its subscription, such as Azure Edge
Sites. Sites at resource group level in that subscription can consume those
outputs by chaining across scopes.

## Site structure

**Site at resource group level** (most common):

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

A Site is reachable by its filename basename, its relative path under the trusted directory, or its internal `name:` field. The three forms are symmetric. By convention `name:` matches the basename, but it can differ when a friendlier identifier is needed. See [targeting.md](targeting.md) for the full identity model and the workspace invariants the orchestrator enforces at load time.

Saving with `siteops inputs ... --save-site` into the selected `sites/`
inventory checks Site identities before writing. A later plan loads that
inventory again so concurrent or subsequent configuration changes are
still checked.

**Site at subscription level** (for shared resources):

```yaml
apiVersion: siteops/v1
kind: Site
name: contoso-global
inherits: base-site.yaml

subscription: "00000000-0000-0000-0000-000000000000"
location: eastus
# No resourceGroup, so this is a subscription-level site

labels:
  scope: subscription      # Workspace convention for selectors
  country: US
  city: Redmond

properties:
  deployOptions:
    enableGlobalSite: true  # Gates the subscription-scoped edge site step

parameters:
  siteName: contoso-edge-site
```

## Labels vs Parameters vs Properties

Sites have three ways to attach data, each serving a different purpose:

| Field | Data Type | Filtering | Conditionals | Template Access |
|-------|-----------|-----------|--------------|-----------------|
| `labels` | Flat strings only | Yes, `-l "key=value"` | Yes, `when:` | `{{ site.labels.X }}` |
| `parameters` | Any structure | No | No | `{{ site.parameters.X }}` |
| `properties` | Any structure | No | Yes, `when:` | `{{ site.properties.X.Y }}` |

### Labels

Simple strings for **filtering** and **conditionals**:

```yaml
labels:
  environment: prod        # Filter: siteops deploy -l "environment=prod"
  city: Seattle            # Template variable: {{ site.labels.city }}
```

Use labels when you need to:

- Select Sites with `-l` / `--selector`
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
- Values that vary by Site based on capacity
- Complex objects consumed by Bicep templates

#### Top level vs namespaced parameters

Site Ops hands a `site.parameters` entry to a template only when its key at
the top level matches one of the template's `param` names. A nested block reaches a
template only if the template declares a matching object parameter (such as
`param brokerConfig`). Otherwise each nested field must be mapped to a
`param` at the top level in a `parameters/inputs/*.yaml` file.

These habits keep this predictable:

- A value used by more than one layer belongs at the top level, as one key the
  layers cannot disagree on. `clusterName` is shared: the AKS Edge Essentials
  host registers the cluster and Azure IoT Operations deploys onto it. Split it
  (host sets one key, AIO reads another) and the bootstrap passes, then AIO
  fails looking up a cluster name that was never registered.
- Group a feature's settings under a namespace for clarity, like the `aksee`
  block. Namespacing is optional: values with a single purpose, such as
  `brokerConfig`, sit at the top level too.

```yaml
parameters:
  clusterName: arc-seattle-prod        # shared: the host registers it, AIO runs on it
  aksee:                               # host bootstrap and upgrade settings
    machineName: arc-seattle-prod-vm   # the Arc machine resource and VM hostname
    customLocationsOid: <object-id>    # custom-locations resource provider object id
```

### Properties

Site state in any shape, read by manifests and templates via
`{{ site.properties.<path> }}` substitution and `when:` conditions.
Open schema. Site Ops does not enforce field names or shapes. The
workspace defines its own conventions.

```yaml
properties:
  # Pinned AIO release (workspace convention). Selects which
  # `parameters/aio-releases/<release>.yaml` gets loaded. This must be a
  # property, not a parameter: parameter-file path interpolation reads
  # `site.properties` and `site.labels`, never `site.parameters`.
  aioRelease: "2608"

  # Ordered resource sets. Each list item names a YAML file in the matching
  # `resource-sets/<area>/` directory. Omit an area for no selection, or use [] to
  # clear an inherited list. Deselecting a set does not delete resources.
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
    dataflows:
      - site-telemetry

  # Capability toggles, kept in one place. Some gate a whole step via
  # `when:` (enableSecretSync, enableGlobalSite, enableEdgeSite). Others
  # pass through to a Bicep `param` (enableCertManager,
  # enableWorkloadIdentity, allowKubernetesMinorUpgrade). `enable*` turns a
  # component on, `allow*` permits a behavior.
  deployOptions:
    enableGlobalSite: false
    enableEdgeSite: false
    enableSecretSync: false
    enableCertManager: true
    allowKubernetesMinorUpgrade: false

  # Free-form custom fields. Anything you reference via
  # `{{ site.properties.X }}` in a manifest, parameters file,
  # or `when:` condition belongs here.
  opcUaEndpoints:
    - name: cnc-machine-1
      address: opc.tcp://10.1.1.100:4840
```

Use properties for:

- Capability toggles, gated via `when:` or passed through to a template (`deployOptions.*`)
- Keys the engine reads to select paths (`aioRelease` picks one AIO release file, and each list under `resourceSets` selects ordered files from the matching `resource-sets/<area>/` directory)
- Data structures in any shape, consumed via `{{ site.properties.X }}`

> **Template values go in `parameters:`. Capability toggles are the one
> exception.** A name, size, or count the engine hands to a Bicep `param`
> belongs in `parameters:`. On/off toggles stay together under
> `properties.deployOptions`, and when a template needs one, a line in the
> step's `parameters/inputs` file passes it through to the `param`.

### What Site Ops enforces vs what the workspace conventions are

The Site Ops engine has a deliberately narrow contract over a Site
file. Knowing where the boundary sits tells you what you can rename
when forking the workspace:

| Layer | Owned by | What it cares about |
|---|---|---|
| YAML and preparation mechanics | Site Ops engine | Fields at the top level (`name`, `subscription`, `resourceGroup`, `location`, `labels`, `inherits`, `parameters`, `properties`), selector parsing on `labels`, supported substitutions of Site values, and executable filtering against an acquired template schema. |
| Field semantics | The workspace | The names of fields under `properties:` (`aioRelease`, `deployOptions`, the `enable*`/`allow*` toggle prefixes, etc.) and the names of label keys used in selectors (`environment`, `country`, `scope`, etc.). |

Anything in the second row is a convention you can rename for your own
workspace. The IoT Operations workspace happens to use
`properties.aioRelease`, `properties.deployOptions.enable*`, and
`labels.environment`. A forked workspace could call them
`properties.release`, `properties.featureFlags.*`, or `labels.tier`
without the engine caring. Just keep manifest `when:` conditions and
`{{ site.properties.X }}` references in sync with whatever the
workspace decides.

### What a Site file is checked against

The set of fields at the top level above is **closed**. A key the engine does
not read is rejected when the Site loads, rather than being ignored, and the error suggests
the field you probably meant:

```
Error: Site 'munich-dev' has unknown top-level key(s): `paramaters` (did
  you mean `parameters`?). Allowed: ['apiVersion', 'description',
  'inherits', 'kind', 'labels', 'location', 'name', 'parameters',
  'properties', 'resourceGroup', 'subscription']. Merged from:
  sites/base-site.yaml, sites/shared/germany.yaml, sites/munich-dev.yaml.
```

The check runs on the merged result of the inherit chain and every overlay, so
the key may come from a file other than the one you named. `Merged from:` lists
those files in merge order, which includes a parent template, an extra trusted
directory, and `sites.local/`.

Rejecting rather than ignoring is what makes a misspelled field visible. It
matters most for `properties`, since resource sets read it to decide what a
Site deploys, and a typo there leaves the Site on defaults.

Further rules apply:

- **`subscription` and `location` must carry a value.** A key written with
  nothing after the colon parses as null, which is not the same as a default.
  Give it a value, inherit one from a parent template, or remove the key, since
  a blank key overrides the inherited value.
- **`labels`, `properties`, and `parameters` must be mappings** when present.
  Writing one with no value is normalized as an empty mapping before
  inheritance merge, so it preserves values from the parent. Use an explicit
  nested value to clear supported state, such as
  `resourceSets.dataflows: []`.
- **A label value must be text.** Selectors compare text, so `release: 2607`
  matches nothing. Quote it as `release: "2607"`.
- **A field that holds text must hold text.** `name`, `subscription`,
  `resourceGroup`, `location`, `description`, and `inherits` are rejected when
  written as a list or a number. Quote a value YAML would otherwise read as a
  number, such as a Site named for a release.
- **`description` is accepted and not read.** It is there so a shared
  `SiteTemplate` can carry a note.
- **`inherits` is read at the top level of the file.** Both shapes inherit that way. A Site using
  the `metadata`/`spec` envelope inherits by putting `inherits:` alongside `apiVersion` and `kind`.
  Written inside `spec` it is rejected by name rather than reported as a misspelling, since the
  placement is what is wrong.

A file that declares `spec` and also carries fields of the flat shape at the top level
is reported as mixing the two shapes. Pick one: move the fields under
`metadata` and `spec`, or remove `spec` and keep everything at the top level.

The envelope shape puts `name`, `description` and `labels` under `metadata`,
and the remaining fields under `spec`:

```yaml
apiVersion: siteops/v1
kind: Site
metadata:
  name: munich-dev
  labels:
    environment: dev
spec:
  subscription: "00000000-0000-0000-0000-000000000000"
  resourceGroup: rg-iot-munich-dev
  location: germanywestcentral
  parameters:
    clusterName: munich-dev-arc
```

Keep one shape across an `inherits` chain. The check runs on merged data, so a
flat parent with an envelope child, or the reverse, is also reported as mixing
the two shapes.

What is inside `labels`, `properties`, and `parameters` stays **open**, because
those hold content defined by the workspace and the engine stays out of it. A key in
there that nothing reads is a different problem, and the workspace's own test
suite catches it rather than the engine.

### Templates in a parameter name

Supported Site expressions resolve in a parameter **name** as well as a value,
so one declaration can key data by Site:

```yaml
parameters:
  siteRoles:
    "{{ site.name }}":
      role: primary
```

Keep the template on a **nested** name unless the resolved name at the top
level is itself a declared template parameter. Executable preparation filters
a name at the top level that the acquired template does not accept and reports any
required parameter that remains missing.

These cases fail rather than resolve: a name that cannot be resolved, a
template that resolves to a whole object or list, and two names that resolve to
the same string. The last is rejected rather than letting one overwrite the
other.

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

- Need to filter Sites? Use **labels** (strings only).
- Need a `when` condition? Use **labels** (string comparison) or **properties** (truthy check).
- Goes into Bicep templates? Use **parameters**.
- Structured metadata (tags, arrays, deployment options)? Use **properties**.

## Site overlays

Sites support layered definitions for separating committed config from local/CI overrides:

```
sites/           # Base definitions (committed to git)
sites.local/     # Overrides (gitignored)
```

**Merge order**: `sites/`, then `sites.local/` (later values override earlier)

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

> **Security**: Only base files (in trusted Site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance.

## Extra trusted Site directories

In addition to the configuration root's `sites/` directory, Site Ops can search
one or more extra trusted directories for Site files. Files in these
directories are treated exactly like files in `sites/`: they are
discoverable by `siteops sites`, they can declare `inherits`, and they
serve as valid base files for the inheritance chain.

Use cases include:

- **CI and E2E tests**: keep Sites used only by tests out of `workspaces/*/sites/`
  (production config) and inject them only when the test workflow runs.
- **Site libraries in other repositories**: pull shared Sites from another repository
  checked out alongside the workspace.
- **Blueprint catalogs**: keep opinionated Site templates in a central
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

Extras cannot collide with the configuration root's own `sites/` or `sites.local/` directories. Site Ops rejects both when it starts. Registering `sites.local/` as trusted is specifically refused because it would let overlays inject inheritance and break the overlay security invariant.

### Discovery walks subdirectories

Site discovery scans `sites/` and each extra trusted directory recursively
for `.yaml` and `.yml` files. `sites.local/` supplies matching overlays, not
new Site identities. A Site at any depth is reachable by its basename
(filename without extension), its relative path under the trusted directory,
or its internal `name:` field. Basenames must be unique within each trusted
directory. A basename shared across directories must also have the same
relative path so the files describe one overlaid Site.

| Path | Kind | Reachable via |
|---|---|---|
| `sites/munich-prod.yaml` | `Site` | `munich-prod`, internal `name:` |
| `sites/regions/eu/munich-prod.yaml` | `Site` | `munich-prod`, `regions/eu/munich-prod`, internal `name:` |
| `sites/base-site.yaml` | `SiteTemplate` | `inherits: base-site.yaml` only |
| `sites/shared/usa-west.yaml` | `SiteTemplate` | `inherits: shared/usa-west.yaml` only |

See [targeting.md](targeting.md) for the full identity model and CLI grammar.

## Inspection output details

A bare listing of an empty workspace succeeds. YAML emits no documents and
JSON emits `[]`. An explicit name or selector with no matches fails. Invalid
or incomplete selections also fail without emitting a partial document.
Diagnostics use stderr rather than the structured stdout stream.

Source annotations are part of the plain display. Combining `--show-sources`
with YAML or JSON reports `Error: --show-sources requires --output plain.`

Site inspection is private. Keys that look sensitive in parameters and
properties are masked, but Site identities and ordinary configuration values
remain. These views are not publishable projections or lossless configuration
exports. Site details are unavailable when `SITEOPS_REDACT_OUTPUT` or a CI
environment marker enables redaction. For an authorized private destination,
explicitly set `SITEOPS_REDACT_OUTPUT=0` and keep the output private.

JSON requires string mapping keys and representable values. YAML values
such as timestamps and numbers that are not finite produce a clear JSON error rather
than being silently converted. Use `--output yaml` to inspect those values.

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
exception is the fallback for a bare filename (row 1 below), which lets a
Site in an extras directory inherit a template from the configuration root
without copying it.

| Form | Example | Resolves to |
|---|---|---|
| Bare filename | `inherits: base-site.yaml` | `./base-site.yaml` next to the child, then fallback to `<configuration-root>/sites/base-site.yaml` |
| Subpath | `inherits: shared/usa-east.yaml` | `<child-dir>/shared/usa-east.yaml` |
| Parent / sibling | `inherits: ../base-site.yaml` | `<child-dir>/../base-site.yaml` |
| Absolute | `inherits: /abs/path/tpl.yaml` | Used unchanged |

The fallback searches only the configuration root's `sites/`: the project's
with `--project`, otherwise the workspace's. It never searches extras
directories or `sites.local/`, so trusted directories share no implicit
namespace for templates.

> **Trust model.** `inherits:` trusts its author and is not confined to a
> filesystem sandbox. It may point to a sibling `shared/` directory or an
> absolute path. The control is *who may author files in trusted Site
> locations* (`sites/` under the configuration root, and extras directories).
> Anyone who can write an `inherits:` value can already set any other Site
> field. `sites.local/` overlays strip `inherits:`, so runtime
> overlays cannot introduce new inheritance targets.

### SiteTemplate vs Site

| Aspect | `kind: Site` | `kind: SiteTemplate` |
|--------|--------------|----------------------|
| Can be deployed | Yes | No |
| Can be inherited from | Yes | Yes |
| Requires subscription/location | Yes | No |
| Discovered by `siteops sites` | Yes | No |

### Merge order with inheritance

`inherits target`, then `sites/`, then `<extra trusted dirs>`, then `sites.local/`

Inherited values are overridden by child Site values. Nested objects (labels, parameters, properties) merge recursively. See [Extra trusted Site directories](#extra-trusted-site-directories) for how extra directories participate in the chain.

> **Security**: Only base files (in trusted Site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance, even when extra trusted directories are configured.

## Site selection from a manifest

A manifest's Sites resolve from CLI `-l/--selector` (overrides
everything), manifest `sites:` (explicit name list), or manifest `selector:`
(label expression). A manifest with none of these is a library or partial. It
can be checked with `validate`, while `plan` and `deploy` require `-l` or one
explicit Site supplied with `--site-file`, `--input-file`, or `--input`. An
explicit Site cannot be combined with `-l`.

```bash
siteops plan aio-install                           # uses manifest selector
siteops plan aio-install -l environment=dev        # CLI overrides manifest
siteops plan aio-install -l name=munich-dev        # single site
siteops plan aio-install -l name=a,name=b          # multi-site (name OR-combines)
```

`deploy` accepts the same selection once the plan is right.

`-l` is repeatable. Distinct keys combine with AND. Repeated `name=` values combine with OR. Any other duplicate key is an error. Path names (`-l name=regions/eu/munich`) work for nested Site files. See [targeting.md](targeting.md) for the full grammar, the diagnostic when nothing matches, and the validation rules.

## Scaling to a fleet

Once you pass a handful of Sites, composing on two axes (region by environment) duplicates environment config across `<region>-dev.yaml` and `<region>-prod.yaml`. The recommended pattern: introduce intermediate `SiteTemplate` files so each concrete Site inherits one chain.

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

Each concrete Site declares a single `inherits:` parent. The intermediate `SiteTemplate` files capture the axes shared across Sites, so adding a new region or environment changes one file instead of N files across N regions.

For an AIO fleet, use one resource group per AIO instance. Each Site deploys into its `resourceGroup`, which is its Arc cluster's resource group, so connect each cluster in its own resource group. Set `resourceGroup` on each concrete Site, not in a shared `SiteTemplate`.

> Validate the resolved shape with `siteops -w <workspace> sites <name> --output yaml` before committing the new template chain.

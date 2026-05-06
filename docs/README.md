# Documentation

Extended documentation for the Digital Operations Scale Kit.

**New to siteops?** Start with [site-configuration.md](site-configuration.md), then [targeting.md](targeting.md), then [manifest-reference.md](manifest-reference.md). Operating in CI/CD? Jump to [ci-cd-setup.md](ci-cd-setup.md).

## Contents

| Document | Description |
|----------|-------------|
| [site-configuration.md](site-configuration.md) | Site definitions, inheritance, overlays |
| [targeting.md](targeting.md) | Selector grammar, site identity, no-match diagnostic |
| [manifest-reference.md](manifest-reference.md) | Manifest syntax, step types, conditions |
| [manifest-includes.md](manifest-includes.md) | Splicing one manifest into another via `include:` |
| [parameter-resolution.md](parameter-resolution.md) | Template variables, output chaining, auto-filtering |
| [aio-releases.md](aio-releases.md) | Pinning an AIO release per site, in-place upgrades, adding a new release |
| [secret-sync.md](secret-sync.md) | Secret sync enablement and usage |
| [ci-cd-setup.md](ci-cd-setup.md) | GitHub Actions, OIDC, secrets configuration |
| [e2e-testing.md](e2e-testing.md) | End-to-end live-subscription test workflow |
| [troubleshooting.md](troubleshooting.md) | Common issues and solutions |

## Glossary

| Term | Meaning |
|------|---------|
| **Workspace** | A directory under `workspaces/` containing the standard subdirs (`sites/`, `manifests/`, `parameters/`, `templates/`) plus optional `samples/`, `scenarios/`, `sites.local/`. |
| **Site** | A deployment target (`kind: Site`). Has subscription, optional resource group, location, labels, parameters, properties. |
| **SiteTemplate** | A reusable site base (`kind: SiteTemplate`). Cannot be deployed directly. Referenced via `inherits:`. |
| **Manifest** | A `kind: Manifest` YAML defining ordered steps + parameters + a site selector. The unit of `siteops deploy`. |
| **Selector** | A label expression (`key=value,key=value`) that filters sites. Set on a manifest as `selector:` or via the CLI `--selector` / `-l` flag. See [targeting.md](targeting.md). |
| **Inheritance** | Single-parent merge for sites. A site `inherits:` from a SiteTemplate. Child overrides parent on conflict. Nested objects merge recursively. |
| **Overlay** | A same-name site file in `sites.local/` (or an extras dir) that merges into a base site at load time. Cannot introduce `inherits:` or rename the site. |
| **Include** | A step shape that splices another manifest's steps into the parent's step list at the include's position. Optionally gated by `when:`. |
| **Standalone manifest** | A manifest meant to be deployed directly. The default. |
| **Partial** | A manifest authored to be `include:`-d, not deployed standalone. Filename prefixed `_` by convention. |
| **Sample** | A self-contained workload bundle in `samples/<name>/` (manifest, partial, inputs file, Bicep template). |
| **Step** | A unit of work in a manifest's `steps:` list. Three shapes: Bicep deploy (`template:`), kubectl op (`type: kubectl`), include (`include:`). |
| **Scope** | A step's deployment scope: `resourceGroup` or `subscription`. |
| **AIO release** | A versioned bundle of pinned extension versions and API versions, defined by a YAML in `parameters/aio-releases/` and selected per site via `properties.aioRelease`. |
| **Auto-filtering** | The engine drops parameter keys that the target Bicep template does not declare. Enables shared parameter files across templates. |
| **Chaining** | Wiring a step's outputs into a downstream step's parameters via `{{ steps.X.outputs.Y }}`. |
| **Dispatcher** | A Bicep template that switches on an API-version param into per-API-version inner modules under `templates/<area>/modules/`. |

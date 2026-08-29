# Documentation

Extended documentation for the Digital Operations Scale Kit.

**New to siteops?** Start with [site-configuration.md](site-configuration.md), then [targeting.md](targeting.md), then [manifest-reference.md](manifest-reference.md). Operating in CI/CD? Jump to [ci-cd-setup.md](ci-cd-setup.md).

## Contents

| Document | Description |
|----------|-------------|
| [migrating.md](migrating.md) | What to change in a workspace when moving to a newer Scale Kit release |
| [site-configuration.md](site-configuration.md) | Site definitions, inheritance, overlays |
| [targeting.md](targeting.md) | Selector grammar, site identity, no-match diagnostic |
| [manifest-reference.md](manifest-reference.md) | Manifest syntax, step types, conditions |
| [manifest-includes.md](manifest-includes.md) | Splicing one manifest into another via `include:` |
| [parameter-resolution.md](parameter-resolution.md) | Template variables, output chaining, auto-filtering |
| [aio-releases.md](aio-releases.md) | Pinning an AIO release per site, in-place upgrades, adding a new release |
| [resource-catalog.md](resource-catalog.md) | Declaring AIO workload resources in YAML, attachment routes, when to use Bicep |
| [assets.md](assets.md) | Device Registry devices and assets |
| [dataflows.md](dataflows.md) | Dataflow endpoints, profiles, and dataflows |
| [secret-sync.md](secret-sync.md) | Secret sync enablement and usage |
| [ci-cd-setup.md](ci-cd-setup.md) | GitHub Actions, OIDC, secrets configuration |
| [e2e-testing.md](e2e-testing.md) | End-to-end live-subscription test workflow |
| [troubleshooting.md](troubleshooting.md) | Common issues and solutions |

## Glossary

| Term | Meaning |
|------|---------|
| **Workspace** | A directory under `workspaces/` containing the standard subdirs (`sites/`, `manifests/`, `parameters/`, `templates/`) plus optional `samples/`, `sites.local/`. |
| **Site** | A deployment target (`kind: Site`). Has subscription, optional resource group, location, labels, parameters, properties. |
| **SiteTemplate** | A reusable site base (`kind: SiteTemplate`). Cannot be deployed directly. Referenced via `inherits:`. |
| **Manifest** | A `kind: Manifest` YAML defining ordered steps + parameters + a site selector. The unit of `siteops deploy`. |
| **Selector** | A label expression (`key=value,key=value`) that filters sites. Set on a manifest as `selector:` or via the CLI `--selector` / `-l` flag. See [targeting.md](targeting.md). |
| **Inheritance** | Single-parent merge for sites. A site `inherits:` from a SiteTemplate. Child overrides parent on conflict. Nested objects merge recursively. |
| **Overlay** | A same-name site file in `sites.local/` (or an extras dir) that merges into a base site at load time. Cannot introduce `inherits:` or rename the site. |
| **Include** | A step shape that splices another manifest's steps into the parent's step list at the include's position. Optionally gated by `when:`. |
| **Standalone manifest** | A manifest meant to be deployed directly. The default. |
| **Partial** | A manifest authored to be `include:`-d, not deployed standalone. Filename prefixed `_` by convention. |
| **Sample** | A deployable example in `samples/<name>/`. Two shapes are supported, split by whether other samples can compose it: bundles (carry a `_partial.yaml`, so other samples can compose them) and compositions (`include:` other partials instead). |
| **Composition** | A sample that carries no `_partial.yaml`, so it is an endpoint rather than a building block. Its `manifest.yaml` is built from `include:` steps pulling in `_partial.yaml`s from `manifests/` and other samples, plus any glue step they need and any declaration files it attaches. |
| **Declaration** | An operator-authored parameter file describing values or resources to apply, such as a `secrets` array or the asset catalog's `devices` and `assets`. It attaches at manifest level. Ordinary values remain overridable through `site.parameters`. Composed resource collections change by selecting different resource sets. |
| **Resource catalog** | The workspace library of reusable AIO resource definitions. `manifests/aio-resources.yaml` composes the sets each site selects. See [resource-catalog.md](resource-catalog.md). |
| **Resource area** | A public selection axis under `properties.resourceSets`, such as `devices`, `assets`, or `dataflows`. Several areas may share one internal deployment step. |
| **Deployment family** | An internal group of related resource kinds deployed as one step, such as Device Registry devices and assets, or dataflow endpoints, profiles, and dataflows. |
| **Resource set** | A named YAML source containing resource definitions or advanced composition metadata. Sites compose ordered sets through `properties.resourceSets.<area>`. Deselecting a set does not delete resources. |
| **Step** | A unit of work in a manifest's `steps:` list. Shapes: Bicep deploy (`template:`), kubectl op (`type: kubectl`), wait gate (`type: wait`), include (`include:`). |
| **Scope** | A step's deployment scope: `resourceGroup` or `subscription`. |
| **AIO release** | A versioned bundle of pinned extension versions and API versions, defined by a YAML in `parameters/aio-releases/` and selected per site via `properties.aioRelease`. |
| **Auto-filtering** | The engine drops parameter keys that the target Bicep template does not declare. Enables shared parameter files across templates. |
| **Chaining** | Wiring a step's outputs into a downstream step's parameters via `{{ steps.X.outputs.Y }}`. |
| **Dispatcher** | A Bicep template that switches on an API-version param into per-API-version inner modules under `templates/<area>/modules/`. |

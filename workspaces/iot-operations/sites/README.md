# sites/

Site files (`kind: Site` and `kind: SiteTemplate`), one for each deployment target or shared template.

## Files

- **`base-site.yaml`**: `kind: SiteTemplate`. The shared base holds AIO release selection, labels, common parameters and deployment options. Concrete Sites supply their subscription and location.
- **`<site>.yaml`**: `kind: Site`. A deployable Site. Names match `<city>-<env>` for Sites at resource group level or `<tenant>-global` for Sites at subscription level.
- **`shared/`**: additional `kind: SiteTemplate` files for shared settings (for example `germany.yaml`, `usa-east.yaml`).
- **`catalog-basic.yaml` and `catalog-composition.yaml`**: deployable sample
  Sites for the beginner and advanced resource set walkthroughs under
  `samples/`.

## Conventions

- **Inheritance**: a Site declares `inherits: base-site.yaml` (or any `SiteTemplate`). Single parent. Child wins on conflict. Nested objects merge recursively. See `docs/site-configuration.md`.
- **Overlays**: a file with the same name under `sites.local/` (or any extras directory passed via `--extra-sites-dir`) merges into the base Site at load time. Overlays cannot introduce `inherits:`.
- **Scope**: Sites with no `resourceGroup:` are scoped to the subscription and must carry `labels.scope: subscription` so manifests can select them with `selector: scope=subscription`. The `test_subscription_scoped_sites_carry_scope_label` workspace test enforces this.
- **Resource sets**: each ordered list under `properties.resourceSets` names reusable resource definitions from the matching `resource-sets/<area>/` directory. An omitted area in the effective Site means no selection. A child Site must use `[]` to clear a list inherited from its parent. Deselecting a set stops applying it and does not delete resources. See `docs/resource-catalog.md`.

## Authoring tips

- Preview the fully resolved Site (after inheritance + overlays) with `siteops -w workspaces/iot-operations sites <name> --output yaml`.
- Keep values specific to an environment or region in intermediate `SiteTemplate` files under `shared/` to avoid duplicating env config across `<city>-dev.yaml` and `<city>-prod.yaml` pairs.

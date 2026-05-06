# sites/

Per-deployment-target YAML files (`kind: Site` and `kind: SiteTemplate`).

## Files

- **`base-site.yaml`**: `kind: SiteTemplate`. The shared base every concrete site inherits from. Holds workspace defaults: subscription, location, labels, common parameters, the `deployOptions` shape.
- **`<site>.yaml`**: `kind: Site`. A deployable target. Names match `<region>-<env>` for RG-scoped sites or `<tenant>-global` for subscription-scoped.
- **`shared/`**: additional `kind: SiteTemplate` files for partial reuse (e.g. `germany.yaml`, `usa-east.yaml`).

## Conventions

- **Inheritance**: a site declares `inherits: base-site.yaml` (or any `SiteTemplate`). Single parent. Child wins on conflict. Nested objects merge recursively. See `docs/site-configuration.md`.
- **Overlays**: a same-name file under `sites.local/` (or any extras dir passed via `--extra-sites-dir`) merges into the base site at load time. Overlays cannot introduce `inherits:`.
- **Scope**: sites with no `resourceGroup:` are subscription-scoped and must carry `labels.scope: subscription` so manifests can target them with `selector: scope=subscription`. The `test_subscription_scoped_sites_carry_scope_label` workspace test enforces this.

## Authoring tips

- Preview the fully-resolved site (after inheritance + overlays) with `siteops -w workspaces/iot-operations sites <name> --render`.
- Keep environment- or region-specific values in intermediate `SiteTemplate` files under `shared/` to avoid duplicating env config across `<region>-dev.yaml` and `<region>-prod.yaml` pairs.

# Apply AIO resource sets

Apply reusable devices, assets, endpoints, profiles and dataflows to an
existing AIO instance. Each Site selects the resource sets it needs.
This operation does not install AIO or implicitly enable Secret Sync.

## Select what to apply

Confirm the Site's subscription, resource group and AIO release. Supply
`parameters.aioInstanceName` when the existing instance does not use the
Site-derived naming convention.

Select named files from the [resource-set library](../../resource-sets/README.md):

```yaml
properties:
  resourceSets:
    dataflows:
      - basic-routing
```

Omitting an area preserves any inherited selection. An explicit empty list
clears it. Selected collections come from their declaration files, not
`site.parameters` or step-level replacements.
See [composition](../../../../docs/resource-catalog.md) for advanced references
and external-provider assertions.

## Review and deploy

From the repository root, use one configured Site first:

```bash
siteops -w workspaces/iot-operations plan manifests/aio-resources/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-resources/manifest.yaml -l name=<site>
```

The lookup uses the supplied instance name and reads its associated
infrastructure during deployment. Selected Device Registry resources precede
dataflows. An unselected family is skipped.

## Effects, outcome and removal

The deployment creates or updates selected resources using the intended Azure
identity. Read permissions, ARM deployment permissions and the applicable
child-resource write permissions are required. Resource use can incur costs.

Inspect the provider and workload state after deployment. A successfully
applied declaration does not establish data movement. The
[basic-routing example](../../samples/resource-set-basic/README.md) includes a
separate MQTT canary procedure.

Removing an entry or deselecting a set stops applying it. It does not delete
previously created resources. Delete those resources deliberately, accounting
for their dependencies and ownership.

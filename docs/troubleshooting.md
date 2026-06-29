# Troubleshooting

Common issues and solutions.

## Validation errors

### "Site not found"

```
Error: Site 'munich-dev' not found
```

**Cause**: Site file doesn't exist or has wrong name.

**Solution**: Check `sites/` directory. The site basename, relative path, or internal `name:` must match the identifier referenced in the manifest. See [targeting.md](targeting.md) for the identity model.

### "CLI selector matched no sites"

```
Error: CLI selector `-l environment=prdo` matched no sites.
`environment=prdo` requested. Workspace `environment` values: dev, prod, staging.
```

**Cause**: A typo in `-l/--selector`, or the requested label value does not exist on any site.

**Solution**: The diagnostic lists the workspace's actual values for each requested key. Fix the typo or update the site labels. See [targeting.md](targeting.md) for the no-match diagnostic and selector grammar.

### "Template file not found"

```
Error: Template not found: templates/missing.bicep
```

**Cause**: Template path is incorrect or file doesn't exist.

**Solution**: Paths are relative to workspace directory. Verify the path exists.

### "Step references unknown step"

```
Error: Step 'aio-instance' references unknown step 'schema-reg'
```

**Cause**: Output chaining references a step that doesn't exist.

**Solution**: Check step names in manifest match the references in parameter files.

### Site looks wrong after inheritance / overlay

When a site's resolved values disagree with what you expect (wrong location, missing label, an overlay in `sites.local/` or an extras dir not taking effect), preview the fully-resolved shape:

```
siteops -w <workspace> sites <name> --render
```

The output is the post-inherit + post-overlay site as a single YAML doc, with empty `resourceGroup:` omitted for subscription-scoped sites. Use it to verify which file contributed which field before re-running a deploy.

## Deployment errors

### "ResourceGroupNotFound"

**Cause**: Resource group doesn't exist yet.

**Solution**: Either create the resource group first, or use a subscription-scoped step to create it.

### "AuthorizationFailed"

**Cause**: Service principal lacks permissions.

**Solution**: Verify role assignments on the subscription/resource group.

### Partial deployment failure

**Cause**: One step failed, stopping the site deployment.

**Solution**:

1. Check Azure portal for deployment error details
2. Fix the issue
3. Re-run. Bicep deployments are idempotent.

## Arc proxy issues

### "Failed to establish Arc proxy"

**Cause**: Arc cluster unreachable or Cluster Connect not enabled.

**Solution**:

1. Verify cluster is connected: `az connectedk8s show -n <cluster> -g <rg>`
2. Enable Cluster Connect: `az connectedk8s enable-features -n <cluster> -g <rg> --features cluster-connect`

### "Connection refused on port 47021"

**Cause**: Port conflict with another proxy instance.

**Solution**: Site Ops manages ports automatically. If running multiple instances, wait for the first to complete.

## Debug commands

```bash
# Verbose output (shows deployment plan)
siteops -w workspaces/iot-operations validate manifests/aio-install.yaml -v

# Dry run to see exact commands
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml --dry-run

# Show every value's source file (post inherit + overlay merge)
siteops -w workspaces/iot-operations sites <name> -v

# Print the fully-resolved site as YAML
siteops -w workspaces/iot-operations sites <name> --render

# Check Azure CLI authentication
az account show
```

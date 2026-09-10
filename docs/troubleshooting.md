# Troubleshooting

Common issues and solutions.

## Validation errors

### "Site not found"

```
Error: Site file not found: munich-dev (searched sites/)
```

**Cause**: Site file doesn't exist or has wrong name.

**Solution**: Check `sites/` directory. The site basename, relative path, or internal `name:` must match the identifier referenced in the manifest. See [targeting.md](targeting.md) for the identity model.

### "CLI selector matched no sites"

```
Error: CLI selector `-l environment=prdo` matched no sites.
`environment=prdo` requested. Workspace `environment` values: 'dev', 'prod', 'staging'.
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
Error: Step 'aio-instance' references unknown step 'schema-reg' in parameters/p.yaml
```

**Cause**: Output chaining references a step that doesn't exist.

**Solution**: Check step names in manifest match the references in parameter files.

### Site looks wrong after inheritance / overlay

When a site's resolved values disagree with what you expect (wrong location, missing label, an overlay in `sites.local/` or an extras dir not taking effect), preview the fully-resolved shape:

```
siteops -w <workspace> sites <name> --output yaml
```

The output is the resolved site as a single YAML document, with
`resourceGroup` omitted for subscription-scoped sites. To see which file
contributed each value, use `siteops -w <workspace> sites <name> --show-sources`
with the default plain output.

## Deployment errors

### "ResourceGroupNotFound"

**Cause**: Resource group doesn't exist yet.

**Solution**: Either create the resource group first, or use a subscription-scoped step to create it.

### "AuthorizationFailed"

**Cause**: Service principal lacks permissions.

**Solution**: Verify role assignments on the subscription/resource group.

### A `kubectl` step fails with "is forbidden"

**Cause**: The identity has Azure permissions on the cluster resource but no Kubernetes RBAC inside
the cluster. Arc cluster-connect authorizes the connection rather than the operations that travel
over it, so ARM steps succeed while a `kubectl` step is refused by the API server.

**Solution**: Grant the identity named in the error the Kubernetes permissions its step needs, in
the namespace the step writes to. Run the grant from a context that already holds cluster admin,
since the Arc proxy is the connection being refused.

For a development cluster, binding the built-in `admin` role to the target namespace is the quickest
way to continue:

```bash
kubectl create rolebinding siteops-admin --clusterrole=admin --user=<object-id> --namespace=azure-iot-operations
```

Choose the role deliberately before using this beyond a development cluster. `admin` grants read on
Secrets in that namespace, which is where Secret Sync materializes Key Vault values. It also grants
creation of Roles and RoleBindings, which lets a holder widen its own access. Bind a `ClusterRole`
naming the resources your manifests manage instead. A manifest that applies its own Role or
RoleBinding, as the OPC UA sample's simulator does, needs those verbs in the grant. See
[ci-cd-setup.md](ci-cd-setup.md#kubernetes-rbac-for-arc-proxy-operations) for the Azure RBAC
alternative, which keeps the decision in Azure rather than on the cluster.

### Partial deployment failure

**Cause**: One step failed, stopping the site deployment.

**Solution**:

1. Inspect the failure details and affected resources.
2. Correct the issue and review a fresh `siteops plan`.
3. Decide whether to deploy again based on the resources' current state.

The final summary reports every prepared operation, so the steps that never
started after the failure are listed as `not-run`. A new run executes a
fresh plan rather than resuming only unfinished operations.

### Ctrl-C does not return the prompt right away

**Cause**: A stop request reaches waiting code immediately, but a call already
running in a child process is not interrupted. Site Ops waits for it rather
than abandoning scratch files and observed outcomes.

**Solution**: Wait for the call in flight. The bounds are 60 seconds for one
deployment state read, 5 minutes for a deployment submission, and 10 minutes
for a kubectl operation. Pressing Ctrl-C again repeats the same expectation.
An interrupted execution prints its final result and exits `130`. Stopping
locally does not cancel accepted Azure work, so inspect unconfirmed effects
before deciding to deploy again.

A request during preparation lets preparation finish, including any
remaining template compilations. If preparation succeeds, no deployment
operation starts. Preparation failures are still reported normally.
The per-call timeouts above do not bound the whole preparation phase.

### An operation reports "unknown"

**Cause**: The provider's final result could not be confirmed, for example
after lost observation or an incomplete kubectl apply.

**Solution**: Inspect the affected resources before deciding to deploy
again. For an ARM operation, local output names the unconfirmed deployment.
Check it in the Azure portal or use the command for its scope:

```bash
az deployment group show --name <deployment> --resource-group <resource-group> --subscription <subscription>
az deployment sub show --name <deployment> --subscription <subscription>
```

For a kubectl or wait operation, inspect its resources or condition at the
target. See [run-output.md](run-output.md).

## Arc proxy issues

### "Failed to establish Arc proxy"

**Cause**: Arc cluster unreachable or Cluster Connect not enabled.

**Solution**:

1. Verify cluster is connected: `az connectedk8s show -n <cluster> -g <rg>`
2. Enable Cluster Connect: `az connectedk8s enable-features -n <cluster> -g <rg> --features cluster-connect`

### "Connection refused" during Arc proxy setup

**Cause**: The Arc proxy did not become ready on its allocated local port, or
an external process occupied the slot.

**Solution**: Site Ops allocates separate slots for concurrent proxies and
retries explicit port-in-use failures. If the error persists, stop stale
manual proxy sessions or wait for the process using the port to finish, then
retry.

## Debug commands

```bash
# Prepare and show the executable deployment plan
siteops -w workspaces/iot-operations plan manifests/aio-install.yaml

# Emit one publishable JSON plan document
siteops -w workspaces/iot-operations plan manifests/aio-install.yaml --output json --projection publishable

# Emit one publishable JSON run result
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml --output json --projection publishable

# Show the faster compile-free plan shape
siteops -w workspaces/iot-operations plan manifests/aio-install.yaml --describe

# Show every value's source file (post inherit + overlay merge)
siteops -w workspaces/iot-operations sites <name> --show-sources

# Print the fully-resolved site as YAML
siteops -w workspaces/iot-operations sites <name> --output yaml

# Check Azure CLI authentication
az account show
```

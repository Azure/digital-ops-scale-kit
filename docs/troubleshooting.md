# Troubleshooting

Common issues and solutions. For problems while installing Site Ops, see
[installation problems](install-siteops.md#common-problems).

## Deployment commands and published content

### "is a global option. Put it before the command"

```
siteops: error: -w is a global option. Put it before the command: siteops -w PATH plan ...
```

**Cause**: A global option came after the command name. `-w`/`--workspace`,
`--project`, `--approved-source`, `--trust-policy`, `--trusted-root`,
`--extra-sites-dir` and `-v` select content, trust and logging for the whole
invocation, so Site Ops accepts them only before the command. The message
names every misplaced global option.

**Solution**: Nothing was read. Move the options before the command, for
example `siteops -w workspaces/iot-operations plan aio-install`.

### "Manifest not found"

```
Error: Manifest not found. Did you mean 'aio-install'? Run `siteops browse` to list manifests, or use an explicit path.
```

**Cause**: No manifest in the selected workspace or release has that name.
When a manifest name is close, Site Ops suggests it. Redacted output omits the
suggestion.

**Solution**: Use the suggested name, list the manifests with
`siteops browse`, or pass an explicit path such as
`./manifests/custom/manifest.yaml`.

### "Noninteractive deployment requires --yes"

```
siteops: error: Noninteractive deployment requires --yes. Use `plan` to inspect without deploying.
```

**Cause**: `deploy` ran without `--yes` where it cannot ask for confirmation:
JSON output, redacted output, a CI environment (`CI`, `GITHUB_ACTIONS` or
`TF_BUILD` set), or an input or error stream that is not a terminal.

**Solution**: Nothing was read or deployed. Review the plan with
`siteops plan`, then rerun `deploy` with `--yes` when you confirm unattended
execution.

### "Direct content requires explicit Site inputs"

```
siteops: error: Direct content requires explicit Site inputs or --project for configured targets.
```

**Cause**: `plan`, `deploy` or `validate` used `--source SOURCE@RELEASE`
without `--input`, `--input-file`, `--site-file` or `--project`. Example Sites
inside a published release are not deployment targets.

**Solution**: Supply the target with `--input` or `--input-file` as shown in
[guided inputs](guided-inputs.md), pass a complete `--site-file`, or add
`--project DIRECTORY` to use your configured Sites.

### "Workspace pin not found"

```
Error: Workspace pin not found. Use project pin to select a package, or -w to select local content.
```

**Cause**: The selected project has no `siteops.pin`, and the command selected
neither local content with `-w` nor a release with `--source`.

**Solution**: Pin a release with `siteops project pin`, as described in
[operator projects](projects.md#run-project-pin), or select content
explicitly with `-w` or `--source`.

### "GitHub CLI 2.95 or newer is required"

```
Error: GitHub CLI 2.95 or newer is required for detached verification.
```

**Cause**: Site Ops verifies every use of published workspace content with
GitHub CLI, and no `gh` executable is on `PATH`. Related errors report an
unsupported version or a `gh` executable that other users can change.

**Solution**: Install GitHub CLI 2.95 or newer from https://cli.github.com or
your approved channel, in a location that only you or administrators can
change. No GitHub login is needed.

### "The approved source policy has expired"

```
Error: The approved source policy has expired. Inspect it with `siteops source show NAME`. Renew a standard approval with `siteops source enroll NAME`. For a custom policy, remove the name and enroll it again with reviewed trust files.
```

**Cause**: The approval for the source name in `--source NAME@<release>` or
`--approved-source NAME` is older than its policy allows. The error code is
`source.profile-expired`. Approvals created by `siteops source enroll` last
30 days.

**Solution**: Run `siteops source enroll NAME` again to renew it with the same
publisher. For a custom policy, follow
[renew a custom policy](projects.md#use-a-custom-policy).

### "Approved source '...' is not enrolled"

```
Error: Approved source 'official' is not enrolled. Run `siteops source enroll official`. Add --source github:OWNER/REPO to enroll a publisher other than the official one.
Error: Approved source 'official' is not enrolled. Run `siteops source list` to see enrolled names.
```

**Cause**: No source is enrolled under the name used in `--source NAME@<release>`,
`--approved-source NAME` or `siteops source show NAME`, which print the first
message, or in `siteops source remove NAME`, which prints the second. The error
code is `source.profile-missing`. Redacted output omits the name.

**Solution**: Run `siteops source list` to see the enrolled names, or enroll
the official publisher with `siteops source enroll NAME`. See
[approved sources](projects.md#use-an-approved-source).

### "Verified content requires an approved source"

```
Error: Verified content from github:Azure/digital-ops-scale-kit requires an approved source. Approved source 'official' is enrolled for github:Azure/digital-ops-scale-kit: put --approved-source official before the command.
```

**Cause**: A command used a workspace pin, `project pin` or a
`--source github:OWNER/REPO@<release>` locator without an approved source or
trust files. The error code is `project.trust-required`. When a source is
enrolled for that publisher, the message names it. For a locator it suggests
`--source official@<release>` instead. When none is enrolled, it shows the
`siteops source enroll NAME --source github:OWNER/REPO` command. Redacted
output omits the publisher and source names.

**Solution**: Add the named approved source as the message shows, or enroll
one first. Independent `--trust-policy` and `--trusted-root` files are an
alternative.

### "The approved source does not match the selected workspace source"

```
Error: Approved source 'fork' does not match the selected workspace source: it is enrolled for github:example/fork, not github:Azure/digital-ops-scale-kit. Approved source 'official' is enrolled for github:Azure/digital-ops-scale-kit: put --approved-source official before the command.
```

**Cause**: `--approved-source NAME` names a source enrolled for a different
publisher than the project pin or `--source` locator.

**Solution**: Use the approved source the message names, or enroll one for
that publisher.

### "Choose one approved source"

```
Error: Choose one approved source: --source NAME@RELEASE or --approved-source NAME, not both.
```

**Cause**: The command named an approved source twice: in
`--source NAME@<release>` and in global `--approved-source NAME`.

**Solution**: Keep `--source NAME@<release>` for a direct release. Use global
`--approved-source NAME` only with a project pin, `project pin` or a
`--source github:OWNER/REPO@<release>` locator.

### "apply only to a workspace pin or --source content"

```
Error: --approved-source, --trust-policy and --trusted-root apply only to a workspace pin or --source content, not to a local workspace.
Error: --offline-content applies only to a workspace pin, not to a local workspace.
```

**Cause**: Trust options were given with local content selected by `-w` or
local workspace discovery. Local content is not verified, so the options have
no effect there. The same applies to `--offline-content`.

**Solution**: Remove the options, or select verified content with
`--project DIRECTORY` or `--source NAME@<release>`.

### "The existing source approval differs"

```
Error: The existing source approval differs. Remove it before enrolling changed trust.
```

**Cause**: `siteops source enroll NAME` would change the publisher or release
identity that `NAME` already approves. Site Ops never replaces trust
silently.

**Solution**: Inspect the record with `siteops source show NAME`. Enroll the
other publisher under a new name, or run `siteops source remove NAME` first
if you intend to replace it.

## Azure CLI errors

[Azure CLI and az login](install-siteops.md#azure-cli-and-az-login) lists what
needs Azure CLI and which accounts work.

### "Azure CLI (`az`) was not found on PATH"

```
Error: Azure CLI (`az`) was not found on PATH. Install Azure CLI and retry. Step 'aio-instance' needs Azure CLI to submit ARM deployments or read resource tags. Installation instructions are at https://aka.ms/installazurecli.
```

**Cause**: `siteops plan` or `siteops deploy` selected deployment, wait or
`kubectl` steps, and no `az` executable is on `PATH`. For a `kubectl` step the
message says the step needs Azure CLI and its connectedk8s extension. JSON
plans report `capability.arm-control-plane.missing` or
`capability.arc-proxy.missing` with the same fix.

**Solution**: Nothing ran. Install Azure CLI from
https://aka.ms/installazurecli and open a new shell. For `kubectl` steps, also
run `az extension add --name connectedk8s`. Then rerun the command.

### "The Azure CLI connectedk8s extension is not installed"

```
Error: The Azure CLI connectedk8s extension is not installed. Step 'opc-plc-simulator' needs Azure CLI and its connectedk8s extension to reach the cluster through `az connectedk8s proxy`. Run `az extension add --name connectedk8s`, then rerun the command.
```

**Cause**: A selected `kubectl` step reaches its cluster through
`az connectedk8s proxy`. Before anything runs, `siteops plan` and
`siteops deploy` ask Azure CLI whether the extension is installed, and it is
not. The `kubectl` steps are blocked. When Azure CLI is set to install
extensions without a prompt, through
`az config set extension.use_dynamic_install=yes_without_prompt` or the
`AZURE_EXTENSION_USE_DYNAMIC_INSTALL` environment variable, the plan proceeds
and Azure CLI installs the extension on first use instead.

**Solution**: Nothing ran. Run `az extension add --name connectedk8s`, then
rerun the command.

### "Azure CLI (`az`) with Bicep is required for Bicep template steps"

```
Azure CLI (`az`) with Bicep is required for Bicep template steps. Install Azure CLI from https://aka.ms/installazurecli, run `az bicep install`, then rerun the command.
```

**Cause**: `siteops plan` or `siteops deploy` compiles Bicep templates through
Azure CLI, and its Bicep compiler is missing or could not run. Redacted output
and publishable JSON print this text. Local output names the steps and the
observed cause.

**Solution**: Nothing ran. Run `az bicep install`, then rerun the command.

### "The target subscription is not visible to the account signed in to Azure CLI"

```
The target subscription is not visible to the account signed in to Azure CLI. No deployment was started. Run `az account list` to see the subscriptions this account can use, or run `az login` with an account that can access the subscription. Azure CLI reported: ERROR: Subscription '<subscription>' not found. Check the spelling and casing and try again.
```

**Cause**: The Site's `subscription` is not among the subscriptions of the
account signed in to Azure CLI. Azure CLI rejects the request before sending
it, so the step fails rather than reporting an unconfirmed result. A reported
`Profile has tenant-level account only` means the account can sign in to the
tenant but can use no subscription. A wait step reports the same cause.

**Solution**: Run `az account list` and look for the Site's subscription. If
it is missing, sign in with an account that can access it, assign the service
principal a role on it, or correct `subscription` in the Site. Then rerun the
command.

### "Azure CLI 2.70.0 or newer is required"

```
Error: Azure CLI 2.69.0 was found. Azure CLI 2.70.0 or newer is required. Run `az upgrade`, then retry.
```

**Cause**: `plan` or `deploy` selected steps that use Azure CLI, and the
installed Azure CLI is older than 2.70.0. Nothing was submitted.

**Solution**: Run `az upgrade`, or install a current release from
https://aka.ms/installazurecli, then rerun the command.

### "Template parameters have no value"

```
Template parameters have no value, so the deployment was rejected before any resource changed. Add values for them to the step's parameter files or the Site's parameters, then retry. Azure CLI reported: WARNING: Missing input parameters: location
```

**Cause**: A template parameter without a default has no value in the step's
parameter files or the Site's `parameters`. Site Ops runs Azure CLI without
prompts, so Azure CLI names the missing parameters and Azure Resource Manager
rejects the template before any resource changes.

**Solution**: Add the named parameters to the step's parameter files or the
Site's `parameters`, review the plan with `siteops plan`, then rerun the
command.

### "Input '...' read failed"

```
Error: inputs.resource.not-logged-in: Input 'cluster' read failed. The selected Azure CLI session is not signed in. Run `az login`, then retry.
```

**Cause**: `inputs --read-resources`, or `plan` and `deploy` with a resource
ID answer, read the resource through Azure CLI before planning. The code
after `inputs.resource.` names the reason, and the message ends with its fix.

| Code | Fix |
|---|---|
| `tool-missing` | Install Azure CLI from https://aka.ms/installazurecli and make sure `az` is on `PATH`. |
| `not-logged-in` | Run `az login`. |
| `subscription-missing` | Run `az account list`, or sign in with an account that can access the subscription. |
| `forbidden` | Ask for a role that grants read access to the resource, such as Reader. |
| `not-found` | Check the resource ID. |
| `timeout` | Check network access to Azure. |

**Solution**: Nothing was planned or deployed. Apply the fix, then rerun the
same command.

## Typed inputs

### "contain an unknown input"

```
Error: Inline inputs contain an unknown input 'clster'. Did you mean 'cluster'?
```

**Cause**: An `--input NAME=VALUE` name, or a name under `values:` in an
`--input-file` (reported as `Input values contain ...`), is not declared by
the manifest. Redacted output omits the name and the suggestion.

**Solution**: Use the suggested name, or run `siteops inputs MANIFEST` to list
the declared inputs.

### "already exists. Choose a new file name"

```
Error: ./aio-inputs.yaml already exists. Choose a new file name.
Error: The directory for ./answers/aio-inputs.yaml does not exist.
```

**Cause**: `inputs --example FILE` or `inputs --save-site FILE` never replaces
an existing file or creates a directory. Redacted output omits the path.

**Solution**: Choose a new file name or an existing directory.

## Validation errors

### "Site files not found for manifest"

```
Error: Site files not found for manifest 'aio-install': munich-dev. Create those Site YAML files under `sites/`, or fix the Site names listed in the manifest.
```

**Cause**: A name in the manifest's `sites:` list matches no Site file.
`siteops sites NAME` reports `Error: No Sites matched the selector: name=NAME`
for the same cause.

**Solution**: Check `sites/` directory. The Site basename, relative path, or internal `name:` must match the identifier referenced in the manifest. See [targeting.md](targeting.md) for the identity model.

### "CLI selector matched no Sites"

```
Error: CLI selector `-l environment=prdo` matched no Sites. `environment=prdo` requested. Workspace `environment` values: 'dev', 'prod', 'sample', 'staging'.
```

**Cause**: A typo in `-l/--selector`, or the requested label value does not exist on any Site.

**Solution**: The diagnostic lists the workspace's actual values for each requested key. Fix the typo or update the Site labels. See [targeting.md](targeting.md) for the no-match diagnostic and selector grammar.

### "Template not found"

```
Error: Template not found: templates/missing.bicep
```

**Cause**: Template path is incorrect or file doesn't exist.

**Solution**: Paths are relative to workspace directory. Verify the path exists.

### "references unknown step"

```
Error: Step 'aio-instance' references unknown step 'schema-reg' in parameters/p.yaml
```

**Cause**: Output chaining references a step that doesn't exist.

**Solution**: Check step names in manifest match the references in parameter files.

### Site looks wrong after inheritance / overlay

When a Site's resolved values disagree with what you expect (wrong location, missing label, an overlay in `sites.local/` or an extras dir not taking effect), preview the fully-resolved shape:

```
siteops -w <workspace> sites <name> --output yaml
```

The output is the resolved Site as a single YAML document, with
`resourceGroup` omitted for Sites without a resource group. To see which file
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

**Cause**: One step failed, stopping the deployment to that Site.

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
An earlier log line names the cause, as described in the entries below.

**Solution**:

1. Verify cluster is connected: `az connectedk8s show -n <cluster> -g <rg>`
2. Enable Cluster Connect: `az connectedk8s enable-features -n <cluster> -g <rg> --features cluster-connect`

### "Arc proxy needs the Azure CLI connectedk8s extension"

```
Arc proxy needs the Azure CLI connectedk8s extension. Run `az extension add --name connectedk8s`, then retry. Azure CLI reported: ERROR: The command requires the extension connectedk8s. ...
```

**Cause**: `az connectedk8s proxy` exited because the extension is missing.
Site Ops gives Azure CLI no input, so Azure CLI cannot offer to install the
extension and exits instead of waiting.

**Solution**: Run `az extension add --name connectedk8s`, then rerun the
command. `siteops plan` reports the same condition before anything runs.

### "Arc proxy did not open local port"

```
Arc proxy did not open local port 47021 within 180s. Check network access to Azure, that the cluster has cluster connect enabled, and that the account signed in to Azure CLI can reach it.
```

**Cause**: `az connectedk8s proxy` kept running but never accepted
connections on its local port. On first use it also downloads its proxy
binary, which needs network access.

**Solution**: Check the cluster with
`az connectedk8s show -n <cluster> -g <rg>`, enable cluster connect as shown
above, and confirm that the account signed in to Azure CLI can access the
cluster resource. Then retry.

### "Arc proxy opened local port ... but did not become responsive"

```
Arc proxy opened local port 47021 but did not become responsive within 180s. Check that the cluster is reachable and that the account signed in to Azure CLI can use cluster connect.
```

**Cause**: The proxy listened locally, but `kubectl` could not reach the
cluster's API server through it before the deadline.

**Solution**: Confirm the cluster is connected and its agents are healthy with
`az connectedk8s show -n <cluster> -g <rg>`. Check that the account can use
cluster connect on the cluster, then retry.

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
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml

# Emit one publishable JSON plan document
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml --output json --projection publishable

# Show the faster compile-free plan shape
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml --describe

# Show every value's source file (post inherit + overlay merge)
siteops -w workspaces/iot-operations sites <name> --show-sources

# Print the fully resolved Site as YAML
siteops -w workspaces/iot-operations sites <name> --output yaml

# Check Azure CLI authentication
az account show
```

For an explicitly confirmed deployment, follow the
[run output guide](run-output.md). Deployment changes the selected targets,
so use a bounded selector and review its plan before unattended execution.

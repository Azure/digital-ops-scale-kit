# Migrating between Scale Kit releases

Use this guide when upgrading the Scale Kit you run. Sections are newest
first. Apply the sections between your current and target releases,
starting with the oldest applicable release. From a release before
v1.0.0b6, also apply the migration notes in the
[v1.0.0b4 release notes](https://github.com/Azure/digital-ops-scale-kit/releases/tag/v1.0.0b4)
and review the
[v1.0.0b5 release notes](https://github.com/Azure/digital-ops-scale-kit/releases/tag/v1.0.0b5).
The v1.0.0b4 and v1.0.0b3 sections correct and supplement those releases'
notes.

To upgrade deployed Azure IoT Operations or Kubernetes instead, see
[aio-releases.md](aio-releases.md) and the `aio-upgrade` and
`aksee-upgrade` entries.

For release summaries, see the
[release notes](https://github.com/Azure/digital-ops-scale-kit/releases).
The [site](site-configuration.md) and [manifest](manifest-reference.md)
references describe the current configuration rules.

## Before you migrate

With the release you run now, list your Sites and validate each manifest you
deploy before changing anything:

```bash
siteops -w <workspace> sites
siteops -w <workspace> validate <manifest>
```

Resolve any reported Site loading or validation errors. Then install the new
release, apply the sections below, and plan each manifest you deploy:

```bash
siteops -w <workspace> plan <manifest> -l <selector>
```

`plan` arrives with v1.0.0b7, so run it with the new engine.

## To v1.0.0b8

These changes affect how you install or run Site Ops after v1.0.0b7.

| If you... | What to change |
|---|---|
| Install Site Ops on Windows with the PowerShell bootstrap | Review the [saved user PATH](#windows-user-path). |
| Match artifact verification error text, or read `source` from failed `browse --output json` | Review [artifact verification messages](#artifact-verification-messages). |

### Windows user PATH

The PowerShell bootstrap adds uv's command directory, normally
`%USERPROFILE%\.local\bin`, to your user PATH with `uv tool update-shell`,
so new terminals find `siteops`. It lists this change with the others
before asking for consent. It leaves the machine PATH and the order of
other entries unchanged. You no longer need to run `uv tool update-shell`
yourself.

To keep your saved PATH unchanged, add `-NoModifyPath` to the bootstrap
invocation. The bootstrap then prints the commands that add the directory
for the current terminal and for new terminals. It also leaves the PATH
unchanged, and says why, when the saved PATH is plain text that contains
`%` or the command directory contains `%`.

If another `siteops` would run first in a new terminal, such as an older
pip installation, the bootstrap prints a line that starts with
`Warning: new terminals run`. Remove that command with the tool that
installed it.

### Artifact verification messages

A rejected package proof now names the check that failed, for example
`The artifact proof names a runner class that the approved source does not allow.`
These messages replace
`The verified certificate does not satisfy publisher/source policy.`, and
`The detached artifact proof did not pass verification.` remains only when
the reason cannot be named. The error code stays `artifact.invalid`, so
match the code rather than the text.
[Troubleshooting](troubleshooting.md#the-artifact-proof-names--or-the-detached-proof-does-not-verify-this-artifact)
lists each message.

When `browse` fails for a pinned project, its JSON `source` names the pinned
package, with `verification` set to `failed` when verification rejected it.
It previously reported `local` and `not-performed`.

## To v1.0.0b7

These changes affect workspaces, scripts and pipelines that worked with
v1.0.0b6. Features that need no change to existing use, such as typed Site
inputs, operator projects, verified published content and the device and asset
resource areas, are described in the
[release notes](https://github.com/Azure/digital-ops-scale-kit/releases) and
the [documentation index](README.md).

Start with the changes that affect your workflow:

| If you... | What to change |
|---|---|
| Installed Site Ops with `pip install -e .` | [Run it again](#site-ops-installation) after updating your clone. |
| Check the installed release with `siteops --version` | Expect the [engine version](#reported-version), `1.0.0b1`, with a build suffix for an installed release. |
| Deploy with Azure CLI older than 2.70.0 | Run `az upgrade`. See [Azure CLI version](#azure-cli-version). |
| Run `plan` or `deploy` for Bicep or kubectl steps | Install the [local tools](#local-tool-checks) those steps use. |
| Run `deploy` from a script or pipeline | Add [`--yes`](#deployment-confirmation). |
| Preview with `deploy --dry-run` or `validate --plan` | Use [`siteops plan`](#plan-replaces-preview-options). |
| Use `sites --render`, read plain `sites` output or run `sites` in CI | Review [site inspection](#inspect-sites). |
| Capture `deploy` output or match text in plain output or error messages | Review [plain output](#plain-output) and [error messages](#error-messages). |
| Capture validation JSON or publish pipeline diagnostics | Review [diagnostic output](#diagnostic-output) and copy the matching renderer with your templates. |
| Repeat an option such as `-w`, or shorten option names | Review [option spelling](#option-spelling). |
| Reference shipped manifests, partials or dataflow sets by path | Update [workspace content paths](#workspace-content-paths). |
| Pass a manifest filename without a directory | Review [manifest names and paths](#manifest-names-and-paths). |
| Rely on the workspace default AIO release | Review the [2609 default](#default-aio-release). |
| Pin AIO 2603 through 2606 and use the OPC UA connector | Review the [connector certificate settings](#connector-certificate-settings). |
| Select dataflow sets or read catalog step outputs | Update [resource sets](#resource-sets) and [catalog step outputs](#catalog-step-outputs). |
| Author sites, manifests or parameter files | Review [preparation checks](#preparation-checks), [empty site mappings](#empty-site-mappings) and [parameter file selection](#parameter-file-selection). |
| Find Azure deployments by name in a script | Review [Azure deployment names](#azure-deployment-names). |
| Write `when: ""` on a step or include | Omit `when:` or set it to `null`. See [empty conditions](#empty-conditions). |
| Use the Azure Pipelines templates | Choose an [engine selection](#azure-pipelines-templates). |
| Reference the Azure Pipelines templates from another repository | Set [`templateRepository`](#template-repositories-and-copied-templates) to the repository resource alias. |
| Copy the Azure Pipelines templates into your repository | Copy one [matching set](#template-repositories-and-copied-templates) from the same release. |
| Set `siteopsSource` in your pipeline | Keep the template `ref` on [the same release](#template-repositories-and-copied-templates). |
| Use `SITE_OVERRIDES` in Azure Pipelines | Define the variable and review [override validation](#site-overrides). |
| Pass absolute paths to the deployment pipelines | Use [relative paths](#pipeline-checkout-paths). |
| Queue `.pipelines/deploy.yaml` with `siteopsSource` | Set `release` and `sourceCommit` instead. See [Azure Pipelines templates](#azure-pipelines-templates). |
| Set `serviceConnections` or `secretGroups` in the Azure Pipelines deploy or integration pipeline | Move the names into the [Environment settings block](#azure-pipelines-environment-settings). |
| Call the reusable GitHub Actions workflow | Pass the selector [as a secret](#github-actions-workflows). |
| Run Windows batch launchers such as `az.cmd` | Review [literal tool arguments](#windows-tool-arguments). |
| Clean up `.siteops/tmp` | Review [temporary files](#temporary-files). |
| Call the engine from Python | Update [internal callers](#internal-python-callers). |

### Site Ops installation

After updating a clone, run `pip install -e .` again, because the engine now
also depends on `packaging`. In Azure Pipelines, `siteopsSource` installs Site
Ops with pip from the source you name, such as a tagged Git source. See
[Azure Pipelines templates](#azure-pipelines-templates).

You can also install the engine built for a release, using the command from
that release's notes:
[native `uv tool install`](install-siteops.md#install-the-release-wheel) of the
release wheel, or the [verified bootstrap](install-siteops.md#bootstrap-from-https).
Deactivate or uninstall the pip installation first, then confirm the version
with `siteops --version`.

### Reported version

`siteops --version` reports the Site Ops engine version, which is separate
from the Scale Kit release. The v1.0.0b7 release keeps the engine at
`1.0.0b1`, and an engine installed from that release adds a build suffix,
for example:

```text
siteops 1.0.0b1+build.12345.1.gabcdef123456
```

An installation from source, such as a Git pip source or an editable clone at
the v1.0.0b7 tag, reports `siteops 1.0.0b1`. See [Use the installed CLI](install-siteops.md#use-the-installed-cli).

### Azure CLI version

v1.0.0b6 accepted any Azure CLI version. `plan` and `deploy` now require Azure
CLI 2.70.0 or newer for steps that use Azure CLI, the same minimum that the
connectedk8s and azure-iot-ops extensions require. An older version blocks
those steps before anything is submitted:

```text
Error: Azure CLI 2.69.0 was found. Azure CLI 2.70.0 or newer is required. Run `az upgrade`, then retry.
```

`validate` and `browse` do not check Azure CLI. Deployments now run with
`--no-prompt`, so a template parameter without a value fails at once instead
of waiting for console input.

### Local tool checks

v1.0.0b6 found a missing tool when the step that needed it ran. `plan` and
`deploy` now check the tools that the selected steps use before anything is
submitted:

| Steps | Local tools |
|---|---|
| Deployment and wait steps | Azure CLI 2.70.0 or newer |
| Bicep template steps | Azure CLI with Bicep, installed with `az bicep install` |
| kubectl steps | kubectl, and the Azure CLI connectedk8s extension, installed with `az extension add --name connectedk8s` |

A missing tool blocks the steps that use it, which report
`A required local tool is unavailable`. The diagnostic names the step and the
fix, for example:

```text
Error: The Azure CLI connectedk8s extension is not installed. Step 'apply' needs Azure CLI and its connectedk8s extension to reach the cluster through `az connectedk8s proxy`. Run `az extension add --name connectedk8s`, then rerun the command.
```

`validate` and `plan --describe` run without these tools. See
[Azure CLI errors](troubleshooting.md#azure-cli-errors) for the Azure CLI
messages.

### Deployment confirmation

`deploy` now prints the prepared plan and asks `Deploy this plan? [y/N]` before
it submits anything. An answer other than `y` or `yes` reports
`Deployment cancelled. No operations were submitted.` and exits with code 130.

Without `--yes`, `deploy` stops with usage error 2 before reading content when
it has no interactive terminal, when `--output json` is selected, when output
redaction is enabled, or when `CI`, `GITHUB_ACTIONS` or `TF_BUILD` is set:

```text
siteops: error: Noninteractive deployment requires --yes. Use `plan` to inspect without deploying.
```

Add `--yes` to unattended deployments. It does not bypass validation or target
prerequisites. The shipped GitHub Actions workflow and Azure Pipelines template
already pass it.

During execution, Ctrl-C stops new operations and waits for calls already
running to return or reach their own timeout. Work Azure already accepted is
not cancelled. An interrupted run exits with code 130.

### Plan replaces preview options

The preview options were removed, not aliased:

| Removed invocation | Replacement |
|---|---|
| `deploy <manifest> --dry-run` | `plan <manifest>` |
| `validate <manifest> --plan` | `plan <manifest> --describe` |

Each removed option reports `unrecognized arguments` (argparse usage error 2).
`plan` validates, compiles templates, checks the local tools the selected
operations need and prepares every value known before execution, without
deployment writes. `plan --describe` shows the plan without compiling. Both
need targets. Bare `validate` remains the structural check for a library
manifest without targets.

### Inspect sites

`sites --render` was removed and reports `unrecognized arguments: --render`.
Use one of these instead:

```bash
siteops -w <workspace> sites <name> --output yaml
siteops -w <workspace> sites --output json
```

YAML writes one document per site. JSON always writes an array.

Plain `sites` output lists several Sites one per line with name, location,
resource group and labels. Use `sites NAME`, `--show-sources` or
`--output yaml` for full resolved values. Plain values use YAML spelling,
such as `false` and `[basic-routing]`.

When `GITHUB_ACTIONS` or `TF_BUILD` is set, or `SITEOPS_REDACT_OUTPUT` enables
redaction, `sites` no longer prints a masked listing. It reports
`Site inspection output is private` and exits with code 1. Set
`SITEOPS_REDACT_OUTPUT=0` only for an authorized private destination. See
[inspection output details](site-configuration.md#inspection-output-details).

### Plain output

Plain output uses ASCII markers: `+` succeeded or runs, `x` failed or blocked,
`-` skipped or not run and `?` unconfirmed. Private local `validate` reports
`+ Manifest is valid: NAME` with the Manifest name rather than its file name.
Redacted success reports `+ Manifest structure and static references are valid.`
Validation errors use stderr. Scripts should test the exit code rather than
this text.

`deploy` progress and its confirmation prompt use stderr, and the final
summary uses stdout. Capture both streams to log a deployment. For
automation, use `--output json` and the exit code.

`deploy` progress and summary lines use new text:

| v1.0.0b6 | v1.0.0b7 |
|---|---|
| `[Phase 1] Subscription-scoped steps: N subscription(s)` | `[Phase 1] Steps at subscription scope: N Sites` |
| `[Phase 2] Resource group-scoped steps: N site(s)` | `[Phase 2] Steps in resource groups: N Sites` |
| `[Parallel] Deploying to N sites (N concurrent)` | `[Parallel] Deploying to N Sites (N concurrent)` |
| `Deployment Summary` | `Deployment summary` |
| `Total: N succeeded, N failed (N sites)` and `Duration: Ns` | `Result: all deployment operations succeeded in Ns`, then `Sites:` and `Operations:` counts |

A run without steps at subscription scope starts with
`[Execution] Prepared Sites: N Sites`. With `--yes`, `deploy` reports
`Prepared N Sites for deployment.` before it runs. `plan --describe` notes
`Plan shape only.` above its Sites. See [run output](run-output.md) and
[plan output](plan-output.md).

### Error messages

Plain errors start with `Error:`. `validate` reports
`Error: Validation failed with N error(s):`. Plan and run
diagnostics are labeled `Error:` and `Warning:`. JSON output keeps the codes.
Messages capitalize Site, for example `No Sites matched the specified criteria`.

A global option after the command still fails with usage error 2. Instead of
`unrecognized arguments`, it names the option and where it goes:

```text
siteops: error: -w is a global option. Put it before the command: siteops -w PATH plan ...
```

### Diagnostic output

Redacted output preserves content causes such as `content.duplicate-key` and
`content.unknown-field`, with fixed guidance and documentation links. The
[diagnostic index](diagnostics.md) explains each code without publishing values,
paths or provider messages.

Use `validate --output json --projection publishable` to capture a
`ValidationResult`. A reusable Manifest without Sites remains valid.
JSON keeps one diagnostic record per occurrence. Human output can group
identical public records as `N occurrences`, without changing record counts
or private context.

Run JSON reports `summary.operations.failureDiagnostics` beside `counts` and
`total`. Groups count logical operations, not provider attempts or nested ARM
resources. Failed and unknown remain separate. Neither JSON projection
includes raw provider messages.

The pipeline templates capture publishable JSON and render it with
`scripts/render-siteops-output.py`. Use the templates, helpers and engine
from the same release. An unsupported engine invocation that exits `2`
without a result prints
`Site Ops engine and template releases do not match. Use matching engine, setup, templates and helpers.`
and keeps exit `2`, without probing or retrying. See
[CI diagnostic output](ci-cd-setup.md#ci-diagnostic-output).

### Option spelling

v1.0.0b6 kept the last value of a repeated option and accepted a unique
prefix of an option name, such as `--sel` for `--selector`. Both now stop
with usage error 2:

```text
siteops: error: argument -w/--workspace: may be supplied only once
siteops: error: unrecognized arguments: --sel
```

Give options that take one value, such as `-w`, `--project`, `--source`,
`--input-file` and `--parallel`, only once, and spell option names in full.
Options documented as repeatable, such as `-l`, accept several values.

### Workspace content paths

Core entries now use a named directory with `manifest.yaml` and an operator
guide, and dataflow sets moved to `resource-sets/`:

| v1.0.0b6 path | Current path |
|---|---|
| `manifests/aio-install.yaml` | `manifests/aio-install/manifest.yaml` |
| `manifests/aio-upgrade.yaml` | `manifests/aio-upgrade/manifest.yaml` |
| `manifests/aio-resources.yaml` | `manifests/aio-resources/manifest.yaml` |
| `manifests/secretsync.yaml` | `manifests/secretsync/manifest.yaml` |
| `manifests/aksee-bootstrap.yaml` | `manifests/aksee-bootstrap/manifest.yaml` |
| `manifests/aksee-upgrade.yaml` | `manifests/aksee-upgrade/manifest.yaml` |
| `manifests/_<name>.yaml` | `manifests/_partials/_<name>.yaml` |
| `parameters/dataflows/<set>.yaml` | `resource-sets/dataflows/<set>.yaml` |

Update custom commands, workflow and pipeline manifest selections, and fixed
parameter source paths. Recompute relative `include` paths from each including
file's directory. Sample directories keep their paths.

There are no forwarding manifests at the old paths. An old path reports
`Manifest not found.` and suggests the nearest current manifest, for example
`Did you mean 'aio-install' (manifests/aio-install/manifest.yaml)?`. A file
move does not delete or recreate Azure resources.

### Manifest names and paths

In v1.0.0b6, a manifest argument without a directory, such as `install.yaml`,
was joined to the workspace path. It is now looked up among the workspace's
manifest names and root filenames, so `plan aio-install` also works. If a name
and a filename identify different files, the command reports
`Manifest selection is ambiguous`. With an incomplete inventory, it reports
`Name lookup requires a complete inventory`.

Prefix a filename with `./`, for example `plan ./install.yaml`, to select the
file directly. A path that contains a directory, such as
`manifests/aio-install/manifest.yaml`, is still resolved against the
workspace.

### Default AIO release

`sites/base-site.yaml` now sets `aioRelease: "2609"`, where v1.0.0b6 set
`"2607"`. Sites that do not pin `properties.aioRelease` target 2609 on their
next deploy. Pin `properties.aioRelease: "2607"` to stay on the v1.0.0b6 release,
or `properties.aioRelease: "2608"` to select that release.
The guided installation and Secret Sync API profile also default to 2609.
Use the [upgrade operation](../workspaces/iot-operations/manifests/aio-upgrade/README.md)
for in-place upgrades of existing instances instead of rerunning installation.
Moving off 2607 adds the OPC UA connector template unless OPC UA is disabled.
Install and upgrade skip the template when the feature mode is `Disabled`.
This includes the legacy `connectors` alias when `opcua` is absent.
The canonical `opcua` feature takes precedence over `connectors`.
See [AIO releases](aio-releases.md).

### Connector certificate settings

On AIO 2603 through 2606, installation and upgrade now set the OPC UA
connector's `connectors.values.securityPki.applicationUri`. On 2605 and 2606
they also set `connectors.values.securityPki.subjectName`. Both match the
values 2607 and later use. Upgrade keeps a value already set. If an OPC UA
server trusts the connector by other values, set them in
`aioConfigurationOverrides`.

### Resource sets

**Resource-set selections are ordered lists.** Replace each scalar set name
with a one-item list:

```yaml
# Before
properties:
  resourceSets:
    dataflows: site-telemetry

# After
properties:
  resourceSets:
    dataflows:
      - site-telemetry
```

Remove `none`. Omit an area for no selection, or use `[]` when a child site
must clear a list inherited from its parent. `parameters/dataflows/none.yaml`
has no replacement. The old scalar and `none` forms report
`to the legacy scalar`, naming the site and the ordered list to write instead.

**A directly attached declaration names its collections.** A plain manifest
parameter path that carries dataflow resources must become the typed object
form, even when it loads one fixed file. Otherwise planning reports that the
source contributes a governed collection without listing it in `collections`.

```yaml
# Before
parameters:
  - samples/my-sample/dataflows.yaml

# After
parameters:
  - path: samples/my-sample/dataflows.yaml
    collections: [dataflowEndpoints, dataflowProfiles, dataflows]
```

Update a custom catalog manifest from scalar path interpolation and the
`none` comparison:

```yaml
# Before: manifests/custom.yaml
parameters:
  - "parameters/dataflows/{{ site.properties.resourceSets.dataflows }}.yaml"
steps:
  - include: _dataflows.yaml
    when: "{{ site.properties.resourceSets.dataflows != 'none' }}"

# After: manifests/custom/manifest.yaml
parameters:
  - path: "resource-sets/dataflows/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.dataflows }}"
    collections: [dataflowEndpoints, dataflowProfiles, dataflows]
steps:
  - include: ../_partials/_dataflows.yaml
    when: "{{ site.properties.resourceSets.dataflows }}"
```

The included family partial contributes its `parameterCompositions` contract.
See [Manifest reference](manifest-reference.md) when a custom partial needs to
declare one directly.

**Dataflow references are checked before deployment.** Every `endpointRef` and
`profileRef` must name an endpoint or profile from a selected source, the
`default` endpoint and profile that AIO creates, or an `_siteops.external`
assertion. Otherwise validation and planning report `does not resolve to`.
Declaring an endpoint or profile named `default` reports
`is provider-owned and cannot be written by a resource set`. See
[Resource catalog](resource-catalog.md#compose-sets-by-resource-identity).

### Catalog step outputs

**The `dataflow-resources` step keeps a parameter-only interface.** If a custom
manifest reads its outputs, remove references to `endpointNames`,
`profileNames`, `dataflowNames`, `dataflowProfileRefs`, or `apiVersion`.

Use `siteops plan <manifest> --describe` to inspect the effective composition
before deployment. Read the deployed resources from Azure or their projected
custom resources when verifying provider state.

### Empty site mappings

**A mapping key with no value no longer erases its inherited mapping.** A bare
`labels:`, `properties:`, or `parameters:` is normalized as an empty mapping
before inheritance merge. Use an explicit field value, such as
`resourceSets.dataflows: []`, when a child needs to clear supported state.

### Empty conditions

v1.0.0b6 treated `when: ""` as no condition and ran the step. `validate`,
`plan` and `deploy` now report `Invalid 'when' condition syntax on step 'NAME'`,
or `on include of 'FILE'` for an include. For a step that always runs, omit
`when:` or set it to `null`.

### Parameter file selection

**A site-selected parameter path stays within the workspace.** A parameter path containing a
template must resolve to a relative path with no `..` segments, and the resulting file must remain
inside the workspace. Keep the selectable value to a file or subdirectory name under the manifest's
intended parameter directory.

### Preparation checks

In v1.0.0b6, `deploy` did not run `validate`. `plan` and `deploy` now share its
checks before compiling templates or writing resources, and stop on problems
that v1.0.0b6 `deploy` warned about or left to Azure:

- **Parameter files:** every attached file must exist. A missing fixed file
  reports `Manifest parameter file not found` or `Parameter file not found`,
  where v1.0.0b6 `deploy` logged a warning and continued. A file that holds a
  scalar or an array reports `must contain a mapping`.
- **Targets:** a manifest selector that matches no Site reports
  `No Sites matched the specified criteria` and exits with code 1. A manifest
  without steps reports `Manifest has no steps defined`. In both cases
  v1.0.0b6 printed `Nothing to deploy` and exited with code 0.
- **Template inputs:** a template parameter that is not nullable and has no
  default must receive a value. Otherwise preparation reports
  `missing required template parameter` before anything is submitted.
- **Kubectl inputs:** local files must exist inside the workspace, and URLs
  must use HTTPS. A path outside the workspace reports
  `Kubectl file must stay within the workspace`.

### Azure deployment names

When `<manifest>-<Site>-<step>` exceeds 49 characters, its Azure deployment
name now keeps the first 38 characters and adds a 10 character SHA-256
suffix, instead of 42 characters and a 6 character MD5 suffix. Update scripts
that look up these deployments by name. Resource names are unchanged.

### Azure Pipelines templates

With an empty `siteopsSource`, the v1.0.0b6 templates installed the checkout
with `pip install -e .`. They now install a released engine through the
verified bootstrap, which needs an engine selection. Without one, setup
reports:

```text
Site Ops setup: Select a tagged GitHub template repository, an explicit release and sourceCommit, or siteopsSource. An unpinned checkout is not an engine selection.
```

Choose one route:

- Run the templates from a GitHub repository checkout at a release tag, either
  the pipeline's own repository or the repository resource named by
  `templateRepository`.
- Set `release` and `sourceCommit`, adding `repository` for a fork.
- Set `siteopsSource` to an explicit pip source in reviewed YAML.
- Set `installDev: true` on `setup-siteops.yaml` for a contributor checkout.

These routes are mutually exclusive. The release route requires a Linux agent.
If you maintain copied templates, update their scripts and helper files
together. See the
[consumer example](ci-cd-setup.md#reference-the-deployment-template-from-another-repository).

`.pipelines/deploy.yaml` no longer offers `siteopsSource` when you queue a
run, and Azure Pipelines rejects a run request that still sets it. Select the
engine with the `release` and `sourceCommit` run parameters, or queue the
pipeline from a release tag. A custom engine source belongs in reviewed YAML,
as the `siteopsSource` parameter of `templates/siteops-deploy.yaml`.

`dryRun` now prepares an executable plan with `plan` rather than running
`deploy --dry-run`, and deployment passes `--yes`.

### Template repositories and copied templates

When your pipeline references the stage templates from another repository,
set `templateRepository` to that repository resource's alias. For a reference
ending in `@scalekit`, pass `templateRepository: scalekit`. The templates run
helper scripts from the repository that holds them, and the default `self`
looks in your own checkout. `siteopsSource` installs the engine but does not
supply those helpers. Without `templateRepository`, a release engine
selection stops while installing Site Ops, and a `siteopsSource` selection
stops after it:

```text
Release installation requires the matching automation scripts. Select templateRepository or copy the matching helpers.
The templates need their matching automation scripts. Set templateRepository to the repository resource that holds the templates, or copy the matching helpers.
```

If you copy the templates into your repository, copy one matching set from
the same release and replace the whole set when you upgrade. See
[templates your pipeline uses](ci-cd-setup.md#templates-your-pipeline-uses)
for the files each route needs.

When you set `siteopsSource`, use the same release as the template `ref`. A
template older than its engine can report success for a command the engine
rejected. With v1.0.0b2 or v1.0.0b3 templates, for example, the validation
step succeeds even though validation never ran.

### Site overrides

Define `SITE_OVERRIDES` in Azure Pipelines, empty when you do not use
overrides. An undefined variable reaches the template as the literal text
`$(SITE_OVERRIDES)` and is not treated as empty.

Invalid JSON stops with
`Site overrides could not be masked. Check the JSON locally.` Set each field
through one path, so combine `parameters.brokerConfig` and
`parameters.brokerConfig.memoryProfile` into one mapping. Empty field path
segments, strings containing NUL and input longer than 1,048,576 characters
also fail.

### Pipeline checkout paths

Pass workspace and Site file paths relative to your checkout, and manifest
paths relative to the workspace. Absolute paths, leading backslashes and
Windows drive paths now fail input validation in the Azure Pipelines
templates. The reusable GitHub Actions deployment workflow applies the same
rule to its workspace and manifest inputs. Local CLI commands are unaffected.

### Azure Pipelines environment settings

In v1.0.0b6, `.pipelines/deploy.yaml` and `.pipelines/integration-test.yaml`
read each environment's service connection and variable group from the
`serviceConnections` and `secretGroups` parameter defaults. Both pipelines
now read them from an Environment settings block under `variables`, and a
queued run chooses only the environment. Move your names from the old
defaults into the block in each file, as shown in
[Environment settings](ci-cd-setup.md#environment-settings). Azure Pipelines
rejects a run request that still sets `serviceConnections` or
`secretGroups`. The integration pipeline's `environment` parameter now
accepts only `dev`, `staging` and `prod`, the environments the block maps.

### GitHub Actions workflows

The reusable `.github/workflows/_siteops-deploy.yaml` workflow no longer
accepts a `selector` input. Pass the selector as the `SITE_SELECTOR` secret, as
`deploy.yaml` does. Its `dry-run` input now prepares an executable plan with
`plan`, and deployment passes `--yes`. An empty `siteops-source` still installs
the checkout.

### Windows tool arguments

Site Ops now passes arguments to Windows `.cmd` and `.bat` launchers, such as
`az.cmd`, as literal quoted values, with AutoRun and delayed expansion
disabled. Paths containing ampersands or parentheses remain one argument.

An argument that cannot be passed literally reports
`Windows batch launchers require arguments without percent signs, double quotes or control characters`.
An oversized invocation reports
`The Windows batch command exceeds its supported length.` Both failures occur
before the tool starts. Rename a workspace or temporary path that contains
these characters, shorten the invocation, or use a native executable where
available. Values inside parameter JSON files are unaffected. Native `.exe`
launchers and Linux argument vectors behave as before.

### Temporary files

Resolved parameter files now use the operating system temporary directory.
Set `SITEOPS_TEMP_DIR` to an absolute path to choose another parent.
POSIX files have owner only permissions. Windows files inherit the parent's
ACLs. Cleanup is best effort: a removal failure warns and may leave files.

**Existing files:** Site Ops no longer writes to `<workspace>/.siteops/tmp`
and does not automatically clean it. Inspect remaining content before
deleting it, since it may contain sensitive resolved inputs or files you
want to retain.

### Internal Python callers

These are internal interfaces, not a separately supported Python SDK.

| Call or usage | Update |
|---|---|
| `Orchestrator.deploy` | Consume the returned `RunResult` instead of a dictionary summary. |
| `Orchestrator.validate` | Consume the immutable `ValidationResult` and its diagnostics instead of a list of error strings. |
| Executor `_run_az` / `_run_kubectl` | Consume `ToolCallResult.success`, `.stdout`, `.stderr` and optional typed diagnostic instead of unpacking a tuple. |
| Deploying invalid preparation | Handle `PlanNotExecutableError`. |
| `get_template_parameters` / `filter_parameters` | These executor helpers are removed. Use shared plan preparation through `Orchestrator.build_plan` with `intent=PlanIntent.EXECUTABLE`. |

## To v1.0.0b6

Site files and manifests are checked more strictly. Every check exists because the shape it rejects
was doing nothing, or something other than what it read as, and doing it silently.

### Every file siteops reads

**A duplicate key is rejected.** YAML keeps the last of a repeated key and discards the rest, so a
file carrying `location:` twice deployed the second value while the first read as though it
applied. This applies to every YAML and JSON file siteops reads, including site files, manifests,
and parameter files. The error names the key and the line it repeats on. Merge the two entries, or
rename one.

### Site files

**A site key siteops does not read is rejected.** The allowed top-level fields are `apiVersion`,
`kind`, `name`, `description`, `inherits`, `subscription`, `resourceGroup`, `location`, `labels`,
`properties`, and `parameters`. In the `metadata`/`spec` envelope, `metadata` takes `name`,
`description`, and `labels`, and `spec` takes `subscription`, `resourceGroup`, `location`,
`properties`, and `parameters`. Operator metadata such as `owner`, `contact`, `costCenter`, or
`lastVerified` belongs in `labels:` or `properties:`. Both stay open, so anything you put there is
yours to name.

**The check runs on merged data, and the error lists every file behind it.** A site is checked after
its `inherits:` chain and every overlay are merged, so the key that failed can live in a parent
template, in `sites.local/`, or in an extras directory. The error ends with `Merged from:` and lists
those files in merge order. One key in a shared `SiteTemplate` reports against every site that
inherits it.

**`subscription` and `location` must carry a value.** A key written with nothing after the colon
parses as null, which is not the same as absent. If you wrote a bare `subscription:` expecting to
inherit the value, delete the key. A blank key overrides the parent's value.

**`labels`, `properties`, and `parameters` must be mappings.** A `labels:` written as a list of
`key=value` strings matches no selector, so a site written that way was never selected for
deployment.

**A label value must be text.** A selector compares text, so `release: 2607` or `active: true`
matched no selector and the site was silently never targeted. Quote the value:

```yaml
labels:
  release: "2607"
  active: "true"
```

It is rejected rather than quoted for you, because coercing would make a site start matching a
selector it never matched, which changes what a deployment targets.

**A field that holds text must hold text.** `name`, `subscription`, `resourceGroup`, `location`,
`description`, and `inherits` are rejected when written as a list or a number. Quote any value YAML
would otherwise read as a number, such as a site named for a release:

```yaml
name: "2607"
```

**`inherits` is read at the top level of the file.** Both site shapes inherit that way, so a site
using the `metadata`/`spec` envelope can inherit as long as `inherits:` sits alongside `apiVersion`
and `kind` rather than inside `spec`. A site that wrote `inherits:` inside `spec:` has been
deploying without its parent's `properties` and `parameters`. Move the one line to the top level,
then confirm what the site resolves to with `siteops -w <workspace> sites <name> --render`. Expect
values the site did not have before, and review them before you deploy.

**Keep one shape across an `inherits` chain.** A flat site inheriting a `metadata`/`spec` template,
or the reverse, produced a site assembled from the parent with the child's `name`, `resourceGroup`,
and `labels` dropped. Put both files in the same shape. A single file carrying `spec` alongside
top-level fields is reported as mixing the two.

### Manifests

**List fields must be lists, and their entries must be text.** Manifest `sites:`, `steps:`, and
`parameters:`, and step `parameters:` and `files:`, are lists. A bare string was previously iterated
one character at a time. Add the `-` bullets. Each entry in `sites:`, `parameters:`, and `files:`
must also be text, so quote anything YAML would read as a number:

```yaml
sites:
  - "2607"
```

An unquoted entry was left out of the target set without a word, which means deploying to fewer
sites than the command named.

**An include's `when:` must use the supported syntax.** A malformed expression fails while the
manifest loads with `Invalid 'when' condition syntax on include of ...`. It used to log a warning
and run the included steps on every site.

### Parameters

**A parameter name carrying an unresolved template fails the step.** Previously such a name was
dropped before the deployment, and the step reported success while deploying defaults. Search your
parameter files for `{{` to the left of a colon.

**A templated parameter name resolves, which changes deployed content.** A nested key such as
`siteRoles: {"{{ site.name }}": {...}}` reached ARM as the literal text `{{ site.name }}` and now
arrives as the site name. Templates are supported in a nested name. A top-level name is matched
against the parameters the template declares. It is kept only when the
resolved name is a declared parameter.

**Two parameter names that resolve to the same string are rejected.** Reachable only now that names
resolve. Rename one, since keeping either would drop the other.

**A mistyped template delimiter fails the step.** `{ site.x }}` and `{{ site.x }` reached ARM as
literal text. Both now fail.

**`deploy --dry-run` fails on what the real deployment would fail on.** A dry run resolves
everything a real run resolves, apart from `{{ steps.X.outputs.Y }}` naming a step that runs earlier
in the same manifest, which depends on outputs no dry run produces. Those still warn. An unresolved
`{{ site.X }}` path, a mistyped delimiter, and a reference to a step that does not exist or that runs
later all fail the dry run, so a pipeline that gates on `--dry-run` sees the same answer the
deployment would give it.

**A parameter path that selects a file by site value must resolve to a real file.** A path such as
`parameters/aio-releases/{{ site.properties.aioRelease }}.yaml` lets the site choose which file to
load. When the site does not carry the property, or carries a value naming a file that is not
there, the step deployed without those parameters and reported success. Both cases now fail the
step. In `deploy`, a path with no template in it is unchanged and still warns, so an optional fixed
file keeps working.

### Deployment and targeting

**A command line selector term must be written as `key=value`.** A term without `=` was dropped, so
`-l munich-dev` left the command with no selector and it ran against whatever the manifest targets,
usually a wider set than the term names. It now fails, and the error suggests what you probably
meant:

```
Selector term `munich-dev` is not in `key=value` form. Did you mean `name=munich-dev`?
```

**A site that does not load stops the command.** `deploy` and `sites` name the sites they could not
load and exit non-zero, where they previously logged each one and carried on with the rest. This
matters most on this release, because the checks above reject files that earlier releases accepted,
so a pipeline can go red against a workspace nobody changed. Run `siteops -w <workspace> sites`
first and fix everything it names.

**One subscription holds one subscription-level site.** `deploy` reports a second one rather than
choosing between them. Subscription-scoped steps run once per subscription and their outputs feed
every resource group site under it, so two candidates have no correct resolution. `validate`
already reported this, and `deploy` does not run `validate`, so the check now covers both paths.

### Secret Sync

**The Key Vault secret declaration moved into the sample that owns it.**
`parameters/inputs/sync-secrets.yaml` split in two:
`samples/secretsync-sample/secrets.yaml` holds the secrets a site declares and attaches at manifest
level, so a site or a `sites.local/` overlay can override it, and
`samples/secretsync-sample/inputs.yaml` holds the step output wiring and attaches at step level. A
manifest that referenced the old path reports `Parameter file not found` and does not deploy. Copy
`secrets.yaml` into your own workspace, declare your secrets there, and reference your copy.

### Command line

**`-v` is global and controls logging.** The output it used to select has its own flag:

| Previously | Now |
|---|---|
| `siteops validate m.yaml -v` | `siteops validate m.yaml --plan` |
| `siteops sites -v` | `siteops sites --show-sources` |

`siteops deploy --dry-run` prints the plan on its own. Put `-v` before the
command when you want debug logging. A trailing `-v`, such as
`siteops validate m.yaml -v`, reports `unrecognized arguments: -v` and exits
with code 2. Update copied validation commands and pipeline templates too.

## To v1.0.0b4

This section corrects and supplements the
[v1.0.0b4 release notes](https://github.com/Azure/digital-ops-scale-kit/releases/tag/v1.0.0b4).

### Deployment options

Rename `deployOptions.includeGlobalSite` and `includeEdgeSite` to
`enableGlobalSite` and `enableEdgeSite`. The old names have no aliases and no
longer affect a deployment. `sites/base-site.yaml` also changed
the `edge-site` step's default from `includeEdgeSite: true` to
`enableEdgeSite: false`. Set `enableEdgeSite: true` on Sites that should keep
deploying it.

### Selectors

Only `name` can repeat in a selector, and its values match any of them.
Repeating another key, such as `-l country=a -l country=b`, fails with
`may only appear once`. An empty key or value also fails.

### Pipelines

Define `SITE_OVERRIDES` in Azure Pipelines, empty when you do not use
overrides. Invalid JSON, including the literal `$(SITE_OVERRIDES)` of an
undefined variable, fails the override step.

Validation errors now fail the validation step, so Deploy does not run.

The `manifest` input of `.pipelines/deploy.yaml` and
`.github/workflows/deploy.yaml` is a path relative to the workspace, such as
`manifests/aio-install.yaml` instead of `aio-install`.

### Sites and manifests

- A parent declared `kind: Site` fails with `Cannot inherit from kind 'Site'`.
  Move shared settings into a `kind: SiteTemplate` file.
- An overlay that changes `name` fails with `cannot rename the site`. Use
  `inherits:` or rename the base file.
- Manifest `sites` entries use the relative form, such as `regions/eu/munich`,
  without a leading `./`.
- A manifest key Site Ops does not read fails with `has unknown ... key(s)`.

### Content paths

Update references to the moved shared files under `workspaces/iot-operations`:

| v1.0.0b3 | v1.0.0b4 |
|---|---|
| `parameters/common.yaml` | `parameters/common/common.yaml` |
| `parameters/chaining.yaml` | `parameters/inputs/aio-instance.yaml` |
| `parameters/post-instance.yaml` | `parameters/outputs/aio-instance.yaml` |
| `parameters/secretsync-chaining.yaml` | `parameters/inputs/secretsync.yaml` |
| `templates/iot-ops/common/resolve-aio.bicep` | `templates/aio/resolve-aio.bicep` |
| `templates/iot-ops/common/modules/` | `templates/common/modules/` |
| `templates/iot-ops/deps/` | `templates/deps/` |

### Secret Sync template

`templates/secretsync/sync-secrets.bicep` replaces
`templates/iot-ops/secretsync/sync-secret.bicep`. Pass a `secrets` array, put
the values in `secretValues` keyed by secret name, and supply
`managedIdentityClientId` and `instanceLocation`. Read results from
`materializedSecrets` or `secretCount` instead of the former single secret
outputs.

### Python callers

`Manifest.from_file` requires `workspace_root`, for example
`Manifest.from_file(path, workspace_root=workspace)`.

## To v1.0.0b3

This section supplements the
[v1.0.0b3 release notes](https://github.com/Azure/digital-ops-scale-kit/releases/tag/v1.0.0b3).

### Site overrides in Azure Pipelines

With a nonempty `SITE_OVERRIDES`, the deployment template runs
`scripts/generate-site-overrides.py` from your own checkout. If your pipeline
references the template from another repository, copy that script from the
same release into your repository at the same path.

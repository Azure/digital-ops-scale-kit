# Migrating between Scale Kit releases

Use this guide when upgrading the Scale Kit you run. Sections are newest
first. Apply the sections between your current and target releases,
starting with the oldest applicable release.

To upgrade deployed Azure IoT Operations or Kubernetes instead, see
[aio-releases.md](aio-releases.md) and the `aio-upgrade` and
`aksee-upgrade` entries.

For release summaries, see the
[release notes](https://github.com/Azure/digital-ops-scale-kit/releases).
The [site](site-configuration.md) and [manifest](manifest-reference.md)
references describe the current configuration rules.

## Before you migrate

Run the listing against your workspace before changing anything:

```bash
siteops -w <workspace> sites
```

Resolve any reported site-loading errors, then plan each manifest you deploy:

```bash
siteops -w <workspace> plan <manifest> -l <selector>
```

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
| Installed Site Ops with `pip install -e .` | [Reinstall, or install an identified release](#site-ops-installation). |
| Run `deploy` from a script or pipeline | Add [`--yes`](#deployment-confirmation). |
| Preview with `deploy --dry-run` or `validate --plan` | Use [`siteops plan`](#plan-replaces-preview-options). |
| Use `sites --render` or run `sites` in CI | Review [site inspection](#inspect-sites). |
| Reference shipped manifests, partials or dataflow sets by path | Update [workspace content paths](#workspace-content-paths). |
| Pass a manifest filename without a directory | Review [manifest names and paths](#manifest-names-and-paths). |
| Rely on the workspace default AIO release | Review the [2608 default](#default-aio-release). |
| Select dataflow sets or read catalog step outputs | Update [resource sets](#resource-sets) and [catalog step outputs](#catalog-step-outputs). |
| Author sites, manifests or parameter files | Review [preparation checks](#preparation-checks), [empty site mappings](#empty-site-mappings) and [parameter file selection](#parameter-file-selection). |
| Use the Azure Pipelines templates | Choose an [engine selection](#azure-pipelines-templates). |
| Call the reusable GitHub Actions workflow | Pass the selector [as a secret](#github-actions-workflows). |
| Run Windows batch launchers such as `az.cmd` | Review [literal tool arguments](#windows-tool-arguments). |
| Clean up `.siteops/tmp` | Review [temporary files](#temporary-files). |
| Call the engine from Python | Update [internal callers](#internal-python-callers). |

### Site Ops installation

The v1.0.0b6 guide installed Site Ops with `pip install -e .` in a clone. The
engine now also depends on `packaging`, so run `pip install -e .` again after
updating a clone.

The editable installation is now a contributor workflow. To run an identified
engine instead, use the command from the selected release:
[native `uv tool install`](install-siteops.md#install-the-release-wheel) of the
release wheel, or the [verified bootstrap](install-siteops.md#bootstrap-from-https).
Deactivate or uninstall the editable installation first, then confirm the
version with `siteops --version`.

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

When `GITHUB_ACTIONS` or `TF_BUILD` is set, or `SITEOPS_REDACT_OUTPUT` enables
redaction, `sites` no longer prints a masked listing. It reports
`Site inspection output is private` and exits with code 1. Set
`SITEOPS_REDACT_OUTPUT=0` only for an authorized private destination. See
[inspection output details](site-configuration.md#inspection-output-details).

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

There are no forwarding manifests at the old paths, so an old path reports
`Manifest not found.` A file move does not delete or recreate Azure resources.

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

`sites/base-site.yaml` now sets `aioRelease: "2608"`, where v1.0.0b6 set
`"2607"`. A site that inherits it without its own `aioRelease` selects 2608 on
its next deployment, and 2608 adds the OPC UA connector template. Pin
`properties.aioRelease: "2607"` on a site that must stay on 2607, or move it
with the [upgrade operation](../workspaces/iot-operations/manifests/aio-upgrade/README.md).
See [AIO releases](aio-releases.md).

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
- **Targets:** a manifest selector that matches no site reports
  `No sites matched the specified criteria` and exits with code 1. A manifest
  without steps reports `Manifest has no steps defined`. In both cases
  v1.0.0b6 printed `Nothing to deploy` and exited with code 0.
- **Template inputs:** a template parameter that is not nullable and has no
  default must receive a value. Otherwise preparation reports
  `missing required template parameter` before anything is submitted.
- **Kubectl inputs:** local files must exist inside the workspace, and URLs
  must use HTTPS. A path outside the workspace reports
  `Kubectl file must stay within the workspace`.

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
- Set `siteopsSource` to an explicit pip source, as before.
- Set `installDev: true` on `setup-siteops.yaml` for a contributor checkout.

These routes are mutually exclusive. The release route requires a Linux agent.
If you maintain copied templates, update their scripts and helper files
together. See the
[consumer example](ci-cd-setup.md#reference-the-deployment-template-from-another-repository).

`dryRun` now prepares an executable plan with `plan` rather than running
`deploy --dry-run`, and deployment passes `--yes`.

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

`siteops deploy --dry-run` prints the plan on its own. Running `-v` where one of these flags is
meant prints a note naming the flag.

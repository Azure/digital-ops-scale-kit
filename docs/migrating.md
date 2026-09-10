# Migrating between Scale Kit releases

Use this guide when upgrading the Scale Kit you run. Sections are newest
first. Apply the sections between your current and target releases,
starting with the oldest applicable release.

To upgrade deployed Azure IoT Operations or Kubernetes instead, see
[aio-releases.md](aio-releases.md) and the `aio-upgrade.yaml` and
`aksee-upgrade.yaml` manifests.

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

## Current preview

Start with the changes that affect your workflow:

| If you... | What to change |
|---|---|
| Use `sites --render` | Replace it with [`--output yaml`](#inspect-sites). |
| Preview a deployment | Use [`siteops plan`](#plan-and-validate). |
| Author manifests or parameters | Review the [preparation checks](#preparation-checks). |
| Capture output in scripts or CI | Use [structured results and explicit projections](#results-and-ci-output). |
| Manage temporary files | Review the [new location and cleanup behavior](#temporary-files). |
| Call the engine from Python | Update the [internal result consumers](#internal-python-callers). |

### Inspect sites

Replace `--render`, which now reports `unrecognized arguments: --render`:

```bash
siteops -w <workspace> sites <name> --output yaml
siteops -w <workspace> sites --output json
```

YAML keeps one document per site. JSON always uses an array. The default
plain display and `--show-sources` retain their local behavior. Source
annotations require plain output.

Site inspection is private and has no publishable projection. If you see
`Site inspection output is private`, use `SITEOPS_REDACT_OUTPUT=0` only for
an authorized private destination. Sensitive-key masking is not a publication
guarantee. See [inspection output details](site-configuration.md#inspection-output-details).

### Plan and validate

`siteops plan <manifest>` validates, compiles, preflights, and previews a
deployment without executing it. For a faster compile-free description, use
`plan --describe`.

The compatibility spellings still work:

- `validate --plan` means `plan --describe`.
- `deploy --dry-run` means executable `plan`, not simulated deployment.

`-v` controls logging only. To inspect operation and dependency metadata
locally, use `plan --output json --projection local-private` with redaction
disabled. This projection omits parameter values and full value-bearing
command lines.

### Preparation checks

`plan` and `deploy` share validation before compilation or resource writes.
Structurally invalid inputs produce an invalid plan, not a partial target plan.
Check these areas when existing content is rejected:

- **Targets:** every plan form requires targets, including describe mode
  and `validate --plan`. Use bare `validate` for a reusable manifest without
  targets. A selector matching no sites exits nonzero.
- **Parameter files:** use a mapping. Empty documents remain empty mappings.
  Scalars and arrays report `must contain a mapping`.
- **Template inputs:** non-nullable parameters without defaults are required.
  Nullable parameters, including ARM type references, and parameters with
  explicit defaults (including `null`) may be omitted.
- **Kubectl inputs:** resolved local files and directories must exist inside
  the workspace and URLs must use HTTPS. Site-specific inputs are required
  only for applicable operations, not conditionally skipped steps.
- **Deferred inputs:** output-dependent top-level parameter names are checked
  against the template schema after resolution. Paths and values derived
  from outputs retain runtime validation. Known kubectl scalar inputs and
  wait conditions are checked during preparation.

### Results and CI output

`siteops deploy` prints a plain summary by default. `deploy --output json`
writes one `DeploymentRun` document to stdout. Progress and logs use stderr.
The final result accounts for every prepared operation, including skipped,
unstarted, and unconfirmed work.

| Exit | Deployment result |
|---|---|
| `0` | Succeeded, or every operation was skipped. A skipped run explicitly reports no work. |
| `1` | Failed, invalid, incomplete, or unconfirmed. |
| `130` | Interrupted, with `summary.interrupted` set, regardless of observed successes. |

For CI artifacts, use `--output json --projection publishable` with
`plan` or `deploy`. Capture stdout separately and keep diagnostic stderr
private. Redacted plain plans use the same allowlisted fields: aggregate
activity and generic diagnostics, not private identities or prepared values.

See [plan output](plan-output.md), [run output](run-output.md), and the
[CI capture contract](ci-cd-setup.md).

### Stopping a run

- **During execution:** Ctrl-C stops new work and wakes waiting loops.
  Active child processes return or reach their own timeout before the
  final result is printed.
- **During preparation:** preparation finishes, including remaining
  compilations, before execution is prevented. Preparation failures still
  report failure.

Stopping locally does not cancel work Azure already accepted.

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
| `Orchestrator.deploy` / `execute_plan` | Consume `RunResult` instead of a dictionary summary. |
| `Orchestrator.build_plan` | Use `intent=PlanIntent.EXECUTABLE` for deployment preparation. |
| Executing invalid preparation | Handle `PlanNotExecutableError`. |
| `get_template_parameters` / `filter_parameters` | These executor helpers are removed. Use shared plan preparation. |
| Cooperative stopping | Pass `stop_requested`. Signal handling belongs to the CLI. |

## To v1.0.0b7

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
must clear a list inherited from its parent. The old scalar and `none` forms
report `to the legacy scalar`, naming the site and the ordered list to write
instead.

A plain manifest parameter path that carries a governed collection must become
the typed object form even when it loads one fixed file:

```yaml
# Before
parameters:
  - samples/my-sample/resources.yaml

# After
parameters:
  - path: samples/my-sample/resources.yaml
    collections: [devices, assets]
```

Update a custom catalog manifest from scalar path interpolation and the
`none` comparison:

```yaml
# Before
parameters:
  - "parameters/dataflows/{{ site.properties.resourceSets.dataflows }}.yaml"
steps:
  - include: _dataflows.yaml
    when: "{{ site.properties.resourceSets.dataflows != 'none' }}"

# After
parameters:
  - path: "parameters/dataflows/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.dataflows }}"
    collections: [dataflowEndpoints, dataflowProfiles, dataflows]
steps:
  - include: _dataflows.yaml
    when: "{{ site.properties.resourceSets.dataflows }}"
```

The included family partial contributes its `parameterCompositions` contract.
See [Manifest reference](manifest-reference.md) when a custom partial needs to
declare one directly.

Device and asset sets are selected independently:

```yaml
properties:
  resourceSets:
    devices:
      - site-devices
    assets:
      - site-assets
```

Selecting a set applies its definitions. Deselecting it stops applying them and
does not delete resources from an earlier deployment.

### Catalog step outputs

**Catalog deployment steps now keep a parameter-only interface.** If a custom
manifest reads catalog step outputs, remove references to `endpointNames`,
`profileNames`, `dataflowNames`, `dataflowProfileRefs`, `deviceNames`,
`assetNames`, `assetDeviceRefs`, or `apiVersion`.

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
then confirm what the site resolves to with `siteops -w <workspace> sites <name> --output yaml`. Expect
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

**Executable planning fails on what deployment preparation would fail on.**
`siteops plan` resolves everything available before execution. A
`{{ steps.X.outputs.Y }}` reference to an earlier operation remains a typed
deferred value because the deployment has not produced it yet. An unresolved
`{{ site.X }}` path, a mistyped delimiter, and a reference to an unknown or
later step fail planning.

**A parameter path that selects a file by site value must resolve to a real file.** A path such as
`parameters/aio-releases/{{ site.properties.aioRelease }}.yaml` lets the site choose which file to
load. When the site does not carry the property, or carries a value naming a file that is not
there, the step deployed without those parameters and reported success. Both cases now fail the
step. A path with no template in it is unchanged and still warns, so an optional fixed file keeps
working when the template can use defaults. Executable preparation still
reports a required parameter that the missing file would have supplied.

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
every resource group site under it, so two candidates have no correct resolution.
Shared preparation reports the ambiguity for both planning and deployment.

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
| `siteops validate m.yaml -v` | `siteops plan m.yaml --describe` |
| `siteops sites -v` | `siteops sites --show-sources` |

Use `siteops plan` for executable preflight. Running `-v` where one of these
commands is meant prints a note naming the command.

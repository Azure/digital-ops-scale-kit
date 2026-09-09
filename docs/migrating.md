# Migrating between Scale Kit releases

What to change in a workspace when you move to a newer Scale Kit release, newest first. Read every
section between the release you are on and the one you are moving to, oldest of those first.

This is about the version of Scale Kit you run. For upgrading what Scale Kit deploys, meaning Azure
IoT Operations and the Kubernetes platform under it, see [aio-releases.md](aio-releases.md) and the
`aio-upgrade.yaml` and `aksee-upgrade.yaml` manifests.

For what each release added, see the
[release notes](https://github.com/Azure/digital-ops-scale-kit/releases). For what the rules are
today rather than what changed, see [site-configuration.md](site-configuration.md) and
[manifest-reference.md](manifest-reference.md).

## Before you migrate

Run the listing against your workspace before changing anything:

```bash
siteops -w <workspace> sites
```

Any site missing from that listing no longer loads, and the error names it. Fix those first, since
a site that does not load is not a site that deploys. Then plan each manifest you deploy:

```bash
siteops -w <workspace> plan <manifest> -l <selector>
```

## Current preview

**Executable planning has its own command.** Use `siteops plan <manifest>` to
validate, compile, preflight, and inspect a deployment without executing it.
Use `siteops plan <manifest> --describe` for the faster compile-free shape.

`validate --plan` remains a compatibility spelling for the describe form.
`deploy --dry-run` remains a compatibility spelling for executable planning
and no longer reports simulated deployment success.

**Preparation uses the same validation boundary from the CLI and Python
API.** `plan` and `deploy` validate the loaded manifest and targets before
compilation or resource writes. Structurally invalid inputs produce an
invalid plan rather than a partial target plan. Python callers receive
`PlanNotExecutableError` when attempting to deploy that invalid result.

Python integrations use `Orchestrator.build_plan` with
`intent=PlanIntent.EXECUTABLE` for deployment preparation. The executor-level
`get_template_parameters` and `filter_parameters` helpers have been removed.

**Known kubectl inputs are checked during shared validation.** After site
values resolve, local files and directories must exist inside the workspace,
and URLs must use HTTPS. Per-site inputs are required only when the operation
applies to that site. A conditionally skipped kubectl step does not require
its site-selected file. A file path that depends on a prior deployment output
remains deferred and is validated when that output resolves during execution.

**Executable preparation checks required template inputs.** A non-nullable
template parameter without a default must have a known supplied name unless
a top-level parameter name still depends on a prior operation output.
Nullable parameters may be omitted, including nullable types referenced
through local ARM definitions. Explicit defaults, including `null`, also
permit omission. Deferred names are checked against the same template schema
after the output resolves.
Fully resolved kubectl scalar inputs and wait conditions also use their
runtime guards during preparation. Output-dependent values keep their runtime
validation.

Planning requires a target set, including `plan --describe` and its
`validate --plan` compatibility spelling. To check a library manifest
without targeting, use `validate` without `--plan`. A manifest or CLI
selector matching no sites returns a nonzero exit code.

Parameter files must contain a mapping. An empty document remains an empty
parameter mapping. Scalar values and arrays report `must contain a mapping`
before compilation or execution.

**Verbose dry-run command previews are replaced by prepared-plan
inspection.** `-v` controls logging and does not generate simulated Azure or
kubectl commands. Use `plan --output json --projection local-private` to
inspect operation and dependency metadata locally. Parameter values and exact
value-bearing command lines are not exported by that projection.

**Redacted plain plans use the publishable projection.** They show aggregate
activity and generic diagnostics rather than individual steps, manifest
descriptions, paths, conditions, or literal target inputs. Local plain output
keeps its detailed view with redaction disabled. For CI artifacts, capture
`plan --output json --projection publishable` from stdout separately from
diagnostic stderr.

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

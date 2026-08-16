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
a site that does not load is not a site that deploys. Then dry run each manifest you deploy:

```bash
siteops -w <workspace> deploy <manifest> --dry-run -l <selector>
```

## To v1.0.0b6

Site files and manifests are checked more strictly. Every check exists because the shape it rejects
was doing nothing, or something other than what it read as, and doing it silently.

### Every file the engine reads

**A mapping key given twice is rejected.** YAML keeps the last of a repeated key and discards the
rest, so a file carrying `location:` twice deployed the second value while the first read as though
it applied. This applies to every YAML and JSON file the engine reads, including site files,
manifests, and parameter files. The error names the key and the line it repeats on. Merge the two
entries, or rename one.

### Site files

**A site key the engine does not read is rejected.** The allowed top-level fields are `apiVersion`,
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
against the parameters the template declares, so a resolved site value will not be one of them.

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

### Importing siteops as a library

`Site.from_file` and `Manifest.from_file` apply the checks above.
`Site.from_data(data, *, source, default_name)` is new and builds a site from already-parsed data in
either shape. `Site(labels=None)` and `Manifest(parameters=None)` normalize to empty collections.
Nothing importable was renamed or removed.

# Use a project with packaged or local content

A project keeps your Site configuration separate from the deployment content
you use with it. Run `project pin` to select one complete workspace package,
then choose a manifest with ordinary `browse`, `validate`, `plan` and `deploy`
commands.

## Select configuration and content

`--project DIRECTORY` selects an operator project directory, not a registered
project name. Site Ops has no global project registry. The project supplies
your Site configuration. An explicit `-w PATH` selects local content.
Otherwise, the project's workspace pin selects packaged content.

| Selection | Deployment content | Site configuration |
|---|---|---|
| `--project ./factory` | Package identified by `factory/siteops.pin` | `factory/sites` and overlays |
| `--project ./factory -w ./clone` | Local content in `clone` | `factory/sites` and overlays |
| `-w ./clone` without a selected project | Local content in `clone` | `clone/sites` and overlays |

Relative project and workspace paths resolve independently from the command's
current directory. When `--project` is omitted, only `siteops.pin` in the exact
current directory implies a project. Ancestors and unrelated directories are
not searched.

An explicit local `-w` leaves the workspace pin unchanged and retains project
Sites. Omit package trust options in local authoring mode. This lets you
develop content in a clone without moving your Site configuration into it.

An explicit project with no workspace pin and no local `-w` reports:

```text
Workspace pin not found. Use project pin to select a package, or -w to select local content.
```

It does not select a default remote source or silently switch to local
content. `sites` can inspect project configuration without a workspace pin or
package verification.

## Project files

```text
factory/
  siteops.pin
  sites/
    one.yaml
  sites.local/
  .siteops/
```

`sites` and optional `sites.local` contain your existing configuration.
The workspace pin, `siteops.pin`, records the selected package. `.siteops`
holds private local coordination files. When Site Ops creates that directory,
it adds a `.gitignore` file.
`project pin` does not generate or replace Site files.

## Run `project pin`

Prepare these inputs:

- An installed Site Ops engine and GitHub CLI compatible with the
  [artifact verifier](artifact-verification.md).
- A published release containing a complete workspace package, detached proof
  and [workspace release descriptor](workspace-sources.md).
- A local consumer policy and independently provisioned trusted root.
- Your configured Sites in the project directory.

The current repository release workflow publishes engine assets only. It does
not generate or publish the workspace asset set. Use a source that implements
the workspace release contract and review its deployment qualification
evidence for your targets.

Replace the source and release placeholders below with an approved release.
The examples assume `policy.json` and `trusted-root.json` are your independently
trusted local inputs, and that the package contains a manifest named `storage`.
Global options go before the command:

```text
siteops --trust-policy policy.json --trusted-root trusted-root.json project pin ./factory --source github:<owner>/<repository> --release <release>
siteops project show ./factory
siteops --project ./factory sites
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json plan storage -l name=one
```

The workspace pin identifies the whole workspace. Each command still selects
its manifest by the existing exact name/path rules.

If a release contains several workspaces, add
`--release-workspace <path-from-the-release>` to `project pin`.
The command requires an explicit published release and uses anonymous source
access. Policy and root files remain outside the content cache and are
supplied independently on package use. The workspace pin cannot select them.
Project and cache directories must occupy separate directory trees.
Appending `@<release>` to the source is also supported as shorthand instead
of `--release`.

The `--auth cli` option applies only to descriptive `browse --source`
metadata. Authenticated package acquisition is not implemented.

After reviewing the plan, deploy with the same project, trust inputs and
target selection:

```text
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json deploy storage -l name=one
```

Deployment prepares again. These commands do not promise execution of a saved
plan from a previous invocation. Provider operations can create or update
resources and incur charges.

## Reuse and change a workspace pin

Using the project makes no source request when all bytes selected by the
workspace pin are present. The cache rechecks retained package and proof bytes
against current consumer policy. Missing package or proof bytes can be
restored only when release observations still match the complete workspace
pin. An altered release reports this error rather than updating the selection:

```text
The published source differs from the workspace pin. Repin explicitly to change the selection.
```

Add `--offline` after `browse`, `validate`, `plan` or `deploy` to require the
package and proof already in cache. Offline use still requires valid local
policy and roots. Expired policy, corrupt objects and invalid source
expectations fail without automatic repair.

[Cache maintenance](cache.md) can remove a specific corrupt entry before its
exact content is restored. This does not modify the workspace pin or Site
configuration, but it can make offline use require source access.

Run `project pin` with an explicit approved release to change the selection.
It acquires and verifies the package before atomically replacing a recognized
workspace pin. A concurrent workspace pin change during acquisition is
reported instead of being overwritten. Other files in the project remain
untouched.

Use `siteops project show ./factory --output json` to inspect the selection
without acquiring or verifying package content. `project pin`, `project show`
and `browse` with a selected project produce private source details and require
an authorized private destination. Normal plan and run projections retain
their existing privacy rules.

## What the workspace pin contains

The JSON envelope uses `apiVersion: siteops/v1alpha1` and `kind: WorkspacePin`.
Its `source` record identifies provider, reference, release, exact revision
and the descriptor filename, byte size and SHA-256 digest. Its `content`
record identifies workspace path, kit ID and version, package and proof names,
byte sizes and SHA-256 digests, plus any optional index correlation digest.

These are the common [source expectations](workspace-sources.md), without
GitHub database IDs or transport URLs. Trust policy, roots, credentials,
Site values and local cache paths are separate inputs.

Share the workspace pin and Site configuration only where their contents are
appropriate to publish, and keep sensitive configuration and overlays private.

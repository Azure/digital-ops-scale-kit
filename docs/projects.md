# Use a project with packaged or local content

A project keeps your Site configuration separate from workspace content.
Pinning a complete workspace package is useful for repeatable commands with
configured Sites and fleets, not required for one Site built from typed inputs.
[Guided inputs](guided-inputs.md) can use a published release from an approved
source directly. Its release instructions supply the source and version.

## Select configuration and content

`--project DIRECTORY` selects an operator project directory, not a registered
project name. Site Ops has no global project registry. The project supplies
your configured Sites when you use them. [Guided inputs](guided-inputs.md)
can construct one Site in memory instead. An explicit `-w PATH` selects local content.
Otherwise, the project's workspace pin selects packaged content. A command
with `--source SOURCE@RELEASE` selects that release directly. Global
`--project DIRECTORY` can still supply the operator's Site configuration
without replacing its pin. Direct release selection requires online source
resolution each invocation (valid cached package bytes can be reused).
`-w` with `--source` selects a relative workspace path inside that release,
not a local checkout.

| Selection | Workspace content | Site configuration |
|---|---|---|
| `--project ./factory` | Package identified by `factory/siteops.pin` | `factory/sites` and overlays |
| `--project ./factory -w ./clone` | Local content in `clone` | `factory/sites` and overlays |
| `-w ./clone` without a selected project | Local content in `clone` | `clone/sites` and overlays |
| `--project ./factory` with `--source official@<release>` | Verified selected release, pin unchanged | `factory/sites` and overlays |
| `--source official@<release>` without a project | Verified selected release | Explicit inline, file or standalone Site only |

Relative project and workspace paths resolve independently from the command's
current directory. When `--project` is omitted, only `siteops.pin` in the exact
current directory implies a project. Ancestors and unrelated directories are
not searched.

An explicit local `-w` leaves the workspace pin unchanged and retains project
Sites. Omit package trust options in local authoring mode. This lets you
develop content in a clone without moving your Site configuration into it.

An explicit project with no workspace pin, local `-w` or direct `--source` reports:

```text
Error: Workspace pin not found. Use project pin to select a package, or -w to select local content.
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
- An [approved source](#use-an-approved-source), or a local consumer policy
  and independently provisioned trusted root.
- Your configured Sites in the project directory.

Configured Sites are optional for manifests with typed inputs. You
can inspect `siteops inputs`, then supply one explicit Site with
`--input-file`, `--input`, or a complete `--site-file`.
Resource ID answers on `plan` and `deploy` authorize bounded reads of the
declared ID and related resources using the configured Azure identity.
`inputs` requires `--read-resources` for that optional inspection or save
read. `validate` does not read resources. Package verification and source
trust do not confer Azure read or deployment permission.

The [content release workflow](releasing.md) can publish the workspace asset
set from reviewed declarations after qualification and approval. Choose a
published source that implements the workspace release contract and review
its deployment qualification evidence for your Sites. Package compatibility
and catalog loading do not establish workload health.

Replace `<release>` below with a reviewed release. The examples use the
official source, enrolled as described in
[Use an approved source](#use-an-approved-source), and a configured Site
named `one`. Global options go before the command:

```text
siteops --approved-source official project pin ./factory --release <release>
siteops project show ./factory
siteops --project ./factory sites
siteops --approved-source official --project ./factory plan aio-install -l name=one
```

To verify with local trust files instead, replace `--approved-source official`
with `--trust-policy policy.json --trusted-root trusted-root.jsonl`, and name
the publisher with `--source` on `project pin`. Both files must be
independently trusted local inputs:

```text
siteops --trust-policy policy.json --trusted-root trusted-root.jsonl project pin ./factory --source github:<owner>/<repository> --release <release>
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.jsonl plan <manifest> -l name=one
```

The workspace pin identifies the whole workspace. Each command still selects
its manifest by the existing exact name/path rules.

If a release contains several workspaces, add
`--release-workspace <path-from-the-release>` to `project pin`.
The command requires an explicit published release. It reads release
metadata without credentials, or with `GH_TOKEN` when set, and downloads
release files without credentials, as described in
[source access](remote-content.md#public-and-authorized-source-access).
Policy and root files remain outside the content cache and are
supplied independently on package use. The workspace pin cannot select them.
Project and cache directories must occupy separate directory trees.
Appending `@<release>` to the source is also supported as shorthand instead
of `--release`.

After reviewing the plan, deploy with the same project, approved source or
trust files, and Site selection:

```text
siteops --approved-source official --project ./factory deploy aio-install -l name=one
```

Deployment prepares again. These commands do not promise execution of a saved
plan from a previous invocation. Provider operations can create or update
resources and incur charges.

## Use an approved source

An approved source is a name in your private user configuration that records
which publisher you trust and how its releases must be built. Enroll the
official publisher once, under any lowercase name:

```text
siteops source enroll official
siteops source show official
siteops --approved-source official project pin ./factory --release <approved-release>
siteops --approved-source official --project ./factory browse aio-install
```

Without trust files, `source enroll` writes the publisher's standard release
policy: releases built from its `refs/heads/main` branch by the
`release.yaml` workflow, signed by `_workspace-distribution.yaml` on a
self-hosted runner. The enrollment lasts 30 days and uses the current GitHub
trusted root, read through GitHub CLI without a login. Add
`--source github:<owner>/<repository>` to enroll a different publisher
that follows the same release contract.

The [bootstrap scripts](install-siteops.md#choose-an-installation-route) run
the same enrollment when the operator explicitly chooses
`--enroll-source official` or `-EnrollSource official`. Enrollment does not
sign in to GitHub or Azure. Use `siteops source list` to inspect the names.
With no names enrolled, it prints:

```text
No approved sources. Run `siteops source enroll NAME` to add one.
```

In redacted CI output, explicit enrollment and removal report only success
or failure, without echoing the source name. Inspect records with `show` or
`list` only in an authorized private destination.
The approved source selects the repository and its verification files only
when `--approved-source NAME` is passed. It does not change a project's pin
or select a Site. An explicit `--source` on `project pin` must
match the approved source. Mixing an approved source with explicit trust
files is rejected instead of silently overriding either.
For direct commands, `--source official@<release>` selects the enrolled
repository and that exact published release, without a pin. It does not
automatically enroll `official`. Direct `plan`, `deploy` and `validate`
without a project require explicit Site inputs rather than packaged example
Sites. Source enrollment and deployment confirmation remain separate decisions.
After browsing, [supply one explicit Site](guided-inputs.md) or use
configured project Sites. To retire an enrollment deliberately, run
`siteops source remove official`. This leaves the project pin intact,
but later package use requires another explicitly selected approved source.

Source records are private user configuration. Package use still checks
current policy validity, trusted root identity, source selection, package
provenance and byte integrity. An expired enrollment reports
`source.profile-expired`, and there is no automatic renewal. Browsing
metadata with `browse --source NAME` may still resolve the enrolled
repository after the enrollment expires. It does not restore trust.

### Renew an enrollment

Run the same enrollment again before or after it expires:

```text
siteops source enroll official
```

Renewal keeps the publisher already enrolled under that name, so you can
omit `--source`. It writes a new 30 day policy with the current trusted
root, and only when the publisher and release identity are unchanged. The
output says whether the trusted root changed:

```text
Renewed approved source official: github:Azure/digital-ops-scale-kit.
Trusted root: unchanged.
Valid until: <timestamp>.
```

`Trusted root: updated.` means the current GitHub trusted root differs from
the one recorded at the last enrollment. A name enrolled with a different
publisher or a custom policy reports this error and is left as it was:

```text
Error: The existing source approval differs. Remove it before enrolling changed trust.
```

### Use a custom policy

To manage the policy and trusted root yourself, supply both files before
`source enroll`. The examples assume `policy.json` and `trusted-root.jsonl`
were reviewed and provisioned independently:

```text
siteops --trust-policy policy.json --trusted-root trusted-root.jsonl source enroll official --source github:Azure/digital-ops-scale-kit
```

The [artifact verification guide](artifact-verification.md#github-policy)
describes the policy format. Rerunning with the same files keeps the
existing enrollment and does not extend it. To renew a custom policy, inspect
the record, then replace that exact name when the reviewed files are ready:

```text
siteops source show official
siteops source remove official
siteops --trust-policy renewed-policy.json --trusted-root renewed-root.jsonl source enroll official --source github:Azure/digital-ops-scale-kit
```

Removal leaves the project pin in place, but package use fails closed until
you enroll the replacement. Keep these commands and their files in a private
operator environment. You can also pass `--trust-policy` and
`--trusted-root` directly on `project pin`, `plan` and `deploy`, as shown in
[Run `project pin`](#run-project-pin), without enrolling a name.

## Reuse and change a workspace pin

Using the project makes no source request when all bytes selected by the
workspace pin are present. The cache rechecks retained package and proof bytes
against current consumer policy. Missing package or proof bytes can be
restored only when release observations still match the complete workspace
pin. An altered release reports this error rather than updating the selection:

```text
Error: The published source differs from the workspace pin. Repin explicitly to change the selection.
```

Add `--offline-content` after `browse`, `inputs`, `validate`, `plan` or
`deploy` with a pinned project to require cached package and proof bytes.
This limits content acquisition, not Azure resource reads or deployment
writes. Direct `--source` does not support it: release resolution must be
online, even when cached bytes can be reused. Pinned offline use still
requires valid local policy and roots. Expired policy, corrupt objects and
invalid source expectations fail without automatic repair.

[Cache maintenance](cache.md) can remove a specific corrupt entry before its
exact content is restored. This does not modify the workspace pin or Site
configuration, but it can make offline use require source access.

Run `project pin` with an explicitly selected release to change the selection.
It acquires and verifies the package before atomically replacing a recognized
workspace pin. A concurrent workspace pin change during acquisition is
reported instead of being overwritten. Other files in the project remain
untouched.

`siteops project show ./factory` prints the selection without acquiring or
verifying package content:

```text
Project: <project directory>
Pin: siteops.pin
Source: github:<owner>/<repository> @ <release>
Workspace: <workspace path in the release>
Package: <package identifier> <package version>
Revision: <commit>
Package SHA-256: <digest>
The pin records selection. Each use verifies the package against current source approval.
```

Add `--output json` to print the workspace pin document instead.
`project pin`, `project show` and `browse` with a selected project produce
private source details and require an authorized private destination. Normal
plan and run projections retain their existing privacy rules.

## What the workspace pin contains

The JSON envelope uses `apiVersion: siteops/v1alpha1` and `kind: WorkspacePin`.
Its `source` record identifies provider, reference, release, exact revision
and the descriptor filename, byte size and SHA-256 digest. Its `content`
record identifies workspace path, package identifier and version (`kit`),
package and proof names,
byte sizes and SHA-256 digests, plus any optional index correlation digest.

These are the common [source expectations](workspace-sources.md), without
GitHub database IDs or transport URLs. Trust policy, roots, credentials,
Site values and local cache paths are separate inputs.

Share the workspace pin and Site configuration only where their contents are
appropriate to publish, and keep sensitive configuration and overlays private.

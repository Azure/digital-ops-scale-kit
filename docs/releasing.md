# Prepare and publish a release

Prepare the release tag, headline, and notes in a pull request. After merge,
review the candidate and approve publication. A candidate is the exact source
commit, release declaration, notes, and asset bytes proposed for that release.

Site Ops and Scale Kit have independent version streams in the same repository.
A content release can reference an existing engine release without rebuilding
it or changing its version.

## Choose the right workflow

| Workflow | Use it for | Can it publish? |
|---|---|---|
| **CI** | Code checks, installer checks, or a release preview | No |
| **Release (approval required)** | Preparing and publishing a reviewed release | Only after the configured reviewer approves |

These entry points share candidate preparation. A CI preview is not a pending
release and cannot be promoted. A real release prepares its own candidate from
the reviewed files on `main`.

If **Release (approval required)** is not available in Actions, its workflow
file must first exist on the repository's default branch. Use the already
registered CI workflow to preview feature-branch changes. Changing the default
branch or pushing the feature directly to `main` is not needed for a preview.

## Preview a release without publishing

In **Actions > CI > Run workflow**, select the feature branch and choose:

| Input | Value |
|---|---|
| `run-mode` | `release-preview` |
| `expected-source-sha` | The selected branch's full commit SHA |
| `release-file` | Keep the supplied example, or select a committed `releases/<name>/release.json` |

To obtain the SHA, open the selected branch's latest commit and copy its full
commit identifier. This field confirms which commit will run. It does not
select an older commit from the branch.

The default example is
`.github/release-examples/combined-preview/release.json`. It exercises the
combined preview path using the selected source commit. Examples are accepted
only for a dry run and never trigger publication when merged.

The preview runs ordinary CI, then uses the real release-file preparation,
wheel and bundle production, independent attestation, and installation
qualification.
The final summary shows one Python/platform matrix and a link to the attested
release assets. Individual job logs remain available for diagnosis.

This path needs no `siteops-release` environment. Its jobs have no repository
content-write permission, request no publishing approval, and create no tag or
GitHub Release. A generated dry-run plan cannot be used by the publisher.

The other `run-mode` choices are `installer-check` for CI plus bundle and
installation checks, and `ci-only` for ordinary CI. `ci-only` uses neither
additional input. `installer-check` uses the SHA but ignores `release-file`.
None of these modes publishes a release.

Successful dry runs establish preparation and installation behavior. The
approval UI and actual release upload still require a configured environment
and an explicitly approved publication.

## Prepare the real release files

Create a new directory for each release:

```text
releases/<name>/release.json
releases/<name>/notes.md
```

`release.json` specifies the tag, headline, and engine selection. `notes.md`
contains the Markdown release notes. The tag defines the release version.
The folder name identifies the record, rather than providing another version
setting.

The release title is `<tag>: <headline>`. For example, a declaration with
`"tag": "v1.0.0b8"` and `"headline": "Native Site Ops installation"` produces
`v1.0.0b8: Native Site Ops installation`. Use 1-120 printable characters for
the headline, with no line breaks or surrounding whitespace. Describe the main
operator benefit rather than repeating the product name or version.

Start the notes with `## Highlights` and explain what operators can do with
this release. Include `## Upgrading` when existing users must take action,
linking to the applicable migration instructions. The GitHub release title
already identifies the release, so the notes do not need another top-level
title.

Write the changes and release-specific guidance in `notes.md`. The workflow
adds **Install Site Ops** automatically. It includes the exact versioned wheel
URL for the simple online pipx path, the configured package index policy, the
four release asset links, and the source-pinned verified installation guide.
A content-only release links to its independently published engine. There is no
need to copy installation commands, source hashes, or download URLs into the
authored notes.

| Location | Purpose |
|---|---|
| `.github/release-examples/<name>/` | Safe examples accepted only by previews |
| `releases/<name>/` | Reviewed files that can start a real release |

Merging examples does not start the publishing workflow. Create a new folder
under `releases/` for a real release, with its intended tag and notes.
CI validates changed release declarations on pull requests.
Invalid fields, version choices, headlines, or record paths fail before the
publication workflow needs to build installation assets.

Choose the release-file shape that matches the release. These examples illustrate
formats, not scheduled releases.

### Release Site Ops independently

```json
{
  "tag": "siteops/v1.1.0",
  "headline": "Native installation and deployment outcomes"
}
```

The version must exactly match `siteops.__version__` in the selected source
commit. The workflow builds the engine wheel once with that source version. It
publishes the identical standalone wheel and includes those same bytes in a ZIP
with the pinned runtime wheels, `pylock.toml`, metadata, and notices. Scale Kit
content does not need a version change.

The current beta policy still keeps the source package at `1.0.0b1`.
Do not create another Site Ops beta tag without an approved version-policy
change.

### Release content against an existing engine

```json
{
  "tag": "v1.2.0",
  "headline": "Fleet workload configuration",
  "siteops": {
    "release": "siteops/v1.1.0"
  }
}
```

This references a published Site Ops release in the same repository. It does
not build an engine bundle or depend on the current engine development version
on `main`. Confirm content compatibility with the referenced engine as part of
the release evidence.

The reference is an exact engine release, not a minimum-version range.
Stable content requires a stable referenced engine release. Candidate
preparation requires its complete native asset set: the ZIP, standalone wheel,
and one detached proof for each. It freezes their GitHub digests and rejects an
older ZIP-only release.

Scale Kit content currently comes from the tagged repository. This workflow
does not produce a separately packaged workspace or claim that GitHub's
generated source archives are verified workspace packages.

### Include an engine build in a content prerelease

```json
{
  "tag": "v1.0.0b8",
  "headline": "Native Site Ops installation",
  "siteops": {
    "build": true
  }
}
```

This is the combined prerelease path. The content tag advances independently,
while the source engine version follows its existing policy. The included
engine receives a distinguishable build version such as
`1.0.0b1+build.12345.1.gabcdef123456`.

It creates one content release with the Site Ops ZIP and standalone wheel. Each
subject has its own detached attestation. It does not create another Site Ops
tag. This option requires a prerelease content version. Stable content
references a separately released engine instead.

## Release fields and defaults

| Field | Meaning |
|---|---|
| `tag` | Required version identity. `v...` releases Scale Kit content, while `siteops/v...` releases the engine independently. |
| `headline` | Required short description used with the tag to form the release title. |
| `siteops` | Required for a content release. Choose `{"build": true}` or `{"release": "siteops/v<version>"}`. Omit it for an independent engine release. |
| `latest` | Optional, defaults to `false`. Set `true` only to designate a stable Scale Kit release as GitHub's Latest release. |

`siteops.build` is a selection, not an on/off switch. `true` includes a fresh
engine build in a content prerelease. `false` is not supported: name an existing
engine release instead. Independent engine releases always build their own
assets from the matching source package version.

Prerelease status comes from the tag, with no separate `prerelease` field.
Versions containing a development, alpha, beta, or release-candidate suffix,
such as `v0.0.2.dev20260912`, `v1.0.0b8`, or `v1.0.0rc1`, are prereleases.
`v1.0.0` is stable. Stable content must reference a stable engine release.

`latest` controls GitHub's repository-wide Latest badge and
`releases/latest` destination. It does not update installed applications.
Use `true` when publishing the stable content release you want that destination
to recommend. Prereleases and independent Site Ops releases cannot take over
that designation. A stable release may still use `latest: false`.

Do not commit source hashes, run IDs, artifact IDs, artifact digests, or a
`published` flag into the release file. These are generated evidence or GitHub
state, not authoring inputs. Keep published release files as historical records
rather than maintaining a mutable `next release` file.

Notes are plain Markdown. Release files cannot supply local hooks, commands,
arbitrary repository URLs, or custom note-file paths.

## Prepare and review the candidate

```text
Release files PR
          |
          v
Merge to main at commit A
          |
          v
Read release files from A
Run CI for A
Build, attest, and qualify the engine ZIP and standalone wheel when requested
          |
          v
Review the candidate summary and applicable content evidence
          |
          v
Approve tag creation and publication
          |
          v
Tag A and publish the same qualified bytes
```

Use one release folder per release-preparation PR. A push to `main` that
changes `releases/<name>/release.json` or `notes.md` starts preparation,
normally as the result of merging that PR. Protect `main` with required pull
requests to enforce the review step. The workflow itself listens for the
matching push, not a PR event. If several release folders change together,
automatic selection stops instead of choosing one silently.

Normally, no manual dispatch or SHA entry is needed after merging the release
files. Open the automatically started **Release (approval required)** run to
review its candidate. Automatic preparation still waits for human approval
before publication.

The workflow reads metadata and notes from the selected Git commit. It does
not depend on a mutable checkout or select the newest successful build later.
Source and artifact identities remain fixed while approval is pending.
An unrelated advance of `main` does not change the candidate. Changing its
release file or notes requires a fresh candidate and approval.

The approval summary shows the tag, source commit, version stream, engine
selection, title, ZIP and wheel digests when applicable, tag action, and final
release notes.
The summary nests the note headings beneath **Release notes**. Published notes
retain their authored Markdown heading levels. Installation commands are
included in the final notes before approval and remain bound to that approval.
You may download the attested release artifact for a hands-on trial. It contains
exactly the ZIP, wheel, and one detached proof for each. The pipeline
authenticates both subjects and confirms their application wheel bytes match.
For a verified installation, consume only the ZIP and its proof by following
[the installation guide](install-siteops.md).
The generated online command is for the published release.
For a candidate preview, use the Actions artifact above the notes and the
installation guide's steps for manually downloaded files.

CI and engine installation qualification are automated. For content releases,
the reviewer must also confirm the applicable content/AIO evidence and any
valid carry-forward from earlier qualification. The release workflow does not
deploy Azure resources or infer workload health from installation success.

## Approve publication

Before preparing a real release, create `siteops-release` in **Settings >
Environments**, add required reviewers, and restrict it to `main`.
This is a GitHub approval gate, not an Azure environment. It needs no
environment secrets for the current workflow.

The workflow checks this after reading an active release file and before
building its native release assets. It also checks for an existing release or
conflicting tag before building. The publisher independently rechecks the
destination after approval. Choose self-review and
administrator-bypass settings according to repository policy. Previews do not
need this environment.

`siteops-release` is the shared publication gate for both version streams,
including Scale Kit content releases that reference an existing engine.

After reviewing the generated summary, an authorized reviewer uses **Review
deployments**, selects `siteops-release`, then selects **Approve and deploy**.
This is GitHub's label for allowing the publishing job. It does not deploy
Azure infrastructure. If self-review is prevented, another reviewer must
approve the run.

After approval, the workflow:

1. Downloads and verifies the same release file, notes, frozen asset list, and
   qualified release assets.
2. Confirms the release file has not changed and the referenced engine, if any,
   still has the same release identity and tag target.
3. Creates a missing tag at the approved commit, or reuses a tag already
   pointing there. It never moves a conflicting tag.
4. Reauthenticates the ZIP and standalone wheel, confirms their byte identity,
   and creates the GitHub Release with the approved notes and four declared
   assets.
5. Confirms every uploaded asset digest and verifies GitHub's release
   attestation when immutable releases are enabled.

The destination is the repository running the workflow. Official publication
occurs in `Azure/digital-ops-scale-kit`. A fork's workflow writes only to that
fork. Existing releases are never overwritten.

With native installation assets, GitHub CLI creates a draft, uploads the files,
and then publishes. This temporary draft is not a separate human-review stage.
If immutability is enabled, GitHub locks the uploaded assets and tag when
publication completes. Titles and release notes remain editable through
GitHub's normal controls. The workflow does not enable immutability itself.

## Retry or prepare a new candidate

A failed check stops publication. Missing artifacts, changed release files,
conflicting tags, or an existing release require an explicit resolution rather
than a fallback to different source or replacement assets.

Tag creation and publication are separate GitHub operations. A failure can leave
the correct tag without a completed release. Inspect the result before retrying.
Rebuilding creates a new candidate with its own evidence and approval.

For a transient preparation failure, rerun all jobs in the original workflow
run. After changing source or notes, prepare a new candidate instead.
If an older candidate is still waiting for approval, cancel that superseded
run rather than approving it.
Publication consumes the exact reviewed artifact IDs and hashes, rather than
choosing a different successful build later. A failed publishing operation can
have partial effects, so inspect its result before retrying.

To prepare a new candidate manually, open **Actions > Release (approval
required) > Run workflow** and enter:

| Input | Value |
|---|---|
| Branch | `main` |
| `release-file` | A repository-relative path such as `releases/my-release/release.json`, already committed on `main` |
| `expected-source-sha` | The full current commit SHA of `main` |

`release-file` is a path, not an upload or branch selector. This starts the same
real release workflow as the automatic trigger and requests the same approval.
Use it to prepare a new candidate from an existing declaration, for example
after correcting a workflow problem. There is no need to start it manually
when the merge already triggered preparation. If `main` no longer matches the
supplied SHA, preparation stops rather than selecting an older commit.

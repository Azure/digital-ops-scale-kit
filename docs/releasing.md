# Prepare and publish a release

Prepare the release tag, headline, and notes in a pull request. After merge,
review the candidate and approve publication. A candidate is the exact source
commit, release declaration, notes, and asset bytes proposed for that release.

Site Ops and Scale Kit have independent version streams in the same repository.
A content release can reference an existing engine release without rebuilding
it or changing its version.

A detached attestation proof is a separate file containing signed provenance
evidence for an artifact. Each signed artifact has its own proof.

## Choose the right workflow

| Workflow | Use it for | Can it publish a release? |
|---|---|---|
| **CI** | Code and runner checks, attestation diagnostics, installer checks, or a release preview | No |
| **Release (approval required)** | Preparing and publishing a reviewed release | Only after the configured reviewer approves |

These entry points share candidate preparation. A CI preview is not a pending
release and cannot be promoted. A real release prepares its own candidate from
the reviewed files on `main`.

CI run titles identify the selected mode and branch. The **Overview** summary
shows ordinary checks and only the optional path selected for that run.
The graph still contains the workflow's other branches because GitHub controls
its layout. Detailed installation and release reports remain in their jobs.

If **Release (approval required)** is not available in Actions, its workflow
file must first exist on the repository's default branch. Use the already
registered CI workflow to preview feature-branch changes. Changing the default
branch or pushing the feature directly to `main` is not needed for a preview.

## Configure release artifact runners

In each publishing or rehearsal repository, set the Actions repository variable
`SITEOPS_RELEASE_POOL` to its dedicated 1ES GitHub runner pool name. Register
the pool for that exact repository. A fork has its own configuration and pool,
separate from the upstream repository.

Use `runner-check` to confirm the pool's baseline. A fork can also run the
attestation diagnostic below. Review the repository registration, maintained
image, isolation controls and verification policy, then
set `SITEOPS_RELEASE_PROVENANCE_READY` to `true` to enable artifact execution
in that repository. An absent or different value keeps admission closed
before worker allocation. Configuring the fork does not enable the upstream
repository. Run an approved release preview to qualify the actual artifact
path before publication.

Artifact execution requires `SITEOPS_RELEASE_RUNNER_MODE=scaleset` and an
enrolled Scale Set pool. Each artifact job checks its Linux `self-hosted`
runtime before source or artifact work. Verification separately requires
that exact signing class and the selected repository, source and workflow
identities. A pool label or runner class does not establish 1ES membership.

| Work | Runner |
|---|---|
| Runner configuration admission | Public GitHub runner, without source checkout or write permissions |
| Release preparation, engine and workspace builds, signing, descriptor and payload assembly, publication | Configured 1ES pool |
| Lint, unit tests, template validation, installation and workspace qualification, result summaries | Public GitHub runners |

The admission job requires the expected source commit and a supported entry
point before allocating release workers. CI requests release workers only for
explicit `installer-check` or `release-preview` dispatches. The Release workflow
requires `main`. Pull request events cannot enter the reusable release producers.
Missing or malformed pool configuration fails explicitly, with no fallback to
public runners.

The pool comes from repository configuration, not a release declaration or
dispatch override. Artifact jobs request only the admitted pool name.
Keep source builds separate from signing and publishing jobs. Their
permissions and publication approval remain independent
of runner placement.

Use the maintained runner image for baseline tools. Existing workflow steps
select Python, install locked packages through the configured feed and verify
the Bicep compiler before use. A custom image bootstrap is not required.

### Check the runner without producing artifacts

Use **Actions > CI > Run workflow**, select a reviewed branch, choose
`runner-check` for `run-mode`, and enter its full `expected-source-sha`.
The configured pool runs two short jobs without repository checkout, signing
permissions, artifact production or deployment. The first records baseline
Python, Git, GitHub CLI and Azure CLI versions using temporary empty profiles.
The second requires a different boot session. The summary contains tool
versions and the comparison outcome, not machine identities or credentials.
For legacy routing, set the repository Actions variable `SITEOPS_RELEASE_IMAGE`
to the approved image name configured in that pool before using this mode. The diagnostic
requests `self-hosted`, the configured pool label and an explicit
`1ES.ImageOverride` label. Missing or malformed image configuration stops
admission before allocation. The image choice is not a dispatch override.

For a pool explicitly enrolled in the 1ES Scale Set API preview, set the
repository variable `SITEOPS_RELEASE_RUNNER_MODE` to `scaleset`.
An unset variable or `legacy` retains legacy routing for `runner-check`.
Artifact execution requires `scaleset`. Other values stop admission rather
than choosing a fallback.

The Scale Set diagnostic requests only the admitted pool name. It uses the
pool's configured image, rather than `SITEOPS_RELEASE_IMAGE`, and sends no
`self-hosted`, `1ES.Pool`, `ImageOverride` or `JobId` demand labels.
Configure a single image on that pool before using this preview.
The variable must agree with the pool's integration mode. It does not
reconfigure the pool. Other repositories retain their own routing settings.

Scale Set routing supports the diagnostics and explicitly enabled artifact
jobs. Preview support limitations apply.

This mode does not change release admission. Different boot sessions
do not establish complete machine isolation, Trusted Launch or provenance.
Ordinary CI still runs on public runners, and `release-file` is ignored.

### Inspect an attestation from the fork runner

After `runner-check` succeeds on a fork's Scale Set pool, choose
`attestation-check` for `run-mode` and enter the reviewed branch's full
`expected-source-sha`. This mode requires a fork, a manual branch dispatch,
and `SITEOPS_RELEASE_RUNNER_MODE=scaleset`. It waits for ordinary CI to pass.
The `release-file` input is ignored.

This is real signing. It creates a public attestation and permanent
transparency record identifying the fork, workflow and commit, even if later
verification fails. Deleting the run or its temporary artifacts does not remove
that record. Approve those consequences before dispatching.

The 1ES job creates only `runner-check.txt` with fixed diagnostic text and
attests it without checking out repository source. A public runner downloads
the exact evidence artifact, uses stock GitHub CLI verification, and compares
the observed certificate with the expected repository, branch, source commit,
reusable signer, caller workflow and `self-hosted` class. This diagnostic uses
the CLI's online trust roots. Release consumers retain their separate policy
and trusted root requirements.

The subject and detached proof are retained as a workflow artifact for seven
days. If signature verification succeeds, its bounded JSON result is also
retained, including when the later certificate comparison fails. A failed
comparison requires investigation rather than wider identity matching.

This mode creates no tag or release and leaves the production verification
policy and release gate unchanged. A `self-hosted` certificate does not identify
a particular pool or establish Trusted Launch, complete worker isolation or
release authority. The upstream pool and installed engine compatibility require
their own qualification.

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
`.github/release-examples/workspace-preview/release.json`. It builds one
complete IoT Operations workspace and the selected engine from the chosen
source commit. Choose
`.github/release-examples/combined-preview/release.json` to preview engine
artifacts under a content tag. Examples are accepted only for a dry run and
never trigger publication when merged.

For the default example, the CI preview performs real builds and signing, but
it cannot publish and cannot be promoted into a release. Once the repository
opts in to artifact execution as described above, it follows ordinary CI with
these steps:

1. Prepares the exact release plan and requested engine assets.
2. Builds each declared workspace with read permissions, using that release
   plan and checking any committed indexes for freshness.
3. Creates a proof for each workspace package and build record. A collector
   with read permissions verifies those subjects before creating the public
   workspace routing descriptor.
4. Installs the selected engine from its authenticated lock and checks package
   compatibility, protected cache use, and guarded catalog loading on every
   declared Windows or Linux Python target.
5. Requires all qualification results to identify the same engine, workspace
   inventory, and release plan, then freezes the complete publication
   inventory.

Workspace qualification does not compare executable deployment plans,
authorize targets, deploy resources, or evaluate workload health. Publication
remains a separate approval step.

The default preview's final summary shows one matrix of Python versions and
platforms, plus a link to the attested release assets. Individual job logs
remain available for diagnosis.

This path needs no `siteops-release` environment. Its jobs have no permission
to write repository contents, request no publishing approval, and
create no tag or GitHub Release. A release plan from a preview cannot be used
by the publisher.

The other `run-mode` choices are `installer-check` for CI plus bundle and
installation checks, and `ci-only` for ordinary CI. `ci-only` uses neither
additional input. `installer-check` uses the SHA but ignores `release-file`.
None of these modes publishes a release.

A successful CI release preview exercises preparation and the builds, signing,
and qualification required by its declaration. Script commands that accept
`--dry-run` can instead perform unsigned local preparation. The flag alone
does not imply signing. The approval UI and release upload still require a
configured environment and explicit approval.

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
four release asset links, and the verified installation guide pinned to the
source commit. A content release that references an existing engine links to
that independently published release. There is no need to copy installation
commands, source hashes, or download URLs into the authored notes.

| Location | Purpose |
|---|---|
| `.github/release-examples/<name>/` | Safe examples accepted only by previews |
| `releases/<name>/` | Reviewed files that can start a real release |

Merging examples does not start the publishing workflow. Create a new folder
under `releases/` for a real release, with its intended tag and notes.
CI validates changed release declarations on pull requests.
Invalid fields, version choices, headlines, or record paths fail before the
publication workflow needs to build installation assets.

Choose the components through the release tag and engine selection:

| Components | Reviewed declaration |
|---|---|
| Site Ops only | A `siteops/v...` tag that matches the source engine version |
| Content only | A `v...` tag and `siteops.release` naming an existing engine release |
| Both | A prerelease `v...` tag and `siteops.build: true` |

The generated release plan and approval summary show that resolved choice, the
content version, and whether the engine is built or referenced. The workflow
uses that reviewed choice directly. The examples below illustrate formats, not
scheduled releases.

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
preparation requires its complete native asset set and freezes the GitHub
digests. Earlier engine releases with four assets (ZIP, standalone
wheel and a proof for each) remain valid references. Newer releases also
carry both bootstrap scripts and their separate proofs. Referencing an
older release with four assets does not make the bootstrap routes available.
A release that contains only a ZIP is rejected.

Add reviewed `workspaces` records to publish complete workspace packages
and their proofs with the content release. Their kit version comes
from the content tag, while the referenced engine retains its own version
and assets. See [workspace production](workspace-packages.md#build-workspaces-declared-by-a-release).

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

It creates one content release with the Site Ops ZIP, standalone wheel,
`siteops-bootstrap.sh`, `siteops-bootstrap.ps1`, and a separate detached
proof for each. Any declared workspace packages have their own proofs.
It does not create another Site Ops tag.
This option requires a prerelease content version. Stable content references
a separately released engine instead.

## Release fields and defaults

| Field | Meaning |
|---|---|
| `tag` | Required version identity. `v...` releases Scale Kit content, while `siteops/v...` releases the engine independently. |
| `headline` | Required short description used with the tag to form the release title. |
| `siteops` | Required for a content release. Choose `{"build": true}` or `{"release": "siteops/v<version>"}`. Omit it for an independent engine release. |
| `workspaces` | Optional reviewed workspace build inputs for a content release. See [workspace production](workspace-packages.md#build-workspaces-declared-by-a-release). Publication requires every declared package, its proof, and the routing descriptor. |
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
Build and attest the requested engine and workspace assets
Qualify native installation and workspace consumption as applicable
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

The approval summary shows the components, tag, source commit, independent
content and engine versions, engine selection, title, ZIP and wheel digests
when applicable, tag action, and final release notes.
The summary nests the note headings beneath **Release notes**. Published notes
retain their authored Markdown heading levels. Installation commands are
included in the final notes before approval and remain bound to that approval.
Download the complete candidate payload from the summary for a local trial.
It includes the declared workspace assets and, when an engine is built, its
installation ZIP, standalone wheel, both bootstrap scripts, and a separate
proof for each. The pipeline authenticates all four engine subjects and
confirms the application wheel bytes in the ZIP and standalone wheel match.
For a verified installation, consume only the ZIP and its proof by following
[the installation guide](install-siteops.md).
The generated online command is for the published release.
For a candidate preview, use the Actions artifact above the notes and the
installation guide's steps for manually downloaded files.

The frozen asset inventory separates files to publish from assets that remain
in an existing engine release. Every asset records its filename, byte size, and
SHA-256 digest.
Referenced engine assets retain their own release identity and tag target.
They are not uploaded to the content release again. The publisher consumes
the approved publication list and compares the uploaded identities with that
list.

When workspaces are declared, the final payload combines their qualified ZIPs,
proofs and `siteops-workspaces.json` with any engine assets built for this
release. The referenced engine's files remain in its own release. Candidate
preparation compares its frozen engine selection with the current native
inventory, preserving the exact engine that qualified the workspaces.

This approval inventory is distinct from the public `siteops-workspaces.json`
routing descriptor. Workspace package delivery is described in
[workspace release sources](workspace-sources.md).

The workflow runs CI and applicable engine and workspace qualification. For
content releases, the reviewer must also confirm the applicable content/AIO
evidence and any valid evidence carried forward from earlier qualification.
The release workflow does not deploy Azure resources or infer workload health
from installation success.

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
   and reauthenticates each declared workspace package. It compares workspace
   routing, source, kit and compatibility metadata with the approved
   declaration before creating the release with its unchanged payload.
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

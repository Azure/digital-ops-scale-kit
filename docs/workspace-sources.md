# Workspace release sources

Select one complete workspace package from a published release either
directly with `siteops deploy <manifest> --source SOURCE@RELEASE` and
explicit Site inputs, or with `siteops project pin --release` for
repeatable configured Sites. Both routes require independently supplied
consumer trust policy and trusted roots (or a separately enrolled approval).
The release descriptor cannot select them. See
[guided inputs](guided-inputs.md) and
[workspace pins and configured Site execution](projects.md#run-project-pin)
for operator commands.

`siteops-workspaces.json` routes the selected package and its detached
attestation proof. A proof is a separate file containing signed provenance
evidence. Selection does not require inspection of every ZIP or a descriptive
index.

Release candidate preparation can build workspace packages, create their
proofs, generate this descriptor, and qualify package consumption through the
selected installed engine. Publication requires the complete qualified
inventory, configured approval environment, and applicable release evidence.

## Descriptor

A release containing workspace content uses one descriptor with this shape.
Replace the example names, sizes, revision and digest placeholders with the
identities of the actual published files:

```json
{
  "apiVersion": "siteops/v1alpha1",
  "kind": "WorkspaceReleaseAssets",
  "source": {
    "revision": "<immutable-source-revision>"
  },
  "workspaces": [
    {
      "workspace": "workspace",
      "kit": {
        "id": "example.storage",
        "version": "1.0"
      },
      "package": {
        "name": "storage.zip",
        "size": 123,
        "sha256": "<package-sha256>"
      },
      "proof": {
        "name": "storage-proof.jsonl",
        "size": 456,
        "sha256": "<proof-sha256>"
      }
    }
  ]
}
```

The revision is opaque in the common format. The current GitHub adapter supplies
the commit resolved from the selected release tag. Other providers can supply
their own immutable revision identity.

Each workspace is `.` or a canonical relative path matching the package's
workspace root. A release with one workspace can select it by default.
Several workspaces require an exact selection. Case variants, path rewriting
and unknown selections do not choose an alternative automatically.

Package and proof references have explicit, unique filenames, byte sizes and
lowercase SHA-256 digests. They cannot name the descriptor itself. Workspace
paths and referenced filenames cannot collide by case. Unrelated release
assets are allowed.

An entry may also contain `index: {"sha256": "<public-index-sha256>"}`.
This is optional correlation data, not an acquisition dependency. The
descriptor does not list itself or contain its own final digest.

The descriptor is limited to 256 KiB and 64 workspaces. Each declared artifact
size is limited to 128 MiB, with stricter bounds imposed by its consumer. The
current GitHub proof verifier accepts proofs up to 2 MiB. Duplicate JSON keys,
unsupported fields and numbers outside JSON syntax are rejected.

## Source identity and trust

The descriptor is unsigned routing metadata. An approved source adapter
establishes its expected digest and revision independently. Before parsing,
the caller compares the descriptor bytes with that observed identity.

`ResolvedReleaseSource` and `ResolvedWorkspaceSource` retain common source
expectations without transport URLs, GitHub asset IDs, publisher policy or
trusted roots. `check_package` compares the inspected archive identity,
source revision, workspace root, kit ID and kit version with the selection.
That comparison does not authenticate the publisher.

The GitHub binding compares every declared package and proof with the
observed release asset inventory. Missing or inconsistent identities fail
before package acquisition. The release snapshot retains GitHub IDs and its
reported immutable status separately. GitHub's immutable release setting is
useful source evidence, but it does not replace exact digests, source identity
or consumer provenance policy. These APIs do not enable or require that
repository setting.

Downloads and provenance verification remain separate operations. After
verification, the selected package enters the existing protected cache and
execution flow. A descriptor, source observation or stored receipt alone
does not authorize execution.

## Anonymous asset transfer

The internal `download_https_asset` context acquires opaque bytes into a new
private directory beneath the caller's staging location. Trusted adapter code
supplies the URL, permitted HTTPS origins and expected artifact identity.
Artifact names remain descriptive and never become local output paths.

The transfer uses normal TLS verification and configured proxies. It permits
up to three redirects within the approved origins, rebuilding anonymous
request headers at each hop. Redirect and error bodies are closed without
being consumed. Responses must have supported HTTP framing and identity
content encoding. Size and SHA-256 must match before the caller receives the
file, and downloaded content is never imported or executed by the transfer.

A dedicated engine worker runs with isolated Python imports. Its deadline
covers DNS, connection setup, headers and body reads. The default is 120
seconds, with an internal maximum of 300 seconds. HTTP header lines are
limited to 8 KiB and header count to 64. Worker output is bounded separately.
Staging is removed after the caller exits the context and worker exit is
confirmed. An unconfirmed exit retains staging and reports a warning.

Failures provide safe categories and numeric HTTP status or retry information
when available. The transfer does not automatically retry, switch credentials
or substitute cached bytes. This primitive is anonymous and separate from
GitHub metadata authentication.

The internal `download_workspace_release` context resolves a GitHub release,
downloads its descriptor and binds the selected workspace before requesting
the package and proof. Each request addresses the observed asset ID through
the GitHub API. Download locations are constructed by the adapter rather than
read from the descriptor, with redirects restricted to the API and supported
GitHub asset origins.

Package and proof files remain opaque and available only within that context.
A failed proof download also cleans up the temporary package. Configured CLI
authentication is rejected explicitly for this acquisition path rather than
silently changed to anonymous access. Retained proofs and cache orchestration
use the internal acquisition boundary below.

## Acquisition and workspace pin reuse

`GitHubWorkspaceAcquirer` connects release selection, retained proofs and the
existing workspace cache. Its caller supplies a local consumer policy and
an independently provisioned trusted root. Repository approval, policy expiry
and root identity are checked before source resolution.

`acquire` resolves the explicit release and its descriptor, then reuses valid
cached bytes or downloads the missing proof and package by observed asset ID.
The package must pass the existing detached provenance verifier before
extraction. Source revision, workspace, kit identity and version must agree
with the selection before cache publication.
Direct `--source SOURCE@RELEASE` performs this source resolution online on
every invocation, including when valid package bytes are cached. It does
not create or replace a project pin. With `--source`, global `-w` selects a
relative workspace inside that release. `--project` supplies operator
configuration without changing its pin. Without a project, direct
`plan`, `deploy` and `validate` require explicit Site inputs rather than
acquired example Sites.

`lease` accepts an already resolved selection. It checks the retained proof,
current local policy and roots, package bytes and materialized content without
contacting the source. Keep the lease open through inspection, planning and
execution. The returned `CachedWorkspace` uses the existing manifest resolver,
planner and executor with separate operator Sites.

| Operation | Source access | Package and proof transfer |
|---|---|---|
| First acquisition | Resolve release and descriptor | Acquire missing identified bytes |
| Explicit acquisition again | Resolve release and descriptor again | Reuse valid cached bytes |
| Direct CLI `--source SOURCE@RELEASE` | Resolve release and descriptor each invocation | Reuse valid bytes or acquire missing identified bytes |
| Use an existing workspace pin | None | None |
| Internal lease with missing proof | None, returns `cache.proof-missing` | Caller decides whether to restore |
| Corrupt entry during workspace pin reuse | None | Reject without automatic repair |

Every publication and lease invokes the trusted verifier. A changed local
policy can revalidate the same bytes without another download. Expired policy,
changed roots or a source outside the approved repository fail explicitly.
Stored receipts are records of evaluation, never permission to bypass it.

Ordinary commands for a selected project restore missing objects identified
by the workspace pin unless `--offline-content` was requested. That option
restricts content acquisition, not declared Azure target reads or execution.
Direct `--source` requires online resolution and rejects `--offline-content`.
Restoration requires
the fresh release selection to equal the workspace pin before package and
proof transfer. The internal `lease` itself remains local and never performs
that restoration.

The common `WorkspaceAcquisition` layer accepts a trusted verifier supplied by
application code. Its source expectations, proof storage and cache contracts
contain no GitHub transport fields. Other approved providers can use the same
boundary without adding an executor or changing operator configuration.

These internal APIs support
[workspace pins and configured Site execution](projects.md).
[Cache maintenance](cache.md) provides storage inspection and targeted
removal. Descriptive browsing uses its own
[reference and index cache](remote-content.md#use-cached-metadata-with-browse).
Those observations never authorize package execution.

## Publication integration

Candidate preparation builds each workspace from the reviewed declaration
through the existing package producer. A separate signing job creates one proof
for the ZIP and another for its build record. The collector verifies the record
before interpreting it and the ZIP before inspecting its metadata. Both proofs
must satisfy the independently supplied repository, source, and workflow
policy.

Only after every selected workspace passes collection does it generate the
descriptor and freeze its bytes with the package and proof identities.
Each workspace proof names one artifact, matching the current consumer
contract. The descriptor contains no trust policy or trusted root selection.
After approval, the publisher rechecks the exact package, proof, and descriptor
bytes against the approved inventory and source contracts, then uploads those
bytes.

Keep the internal frozen `SiteOpsReleaseAssets` inventory separate from the
public `WorkspaceReleaseAssets` routing descriptor. The inventory's publication
list is also separate from the identity and assets of an existing engine
release. Engine assets and workspace assets retain their independent version
and compatibility rules. Workspace delivery uses the existing release
pipeline.

See [workspace packages](workspace-packages.md) for package contents and
[artifact verification](artifact-verification.md) for consumer trust policy.

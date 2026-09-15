# Workspace release sources

`siteops-workspaces.json` identifies the workspace packages and detached proofs
in a content release. It lets an acquisition caller select one workspace
without inspecting every ZIP or requiring a discovery index.

This is an internal source contract. Release automation does not yet generate
or publish this descriptor, and public `plan` and `deploy` commands do not
acquire a release source.

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

The revision is opaque in the common format. A GitHub adapter supplies the
commit resolved from the selected release tag. Another provider supplies its
own immutable revision identity.

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

The descriptor is limited to 256 KiB and 64 workspaces. Individual artifact
identities are limited to 128 MiB, with stricter bounds imposed by their
consumers. The current GitHub proof verifier accepts proofs up to 2 MiB.
Duplicate JSON keys, unsupported fields and numbers outside JSON syntax are
rejected.

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
before package acquisition. The release snapshot retains GitHub IDs and its reported
immutable status separately. The host's release lock is useful evidence, but
does not replace exact digests, source identity or consumer provenance policy.
These APIs do not enable or require that repository setting.

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

A fixed engine worker runs with isolated Python imports. Its deadline covers
DNS, connection setup, headers and body reads. The default is 120 seconds,
with an internal maximum of 300 seconds. HTTP header lines are limited to
8 KiB and header count to 64. Worker output is bounded separately. Staging is
removed after the caller exits the context and worker exit is confirmed.
An unconfirmed exit retains staging and reports a warning.

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
silently changed to anonymous access. Retained proof inputs, verification/cache
orchestration and public command integration remain separate work.

## Publication integration

The descriptor should be generated after the package and proof bytes exist,
then frozen with their identities in the existing reviewed candidate inventory.
The publisher must upload those exact bytes rather than regenerate them.

Keep the existing internal `SiteOpsReleaseAssets` approval document separate
from this public routing contract. Engine assets and workspace assets retain
their independent version and compatibility rules. Extending publication
must use the existing release pipeline rather than introduce another system.

See [workspace packages](workspace-packages.md) for package contents and
[artifact verification](artifact-verification.md) for consumer trust policy.

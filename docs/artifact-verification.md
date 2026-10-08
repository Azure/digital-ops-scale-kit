# Artifact verification policy

Artifact verification binds downloaded bytes to publisher and workflow
evidence under policy that the consumer owns. An artifact digest identifies bytes.
A provenance proof identifies the workflow that attested to those bytes.

The verifier consumes an already downloaded artifact, its detached proof, a
separately provisioned trusted root snapshot, and trusted local policy. It
creates a receipt only after the artifact identity and verified observations
satisfy that policy.

This is an internal acquisition building block, not a deployment command or
public Python SDK. Source resolution, workspace pins and cache execution must
use the receipt together with their own identity and access boundaries.

## GitHub policy

The current adapter supports GitHub CLI 2.95 or newer in the 2.x release line
and reusable workflows in the same repository and commit.
The policy identifies both the reusable signing workflow and the build
workflow that calls it, plus one exact signing runner class.

An administrator supplies a policy with this shape, replacing the placeholders:

```json
{
  "apiVersion": "siteops/v1alpha1",
  "kind": "ArtifactVerificationPolicy",
  "id": "approved-workspace-content",
  "version": 1,
  "validUntil": "<UTC deadline with timezone>",
  "trustedRootSha256": "<SHA-256 of the independently provisioned root snapshot>",
  "provider": {
    "kind": "github-attestation/v1",
    "repository": "example/content",
    "sourceRef": "refs/heads/main",
    "signerWorkflow": ".github/workflows/sign.yml",
    "builderWorkflow": ".github/workflows/release.yml",
    "runnerEnvironment": "github-hosted"
  }
}
```

An acquisition caller must supply the exact source commit and expected
artifact SHA-256 from a trusted source resolver. Source resolution happens
outside this verifier. A package, release note body, or verifier policy
echo cannot supply the consumer's publisher policy.

`siteops source enroll NAME` without trust files writes this shape for the
publisher's standard release contract: `sourceRef` `refs/heads/main`,
`signerWorkflow` `.github/workflows/_workspace-distribution.yaml`,
`builderWorkflow` `.github/workflows/release.yaml` and `runnerEnvironment`
`self-hosted`, valid for 30 days. Supply your own policy when your publisher
or enrollment period differs. See [approved sources](projects.md#use-an-approved-source).

`runnerEnvironment` is required. Choose `github-hosted` or `self-hosted`
according to your independently reviewed publisher policy. There is no
inference from the downloaded proof and no acceptance of both classes through
one value. A `self-hosted` certificate does not identify a particular pool,
image or isolation configuration. Approve those controls separately.

The adapter checks the artifact hash before invoking GitHub CLI. It supplies
the detached proof and custom trusted root, exact certificate identity,
source and signer digests, source ref, GitHub OIDC issuer, SLSA predicate,
and SHA-256 algorithm explicitly. A `github-hosted` policy also passes
`--deny-self-hosted-runners`. Both policies require every verified certificate
to match the selected runner class exactly.
On Windows, it requires the native `gh.exe` executable rather than a batch
wrapper.

Successful tool exit alone is insufficient. Every returned result must
contain the expected subject, certificate/source/builder observations and a
supported verified timestamp. `verifiedIdentity` is a policy echo, not an
independent observation. Arbitrary signed predicate fields are not
qualification evidence.

## Receipt and lifecycle

### Release source observations

The internal `GitHubClient.resolve_release` method observes an explicitly
selected published release before acquisition. It resolves the exact tag
namespace, including a bounded chain of annotated tags, rather than treating
a branch or `target_commitish` as immutable source identity.

The result identifies the repository, release, tag object, source commit and
uploaded assets. Asset metadata is enumerated through bounded pagination.
Repository, release, tag and asset identities are rechecked before returning.
Missing asset digests remain unknown, and a selected asset must supply its
SHA-256 digest before acquisition can use it.

These observations come from the source API, not a cryptographic provenance
proof. They do not authenticate a publisher, select consumer policy or
authorize execution. `resolve_release` downloads neither the package nor its
proof. `project pin` uses the result only to identify selected assets before
verification. Existing workspace pins retain those exact identities rather
than following the release tag again.
The [workspace release descriptor](workspace-sources.md) connects those
observations to one selected package and its detached proof.

### Verification receipts

The `ArtifactVerification` receipt records:

- The exact artifact SHA-256 and size.
- Policy identity, version, exact file digest and validity deadline.
- Digests of the detached proof and trusted root.
- Verifier identity and the evaluation time.
- Verified observations inside an evidence object specific to the provider.

The common receipt does not require GitHub repository or workflow fields.
Those belong to the GitHub evidence object, so another verifier can preserve
the same artifact and policy identities.

Proof and root inputs are copied into staging owned by the invocation before
the native tool reads them. Artifact identity, policy expiry and policy file
identity are checked again before a receipt is returned. An expired or
changed policy fails explicitly.

The internal [workspace acquisition flow](workspace-sources.md#acquisition-and-workspace-pin-reuse)
retains identified proofs separately from receipts. It binds the selected
source to local consumer policy, then passes retained proof and root inputs
to this same verifier on each cache publication and lease. Pinned reuse
requires no source request. Policy and root paths are supplied by application
code, not by package or release metadata.

On POSIX, only the owner can access temporary files. Windows inherits the staging
parent's access controls, so acquisition must supply a protected location.
Cleanup failures are warnings and do not replace a primary verification
failure.

## Offline limits

A detached local proof plus a custom, independently provisioned trusted root
allows verification without a GitHub login or automatic trusted root retrieval.
Acquiring or refreshing those roots is a separate trusted operation.

This is historical cryptographic evidence under a particular root and policy
snapshot. It does not establish current key revocation status, current
attestation availability, the latest release, or deployment safety.
Receipts explicitly record `revocation: not-checked`.

Cache code must not treat a receipt filename or an editable JSON document as
execution authority. It must bind the expected artifact, current policy and
retained evidence, preserve expiry semantics, and revalidate content before
execution. Operator configuration and credentials remain outside package
content.

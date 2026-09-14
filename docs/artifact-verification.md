# Artifact verification policy

An artifact digest identifies bytes. A provenance proof identifies the
workflow that attested to those bytes. Consumer-owned policy decides which
publisher and workflow identities are acceptable.

The verification boundary consumes an already downloaded artifact, its
detached proof, a separately provisioned trusted-root snapshot, and trusted
local policy. It creates a receipt only after the artifact identity and
verified observations satisfy that policy.

This is an acquisition building block, not a new deployment command or a
public Python SDK. Source resolution, project pins and cache execution must
use the receipt together with their own identity and access boundaries.

## GitHub policy

The first adapter supports GitHub CLI version 2.95 or newer within version 2,
GitHub-hosted runners, and same-repository, same-commit reusable workflows.
The policy identifies both the reusable signing workflow and its top-level
build workflow. It does not accept a workflow-prefix match.

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
    "builderWorkflow": ".github/workflows/release.yml"
  }
}
```

The approved source resolver supplies the exact source commit and expected
artifact SHA-256 for each request. A package, release-note body, or verifier
policy echo cannot supply the consumer's publisher policy.

The adapter checks the artifact hash before invoking GitHub CLI. It supplies
the detached proof and custom trusted root, exact certificate identity,
source and signer digests, source ref, GitHub OIDC issuer, SLSA predicate,
hosted-runner requirement and SHA-256 algorithm explicitly.

Successful tool exit alone is insufficient. Every returned result must
contain the expected subject, certificate/source/builder observations and a
supported verified timestamp. `verifiedIdentity` is a policy echo, not an
independent observation. Arbitrary signed predicate fields are not
qualification evidence.

## Receipt and lifecycle

The `ArtifactVerification` receipt records:

- The exact artifact SHA-256 and size.
- Policy identity, version, exact file digest and validity deadline.
- Detached-proof and trusted-root digests.
- Verifier identity and the evaluation time.
- Verified observations inside a provider-specific evidence object.

The common receipt does not require GitHub repository or workflow fields.
Those belong to the GitHub evidence object, so another verifier can preserve
the same artifact and policy identities.

Proof and root inputs are copied into invocation-owned staging before the
native tool reads them. Artifact identity, policy expiry and policy-file
identity are checked again before a receipt is returned. An expired or
changed policy fails explicitly.

On POSIX, temporary files are owner-only. Windows inherits the staging
parent's access controls, so acquisition must supply a protected location.
Cleanup failures are warnings and do not replace a primary verification
failure.

## Offline limits

A detached local proof plus a custom, independently provisioned trusted root
allows verification without a GitHub login or automatic trust-root retrieval.
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

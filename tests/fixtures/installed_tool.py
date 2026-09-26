"""Closed local command double, not a cryptographic verifier or deployment provider."""

import hashlib
import json
import os
import sys
from pathlib import Path


def main():
    name = Path(sys.argv[0]).stem
    args = sys.argv[1:]
    context_path = Path(os.environ["SITEOPS_TEST_TOOL_CONTEXT"])
    root = context_path.parent.resolve()
    context = json.loads(context_path.read_bytes())
    if name == "az":
        if args == ["version", "--output", "json"]:
            print('{"azure-cli":"fixture"}')
            return
        allowed = context.get("allowedRead")
        assert allowed is not None, "Azure reads and deployment commands are forbidden."
        assert args == [
            "resource", "show", "--ids", allowed["id"],
            "--api-version", allowed["apiVersion"],
            "--subscription", allowed["subscription"],
            "--output", "json", "--only-show-errors",
        ], "Only the declared fixture resource read is permitted."
        print(json.dumps({
            "id": allowed["id"], "type": "Microsoft.Kubernetes/connectedClusters",
            "location": "eastus", "name": "existing-arc",
        }))
        return
    assert name == "gh"
    if args == ["--version"]:
        print("gh version 2.95.0 (installed command fixture)")
        return
    assert args[:2] == ["attestation", "verify"]
    for flag in ("--bundle", "--custom-trusted-root", "--source-digest", "--repo", "--cert-identity"):
        assert args.count(flag) == 1
    artifact = Path(args[2]).resolve()
    proof = Path(args[args.index("--bundle") + 1]).resolve()
    trusted = Path(args[args.index("--custom-trusted-root") + 1]).resolve()
    assert all(path.is_relative_to(root) for path in (artifact, proof, trusted))
    if "verifications" in context:
        context = context["verifications"][hashlib.sha256(artifact.read_bytes()).hexdigest()]
    repository = context.get("repository", "example/content")
    source_ref = context.get("ref", "refs/heads/main")
    signer = context.get("signer", ".github/workflows/sign.yml")
    builder = context.get("builder", ".github/workflows/release.yml")
    runner = context.get("runner", "github-hosted")
    signer_identity = f"https://github.com/{repository}/{signer}@{source_ref}"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == context["digest"]
    assert hashlib.sha256(proof.read_bytes()).hexdigest() == context["proof"]
    assert hashlib.sha256(trusted.read_bytes()).hexdigest() == context["root"]
    assert args[args.index("--source-digest") + 1] == context["revision"]
    assert args[args.index("--signer-digest") + 1] == context["revision"]
    assert args[args.index("--repo") + 1] == repository
    assert args[args.index("--source-ref") + 1] == source_ref
    assert args[args.index("--cert-identity") + 1] == signer_identity
    expected = [
        "attestation", "verify", str(artifact), "--hostname", "github.com",
        "--bundle", str(proof), "--custom-trusted-root", str(trusted),
        "--repo", repository, "--cert-identity", signer_identity,
        "--signer-digest", context["revision"], "--source-digest", context["revision"],
        "--source-ref", source_ref, "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--predicate-type", "https://slsa.dev/provenance/v1",
        *(["--deny-self-hosted-runners"] if runner == "github-hosted" else []),
        "--digest-alg", "sha256", "--format", "json",
    ]
    assert args == expected, "Only the exact local verification contract is permitted."
    print(json.dumps([{"verificationResult": {
        "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
        "statement": {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": "fixture", "digest": {"sha256": context["digest"]}}],
            "predicate": {},
        },
        "signature": {"certificate": {
            "subjectAlternativeName": signer_identity,
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": "https://github.com/" + repository,
            "sourceRepositoryDigest": context["revision"],
            "sourceRepositoryRef": source_ref,
            "buildSignerDigest": context["revision"],
            "runnerEnvironment": context.get("observedRunner", runner),
            "buildConfigURI": f"https://github.com/{repository}/{builder}@{source_ref}",
            "buildConfigDigest": context["revision"],
        }},
        "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2020-01-01T00:00:00Z"}],
    }}]))

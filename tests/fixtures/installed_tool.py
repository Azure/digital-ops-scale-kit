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
        assert args == ["version", "--output", "json"], "Deployment commands are forbidden."
        print('{"azure-cli":"fixture"}')
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
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == context["digest"]
    assert hashlib.sha256(proof.read_bytes()).hexdigest() == context["proof"]
    assert hashlib.sha256(trusted.read_bytes()).hexdigest() == context["root"]
    assert args[args.index("--source-digest") + 1] == context["revision"]
    assert args[args.index("--signer-digest") + 1] == context["revision"]
    assert args[args.index("--repo") + 1] == "example/content"
    assert args[args.index("--source-ref") + 1] == "refs/heads/main"
    assert args[args.index("--cert-identity") + 1] == (
        "https://github.com/example/content/.github/workflows/sign.yml@refs/heads/main"
    )
    expected = [
        "attestation", "verify", str(artifact), "--hostname", "github.com",
        "--bundle", str(proof), "--custom-trusted-root", str(trusted),
        "--repo", "example/content", "--cert-identity",
        "https://github.com/example/content/.github/workflows/sign.yml@refs/heads/main",
        "--signer-digest", context["revision"], "--source-digest", context["revision"],
        "--source-ref", "refs/heads/main", "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--predicate-type", "https://slsa.dev/provenance/v1",
        "--deny-self-hosted-runners", "--digest-alg", "sha256", "--format", "json",
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
            "subjectAlternativeName": "https://github.com/example/content/.github/workflows/sign.yml@refs/heads/main",
            "issuer": "https://token.actions.githubusercontent.com",
            "sourceRepositoryURI": "https://github.com/example/content",
            "sourceRepositoryDigest": context["revision"],
            "sourceRepositoryRef": "refs/heads/main",
            "buildSignerDigest": context["revision"],
            "runnerEnvironment": "github-hosted",
            "buildConfigURI": "https://github.com/example/content/.github/workflows/release.yml@refs/heads/main",
            "buildConfigDigest": context["revision"],
        }},
        "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2020-01-01T00:00:00Z"}],
    }}]))

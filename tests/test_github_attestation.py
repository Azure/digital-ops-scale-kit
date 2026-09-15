"""Consumer-owned attestation policy and verified-observation boundaries."""

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from siteops import github_attestation as verification
from siteops.artifacts import ArtifactError

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
COMMIT = "a" * 40
REPOSITORY = "example/content"
SOURCE_REF = "refs/heads/main"


def _policy(root_digest):
    return {
        "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
        "id": "approved-content", "version": 1, "validUntil": "2026-10-01T00:00:00Z",
        "trustedRootSha256": root_digest,
        "provider": {
            "kind": "github-attestation/v1", "repository": REPOSITORY,
            "sourceRef": SOURCE_REF,
            "signerWorkflow": ".github/workflows/sign.yml",
            "builderWorkflow": ".github/workflows/release.yml",
        },
    }


def _result(digest):
    return {
        "attestation": {"bundle": {"PRIVATE_UNTRUSTED": "do not copy"}},
        "verificationResult": {
            "mediaType": verification._RESULT_MEDIA_TYPE,
            "statement": {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": verification._PREDICATE,
                "subject": [{"name": "producer-controlled-name", "digest": {"sha256": digest}}],
                "predicate": {"PRIVATE_UNTRUSTED": "do not use as policy"},
            },
            "signature": {"certificate": {
                "subjectAlternativeName": f"https://github.com/{REPOSITORY}/.github/workflows/sign.yml@{SOURCE_REF}",
                "issuer": verification._ISSUER,
                "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
                "sourceRepositoryDigest": COMMIT,
                "sourceRepositoryRef": SOURCE_REF,
                "buildSignerDigest": COMMIT,
                "runnerEnvironment": "github-hosted",
                "buildConfigURI": f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@{SOURCE_REF}",
                "buildConfigDigest": COMMIT,
            }},
            "verifiedTimestamps": [
                {"type": "Tlog", "timestamp": "2026-09-13T23:00:00Z"},
            ],
            "verifiedIdentity": {"PRIVATE_UNTRUSTED": "a policy echo, not an observation"},
        },
    }


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.zip"
    artifact.write_bytes(b"opaque artifact bytes, not parsed by the provenance verifier")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    proof = tmp_path / "proof.jsonl"
    proof.write_bytes(b'{"fixture":"detached-proof"}\r\n')
    roots = tmp_path / "roots.jsonl"
    roots.write_bytes(b'{"fixture":"consumer-provisioned-roots"}\r\n')
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_policy(hashlib.sha256(roots.read_bytes()).hexdigest())), encoding="utf-8")
    executable = tmp_path / "gh.exe"
    executable.write_bytes(b"test executable placeholder")
    state = {"results": [_result(digest)], "code": 0, "calls": [], "version": b"gh version 2.95.0 (test)\n"}

    def run(argv):
        state["calls"].append(argv)
        if argv[1:] == ["--version"]:
            return 0, state["version"], b""
        assert argv[1:3] == ["attestation", "verify"]
        assert Path(argv[argv.index("--bundle") + 1]).read_bytes() == proof.read_bytes()
        assert Path(argv[argv.index("--custom-trusted-root") + 1]).read_bytes() == roots.read_bytes()
        return state["code"], json.dumps(state["results"]).encode(), b"PRIVATE_TOOL_DIAGNOSTIC"

    monkeypatch.setattr(verification, "resolve_tool_from_path", lambda name: str(executable))
    monkeypatch.setattr(verification, "_run_gh", run)
    monkeypatch.setattr(verification, "_now", lambda: NOW)
    return artifact, proof, roots, policy, digest, state


def _verify(inputs, **kwargs):
    artifact, proof, roots, policy, digest, _ = inputs
    return verification.verify_github_artifact(
        artifact, proof, roots, policy, expected_sha256=digest, source_commit=COMMIT,
        staging_parent=artifact.parent, **kwargs,
    )


def test_verifier_binds_exact_policy_root_artifact_and_observations(inputs):
    artifact, proof, roots, policy, digest, state = inputs
    result = _verify(inputs)
    document = result.document()
    assert document["subject"] == {"algorithm": "sha256", "digest": digest, "size": artifact.stat().st_size}
    assert document["policy"]["sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert document["proofSha256"] == hashlib.sha256(proof.read_bytes()).hexdigest()
    assert document["trustedRootSha256"] == hashlib.sha256(roots.read_bytes()).hexdigest()
    assert document["revocation"] == "not-checked"
    assert "PRIVATE_" not in result.serialized().decode()
    assert result.digest == hashlib.sha256(result.serialized()).hexdigest()
    assert len(state["calls"]) == 2
    argv = state["calls"][-1]
    expected = {
        "--repo": REPOSITORY, "--source-digest": COMMIT, "--signer-digest": COMMIT,
        "--source-ref": SOURCE_REF, "--cert-oidc-issuer": verification._ISSUER,
        "--predicate-type": verification._PREDICATE, "--format": "json",
        "--digest-alg": "sha256", "--hostname": "github.com",
    }
    for flag, value in expected.items():
        assert argv[argv.index(flag) + 1] == value
    assert "--deny-self-hosted-runners" in argv
    assert "--cert-identity-regex" not in argv
    assert "--no-public-good" not in argv
    assert not list(artifact.parent.glob("siteops-verification-*"))


@pytest.mark.parametrize("field", [
    "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
    "sourceRepositoryRef", "buildSignerDigest", "runnerEnvironment", "buildConfigURI", "buildConfigDigest",
])
def test_verified_certificate_observations_must_match_policy(inputs, field):
    state = inputs[-1]
    certificate = state["results"][0]["verificationResult"]["signature"]["certificate"]
    certificate[field] = "PRIVATE_WRONG_CLAIM"
    with pytest.raises(verification.VerificationError, match="certificate") as failed:
        _verify(inputs)
    assert "PRIVATE_" not in str(failed.value)


def test_policy_echo_cannot_replace_missing_certificate_evidence(inputs):
    state = inputs[-1]
    result = state["results"][0]["verificationResult"]
    result["verifiedIdentity"] = copy.deepcopy(result["signature"]["certificate"])
    del result["signature"]
    with pytest.raises(verification.VerificationError):
        _verify(inputs)


@pytest.mark.parametrize("mutate", [
    lambda result: result.update(mediaType="unsupported"),
    lambda result: result["statement"].update(predicateType="unsupported"),
    lambda result: result["statement"].update(subject=[]),
    lambda result: result["statement"]["subject"].append(copy.deepcopy(result["statement"]["subject"][0])),
    lambda result: result["statement"]["subject"][0]["digest"].update(sha256="0" * 64),
    lambda result: result.update(verifiedTimestamps=[]),
    lambda result: result["verifiedTimestamps"][0].update(type="unverified"),
    lambda result: result["verifiedTimestamps"][0].update(timestamp="2026-09-14T00:00:00"),
    lambda result: result["verifiedTimestamps"][0].update(timestamp="2027-01-01T00:00:00Z"),
])
def test_malformed_or_mismatched_evidence_fails_closed(inputs, mutate):
    mutate(inputs[-1]["results"][0]["verificationResult"])
    with pytest.raises(verification.VerificationError):
        _verify(inputs)


def test_each_returned_match_is_checked_but_repeated_valid_attestations_are_supported(inputs):
    state = inputs[-1]
    state["results"].append(copy.deepcopy(state["results"][0]))
    assert len(_verify(inputs).document()["evidence"]["matches"]) == 2
    state["results"][1]["verificationResult"]["signature"]["certificate"]["buildSignerDigest"] = "b" * 40
    with pytest.raises(verification.VerificationError):
        _verify(inputs)


@pytest.mark.parametrize("results", [[], {}, None])
def test_empty_or_unsupported_result_shape_is_not_success(inputs, results):
    inputs[-1]["results"] = results
    with pytest.raises(verification.VerificationError):
        _verify(inputs)


def test_nonzero_exit_does_not_accept_success_shaped_output(inputs):
    inputs[-1]["code"] = 1
    with pytest.raises(verification.VerificationError, match="did not pass") as failed:
        _verify(inputs)
    assert "PRIVATE_TOOL_DIAGNOSTIC" not in str(failed.value)


@pytest.mark.parametrize("changed", ["artifact", "root", "expired-policy"])
def test_input_identity_and_policy_failures_precede_native_tool_execution(inputs, changed):
    artifact, _, roots, policy, _, state = inputs
    if changed == "artifact":
        artifact.write_bytes(b"changed")
    elif changed == "root":
        roots.write_bytes(b"changed")
    else:
        document = json.loads(policy.read_bytes())
        document["validUntil"] = "2026-09-01T00:00:00Z"
        policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ArtifactError):
        _verify(inputs)
    assert state["calls"] == []


@pytest.mark.parametrize("version", [b"gh version 2.90.0\n", b"gh version 3.0.0\n", b"unknown\n"])
def test_unqualified_verifier_versions_are_rejected(inputs, version):
    inputs[-1]["version"] = version
    with pytest.raises(verification.VerificationError):
        _verify(inputs)
    assert len(inputs[-1]["calls"]) == 1


def test_artifact_changes_during_verification_do_not_receive_a_receipt(inputs, monkeypatch):
    original = verification._run_gh

    def changed(argv):
        result = original(argv)
        if argv[1:3] == ["attestation", "verify"]:
            inputs[0].write_bytes(b"changed after tool read")
        return result

    monkeypatch.setattr(verification, "_run_gh", changed)
    with pytest.raises(verification.VerificationError, match="changed"):
        _verify(inputs)


def test_policy_expiry_during_verification_is_rechecked(inputs, monkeypatch):
    times = iter([NOW, datetime(2026, 10, 2, tzinfo=timezone.utc)])
    monkeypatch.setattr(verification, "_now", lambda: next(times))
    with pytest.raises(verification.VerificationError, match="expired during"):
        _verify(inputs)


def test_policy_changes_during_verification_do_not_receive_a_receipt(inputs, monkeypatch):
    original = verification._run_gh

    def changed(argv):
        result = original(argv)
        if argv[1:3] == ["attestation", "verify"]:
            document = json.loads(inputs[3].read_bytes())
            document["version"] = 2
            inputs[3].write_text(json.dumps(document), encoding="utf-8")
        return result

    monkeypatch.setattr(verification, "_run_gh", changed)
    with pytest.raises(verification.VerificationError, match="policy changed"):
        _verify(inputs)


def test_proof_and_root_receipts_describe_the_native_inputs_not_later_path_contents(inputs, monkeypatch):
    original = verification._run_gh
    proof_digest = hashlib.sha256(inputs[1].read_bytes()).hexdigest()
    root_digest = hashlib.sha256(inputs[2].read_bytes()).hexdigest()

    def changed(argv):
        result = original(argv)
        if argv[1:3] == ["attestation", "verify"]:
            inputs[1].write_bytes(b"later proof")
            inputs[2].write_bytes(b"later roots")
        return result

    monkeypatch.setattr(verification, "_run_gh", changed)
    document = _verify(inputs).document()
    assert document["proofSha256"] == proof_digest
    assert document["trustedRootSha256"] == root_digest


def test_cleanup_warning_does_not_hide_the_primary_verification_failure(inputs, monkeypatch, caplog):
    inputs[-1]["code"] = 1
    original = Path.unlink

    def failed(path, *args, **kwargs):
        if path.parent.name.startswith("siteops-verification-"):
            raise OSError("fixture cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed)
    with pytest.raises(verification.VerificationError, match="did not pass"):
        _verify(inputs)
    assert "cleanup" in caplog.text


def test_duplicate_policy_keys_are_rejected_before_tool_execution(inputs):
    inputs[3].write_bytes(b'{"id":"first","id":"PRIVATE_SECOND"}')
    with pytest.raises(verification.VerificationError, match="duplicate"):
        _verify(inputs)
    assert inputs[-1]["calls"] == []


@pytest.mark.parametrize("limit", ["MAX_POLICY_BYTES", "MAX_EVIDENCE_BYTES", "MAX_RESULT_BYTES"])
def test_verification_inputs_and_output_are_bounded(inputs, monkeypatch, limit):
    monkeypatch.setattr(verification, limit, 8)
    with pytest.raises(ArtifactError, match="limit"):
        _verify(inputs)
    if limit != "MAX_RESULT_BYTES":
        assert inputs[-1]["calls"] == []

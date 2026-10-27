"""Consumer-owned attestation policy and verified-observation boundaries."""

import copy
import hashlib
import json
import re
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
            "runnerEnvironment": "github-hosted",
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


def _gh_filters_accept(argv, claims):
    """Model the certificate filters of gh 2.95 attestation verify for the options Site Ops passes.

    --repo also fixes the source repository and the signer repository prefix, and the issuer
    defaults to GitHub Actions, so a run without the identity options still applies those.
    """
    def option(name):
            return argv[argv.index(name) + 1] if name in argv else None

    repository = f"https://github.com/{option('--repo')}"
    checks = [
            claims["issuer"] == (option("--cert-oidc-issuer") or verification._ISSUER),
            claims["sourceRepositoryURI"] == repository,
            claims["subjectAlternativeName"].startswith(repository + "/"),
    ]
    for name, field in (
            ("--cert-identity", "subjectAlternativeName"), ("--source-ref", "sourceRepositoryRef"),
            ("--source-digest", "sourceRepositoryDigest"), ("--signer-digest", "buildSignerDigest"),
    ):
            if option(name) is not None:
                checks.append(claims[field] == option(name))
    if "--deny-self-hosted-runners" in argv:
            checks.append(claims["runnerEnvironment"] == "github-hosted")
    return all(checks)


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
    state = {
        "results": [_result(digest)], "code": 0, "calls": [], "version": b"gh version 2.95.0 (test)\n",
        "diagnostic": None, "claims": None, "signature": True,
    }

    def run(argv):
        state["calls"].append(argv)
        if argv[1:] == ["--version"]:
            return 0, state["version"], b""
        assert argv[1:3] == ["attestation", "verify"]
        assert Path(argv[argv.index("--bundle") + 1]).read_bytes() == proof.read_bytes()
        assert Path(argv[argv.index("--custom-trusted-root") + 1]).read_bytes() == roots.read_bytes()
        if state["claims"] is not None:
            if not (state["signature"] and _gh_filters_accept(argv, state["claims"])):
                return 1, b"", b"PRIVATE_TOOL_DIAGNOSTIC"
            result = copy.deepcopy(state["results"][0])
            result["verificationResult"]["signature"]["certificate"] = dict(state["claims"])
            return 0, json.dumps([result]).encode(), b""
        if "--cert-identity" not in argv and state["diagnostic"] is not None:
            code, output = state["diagnostic"]
            return code, output, b"PRIVATE_TOOL_DIAGNOSTIC"
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


@pytest.mark.parametrize("expected", ["github-hosted", "self-hosted"])
@pytest.mark.parametrize("observed", ["github-hosted", "self-hosted", None, "unknown"])
def test_runner_class_requires_an_exact_policy_match(inputs, expected, observed):
    policy_path, state = inputs[3], inputs[-1]
    policy = json.loads(policy_path.read_bytes())
    policy["provider"]["runnerEnvironment"] = expected
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    state["results"][0]["verificationResult"]["signature"]["certificate"]["runnerEnvironment"] = observed
    if expected == observed:
        receipt = _verify(inputs)
        assert receipt.document()["evidence"]["matches"][0]["certificate"]["runnerEnvironment"] == expected
    else:
        with pytest.raises(verification.VerificationError, match="runner class") as failed:
            _verify(inputs)
        assert failed.value.reason == "runner"
    assert ("--deny-self-hosted-runners" in state["calls"][1]) is (expected == "github-hosted")


@pytest.mark.parametrize("value", ["", "*", "Self-Hosted", "1ES", ["self-hosted"], None, True])
def test_unsupported_runner_policy_fails_before_tool_execution(inputs, value):
    policy = json.loads(inputs[3].read_bytes())
    policy["provider"]["runnerEnvironment"] = value
    inputs[3].write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="runnerEnvironment"):
        _verify(inputs)
    assert inputs[-1]["calls"] == []


def test_runner_policy_is_required_rather_than_inferred_from_the_proof(inputs):
    policy = json.loads(inputs[3].read_bytes())
    del policy["provider"]["runnerEnvironment"]
    inputs[3].write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="missing fields"):
        _verify(inputs)
    assert inputs[-1]["calls"] == []


_FIELD_REASONS = {
    "subjectAlternativeName": "signer", "issuer": "issuer", "sourceRepositoryURI": "repository",
    "sourceRepositoryDigest": "commit", "sourceRepositoryRef": "ref", "buildSignerDigest": "commit",
    "runnerEnvironment": "runner", "buildConfigURI": "caller", "buildConfigDigest": "commit",
}


@pytest.mark.parametrize("field", sorted(_FIELD_REASONS))
def test_verified_certificate_observations_must_match_policy(inputs, field):
    state = inputs[-1]
    certificate = state["results"][0]["verificationResult"]["signature"]["certificate"]
    certificate[field] = "PRIVATE_WRONG_CLAIM"
    with pytest.raises(verification.VerificationError) as failed:
        _verify(inputs)
    assert failed.value.reason == _FIELD_REASONS[field]
    assert str(failed.value) == verification._PROOF_FAILURES[_FIELD_REASONS[field]]
    assert "PRIVATE_" not in str(failed.value)


def test_a_changed_branch_is_named_before_the_workflow_identities_that_embed_it(inputs):
    certificate = inputs[-1]["results"][0]["verificationResult"]["signature"]["certificate"]
    for key in ("sourceRepositoryRef", "subjectAlternativeName", "buildConfigURI"):
        certificate[key] = certificate[key].replace(SOURCE_REF, "refs/heads/PRIVATE_BRANCH")
    with pytest.raises(verification.VerificationError) as failed:
        _verify(inputs)
    assert failed.value.reason == "ref"


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
    with pytest.raises(verification.VerificationError, match="did not pass verification") as failed:
        _verify(inputs)
    assert failed.value.reason == "unclassified"
    assert "PRIVATE_TOOL_DIAGNOSTIC" not in str(failed.value)


@pytest.mark.parametrize(("diagnostic", "reason"), [
    ("rejected", "unclassified"),
    ("runner", "runner"),
    ("ref", "ref"),
    ("commit", "commit"),
    ("signer", "signer"),
    ("matches", "unclassified"),
    ("malformed", "unclassified"),
])
def test_rejected_proof_names_a_closed_reason_without_admitting_the_artifact(inputs, diagnostic, reason):
    state = inputs[-1]
    state["code"] = 1
    result = copy.deepcopy(state["results"][0])
    certificate = result["verificationResult"]["signature"]["certificate"]
    changed = {
        "runner": ("runnerEnvironment", "self-hosted"),
        "ref": ("sourceRepositoryRef", "refs/heads/PRIVATE_BRANCH"),
        "commit": ("buildSignerDigest", "b" * 40),
        "signer": ("subjectAlternativeName", "https://github.com/example/content/PRIVATE_SIGNER"),
    }.get(diagnostic)
    if changed:
        certificate[changed[0]] = changed[1]
    output = b"{PRIVATE_MALFORMED" if diagnostic == "malformed" else json.dumps([result]).encode()
    state["diagnostic"] = (1 if diagnostic == "rejected" else 0, output)
    with pytest.raises(verification.VerificationError) as failed:
        _verify(inputs)
    assert failed.value.reason == reason
    assert failed.value.code == "artifact.invalid"
    assert str(failed.value) == verification._PROOF_FAILURES[reason]
    assert "PRIVATE_" not in str(failed.value)
    strict, relaxed = state["calls"][1:]
    assert "--cert-identity" in strict
    assert relaxed[:3] == strict[:3]
    for option in ("--cert-identity", "--source-ref", "--source-digest", "--signer-digest",
                   "--deny-self-hosted-runners"):
        assert option not in relaxed
    assert relaxed[relaxed.index("--repo") + 1] == REPOSITORY
    assert not list(inputs[0].parent.glob("siteops-verification-*"))


def test_successful_policy_verification_runs_no_diagnostic(inputs):
    _verify(inputs)
    assert sum(argv[1:3] == ["attestation", "verify"] for argv in inputs[-1]["calls"]) == 1


_OTHER = "https://github.com/PRIVATE-owner/PRIVATE-repo"


@pytest.mark.parametrize(("case", "changes", "reason"), [
    # gh still applies these filters in the diagnostic run, so the failure cannot be told apart.
    ("bad-proof", {}, "unclassified"),
    ("other-repository", {
        "sourceRepositoryURI": _OTHER,
        "subjectAlternativeName": f"{_OTHER}/.github/workflows/sign.yml@{SOURCE_REF}",
        "buildConfigURI": f"{_OTHER}/.github/workflows/release.yml@{SOURCE_REF}",
    }, "unclassified"),
    ("other-issuer", {"issuer": "https://PRIVATE-issuer.example"}, "unclassified"),
    ("signer-in-other-repository", {
        "subjectAlternativeName": f"{_OTHER}/.github/workflows/sign.yml@{SOURCE_REF}",
    }, "unclassified"),
    # Claims that pass the diagnostic filters are observed and named.
    ("ref", {
        "sourceRepositoryRef": "refs/heads/PRIVATE_BRANCH",
        "subjectAlternativeName": f"https://github.com/{REPOSITORY}/.github/workflows/sign.yml@refs/heads/PRIVATE_BRANCH",
        "buildConfigURI": f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/heads/PRIVATE_BRANCH",
    }, "ref"),
    ("commit", {"sourceRepositoryDigest": "b" * 40, "buildSignerDigest": "b" * 40}, "commit"),
    ("signer", {
        "subjectAlternativeName": f"https://github.com/{REPOSITORY}/.github/workflows/PRIVATE.yml@{SOURCE_REF}",
    }, "signer"),
    ("runner", {"runnerEnvironment": "self-hosted"}, "runner"),
])
def test_diagnosis_names_only_claims_that_pass_the_retained_gh_filters(inputs, case, changes, reason):
    state = inputs[-1]
    claims = dict(state["results"][0]["verificationResult"]["signature"]["certificate"])
    claims.update(changes)
    state["claims"] = claims
    state["signature"] = case != "bad-proof"
    with pytest.raises(verification.VerificationError) as failed:
        _verify(inputs)
    assert failed.value.reason == reason
    assert failed.value.code == "artifact.invalid"
    assert str(failed.value) == verification._PROOF_FAILURES[reason]
    assert "trusted root" not in str(failed.value)
    assert "PRIVATE" not in str(failed.value)
    # The strict run rejected the proof, and the diagnostic run supplied the reason.
    strict, diagnostic = state["calls"][1:]
    assert "--cert-identity" in strict
    assert "--cert-identity" not in diagnostic


def test_troubleshooting_lists_each_proof_failure_message():
    guide = (Path(__file__).resolve().parents[1] / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    section = guide.split('### "The artifact proof names ..."', 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| ((?:a |did ).+?) \|", section, re.MULTILINE)
    listed = [phrase for row in rows for phrase in re.split(r" or (?=a )", row)]
    assert [message for message in verification._PROOF_FAILURES.values()
            if not any(phrase in message for phrase in listed)] == []


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
    with pytest.raises(verification.VerificationError, match="did not pass verification"):
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

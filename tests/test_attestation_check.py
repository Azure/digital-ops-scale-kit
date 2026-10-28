"""Exercise the fixed diagnostic subject and observed claims without signing."""

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.shell_helpers import required_bash, write_executable
from tests.yaml_documents import cached_safe_load

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DOCUMENT = cached_safe_load((WORKFLOWS / "_attestation-check.yaml").read_text(encoding="utf-8"))
CI = cached_safe_load((WORKFLOWS / "ci.yaml").read_text(encoding="utf-8"))
SUBJECT = b"Site Ops 1ES attestation diagnostic. Not a release.\n"
SHA = "a" * 40
REPOSITORY = "example/content"
REF = "refs/heads/diagnostic"
SIGNER = f"https://github.com/{REPOSITORY}/.github/workflows/_attestation-check.yaml@{REF}"


def step(job, name):
    return next(item for item in DOCUMENT["jobs"][job]["steps"] if item.get("name") == name)


CREATE = step("attest", "Create the fixed non-release subject")
RETAIN = step("attest", "Retain the detached diagnostic proof")
VERIFY = step("verify", "Verify the diagnostic signature and exact source")
COMPARE = step("verify", "Compare the verified certificate claims")


def environment(tmp_path):
    return {
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
        "RUNNER_TEMP": str(tmp_path), "RUNNER_CLASS": "self-hosted",
        "SOURCE_SHA": SHA, "SOURCE_REF": REF, "SOURCE_REPOSITORY": REPOSITORY,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }


def run_python(item, tmp_path, **changes):
    return subprocess.run(
        [sys.executable, "-I", "-c", item["run"]], env={**environment(tmp_path), **changes},
        cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15,
    )


def evidence(tmp_path):
    directory = tmp_path / "attestation-check"
    directory.mkdir()
    (directory / "runner-check.txt").write_bytes(SUBJECT)
    (directory / "runner-check.txt.attestation.jsonl").write_bytes(b"synthetic proof")
    return directory


def result():
    return {
        "attestation": {"PRIVATE_UNTRUSTED": "not certificate evidence"},
        "verificationResult": {
            "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
            "signature": {"certificate": {
                "subjectAlternativeName": SIGNER,
                "issuer": "https://token.actions.githubusercontent.com",
                "sourceRepositoryURI": f"https://github.com/{REPOSITORY}",
                "sourceRepositoryDigest": SHA, "sourceRepositoryRef": REF,
                "buildSignerDigest": SHA, "runnerEnvironment": "self-hosted",
                "buildConfigURI": f"https://github.com/{REPOSITORY}/.github/workflows/ci.yaml@{REF}",
                "buildConfigDigest": SHA,
            }},
            "statement": {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [{"name": "runner-check.txt", "digest": {"sha256": hashlib.sha256(SUBJECT).hexdigest()}}],
            },
            "verifiedTimestamps": [
                {"type": "Tlog", "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
            ],
        },
    }


def compare(tmp_path, results=None, raw=None):
    (tmp_path / "summary").write_text("existing\n")
    payload = json.dumps(results).encode() if raw is None else raw
    (tmp_path / "attestation-check-verification.json").write_bytes(payload)
    completed = run_python(COMPARE, tmp_path)
    return completed, (tmp_path / "summary").read_text()


def assert_rejected(completed, summary):
    assert completed.returncode != 0 and "::error::" in completed.stderr
    assert summary == "existing\n"
    assert "PRIVATE_" not in completed.stdout + completed.stderr


def test_diagnostic_requires_ci_and_admission_without_release_authority():
    assert "permanent public signing record" in CI[True]["workflow_dispatch"]["inputs"]["run-mode"]["description"]
    caller = CI["jobs"]["attestation-check"]
    assert caller["if"] == "${{ github.event_name == 'workflow_dispatch' && inputs.run-mode == 'attestation-check' }}"
    assert caller["needs"] == ["lint", "test", "validate"]
    assert caller["uses"] == "./.github/workflows/_attestation-check.yaml"
    assert caller["with"] == {"expected-source-sha": "${{ inputs.expected-source-sha }}"}
    assert caller["permissions"] == {
        "contents": "read", "actions": "read", "id-token": "write", "attestations": "write",
    }
    assert "secrets" not in caller
    assert DOCUMENT[True] == {"workflow_call": {"inputs": {
        "expected-source-sha": {"required": True, "type": "string"},
    }}}
    assert set(DOCUMENT["jobs"]) == {"admit", "attest", "verify"}
    admission, signer, verifier = (DOCUMENT["jobs"][key] for key in ("admit", "attest", "verify"))
    assert admission["uses"] == "./.github/workflows/_release-runner.yaml"
    assert admission["with"] == {"expected-source-sha": "${{ inputs.expected-source-sha }}", "mode": "attestation"}
    assert admission["permissions"] == {"contents": "read"}
    assert signer["needs"] == "admit"
    assert signer["runs-on"] == "${{ fromJSON(needs.admit.outputs.labels) }}"
    assert signer["permissions"] == {"contents": "read", "id-token": "write", "attestations": "write"}
    assert verifier["needs"] == "attest" and verifier["runs-on"] == "ubuntu-24.04"
    assert verifier["permissions"] == {"contents": "read", "actions": "read"}
    assert all("if" not in job for job in (signer, verifier))
    pins = {
        "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
        "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
        "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    }
    for job in (signer, verifier):
        assert job["timeout-minutes"] <= 10 and "environment" not in job
        for item in job["steps"]:
            if "uses" in item:
                action, pin = item["uses"].split("@")
                assert pins[action] == pin
    attest = step("attest", "Attest the diagnostic subject")
    assert attest["with"] == {
        "subject-path": "${{ runner.temp }}/attestation-check/runner-check.txt", "show-summary": False,
    }
    assert "if" not in attest
    assert CREATE["env"] == {"RUNNER_CLASS": "${{ runner.environment }}"}


def test_evidence_transfer_and_retention_bind_the_exact_run_and_verifier():
    download = step("verify", "Download the exact diagnostic evidence")["with"]
    assert download == {
        "artifact-ids": "${{ needs.attest.outputs.artifact-id }}", "repository": "${{ github.repository }}",
        "run-id": "${{ github.run_id }}", "github-token": "${{ github.token }}",
        "path": "${{ runner.temp }}/attestation-check", "merge-multiple": True, "digest-mismatch": "error",
    }
    assert DOCUMENT["jobs"]["attest"]["outputs"]["artifact-id"] == "${{ steps.upload.outputs.artifact-id }}"
    assert VERIFY["id"] == "verify"
    assert VERIFY["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    assert VERIFY["env"]["SOURCE_REF"] == "${{ github.ref }}"
    assert VERIFY["env"]["SIGNER_IDENTITY"] == (
        "https://github.com/${{ github.repository }}/.github/workflows/_attestation-check.yaml@${{ github.ref }}"
    )
    assert COMPARE["env"] == {
        "SOURCE_SHA": "${{ github.sha }}", "SOURCE_REF": "${{ github.ref }}",
        "SOURCE_REPOSITORY": "${{ github.repository }}",
    }
    retained = step("verify", "Retain cryptographically verified observations")
    assert retained["if"] == "${{ always() && steps.verify.outcome == 'success' }}"
    for job, name, prefix in (
        ("attest", "Upload the non-release evidence", "runner-attestation"),
        ("verify", "Retain cryptographically verified observations", "runner-attestation-verification"),
    ):
        upload = step(job, name)["with"]
        assert upload["name"] == prefix + "-${{ github.run_id }}-${{ github.run_attempt }}"
        assert upload["retention-days"] == 7 and upload["overwrite"] is False
        assert upload["if-no-files-found"] == "error"
    assert step("attest", "Upload the non-release evidence")["with"]["path"] == "${{ runner.temp }}/attestation-check"
    assert retained["with"]["path"] == "${{ runner.temp }}/attestation-check-verification.json"


@pytest.mark.parametrize("platform,runner,allowed", [
    ("linux", "self-hosted", True), ("linux", "github-hosted", False), ("win32", "self-hosted", False),
])
def test_only_the_fixed_subject_is_created_on_the_expected_runner(tmp_path, platform, runner, allowed):
    simulated = {**CREATE, "run": f"import sys\nsys.platform = {platform!r}\n" + CREATE["run"]}
    completed = run_python(simulated, tmp_path, RUNNER_CLASS=runner)
    assert (completed.returncode == 0) is allowed, completed.stderr
    subject = tmp_path / "attestation-check" / "runner-check.txt"
    assert subject.exists() is allowed
    if allowed:
        assert subject.read_bytes() == SUBJECT
        assert list(subject.parent.iterdir()) == [subject]


@pytest.mark.parametrize("case", ["valid", "changed-subject", "empty-proof", "large-proof", "directory", "symlink"])
def test_detached_proof_is_bounded_and_preserves_fixed_bytes(tmp_path, case):
    if case == "symlink" and sys.platform != "linux":
        pytest.skip("Exercise unprivileged symlinks in the isolated Linux lane.")
    directory = evidence(tmp_path)
    target = directory / "runner-check.txt.attestation.jsonl"
    target.unlink()
    bundle = tmp_path / "bundle.jsonl"
    if case == "directory":
        bundle.mkdir()
    elif case == "symlink":
        bundle.symlink_to(directory / "runner-check.txt")
    else:
        bundle.write_bytes(b"" if case == "empty-proof" else b"x" * (2097153 if case == "large-proof" else 10))
    if case == "changed-subject":
        (directory / "runner-check.txt").write_bytes(b"PRIVATE_CHANGED")
    completed = run_python(RETAIN, tmp_path, BUNDLE_PATH=str(bundle))
    assert (completed.returncode == 0) is (case == "valid"), completed.stderr
    assert target.exists() is (case == "valid")
    if case == "valid":
        assert target.read_bytes() == bundle.read_bytes()
    assert "PRIVATE_" not in completed.stdout + completed.stderr


@pytest.mark.parametrize("kind", ["Tlog", "TimestampAuthority"])
def test_verified_claims_accept_each_valid_match_without_echoing_untrusted_fields(tmp_path, kind):
    document = result()
    document["verificationResult"]["verifiedTimestamps"][0]["type"] = kind
    completed, summary = compare(tmp_path, [document, copy.deepcopy(document)])
    assert completed.returncode == 0, completed.stderr
    assert "exact fork source" in summary and SHA in summary
    assert "does not identify a particular pool" in summary
    assert "PRIVATE_" not in summary + completed.stdout + completed.stderr


@pytest.mark.parametrize("field", [
    "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
    "sourceRepositoryRef", "buildSignerDigest", "runnerEnvironment", "buildConfigURI", "buildConfigDigest",
])
def test_each_verified_certificate_field_must_match(tmp_path, field):
    document = result()
    certificate = document["verificationResult"]["signature"]["certificate"]
    certificate[field] = "github-hosted" if field == "runnerEnvironment" else "PRIVATE_WRONG"
    assert_rejected(*compare(tmp_path, [result(), document]))


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(mediaType="PRIVATE_WRONG"),
    lambda value: value["statement"].update(_type="PRIVATE_WRONG"),
    lambda value: value["statement"].update(predicateType="PRIVATE_WRONG"),
    lambda value: value["statement"].update(subject=[]),
    lambda value: value["statement"]["subject"].append(copy.deepcopy(value["statement"]["subject"][0])),
    lambda value: value["statement"]["subject"][0]["digest"].update(sha256="0" * 64),
    lambda value: value.update(signature={"certificate": None}),
    lambda value: value.update(statement=[]),
    lambda value: value.update(verifiedTimestamps=[]),
    lambda value: value.update(verifiedTimestamps=[None]),
    lambda value: value.update(verifiedTimestamps=[{}] * 33),
    lambda value: value["verifiedTimestamps"][0].update(type="PRIVATE_UNVERIFIED"),
])
def test_unsupported_evidence_cannot_emit_a_success_summary(tmp_path, mutate):
    document = result()
    mutate(document["verificationResult"])
    assert_rejected(*compare(tmp_path, [document]))


@pytest.mark.parametrize("timestamp", [
    "", None, 0, [], "PRIVATE_BAD", "2026-99-99T00:00:00Z",
    "2026-09-14T00:00:00", "2026-09-14T00:00:00+03:00", "9999-01-01T00:00:00Z",
])
def test_verified_timestamps_must_be_valid_utc_and_not_in_the_future(tmp_path, timestamp):
    document = result()
    document["verificationResult"]["verifiedTimestamps"][0]["timestamp"] = timestamp
    assert_rejected(*compare(tmp_path, [document]))


@pytest.mark.parametrize("raw", [
    b"", b"null", b"{}", b"[]", b"[null]", b'[{"verificationResult":null}]',
    b'[{"verificationResult":{},"verificationResult":{}}]', b"[NaN]",
    b"[" * 2000 + b"0" + b"]" * 2000, b"x" * 8388609,
], ids=["empty", "null", "object", "empty-list", "null-entry", "null-verification", "duplicate-key", "nonfinite", "depth", "size"])
def test_result_parsing_is_bounded_and_rejects_ambiguous_json(tmp_path, raw):
    assert_rejected(*compare(tmp_path, raw=raw))


def test_result_count_is_bounded(tmp_path):
    assert_rejected(*compare(tmp_path, [result()] * 129))


@pytest.fixture
def verifier(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Execute Linux verifier resource limits in the isolated Linux lane.")
    directory = evidence(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    expected = [
        "attestation", "verify", str(directory / "runner-check.txt"),
        "--bundle", str(directory / "runner-check.txt.attestation.jsonl"),
        "--repo", REPOSITORY, "--cert-identity", SIGNER,
        "--signer-digest", SHA, "--source-digest", SHA, "--source-ref", REF,
        "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
        "--predicate-type", "https://slsa.dev/provenance/v1",
        "--digest-alg", "sha256", "--hostname", "github.com", "--format", "json",
    ]
    (tmp_path / "expected.json").write_text(json.dumps(expected))
    (tmp_path / "synthetic.json").write_text(json.dumps([result()]))
    double = tmp_path / "gh.py"
    double.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path('calls.json').write_text(json.dumps(sys.argv[1:]))\n"
        "if sys.argv[1:] != json.loads(Path('expected.json').read_text()):\n"
        "    raise SystemExit('Unexpected verifier invocation.')\n"
        "payload = Path('synthetic.json').read_bytes()\n"
        "if os.environ.get('OVERFLOW'):\n"
        "    for unused in range(129):\n"
        "        os.write(int(os.environ['OVERFLOW']), b'x' * 65536)\n"
        "sys.stdout.buffer.write(payload)\n"
        "sys.stderr.write('PRIVATE_TOOL_DIAGNOSTIC')\n"
        "sys.exit(int(os.environ.get('EXIT_CODE', '0')))\n"
    )
    write_executable(bin_dir / "gh", f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(double))} \"$@\"\n")
    write_executable(bin_dir / "python3", f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I \"$@\"\n")
    home = tmp_path / "home"
    home.mkdir()

    def invoke(**changes):
        return subprocess.run(
            [str(required_bash()), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", VERIFY["run"]],
            cwd=tmp_path, env={
                **environment(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home),
                "GH_CONFIG_DIR": str(home / "github"), "GH_PROMPT_DISABLED": "1",
                "GITHUB_REPOSITORY": REPOSITORY, "SIGNER_IDENTITY": SIGNER, **changes,
            },
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20,
        )
    return invoke


def test_actual_verifier_call_requires_exact_identity_and_detached_proof(tmp_path, verifier):
    completed = verifier()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads((tmp_path / "calls.json").read_text()) == json.loads((tmp_path / "expected.json").read_text())
    assert (tmp_path / "attestation-check-verification.json").read_bytes() == (tmp_path / "synthetic.json").read_bytes()
    assert "PRIVATE_" not in completed.stdout + completed.stderr
    assert not (tmp_path / "summary").exists()


def test_empty_successful_tool_output_is_not_verified_evidence(tmp_path, verifier):
    (tmp_path / "synthetic.json").write_bytes(b"")
    completed = verifier()
    assert completed.returncode != 0
    assert "verified diagnostic result exceeds its supported size" in completed.stdout
    assert "PRIVATE_" not in completed.stdout + completed.stderr


@pytest.mark.parametrize("case", ["changed", "extra", "missing", "empty", "large", "directory", "symlink"])
def test_invalid_evidence_is_rejected_before_invoking_the_verifier(tmp_path, verifier, case):
    directory = tmp_path / "attestation-check"
    subject, proof = directory / "runner-check.txt", directory / "runner-check.txt.attestation.jsonl"
    if case == "changed":
        subject.write_bytes(b"PRIVATE_CHANGED")
    elif case == "extra":
        (directory / "PRIVATE_EXTRA").write_text("extra")
    elif case in {"empty", "large"}:
        proof.write_bytes(b"" if case == "empty" else b"x" * 2097153)
    else:
        proof.unlink()
        if case == "directory":
            proof.mkdir()
        elif case == "symlink":
            proof.symlink_to(subject)
    completed = verifier()
    assert completed.returncode != 0
    assert not (tmp_path / "calls.json").exists()
    assert "PRIVATE_" not in completed.stdout + completed.stderr


@pytest.mark.parametrize("changes", [{"EXIT_CODE": "1"}, {"OVERFLOW": "1"}, {"OVERFLOW": "2"}])
def test_failed_or_unbounded_tool_output_is_not_verified_evidence(tmp_path, verifier, changes):
    completed = verifier(**changes)
    assert completed.returncode != 0
    assert "Diagnostic signature verification failed" in completed.stdout
    assert "PRIVATE_" not in completed.stdout + completed.stderr
    for suffix in ("json", "err"):
        assert (tmp_path / f"attestation-check-verification.{suffix}").stat().st_size <= 8388608

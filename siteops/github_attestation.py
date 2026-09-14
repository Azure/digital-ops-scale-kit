# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Detached GitHub attestation verification under consumer-owned local policy."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from packaging.version import Version

from siteops.artifact_verification import ArtifactVerification, utc_text
from siteops.artifacts import ArtifactError, hash_file, open_regular_file, relative_artifact_path
from siteops.browse import BrowseError
from siteops.compilation import resolve_tool_from_path
from siteops.content_metadata import require_mapping, validate_envelope
from siteops.github_source import _run_gh

MAX_POLICY_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
_ISSUER = "https://token.actions.githubusercontent.com"
_PREDICATE = "https://slsa.dev/provenance/v1"
_RESULT_MEDIA_TYPE = "application/vnd.dev.sigstore.verificationresult+json;version=0.1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
logger = logging.getLogger(__name__)


class VerificationError(ArtifactError):
    """A value-safe failure before an artifact can receive a verification receipt."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > maximum
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise VerificationError("Verification metadata must use bounded printable text.")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise VerificationError("Verification identities must be lowercase SHA-256 digests.")
    return value


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, maximum=64).replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError("Verification timestamps must use an explicit timezone.") from None
    if parsed.tzinfo is None:
        raise VerificationError("Verification timestamps must use an explicit timezone.")
    return parsed.astimezone(timezone.utc)


def _json(raw: bytes, maximum: int) -> Any:
    if len(raw) > maximum:
        raise VerificationError("Verification metadata exceeds its byte limit.")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise VerificationError("Verification metadata contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise VerificationError("Verification metadata contains an unsupported JSON number.")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, VerificationError):
            raise
        raise VerificationError("Verification metadata must be bounded UTF-8 JSON.") from None


def _record(value: Any, keys: set[str] | None = None) -> dict[str, Any]:
    try:
        result = require_mapping(value, keys)
    except ValueError:
        raise VerificationError("Verification metadata has an unsupported object shape.") from None
    if keys is not None and result.keys() != keys:
        raise VerificationError("Verification metadata has missing fields.")
    return result


def _read(path: Path, maximum: int) -> bytes:
    try:
        with open_regular_file(path) as stream:
            value = stream.read(maximum + 1)
    except OSError:
        raise VerificationError("A verification input could not be read.") from None
    if len(value) > maximum:
        raise VerificationError("A verification input exceeds its byte limit.")
    return value


@dataclass(frozen=True)
class GitHubArtifactPolicy:
    policy_id: str
    version: int
    valid_until: datetime
    trusted_root_sha256: str
    repository: str
    source_ref: str
    signer_workflow: str
    builder_workflow: str
    sha256: str

    @property
    def repository_uri(self) -> str:
        return "https://github.com/" + self.repository

    def workflow_identity(self, workflow: str) -> str:
        return self.repository_uri + "/" + workflow + "@" + self.source_ref


def load_github_policy(path: Path) -> GitHubArtifactPolicy:
    """Read trusted local configuration, never a policy suggested by a package."""
    raw = _read(path, MAX_POLICY_BYTES)
    try:
        document = validate_envelope(
            _json(raw, MAX_POLICY_BYTES), "ArtifactVerificationPolicy",
            {"apiVersion", "kind", "id", "version", "validUntil", "trustedRootSha256", "provider"},
        )
    except ValueError as error:
        if isinstance(error, VerificationError):
            raise
        raise VerificationError("The artifact verification policy is unsupported.") from None
    document = _record(document, {
        "apiVersion", "kind", "id", "version", "validUntil", "trustedRootSha256", "provider",
    })
    version = document["version"]
    if type(version) is not int or not 1 <= version <= 2**31 - 1:
        raise VerificationError("The verification policy version must be a positive integer.")
    provider = _record(document["provider"], {
        "kind", "repository", "sourceRef", "signerWorkflow", "builderWorkflow",
    })
    if provider["kind"] != "github-attestation/v1":
        raise VerificationError("The verification policy provider is unsupported.")
    repository = _text(provider["repository"], maximum=256)
    if not _REPOSITORY.fullmatch(repository) or any(part in {".", ".."} for part in repository.split("/")):
        raise VerificationError("The verification policy repository is invalid.")
    reference = _text(provider["sourceRef"], maximum=256)
    if not reference.startswith(("refs/heads/", "refs/tags/")) or any(c.isspace() for c in reference):
        raise VerificationError("The verification policy requires an explicit source ref.")
    workflows = []
    for key in ("signerWorkflow", "builderWorkflow"):
        value = relative_artifact_path(_text(provider[key]))
        if not value.startswith(".github/workflows/") or not value.endswith((".yaml", ".yml")):
            raise VerificationError("Verification workflow identities must name repository workflows.")
        workflows.append(value)
    return GitHubArtifactPolicy(
        _text(document["id"], maximum=128), version, _timestamp(document["validUntil"]),
        _digest(document["trustedRootSha256"]), repository, reference,
        workflows[0], workflows[1], hashlib.sha256(raw).hexdigest(),
    )


def _resolve_verifier() -> str:
    executable = resolve_tool_from_path("gh.exe" if os.name == "nt" else "gh")
    if executable is None:
        raise VerificationError("GitHub CLI 2.95 or newer is required for detached verification.")
    try:
        path = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError):
        raise VerificationError("The GitHub CLI executable could not be resolved.") from None
    if not path.is_absolute() or not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise VerificationError("Use an installed GitHub CLI executable, not a shell wrapper.")
    return str(path)


def _run(argv: list[str]) -> tuple[int, bytes]:
    try:
        code, stdout, _ = _run_gh(argv)
    except BrowseError:
        raise VerificationError("The verification tool could not complete within its output/time limits.") from None
    return code, stdout


def _observations(
    payload: bytes, policy: GitHubArtifactPolicy, digest: str, source_commit: str, evaluated_at: datetime,
) -> bytes:
    results = _json(payload, MAX_RESULT_BYTES)
    if not isinstance(results, list) or not 1 <= len(results) <= 128:
        raise VerificationError("The verifier returned no bounded successful evidence set.")
    expected = {
        "subjectAlternativeName": policy.workflow_identity(policy.signer_workflow),
        "issuer": _ISSUER,
        "sourceRepositoryURI": policy.repository_uri,
        "sourceRepositoryDigest": source_commit,
        "sourceRepositoryRef": policy.source_ref,
        "buildSignerDigest": source_commit,
        "runnerEnvironment": "github-hosted",
        "buildConfigURI": policy.workflow_identity(policy.builder_workflow),
        "buildConfigDigest": source_commit,
    }
    observations = []
    for result in results:
        verification = _record(_record(result).get("verificationResult"))
        if verification.get("mediaType") != _RESULT_MEDIA_TYPE:
            raise VerificationError("The verifier evidence format is unsupported.")
        statement = _record(verification.get("statement"))
        if (
            statement.get("_type") != "https://in-toto.io/Statement/v1"
            or statement.get("predicateType") != _PREDICATE
        ):
            raise VerificationError("The verified provenance statement has an unsupported type.")
        subjects = statement.get("subject")
        if not isinstance(subjects, list) or len(subjects) != 1:
            raise VerificationError("The verified statement must identify one artifact subject.")
        subject = _record(subjects[0])
        if _record(subject.get("digest")).get("sha256") != digest:
            raise VerificationError("The verified subject does not match the expected artifact.")
        certificate = _record(_record(verification.get("signature")).get("certificate"))
        if any(certificate.get(key) != value for key, value in expected.items()):
            raise VerificationError("The verified certificate does not satisfy publisher/source policy.")
        timestamps = verification.get("verifiedTimestamps")
        if not isinstance(timestamps, list) or not 1 <= len(timestamps) <= 32:
            raise VerificationError("The verifier did not establish a bounded timestamp set.")
        times = []
        for value in timestamps:
            timestamp = _record(value)
            kind = timestamp.get("type")
            if kind not in {"Tlog", "TimestampAuthority"}:
                raise VerificationError("The verified timestamp type is unsupported.")
            observed_at = _timestamp(timestamp.get("timestamp"))
            if observed_at > evaluated_at + timedelta(minutes=5):
                raise VerificationError("The verified timestamp is ahead of the local clock.")
            times.append({"type": kind, "timestamp": utc_text(observed_at)})
        observations.append({
            "certificate": {key: certificate[key] for key in expected},
            "predicateType": _PREDICATE,
            "subjectSha256": digest,
            "verifiedTimestamps": times,
        })
    return json.dumps({
        "provider": "github-attestation/v1", "resultMediaType": _RESULT_MEDIA_TYPE,
        "matches": observations,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def verify_github_artifact(
    artifact: Path,
    proof: Path,
    trusted_root: Path,
    policy_file: Path,
    *,
    expected_sha256: str,
    source_commit: str,
    staging_parent: Path | None = None,
) -> ArtifactVerification:
    """Verify a pinned artifact and detached proof without login or trust-root retrieval.

    The caller supplies the source commit and artifact identity from its
    approved source resolver. This first policy supports same-repository,
    same-commit reusable workflows. It makes no live revocation claim.
    """
    artifact, proof, trusted_root, policy_file = (
        Path(os.path.abspath(path)) for path in (artifact, proof, trusted_root, policy_file)
    )
    if staging_parent is not None:
        staging_parent = Path(os.path.abspath(staging_parent))
    digest = _digest(expected_sha256)
    if not isinstance(source_commit, str) or not _COMMIT.fullmatch(source_commit):
        raise VerificationError("GitHub verification requires an exact source commit.")
    policy = load_github_policy(policy_file)
    evaluated_at = _now()
    if evaluated_at >= policy.valid_until:
        raise VerificationError("The artifact verification policy has expired. Refresh it explicitly.")
    size, actual = hash_file(artifact, limit=MAX_ARTIFACT_BYTES)
    if actual != digest:
        raise VerificationError("The artifact SHA-256 does not match the expected source identity.")
    proof_bytes = _read(proof, MAX_EVIDENCE_BYTES)
    root_bytes = _read(trusted_root, MAX_EVIDENCE_BYTES)
    root_digest = hashlib.sha256(root_bytes).hexdigest()
    if root_digest != policy.trusted_root_sha256:
        raise VerificationError("The trusted-root snapshot does not match consumer policy.")
    executable = _resolve_verifier()
    code, output = _run([executable, "--version"])
    match = re.match(rb"gh version ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", output)
    if code != 0 or match is None:
        raise VerificationError("The GitHub CLI version could not be established.")
    version = match.group(1).decode("ascii")
    if not Version("2.95.0") <= Version(version) < Version("3"):
        raise VerificationError("This verification adapter requires GitHub CLI 2.95 or newer in version 2.")
    root: Path | None = None
    created: list[Path] = []
    try:
        root = Path(tempfile.mkdtemp(prefix="siteops-verification-", dir=staging_parent))
        for name, content in (("proof.jsonl", proof_bytes), ("trusted-root.jsonl", root_bytes)):
            path = root / name
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
            )
            created.append(path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        code, output = _run([
            executable, "attestation", "verify", str(artifact.resolve()),
            "--hostname", "github.com", "--bundle", str(root / "proof.jsonl"),
            "--custom-trusted-root", str(root / "trusted-root.jsonl"),
            "--repo", policy.repository,
            "--cert-identity", policy.workflow_identity(policy.signer_workflow),
            "--signer-digest", source_commit, "--source-digest", source_commit,
            "--source-ref", policy.source_ref,
            "--cert-oidc-issuer", _ISSUER, "--predicate-type", _PREDICATE,
            "--deny-self-hosted-runners", "--digest-alg", "sha256", "--format", "json",
        ])
        if code != 0:
            raise VerificationError("The detached artifact proof did not pass verification.")
        if hash_file(artifact, limit=MAX_ARTIFACT_BYTES) != (size, digest):
            raise VerificationError("The artifact changed during verification.")
        evidence = _observations(output, policy, digest, source_commit, evaluated_at)
    except OSError:
        raise VerificationError("Verification input staging could not be completed.") from None
    finally:
        cleanup_failed = False
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                cleanup_failed = True
        if root is not None:
            try:
                root.rmdir()
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            logger.warning("Verification staging cleanup could not be completed.")
    if _now() >= policy.valid_until:
        raise VerificationError("The verification policy expired during evaluation.")
    if hashlib.sha256(_read(policy_file, MAX_POLICY_BYTES)).hexdigest() != policy.sha256:
        raise VerificationError("The consumer policy changed during verification. Retry under its current version.")
    return ArtifactVerification(
        digest, size, policy.policy_id, policy.version, policy.sha256, root_digest,
        hashlib.sha256(proof_bytes).hexdigest(), evaluated_at, policy.valid_until,
        "gh", version, evidence,
    )

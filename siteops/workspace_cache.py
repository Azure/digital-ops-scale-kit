# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Internal content-addressed workspace storage, independent of source providers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import hash_file, open_regular_file, path_inventory
from siteops.cache_filesystem import (
    CacheError,
    cache_lock,
    check_private_node,
    make_private_directory,
)
from siteops.cache_layout import CACHE_DIR_ENV as CACHE_DIR_ENV
from siteops.cache_layout import CacheLayout
from siteops.cache_layout import cache_io as _cache_io
from siteops.cache_layout import cleanup_directory as _cleanup_created_directory
from siteops.cache_layout import default_cache_root as default_cache_root
from siteops.cache_layout import write_new as _write_new
from siteops.workspace_package import (
    MAX_ARCHIVE_BYTES,
    MAX_NODES,
    PACKAGE_NAME,
    MaterializedPackageBinding,
    PackageInspection,
    extract_package,
    inspect_package,
)
from siteops.workspace_source import ArtifactIdentity

logger = logging.getLogger(__name__)
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
ArtifactVerifier = Callable[[Path], ArtifactVerification]
SourceCheck = Callable[[PackageInspection], None]


def _digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CacheError("Cache identities must be lowercase SHA-256 digests.", code="cache.identity")
    return value


def _copy_artifact(
    source: Path, destination: Path, expected: str, *,
    limit: int = MAX_ARCHIVE_BYTES, expected_size: int | None = None,
) -> None:
    digest = hashlib.sha256()
    size = 0
    with open_regular_file(source) as original:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            while chunk := original.read(min(1024 * 1024, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise CacheError("The artifact exceeds its cache byte limit.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    if digest.hexdigest() != expected or (expected_size is not None and size != expected_size):
        raise CacheError("The artifact differs from its expected size or SHA-256.", code="cache.identity")


@dataclass(frozen=True)
class CachedWorkspace:
    """Verified content usable only while the enclosing cache lease is held."""

    package_root: Path
    inspection: PackageInspection
    verification: ArtifactVerification

    def bind(self, manifest: str) -> MaterializedPackageBinding:
        return MaterializedPackageBinding.bind(self.inspection, self.package_root, manifest)


class WorkspaceCache(CacheLayout):
    """Internal local storage with immutable objects and separate verification receipts.

    Callers supply independently resolved artifact/source identities and a trusted
    verifier. The verifier runs for every publication and use, including warm
    hits. Persisted receipts are evidence, never execution authorization.
    """

    def _object(self, digest: str) -> Path:
        return self.root / "objects" / "sha256" / _digest(digest)

    def _prepare_proofs(self) -> None:
        self.prepare_namespace("proofs")

    @contextmanager
    def _locked_proof(self, expected: ArtifactIdentity, *, exclusive: bool) -> Iterator[Path]:
        if not isinstance(expected, ArtifactIdentity):
            raise CacheError("A retained proof requires an artifact identity.")
        self._check_root()
        with cache_lock(
            self.root / "locks" / f"proof-{expected.sha256}.lock",
            exclusive=exclusive, timeout=self.lock_timeout,
        ):
            self._check_root()
            yield self.root / "proofs" / "sha256" / expected.sha256

    def _read_proof(self, root: Path, expected: ArtifactIdentity) -> Path:
        try:
            root.lstat()
        except FileNotFoundError:
            raise CacheError(
                "The selected proof is not cached. Acquire that exact proof before use.",
                code="cache.proof-missing",
            ) from None
        check_private_node(root, directory=True)
        if {entry.name for entry in root.iterdir()} != {"proof.bin"}:
            raise CacheError("The cached proof is incomplete or has unexpected paths.")
        path = root / "proof.bin"
        check_private_node(path, directory=False)
        if hash_file(path, limit=expected.size) != (expected.size, expected.sha256):
            raise CacheError("The cached proof differs from its expected identity.", code="cache.identity")
        return path

    def retain_proof(self, proof: Path, expected: ArtifactIdentity) -> None:
        """Retain exact opaque proof bytes, independently of any claim of publisher trust."""
        if not isinstance(expected, ArtifactIdentity):
            raise CacheError("A retained proof requires an artifact identity.")
        with _cache_io():
            self._prepare_proofs()
            with self._locked_proof(expected, exclusive=True) as target:
                try:
                    target.lstat()
                except FileNotFoundError:
                    pass
                else:
                    self._read_proof(target, expected)
                    return
                candidate = self.root / "staging" / f"proof-{uuid.uuid4().hex}"
                make_private_directory(candidate)
                published = False
                try:
                    _copy_artifact(
                        proof, candidate / "proof.bin", expected.sha256,
                        limit=expected.size, expected_size=expected.size,
                    )
                    self._read_proof(candidate, expected)
                    candidate.rename(target)
                    published = True
                finally:
                    if not published:
                        _cleanup_created_directory(candidate)

    @contextmanager
    def lease_proof(self, expected: ArtifactIdentity) -> Iterator[Path]:
        """Hold identified proof bytes through verification and check them again on return."""
        with self._locked_proof(expected, exclusive=False) as root:
            with _cache_io():
                path = self._read_proof(root, expected)
            yield path
            with _cache_io():
                self._read_proof(root, expected)

    @contextmanager
    def _locked(self, digest: str, *, exclusive: bool) -> Iterator[Path]:
        self._check_root()
        with cache_lock(
            self.root / "locks" / f"{_digest(digest)}.lock",
            exclusive=exclusive, timeout=self.lock_timeout,
        ):
            self._check_root()
            yield self._object(digest)

    def _verify(
        self, archive: Path, digest: str, verifier: ArtifactVerifier,
    ) -> ArtifactVerification:
        check_private_node(archive, directory=False)
        size, actual = hash_file(archive, limit=MAX_ARCHIVE_BYTES)
        if actual != digest:
            raise CacheError("The cached archive differs from its expected identity.", code="cache.identity")
        verification = verifier(archive)
        if not isinstance(verification, ArtifactVerification):
            raise CacheError("The selected verifier did not return artifact evidence.")
        if (verification.sha256, verification.size) != (actual, size):
            raise CacheError("Verification evidence names a different artifact.", code="cache.identity")
        now = datetime.now(timezone.utc)
        if (
            verification.evaluated_at.tzinfo is None
            or verification.valid_until.tzinfo is None
            or verification.evaluated_at > now
            or verification.valid_until <= now
            or verification.evaluated_at >= verification.valid_until
        ):
            raise CacheError("The artifact verification policy is not currently valid.", code="cache.policy")
        if hash_file(archive, limit=MAX_ARCHIVE_BYTES) != (size, digest):
            raise CacheError("The package changed during verification.", code="cache.identity")
        return verification

    def _validate_content(
        self, content: Path, inspection: PackageInspection, source_revision: str,
    ) -> None:
        if not source_revision or inspection.metadata.source_revision != source_revision:
            raise CacheError("The package revision differs from the resolved source.", code="cache.identity")
        inspection.validate_materialization(content)
        check_private_node(content, directory=True)
        files = [PACKAGE_NAME, *(entry.path for entry in inspection.metadata.files)]
        for relative in path_inventory(files, limit=MAX_NODES):
            check_private_node(content.joinpath(*relative.split("/")), directory=True)
        for relative in files:
            check_private_node(content.joinpath(*relative.split("/")), directory=False)

    def _record(self, digest: str, verification: ArtifactVerification) -> None:
        key = hashlib.sha256("".join((
            _digest(verification.policy_sha256), _digest(verification.root_sha256),
            _digest(verification.proof_sha256),
        )).encode("ascii")).hexdigest()
        if datetime.now(timezone.utc) >= verification.valid_until:
            raise CacheError("The artifact verification policy expired during cache validation.", code="cache.policy")
        try:
            raw = verification.serialized()
        except (TypeError, ValueError):
            raise CacheError("Artifact verification evidence could not be recorded.") from None
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise CacheError("Artifact verification evidence exceeds the cache limit.")
        directory = self.root / "receipts" / digest
        try:
            make_private_directory(directory)
        except FileExistsError:
            pass
        check_private_node(directory, directory=True)
        candidate = directory / f".{uuid.uuid4().hex}.tmp"
        created = False
        try:
            _write_new(candidate, raw)
            created = True
            target = directory / f"{key}.json"
            try:
                target.lstat()
            except FileNotFoundError:
                pass
            else:
                check_private_node(target, directory=False)
            candidate.replace(target)
            created = False
        finally:
            if created:
                try:
                    candidate.unlink()
                except OSError:
                    logger.warning("Cache receipt staging cleanup could not be completed.")

    def _read_object(
        self, root: Path, digest: str, source_revision: str, verify: ArtifactVerifier,
        check_source: SourceCheck | None = None,
    ) -> CachedWorkspace:
        try:
            root.lstat()
        except FileNotFoundError:
            raise CacheError(
                "The selected workspace package is not cached. Acquire that exact package before use.",
                code="cache.missing",
            ) from None
        check_private_node(root, directory=True)
        if {entry.name for entry in root.iterdir()} != {"package.zip", "content"}:
            raise CacheError("The cached package is incomplete or has unexpected paths.")
        archive = root / "package.zip"
        verification = self._verify(archive, digest, verify)
        inspection = inspect_package(archive, digest)
        content = root / "content"
        self._validate_content(content, inspection, source_revision)
        if check_source is not None:
            check_source(inspection)
        self._record(digest, verification)
        return CachedWorkspace(content, inspection, verification)

    def publish(
        self, archive: Path, expected_sha256: str, *,
        source_revision: str, verify: ArtifactVerifier,
        check_source: SourceCheck | None = None,
    ) -> PackageInspection:
        """Verify and atomically publish an archive under its SHA-256 identity.

        The supplied verifier runs before extraction and when an existing
        object is reused. Corrupt objects fail rather than being repaired or
        replaced in place. An optional trusted source check runs after package
        inspection and before publication or reuse. The operation performs no
        download.
        """
        digest = _digest(expected_sha256)
        with _cache_io(), self._locked(digest, exclusive=True) as target:
            try:
                target.lstat()
            except FileNotFoundError:
                pass
            else:
                return self._read_object(target, digest, source_revision, verify, check_source).inspection
            candidate = self.root / "staging" / uuid.uuid4().hex
            make_private_directory(candidate)
            published = False
            try:
                copied = candidate / "package.zip"
                _copy_artifact(archive, copied, digest)
                verification = self._verify(copied, digest, verify)
                inspection = extract_package(copied, digest, candidate / "content")
                self._validate_content(candidate / "content", inspection, source_revision)
                if check_source is not None:
                    check_source(inspection)
                self._record(digest, verification)
                candidate.rename(target)
                published = True
                return inspection
            finally:
                if not published:
                    _cleanup_created_directory(candidate)

    @contextmanager
    def lease(
        self, expected_sha256: str, *, source_revision: str, verify: ArtifactVerifier,
        check_source: SourceCheck | None = None,
    ) -> Iterator[CachedWorkspace]:
        """Revalidate cached content and policy under a shared use lease.

        Keep the context open through browsing, preparation and deployment.
        The trusted verifier must use retained local proof/root inputs for
        offline use. The cache never reads persisted receipts as authority.
        """
        digest = _digest(expected_sha256)
        with self._locked(digest, exclusive=False) as root:
            with _cache_io():
                content = self._read_object(root, digest, source_revision, verify, check_source)
            yield content

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Content-addressed workspace storage, independent of source providers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import ArtifactError, hash_file, open_regular_file, path_inventory
from siteops.cache_filesystem import (
    CacheError,
    cache_lock,
    check_cache_ancestors,
    check_private_node,
    make_private_directory,
)
from siteops.workspace_package import (
    MAX_ARCHIVE_BYTES,
    MAX_NODES,
    PACKAGE_NAME,
    MaterializedPackageBinding,
    PackageInspection,
    extract_package,
    inspect_package,
)

logger = logging.getLogger(__name__)
CACHE_DIR_ENV = "SITEOPS_CACHE_DIR"
_MARKER = b'{"apiVersion":"siteops/v1alpha1","kind":"WorkspaceCache"}\n'
_DIRECTORIES = ("objects", "receipts", "staging", "locks")
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
ArtifactVerifier = Callable[[Path], ArtifactVerification]


def _digest(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise CacheError("Cache identities must be lowercase SHA-256 digests.", code="cache.identity")
    return value


def default_cache_root(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the one cache override or platform default without creating files."""
    env = os.environ if environment is None else environment
    selected = env.get(CACHE_DIR_ENV)
    if selected is not None:
        value, suffix = selected, ()
    elif os.name == "nt":
        value, suffix = env.get("LOCALAPPDATA", ""), ("siteops", "cache")
    elif env.get("XDG_CACHE_HOME") and Path(env["XDG_CACHE_HOME"]).is_absolute():
        value, suffix = env["XDG_CACHE_HOME"], ("siteops",)
    else:
        value, suffix = str(Path.home()), (".cache", "siteops")
    if not value.strip():
        raise CacheError("The cache location is empty. Select an absolute cache directory.", code="cache.path")
    try:
        root = Path(value).expanduser()
    except RuntimeError:
        raise CacheError("The cache location could not be expanded.", code="cache.path") from None
    if not root.is_absolute() or root == root.parent or ".." in root.parts:
        raise CacheError("The cache must be an absolute, non-root directory.", code="cache.path")
    return root.joinpath(*suffix)


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        complete = True
    finally:
        if not complete:
            try:
                path.unlink()
            except OSError:
                logger.warning("Cache file staging cleanup could not be completed.")


def _copy_archive(source: Path, destination: Path, expected: str) -> None:
    digest = hashlib.sha256()
    size = 0
    with open_regular_file(source) as original:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            while chunk := original.read(min(1024 * 1024, MAX_ARCHIVE_BYTES - size + 1)):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise CacheError("The package exceeds the cache archive limit.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    if digest.hexdigest() != expected:
        raise CacheError("The package SHA-256 does not match the selected source.", code="cache.identity")


def _cleanup_created_directory(path: Path) -> None:
    """Remove only a directory created by this operation, never a published object."""
    try:
        check_private_node(path, directory=True)
        shutil.rmtree(path)
    except (OSError, ArtifactError):
        logger.warning("Cache staging cleanup could not be completed.")


@contextmanager
def _cache_io() -> Iterator[None]:
    try:
        yield
    except OSError:
        raise CacheError("The cache operation could not complete its file access.", code="cache.io") from None


@dataclass(frozen=True)
class CachedWorkspace:
    """Verified content usable only while the enclosing cache lease is held."""

    package_root: Path
    inspection: PackageInspection
    verification: ArtifactVerification

    def bind(self, manifest: str) -> MaterializedPackageBinding:
        return MaterializedPackageBinding.bind(self.inspection, self.package_root, manifest)


class WorkspaceCache:
    """Private local storage with immutable objects and separate verification receipts.

    Callers supply independently resolved artifact/source identities and a trusted
    verifier. The verifier runs for every publication and use, including warm
    hits. Persisted receipts are evidence, never execution authorization.
    """

    def __init__(self, root: Path | None = None, *, lock_timeout: float = 20):
        self.root = default_cache_root() if root is None else Path(root)
        self.lock_timeout = lock_timeout
        if not self.root.is_absolute() or self.root == self.root.parent or ".." in self.root.parts:
            raise CacheError("The cache must be an absolute, non-root directory.", code="cache.path")
        try:
            self._initialize()
        except OSError:
            raise CacheError("The private cache directory could not be prepared.", code="cache.path") from None

    def _check_root(self) -> None:
        check_cache_ancestors(self.root)
        check_private_node(self.root, directory=True)
        marker = self.root / "cache.json"
        check_private_node(marker, directory=False)
        with open_regular_file(marker) as stream:
            if stream.read(len(_MARKER) + 1) != _MARKER:
                raise CacheError("The selected directory is not a supported Site Ops cache.")
        if {entry.name for entry in self.root.iterdir()} != {"cache.json", *_DIRECTORIES}:
            raise CacheError("The cache root contains an unexpected path.")
        for name in (*_DIRECTORIES, "objects/sha256"):
            check_private_node(self.root.joinpath(*name.split("/")), directory=True)

    def _initialize(self) -> None:
        try:
            self.root.lstat()
        except FileNotFoundError:
            pass
        else:
            self._check_root()
            return
        missing: list[Path] = []
        parent = self.root.parent
        while True:
            try:
                parent.lstat()
                break
            except FileNotFoundError:
                missing.append(parent)
                parent = parent.parent
        for directory in reversed(missing):
            check_cache_ancestors(directory)
            try:
                make_private_directory(directory)
            except FileExistsError:
                pass
            check_private_node(directory, directory=True)
        check_cache_ancestors(self.root)
        candidate = self.root.parent / f".siteops-cache-{uuid.uuid4().hex}"
        make_private_directory(candidate)
        published = False
        try:
            _write_new(candidate / "cache.json", _MARKER)
            for name in _DIRECTORIES:
                make_private_directory(candidate / name)
            make_private_directory(candidate / "objects" / "sha256")
            try:
                candidate.rename(self.root)
                published = True
            except OSError:
                # Another initializer can win publication with its complete root.
                if not self.root.exists():
                    raise
            self._check_root()
        finally:
            if not published:
                _cleanup_created_directory(candidate)

    def _object(self, digest: str) -> Path:
        return self.root / "objects" / "sha256" / _digest(digest)

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
    ) -> CachedWorkspace:
        try:
            root.lstat()
        except FileNotFoundError:
            raise CacheError(
                "The pinned package is not cached. Acquire that exact package before use.",
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
        self._record(digest, verification)
        return CachedWorkspace(content, inspection, verification)

    def publish(
        self, archive: Path, expected_sha256: str, *,
        source_revision: str, verify: ArtifactVerifier,
    ) -> PackageInspection:
        """Copy and verify an opaque local archive before extraction and atomic publication.

        Reuse an existing valid object under the same verifier. Corrupt objects
        fail rather than being repaired or replaced in place. Nothing downloads.
        """
        digest = _digest(expected_sha256)
        with _cache_io(), self._locked(digest, exclusive=True) as target:
            try:
                target.lstat()
            except FileNotFoundError:
                pass
            else:
                return self._read_object(target, digest, source_revision, verify).inspection
            candidate = self.root / "staging" / uuid.uuid4().hex
            make_private_directory(candidate)
            published = False
            try:
                copied = candidate / "package.zip"
                _copy_archive(archive, copied, digest)
                verification = self._verify(copied, digest, verify)
                inspection = extract_package(copied, digest, candidate / "content")
                self._validate_content(candidate / "content", inspection, source_revision)
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
    ) -> Iterator[CachedWorkspace]:
        """Revalidate cached bytes and current policy, holding a shared lease through use.

        Keep the context open through browsing, preparation and deployment.
        The trusted verifier must use retained local proof/root inputs for
        offline use. The cache never reads persisted receipts as authority.
        """
        digest = _digest(expected_sha256)
        with self._locked(digest, exclusive=False) as root:
            with _cache_io():
                content = self._read_object(root, digest, source_revision, verify)
            yield content

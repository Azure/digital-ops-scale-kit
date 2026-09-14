# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workspace-package integrity and confined materialization.

These operations do not authenticate a publisher. Remote acquisition must
establish provenance under consumer-owned policy before materialization.
No manifest, Site, template or package-provided code is evaluated here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from siteops import __version__
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    hash_stream,
    open_regular_file,
    path_inventory,
    relative_artifact_path,
    require_node,
)

PACKAGE_NAME = "siteops-package.json"
PACKAGE_API = "siteops/v1alpha1"
MAX_FILES = 10000
MAX_NODES = 20000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_CENTRAL_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
SUPPORTED_FEATURES = frozenset({"manifest/v1", "composition/v1", "manifest-selection/v1"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
logger = logging.getLogger(__name__)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ArtifactError("Workspace package metadata has missing or unsupported fields.")
    return value


def _text(value: Any, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > maximum
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ArtifactError("Workspace package text fields must be bounded and printable.")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArtifactError("Workspace package identities must be lowercase SHA-256 digests.")
    return value


def _engine_range(value: Any) -> str:
    text = _text(value)
    try:
        specifiers = SpecifierSet(text)
    except InvalidSpecifier:
        raise ArtifactError("The Site Ops compatibility range is invalid.") from None
    operators = {item.operator for item in specifiers}
    if "===" in operators or not (
        operators & {"==", "~="}
        or (operators & {">", ">="} and operators & {"<", "<="})
    ):
        raise ArtifactError("The Site Ops compatibility range must have lower and upper bounds.")
    return text


def json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def workspace_tree_digest(files: tuple[PayloadFile, ...], workspace: str) -> str:
    """Identify raw workspace files using sorted workspace-relative file records."""
    prefix = "" if workspace == "." else workspace + "/"
    records = [
        {**entry.document(), "path": entry.path.removeprefix(prefix)}
        for entry in sorted(files, key=lambda entry: entry.path)
        if entry.path.startswith(prefix)
    ]
    if not records:
        raise ArtifactError("The package contains no files in its declared workspace.")
    framed = b"siteops.workspace-tree/v1\x00" + json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True)
class WorkspacePackage:
    kit_id: str
    version: str
    source_revision: str
    workspace_root: str
    siteops_range: str
    required_features: tuple[str, ...]
    files: tuple[PayloadFile, ...]
    tree_sha256: str

    def document(self) -> dict[str, Any]:
        return {
            "apiVersion": PACKAGE_API,
            "kind": "WorkspacePackage",
            "kit": {"id": self.kit_id, "version": self.version},
            "source": {"revision": self.source_revision},
            "workspace": {
                "root": self.workspace_root,
                "tree": {"algorithm": "sha256", "digest": self.tree_sha256},
            },
            "compatibility": {
                "siteops": self.siteops_range, "requiredFeatures": list(self.required_features),
            },
            "files": [entry.document() for entry in self.files],
        }

    @classmethod
    def from_document(cls, document: Any) -> WorkspacePackage:
        root = _object(document, {
            "apiVersion", "kind", "kit", "source", "workspace", "compatibility", "files",
        })
        if root["apiVersion"] != PACKAGE_API or root["kind"] != "WorkspacePackage":
            raise ArtifactError("The workspace package format is unsupported.")
        kit = _object(root["kit"], {"id", "version"})
        source = _object(root["source"], {"revision"})
        workspace = _object(root["workspace"], {"root", "tree"})
        tree = _object(workspace["tree"], {"algorithm", "digest"})
        if tree["algorithm"] != "sha256":
            raise ArtifactError("The workspace tree identity must use SHA-256.")
        workspace_root = workspace["root"]
        if workspace_root != ".":
            workspace_root = relative_artifact_path(workspace_root)
        compatibility = _object(root["compatibility"], {"siteops", "requiredFeatures"})
        features = compatibility["requiredFeatures"]
        if not isinstance(features, list) or len(features) > 64:
            raise ArtifactError("The required-feature inventory is invalid.")
        required_features = tuple(_text(item, maximum=128) for item in features)
        if len(set(required_features)) != len(required_features):
            raise ArtifactError("The required-feature inventory contains duplicates.")
        entries = root["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise ArtifactError("The package file inventory exceeds its limits or is empty.")
        files = []
        total = 0
        for value in entries:
            row = _object(value, {"path", "sha256", "size"})
            path = relative_artifact_path(row["path"])
            if path == PACKAGE_NAME:
                raise ArtifactError("The package metadata cannot include itself in its payload.")
            size = row["size"]
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                raise ArtifactError("A package file size is invalid or exceeds its limit.")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactError("The package payload exceeds its total byte limit.")
            files.append(PayloadFile(path, _digest(row["sha256"]), size))
        path_inventory([PACKAGE_NAME, *(entry.path for entry in files)], limit=MAX_NODES)
        ordered = tuple(sorted(files, key=lambda entry: entry.path))
        expected_tree = _digest(tree["digest"])
        if workspace_tree_digest(ordered, workspace_root) != expected_tree:
            raise ArtifactError("The workspace tree identity does not match its file inventory.")
        return cls(
            _text(kit["id"], maximum=128), _text(kit["version"], maximum=128),
            _text(source["revision"]), workspace_root, _engine_range(compatibility["siteops"]),
            tuple(sorted(required_features)), ordered, expected_tree,
        )


@dataclass(frozen=True)
class PackageInspection:
    """Content identities and compatibility, without publisher authentication."""

    metadata: WorkspacePackage
    sha256: str
    size: int


def check_compatibility(
    metadata: WorkspacePackage,
    *,
    engine_version: str = __version__,
    features: frozenset[str] = SUPPORTED_FEATURES,
) -> None:
    try:
        compatible = SpecifierSet(metadata.siteops_range).contains(
            Version(engine_version), prereleases=True,
        )
    except (InvalidVersion, InvalidSpecifier):
        raise ArtifactError("The Site Ops compatibility declaration is invalid.") from None
    if not compatible:
        raise ArtifactError("This package requires a different Site Ops version.")
    if set(metadata.required_features) - features:
        raise ArtifactError("This package requires unsupported Site Ops features.")


def _load_metadata(raw: bytes) -> WorkspacePackage:
    if len(raw) > MAX_METADATA_BYTES:
        raise ArtifactError("Workspace package metadata exceeds its byte limit.")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactError("Workspace package metadata contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ArtifactError("Workspace package metadata contains a non-JSON number.")

    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ArtifactError):
            raise
        raise ArtifactError("Workspace package metadata must be bounded UTF-8 JSON.") from None
    return WorkspacePackage.from_document(document)


def _preflight_directory(stream: BinaryIO, size: int) -> None:
    """Bound the ZIP32 directory before ZipFile allocates entry objects."""
    if size < 22:
        raise ArtifactError("The package ZIP is truncated.")
    stream.seek(size - 22)
    signature, disk, start_disk, disk_entries, entries, central_size, offset, comment = struct.unpack(
        "<4s4H2IH", stream.read(22),
    )
    if (
        signature != b"PK\x05\x06" or disk or start_disk or disk_entries != entries or comment
        or entries == 0 or entries > MAX_FILES + 1
        or central_size > MAX_CENTRAL_BYTES or offset + central_size != size - 22
    ):
        raise ArtifactError("The package requires a bounded, single-volume ZIP32 directory.")
    stream.seek(offset)
    directory = stream.read(central_size)
    position = 0
    count = 0
    while position < len(directory):
        if len(directory) - position < 46 or directory[position:position + 4] != b"PK\x01\x02":
            raise ArtifactError("The package ZIP directory is malformed.")
        version = struct.unpack_from("<H", directory, position + 6)[0]
        name_size, extra_size, comment_size = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name_size + extra_size + comment_size
        count += 1
        if version > 20 or not 0 < name_size <= 2048 or count > MAX_FILES + 1:
            raise ArtifactError("The package ZIP directory exceeds its supported limits.")
    if position != central_size or count != entries:
        raise ArtifactError("The package ZIP directory count or size is inconsistent.")
    stream.seek(0)


def _preflight_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries or entries[0].filename != PACKAGE_NAME or entries[0].header_offset != 0:
        raise ArtifactError("The package ZIP must start with its metadata file.")
    total = 0
    for entry in entries:
        relative_artifact_path(entry.orig_filename)
        mode = entry.external_attr >> 16
        if (
            entry.filename != entry.orig_filename or entry.is_dir()
            or entry.volume != 0
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or entry.external_attr & (0x10 | 0x400) or entry.flag_bits & ~0x800
            or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or entry.extra or entry.comment
        ):
            raise ArtifactError("Package ZIP members must be regular files with supported encoding.")
        maximum = MAX_METADATA_BYTES if entry.filename == PACKAGE_NAME else MAX_FILE_BYTES
        if (
            entry.file_size > maximum
            or entry.file_size > MAX_COMPRESSION_RATIO * entry.compress_size
        ):
            raise ArtifactError("A package ZIP member exceeds its size or compression limit.")
        total += entry.file_size
        if total > MAX_TOTAL_BYTES + MAX_METADATA_BYTES:
            raise ArtifactError("The package ZIP exceeds its total expansion limit.")
    path_inventory([entry.filename for entry in entries], limit=MAX_NODES)
    return {entry.filename: entry for entry in entries}


def _read_metadata_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    """Bound decompression even when a ZIP header understates the decoded size."""
    data = bytearray()
    with archive.open(entry) as source:
        while chunk := source.read(min(1024 * 1024, MAX_METADATA_BYTES - len(data) + 1)):
            data.extend(chunk)
            if len(data) > MAX_METADATA_BYTES:
                raise ArtifactError("Workspace package metadata exceeds its byte limit.")
    return bytes(data)


@contextmanager
def _open_package(path: Path, expected_sha256: str) -> Iterator[
    tuple[zipfile.ZipFile, PackageInspection, bytes]
]:
    expected_sha256 = _digest(expected_sha256)
    try:
        with open_regular_file(path) as stream:
            size, actual = hash_stream(stream, limit=MAX_ARCHIVE_BYTES)
            if actual != expected_sha256:
                raise ArtifactError("The package SHA-256 does not match the expected artifact.")
            _preflight_directory(stream, size)
            with zipfile.ZipFile(stream) as archive:
                entries = _preflight_members(archive)
                raw = _read_metadata_member(archive, entries[PACKAGE_NAME])
                metadata = _load_metadata(raw)
                if set(entries) != {PACKAGE_NAME, *(entry.path for entry in metadata.files)}:
                    raise ArtifactError("The package ZIP does not match its declared file inventory.")
                for entry in metadata.files:
                    if entries[entry.path].file_size != entry.size:
                        raise ArtifactError("A package ZIP file size differs from its declared size.")
                check_compatibility(metadata)
                yield archive, PackageInspection(metadata, actual, size), raw
    except (EOFError, struct.error, zipfile.BadZipFile, zlib.error):
        raise ArtifactError("The package archive could not be read safely.") from None


def _copy_member(archive: zipfile.ZipFile, entry: PayloadFile, destination: BinaryIO | None) -> None:
    digest = hashlib.sha256()
    size = 0
    with archive.open(entry.path) as source:
        while chunk := source.read(min(1024 * 1024, entry.size - size + 1)):
            size += len(chunk)
            if size > entry.size:
                raise ArtifactError("A package file exceeds its declared byte count.")
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
    if size != entry.size or digest.hexdigest() != entry.sha256:
        raise ArtifactError("A package file does not match its declared SHA-256 and size.")


def inspect_package(path: Path, expected_sha256: str) -> PackageInspection:
    """Check exact archive bytes, every payload file and current-engine compatibility."""
    try:
        with _open_package(path, expected_sha256) as (archive, inspection, _):
            for entry in inspection.metadata.files:
                _copy_member(archive, entry, None)
    except OSError:
        raise ArtifactError("The package archive could not be read safely.") from None
    return inspection


def extract_package(path: Path, expected_sha256: str, destination: Path) -> PackageInspection:
    """Materialize into a new staging directory, never an existing cache or project.

    The caller owns provenance verification and atomic cache publication.
    Failure removes only files and directories created by this operation.
    Windows access controls are inherited from the caller's staging parent.
    """
    created_files: list[Path] = []
    created_directories: list[Path] = []
    complete = False
    destination = Path(os.path.abspath(destination))
    try:
        with _open_package(path, expected_sha256) as (archive, inspection, raw):
            require_node(destination.parent, directory=True)
            destination.mkdir(mode=0o700)
            created_directories.append(destination)
            root = destination.resolve()
            directories: set[Path] = {root}
            entries = (
                PayloadFile(PACKAGE_NAME, hashlib.sha256(raw).hexdigest(), len(raw)),
                *inspection.metadata.files,
            )
            for entry in entries:
                parts = entry.path.split("/")
                parent = root
                for component in parts[:-1]:
                    parent /= component
                    if parent not in directories:
                        parent.mkdir(mode=0o700)
                        created_directories.append(parent)
                        directories.add(parent)
                    require_node(parent, directory=True)
                    if parent.resolve(strict=True) != parent:
                        raise ArtifactError("Package extraction encountered a filesystem alias.")
                target = parent / parts[-1]
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                created_files.append(target)
                with os.fdopen(descriptor, "wb") as output:
                    _copy_member(archive, entry, output)
                    output.flush()
                    os.fsync(output.fileno())
                if target.resolve(strict=True) != target:
                    raise ArtifactError("Package extraction encountered a filesystem alias.")
        complete = True
        return inspection
    except OSError:
        raise ArtifactError("Package extraction requires a new writable staging directory.") from None
    finally:
        if not complete:
            cleanup_failed = False
            for target in reversed(created_files):
                try:
                    target.unlink()
                except OSError:
                    cleanup_failed = True
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    cleanup_failed = True
            if cleanup_failed:
                logger.warning("Incomplete package staging cleanup could not be completed.")

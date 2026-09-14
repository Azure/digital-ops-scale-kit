# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Build complete workspace packages from a producer-owned source snapshot."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path

from siteops import workspace_package as package
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    checked_path,
    hash_file,
    is_link,
    open_regular_file,
    path_inventory,
    relative_artifact_path,
    require_node,
)

logger = logging.getLogger(__name__)


def _source_files(root: Path, workspace: str, companions: tuple[str, ...]) -> tuple[str, ...]:
    require_node(root, directory=True)
    if workspace != ".":
        checked_path(root, workspace, directory=True)
    files: set[str] = set()
    visited: set[str] = set()
    pending = [workspace, *companions]
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        if len(visited) > package.MAX_NODES:
            raise ArtifactError("The source snapshot exceeds its path limit.")
        path = root if relative == "." else root.joinpath(*relative_artifact_path(relative).split("/"))
        try:
            info = path.lstat()
            if is_link(info):
                raise ArtifactError("A package source snapshot cannot contain links.")
            if relative != ".":
                checked_path(root, relative, directory=stat.S_ISDIR(info.st_mode))
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as children:
                    for child in children:
                        if len(pending) + len(visited) >= package.MAX_NODES:
                            raise ArtifactError("The source snapshot exceeds its path limit.")
                        pending.append(child.name if relative == "." else relative + "/" + child.name)
            else:
                if relative == package.PACKAGE_NAME:
                    raise ArtifactError("The source already contains reserved package metadata.")
                files.add(relative)
                if len(files) > package.MAX_FILES:
                    raise ArtifactError("The source snapshot exceeds its file limit.")
        except OSError:
            raise ArtifactError("The package source snapshot could not be inspected.") from None
    path_inventory([package.PACKAGE_NAME, *files], limit=package.MAX_NODES)
    return tuple(sorted(files))


def _record(root: Path, relative: str) -> tuple[PayloadFile, int]:
    digest = hashlib.sha256()
    compressor = zlib.compressobj(level=6, wbits=-15)
    compressed = 0
    size = 0
    with open_regular_file(checked_path(root, relative)) as stream:
        while chunk := stream.read(1024 * 1024):
            if size == 0 and chunk.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ArtifactError("Materialize Git LFS content before producing a workspace package.")
            size += len(chunk)
            if size > package.MAX_FILE_BYTES:
                raise ArtifactError("A package source file exceeds its byte limit.")
            digest.update(chunk)
            compressed += len(compressor.compress(chunk))
    compressed += len(compressor.flush())
    compression = (
        zipfile.ZIP_STORED if size > package.MAX_COMPRESSION_RATIO * compressed
        else zipfile.ZIP_DEFLATED
    )
    return PayloadFile(relative, digest.hexdigest(), size), compression


def _zip_info(path: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = compression
    return info


def _publish(source: Path, output: Path, expected: str) -> None:
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as target, open_regular_file(source) as incoming:
            digest = hashlib.sha256()
            while chunk := incoming.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
            if digest.hexdigest() != expected:
                raise ArtifactError("The package changed before output publication.")
        complete = True
    finally:
        if not complete:
            try:
                output.unlink()
            except OSError:
                logger.warning("Incomplete package output cleanup could not be completed.")


def build_package(
    snapshot: Path,
    output: Path,
    *,
    workspace: str,
    kit_id: str,
    version: str,
    source_revision: str,
    siteops_range: str,
    companions: tuple[str, ...] = (),
    required_features: tuple[str, ...] = ("manifest/v1",),
) -> package.PackageInspection:
    """Package every workspace file and approved companion, without publishing remotely.

    The caller supplies an immutable reviewed snapshot. The Git producer
    command establishes that snapshot separately. This function creates no
    provenance assertion and never overwrites an existing output.
    """
    require_node(snapshot, directory=True)
    snapshot = snapshot.resolve()
    output = Path(os.path.abspath(output))
    require_node(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        raise ArtifactError("The package output already exists.")
    paths = _source_files(snapshot, workspace, companions)
    records = []
    compression = {}
    total = 0
    for relative in paths:
        entry, method = _record(snapshot, relative)
        records.append(entry)
        compression[relative] = method
        total += entry.size
        if total > package.MAX_TOTAL_BYTES:
            raise ArtifactError("The package source exceeds its total byte limit.")
    files = tuple(records)
    metadata = package.WorkspacePackage(
        kit_id, version, source_revision, workspace, siteops_range, required_features,
        files, package.workspace_tree_digest(files, workspace),
    )
    metadata = package.WorkspacePackage.from_document(metadata.document())
    package.check_compatibility(metadata)
    raw = package.json_bytes(metadata.document())
    if len(raw) > package.MAX_METADATA_BYTES:
        raise ArtifactError("Workspace package metadata exceeds its byte limit.")
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".siteops-package-", delete=False) as temp:
            staged = Path(temp.name)
            with zipfile.ZipFile(temp, "w", allowZip64=False, compresslevel=6) as archive:
                archive.writestr(_zip_info(package.PACKAGE_NAME, zipfile.ZIP_STORED), raw)
                for entry in metadata.files:
                    info = _zip_info(entry.path, compression[entry.path])
                    info.file_size = entry.size
                    digest = hashlib.sha256()
                    size = 0
                    with (
                        open_regular_file(checked_path(snapshot, entry.path)) as source,
                        archive.open(info, "w") as target,
                    ):
                        while chunk := source.read(min(1024 * 1024, entry.size - size + 1)):
                            size += len(chunk)
                            if size > entry.size:
                                raise ArtifactError("A package source changed during production.")
                            target.write(chunk)
                            digest.update(chunk)
                    if size != entry.size or digest.hexdigest() != entry.sha256:
                        raise ArtifactError("A package source changed during production.")
        _, digest = hash_file(staged, limit=package.MAX_ARCHIVE_BYTES)
        inspection = package.inspect_package(staged, digest)
        _publish(staged, output, digest)
        return inspection
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        raise ArtifactError("The workspace package could not be produced.") from None
    finally:
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                logger.warning("Package-production staging cleanup could not be completed.")

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Portable paths and bounded regular-file access for local artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO


class ArtifactError(ValueError):
    """An artifact failure with a value-safe message and stable category."""

    def __init__(self, message: str, *, code: str = "artifact.invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PayloadFile:
    path: str
    sha256: str
    size: int

    def document(self) -> dict[str, str | int]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


def load_artifact_json(raw: bytes, *, limit: int, label: str) -> Any:
    """Parse bounded UTF-8 JSON while rejecting duplicate keys and numbers outside JSON syntax."""
    if type(limit) is not int or limit < 0 or not isinstance(raw, bytes):
        raise ArtifactError("The artifact JSON input or byte limit is invalid.")
    if len(raw) > limit:
        raise ArtifactError(f"{label} exceeds its byte limit.")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactError(f"{label} contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ArtifactError(f"{label} contains a non-JSON number.")

    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=unique,
            parse_constant=invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ArtifactError):
            raise
        raise ArtifactError(f"{label} must be bounded UTF-8 JSON.") from None


def is_link(info: os.stat_result) -> bool:
    """Include Windows reparse points in the link boundary."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def check_portable_component(part: str) -> None:
    """Reject Windows aliases without changing a caller's directory policy."""
    if part != part.rstrip(" .") or PureWindowsPath(part).is_reserved():
        raise ArtifactError("Ambiguous Windows path components are excluded.", code="path.alias")
    if ":" in part:
        raise ArtifactError("Content paths cannot address alternate streams.", code="path.invalid")


def relative_artifact_path(value: str) -> str:
    """Validate an NFC, portable relative path without rewriting its identity."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ArtifactError("Artifact paths must be bounded relative paths.")
    if value != unicodedata.normalize("NFC", value):
        raise ArtifactError("Artifact paths must use normalized Unicode.")
    if "\\" in value or value.startswith("/") or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ArtifactError("Artifact paths must be printable relative POSIX paths.")
    parts = value.split("/")
    if len(parts) > 32 or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError("Artifact path components or nesting are unsupported.")
    for part in parts:
        check_portable_component(part)
        if len(part.encode("utf-8")) > 255 or any(character in part for character in '<>"|?*'):
            raise ArtifactError("Artifact paths must be portable to Windows.")
    return value


def require_node(path: Path, *, directory: bool) -> os.stat_result:
    """Inspect the node itself, rejecting links and nonregular files."""
    try:
        info = path.lstat()
    except OSError:
        raise ArtifactError("An artifact path could not be accessed.") from None
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if is_link(info) or not expected or (not directory and info.st_nlink != 1):
        raise ArtifactError("Artifact paths must be regular, unlinked files or directories.")
    return info


def checked_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    """Resolve an existing payload path through regular, canonical directories."""
    relative_artifact_path(relative)
    require_node(root, directory=True)
    root = root.resolve()
    path = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        path /= part
        require_node(path, directory=index < len(parts) - 1 or directory)
        try:
            if path.resolve(strict=True) != path:
                raise ArtifactError("Artifact paths cannot use filesystem aliases.")
        except OSError:
            raise ArtifactError("An artifact path could not be resolved.") from None
    return path


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
        info.st_nlink,
    )


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """Keep reads on one regular-file handle and detect changes during use."""
    before = require_node(path, directory=False)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            opened = os.fstat(descriptor)
        except OSError:
            raise ArtifactError("An artifact file could not be opened safely.") from None
        if is_link(opened) or not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise ArtifactError("An artifact changed while it was opened.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            yield stream
            if _identity(os.fstat(stream.fileno())) != _identity(opened):
                raise ArtifactError("An artifact changed while it was read.")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def hash_stream(stream: BinaryIO, *, limit: int) -> tuple[int, str]:
    """Hash at most the declared byte budget from the current stream position."""
    if type(limit) is not int or limit < 0:
        raise ArtifactError("The artifact read limit is invalid.")
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := stream.read(min(1024 * 1024, limit - size + 1)):
            size += len(chunk)
            if size > limit:
                raise ArtifactError("An artifact exceeds its byte limit.")
            digest.update(chunk)
    except OSError:
        raise ArtifactError("An artifact file could not be read safely.") from None
    return size, digest.hexdigest()


def hash_file(path: Path, *, limit: int) -> tuple[int, str]:
    with open_regular_file(path) as stream:
        return hash_stream(stream, limit=limit)


def path_inventory(paths: list[str], *, limit: int) -> set[str]:
    """Reject duplicate files, case aliases, and file/directory conflicts."""
    nodes: dict[str, tuple[str, bool]] = {}
    directories: set[str] = set()
    for path in paths:
        parts = relative_artifact_path(path).split("/")
        for depth in range(1, len(parts) + 1):
            name = "/".join(parts[:depth])
            directory = depth < len(parts)
            key = name.casefold()
            previous = nodes.get(key)
            if previous is not None and (previous != (name, directory) or not directory):
                raise ArtifactError("Artifact paths contain a collision or duplicate file.")
            nodes[key] = (name, directory)
            if directory:
                directories.add(name)
            if len(nodes) > limit:
                raise ArtifactError("The artifact path inventory exceeds its limit.")
    return directories

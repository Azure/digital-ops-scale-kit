# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Producer-side Git snapshots, independent of an installed Site Ops engine."""

import os
import re
import shutil
import stat
import subprocess
import unicodedata
import zipfile
from pathlib import Path, PureWindowsPath


class SourceSnapshotError(RuntimeError):
    """An expected source-export failure safe to report without tool output."""


def _git(root: Path, arguments: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    executable = None
    for directory in os.get_exec_path():
        if Path(directory).is_absolute():
            candidate = Path(directory) / ("git.exe" if os.name == "nt" else "git")
            executable = shutil.which(str(candidate))
            if executable:
                break
    if not executable:
        raise SourceSnapshotError("Git must be available from an absolute PATH directory.")
    try:
        return subprocess.run(
            [executable, "--literal-pathspecs", "-C", str(root), *arguments], cwd=root,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SourceSnapshotError("Git could not complete the source snapshot operation.") from None


def validate_repository(root: Path, expected_source_sha: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_sha) is None:
        raise SourceSnapshotError("The expected source commit must be a full lowercase Git SHA.")
    location = _git(root, ["rev-parse", "--show-toplevel"])
    try:
        top = Path(location.stdout.decode("utf-8").rstrip("\r\n")).resolve()
    except (UnicodeError, OSError):
        raise SourceSnapshotError("Git could not identify the source repository root.") from None
    if location.returncode != 0 or top != root.resolve():
        raise SourceSnapshotError("--root must select the source repository root.")
    head = _git(root, ["rev-parse", "--verify", "HEAD"])
    if head.returncode != 0 or head.stdout.decode("ascii", errors="ignore").strip() != expected_source_sha:
        raise SourceSnapshotError("The checked out commit does not match --expected-source-sha.")
    status = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if status.returncode != 0:
        raise SourceSnapshotError("Git could not confirm the source state.")
    if status.stdout:
        raise SourceSnapshotError("The source checkout has tracked or untracked changes.")


def safe_archive_path(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/"):
        raise SourceSnapshotError("The source archive contains an unsafe path.")
    stripped = name[:-1] if name.endswith("/") else name
    parts = tuple(stripped.split("/"))
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise SourceSnapshotError("The source archive contains an unsafe path.")
    if (
        name != unicodedata.normalize("NFC", name)
        or any(unicodedata.category(character).startswith("C") for character in name)
        or any(
            part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
            or any(character in part for character in '<>"|?*')
            for part in parts
        )
    ):
        raise SourceSnapshotError("The source archive contains a nonportable path.")
    return parts


def export_tracked_source(
    root: Path, destination: Path, source_sha: str, *, paths: tuple[str, ...] = (),
) -> None:
    """Export an identified producer input, not an arbitrary downloaded archive."""
    archive_path = destination.parent / "source.zip"
    if archive_path.exists():
        raise SourceSnapshotError("The source archive staging path already exists.")
    result = _git(
        root, ["archive", "--format=zip", "--output", str(archive_path), source_sha, "--", *paths],
        timeout=120,
    )
    if result.returncode != 0:
        raise SourceSnapshotError("Git could not export the tracked source.")
    destination.mkdir()
    normalized: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as source:
            for item in source.infolist():
                parts = safe_archive_path(item.filename)
                key = "/".join(parts).casefold()
                if key in normalized:
                    raise SourceSnapshotError("The source archive contains colliding paths.")
                normalized.add(key)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise SourceSnapshotError("The source archive cannot contain symbolic links.")
                target = destination.joinpath(*parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if mode and not stat.S_ISREG(mode):
                    raise SourceSnapshotError("The source archive contains a nonregular file.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
    except (OSError, zipfile.BadZipFile):
        raise SourceSnapshotError("The tracked source archive could not be extracted.") from None


def require_complete_export(root: Path, source_sha: str, paths: tuple[str, ...], snapshot: Path) -> None:
    result = _git(root, ["ls-tree", "-r", "-z", "--full-tree", source_sha, "--", *paths])
    if result.returncode:
        raise SourceSnapshotError("Git could not enumerate the selected source.")
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        if not header.startswith((b"100644 blob ", b"100755 blob ")):
            raise SourceSnapshotError("Workspace packages require regular tracked files, not submodules.")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeError:
            raise SourceSnapshotError("Package source paths must be UTF-8.") from None
        if not snapshot.joinpath(*safe_archive_path(path)).is_file():
            raise SourceSnapshotError("The source export omitted a selected tracked file.")

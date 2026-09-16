# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded storage inspection and explicitly targeted removal of cached entries."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siteops.artifacts import ArtifactError, is_link
from siteops.cache_filesystem import CacheError, cache_lock, check_private_node
from siteops.cache_layout import CacheLayout, cache_io, default_cache_root

CACHE_KINDS = ("package", "proof", "metadata")
_MAX_ENTRIES = 10_000
_MAX_ENTRY_NODES = 50_000
_MAX_LIST_NODES = 100_000
_MAX_DEPTH = 64
_DIGEST = re.compile(r"[0-9a-f]{64}")
_METADATA_TEMP = re.compile(r"\.record-[0-9a-f]{32}\.tmp")


def _selection(kind: str, identity: str | None = None) -> None:
    if kind not in CACHE_KINDS:
        raise CacheError("Choose a package, proof or metadata cache entry.", code="cache.selection")
    if identity is not None and (not isinstance(identity, str) or _DIGEST.fullmatch(identity) is None):
        raise CacheError("Use the complete lowercase cache entry ID.", code="cache.selection")


def _roots(layout: CacheLayout, kind: str, identity: str) -> tuple[Path, ...]:
    if kind == "package":
        return (
            layout.root / "objects" / "sha256" / identity,
            layout.root / "receipts" / identity,
        )
    if kind == "proof":
        return (layout.root / "proofs" / "sha256" / identity,)
    return (layout.root / "metadata" / "records" / f"{identity}.json",)


def _parents(layout: CacheLayout, kind: str) -> tuple[Path, ...]:
    return {
        "package": (layout.root / "objects" / "sha256", layout.root / "receipts"),
        "proof": (layout.root / "proofs" / "sha256",),
        "metadata": (layout.root / "metadata" / "records",),
    }[kind]


def _check_parents(layout: CacheLayout, kind: str) -> None:
    device = layout.root.stat().st_dev
    for parent in _parents(layout, kind):
        if _present(parent):
            check_private_node(parent, directory=True)
            if parent.stat().st_dev != device or os.path.ismount(parent):
                raise CacheError("Cache maintenance cannot cross filesystem mounts.", code="cache.unsafe-entry")


def _lock(layout: CacheLayout, kind: str, identity: str, *, exclusive: bool):
    name = identity if kind == "package" else f"{kind}-{identity}"
    return cache_lock(layout.root / "locks" / f"{name}.lock", exclusive=exclusive, timeout=0)


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@dataclass
class _Budget:
    remaining: int

    def take(self) -> None:
        if self.remaining <= 0:
            raise CacheError(
                "Cache inspection exceeds its node limit. Narrow the kind or entry ID.",
                code="cache.scan-limit",
            )
        self.remaining -= 1


def _inventory(paths: tuple[Path, ...], device: int, budget: _Budget) -> list[tuple[Path, bool, int]]:
    """Inspect regular private nodes without following filesystem aliases or mounts."""
    pending = [(path, 0) for path in paths if _present(path)]
    result: list[tuple[Path, bool, int]] = []
    while pending:
        path, depth = pending.pop()
        budget.take()
        if depth > _MAX_DEPTH or len(result) >= _MAX_ENTRY_NODES:
            raise CacheError("The cache entry exceeds its maintenance bounds.", code="cache.scan-limit")
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        if (
            is_link(info) or info.st_dev != device or os.path.ismount(path)
            or not (directory or stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
        ):
            raise CacheError(
                "Cache maintenance requires regular private storage without aliases or mounts.",
                code="cache.unsafe-entry",
            )
        check_private_node(path, directory=directory)
        if path.resolve(strict=True) != path:
            raise CacheError("Cache maintenance cannot follow filesystem aliases.", code="cache.unsafe-entry")
        result.append((path, directory, 0 if directory else info.st_size))
        if directory:
            for child in path.iterdir():
                if len(pending) + len(result) >= _MAX_ENTRY_NODES:
                    raise CacheError("The cache entry exceeds its maintenance bounds.", code="cache.scan-limit")
                pending.append((child, depth + 1))
    return result


@dataclass(frozen=True)
class CacheEntry:
    kind: str
    identity: str
    state: str
    stored_bytes: int | None
    files: int | None
    issue: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "id": self.identity, "storageState": self.state,
            "storedBytes": self.stored_bytes, "files": self.files,
            "verification": "not-performed", "issue": self.issue,
        }


@dataclass(frozen=True)
class CacheListing:
    root: Path
    initialized: bool
    matched: int
    entries: tuple[CacheEntry, ...]

    def document(self) -> dict[str, Any]:
        return {
            "apiVersion": "siteops/v1alpha1", "kind": "CacheInventory",
            "root": str(self.root), "initialized": self.initialized,
            "matched": self.matched, "shown": len(self.entries),
            "hasMore": self.matched > len(self.entries),
            "verification": "not-performed",
            "entries": [entry.document() for entry in self.entries],
        }


class CacheManagement:
    """Inspect retained storage or remove one known entry without source or trust operations.

    Listing describes filesystem state, not integrity or publisher approval.
    Removal holds the same exclusive lock used by acquisition and fails on
    active use. Package removal includes its receipts, not shared proof or
    metadata entries. Lock files and all other entries remain untouched.
    """

    def __init__(self, root: Path | None = None):
        self.root = default_cache_root() if root is None else Path(root)

    def _open(self) -> CacheLayout | None:
        try:
            return CacheLayout(self.root, create=False)
        except CacheError as error:
            if error.code != "cache.uninitialized":
                raise
            return None

    def _identities(self, layout: CacheLayout, kind: str) -> set[str]:
        _check_parents(layout, kind)
        result: set[str] = set()
        observed = 0
        for parent in _parents(layout, kind):
            if not _present(parent):
                continue
            for entry in parent.iterdir():
                observed += 1
                if observed > _MAX_ENTRIES:
                    raise CacheError("Cache inventory exceeds its entry limit. Select an entry ID.",
                                     code="cache.scan-limit")
                name = entry.name
                if kind == "metadata":
                    if _METADATA_TEMP.fullmatch(name):
                        continue
                    if not name.endswith(".json"):
                        raise CacheError("The cache namespace contains an unexpected entry.")
                    name = name[:-5]
                _selection(kind, name)
                result.add(name)
        return result

    def list(
        self, *, kind: str | None = None, identity: str | None = None, limit: int = 100,
    ) -> CacheListing:
        if type(limit) is not int or not 1 <= limit <= _MAX_ENTRIES:
            raise CacheError("Cache list limit must be between 1 and 10000.", code="cache.selection")
        if kind is not None:
            _selection(kind, identity)
        elif identity is not None:
            raise CacheError("Select a cache kind when supplying an entry ID.", code="cache.selection")
        with cache_io():
            layout = self._open()
            if layout is None:
                return CacheListing(self.root, False, 0, ())
            kinds = (kind,) if kind is not None else CACHE_KINDS
            selections = [
                (selected, value) for selected in kinds
                for value in sorted({identity} if identity is not None else self._identities(layout, selected))
            ]
            entries = []
            budget = _Budget(_MAX_LIST_NODES)
            for selected, value in selections[:limit]:
                try:
                    # Package readers also publish receipts, so inspection needs a quiet entry.
                    with _lock(layout, selected, value, exclusive=True):
                        layout._check_root()
                        _check_parents(layout, selected)
                        roots = _roots(layout, selected, value)
                        for path in roots:
                            if _present(path):
                                check_private_node(path, directory=selected != "metadata")
                        nodes = _inventory(roots, layout.root.stat().st_dev, budget)
                        present = {path: directory for path, directory, _ in nodes}
                        expected = (
                            {roots[0] / "package.zip": False, roots[0] / "content": True} if selected == "package"
                            else {roots[0] / "proof.bin": False} if selected == "proof" else {roots[0]: False}
                        )
                        valid = all(present.get(path) is directory for path, directory in expected.items())
                        if selected != "metadata":
                            valid = valid and {path for path in present if path.parent == roots[0]} == expected.keys()
                        state = "missing" if not nodes else "stored" if valid else "incomplete"
                        entries.append(CacheEntry(
                            selected, value, state, sum(size for _, _, size in nodes),
                            sum(not directory for _, directory, _ in nodes),
                        ))
                except ArtifactError as error:
                    state = "busy" if error.code == "cache.busy" else "unavailable"
                    entries.append(CacheEntry(selected, value, state, None, None, error.code))
            return CacheListing(layout.root, True, len(selections), tuple(entries))

    def remove(self, kind: str, identity: str) -> CacheEntry:
        """Remove only the selected entry after complete private-node preflight.

        Filesystem failure can leave a partially removed entry. It remains
        invalid for acquisition and the same targeted removal can be retried.
        There is no in-place repair or recursive deletion of a cache root.
        """
        _selection(kind, identity)
        with cache_io():
            layout = self._open()
            if layout is None:
                raise CacheError("The selected cache entry was not found.", code="cache.entry-missing")
            with _lock(layout, kind, identity, exclusive=True):
                layout._check_root()
                _check_parents(layout, kind)
                roots = _roots(layout, kind, identity)
                for path in roots:
                    if _present(path):
                        check_private_node(path, directory=kind != "metadata")
                nodes = _inventory(roots, layout.root.stat().st_dev, _Budget(_MAX_ENTRY_NODES))
                if not nodes:
                    raise CacheError("The selected cache entry was not found.", code="cache.entry-missing")
                try:
                    for path, directory, _ in reversed(nodes):
                        if directory:
                            path.rmdir()
                        else:
                            path.unlink()
                except OSError:
                    raise CacheError(
                        "Cache removal is incomplete. Resolve file access and retry this entry.",
                        code="cache.remove-incomplete",
                    ) from None
                return CacheEntry(
                    kind, identity, "removed", sum(size for _, _, size in nodes),
                    sum(not directory for _, directory, _ in nodes),
                )

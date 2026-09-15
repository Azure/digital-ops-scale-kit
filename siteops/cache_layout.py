# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Marked private storage shared by workspace and source metadata caches."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from siteops.artifacts import ArtifactError, open_regular_file
from siteops.cache_filesystem import (
    CacheError,
    check_cache_ancestors,
    check_private_node,
    make_private_directory,
)

logger = logging.getLogger(__name__)
CACHE_DIR_ENV = "SITEOPS_CACHE_DIR"
_MARKER = b'{"apiVersion":"siteops/v1alpha1","kind":"WorkspaceCache"}\n'
_DIRECTORIES = ("objects", "receipts", "staging", "locks")
_NAMESPACES = {"proofs": "sha256", "metadata": "records"}


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


def write_new(path: Path, data: bytes) -> None:
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


def cleanup_directory(path: Path) -> None:
    """Remove only a directory created by this operation, never a published object."""
    try:
        check_private_node(path, directory=True)
        shutil.rmtree(path)
    except (OSError, ArtifactError):
        logger.warning("Cache staging cleanup could not be completed.")


@contextmanager
def cache_io() -> Iterator[None]:
    try:
        yield
    except OSError:
        raise CacheError("The cache operation could not complete its file access.", code="cache.io") from None


class CacheLayout:
    """Validate the shared cache marker, ownership and closed storage namespaces."""

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
        names = {entry.name for entry in self.root.iterdir()}
        required = {"cache.json", *_DIRECTORIES}
        if not required <= names or names - required - _NAMESPACES.keys():
            raise CacheError("The cache root contains an unexpected path.")
        for name in (*_DIRECTORIES, "objects/sha256"):
            check_private_node(self.root.joinpath(*name.split("/")), directory=True)
        for name, child in _NAMESPACES.items():
            if name in names:
                directory = self.root / name
                check_private_node(directory, directory=True)
                if {entry.name for entry in directory.iterdir()} != {child}:
                    raise CacheError("The cache namespace contains an unexpected path.")
                check_private_node(directory / child, directory=True)

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
            write_new(candidate / "cache.json", _MARKER)
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
                cleanup_directory(candidate)

    def prepare_namespace(self, name: str) -> Path:
        """Atomically create a supported optional namespace, preserving any existing winner."""
        if not isinstance(name, str) or name not in _NAMESPACES:
            raise CacheError("The requested cache namespace is unsupported.")
        self._check_root()
        target = self.root / name
        if target.exists():
            return target / _NAMESPACES[name]
        candidate = self.root / "staging" / f"{name}-{uuid.uuid4().hex}"
        make_private_directory(candidate)
        published = False
        try:
            make_private_directory(candidate / _NAMESPACES[name])
            try:
                candidate.rename(target)
                published = True
            except OSError:
                if not target.exists():
                    raise
            self._check_root()
            return target / _NAMESPACES[name]
        finally:
            if not published:
                cleanup_directory(candidate)

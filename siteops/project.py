# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Store workspace pins separately from operator configuration and trust."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from siteops.artifacts import ArtifactError, load_artifact_json, open_regular_file
from siteops.cache_filesystem import (
    cache_lock,
    check_cache_ancestors,
    check_private_node,
    make_private_directory,
)
from siteops.cache_layout import write_new
from siteops.workspace_source import ResolvedWorkspaceSource

logger = logging.getLogger(__name__)
PIN_NAME = "siteops.pin"
MAX_PIN_BYTES = 64 * 1024


class ProjectError(ArtifactError):
    def __init__(self, message: str, *, code: str = "project.invalid"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class WorkspacePin:
    """An exact workspace selection, containing no policy, credentials or local cache paths."""

    selection: ResolvedWorkspaceSource

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ResolvedWorkspaceSource):
            raise ProjectError("A workspace pin requires a resolved workspace selection.")
        ResolvedWorkspaceSource.from_document(self.selection.document())

    @classmethod
    def from_bytes(cls, raw: bytes) -> WorkspacePin:
        try:
            row = load_artifact_json(raw, limit=MAX_PIN_BYTES, label="Project pin")
            if (
                not isinstance(row, dict)
                or row.keys() != {"apiVersion", "kind", "source", "content"}
                or row["apiVersion"] != "siteops/v1alpha1" or row["kind"] != "WorkspacePin"
            ):
                raise ProjectError("The workspace pin format is unsupported.")
            return cls(ResolvedWorkspaceSource.from_document({
                "source": row["source"], "content": row["content"],
            }))
        except ArtifactError:
            raise ProjectError("The workspace pin is invalid.", code="project.pin-invalid") from None

    def document(self) -> dict:
        return {
            "apiVersion": "siteops/v1alpha1", "kind": "WorkspacePin",
            **self.selection.document(),
        }

    def serialized(self) -> bytes:
        raw = (json.dumps(self.document(), ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
        if len(raw) > MAX_PIN_BYTES:
            raise ProjectError("The workspace pin exceeds its byte limit.")
        return raw


@dataclass(frozen=True)
class PinSnapshot:
    pin: WorkspacePin
    sha256: str


def pin_exists(root: Path) -> bool:
    try:
        (root / PIN_NAME).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ProjectError("The workspace pin could not be accessed.", code="project.io") from None


def read_pin(root: Path) -> PinSnapshot:
    path = root / PIN_NAME
    if not pin_exists(root):
        raise ProjectError(
            "Workspace pin not found. Use project pin to select a package, or -w to select local content.",
            code="project.pin-missing",
        )
    try:
        with open_regular_file(path) as stream:
            raw = stream.read(MAX_PIN_BYTES + 1)
    except OSError:
        raise ProjectError("The workspace pin could not be read.", code="project.io") from None
    return PinSnapshot(WorkspacePin.from_bytes(raw), hashlib.sha256(raw).hexdigest())


def project_root(path: Path, *, create: bool = False) -> Path:
    root = Path(path).absolute()
    try:
        if create and not root.exists():
            check_cache_ancestors(root)
            try:
                make_private_directory(root)
            except FileExistsError:
                pass
        if not root.is_dir():
            raise ProjectError("Project directory not found.", code="project.missing")
        return root.resolve(strict=True)
    except OSError:
        raise ProjectError("The project directory could not be accessed.", code="project.io") from None


def require_separate_cache(project: Path, cache: Path) -> None:
    project, cache = project.resolve(), cache.resolve()
    if project.is_relative_to(cache) or cache.is_relative_to(project):
        raise ProjectError(
            "Choose project and cache directories in separate directory trees.", code="project.cache-overlap",
        )


def write_pin(root: Path, pin: WorkspacePin, *, expected_previous: str | None) -> PinSnapshot:
    """Atomically replace a recognized pin, detecting competing changes during acquisition."""
    raw = pin.serialized()
    state = root / ".siteops"
    temporary = state / f".pin-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        check_cache_ancestors(state)
        try:
            make_private_directory(state)
        except FileExistsError:
            pass
        else:
            write_new(state / ".gitignore", b"*\n")
        check_private_node(state, directory=True)
        with cache_lock(state / "project.lock", exclusive=True, timeout=20):
            previous = read_pin(root) if pin_exists(root) else None
            if (previous.sha256 if previous else None) != expected_previous:
                raise ProjectError(
                    "The workspace pin changed during acquisition. Repeat project pin explicitly.",
                    code="project.pin-changed",
                )
            write_new(temporary, raw)
            created = True
            check_private_node(temporary, directory=False)
            temporary.replace(root / PIN_NAME)
            created = False
            check_private_node(root / PIN_NAME, directory=False)
    except OSError:
        raise ProjectError("The workspace pin could not be published.", code="project.io") from None
    finally:
        if created:
            try:
                temporary.unlink()
            except OSError:
                logger.warning("Project pin staging cleanup could not be completed.")
    return PinSnapshot(pin, hashlib.sha256(raw).hexdigest())

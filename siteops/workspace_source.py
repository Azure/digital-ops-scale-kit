# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workspace release routing and source expectations, independent of hosting providers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from siteops.artifacts import ArtifactError, load_artifact_json, relative_artifact_path
from siteops.content_metadata import require_mapping, validate_envelope

if TYPE_CHECKING:
    from siteops.workspace_package import PackageInspection

WORKSPACE_RELEASE_NAME = "siteops-workspaces.json"
MAX_DESCRIPTOR_BYTES = 256 * 1024
MAX_WORKSPACES = 64
MAX_SOURCE_ARTIFACT_BYTES = 128 * 1024 * 1024


class SourceResolutionError(ArtifactError):
    """A source selection failure with safe text and optional private choices."""

    def __init__(
        self, message: str, *, code: str = "source.invalid", choices: tuple[str, ...] = (),
    ):
        super().__init__(message, code=code)
        self.choices = choices


def _text(value: Any, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip() or len(value) > limit
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise SourceResolutionError("Source fields must contain bounded printable text.")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SourceResolutionError("Source artifact identities must use lowercase SHA-256 digests.")
    return value


def _record(
    value: Any, required: set[str], optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    try:
        row = require_mapping(value, required | optional)
    except ValueError:
        raise SourceResolutionError("Workspace release metadata has unsupported fields.") from None
    if not required <= row.keys():
        raise SourceResolutionError("Workspace release metadata has missing fields.")
    return row


def _workspace(value: Any) -> str:
    if value == ".":
        return value
    try:
        return relative_artifact_path(value)
    except ArtifactError:
        raise SourceResolutionError("A workspace must be a canonical relative path or '.'.") from None


@dataclass(frozen=True)
class ArtifactIdentity:
    """The name and exact bytes of a release artifact, not its download location."""

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        try:
            relative_artifact_path(self.name)
        except ArtifactError:
            raise SourceResolutionError("Release artifacts require portable filenames.") from None
        if "/" in self.name:
            raise SourceResolutionError("Release artifacts require filenames without directories.")
        if type(self.size) is not int or not 0 < self.size <= MAX_SOURCE_ARTIFACT_BYTES:
            raise SourceResolutionError("The declared source artifact size is outside its limit.")
        _digest(self.sha256)

    @classmethod
    def from_document(cls, value: Any) -> ArtifactIdentity:
        row = _record(value, {"name", "size", "sha256"})
        return cls(row["name"], row["size"], row["sha256"])

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class WorkspaceReleaseEntry:
    workspace: str
    kit_id: str
    kit_version: str
    package: ArtifactIdentity
    proof: ArtifactIdentity
    index_sha256: str | None = None

    def __post_init__(self) -> None:
        _workspace(self.workspace)
        _text(self.kit_id, limit=128)
        _text(self.kit_version, limit=128)
        if not isinstance(self.package, ArtifactIdentity) or not isinstance(self.proof, ArtifactIdentity):
            raise SourceResolutionError("A workspace entry requires package and proof identities.")
        if self.index_sha256 is not None:
            _digest(self.index_sha256)

    @classmethod
    def from_document(cls, value: Any) -> WorkspaceReleaseEntry:
        row = _record(value, {"workspace", "kit", "package", "proof"}, {"index"})
        kit = _record(row["kit"], {"id", "version"})
        index = _record(row["index"], {"sha256"}) if "index" in row else None
        return cls(
            row["workspace"], kit["id"], kit["version"],
            ArtifactIdentity.from_document(row["package"]),
            ArtifactIdentity.from_document(row["proof"]),
            _digest(index["sha256"]) if index is not None else None,
        )

    def document(self) -> dict[str, Any]:
        result = {
            "workspace": self.workspace,
            "kit": {"id": self.kit_id, "version": self.kit_version},
            "package": self.package.document(),
            "proof": self.proof.document(),
        }
        if self.index_sha256 is not None:
            result["index"] = {"sha256": self.index_sha256}
        return result


@dataclass(frozen=True)
class WorkspaceReleaseAssets:
    """Unsigned routing metadata whose claims must match the verified package."""

    revision: str
    workspaces: tuple[WorkspaceReleaseEntry, ...]

    def __post_init__(self) -> None:
        _text(self.revision)
        if not isinstance(self.workspaces, tuple) or not 1 <= len(self.workspaces) <= MAX_WORKSPACES:
            raise SourceResolutionError("The workspace release inventory is empty or exceeds its limit.")
        roots: set[str] = set()
        names = {WORKSPACE_RELEASE_NAME.casefold()}
        for entry in self.workspaces:
            if not isinstance(entry, WorkspaceReleaseEntry):
                raise SourceResolutionError("The workspace release inventory has an invalid entry.")
            if entry.workspace.casefold() in roots:
                raise SourceResolutionError("The workspace release inventory contains duplicate workspace paths.")
            roots.add(entry.workspace.casefold())
            for artifact in (entry.package, entry.proof):
                if artifact.name.casefold() in names:
                    raise SourceResolutionError("Workspace release artifact names must be unique.")
                names.add(artifact.name.casefold())
        object.__setattr__(self, "workspaces", tuple(sorted(self.workspaces, key=lambda entry: entry.workspace)))

    @classmethod
    def from_bytes(cls, raw: bytes) -> WorkspaceReleaseAssets:
        try:
            document = load_artifact_json(raw, limit=MAX_DESCRIPTOR_BYTES, label="Workspace release descriptor")
            row = validate_envelope(
                document, "WorkspaceReleaseAssets",
                {"apiVersion", "kind", "source", "workspaces"},
            )
            row = _record(row, {"apiVersion", "kind", "source", "workspaces"})
            source = _record(row["source"], {"revision"})
            entries = row["workspaces"]
            if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_WORKSPACES:
                raise SourceResolutionError("The workspace release inventory is empty or exceeds its limit.")
            return cls(source["revision"], tuple(WorkspaceReleaseEntry.from_document(entry) for entry in entries))
        except SourceResolutionError:
            raise
        except (ArtifactError, ValueError):
            raise SourceResolutionError("The workspace release descriptor is invalid.") from None

    def serialized(self) -> bytes:
        raw = (json.dumps({
            "apiVersion": "siteops/v1alpha1", "kind": "WorkspaceReleaseAssets",
            "source": {"revision": self.revision},
            "workspaces": [entry.document() for entry in self.workspaces],
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > MAX_DESCRIPTOR_BYTES:
            raise SourceResolutionError("The workspace release descriptor exceeds its byte limit.")
        return raw

    def select(self, workspace: str | None = None) -> WorkspaceReleaseEntry:
        if workspace is None and len(self.workspaces) == 1:
            return self.workspaces[0]
        choices = tuple(entry.workspace for entry in self.workspaces)
        if workspace is None:
            raise SourceResolutionError(
                "Select a workspace from the release.",
                code="source.workspace-ambiguous", choices=choices,
            )
        _workspace(workspace)
        for entry in self.workspaces:
            if entry.workspace == workspace:
                return entry
        raise SourceResolutionError(
            "The selected workspace is not listed in the release.",
            code="source.workspace-not-found", choices=choices,
        )


@dataclass(frozen=True)
class ResolvedReleaseSource:
    """Source selection plus immutable expectations, without provider transport fields."""

    provider: str
    reference: str
    release: str
    revision: str
    descriptor: ArtifactIdentity

    def __post_init__(self) -> None:
        _text(self.provider, limit=128)
        _text(self.reference, limit=2048)
        _text(self.release)
        _text(self.revision)
        if (
            not isinstance(self.descriptor, ArtifactIdentity)
            or self.descriptor.name != WORKSPACE_RELEASE_NAME
            or self.descriptor.size > MAX_DESCRIPTOR_BYTES
        ):
            raise SourceResolutionError("The source requires the bounded workspace release descriptor.")

    def parse_descriptor(self, raw: bytes) -> WorkspaceReleaseAssets:
        """Check routing bytes and revision against independent source expectations."""
        if (
            not isinstance(raw, bytes)
            or len(raw) != self.descriptor.size
            or hashlib.sha256(raw).hexdigest() != self.descriptor.sha256
        ):
            raise SourceResolutionError(
                "The workspace descriptor differs from its observed asset.", code="source.identity",
            )
        descriptor = WorkspaceReleaseAssets.from_bytes(raw)
        if descriptor.revision != self.revision:
            raise SourceResolutionError(
                "The descriptor revision differs from the release source.", code="source.identity",
            )
        return descriptor


@dataclass(frozen=True)
class ResolvedWorkspaceSource:
    source: ResolvedReleaseSource
    entry: WorkspaceReleaseEntry

    def __post_init__(self) -> None:
        if not isinstance(self.source, ResolvedReleaseSource) or not isinstance(self.entry, WorkspaceReleaseEntry):
            raise SourceResolutionError("A resolved workspace requires source and entry identities.")

    def check_package(self, inspection: PackageInspection) -> None:
        """Check selection against inspected bytes, not publisher authority."""
        metadata = inspection.metadata
        if (inspection.sha256, inspection.size) != (self.entry.package.sha256, self.entry.package.size):
            raise SourceResolutionError("The package differs from the selected artifact.", code="source.identity")
        if (
            metadata.source_revision, metadata.workspace_root, metadata.kit_id, metadata.version
        ) != (
            self.source.revision, self.entry.workspace, self.entry.kit_id, self.entry.kit_version
        ):
            raise SourceResolutionError("Package metadata differs from the selected source.", code="source.identity")

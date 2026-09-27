# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Model the frozen publication inventory separately from an existing engine release."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_VERSION = "siteops.release.assets/v2"
MAX_INVENTORY_BYTES = 2 * 1024 * 1024
MAX_ASSETS = 512
MAX_ASSET_BYTES = 4 * 1024 * 1024 * 1024
ARCHIVE_NAME = "siteops-install.zip"
PROOF_SUFFIX = ".attestation.jsonl"
BOOTSTRAP_SCRIPTS = ("siteops-bootstrap.ps1", "siteops-bootstrap.sh")
_WHEEL = re.compile(r"siteops-[0-9A-Za-z][0-9A-Za-z.!+_~-]*-py3-none-any\.whl")
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ReleaseAssetsError(ValueError):
    """The frozen inventory is incomplete or has an invalid identity."""


def validate_asset_name(name: str) -> str:
    """Validate a portable filename before producing release bytes."""
    _text(name, r"[0-9A-Za-z][0-9A-Za-z.!+_~-]*", 255)
    if (
        ".." in name or name.endswith(".")
        or name.split(".", 1)[0].upper() in _RESERVED_NAMES
    ):
        raise ReleaseAssetsError("Release assets require portable filenames.")
    return name


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ReleaseAssetsError("The release asset inventory contains unsupported or missing fields.")
    return value


def _text(value: Any, pattern: str, limit: int) -> str:
    if type(value) is not str or len(value) > limit or re.fullmatch(pattern, value) is None:
        raise ReleaseAssetsError("A release asset identity is invalid.")
    return value


@dataclass(frozen=True)
class ReleaseAsset:
    """One portable publication filename and its exact byte identity."""

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        validate_asset_name(self.name)
        if type(self.size) is not int or not 0 < self.size <= MAX_ASSET_BYTES:
            raise ReleaseAssetsError("A release asset size is outside its limit.")
        _text(self.sha256, r"[0-9a-f]{64}", 64)

    @classmethod
    def from_document(cls, value: Any) -> ReleaseAsset:
        row = _object(value, {"name", "size", "sha256"})
        return cls(row["name"], row["size"], row["sha256"])

    def document(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


def _inventory(values: Any) -> tuple[ReleaseAsset, ...]:
    if type(values) is not list or len(values) > MAX_ASSETS:
        raise ReleaseAssetsError("The release asset inventory exceeds its limit or is invalid.")
    result = tuple(ReleaseAsset.from_document(value) for value in values)
    _unique_assets(result)
    return result


def _unique_assets(values: tuple[ReleaseAsset, ...]) -> None:
    if (
        type(values) is not tuple or len(values) > MAX_ASSETS
        or any(not isinstance(value, ReleaseAsset) for value in values)
    ):
        raise ReleaseAssetsError("The release asset inventory exceeds its limit or is invalid.")
    if len({value.name.casefold() for value in values}) != len(values):
        raise ReleaseAssetsError("Release asset filenames must be unique without case collisions.")


def native_engine_wheel(
    assets: tuple[ReleaseAsset, ...], *, require_bootstrap: bool = False,
) -> ReleaseAsset:
    """Require the exact native subjects and proofs, including bootstrap for new builds."""
    _unique_assets(assets)
    wheels = [asset for asset in assets if _WHEEL.fullmatch(asset.name)]
    if len(wheels) != 1:
        raise ReleaseAssetsError("The engine release requires its complete native asset set.")
    native = {
        ARCHIVE_NAME, ARCHIVE_NAME + PROOF_SUFFIX,
        wheels[0].name, wheels[0].name + PROOF_SUFFIX,
    }
    bootstrap = {
        name for script in BOOTSTRAP_SCRIPTS
        for name in (script, script + PROOF_SUFFIX)
    }
    names = {asset.name for asset in assets}
    if names != (native | bootstrap) and (require_bootstrap or names != native):
        raise ReleaseAssetsError("The engine release requires its complete native asset set.")
    return wheels[0]


@dataclass(frozen=True)
class ReferencedEngine:
    """An observed existing engine release, not assets to upload again."""

    release_id: str
    tag: str
    target: str
    assets: tuple[ReleaseAsset, ...]

    def __post_init__(self) -> None:
        _text(self.release_id, r"[1-9][0-9]*", 32)
        _text(self.tag, r"siteops/v[0-9][A-Za-z0-9.!_-]*", 160)
        _text(self.target, r"[0-9a-f]{40}", 40)
        native_engine_wheel(self.assets)

    @classmethod
    def from_document(cls, value: Any) -> ReferencedEngine:
        row = _object(value, {"releaseId", "tag", "target", "assets"})
        return cls(row["releaseId"], row["tag"], row["target"], _inventory(row["assets"]))

    def document(self) -> dict[str, Any]:
        return {
            "releaseId": self.release_id, "tag": self.tag, "target": self.target,
            "assets": [asset.document() for asset in self.assets],
        }


@dataclass(frozen=True)
class FrozenReleaseAssets:
    """Internal frozen inventory for one candidate, separate from public routing."""

    repository: str
    commit: str
    source_ref: str
    assets: tuple[ReleaseAsset, ...]
    engine: ReferencedEngine | None = None

    def __post_init__(self) -> None:
        _text(
            self.repository,
            r"[A-Za-z0-9][A-Za-z0-9.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
            140,
        )
        if self.repository.endswith("."):
            raise ReleaseAssetsError("The release repository identity is invalid.")
        _text(self.commit, r"[0-9a-f]{40}", 40)
        _text(self.source_ref, r"refs/(?:heads|tags)/[A-Za-z0-9._/-]+", 255)
        if ".." in self.source_ref or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) is None
            or part.endswith(".") or part.lower().endswith(".lock")
            for part in self.source_ref.split("/")[2:]
        ):
            raise ReleaseAssetsError("The release source ref is invalid.")
        _unique_assets(self.assets)
        if self.engine is not None and not isinstance(self.engine, ReferencedEngine):
            raise ReleaseAssetsError("The referenced engine identity is invalid.")
        if not self.assets and self.engine is None:
            raise ReleaseAssetsError("The release inventory contains no published or referenced assets.")

    @property
    def source(self) -> dict[str, str]:
        return {"repository": self.repository, "commit": self.commit, "ref": self.source_ref}

    @classmethod
    def from_document(cls, value: Any) -> FrozenReleaseAssets:
        row = _object(value, {"apiVersion", "kind", "source", "assets", "engine"})
        if row["apiVersion"] != API_VERSION or row["kind"] != "SiteOpsReleaseAssets":
            raise ReleaseAssetsError("The release asset inventory version or kind is unsupported.")
        source = _object(row["source"], {"repository", "commit", "ref"})
        return cls(
            source["repository"], source["commit"], source["ref"], _inventory(row["assets"]),
            ReferencedEngine.from_document(row["engine"]) if row["engine"] is not None else None,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> FrozenReleaseAssets:
        if type(raw) is not bytes or len(raw) > MAX_INVENTORY_BYTES:
            raise ReleaseAssetsError("The release asset inventory exceeds its byte limit.")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ReleaseAssetsError("The release asset inventory contains duplicate JSON keys.")
                result[key] = value
            return result

        def invalid_constant(value: str) -> None:
            raise ReleaseAssetsError("The release asset inventory contains a non-JSON number.")

        try:
            document = json.loads(
                raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant,
            )
        except ReleaseAssetsError:
            raise
        except (UnicodeError, ValueError, RecursionError) as error:
            raise ReleaseAssetsError("The release asset inventory must be bounded UTF-8 JSON.") from error
        return cls.from_document(document)

    @classmethod
    def read(cls, path: Path) -> FrozenReleaseAssets:
        with path.open("rb") as stream:
            return cls.from_bytes(stream.read(MAX_INVENTORY_BYTES + 1))

    def document(self) -> dict[str, Any]:
        return {
            "apiVersion": API_VERSION, "kind": "SiteOpsReleaseAssets", "source": self.source,
            "assets": [asset.document() for asset in self.assets],
            "engine": self.engine.document() if self.engine is not None else None,
        }

    def serialized(self) -> bytes:
        raw = (json.dumps(self.document(), indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > MAX_INVENTORY_BYTES:
            raise ReleaseAssetsError("The release asset inventory exceeds its byte limit.")
        return raw


def publication_assets(plan: dict[str, Any], inventory: FrozenReleaseAssets) -> tuple[tuple[ReleaseAsset, ...], tuple[ReleaseAsset, ...]]:
    """Partition the exact approved publication set into engine and workspace assets."""
    if inventory.source != plan["source"]:
        raise ReleaseAssetsError("The publication inventory describes another candidate.")
    requests = plan.get("workspaces", [])
    if type(requests) is not list or len(requests) > 64:
        raise ReleaseAssetsError("The publication workspace selection is invalid.")
    workspace_names: set[str] = {"siteops-workspaces.json"} if requests else set()
    for request in requests:
        name = validate_asset_name(request["package"])
        names = {name, validate_asset_name(name + PROOF_SUFFIX)}
        if workspace_names & names:
            raise ReleaseAssetsError("Publication workspace filenames must be unique.")
        workspace_names.update(names)
    workspace = tuple(asset for asset in inventory.assets if asset.name in workspace_names)
    native = tuple(asset for asset in inventory.assets if asset.name not in workspace_names)
    if {asset.name for asset in workspace} != workspace_names:
        raise ReleaseAssetsError("The publication set lacks declared workspace packages, proofs or routing metadata.")
    if plan["siteops"]["bundle"]:
        if inventory.engine is not None:
            raise ReleaseAssetsError("A built engine cannot also select a referenced engine.")
        native_engine_wheel(native, require_bootstrap=True)
    elif native or inventory.engine is None or inventory.engine.tag != plan["siteops"]["releaseTag"]:
        raise ReleaseAssetsError("The referenced engine differs from the reviewed selection.")
    return native, workspace

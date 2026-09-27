# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Prepare native engine identities for workspace qualification."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import struct
import zipfile
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version
from siteops_distribution import BundleManifest, load_manifest, verify_payload
from siteops_release import ReleaseIntent
from siteops_release_assets import (
    ARCHIVE_NAME,
    BOOTSTRAP_SCRIPTS,
    PROOF_SUFFIX,
    FrozenReleaseAssets,
    ReferencedEngine,
    ReleaseAsset,
    native_engine_wheel,
)

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import (
    ArtifactError,
    hash_file,
    hash_stream,
    load_artifact_json,
    open_regular_file,
)
from siteops.github_source import GitHubClient, GitHubReference
from siteops.github_workspace_source import download_release_asset
from siteops.workspace_source import ArtifactIdentity

MAX_ENGINE_ASSET_BYTES = 128 * 1024 * 1024
MAX_PROOF_BYTES = 2 * 1024 * 1024
SELECTION_NAME = "workspace-engine.json"
PLATFORMS = {"linux-x86_64": "ubuntu-24.04", "windows-x86_64": "windows-2025"}
PYTHONS = frozenset(f"3.{number}" for number in range(10, 15))
Verifier = Callable[[Path, Path, ArtifactIdentity, dict[str, str], str], ArtifactVerification]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineSelection:
    candidate: dict[str, str]
    plan_sha256: str
    native: FrozenReleaseAssets
    version: str
    manifest_sha256: str
    targets: tuple[tuple[str, str], ...]
    reference: ReferencedEngine | None = None

    def matrix(self) -> dict:
        rows = []
        for python, platform in self.targets:
            if python not in PYTHONS or platform not in PLATFORMS:
                raise ArtifactError(
                    "The engine declares an unsupported workspace qualification target."
                )
            rows.append({"python": python, "platform": platform, "os": PLATFORMS[platform]})
        if not rows or len(rows) != len(set(self.targets)):
            raise ArtifactError("The engine qualification matrix is empty or ambiguous.")
        return {"include": rows}

    def document(self) -> dict:
        return {
            "apiVersion": "siteops.release.engine/v1",
            "kind": "WorkspaceEngine",
            "candidate": self.candidate,
            "planSha256": self.plan_sha256,
            "native": self.native.document(),
            "version": self.version,
            "bundleManifestSha256": self.manifest_sha256,
            "targets": [
                {"python": python, "platform": platform} for python, platform in self.targets
            ],
            "reference": self.reference.document() if self.reference is not None else None,
        }

    def serialized(self) -> bytes:
        return (json.dumps(self.document(), sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )

    @classmethod
    def read(cls, path: Path, expected: str) -> EngineSelection:
        with open_regular_file(path) as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ArtifactError("The selected engine record differs from its expected identity.")
        row = load_artifact_json(raw, limit=2 * 1024 * 1024, label="Engine selection")
        if (
            type(row) is not dict
            or set(row)
            != {
                "apiVersion",
                "kind",
                "candidate",
                "planSha256",
                "native",
                "version",
                "bundleManifestSha256",
                "targets",
                "reference",
            }
            or row["apiVersion"] != "siteops.release.engine/v1"
            or row["kind"] != "WorkspaceEngine"
            or type(row["targets"]) is not list
            or any(
                type(target) is not dict or set(target) != {"python", "platform"}
                for target in row["targets"]
            )
        ):
            raise ArtifactError("The selected engine record has an unsupported shape.")
        result = cls(
            row["candidate"],
            row["planSha256"],
            FrozenReleaseAssets.from_document(row["native"]),
            row["version"],
            row["bundleManifestSha256"],
            tuple((target["python"], target["platform"]) for target in row["targets"]),
            ReferencedEngine.from_document(row["reference"])
            if row["reference"] is not None
            else None,
        )
        if (
            type(result.candidate) is not dict
            or set(result.candidate) != {"repository", "commit", "ref"}
            or type(result.version) is not str
            or len(result.version) > 128
            or str(Version(result.version)) != result.version
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in (result.plan_sha256, result.manifest_sha256))
            or result.native.engine is not None
            or (result.reference is not None and (
                result.reference.assets != result.native.assets
                or result.reference.tag != "siteops/v" + result.version
            ))
        ):
            raise ArtifactError("The selected engine identities are inconsistent.")
        native_engine_wheel(result.native.assets)
        result.matrix()
        return result


def _copy(source: Path, target: Path, identity: ReleaseAsset, created: list[Path]) -> None:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created.append(target)
    with os.fdopen(descriptor, "wb") as output, open_regular_file(source) as incoming:
        size = 0
        digest = hashlib.sha256()
        while chunk := incoming.read(min(1024 * 1024, identity.size - size + 1)):
            size += len(chunk)
            if size > identity.size:
                raise ArtifactError("An engine asset changed during staging.")
            output.write(chunk)
            digest.update(chunk)
    if (size, digest.hexdigest()) != (identity.size, identity.sha256):
        raise ArtifactError("An engine asset changed during staging.")


def _cleanup(files: list[Path], directories: list[Path]) -> None:
    for path in reversed(files):
        try:
            path.unlink()
        except OSError:
            logger.warning("An incomplete engine output file was retained.")
    for path in reversed(directories):
        try:
            path.rmdir()
        except OSError:
            logger.warning("An incomplete engine output directory was retained.")


@contextmanager
def _open_engine_bundle(path: Path, expected_sha: str, expected_manifest_sha: str | None = None):
    with open_regular_file(path) as incoming:
        size, digest = hash_stream(incoming, limit=MAX_ENGINE_ASSET_BYTES)
        if digest != expected_sha or size < 22:
            raise ArtifactError("The engine bundle differs from its expected bytes.")
        incoming.seek(size - 22)
        footer = incoming.read(22)
        (
            signature,
            disk,
            central_disk,
            disk_entries,
            entries,
            central_size,
            central_offset,
            comment,
        ) = struct.unpack(
            "<4s4H2IH",
            footer,
        )
        if (
            signature != b"PK\x05\x06"
            or disk
            or central_disk
            or comment
            or disk_entries != entries
            or not 1 <= entries <= 1025
            or central_size > 1024 * 1024
            or central_offset + central_size != size - 22
        ):
            raise ArtifactError("The engine ZIP directory is unsupported or exceeds its limits.")
        incoming.seek(0)
        with zipfile.ZipFile(incoming) as archive:
            if len(archive.infolist()) != entries:
                raise ArtifactError("The engine ZIP directory count is inconsistent.")
            manifest, manifest_sha = _inspect_archive(archive, expected_manifest_sha)
            yield archive, manifest, manifest_sha


def _inspect_archive(
    archive: zipfile.ZipFile, expected_manifest_sha: str | None
) -> tuple[BundleManifest, str]:
    members = archive.infolist()
    if len(members) > 1025 or len({entry.filename for entry in members}) != len(members):
        raise ArtifactError("The verified engine bundle has an invalid member inventory.")
    matches = [entry for entry in members if entry.filename == "bundle.json"]
    if len(matches) != 1 or not 0 < matches[0].file_size <= 1024 * 1024:
        raise ArtifactError("The verified engine bundle has no bounded manifest.")
    with archive.open(matches[0]) as stream:
        raw = stream.read(1024 * 1024 + 1)
    manifest_sha = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha is not None and manifest_sha != expected_manifest_sha:
        raise ArtifactError("The engine bundle manifest differs from the frozen selection.")
    document = load_artifact_json(raw, limit=1024 * 1024, label="Engine bundle manifest")
    manifest = BundleManifest.from_dict(document)
    expected = {"bundle.json": len(raw), **{entry.path: entry.size for entry in manifest.files}}
    if {entry.filename for entry in members} != set(expected):
        raise ArtifactError("The verified engine bundle differs from its payload inventory.")
    for entry in members:
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if (
            entry.is_dir()
            or kind not in {0, stat.S_IFREG}
            or entry.flag_bits & 1
            or entry.file_size != expected[entry.filename]
            or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise ArtifactError("The verified engine bundle contains an unsupported member.")
    return manifest, manifest_sha


def inspect_engine_bundle(
    path: Path,
    expected_sha: str,
    expected_manifest_sha: str | None = None,
) -> tuple[BundleManifest, str]:
    with _open_engine_bundle(path, expected_sha, expected_manifest_sha) as (
        _,
        manifest,
        manifest_sha,
    ):
        return manifest, manifest_sha


def extract_engine_bundle(
    archive_path: Path, destination: Path, selection: EngineSelection
) -> BundleManifest:
    """Extract verified native bytes, then validate the complete manifest and payload."""
    archive_id = next(asset for asset in selection.native.assets if asset.name == ARCHIVE_NAME)
    files: list[Path] = []
    directories: list[Path] = []
    complete = False
    try:
        with _open_engine_bundle(archive_path, archive_id.sha256, selection.manifest_sha256) as (
            archive,
            manifest,
            _,
        ):
            destination.mkdir(mode=0o700)
            directories.append(destination)
            for entry in archive.infolist():
                parent = destination
                parts = entry.filename.split("/")
                for part in parts[:-1]:
                    parent /= part
                    if parent not in directories:
                        parent.mkdir(mode=0o700)
                        directories.append(parent)
                target = parent / parts[-1]
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                files.append(target)
                size = 0
                with archive.open(entry) as incoming, os.fdopen(descriptor, "wb") as output:
                    while chunk := incoming.read(min(1024 * 1024, entry.file_size - size + 1)):
                        size += len(chunk)
                        if size > entry.file_size:
                            raise ArtifactError("An engine member exceeds its declared size.")
                        output.write(chunk)
        verified = load_manifest(destination)
        if verified != manifest:
            raise ArtifactError("The materialized engine manifest changed.")
        if (
            verified.version != selection.version
            or (verified.repository, verified.source_sha, verified.source_ref) != (
                selection.native.repository, selection.native.commit, selection.native.source_ref,
            )
            or tuple((target.python, target.platform) for target in verified.targets) != selection.targets
        ):
            raise ArtifactError("The engine manifest differs from the selected source and targets.")
        verify_payload(destination, verified)
        complete = True
        return verified
    finally:
        if not complete:
            _cleanup(files, directories)


def prepare_engine(
    intent: ReleaseIntent,
    plan_sha: str,
    output: Path,
    transfers: Path,
    *,
    build_number: int,
    build_attempt: int,
    builder_workflow: str,
    verifier: Verifier,
    built_assets: Path | None = None,
    archive_sha: str | None = None,
    wheel_sha: str | None = None,
    release_client: GitHubClient | None = None,
    downloader=download_release_asset,
) -> EngineSelection:
    """Freeze built or published native assets without rebuilding a referenced engine."""
    if not intent.workspaces:
        raise ArtifactError("Engine selection requires a workspace candidate.")
    version = intent.engine_version(build_number, build_attempt)
    reference = None
    snapshot = None
    if intent.bundle:
        if built_assets is None or not archive_sha or not wheel_sha:
            raise ArtifactError(
                "A built engine requires its exact staged assets and subject digests."
            )
        paths = []
        for path in built_assets.iterdir():
            if len(paths) == 8:
                raise ArtifactError("The engine build contains undeclared assets.")
            paths.append(path)
        assets = tuple(
            ReleaseAsset(path.name, *hash_file(path, limit=MAX_ENGINE_ASSET_BYTES))
            for path in paths
        )
        wheel = native_engine_wheel(assets, require_bootstrap=True)
        if (
            next(asset.sha256 for asset in assets if asset.name == ARCHIVE_NAME) != archive_sha
            or wheel.sha256 != wheel_sha
        ):
            raise ArtifactError("The engine subjects differ from the completed build.")
        source = intent.to_dict()["source"]
    else:
        if built_assets is not None:
            raise ArtifactError("A referenced engine cannot be replaced by a local build.")
        owner, repository = intent.repository.split("/", 1)
        client = release_client or GitHubClient(
            GitHubReference(owner, repository, intent.release_tag)
        )
        snapshot = client.resolve_release()
        if (
            snapshot.reference.owner.casefold() != owner.casefold()
            or snapshot.reference.repository.casefold() != repository.casefold()
            or snapshot.reference.ref != intent.release_tag
        ):
            raise ArtifactError("The resolved engine release differs from the reviewed selection.")
        assets = tuple(
            ReleaseAsset(asset.name, asset.size, asset.require_digest())
            for asset in snapshot.assets
        )
        native_engine_wheel(assets)
        reference = ReferencedEngine(
            str(snapshot.release_id), intent.release_tag, snapshot.tag_object, assets
        )
        source = {
            "repository": intent.repository,
            "commit": snapshot.source_commit,
            "ref": "refs/heads/main",
        }
        builder_workflow = ".github/workflows/release.yaml"
    for asset in assets:
        if asset.size > (
            MAX_PROOF_BYTES if asset.name.endswith(PROOF_SUFFIX) else MAX_ENGINE_ASSET_BYTES
        ):
            raise ArtifactError(
                "A native engine asset exceeds the workspace qualification transfer limit."
            )
    output.mkdir(mode=0o700)
    created: list[Path] = []
    complete = False
    try:
        for asset in assets:
            original = (
                nullcontext(built_assets / asset.name)
                if snapshot is None
                else downloader(
                    snapshot,
                    next(value for value in snapshot.assets if value.name == asset.name),
                    staging_parent=transfers,
                )
            )
            with original as path:
                _copy(path, output / asset.name, asset, created)
        wheel = native_engine_wheel(assets)
        subjects = (
            next(value for value in assets if value.name == ARCHIVE_NAME),
            wheel,
            *(value for value in assets if value.name in BOOTSTRAP_SCRIPTS),
        )
        for asset in subjects:
            proof = next(value for value in assets if value.name == asset.name + PROOF_SUFFIX)
            receipt = verifier(
                output / asset.name,
                output / proof.name,
                ArtifactIdentity(asset.name, asset.size, asset.sha256),
                source,
                builder_workflow,
            )
            if (
                not isinstance(receipt, ArtifactVerification)
                or (receipt.sha256, receipt.size, receipt.proof_sha256)
                != (asset.sha256, asset.size, proof.sha256)
                or hash_file(output / asset.name, limit=asset.size) != (asset.size, asset.sha256)
                or hash_file(output / proof.name, limit=proof.size) != (proof.size, proof.sha256)
            ):
                raise ArtifactError(
                    "Engine verification did not retain the selected subject and proof identities."
                )
        archive_id = next(value for value in assets if value.name == ARCHIVE_NAME)
        manifest, manifest_sha = inspect_engine_bundle(output / ARCHIVE_NAME, archive_id.sha256)
        if (
            manifest.version != version
            or manifest.application_wheel != "wheels/" + wheel.name
            or (manifest.repository, manifest.source_sha, manifest.source_ref)
            != (
                source["repository"],
                source["commit"],
                source["ref"],
            )
        ):
            raise ArtifactError(
                "The verified engine bundle differs from the selected version and source."
            )
        with _open_engine_bundle(output / ARCHIVE_NAME, archive_id.sha256, manifest_sha) as (
            archive,
            _,
            _,
        ):
            with archive.open(manifest.application_wheel) as stream:
                size, embedded = hash_stream(stream, limit=wheel.size)
        if (size, embedded) != (wheel.size, wheel.sha256):
            raise ArtifactError(
                "The standalone engine wheel differs from the verified bundle member."
            )
        selection = EngineSelection(
            intent.to_dict()["source"],
            plan_sha,
            FrozenReleaseAssets(source["repository"], source["commit"], source["ref"], assets),
            version,
            manifest_sha,
            tuple((target.python, target.platform) for target in manifest.targets),
            reference,
        )
        selection.matrix()
        record = output / SELECTION_NAME
        with record.open("xb") as stream:
            created.append(record)
            stream.write(selection.serialized())
        complete = True
        return selection
    finally:
        if not complete:
            _cleanup(created, [output])

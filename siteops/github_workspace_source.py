# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bind workspace release routing to observed GitHub asset identities."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from siteops.artifacts import open_regular_file
from siteops.asset_transfer import download_https_asset
from siteops.github_attestation import MAX_EVIDENCE_BYTES
from siteops.github_source import GitHubClient, GitHubReleaseAsset, GitHubReleaseSnapshot
from siteops.workspace_source import (
    MAX_DESCRIPTOR_BYTES,
    MAX_SOURCE_ARTIFACT_BYTES,
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    ResolvedReleaseSource,
    ResolvedWorkspaceSource,
    SourceResolutionError,
)

_ASSET_ORIGINS = (
    "https://api.github.com",
    "https://release-assets.githubusercontent.com",
    "https://objects.githubusercontent.com",
)


def _artifact_identity(asset: GitHubReleaseAsset, *, limit: int) -> ArtifactIdentity:
    if asset.sha256 is None:
        raise SourceResolutionError("A selected release asset has no SHA-256 digest.", code="source.identity")
    if asset.size > limit:
        raise SourceResolutionError("A selected release asset exceeds its byte limit.", code="source.limit")
    return ArtifactIdentity(asset.name, asset.size, asset.sha256)


def workspace_descriptor_asset(release: GitHubReleaseSnapshot) -> GitHubReleaseAsset:
    """Select the fixed descriptor before its bytes are downloaded or parsed."""
    matches = [
        asset for asset in release.assets
        if asset.name.casefold() == WORKSPACE_RELEASE_NAME.casefold()
    ]
    if len(matches) != 1 or matches[0].name != WORKSPACE_RELEASE_NAME:
        raise SourceResolutionError(
            "The release must contain exactly one canonical workspace descriptor.",
            code="source.descriptor-missing",
        )
    _artifact_identity(matches[0], limit=MAX_DESCRIPTOR_BYTES)
    return matches[0]


@dataclass(frozen=True)
class GitHubWorkspaceSource:
    """Provider observations paired with common selection expectations, not authorization."""

    resolved: ResolvedWorkspaceSource
    release: GitHubReleaseSnapshot
    descriptor_asset: GitHubReleaseAsset
    package_asset: GitHubReleaseAsset
    proof_asset: GitHubReleaseAsset


def bind_workspace_release(
    release: GitHubReleaseSnapshot,
    descriptor_bytes: bytes,
    *,
    workspace: str | None = None,
) -> GitHubWorkspaceSource:
    """Check descriptor routing against exact observed assets before acquisition.

    This performs no downloads or provenance verification. GitHub's immutable
    release flag remains provider evidence, not a substitute for pinned bytes,
    source revision and consumer policy.
    """
    descriptor_asset = workspace_descriptor_asset(release)
    descriptor_identity = _artifact_identity(descriptor_asset, limit=MAX_DESCRIPTOR_BYTES)
    reference = release.reference
    if reference.ref is None:
        raise SourceResolutionError("A resolved workspace source requires an explicit release.")
    source = ResolvedReleaseSource(
        provider="github-release/v1",
        reference=f"github:{reference.owner}/{reference.repository}",
        release=reference.ref,
        revision=release.source_commit,
        descriptor=descriptor_identity,
    )
    descriptor = source.parse_descriptor(descriptor_bytes)

    assets = {asset.name: asset for asset in release.assets}
    if len(assets) != len(release.assets):
        raise SourceResolutionError("The release asset inventory contains duplicate names.")
    for entry in descriptor.workspaces:
        for identity in (entry.package, entry.proof):
            asset = assets.get(identity.name)
            if asset is None or (asset.size, asset.sha256) != (identity.size, identity.sha256):
                raise SourceResolutionError(
                    "Workspace release routing differs from the observed asset inventory.",
                    code="source.identity",
                )
        _artifact_identity(assets[entry.proof.name], limit=MAX_EVIDENCE_BYTES)

    entry = descriptor.select(workspace)
    return GitHubWorkspaceSource(
        ResolvedWorkspaceSource(source, entry), release, descriptor_asset,
        assets[entry.package.name], assets[entry.proof.name],
    )


@contextmanager
def download_release_asset(
    release: GitHubReleaseSnapshot, asset: GitHubReleaseAsset, *, staging_parent: Path,
) -> Iterator[Path]:
    """Download an observed asset by ID using anonymous HTTPS and fixed provider origins."""
    if (
        asset not in release.assets or type(asset.identifier) is not int
        or not 0 < asset.identifier <= 2**63 - 1
    ):
        raise SourceResolutionError("The selected asset must belong to the observed release.")
    identity = _artifact_identity(asset, limit=MAX_SOURCE_ARTIFACT_BYTES)
    reference = release.reference
    url = (
        "https://api.github.com/repos/"
        f"{quote(reference.owner, safe='')}/{quote(reference.repository, safe='')}"
        f"/releases/assets/{asset.identifier}"
    )
    with download_https_asset(
        url, identity, origins=_ASSET_ORIGINS, staging_parent=staging_parent,
    ) as path:
        yield path


@dataclass(frozen=True)
class GitHubWorkspaceDownload:
    """Opaque package and proof paths valid only within the enclosing download context."""

    source: GitHubWorkspaceSource
    package: Path
    proof: Path


def resolve_workspace_release(
    client: GitHubClient, *, staging_parent: Path, workspace: str | None = None,
) -> GitHubWorkspaceSource:
    """Bind one workspace through its descriptor before downloading a package or proof."""
    if client.auth != "anonymous":
        raise SourceResolutionError(
            "Workspace asset acquisition currently uses anonymous HTTPS.",
            code="source.auth-unsupported",
        )
    release = client.resolve_release()
    descriptor = workspace_descriptor_asset(release)
    with download_release_asset(release, descriptor, staging_parent=staging_parent) as path:
        with open_regular_file(path) as stream:
            return bind_workspace_release(
                release, stream.read(MAX_DESCRIPTOR_BYTES + 1), workspace=workspace,
            )


@contextmanager
def download_workspace_release(
    client: GitHubClient, *, staging_parent: Path, workspace: str | None = None,
) -> Iterator[GitHubWorkspaceDownload]:
    """Download one selected workspace without extracting or authorizing it.

    Consumer policy and trusted roots are supplied separately to verification.
    Source read access and descriptor metadata never select that authority.
    """
    selected = resolve_workspace_release(client, staging_parent=staging_parent, workspace=workspace)
    with download_release_asset(
        selected.release, selected.package_asset, staging_parent=staging_parent,
    ) as package, download_release_asset(
        selected.release, selected.proof_asset, staging_parent=staging_parent,
    ) as proof:
        yield GitHubWorkspaceDownload(selected, package, proof)

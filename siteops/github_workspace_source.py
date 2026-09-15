# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bind workspace release routing to observed GitHub asset identities."""

from __future__ import annotations

from dataclasses import dataclass

from siteops.github_attestation import MAX_EVIDENCE_BYTES
from siteops.github_source import GitHubReleaseAsset, GitHubReleaseSnapshot
from siteops.workspace_source import (
    MAX_DESCRIPTOR_BYTES,
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    ResolvedReleaseSource,
    ResolvedWorkspaceSource,
    SourceResolutionError,
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

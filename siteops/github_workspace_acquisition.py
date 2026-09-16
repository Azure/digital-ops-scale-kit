# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""GitHub source acquisition under independently supplied local verification policy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import hash_file
from siteops.cache_filesystem import CacheError
from siteops.github_attestation import (
    MAX_EVIDENCE_BYTES,
    GitHubArtifactPolicy,
    VerificationError,
    load_github_policy,
    verify_github_artifact,
)
from siteops.github_source import GitHubClient, GitHubReference
from siteops.github_workspace_source import (
    GitHubWorkspaceSource,
    download_release_asset,
    resolve_workspace_release,
)
from siteops.workspace_acquisition import WorkspaceAcquisition
from siteops.workspace_cache import CachedWorkspace, WorkspaceCache
from siteops.workspace_source import ResolvedWorkspaceSource, SourceResolutionError


class GitHubWorkspaceAcquirer:
    """Acquire a release or reuse bytes identified by a workspace pin.

    Policy and trusted roots are local inputs selected by application code.
    Leasing bytes selected by a workspace pin performs no source requests.
    Explicit acquisition resolves the requested release again, reusing valid
    cached package and proof bytes.
    """

    def __init__(self, cache: WorkspaceCache, *, policy_file: Path, trusted_root: Path):
        self.cache = cache
        self.policy_file = Path(policy_file).absolute()
        self.trusted_root = Path(trusted_root).absolute()
        self._acquisition = WorkspaceAcquisition(cache, verify=self._verify)

    def _policy_for(self, reference: str) -> GitHubArtifactPolicy:
        policy = load_github_policy(self.policy_file)
        if reference.casefold() != f"github:{policy.repository}".casefold():
            raise VerificationError("The selected source differs from the consumer policy repository.")
        if datetime.now(timezone.utc) >= policy.valid_until:
            raise VerificationError("The artifact verification policy has expired. Refresh it explicitly.")
        if hash_file(self.trusted_root, limit=MAX_EVIDENCE_BYTES)[1] != policy.trusted_root_sha256:
            raise VerificationError("The trusted-root snapshot does not match consumer policy.")
        return policy

    def _verify(
        self, artifact: Path, proof: Path, source: ResolvedWorkspaceSource,
    ) -> ArtifactVerification:
        if source.source.provider != "github-release/v1":
            raise VerificationError("This acquisition adapter requires a GitHub release source.")
        policy = self._policy_for(source.source.reference)
        result = verify_github_artifact(
            artifact, proof, self.trusted_root, self.policy_file,
            expected_sha256=source.entry.package.sha256,
            source_commit=source.source.revision, staging_parent=self.cache.root / "staging",
        )
        if result.policy_sha256 != policy.sha256:
            raise VerificationError("The consumer policy changed during source verification. Retry the acquisition.")
        return result

    def _retain_proof(self, selected: GitHubWorkspaceSource) -> None:
        expected = selected.resolved.entry.proof
        try:
            with self.cache.lease_proof(expected):
                return
        except CacheError as error:
            if error.code != "cache.proof-missing":
                raise
        with download_release_asset(
            selected.release, selected.proof_asset, staging_parent=self.cache.root / "staging",
        ) as path:
            self.cache.retain_proof(path, expected)

    def acquire(
        self, client: GitHubClient, *, workspace: str | None = None,
        expected: ResolvedWorkspaceSource | None = None,
    ) -> GitHubWorkspaceSource:
        """Resolve the explicit release and acquire only missing package and proof bytes."""
        reference = client.reference
        self._policy_for(f"github:{reference.owner}/{reference.repository}")
        selected = resolve_workspace_release(
            client, staging_parent=self.cache.root / "staging", workspace=workspace,
        )
        if expected is not None and selected.resolved != expected:
            raise SourceResolutionError(
                "The published source differs from the workspace pin. Repin explicitly to change the selection.",
                code="source.pin-changed",
            )
        try:
            with self._acquisition.lease(selected.resolved):
                return selected

        except CacheError as error:
            if error.code not in {"cache.missing", "cache.proof-missing"}:
                raise
            missing = error.code
        self._retain_proof(selected)
        if missing == "cache.proof-missing":
            with self._acquisition.lease(selected.resolved):
                return selected
        with download_release_asset(
            selected.release, selected.package_asset, staging_parent=self.cache.root / "staging",
        ) as path:
            self._acquisition.publish(selected.resolved, path)
        return selected

    def restore(self, source: ResolvedWorkspaceSource) -> None:
        """Acquire missing bytes only when release observations match the workspace pin."""
        if source.source.provider != "github-release/v1":
            raise VerificationError("This acquisition adapter requires a GitHub release source.")
        client = GitHubClient(GitHubReference.parse(source.source.reference, ref=source.source.release))
        self.acquire(client, workspace=source.entry.workspace, expected=source)

    @contextmanager
    def lease(self, source: ResolvedWorkspaceSource) -> Iterator[CachedWorkspace]:
        """Hold a locally revalidated package through ordinary inspection or execution."""
        with self._acquisition.lease(source) as content:
            yield content

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Verify selected source content and retained proofs through the workspace cache."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from siteops.artifact_verification import ArtifactVerification
from siteops.workspace_cache import ArtifactVerifier, CachedWorkspace, WorkspaceCache
from siteops.workspace_package import PackageInspection
from siteops.workspace_source import ResolvedWorkspaceSource, SourceResolutionError

ProofVerifier = Callable[[Path, Path, ResolvedWorkspaceSource], ArtifactVerification]


class WorkspaceAcquisition:
    """Apply source expectations with a trusted verifier supplied by application code.

    Proof storage is opaque. The verifier runs on each publication and lease,
    with source and proof identity checked before package content is admitted.
    Persisted receipts and source metadata never choose the verifier or policy.
    """

    def __init__(self, cache: WorkspaceCache, *, verify: ProofVerifier):
        if not isinstance(cache, WorkspaceCache) or not callable(verify):
            raise TypeError("Acquisition requires a workspace cache and a trusted verifier.")
        self.cache = cache
        self._verify = verify

    def _verifier(self, source: ResolvedWorkspaceSource) -> ArtifactVerifier:
        if not isinstance(source, ResolvedWorkspaceSource):
            raise SourceResolutionError("Acquisition requires a resolved workspace source.")

        def verify(artifact: Path) -> ArtifactVerification:
            with self.cache.lease_proof(source.entry.proof) as proof:
                result = self._verify(artifact, proof, source)
                if (
                    not isinstance(result, ArtifactVerification)
                    or result.proof_sha256 != source.entry.proof.sha256
                ):
                    raise SourceResolutionError(
                        "Verification evidence must identify the selected proof.",
                        code="source.identity",
                    )
            return result

        return verify

    def publish(self, source: ResolvedWorkspaceSource, archive: Path) -> PackageInspection:
        """Verify with the retained proof and check selection before atomic publication."""
        verifier = self._verifier(source)
        return self.cache.publish(
            archive, source.entry.package.sha256,
            source_revision=source.source.revision, verify=verifier,
            check_source=source.check_package,
        )

    @contextmanager
    def lease(self, source: ResolvedWorkspaceSource) -> Iterator[CachedWorkspace]:
        """Revalidate a pinned workspace locally, holding the lease through its use."""
        verifier = self._verifier(source)
        with self.cache.lease(
            source.entry.package.sha256,
            source_revision=source.source.revision, verify=verifier,
            check_source=source.check_package,
        ) as content:
            yield content

"""Small complete workspace and explicit verifier doubles for acquisition boundaries."""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import hash_file
from siteops.package_builder import build_package
from siteops.workspace_cache import WorkspaceCache
from siteops.workspace_source import (
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    ResolvedReleaseSource,
    ResolvedWorkspaceSource,
    WorkspaceReleaseAssets,
    WorkspaceReleaseEntry,
)


def identity(name, content):
    return ArtifactIdentity(name, len(content), hashlib.sha256(content).hexdigest())


class RecordingVerifier:
    def __init__(self):
        self.calls = []
        self.changes = {}
        self.version = 1

    def __call__(self, artifact, proof, source):
        self.calls.append((artifact, proof, source))
        size, digest = hash_file(artifact, limit=source.entry.package.size)
        proof_digest = hash_file(proof, limit=source.entry.proof.size)[1]
        now = datetime.now(timezone.utc)
        return replace(ArtifactVerification(
            digest, size, "test-publisher", self.version,
            hashlib.sha256(str(self.version).encode()).hexdigest(),
            "2" * 64, proof_digest, now, now + timedelta(days=1),
            "test-verifier", "1", b"{}",
        ), **self.changes)


@dataclass
class SourceFixture:
    cache: WorkspaceCache
    source: ResolvedWorkspaceSource
    archive: Path
    proof: Path
    descriptor: bytes
    verifier: RecordingVerifier


def make_source(
    tmp_path, *, provider="example-registry/v1", reference="registry:example/storage",
    revision="opaque:revision-7",
):
    directory = tmp_path / "source"
    workspace = directory / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "templates").mkdir()
    (workspace / "manifests" / "storage.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nname: storage\nselector: name=one\nsteps:\n"
        "  - name: storage\n    template: templates/main.json\n    scope: resourceGroup\n",
        encoding="utf-8",
    )
    (workspace / "templates" / "main.json").write_text(json.dumps({
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "resources": [],
    }), encoding="utf-8")
    archive = tmp_path / "workspace.zip"
    inspection = build_package(
        directory, archive, workspace="workspace", kit_id="example.storage",
        version="7", source_revision=revision, siteops_range=">=1.0.0b1,<2",
    )
    proof = tmp_path / "proof.jsonl"
    proof.write_bytes(b'{"fixture":"opaque proof"}\n')
    entry = WorkspaceReleaseEntry(
        "workspace", "example.storage", "7",
        ArtifactIdentity(archive.name, inspection.size, inspection.sha256),
        identity(proof.name, proof.read_bytes()),
    )
    descriptor = WorkspaceReleaseAssets(revision, (entry,)).serialized()
    selected = ResolvedWorkspaceSource(
        ResolvedReleaseSource(
            provider, reference, "release-7", revision, identity(WORKSPACE_RELEASE_NAME, descriptor),
        ),
        entry,
    )
    return SourceFixture(
        WorkspaceCache(tmp_path / "cache", lock_timeout=0), selected,
        archive, proof, descriptor, RecordingVerifier(),
    )

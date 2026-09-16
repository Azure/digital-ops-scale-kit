"""Prepare owned unsigned content and a pin through the installed engine's real APIs."""

import hashlib
import json
import sys
from pathlib import Path

import siteops
from siteops.github_attestation import verify_github_artifact
from siteops.package_builder import build_package
from siteops.project import WorkspacePin, project_root, write_pin
from siteops.workspace_acquisition import WorkspaceAcquisition
from siteops.workspace_cache import WorkspaceCache
from siteops.workspace_source import (
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    ResolvedReleaseSource,
    ResolvedWorkspaceSource,
    WorkspaceReleaseAssets,
    WorkspaceReleaseEntry,
)

root = Path(sys.argv[1]).resolve()
assert Path(siteops.__file__).is_relative_to(root / "application")
source = root / "authored"
workspace = source / "workspace"
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
revision = "a" * 40
archive = root / "workspace.zip"
inspection = build_package(
    source, archive, workspace="workspace", kit_id="example.storage", version="7",
    source_revision=revision, siteops_range=">=1.0.0b1,<2",
)
proof, trusted = root / "proof.jsonl", root / "trusted-root.json"
proof.write_bytes(b"unsigned fixture proof")
trusted.write_bytes(b"fixture trusted roots")
policy = root / "policy.json"
policy.write_text(json.dumps({
    "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
    "id": "installed-fixture", "version": 1, "validUntil": "2100-01-01T00:00:00Z",
    "trustedRootSha256": hashlib.sha256(trusted.read_bytes()).hexdigest(),
    "provider": {
        "kind": "github-attestation/v1", "repository": "example/content",
        "sourceRef": "refs/heads/main", "signerWorkflow": ".github/workflows/sign.yml",
        "builderWorkflow": ".github/workflows/release.yml",
    },
}), encoding="utf-8")
(root / "tool-context.json").write_text(json.dumps({
    "digest": inspection.sha256, "revision": revision,
    "proof": hashlib.sha256(proof.read_bytes()).hexdigest(),
    "root": hashlib.sha256(trusted.read_bytes()).hexdigest(),
}), encoding="utf-8")


def identity(name, content):
    return ArtifactIdentity(name, len(content), hashlib.sha256(content).hexdigest())


entry = WorkspaceReleaseEntry(
    "workspace", "example.storage", "7",
    ArtifactIdentity(archive.name, inspection.size, inspection.sha256),
    identity(proof.name, proof.read_bytes()),
)
descriptor = WorkspaceReleaseAssets(revision, (entry,)).serialized()
selected = ResolvedWorkspaceSource(
    ResolvedReleaseSource(
        "github-release/v1", "github:example/content", "release-7", revision,
        identity(WORKSPACE_RELEASE_NAME, descriptor),
    ), entry,
)
cache = WorkspaceCache(root / "cache")
cache.retain_proof(proof, entry.proof)


def verify(artifact, retained_proof, source):
    return verify_github_artifact(
        artifact, retained_proof, trusted, policy,
        expected_sha256=source.entry.package.sha256, source_commit=source.source.revision,
        staging_parent=cache.root / "staging",
    )


WorkspaceAcquisition(cache, verify=verify).publish(selected, archive)
project = project_root(root / "project", create=True)
(project / "sites").mkdir()
(project / "sites" / "one.yaml").write_text(
    "apiVersion: siteops/v1\nkind: Site\nname: one\nsubscription: fixture-subscription\n"
    "resourceGroup: fixture-group\nlocation: eastus\n", encoding="utf-8",
)
write_pin(project, WorkspacePin(selected), expected_previous=None)
print(json.dumps({"package": entry.package.sha256, "proof": entry.proof.sha256}))

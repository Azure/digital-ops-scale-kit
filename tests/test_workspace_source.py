"""Workspace release routing, selection and independently bound source expectations."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from siteops import package_builder
from siteops.github_source import GitHubReference, GitHubReleaseAsset, GitHubReleaseSnapshot
from siteops.github_workspace_source import bind_workspace_release, workspace_descriptor_asset
from siteops.workspace_source import (
    MAX_DESCRIPTOR_BYTES,
    MAX_WORKSPACES,
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    ResolvedReleaseSource,
    ResolvedWorkspaceSource,
    SourceResolutionError,
    WorkspaceReleaseAssets,
    WorkspaceReleaseEntry,
)

REVISION = "1" * 40


def identity(name, content):
    return {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def entry(workspace="workspace", suffix=""):
    return {
        "workspace": workspace,
        "kit": {"id": "example.storage" + suffix, "version": "preview-7"},
        "package": identity("package" + suffix + ".zip", b"package"),
        "proof": identity("proof" + suffix + ".jsonl", b"proof"),
    }


def document(*entries, revision=REVISION):
    return {
        "apiVersion": "siteops/v1alpha1",
        "kind": "WorkspaceReleaseAssets",
        "source": {"revision": revision},
        "workspaces": list(entries) or [entry()],
    }


def raw(document):
    return json.dumps(document, ensure_ascii=True).encode("utf-8")


def snapshot(document, *, immutable=False, extra=()):
    descriptor = raw(document)
    artifacts = [identity(WORKSPACE_RELEASE_NAME, descriptor)]
    for workspace in document["workspaces"]:
        artifacts.extend((workspace["package"], workspace["proof"]))
    assets = tuple(
        GitHubReleaseAsset(index + 1, value["name"], value["size"], value["sha256"])
        for index, value in enumerate(artifacts)
    )
    return GitHubReleaseSnapshot(
        GitHubReference("example", "content", "v7"), 11, 22, REVISION, REVISION,
        True, datetime(2026, 1, 1, tzinfo=timezone.utc), immutable, assets + extra,
    ), descriptor


def test_descriptor_round_trip_is_provider_independent_and_does_not_require_an_index():
    descriptor = WorkspaceReleaseAssets.from_bytes(raw(document(revision="registry:opaque-revision")))
    assert descriptor.revision == "registry:opaque-revision"
    assert descriptor.select().workspace == "workspace"
    assert descriptor.select().index_sha256 is None
    encoded = descriptor.serialized()
    assert WorkspaceReleaseAssets.from_bytes(encoded) == descriptor
    assert b"github" not in encoded
    assert b"policy" not in encoded
    assert b"download" not in encoded


def test_workspace_selection_is_exact_and_explicit_when_ambiguous():
    descriptor = WorkspaceReleaseAssets.from_bytes(raw(document(
        entry("z/workspace", "-z"), entry(".", "-root"),
    )))
    with pytest.raises(SourceResolutionError) as failure:
        descriptor.select()
    assert failure.value.code == "source.workspace-ambiguous"
    assert failure.value.choices == (".", "z/workspace")
    assert descriptor.select(".").workspace == "."
    with pytest.raises(SourceResolutionError) as failure:
        descriptor.select("Z/workspace")
    assert failure.value.code == "source.workspace-not-found"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(kind="Other"),
    lambda value: value.update(policy={"trusted": True}),
    lambda value: value["source"].update(revision=""),
    lambda value: value["source"].update(repository="unapproved"),
    lambda value: value.update(workspaces=[]),
    lambda value: value["workspaces"][0].update(workspace="../escape"),
    lambda value: value["workspaces"][0].update(workspace="workspace\\nested"),
    lambda value: value["workspaces"][0].update(downloadUrl="https://example.invalid"),
    lambda value: value["workspaces"][0]["kit"].update(version=True),
    lambda value: value["workspaces"][0]["package"].update(name="nested/package.zip"),
    lambda value: value["workspaces"][0]["package"].update(name=WORKSPACE_RELEASE_NAME),
    lambda value: value["workspaces"][0]["package"].update(size=True),
    lambda value: value["workspaces"][0]["package"].update(size=0),
    lambda value: value["workspaces"][0]["package"].update(sha256="A" * 64),
    lambda value: value["workspaces"][0].pop("proof"),
    lambda value: value["workspaces"][0].update(index={"sha256": None}),
])
def test_descriptor_rejects_unsupported_or_ambiguous_shapes(mutate):
    value = document()
    mutate(value)
    with pytest.raises(SourceResolutionError):
        WorkspaceReleaseAssets.from_bytes(raw(value))


@pytest.mark.parametrize("content", [
    b'{"kind":"WorkspaceReleaseAssets","kind":"PRIVATE_VALUE"}',
    b'{"value":NaN}',
    b"\xff",
    b"[" * 3000,
    b" " * (MAX_DESCRIPTOR_BYTES + 1),
], ids=["duplicate-key", "non-json-number", "invalid-utf8", "deep-json", "oversized"])
def test_descriptor_json_limits_and_errors_are_value_safe(content):
    with pytest.raises(SourceResolutionError) as failure:
        WorkspaceReleaseAssets.from_bytes(content)
    assert "PRIVATE_VALUE" not in str(failure.value)


@pytest.mark.parametrize("change", ["workspace", "workspace-case", "package", "proof"])
def test_descriptor_rejects_colliding_entries_and_artifact_names(change):
    first, second = entry("workspace"), entry("other", "-other")
    if change == "workspace":
        second["workspace"] = first["workspace"]
    elif change == "workspace-case":
        second["workspace"] = first["workspace"].upper()
    else:
        second[change]["name"] = first[change]["name"]
    with pytest.raises(SourceResolutionError):
        WorkspaceReleaseAssets.from_bytes(raw(document(first, second)))


def test_descriptor_workspace_count_is_bounded():
    entries = [entry(f"workspace-{index}", f"-{index}") for index in range(MAX_WORKSPACES + 1)]
    with pytest.raises(SourceResolutionError):
        WorkspaceReleaseAssets.from_bytes(raw(document(*entries)))


def test_generated_descriptor_cannot_exceed_the_consumer_byte_limit():
    entries = tuple(
        WorkspaceReleaseEntry(
            "/".join(["\u00e9" * 120] * 4) + f"/{index}",
            "\u00e9" * 128, "\u00e9" * 128,
            ArtifactIdentity("\u00e9" * 100 + f"-{index}.zip", 1, "a" * 64),
            ArtifactIdentity("\u00e9" * 100 + f"-{index}.proof", 1, "b" * 64),
        )
        for index in range(MAX_WORKSPACES)
    )
    descriptor = WorkspaceReleaseAssets("opaque", entries)
    with pytest.raises(SourceResolutionError, match="byte limit"):
        descriptor.serialized()


@pytest.mark.parametrize("immutable", [False, True, None])
def test_github_binding_preserves_routing_and_optional_index_without_authorizing(immutable):
    value = document()
    value["workspaces"][0]["index"] = {"sha256": "c" * 64}
    unrelated = GitHubReleaseAsset(1000, "unrelated.bin", 200, None)
    release, data = snapshot(value, immutable=immutable, extra=(unrelated,))
    bound = bind_workspace_release(release, data)
    assert bound.resolved.source.provider == "github-release/v1"
    assert bound.resolved.source.reference == "github:example/content"
    assert bound.resolved.source.release == "v7"
    assert bound.resolved.source.revision == REVISION
    assert bound.resolved.entry.index_sha256 == "c" * 64
    assert bound.package_asset.identifier == 2
    assert bound.proof_asset.identifier == 3
    assert bound.release.immutable is immutable
    assert workspace_descriptor_asset(release).identifier == 1


@pytest.mark.parametrize("change", ["missing", "duplicate-case", "missing-digest", "oversized"])
def test_descriptor_asset_must_be_identified_before_parsing(change):
    release, data = snapshot(document())
    assets = list(release.assets)
    if change == "missing":
        assets.pop(0)
    elif change == "duplicate-case":
        assets.append(replace(assets[0], identifier=100, name=WORKSPACE_RELEASE_NAME.upper()))
    elif change == "missing-digest":
        assets[0] = replace(assets[0], sha256=None)
    else:
        assets[0] = replace(assets[0], size=MAX_DESCRIPTOR_BYTES + 1)
    with pytest.raises(SourceResolutionError):
        bind_workspace_release(replace(release, assets=tuple(assets)), data)


def test_descriptor_bytes_are_checked_before_json_parsing():
    release, _ = snapshot(document())
    with pytest.raises(SourceResolutionError) as failure:
        bind_workspace_release(release, b"not-json")
    assert failure.value.code == "source.identity"


def test_descriptor_source_revision_must_match_the_resolved_tag():
    release, data = snapshot(document(revision="2" * 40))
    with pytest.raises(SourceResolutionError) as failure:
        bind_workspace_release(release, data)
    assert failure.value.code == "source.identity"


@pytest.mark.parametrize("change", ["size", "digest", "missing", "large-proof"])
def test_all_declared_artifacts_match_provider_observations(change):
    value = document(entry(), entry("other", "-other"))
    if change == "large-proof":
        value["workspaces"][1]["proof"]["size"] = 3 * 1024 * 1024
    release, data = snapshot(value)
    assets = list(release.assets)
    if change == "size":
        assets[-1] = replace(assets[-1], size=assets[-1].size + 1)
    elif change == "digest":
        assets[-1] = replace(assets[-1], sha256="f" * 64)
    elif change == "missing":
        assets.pop()
    with pytest.raises(SourceResolutionError):
        bind_workspace_release(replace(release, assets=tuple(assets)), data, workspace="workspace")


def test_common_selection_checks_real_package_metadata_after_integrity_inspection(tmp_path):
    source = tmp_path / "source"
    workspace = source / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "manifests" / "empty.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nname: empty\nsteps: []\n",
        encoding="utf-8",
    )
    inspection = package_builder.build_package(
        source, tmp_path / "package.zip", workspace="workspace", kit_id="example.storage",
        version="preview-7", source_revision="registry:revision",
        siteops_range=">=1.0.0b1,<2",
    )
    value = document(revision="registry:revision")
    value["workspaces"][0]["package"].update(size=inspection.size, sha256=inspection.sha256)
    descriptor = WorkspaceReleaseAssets.from_bytes(raw(value))
    data = descriptor.serialized()
    resolved = ResolvedWorkspaceSource(
        ResolvedReleaseSource(
            "registry/v1", "catalog:team", "release-7", descriptor.revision,
            ArtifactIdentity(WORKSPACE_RELEASE_NAME, len(data), hashlib.sha256(data).hexdigest()),
        ),
        descriptor.select(),
    )
    assert resolved.source.parse_descriptor(data) == descriptor
    resolved.check_package(inspection)
    for changes in (
        {"kit_id": "different"}, {"version": "different"},
        {"workspace_root": "."}, {"source_revision": "different"},
    ):
        with pytest.raises(SourceResolutionError):
            resolved.check_package(replace(inspection, metadata=replace(inspection.metadata, **changes)))
    with pytest.raises(SourceResolutionError):
        resolved.check_package(replace(inspection, sha256="f" * 64))

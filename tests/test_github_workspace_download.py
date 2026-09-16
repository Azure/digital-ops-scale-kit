"""GitHub asset IDs, descriptor selection and opaque package/proof lifetimes."""

import hashlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from siteops import github_workspace_source as source
from siteops.cache_filesystem import make_private_directory
from siteops.github_source import (
    GitHubClient,
    GitHubReference,
    GitHubReleaseAsset,
    GitHubReleaseSnapshot,
)
from siteops.workspace_source import (
    WORKSPACE_RELEASE_NAME,
    ArtifactIdentity,
    SourceResolutionError,
    WorkspaceReleaseAssets,
    WorkspaceReleaseEntry,
)


@pytest.fixture
def release_source(tmp_path, monkeypatch):
    def identity(name, value):
        return ArtifactIdentity(name, len(value), hashlib.sha256(value).hexdigest())

    revision = "1" * 40
    package = identity("workspace.zip", b"opaque package")
    proof = identity("workspace-proof.jsonl", b"opaque proof")
    descriptor = WorkspaceReleaseAssets(revision, (
        WorkspaceReleaseEntry("workspace", "example.storage", "1", package, proof),
    )).serialized()
    descriptor_identity = identity(WORKSPACE_RELEASE_NAME, descriptor)
    assets = tuple(
        GitHubReleaseAsset(number, item.name, item.size, item.sha256)
        for number, item in enumerate((descriptor_identity, package, proof), start=100)
    )
    release = GitHubReleaseSnapshot(
        GitHubReference("example", "content", "releases/v1"), 1, 2, revision, revision,
        True, datetime(2026, 1, 1, tzinfo=timezone.utc), False, assets,
    )
    client = Mock(spec=GitHubClient)
    client.auth = "anonymous"
    client.resolve_release.return_value = release
    staging = tmp_path / "staging"
    make_private_directory(staging)
    content = {
        WORKSPACE_RELEASE_NAME: descriptor, package.name: b"opaque package", proof.name: b"opaque proof",
    }
    calls = []
    paths = []

    @contextmanager
    def download(url, expected, *, origins, staging_parent):
        assert staging_parent == staging
        calls.append((url, expected, origins))
        path = staging / str(len(calls))
        paths.append(path)
        path.write_bytes(content[expected.name])
        try:
            yield path
        finally:
            path.unlink()

    monkeypatch.setattr(source, "download_https_asset", download)
    return client, staging, content, calls, paths


def test_descriptor_then_exact_asset_ids_without_filename_selection(release_source):
    client, staging, _, calls, paths = release_source
    with source.download_workspace_release(client, staging_parent=staging) as acquired:
        assert acquired.source.resolved.entry.workspace == "workspace"
        assert acquired.package.read_bytes() == b"opaque package"
        assert acquired.proof.read_bytes() == b"opaque proof"
        assert not paths[0].exists()
        assert acquired.source.release is client.resolve_release.return_value
        assert [url for url, _, _ in calls] == [
            f"https://api.github.com/repos/example/content/releases/assets/{identifier}"
            for identifier in (100, 101, 102)
        ]
        assert all(origins == (
            "https://api.github.com", "https://release-assets.githubusercontent.com",
            "https://objects.githubusercontent.com",
        ) for _, _, origins in calls)
        assert not hasattr(acquired, "verification")
    assert len(calls) == 3
    assert all(not path.exists() for path in paths)
    client.resolve_release.assert_called_once_with()


@pytest.mark.parametrize("workspace", [None, "workspace"])
def test_default_and_explicit_workspace_share_selection(release_source, workspace):
    client, staging, _, calls, _ = release_source
    with source.download_workspace_release(client, staging_parent=staging, workspace=workspace) as acquired:
        assert acquired.source.resolved.entry.kit_id == "example.storage"
    assert len(calls) == 3


def test_unknown_workspace_stops_before_package_download(release_source):
    client, staging, _, calls, paths = release_source
    with pytest.raises(SourceResolutionError) as caught:
        with source.download_workspace_release(client, staging_parent=staging, workspace="other"):
            pytest.fail("Unknown workspace was selected.")
    assert caught.value.code == "source.workspace-not-found"
    assert len(calls) == 1
    assert all(not path.exists() for path in paths)


def test_descriptor_identity_precedes_package_download(release_source):
    client, staging, content, calls, _ = release_source
    content[WORKSPACE_RELEASE_NAME] += b" "
    with pytest.raises(SourceResolutionError) as caught:
        with source.download_workspace_release(client, staging_parent=staging):
            pytest.fail("An altered descriptor was used.")
    assert caught.value.code == "source.identity"
    assert len(calls) == 1


def test_proof_failure_cleans_package_without_yield(release_source):
    client, staging, content, calls, paths = release_source
    del content["workspace-proof.jsonl"]
    with pytest.raises(KeyError):
        with source.download_workspace_release(client, staging_parent=staging):
            pytest.fail("An incomplete download was yielded.")
    assert len(calls) == 3
    assert all(not path.exists() for path in paths)


def test_cli_auth_is_not_silently_changed_to_anonymous(release_source):
    client, staging, _, calls, _ = release_source
    client.auth = "cli"
    with pytest.raises(SourceResolutionError) as caught:
        with source.download_workspace_release(client, staging_parent=staging):
            pytest.fail("Authentication mode was changed.")
    assert caught.value.code == "source.auth-unsupported"
    client.resolve_release.assert_not_called()
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"identifier": 999}, {"identifier": 0}, {"identifier": "100/other"},
    {"sha256": None}, {"size": 129 * 1024 * 1024},
])
def test_unobserved_or_unsupported_asset_stops_before_transfer(release_source, changes):
    client, staging, _, calls, _ = release_source
    release = client.resolve_release.return_value
    asset = replace(release.assets[0], **changes)
    if asset.identifier != 999:
        release = replace(release, assets=(asset,))
    with pytest.raises(SourceResolutionError):
        with source.download_release_asset(release, asset, staging_parent=staging):
            pytest.fail("An unsupported asset was requested.")
    assert calls == []

"""GitHub asset IDs, descriptor selection and opaque package/proof lifetimes."""

import hashlib
import io
import json
import urllib.error
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from siteops import _http_asset_worker as worker
from siteops import asset_transfer as transfer
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


def test_descriptor_then_exact_asset_names_from_github_release_downloads(release_source):
    client, staging, _, calls, paths = release_source
    with source.download_workspace_release(client, staging_parent=staging) as acquired:
        assert acquired.source.resolved.entry.workspace == "workspace"
        assert acquired.package.read_bytes() == b"opaque package"
        assert acquired.proof.read_bytes() == b"opaque proof"
        assert not paths[0].exists()
        assert acquired.source.release is client.resolve_release.return_value
        assert [url for url, _, _ in calls] == [
            f"https://github.com/example/content/releases/download/releases%2Fv1/{name}"
            for name in (WORKSPACE_RELEASE_NAME, "workspace.zip", "workspace-proof.jsonl")
        ]
        assert all(origins == (
            "https://github.com", "https://release-assets.githubusercontent.com",
            "https://objects.githubusercontent.com",
        ) for _, _, origins in calls)
        assert not any("api.github.com" in url or "api.github.com" in str(origins) for url, _, origins in calls)
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


def _snapshot(tag, *assets):
    return GitHubReleaseSnapshot(
        GitHubReference("example", "content", tag), 1, 2, "1" * 40, "1" * 40,
        True, datetime(2026, 1, 1, tzinfo=timezone.utc), False, assets,
    )


@pytest.mark.parametrize(("tag", "name", "path"), [
    ("v1.0.0", "workspace.zip", "v1.0.0/workspace.zip"),
    ("siteops/v1.0.0b7", WORKSPACE_RELEASE_NAME, "siteops%2Fv1.0.0b7/" + WORKSPACE_RELEASE_NAME),
    ("v1+build#2", "a b%.zip", "v1%2Bbuild%232/a%20b%25.zip"),
    ("release@1/\u00e9", "proof?.jsonl", "release%401%2F%C3%A9/proof%3F.jsonl"),
])
def test_release_asset_url_encodes_each_segment_under_github_origin(tag, name, path):
    asset = GitHubReleaseAsset(7, name, 10, "a" * 64)
    url = source.release_asset_url(_snapshot(tag, asset), asset)
    assert url == "https://github.com/example/content/releases/download/" + path
    assert worker.validate_request({
        "url": url, "origins": list(source._ASSET_ORIGINS), "size": 10, "sha256": "a" * 64,
    }) == {
        ("github.com", 443), ("release-assets.githubusercontent.com", 443),
        ("objects.githubusercontent.com", 443),
    }


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_segment_asset_names_stop_before_transfer(release_source, name):
    _, staging, _, calls, _ = release_source
    asset = GitHubReleaseAsset(7, name, 10, "a" * 64)
    with pytest.raises(SourceResolutionError):
        with source.download_release_asset(_snapshot("v1", asset), asset, staging_parent=staging):
            pytest.fail("A dot segment asset was requested.")
    assert calls == []


@pytest.mark.parametrize("changes", [{"sha256": "b" * 64}, {"size": 13}, {"name": "other.zip"}])
def test_asset_must_match_the_observed_listing_before_transfer(release_source, changes):
    client, staging, _, calls, _ = release_source
    release = client.resolve_release.return_value
    with pytest.raises(SourceResolutionError) as caught:
        with source.download_release_asset(release, replace(release.assets[1], **changes), staging_parent=staging):
            pytest.fail("An unobserved asset was requested.")
    assert str(caught.value) == "The selected asset must belong to the observed release."
    assert calls == []


@pytest.fixture
def in_process_worker(monkeypatch):
    """Run the transfer worker in process so its HTTP exchange can be observed."""
    exchange = SimpleNamespace(requests=[], responses=[])

    class Opener:
        def open(self, request, timeout):
            exchange.requests.append(request)
            result = exchange.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    def run(operation, request, timeout):
        try:
            result = worker.transfer(json.loads(request), str(operation / "asset.bin"))
        except worker.TransferFailure as error:
            return 1, json.dumps(error.result).encode()
        return 0, json.dumps(result).encode()

    monkeypatch.setattr(worker.urllib.request, "build_opener", lambda *_handlers: Opener())
    monkeypatch.setattr(transfer, "_run_worker", run)
    return exchange


class _Body(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url
        self.status = 200
        self.headers = Message()

    def geturl(self):
        return self.url


def _redirect(url, location):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError(url, 302, "Found", headers, io.BytesIO())


def test_github_download_follows_approved_redirect_without_credentials(
    release_source, in_process_worker, monkeypatch,
):
    monkeypatch.setenv("GH_TOKEN", "test-source-token")
    monkeypatch.setenv("GITHUB_TOKEN", "test-source-token")
    client, staging, content, _, _ = release_source
    monkeypatch.setattr(source, "download_https_asset", transfer.download_https_asset)
    release = client.resolve_release.return_value
    package = release.assets[1]
    signed = "https://release-assets.githubusercontent.com/github-production-release-asset/1?sig=opaque"
    url = "https://github.com/example/content/releases/download/releases%2Fv1/workspace.zip"
    in_process_worker.responses[:] = [_redirect(url, signed), _Body(content[package.name], signed)]
    with source.download_release_asset(release, package, staging_parent=staging) as path:
        assert path.read_bytes() == content[package.name]
    assert [request.full_url for request in in_process_worker.requests] == [url, signed]
    for request in in_process_worker.requests:
        assert {key.lower() for key, _ in request.header_items()} == {"accept", "accept-encoding", "user-agent"}


@pytest.mark.parametrize("location", [
    "https://api.github.com/repos/example/content/releases/assets/101",
    "https://evil.example/workspace.zip",
])
def test_github_download_rejects_redirects_outside_asset_origins(
    release_source, in_process_worker, monkeypatch, location,
):
    client, staging, _, _, _ = release_source
    monkeypatch.setattr(source, "download_https_asset", transfer.download_https_asset)
    release = client.resolve_release.return_value
    url = source.release_asset_url(release, release.assets[1])
    in_process_worker.responses[:] = [_redirect(url, location)]
    with pytest.raises(transfer.AssetTransferError) as caught:
        with source.download_release_asset(release, release.assets[1], staging_parent=staging):
            pytest.fail("A redirect outside the asset origins was followed.")
    assert caught.value.code == "source.redirect"
    assert len(in_process_worker.requests) == 1


@pytest.mark.parametrize("body", [b"opaque packagX", b"opaque package!", b"opaque packag"])
def test_github_download_rejects_bytes_that_differ_from_the_listing_digest(
    release_source, in_process_worker, monkeypatch, body,
):
    client, staging, _, _, _ = release_source
    monkeypatch.setattr(source, "download_https_asset", transfer.download_https_asset)
    release = client.resolve_release.return_value
    url = source.release_asset_url(release, release.assets[1])
    in_process_worker.responses[:] = [_Body(body, url)]
    with pytest.raises(transfer.AssetTransferError) as caught:
        with source.download_release_asset(release, release.assets[1], staging_parent=staging):
            pytest.fail("Bytes with a different digest were yielded.")
    assert caught.value.code == "source.identity"
    assert list(staging.iterdir()) == []

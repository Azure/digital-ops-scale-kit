"""Cold, cached, refreshed and offline source inspection through the shared CLI."""

import base64
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from siteops import cli, github_source, source_metadata_cache
from siteops.browse import BrowseError
from siteops.browse_output import render_browse_plain
from siteops.github_catalog import inspect_github
from siteops.github_metadata_cache import REFERENCE_MAX_AGE, CachedGitHubClient
from siteops.github_source import GitHubClient, GitHubReference
from siteops.source_metadata_cache import SourceMetadataCache
from tests.github_catalog_helpers import PREFIX, REVISION, Client, publish


@pytest.fixture
def remote(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parent / "fixtures" / "browse-workspace", workspace)
    published = Client()
    publish(workspace, published)
    state = SimpleNamespace(
        now=datetime(2026, 9, 15, 20, tzinfo=timezone.utc),
        revision=REVISION, routes=[], published=published, workspace=workspace, fail=None,
    )

    def request(route):
        state.routes.append(route)
        if state.fail is not None:
            raise state.fail
        if route == "/repos/example/kit":
            return {"default_branch": "main", "full_name": "example/kit"}
        if "/commits/" in route:
            return {"sha": state.revision}
        if "/git/trees/" in route:
            return {
                "sha": "c" * 40, "truncated": False,
                "tree": [
                    {"path": row.path, "sha": row.sha, "type": row.type, "mode": row.mode, "size": row.size}
                    for row in published.tree.values()
                ],
            }
        assert "/git/blobs/" in route
        sha = route.rsplit("/", 1)[1]
        content = published.blobs[sha]
        return {
            "sha": sha, "encoding": "base64", "size": len(content),
            "content": base64.b64encode(content).decode("ascii"),
        }

    state.request = request
    state.cache = SourceMetadataCache(tmp_path / "cache", clock=lambda: state.now)
    return state


def client(remote, *, ref=None, auth="anonymous", refresh=False, offline=False):
    return CachedGitHubClient(
        GitHubClient(GitHubReference("example", "kit", ref), auth=auth, transport=remote.request),
        remote.cache, refresh=refresh, offline=offline,
    )


def inspect(remote, **kwargs):
    return inspect_github("github:example/kit", client=client(remote, **kwargs))


def test_cold_reads_and_repeat_inspection_are_independent_of_entry_count(remote):
    first = inspect(remote)
    assert first.status == "complete"
    assert len(remote.routes) == 5
    assert first.source.observation.origin == "source"
    second = inspect(remote)
    assert second.entries == first.entries
    assert len(remote.routes) == 5
    assert second.source.observation.origin == "cache"
    assert second.source.observation.observed_at == first.source.observation.observed_at
    assert second.document()["source"]["verification"] == "not-performed"
    assert "Refresh after:" in render_browse_plain(second)


def test_pin_from_a_displayed_revision_uses_no_source_requests(remote):
    original = inspect(remote)
    remote.routes.clear()
    remote.fail = AssertionError("Pinned lookup contacted the source.")
    pinned = inspect(remote, ref=original.source.revision)
    assert pinned.status == "complete"
    assert pinned.source.observation.refresh_after is None
    assert pinned.source.observation.origin == "cache"
    assert remote.routes == []
    assert pinned.entries == original.entries


def test_complete_commit_miss_is_verified_and_cannot_resolve_to_a_different_commit(remote):
    remote.revision = "b" * 40
    result = inspect(remote, ref=REVISION)
    assert result.status == "invalid"
    assert result.diagnostics[0].code == "github.integrity"
    assert len(remote.routes) == 1
    remote.revision = REVISION
    assert inspect(remote, ref=REVISION.upper()).status == "complete"


def test_base_client_enforces_complete_commit_identity_but_keeps_named_refs(remote):
    remote.revision = "b" * 40
    pinned = GitHubClient(GitHubReference("example", "kit", REVISION), transport=remote.request)
    with pytest.raises(BrowseError) as caught:
        pinned.resolve_commit()
    assert caught.value.diagnostic.code == "github.integrity"
    named = GitHubClient(GitHubReference("example", "kit", "release"), transport=remote.request)
    assert named.resolve_commit() == "b" * 40


def test_refresh_observes_the_reference_without_redownloading_unchanged_index_blobs(remote):
    inspect(remote)
    remote.routes.clear()
    remote.now += timedelta(seconds=10)
    refreshed = inspect(remote, refresh=True)
    assert refreshed.status == "complete"
    assert remote.routes == ["/repos/example/kit", "/repos/example/kit/commits/main"]
    assert refreshed.source.observation.origin == "source"
    assert refreshed.source.observation.observed_at == remote.now


def test_expired_reference_refreshes_by_default_and_explicit_offline_keeps_its_age(remote):
    first = inspect(remote)
    remote.routes.clear()
    remote.now += timedelta(seconds=REFERENCE_MAX_AGE)
    offline = inspect(remote, offline=True)
    assert offline.entries == first.entries and remote.routes == []
    observation = offline.document()["source"]["observation"]
    assert observation["offline"] is True and observation["stale"] is True
    assert "refresh overdue" in render_browse_plain(offline)
    refreshed = inspect(remote)
    assert refreshed.status == "complete" and not refreshed.source.observation.stale
    assert len(remote.routes) == 2


def test_changed_source_is_validated_again_and_never_silently_uses_the_old_snapshot(remote):
    first = inspect(remote)
    remote.revision = "b" * 40
    remote.published.put(PREFIX + "manifests/storage/manifest.yaml", b"changed source header")
    changed = inspect(remote, refresh=True)
    assert changed.status == "invalid"
    assert changed.diagnostics[0].code == "index.stale"
    assert changed.source.revision == "b" * 40
    assert not changed.entries
    remote.routes.clear()
    old_pin = inspect(remote, ref=first.source.revision, offline=True)
    assert old_pin.status == "complete" and remote.routes == []


def test_rate_limit_failure_preserves_cache_but_requires_explicit_offline_selection(remote):
    first = inspect(remote)
    remote.now += timedelta(seconds=REFERENCE_MAX_AGE)
    remote.fail = BrowseError("github.rate-limit", "The source rate limit was reached.")
    failed = inspect(remote)
    assert failed.status == "invalid" and not failed.entries
    assert failed.diagnostics[0].code == "github.rate-limit"
    assert "--offline" in failed.diagnostics[0].summary
    remote.routes.clear()
    offline = inspect(remote, offline=True)
    assert offline.entries == first.entries and offline.source.observation.stale
    assert remote.routes == []


def test_authentication_modes_do_not_share_retained_private_observations(remote):
    private = inspect(remote, auth="cli")
    assert private.status == "complete"
    remote.routes.clear()
    anonymous = inspect(remote, offline=True)
    assert anonymous.status == "invalid"
    assert anonymous.diagnostics[0].code == "cache.metadata-missing"
    assert remote.routes == []
    retained = inspect(remote, auth="cli", offline=True)
    assert retained.entries == private.entries and remote.routes == []


def test_reference_default_is_distinct_from_a_branch_named_null(remote):
    assert inspect(remote).status == "complete"
    remote.routes.clear()
    missing = inspect(remote, ref="null", offline=True)
    assert missing.status == "invalid"
    assert missing.diagnostics[0].code == "cache.metadata-missing"
    assert remote.routes == []


def test_offline_missing_tree_does_not_request_it(remote):
    cached = client(remote)
    assert cached.resolve_commit() == REVISION
    remote.routes.clear()
    result = inspect(remote, offline=True)
    assert result.status == "invalid"
    assert result.source.revision == REVISION
    assert result.diagnostics[0].code == "cache.metadata-missing"
    assert remote.routes == []


def test_cached_blob_is_checked_against_its_git_identity_not_only_record_checksum(remote):
    assert inspect(remote).status == "complete"
    records = list((remote.cache.root / "metadata" / "records").glob("*.json"))
    path = next(path for path in records if json.loads(path.read_bytes())["key"]["kind"] == "blob")
    row = json.loads(path.read_bytes())
    content = b"x" * row["payload"]["size"]
    row["payload"]["base64"] = base64.b64encode(content).decode("ascii")
    row["payload"]["sha256"] = hashlib.sha256(content).hexdigest()
    path.write_text(json.dumps(row), encoding="utf-8")
    remote.routes.clear()
    result = inspect(remote, offline=True)
    assert result.status == "invalid" and not result.entries
    assert result.diagnostics[0].code == "github.integrity"
    assert remote.routes == []


def test_cached_tree_still_passes_the_regular_path_and_mode_checks(remote):
    assert inspect(remote).status == "complete"
    path = next(
        path for path in (remote.cache.root / "metadata" / "records").glob("*.json")
        if json.loads(path.read_bytes())["key"]["kind"] == "tree"
    )
    row = json.loads(path.read_bytes())
    entries = json.loads(base64.b64decode(row["payload"]["base64"]))
    entries[0]["path"] = "../outside"
    content = json.dumps(entries).encode()
    row["payload"] = {
        "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        "base64": base64.b64encode(content).decode(),
    }
    path.write_text(json.dumps(row), encoding="utf-8")
    result = inspect(remote, offline=True)
    assert result.status == "invalid"
    assert result.diagnostics[0].code == "github.invalid-data"


def test_cached_blob_still_honors_a_smaller_caller_limit(remote):
    cached = client(remote)
    tree = cached.get_tree(REVISION)
    entry = next(value for value in tree.values() if value.size and value.type == "blob")
    cached.read_blob(entry, max_bytes=entry.size)
    remote.routes.clear()
    with pytest.raises(BrowseError) as caught:
        cached.read_blob(entry, max_bytes=entry.size - 1)
    assert caught.value.diagnostic.code == "github.blob-limit"
    assert remote.routes == []


def test_unicode_tree_cache_keeps_the_source_utf8_byte_budget(remote, monkeypatch):
    for number in range(100):
        remote.published.put("documents/" + "\u4e2d" * 80 + str(number) + ".md", b"")
    expected = json.dumps([
        {"path": row.path, "sha": row.sha, "type": row.type, "mode": row.mode, "size": row.size}
        for row in remote.published.tree.values()
    ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    monkeypatch.setattr(source_metadata_cache, "MAX_METADATA_BYTES", len(expected))
    cached = client(remote)
    tree = cached.get_tree(REVISION)
    assert len(tree) == len(remote.published.tree)
    remote.routes.clear()
    assert cached.get_tree(REVISION) == tree
    assert remote.routes == []


def test_actual_cli_cache_modes_are_parseable_and_do_not_load_sites(remote, monkeypatch, capsys):
    native = github_source.GitHubClient
    monkeypatch.setattr(github_source, "GitHubClient", lambda reference, auth: native(
        reference, auth=auth, transport=remote.request,
    ))
    monkeypatch.setattr(source_metadata_cache, "SourceMetadataCache", lambda: remote.cache)
    monkeypatch.setattr(cli, "Orchestrator", Mock(side_effect=AssertionError("Loaded Sites.")))
    monkeypatch.setattr(cli, "_auto_discover_workspace", Mock(side_effect=AssertionError("Local discovery.")))
    for options, expected_reads in (([], 5), ([], 5), (["--offline"], 5), (["--refresh"], 7)):
        monkeypatch.setattr(sys, "argv", [
            "siteops", "browse", "storage", "--source", "github:example/kit", "--output", "json", *options,
        ])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        output = capsys.readouterr()
        assert stopped.value.code == 0 and not output.err
        document = json.loads(output.out)
        assert document["entries"][0]["name"] == "storage"
        assert document["source"]["observation"]["offline"] == ("--offline" in options)
        assert len(remote.routes) == expected_reads
    cli.Orchestrator.assert_not_called()


@pytest.mark.parametrize("options", [["--offline"], ["--refresh"], ["--offline", "--refresh"]])
def test_cli_cache_options_require_a_source_and_are_mutually_exclusive(
    tmp_path, monkeypatch, capsys, options,
):
    root = tmp_path / "cache"
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(root))
    monkeypatch.setattr(sys, "argv", ["siteops", "browse", *options])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code in {1, 2}
    assert not root.exists()
    assert capsys.readouterr().err


def test_cli_redaction_precedes_cache_creation(tmp_path, monkeypatch, capsys):
    root = tmp_path / "cache"
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(root))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(sys, "argv", ["siteops", "browse", "--source", "github:private/kit", "--offline"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1 and not output.out
    assert "private/kit" not in output.err and not root.exists()

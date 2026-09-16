"""Protected observations, exact keys, explicit freshness and atomic publication."""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import CacheError, check_private_node, make_private_directory
from siteops.source_metadata_cache import MAX_METADATA_BYTES, MetadataKey, SourceMetadataCache
from siteops.workspace_cache import WorkspaceCache

NOW = datetime(2026, 9, 15, 20, tzinfo=timezone.utc)
KEY = MetadataKey("registry/v1", "anonymous:example", "reference", "release")


@pytest.fixture
def cache(tmp_path):
    state = {"now": NOW}
    value = SourceMetadataCache(tmp_path / "cache", lock_timeout=0, clock=lambda: state["now"])
    return value, state


def record_path(cache, key=KEY):
    return cache.root / "metadata" / "records" / f"{key.digest}.json"


def test_mutable_reference_is_reused_until_its_refresh_boundary(cache):
    store, state = cache
    fetch = Mock(side_effect=[b"first", b"second"])
    first = store.get_or_fetch(KEY, fetch, maximum_age=300)
    assert first.payload == b"first" and not first.reused
    state["now"] += timedelta(seconds=299)
    second = store.get_or_fetch(KEY, fetch, maximum_age=300)
    assert second.payload == first.payload and second.reused and not second.stale
    assert second.observed_at == first.observed_at
    state["now"] += timedelta(seconds=1)
    third = store.get_or_fetch(KEY, fetch, maximum_age=300)
    assert third.payload == b"second" and not third.reused
    assert fetch.call_count == 2


def test_offline_reuse_keeps_original_observation_and_reports_expiry(cache):
    store, state = cache
    first = store.get_or_fetch(KEY, lambda: b"first", maximum_age=300)
    before = record_path(store).read_bytes()
    state["now"] += timedelta(days=1)
    fetch = Mock(side_effect=AssertionError("Offline read made a source request."))
    result = store.get_or_fetch(KEY, fetch, maximum_age=300, offline=True)
    assert result.payload == b"first" and result.reused and result.stale
    assert result.observed_at == first.observed_at
    assert record_path(store).read_bytes() == before
    fetch.assert_not_called()


def test_offline_miss_does_not_fetch_or_create_metadata_namespace(cache):
    store, _ = cache
    fetch = Mock()
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, fetch, offline=True)
    assert caught.value.code == "cache.metadata-missing"
    fetch.assert_not_called()
    assert not (store.root / "metadata").exists()


def test_refresh_is_explicit_and_immutable_records_keep_their_identity(cache):
    store, state = cache
    fetch = Mock(side_effect=[b"fixed", b"fixed", b"changed"])
    first = store.get_or_fetch(KEY, fetch)
    state["now"] += timedelta(days=100)
    assert store.get_or_fetch(KEY, fetch).payload == b"fixed"
    assert fetch.call_count == 1
    refreshed = store.get_or_fetch(KEY, fetch, refresh=True)
    assert not refreshed.reused and refreshed.observed_at > first.observed_at
    before = record_path(store).read_bytes()
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, fetch, refresh=True)
    assert caught.value.code == "cache.metadata-identity"
    assert record_path(store).read_bytes() == before


def test_remember_binds_observation_to_a_second_immutable_key_without_redating(cache):
    store, state = cache
    reference = store.get_or_fetch(KEY, lambda: b"revision", maximum_age=300)
    commit = replace(KEY, kind="commit", identity="revision")
    state["now"] += timedelta(seconds=100)
    store.remember(commit, reference)
    observed = store.get_or_fetch(commit, Mock(side_effect=AssertionError("Pinned fetch.")), offline=True)
    assert observed.observed_at == NOW
    assert observed.payload == b"revision"
    store.remember(commit, reference)
    with pytest.raises(CacheError, match="immutable identity"):
        store.remember(commit, replace(reference, payload=b"other"))


def test_fetch_failure_is_preserved_and_never_silently_returns_stale_data(cache):
    store, state = cache
    store.get_or_fetch(KEY, lambda: b"old", maximum_age=300)
    before = record_path(store).read_bytes()
    state["now"] += timedelta(seconds=300)
    error = OSError("source callback failure")
    with pytest.raises(OSError) as caught:
        store.get_or_fetch(KEY, Mock(side_effect=error), maximum_age=300)
    assert caught.value is error
    assert record_path(store).read_bytes() == before
    assert store.get_or_fetch(KEY, Mock(), maximum_age=300, offline=True).stale


def test_clock_is_observed_after_waiting_for_another_writer(cache, monkeypatch):
    store, state = cache
    state["now"] += timedelta(seconds=30)
    store.get_or_fetch(KEY, lambda: b"winner", maximum_age=300)
    state["now"] = NOW
    lock = store._locked_record

    @contextmanager
    def wait(key):
        with lock(key) as path:
            state["now"] += timedelta(seconds=30)
            yield path

    monkeypatch.setattr(store, "_locked_record", wait)
    assert store.get_or_fetch(KEY, Mock(), maximum_age=300).payload == b"winner"


def test_future_timestamp_does_not_claim_freshness(cache):
    store, state = cache
    store.get_or_fetch(KEY, lambda: b"value")
    state["now"] -= timedelta(seconds=1)
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, Mock(), offline=True)
    assert caught.value.code == "cache.metadata-time"


@pytest.mark.parametrize("field,value", [
    ("provider", "other/v1"), ("scope", "cli:example"),
    ("kind", "commit"), ("identity", "different/revision"),
])
def test_provider_scope_kind_and_identity_are_separate_keys(cache, field, value):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"original")
    other = replace(KEY, **{field: value})
    with pytest.raises(CacheError, match="not cached"):
        store.get_or_fetch(other, Mock(), offline=True)
    assert len(KEY.digest) == 64 and KEY.digest != other.digest


@pytest.mark.parametrize("value", ["", "line\nbreak", "control\x00", "x" * 4097])
def test_invalid_keys_never_become_paths(value):
    with pytest.raises(CacheError):
        replace(KEY, identity=value)
    assert "/" not in replace(KEY, identity="../../elsewhere").digest


@pytest.mark.parametrize("change", [
    lambda row: row.update(kind="Other"),
    lambda row: row["key"].update(scope="other"),
    lambda row: row.update(mutable=True),
    lambda row: row.update(observedAt="not a timestamp"),
    lambda row: row["payload"].update(size=True),
    lambda row: row["payload"].update(sha256="0" * 64),
    lambda row: row["payload"].update(base64="eA== "),
    lambda row: row.update(private="PRIVATE_VALUE"),
])
def test_invalid_cached_record_fails_before_fetch_even_on_refresh(cache, change):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"x")
    path = record_path(store)
    row = json.loads(path.read_bytes())
    change(row)
    path.write_text(json.dumps(row), encoding="utf-8")
    fetch = Mock()
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, fetch, refresh=True)
    assert caught.value.code == "cache.metadata-corrupt"
    assert "PRIVATE_VALUE" not in str(caught.value)
    fetch.assert_not_called()


def test_duplicate_json_keys_fail_before_fetch(cache):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"x")
    record_path(store).write_bytes(b'{"kind":"SourceMetadataRecord","kind":"PRIVATE_VALUE"}')
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, Mock(), refresh=True)
    assert caught.value.code == "cache.metadata-corrupt"
    assert "PRIVATE_VALUE" not in str(caught.value)


def test_payload_limit_and_empty_payload_control(cache):
    store, _ = cache
    with pytest.raises(CacheError, match="byte limit"):
        store.get_or_fetch(KEY, lambda: b"x" * (MAX_METADATA_BYTES + 1))
    assert not record_path(store).exists()
    assert store.get_or_fetch(KEY, lambda: b"").payload == b""


def test_namespace_and_record_are_private_and_share_the_existing_root(cache):
    store, _ = cache
    marker = (store.root / "cache.json").read_bytes()
    store.get_or_fetch(KEY, lambda: b"value")
    for path in (store.root / "metadata", store.root / "metadata" / "records"):
        check_private_node(path, directory=True)
    check_private_node(record_path(store), directory=False)
    assert WorkspaceCache(store.root).root == store.root
    assert (store.root / "cache.json").read_bytes() == marker
    assert not (store.root / "proofs").exists()


def test_invalid_existing_namespace_is_not_overwritten(cache):
    store, _ = cache
    target = store.root / "metadata"
    make_private_directory(target)
    sentinel = target / "operator.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(CacheError):
        store.get_or_fetch(KEY, Mock())
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode checks")
def test_shared_record_permissions_are_not_repaired(cache):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"value")
    path = record_path(store)
    path.chmod(0o644)
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, Mock())
    assert caught.value.code == "cache.permissions"
    assert path.stat().st_mode & 0o077


def test_failed_record_replacement_preserves_old_bytes_and_removes_temporary_file(cache, monkeypatch):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"old", maximum_age=300)
    before = record_path(store).read_bytes()

    def fail(*args):
        raise OSError("replace failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(CacheError) as caught:
        store.get_or_fetch(KEY, lambda: b"new", maximum_age=300, refresh=True)
    assert caught.value.code == "cache.io"
    assert record_path(store).read_bytes() == before
    assert list(record_path(store).parent.iterdir()) == [record_path(store)]


@pytest.mark.parametrize("options", [
    {"refresh": True, "offline": True}, {"maximum_age": -1},
    {"maximum_age": True}, {"maximum_age": float("nan")}, {"maximum_age": 86401},
])
def test_invalid_read_policy_creates_no_observation(cache, options):
    store, _ = cache
    fetch = Mock()
    with pytest.raises(CacheError):
        store.get_or_fetch(KEY, fetch, **options)
    assert not (store.root / "metadata").exists()
    fetch.assert_not_called()


def test_reparse_record_is_rejected_before_read(cache, tmp_path):
    store, _ = cache
    store.get_or_fetch(KEY, lambda: b"value")
    path = record_path(store)
    other = tmp_path / "other.json"
    path.rename(other)
    try:
        path.symlink_to(other)
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    with pytest.raises(ArtifactError):
        store.get_or_fetch(KEY, Mock())

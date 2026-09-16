"""Bounded cache inventory, selected removal and protected active-use lifetimes."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from siteops import cache_management as management
from siteops import cli
from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import CacheError, cache_lock, make_private_directory
from siteops.cache_layout import CacheLayout
from siteops.cache_management import CacheManagement
from siteops.source_metadata_cache import MetadataKey, SourceMetadataCache
from siteops.workspace_acquisition import WorkspaceAcquisition
from siteops.workspace_cache import WorkspaceCache
from tests.workspace_acquisition_helpers import make_source


@pytest.fixture
def stored(tmp_path):
    fixture = make_source(tmp_path)
    cache = fixture.cache
    cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    acquisition = WorkspaceAcquisition(cache, verify=fixture.verifier)
    acquisition.publish(fixture.source, fixture.archive)
    key = MetadataKey("registry/v1", "private-context", "reference", "current")
    metadata = SourceMetadataCache(cache.root)
    metadata.get_or_fetch(key, lambda: b"opaque metadata", maximum_age=300)
    return SimpleNamespace(
        fixture=fixture, cache=cache, acquisition=acquisition, metadata=metadata, key=key,
        manager=CacheManagement(cache.root),
        ids={"package": fixture.source.entry.package.sha256, "proof": fixture.source.entry.proof.sha256,
             "metadata": key.digest},
    )


def path_for(state, kind):
    root = state.cache.root
    if kind == "package":
        return root / "objects" / "sha256" / state.ids[kind]
    if kind == "proof":
        return root / "proofs" / "sha256" / state.ids[kind]
    return root / "metadata" / "records" / f"{state.ids[kind]}.json"


def test_empty_inspection_does_not_initialize_a_cache(tmp_path):
    root = tmp_path / "absent" / "cache"
    result = CacheManagement(root).list()
    assert not result.initialized and not result.entries
    assert not root.parent.exists()
    with pytest.raises(CacheError) as caught:
        CacheManagement(root).remove("package", "a" * 64)
    assert caught.value.code == "cache.entry-missing"
    assert not root.parent.exists()


def test_open_existing_mode_preserves_marker_and_does_not_create_missing_root(tmp_path):
    root = tmp_path / "cache"
    with pytest.raises(CacheError) as caught:
        CacheLayout(root, create=False)
    assert caught.value.code == "cache.uninitialized" and not root.exists()
    cache = WorkspaceCache(root)
    marker = (root / "cache.json").read_bytes()
    assert CacheLayout(root, create=False).root == cache.root
    assert (root / "cache.json").read_bytes() == marker


def test_inventory_reports_storage_without_reading_payloads_or_verifying(stored, monkeypatch):
    calls = len(stored.fixture.verifier.calls)
    monkeypatch.setattr("socket.socket", Mock(side_effect=AssertionError("Source access.")))
    result = stored.manager.list()
    assert result.matched == 3 and len(result.entries) == 3
    assert {entry.kind for entry in result.entries} == set(stored.ids)
    assert all(entry.state == "stored" and entry.stored_bytes > 0 for entry in result.entries)
    assert all(entry.document()["verification"] == "not-performed" for entry in result.entries)
    assert len(stored.fixture.verifier.calls) == calls
    assert "private-context" not in json.dumps(result.document())
    for entry in result.entries:
        paths = [path_for(stored, entry.kind)]
        if entry.kind == "package":
            paths.append(stored.cache.root / "receipts" / entry.identity)
        expected = sum(
            path.stat().st_size
            for root in paths for path in ([root] if root.is_file() else root.rglob("*"))
            if path.is_file()
        )
        assert entry.stored_bytes == expected


def test_inventory_limits_and_filters_are_explicit(stored):
    result = stored.manager.list(limit=1)
    assert len(result.entries) == 1 and result.document()["hasMore"]
    assert result.matched == 3
    one = stored.manager.list(kind="metadata", identity=stored.ids["metadata"])
    assert one.matched == 1 and one.entries[0].identity == stored.ids["metadata"]
    missing = stored.manager.list(kind="metadata", identity="0" * 64)
    assert missing.entries[0].state == "missing"


@pytest.mark.parametrize("kind", ["package", "proof", "metadata"])
def test_remove_only_selected_entry_and_preserve_configuration(stored, tmp_path, kind):
    pin = tmp_path / "siteops.pin"
    site = tmp_path / "site.yaml"
    trust = tmp_path / "policy.json"
    for path in (pin, site, trust):
        path.write_bytes(b"operator input")
    metadata_before = (stored.cache.root / "cache.json").read_bytes()
    result = stored.manager.remove(kind, stored.ids[kind])
    assert result.state == "removed" and result.stored_bytes > 0
    assert not path_for(stored, kind).exists()
    for other in set(stored.ids) - {kind}:
        assert path_for(stored, other).exists()
    receipts = stored.cache.root / "receipts" / stored.ids["package"]
    assert receipts.exists() is (kind != "package")
    for path in (pin, site, trust):
        assert path.read_bytes() == b"operator input"
    assert (stored.cache.root / "cache.json").read_bytes() == metadata_before
    assert (stored.cache.root / "locks").is_dir()
    with pytest.raises(CacheError) as caught:
        stored.manager.remove(kind, stored.ids[kind])
    assert caught.value.code == "cache.entry-missing"


def test_corrupt_package_can_be_removed_then_reacquired_exactly(stored):
    package = path_for(stored, "package")
    (package / "package.zip").write_bytes(b"corrupt")
    with pytest.raises(ArtifactError):
        with stored.acquisition.lease(stored.fixture.source):
            pytest.fail("Corrupt content was accepted.")
    stored.manager.remove("package", stored.ids["package"])
    stored.acquisition.publish(stored.fixture.source, stored.fixture.archive)
    with stored.acquisition.lease(stored.fixture.source) as content:
        assert content.bind("storage").manifest_path.name == "storage.yaml"


def test_corrupt_metadata_can_be_removed_without_parsing_or_repairing_it(stored):
    path_for(stored, "metadata").write_bytes(b"not JSON")
    with pytest.raises(CacheError):
        stored.metadata.get_or_fetch(stored.key, Mock(), maximum_age=300)
    stored.manager.remove("metadata", stored.ids["metadata"])
    assert stored.metadata.get_or_fetch(stored.key, lambda: b"fresh", maximum_age=300).payload == b"fresh"


def test_incomplete_package_and_orphan_receipts_remain_targeted_entries(stored):
    package = path_for(stored, "package")
    (package / "package.zip").unlink()
    assert stored.manager.list(kind="package").entries[0].state == "incomplete"
    stored.manager.remove("package", stored.ids["package"])
    receipt = stored.cache.root / "receipts" / stored.ids["package"]
    make_private_directory(receipt)
    marker = receipt / "old.json"
    marker.write_bytes(b"old evaluation")
    marker.chmod(0o600)
    assert stored.manager.list(kind="package").entries[0].state == "incomplete"
    stored.manager.remove("package", stored.ids["package"])
    assert not receipt.exists()


@pytest.mark.parametrize("kind", ["package", "proof", "metadata"])
def test_active_entry_is_busy_and_cannot_be_removed(stored, kind):
    name = stored.ids[kind] if kind == "package" else f"{kind}-{stored.ids[kind]}"
    with cache_lock(stored.cache.root / "locks" / f"{name}.lock", exclusive=False, timeout=0):
        result = stored.manager.list(kind=kind, identity=stored.ids[kind]).entries[0]
        assert result.state == "busy" and result.stored_bytes is None
        with pytest.raises(CacheError) as caught:
            stored.manager.remove(kind, stored.ids[kind])
        assert caught.value.code == "cache.busy"
    assert path_for(stored, kind).exists()


def test_actual_package_and_proof_leases_block_removal(stored):
    with stored.acquisition.lease(stored.fixture.source):
        with pytest.raises(CacheError, match="in use"):
            stored.manager.remove("package", stored.ids["package"])
    with stored.cache.lease_proof(stored.fixture.source.entry.proof):
        with pytest.raises(CacheError, match="in use"):
            stored.manager.remove("proof", stored.ids["proof"])


def test_metadata_fetch_lock_blocks_removal(stored):
    def fetch():
        with pytest.raises(CacheError, match="in use"):
            stored.manager.remove("metadata", stored.ids["metadata"])
        return b"new"
    stored.metadata.get_or_fetch(stored.key, fetch, maximum_age=300, refresh=True)


@pytest.mark.parametrize("identity", ["", "../outside", "A" * 64, "a" * 63, "a" * 65])
def test_only_complete_canonical_ids_are_accepted(stored, identity):
    with pytest.raises(CacheError):
        stored.manager.remove("package", identity)
    assert path_for(stored, "package").exists()


def test_unmarked_operator_directory_is_untouched(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    sentinel = root / "site.yaml"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(ArtifactError):
        CacheManagement(root).remove("package", "a" * 64)
    assert sentinel.read_bytes() == b"preserve"


def test_links_inside_entry_are_rejected_before_any_removal(stored, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_bytes(b"outside")
    link = path_for(stored, "package") / "content" / "link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    result = stored.manager.list(kind="package").entries[0]
    assert result.state == "unavailable"
    with pytest.raises(ArtifactError):
        stored.manager.remove("package", stored.ids["package"])
    assert (path_for(stored, "package") / "package.zip").exists()
    assert sentinel.read_bytes() == b"outside"


def test_hardlinks_are_rejected_before_any_removal(stored, tmp_path):
    external = tmp_path / "external"
    external.write_bytes(b"outside")
    alias = path_for(stored, "package") / "content" / "alias"
    os.link(external, alias)
    with pytest.raises(ArtifactError):
        stored.manager.remove("package", stored.ids["package"])
    assert external.read_bytes() == b"outside"
    assert (path_for(stored, "package") / "package.zip").exists()


def test_mount_boundary_is_refused(stored, monkeypatch):
    mounted = path_for(stored, "package") / "content"
    monkeypatch.setattr(management.os.path, "ismount", lambda path: path == mounted)
    with pytest.raises(CacheError) as caught:
        stored.manager.remove("package", stored.ids["package"])
    assert caught.value.code == "cache.unsafe-entry"
    assert (path_for(stored, "package") / "package.zip").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_shared_node_permissions_are_not_repaired(stored):
    path = path_for(stored, "proof") / "proof.bin"
    path.chmod(0o644)
    with pytest.raises(CacheError) as caught:
        stored.manager.remove("proof", stored.ids["proof"])
    assert caught.value.code == "cache.permissions"
    assert path.exists() and path.stat().st_mode & 0o077


def test_node_limit_stops_before_deleting_anything(stored, monkeypatch):
    monkeypatch.setattr(management, "_MAX_ENTRY_NODES", 2)
    with pytest.raises(CacheError) as caught:
        stored.manager.remove("package", stored.ids["package"])
    assert caught.value.code == "cache.scan-limit"
    assert (path_for(stored, "package") / "package.zip").exists()


def test_partial_filesystem_failure_is_explicit_and_targeted_retry_works(stored, monkeypatch):
    original = Path.unlink
    proof = path_for(stored, "proof") / "proof.bin"

    def fail(path, *args, **kwargs):
        if path == proof:
            raise PermissionError("private failure text")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail)
        with pytest.raises(CacheError) as caught:
            stored.manager.remove("proof", stored.ids["proof"])
        assert caught.value.code == "cache.remove-incomplete"
        assert "private failure text" not in str(caught.value)
    stored.manager.remove("proof", stored.ids["proof"])
    assert not proof.parent.exists()


def invoke(monkeypatch, capsys, arguments):
    monkeypatch.setattr(sys, "argv", ["siteops", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    return stopped.value.code, capsys.readouterr()


def test_cli_inventory_and_removal_are_private_parseable_and_source_free(stored, monkeypatch, capsys):
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(stored.cache.root))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.setattr("socket.socket", Mock(side_effect=AssertionError("Source request.")))
    monkeypatch.setattr(cli, "Orchestrator", Mock(side_effect=AssertionError("Engine creation.")))
    code, output = invoke(monkeypatch, capsys, ["cache", "list", "--output", "json"])
    assert code == 0 and not output.err
    assert json.loads(output.out)["matched"] == 3
    code, output = invoke(monkeypatch, capsys, [
        "cache", "remove", "metadata", stored.ids["metadata"], "--output", "json",
    ])
    assert code == 0 and not output.err
    assert json.loads(output.out)["entry"]["storageState"] == "removed"
    assert path_for(stored, "package").exists()


def test_cli_redaction_precedes_cache_creation(tmp_path, monkeypatch, capsys):
    root = tmp_path / "cache"
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(root))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    code, output = invoke(monkeypatch, capsys, ["cache", "list", "--output", "json"])
    assert code == 1 and not output.out
    assert not root.exists()


def test_cli_rejects_project_scoping_and_invalid_limits(stored, monkeypatch, capsys):
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(stored.cache.root))
    for args in (["--project", "factory", "cache", "list"], ["cache", "list", "--limit", "0"]):
        code, output = invoke(monkeypatch, capsys, args)
        assert code == 1 and output.err
    assert path_for(stored, "package").exists()

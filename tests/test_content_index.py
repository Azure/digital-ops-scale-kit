"""Public projection, source-private freshness evidence and index publication."""

import hashlib
import json
import os
import shutil
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from siteops import cli
from siteops.browse import BrowseError, BrowseResult, BrowseSource, ContentReader, select_entries
from siteops.browse_output import render_browse_plain
from siteops.content_index import (
    BINDINGS_NAME,
    INDEX_NAME,
    build_content_index,
    canonical_index_path,
    canonical_text,
    load_content_index,
    load_source_bindings,
    validate_source_snapshot,
    write_content_index,
)


@pytest.fixture
def workspace(tmp_path):
    source = Path(__file__).parent / "fixtures" / "browse-workspace"
    target = tmp_path / "workspace"
    shutil.copytree(source, target)
    return target


def _source(workspace, bindings):
    paths = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*") if path.is_file()
    }
    digests = {
        item.path: hashlib.sha256(canonical_text((workspace / item.path).read_bytes())).hexdigest()
        for item in bindings.inputs if (workspace / item.path).is_file()
    }
    return paths, digests


def test_public_export_requires_approval_before_inspection(workspace, monkeypatch):
    monkeypatch.setattr(ContentReader, "__init__", lambda *a: pytest.fail("Read without approval"))
    with pytest.raises(BrowseError, match="--public"):
        build_content_index(workspace)


def test_projection_is_allowlisted_not_a_private_document(workspace):
    manifest = workspace / "manifests" / "storage" / "manifest.yaml"
    body = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    body["description"] = "PRIVATE_DESCRIPTION"
    body["selector"] = "private=PRIVATE_SELECTOR"
    body["sites"] = ["PRIVATE_SITE"]
    manifest.write_text(yaml.safe_dump(body), encoding="utf-8")
    metadata = manifest.with_name("entry.yaml")
    guide = yaml.safe_load(metadata.read_text(encoding="utf-8"))
    guide["inputs"][0]["source"] = "PRIVATE_SOURCE"
    metadata.write_text(yaml.safe_dump(guide), encoding="utf-8")
    bundle = build_content_index(workspace, approve_public=True)
    text = bundle.index.decode()
    for sentinel in ("PRIVATE_DESCRIPTION", "PRIVATE_SELECTOR", "PRIVATE_SITE", "PRIVATE_SOURCE"):
        assert sentinel not in text
    assert str(workspace) not in text
    document = json.loads(text)
    assert document["projection"] == "publishable"
    assert "diagnostics" not in document
    assert "source" not in document
    assert "indexDigest" not in document
    entry = load_content_index(bundle.index)[0]
    assert not entry.targeting_known
    assert entry.guidance.inputs[0].source is None
    assert entry.guidance.category == "example"
    bindings = json.loads(bundle.bindings)
    assert bindings["projection"] == "source-private"
    assert bindings["indexDigest"] == hashlib.sha256(bundle.index).hexdigest()


def test_index_is_deterministic_and_source_neutral(workspace):
    first = build_content_index(workspace, approve_public=True)
    second = build_content_index(workspace, approve_public=True)
    assert first == second
    bindings = load_source_bindings(first.bindings, first.index)
    assert all(
        dict(item.digests).keys() == {"sha256"}
        for item in bindings.inputs if item.digests
    )
    entries = load_content_index(first.index)
    source = BrowseSource("remote", "approved-feed:sample", "immutable-version", "example-provider", "current")
    result = select_entries(
        BrowseResult("workspace", entries, discovered=len(entries), name_inventory_complete=True, source=source),
        "storage",
    )
    text = render_browse_plain(result)
    assert "approved-feed:sample" in text and "immutable-version" in text
    assert "Remote preview only" in text
    assert "siteops -w" not in text
    assert "No targets declared" not in text
    assert result.document()["source"]["verification"] == "not-performed"


def test_source_snapshot_detects_changes_and_new_candidates(workspace):
    bundle = build_content_index(workspace, approve_public=True)
    bindings = load_source_bindings(bundle.bindings, bundle.index)
    paths, digests = _source(workspace, bindings)
    validate_source_snapshot(bindings, paths, digests, algorithm="sha256")
    with pytest.raises(BrowseError, match="entries changed"):
        validate_source_snapshot(
            bindings, paths | {"samples/new/manifest.yaml"}, digests, algorithm="sha256"
        )
    changed = dict(digests)
    changed["manifests/storage/entry.yaml"] = "0" * 64
    with pytest.raises(BrowseError, match="inputs changed"):
        validate_source_snapshot(bindings, paths, changed, algorithm="sha256")
    with pytest.raises(BrowseError, match="lacks freshness"):
        validate_source_snapshot(bindings, paths, digests, algorithm="unavailable")


def test_nested_inputs_manifest_is_indexed_but_input_companion_is_not(workspace):
    companion = workspace / "manifests" / "storage" / "inputs.yaml"
    companion.write_text(
        "apiVersion: siteops.inputs/v1\nkind: SiteInputContract\ninputs: []\n",
        encoding="utf-8",
    )
    before = build_content_index(workspace, approve_public=True)
    before_bindings = load_source_bindings(before.bindings, before.index)
    paths, digests = _source(workspace, before_bindings)
    validate_source_snapshot(before_bindings, paths, digests, algorithm="sha256")
    assert "manifests/storage/inputs.yaml" not in before_bindings.candidates

    manifest = workspace / "manifests" / "network" / "inputs.yaml"
    manifest.parent.mkdir()
    manifest.write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nname: network\nsteps: []\n",
        encoding="utf-8",
    )
    paths, digests = _source(workspace, before_bindings)
    with pytest.raises(BrowseError, match="entries changed"):
        validate_source_snapshot(before_bindings, paths, digests, algorithm="sha256")

    updated = build_content_index(workspace, approve_public=True)
    updated_bindings = load_source_bindings(updated.bindings, updated.index)
    assert "manifests/network/inputs.yaml" in updated_bindings.candidates
    paths, digests = _source(workspace, updated_bindings)
    validate_source_snapshot(updated_bindings, paths, digests, algorithm="sha256")


def test_absent_guidance_and_workspace_metadata_are_bound(workspace):
    metadata = workspace / "manifests" / "storage" / "entry.yaml"
    metadata.unlink()
    bundle = build_content_index(workspace, approve_public=True)
    assert bundle.unclassified == 1 and bundle.published == 0
    bindings = load_source_bindings(bundle.bindings, bundle.index)
    paths, digests = _source(workspace, bindings)
    assert next(item for item in bindings.inputs if item.path.endswith("entry.yaml")).digests is None
    for added in ("manifests/storage/entry.yaml", "content.yaml"):
        with pytest.raises(BrowseError, match="guidance changed"):
            validate_source_snapshot(bindings, paths | {added}, digests, algorithm="sha256")


def test_bindings_cannot_be_swapped_or_extend_private_reads(workspace):
    bundle = build_content_index(workspace, approve_public=True)
    with pytest.raises(BrowseError, match="same bytes"):
        load_source_bindings(bundle.bindings, bundle.index + b" ")
    data = json.loads(bundle.bindings)
    data["inputs"].append({"path": "sites/private.yaml", "digests": {"sha256": "0" * 64}})
    with pytest.raises(BrowseError):
        load_source_bindings(json.dumps(data).encode(), bundle.index)
    data = json.loads(bundle.bindings)
    data["inputs"].pop()
    with pytest.raises(BrowseError, match="incomplete"):
        load_source_bindings(json.dumps(data).encode(), bundle.index)


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(selector="PRIVATE_SELECTOR"),
    lambda row: row.update(role="unclassified"),
    lambda row: row["guidance"].update(source="PRIVATE_SOURCE"),
    lambda row: row["guidance"]["inputs"][0].update(source="PRIVATE_SOURCE"),
    lambda row: row.update(path="../outside.yaml"),
])
def test_index_rejects_private_or_unsupported_fields(workspace, mutate):
    bundle = build_content_index(workspace, approve_public=True)
    document = json.loads(bundle.index)
    mutate(document["entries"][0])
    with pytest.raises(BrowseError) as failure:
        load_content_index(json.dumps(document).encode())
    assert "PRIVATE_" not in str(failure.value)


@pytest.mark.parametrize("path", ["/absolute", "../parent", "a/../b", "a//b", "a\\b", "C:relative", "sites/private.yaml"])
def test_index_paths_have_no_filesystem_or_provider_aliases(path):
    with pytest.raises((ValueError, BrowseError)):
        canonical_index_path(path)


def test_index_writer_refreshes_only_its_generated_files(workspace):
    bundle = build_content_index(workspace, approve_public=True)
    write_content_index(workspace, bundle)
    write_content_index(workspace, bundle, check=True)
    assert (workspace / INDEX_NAME).read_bytes() == bundle.index
    assert (workspace / BINDINGS_NAME).read_bytes() == bundle.bindings
    altered = replace(bundle, index=bundle.index + b"\n")
    with pytest.raises(BrowseError, match="rebuilt"):
        write_content_index(workspace, altered, check=True)
    (workspace / INDEX_NAME).write_text('{"kind":"OperatorData"}', encoding="utf-8")
    with pytest.raises(BrowseError, match="unrelated"):
        write_content_index(workspace, bundle)
    assert (workspace / INDEX_NAME).read_text() == '{"kind":"OperatorData"}'


def test_same_reader_snapshot_cannot_silently_change(workspace):
    reader = ContentReader(workspace)
    path = workspace / "manifests" / "storage" / "manifest.yaml"
    reader.entry(path)
    path.write_text(path.read_text().replace("storage", "other"), encoding="utf-8")
    with pytest.raises(BrowseError, match="changed"):
        reader.entry(path)


def test_index_identity_is_portable_across_lf_and_crlf(workspace):
    before = build_content_index(workspace, approve_public=True)
    for path in workspace.rglob("*.yaml"):
        path.write_bytes(canonical_text(path.read_bytes()).replace(b"\n", b"\r\n"))
    after = build_content_index(workspace, approve_public=True)
    assert before == after
    bindings = load_source_bindings(
        before.bindings.replace(b"\n", b"\r\n"),
        before.index.replace(b"\n", b"\r\n"),
    )
    assert bindings.index_digest == hashlib.sha256(before.index).hexdigest()
    with pytest.raises(BrowseError, match="uniform"):
        canonical_text(b"first\nsecond\r\n")


def test_index_cli_is_explicit_and_does_not_load_sites(workspace, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("Loaded Sites"))
    monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(workspace), "index"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert not (workspace / INDEX_NAME).exists()
    capsys.readouterr()
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(sys, "argv", [
        "siteops", "-w", str(workspace), "index", "--public", "--for-source", "github",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 0 and not output.err
    assert str(workspace) not in output.out
    assert (workspace / INDEX_NAME).is_file()
    assert (workspace / BINDINGS_NAME).is_file()


def test_cleanup_warning_does_not_mask_completed_outputs(workspace, monkeypatch, caplog):
    bundle = build_content_index(workspace, approve_public=True)
    original = Path.unlink
    attempted = []

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".siteops-index-") or path.parent.name.startswith(".siteops-index-"):
            attempted.append(path)
            raise OSError("fixture cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    write_content_index(workspace, bundle)
    assert len(attempted) >= 2
    assert (workspace / INDEX_NAME).read_bytes() == bundle.index
    assert (workspace / BINDINGS_NAME).read_bytes() == bundle.bindings
    assert "temporary" in caplog.text.lower()


def test_index_refresh_does_not_silently_remove_adapter_bindings(workspace):
    from siteops.github_catalog import github_input_digests

    bound = build_content_index(workspace, approve_public=True, additional_digests=github_input_digests)
    write_content_index(workspace, bound)
    plain = build_content_index(workspace, approve_public=True)
    with pytest.raises(BrowseError, match="--for-source"):
        write_content_index(workspace, plain)
    assert (workspace / BINDINGS_NAME).read_bytes() == bound.bindings


@pytest.mark.parametrize("umask", [0, 0o077])
def test_generated_file_creation_is_owner_only(workspace, monkeypatch, umask):
    bundle = build_content_index(workspace, approve_public=True)
    original = os.open
    requested_modes = {}

    def record(path, flags, mode=0o600, **kwargs):
        if Path(path).name in {INDEX_NAME, BINDINGS_NAME}:
            requested_modes[Path(path).name] = mode
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", record)
    previous_umask = os.umask(umask)
    try:
        write_content_index(workspace, bundle)
    finally:
        os.umask(previous_umask)
    assert requested_modes == {INDEX_NAME: 0o600, BINDINGS_NAME: 0o600}
    if os.name != "nt":
        for name in (INDEX_NAME, BINDINGS_NAME):
            assert stat.S_IMODE((workspace / name).stat().st_mode) == 0o600 & ~umask


@pytest.mark.parametrize(("existing_mode", "expected_mode"), [
    (0o7777, 0o600),
    (0o666, 0o600),
    (0o644, 0o600),
    (0o640, 0o600),
    (0o600, 0o600),
    (0o444, 0o400),
    (0o400, 0o400),
])
def test_generated_file_refresh_preserves_only_safe_modes(
    workspace, monkeypatch, existing_mode, expected_mode,
):
    bundle = build_content_index(workspace, approve_public=True)
    write_content_index(workspace, bundle)
    expected = {INDEX_NAME: expected_mode, BINDINGS_NAME: expected_mode}
    original_stat = Path.stat
    original_chmod = Path.chmod
    requested_modes = {}

    def existing_permissions(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path.parent == workspace and path.name in expected:
            return os.stat_result((stat.S_IFREG | existing_mode, *info[1:]))
        return info

    def record_chmod(path, mode, *args, **kwargs):
        if path.name in expected and path.parent.name.startswith(".siteops-index-"):
            requested_modes[path.name] = mode
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", existing_permissions)
    monkeypatch.setattr(Path, "chmod", record_chmod)
    write_content_index(workspace, bundle)
    assert requested_modes == expected


def test_generated_file_refresh_keeps_restrictive_public_permissions(workspace):
    bundle = build_content_index(workspace, approve_public=True)
    write_content_index(workspace, bundle)
    public = workspace / INDEX_NAME
    public.chmod(0o600)
    existing_mode = public.stat().st_mode & 0o777
    write_content_index(workspace, bundle)
    assert public.stat().st_mode & 0o777 == existing_mode

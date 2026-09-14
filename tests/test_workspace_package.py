"""Workspace package identities, compatibility and confined staging."""

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tracemalloc
import zipfile
import zlib
from pathlib import Path

import pytest

from siteops import package_builder
from siteops import workspace_package as package
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    hash_stream,
    open_regular_file,
    relative_artifact_path,
)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    shutil.copytree(Path(__file__).parent / "fixtures" / "browse-workspace", root / "workspace")
    (root / "LICENSE").write_text("Fixture license\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("Companion guide\n", encoding="utf-8")
    return root


def _build(snapshot, output, **overrides):
    return package_builder.build_package(snapshot, output, **{
        "workspace": "workspace", "kit_id": "example/storage", "version": "preview-7",
        "source_revision": "feed:immutable-revision-7",
        "siteops_range": ">=1.0.0b1,<2", "companions": ("LICENSE", "docs"),
        **overrides,
    })


def _document(files=None):
    files = files or {"workspace/file.txt": b"payload"}
    records = tuple(PayloadFile(name, hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items())
    return package.WorkspacePackage(
        "example", "7", "opaque-revision", "workspace", ">=1.0.0b1,<2",
        ("manifest/v1",), records, package.workspace_tree_digest(records, "workspace"),
    ).document()


def _archive(path, document=None, files=None, *, info_mutator=None, raw_metadata=None):
    files = files or {"workspace/file.txt": b"payload"}
    document = document or _document(files)
    with zipfile.ZipFile(path, "w") as archive:
        info = package_builder._zip_info(package.PACKAGE_NAME, zipfile.ZIP_STORED)
        archive.writestr(info, raw_metadata if raw_metadata is not None else package.json_bytes(document))
        for name, data in files.items():
            info = package_builder._zip_info(name, zipfile.ZIP_STORED)
            if info_mutator:
                info_mutator(info)
            archive.writestr(info, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_package_preserves_workspace_and_companion_paths(snapshot, tmp_path):
    output = tmp_path / "package.zip"
    built = _build(snapshot, output)
    inspected = package.inspect_package(output, built.sha256)
    assert built == inspected
    assert built.metadata.source_revision == "feed:immutable-revision-7"
    assert built.metadata.workspace_root == "workspace"
    assert not any("github" in key.casefold() for key in built.metadata.document())
    expected = {
        path.relative_to(snapshot).as_posix(): path.read_bytes()
        for path in snapshot.rglob("*") if path.is_file()
    }
    assert {entry.path for entry in built.metadata.files} == set(expected)
    destination = tmp_path / "materialized"
    assert package.extract_package(output, built.sha256, destination) == built
    for relative, data in expected.items():
        target = destination / relative
        assert target.read_bytes() == data
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (destination / package.PACKAGE_NAME).is_file()


def test_production_is_deterministic_across_source_timestamps(snapshot, tmp_path):
    first = _build(snapshot, tmp_path / "first.zip")
    for path in snapshot.rglob("*"):
        if path.is_file():
            os.utime(path, (1000000000, 1000000000))
    second = _build(snapshot, tmp_path / "second.zip")
    assert first == second
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert archive.namelist()[0] == package.PACKAGE_NAME


def test_tree_identity_uses_raw_workspace_bytes_not_companion_bytes(snapshot, tmp_path):
    manifest = snapshot / "workspace" / "manifests" / "storage" / "manifest.yaml"
    lf = manifest.read_bytes().replace(b"\r\n", b"\n")
    manifest.write_bytes(lf)
    before = _build(snapshot, tmp_path / "first.zip")
    (snapshot / "docs" / "guide.md").write_text("Changed guide\n", encoding="utf-8")
    companion_change = _build(snapshot, tmp_path / "second.zip")
    assert before.sha256 != companion_change.sha256
    assert before.metadata.tree_sha256 == companion_change.metadata.tree_sha256
    manifest.write_bytes(lf.replace(b"\n", b"\r\n"))
    workspace_change = _build(snapshot, tmp_path / "third.zip")
    assert workspace_change.metadata.tree_sha256 != companion_change.metadata.tree_sha256


def test_root_workspace_is_supported(snapshot, tmp_path):
    built = _build(snapshot / "workspace", tmp_path / "package.zip", workspace=".", companions=())
    assert built.metadata.workspace_root == "."
    assert "manifests/storage/manifest.yaml" in {entry.path for entry in built.metadata.files}


def test_highly_compressible_source_is_stored_without_weakening_consumer_limits(snapshot, tmp_path):
    (snapshot / "workspace" / "zeroes.bin").write_bytes(b"\0" * 128000)
    built = _build(snapshot, tmp_path / "package.zip")
    with zipfile.ZipFile(tmp_path / "package.zip") as archive:
        assert archive.getinfo("workspace/zeroes.bin").compress_type == zipfile.ZIP_STORED
    assert package.inspect_package(tmp_path / "package.zip", built.sha256) == built


def test_digest_is_checked_before_zip_parsing_or_destination_creation(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    _archive(path)
    monkeypatch.setattr(package.zipfile, "ZipFile", lambda *a, **k: pytest.fail("Parsed before digest"))
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, "0" * 64, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("mutate", [
    lambda document: document.update(apiVersion="unknown"),
    lambda document: document.update(hooks={"local": "PRIVATE_SENTINEL"}),
    lambda document: document["workspace"].update(root="../escape"),
    lambda document: document["workspace"]["tree"].update(algorithm="sha1"),
    lambda document: document["workspace"]["tree"].update(digest="0" * 64),
    lambda document: document["files"][0].update(size=True),
    lambda document: document["files"][0].update(sha256="not-a-digest"),
    lambda document: document["compatibility"].update(siteops=">=1.0"),
    lambda document: document["compatibility"].update(siteops=">=2,<3"),
    lambda document: document["compatibility"].update(requiredFeatures=["unknown/required"]),
    lambda document: document["compatibility"].update(requiredFeatures=["manifest/v1", "manifest/v1"]),
])
def test_invalid_metadata_and_compatibility_fail_before_materialization(tmp_path, mutate):
    document = _document()
    mutate(document)
    path = tmp_path / "package.zip"
    digest = _archive(path, document)
    with pytest.raises(ArtifactError) as failed:
        package.extract_package(path, digest, tmp_path / "staging")
    assert "PRIVATE_SENTINEL" not in str(failed.value)
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("raw", [
    b'{"kind":"WorkspacePackage","kind":"PRIVATE_SENTINEL"}',
    b'{"private":NaN}',
    b"\xff",
    b"[" * 3000,
])
def test_invalid_json_is_bounded_and_value_safe(tmp_path, raw):
    path = tmp_path / "package.zip"
    digest = _archive(path, raw_metadata=raw)
    with pytest.raises(ArtifactError) as failed:
        package.inspect_package(path, digest)
    assert "PRIVATE_SENTINEL" not in str(failed.value)


@pytest.mark.parametrize("path", [
    "../outside", "/absolute", "C:/drive", "C:drive", "a\\b", "a//b", "a/./b",
    "a/../b", "a.", "a ", "AUX.txt", "COM1", "COM\u00b9", "a:stream",
    "a?b", "a\x00b", "a\u202eb", "e\u0301.txt", "a/" + "b" * 256,
])
def test_package_paths_are_portable_and_confined(path):
    with pytest.raises(ArtifactError):
        relative_artifact_path(path)


@pytest.mark.parametrize("paths", [
    ("workspace/file", "workspace/FILE"),
    ("workspace/Dir/a", "workspace/dir/b"),
    ("workspace/file", "workspace/file/child"),
])
def test_case_aliases_and_file_directory_conflicts_are_rejected(tmp_path, paths):
    files = {path: b"data" for path in paths}
    path = tmp_path / "package.zip"
    digest = _archive(path, _document(files), files)
    with pytest.raises(ArtifactError, match="collision"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("mutate", [
    lambda info: setattr(info, "external_attr", (stat.S_IFLNK | 0o600) << 16),
    lambda info: setattr(info, "external_attr", (stat.S_IFIFO | 0o600) << 16),
    lambda info: setattr(info, "external_attr", info.external_attr | 0x400),
    lambda info: setattr(info, "extra", b"\x01\x00\x00\x00"),
    lambda info: setattr(info, "comment", b"comment"),
])
def test_zip_member_features_are_rejected_before_writing(tmp_path, mutate):
    path = tmp_path / "package.zip"
    digest = _archive(path, info_mutator=mutate)
    with pytest.raises(ArtifactError):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_actual_central_directory_count_is_checked_before_zip_allocation(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    _archive(path)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<HH", raw, len(raw) - 22 + 8, 1, 1)
    path.write_bytes(raw)
    monkeypatch.setattr(package.zipfile, "ZipFile", lambda *a, **k: pytest.fail("Allocated forged directory"))
    with pytest.raises(ArtifactError, match="count or size"):
        package.inspect_package(path, hashlib.sha256(raw).hexdigest())


def test_understated_metadata_size_cannot_cause_unbounded_decompression(tmp_path):
    path = tmp_path / "package.zip"
    metadata = package.json_bytes(_document())
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            package_builder._zip_info(package.PACKAGE_NAME, zipfile.ZIP_DEFLATED),
            metadata + b" " * (16 * 1024 * 1024),
        )
        archive.writestr(
            package_builder._zip_info("workspace/file.txt", zipfile.ZIP_STORED), b"payload",
        )
    raw = bytearray(path.read_bytes())
    central = struct.unpack_from("<I", raw, len(raw) - 22 + 16)[0]
    struct.pack_into("<I", raw, 22, len(metadata))
    struct.pack_into("<I", raw, central + 24, len(metadata))
    assert struct.unpack_from("<I", raw, central + 16)[0] != zlib.crc32(metadata)
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    tracemalloc.start()
    try:
        with pytest.raises(ArtifactError):
            package.extract_package(path, expected, tmp_path / "staging")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_NODES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES",
                                   "MAX_ARCHIVE_BYTES", "MAX_METADATA_BYTES", "MAX_CENTRAL_BYTES"])
def test_resource_limits_fail_before_materialization(tmp_path, monkeypatch, limit):
    path = tmp_path / "package.zip"
    digest = _archive(path)
    monkeypatch.setattr(package, limit, 0 if limit == "MAX_FILES" else 1)
    with pytest.raises(ArtifactError):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_compression_expansion_is_rejected_before_writing(tmp_path):
    files = {"workspace/zeroes": b"\0" * 128000}
    path = tmp_path / "package.zip"
    digest = _archive(
        path, _document(files), files,
        info_mutator=lambda info: setattr(info, "compress_type", zipfile.ZIP_DEFLATED),
    )
    with pytest.raises(ArtifactError, match="compression"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_changed_payload_is_removed_without_publishing_partial_content(tmp_path):
    path = tmp_path / "package.zip"
    document = _document({"workspace/file.txt": b"expected"})
    digest = _archive(path, document, {"workspace/file.txt": b"modified"})
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_existing_destination_and_output_are_preserved(snapshot, tmp_path):
    output = tmp_path / "package.zip"
    built = _build(snapshot, output)
    original = output.read_bytes()
    with pytest.raises(ArtifactError, match="already exists"):
        _build(snapshot, output)
    assert output.read_bytes() == original
    destination = tmp_path / "operator"
    destination.mkdir()
    sentinel = destination / "operator.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ArtifactError, match="new writable"):
        package.extract_package(output, built.sha256, destination)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_archive_change_during_extraction_invalidates_and_removes_staging(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    digest = _archive(path)
    original = package._copy_member

    def changed(archive, entry, target):
        original(archive, entry, target)
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))

    monkeypatch.setattr(package, "_copy_member", changed)
    with pytest.raises(ArtifactError, match="changed"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_failed_cleanup_keeps_the_primary_integrity_error(tmp_path, monkeypatch, caplog):
    path = tmp_path / "package.zip"
    digest = _archive(path, _document({"workspace/file.txt": b"expected"}), {"workspace/file.txt": b"modified"})
    original = Path.unlink

    def failed(path, *args, **kwargs):
        if path.name == "file.txt":
            raise OSError("cleanup fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed)
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert "cleanup" in caplog.text


def test_hardlinked_and_lfs_sources_fail_before_output(snapshot, tmp_path):
    original = snapshot / "workspace" / "manifests" / "storage" / "README.md"
    os.link(original, snapshot / "workspace" / "linked.md")
    with pytest.raises(ArtifactError, match="unlinked"):
        _build(snapshot, tmp_path / "package.zip")
    (snapshot / "workspace" / "linked.md").unlink()
    original.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(ArtifactError, match="Git LFS"):
        _build(snapshot, tmp_path / "package.zip")
    assert not (tmp_path / "package.zip").exists()


def test_source_changes_during_production_preserve_output_absence(snapshot, tmp_path, monkeypatch):
    original = package_builder._record

    def changed(root, relative):
        result = original(root, relative)
        if relative == "LICENSE":
            (root / relative).write_text("changed source", encoding="utf-8")
        return result

    monkeypatch.setattr(package_builder, "_record", changed)
    with pytest.raises(ArtifactError, match="changed"):
        _build(snapshot, tmp_path / "package.zip")
    assert not (tmp_path / "package.zip").exists()
    assert not list(tmp_path.glob(".siteops-package-*"))


def test_tree_digest_does_not_depend_on_file_order():
    document = _document({"workspace/z": b"z", "workspace/a": b"a"})
    reversed_document = copy.deepcopy(document)
    reversed_document["files"].reverse()
    assert package.WorkspacePackage.from_document(document) == package.WorkspacePackage.from_document(
        reversed_document
    )


def test_prerelease_engine_versions_use_pep440_comparison():
    metadata = package.WorkspacePackage.from_document(_document())
    package.check_compatibility(metadata, engine_version="1.0.0b1+build.123")
    with pytest.raises(ArtifactError, match="different"):
        package.check_compatibility(metadata, engine_version="1.0.0a9")
    with pytest.raises(ArtifactError, match="different"):
        package.check_compatibility(metadata, engine_version="2.0.0")


def test_negative_read_budget_does_not_read_a_stream():
    class Unreadable(io.BytesIO):
        def read(self, *args):
            pytest.fail("Read with invalid budget")

    with pytest.raises(ArtifactError, match="limit"):
        hash_stream(Unreadable(b"data"), limit=-1)


def test_file_context_preserves_errors_from_its_caller(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"payload")
    failure = OSError("caller write failure")
    with pytest.raises(OSError) as raised:
        with open_regular_file(path):
            raise failure
    assert raised.value is failure


def test_encrypted_zip_flags_are_rejected_before_materialization(tmp_path):
    path = tmp_path / "package.zip"
    _archive(path)
    raw = bytearray(path.read_bytes())
    central = struct.unpack_from("<I", raw, len(raw) - 22 + 16)[0]
    struct.pack_into("<H", raw, central + 8, 1)
    path.write_bytes(raw)
    with pytest.raises(ArtifactError, match="encoding"):
        package.extract_package(path, hashlib.sha256(raw).hexdigest(), tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_duplicate_zip_members_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "package.zip"
    _archive(path)
    with pytest.warns(UserWarning, match="Duplicate"):
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(package_builder._zip_info("workspace/file.txt", zipfile.ZIP_STORED), b"payload")
    with pytest.raises(ArtifactError, match="collision"):
        package.extract_package(path, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def _git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_snapshot(snapshot):
    _git(snapshot, "init", "--quiet")
    _git(snapshot, "config", "user.name", "Package Test")
    _git(snapshot, "config", "user.email", "package@example.invalid")
    (snapshot / ".gitignore").write_text("ignored-secret\ngit.exe\n", encoding="utf-8")
    _git(snapshot, "add", ".")
    _git(snapshot, "commit", "--quiet", "-m", "package fixture")
    return snapshot, _git(snapshot, "rev-parse", "HEAD")


def _producer(root, sha, output, *extra):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build-workspace-package.py"
    return subprocess.run(
        [
            sys.executable, str(script), "--root", str(root),
            "--expected-source-sha", sha, "--workspace", "workspace",
            "--id", "example/storage", "--version", "preview-7",
            "--requires-siteops", ">=1.0.0b1,<2", "--include", "docs",
            "--license", "LICENSE", "--output", str(output), *extra,
        ],
        cwd=root, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL, check=False,
    )


def test_git_producer_uses_reviewed_source_not_ignored_files_or_cwd_tools(git_snapshot, tmp_path):
    root, sha = git_snapshot
    (root / "ignored-secret").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (root / "git.exe").write_bytes(b"not an executable")
    result = _producer(root, sha, tmp_path / "package.zip")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["provenance"] == "not-established"
    inspected = package.inspect_package(tmp_path / "package.zip", receipt["sha256"])
    assert inspected.metadata.source_revision == sha
    assert "ignored-secret" not in {entry.path for entry in inspected.metadata.files}
    assert "PRIVATE_SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["dirty", "wrong-commit", "export-ignore"])
def test_git_producer_refuses_unidentified_or_incomplete_source(git_snapshot, tmp_path, fault):
    root, sha = git_snapshot
    if fault == "dirty":
        (root / "LICENSE").write_text("changed", encoding="utf-8")
    elif fault == "wrong-commit":
        sha = "0" * 40
    else:
        (root / ".gitattributes").write_text("workspace/** export-ignore\n", encoding="utf-8")
        _git(root, "add", ".gitattributes")
        _git(root, "commit", "--quiet", "-m", "excluded fixture")
        sha = _git(root, "rev-parse", "HEAD")
    result = _producer(root, sha, tmp_path / "package.zip")
    assert result.returncode == 1
    assert "Error:" in result.stderr
    assert not (tmp_path / "package.zip").exists()


def test_git_companion_paths_are_literal_not_glob_patterns(git_snapshot, tmp_path):
    root, _ = git_snapshot
    (root / "guide[one].md").write_text("literal guide", encoding="utf-8")
    (root / "guideo.md").write_text("different guide", encoding="utf-8")
    _git(root, "add", "guide[one].md", "guideo.md")
    _git(root, "commit", "--quiet", "-m", "literal fixture")
    sha = _git(root, "rev-parse", "HEAD")
    result = _producer(root, sha, tmp_path / "package.zip", "--include", "guide[one].md")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    paths = {
        entry.path for entry in package.inspect_package(tmp_path / "package.zip", receipt["sha256"]).metadata.files
    }
    assert "guide[one].md" in paths
    assert "guideo.md" not in paths

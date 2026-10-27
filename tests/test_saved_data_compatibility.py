"""Files that v1.0.0b7 saved stay readable, unchanged and identical when written again.

The fixtures under tests/fixtures/compatibility/v1.0.0b7/saved-data were written
by the v1.0.0b7 engine. Each test reads them through the current engine's own
entry point, checks their meaning and that reading leaves every byte and
modification time unchanged, then writes the same content with the current
engine. Those writes must produce the same bytes, which the v1.0.0b7 readers
already accept, so a rollback to that release reads them without running its
code here. A package that the current engine builds again must hold the same
members as the released archive, since compressed bytes depend on the zlib
build.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from siteops.artifacts import hash_file
from siteops.cache_filesystem import make_private_directory
from siteops.cache_layout import CacheLayout, write_new
from siteops.cli import main
from siteops.content_index import (
    build_content_index,
    load_content_index,
    load_source_bindings,
    write_content_index,
)
from siteops.github_attestation import load_github_policy
from siteops.package_builder import build_package
from siteops.project import read_pin, write_pin
from siteops.source_metadata_cache import MetadataKey, SourceMetadataCache
from siteops.source_profiles import enroll_source, list_sources, read_source
from siteops.workspace_cache import WorkspaceCache
from siteops.workspace_source import ArtifactIdentity

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "tests" / "fixtures" / "compatibility" / "v1.0.0b7"
SAVED = RELEASE / "saved-data"
MOMENT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
# An installed fixture keeps this old modification time, so any later write or touch changes it.
STAMP = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) * 10**9
METADATA_KEY = MetadataKey("github-release/v1", "github:example/content", "release", "v1.0.0")
METADATA = b'{"tag_name":"v1.0.0"}'
PROOF = ArtifactIdentity("content.zip.attestation.jsonl", 12, hashlib.sha256(b"proof bytes\n").hexdigest())
CACHE_DIRECTORIES = ("objects/sha256", "receipts", "staging", "locks")
PIN = json.loads((SAVED / "project" / "siteops.pin").read_text(encoding="utf-8"))
PACKAGE, REVISION = PIN["content"]["package"]["sha256"], PIN["source"]["revision"]
# The package archive that v1.0.0b7 published, kept byte for byte.
PUBLISHED = SAVED / "cache" / "objects" / "sha256" / PACKAGE / "package.zip"
# Every use writes a fresh verification receipt, and no command reads one back.
_UNREAD = ("locks/", "receipts/")


def _install(source: Path, target: Path) -> Path:
    """Copy fixture files through the private storage helpers the engine checks."""
    make_private_directory(target)
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            make_private_directory(destination)
        else:
            write_new(destination, path.read_bytes())
            os.utime(destination, ns=(STAMP, STAMP))
    return target


def _age(root: Path) -> Path:
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, ns=(STAMP, STAMP))
    return root


def _state(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _bytes(root: Path) -> dict[str, bytes]:
    return {name: content for name, (content, _) in _state(root).items()}


def _members(archive: Path) -> tuple[bytes, list[tuple]]:
    """Describe a package archive by everything except its compressed bytes.

    Deflate output differs between zlib builds, such as the zlib-ng in some
    Python distributions, while each member's name, order, metadata and
    content stay the same.
    """
    with zipfile.ZipFile(archive) as opened:
        return opened.comment, [
            (
                item.filename, item.date_time, item.compress_type, item.external_attr, item.create_system,
                item.create_version, item.extract_version, item.flag_bits, item.extra, item.comment,
                item.file_size, item.CRC, hashlib.sha256(opened.read(item)).hexdigest(),
            )
            for item in opened.infolist()
        ]


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    root = tmp_path / "config"
    monkeypatch.setenv("LOCALAPPDATA" if os.name == "nt" else "XDG_CONFIG_HOME", str(root))
    return root


def test_released_pin_is_read_unchanged_and_written_identically(tmp_path):
    project = _install(SAVED / "project", tmp_path / "project")
    before = _state(project)
    snapshot = read_pin(project)
    assert _state(project) == before
    selection = snapshot.pin.selection
    assert (selection.source.reference, selection.source.release) == ("github:example/content", "v1.0.0")
    assert (selection.entry.workspace, selection.entry.kit_id, selection.entry.kit_version) == (
        "workspace", "example.consumer", "1.0.0",
    )
    assert snapshot.sha256 == hashlib.sha256(before["siteops.pin"][0]).hexdigest()
    fresh = tmp_path / "fresh"
    make_private_directory(fresh)
    write_pin(fresh, snapshot.pin, expected_previous=None)
    assert (fresh / "siteops.pin").read_bytes() == before["siteops.pin"][0]
    assert (fresh / ".siteops" / ".gitignore").read_bytes() == b"*\n"
    write_pin(project, snapshot.pin, expected_previous=snapshot.sha256)
    assert (project / "siteops.pin").read_bytes() == before["siteops.pin"][0]


def test_released_source_enrollment_is_read_unchanged_and_written_identically(tmp_path, configuration):
    _install(SAVED / "config", configuration)
    record = configuration / "siteops" / "sources" / "example"
    before = _state(record)
    source = read_source("example")
    assert (source.provider, source.reference) == ("github-release/v1", "github:example/content")
    assert source.policy_sha256 == hashlib.sha256(before["policy.json"][0]).hexdigest()
    assert source.root_sha256 == hashlib.sha256(before["trusted-root.jsonl"][0]).hexdigest()
    policy = load_github_policy(source.policy)
    assert (policy.repository, policy.source_ref) == ("example/content", "refs/heads/main")
    assert policy.valid_until == datetime(2126, 1, 1, tzinfo=timezone.utc)
    assert list_sources() == ("example",)
    inputs = _install(SAVED / "config" / "siteops" / "sources" / "example", tmp_path / "inputs")
    assert enroll_source("example", "github:example/content", inputs / "policy.json",
                         inputs / "trusted-root.jsonl").policy_sha256 == source.policy_sha256
    assert _state(record) == before
    shutil.rmtree(configuration)
    enroll_source("example", "github:example/content", inputs / "policy.json", inputs / "trusted-root.jsonl")
    assert _bytes(record) == {name: content for name, (content, _) in before.items()}


def test_released_cache_is_read_unchanged_and_written_identically(tmp_path):
    cache = _install(SAVED / "cache", tmp_path / "cache")
    for name in CACHE_DIRECTORIES:
        for directory in (cache / name.split("/")[0], cache / name):
            if not directory.exists():
                make_private_directory(directory)
    before = _state(cache)
    CacheLayout(cache, create=False)
    observation = SourceMetadataCache(cache, clock=lambda: MOMENT).get_or_fetch(
        METADATA_KEY, lambda: pytest.fail("A cached immutable observation was fetched again."),
    )
    assert (observation.payload, observation.observed_at, observation.reused) == (METADATA, MOMENT, True)
    with WorkspaceCache(cache, lock_timeout=0).lease_proof(PROOF) as proof:
        assert proof.read_bytes() == b"proof bytes\n"
    # The package that v1.0.0b7 published, with its extracted content.
    with WorkspaceCache(cache, lock_timeout=0).lease(PACKAGE, source_revision=REVISION, verify=_Verifier()) as used:
        assert used.inspection.sha256 == PACKAGE
        assert _bytes(used.package_root / "workspace") == _bytes(RELEASE / "ado-consumer" / "workspace")
    assert {name: content for name, (content, _) in _state(cache).items() if not name.startswith(_UNREAD)} == {
        name: content for name, (content, _) in before.items()
    }
    assert all(stamp == before[name][1] for name, (_, stamp) in _state(cache).items() if name in before)
    fresh = tmp_path / "fresh"
    CacheLayout(fresh)
    SourceMetadataCache(fresh, clock=lambda: MOMENT).get_or_fetch(METADATA_KEY, lambda: METADATA)
    retained = tmp_path / "proof.bin"
    retained.write_bytes(b"proof bytes\n")
    WorkspaceCache(fresh, lock_timeout=0).retain_proof(retained, PROOF)
    # The cache keeps a published archive as downloaded, so publish the released bytes.
    archive = tmp_path / "content.zip"
    shutil.copyfile(PUBLISHED, archive)
    WorkspaceCache(fresh, lock_timeout=0).publish(archive, PACKAGE, source_revision=REVISION, verify=_Verifier())
    written = {name: content for name, content in _bytes(fresh).items() if not name.startswith(_UNREAD)}
    assert written == {name: content for name, (content, _) in before.items()}
    assert {path.relative_to(fresh).as_posix() for path in fresh.rglob("*") if path.is_dir()} >= {
        *CACHE_DIRECTORIES, "objects", "metadata/records", "proofs/sha256",
    }


@dataclass
class _Verifier:
    calls: int = 0

    def __call__(self, artifact):
        from siteops.artifact_verification import ArtifactVerification

        self.calls += 1
        size, digest = hash_file(artifact, limit=16 * 1024 * 1024)
        now = datetime.now(timezone.utc)
        return ArtifactVerification(
            sha256=digest, size=size, policy_id="compatibility", policy_version=1, policy_sha256="1" * 64,
            root_sha256="2" * 64, proof_sha256="3" * 64, evaluated_at=now - timedelta(seconds=1),
            valid_until=now + timedelta(days=1), verifier="test", verifier_version="1", evidence_json=b"{}",
        )


def test_released_package_identity_matches_the_current_package_and_cache(tmp_path):
    pin = read_pin(_install(SAVED / "project", tmp_path / "project")).pin.selection
    assert hash_file(PUBLISHED, limit=16 * 1024 * 1024) == (pin.entry.package.size, pin.entry.package.sha256)
    source = tmp_path / "source"
    shutil.copytree(RELEASE / "ado-consumer", source)
    archive = tmp_path / "content.zip"
    build_package(source, archive, workspace="workspace", kit_id="example.consumer", version="1.0.0",
                  source_revision=pin.source.revision, siteops_range=">=1.0.0b1,<2")
    assert _members(archive) == _members(PUBLISHED)
    published = tmp_path / "published.zip"
    shutil.copyfile(PUBLISHED, published)
    cache = WorkspaceCache(tmp_path / "cache", lock_timeout=0)
    cache.publish(published, pin.entry.package.sha256, source_revision=pin.source.revision, verify=_Verifier())
    with cache.lease(pin.entry.package.sha256, source_revision=pin.source.revision, verify=_Verifier()) as workspace:
        pin.check_package(workspace.inspection)
        assert (workspace.package_root / "workspace" / "manifests" / "smoke.yaml").is_file()
    stored = tmp_path / "cache" / "objects" / "sha256" / pin.entry.package.sha256
    assert sorted(path.name for path in stored.iterdir()) == ["content", "package.zip"]


def _inputs(*arguments):
    with patch.object(sys, "argv", ["siteops", *arguments]):
        with pytest.raises(SystemExit) as stopped:
            main()
    return stopped.value.code


def test_released_input_files_are_read_unchanged_and_written_identically(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    shutil.copytree(RELEASE / "ado-consumer" / "workspace", workspace)
    shutil.copyfile(SAVED / "inputs" / "smoke.inputs.yaml", workspace / "manifests" / "smoke.inputs.yaml")
    released = tmp_path / "released"
    released.mkdir()
    for name in ("answers.yaml", "answers-filled.yaml", "site.yaml"):
        shutil.copyfile(SAVED / "inputs" / name, released / name)
    before = _state(_age(released))
    answers = ["--input", "siteName=saved-site", "--input", "subscription=00000000-0000-0000-0000-000000000001",
               "--input", "resourceGroup=rg-saved", "--input", "location=eastus"]
    written = tmp_path / "written"
    written.mkdir()
    assert _inputs("-w", str(workspace), "inputs", "manifests/smoke.yaml", "--example", str(written / "answers.yaml")) == 0
    assert _inputs("-w", str(workspace), "inputs", "manifests/smoke.yaml", "--input-file", str(released / "answers.yaml"),
                   *answers, "--save-site", str(written / "site.yaml")) == 0
    # The released answer file with its values filled in, which v1.0.0b7 saves as the same Site.
    assert _inputs("-w", str(workspace), "inputs", "manifests/smoke.yaml",
                   "--input-file", str(released / "answers-filled.yaml"),
                   "--save-site", str(written / "from-answers.yaml")) == 0
    assert _inputs("-w", str(workspace), "validate", "manifests/smoke.yaml",
                   "--site-file", str(released / "site.yaml")) == 0
    capsys.readouterr()
    assert _state(released) == before
    saved = _bytes(written)
    assert saved.pop("from-answers.yaml") == before["site.yaml"][0]
    assert saved == {name: before[name][0] for name in ("answers.yaml", "site.yaml")}


def test_released_content_index_is_read_unchanged_and_built_identically(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(SAVED / "index", workspace)
    before = _state(_age(workspace))
    index = before["siteops-index.json"][0]
    entries = load_content_index(index)
    assert [(entry.path, entry.name) for entry in entries] == [("manifests/storage/manifest.yaml", "storage")]
    bindings = load_source_bindings(before["siteops-index.inputs.json"][0], index)
    assert bindings.index_digest == hashlib.sha256(index).hexdigest()
    built = build_content_index(workspace, approve_public=True)
    assert (built.index, built.bindings) == (index, before["siteops-index.inputs.json"][0])
    write_content_index(workspace, built, check=True)
    assert _state(workspace) == before


def test_saved_data_lists_every_bootstrap_store():
    bootstrap = ROOT / "scripts" / "bootstrap"
    stores = {
        *re.findall(r'"\$data/([a-z-]+)', (bootstrap / "siteops-bootstrap.sh").read_text(encoding="utf-8")),
        *re.findall(r"Join-Path \$data '([a-z-]+)", (bootstrap / "siteops-bootstrap.ps1").read_text(encoding="utf-8"),
                    re.IGNORECASE),
    }
    assert stores == {"bundles", "engine-references", "install-downloads", "install-staging", "tools"}
    guide = (ROOT / "docs" / "saved-data.md").read_text(encoding="utf-8")
    assert [store for store in sorted(stores) if f"| `<data>/{store}/" not in guide] == []

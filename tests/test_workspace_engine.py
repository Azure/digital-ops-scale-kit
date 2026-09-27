"""Native engine selection and extraction without live source or signing operations."""

import hashlib
import json
import shutil
import stat
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import ArtifactError
from siteops.github_source import GitHubReference, GitHubReleaseAsset, GitHubReleaseSnapshot
from tests.native_bundle import bundle_factory as bundle_factory
from tests.release_helpers import SCRIPTS

sys.path.insert(0, str(SCRIPTS))

import workspace_engine as engine  # noqa: E402
from siteops_release import ReleaseIntent  # noqa: E402
from workspace_release import WorkspaceBuild  # noqa: E402


def intent(*, reference=False):
    return ReleaseIntent(
        "example/publisher", "b" * 40 if reference else "a" * 40, "refs/heads/main",
        "releases/candidate/release.json", "c" * 64, "releases/candidate/notes.md", "d" * 64,
        "scalekit", "v2.0.0b1", "2.0.0b1", "Candidate", True, False,
        not reference, None if reference else "build", None if reference else "1.0.0b1",
        "siteops/v1.2.3" if reference else None, "Notes", workspaces=(
            WorkspaceBuild.from_document({
                "workspace": "workspace", "id": "fixture", "package": "workspace.zip",
                "licenses": ["LICENSE"], "compatibility": {"siteops": ">=1.0.0b1,<2"},
            }),
        ),
    )


class Verifier:
    def __init__(self):
        self.calls = []
        self.reject = False

    def __call__(self, artifact, proof, identity, source, builder):
        self.calls.append((artifact.name, source, builder))
        if self.reject:
            raise ArtifactError("Controlled engine proof rejection.")
        now = datetime.now(timezone.utc)
        return ArtifactVerification(
            identity.sha256, identity.size, "fixture", 1, "a" * 64, "b" * 64,
            hashlib.sha256(proof.read_bytes()).hexdigest(), now, now + timedelta(hours=1),
            "fixture", "1", b"{}",
        )


def assets(bundle, manifest, tmp_path, mutate=None, *, bootstrap=True):
    directory = tmp_path / "native"
    directory.mkdir()
    with zipfile.ZipFile(directory / engine.ARCHIVE_NAME, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                relative = path.relative_to(bundle).as_posix()
                if mutate is None:
                    archive.write(path, relative)
                else:
                    info = zipfile.ZipInfo(relative)
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(mutate(info), path.read_bytes())
    wheel = bundle / manifest.application_wheel
    shutil.copyfile(wheel, directory / wheel.name)
    subjects = (engine.ARCHIVE_NAME, wheel.name)
    if bootstrap:
        for name in ("siteops-bootstrap.ps1", "siteops-bootstrap.sh"):
            (directory / name).write_bytes(name.encode("ascii"))
        subjects += ("siteops-bootstrap.ps1", "siteops-bootstrap.sh")
    for name in subjects:
        (directory / (name + engine.PROOF_SUFFIX)).write_bytes(b"opaque fixture proof")
    return directory, wheel.name


def prepare(directory, wheel, tmp_path, verifier, selected=None, **kwargs):
    return engine.prepare_engine(
        selected or intent(), "e" * 64, tmp_path / "selected", tmp_path / "transfers",
        build_number=1, build_attempt=1, builder_workflow=".github/workflows/ci.yaml",
        verifier=verifier, built_assets=directory,
        archive_sha=hashlib.sha256((directory / engine.ARCHIVE_NAME).read_bytes()).hexdigest(),
        wheel_sha=hashlib.sha256((directory / wheel).read_bytes()).hexdigest(), **kwargs,
    )


def test_built_engine_preserves_verified_wheel_and_declared_targets(bundle_factory, tmp_path):
    bundle, manifest = bundle_factory()
    directory, wheel = assets(bundle, manifest, tmp_path)
    verifier = Verifier()
    selected = prepare(directory, wheel, tmp_path, verifier)
    assert [row[0] for row in verifier.calls] == [
        engine.ARCHIVE_NAME, wheel, "siteops-bootstrap.ps1", "siteops-bootstrap.sh",
    ]
    assert selected.version == manifest.version
    assert selected.native.source["commit"] == "a" * 40
    assert len(selected.matrix()["include"]) == 10
    assert selected.reference is None
    path = tmp_path / "selected" / engine.SELECTION_NAME
    assert engine.EngineSelection.read(path, hashlib.sha256(path.read_bytes()).hexdigest()) == selected
    materialized = tmp_path / "materialized"
    assert engine.extract_engine_bundle(tmp_path / "selected" / engine.ARCHIVE_NAME, materialized, selected) == manifest
    assert (materialized / manifest.application_wheel).read_bytes() == (directory / wheel).read_bytes()


@pytest.mark.parametrize("bootstrap", [False, True])
def test_referenced_engine_uses_its_own_commit_without_building(bundle_factory, tmp_path, bootstrap):
    bundle, manifest = bundle_factory(version="1.2.3", source_sha="a" * 40)
    directory, wheel = assets(bundle, manifest, tmp_path, bootstrap=bootstrap)
    values = tuple(
        GitHubReleaseAsset(index, path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for index, path in enumerate(sorted(directory.iterdir()), 1)
    )
    snapshot = GitHubReleaseSnapshot(
        GitHubReference("example", "publisher", "siteops/v1.2.3"), 11, 71, "d" * 40,
        "a" * 40, False, datetime.now(timezone.utc), True, values,
    )

    class Client:
        def resolve_release(self):
            return snapshot

    downloads = []

    @contextmanager
    def download(release, asset, *, staging_parent):
        assert release is snapshot
        downloads.append(asset.identifier)
        yield directory / asset.name

    verifier = Verifier()
    selected = engine.prepare_engine(
        intent(reference=True), "e" * 64, tmp_path / "selected", tmp_path / "transfers",
        build_number=99, build_attempt=8, builder_workflow=".github/workflows/ci.yaml",
        verifier=verifier, release_client=Client(), downloader=download,
    )
    assert downloads == [asset.identifier for asset in values]
    assert [call[0] for call in verifier.calls] == [
        engine.ARCHIVE_NAME, wheel,
        *(("siteops-bootstrap.ps1", "siteops-bootstrap.sh") if bootstrap else ()),
    ]
    assert selected.version == "1.2.3" and selected.reference.release_id == "71"
    assert selected.reference.target == "d" * 40
    assert selected.candidate["commit"] == "b" * 40
    assert selected.native.source["commit"] == "a" * 40
    assert all(source["ref"] == "refs/heads/main" and builder == ".github/workflows/release.yaml" for _, source, builder in verifier.calls)


def test_engine_proof_precedes_archive_parsing(bundle_factory, tmp_path, monkeypatch):
    bundle, manifest = bundle_factory()
    directory, wheel = assets(bundle, manifest, tmp_path)
    verifier = Verifier()
    verifier.reject = True
    monkeypatch.setattr(engine, "inspect_engine_bundle", lambda *a: pytest.fail("Archive parsed before proof."))
    with pytest.raises(ArtifactError, match="Controlled engine proof rejection"):
        prepare(directory, wheel, tmp_path, verifier)
    assert not (tmp_path / "selected").exists()


@pytest.mark.parametrize("fault", ["version", "source", "member", "symlink", "extra"])
def test_engine_selection_rejects_inconsistent_native_assets(bundle_factory, tmp_path, fault):
    bundle, manifest = bundle_factory()
    if fault in {"version", "source"}:
        document = manifest.to_dict()
        if fault == "version":
            document["package"]["version"] = "9.0.0"
        else:
            document["source"]["commit"] = "d" * 40
        (bundle / "bundle.json").write_text(json.dumps(document))

    def mutate(info):
        if info.filename == "LICENSE" and fault == "member":
            info.filename = "../outside"
        elif info.filename == "LICENSE" and fault == "symlink":
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return info
    directory, wheel = assets(bundle, manifest, tmp_path, mutate)
    if fault == "extra":
        (directory / "unexpected").write_bytes(b"unlisted")
    with pytest.raises((ArtifactError, ValueError)):
        prepare(directory, wheel, tmp_path, Verifier())
    assert not (tmp_path / "selected").exists()


def test_failed_payload_verification_removes_only_new_extraction(bundle_factory, tmp_path):
    bundle, manifest = bundle_factory()
    original = (bundle / "LICENSE").read_bytes()
    (bundle / "LICENSE").write_bytes(b"x" * len(original))
    directory, wheel = assets(bundle, manifest, tmp_path)
    selected = prepare(directory, wheel, tmp_path, Verifier())
    output = tmp_path / "extract"
    with pytest.raises((ArtifactError, ValueError)):
        engine.extract_engine_bundle(directory / engine.ARCHIVE_NAME, output, selected)
    assert not output.exists()


def test_engine_matrix_never_selects_arbitrary_runner_labels(bundle_factory, tmp_path):
    bundle, manifest = bundle_factory()
    directory, wheel = assets(bundle, manifest, tmp_path)
    selected = prepare(directory, wheel, tmp_path, Verifier())
    with pytest.raises(ArtifactError, match="unsupported"):
        replace(selected, targets=(("3.11", "self-hosted"),)).matrix()

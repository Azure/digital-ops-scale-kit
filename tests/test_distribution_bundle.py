"""Validate the shared installation bundle contract and payload checks."""

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from siteops_distribution import (  # noqa: E402
    BundleManifest,
    BundleTarget,
    DistributionError,
    PayloadFile,
    load_manifest,
    select_target,
    verify_payload,
)


def _write_bundle(root: Path) -> BundleManifest:
    payload = {
        "pylock.toml": b'lock-version = "1.0"\n',
        "LICENSE": b"license\n",
        "ThirdPartyNotices.txt": b"notices\n",
        "wheels/siteops-1.0.0-py3-none-any.whl": b"application wheel\n",
        "wheels/dependency-1.0-cp311-cp311-win_amd64.whl": b"dependency wheel\n",
    }
    for name, content in payload.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files = tuple(
        PayloadFile(
            path=name,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )
        for name, content in sorted(payload.items())
    )
    application = "wheels/siteops-1.0.0-py3-none-any.whl"
    dependency = "wheels/dependency-1.0-cp311-cp311-win_amd64.whl"
    manifest = BundleManifest(
        version="1.0.0",
        base_version="1.0.0",
        repository="example/siteops",
        source_sha="a" * 40,
        source_ref="refs/tags/v1.0.0",
        build_number=12,
        build_attempt=2,
        application_wheel=application,
        targets=(
            BundleTarget(
                python="3.11",
                platform="windows-x86_64",
                wheels=(application, dependency),
            ),
        ),
        files=files,
    )
    (root / "bundle.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def test_valid_manifest_round_trips_verifies_and_selects_exact_target(tmp_path):
    manifest = _write_bundle(tmp_path)
    loaded = load_manifest(tmp_path)

    assert loaded == manifest
    assert BundleManifest.from_dict(loaded.to_dict()) == manifest
    verify_payload(tmp_path, loaded)
    assert select_target(loaded, "3.11", "windows-x86_64") == loaded.targets[0]


def test_target_selection_reports_declared_neighbors(tmp_path):
    manifest = _write_bundle(tmp_path)

    with pytest.raises(
        DistributionError,
        match=r"Python 3\.12.*Declared targets: Python 3\.11 on windows-x86_64",
    ):
        select_target(manifest, "3.12", "windows-x86_64")


def test_load_manifest_rejects_duplicate_json_keys(tmp_path):
    _write_bundle(tmp_path)
    document = (tmp_path / "bundle.json").read_text(encoding="utf-8")
    document = document.replace(
        '"kind": "SiteOpsBundle",',
        '"kind": "SiteOpsBundle", "kind": "SiteOpsBundle",',
        1,
    )
    (tmp_path / "bundle.json").write_text(document, encoding="utf-8")

    with pytest.raises(DistributionError, match="duplicate JSON key: kind"):
        load_manifest(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.whl",
        "wheels\\package.whl",
        "C:package.whl",
        "wheels//package.whl",
        "wheels/../package.whl",
        "wheels/CON.txt",
        "wheels/package.whl.",
        "wheels/package.whl ",
        "wheels/e\u0301.whl",
    ],
)
def test_manifest_rejects_nonportable_paths(tmp_path, path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["files"].append(
        {"path": path, "sha256": "0" * 64, "size": 0},
    )

    with pytest.raises(DistributionError):
        BundleManifest.from_dict(document)


def test_manifest_accepts_valid_neighbors_of_reserved_names(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["files"].extend(
        [
            {"path": "data/COM10.txt", "sha256": "0" * 64, "size": 0},
            {"path": "data/name..txt", "sha256": "1" * 64, "size": 0},
        ]
    )

    parsed = BundleManifest.from_dict(document)

    assert parsed.files[-2].path == "data/COM10.txt"
    assert parsed.files[-1].path == "data/name..txt"


def test_manifest_rejects_case_collisions(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["files"].extend(
        [
            {"path": "data/Name.txt", "sha256": "0" * 64, "size": 0},
            {"path": "data/name.txt", "sha256": "1" * 64, "size": 0},
        ]
    )

    with pytest.raises(DistributionError, match="case collisions"):
        BundleManifest.from_dict(document)


def test_manifest_rejects_boolean_in_integer_fields(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["build"]["number"] = True

    with pytest.raises(DistributionError, match="build.number"):
        BundleManifest.from_dict(document)


def test_manifest_rejects_extra_shape_fields(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["package"]["approval"] = "trusted"

    with pytest.raises(DistributionError, match="package must contain exactly"):
        BundleManifest.from_dict(document)


def test_manifest_rejects_excessive_file_count(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["files"].extend(
        {
            "path": f"data/file-{index}.txt",
            "sha256": f"{index:064x}",
            "size": 0,
        }
        for index in range(1024)
    )

    with pytest.raises(DistributionError, match="unsupported item count"):
        BundleManifest.from_dict(document)


def test_manifest_requires_application_wheel_in_every_target(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["targets"][0]["wheels"] = [
        "wheels/dependency-1.0-cp311-cp311-win_amd64.whl"
    ]

    with pytest.raises(DistributionError, match="include the application wheel"):
        BundleManifest.from_dict(document)


@pytest.mark.parametrize("required", ["ThirdPartyNotices.txt", "pylock.toml"])
def test_manifest_requires_all_named_payloads(tmp_path, required):
    manifest = _write_bundle(tmp_path)
    document = manifest.to_dict()
    document["files"] = [
        entry for entry in document["files"] if entry["path"] != required
    ]

    with pytest.raises(DistributionError, match=required):
        BundleManifest.from_dict(document)


@pytest.mark.parametrize("change", ["tamper", "missing", "unexpected"])
def test_verify_payload_rejects_inventory_and_content_changes(tmp_path, change):
    manifest = _write_bundle(tmp_path)
    if change == "tamper":
        (tmp_path / "LICENSE").write_bytes(b"changed\n")
    elif change == "missing":
        (tmp_path / "LICENSE").unlink()
    else:
        (tmp_path / "workspace.yaml").write_text("private workspace\n", encoding="utf-8")

    with pytest.raises(DistributionError):
        verify_payload(tmp_path, manifest)


def test_verify_payload_rejects_hard_links(tmp_path):
    manifest = _write_bundle(tmp_path)
    license_path = tmp_path / "LICENSE"
    license_path.unlink()
    try:
        os.link(tmp_path / "ThirdPartyNotices.txt", license_path)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    with pytest.raises(DistributionError, match="linked"):
        verify_payload(tmp_path, manifest)


def test_verify_payload_revalidates_directly_constructed_manifest(tmp_path):
    manifest = _write_bundle(tmp_path)
    invalid = BundleManifest(
        version=manifest.version,
        base_version=manifest.base_version,
        repository=manifest.repository,
        source_sha=manifest.source_sha,
        source_ref=manifest.source_ref,
        build_number=manifest.build_number,
        build_attempt=manifest.build_attempt,
        application_wheel=manifest.application_wheel,
        targets=manifest.targets,
        files=(
            PayloadFile(path="../outside", sha256="0" * 64, size=0),
            *manifest.files,
        ),
    )

    with pytest.raises(DistributionError, match="unsafe path component"):
        verify_payload(tmp_path, invalid)


def test_verify_payload_rejects_symbolic_links_when_supported(tmp_path):
    manifest = _write_bundle(tmp_path)
    license_path = tmp_path / "LICENSE"
    license_path.unlink()
    try:
        license_path.symlink_to(tmp_path / "ThirdPartyNotices.txt")
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable: {error}")

    with pytest.raises(DistributionError, match="links"):
        verify_payload(tmp_path, manifest)


def test_load_manifest_rejects_linked_metadata(tmp_path):
    _write_bundle(tmp_path)
    original = tmp_path / "bundle-original.json"
    (tmp_path / "bundle.json").replace(original)
    try:
        os.link(original, tmp_path / "bundle.json")
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    with pytest.raises(DistributionError, match="must not be linked"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_oversized_metadata(tmp_path):
    (tmp_path / "bundle.json").write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(DistributionError, match="too large"):
        load_manifest(tmp_path)


def test_from_dict_does_not_retain_mutable_input(tmp_path):
    manifest = _write_bundle(tmp_path)
    document = copy.deepcopy(manifest.to_dict())
    parsed = BundleManifest.from_dict(document)
    document["targets"][0]["wheels"].clear()
    document["files"].clear()

    assert parsed == manifest

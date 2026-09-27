"""Exercise the frozen inventory independently of provider calls and publication."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from siteops_release_assets import (  # noqa: E402
    MAX_ASSET_BYTES,
    MAX_ASSETS,
    MAX_INVENTORY_BYTES,
    FrozenReleaseAssets,
    ReferencedEngine,
    ReleaseAsset,
    ReleaseAssetsError,
    native_engine_wheel,
)


@pytest.fixture
def engine_assets():
    return tuple(
        ReleaseAsset(name, number + 1, str(number) * 64)
        for number, name in enumerate((
            "siteops-install.zip", "siteops-install.zip.attestation.jsonl",
            "siteops-1.2.3-py3-none-any.whl", "siteops-1.2.3-py3-none-any.whl.attestation.jsonl",
        ))
    )


@pytest.fixture
def reference(engine_assets):
    return ReferencedEngine("71", "siteops/v1.2.3", "d" * 40, engine_assets)


@pytest.fixture
def inventory(engine_assets):
    return FrozenReleaseAssets(
        "example/releases", "c" * 40, "refs/heads/feat/release", engine_assets,
    )


def test_frozen_inventory_round_trips_without_mutable_document_aliases(inventory):
    raw = inventory.serialized()
    assert FrozenReleaseAssets.from_bytes(raw) == inventory
    assert inventory.serialized() == raw
    document = inventory.document()
    document["assets"][0]["size"] = 999
    assert inventory.assets[0].size == 1


def test_content_assets_and_existing_engine_have_separate_inventories(inventory, reference):
    published = (
        ReleaseAsset("workspace.zip", 10, "a" * 64),
        ReleaseAsset("workspace.zip.attestation.jsonl", 11, "b" * 64),
        ReleaseAsset("siteops-workspaces.json", 12, "c" * 64),
    )
    combined = replace(inventory, assets=published, engine=reference)
    document = combined.document()
    assert [asset["name"] for asset in document["assets"]] == [
        asset.name for asset in published
    ]
    assert document["engine"]["assets"] == [asset.document() for asset in reference.assets]
    assert document["source"]["commit"] != document["engine"]["target"]
    assert FrozenReleaseAssets.from_bytes(combined.serialized()) == combined
    reference_only = replace(combined, assets=())
    assert reference_only.document()["assets"] == []
    assert FrozenReleaseAssets.from_bytes(reference_only.serialized()) == reference_only


def test_filenames_can_repeat_in_distinct_release_inventories(inventory, reference):
    value = replace(inventory, engine=reference)
    assert FrozenReleaseAssets.from_bytes(value.serialized()) == value


@pytest.mark.parametrize("name", [
    "", "../file.zip", "folder/file.zip", r"folder\file.zip", "-file.zip",
    "asset.zip#label", "asset.zip?", "asset.zip\n", "name.", "CON", "com1.zip",
    "LPT9.proof", "a" * 256, "\u00e9.zip",
])
def test_publication_names_cannot_be_paths_labels_or_platform_aliases(name):
    with pytest.raises(ReleaseAssetsError):
        ReleaseAsset(name, 1, "a" * 64)


@pytest.mark.parametrize("name", [
    "a.zip", "siteops-1.0.0b1+build.42.1.gcccccccccccc-py3-none-any.whl",
    "component.v1~build.zip", "console.zip", "a" * 255,
])
def test_portable_neighboring_names_remain_valid(name):
    assert ReleaseAsset(name, 1, "a" * 64).name == name


@pytest.mark.parametrize("size", [None, True, False, 0, -1, 1.5, "1", MAX_ASSET_BYTES + 1])
def test_asset_size_requires_a_positive_bounded_integer(size):
    with pytest.raises(ReleaseAssetsError):
        ReleaseAsset("workspace.zip", size, "a" * 64)


def test_asset_size_accepts_the_exact_bound():
    assert ReleaseAsset("workspace.zip", MAX_ASSET_BYTES, "a" * 64).size == MAX_ASSET_BYTES


def test_publication_inventory_accepts_the_exact_count_bound(inventory):
    assets = tuple(
        ReleaseAsset(f"file-{number}.zip", 1, "a" * 64) for number in range(MAX_ASSETS)
    )
    value = replace(inventory, assets=assets)
    assert FrozenReleaseAssets.from_bytes(value.serialized()) == value


@pytest.mark.parametrize("digest", [None, "A" * 64, "a" * 63, "g" * 64])
def test_asset_digest_is_an_exact_lowercase_sha256(digest):
    with pytest.raises(ReleaseAssetsError):
        ReleaseAsset("workspace.zip", 1, digest)


@pytest.mark.parametrize("fault", ["duplicate", "case", "too-many", "empty"])
def test_publication_inventory_is_bounded_and_unambiguous(inventory, fault):
    assets = inventory.assets
    if fault == "duplicate":
        assets += (assets[0],)
    elif fault == "case":
        assets += (replace(assets[0], name=assets[0].name.upper()),)
    elif fault == "too-many":
        assets = tuple(
            ReleaseAsset(f"file-{number}.zip", 1, "a" * 64)
            for number in range(MAX_ASSETS + 1)
        )
    else:
        assets = ()
    with pytest.raises(ReleaseAssetsError):
        replace(inventory, assets=assets)


@pytest.mark.parametrize("field,value", [
    ("repository", "../publisher"), ("repository", "example/release."),
    ("commit", "c" * 39), ("source_ref", "main"), ("source_ref", "refs/heads/../main"),
    ("source_ref", "refs/heads/a//b"), ("source_ref", "refs/heads/main.lock"),
])
def test_inventory_requires_candidate_source_identity(inventory, field, value):
    with pytest.raises(ReleaseAssetsError):
        replace(inventory, **{field: value})


@pytest.mark.parametrize("field,value", [
    ("release_id", "0"), ("release_id", True), ("tag", "v1.2.3"),
    ("target", "main"), ("assets", ()),
])
def test_referenced_engine_requires_its_own_complete_identity(reference, field, value):
    with pytest.raises(ReleaseAssetsError):
        replace(reference, **{field: value})


def test_native_engine_keeps_both_subjects_and_both_proofs(engine_assets):
    assert native_engine_wheel(engine_assets) == engine_assets[2]
    for index in range(4):
        with pytest.raises(ReleaseAssetsError):
            native_engine_wheel(engine_assets[:index] + engine_assets[index + 1:])
    with pytest.raises(ReleaseAssetsError):
        native_engine_wheel((*engine_assets, ReleaseAsset("extra.zip", 1, "a" * 64)))


def test_new_engine_requires_both_bootstrap_scripts_and_their_proofs(engine_assets):
    scripts = tuple(
        ReleaseAsset(name, 1, "a" * 64)
        for name in (
            "siteops-bootstrap.ps1", "siteops-bootstrap.ps1.attestation.jsonl",
            "siteops-bootstrap.sh", "siteops-bootstrap.sh.attestation.jsonl",
        )
    )
    assert native_engine_wheel((*engine_assets, *scripts), require_bootstrap=True) == engine_assets[2]
    with pytest.raises(ReleaseAssetsError):
        native_engine_wheel(engine_assets, require_bootstrap=True)
    for script in scripts:
        with pytest.raises(ReleaseAssetsError):
            native_engine_wheel(
                (*engine_assets, *(item for item in scripts if item != script)),
                require_bootstrap=True,
            )
    assert native_engine_wheel(engine_assets) == engine_assets[2]


@pytest.mark.parametrize("fault", ["root", "source", "asset", "version", "kind", "array", "missing"])
def test_inventory_rejects_unknown_or_incomplete_contracts(inventory, fault):
    document = inventory.document()
    if fault == "root":
        document["mode"] = "publish"
    elif fault == "source":
        document["source"]["trustPolicy"] = "package-policy"
    elif fault == "asset":
        document["assets"][0]["url"] = "https://example.invalid/asset"
    elif fault == "version":
        document["apiVersion"] = "siteops.release.assets/v1"
    elif fault == "kind":
        document["kind"] = "WorkspaceReleaseAssets"
    elif fault == "array":
        document["assets"] = {}
    else:
        del document["engine"]
    with pytest.raises(ReleaseAssetsError):
        FrozenReleaseAssets.from_bytes(json.dumps(document).encode())


@pytest.mark.parametrize("raw", [
    b"\xff", b"{", b'{"assets":[],"assets":[]}', b'{"size":NaN}',
    b'{"size":Infinity}', b"[" * 2000 + b"]" * 2000,
])
def test_inventory_rejects_malformed_duplicate_or_non_json_input(raw):
    with pytest.raises(ReleaseAssetsError):
        FrozenReleaseAssets.from_bytes(raw)


def test_inventory_read_enforces_byte_limit_before_parsing(tmp_path, inventory):
    path = tmp_path / "release-assets.json"
    path.write_bytes(inventory.serialized())
    assert FrozenReleaseAssets.read(path) == inventory
    path.write_bytes(b" " * (MAX_INVENTORY_BYTES + 1))
    with pytest.raises(ReleaseAssetsError, match="byte limit"):
        FrozenReleaseAssets.read(path)

"""Stage exactly the qualified publication roles without rebuilding any bytes."""

import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from siteops_release_assets import (  # noqa: E402
    FrozenReleaseAssets,
    ReferencedEngine,
    ReleaseAsset,
    ReleaseAssetsError,
    publication_assets,
)


@pytest.fixture
def stage():
    spec = importlib.util.spec_from_file_location("payload_stager_test", ROOT / "scripts/stage-release-payload.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.stage


def identity(root, name, raw):
    (root / name).write_bytes(raw)
    return ReleaseAsset(name, len(raw), hashlib.sha256(raw).hexdigest())


@pytest.fixture
def inputs(tmp_path):
    native_dir, workspace_dir = tmp_path / "engine", tmp_path / "workspaces"
    native_dir.mkdir()
    workspace_dir.mkdir()
    source = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
    native_assets = tuple(identity(native_dir, name, name.encode()) for name in (
        "siteops-install.zip", "siteops-install.zip.attestation.jsonl",
        "siteops-1.2.3-py3-none-any.whl", "siteops-1.2.3-py3-none-any.whl.attestation.jsonl",
        "siteops-bootstrap.ps1", "siteops-bootstrap.ps1.attestation.jsonl",
        "siteops-bootstrap.sh", "siteops-bootstrap.sh.attestation.jsonl",
    ))
    workspaces = tuple(identity(workspace_dir, name, name.encode()) for name in (
        "workspace.zip", "workspace.zip.attestation.jsonl", "siteops-workspaces.json",
    ))
    native = FrozenReleaseAssets(source["repository"], source["commit"], source["ref"], native_assets)
    workspace = replace(native, assets=workspaces)
    (workspace_dir / "release-assets.json").write_bytes(workspace.serialized())
    plan = {
        "source": source, "siteops": {"bundle": True, "releaseTag": None},
        "workspaces": [{"workspace": "workspace", "package": "workspace.zip", "id": "fixture"}],
    }
    selected = {
        "candidate": source, "planSha256": "c" * 64, "native": native.document(),
        "version": "1.2.3", "reference": None,
    }
    path = tmp_path / "engine-selection.json"
    path.write_text(json.dumps(selected))
    return {
        "plan": plan, "native": native, "native_dir": native_dir, "workspace": workspace,
        "workspace_dir": workspace_dir, "selected": path, "output": tmp_path / "payload",
    }


def invoke(stage, inputs):
    return stage(
        inputs["plan"], "c" * 64, inputs["native"], inputs["output"],
        engine_directory=inputs["native_dir"], workspace_directory=inputs["workspace_dir"],
        workspace_sha=hashlib.sha256(inputs["workspace"].serialized()).hexdigest(),
        selected_engine=inputs["selected"],
        selected_engine_sha=hashlib.sha256(inputs["selected"].read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("built", [False, True])
def test_complete_payload_preserves_roles_and_exact_bytes(stage, inputs, built):
    if not built:
        native = inputs["native"]
        reference = ReferencedEngine("71", "siteops/v1.2.3", "b" * 40, native.assets)
        inputs["native"] = replace(native, assets=(), engine=reference)
        inputs["plan"]["siteops"] = {"bundle": False, "releaseTag": reference.tag}
        selected = json.loads(inputs["selected"].read_bytes())
        selected["reference"] = reference.document()
        inputs["selected"].write_text(json.dumps(selected))
    result = invoke(stage, inputs)
    native, workspace = publication_assets(inputs["plan"], result)
    assert len(native) == (8 if built else 0) and len(workspace) == 3
    assert {path.name for path in inputs["output"].iterdir()} == {asset.name for asset in result.assets}
    for asset in result.assets:
        assert hashlib.sha256((inputs["output"] / asset.name).read_bytes()).hexdigest() == asset.sha256
    assert result.engine is inputs["native"].engine


@pytest.mark.parametrize("fault", ["candidate", "engine-bytes", "plan", "extra-asset", "missing-proof", "changed-payload"])
def test_publication_refuses_changed_qualified_inputs(stage, inputs, fault):
    if fault in {"candidate", "engine-bytes", "plan"}:
        selected = json.loads(inputs["selected"].read_bytes())
        if fault == "candidate":
            selected["candidate"]["commit"] = "d" * 40
        elif fault == "engine-bytes":
            selected["native"]["assets"][0]["sha256"] = "d" * 64
        else:
            selected["planSha256"] = "d" * 64
        inputs["selected"].write_text(json.dumps(selected))
    elif fault in {"extra-asset", "missing-proof"}:
        assets = inputs["workspace"].assets
        assets = (*assets, identity(inputs["workspace_dir"], "extra.txt", b"unapproved")) if fault == "extra-asset" else assets[:-2] + assets[-1:]
        inputs["workspace"] = replace(inputs["workspace"], assets=assets)
        (inputs["workspace_dir"] / "release-assets.json").write_bytes(inputs["workspace"].serialized())
    else:
        path = inputs["workspace_dir"] / "workspace.zip"
        path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(ReleaseAssetsError):
        invoke(stage, inputs)
    assert not inputs["output"].exists()


def test_engine_only_and_reference_only_keep_existing_publication_behavior(stage, inputs, tmp_path):
    plan = {key: value for key, value in inputs["plan"].items() if key != "workspaces"}
    result = stage(plan, "c" * 64, inputs["native"], inputs["output"], engine_directory=inputs["native_dir"])
    assert result == inputs["native"]
    reference = ReferencedEngine("71", "siteops/v1.2.3", "b" * 40, result.assets)
    plan["siteops"] = {"bundle": False, "releaseTag": reference.tag}
    referenced = replace(result, assets=(), engine=reference)
    empty = tmp_path / "reference"
    assert stage(plan, "c" * 64, referenced, empty, engine_directory=None) == referenced
    assert list(empty.iterdir()) == []

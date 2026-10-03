"""Run the qualification probe through an installed real wheel, outside the checkout."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from siteops import __version__
from siteops.workspace_source import ArtifactIdentity, WorkspaceReleaseAssets, WorkspaceReleaseEntry
from tests.installed_runtime import build_engine_wheel, install_engine

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe-installed-workspaces.py"


@pytest.fixture(scope="module")
def application(tmp_path_factory):
    root = tmp_path_factory.mktemp("qualified-engine")
    wheel = build_engine_wheel(root)
    app = install_engine(root / "runtime", wheel)
    result = subprocess.run(
        [str(app.python), "-I", str(ROOT / "tests" / "fixtures" / "prepare_installed_project.py"), str(app.root)],
        cwd=app.root / "unrelated", env=app.environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    identities = json.loads(result.stdout)
    assets = app.root / "verified-assets"
    assets.mkdir()
    for name in ("workspace.zip", "proof.jsonl"):
        shutil.copyfile(app.root / name, assets / name)
    entry = WorkspaceReleaseEntry(
        "workspace", "example.storage", "7",
        ArtifactIdentity("workspace.zip", (assets / "workspace.zip").stat().st_size, identities["package"]),
        ArtifactIdentity("proof.jsonl", (assets / "proof.jsonl").stat().st_size, identities["proof"]),
    )
    descriptor = WorkspaceReleaseAssets("a" * 40, (entry,)).serialized()
    (assets / "siteops-workspaces.json").write_bytes(descriptor)
    (app.root / "unrelated" / "siteops.py").write_text("raise AssertionError('Imported cwd engine')\n")
    return app, assets, descriptor


def invoke(
    application, name, *, version=__version__, isolated=True, wrong_spec=False,
    project_workspace=None, assets_override=None, descriptor_override=None,
):
    app, assets, descriptor = application
    if assets_override is not None:
        assets = assets_override
    if descriptor_override is not None:
        descriptor = descriptor_override
    spec = {
        "engineVersion": version, "assets": str(assets), "state": str(app.root / name),
        "source": {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main", "release": "release-7"},
        "descriptor": {"name": "siteops-workspaces.json", "size": len(descriptor), "sha256": hashlib.sha256(descriptor).hexdigest()},
        "policy": str(app.root / "policy.json"), "trustedRoot": str(app.root / "trusted-root.json"),
        "workspaceInventorySha256": "c" * 64,
    }
    if project_workspace is not None:
        spec["projectWorkspace"] = project_workspace
    raw = json.dumps(spec).encode()
    path = app.root / (name + ".json")
    path.write_bytes(raw)
    args = [str(app.python), *(["-I"] if isolated else []), str(PROBE),
            "--spec", str(path), "--expected-spec-sha", "d" * 64 if wrong_spec else hashlib.sha256(raw).hexdigest()]
    return subprocess.run(
        args, cwd=app.root / "unrelated",
        env={**app.environment, "PYTHONPATH": str(ROOT)}, capture_output=True,
        text=True, timeout=120,
    )


def test_probe_consumes_frozen_content_with_the_actual_installed_engine(application):
    result = invoke(application, "qualified")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["engineVersion"] == __version__
    assert report["packages"] == 1 and report["catalogManifests"] == 1
    assert report["workspaceInventorySha256"] == "c" * 64
    assert report["deployment"] == "not-run" and report["workloadHealth"] == "not-checked"
    assert "guarded-catalog-load" in report["checks"]
    app, _, _ = application
    assert list((app.root / "qualified" / "operator" / "sites").iterdir()) == []


@pytest.mark.parametrize("fault", ["version", "isolation", "spec"])
def test_probe_rejects_wrong_version_or_checkout_fallback(application, fault):
    result = invoke(
        application, "rejected-" + fault,
        version="99.0.0" if fault == "version" else __version__,
        isolated=fault != "isolation", wrong_spec=fault == "spec",
    )
    assert result.returncode != 0
    assert not result.stdout
    assert not (application[0].root / ("rejected-" + fault)).exists()


def test_installed_probe_creates_a_project_usable_by_fresh_offline_commands(application):
    app, assets, _ = application
    copied = app.root / "project-seed-assets"
    shutil.copytree(assets, copied)
    result = invoke(application, "candidate-project", project_workspace="workspace", assets_override=copied)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    state = app.root / "candidate-project"
    project = state / "operator"
    pin = project / "siteops.pin"
    assert report["project"] == {
        "workspace": "workspace", "pinSha256": hashlib.sha256(pin.read_bytes()).hexdigest(),
        "sourceReleaseObservation": "not-performed",
    }
    selection = json.loads(pin.read_text())
    assert selection["source"]["release"] == "release-7"
    assert selection["source"]["revision"] == "a" * 40
    assert selection["content"]["package"]["sha256"] == json.loads(
        (app.root / "tool-context.json").read_text(),
    )["digest"]
    shutil.rmtree(copied)
    (project / "sites" / "one.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: one\nsubscription: fixture-subscription\n"
        "resourceGroup: fixture-group\nlocation: eastus\n",
        encoding="utf-8",
    )
    original = pin.read_bytes()
    command = [
        str(app.command), "--project", str(project),
        "--trust-policy", str(app.root / "policy.json"), "--trusted-root", str(app.root / "trusted-root.json"),
    ]
    environment = {**app.environment, "SITEOPS_CACHE_DIR": str(state / "cache")}
    for operation in (
        ["browse", "storage", "--offline-content", "--output", "json"],
        ["plan", "storage", "--offline-content", "--output", "json"],
    ):
        consumed = subprocess.run(
            [*command, *operation], cwd=app.root / "unrelated", env=environment,
            capture_output=True, text=True, timeout=120,
        )
        assert consumed.returncode == 0, consumed.stdout + consumed.stderr
        document = json.loads(consumed.stdout)
        if operation[0] == "browse":
            assert document["source"]["verification"] == "verified"
        else:
            assert document["status"] == "planned" and document["intent"] == "executable"
            assert [target["name"] for target in document["plan"]["targets"]] == ["one"]
    assert pin.read_bytes() == original


@pytest.mark.parametrize("workspace", ["missing", "", 7])
def test_candidate_project_requires_a_declared_workspace_before_creating_state(application, workspace):
    name = f"bad-project-{workspace}"
    result = invoke(application, name, project_workspace=workspace)
    assert result.returncode != 0
    assert not result.stdout
    assert not (application[0].root / name).exists()


def test_candidate_project_is_not_published_when_payload_verification_fails(application):
    app, assets, _ = application
    copied = app.root / "changed-project-assets"
    shutil.copytree(assets, copied)
    (copied / "workspace.zip").write_bytes(b"not the selected payload")
    result = invoke(application, "refused-project", project_workspace="workspace", assets_override=copied)
    assert result.returncode != 0
    assert not result.stdout
    assert not (app.root / "refused-project" / "operator" / "siteops.pin").exists()


def test_candidate_project_waits_until_all_declared_packages_are_checked(application):
    app, assets, descriptor = application
    copied = app.root / "incomplete-project-assets"
    shutil.copytree(assets, copied)
    original = WorkspaceReleaseAssets.from_bytes(descriptor).workspaces[0]
    shutil.copyfile(copied / "workspace.zip", copied / "second.zip")
    shutil.copyfile(copied / "proof.jsonl", copied / "second-proof.jsonl")
    second = WorkspaceReleaseEntry(
        "z-other", original.kit_id, original.kit_version,
        ArtifactIdentity("second.zip", original.package.size, original.package.sha256),
        ArtifactIdentity("second-proof.jsonl", original.proof.size, original.proof.sha256),
    )
    descriptor = WorkspaceReleaseAssets("a" * 40, (original, second)).serialized()
    (copied / "siteops-workspaces.json").write_bytes(descriptor)
    result = invoke(
        application, "incomplete-project", project_workspace="workspace",
        assets_override=copied, descriptor_override=descriptor,
    )
    assert result.returncode != 0
    assert not result.stdout
    assert not (app.root / "incomplete-project" / "operator" / "siteops.pin").exists()

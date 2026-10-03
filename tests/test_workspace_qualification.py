"""Exercise the qualification controller with real wheels and closed local proof tools."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

from siteops import __version__
from tests.installed_runtime import build_engine_wheel, install_engine
from tests.native_bundle import _backend_wheelhouse, pinned_backend
from tests.release_helpers import ROOT, SCRIPTS
from tests.workspace_acquisition_helpers import make_source

sys.path.insert(0, str(SCRIPTS))

from siteops_distribution import BundleManifest, BundleTarget, PayloadFile  # noqa: E402
from siteops_release_assets import FrozenReleaseAssets, ReferencedEngine, ReleaseAsset  # noqa: E402
from workspace_engine import EngineSelection  # noqa: E402


def checked(argv, *, root, env=None, expected=0):
    result = subprocess.run(
        list(map(str, argv)), cwd=root, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def qualification_inputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("workspace-qualification")
    wheel = build_engine_wheel(root)
    tools = install_engine(root / "native-tools", wheel)
    root = tools.root
    wheels = tools.runtime_wheelhouse
    backend = _backend_wheelhouse(root / "backend")
    tooling = root / "tooling"
    checked([sys.executable, "-m", "venv", "--without-pip", tooling], root=root, env=tools.environment)
    python = tooling / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version, expected = pinned_backend()
    pip_wheel = backend / f"pip-{version}-py3-none-any.whl"
    assert hashlib.sha256(pip_wheel.read_bytes()).hexdigest() == expected
    checked([
        sys.executable, "-m", "pip", "--python", python, "install", "--no-index", "--no-deps", pip_wheel,
    ], root=root, env=tools.environment)
    checked([
        python, "-m", "pip", "install", "--no-index", "--only-binary=:all:", "--require-hashes",
        "--find-links", wheels, "-r", ROOT / "scripts/siteops-runtime-requirements.txt",
    ], root=root, env=tools.environment)
    bundle = root / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    shutil.copyfile(wheel, bundle / "wheels" / wheel.name)
    for dependency in wheels.glob("*.whl"):
        shutil.copyfile(dependency, bundle / "wheels" / dependency.name)
    platform = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    target = BundleTarget(
        f"{sys.version_info.major}.{sys.version_info.minor}", platform,
        tuple("wheels/" + path.name for path in sorted((bundle / "wheels").iterdir())),
    )
    spec = importlib.util.spec_from_file_location("qualification_fixture_builder", SCRIPTS / "build-siteops-bundle.py")
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)
    builder._write_pylock(bundle, (target,))
    for name in ("LICENSE", "ThirdPartyNotices.txt"):
        (bundle / name).write_bytes(b"Fixture notices")
    shutil.copyfile(SCRIPTS / "siteops_distribution.py", bundle / "siteops-install.py")
    files = tuple(
        PayloadFile(path.relative_to(bundle).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
        for path in sorted(bundle.rglob("*")) if path.is_file()
    )
    manifest = BundleManifest(
        __version__, __version__, "example/content", "b" * 40, "refs/heads/main",
        1, 1, "wheels/" + wheel.name, (target,), files,
    )
    manifest_bytes = (json.dumps(manifest.to_dict()) + "\n").encode()
    (bundle / "bundle.json").write_bytes(manifest_bytes)
    engine = root / "engine"
    engine.mkdir()
    archive = engine / "siteops-install.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(bundle).as_posix())
    shutil.copyfile(wheel, engine / wheel.name)
    for name in ("siteops-install.zip", wheel.name):
        (engine / (name + ".attestation.jsonl")).write_bytes(b"opaque engine proof")
    native_assets = tuple(
        ReleaseAsset(path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(engine.iterdir())
    )
    native = FrozenReleaseAssets("example/content", "b" * 40, "refs/heads/main", native_assets)
    candidate = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
    source = make_source(root / "source-fixture", revision="a" * 40)
    plan = root / "plan.json"
    plan.write_text(json.dumps({
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True,
        "dryRun": True, "source": candidate, "release": {"tag": "v1.0.0b7"},
        "workspaces": [{"workspace": source.source.entry.workspace}],
    }))
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    selection = EngineSelection(
        candidate, plan_sha, native, __version__, hashlib.sha256(manifest_bytes).hexdigest(),
        ((target.python, platform),), ReferencedEngine("71", "siteops/v" + __version__, "b" * 40, native_assets),
    )
    (engine / "workspace-engine.json").write_bytes(selection.serialized())
    workspaces = root / "workspaces"
    workspaces.mkdir()
    shutil.copyfile(source.archive, workspaces / source.archive.name)
    shutil.copyfile(source.proof, workspaces / source.proof.name)
    (workspaces / "siteops-workspaces.json").write_bytes(source.descriptor)
    workspace_assets = FrozenReleaseAssets(
        "example/content", "a" * 40, "refs/heads/main",
        tuple(ReleaseAsset(path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
              for path in sorted(workspaces.iterdir())),
    )
    (workspaces / "release-assets.json").write_bytes(workspace_assets.serialized())
    roots = root / "roots.json"
    roots.write_bytes(b"independent fixture roots")
    root_sha = hashlib.sha256(roots.read_bytes()).hexdigest()
    verifications = {}
    for asset in (next(value for value in native_assets if value.name == "siteops-install.zip"),
                  next(value for value in native_assets if value.name == wheel.name)):
        proof = engine / (asset.name + ".attestation.jsonl")
        verifications[asset.sha256] = {
            "digest": asset.sha256, "proof": hashlib.sha256(proof.read_bytes()).hexdigest(),
            "root": root_sha, "revision": "b" * 40,
            "signer": ".github/workflows/_siteops-distribution.yaml", "builder": ".github/workflows/release.yaml",
            "runner": "self-hosted",
        }
    verifications[source.source.entry.package.sha256] = {
        "digest": source.source.entry.package.sha256, "proof": source.source.entry.proof.sha256,
        "root": root_sha, "revision": "a" * 40,
        "signer": ".github/workflows/_workspace-distribution.yaml", "builder": ".github/workflows/ci.yaml",
        "runner": "self-hosted",
    }
    context = root / "tool-context.json"
    context.write_text(json.dumps({"verifications": verifications}))
    harness = root / "controller.py"
    harness.write_text("""
import importlib.util, os, sys
from pathlib import Path
source = Path(sys.argv.pop(1))
sys.path[:0] = [str(source), str(source / "scripts")]
spec = importlib.util.spec_from_file_location("qualified_controller", source / "scripts/qualify-workspace-engine.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module.application_environment
def fixture_environment(*args):
    return {**original(*args), "SITEOPS_TEST_TOOL_CONTEXT": os.environ["SITEOPS_TEST_TOOL_CONTEXT"]}
module.application_environment = fixture_environment
raise SystemExit(module.main())
""", encoding="utf-8")
    return {
        "root": root, "python": python, "engine": engine, "workspaces": workspaces,
        "roots": roots, "context": context, "harness": harness, "platform": platform,
        "engine_sha": hashlib.sha256(selection.serialized()).hexdigest(),
        "workspace_sha": hashlib.sha256(workspace_assets.serialized()).hexdigest(),
        "gh": tools.root / "tools" / ("gh.exe" if os.name == "nt" else "gh"),
        "environment": tools.environment,
        "plan": plan, "plan_sha": plan_sha, "workspace": source.source.entry.workspace,
    }


@pytest.mark.parametrize("rejected", [None, "engine", "workspace"])
@pytest.mark.parametrize("project", [False, True])
def test_controller_installs_authenticated_lock_then_uses_selected_engine(qualification_inputs, rejected, project):
    values = qualification_inputs
    name = (rejected or "valid") + ("-project" if project else "")
    report = values["root"] / (name + "-qualified.json")
    state = values["root"] / (name + "-qualification")
    context = values["context"]
    if rejected:
        document = json.loads(context.read_bytes())
        for observation in document["verifications"].values():
            if ("_siteops-distribution.yaml" in observation["signer"]) == (rejected == "engine"):
                observation["observedRunner"] = "github-hosted"
        context = values["root"] / (name + "-context.json")
        context.write_text(json.dumps(document), encoding="utf-8")
    result = checked([
        values["python"], "-I", values["harness"], ROOT,
        "--engine", values["engine"], "--expected-engine-selection-sha256", values["engine_sha"],
        "--workspaces", values["workspaces"], "--expected-workspace-inventory-sha256", values["workspace_sha"],
        "--trusted-root", values["roots"], "--expected-plan-sha", values["plan_sha"],
        "--builder-workflow", ".github/workflows/ci.yaml", "--platform", values["platform"],
        "--expected-runner-environment", "self-hosted",
        "--state", state, "--output", report, "--gh", values["gh"],
        *(["--plan", values["plan"], "--project-workspace", values["workspace"]] if project else []),
    ], root=values["root"], env={
        **values["environment"], "SITEOPS_TEST_TOOL_CONTEXT": str(context),
    }, expected=1 if rejected else 0)
    if rejected:
        assert not report.exists()
        if rejected == "engine":
            assert "certificate" in result.stderr and not (state / "application").exists()
        else:
            assert (state / "application").is_dir()
            assert "certificate" in (state / "probe.log").read_text(encoding="utf-8")
        return
    data = json.loads(report.read_bytes())
    assert data["engineVersion"] == __version__
    assert data["packages"] == 1 and data["catalogManifests"] == 1
    assert data["engineSelectionSha256"] == values["engine_sha"]
    assert data["workspaceInventorySha256"] == values["workspace_sha"]
    assert data["deployment"] == "not-run"
    assert "selected installed engine" in result.stdout
    if project:
        pin = state / "probe-state" / "operator" / "siteops.pin"
        assert data["project"] == {
            "workspace": values["workspace"],
            "pinSha256": hashlib.sha256(pin.read_bytes()).hexdigest(),
            "sourceReleaseObservation": "not-performed",
        }
        assert json.loads(pin.read_bytes())["source"]["release"] == "v1.0.0b7"

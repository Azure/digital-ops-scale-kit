"""Exercise the declaration-driven publisher through its actual workflow steps."""

import ast
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
import yaml

from tests.actions_expressions import job_runs
from tests.release_helpers import CLI, _commit, _write_record, _write_source_version
from tests.release_helpers import repository as repository
from tests.shell_helpers import bash_path, write_executable
from tests.shell_helpers import run_script as _run_script
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yaml").read_text())
CANDIDATE_WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "_release-candidate.yaml").read_text())
CI_WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
JOBS = {**CANDIDATE_WORKFLOW["jobs"], **WORKFLOW["jobs"]}
SHA = "c" * 40
REPO = "example/publisher"
ARCHIVE = "siteops-install.zip"
PROOF = ARCHIVE + ".attestation.jsonl"
WHEEL = "siteops-1.0.0b1+build.42.1.gcccccccccccc-py3-none-any.whl"
WHEEL_PROOF = WHEEL + ".attestation.jsonl"
BOOTSTRAP = (
    "siteops-bootstrap.ps1", "siteops-bootstrap.ps1.attestation.jsonl",
    "siteops-bootstrap.sh", "siteops-bootstrap.sh.attestation.jsonl",
)
ENGINE_ASSETS = (ARCHIVE, PROOF, WHEEL, WHEEL_PROOF, *BOOTSTRAP)
RENDERER = ROOT / "scripts" / "render-siteops-release.py"
ASSET_MODEL = ROOT / "scripts" / "siteops_release_assets.py"
PAYLOAD_TOOL = ROOT / "scripts" / "stage-release-payload.py"
VERIFIED = "<details><summary>Verify the script before it runs</summary>"
PROVENANCE = "<details><summary>Provenance, verification and maintenance</summary>"
SIGNER_WORKFLOW = ".github/workflows/_siteops-distribution.yaml"


def step(job, name):
    return next(item for item in JOBS[job]["steps"] if item.get("name") == name)


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture(scope="module")
def renderer():
    spec = importlib.util.spec_from_file_location("siteops_release_renderer", RENDERER)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as context:
        context.syspath_prepend(str(RENDERER.parent))
        spec.loader.exec_module(module)
    return module


def qualification_matrix(state="passed", **cells):
    """Return the summary rows the distribution workflow emits, with optional (python, column) overrides."""
    rows = []
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        limited = "n/a" if version != "3.11" else state
        row = {"python": version, "linux": state, "ubuntu-26.04": limited, "windows": state,
               "windows-standard-user": limited}
        row.update(cells.get(version, {}))
        rows.append(row)
    return rows


def summary_values(**overrides):
    return {
        "DRY_RUN": "false", "ENGINE_VERSION": "1.0.0b1+build.42",
        "CI_URL": f"https://github.com/{REPO}/actions/runs/42",
        "BUNDLE_SHA": "a" * 64, "WHEEL_NAME": WHEEL, "WHEEL_SHA": "b" * 64,
        "ASSET_LIST_SHA": "d" * 64, "ARCHIVE_NAME": ARCHIVE, "TAG_EXISTS": "false",
        "MATRIX": json.dumps(qualification_matrix()),
        "ARTIFACT_URL": f"https://github.com/{REPO}/actions/runs/42/artifacts/99",
        **overrides,
    }


@pytest.fixture
def candidate(tmp_path):
    directory = tmp_path / "temp"
    plan_dir = directory / "release-plan"
    plan_dir.mkdir(parents=True)
    bundle_dir = directory / "release-bundle"
    bundle_dir.mkdir()
    declaration = {"tag": "v1.0.0b8", "headline": "Release highlights", "siteops": {"build": True}}
    raw = json.dumps(declaration).encode()
    notes = b"## Changes\n\nReviewed release notes.\n"
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True, "dryRun": False,
        "source": {"repository": REPO, "commit": SHA, "ref": "refs/heads/main"},
        "intent": {
            "path": "releases/candidate/release.json", "sha256": digest(raw),
            "notesPath": "releases/candidate/notes.md", "notesSha256": digest(notes),
        },
        "release": {
            "stream": "scalekit", "components": "both", "tag": "v1.0.0b8", "version": "1.0.0b8",
            "title": "v1.0.0b8: Release highlights", "prerelease": True, "latest": False,
        },
        "siteops": {
            "bundle": True, "versionMode": "build", "baseVersion": "1.0.0b1", "releaseTag": None,
        },
    }
    manifest = {
        "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle",
        "source": plan["source"], "build": {"number": 42, "attempt": 1},
        "package": {
            "name": "siteops", "version": "1.0.0b1+build.42.1.gcccccccccccc",
            "wheel": "wheels/" + WHEEL,
        },
    }
    wheel_bytes = b"synthetic standalone wheel"
    with zipfile.ZipFile(bundle_dir / ARCHIVE, "w") as archive:
        archive.writestr("bundle.json", json.dumps(manifest))
        archive.writestr("wheels/" + WHEEL, wheel_bytes)
    (bundle_dir / WHEEL).write_bytes(wheel_bytes)
    (bundle_dir / PROOF).write_text("synthetic proof")
    (bundle_dir / WHEEL_PROOF).write_text("synthetic wheel proof")
    for name in BOOTSTRAP:
        (bundle_dir / name).write_text("synthetic " + name, encoding="utf-8")
    (plan_dir / "release-notes.md").write_bytes(notes)
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (directory / "release-notes").mkdir()
    (directory / "release-notes" / "publish-notes.md").write_bytes(notes)
    (directory / "release-assets").mkdir()
    assets = {
        "apiVersion": "siteops.release.assets/v2",
        "kind": "SiteOpsReleaseAssets",
        "source": plan["source"],
        "assets": [
            {"name": name, "size": (bundle_dir / name).stat().st_size,
             "sha256": digest((bundle_dir / name).read_bytes())}
            for name in ENGINE_ASSETS
        ],
        "engine": None,
    }
    (directory / "release-assets" / "release-assets.json").write_text(
        json.dumps(assets, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    responses = {}
    for revision in (SHA, "refs/heads/main"):
        for name, body in (("release.json", raw.decode()), ("notes.md", notes.decode())):
            responses[f"repos/{REPO}/contents/releases/candidate/{name}?ref={revision}"] = {
                "status": 200, "raw": body,
            }
    return {"root": directory, "plan": plan, "declaration": declaration, "responses": responses}


def _reference_inventory(candidate):
    path = candidate["root"] / "release-assets" / "release-assets.json"
    inventory = json.loads(path.read_text(encoding="utf-8"))
    inventory["engine"] = {
        "releaseId": "71", "tag": "siteops/v1.0.0", "target": "d" * 40,
        "assets": inventory["assets"][:4],
    }
    inventory["assets"] = []
    path.write_text(json.dumps(inventory), encoding="utf-8")


def _workspace_publication(candidate, tmp_path, *, built=True):
    from siteops_release_assets import engine_reference

    from siteops.package_builder import build_package
    from siteops.workspace_source import (
        ArtifactIdentity,
        WorkspaceReleaseAssets,
        WorkspaceReleaseEntry,
    )

    root = candidate["root"]
    if not built:
        _reference_inventory(candidate)
        candidate["plan"]["siteops"] = {
            "bundle": False, "versionMode": None, "baseVersion": None, "releaseTag": "siteops/v1.0.0",
        }
        candidate["plan"]["release"]["components"] = "content"
        candidate["declaration"]["siteops"] = {"release": "siteops/v1.0.0"}
    directory = root / "release-workspaces"
    directory.mkdir()
    source = tmp_path / "workspace-source"
    shutil.copytree(ROOT / "tests/fixtures/browse-workspace", source / "workspace")
    inspection = build_package(
        source, directory / "workspace.zip", workspace="workspace", kit_id="fixture.storage",
        version=candidate["plan"]["release"]["version"], source_revision=SHA,
        siteops_range=">=1.0.0b1,<2",
    )
    proof = b"opaque workspace proof"
    (directory / "workspace.zip.attestation.jsonl").write_bytes(proof)
    entry = WorkspaceReleaseEntry(
        "workspace", "fixture.storage", candidate["plan"]["release"]["version"],
        ArtifactIdentity("workspace.zip", inspection.size, inspection.sha256),
        ArtifactIdentity("workspace.zip.attestation.jsonl", len(proof), digest(proof)),
    )
    descriptor = WorkspaceReleaseAssets(SHA, (entry,)).serialized()
    (directory / "siteops-workspaces.json").write_bytes(descriptor)
    native = json.loads((root / "release-assets/release-assets.json").read_bytes())
    workspace = {
        **native, "engine": None,
        "assets": [entry.package.document(), entry.proof.document(), {
            "name": "siteops-workspaces.json", "size": len(descriptor), "sha256": digest(descriptor),
        }],
    }
    workspace_raw = json.dumps(workspace).encode()
    (directory / "release-assets.json").write_bytes(workspace_raw)
    request = {
        "workspace": "workspace", "id": "fixture.storage", "package": "workspace.zip",
        "compatibility": {"siteops": ">=1.0.0b1,<2"}, "licenses": ["LICENSE"],
    }
    candidate["plan"]["workspaces"] = candidate["declaration"]["workspaces"] = [request]
    declaration = json.dumps(candidate["declaration"])
    candidate["plan"]["intent"]["sha256"] = digest(declaration.encode())
    for revision in (SHA, "refs/heads/main"):
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref={revision}"]["raw"] = declaration
    plan_sha = digest(json.dumps(candidate["plan"]).encode())
    selected = {
        "candidate": native["source"], "planSha256": plan_sha,
        "native": {**native, "engine": None, "assets": native["assets"] if built else native["engine"]["assets"]},
        "version": "1.0.0b1+build.42.1.gcccccccccccc" if built else "1.0.0",
        "reference": native["engine"],
    }
    if not built:
        selected["native"]["source"] = {**native["source"], "commit": "e" * 40}
    selected_root = root / "release-engine-selection"
    selected_root.mkdir()
    selected_raw = json.dumps(selected).encode()
    (selected_root / "workspace-engine.json").write_bytes(selected_raw)
    reference_root = root / "release-engine-reference"
    reference_root.mkdir()
    reference_raw = engine_reference(candidate["plan"], selected).serialized()
    (reference_root / "siteops-engine.json").write_bytes(reference_raw)
    (reference_root / "siteops-engine.json.attestation.jsonl").write_bytes(b"synthetic engine reference proof")
    if not built:
        tag = urllib.parse.quote(native["engine"]["tag"], safe="")
        candidate["responses"][f"repos/{REPO}/releases/tags/{tag}"] = {
            "status": 200, "body": {
                "id": 71, "draft": False, "tag_name": native["engine"]["tag"],
                "assets": [{**asset, "digest": "sha256:" + asset["sha256"], "state": "uploaded"} for asset in native["engine"]["assets"]],
            },
        }
        candidate["responses"][f"repos/{REPO}/git/ref/tags/{tag}"] = {
            "status": 200, "body": {"object": {"sha": "d" * 40}},
        }
    return {"WORKSPACE_SHA": digest(workspace_raw), "ENGINE_SELECTION_SHA": digest(selected_raw),
            "APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "d" * 40,
            "ENGINE_REFERENCE_SHA": digest(reference_raw),
            "APPROVED_ENGINE_REVISION": selected["native"]["source"]["commit"],
            "APPROVED_ENGINE_VERSION": selected["version"]}


@pytest.fixture
def runner(tmp_path, candidate):
    rendering_source = tmp_path / "release-tools" / "scripts"
    rendering_source.mkdir(parents=True)
    (rendering_source / RENDERER.name).write_bytes(RENDERER.read_bytes())
    (rendering_source / ASSET_MODEL.name).write_bytes(ASSET_MODEL.read_bytes())
    (rendering_source / PAYLOAD_TOOL.name).write_bytes(PAYLOAD_TOOL.read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake-gh.py"
    fake.write_text(
        """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as output:
    output.write(json.dumps(args) + "\\n")
    if "--input" in args:
        output.write(json.dumps(["<input>", sys.stdin.read()]) + "\\n")
if args[:2] == ["attestation", "verify"]:
    expected = os.environ.get("EXPECTED_SIGNER_IDENTITY")
    if Path(args[2]).name == "siteops-engine.json":
        expected = "https://github.com/" + os.environ["GITHUB_REPOSITORY"] + "/.github/workflows/_release-candidate.yaml@" + os.environ["SOURCE_REF"]
    if Path(args[2]).name in json.loads(os.environ.get("WORKSPACE_SUBJECTS", "[]")):
        expected = "https://github.com/" + os.environ["GITHUB_REPOSITORY"] + "/.github/workflows/_workspace-distribution.yaml@" + os.environ["SOURCE_REF"]
    if expected and args[args.index("--cert-identity") + 1] != expected:
        raise SystemExit(9)
    failed_subject = os.environ.get("FAIL_ATTESTATION_SUBJECT")
    if failed_subject and Path(args[2]).name == failed_subject:
        raise SystemExit(9)
    print(json.dumps(json.loads(Path(os.environ["FAKE_VERIFICATIONS"]).read_text())[Path(args[2]).name]))
    raise SystemExit(int(os.environ.get("FAIL_ATTESTATION", "0")))
if args[0] == "release":
    raise SystemExit(int(os.environ.get("FAIL_RELEASE", "0")))
if args[0] != "api":
    raise SystemExit("Unsupported fake command.")
endpoint = next((value for value in args if value.startswith("repos/")), None)
responses = json.loads(Path(os.environ["FAKE_RESPONSES"]).read_text())
if endpoint not in responses:
    raise SystemExit("Unexpected fake GitHub request: " + str(endpoint))
response = responses[endpoint]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
if response.get("method", method) != method:
    raise SystemExit("Unexpected fake GitHub method: " + method)
if any(flag not in args for flag in response.get("flags", [])):
    raise SystemExit("A fake GitHub request lacks a required flag.")
status = response["status"]
if "--include" in args:
    print("HTTP/2.0 " + str(status) + (" Not Found" if status == 404 else " Result"))
    print()
if "raw" in response:
    sys.stdout.buffer.write(response["raw"].encode("utf-8"))
else:
    print(json.dumps(response.get("body", {})))
raise SystemExit(0 if 200 <= status < 300 else 1)
""",
        encoding="utf-8",
    )
    shim = tmp_path / "python-shim.py"
    shim.write_text(
        """import os, runpy, subprocess, sys, time
from pathlib import Path
native = subprocess.run
def isolated_run(command, *args, **kwargs):
    if command[0] != "gh":
        raise AssertionError("Unexpected subprocess in workflow fixture.")
    return native([sys.executable, os.environ["FAKE_PROGRAM"], *command[1:]], *args, **kwargs)
subprocess.run = isolated_run
if os.environ.get("FAST_CI_CLOCK"):
    ticks = iter([0, 2000, 4000])
    time.monotonic = lambda: next(ticks)
    time.sleep = lambda _: None
if sys.argv[1] == "-B":
    del sys.argv[1]
if sys.argv[1] == "-c":
    code = sys.argv[2]
    sys.argv = ["-c", *sys.argv[3:]]
    exec(compile(code, "<workflow>", "exec"), {"__name__": "__main__"})
else:
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    runpy.run_path(sys.argv[0], run_name="__main__")
""",
        encoding="utf-8",
    )
    write_executable(
        bin_dir / "gh",
        '#!/usr/bin/env bash\nexec "$FAKE_PYTHON" "$FAKE_PROGRAM" "$@"\n',
    )
    write_executable(
        bin_dir / "python3",
        '#!/usr/bin/env bash\nexec "$FAKE_PYTHON" "$FAKE_SHIM" "$@"\n',
    )
    responses = tmp_path / "responses.json"
    calls = tmp_path / "calls.jsonl"
    counter = 0

    def invoke(job, name, *, extra=None):
        nonlocal counter
        counter += 1
        (candidate["root"] / "release-plan" / "plan.json").write_text(
            json.dumps(candidate["plan"]), encoding="utf-8",
        )
        responses.write_text(json.dumps(candidate["responses"]), encoding="utf-8")
        output = tmp_path / f"output-{counter}.txt"
        native_root = candidate["root"] / candidate.get("native_directory", "release-bundle")
        wheel_paths = [
            path for path in native_root.iterdir()
            if path.name.endswith(".whl")
        ]
        wheel_path = wheel_paths[0] if len(wheel_paths) == 1 else native_root / WHEEL
        asset_document = json.loads(
            (candidate["root"] / "release-assets" / "release-assets.json").read_text()
        )
        if name == "Render the final release notes" and not (candidate["root"] / "final-release-assets").exists():
            (candidate["root"] / "final-release-assets").mkdir()
            (candidate["root"] / "final-release-assets" / "release-assets.json").write_text(
                json.dumps(asset_document), encoding="utf-8",
            )
        engine_assets = asset_document["engine"]["assets"] if asset_document["engine"] else asset_document["assets"]
        approved_wheel = next(item for item in engine_assets if item["name"].endswith(".whl"))
        environment = {
            "FAKE_PYTHON": Path(sys.executable).as_posix(),
            "FAKE_PROGRAM": fake.as_posix(), "FAKE_SHIM": shim.as_posix(),
            "FAKE_RESPONSES": responses.as_posix(), "FAKE_CALLS": calls.as_posix(),
            "GITHUB_OUTPUT": output.as_posix(),
            "GITHUB_STEP_SUMMARY": (tmp_path / "summary.md").as_posix(),
            "GITHUB_REPOSITORY": REPO, "SOURCE_SHA": candidate.get("source_sha", SHA),
            "SOURCE_REF": "refs/heads/main",
            "DRY_RUN": "false",
            "EXPECTED_RUNNER_ENVIRONMENT": "self-hosted",
            "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
            "PYTHONIOENCODING": WORKFLOW["env"]["PYTHONIOENCODING"],
            "RUNNER_TEMP": candidate["root"].as_posix(),
            "ARCHIVE_NAME": ARCHIVE, "ATTESTATION_SUFFIX": ".attestation.jsonl",
            "BUILD_NUMBER": "42", "BUILD_ATTEMPT": "1",
            "OIDC_ISSUER": "https://token.actions.githubusercontent.com",
            "PREDICATE_TYPE": "https://slsa.dev/provenance/v1",
            "BUILD_ARCHIVE_SHA": digest((native_root / ARCHIVE).read_bytes()),
            "BUILD_WHEEL_NAME": wheel_path.name,
            "BUILD_WHEEL_SHA": digest(wheel_path.read_bytes()),
            "WHEEL_NAME": wheel_path.name,
            "WHEEL_SHA": digest(wheel_path.read_bytes()),
            "ASSET_LIST_SHA": digest(
                (candidate["root"] / "release-assets" / "release-assets.json").read_bytes()
            ),
            "APPROVED_PLAN_SHA": digest((candidate["root"] / "release-plan" / "plan.json").read_bytes()),
            "EXPECTED_PLAN_SHA": digest((candidate["root"] / "release-plan" / "plan.json").read_bytes()),
            "EXPECTED_NATIVE_SHA": digest((candidate["root"] / "release-assets/release-assets.json").read_bytes()),
            "WORKSPACES": str(bool(candidate["plan"].get("workspaces"))).lower(),
            "BUNDLE": str(candidate["plan"]["siteops"]["bundle"]).lower(),
            "APPROVED_NOTES_SHA": digest((candidate["root"] / "release-notes" / "publish-notes.md").read_bytes()),
            "APPROVED_BUNDLE_SHA": digest((native_root / ARCHIVE).read_bytes()),
            "APPROVED_WHEEL_NAME": approved_wheel["name"],
            "APPROVED_WHEEL_SHA": approved_wheel["sha256"],
            "APPROVED_ASSET_LIST_SHA": digest(
                (candidate["root"] / "release-assets" / "release-assets.json").read_bytes()
            ),
            "TAG": candidate["plan"]["release"]["tag"],
        }
        environment.update(extra or {})
        signer_identity = f"https://github.com/{REPO}/.github/workflows/_siteops-distribution.yaml@{environment['SOURCE_REF']}"
        environment.setdefault("SIGNER_IDENTITY", signer_identity)
        environment.setdefault("EXPECTED_SIGNER_IDENTITY", signer_identity)
        builder = ".github/workflows/" + ("ci.yaml" if environment["DRY_RUN"] == "true" else "release.yaml")
        environment.setdefault("BUILDER_IDENTITY", f"https://github.com/{REPO}/{builder}@{environment['SOURCE_REF']}")
        packages = [request["package"] for request in candidate["plan"].get("workspaces", [])]
        environment["WORKSPACE_SUBJECTS"] = json.dumps(packages)
        observations = {}
        scripts = tuple(name for name in BOOTSTRAP[::2] if (native_root / name).is_file())
        for subject in (ARCHIVE, wheel_path.name, *scripts, *packages, *(("siteops-engine.json",) if packages else ())):
            signer = ".github/workflows/" + (
                "_release-candidate.yaml" if subject == "siteops-engine.json"
                else "_workspace-distribution.yaml" if subject in packages else "_siteops-distribution.yaml"
            )
            observation = verified_observation(
                REPO, environment["SOURCE_SHA"], environment["SOURCE_REF"], signer,
                builder,
            )
            observations[subject] = [observation]
            if environment.get("CERTIFICATE_FAULT"):
                changed = copy.deepcopy(observation)
                changed["verificationResult"]["signature"]["certificate"][environment["CERTIFICATE_FAULT"]] = "PRIVATE_WRONG"
                observations[subject].append(changed)
        evidence = tmp_path / "verifications.json"
        evidence.write_text(json.dumps(observations), encoding="utf-8")
        environment["FAKE_VERIFICATIONS"] = evidence.as_posix()
        result = _run_script(step(job, name)["run"], tmp_path, environment)
        values = dict(
            line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
        ) if output.exists() else {}
        recorded = [
            json.loads(line) for line in calls.read_text().splitlines()
        ] if calls.exists() else []
        return result, values, recorded

    return invoke


def test_publication_uses_only_the_completed_candidate_and_required_approval():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    for document in (WORKFLOW, CANDIDATE_WORKFLOW):
        assert document["env"]["BUILDER_IDENTITY"] == "https://github.com/${{ github.workflow_ref }}"
    assert CANDIDATE_WORKFLOW["env"]["EXPECTED_RUNNER_ENVIRONMENT"] == "self-hosted"
    assert "head_sha=" in step("review", "Require successful CI for the candidate")["run"]
    assert JOBS["distribution"]["uses"] == "./.github/workflows/_siteops-distribution.yaml"
    assert JOBS["distribution"]["with"]["version-mode"] == "${{ needs.prepare.outputs.version-mode }}"
    assert CANDIDATE_WORKFLOW[True]["workflow_call"]["outputs"]["wheel-name"]["value"] == (
        "${{ jobs.review.outputs.wheel-name }}"
    )
    assert CANDIDATE_WORKFLOW[True]["workflow_call"]["outputs"]["wheel-sha"]["value"] == (
        "${{ jobs.review.outputs.wheel-sha }}"
    )
    condition = " ".join(JOBS["review"]["if"].split())
    assert "needs.distribution.result == 'success'" in condition
    assert "needs.distribution.result == 'skipped'" in condition
    assert JOBS["publish"]["needs"] == ["release-runner", "candidate", "accept"]
    assert JOBS["candidate"]["uses"] == "./.github/workflows/_release-candidate.yaml"
    assert JOBS["candidate"]["with"]["dry-run"] is False
    assert JOBS["publish"]["environment"] == "siteops-release"
    assert JOBS["publish"]["permissions"] == {
        "contents": "write", "actions": "read", "attestations": "read",
    }
    for name in ("prepare", "review"):
        assert JOBS[name]["permissions"]["contents"] == "read"
    assert JOBS["publish"]["concurrency"]["cancel-in-progress"] is False


def test_shared_verification_runs_again_before_any_publication_write():
    names = [item["name"] for item in JOBS["publish"]["steps"]]
    assert names.index("Verify the approved candidate") < names.index("Create only the approved missing tag")
    assert names.index("Verify the approved release assets") < names.index("Create only the approved missing tag")
    assert names.index("Verify the approved workspace subjects and descriptor") < names.index("Create only the approved missing tag")
    assert names.index("Check the publication target") < names.index("Create only the approved missing tag")
    assert step("prepare", "Check the publication target")["run"] == step("publish", "Check the publication target")["run"]
    assert JOBS["distribution"]["needs"] == "prepare"
    assert step("prepare", "Check the publication target")["if"] == "steps.plan.outputs.active == 'true'"
    assert step("review", "Show the release approval preview")["env"]["TAG_EXISTS"] == "${{ needs.prepare.outputs.tag-exists }}"
    assert step("review", "Download the pinned declaration")["with"]["artifact-ids"] == "${{ needs.prepare.outputs.artifact-id }}"
    assert step("publish", "Download the qualified release assets")["with"]["artifact-ids"] == (
        "${{ needs.candidate.outputs.payload-artifact-id }}"
    )
    assert step("publish", "Download the frozen asset list")["with"]["artifact-ids"] == "${{ needs.candidate.outputs.assets-artifact-id }}"
    assert not any("checkout@" in item.get("uses", "") for item in JOBS["publish"]["steps"])


@pytest.mark.parametrize("job,name", [
    ("review", "Verify the pinned candidate"), ("publish", "Verify the approved release assets"),
])
@pytest.mark.parametrize("field", [
    "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
    "sourceRepositoryRef", "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
])
def test_candidate_and_publisher_reject_each_mismatched_verified_claim(runner, job, name, field):
    result, _, calls = runner(job, name, extra={"CERTIFICATE_FAULT": field})
    assert result.returncode != 0
    assert "certificate does not match release policy" in result.stdout + result.stderr
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr
    assert len([call for call in calls if call[:2] == ["attestation", "verify"]]) == 1
    assert not any("--method" in call or call[:2] == ["release", "create"] for call in calls)


@pytest.mark.parametrize("built", [False, True])
@pytest.mark.parametrize("fault", ["missing", "selection", "preview", "policy", "proof"])
def test_publisher_requires_the_exact_signed_engine_reference(candidate, runner, tmp_path, built, fault):
    extra = _workspace_publication(candidate, tmp_path, built=built)
    result, _, _ = runner("review", "Freeze the complete qualified publication payload", extra=extra)
    assert result.returncode == 0, result.stdout + result.stderr
    root = candidate["root"]
    (root / "release-bundle").rename(root / "review-native")
    candidate["native_directory"] = "review-native"
    shutil.copytree(root / "release-payload", root / "release-bundle")
    inventory_path = root / "release-assets/release-assets.json"
    inventory = json.loads((root / "final-release-assets/release-assets.json").read_bytes())
    path = root / "release-bundle/siteops-engine.json"
    if fault == "missing":
        inventory["assets"] = [row for row in inventory["assets"] if not row["name"].startswith("siteops-engine.json")]
    elif fault in {"selection", "preview", "policy"}:
        record = json.loads(path.read_bytes())
        if fault == "selection":
            record["engine"]["revision"] = "b" * 40
        elif fault == "preview":
            record["preview"] = True
        else:
            record["policy"] = {"allow": True}
        raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.write_bytes(raw)
        row = next(row for row in inventory["assets"] if row["name"] == path.name)
        row.update(size=len(raw), sha256=digest(raw))
    inventory_path.write_text(json.dumps(inventory))
    if fault == "proof":
        extra["FAIL_ATTESTATION_SUBJECT"] = "siteops-engine.json"
    result, _, calls = runner(
        "publish", "Verify the approved release assets" if fault == "proof" else "Verify the approved candidate",
        extra=extra,
    )
    assert result.returncode != 0
    if fault == "proof":
        assert any(call[:2] == ["attestation", "verify"] and Path(call[2]).name == "siteops-engine.json" for call in calls)
    assert not any("--method" in call or call[:2] == ["release", "create"] for call in calls)


def test_rendering_source_is_pinned_and_only_executed_in_read_only_preparation():
    assert WORKFLOW["env"]["PYTHONIOENCODING"] == CANDIDATE_WORKFLOW["env"]["PYTHONIOENCODING"] == "utf-8"
    checkout = step("review", "Checkout the exact rendering source")
    assert checkout["with"] == {
        "ref": "${{ github.sha }}", "path": "release-tools", "persist-credentials": False,
        "sparse-checkout": "scripts/render-siteops-release.py\nscripts/siteops_release_assets.py\nscripts/stage-release-payload.py\n",
        "sparse-checkout-cone-mode": False,
    }
    assert JOBS["review"]["permissions"] == {"contents": "read", "actions": "read"}
    names = [item["name"] for item in JOBS["review"]["steps"]]
    assert names.index("Verify the pinned candidate") < names.index(checkout["name"])
    assert names.index(checkout["name"]) < names.index("Render the final release notes")
    for name, mode in (("Render the final release notes", "notes"), ("Show the release approval preview", "summary")):
        assert f"python3 -B release-tools/scripts/render-siteops-release.py {mode}" in step("review", name)["run"]
    assert RENDERER.name not in yaml.safe_dump(JOBS["publish"])
    assert not any("scripts/" in item.get("run", "") for item in JOBS["publish"]["steps"])


def test_declared_record_events_do_not_create_a_release_on_an_ordinary_push():
    events = WORKFLOW[True]
    assert events["push"]["branches"] == ["main"]
    assert set(events["push"]["paths"]) == {"releases/**/release.json", "releases/**/notes.md"}
    assert set(events["workflow_dispatch"]["inputs"]) == {"release-file", "expected-source-sha"}
    assert WORKFLOW["name"] == "Release (approval required)"
    assert JOBS["candidate"]["with"]["intent"] == "${{ inputs.release-file || '' }}"
    assert JOBS["candidate"]["with"]["expected-source-sha"] == (
        "${{ github.event_name != 'workflow_dispatch' && github.sha || inputs.expected-source-sha }}"
    )
    assert "pull_request" not in events


def test_operator_guide_matches_the_visible_workflow_controls():
    guide = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    for name in ci[True]["workflow_dispatch"]["inputs"]:
        assert f"`{name}`" in guide
    for option in ci[True]["workflow_dispatch"]["inputs"]["run-mode"]["options"]:
        assert f"`{option}`" in guide
    assert WORKFLOW["name"] in guide
    assert "`release-intent`" not in guide and "`rehearsal`" not in guide
    assert not (ROOT / ".github" / "workflows" / "siteops-distribution.yaml").exists()
    assert ".github/release-examples/<name>/" in guide
    assert "releases/<name>/release.json" in guide


def _source_check_fixture(repository, tmp_path):
    scripts = repository / "scripts"
    scripts.mkdir()
    for name in (
        "prepare-siteops-release.py", "siteops_release.py",
        "siteops_release_assets.py", "workspace_release.py",
    ):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    _write_source_version(repository, '__version__ = "1.0.0b1"\n')
    for name in ("artifacts.py", "workspace_compatibility.py"):
        shutil.copyfile(ROOT / "siteops" / name, repository / "siteops" / name)
    before = _commit(repository, "source before release")
    (repository / "bin").mkdir()
    write_executable(
        repository / "bin" / "python",
        '#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n',
    )
    temporary = tmp_path / "runner"
    temporary.mkdir()
    exports = {
        "TEST_PYTHON": Path(sys.executable).as_posix(),
        "BEFORE_SHA": before, "SOURCE_REPOSITORY": REPO, "GITHUB_REPOSITORY": REPO,
        "SOURCE_REF": "refs/heads/main", "DRY_RUN": "true", "INTENT_PATH": "",
        "RUNNER_TEMP": temporary.as_posix(), "PYTHONDONTWRITEBYTECODE": "1",
        "GITHUB_OUTPUT": (tmp_path / "outputs.txt").as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "summary.md").as_posix(),
    }
    return temporary, exports


@pytest.mark.parametrize("valid", [False, True])
def test_ci_validates_changed_release_files_without_publishing(repository, tmp_path, valid):
    temporary, exports = _source_check_fixture(repository, tmp_path)
    _write_record(repository, {
        "tag": "v1.0.0b8" if valid else "v1.0.0", "siteops": {"build": True},
    })
    exports["SOURCE_SHA"] = _commit(repository, "release declaration")
    job = CI_WORKFLOW["jobs"]["lint"]
    check = next(item for item in job["steps"] if item["name"] == "Check changed release declarations")
    assert check["if"] == "github.event_name == 'pull_request'"
    assert check["env"]["BEFORE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    checkout = next(item for item in job["steps"] if item.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert job["permissions"] == {"contents": "read"}
    result = _run_script(check["run"], repository, exports)
    if valid:
        assert result.returncode == 0, result.stdout + result.stderr
        plan = json.loads((temporary / "release-file-check" / "plan.json").read_text())
        assert plan["dryRun"] is True and plan["active"] is True
        assert plan["source"]["commit"] == exports["SOURCE_SHA"]
    else:
        assert result.returncode != 0
        assert "only for a prerelease Scale Kit version" in result.stderr
        assert not (temporary / "release-file-check").exists()


def test_inactive_release_preparation_explains_why_nothing_will_publish(repository, tmp_path):
    temporary, exports = _source_check_fixture(repository, tmp_path)
    (repository / "README.md").write_text("Unrelated source change\n", encoding="utf-8")
    exports["SOURCE_SHA"] = _commit(repository, "no release")
    result = _run_script(step("prepare", "Prepare the immutable declaration")["run"], repository, exports)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads((temporary / "release-plan" / "plan.json").read_text())
    assert plan["active"] is False
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Nothing will be published" in summary
    assert "releases/<name>/release.json" in summary


@pytest.mark.parametrize("expected", [SHA, "", "abc", "a" * 40])
def test_manual_source_request_requires_the_exact_commit(runner, expected):
    result, _, _ = runner("prepare", "Confirm the source request", extra={"EXPECTED_SHA": expected})
    assert result.returncode == (0 if expected == SHA else 1), result.stdout + result.stderr
    if len(expected) != 40:
        assert "Enter the full 40-character" in result.stdout
        assert "no longer points" not in result.stdout


@pytest.mark.parametrize("outcome", ["success", "failure", "missing", "other-sha", "other-workflow"])
def test_exact_source_ci_readiness_is_a_required_gate(candidate, runner, outcome):
    run = {
        "id": 10, "head_sha": SHA, "path": ".github/workflows/ci.yaml",
        "event": "push", "repository": {"full_name": REPO},
        "status": "completed", "conclusion": outcome,
        "html_url": f"https://github.com/{REPO}/actions/runs/10",
    }
    if outcome == "other-sha":
        run["head_sha"] = "a" * 40
    if outcome == "other-workflow":
        run["path"] = ".github/workflows/release.yaml"
    candidate["responses"][f"repos/{REPO}/actions/workflows/ci.yaml/runs"] = {
        "status": 200, "body": {"workflow_runs": [] if outcome == "missing" else [run]},
    }
    result, outputs, calls = runner(
        "review", "Require successful CI for the candidate", extra={"FAST_CI_CLOCK": "1"},
    )
    assert result.returncode == (0 if outcome == "success" else 1), result.stdout + result.stderr
    if outcome == "success":
        assert outputs["url"] == run["html_url"]
    if outcome in {"missing", "other-sha", "other-workflow"}:
        assert "Waiting for CI to start for this commit" in result.stderr
    assert all("--method" not in call or call[call.index("--method") + 1] == "GET" for call in calls)


def test_approval_preview_discloses_tag_authorization_notes_and_evidence(candidate, runner):
    runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    result, _, _ = runner(
        "review", "Show the release approval preview",
        extra=summary_values(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text()
    for value in (SHA, "Create the missing tag", "Reviewed release notes", "Content", "Approval authorizes"):
        assert value.lower() in summary.lower()
    assert "- Title: `v1.0.0b8: Release highlights`" in summary
    assert "- Components: `Both`" in summary
    assert "- Content version: `1.0.0b8`" in summary
    assert "- Engine selection: `Build from this commit`" in summary


@pytest.mark.parametrize(
    "mode,label,version",
    [("siteops", "Site Ops only", "Not included"), ("content", "Content only", "1.0.0b8"),
     ("both", "Both", "1.0.0b8")],
)
def test_component_summary_distinguishes_versions_and_engine_source(
    candidate, renderer, mode, label, version,
):
    plan = candidate["plan"]
    plan["release"]["components"] = mode
    plan["release"]["stream"] = "siteops" if mode == "siteops" else "scalekit"
    if mode == "content":
        plan["siteops"] = {"bundle": False, "releaseTag": "siteops/v1.2.3"}
    summary = renderer.render_summary(plan, "## Changes\n", summary_values())
    assert f"- Components: `{label}`" in summary
    assert f"- Content version: `{version}`" in summary
    if mode == "content":
        assert "- Engine selection: `Use an existing release`" in summary
        assert "- Site Ops: `siteops/v1.2.3`" in summary
    else:
        assert "- Engine selection: `Build from this commit`" in summary
        assert "- Site Ops: `1.0.0b1+build.42`" in summary


def test_workspace_summary_preserves_literal_reviewed_values(candidate, renderer):
    request = {
        "workspace": "workspaces/storage_demo",
        "id": "[Label](https://example.invalid) | <b>bold</b> `code` \\ *text*",
        "package": "storage_demo.zip",
        "compatibility": {"siteops": ">=1.0.0b1,<2"},
    }
    candidate["plan"]["workspaces"] = [request]
    summary = renderer.render_summary(candidate["plan"], "## Changes\n", summary_values())
    rows = [
        line for line in summary.split("\n## Workspace builds\n", 1)[1].split("\n## Installation checks", 1)[0].splitlines()
        if line.startswith("|")
    ]
    assert len(rows) == 3
    assert rows[2].count("|") == 5
    assert "storage&#95;demo" in rows[2]
    assert "&#91;Label&#93;&#40;https://example.invalid&#41;" in rows[2]
    assert "&#124; &lt;b&gt;bold&lt;/b&gt; &#96;code&#96; &#92; &#42;text&#42;" in rows[2]
    assert "&gt;=1.0.0b1,&lt;2" in rows[2]
    assert candidate["plan"]["workspaces"] == [request]


@pytest.mark.parametrize("mode", [None, {}, "unreviewed"])
def test_invalid_component_summary_leaves_existing_output_unchanged(candidate, runner, mode):
    _render_install_notes(candidate, runner)
    candidate["plan"]["release"]["components"] = mode
    summary = candidate["root"].parent / "summary.md"
    summary.write_text("Earlier summary\n")
    result, _, _ = runner("review", "Show the release approval preview", extra=summary_values())
    assert result.returncode != 0
    assert "components disagree" in result.stdout + result.stderr
    assert summary.read_text() == "Earlier summary\n"


@pytest.mark.parametrize("job,name", [
    ("review", "Verify the pinned candidate"), ("publish", "Verify the approved candidate"),
])
@pytest.mark.parametrize("fault", ["omitted", "missing-assets"])
def test_declared_workspaces_cannot_be_silently_omitted(candidate, runner, job, name, fault):
    request = {
        "workspace": "workspace", "id": "fixture.storage", "package": "workspace.zip",
        "compatibility": {"siteops": ">=1.0.0b1,<2"}, "licenses": ["LICENSE"],
    }
    declaration = {**candidate["declaration"], "workspaces": [request]}
    raw = json.dumps(declaration)
    candidate["plan"]["intent"]["sha256"] = digest(raw.encode())
    for revision in (SHA, "refs/heads/main"):
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref={revision}"]["raw"] = raw
    if fault == "missing-assets":
        candidate["plan"]["workspaces"] = [request]
    result, _, calls = runner(job, name)
    if job == "review" and fault == "missing-assets":
        assert result.returncode == 0, result.stdout + result.stderr
        result, _, calls = runner("review", "Freeze the complete qualified publication payload")
    assert result.returncode != 0
    assert "workspace" in (result.stdout + result.stderr).lower()
    assert not any("--method" in call or call[:2] == ["release", "create"] for call in calls)


def test_note_renderer_rejects_unknown_inventory_fields_before_output(candidate, runner):
    path = candidate["root"] / "release-assets" / "release-assets.json"
    inventory = json.loads(path.read_text())
    inventory["mode"] = "publish"
    path.write_text(json.dumps(inventory))
    result, _, _ = runner(
        "review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"},
    )
    assert result.returncode != 0
    assert not (candidate["root"] / "publish-notes.md").exists()


def test_every_embedded_python_program_compiles_without_shell_indentation():
    count = 0
    for job in JOBS.values():
        for item in job.get("steps", []):
            for match in re.finditer(r"\bpython3?\s+-c\s+'([^']*)'", item.get("run", "")):
                compile(match[1], "<workflow>", "exec")
                count += 1
    assert count >= 10


def test_valid_preview_bundle_keeps_its_exact_source_and_artifact(candidate, runner):
    result, outputs, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["engine-version"] == "1.0.0b1+build.42.1.gcccccccccccc"
    assert len(outputs["plan-sha"]) == len(outputs["bundle-sha"]) == 64
    assert outputs["wheel-name"] == WHEEL
    assert len(outputs["wheel-sha"]) == len(outputs["asset-list-sha"]) == 64
    verifies = [call for call in calls if call[:2] == ["attestation", "verify"]]
    assert [Path(call[2]).name for call in verifies] == [ARCHIVE, WHEEL, BOOTSTRAP[0], BOOTSTRAP[2]]
    for verify in verifies:
        assert verify[verify.index("--source-digest") + 1] == SHA
        assert verify[verify.index("--signer-digest") + 1] == SHA
        assert "--deny-self-hosted-runners" not in verify
        assert verify[verify.index("--format") + 1] == "json"
        assert "--signer-repo" not in verify
    assert not any("--method" in call for call in calls)
    asset_list = json.loads(
        (candidate["root"] / "release-assets" / "release-assets.json").read_text()
    )
    assert [item["name"] for item in asset_list["assets"]] == list(ENGINE_ASSETS)
    for item in asset_list["assets"]:
        assert item["size"] == (candidate["root"] / "release-bundle" / item["name"]).stat().st_size
        assert item["sha256"] == digest(
            (candidate["root"] / "release-bundle" / item["name"]).read_bytes()
        )


@pytest.mark.parametrize("fault", ["name", "digest", "identity", "embedded-bytes", "inventory"])
def test_candidate_rejects_invalid_native_release_assets(candidate, runner, fault):
    extra = {}
    if fault == "name":
        extra["BUILD_WHEEL_NAME"] = "../" + WHEEL
    elif fault == "digest":
        extra["BUILD_WHEEL_SHA"] = "a" * 64
    elif fault == "identity":
        extra["SIGNER_IDENTITY"] = "https://github.com/example/other/.github/workflows/build.yaml@refs/heads/main"
    elif fault == "embedded-bytes":
        wheel = candidate["root"] / "release-bundle" / WHEEL
        wheel.write_bytes(b"different standalone bytes")
        extra["BUILD_WHEEL_SHA"] = digest(wheel.read_bytes())
    else:
        (candidate["root"] / "release-bundle" / "unexpected.txt").write_text("unexpected")
    result, _, calls = runner("review", "Verify the pinned candidate", extra=extra)
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


def test_candidate_requires_independent_wheel_attestation(candidate, runner):
    result, _, calls = runner(
        "review", "Verify the pinned candidate",
        extra={"FAIL_ATTESTATION_SUBJECT": WHEEL},
    )
    assert result.returncode != 0
    assert [Path(call[2]).name for call in calls if call[:2] == ["attestation", "verify"]] == [
        ARCHIVE, WHEEL,
    ]


@pytest.mark.parametrize("kind", ["siteops", "content", "combined"])
def test_real_git_declaration_and_cli_feed_the_candidate_controller(
    repository, tmp_path, candidate, runner, kind,
):
    if kind == "siteops":
        declaration = {"tag": "siteops/v1.2.3"}
        base = "1.2.3"
    elif kind == "combined":
        declaration = {"tag": "v1.0.0b8", "siteops": {"build": True}}
        base = "1.0.0b1"
    else:
        declaration = {"tag": "v2.0.0", "siteops": {"release": "siteops/v1.0.0"}}
        base = "9.0.0b1"
    _write_source_version(repository, f'__version__ = "{base}"\n')
    raw, notes = _write_record(repository, declaration, notes="Reviewed UTF-8 notes: \u03b1.\n".encode())
    sha = _commit(repository, "reviewed declaration")
    output = tmp_path / "prepared"
    result = subprocess.run(
        [sys.executable, "-B", str(CLI), "--repository", REPO, "--source-sha", sha,
         "--source-ref", "refs/heads/main", "--intent", "releases/candidate/release.json",
         "--output-dir", str(output)],
        cwd=repository, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    candidate["plan"], candidate["source_sha"] = plan, sha
    (candidate["root"] / "release-plan" / "release-notes.md").write_bytes(
        (output / "release-notes.md").read_bytes(),
    )
    for revision in (sha, "refs/heads/main"):
        for name, body in (("release.json", raw.decode()), ("notes.md", notes.decode())):
            candidate["responses"][f"repos/{REPO}/contents/releases/candidate/{name}?ref={revision}"] = {
                "status": 200, "raw": body,
            }
    candidate_extra = {}
    if plan["siteops"]["bundle"]:
        version = base if kind == "siteops" else base + "+build.42.1.g" + sha[:12]
        wheel_name = f"siteops-{version}-py3-none-any.whl"
        for path in (candidate["root"] / "release-bundle").iterdir():
            path.unlink()
        wheel_bytes = ("wheel:" + version).encode()
        with zipfile.ZipFile(candidate["root"] / "release-bundle" / ARCHIVE, "w") as archive:
            archive.writestr("bundle.json", json.dumps({
                "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle",
                "source": plan["source"], "build": {"number": 42, "attempt": 1},
                "package": {"name": "siteops", "version": version, "wheel": "wheels/" + wheel_name},
            }))
            archive.writestr("wheels/" + wheel_name, wheel_bytes)
        (candidate["root"] / "release-bundle" / wheel_name).write_bytes(wheel_bytes)
        (candidate["root"] / "release-bundle" / (ARCHIVE + ".attestation.jsonl")).write_text("proof")
        (candidate["root"] / "release-bundle" / (wheel_name + ".attestation.jsonl")).write_text("proof")
        for name in BOOTSTRAP:
            (candidate["root"] / "release-bundle" / name).write_text("synthetic " + name)
        candidate_extra = {
            "BUILD_WHEEL_NAME": wheel_name,
            "BUILD_WHEEL_SHA": digest(wheel_bytes),
        }
    else:
        tag = "siteops/v1.0.0"
        encoded = urllib.parse.quote(tag, safe="")
        assets = [
            {"name": name, "size": len(name), "digest": "sha256:" + digest(name.encode()), "state": "uploaded"}
            for name in (ARCHIVE, PROOF, WHEEL, WHEEL_PROOF)
        ]
        candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"] = {
            "status": 200, "body": {"id": 71, "tag_name": tag, "draft": False,
                                  "assets": assets},
        }
        candidate["responses"][f"repos/{REPO}/git/ref/tags/{encoded}"] = {
            "status": 200, "body": {"object": {"sha": "d" * 40}},
        }
    result, outputs, _ = runner("review", "Verify the pinned candidate", extra=candidate_extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["tag"] == declaration["tag"]
    assert outputs["title"] == declaration["tag"] + ": Release highlights"
    assert outputs["bundle"] == str(kind != "content").lower()
    result, _, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": outputs.get("engine-version", "")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "\u03b1" in (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["source", "notes", "latest", "version", "mode", "components", "title", "current-intent"])
def test_invalid_or_superseded_candidate_cannot_reach_tag_creation(candidate, runner, fault):
    plan = candidate["plan"]
    if fault == "source":
        plan["source"]["commit"] = "d" * 40
    elif fault == "notes":
        (candidate["root"] / "release-plan" / "release-notes.md").write_text("changed")
    elif fault == "latest":
        plan["release"]["latest"] = True
    elif fault == "version":
        plan["release"]["version"] = "1.0.0"
    elif fault == "mode":
        plan["siteops"]["versionMode"] = "source"
    elif fault == "components":
        plan["release"]["components"] = "content"
    elif fault == "title":
        plan["release"]["title"] = "v1.0.0b8: Unreviewed title"
    else:
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/notes.md?ref=refs/heads/main"]["raw"] = "new intent"
    result, _, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize(
    "variable",
    [
        "FAIL_ATTESTATION", "APPROVED_PLAN_SHA", "APPROVED_BUNDLE_SHA",
        "APPROVED_WHEEL_SHA", "APPROVED_ASSET_LIST_SHA", "APPROVED_NOTES_SHA",
    ],
)
def test_authentication_and_approved_digest_drift_fail_closed(runner, variable):
    value = "1" if variable == "FAIL_ATTESTATION" else "a" * 64
    name = (
        "Verify the approved release assets"
        if variable in {"FAIL_ATTESTATION", "APPROVED_BUNDLE_SHA"}
        else "Verify the approved candidate"
    )
    result, _, calls = runner("publish", name, extra={variable: value})
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


def test_publisher_reauthenticates_all_approved_engine_subjects(runner):
    result, _, calls = runner("publish", "Verify the approved release assets")
    assert result.returncode == 0, result.stdout + result.stderr
    verifies = [call for call in calls if call[:2] == ["attestation", "verify"]]
    assert [Path(call[2]).name for call in verifies] == [ARCHIVE, WHEEL, BOOTSTRAP[0], BOOTSTRAP[2]]
    for call in verifies:
        assert call[call.index("--cert-identity") + 1] == (
            f"https://github.com/{REPO}/.github/workflows/_siteops-distribution.yaml@refs/heads/main"
        )
        assert call[call.index("--source-digest") + 1] == SHA
        assert call[call.index("--signer-digest") + 1] == SHA


def test_publisher_rejects_a_title_that_disagrees_with_reviewed_source(candidate, runner):
    candidate["plan"]["release"]["title"] = "v1.0.0b8: A different headline"
    result, _, calls = runner("publish", "Verify the approved candidate")
    assert result.returncode != 0
    assert "title differs from the reviewed declaration" in result.stdout + result.stderr
    assert all("--method" not in call for call in calls)


def test_reviewed_headline_flows_through_preparation_and_publication(candidate, runner):
    headline = 'Native "pipx" installation and d\u00e9ploiement'
    candidate["declaration"]["headline"] = headline
    title = candidate["declaration"]["tag"] + ": " + headline
    raw = json.dumps(candidate["declaration"]).encode("utf-8")
    candidate["plan"]["intent"]["sha256"] = digest(raw)
    candidate["plan"]["release"]["title"] = title
    for revision in (SHA, "refs/heads/main"):
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref={revision}"]["raw"] = raw.decode()
    prepared, values, _ = runner("review", "Verify the pinned candidate")
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert values["title"] == title
    approved, values, _ = runner("publish", "Verify the approved candidate")
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert values["title"] == title
    published, _, calls = runner(
        "publish", "Publish the approved release",
        extra={"TITLE": values["title"], "PRERELEASE": "true", "LATEST": "false", "BUNDLE": "true"},
    )
    assert published.returncode == 0, published.stdout + published.stderr
    command = calls[-1]
    assert command[command.index("--title") + 1] == title


def test_changed_headline_requires_a_fresh_release_candidate(candidate, runner):
    declaration = {**candidate["declaration"], "headline": "New release headline"}
    candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref=refs/heads/main"]["raw"] = json.dumps(declaration)
    result, _, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode != 0
    assert "Prepare and review a new candidate" in result.stdout + result.stderr
    assert all("--method" not in call for call in calls)


def test_publisher_rejects_changed_approved_proof(candidate, runner):
    (candidate["root"] / "release-bundle" / WHEEL_PROOF).write_text("changed proof")
    result, _, calls = runner("publish", "Verify the approved release assets")
    assert result.returncode != 0
    assert not any(call[:2] == ["attestation", "verify"] for call in calls)


def test_publisher_rejects_approved_wheel_that_differs_from_the_bundle(candidate, runner):
    wheel = candidate["root"] / "release-bundle" / WHEEL
    wheel.write_bytes(b"separately approved but unequal wheel")
    asset_path = candidate["root"] / "release-assets" / "release-assets.json"
    asset_list = json.loads(asset_path.read_text())
    next(item for item in asset_list["assets"] if item["name"] == WHEEL)["sha256"] = digest(
        wheel.read_bytes()
    )
    next(item for item in asset_list["assets"] if item["name"] == WHEEL)["size"] = wheel.stat().st_size
    asset_path.write_text(
        json.dumps(asset_list, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result, _, calls = runner("publish", "Verify the approved release assets")
    assert result.returncode != 0
    assert [Path(call[2]).name for call in calls if call[:2] == ["attestation", "verify"]] == [
        ARCHIVE, WHEEL, BOOTSTRAP[0], BOOTSTRAP[2],
    ]


def test_independent_content_uses_released_engine_without_bundle_verification(candidate, runner):
    plan = candidate["plan"]
    tag = "siteops/v1.0.0"
    raw = json.dumps({"tag": "v2.0.0", "headline": "Content highlights", "siteops": {"release": tag}}).encode()
    plan["release"].update(
        tag="v2.0.0", version="2.0.0", components="content",
        prerelease=False, title="v2.0.0: Content highlights",
    )
    plan["siteops"] = {"bundle": False, "versionMode": None, "baseVersion": None, "releaseTag": tag}
    plan["intent"]["sha256"] = digest(raw)
    for revision in (SHA, "refs/heads/main"):
        candidate["responses"][f"repos/{REPO}/contents/releases/candidate/release.json?ref={revision}"]["raw"] = raw.decode()
    encoded = urllib.parse.quote(tag, safe="")
    assets = [
        {"name": name, "size": len(name), "digest": "sha256:" + digest(name.encode()), "state": "uploaded"}
        for name in (ARCHIVE, PROOF, WHEEL, WHEEL_PROOF)
    ]
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"] = {
        "status": 200, "body": {"id": 71, "tag_name": tag, "draft": False,
                              "assets": assets},
    }
    candidate["responses"][f"repos/{REPO}/git/ref/tags/{encoded}"] = {
        "status": 200, "body": {"object": {"sha": "d" * 40}},
    }
    result, outputs, calls = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs["engine-id"] == "71"
    assert outputs["wheel-name"] == WHEEL
    assert not any(call[0] == "attestation" for call in calls)
    inventory = json.loads(
        (candidate["root"] / "release-assets" / "release-assets.json").read_text()
    )
    assert inventory["source"] == plan["source"]
    assert inventory["assets"] == []
    assert len(inventory["engine"]["assets"]) == 4
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"]["body"]["assets"] = [
        {"name": ARCHIVE, "digest": "sha256:" + digest(ARCHIVE.encode()), "state": "uploaded"},
        {"name": PROOF, "digest": "sha256:" + digest(PROOF.encode()), "state": "uploaded"},
    ]
    result, _, _ = runner("review", "Verify the pinned candidate")
    assert result.returncode != 0
    candidate["responses"][f"repos/{REPO}/releases/tags/{encoded}"]["body"]["assets"] = assets
    result, _, _ = runner("review", "Verify the pinned candidate")
    assert result.returncode == 0
    result, _, _ = runner(
        "publish", "Verify the approved candidate",
        extra={"APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "d" * 40},
    )
    assert result.returncode == 0
    assets[2]["digest"] = "sha256:" + "a" * 64
    result, _, _ = runner(
        "publish", "Verify the approved candidate",
        extra={"APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "d" * 40},
    )
    assert result.returncode != 0
    assets[2]["digest"] = "sha256:" + digest(WHEEL.encode())
    result, _, _ = runner("publish", "Verify the approved candidate", extra={"APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "e" * 40})
    assert result.returncode != 0


@pytest.mark.parametrize("components", ["siteops", "content", None])
def test_publisher_rejects_inconsistent_components(candidate, runner, components):
    candidate["plan"]["release"]["components"] = components
    result, _, calls = runner("publish", "Verify the approved candidate")
    assert result.returncode != 0
    assert "components disagree" in result.stdout + result.stderr
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize("size", [None, True, 0, -1, 4294967297, "12"])
def test_publisher_rejects_invalid_frozen_asset_size(candidate, runner, size):
    path = candidate["root"] / "release-assets" / "release-assets.json"
    inventory = json.loads(path.read_text())
    inventory["assets"][0]["size"] = size
    path.write_text(json.dumps(inventory))
    result, _, calls = runner("publish", "Verify the approved candidate")
    assert result.returncode != 0
    assert "asset identity is invalid" in result.stdout + result.stderr
    assert not any("--method" in call for call in calls)


def test_publisher_detects_size_drift_before_authentication(candidate, runner):
    path = candidate["root"] / "release-assets" / "release-assets.json"
    inventory = json.loads(path.read_text())
    inventory["assets"][0]["size"] += 1
    path.write_text(json.dumps(inventory))
    result, _, calls = runner("publish", "Verify the approved release assets")
    assert result.returncode != 0
    assert not calls


@pytest.mark.parametrize("fault", ["digest", "size", "approved-list"])
def test_publication_rechecks_frozen_bytes_before_upload(candidate, runner, fault):
    extra = {"TITLE": "Example release", "PRERELEASE": "true", "LATEST": "false"}
    wheel = candidate["root"] / "release-bundle" / WHEEL
    if fault == "digest":
        wheel.write_bytes(b"x" * wheel.stat().st_size)
    elif fault == "size":
        wheel.write_bytes(wheel.read_bytes() + b"x")
    else:
        extra["APPROVED_ASSET_LIST_SHA"] = "a" * 64
    result, _, calls = runner("publish", "Publish the approved release", extra=extra)
    assert result.returncode != 0
    assert not any(call[:2] == ["release", "create"] for call in calls)


def test_published_identity_comes_from_approval_not_changed_local_bytes(candidate, runner):
    path = candidate["root"] / "release-bundle" / WHEEL
    path.write_bytes(b"changed after upload")
    asset_path = candidate["root"] / "release-assets" / "release-assets.json"
    inventory = json.loads(asset_path.read_text())
    candidate["responses"][f"repos/{REPO}/releases/tags/v1.0.0b8"] = {
        "status": 200,
        "body": {
            "tag_name": "v1.0.0b8", "draft": False, "prerelease": True, "immutable": True,
            "assets": [
                {**asset, "digest": "sha256:" + asset["sha256"], "state": "uploaded"}
                for asset in inventory["assets"]
            ],
        },
    }
    result, _, _ = runner(
        "publish", "Confirm published assets and immutability", extra={"PRERELEASE": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("state", ["missing", "matching", "conflicting", "unavailable", "published"])
def test_tag_and_release_target_conditions(candidate, runner, state):
    tag = candidate["plan"]["release"]["tag"]
    candidate["responses"][f"repos/{REPO}/releases/tags/{tag}"] = {
        "status": 200 if state == "published" else 404, "body": {},
    }
    candidate["responses"][f"repos/{REPO}/git/ref/tags/{tag}"] = {
        "status": 404 if state == "missing" else 403 if state == "unavailable" else 200,
        "body": {"object": {"type": "commit", "sha": "d" * 40 if state == "conflicting" else SHA}},
    }
    result, outputs, calls = runner("prepare", "Check the publication target")
    assert result.returncode == (0 if state in {"missing", "matching"} else 1), result.stdout + result.stderr
    if result.returncode == 0:
        assert outputs["tag-exists"] == str(state == "matching").lower()
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize("configured", [False, True])
def test_publication_requires_real_environment_reviewers(candidate, runner, configured):
    assert JOBS["prepare"]["permissions"] == {"contents": "read", "actions": "read"}
    candidate["responses"][f"repos/{REPO}/environments/siteops-release"] = {
        "status": 200,
        "body": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "Team"}]}] if configured else []},
    }
    result, _, _ = runner("prepare", "Require configured publication reviewers")
    assert result.returncode == (0 if configured else 1), result.stdout + result.stderr


def test_tag_creation_uses_only_the_approved_commit(candidate, runner):
    tag = candidate["plan"]["release"]["tag"]
    candidate["responses"][f"repos/{REPO}/git/refs"] = {
        "status": 201, "body": {"ref": "refs/tags/" + tag, "object": {"sha": SHA}},
    }
    result, _, calls = runner("publish", "Create only the approved missing tag")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [["api", "--method", "POST", f"repos/{REPO}/git/refs", "-f", "ref=refs/tags/" + tag, "-f", "sha=" + SHA]]
    assert "force" not in json.dumps(calls)


def test_notes_preview_and_publication_use_identical_rendering(candidate, runner):
    result, values, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_bytes()
    assert digest(notes) == values["sha256"]
    (candidate["root"] / "release-notes" / "publish-notes.md").write_bytes(notes)
    result, _, _ = runner("publish", "Verify the approved candidate", extra={"APPROVED_NOTES_SHA": values["sha256"]})
    assert result.returncode == 0
    (candidate["root"] / "release-notes" / "publish-notes.md").write_bytes(notes + b"changed")
    result, _, _ = runner(
        "publish", "Verify the approved candidate", extra={"APPROVED_NOTES_SHA": values["sha256"]},
    )
    assert result.returncode != 0


def test_release_rehearsal_cannot_reach_publishing_permissions():
    assert "publish" not in CANDIDATE_WORKFLOW["jobs"]
    for job in CANDIDATE_WORKFLOW["jobs"].values():
        assert job.get("permissions", {}).get("contents") != "write"
        assert "environment" not in job
    assert step("prepare", "Require configured publication reviewers")["if"] == "${{ !inputs.dry-run && steps.plan.outputs.active == 'true' }}"
    names = [item["name"] for item in JOBS["prepare"]["steps"]]
    assert names.index("Prepare the immutable declaration") < names.index("Require configured publication reviewers")
    assert names.index("Require configured publication reviewers") < names.index("Retain the declaration and notes")
    assert names.index("Check the publication target") < names.index("Retain the declaration and notes")
    assert JOBS["distribution"]["with"]["report-summary"] is False
    text = yaml.safe_dump(CANDIDATE_WORKFLOW)
    assert "release create" not in text and "--method POST" not in text
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    job = ci["jobs"]["release-preview"]
    assert job["needs"] == ["lint", "test", "validate", "release-runner"]
    assert job["with"]["dry-run"] is True
    assert job["permissions"]["contents"] == "read"


@pytest.mark.parametrize("is_dry_run", [True, False])
def test_example_branch_candidate_is_allowed_only_as_a_dry_run(candidate, runner, is_dry_run):
    plan = candidate["plan"]
    ref = "refs/heads/preview"
    plan["dryRun"] = is_dry_run
    plan["source"]["ref"] = ref
    plan["intent"]["path"] = ".github/release-examples/example/release.json"
    plan["intent"]["notesPath"] = ".github/release-examples/example/notes.md"
    for revision in (SHA, ref):
        for name in ("release.json", "notes.md"):
            candidate["responses"][f"repos/{REPO}/contents/.github/release-examples/example/{name}?ref={revision}"] = dict(
                candidate["responses"][f"repos/{REPO}/contents/releases/candidate/{name}?ref={SHA}"],
            )
    with zipfile.ZipFile(candidate["root"] / "release-bundle" / ARCHIVE, "w") as archive:
        archive.writestr("bundle.json", json.dumps({
            "apiVersion": "siteops.install/v1", "kind": "SiteOpsBundle", "source": plan["source"],
            "build": {"number": 42, "attempt": 1},
            "package": {
                "name": "siteops", "version": "1.0.0b1+build.42.1.gcccccccccccc",
                "wheel": "wheels/" + WHEEL,
            },
        }))
        archive.writestr(
            "wheels/" + WHEEL,
            (candidate["root"] / "release-bundle" / WHEEL).read_bytes(),
        )
    extra = {"DRY_RUN": str(is_dry_run).lower(), "SOURCE_REF": ref}
    result, _, _ = runner("review", "Verify the pinned candidate", extra=extra)
    assert result.returncode == (0 if is_dry_run else 1), result.stdout + result.stderr
    result, _, calls = runner("publish", "Verify the approved candidate", extra=extra)
    assert result.returncode != 0
    assert not any("--method" in call for call in calls)


def test_dry_run_uses_completed_ci_jobs_without_waiting_for_its_own_run(candidate, runner):
    required_names = [
        CI_WORKFLOW["jobs"][job]["name"]
        for job in CI_WORKFLOW["jobs"]["release-preview"]["needs"]
        if job != "release-runner"
    ]
    candidate["responses"][f"repos/{REPO}/actions/runs/42/attempts/1/jobs?per_page=100"] = {
        "status": 200, "body": {"jobs": [
            {"name": name, "conclusion": "success"} for name in required_names
        ]},
    }
    result, output, _ = runner("review", "Require successful CI for the candidate", extra={"DRY_RUN": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert output["url"].endswith("/actions/runs/42")
    candidate["responses"][f"repos/{REPO}/actions/runs/42/attempts/1/jobs?per_page=100"]["body"]["jobs"].pop()
    result, _, _ = runner("review", "Require successful CI for the candidate", extra={"DRY_RUN": "true"})
    assert result.returncode != 0


def test_dry_run_summary_stops_at_preview_without_approval_instructions(candidate, runner):
    candidate["plan"]["dryRun"] = True
    runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    result, _, _ = runner(
        "review", "Show the release approval preview",
        extra=summary_values(DRY_RUN="true"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text()
    assert summary.startswith("# Release preview (no publication)")
    assert "No tag, GitHub Release, or approval request was created." in summary
    assert "Approve and deploy" not in summary
    assert summary.count(
        "| Python | Ubuntu 24.04 | Ubuntu 26.04 | Windows | Windows standard user |"
    ) == 1
    assert "| 3.10 | passed | n/a | passed | n/a |" in summary
    assert "| 3.11 | passed | passed | passed | passed |" in summary
    assert "Download the attested release assets" in summary
    assert WHEEL in summary


def _render_install_notes(candidate, runner):
    result, values, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")
    assert values["sha256"] == digest(notes.encode())
    return notes


def test_install_notes_require_explicit_supported_runner_policy(candidate, runner):
    result, _, _ = runner(
        "review", "Render the final release notes",
        extra={"ENGINE_VERSION": "1.0.0b1", "EXPECTED_RUNNER_ENVIRONMENT": "PRIVATE_UNKNOWN"},
    )
    assert result.returncode != 0
    assert "expected provenance runner class is unsupported" in result.stdout + result.stderr
    assert "PRIVATE_UNKNOWN" not in result.stdout + result.stderr
    assert not (candidate["root"] / "publish-notes.md").exists()


def _installation_block(notes):
    match = re.search(r"```console\n(.*?)\n```", notes, re.DOTALL)
    assert match is not None
    return match[1]


@pytest.mark.parametrize("tag", ["v1.0.0b8", "siteops/v1.1.0"])
def test_install_notes_bind_downloads_and_commands_to_the_selected_release(candidate, runner, tag):
    candidate["plan"]["release"]["tag"] = tag
    candidate["plan"]["release"]["stream"] = "siteops" if tag.startswith("siteops/") else "scalekit"
    notes = _render_install_notes(candidate, runner)
    assert notes.count("## Install Site Ops") == 1
    base = f"https://github.com/{REPO}/releases/download/{urllib.parse.quote(tag, safe='')}/"
    for name in ENGINE_ASSETS:
        assert base + name in notes
    assert (
        f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md#choose-an-installation-route"
        in notes
    )
    block = _installation_block(notes)
    assert block.startswith(f'uv tool install "{base}{WHEEL}"')
    assert "--python 3.11.16" in block
    assert "--managed-python" in block
    assert "--no-build" in block
    assert "--no-config" not in block and "--default-index" not in block
    assert "--force" not in block
    assert "Runtime dependencies come from your configured package index as wheels" in notes
    assert "`--default-index <your approved index>`" in notes
    assert "packagefeedproxy.microsoft.io" not in notes
    assert "uv does not automatically verify GitHub attestations" in notes
    assert (
        f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md#verify-the-bootstrap-script"
        in _verified_section(notes)
    )
    assert f"Confirm that `{REPO}` is the publisher you intend" in notes
    assert "not that it is the right publisher" in notes
    assert "It is not independent publisher authentication" in notes
    assert "use the commands under Verify the script before it runs" in notes
    assert "use this release's reviewed provenance values instead" not in notes
    assert "configured-Site fleet selectors remain supported" in notes
    assert "A project pin selects content, not operator Site configuration" in notes
    assert "`--reinstall`" in notes
    assert "downloads the ZIP and its detached proof, authenticates the ZIP before extraction" in notes
    assert f"Expected publisher: `{REPO}`" in notes
    assert f"Source commit: `{SHA}`" in notes
    assert f'Source ref: `{candidate["plan"]["source"]["ref"]}`' in notes
    assert "Expected provenance runner class: `self-hosted`" in notes
    assert "runner class does not identify a particular pool" in notes
    assert "switching between online and verified installations" in notes
    assert "native uv only after independent payload admission" in notes
    assert "`install.py`" not in notes and "siteops_distribution.py" not in notes
    assert "Invoke-WebRequest" not in notes and "urllib.request" not in notes
    assert "uv 0.12.20" in notes
    assert "uv-managed CPython" in notes
    routes, details = notes.split(PROVENANCE + "\n\n", 1)
    details = details.split("\n\n</details>", 1)[0]
    assert "```" not in details
    assert "Expected publisher" not in routes and "Source commit" not in routes
    for identity in (
        f"Expected publisher: `{REPO}`", f"Source commit: `{SHA}`",
        f'Source ref: `{candidate["plan"]["source"]["ref"]}`',
        "Expected provenance runner class: `self-hosted`",
        f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md#choose-an-installation-route",
        *(base + name for name in ENGINE_ASSETS),
    ):
        assert identity in details
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    for argument in (
        "--python 3.11.16", "--managed-python", "--no-build",
    ):
        assert argument in block and argument in guide
    assert "packagefeedproxy.microsoft.io" not in guide


def downloads_url(tag):
    return f"https://github.com/{REPO}/releases/download/{urllib.parse.quote(tag, safe='')}/{ARCHIVE}"


def test_content_only_install_notes_link_to_the_engine_release_without_wrong_source_commands(candidate, runner):
    candidate["plan"]["siteops"] = {
        "bundle": False, "versionMode": None, "baseVersion": None, "releaseTag": "siteops/v1.0.0",
    }
    _reference_inventory(candidate)
    notes = _render_install_notes(candidate, runner)
    assert f"https://github.com/{REPO}/releases/tag/siteops%2Fv1.0.0" in notes
    assert "own source commit and native installation assets" in notes
    assert "uv tool install" not in notes and "releases/download/" not in notes


@pytest.mark.parametrize("uv_exit", [0, 7])
def test_generated_online_install_command_runs_from_an_unrelated_directory(
    candidate, runner, tmp_path, uv_exit,
):
    command = _installation_block(_render_install_notes(candidate, runner))
    arguments = tmp_path / "uv-args.log"
    script = """
uv() {
    printf '%s\n' "$@" > "$UV_ARGS"
    return "$UV_EXIT"
}
pipx() { echo 'Unexpected legacy manager invocation.' >&2; return 98; }
""" + command
    result = _run_script(
        script,
        tmp_path,
        {"UV_ARGS": bash_path(arguments), "UV_EXIT": str(uv_exit)},
    )
    assert result.returncode == uv_exit
    assert arguments.read_text().splitlines() == [
        "tool", "install",
        downloads_url("v1.0.0b8").removesuffix(ARCHIVE) + WHEEL,
        "--python", "3.11.16", "--managed-python", "--no-build", "--system-certs",
    ]


def _bootstrap_block(notes, language):
    match = re.search(rf"```{language}\n(.*?)\n```", notes, re.DOTALL)
    assert match is not None, f"Missing complete {language} bootstrap command."
    return match[1]


@pytest.mark.parametrize("caller", ["release.yaml", "ci.yaml"])
def test_generated_bootstrap_entries_bind_the_full_selection(candidate, runner, caller):
    tag = "siteops/v1.1.0"
    candidate["plan"]["release"]["tag"] = tag
    result, _, _ = runner("review", "Render the final release notes", extra={
        "ENGINE_VERSION": "1.0.0b1",
        "BUILDER_IDENTITY": f"https://github.com/{REPO}/.github/workflows/{caller}@refs/heads/main",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")
    for language, filename in (("bash", "siteops-bootstrap.sh"), ("powershell", "siteops-bootstrap.ps1")):
        block = _bootstrap_block(notes, language)
        payload = (candidate["root"] / "release-bundle" / filename).read_bytes()
        assert downloads_url(tag).removesuffix(ARCHIVE) + filename in block
        assert digest(payload) in block and str(len(payload)) in block
        for identity in (REPO, SHA, tag, "refs/heads/main", caller):
            assert identity in block
        assert "<approved" not in block and "<full" not in block and "latest" not in block
        assert "--yes" not in block and "-Yes" not in block
        assert "enroll-source" not in block and "-EnrollSource" not in block
        assert "az login" not in block and "gh auth" not in block
    assert "not independent publisher authentication" in notes
    assert "before any installer code runs" in notes
    details = notes.split(PROVENANCE, 1)[1]
    assert f"Expected calling workflow: `{caller}`" in details
    assert notes.index("```powershell") < notes.index(PROVENANCE)
    enrollment = f"```console\nsiteops source enroll example --source github:{REPO}\n```"
    if caller == "release.yaml":
        assert notes.index("```powershell") < notes.index(enrollment) < notes.index(PROVENANCE)
        assert "### Enroll the content source" in notes
        assert "renew the 30 day enrollment" in notes
        assert "approval" not in notes.split(PROVENANCE, 1)[0]
    else:
        assert "### Enroll the content source" not in notes and "source enroll" not in notes.split(PROVENANCE, 1)[0]


def _verified_section(notes):
    assert notes.count(VERIFIED) == 1, "Missing the collapsed verified bootstrap commands."
    return notes.split(VERIFIED, 1)[1].split("\n</details>", 1)[0]


def _verified_block(notes, language):
    return _bootstrap_block(_verified_section(notes), language)


@pytest.mark.parametrize("caller", ["release.yaml", "ci.yaml"])
def test_generated_verified_bootstrap_binds_the_full_selection(candidate, runner, caller):
    tag = "siteops/v1.1.0"
    candidate["plan"]["release"]["tag"] = tag
    result, _, _ = runner("review", "Render the final release notes", extra={
        "ENGINE_VERSION": "1.0.0b1",
        "BUILDER_IDENTITY": f"https://github.com/{REPO}/.github/workflows/{caller}@refs/heads/main",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")
    section = _verified_section(notes)
    https = notes.index("```powershell")
    assert https < notes.index(VERIFIED) < notes.index(PROVENANCE)
    if caller == "release.yaml":
        assert notes.index(VERIFIED) < notes.index("### Enroll the content source")
    prose = " ".join(section.split("```", 1)[0].split())
    assert f"Confirm that `{REPO}` is the publisher you intend." in prose
    assert "not that it is the right publisher" in prose
    assert (
        f"[installation guide](https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md"
        "#verify-the-bootstrap-script)" in prose
    )
    base = downloads_url(tag).removesuffix(ARCHIVE)
    for language, filename in (("bash", "siteops-bootstrap.sh"), ("powershell", "siteops-bootstrap.ps1")):
        block = _verified_block(notes, language)
        for identity in (
            f"'{tag}'", f"'{SHA}'", f"'{REPO}'", "'refs/heads/main'", f"/{caller}@",
            "/_siteops-distribution.yaml@", "self-hosted", base, filename + ".attestation.jsonl",
        ):
            assert identity in block
        assert re.search(r"<[a-z][\w-]*>", block) is None
        assert "latest" not in block and "--yes" not in block and "-Yes" not in block
        assert "enroll" not in block.lower()
        assert "az login" not in block and "gh auth" not in block
    assert "source enroll" not in section


@pytest.mark.parametrize(("repository", "ref", "builder", "runner_class", "expected"), [
    ("Azure/digital-ops-scale-kit", "refs/heads/main", "release.yaml", "self-hosted",
     "siteops source enroll official"),
    ("azure/Digital-Ops-Scale-Kit", "refs/heads/main", "release.yaml", "self-hosted",
     "siteops source enroll official"),
    ("digimaun/digital-ops-scale-kit", "refs/heads/main", "release.yaml", "self-hosted",
     "siteops source enroll digimaun --source github:digimaun/digital-ops-scale-kit"),
    ("Contoso_Ops/content", "refs/heads/main", "release.yaml", "self-hosted",
     "siteops source enroll contoso-ops --source github:Contoso_Ops/content"),
    ("9lives/content", "refs/heads/main", "release.yaml", "self-hosted",
     "siteops source enroll publisher --source github:9lives/content"),
    ("example/publisher", "refs/heads/other", "release.yaml", "self-hosted", None),
    ("example/publisher", "refs/heads/main", "ci.yaml", "self-hosted", None),
    ("example/publisher", "refs/heads/main", "release.yaml", "github-hosted", None),
])
def test_source_enrollment_matches_the_standard_release_policy_only(
    renderer, repository, ref, builder, runner_class, expected,
):
    identity = f"https://github.com/{repository}/.github/workflows/{builder}@{ref}"
    assert renderer.source_enrollment(repository, ref, identity, runner_class) == expected


@pytest.mark.parametrize(("workspaces", "enrollment", "expected"), [
    (["workspaces/iot-operations"], "siteops source enroll official",
     'siteops deploy aio-install --source "official@v1.0.0b7" --input "cluster=<Arc-cluster-resource-ID>"'),
    (["workspaces/iot-operations"], "siteops source enroll contoso --source github:contoso/kit",
     'siteops deploy aio-install --source "contoso@v1.0.0b7" --input "cluster=<Arc-cluster-resource-ID>"'),
    (["workspaces/iot-operations", "workspaces/other"], None,
     'siteops -w workspaces/iot-operations deploy aio-install --source "<approved-source>@v1.0.0b7" '
     '--input "cluster=<Arc-cluster-resource-ID>"'),
    (["workspaces/other"], "siteops source enroll official",
     'siteops deploy <manifest> --source "official@v1.0.0b7" --input-file <answers.yaml>'),
])
def test_workspace_notes_lead_with_one_deploy_command_for_the_enrolled_name(
    renderer, workspaces, enrollment, expected,
):
    plan = {
        "release": {"tag": "v1.0.0b7"},
        "workspaces": [
            {"workspace": path, "id": "azure.iot-operations" if path.endswith("iot-operations") else "other.kit"}
            for path in workspaces
        ],
    }
    assert renderer.workspace_deploy_command(plan, enrollment) == expected


@pytest.mark.parametrize("builder", [
    "", "https://github.com/other/publisher/.github/workflows/release.yaml@refs/heads/main",
    f"https://github.com/{REPO}/.github/workflows/unknown.yaml@refs/heads/main",
    f"https://github.com/{REPO}/.github/workflows/release.yaml@refs/heads/other",
])
def test_generated_bootstrap_rejects_unsupported_caller_identity(candidate, runner, builder):
    result, _, _ = runner("review", "Render the final release notes", extra={
        "ENGINE_VERSION": "1.0.0b1", "BUILDER_IDENTITY": builder,
    })
    assert result.returncode != 0
    assert "calling workflow" in result.stderr
    assert not (candidate["root"] / "publish-notes.md").exists()


def test_hosted_runner_notes_do_not_offer_incompatible_bootstrap(candidate, runner):
    result, _, _ = runner("review", "Render the final release notes", extra={
        "ENGINE_VERSION": "1.0.0b1", "EXPECTED_RUNNER_ENVIRONMENT": "github-hosted",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (candidate["root"] / "publish-notes.md").read_text(encoding="utf-8")
    assert "uv tool install" in notes
    assert "```bash" not in notes and "```powershell" not in notes
    assert "Verify the script before it runs" not in notes
    assert "bootstrap requires the approved `self-hosted` provenance policy" in notes
    assert "### Enroll the content source" not in notes


@pytest.mark.parametrize("case", [
    "success", "download-failed", "truncated", "changed", "installer-failed",
])
def test_generated_bash_bootstrap_downloads_and_checks_before_execution(candidate, runner, tmp_path, case):
    command = _bootstrap_block(_render_install_notes(candidate, runner), "bash")
    arguments, downloads = tmp_path / "bootstrap-args.txt", tmp_path / "download-args.txt"
    script = r"""
curl() {
    printf '%s\n' "$@" > "$DOWNLOAD_ARGS"
    while [[ "$1" != --output ]]; do shift; done
    cp "$SCRIPT_BYTES" "$2"
    case "$CASE" in
      download-failed) return 22 ;;
      truncated) head -c -1 "$SCRIPT_BYTES" > "$2" ;;
      changed) printf X | dd of="$2" bs=1 count=1 conv=notrunc 2>/dev/null ;;
    esac
}
bash() {
    printf '%s\n' "$@" > "$BOOTSTRAP_ARGS"
    [[ "$CASE" != installer-failed ]]
}
uv() { echo 'Unexpected manager invocation.' >&2; return 98; }
pipx() { echo 'Unexpected legacy manager invocation.' >&2; return 98; }
gh() { echo 'Unexpected authentication operation.' >&2; return 98; }
""" + command
    result = _run_script(script, tmp_path, {
        "CASE": case, "TMPDIR": bash_path(tmp_path),
        "SCRIPT_BYTES": bash_path(candidate["root"] / "release-bundle" / "siteops-bootstrap.sh"),
        "BOOTSTRAP_ARGS": bash_path(arguments), "DOWNLOAD_ARGS": bash_path(downloads),
    })
    assert (result.returncode == 0) is (case == "success"), result.stdout + result.stderr
    assert arguments.exists() is (case in {"success", "installer-failed"})
    if arguments.exists():
        assert arguments.read_text().splitlines()[1:] == [
            "--release", "v1.0.0b8", "--source-commit", SHA,
            "--repository", REPO, "--source-ref", "refs/heads/main", "--caller", "release.yaml",
        ]
    download_args = downloads.read_text().splitlines()
    assert download_args[-1] == downloads_url("v1.0.0b8").removesuffix(ARCHIVE) + "siteops-bootstrap.sh"
    for option, value in (("--proto", "=https"), ("--proto-redir", "=https"),
                          ("--max-redirs", "3"), ("--max-time", "120")):
        assert download_args[download_args.index(option) + 1] == value
    assert "--tlsv1.2" in download_args and "--max-filesize" in download_args
    assert not list(tmp_path.glob("tmp.*/siteops-bootstrap.sh"))


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell command handling.")
@pytest.mark.parametrize("case", [
    "success", "download-failed", "truncated", "changed", "installer-failed", "acl-failed",
])
def test_generated_powershell_bootstrap_downloads_and_checks_before_execution(
    candidate, runner, tmp_path, case,
):
    command = _bootstrap_block(_render_install_notes(candidate, runner), "powershell")
    arguments, downloads = tmp_path / "bootstrap-args.json", tmp_path / "download-args.json"
    wrapper = tmp_path / "entry.ps1"
    wrapper.write_text(r"""
function curl.exe {
    ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) |
        Set-Content -LiteralPath $env:DOWNLOAD_ARGS
    $destination = $args[[Array]::IndexOf($args, '--output') + 1]
    $bytes = [IO.File]::ReadAllBytes($env:SCRIPT_BYTES)
    if ($env:CASE -eq 'truncated') { $bytes = $bytes[0..($bytes.Length - 2)] }
    if ($env:CASE -eq 'changed') { $bytes[0] = 88 }
    [IO.File]::WriteAllBytes($destination, $bytes)
    $global:LASTEXITCODE = if ($env:CASE -eq 'download-failed') { 22 } else { 0 }
}
function powershell.exe {
    ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) |
        Set-Content -LiteralPath $env:BOOTSTRAP_ARGS
    $global:LASTEXITCODE = if ($env:CASE -eq 'installer-failed') { 7 } else { 0 }
}
function uv { throw 'Unexpected manager invocation.' }
function pipx { throw 'Unexpected legacy manager invocation.' }
function gh { throw 'Unexpected authentication operation.' }
if ($env:CASE -eq 'acl-failed') {
    function icacls { $global:LASTEXITCODE = 5 }
}
""" + command, encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={
            **os.environ, "CASE": case, "TEMP": str(tmp_path),
            "SCRIPT_BYTES": str(candidate["root"] / "release-bundle" / "siteops-bootstrap.ps1"),
            "BOOTSTRAP_ARGS": str(arguments), "DOWNLOAD_ARGS": str(downloads),
        }, capture_output=True, text=True, timeout=120,
    )
    assert (result.returncode == 0) is (case == "success"), result.stdout + result.stderr
    assert arguments.exists() is (case in {"success", "installer-failed"})
    if arguments.exists():
        invoked = json.loads(arguments.read_text(encoding="utf-8-sig"))
        assert invoked[:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", invoked[4]]
        assert invoked[5:] == [
            "-Release", "v1.0.0b8", "-SourceCommit", SHA,
            "-Repository", REPO, "-SourceRef", "refs/heads/main", "-Caller", "release.yaml",
        ]
    assert downloads.exists() is (case != "acl-failed")
    if downloads.exists():
        download_args = json.loads(downloads.read_text(encoding="utf-8-sig"))
        assert download_args[-1] == downloads_url("v1.0.0b8").removesuffix(ARCHIVE) + "siteops-bootstrap.ps1"
        for option, value in (("--proto", "=https"), ("--proto-redir", "=https"),
                              ("--max-redirs", "3"), ("--max-time", "120")):
            assert download_args[download_args.index(option) + 1] == value
        assert "--tlsv1.2" in download_args and "--max-filesize" in download_args
    assert not list(tmp_path.glob("siteops-bootstrap-*/siteops-bootstrap.ps1"))


@pytest.fixture(scope="module")
def verified_notes(renderer):
    """Render the selected release's notes once for executable verified command cases."""
    from siteops_release_assets import FrozenReleaseAssets

    source = {"repository": REPO, "commit": SHA, "ref": "refs/heads/main"}
    assets = FrozenReleaseAssets.from_document({
        "apiVersion": "siteops.release.assets/v2", "kind": "SiteOpsReleaseAssets", "source": source,
        "assets": [{"name": name, "size": 7, "sha256": digest(name.encode())} for name in ENGINE_ASSETS],
        "engine": None,
    })
    plan = {"source": source, "release": {"tag": "v1.0.0b8"}, "siteops": {"bundle": True, "releaseTag": None}}
    return renderer.render_notes(
        plan, "## Changes\n", assets, engine_version="1.0.0b1", archive_name=ARCHIVE,
        attestation_suffix=".attestation.jsonl", runner_environment="self-hosted",
        builder_identity=f"https://github.com/{REPO}/.github/workflows/release.yaml@refs/heads/main",
    )


def _guide_verified_block(language):
    """Return the guide's verified example with this fixture's release selection."""
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    section = guide.split("### Verify the bootstrap script\n", 1)[1].split("\n### ", 1)[0]
    return _bootstrap_block(section, language).replace("<approved-release-tag>", "v1.0.0b8").replace(
        "<full-source-commit>", SHA,
    ).replace("Azure/digital-ops-scale-kit", REPO)


CERTIFICATE_MISMATCHES = {
    "subjectAlternativeName": f"https://github.com/{REPO}/.github/workflows/other.yaml@refs/heads/main",
    "issuer": "https://issuer.invalid",
    "sourceRepositoryURI": "https://github.com/other/publisher",
    "sourceRepositoryDigest": "d" * 40,
    "sourceRepositoryRef": "refs/heads/other",
    "buildSignerDigest": "d" * 40,
    "buildConfigURI": f"https://github.com/{REPO}/.github/workflows/ci.yaml@refs/heads/main",
    "buildConfigDigest": "d" * 40,
    "runnerEnvironment": "github-hosted",
}
EXPECTED_SCRIPT_QUERY = (
    'length > 0 and all(.[]; .verificationResult.mediaType == '
    '"application/vnd.dev.sigstore.verificationresult+json;version=0.1" and '
    '(.verificationResult.signature.certificate | .buildConfigURI == '
    f'"https://github.com/{REPO}/.github/workflows/release.yaml@refs/heads/main" and '
    f'.buildConfigDigest == "{SHA}" and .runnerEnvironment == "self-hosted"))'
)


def _script_observations(case):
    """Return synthetic evidence for a case. A certificate field name selects one changed field."""
    valid = verified_observation(
        REPO, SHA, "refs/heads/main", SIGNER_WORKFLOW, ".github/workflows/release.yaml",
    )
    changed = copy.deepcopy(valid)
    certificate = changed["verificationResult"]["signature"]["certificate"]
    if case == "empty":
        return []
    if case == "too-many":
        return [valid] * 129
    if case == "one-of-two":
        certificate["runnerEnvironment"] = "github-hosted"
        return [valid, changed]
    if case == "media-type":
        changed["verificationResult"]["mediaType"] = (
            "application/vnd.dev.sigstore.verificationresult+json;version=0.2"
        )
    elif case in CERTIFICATE_MISMATCHES:
        certificate[case] = CERTIFICATE_MISMATCHES[case]
    return [changed]


def _expected_gh_options():
    return {
        "--repo": REPO,
        "--cert-identity": f"https://github.com/{REPO}/{SIGNER_WORKFLOW}@refs/heads/main",
        "--source-ref": "refs/heads/main", "--source-digest": SHA, "--signer-digest": SHA,
        "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--predicate-type": "https://slsa.dev/provenance/v1", "--hostname": "github.com",
        "--digest-alg": "sha256", "--format": "json",
    }


def _gh_options(arguments):
    """Split one recorded verification call into its subject and unique options."""
    assert arguments[:2] == ["attestation", "verify"] and len(arguments) % 2 == 1
    options = dict(zip(arguments[3::2], arguments[4::2]))
    assert len(options) == len(arguments[3:]) // 2
    assert options.pop("--bundle") == arguments[2] + ".attestation.jsonl"
    return arguments[2], options


def _requested_assets(case, filename):
    base = downloads_url("v1.0.0b8").removesuffix(ARCHIVE)
    requested = [base + filename, base + filename + ".attestation.jsonl"]
    return requested[:1] if case == "script-download-failed" else requested


def _assert_bounded_downloads(calls):
    for call in calls:
        for option, value in (("--proto", "=https"), ("--proto-redir", "=https"),
                              ("--max-redirs", "3"), ("--max-time", "120")):
            assert call[call.index(option) + 1] == value
        assert "--tlsv1.2" in call and "--fail" in call


# Emulates gh's `--jq` evaluation for the policy shape the commands use and rejects any other call.
GH_POLICY_EVALUATOR = r'''import json
import os
import re
import sys
from pathlib import Path


def reject(reason):
    print(reason, file=sys.stderr)
    raise SystemExit(99)


arguments = Path(os.environ["GH_ARGS"]).read_bytes().decode("utf-8").split("\0")[:-1]
subject, names, values = arguments[2], arguments[3::2], arguments[4::2]
if len(names) != len(values) or len(set(names)) != len(names):
    reject("Unexpected gh arguments.")
options = dict(zip(names, values))
query = options.pop("--jq", "")
expected = json.loads(Path(os.environ["GH_EXPECTED"]).read_text(encoding="utf-8"))
if options != {**expected, "--bundle": subject + ".attestation.jsonl"}:
    reject("Unexpected gh options.")
policy = re.fullmatch(
    r'length > 0 and all\(\.\[\]; \.verificationResult\.mediaType == "([^"]+)" and '
    r'\(\.verificationResult\.signature\.certificate \| (.+)\)\)',
    query,
)
if policy is None:
    reject("Unexpected gh policy.")
checks = [re.fullmatch(r'\.([A-Za-z]+) == "([^"]*)"', term) for term in policy[2].split(" and ")]
if not all(checks):
    reject("Unexpected gh policy.")
observations = json.loads(Path(os.environ["OBSERVATIONS"]).read_text(encoding="utf-8"))
print(json.dumps(len(observations) > 0 and all(
    item["verificationResult"]["mediaType"] == policy[1] and all(
        item["verificationResult"]["signature"]["certificate"].get(check[1]) == check[2]
        for check in checks
    )
    for item in observations
)))
'''
BASH_VERIFY_DOUBLES = r"""
curl() {
    local output='' url="${!#}"
    printf '%s\n' "$@" --end-- >> "$DOWNLOAD_ARGS"
    while (($#)); do
        if [[ "$1" == --output ]]; then output="$2"; shift 2; else shift; fi
    done
    case "$url" in
      "${DOWNLOADS}siteops-bootstrap.sh")
        [[ "$CASE" != script-download-failed ]] || return 22
        cp "$SCRIPT_BYTES" "$output" ;;
      "${DOWNLOADS}siteops-bootstrap.sh.attestation.jsonl")
        [[ "$CASE" != proof-download-failed ]] || return 22
        cp "$PROOF_BYTES" "$output" ;;
      *) echo 'Unexpected download.' >&2; return 99 ;;
    esac
}
gh() {
    printf '%s\0' "$@" > "$GH_ARGS"
    [[ "${1-} ${2-}" == 'attestation verify' ]] || { echo 'Unexpected gh command.' >&2; return 99; }
    [[ "$CASE" != gh-failed ]] || return 1
    [[ "$CASE" != no-output ]] || return 0
    cmp -s -- "$3" "$SCRIPT_BYTES" && cmp -s -- "$3.attestation.jsonl" "$PROOF_BYTES" || return 1
    "$FAKE_PYTHON" "$GH_EVALUATOR"
}
bash() {
    printf '%s\n' "$@" > "$BOOTSTRAP_ARGS"
    [[ "$CASE" != installer-failed ]]
}
uv() { echo 'Unexpected manager invocation.' >&2; return 98; }
pipx() { echo 'Unexpected legacy manager invocation.' >&2; return 98; }
powershell() { echo 'Unexpected shell invocation.' >&2; return 98; }
"""
BASH_VERIFY_CASES = (
    "success", "installer-failed", "script-download-failed", "proof-download-failed", "gh-failed",
    "no-output", "empty", "one-of-two", "media-type", "buildConfigURI", "buildConfigDigest",
    "runnerEnvironment",
)
GUIDE_BASH_VERIFY_CASES = (
    "success", "gh-failed", "empty", "one-of-two", "media-type", "buildConfigURI",
    "buildConfigDigest", "runnerEnvironment",
)


@pytest.mark.parametrize(("source", "case"), [
    *(("notes", case) for case in BASH_VERIFY_CASES),
    *(("guide", case) for case in GUIDE_BASH_VERIFY_CASES),
])
def test_verified_bash_bootstrap_runs_the_script_only_after_a_matching_proof(
    verified_notes, tmp_path, source, case,
):
    command = _verified_block(verified_notes, "bash") if source == "notes" else _guide_verified_block("bash")
    script_bytes, proof_bytes = tmp_path / "script.sh", tmp_path / "script.sh.attestation.jsonl"
    script_bytes.write_bytes(b"echo SCRIPT_RAN\n")
    proof_bytes.write_bytes(b'{"synthetic": "proof"}\n')
    evaluator, expected = tmp_path / "gh-evaluator.py", tmp_path / "gh-expected.json"
    evaluator.write_text(GH_POLICY_EVALUATOR, encoding="utf-8")
    expected.write_text(json.dumps(_expected_gh_options()), encoding="utf-8")
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps(_script_observations(case)), encoding="utf-8")
    verified, installed, downloads = (
        tmp_path / "gh-args.bin", tmp_path / "installer-args.txt", tmp_path / "download-args.txt",
    )
    result = _run_script(BASH_VERIFY_DOUBLES + command, tmp_path, {
        "CASE": case, "TMPDIR": bash_path(tmp_path),
        "DOWNLOADS": downloads_url("v1.0.0b8").removesuffix(ARCHIVE),
        "SCRIPT_BYTES": bash_path(script_bytes), "PROOF_BYTES": bash_path(proof_bytes),
        "FAKE_PYTHON": Path(sys.executable).as_posix(), "GH_EVALUATOR": evaluator.as_posix(),
        "GH_ARGS": verified.as_posix(), "GH_EXPECTED": expected.as_posix(),
        "OBSERVATIONS": observations.as_posix(),
        "BOOTSTRAP_ARGS": bash_path(installed), "DOWNLOAD_ARGS": bash_path(downloads),
    })
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is (case == "success"), output
    assert "Unexpected" not in output
    assert installed.exists() is (case in {"success", "installer-failed"}), output
    if installed.exists():
        invoked = installed.read_text(encoding="utf-8").splitlines()
        assert invoked[0].endswith("/siteops-bootstrap.sh")
        assert invoked[1:] == ["--release", "v1.0.0b8", "--source-commit", SHA] + (
            ["--repository", REPO, "--source-ref", "refs/heads/main", "--caller", "release.yaml"]
            if source == "notes" else []
        )
    calls = [call.splitlines() for call in downloads.read_text(encoding="utf-8").split("--end--\n") if call]
    assert [call[-1] for call in calls] == _requested_assets(case, "siteops-bootstrap.sh")
    _assert_bounded_downloads(calls)
    assert verified.exists() is (case not in {"script-download-failed", "proof-download-failed"})
    if verified.exists():
        subject, options = _gh_options(verified.read_bytes().decode("utf-8").split("\0")[:-1])
        assert subject.endswith("/siteops-bootstrap.sh")
        assert options.pop("--jq") == EXPECTED_SCRIPT_QUERY
        assert options == _expected_gh_options()
    if source == "notes":
        assert not list(tmp_path.glob("tmp.*"))


POWERSHELL_VERIFY_DOUBLES = r"""
function curl.exe {
    $call = @($args | ForEach-Object { [string]$_ })
    Add-Content -LiteralPath $env:DOWNLOAD_ARGS -Encoding UTF8 -Value (ConvertTo-Json -Compress -InputObject $call)
    $output = $call[[Array]::IndexOf($call, '--output') + 1]
    if ($call[-1] -ceq $env:DOWNLOADS + 'siteops-bootstrap.ps1') {
        $source = $env:SCRIPT_BYTES; $failure = 'script-download-failed'
    } elseif ($call[-1] -ceq $env:DOWNLOADS + 'siteops-bootstrap.ps1.attestation.jsonl') {
        $source = $env:PROOF_BYTES; $failure = 'proof-download-failed'
    } else { throw 'Unexpected download.' }
    if ($env:CASE -eq $failure) { $global:LASTEXITCODE = 22; return }
    [IO.File]::WriteAllBytes($output, [IO.File]::ReadAllBytes($source))
    $global:LASTEXITCODE = 0
}
function Test-SameBytes([string]$Left, [string]$Right) {
    [Convert]::ToBase64String([IO.File]::ReadAllBytes($Left)) -ceq
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($Right))
}
function gh.exe {
    $call = @($args | ForEach-Object { [string]$_ })
    ConvertTo-Json -InputObject $call | Set-Content -LiteralPath $env:GH_ARGS -Encoding UTF8
    if ($call.Count -lt 3 -or $call[0] -cne 'attestation' -or $call[1] -cne 'verify') { throw 'Unexpected gh command.' }
    $bundle = $call[[Array]::IndexOf($call, '--bundle') + 1]
    if ($env:CASE -eq 'gh-failed' -or -not (Test-SameBytes $call[2] $env:SCRIPT_BYTES) -or
        -not (Test-SameBytes $bundle $env:PROOF_BYTES)) {
        $global:LASTEXITCODE = 1; return
    }
    if ($env:CASE -eq 'oversized') { 'x' * 8388609 }
    elseif ($env:CASE -ne 'no-output') { Get-Content -LiteralPath $env:OBSERVATIONS -Raw }
    $global:LASTEXITCODE = 0
}
function powershell.exe {
    ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) |
        Set-Content -LiteralPath $env:BOOTSTRAP_ARGS -Encoding UTF8
    $global:LASTEXITCODE = if ($env:CASE -eq 'installer-failed') { 7 } else { 0 }
}
function uv { throw 'Unexpected manager invocation.' }
function pipx { throw 'Unexpected legacy manager invocation.' }
function bash { throw 'Unexpected shell invocation.' }
"""
POWERSHELL_VERIFY_FAILURES = {
    "installer-failed": "Site Ops installation did not complete.",
    "script-download-failed": "could not be downloaded.",
    "proof-download-failed": "could not be downloaded.",
    "gh-failed": "Script verification failed.",
    "no-output": "Script verification failed.",
    "empty": "Script verification returned an unsupported result count.",
    "too-many": "Script verification returned an unsupported result count.",
    "oversized": "Verification evidence is too large.",
    "media-type": "Unsupported verified script observation.",
    "one-of-two": "The verified script certificate differs from the selected release.",
    **dict.fromkeys(
        CERTIFICATE_MISMATCHES, "The verified script certificate differs from the selected release.",
    ),
}
GUIDE_POWERSHELL_VERIFY_CASES = (
    "success", "empty", "too-many", "oversized", "one-of-two", "media-type", *CERTIFICATE_MISMATCHES,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell command handling.")
@pytest.mark.parametrize(("source", "case"), [
    *(("notes", case) for case in ("success", *POWERSHELL_VERIFY_FAILURES)),
    *(("guide", case) for case in GUIDE_POWERSHELL_VERIFY_CASES),
])
def test_verified_powershell_bootstrap_runs_the_script_only_after_a_matching_proof(
    verified_notes, tmp_path, source, case,
):
    command = (
        _verified_block(verified_notes, "powershell") if source == "notes"
        else _guide_verified_block("powershell")
    )
    script_bytes, proof_bytes = tmp_path / "script.ps1", tmp_path / "script.ps1.attestation.jsonl"
    script_bytes.write_bytes(b"'SCRIPT_RAN'\r\n")
    proof_bytes.write_bytes(b'{"synthetic": "proof"}\n')
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps(_script_observations(case)), encoding="utf-8")
    verified, installed, downloads = (
        tmp_path / "gh-args.json", tmp_path / "installer-args.json", tmp_path / "download-args.jsonl",
    )
    wrapper = tmp_path / "entry.ps1"
    wrapper.write_text(POWERSHELL_VERIFY_DOUBLES + command, encoding="utf-8")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={
            **os.environ, "CASE": case, "TEMP": str(tmp_path),
            "DOWNLOADS": downloads_url("v1.0.0b8").removesuffix(ARCHIVE),
            "SCRIPT_BYTES": str(script_bytes), "PROOF_BYTES": str(proof_bytes),
            "OBSERVATIONS": str(observations), "GH_ARGS": str(verified),
            "BOOTSTRAP_ARGS": str(installed), "DOWNLOAD_ARGS": str(downloads),
        }, capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is (case == "success"), output
    assert "Unexpected" not in output
    if case != "success":
        assert POWERSHELL_VERIFY_FAILURES[case] in " ".join(output.split()), output
    assert installed.exists() is (case in {"success", "installer-failed"}), output
    if installed.exists():
        invoked = json.loads(installed.read_text(encoding="utf-8-sig"))
        assert invoked[:4] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        assert invoked[4].endswith("\\siteops-bootstrap.ps1")
        assert invoked[5:] == ["-Release", "v1.0.0b8", "-SourceCommit", SHA] + (
            ["-Repository", REPO, "-SourceRef", "refs/heads/main", "-Caller", "release.yaml"]
            if source == "notes" else []
        )
    calls = [json.loads(line) for line in downloads.read_text(encoding="utf-8-sig").splitlines()]
    assert [call[-1] for call in calls] == _requested_assets(case, "siteops-bootstrap.ps1")
    _assert_bounded_downloads(calls)
    assert verified.exists() is (case not in {"script-download-failed", "proof-download-failed"})
    if verified.exists():
        subject, options = _gh_options(json.loads(verified.read_text(encoding="utf-8-sig")))
        assert subject.endswith("\\siteops-bootstrap.ps1")
        assert options == _expected_gh_options()
    if source == "notes":
        assert not list(tmp_path.glob("siteops-bootstrap-*"))


def test_installation_guides_name_the_rendered_release_note_labels(verified_notes):
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    guide = " ".join((ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8").split())
    # The short README route leaves script checks and uv instructions in the rendered notes.
    for label, rendered in (
        ("Install Site Ops", "\n## Install Site Ops\n"),
        ("Already have uv", "\n### Already have uv\n"),
        ("Bootstrap without uv", "\n### Bootstrap without uv\n"),
    ):
        assert rendered in verified_notes
        if label == "Already have uv":
            assert f"**{label}** section" in readme
        elif label == "Install Site Ops":
            assert "### 1. Install Site Ops and approve the official source" in readme
        else:
            assert "### Bootstrap from HTTPS" in (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    assert 'bash siteops-bootstrap.sh --release "<release>" --enroll-source official' in readme
    assert r"-File .\siteops-bootstrap.ps1" in readme
    assert '-Release "<release>" -EnrollSource official' in readme
    assert VERIFIED in verified_notes
    for label in ("Install Site Ops", "Verify the script before it runs"):
        assert f"`{label}`" in guide


@pytest.mark.parametrize(
    ("authored", "expected"),
    [
        ("# Main title\n\n## Changes\n\nBody.\n", ["### Main title", "#### Changes"]),
        ("## Changes\n\nBody.\n", ["### Changes"]),
        ("Title\n=====\n\nChanges\n-------\n\nBody.\n", ["### Title", "#### Changes"]),
        ("# Title\n\n```bash\n# Leave this comment alone\n```\n\n~~~\n# Also literal\n~~~\n", ["### Title"]),
        ("# Title\n\n---\n---\n\n    # Indented example\n", ["### Title"]),
    ],
)
def test_summary_nests_markdown_headings_without_changing_published_notes(
    candidate, renderer, authored, expected,
):
    before = authored.encode()
    summary = renderer.render_summary(candidate["plan"], authored, summary_values(DRY_RUN="true"))
    assert summary.startswith("# Release preview")
    assert "\n## Release notes\n" in summary
    for heading in expected:
        assert "\n" + heading + "\n" in summary
    for literal in ("# Leave this comment alone", "# Also literal", "    # Indented example", "---\n---"):
        if literal in authored:
            assert literal in summary
    assert authored.encode() == before


def test_release_notes_join_soft_wrapped_paragraphs_and_list_items(renderer):
    authored = (
        "## Highlights\n\n"
        "Site Ops now installs from a\nsigned release.\n\n"
        "- Devices and assets deploy\n  from resource sets.\n"
        "  - Nested items keep\n    their own lines.\n"
        "1. Upgrade the templates\n   first.\n\n"
        "  A list paragraph after a blank\n  line also joins.\n\n"
        "Inline ```code``` spans\nstill join.\n"
    )
    assert renderer.unwrap_paragraphs(authored) == (
        "## Highlights\n\n"
        "Site Ops now installs from a signed release.\n\n"
        "- Devices and assets deploy from resource sets.\n"
        "  - Nested items keep their own lines.\n"
        "1. Upgrade the templates first.\n\n"
        "  A list paragraph after a blank line also joins.\n\n"
        "Inline ```code``` spans still join.\n"
    )


@pytest.mark.parametrize("authored", [
    "Before\n```bash\nfirst line\nsecond line\n```\nAfter\n",
    "~~~~\n~~~\nstill fenced\n~~~~\n",
    "| Column | Value |\n| --- | --- |\n| a | b |\n",
    "<details><summary>More</summary>\nraw line\nanother\n</details>\n",
    "> Quoted line\n> and its continuation\n",
    "Hard break with spaces  \nnext line\n",
    "Hard break with a backslash\\\nnext line\n",
    "Title\n=====\n",
    "Title\n-----\n",
    "## Heading\nFirst paragraph line\n",
    "Text\n\n    indented code\n    second code line\n",
    "[guide]: https://example.invalid/guide\n[other]: https://example.invalid/other\n",
    "Text\n***\nMore\n",
    "Step text\n2. not a list here\n",
    "- ~~~bash\n  echo one\n  ~~~\n",
    "- ## Upgrade\n  Details\n",
    "- - -\nText\n",
    "~~~\nfirst\n    ~~~\nsecond\nthird\n~~~\n",
    "<pre>\n\nfirst line\nsecond line\n</pre>\n",
    "<!-- note\n\nfirst\nsecond\n-->\n",
    "<!NOTE first\n\nsecond\nthird\n>\n",
    "Use `--input \"label=one \ntwo\"` here\n",
    "Text\n\n    - indented code\n    more\n",
    "- ~~~bash\n  echo one\n  echo two\n  ~~~\n",
    "-     echo one\n      echo two\n",
    "1.     echo one\n       echo two\n",
    " ~~~\nfirst\n    ~~~\nsecond\nthird\n~~~\n",
    "- item\n\n    ```\n    code\n\n```\nnew fence a\nnew fence b\n```\n",
    "- <pre>\n  first\n  second\n  </pre>\n",
    "Intro <pre>\nfirst\nsecond\n</pre>\n",
    "<div>\n<pre>\n\nfirst\nsecond\n</pre>\n</div>\n",
    "A wrapped\r\nline\r\n",
])
def test_release_notes_keep_lines_whose_structure_matters(renderer, authored):
    assert renderer.unwrap_paragraphs(authored) == authored


@pytest.mark.parametrize("path", sorted(ROOT.glob("releases/*/notes.md")), ids=lambda path: path.parent.name)
def test_authored_release_notes_unwrap_without_changing_their_text(renderer, path):
    authored = path.read_text(encoding="utf-8")
    unwrapped = renderer.unwrap_paragraphs(authored)
    assert "".join(unwrapped.split()) == "".join(authored.split())
    assert renderer.unwrap_paragraphs(unwrapped) == unwrapped
    fenced = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
    assert fenced.findall(unwrapped) == fenced.findall(authored)


def test_published_notes_unwrap_the_authored_text_only(verified_notes, renderer):
    plan_notes = verified_notes
    assert plan_notes.startswith("## Changes\n\n## Install Site Ops\n\n")
    source = {"repository": REPO, "commit": SHA, "ref": "refs/heads/main"}
    from siteops_release_assets import FrozenReleaseAssets

    assets = FrozenReleaseAssets.from_document({
        "apiVersion": "siteops.release.assets/v2", "kind": "SiteOpsReleaseAssets", "source": source,
        "assets": [{"name": name, "size": 7, "sha256": digest(name.encode())} for name in ENGINE_ASSETS],
        "engine": None,
    })
    plan = {"source": source, "release": {"tag": "v1.0.0b8"}, "siteops": {"bundle": True, "releaseTag": None}}
    notes = renderer.render_notes(
        plan, "## Highlights\n\nA wrapped\nparagraph.\n", assets, engine_version="1.0.0b1",
        archive_name=ARCHIVE, attestation_suffix=".attestation.jsonl", runner_environment="self-hosted",
        builder_identity=f"https://github.com/{REPO}/.github/workflows/release.yaml@refs/heads/main",
    )
    assert notes.startswith("## Highlights\n\nA wrapped paragraph.\n\n## Install Site Ops\n\n")
    assert notes.split("## Install Site Ops\n\n", 1)[1] == plan_notes.split("## Install Site Ops\n\n", 1)[1]


@pytest.mark.parametrize("matrix", [
    "[]", "{}", '[null]', '[{"python":"3.10"}]',
    json.dumps([{"python": version, "linux": "passed", "windows": "passed"}
                for version in ("3.10", "3.11", "3.12", "3.13", "3.14")]),
])
def test_invalid_rendering_inputs_leave_existing_summary_unchanged(candidate, runner, matrix):
    _render_install_notes(candidate, runner)
    summary = candidate["root"].parent / "summary.md"
    original = "Earlier job summary\n"
    summary.write_text(original, encoding="utf-8")
    result, _, _ = runner(
        "review", "Show the release approval preview", extra=summary_values(MATRIX=matrix),
    )
    assert result.returncode != 0
    assert "Incomplete installation qualification summary" in result.stdout + result.stderr
    assert summary.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(("cells", "valid"), [
    ({"3.11": {"windows-standard-user": "failed", "ubuntu-26.04": "not-run"}}, True),
    ({"3.12": {"ubuntu-26.04": "passed"}}, False),
    ({"3.10": {"windows-standard-user": "unknown"}}, False),
    ({"3.11": {"windows-standard-user": "n/a"}}, False),
    ({"3.11": {"ubuntu-26.04": "n/a"}}, False),
    ({"3.13": {"linux": "n/a"}}, False),
])
def test_summary_renders_extra_installer_cells_only_for_their_python(candidate, renderer, cells, valid):
    values = summary_values(MATRIX=json.dumps(qualification_matrix(**cells)))
    if not valid:
        with pytest.raises(renderer.RenderingError, match="Invalid qualification summary"):
            renderer.render_summary(candidate["plan"], "## Changes\n", values)
        return
    summary = renderer.render_summary(candidate["plan"], "## Changes\n", values)
    assert "| 3.11 | passed | not-run | passed | failed |" in summary
    assert "| 3.14 | passed | n/a | passed | n/a |" in summary


@pytest.mark.parametrize("bundle", [False, True])
def test_publication_uploads_only_declared_assets(candidate, runner, bundle):
    if not bundle:
        _reference_inventory(candidate)
    result, _, calls = runner(
        "publish", "Publish the approved release",
        extra={"TITLE": "Example release", "PRERELEASE": "true", "LATEST": "false", "BUNDLE": str(bundle).lower()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    command = calls[-1]
    assert command[:3] == ["release", "create", "v1.0.0b8"]
    assert "--verify-tag" in command and "--latest=false" in command
    assert "--target" not in command and "--clobber" not in command
    assert any(value.endswith(ARCHIVE) for value in command) is bundle
    assert any(value.endswith(PROOF) for value in command) is bundle
    assert any(value.endswith(WHEEL) for value in command) is bundle
    assert any(value.endswith(WHEEL_PROOF) for value in command) is bundle
    for name in BOOTSTRAP:
        assert any(value.endswith(name) for value in command) is bundle
    asset_arguments = [
        value for value in command
        if Path(value).name in ENGINE_ASSETS
    ]
    assert len(asset_arguments) == (8 if bundle else 0)


@pytest.mark.parametrize("immutable, deleted", [(False, True), (False, False), (True, None)])
def test_published_asset_digests_and_immutable_release_are_checked(candidate, runner, immutable, deleted):
    tag = candidate["plan"]["release"]["tag"]
    assets = [
        {"name": name, "size": (candidate["root"] / "release-bundle" / name).stat().st_size,
         "digest": "sha256:" + digest((candidate["root"] / "release-bundle" / name).read_bytes()), "state": "uploaded"}
        for name in ENGINE_ASSETS
    ]
    endpoint = f"repos/{REPO}/releases/tags/{tag}"
    candidate["responses"][endpoint] = {
        "status": 200,
        "body": {"id": 42, "tag_name": tag, "draft": False, "prerelease": True, "immutable": immutable,
                 "assets": assets},
    }
    candidate["responses"][f"repos/{REPO}/releases/42"] = {"status": 204 if deleted else 500, "method": "DELETE"}
    result, _, calls = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    output = result.stdout + result.stderr
    # A published release must be immutable, so its assets cannot change after approval.
    assert (result.returncode == 0) is immutable, output
    summary = (candidate["root"].parent / "summary.md").read_text(encoding="utf-8")
    assert f"- Immutable: `{str(immutable).lower()}`" in summary
    assert any(call[:2] == ["release", "verify"] for call in calls) is immutable
    assert len([call for call in calls if call[:2] == ["release", "verify-asset"]]) == (8 if immutable else 0)
    # The workflow cannot read the repository setting first, so it removes a mutable release.
    delete = ["api", "--method", "DELETE", f"repos/{REPO}/releases/42"]
    assert (delete in calls) is not immutable
    assert ("::error::The published release is not immutable, so it was deleted and its tag kept."
            in output) is (deleted is True)
    assert ("::error::The published release is not immutable and could not be deleted." in output) is (deleted is False)
    if not immutable:
        assert f"- Mutable release deleted, tag kept: `{str(deleted).lower()}`" in summary
        assert not any(call[:2] == ["api", "--method"] and "git/refs" in " ".join(call) for call in calls)
    candidate["responses"][endpoint]["body"]["immutable"] = True
    assets[0]["digest"] = "sha256:" + "a" * 64
    result, _, _ = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    assert result.returncode != 0
    assets[0]["digest"] = "sha256:" + digest(
        (candidate["root"] / "release-bundle" / ARCHIVE).read_bytes()
    )
    assets.append({"name": "extra.txt", "digest": "sha256:" + "b" * 64, "state": "uploaded"})
    result, _, _ = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    assert result.returncode != 0


@pytest.mark.parametrize("built", [False, True])
def test_complete_workspace_candidate_reaches_only_the_approved_publication_set(candidate, runner, tmp_path, built):
    extra = _workspace_publication(candidate, tmp_path, built=built)
    result, outputs, _ = runner("review", "Freeze the complete qualified publication payload", extra=extra)
    assert result.returncode == 0, result.stdout + result.stderr
    root = candidate["root"]
    inventory = json.loads((root / "final-release-assets/release-assets.json").read_bytes())
    assert outputs["asset-list-sha"] == digest((root / "final-release-assets/release-assets.json").read_bytes())
    assert len(inventory["assets"]) == (13 if built else 5)
    result, _, _ = runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (root / "publish-notes.md").read_text()
    assert "## Workspace content" in notes and "siteops-workspaces.json" in notes
    # The README quick start sends readers to this section for the exact deploy command.
    assert "**Workspace content**" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"https://github.com/{REPO}/blob/{SHA}/docs/projects.md#run-project-pin" in notes
    assert "`siteops project pin` with `--release`" in notes
    workspace_section = notes.split("## Workspace content", 1)[1]
    tag = candidate["plan"]["release"]["tag"]
    deploy = f'siteops deploy <manifest> --source "example@{tag}" --input-file <answers.yaml>'
    assert workspace_section.index(deploy) < workspace_section.index("For a repeatable fleet")
    if not built:
        assert f"```console\nsiteops source enroll example --source github:{REPO}\n{deploy}\n```" in notes
    assert workspace_section.rstrip().endswith(
        "Workspace qualification used the selected installed engine to check package "
        "compatibility, protected cache use, and guarded catalog loading."
    )
    assert "did not" not in workspace_section and "authorize targets" not in notes
    (root / "release-bundle").rename(root / "review-native")
    candidate["native_directory"] = "review-native"
    shutil.copytree(root / "release-payload", root / "release-bundle")
    (root / "release-assets/release-assets.json").write_bytes((root / "final-release-assets/release-assets.json").read_bytes())
    for name in ("Verify the approved candidate", "Verify the approved release assets"):
        result, _, calls = runner("publish", name, extra=extra)
        assert result.returncode == 0, result.stdout + result.stderr
        if name == "Verify the approved release assets":
            assert any(call[:2] == ["attestation", "verify"] and Path(call[2]).name == "siteops-engine.json"
                       for call in calls)
    result, _, calls = runner("publish", "Verify the approved workspace subjects and descriptor", extra=extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call[:2] == ["attestation", "verify"] and Path(call[2]).name == "workspace.zip" for call in calls)
    result, _, calls = runner("publish", "Publish the approved release", extra={
        "TITLE": candidate["plan"]["release"]["title"], "PRERELEASE": "true", "LATEST": "false",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    command = calls[-1]
    assert {Path(value).name for value in command if value.startswith(str(root))} >= {
        asset["name"] for asset in inventory["assets"]
    }
    tag = candidate["plan"]["release"]["tag"]
    candidate["responses"][f"repos/{REPO}/releases/tags/{tag}"] = {
        "status": 200, "body": {
            "tag_name": tag, "draft": False, "prerelease": True, "immutable": True,
            "assets": [{**asset, "digest": "sha256:" + asset["sha256"], "state": "uploaded"}
                       for asset in inventory["assets"]],
        },
    }
    result, _, calls = runner("publish", "Confirm published assets and immutability", extra={"PRERELEASE": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert len([call for call in calls if call[:2] == ["release", "verify-asset"]]) == len(inventory["assets"])


@pytest.mark.parametrize("fault", ["descriptor", "descriptor-source", "source", "kit", "proof"])
def test_workspace_publisher_rechecks_routing_and_verified_metadata(candidate, runner, tmp_path, fault):
    _workspace_publication(candidate, tmp_path)
    root = candidate["root"]
    workspace = root / "release-workspaces"
    inventory_path = root / "release-assets/release-assets.json"
    native = json.loads(inventory_path.read_bytes())
    assets = json.loads((workspace / "release-assets.json").read_bytes())["assets"]
    for asset in assets:
        shutil.copyfile(workspace / asset["name"], root / "release-bundle" / asset["name"])
    native["assets"].extend(assets)
    if fault in {"descriptor", "descriptor-source"}:
        path = root / "release-bundle/siteops-workspaces.json"
        data = json.loads(path.read_bytes())
        if fault == "descriptor":
            data["workspaces"][0]["kit"]["version"] = "wrong"
        else:
            data["source"]["revision"] = "d" * 40
        path.write_text(json.dumps(data))
    elif fault in {"source", "kit"}:
        path = root / "release-bundle/workspace.zip"
        with zipfile.ZipFile(path) as archive:
            contents = [(entry, archive.read(entry)) for entry in archive.infolist()]
        with zipfile.ZipFile(path, "w") as archive:
            for entry, raw in contents:
                if entry.filename == "siteops-package.json":
                    metadata = json.loads(raw)
                    if fault == "source":
                        metadata["source"]["revision"] = "d" * 40
                    else:
                        metadata["kit"]["id"] = "other"
                    raw = json.dumps(metadata).encode()
                archive.writestr(entry, raw)
    inventory_path.write_text(json.dumps(native))
    extra = {}
    if fault == "proof":
        extra["FAIL_ATTESTATION_SUBJECT"] = "workspace.zip"
        result, _, calls = runner("publish", "Verify the approved release assets", extra=extra)
        assert result.returncode != 0
        assert any(call[:2] == ["attestation", "verify"] and Path(call[2]).name == "workspace.zip" for call in calls)
        return
    result, _, calls = runner("publish", "Verify the approved workspace subjects and descriptor", extra=extra)
    assert result.returncode != 0
    if fault in {"source", "kit"}:
        assert "verified workspace differs from the reviewed source contract" in result.stdout + result.stderr
    assert not any("--method" in call or call[:2] == ["release", "create"] for call in calls)


# --- Candidate acceptance gate ------------------------------------------------

ADMISSION = "a" * 64
ACCEPTANCE_RUN = 7000
RUNS = (
    f"repos/{REPO}/actions/workflows/e2e-test.yaml/runs?event=workflow_dispatch&branch=main"
    f"&head_sha={SHA}&per_page=100"
)
MAIN_REF = f"repos/{REPO}/git/ref/heads/main"
DISPATCH = f"repos/{REPO}/actions/workflows/e2e-test.yaml/dispatches"
LISTING = {"status": 200, "method": "GET", "flags": ["--paginate", "--slurp"]}


def _contract_constant(name):
    """Read a literal from the acceptance producer without importing its runtime dependencies."""
    tree = ast.parse((ROOT / "scripts" / "release_acceptance.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"The acceptance producer defines no {name}.")


def _fleet_candidate(candidate, **producer):
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate",
        "source": {"repository": REPO, "commit": SHA, "ref": "refs/heads/main"},
        "producer": {"run": 42, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False,
                     **producer},
        "artifacts": {
            "admission": {"id": 501, "sha256": ADMISSION},
            "plan": {"id": 502, "sha256": digest(json.dumps(candidate["plan"]).encode())},
            "inventory": {"id": 503, "sha256": digest(
                (candidate["root"] / "release-assets" / "release-assets.json").read_bytes())},
            "engine": {"id": 504, "sha256": "e" * 64}, "workspaces": {"id": 505, "sha256": "f" * 64},
        },
    }


def _acceptance_run(run_id=ACCEPTANCE_RUN, attempt=1, **fields):
    return {
        "id": run_id, "run_attempt": attempt, "path": ".github/workflows/e2e-test.yaml",
        "event": "workflow_dispatch", "head_branch": "main", "head_sha": SHA,
        "repository": {"full_name": REPO}, "display_title": "Release acceptance",
        "status": "completed", "conclusion": "success", **fields,
    }


def _receipt_artifact(run_id=ACCEPTANCE_RUN, attempt=1, admission=ADMISSION, **fields):
    return {
        "id": run_id * 10 + attempt, "name": f"release-acceptance-{run_id}-{attempt}-{admission}",
        "expired": False, "size_in_bytes": 900, "digest": "sha256:" + "d" * 64,
        "workflow_run": {"id": run_id, "head_sha": SHA}, **fields,
    }


def _serve_acceptance(candidate, runs, artifacts):
    candidate["responses"][RUNS] = {**LISTING, "body": [{"total_count": len(runs), "workflow_runs": runs}]}
    for run in runs:
        listed = artifacts.get(run["id"], [])
        # Two pages prove that every page of the artifact list is read.
        candidate["responses"][f"repos/{REPO}/actions/runs/{run['id']}/artifacts?per_page=100"] = {
            **LISTING, "body": [{"total_count": len(listed), "artifacts": listed[:1]},
                                {"total_count": len(listed), "artifacts": listed[1:]}],
        }


def _release_receipt(candidate, *, run=ACCEPTANCE_RUN, attempt=1, producer_attempt=1, bundle=True):
    assertions = _contract_constant("ASSERTIONS")
    rows = []
    for name in _contract_constant("SCENARIOS"):
        installer = name == "installer"
        rows.append({
            "scenario": name, "status": "not-applicable" if installer and not bundle else "passed",
            "groups": "not-applicable" if installer else ("ephemeral" if name == "fleet" else "persistent"),
            "assertions": [] if installer and not bundle else list(assertions[name]),
            "cleanup": "not-applicable" if installer else "confirmed-absent",
            # Vault purge never gates publication.
            "vaultPurge": "failed" if name == "site-combined" else "not-applicable",
        })
    selection = _fleet_candidate(candidate, attempt=producer_attempt)
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "ReleaseAcceptance",
        "candidate": {
            "repository": REPO, "sourceCommit": SHA, "sourceRef": "refs/heads/main",
            "caller": ".github/workflows/release.yaml", "producerRun": 42, "producerAttempt": producer_attempt,
            "preview": False, "admissionSha256": ADMISSION,
            "planSha256": selection["artifacts"]["plan"]["sha256"],
            "inventorySha256": selection["artifacts"]["inventory"]["sha256"],
        },
        "acceptance": {"run": run, "attempt": attempt}, "environment": "dev", "transport": "prepublication",
        "publicRelease": "not-observed", "status": "passed", "scenarios": rows,
        "workloadFunctionality": "not-checked", "secretMaterialization": "not-checked",
    }


def _locate(runner, candidate, *, selection=None, run_attempt="1"):
    return runner("publish", "Locate the governing acceptance run", extra={
        "CANDIDATE": json.dumps(selection or _fleet_candidate(candidate)), "ADMISSION_ARTIFACT_ID": "501",
        "GITHUB_RUN_ATTEMPT": run_attempt,
    })


def _verify(runner, candidate, receipt, *, run_attempt="1", producer_attempt=1, acceptance_attempt=1,
            extra_file=False):
    _serve_acceptance(candidate, [_acceptance_run(attempt=acceptance_attempt)], {
        ACCEPTANCE_RUN: [_receipt_artifact(attempt=acceptance_attempt)],
    })
    located, outputs, before = _locate(
        runner, candidate, selection=_fleet_candidate(candidate, attempt=producer_attempt), run_attempt=run_attempt,
    )
    assert located.returncode == 0, located.stdout + located.stderr
    directory = candidate["root"] / "release-acceptance"
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir()
    (directory / "release-acceptance.json").write_text(json.dumps(receipt), encoding="utf-8")
    if extra_file:
        (directory / "notes.txt").write_text("unexpected", encoding="utf-8")
    result, _, calls = runner("publish", "Verify the bound acceptance", extra={"GITHUB_RUN_ATTEMPT": run_attempt})
    return result, calls[len(before):]


def _inline_literal(job, name, variable):
    program = re.search(r"\bpython3 -c '([^']*)'", step(job, name)["run"])[1]
    for node in ast.parse(program).body:
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == variable for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} defines no {variable}.")


def _accept(runner, candidate, *, selection=None, ref="refs/heads/main", run_attempt="1"):
    raw = json.dumps(selection or _fleet_candidate(candidate), sort_keys=True, separators=(",", ":"))
    result, _, calls = runner("accept", "Start acceptance for this exact candidate", extra={
        "CANDIDATE": raw, "GITHUB_REF": ref, "GITHUB_RUN_ATTEMPT": run_attempt,
    })
    return result, calls, raw


def _serve_dispatch(candidate, *, head=SHA, status=200, body=None):
    candidate["responses"][MAIN_REF] = {
        "status": 200, "method": "GET",
        "body": {"ref": "refs/heads/main", "object": {"sha": head, "type": "commit"}},
    }
    candidate["responses"][DISPATCH] = {
        "status": status, "method": "POST",
        "flags": ["--include", "--input"],
        "body": body if body is not None else {
            "workflow_run_id": ACCEPTANCE_RUN, "run_url": f"https://api.github.com/repos/{REPO}/actions/runs/7000",
            "html_url": f"https://github.com/{REPO}/actions/runs/{ACCEPTANCE_RUN}",
        },
    }


def test_accept_holds_only_dispatch_authority_without_source_or_azure():
    accept = JOBS["accept"]
    assert accept["needs"] == "candidate"
    assert accept["permissions"] == {"actions": "write", "contents": "read"}
    assert accept["runs-on"] == "ubuntu-24.04"
    assert "environment" not in accept and "uses" not in accept
    assert [item.get("uses") for item in accept["steps"]] == [None]
    started = step("accept", "Start acceptance for this exact candidate")
    assert started["env"] == {
        "GH_TOKEN": "${{ github.token }}", "CANDIDATE": "${{ needs.candidate.outputs.fleet-candidate }}",
    }
    assert "${{" not in started["run"]
    # Only this job can start workflows, and the release workflow never reads secrets.
    writers = [name for name, job in WORKFLOW["jobs"].items() if job.get("permissions", {}).get("actions") == "write"]
    assert writers == ["accept"]
    release_text = (ROOT / ".github" / "workflows" / "release.yaml").read_text(encoding="utf-8")
    assert "secrets." not in release_text
    # Normal CI never dispatches acceptance or acquires Azure authority.
    ci_text = (ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    for marker in ("e2e-test.yaml", "dispatches", "azure/login", "AZURE_CLIENT_ID", "secrets."):
        assert marker not in ci_text, marker
    assert not any(job.get("permissions", {}).get("actions") == "write" for job in CI_WORKFLOW["jobs"].values())


@pytest.mark.parametrize(("result", "active", "candidate", "cancelled", "expected"), [
    pytest.param("success", "true", "exact-candidate", False, True, id="active-candidate"),
    pytest.param("success", "false", "exact-candidate", False, False, id="inactive-candidate"),
    pytest.param("success", "", "exact-candidate", False, False, id="missing-active-output"),
    pytest.param("success", "true", "", False, False, id="no-fleet-candidate"),
    pytest.param("success", "true", None, False, False, id="missing-candidate-output"),
    pytest.param("failure", "true", "exact-candidate", False, False, id="candidate-failed"),
    pytest.param("skipped", "true", "exact-candidate", False, False, id="candidate-skipped"),
    pytest.param("cancelled", "true", "exact-candidate", False, False, id="candidate-cancelled"),
    pytest.param("success", "true", "exact-candidate", True, False, id="run-cancelled"),
])
def test_accept_dispatches_only_an_active_successful_fleet_candidate(
    result, active, candidate, cancelled, expected,
):
    outputs = {"active": active}
    if candidate is not None:
        outputs["fleet-candidate"] = candidate
    assert job_runs(
        WORKFLOW, "accept", inputs={}, needs={"candidate": result},
        outputs={"candidate": outputs}, cancelled=cancelled,
    ) is expected


@pytest.mark.parametrize(("needs", "active", "cancelled", "expected"), [
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "success"},
                 "true", False, True, id="approved-candidate"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "success"},
                 "false", False, False, id="inactive-candidate"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "success"},
                 "", False, False, id="missing-active-output"),
    pytest.param({"release-runner": "failure", "candidate": "success", "accept": "success"},
                 "true", False, False, id="runner-failed"),
    pytest.param({"release-runner": "skipped", "candidate": "success", "accept": "success"},
                 "true", False, False, id="runner-skipped"),
    pytest.param({"release-runner": "cancelled", "candidate": "success", "accept": "success"},
                 "true", False, False, id="runner-cancelled"),
    pytest.param({"release-runner": "success", "candidate": "failure", "accept": "success"},
                 "true", False, False, id="candidate-failed"),
    pytest.param({"release-runner": "success", "candidate": "skipped", "accept": "success"},
                 "true", False, False, id="candidate-skipped"),
    pytest.param({"release-runner": "success", "candidate": "cancelled", "accept": "success"},
                 "true", False, False, id="candidate-cancelled"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "failure"},
                 "true", False, False, id="acceptance-failed"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "skipped"},
                 "true", False, False, id="acceptance-skipped"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "cancelled"},
                 "true", False, False, id="acceptance-cancelled"),
    pytest.param({"release-runner": "success", "candidate": "success", "accept": "success"},
                 "true", True, False, id="run-cancelled"),
])
def test_publish_requires_successful_candidate_acceptance_and_runner(needs, active, cancelled, expected):
    assert job_runs(
        WORKFLOW, "publish", inputs={}, needs=needs,
        outputs={"candidate": {"active": active}}, cancelled=cancelled,
    ) is expected


@pytest.mark.parametrize("run_attempt", ["1", "2"])
def test_accept_starts_acceptance_for_the_exact_candidate(candidate, runner, run_attempt):
    _serve_dispatch(candidate)
    result, calls, raw = _accept(runner, candidate, run_attempt=run_attempt)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[:2] == [
        ["api", MAIN_REF],
        ["api", "--method", "POST", "--include", DISPATCH, "--input", "-"],
    ]
    assert calls[2][0] == "<input>" and len(calls) == 3
    assert json.loads(calls[2][1]) == {"ref": "main", "return_run_details": True, "inputs": {
        "scenario": "release-acceptance", "candidate": raw, "environment": "dev", "location": "eastus2",
    }}
    summary = (candidate["root"].parent / "summary.md").read_text(encoding="utf-8")
    assert f"(https://github.com/{REPO}/actions/runs/{ACCEPTANCE_RUN})" in summary


@pytest.mark.parametrize("response", [
    {"status": 204, "body": {}},
    {"body": {"html_url": f"https://github.com/{REPO}/actions/runs/{ACCEPTANCE_RUN}"}},
    {"body": {"workflow_run_id": ACCEPTANCE_RUN, "html_url": "https://example.com/runs/7000"}},
], ids=["no-content", "missing-run", "other-run-link"])
def test_accepted_dispatch_without_run_details_never_prompts_a_second_run(candidate, runner, response):
    _serve_dispatch(candidate, **response)
    result, calls, _ = _accept(runner, candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::GitHub started acceptance without identifying the run" in result.stdout
    assert sum(DISPATCH in call for call in calls) == 1
    summary = (candidate["root"].parent / "summary.md").read_text(encoding="utf-8")
    assert "Find the newest Release acceptance run under E2E Tests" in summary
    assert "https://example.com" not in summary


@pytest.mark.parametrize("fault", [
    "moved-main", "rejected",
    "preview", "other-producer", "later-attempt", "other-commit", "other-ref", "unreadable",
])
def test_accept_fails_closed(candidate, runner, fault):
    selection = _fleet_candidate(candidate)
    response = {}
    ref = "refs/heads/main"
    if fault == "moved-main":
        response["head"] = "d" * 40
    elif fault == "rejected":
        response["status"] = 422
    elif fault == "preview":
        selection["producer"].update(preview=True, caller=".github/workflows/ci.yaml")
    elif fault == "other-producer":
        selection["producer"]["run"] = 41
    elif fault == "later-attempt":
        selection["producer"]["attempt"] = 2
    elif fault == "other-commit":
        selection["source"]["commit"] = "d" * 40
    elif fault == "other-ref":
        ref = "refs/heads/feature"
    _serve_dispatch(candidate, **response)
    if fault == "unreadable":
        result, _, calls = runner("accept", "Start acceptance for this exact candidate", extra={
            "CANDIDATE": "[]", "GITHUB_REF": ref,
        })
    else:
        result, calls, _ = _accept(runner, candidate, selection=selection, ref=ref)
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr
    dispatched = any(DISPATCH in call for call in calls)
    assert dispatched is (fault == "rejected")
    if fault not in {"moved-main", "rejected"}:
        assert calls == []


def test_publish_binds_acceptance_before_any_other_input_or_write():
    publish = JOBS["publish"]
    names = [item["name"] for item in publish["steps"]]
    for earlier, later in (
        ("Download the pinned declaration", "Locate the governing acceptance run"),
        ("Locate the governing acceptance run", "Download the acceptance receipt"),
        ("Download the acceptance receipt", "Verify the bound acceptance"),
        ("Verify the bound acceptance", "Download the reviewed release notes"),
        ("Verify the bound acceptance", "Create only the approved missing tag"),
    ):
        assert names.index(earlier) < names.index(later)
    located = step("publish", "Locate the governing acceptance run")
    assert located["env"] == {
        "CANDIDATE": "${{ needs.candidate.outputs.fleet-candidate }}",
        "ADMISSION_ARTIFACT_ID": "${{ needs.candidate.outputs.admission-artifact-id }}",
    }
    assert step("publish", "Download the acceptance receipt")["with"] == {
        "artifact-ids": "${{ steps.acceptance.outputs.artifact-id }}", "repository": "${{ github.repository }}",
        "run-id": "${{ steps.acceptance.outputs.run-id }}", "github-token": "${{ github.token }}",
        "path": "${{ runner.temp }}/release-acceptance", "merge-multiple": True, "digest-mismatch": "error",
    }
    assert "${{" not in located["run"] and "${{" not in step("publish", "Verify the bound acceptance")["run"]


@pytest.mark.parametrize("case", ["single", "rerun-attempt", "newer-other-admission", "newer-bound-pass", "publish-rerun"])
def test_publish_locates_the_newest_bound_acceptance(candidate, runner, case):
    runs = [_acceptance_run()]
    artifacts = {ACCEPTANCE_RUN: [_receipt_artifact(), {"id": 1, "name": "site-outcome-7000-1-disabled"}]}
    selection = _fleet_candidate(candidate)
    run_attempt = "1"
    expected = (ACCEPTANCE_RUN, ACCEPTANCE_RUN * 10 + 1)
    if case == "rerun-attempt":
        runs = [_acceptance_run(attempt=2)]
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(attempt=1), _receipt_artifact(attempt=2)]
        expected = (ACCEPTANCE_RUN, ACCEPTANCE_RUN * 10 + 2)
    elif case == "newer-other-admission":
        # A newer run for another candidate at this commit does not govern this one.
        runs.insert(0, _acceptance_run(run_id=7100, conclusion="failure"))
        artifacts[7100] = [_receipt_artifact(run_id=7100, admission="b" * 64)]
    elif case == "newer-bound-pass":
        runs.insert(0, _acceptance_run(run_id=7100, attempt=2))
        artifacts[7100] = [_receipt_artifact(run_id=7100, attempt=1), _receipt_artifact(run_id=7100, attempt=2)]
        expected = (7100, 71002)
    elif case == "publish-rerun":
        run_attempt = "2"
    _serve_acceptance(candidate, runs, artifacts)
    result, outputs, calls = _locate(runner, candidate, selection=selection, run_attempt=run_attempt)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (int(outputs["run-id"]), int(outputs["artifact-id"])) == expected
    binding = json.loads((candidate["root"] / "acceptance-binding.json").read_text(encoding="utf-8"))
    governing = next(run for run in runs if run["id"] == expected[0])
    assert binding["acceptance"] == {"run": governing["id"], "attempt": governing["run_attempt"]}
    assert binding["candidate"]["producerAttempt"] == 1
    assert all(call[:3] == ["api", "--paginate", "--slurp"] for call in calls)


@pytest.mark.parametrize("fault", [
    "no-runs", "running", "newer-running", "superseded-attempt", "latest-receipt-missing",
    "newer-bound-failure", "duplicate", "expired", "missing-digest", "other-origin",
    "other-path", "other-event", "other-branch", "other-commit", "other-repository", "other-title",
    "other-candidate", "preview", "other-producer", "later-producer-attempt", "other-admission-id",
    "other-plan", "listing-failure",
    "newer-no-result", "newer-unrelated-artifact", "newer-malformed-receipt", "newer-wrong-run-receipt",
])
def test_publish_rejects_unbound_or_unsettled_acceptance(candidate, runner, fault):
    runs = [_acceptance_run()]
    artifacts = {ACCEPTANCE_RUN: [_receipt_artifact()]}
    selection = _fleet_candidate(candidate)
    if fault == "no-runs":
        runs, artifacts = [], {}
    elif fault == "running":
        runs[0].update(status="in_progress", conclusion=None)
    elif fault == "newer-running":
        runs.insert(0, _acceptance_run(run_id=7100, status="queued", conclusion=None))
    elif fault == "superseded-attempt":
        # The older passing attempt cannot stand in for the failing latest attempt.
        runs = [_acceptance_run(attempt=2, conclusion="failure")]
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(attempt=1), _receipt_artifact(attempt=2)]
    elif fault == "latest-receipt-missing":
        runs = [_acceptance_run(attempt=2)]
    elif fault == "newer-bound-failure":
        runs.insert(0, _acceptance_run(run_id=7100, conclusion="failure"))
        artifacts[7100] = [_receipt_artifact(run_id=7100)]
    elif fault in {"newer-no-result", "newer-unrelated-artifact", "newer-malformed-receipt", "newer-wrong-run-receipt"}:
        runs.insert(0, _acceptance_run(run_id=7100))
        artifacts[7100] = {
            "newer-no-result": [],
            "newer-unrelated-artifact": [{"name": "site-outcome-7100-1-disabled"}],
            "newer-malformed-receipt": [{"name": "release-acceptance-7100-1-not-an-admission"}],
            "newer-wrong-run-receipt": [_receipt_artifact(run_id=7200, admission="b" * 64)],
        }[fault]
    elif fault == "duplicate":
        artifacts[ACCEPTANCE_RUN].append(_receipt_artifact(id=99))
    elif fault == "expired":
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(expired=True)]
    elif fault == "missing-digest":
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(digest=None)]
    elif fault == "other-origin":
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(workflow_run={"id": 1, "head_sha": SHA})]
    elif fault.startswith("other-") and fault[6:] in {"path", "event", "branch", "commit", "repository", "title"}:
        field, value = {
            "path": ("path", ".github/workflows/e2e-copy.yaml"), "event": ("event", "push"),
            "branch": ("head_branch", "feature"), "commit": ("head_sha", "d" * 40),
            "repository": ("repository", {"full_name": "fork/publisher"}), "title": ("display_title", "E2E Tests"),
        }[fault[6:]]
        runs[0][field] = value
    elif fault == "other-candidate":
        artifacts[ACCEPTANCE_RUN] = [_receipt_artifact(admission="b" * 64)]
    elif fault == "preview":
        selection["producer"].update(preview=True, caller=".github/workflows/ci.yaml")
    elif fault == "other-producer":
        selection["producer"]["run"] = 41
    elif fault == "later-producer-attempt":
        selection["producer"]["attempt"] = 2
    elif fault == "other-admission-id":
        selection["artifacts"]["admission"]["id"] = 600
    elif fault == "other-plan":
        selection["artifacts"]["plan"]["sha256"] = "b" * 64
    _serve_acceptance(candidate, runs, artifacts)
    if fault == "listing-failure":
        candidate["responses"][RUNS]["status"] = 500
    result, outputs, calls = _locate(runner, candidate, selection=selection)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "run-id" not in outputs and "artifact-id" not in outputs
    assert not (candidate["root"] / "acceptance-binding.json").exists()
    message = {
        "running": "still running", "newer-running": "still running", "superseded-attempt": "did not pass",
        "newer-bound-failure": "did not pass", "no-runs": "No acceptance evidence",
        "other-candidate": "No acceptance evidence", "latest-receipt-missing": "no single valid receipt",
        "listing-failure": "could not list",
        **dict.fromkeys(("newer-no-result", "newer-unrelated-artifact", "newer-malformed-receipt",
                         "newer-wrong-run-receipt"), (
            "::error::The newest acceptance run for this commit recorded no result. "
            "Re-run it, or start acceptance for this candidate, then re-run publish."
        )),
    }.get(fault)
    if message:
        assert message in output
    if fault in {"preview", "other-producer", "later-producer-attempt", "other-admission-id", "other-plan"}:
        assert "not this approved main candidate" in output and calls == []
    assert not any("--method" in call for call in calls)


@pytest.mark.parametrize(("run_attempt", "producer_attempt", "acceptance_attempt", "bundle"), [
    ("1", 1, 1, True), ("2", 1, 1, True), ("2", 2, 1, True), ("1", 1, 3, True), ("1", 1, 1, False),
])
def test_publish_accepts_a_receipt_bound_to_this_candidate(
    candidate, runner, run_attempt, producer_attempt, acceptance_attempt, bundle,
):
    candidate["plan"]["siteops"]["bundle"] = bundle
    receipt = _release_receipt(candidate, attempt=acceptance_attempt, producer_attempt=producer_attempt, bundle=bundle)
    result, calls = _verify(runner, candidate, receipt, run_attempt=run_attempt, producer_attempt=producer_attempt,
                            acceptance_attempt=acceptance_attempt)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = (candidate["root"].parent / "summary.md").read_text(encoding="utf-8")
    assert f"/actions/runs/{ACCEPTANCE_RUN}/attempts/{acceptance_attempt})" in summary
    assert "| fleet | passed | ephemeral | confirmed-absent |" in summary
    assert "| site-aio | passed | persistent | confirmed-absent |" in summary
    assert calls == []


@pytest.mark.parametrize("fault", [
    "other-repository", "other-commit", "other-producer-run", "producer-run-text", "other-producer-attempt",
    "preview", "other-admission", "other-plan", "other-inventory", "other-acceptance-run",
    "older-acceptance-attempt", "environment", "transport", "status", "failed-row", "residual-cleanup",
    "missing-assertion", "reordered-rows", "extra-key", "row-extra-key", "unknown-groups", "extra-file",
    "installer-not-applicable", "installer-cleanup", "installer-groups", "site-groups-unknown",
    "fleet-groups-not-applicable", "fleet-purged", "installer-purged", "site-purge-not-attempted",
])
def test_publish_rejects_a_receipt_that_does_not_bind(candidate, runner, fault):
    receipt = _release_receipt(candidate)
    rows = {row["scenario"]: row for row in receipt["scenarios"]}
    changes = {
        "other-repository": ("candidate", "repository", "fork/publisher"),
        "other-commit": ("candidate", "sourceCommit", "d" * 40),
        "other-producer-run": ("candidate", "producerRun", 41),
        "producer-run-text": ("candidate", "producerRun", "42"),
        "other-producer-attempt": ("candidate", "producerAttempt", 2),
        "preview": ("candidate", "preview", True),
        "other-admission": ("candidate", "admissionSha256", "b" * 64),
        "other-plan": ("candidate", "planSha256", "b" * 64),
        "other-inventory": ("candidate", "inventorySha256", "b" * 64),
        "other-acceptance-run": ("acceptance", "run", 7100),
        "older-acceptance-attempt": ("acceptance", "attempt", 0),
    }
    if fault in changes:
        section, key, value = changes[fault]
        receipt[section][key] = value
    elif fault in {"environment", "transport", "status"}:
        receipt[fault] = {"environment": "prod", "transport": "public", "status": "failed"}[fault]
    elif fault == "failed-row":
        rows["site-existing-secretsync"]["status"] = "failed"
    elif fault == "residual-cleanup":
        rows["fleet"]["cleanup"] = "residual"
    elif fault == "missing-assertion":
        rows["site-combined"]["assertions"].remove("vault-created-by-enablement")
    elif fault == "reordered-rows":
        receipt["scenarios"].reverse()
    elif fault == "extra-key":
        receipt["note"] = "PRIVATE_NOTE"
    elif fault == "row-extra-key":
        rows["site-aio"]["resourceGroup"] = "PRIVATE_GROUP"
    elif fault == "unknown-groups":
        rows["site-aio"]["groups"] = "shared"
    elif fault == "installer-not-applicable":
        rows["installer"].update(status="not-applicable", assertions=[])
    elif fault == "installer-cleanup":
        rows["installer"]["cleanup"] = "confirmed-absent"
    elif fault in {"installer-groups", "site-groups-unknown", "fleet-groups-not-applicable"}:
        scenario, value = {"installer-groups": ("installer", "persistent"),
                           "site-groups-unknown": ("site-aio", "unknown"),
                           "fleet-groups-not-applicable": ("fleet", "not-applicable")}[fault]
        rows[scenario]["groups"] = value
    elif fault in {"fleet-purged", "installer-purged", "site-purge-not-attempted"}:
        scenario, value = {"fleet-purged": ("fleet", "purged"), "installer-purged": ("installer", "failed"),
                           "site-purge-not-attempted": ("site-aio", "not-attempted")}[fault]
        rows[scenario]["vaultPurge"] = value
    result, calls = _verify(runner, candidate, receipt, extra_file=fault == "extra-file")
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "::error::" in output
    assert "PRIVATE_" not in output
    summary = candidate["root"].parent / "summary.md"
    assert not summary.exists() or "Bound candidate acceptance" not in summary.read_text(encoding="utf-8")
    assert calls == []


@pytest.mark.parametrize(("scenario", "key", "value"), [
    ("site-aio", "vaultPurge", "purged"), ("site-existing-secretsync", "vaultPurge", "failed"),
    ("site-combined", "vaultPurge", "not-applicable"), ("site-aio", "groups", "ephemeral"),
    ("fleet", "groups", "persistent"),
])
def test_publish_accepts_every_row_value_the_aggregator_writes(candidate, runner, scenario, key, value):
    receipt = _release_receipt(candidate)
    next(row for row in receipt["scenarios"] if row["scenario"] == scenario)[key] = value
    result, _ = _verify(runner, candidate, receipt)
    assert result.returncode == 0, result.stdout + result.stderr


def test_publish_accepts_the_receipt_the_acceptance_aggregator_writes(candidate, runner):
    spec = importlib.util.spec_from_file_location("release_acceptance_contract", ROOT / "scripts" / "release_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fleet_workflow import FleetCandidate

    selection = FleetCandidate.parse(
        json.dumps(_fleet_candidate(candidate)).encode(), repository=REPO, commit=SHA, ref="refs/heads/main",
    )
    rows = [module.row("installer", "passed", module.ASSERTIONS["installer"])] + [
        module.row(name, "passed", module.ASSERTIONS[name], "confirmed-absent", groups="persistent")
        for name in module.SCENARIOS[1:]
    ]
    receipt = module.receipt(selection, run=ACCEPTANCE_RUN, attempt=1, environment="dev", rows=rows, complete=True)
    result, _ = _verify(runner, candidate, receipt)
    assert result.returncode == 0, result.stdout + result.stderr
    inline = _inline_literal("publish", "Verify the bound acceptance", "assertions")
    assert list(inline) == list(module.SCENARIOS)
    assert {name: tuple(values) for name, values in inline.items()} == module.ASSERTIONS

"""Exercise the declaration-driven publisher through its actual workflow steps."""

import copy
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
import yaml

from tests.release_helpers import CLI, _commit, _write_record, _write_source_version
from tests.release_helpers import repository as repository
from tests.shell_helpers import bash_path, write_executable
from tests.shell_helpers import run_script as _run_script
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parent.parent
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


def summary_values(**overrides):
    return {
        "DRY_RUN": "false", "ENGINE_VERSION": "1.0.0b1+build.42",
        "CI_URL": f"https://github.com/{REPO}/actions/runs/42",
        "BUNDLE_SHA": "a" * 64, "WHEEL_NAME": WHEEL, "WHEEL_SHA": "b" * 64,
        "ASSET_LIST_SHA": "d" * 64, "ARCHIVE_NAME": ARCHIVE, "TAG_EXISTS": "false",
        "MATRIX": json.dumps([
            {"python": version, "linux": "passed", "windows": "passed"}
            for version in ("3.10", "3.11", "3.12", "3.13", "3.14")
        ]),
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
    selected_root = root / "release-engine-selection"
    selected_root.mkdir()
    selected_raw = json.dumps(selected).encode()
    (selected_root / "workspace-engine.json").write_bytes(selected_raw)
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
            "APPROVED_ENGINE_ID": "71", "APPROVED_ENGINE_REF": "d" * 40}


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
if args[:2] == ["attestation", "verify"]:
    expected = os.environ.get("EXPECTED_SIGNER_IDENTITY")
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
        for subject in (ARCHIVE, wheel_path.name, *scripts, *packages):
            signer = ".github/workflows/" + ("_workspace-distribution.yaml" if subject in packages else "_siteops-distribution.yaml")
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
    assert JOBS["publish"]["needs"] == ["release-runner", "candidate"]
    assert JOBS["publish"]["if"] == "needs.candidate.result == 'success' && needs.candidate.outputs.active == 'true'"
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
            "tag_name": "v1.0.0b8", "draft": False, "prerelease": True, "immutable": False,
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
    assert summary.count("| Python | Linux | Windows |") == 1
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
        f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md#install-the-verified-bundle"
        in notes
    )
    block = _installation_block(notes)
    assert block.startswith(f'pipx install "{base}{WHEEL}"')
    assert "--backend pip" in block
    assert "--fetch-python never" in block
    assert "--skip-maintenance" in block
    assert "--app siteops" in block
    assert "--only-binary=:all:" in block
    assert "--no-cache-dir" in block
    assert "--isolated" not in block and "--index-url" not in block
    assert "--force" not in block
    assert "Runtime dependencies come from your configured package index as wheels" in notes
    assert "`--index-url <your approved index>`" in notes
    assert "packagefeedproxy.microsoft.io" not in notes
    assert "pipx does not automatically verify GitHub attestations" in notes
    assert (
        f"https://github.com/{REPO}/blob/{SHA}/docs/install-siteops.md#verify-the-bootstrap-script"
        in notes
    )
    assert "with those identities before running it" in notes
    assert "HTTPS download alone does not authenticate the publisher" in notes
    assert "use this release's reviewed provenance values instead" in notes
    assert "configured-Site fleet selectors remain supported" in notes
    assert "a project pin selects content, not operator Site configuration" in notes
    assert "`--force`" in notes
    assert "downloads only the ZIP and its detached proof, authenticates the ZIP before extraction" in notes
    assert f"Expected publisher: `{REPO}`" in notes
    assert f"Source commit: `{SHA}`" in notes
    assert f'Source ref: `{candidate["plan"]["source"]["ref"]}`' in notes
    assert "Expected provenance runner class: `self-hosted`" in notes
    assert "runner class does not identify a particular pool" in notes
    assert "switching between online and locked installations" in notes
    assert "authenticated `pylock.toml` with stock pipx" in notes
    assert "shared pip 26.2.1" in notes and "experimental" in notes
    assert "install.py" not in notes and "siteops_distribution.py" not in notes
    assert "Invoke-WebRequest" not in notes and "urllib.request" not in notes
    assert "pipx 1.17.2" in notes
    assert "CPython 3.10-3.14" in notes and "glibc 2.17" in notes
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    for argument in (
        "--backend pip", "--fetch-python never", "--skip-maintenance",
        "--app siteops", '--pip-args "--only-binary=:all: --no-cache-dir"',
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
    assert "pipx install" not in notes and "releases/download/" not in notes


@pytest.mark.parametrize("pipx_exit", [0, 7])
def test_generated_online_install_command_runs_from_an_unrelated_directory(
    candidate, runner, tmp_path, pipx_exit,
):
    command = _installation_block(_render_install_notes(candidate, runner))
    arguments = tmp_path / "pipx-args.log"
    script = """
pipx() {
    printf '%s\n' "$@" > "$PIPX_ARGS"
    return "$PIPX_EXIT"
}
""" + command
    result = _run_script(
        script,
        tmp_path,
        {"PIPX_ARGS": bash_path(arguments), "PIPX_EXIT": str(pipx_exit)},
    )
    assert result.returncode == pipx_exit
    assert arguments.read_text().splitlines() == [
        "install",
        downloads_url("v1.0.0b8").removesuffix(ARCHIVE) + WHEEL,
        "--backend", "pip",
        "--fetch-python", "never",
        "--skip-maintenance",
        "--app", "siteops",
        "--pip-args",
        "--only-binary=:all: --no-cache-dir",
    ]


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


@pytest.mark.parametrize("matrix", ["[]", "{}", '[null]', '[{"python":"3.10"}]'])
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


@pytest.mark.parametrize("immutable", [False, True])
def test_published_asset_digests_and_immutable_release_are_checked(candidate, runner, immutable):
    tag = candidate["plan"]["release"]["tag"]
    assets = [
        {"name": name, "size": (candidate["root"] / "release-bundle" / name).stat().st_size,
         "digest": "sha256:" + digest((candidate["root"] / "release-bundle" / name).read_bytes()), "state": "uploaded"}
        for name in ENGINE_ASSETS
    ]
    endpoint = f"repos/{REPO}/releases/tags/{tag}"
    candidate["responses"][endpoint] = {
        "status": 200,
        "body": {"tag_name": tag, "draft": False, "prerelease": True, "immutable": immutable, "assets": assets},
    }
    result, _, calls = runner(
        "publish", "Confirm published assets and immutability",
        extra={"PRERELEASE": "true", "BUNDLE": "true"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(call[:2] == ["release", "verify"] for call in calls) is immutable
    assert len([call for call in calls if call[:2] == ["release", "verify-asset"]]) == (8 if immutable else 0)
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
    assert len(inventory["assets"]) == (11 if built else 3)
    result, _, _ = runner("review", "Render the final release notes", extra={"ENGINE_VERSION": "1.0.0b1+build.42"})
    assert result.returncode == 0, result.stdout + result.stderr
    notes = (root / "publish-notes.md").read_text()
    assert "## Workspace content" in notes and "siteops-workspaces.json" in notes
    assert f"https://github.com/{REPO}/blob/{SHA}/docs/projects.md#run-project-pin" in notes
    assert "`siteops project pin` with `--release`" in notes
    (root / "release-bundle").rename(root / "review-native")
    candidate["native_directory"] = "review-native"
    shutil.copytree(root / "release-payload", root / "release-bundle")
    (root / "release-assets/release-assets.json").write_bytes((root / "final-release-assets/release-assets.json").read_bytes())
    for name in ("Verify the approved candidate", "Verify the approved release assets"):
        result, _, _ = runner("publish", name, extra=extra)
        assert result.returncode == 0, result.stdout + result.stderr
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

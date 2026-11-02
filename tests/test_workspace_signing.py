"""Exercise the actual source-free signing gate and immutable staging steps."""

import hashlib
import json
import re
import shutil
import sys

import pytest

from siteops.github_attestation import load_github_policy
from tests.release_helpers import (
    REPOSITORY,
    ROOT,
    SOURCE_REF,
    _commit,
    _write_record,
    create_repository,
)
from tests.shell_helpers import run_script, write_executable
from tests.workspace_release_helpers import _declaration, _load, _produce, _workspace
from tests.yaml_documents import cached_safe_load

WORKFLOW = cached_safe_load((ROOT / ".github/workflows/_workspace-distribution.yaml").read_text())
CANDIDATE = cached_safe_load((ROOT / ".github/workflows/_release-candidate.yaml").read_text())


def step(name):
    return next(row for row in WORKFLOW["jobs"]["attest"]["steps"] if row.get("name") == name)


@pytest.fixture(scope="module")
def signing_source(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("signing-source")
    repository = create_repository(tmp_path / "repository")
    request = _workspace(repository)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "signing candidate")
    root = tmp_path / "runner"
    (root / "workspace-plan").mkdir(parents=True)
    from siteops_release import serialize_release_plan
    raw = serialize_release_plan(_load(repository, sha).to_dict())
    (root / "workspace-plan/plan.json").write_bytes(raw)
    subject = root / "workspace-subject"
    result = _produce(repository, sha, subject, "--release-workspace", "workspace")
    assert result.returncode == 0, result.stdout + result.stderr
    package_proof, record_proof = root / "package-proof.json", root / "record-proof.json"
    package_proof.write_bytes(b"opaque package proof")
    record_proof.write_bytes(b"opaque record proof")
    env = {
        "RUNNER_TEMP": root.as_posix(), "SOURCE_SHA": sha, "EXPECTED_SOURCE_SHA": sha,
        "SOURCE_REF": SOURCE_REF, "GITHUB_REPOSITORY": REPOSITORY, "DRY_RUN": "false",
        "WORKSPACE_SELECTION": "workspace", "WORKSPACE_SLOT": "1",
        "EXPECTED_PLAN_SHA": hashlib.sha256(raw).hexdigest(),
        "EXPECTED_RECORD_SHA": hashlib.sha256((subject / "workspace-builds.json").read_bytes()).hexdigest(),
        "EXPECTED_PACKAGE_SHA": hashlib.sha256((subject / "workspace.zip").read_bytes()).hexdigest(),
        "PACKAGE_NAME": "workspace.zip", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3",
        "PACKAGE_PROOF": package_proof.as_posix(), "RECORD_PROOF": record_proof.as_posix(),
    }
    return root, env


@pytest.fixture
def signing(signing_source, tmp_path):
    source, source_env = signing_source
    root = tmp_path / "runner"
    shutil.copytree(source, root)
    env = {
        key: value.replace(source.as_posix(), root.as_posix()) for key, value in source_env.items()
    }
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "python3", '#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n')
    env["TEST_PYTHON"] = sys.executable.replace("\\", "/")
    return root, env, tmp_path


def invoke(signing, name):
    _, env, cwd = signing
    return run_script(step(name)["run"], cwd, env)


def test_signer_only_downloads_attests_and_uploads_exact_subjects():
    job = WORKFLOW["jobs"]["attest"]
    assert job["needs"] == "build"
    assert job["permissions"] == {
        "contents": "read", "actions": "read", "id-token": "write", "attestations": "write",
    }
    assert WORKFLOW["jobs"]["build"]["permissions"] == {"contents": "read", "actions": "read"}
    assert {row["uses"].split("@")[0] for row in job["steps"] if "uses" in row} == {
        "actions/download-artifact", "actions/attest", "actions/upload-artifact",
    }
    assert all("scripts/" not in row.get("run", "") and "git " not in row.get("run", "") for row in job["steps"])
    attestations = [row for row in job["steps"] if row.get("uses", "").startswith("actions/attest@")]
    assert len(attestations) == 2
    assert all("*" not in row["with"]["subject-path"] for row in attestations)
    assert step("Download this workspace build")["with"]["artifact-ids"] == "${{ needs.build.outputs.artifact-id }}"
    assert step("Upload the attested workspace")["with"]["overwrite"] is False
    caller = CANDIDATE["jobs"]["workspace-build"]
    assert caller["strategy"]["max-parallel"] == 4
    assert "workspace-matrix" in caller["strategy"]["matrix"]
    collector = CANDIDATE["jobs"]["workspace-assets"]
    assert collector["permissions"] == {"contents": "read", "actions": "read"}
    download = next(row for row in collector["steps"] if row["name"] == "Download attested workspace subjects")
    assert download["with"]["merge-multiple"] is False
    assert download["with"]["digest-mismatch"] == "error"
    assert "github.run_attempt" in download["with"]["pattern"]


def test_signer_and_stager_preserve_both_single_subject_pairs(signing):
    result = invoke(signing, "Admit the exact workspace subjects")
    assert result.returncode == 0, result.stdout + result.stderr
    result = invoke(signing, "Retain the exact attested bytes")
    assert result.returncode == 0, result.stdout + result.stderr
    root, env, _ = signing
    output = root / "workspace-attested"
    assert {path.name for path in output.iterdir()} == {
        "workspace.zip", "workspace.zip.attestation.jsonl",
        "workspace-builds.json", "workspace-builds.json.attestation.jsonl",
    }
    assert hashlib.sha256((output / "workspace.zip").read_bytes()).hexdigest() == env["EXPECTED_PACKAGE_SHA"]
    assert hashlib.sha256((output / "workspace-builds.json").read_bytes()).hexdigest() == env["EXPECTED_RECORD_SHA"]


@pytest.mark.parametrize("fault", ["plan", "plan-bytes", "record", "package", "extra", "slot", "selection", "name", "source", "engine"])
def test_signer_rejects_changed_or_misdirected_build_outputs(signing, fault):
    root, env, _ = signing
    if fault in {"plan", "record", "package"}:
        env[{"plan": "EXPECTED_PLAN_SHA", "record": "EXPECTED_RECORD_SHA", "package": "EXPECTED_PACKAGE_SHA"}[fault]] = "d" * 64
    elif fault == "plan-bytes":
        path = root / "workspace-plan/plan.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif fault == "extra":
        (root / "workspace-subject/.extra").write_bytes(b"unlisted")
    elif fault == "slot":
        env["WORKSPACE_SLOT"] = "2"
    elif fault == "selection":
        env["WORKSPACE_SELECTION"] = "other"
    elif fault == "name":
        env["PACKAGE_NAME"] = "../workspace.zip"
    elif fault == "source":
        env["EXPECTED_SOURCE_SHA"] = "d" * 40
    else:
        path = root / "workspace-subject/workspace-builds.json"
        record = json.loads(path.read_bytes())
        record["engineVersion"] = "9.0.0"
        path.write_text(json.dumps(record))
        env["EXPECTED_RECORD_SHA"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = invoke(signing, "Admit the exact workspace subjects")
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr
    assert not (root / "workspace-attested").exists()


@pytest.mark.parametrize("fault", ["package", "record", "empty-proof", "large-proof"])
def test_stager_rechecks_subjects_and_bounds_proofs(signing, fault):
    root, _, _ = signing
    if fault == "package":
        (root / "workspace-subject/workspace.zip").write_bytes(b"changed")
    elif fault == "record":
        (root / "workspace-subject/workspace-builds.json").write_bytes(b"changed")
    elif fault == "empty-proof":
        (root / "package-proof.json").write_bytes(b"")
    else:
        (root / "package-proof.json").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    result = invoke(signing, "Retain the exact attested bytes")
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr


def test_workspace_workflow_embedded_programs_compile():
    count = 0
    for job in WORKFLOW["jobs"].values():
        for row in job.get("steps", []):
            for match in re.finditer(r"(?:python3|\"\$WORKSPACE_PYTHON\")\s+-c\s+'([^']*)'", row.get("run", "")):
                compile(match[1], "<workspace-workflow>", "exec")
                count += 1
    assert count == 3


@pytest.mark.parametrize("caller", ["ci.yaml", "release.yaml", "other.yaml"])
def test_collection_policy_comes_from_workflow_context_and_independent_roots(tmp_path, caller):
    root = tmp_path / "runner"
    tools = root / "workspace-verifier" / "bin"
    tools.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "python", '#!/usr/bin/env bash\n[[ "$1 $2" == "-m venv" ]] || exit 99\n')
    write_executable(bin_dir / "python3", '#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n')
    write_executable(tools / "python", '#!/usr/bin/env bash\n[[ "$1 $2 $3" == "-m pip install" ]] || exit 99\n')
    write_executable(bin_dir / "gh", """#!/usr/bin/env bash
[[ "$*" == "attestation trusted-root" ]] || exit 99
printf '{"fixture":"independent root"}'
""")
    setup = next(
        row for row in CANDIDATE["jobs"]["workspace-assets"]["steps"]
        if row.get("name") == "Prepare independent verification inputs"
    )
    result = run_script(setup["run"], tmp_path, {
        "RUNNER_TEMP": root.as_posix(), "HOME": (root / "home").as_posix(),
        "TEST_PYTHON": sys.executable.replace("\\", "/"), "GITHUB_REPOSITORY": REPOSITORY,
        "SOURCE_REF": SOURCE_REF,
        "BUILDER_REF": f"{REPOSITORY}/.github/workflows/{caller}@{SOURCE_REF}",
    })
    if caller == "other.yaml":
        assert result.returncode != 0
        assert not (root / "workspace-trust/policy.json").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        policy = load_github_policy(root / "workspace-trust/policy.json")
        assert policy.repository == REPOSITORY and policy.source_ref == SOURCE_REF
        assert policy.signer_workflow == ".github/workflows/_workspace-distribution.yaml"
        assert policy.builder_workflow == f".github/workflows/{caller}"
        assert policy.trusted_root_sha256 == hashlib.sha256(
            (root / "workspace-trust/root.json").read_bytes(),
        ).hexdigest()

"""Bind release acceptance inputs to the producer and its complete frozen payload."""

import copy
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from siteops_release_assets import FrozenReleaseAssets, ReferencedEngine, ReleaseAsset  # noqa: E402


@pytest.fixture
def admission():
    spec = importlib.util.spec_from_file_location(
        "candidate_admission", ROOT / "scripts" / "admit-release-candidate.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def candidate(tmp_path, admission):
    payload = tmp_path / "payload"
    payload.mkdir()
    names = (
        "siteops-install.zip", "siteops-1.0.0b1-py3-none-any.whl",
        "siteops-bootstrap.sh", "siteops-bootstrap.ps1", "workspace.zip",
    )
    assets = []
    for name in (*names, *(name + ".attestation.jsonl" for name in names), "siteops-workspaces.json"):
        raw = name.encode()
        (payload / name).write_bytes(raw)
        assets.append(ReleaseAsset(name, len(raw), hashlib.sha256(raw).hexdigest()))
    inventory = FrozenReleaseAssets("example/repository", "a" * 40, "refs/heads/main", tuple(assets))
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True,
        "dryRun": False, "source": inventory.source,
        "siteops": {"bundle": True, "releaseTag": None},
        "workspaces": [{"workspace": "workspace", "package": "workspace.zip", "id": "fixture"}],
    }
    raw = json.dumps(plan).encode()
    expected = admission.Expected(
        inventory.repository, inventory.commit, inventory.source_ref, 42, 3,
        ".github/workflows/release.yaml", False, {"plan": 11, "inventory": 12, "payload": 13},
        hashlib.sha256(raw).hexdigest(), hashlib.sha256(inventory.serialized()).hexdigest(),
    )
    run = {
        "id": 42, "run_attempt": 3, "head_sha": expected.commit, "head_branch": "main",
        "path": expected.caller, "repository": {"full_name": expected.repository},
        "event": "workflow_dispatch", "status": "in_progress", "conclusion": None,
    }
    jobs = [{
        "total_count": 1, "jobs": [{
            "id": 51, "run_id": 42, "run_attempt": 3, "head_sha": expected.commit,
            "name": "Prepare release candidate / Assemble release",
            "status": "completed", "conclusion": "success",
        }],
    }]
    artifacts = {
        role: {
            "id": expected.artifacts[role], "name": f"{prefix}-42-3",
            "expired": False, "digest": "sha256:" + "b" * 64, "size_in_bytes": 1024,
            "workflow_run": {"id": 42, "head_sha": expected.commit},
        } for role, prefix in admission.ARTIFACT_ROLES.items()
    }
    return expected, run, jobs, artifacts, raw, inventory, payload


def test_completed_producer_is_admitted_while_the_release_is_still_running(admission, candidate):
    expected, run, jobs, artifacts, raw, inventory, payload = candidate
    admission.check_producer(expected, run, jobs, artifacts)
    verified = []
    result = admission.admit(
        expected, raw, inventory.serialized(), payload,
        lambda role, subject, proof, identity: verified.append(
            (role, subject.name, proof.name, identity.name),
        ),
    )
    assert result["status"] == "admitted"
    assert result["installation"] == result["deployment"] == "not-run"
    assert result["subjects"] == {"engine": 4, "workspace": 1}
    assert result["run"] == 42 and result["attempt"] == 3
    assert result["source"] == expected.source and result["artifacts"] == expected.artifacts
    assert {item[1] for item in verified} == {
        "siteops-install.zip", "siteops-1.0.0b1-py3-none-any.whl",
        "siteops-bootstrap.sh", "siteops-bootstrap.ps1", "workspace.zip",
    }
    assert all(name == identity and proof == name + ".attestation.jsonl"
               for _, name, proof, identity in verified)


@pytest.mark.parametrize("fault", [
    "repository", "commit", "branch", "run", "attempt", "caller", "pull-request",
    "producer-failed", "producer-running", "producer-attempt", "missing-producer",
    "duplicate-producer", "incomplete-pages", "artifact-id", "artifact-run", "artifact-attempt",
    "expired-artifact", "missing-artifact", "artifact-commit", "boolean-run",
])
def test_producer_selection_refuses_unrelated_incomplete_or_failed_inputs(admission, candidate, fault):
    expected, run, jobs, artifacts, *_ = copy.deepcopy(candidate)
    if fault == "repository":
        run["repository"]["full_name"] = "other/repository"
    elif fault in {"commit", "branch", "run", "attempt", "caller", "pull-request", "boolean-run"}:
        field, value = {
            "commit": ("head_sha", "b" * 40), "branch": ("head_branch", "other"),
            "run": ("id", 43), "attempt": ("run_attempt", 4),
            "caller": ("path", ".github/workflows/other.yaml"),
            "pull-request": ("event", "pull_request"), "boolean-run": ("id", True),
        }[fault]
        run[field] = value
    elif fault.startswith("producer-"):
        field, value = {
            "producer-failed": ("conclusion", "failure"),
            "producer-running": ("status", "in_progress"), "producer-attempt": ("run_attempt", 2),
        }[fault]
        jobs[0]["jobs"][0][field] = value
    elif fault == "missing-producer":
        jobs[0]["jobs"][0]["name"] = "Other job"
    elif fault == "duplicate-producer":
        duplicate = {**jobs[0]["jobs"][0], "id": 52}
        jobs[0]["jobs"].append(duplicate)
        jobs[0]["total_count"] = 2
    elif fault == "incomplete-pages":
        jobs[0]["total_count"] = 2
    elif fault == "artifact-id":
        artifacts["payload"]["id"] = 99
    elif fault == "artifact-run":
        artifacts["payload"]["workflow_run"]["id"] = 43
    elif fault == "artifact-attempt":
        artifacts["payload"]["name"] = "release-payload-42-2"
    elif fault == "expired-artifact":
        artifacts["payload"]["expired"] = True
    elif fault == "missing-artifact":
        del artifacts["inventory"]
    else:
        artifacts["payload"]["workflow_run"]["head_sha"] = "b" * 40
    with pytest.raises(admission.AdmissionError):
        admission.check_producer(expected, run, jobs, artifacts)


@pytest.mark.parametrize("fault", [
    "plan-digest", "inventory-digest", "changed-asset", "extra-asset", "missing-asset",
    "preview-plan", "different-source", "duplicate-plan-key",
])
def test_payload_failure_precedes_all_provenance_or_execution(admission, candidate, fault):
    expected, _, _, _, raw, inventory, payload = candidate
    encoded = inventory.serialized()
    if fault == "plan-digest":
        raw += b"\n"
    elif fault == "inventory-digest":
        encoded += b"\n"
    elif fault == "changed-asset":
        (payload / "workspace.zip").write_bytes(b"changed")
    elif fault == "extra-asset":
        (payload / "extra.txt").write_bytes(b"extra")
    elif fault == "missing-asset":
        (payload / "workspace.zip").unlink()
    else:
        plan = json.loads(raw)
        if fault == "preview-plan":
            plan["dryRun"] = True
        elif fault == "different-source":
            plan["source"]["commit"] = "b" * 40
        raw = json.dumps(plan).encode()
        if fault == "duplicate-plan-key":
            raw = raw.replace(b'"active": true', b'"active": true, "active": true')
        expected = replace(expected, plan_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        admission.admit(expected, raw, encoded, payload,
                        lambda *args: pytest.fail("Unadmitted bytes reached provenance verification."))


def test_rejected_provenance_never_produces_an_admission_receipt(admission, candidate):
    expected, _, _, _, raw, inventory, payload = candidate

    def rejected(*args):
        raise ValueError("rejected fixture proof")

    with pytest.raises(ValueError, match="rejected fixture proof"):
        admission.admit(expected, raw, inventory.serialized(), payload, rejected)


def test_preview_admission_retains_its_nonpublication_identity(admission, candidate):
    expected, run, jobs, artifacts, raw, inventory, payload = candidate
    plan = json.loads(raw)
    plan["dryRun"] = True
    raw = json.dumps(plan).encode()
    expected = replace(expected, preview=True, caller=".github/workflows/ci.yaml",
                       plan_sha256=hashlib.sha256(raw).hexdigest())
    run["path"] = expected.caller
    admission.check_producer(expected, run, jobs, artifacts)
    receipt = admission.admit(expected, raw, inventory.serialized(), payload, lambda *args: None)
    assert receipt["preview"] is True
    assert receipt["caller"] == ".github/workflows/ci.yaml"


def test_content_only_admission_checks_its_payload_without_claiming_an_engine_install(
    admission, candidate,
):
    expected, _, _, _, raw, inventory, payload = candidate
    native = tuple(asset for asset in inventory.assets if asset.name.startswith("siteops-")
                   and asset.name != "siteops-workspaces.json")
    remaining = tuple(asset for asset in inventory.assets if asset not in native)
    for asset in native:
        (payload / asset.name).unlink()
    reference = ReferencedEngine("71", "siteops/v1.0.0b1", "b" * 40, native)
    inventory = replace(inventory, assets=remaining, engine=reference)
    plan = json.loads(raw)
    plan["siteops"] = {"bundle": False, "releaseTag": reference.tag}
    raw = json.dumps(plan).encode()
    expected = replace(expected, plan_sha256=hashlib.sha256(raw).hexdigest(),
                       inventory_sha256=hashlib.sha256(inventory.serialized()).hexdigest())
    roles = []
    receipt = admission.admit(expected, raw, inventory.serialized(), payload,
                              lambda role, *args: roles.append(role))
    assert roles == ["workspace"]
    assert receipt["subjects"] == {"engine": 0, "workspace": 1}
    assert receipt["installation"] == "not-run"


def test_job_pagination_and_unwrapped_producer_names_are_supported(admission, candidate):
    expected, run, jobs, artifacts, *_ = candidate
    producer = {**jobs[0]["jobs"][0], "name": "Assemble release"}
    other = {**producer, "id": 52, "name": "Other job", "conclusion": "skipped"}
    pages = [{"total_count": 2, "jobs": [other]}, {"total_count": 2, "jobs": [producer]}]
    admission.check_producer(expected, run, pages, artifacts)


@pytest.mark.parametrize("reject", [False, True])
def test_controller_checks_independent_policy_and_writes_only_a_complete_receipt(
    admission, candidate, tmp_path, monkeypatch, capsys, reject,
):
    expected, run, jobs, artifacts, raw, inventory, payload = candidate
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    for name, data in (("run", run), ("jobs", jobs)):
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    for role, data in artifacts.items():
        (metadata / f"{role}.json").write_text(json.dumps(data))
    (tmp_path / "plan.json").write_bytes(raw)
    (tmp_path / "inventory.json").write_bytes(inventory.serialized())
    (tmp_path / "roots.json").write_text("{}")
    environment = {
        "REPOSITORY": expected.repository, "COMMIT": expected.commit, "REF": expected.ref,
        "RUN": str(expected.run), "ATTEMPT": str(expected.attempt), "CALLER": expected.caller,
        "PREVIEW": "false", "PLAN_SHA": expected.plan_sha256, "INVENTORY_SHA": expected.inventory_sha256,
        **{f"{role.upper()}_ARTIFACT": str(value) for role, value in expected.artifacts.items()},
    }
    for key, value in environment.items():
        monkeypatch.setenv("CANDIDATE_" + key, value)
    calls = []

    class Verifier:
        def __init__(self, directory, root, source, **policy):
            assert root == tmp_path / "roots.json"
            assert source == expected.source
            assert policy["builder"] == ".github/workflows/release.yaml"
            assert policy["runner_environment"] == "self-hosted"
            calls.append(policy["signer"])

        def __call__(self, *args):
            if reject:
                raise ValueError("PRIVATE_VERIFIER_DIAGNOSTIC")

    monkeypatch.setattr(admission, "ReleaseVerifier", Verifier)
    output = tmp_path / "receipt.json"
    arguments = ["admit-release-candidate.py"]
    for key, value in {
        "plan": tmp_path / "plan.json", "inventory": tmp_path / "inventory.json",
        "payload": payload, "run": tmp_path / "run.json", "jobs": tmp_path / "jobs.json",
        "artifact-metadata": metadata, "trusted-root": tmp_path / "roots.json",
        "state": tmp_path / "state", "output": output,
    }.items():
        arguments.extend(["--" + key, str(value)])
    monkeypatch.setattr(sys, "argv", arguments)
    assert admission.main() == int(reject)
    captured = capsys.readouterr()
    assert "PRIVATE_VERIFIER_DIAGNOSTIC" not in captured.out + captured.err
    if reject:
        assert not output.exists()
    else:
        assert json.loads(output.read_text())["subjects"] == {"engine": 4, "workspace": 1}
        assert set(calls) == {
            ".github/workflows/_siteops-distribution.yaml",
            ".github/workflows/_workspace-distribution.yaml",
        }


def test_candidate_workflow_admits_exact_artifacts_after_the_producer_without_azure_authority():
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "_release-candidate.yaml").read_text())
    job = data["jobs"]["admit"]
    assert job["needs"] == ["prepare", "review"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    downloads = [step["with"] for step in job["steps"] if
                 step.get("uses", "").startswith("actions/download-artifact@")]
    assert [step["artifact-ids"] for step in downloads] == [
        "${{ needs.prepare.outputs.artifact-id }}",
        "${{ needs.review.outputs.assets-artifact-id }}",
        "${{ needs.review.outputs.payload-artifact-id }}",
    ]
    assert all(step["run-id"] == "${{ github.run_id }}"
               and step["repository"] == "${{ github.repository }}" and step["digest-mismatch"] == "error"
               for step in downloads)
    script = next(step["run"] for step in job["steps"] if step["name"] == "Admit exact bytes and provenance")
    assert "scripts/admit-release-candidate.py" in script
    assert "--trusted-root" in script and "--artifact-metadata" in script
    metadata = next(step["run"] for step in job["steps"] if step["name"] == "Prepare admission tooling and metadata")
    assert "--paginate --slurp" in metadata
    assert "/attempts/$CANDIDATE_ATTEMPT" in metadata
    assert job["env"]["CANDIDATE_PLAN_SHA"] == "${{ needs.review.outputs.plan-sha }}"
    assert job["env"]["CANDIDATE_INVENTORY_SHA"] == "${{ needs.review.outputs.asset-list-sha }}"
    assert all("azure/login" not in step.get("uses", "") for step in job["steps"])


@pytest.mark.parametrize("failure", ["none", "pip", "run", "jobs", "artifact-12", "roots"])
def test_admission_metadata_step_stops_on_failed_tools_and_keeps_diagnostics_private(tmp_path, failure):
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "_release-candidate.yaml").read_text())
    script = next(step["run"] for step in data["jobs"]["admit"]["steps"]
                  if step["name"] == "Prepare admission tooling and metadata")
    binary = tmp_path / "bin"
    binary.mkdir()
    temporary = tmp_path / "runner-temp"
    temporary.mkdir()
    installer = tmp_path / "installer"
    write_executable(installer, """#!/usr/bin/env bash
[[ "$1 $2 $3" == '-m pip install' ]] || exit 98
if [[ "$FAILURE" == pip ]]; then echo PRIVATE_INSTALL_FAILURE >&2; exit 23; fi
""")
    write_executable(binary / "python", """#!/usr/bin/env bash
[[ "$1 $2" == '-m venv' ]] || exit 98
mkdir -p "$3/bin"
cp "$INSTALLER" "$3/bin/python"
""")
    write_executable(binary / "gh", """#!/usr/bin/env bash
case "$*" in
  'api repos/example/repository/actions/runs/42/attempts/3') operation=run ;;
  'api --paginate --slurp repos/example/repository/actions/runs/42/attempts/3/jobs?per_page=100') operation=jobs ;;
  'api repos/example/repository/actions/artifacts/11') operation=artifact-11 ;;
  'api repos/example/repository/actions/artifacts/12') operation=artifact-12 ;;
  'api repos/example/repository/actions/artifacts/13') operation=artifact-13 ;;
  'attestation trusted-root') operation=roots ;;
  *) exit 98 ;;
esac
echo "$operation" >> calls
if [[ "$FAILURE" == "$operation" ]]; then
  echo PRIVATE_REMOTE_FAILURE >&2
  exit 24
fi
printf '{}\\n'
""")
    result = run_script(script, tmp_path, {
        "RUNNER_TEMP": bash_path(temporary), "INSTALLER": bash_path(installer), "FAILURE": failure,
        "CANDIDATE_REPOSITORY": "example/repository", "CANDIDATE_RUN": "42", "CANDIDATE_ATTEMPT": "3",
        "CANDIDATE_PLAN_ARTIFACT": "11", "CANDIDATE_INVENTORY_ARTIFACT": "12",
        "CANDIDATE_PAYLOAD_ARTIFACT": "13",
    })
    assert result.returncode == (0 if failure == "none" else 1), (result.stdout, result.stderr)
    assert "PRIVATE" not in result.stdout + result.stderr
    expected = ["run", "jobs", "artifact-11", "artifact-12", "artifact-13", "roots"]
    if failure == "pip":
        assert not (tmp_path / "calls").exists()
    else:
        if failure != "none":
            expected = expected[:expected.index(failure) + 1]
        assert (tmp_path / "calls").read_text().splitlines() == expected

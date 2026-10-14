"""Keep live-host coordination separate from completed-job dependencies."""

import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.actions_expressions import evaluate, truthy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fleet_process import FleetProcessError  # noqa: E402
from fleet_workflow import (  # noqa: E402
    ROLES,
    CoordinationError,
    FleetBudget,
    FleetCandidate,
    check_inputs,
    hosts_ready,
    jobs,
    participants_started,
    run_jobs,
    wait_for_job_state,
)
from release_acceptance import FLEET_JOBS, SITE_JOB  # noqa: E402
from siteops_release_assets import FrozenReleaseAssets, ReleaseAsset  # noqa: E402
from workspace_engine import EngineSelection  # noqa: E402

SOURCE = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("repository_value", "environment_value", "expected"), [
    ("repository-fixture", "", "repository-fixture"),
    ("", "environment-fixture", "environment-fixture"),
    ("repository-fixture", "environment-fixture", "environment-fixture"),
])
def test_fleet_onboarding_retains_the_existing_environment_secret_name(
    repository_value, environment_value, expected,
):
    caller = yaml.safe_load((ROOT / ".github/workflows/e2e-test.yaml").read_text())["jobs"]["fleet"]
    callee = yaml.safe_load((ROOT / ".github/workflows/_fleet-acceptance.yaml").read_text())
    host = callee["jobs"]["hosts"]
    connection = next(step for step in host["steps"] if step.get("uses") == "./.github/actions/connect-arc")
    assert "environment" not in caller
    assert host["environment"] == "${{ inputs.environment }}"

    def secret_name(expression):
        match = re.fullmatch(r"\$\{\{ secrets\.([A-Z0-9_]+) \}\}", expression)
        assert match, "This boundary must use one named secret, not a conditional fallback."
        return match[1]

    repository = {"CUSTOM_LOCATIONS_OID": repository_value}
    passed = {key: repository.get(secret_name(value), "") for key, value in caller["secrets"].items()}
    available = {**passed, **({"CUSTOM_LOCATIONS_OID": environment_value} if environment_value else {})}
    name = secret_name(connection["with"]["custom-locations-oid"])
    assert available.get(name) == expected
    assert name in callee.get("on", callee.get(True))["workflow_call"]["secrets"]


def selection():
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate", "source": dict(SOURCE),
        "producer": {"run": 42, "attempt": 3, "caller": ".github/workflows/release.yaml", "preview": False},
        "artifacts": {role: {"id": index, "sha256": "b" * 64} for index, role in enumerate(ROLES, 11)},
    }


def parse(value):
    return FleetCandidate.parse(json.dumps(value).encode(), repository=SOURCE["repository"],
                                commit=SOURCE["commit"], ref=SOURCE["ref"])


def host(slot, *, ready=True, status="in_progress", conclusion=None):
    return {
        "id": 1 if slot == "one" else 2, "run_id": 50, "run_attempt": 1, "head_sha": SOURCE["commit"],
        "name": f"Fleet / Fleet host ({slot})", "status": status, "conclusion": conclusion,
        "steps": [{"name": "Host ready", "status": "completed" if ready else "in_progress",
                   "conclusion": "success" if ready else None}],
    }


class Clock:
    def __init__(self):
        self.value = 0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, duration):
        self.delays.append(duration)
        self.value += duration


@pytest.mark.parametrize("fault", ["source", "producer", "duplicate-id", "bad-hash", "unknown-role", "bool-id"])
def test_candidate_rejects_ambiguous_or_unbound_selections(fault):
    value = selection()
    if fault == "source":
        value["source"]["commit"] = "c" * 40
    elif fault == "producer":
        value["producer"]["caller"] = ".github/workflows/other.yaml"
    elif fault == "duplicate-id":
        value["artifacts"]["engine"]["id"] = value["artifacts"]["plan"]["id"]
    elif fault == "bad-hash":
        value["artifacts"]["engine"]["sha256"] = "not-a-digest"
    elif fault == "unknown-role":
        value["artifacts"]["other"] = {"id": 99, "sha256": "b" * 64}
    else:
        value["artifacts"]["engine"]["id"] = True
    with pytest.raises(ValueError):
        parse(value)


def test_reconciliation_can_use_an_immutable_tag_of_the_same_controller_commit():
    value = selection()
    result = FleetCandidate.parse(json.dumps(value).encode(), repository=SOURCE["repository"],
                                 commit=SOURCE["commit"], ref="refs/tags/v1.0.0b7")
    assert result.source["ref"] == "refs/heads/main"
    assert result.document() == value


@pytest.mark.parametrize("invalid", [False, True])
def test_producer_emits_the_exact_selection_without_inventing_missing_artifact_ids(
    tmp_path, monkeypatch, capsys, invalid,
):
    script = load_script("emit-fleet-candidate")
    selected = selection()
    output, summary = tmp_path / "outputs", tmp_path / "summary"
    environment = {
        "GITHUB_REPOSITORY": SOURCE["repository"], "GITHUB_SHA": SOURCE["commit"],
        "GITHUB_REF": SOURCE["ref"], "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3",
        "CANDIDATE_PREVIEW": "false", "CANDIDATE_CALLER": ".github/workflows/release.yaml",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
    }
    for role, record in selected["artifacts"].items():
        environment[f"FLEET_{role.upper()}_ID"] = str(record["id"])
        environment[f"FLEET_{role.upper()}_SHA"] = record["sha256"]
    if invalid:
        environment["FLEET_ENGINE_ID"] = ""
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert script.main() == int(invalid)
    printed = capsys.readouterr().out
    if invalid:
        assert not output.exists() and not summary.exists()
        assert "Exact candidate selection:" not in printed
    else:
        raw = output.read_text().split("=", 1)[1].strip()
        assert json.loads(raw) == selected
        rendered = summary.read_text()
        assert rendered.splitlines()[1] == "### Exact candidate selection"
        assert "does not authorize Azure use or publication" in rendered
        # The log carries the same single line selection as the job output.
        assert printed.splitlines() == ["Exact candidate selection: " + raw]


@pytest.mark.parametrize("fault", ["id", "attempt", "source", "expired"])
def test_artifact_roles_are_bound_to_the_actual_producer(fault):
    selected = parse(selection())
    document = {
        "id": selected.artifacts["engine"]["id"], "name": "workspace-engine-42-3", "expired": False,
        "size_in_bytes": 100, "digest": "sha256:" + "b" * 64,
        "workflow_run": {"id": 42, "head_sha": SOURCE["commit"]},
    }
    selected.verify_artifact("engine", document)
    if fault == "id":
        document["id"] = 99
    elif fault == "attempt":
        document["name"] = "workspace-engine-42-2"
    elif fault == "source":
        document["workflow_run"]["head_sha"] = "c" * 40
    else:
        document["expired"] = True
    with pytest.raises(CoordinationError):
        selected.verify_artifact("engine", document)


def test_ready_hosts_are_live_not_completed_and_both_slots_are_required():
    values = [host("one"), host("two")]
    assert hosts_ready(values)
    assert not hosts_ready(values[:1])
    assert not hosts_ready([host("one"), host("two", ready=False)])
    for status, conclusion in (("completed", "success"), ("completed", "failure"), ("completed", "cancelled")):
        with pytest.raises(CoordinationError):
            hosts_ready([host("one"), host("two", status=status, conclusion=conclusion)])
    with pytest.raises(CoordinationError, match="ambiguous"):
        hosts_ready([*values, host("one")])


def test_coordination_checks_before_sleeping_and_fails_on_timeout():
    clock = Clock()
    count = 0

    def read(timeout):
        nonlocal count
        assert 0 < timeout <= 5
        count += 1
        return [host("one"), host("two", ready=count > 1)]

    wait_for_job_state(read, mode="ready", timeout=5, interval=1, clock=clock.now, sleep=clock.sleep)
    assert count == 2 and clock.delays == [1]
    clock = Clock()
    with pytest.raises(CoordinationError, match="deadline"):
        wait_for_job_state(lambda timeout: [], mode="ready", timeout=3, interval=1, clock=clock.now, sleep=clock.sleep)
    assert clock.value == 3


@pytest.mark.parametrize("mode,name,marker", [
    ("cleanup", "Fleet cleanup", None), ("deployed", "Fleet controller", "Deploy fleet"),
])
def test_hosts_wait_for_the_correct_terminal_boundary_and_reject_failures(mode, name, marker):
    clock = Clock()
    row = {"name": name, "status": "completed", "conclusion": "success"}
    if marker:
        row["steps"] = [{"name": marker, "status": "completed", "conclusion": "success"}]
    wait_for_job_state(lambda timeout: [row], mode=mode, timeout=2, interval=1, clock=clock.now, sleep=clock.sleep)
    assert not clock.delays
    row["conclusion"] = "failure"
    with pytest.raises(CoordinationError):
        wait_for_job_state(lambda timeout: [row], mode=mode, timeout=2, interval=1, clock=clock.now, sleep=clock.sleep)


@pytest.mark.parametrize("fault", ["attempt", "commit", "count", "duplicate"])
def test_job_metadata_must_be_complete_for_this_invocation(fault):
    values = [host("one"), host("two")]
    pages = [{"total_count": 2, "jobs": values}]
    assert jobs(pages, run=50, attempt=1, commit=SOURCE["commit"]) == values
    if fault == "attempt":
        values[0]["run_attempt"] = 2
    elif fault == "commit":
        values[0]["head_sha"] = "b" * 40
    elif fault == "count":
        pages[0]["total_count"] = 3
    else:
        values[1]["id"] = values[0]["id"]
    with pytest.raises(CoordinationError):
        jobs(pages, run=50, attempt=1, commit=SOURCE["commit"])


@pytest.fixture
def inputs(tmp_path):
    directories = {key: tmp_path / key for key in ROLES}
    for value in directories.values():
        value.mkdir()

    def asset(directory, name):
        raw = name.encode()
        (directory / name).write_bytes(raw)
        return ReleaseAsset(name, len(raw), hashlib.sha256(raw).hexdigest())

    native = tuple(asset(directories["engine"], name) for name in (
        "siteops-install.zip", "siteops-install.zip.attestation.jsonl",
        "siteops-1.0.0b1-py3-none-any.whl", "siteops-1.0.0b1-py3-none-any.whl.attestation.jsonl",
        "siteops-bootstrap.sh", "siteops-bootstrap.sh.attestation.jsonl",
        "siteops-bootstrap.ps1", "siteops-bootstrap.ps1.attestation.jsonl",
    ))
    workspace_assets = tuple(asset(directories["workspaces"], name) for name in (
        "workspace.zip", "workspace.zip.attestation.jsonl", "siteops-workspaces.json",
    ))
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True, "dryRun": False,
        "source": SOURCE, "release": {"tag": "v1.0.0b7"}, "siteops": {"bundle": True, "releaseTag": None},
        "workspaces": [{"workspace": "workspaces/iot-operations", "id": "azure.iot-operations", "package": "workspace.zip"}],
    }
    raw = json.dumps(plan).encode()
    engine = EngineSelection(SOURCE, hashlib.sha256(raw).hexdigest(),
                             FrozenReleaseAssets(SOURCE["repository"], SOURCE["commit"], SOURCE["ref"], native),
                             "1.0.0b1", "c" * 64, (("3.11", "linux-x86_64"),))
    workspaces = FrozenReleaseAssets(SOURCE["repository"], SOURCE["commit"], SOURCE["ref"], workspace_assets)
    reference_assets = tuple(asset(directories["inventory"], name) for name in (
        "siteops-engine.json", "siteops-engine.json.attestation.jsonl",
    ))
    frozen = FrozenReleaseAssets(
        SOURCE["repository"], SOURCE["commit"], SOURCE["ref"], (*native, *workspace_assets, *reference_assets),
    )
    values = selection()
    documents = {
        "plan": ("plan.json", raw),
        "inventory": ("release-assets.json", frozen.serialized()),
        "engine": ("workspace-engine.json", engine.serialized()),
        "workspaces": ("release-assets.json", workspaces.serialized()),
    }
    for role, (name, content) in documents.items():
        (directories[role] / name).write_bytes(content)
        values["artifacts"][role]["sha256"] = hashlib.sha256(content).hexdigest()
    admission = {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission", "source": SOURCE,
        **values["producer"], "artifacts": {"plan": values["artifacts"]["plan"]["id"],
                                          "inventory": values["artifacts"]["inventory"]["id"], "payload": 99},
        "planSha256": values["artifacts"]["plan"]["sha256"],
        "inventorySha256": values["artifacts"]["inventory"]["sha256"],
        "subjects": {"engine": 4, "workspace": 1}, "status": "admitted",
        "installation": "not-run", "deployment": "not-run",
    }
    raw = json.dumps(admission).encode()
    (directories["admission"] / "receipt.json").write_bytes(raw)
    values["artifacts"]["admission"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return tmp_path, values, admission


def test_original_qualification_inputs_match_the_final_frozen_candidate(inputs):
    root, value, admission = inputs
    assert check_inputs(parse(value), root) == admission
    (root / "engine" / "siteops-install.zip").write_bytes(b"changed")
    with pytest.raises(ValueError):
        check_inputs(parse(value), root)


def test_wrong_qualification_record_is_rejected_even_when_its_own_digest_matches(inputs):
    root, value, _ = inputs
    path = root / "engine" / "workspace-engine.json"
    document = json.loads(path.read_bytes())
    document["native"]["assets"][0]["sha256"] = "f" * 64
    raw = json.dumps(document).encode()
    path.write_bytes(raw)
    value["artifacts"]["engine"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(CoordinationError, match="frozen"):
        check_inputs(parse(value), root)


@pytest.mark.parametrize("bad", [False, True])
def test_selection_entrypoint_reads_only_the_bound_producer_before_publishing_outputs(inputs, monkeypatch, bad):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    output = root / "outputs"
    environment = {
        "FLEET_CANDIDATE": json.dumps(value), "GITHUB_REPOSITORY": SOURCE["repository"],
        "GITHUB_SHA": SOURCE["commit"], "GITHUB_REF": SOURCE["ref"], "GITHUB_OUTPUT": str(output),
    }
    for name, content in environment.items():
        monkeypatch.setenv(name, content)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Live metadata query escaped isolation."))
    calls = []

    class Reads:
        def __init__(self, path):
            pass

        def read(self, endpoint, *, pages=False):
            calls.append(endpoint)
            if "/jobs?" in endpoint:
                return [{"total_count": 2, "jobs": [{
                    "id": index, "run_id": 42, "run_attempt": 3, "head_sha": SOURCE["commit"],
                    "name": name, "status": "completed", "conclusion": "success",
                } for index, name in enumerate(("Assemble release", "Admit frozen inputs"), 1)]}]
            if "/artifacts/" in endpoint:
                identity = int(endpoint.rsplit("/", 1)[1])
                role = next(name for name, row in value["artifacts"].items() if row["id"] == identity)
                return {
                    "id": identity, "name": f"{ROLES[role]}-42-3", "expired": False,
                    "size_in_bytes": 100, "digest": "sha256:" + "b" * 64,
                    "workflow_run": {"id": 42, "head_sha": "c" * 40 if bad else SOURCE["commit"]},
                }
            return {
                "id": 42, "run_attempt": 3, "head_sha": SOURCE["commit"], "head_branch": "main",
                "path": ".github/workflows/release.yaml", "event": "workflow_dispatch",
                "repository": {"full_name": SOURCE["repository"]},
            }

    monkeypatch.setattr(script, "GitHubReads", Reads)
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "select", "--root", str(root)])
    assert script.main() == int(bad)
    if bad:
        assert not output.exists()
    else:
        assert "admission-id=11" in output.read_text()
        assert len(calls) == 7
    assert all(endpoint.startswith("repos/example/content/actions/") for endpoint in calls)


@pytest.mark.parametrize(("run_status", "completed_at", "metadata_error"), [
    ("completed", "2026-10-09T12:34:56Z", None),
    ("in_progress", "2026-10-09T12:34:56Z", "original fleet run to be stopped"),
    ("completed", "2026-10-09T12:34:56.123Z", "original job completion time is invalid"),
])
@pytest.mark.parametrize("uploaded", [False, True])
@pytest.mark.parametrize("digest", ["sha256:" + "e" * 64, None])
def test_reconciliation_uses_original_execution_and_requires_the_prewrite_upload(
    inputs, monkeypatch, capsys, uploaded, digest, run_status, completed_at, metadata_error,
):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    output = root / "reconcile-outputs"
    for name, content in {
        "FLEET_CANDIDATE": json.dumps(value), "GITHUB_REPOSITORY": SOURCE["repository"],
        "GITHUB_SHA": SOURCE["commit"], "GITHUB_REF": "refs/tags/v1.0.0b7",
        "GITHUB_RUN_ID": "60", "GITHUB_RUN_ATTEMPT": "1", "FLEET_RUN_ID": "50",
        "FLEET_RUN_ATTEMPT": "2", "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(name, content)
    calls = []

    class Reads:
        def __init__(self, path):
            pass

        def read(self, endpoint, *, pages=False):
            calls.append(endpoint)
            if endpoint.endswith("/runs/50/attempts/2"):
                return {
                    "id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
                    "repository": {"full_name": SOURCE["repository"]}, "status": "completed",
                    "updated_at": "2026-10-09T14:00:00Z",
                }
            if endpoint.endswith("/runs/50"):
                return {"id": 50, "head_sha": SOURCE["commit"],
                        "repository": {"full_name": SOURCE["repository"]}, "status": run_status}
            if "/jobs?" in endpoint:
                return [{"total_count": 1, "jobs": [{
                    "id": 100, "run_id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
                    "name": "Fleet / Fleet prepare", "status": "completed", "conclusion": "failure",
                    "completed_at": completed_at,
                    "steps": [
                        {"name": "Preflight resource ownership", "status": "completed", "conclusion": "success"},
                        {"name": "Retain resource ownership", "status": "completed", "conclusion": "success" if uploaded else "failure"},
                    ],
                }]}]
            return [{"artifacts": [{
                "id": 500, "name": "fleet-ownership-50-2", "expired": False,
                "size_in_bytes": 1000, "digest": digest,
                "workflow_run": {"id": 50, "head_sha": SOURCE["commit"]},
            }]}]

    monkeypatch.setattr(script, "GitHubReads", Reads)
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "ownership", "--root", str(root)])
    assert script.main() == (0 if uploaded and digest and metadata_error is None else 1)
    if metadata_error is not None:
        assert metadata_error in capsys.readouterr().err
        assert calls[-1].endswith("/runs/50" if run_status != "completed"
                                  else "/attempts/2/jobs?per_page=100")
    assert all("/runs/50" in endpoint for endpoint in calls)
    if uploaded and digest and metadata_error is None:
        assert output.read_text().splitlines() == ["ownership-id=500", f"original-completed={completed_at}"]
        assert "ownership-sha" not in output.read_text()
    else:
        assert not output.exists()
        if metadata_error is None:
            assert len(calls) == (4 if uploaded else 3)


@pytest.mark.parametrize("matching_owner", [False, True])
def test_host_requires_the_committed_immutable_owner(inputs, monkeypatch, capsys, matching_owner):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    subscription = "00000000-0000-0000-0000-000000000001"
    for key, content in {
        "FLEET_RUN_ID": "50", "FLEET_RUN_ATTEMPT": "1", "AZURE_SUBSCRIPTION_ID": subscription,
        "GITHUB_ENV": str(root / "host-environment"), "E2E_FLEET_RESOURCE_GROUPS": "",
    }.items():
        monkeypatch.setenv(key, content)
    selected = parse(value)
    monkeypatch.setattr(script, "candidate", lambda: selected)
    monkeypatch.setattr(script, "FleetBudget", SimpleNamespace(
        from_environment=lambda: SimpleNamespace(remaining=lambda *args: 60),
    ))
    scope = script.scope_for(selected, root, run=50, attempt=1, subscription=subscription)
    owners = {"one": "siteops-fleet-" + "1" * 64, "two": "siteops-fleet-" + "2" * 64}
    ownership = root / "host-ownership.json"
    ownership.write_text(json.dumps({
        "apiVersion": "siteops.release.fleet/v1", "kind": "FleetOwnership",
        "context": scope.context(), "scopeKey": scope.key,
        "slots": {slot: {"admittedAbsent": True, "ownerSha256": hashlib.sha256(owner.encode()).hexdigest()}
                  for slot, owner in owners.items()},
    }))
    observed = {
        "id": f"/subscriptions/{subscription}/resourceGroups/{scope.group('one')}",
        "name": scope.group("one"), "tags": scope.tags("one"), "location": "eastus2",
        "managedBy": owners["one" if matching_owner else "two"],
    }
    monkeypatch.setattr(script, "AzureGroups", lambda *args: SimpleNamespace(show=lambda slot: observed))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Live request escaped the fixture."))
    monkeypatch.setattr(sys, "argv", [
        "coordinate-release-fleet.py", "check-ownership", "--root", str(root), "--slot", "one",
        "--ownership", str(ownership), "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest(),
    ])
    assert script.main() == (0 if matching_owner else 1)
    captured = capsys.readouterr()
    assert all(owner not in captured.out + captured.err for owner in owners.values())
    assert ((root / "host-environment").read_text() if matching_owner else None) == (
        "FLEET_LOCATION=eastus2\n" if matching_owner else None)


def test_workflow_keeps_hosts_live_and_uploads_ownership_before_creation():
    flow = yaml.safe_load((ROOT / ".github/workflows/_fleet-acceptance.yaml").read_text())
    jobs = flow["jobs"]
    assert jobs["controller"]["needs"] == ["select", "prepare"]
    assert jobs["hosts"]["needs"] == ["select", "prepare"]
    assert jobs["hosts"]["strategy"]["matrix"]["slot"] == ["one", "two"]
    assert jobs["hosts"]["strategy"]["fail-fast"] is False
    assert "hosts" not in jobs["cleanup"]["needs"]
    assert "always()" in jobs["cleanup"]["if"]
    assert jobs["hosts"]["timeout-minutes"] > jobs["controller"]["timeout-minutes"] + jobs["cleanup"]["timeout-minutes"]
    names = [step.get("name") for step in jobs["prepare"]["steps"]]
    assert names.index("Preflight resource ownership") < names.index("Retain resource ownership") < names.index("Prepare fleet resource groups")
    host_steps = jobs["hosts"]["steps"]
    held = next(step for step in host_steps if step.get("name") == "Hold host through cleanup")
    assert "always()" in held["if"] and "wait-cleanup" in held["run"]
    assert "steps.ready.outcome == 'success'" in held["if"]
    assert "steps.participants.outcome == 'success'" in held["if"]
    controller_names = [step.get("name") for step in jobs["controller"]["steps"]]
    assert controller_names.index("Prepare installed candidate project") < controller_names.index("Await both live hosts")
    for key in ("controller", "hosts"):
        assert jobs[key]["env"]["FLEET_STARTED_AT"] == "${{ needs.prepare.outputs.started-at }}"
        assert any("wait-participants" in step.get("run", "") for step in jobs[key]["steps"])
    assert any(step.get("name") == "Observe host readiness" for step in host_steps)
    assert not any(step.get("uses", "").startswith("azure/login") for step in jobs["select"]["steps"])
    for name in ("prepare", "hosts", "controller", "cleanup"):
        assert jobs[name]["environment"] == "${{ inputs.environment }}"
        assert jobs[name]["runs-on"] == "ubuntu-24.04"
        assert "attestations" not in jobs[name]["permissions"]
    caller = yaml.safe_load((ROOT / ".github/workflows/e2e-test.yaml").read_text())
    assert caller["jobs"]["fleet"]["needs"] == ["fleet-request", "prep"]
    assert caller["jobs"]["fleet-cleanup"]["with"]["kind"] == (
        "${{ inputs.scenario == 'site-cleanup' && 'site' || 'fleet' }}"
    )


@pytest.mark.parametrize(("ownership_id", "cancelled", "prior_result", "expected"), [
    pytest.param("123", False, "success", True, id="verified-ownership"),
    pytest.param("", False, "success", False, id="no-owned-group"),
    pytest.param(None, False, "success", False, id="missing-ownership-output"),
    pytest.param("123", True, "failure", True, id="retain-after-cancellation"),
    pytest.param("", True, "failure", False, id="cancelled-without-ownership"),
    pytest.param("123", False, "failure", True, id="retain-after-reconciliation-failure"),
    pytest.param("123", False, "skipped", True, id="retain-after-reconciliation-skip"),
])
def test_reconciliation_work_requires_ownership_and_retains_an_admitted_outcome(
    ownership_id, cancelled, prior_result, expected,
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/_fleet-reconcile.yaml").read_text())
    steps = workflow["jobs"]["reconcile"]["steps"]
    guarded = [
        next(step for step in steps if step.get("with", {}).get("path") == "${{ runner.temp }}/fleet/ownership"),
        next(step for step in steps if step.get("uses", "").startswith("azure/login@")),
        next(step for step in steps if step.get("name") == "Reconcile original owned groups"),
    ]
    outputs = {} if ownership_id is None else {"ownership-id": ownership_id}
    contexts = {
        "steps": {"ownership": {"outputs": outputs}},
        "needs": {"previous": {"result": prior_result}},
    }
    for step in guarded:
        assert truthy(evaluate(step["if"], contexts)) is (ownership_id not in ("", None))
    retained = next(step for step in steps if step.get("name") == "Retain reconciliation outcome")
    assert truthy(evaluate(retained["if"], contexts, cancelled=cancelled)) is expected


def test_allocation_markers_stay_private_and_cleanup_consumes_verified_original_receipts():
    flow = yaml.safe_load((ROOT / ".github/workflows/_fleet-acceptance.yaml").read_text())
    steps = flow["jobs"]["prepare"]["steps"]
    preflight = next(step for step in steps if step.get("id") == "preflight")
    creation = next(step for step in steps if step.get("name") == "Prepare fleet resource groups")
    upload = next(step for step in steps if step.get("id") == "ownership")
    for step in (preflight, creation):
        assert '--allocation-state "$root/allocation.json"' in step["run"]
    assert upload["with"]["path"] == "${{ runner.temp }}/fleet/ownership.json"
    for filename, job, run_id, label in (
        ("_fleet-acceptance.yaml", "cleanup", "${{ github.run_id }}", "Reconcile owned resource groups"),
        ("_fleet-reconcile.yaml", "reconcile", "${{ inputs.original-run }}", "Reconcile original owned groups"),
    ):
        source = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        steps = source["jobs"][job]["steps"]
        download = next(step for step in steps if step.get("with", {}).get("path") == (
            "${{ runner.temp }}/fleet/ownership"
        ))
        assert download["with"]["artifact-ids"] == "${{ steps.ownership.outputs.ownership-id }}"
        assert download["with"]["run-id"] == run_id
        assert download["with"]["repository"] == "${{ github.repository }}"
        assert download["with"]["digest-mismatch"] == "error"
        cleanup_step = next(step for step in steps if step.get("name") == label)
        assert steps.index(download) < steps.index(cleanup_step)
        assert 'sha256sum "$RUNNER_TEMP/fleet/ownership/ownership.json"' in cleanup_step["run"]
        assert '--expected-ownership-sha "$OWNERSHIP_SHA"' in cleanup_step["run"]
        assert "--allocation-state" not in cleanup_step["run"]
        assert "ownership-sha" not in cleanup_step.get("env", {}).get("OWNERSHIP_SHA", "")


def test_reconciliation_passes_original_job_completion_bound_through_environment():
    flow = yaml.safe_load((ROOT / ".github/workflows/_fleet-reconcile.yaml").read_text())
    steps = flow["jobs"]["reconcile"]["steps"]
    ownership = next(step for step in steps if step.get("id") == "ownership")
    reconcile = next(step for step in steps if step.get("name") == "Reconcile original owned groups")
    assert steps.index(ownership) < steps.index(reconcile)
    assert reconcile["env"]["ORIGINAL_COMPLETED"] == "${{ steps.ownership.outputs.original-completed }}"
    script = reconcile["run"]
    assert 'if [[ -n "$ORIGINAL_COMPLETED" ]]; then' in script
    assert 'bound=(--created-before "$ORIGINAL_COMPLETED")' in script
    command = next(line for line in script.splitlines() if "manage-release-fleet.py cleanup" in line)
    assert "${bound[@]}" in shlex.split(command.removesuffix("\\"))
    assert "${{ " not in script


@pytest.mark.skipif(os.name != "posix", reason="The fleet command supervisor runs on Linux hosts.")
def test_linux_supervisor_stops_its_owned_process_group_and_keeps_output_private(tmp_path):
    from fleet_process import run

    code = run(
        [sys.executable, "-c", "import time; print('private-output', flush=True); time.sleep(30)"],
        cwd=tmp_path, logs=tmp_path, name="timeout", timeout=1,
    )
    assert code == 124
    assert (tmp_path / "timeout.out").read_text().strip() == "private-output"


@pytest.mark.parametrize("malformed", ["", "nonsense", "-1", "nan"])
def test_runtime_budget_requires_one_valid_clock(malformed):
    with pytest.raises(CoordinationError):
        FleetBudget(malformed, wall=lambda: 1000)


def test_phase_budgets_never_reset_and_keep_cleanup_headroom():
    clock = Clock()
    budget = FleetBudget("1000", wall=lambda: 1000, clock=clock.now)
    assert budget.remaining("deployed", 9000) == 9000
    clock.value = 244 * 60
    assert budget.remaining("deployed", 9000) == 60
    assert budget.remaining("observed", 1200) == 1200
    clock.value = 245 * 60
    with pytest.raises(CoordinationError, match="headroom"):
        budget.remaining("deployed", 9000)
    clock.value = 265 * 60
    with pytest.raises(CoordinationError):
        budget.remaining("observed", 1200)
    assert budget.remaining("cleanup", 21600) == 55 * 60
    clock.value = 320 * 60
    with pytest.raises(CoordinationError):
        budget.remaining("cleanup", 21600)
    # A later job/step reconstructs the same deadline, not a fresh allowance.
    later = FleetBudget("1000", wall=lambda: 1000 + 310 * 60, clock=clock.now)
    assert later.remaining("cleanup", 21600) == 10 * 60


def test_participant_barrier_prevents_two_held_hosts_starving_the_controller():
    clock = Clock()
    values = [host("one", ready=False), host("two", ready=False),
              {"name": "Fleet controller", "status": "queued", "conclusion": None}]
    assert not participants_started(values)
    with pytest.raises(CoordinationError, match="capacity"):
        wait_for_job_state(lambda timeout: values, mode="participants", timeout=3, interval=1,
                           clock=clock.now, sleep=clock.sleep)
    assert clock.value == 3
    values[2]["status"] = "in_progress"
    assert participants_started(values)


def test_metadata_calls_cannot_consume_a_fresh_timeout_after_the_phase_expires():
    clock = Clock()
    bounds = []

    def read(timeout):
        bounds.append(timeout)
        clock.value += timeout
        return [host("one"), host("two")]

    with pytest.raises(CoordinationError, match="metadata exceeded"):
        wait_for_job_state(read, mode="ready", timeout=2, interval=1, clock=clock.now, sleep=clock.sleep)
    assert bounds == [2] and clock.value == 2


def test_clock_does_not_start_a_new_wait_when_less_than_one_poll_interval_remains():
    clock = Clock()
    bounds = []

    def read(timeout):
        bounds.append(timeout)
        return []

    with pytest.raises(CoordinationError, match="deadline"):
        wait_for_job_state(read, mode="ready", timeout=0.5, interval=15, clock=clock.now, sleep=clock.sleep)
    assert bounds == [0.5] and clock.delays == [0.5]


@pytest.mark.skipif(os.name != "posix", reason="The fleet process supervisor runs on Linux hosts.")
def test_linux_supervisor_caps_child_output_without_publishing_it(tmp_path):
    from fleet_process import MAX_LOG_BYTES, run

    code = run([sys.executable, "-c", f"import sys;sys.stdout.buffer.write(b'x'*{MAX_LOG_BYTES + 1})"],
               cwd=tmp_path, logs=tmp_path, name="oversized", timeout=10)
    assert code == 125
    assert (tmp_path / "oversized.out").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("outcome", "shutdown_timeout", "expected"), [
    ("success", False, 0), ("failure", False, 23), ("signal", False, 130),
    ("timeout", False, 124), ("output", False, 125),
    ("timeout", True, 124), ("output", True, 125),
])
def test_process_shutdown_retains_bounded_private_failure(
    tmp_path, monkeypatch, outcome, shutdown_timeout, expected,
):
    helper = load_script("fleet_process")
    clock = Clock()
    calls, signals, permissions = [], [], []
    code = {"success": 0, "failure": 23, "signal": -15}.get(outcome)

    def wait(*, timeout):
        calls.append(("wait", timeout))
        if shutdown_timeout:
            clock.sleep(timeout)
            raise subprocess.TimeoutExpired(["private-command-marker"], timeout, b"private-output-marker")
        return code if code is not None else -9

    process = SimpleNamespace(pid=12345, poll=lambda: code, wait=wait)

    def popen(arguments, **kwargs):
        assert arguments == ["private-command-marker"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"] == {"PRIVATE_INPUT": "private-environment-marker"}
        return process

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        if code is not None:
            raise ProcessLookupError

    monkeypatch.setattr(helper, "os", SimpleNamespace(
        name="posix", killpg=killpg,
        fchmod=lambda fd, mode: permissions.append(mode),
        fstat=lambda fd: SimpleNamespace(st_size=helper.MAX_LOG_BYTES + 1 if outcome == "output" else 0),
    ))
    monkeypatch.setattr(helper, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(helper, "time", SimpleNamespace(monotonic=clock.now, sleep=clock.sleep))
    monkeypatch.setattr(helper, "subprocess", SimpleNamespace(
        Popen=popen, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    arguments = dict(cwd=tmp_path, logs=tmp_path, name="shutdown", timeout=0.1,
                     environment={"PRIVATE_INPUT": "private-environment-marker"})
    if shutdown_timeout:
        with pytest.raises(ValueError, match="cleanup deadline") as caught:
            helper.run(["private-command-marker"], **arguments)
        assert caught.value.code == expected
        assert "private-" not in str(caught.value)
        assert caught.value.__suppress_context__ is True
    else:
        assert helper.run(["private-command-marker"], **arguments) == expected
    assert calls == [("wait", 5)]
    assert permissions == [0o600, 0o600]
    assert signals[0] == 15
    assert (9 in signals) is (code is None)
    assert clock.value <= 10.2


def test_metadata_polling_keeps_only_the_latest_owned_diagnostics(tmp_path, monkeypatch):
    script = load_script("coordinate-release-fleet")
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    directory = tmp_path / "metadata"

    def run(arguments, *, cwd, logs, name, timeout):
        assert timeout == 3
        (logs / f"{name}.out").write_text('{"value":"observed"}')
        (logs / f"{name}.err").write_text("private-provider-marker")
        return 0

    monkeypatch.setattr(script, "run_private", run)
    reader = script.GitHubReads(directory)
    for _ in range(3):
        assert reader.read("repos/example/content/actions/runs/42", timeout=3) == {"value": "observed"}
    assert {path.name for path in directory.iterdir()} == {"3.out", "3.err"}


@pytest.mark.parametrize("code", [124, 125])
def test_metadata_shutdown_failure_stops_coordination_without_publishing_outputs(
    tmp_path, monkeypatch, capsys, code,
):
    script = load_script("coordinate-release-fleet")
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setattr(script, "candidate", lambda: parse(selection()))
    calls = []

    def run(*args, **kwargs):
        calls.append(True)
        raise FleetProcessError(code)

    monkeypatch.setattr(script, "run_private", run)
    monkeypatch.setattr(sys, "argv", [
        "coordinate-release-fleet.py", "select", "--root", str(tmp_path),
    ])
    assert script.main() == code
    assert calls == [True]
    assert not (tmp_path / "outputs").exists()
    captured = capsys.readouterr()
    assert "cleanup deadline" in captured.err
    assert "synthetic-token" not in captured.out + captured.err


@pytest.mark.parametrize("fault", [None, "future-attempt", "other-run", "duplicate", "unnamed"])
def test_run_jobs_accept_every_earlier_attempt_of_this_run_only(fault):
    values = [{"id": index, "run_id": 50, "run_attempt": attempt, "head_sha": SOURCE["commit"], "name": "Site case"}
              for index, attempt in enumerate((1, 2, 3), 1)]
    if fault == "future-attempt":
        values[0]["run_attempt"] = 4
    elif fault == "other-run":
        values[0]["run_id"] = 51
    elif fault == "duplicate":
        values[1]["id"] = 1
    elif fault == "unnamed":
        values[0]["name"] = None
    pages = [{"total_count": 3, "jobs": values}]
    if fault is None:
        assert run_jobs(pages, run=50, attempt=3, commit=SOURCE["commit"]) == values
    else:
        with pytest.raises(CoordinationError):
            run_jobs(pages, run=50, attempt=3, commit=SOURCE["commit"])


def site_environment(monkeypatch, root, value, **extra):
    for name, content in {
        "FLEET_CANDIDATE": json.dumps(value), "GITHUB_REPOSITORY": SOURCE["repository"],
        "GITHUB_SHA": SOURCE["commit"], "GITHUB_REF": SOURCE["ref"], "FLEET_RUN_ID": "50",
        "FLEET_RUN_ATTEMPT": "2", "GITHUB_RUN_ID": "60", "GITHUB_RUN_ATTEMPT": "1",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "GITHUB_ENV": str(root / "environment"), "GITHUB_OUTPUT": str(root / "outputs"), **extra,
    }.items():
        monkeypatch.setenv(name, content)


@pytest.mark.parametrize("marker", ["rg%0Aprivate-marker", "rg-private\r\n::warning::forged"])
def test_site_scope_masks_every_private_name_before_publishing_it(inputs, monkeypatch, capsys, marker):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value)
    real = script.scope_for

    def scope_for(*args, **kwargs):
        assert kwargs["slot"] == "existing"
        selected = real(*args, **kwargs)
        return SimpleNamespace(subscription=selected.subscription, group=lambda slot: marker,
                               cluster=lambda slot: "arc%25private", vault=lambda slot: "kv%private")

    monkeypatch.setattr(script, "scope_for", scope_for)
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "scope", "--root", str(root),
                                      "--kind", "site", "--slot", "existing"])
    code = script.main()
    captured = capsys.readouterr()
    if "\n" in marker:
        assert code == 1
        assert not (root / "environment").exists()
        assert "::add-mask::" not in captured.out
        assert "rg-private" not in captured.out + captured.err and "forged" not in captured.out + captured.err
        return
    assert code == 0
    masks = [line for line in captured.out.splitlines() if line.startswith("::add-mask::")]
    assert masks[:2] == ["::add-mask::rg%250Aprivate-marker", "::add-mask::arc%2525private"]
    assert masks[-1] == "::add-mask::kv%25private"
    assert all(line.startswith("::add-mask::") for line in captured.out.splitlines())
    environment = (root / "environment").read_text()
    assert f"FLEET_RESOURCE_GROUP={marker}\n" in environment and "FLEET_VAULT_NAME=kv%private\n" in environment


@pytest.mark.parametrize("arguments", [["--slot", "existing"], ["--kind", "site", "--slot", "one"],
                                       ["--kind", "site"]])
def test_site_slots_and_site_kind_are_selected_together(inputs, monkeypatch, arguments):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value)
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "scope", "--root", str(root), *arguments])
    assert script.main() == 1
    assert not (root / "environment").exists()


def test_each_phase_gets_a_fresh_policy_from_the_admitted_roots(inputs, monkeypatch, capsys):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value)
    state = root / "controller"
    (state / "workspace-policy").mkdir(parents=True)
    (state / "workspace-policy" / "root.json").write_bytes(b'{"admitted":"roots"}')
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "policy", "--root", str(root),
                                      "--state", str(state)])
    printed = []
    for _ in range(2):
        assert script.main() == 0
        printed.append(capsys.readouterr().out.strip())
    assert printed == [str(root / "policy-1"), str(root / "policy-2")]
    policy = json.loads((root / "policy-2" / "policy.json").read_text())
    assert policy["provider"] == {
        "kind": "github-attestation/v1", "repository": SOURCE["repository"], "sourceRef": "refs/heads/main",
        "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
        "builderWorkflow": ".github/workflows/release.yaml", "runnerEnvironment": "self-hosted",
    }
    assert (root / "policy-2" / "root.json").read_bytes() == b'{"admitted":"roots"}'


def _reads(responses, calls):
    class Reads:
        def __init__(self, path):
            pass

        def read(self, endpoint, *, pages=False, timeout=60):
            calls.append(endpoint)
            for fragment, value in responses.items():
                if fragment in endpoint:
                    return value
            pytest.fail(f"Unexpected metadata request: {endpoint}")

    return Reads


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize(("job", "retained", "artifact", "expected"), [
    (False, None, False, ("", 0)),
    (True, "failure", False, ("", 0)),
    (True, "skipped", False, ("", 0)),
    (True, "success", True, ("700", 0)),
    (True, "success", False, (None, 1)),
    (True, None, False, (None, 1)),
])
def test_site_reconciliation_reports_nothing_only_when_creation_could_not_start(
    inputs, monkeypatch, job, retained, artifact, expected, standalone,
):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value)
    if not standalone:
        monkeypatch.setenv("GITHUB_RUN_ID", "50")
    steps = [] if retained is None else [{"name": "Retain Site ownership", "status": "completed",
                                         "conclusion": retained}]
    jobs_page = [{"total_count": 1 if job else 1, "jobs": [{
        "id": 1, "run_id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
        "name": "Site case (site-existing-secretsync)" if job else "Site case (site-combined)", "steps": steps,
        "status": "completed", "completed_at": "2026-10-09T12:34:56Z",
    }]}]
    artifacts = [{"artifacts": [{
        "id": 700, "name": "site-ownership-50-2-existing", "expired": False, "size_in_bytes": 10,
        "digest": "sha256:" + "e" * 64, "workflow_run": {"id": 50, "head_sha": SOURCE["commit"]},
    }] if artifact else []}]
    calls = []
    monkeypatch.setattr(script, "GitHubReads", _reads({
        "/attempts/2/jobs": jobs_page, "/runs/50/artifacts": artifacts,
        "/runs/50/attempts/2": {"id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
                                "repository": {"full_name": SOURCE["repository"]}, "status": "completed",
                                "updated_at": "2026-10-09T14:00:00Z"},
        "/runs/50": {"id": 50, "head_sha": SOURCE["commit"],
                     "repository": {"full_name": SOURCE["repository"]}, "status": "completed"},
    }, calls))
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "ownership", "--root", str(root),
                                      "--kind", "site", "--slot", "existing"])
    assert script.main() == expected[1]
    if expected[0] is None:
        assert not (root / "outputs").exists()
    else:
        timestamp = "2026-10-09T12:34:56Z" if standalone and expected[0] else ""
        assert (root / "outputs").read_text().splitlines() == [
            f"ownership-id={expected[0]}", f"original-completed={timestamp}",
        ]
    assert any(call.endswith("/runs/50") for call in calls) == standalone


def _original_ownership(inputs, monkeypatch, kind):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value, E2E_SITE_RESOURCE_GROUP="", E2E_FLEET_RESOURCE_GROUPS="")
    values = [{
        "id": index, "run_id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
        "name": f"Fleet / {name}", "status": "completed", "completed_at": f"2026-10-09T12:{34 + index}:00Z",
        "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                  for step in ("Preflight resource ownership", "Retain resource ownership")],
    } for index, name in enumerate(FLEET_JOBS, 1)]
    values += [{
        "id": 100, "run_id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
        "name": SITE_JOB["existing"], "status": "completed", "completed_at": "2026-10-09T12:34:56Z",
        "steps": [{"name": "Retain Site ownership", "status": "completed", "conclusion": "success"}],
    }, {
        "id": 101, "run_id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
        "name": SITE_JOB["enabled"], "status": "completed", "completed_at": "2026-10-09T16:00:00Z",
    }]
    original = {"id": 50, "run_attempt": 2, "head_sha": SOURCE["commit"],
                "repository": {"full_name": SOURCE["repository"]}, "status": "completed",
                "updated_at": "2026-10-09T17:00:00Z"}
    artifact = "site-ownership-50-2-existing" if kind == "site" else "fleet-ownership-50-2"
    calls = []
    monkeypatch.setattr(script, "GitHubReads", _reads({
        "/attempts/2/jobs": [{"total_count": len(values), "jobs": values}],
        "/runs/50/artifacts": [{"artifacts": [{
            "id": 700, "name": artifact, "expired": False, "size_in_bytes": 10,
            "digest": "sha256:" + "e" * 64, "workflow_run": {"id": 50, "head_sha": SOURCE["commit"]},
        }]}],
        "/runs/50/attempts/2": original,
        "/runs/50": {"id": 50, "head_sha": SOURCE["commit"],
                     "repository": {"full_name": SOURCE["repository"]}, "status": "completed"},
    }, calls))
    arguments = ["--kind", "site", "--slot", "existing"] if kind == "site" else []
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "ownership", "--root", str(root), *arguments])
    return script, root, original, values, calls


@pytest.mark.parametrize("kind", ["site", "fleet"])
@pytest.mark.parametrize("updated_at", ["2026-10-09T17:00:00Z", None, "invalid"])
def test_reconciliation_ownership_uses_original_job_completion(inputs, monkeypatch, kind, updated_at):
    script, root, original, _, calls = _original_ownership(inputs, monkeypatch, kind)
    original["updated_at"] = updated_at
    assert script.main() == 0
    completed = "2026-10-09T12:34:56Z" if kind == "site" else "2026-10-09T12:41:00Z"
    assert (root / "outputs").read_text().splitlines() == ["ownership-id=700", f"original-completed={completed}"]
    assert all("/runs/50" in endpoint for endpoint in calls)


@pytest.mark.parametrize(("kind", "name"), [
    ("site", SITE_JOB["existing"]), *(("fleet", name) for name in FLEET_JOBS),
])
@pytest.mark.parametrize("status", ["in_progress", "queued", None])
def test_reconciliation_ownership_requires_every_present_owning_job_completed(
    inputs, monkeypatch, capsys, kind, name, status,
):
    script, root, _, values, _ = _original_ownership(inputs, monkeypatch, kind)
    script.named_job(values, name)["status"] = status
    assert script.main() == 1
    assert capsys.readouterr().err == "The original owning job has not completed.\n"
    assert not (root / "outputs").exists()


@pytest.mark.parametrize("kind", ["site", "fleet"])
@pytest.mark.parametrize("completed_at", [
    None, 42, "2026-10-09T12:34:56.123Z", "2026-10-09T12:34:56+00:00",
    "2026-10-09T12:34:56", "2026-02-30T12:34:56Z", "2026-10-09T12:34:56Z\n",
])
def test_reconciliation_ownership_requires_exact_original_job_timestamp(
    inputs, monkeypatch, capsys, kind, completed_at,
):
    script, root, _, values, _ = _original_ownership(inputs, monkeypatch, kind)
    name = SITE_JOB["existing"] if kind == "site" else FLEET_JOBS[0]
    script.named_job(values, name)["completed_at"] = completed_at
    assert script.main() == 1
    assert capsys.readouterr().err == "The original job completion time is invalid.\n"
    assert not (root / "outputs").exists()


@pytest.mark.parametrize(("status", "completed_at", "error"), [
    ("in_progress", "2026-10-09T12:34:56Z", "The original owning job has not completed.\n"),
    ("completed", None, "The original job completion time is invalid.\n"),
])
def test_site_reconciliation_validates_present_job_before_reporting_no_ownership(
    inputs, monkeypatch, capsys, status, completed_at, error,
):
    script, root, _, values, _ = _original_ownership(inputs, monkeypatch, "site")
    job = script.named_job(values, SITE_JOB["existing"])
    job.update(status=status, completed_at=completed_at)
    job["steps"][0]["conclusion"] = "failure"
    assert script.main() == 1
    assert capsys.readouterr().err == error
    assert not (root / "outputs").exists()


def test_evidence_selection_publishes_only_ids_of_the_latest_attempts(inputs, monkeypatch):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value, GITHUB_RUN_ID="60", GITHUB_RUN_ATTEMPT="2")
    names = ["Site case (site-aio)", "Site case (site-combined)",
             "Site case (site-existing-secretsync)", "Site case (site-existing-secretsync)",
             *(f"Fleet / {name}" for name in ("Select fleet candidate", "Fleet prepare", "Fleet host (one)",
                                              "Fleet host (two)", "Fleet controller", "Fleet cleanup",
                                              "Require complete fleet acceptance"))]
    executions = [{"id": index, "run_id": 60, "run_attempt": 2 if index == 4 else 1, "head_sha": SOURCE["commit"],
                   "name": name, "status": "completed", "conclusion": "success"}
                  for index, name in enumerate(names, 1)]
    artifacts = [{"id": 800 + index, "name": name, "expired": False, "size_in_bytes": 10,
                  "digest": "sha256:" + "e" * 64, "workflow_run": {"id": 60, "head_sha": SOURCE["commit"]}}
                 for index, name in enumerate(("site-outcome-60-1-disabled", "site-outcome-60-1-enabled",
                                               "site-outcome-60-1-existing", "site-outcome-60-2-existing",
                                               "fleet-acceptance-60-1"))]
    calls = []
    monkeypatch.setattr(script, "GitHubReads", _reads({
        "/runs/60/jobs?filter=all": [{"total_count": len(executions), "jobs": executions}],
        "/runs/60/artifacts": [{"artifacts": artifacts}],
    }, calls))
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "evidence", "--root", str(root)])
    assert script.main() == 0
    assert (root / "outputs").read_text().splitlines() == [
        "disabled-id=800", "enabled-id=801", "existing-id=803", "fleet-id=804"]
    assert json.loads((root / "run-jobs.json").read_text())[0]["jobs"] == executions
    assert all(endpoint.startswith("repos/example/content/actions/runs/60/") for endpoint in calls)


@pytest.mark.parametrize(("kind", "value", "expected"), [
    ("site", "", None), ("site", "rg-Site_1.(a)", ("rg-Site_1.(a)",)),
    ("fleet", "", None), ("fleet", "rg-one,RG-two", ("rg-one", "RG-two")),
    ("site", " rg-private-marker", ValueError), ("site", "rg-private-marker.", ValueError),
    ("site", "rg-private-marker\n", ValueError), ("site", "rg-private,marker", ValueError),
    ("fleet", "rg-private-marker", ValueError), ("fleet", "rg-private-marker, rg-two", ValueError),
    ("fleet", "rg-private-marker,RG-PRIVATE-MARKER", ValueError), ("fleet", "a,b,c", ValueError),
    ("site", "rg-" + "x" * 88, ValueError),
])
def test_persistent_group_secrets_have_a_strict_shape_and_are_never_echoed(kind, value, expected):
    from fleet_workflow import supplied_groups

    environment = {"E2E_SITE_RESOURCE_GROUP" if kind == "site" else "E2E_FLEET_RESOURCE_GROUPS": value}
    if expected is ValueError:
        with pytest.raises(CoordinationError) as caught:
            supplied_groups(kind, environment)
        assert "private" not in str(caught.value) and "rg-" not in str(caught.value)
    else:
        assert supplied_groups(kind, environment) == expected


@pytest.mark.parametrize(("kind", "site", "fleet", "expected"), [
    ("site", "rg-One", "RG-ONE,rg-two", True),
    ("fleet", "Rg-Two", "rg-one,rg-two", True),
    ("site", "rg-site", "rg-one,rg-two", False),
    ("fleet", "rg-site", "rg-one,rg-two", False),
    ("site", "rg-site", "", False),
    ("fleet", "", "rg-one,rg-two", False),
])
def test_site_and_fleet_group_secrets_must_differ(kind, site, fleet, expected):
    from fleet_workflow import supplied_groups

    environment = {"E2E_SITE_RESOURCE_GROUP": site, "E2E_FLEET_RESOURCE_GROUPS": fleet}
    own = site if kind == "site" else fleet
    if expected:
        with pytest.raises(CoordinationError) as caught:
            supplied_groups(kind, environment)
        assert str(caught.value) == "The single Site group must differ from both fleet groups."
    else:
        assert supplied_groups(kind, environment) == (tuple(own.split(",")) if own else None)
    environment.pop("E2E_FLEET_RESOURCE_GROUPS" if kind == "site" else "E2E_SITE_RESOURCE_GROUP")
    if own:
        assert supplied_groups(kind, environment) == tuple(own.split(","))


def test_persistent_hosts_take_their_region_from_the_supplied_group(inputs, monkeypatch, capsys):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    subscription = "00000000-0000-0000-0000-000000000001"
    for key, content in {"FLEET_RUN_ID": "50", "FLEET_RUN_ATTEMPT": "1", "AZURE_SUBSCRIPTION_ID": subscription,
                         "GITHUB_ENV": str(root / "host-environment"),
                         "E2E_FLEET_RESOURCE_GROUPS": "rg-private-one,rg-private-two"}.items():
        monkeypatch.setenv(key, content)
    selected = parse(value)
    monkeypatch.setattr(script, "candidate", lambda: selected)
    monkeypatch.setattr(script, "FleetBudget", SimpleNamespace(
        from_environment=lambda: SimpleNamespace(remaining=lambda *args: 60)))
    scope = script.scope_for(selected, root, run=50, attempt=1, subscription=subscription)
    assert scope.mode == "persistent" and scope.group("two") == "rg-private-two"
    snapshot = sorted({scope.snapshot_digest(f"{scope.group_id('two')}/providers/X/y/old")})
    rows = {slot: {"admittedAbsent": False, "snapshot": snapshot,
                   "ownerSha256": scope.snapshot_commitment(slot, snapshot)} for slot in ("one", "two")}
    ownership = root / "persistent-ownership.json"
    ownership.write_text(json.dumps({"apiVersion": "siteops.release.fleet/v1", "kind": "FleetOwnership",
                                     "context": scope.context(), "scopeKey": scope.key, "slots": rows}))
    observed = {"id": scope.group_id("two"), "name": "RG-PRIVATE-TWO", "location": "westus3"}
    monkeypatch.setattr(script, "AzureGroups", lambda *args: SimpleNamespace(show=lambda slot: observed))
    monkeypatch.setattr(sys, "argv", [
        "coordinate-release-fleet.py", "check-ownership", "--root", str(root), "--slot", "two",
        "--ownership", str(ownership), "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest(),
    ])
    assert script.main() == 0
    assert (root / "host-environment").read_text() == "FLEET_LOCATION=westus3\n"
    assert "rg-private" not in capsys.readouterr().out
    observed["name"] = "rg-private-one"
    (root / "host-environment").unlink()
    assert script.main() == 1
    assert not (root / "host-environment").exists()


@pytest.mark.parametrize(("secret", "expected", "code"), [
    ("", "ephemeral", 0), ("rg-private-marker", "persistent", 0), ("", "persistent", 1),
    ("rg-private-marker", "ephemeral", 1),
])
def test_site_scope_refuses_a_mode_other_than_the_scheduled_one(inputs, monkeypatch, capsys, secret, expected, code):
    root, value, _ = inputs
    script = load_script("coordinate-release-fleet")
    site_environment(monkeypatch, root, value, E2E_SITE_RESOURCE_GROUP=secret)
    monkeypatch.setattr(sys, "argv", ["coordinate-release-fleet.py", "scope", "--root", str(root), "--kind", "site",
                                      "--slot", "enabled", "--expect-groups", expected])
    assert script.main() == code
    output = capsys.readouterr()
    if code:
        assert not (root / "environment").exists()
    else:
        environment = (root / "environment").read_text()
        assert (f"FLEET_RESOURCE_GROUP={secret}\n" in environment) is bool(secret)
        assert "FLEET_VAULT_NAME" not in environment
        assert "::add-mask::" + (secret or "rg-siteops-site-") in output.out

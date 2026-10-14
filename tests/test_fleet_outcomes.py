"""Reject success-shaped fleet counts without exact targets, outcomes and cleanup."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fleet_process import FleetProcessError  # noqa: E402
from fleet_workflow import FleetCandidate  # noqa: E402
from release_fleet import FleetScope  # noqa: E402


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def deployment_run():
    statuses = {
        "global-edge-site": "skipped", "edge-site": "skipped", "schema-registry": "succeeded",
        "adr-ns": "succeeded", "aio-enablement": "succeeded", "aio-instance": "succeeded",
        "schema-registry-role": "succeeded", "resolve-aio": "skipped", "secretsync": "skipped",
    }
    return {
        "apiVersion": "siteops/v1alpha1", "kind": "DeploymentRun", "projection": "local-private",
        "status": "succeeded", "exitCode": 0, "engine": {"version": "1.0.0b1"},
        "summary": {"interrupted": False, "sites": {"total": 2, "counts": {"succeeded": 2}},
                    "operations": {"total": 18, "counts": {"succeeded": 10, "skipped": 8, "failed": 0}}},
        "sites": [{
            "target": f"fleet-{slot}", "status": "succeeded",
            "operations": [{"identity": {"target": f"fleet-{slot}", "step": name}, "status": status}
                           for name, status in statuses.items()],
        } for slot in ("one", "two")],
    }


@pytest.mark.parametrize("fault", [None, "third-target", "wrong-target", "missing-operation", "wrong-operation", "failed-site", "summary", "version"])
def test_execution_checks_the_actual_selected_targets_and_operations(fault):
    helper = module("deploy-release-fleet")
    result = deployment_run()
    if fault == "third-target":
        result["sites"].append(copy.deepcopy(result["sites"][0]))
    elif fault == "wrong-target":
        result["sites"][0]["target"] = "sentinel"
    elif fault == "missing-operation":
        result["sites"][0]["operations"].pop()
    elif fault == "wrong-operation":
        result["sites"][0]["operations"][0]["identity"]["step"] = "other"
    elif fault == "failed-site":
        result["sites"][0]["status"] = "failed"
    elif fault == "summary":
        result["summary"]["operations"]["counts"]["skipped"] = 0
    elif fault == "version":
        result["engine"]["version"] = "other"
    if fault:
        with pytest.raises(helper.FleetRunError):
            helper.check_run(result, "1.0.0b1")
    else:
        helper.check_run(result, "1.0.0b1")


def test_extension_observation_checks_azure_state_not_the_echoed_site_property(tmp_path):
    helper = module("deploy-release-fleet")
    scope = FleetScope("example/repository", "a" * 40, "b" * 64, "c" * 64, 42, 1,
                       "00000000-0000-0000-0000-000000000001")
    slots = {"one": {"version": "1.4.41"}, "two": {"version": "1.4.73"}}
    calls = []

    def runner(args):
        calls.append(args)
        slot = "one" if scope.group("one") in args else "two"
        return 0, json.dumps([{
            "extensionType": "microsoft.iotoperations", "version": slots[slot]["version"],
            "provisioningState": "Succeeded",
        }]).encode(), b""

    helper.observe_versions(scope, slots, tmp_path, runner=runner)
    assert len(calls) == 2 and all("--subscription" in args for args in calls)
    with pytest.raises(helper.FleetRunError, match="differs"):
        helper.observe_versions(scope, slots, tmp_path, runner=lambda _: (
            0, b'[{"extensionType":"microsoft.iotoperations","version":"0.0.0","provisioningState":"Succeeded"}]', b"",
        ))


@pytest.mark.parametrize("fault", [None, "empty", "pending", "unready", "no-instance", "duplicate-instance"])
def test_host_readiness_requires_real_observations(fault):
    helper = module("observe-fleet-host")
    pod = {"metadata": {}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
    pods, instances = {"items": [pod]}, {"items": [{"metadata": {"name": "private-instance"}}]}
    if fault == "empty":
        pods["items"] = []
    elif fault == "pending":
        pod["status"]["phase"] = "Pending"
    elif fault == "unready":
        pod["status"]["conditions"][0]["status"] = "False"
    elif fault == "no-instance":
        instances["items"] = []
    elif fault == "duplicate-instance":
        instances["items"].append({})
    assert helper.ready(pods, instances) is (fault is None)


@pytest.fixture
def receipts(tmp_path):
    source = {"repository": "example/repository", "commit": "a" * 40, "ref": "refs/heads/main"}
    raw = {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate", "source": source,
        "producer": {"run": 40, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False},
        "artifacts": {name: {"id": index, "sha256": "b" * 64}
                      for index, name in enumerate(("admission", "plan", "inventory", "engine", "workspaces"), 11)},
    }
    selected = FleetCandidate.parse(json.dumps(raw).encode(), repository=source["repository"],
                                   commit=source["commit"], ref=source["ref"])
    common = {
        "apiVersion": "siteops.release.fleet/v1", "scopeKey": "c" * 64,
        "context": {"repository": source["repository"], "sourceCommit": source["commit"],
                    "admissionSha256": "b" * 64, "inventorySha256": "b" * 64, "run": 50, "attempt": 1},
    }
    data = {
        "deployment/deployment.json": {
            **copy.deepcopy(common), "kind": "FleetDeployment", "status": "succeeded", "engineVersion": "1.0.0b1",
            "targets": 2, "parallel": 2, "deployInvocations": 1, "sentinelExcluded": True,
            "resolvedParametersVerified": True, "extensionVersionsVerified": True,
            "podReadiness": "pending-host-observation", "workloadFunctionality": "not-checked",
            "slots": {"one": {"release": "2607", "version": "1.4.41", "apiVersion": "2026-07-01"},
                      "two": {"release": "2608", "version": "1.4.73", "apiVersion": "2026-07-01"}},
        },
        "cleanup/cleanup.json": {
            **copy.deepcopy(common), "kind": "FleetCleanup", "groups": "ephemeral", "status": "complete",
            "operationExit": 0,
            "slots": {name: {"state": "absent", "reason": "confirmed-absent"} for name in ("one", "two")},
        },
        **{f"readiness/fleet-readiness-50-1-{slot}/readiness.json": {
            **copy.deepcopy(common), "kind": "FleetHostReadiness", "slot": slot, "status": "ready",
            "checks": ["instance-present", "active-pods-ready"], "workloadFunctionality": "not-checked",
        } for slot in ("one", "two")},
    }
    for name, value in data.items():
        path = tmp_path.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    return selected, tmp_path, data


@pytest.mark.parametrize("fault", [None, "persistent", "missing", "duplicate", "candidate", "scope", "cleanup",
                                   "invocations", "private-field", "groups"])
def test_aggregator_rejects_missing_ambiguous_or_unbound_evidence(receipts, fault):
    selected, root, data = receipts
    helper = module("aggregate-fleet-results")
    name = "deployment/deployment.json"
    if fault == "missing":
        (root / "readiness" / "fleet-readiness-50-1-two" / "readiness.json").unlink()
    elif fault == "duplicate":
        (root / "readiness" / "unexpected").mkdir()
    elif fault == "candidate":
        data[name]["context"]["sourceCommit"] = "d" * 40
    elif fault == "scope":
        data[name]["scopeKey"] = "d" * 64
    elif fault == "cleanup":
        name = "cleanup/cleanup.json"
        data[name]["slots"]["one"]["state"] = "unknown"
    elif fault == "invocations":
        data[name]["deployInvocations"] = 2
    elif fault == "private-field":
        data[name]["resourceGroup"] = "private-marker"
    elif fault in {"persistent", "groups"}:
        name = "cleanup/cleanup.json"
        data[name]["groups"] = "persistent" if fault == "persistent" else "private-marker"
    if fault not in {"missing", "duplicate"}:
        root.joinpath(*name.split("/")).write_text(json.dumps(data[name]))
    if fault and fault != "persistent":
        with pytest.raises((ValueError, OSError)):
            helper.aggregate(selected, root, run=50, attempt=1)
    else:
        report = helper.aggregate(selected, root, run=50, attempt=1)
        assert report["status"] == "passed" and report["cleanup"] == "confirmed-absent"
        assert report["workloadFunctionality"] == "not-checked"
        assert report["groups"] == ("persistent" if fault else "ephemeral")


def test_release_acceptance_consumes_the_exact_fleet_acceptance_receipt(receipts):
    from release_acceptance import ASSERTIONS, check_fleet

    selected, root, _ = receipts
    report = module("aggregate-fleet-results").aggregate(selected, root, run=50, attempt=1)
    row = check_fleet(json.loads(json.dumps(report)), report["context"])
    assert row["status"] == "passed" and row["assertions"] == list(ASSERTIONS["fleet"])
    assert check_fleet({**report, "targetCount": 1}, report["context"])["status"] == "failed"
    assert check_fleet(report, {**report["context"], "attempt": 2})["status"] == "ambiguous"


@pytest.mark.parametrize(("failure", "expected"), [
    ("none", 0), ("command", 23), ("shutdown-timeout", 124), ("shutdown-output", 125),
    ("ownership", 1),
])
def test_controller_saves_two_sites_and_runs_exactly_one_deployment(
    tmp_path, monkeypatch, capsys, failure, expected,
):
    helper = module("deploy-release-fleet")
    inputs, state = tmp_path / "inputs", tmp_path / "state"
    (inputs / "admission").mkdir(parents=True)
    project = state / "probe-state" / "operator"
    (project / "sites").mkdir(parents=True)
    pin = project / "siteops.pin"
    pin.write_text('{"fixture":"qualified-pin"}')
    qualification = tmp_path / "qualification.json"
    qualification.write_text(json.dumps({
        "kind": "WorkspaceEngineQualification", "engineVersion": "1.0.0b1",
        "engineSelectionSha256": "b" * 64, "workspaceInventorySha256": "b" * 64, "planSha256": "b" * 64,
        "project": {
            "workspace": "workspaces/iot-operations",
            "sourceReleaseObservation": "not-performed",
            "pinSha256": hashlib.sha256(pin.read_bytes()).hexdigest(),
        },
    }))
    source = {"repository": "example/repository", "commit": "a" * 40, "ref": "refs/heads/main"}
    producer = {"run": 40, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False}
    artifact_values = {name: {"id": index, "sha256": "b" * 64}
                       for index, name in enumerate(("admission", "plan", "inventory", "engine", "workspaces"), 11)}
    admission = {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission",
        "source": source, **producer, "artifacts": {
            "plan": artifact_values["plan"]["id"], "inventory": artifact_values["inventory"]["id"], "payload": 99,
        }, "planSha256": "b" * 64, "inventorySha256": "b" * 64, "subjects": {"engine": 4, "workspace": 1},
        "status": "admitted", "installation": "not-run", "deployment": "not-run",
    }
    admission_raw = json.dumps(admission).encode()
    (inputs / "admission" / "receipt.json").write_bytes(admission_raw)
    artifact_values["admission"]["sha256"] = hashlib.sha256(admission_raw).hexdigest()
    selection = {"apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate",
                 "source": source, "producer": producer, "artifacts": artifact_values}
    subscription = "00000000-0000-0000-0000-000000000001"
    scope = FleetScope(source["repository"], source["commit"], artifact_values["admission"]["sha256"],
                       "b" * 64, 50, 1, subscription)
    owners = {"one": "siteops-fleet-" + "1" * 64, "two": "siteops-fleet-" + "2" * 64}
    ownership = tmp_path / "ownership.json"
    ownership.write_text(json.dumps({
        "apiVersion": "siteops.release.fleet/v1", "kind": "FleetOwnership",
        "context": scope.context(), "scopeKey": scope.key,
        "slots": {slot: {"admittedAbsent": True, "ownerSha256": hashlib.sha256(owner.encode()).hexdigest()}
                  for slot, owner in owners.items()},
    }))
    environment = {
        "FLEET_CANDIDATE": json.dumps(selection), "GITHUB_REPOSITORY": source["repository"],
        "GITHUB_SHA": source["commit"], "GITHUB_REF": source["ref"], "GITHUB_RUN_ID": "50",
        "GITHUB_RUN_ATTEMPT": "1", "AZURE_SUBSCRIPTION_ID": subscription,
        "PYTHONPATH": "untrusted-source-path", "PYTHONHOME": "untrusted-python-home",
    }
    monkeypatch.setattr(helper, "os", SimpleNamespace(
        name="posix", environ=environment, open=os.open, fdopen=os.fdopen,
        O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL,
    ))

    class Groups:
        def __init__(self, selected, logs):
            assert selected == scope

        def show(self, slot):
            name = scope.group(slot)
            return {"id": f"/subscriptions/{subscription}/resourceGroups/{name}",
                    "name": name, "tags": scope.tags(slot),
                    "managedBy": "another-manager" if failure == "ownership" else owners[slot]}

    monkeypatch.setattr(helper, "AzureGroups", Groups)
    monkeypatch.setenv("FLEET_STARTED_AT", str(int(time.time())))

    def verifier(directory, roots, source, **policy):
        assert source == selection["source"]
        assert roots == state / "workspace-policy" / "root.json"
        assert policy == {
            "signer": ".github/workflows/_workspace-distribution.yaml",
            "builder": ".github/workflows/release.yaml", "runner_environment": "self-hosted",
        }
        return SimpleNamespace(policy_file=directory / "policy.json", root=directory / "root.json")

    monkeypatch.setattr(helper, "ReleaseVerifier", verifier)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("A real process escaped controller isolation."))
    calls = []

    def run(arguments, *, cwd, logs, name, timeout, environment):
        calls.append((name, arguments))
        assert cwd == project
        assert "PYTHONPATH" not in environment and "PYTHONHOME" not in environment
        if name.startswith("save-"):
            answers = json.loads(Path(arguments[arguments.index("--input-file") + 1]).read_text())["values"]
            assert answers["enableSecretSync"] is False and "existingVault" not in answers
            assert answers["aioRelease"] == ("2607" if name == "save-one" else "2608")
            Path(arguments[arguments.index("--save-site") + 1]).write_text(json.dumps(answers))
        elif name == "inspect-plan":
            spec = json.loads(Path(arguments[arguments.index("--spec") + 1]).read_text())
            assert {row["name"] for row in spec["targets"]} == {"fleet-one", "fleet-two"}
            assert (project / "sites" / "fleet-unselected.yaml").is_file()
            assert environment["SITEOPS_REDACT_OUTPUT"] == "0"
            (logs / "inspect-plan.out").write_text(json.dumps({
                "kind": "FleetPlanInspection", "operationIdentitiesVerified": True, "parametersVerified": True,
                "slots": {"one": {"release": "2607", "version": "1.4.41", "apiVersion": "2026-07-01"},
                          "two": {"release": "2608", "version": "1.4.73", "apiVersion": "2026-07-01"}},
            }))
        else:
            assert name == "deploy" and arguments.count("deploy") == 1
            assert arguments[arguments.index("-l") + 1] == "name=fleet-one,name=fleet-two"
            assert arguments[arguments.index("--parallel") + 1] == "2"
            assert "--yes" in arguments and "--offline-content" in arguments
            assert environment["SITEOPS_REDACT_OUTPUT"] == "0"
            (logs / "deploy.out").write_text(json.dumps(deployment_run()))
            if failure.startswith("shutdown-"):
                raise FleetProcessError(expected)
            if failure == "command":
                return 23
        return 0

    observed = []
    monkeypatch.setattr(helper, "run", run)
    monkeypatch.setattr(helper, "observe_versions", lambda *args, **kwargs: observed.append(True))
    output = tmp_path / "deployment.json"
    monkeypatch.setattr(sys, "argv", [
        "deploy-release-fleet.py", "--inputs", str(inputs), "--state", str(state),
        "--qualification", str(qualification), "--ownership", str(ownership),
        "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest(),
        "--output", str(output),
    ])
    assert helper.main() == expected
    assert [name for name, _ in calls] == (
        [] if failure == "ownership" else ["save-one", "save-two", "inspect-plan", "deploy"]
    )
    assert bool(observed) is (failure == "none")
    report = json.loads(output.read_text())
    assert report["status"] == ("succeeded" if failure == "none" else "failed")
    if failure != "none":
        assert report["operationExit"] == expected
    captured = capsys.readouterr()
    assert subscription not in captured.out + captured.err + output.read_text()
    if failure.startswith("shutdown-"):
        assert "cleanup deadline" in captured.err


@pytest.mark.parametrize(("outcome", "expected"), [
    ("ready", 0), ("deadline", 1), ("shutdown-timeout", 124), ("shutdown-output", 125),
])
def test_host_observation_obeys_its_deadline_and_retains_only_latest_private_logs(
    receipts, monkeypatch, capsys, outcome, expected,
):
    selected, root, _ = receipts
    helper = module("observe-fleet-host")
    for key, value in {
        "FLEET_CANDIDATE": json.dumps(selected.document()), "GITHUB_REPOSITORY": selected.source["repository"],
        "GITHUB_SHA": selected.source["commit"], "GITHUB_REF": selected.source["ref"],
        "GITHUB_RUN_ID": "50", "GITHUB_RUN_ATTEMPT": "1",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
    }.items():
        monkeypatch.setenv(key, value)
    scope = SimpleNamespace(context=lambda: {}, key="a" * 64)
    monkeypatch.setattr(helper, "scope_for", lambda *args, **kwargs: scope)
    monkeypatch.setattr(helper, "FleetBudget", SimpleNamespace(
        from_environment=lambda: SimpleNamespace(remaining=lambda *args: 120),
    ))
    elapsed = [0]
    sleeps = []

    def sleep(duration):
        sleeps.append(duration)
        elapsed[0] += duration

    monkeypatch.setattr(helper, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unexpected live pod query."))
    calls = []

    def run(arguments, *, cwd, logs, name, timeout):
        calls.append(arguments)
        assert 0 < timeout <= 30
        if outcome.startswith("shutdown-"):
            raise FleetProcessError(expected)
        if outcome == "deadline":
            elapsed[0] = 121
        if arguments[2] == "pods":
            value = {"items": [{
                "metadata": {"name": "private-pod"},
                "status": {"phase": "Running", "conditions": [
                    {"type": "Ready", "status": "True" if elapsed[0] else "False"},
                ]},
            }]}
        else:
            value = {"items": [{"metadata": {"name": "private-instance"}}]}
        (logs / f"{name}.out").write_text(json.dumps(value))
        (logs / f"{name}.err").write_text("private-diagnostic")
        return 0

    monkeypatch.setattr(helper, "run", run)
    logs, output = root / "host-private", root / "host-result.json"
    monkeypatch.setattr(sys, "argv", [
        "observe-fleet-host.py", "--inputs", str(root), "--slot", "one",
        "--logs", str(logs), "--output", str(output),
    ])
    assert helper.main() == expected
    if outcome != "ready":
        assert len(calls) == 1 and not output.exists()
        assert not sleeps
    else:
        assert len(calls) == 4 and sleeps == [15]
        assert len(list(logs.iterdir())) == 4
        assert all(path.name.startswith("2-") for path in logs.iterdir())
        assert json.loads(output.read_text())["status"] == "ready"
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err
    if outcome.startswith("shutdown-"):
        assert "cleanup deadline" in captured.err

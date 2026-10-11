"""Site and Fleet acceptance require all active AIO pods to be ready."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aio_readiness import PodReadiness, active_pod_readiness  # noqa: E402


def helper(name):
    return SimpleNamespace(**runpy.run_path(str(ROOT / "scripts" / f"{name}.py")))


def pod(phase="Running", *, ready="True", **metadata):
    return {"metadata": {"name": "private-pod", **metadata},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready}]}}


@pytest.mark.parametrize(("case", "counts", "expected"), [
    pytest.param("all-ready", (2, 2, 2), True, id="all-ready"),
    pytest.param("crash-loop", (2, 2, 1), False, id="crash-loop"),
    pytest.param("completed-job", (2, 1, 1), True, id="completed-job"),
    pytest.param("pending", (2, 2, 1), False, id="pending"),
    pytest.param("no-conditions", (2, 2, 1), False, id="no-conditions"),
    pytest.param("empty-conditions", (2, 2, 1), False, id="empty-conditions"),
    pytest.param("deleting", (2, 2, 1), False, id="deleting"),
    pytest.param("no-active", (1, 0, 0), False, id="no-active"),
    pytest.param("empty", (0, 0, 0), False, id="empty"),
])
def test_active_pod_decision_table(case, counts, expected):
    pods = [pod(), pod()]
    if case == "crash-loop":
        pods[0]["status"].update(conditions=[{"type": "Ready", "status": "False"}],
                                 containerStatuses=[{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}])
    elif case in {"completed-job", "no-active"}:
        pods[0] = {"status": {"phase": "Succeeded"}}
        if case == "no-active":
            pods.pop()
    elif case == "pending":
        pods[0]["status"]["phase"] = "Pending"
    elif case == "no-conditions":
        del pods[0]["status"]["conditions"]
    elif case == "empty-conditions":
        pods[0]["status"]["conditions"] = []
    elif case == "deleting":
        pods[0]["metadata"]["deletionTimestamp"] = "2026-10-10T00:00:00Z"
    elif case == "empty":
        pods.clear()
    observed = active_pod_readiness({"items": pods})
    assert (observed.namespace_pods, observed.active_pods, observed.ready_active_pods) == counts
    assert observed.ready is expected


@pytest.mark.parametrize("observation", [
    None, [], {}, {"items": None}, {"items": {}}, {"items": [None]},
    {"items": [{}]}, {"items": [{"status": None}]}, {"items": [{"status": []}]},
    {"items": [{"status": {"phase": "Running"}, "metadata": []}]},
    {"items": [{"status": {"phase": "Running", "conditions": {}}}]},
    {"items": [{"status": {"phase": "Running", "conditions": [None]}}]},
])
def test_active_pod_rule_rejects_invalid_shapes(observation):
    with pytest.raises(ValueError):
        active_pod_readiness(observation)


def test_site_step_and_fleet_observer_call_the_shared_rule(monkeypatch):
    site, fleet = helper("observe-site-readiness"), helper("observe-fleet-host")
    assert site.active_pod_readiness is fleet.active_pod_readiness is active_pod_readiness
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "e2e-test.yaml").read_text())
    step = next(step for step in workflow["jobs"]["e2e"]["steps"]
                if step.get("name") == "Observe bounded AIO readiness")
    assert "python scripts/observe-site-readiness.py" in step["run"]
    assert 'condition.get("type")' not in step["run"]
    assert step["timeout-minutes"] > 15
    calls = []

    def shared(pods):
        calls.append(pods)
        return PodReadiness(2, 2, 2)

    monkeypatch.setitem(site.observe.__globals__, "active_pod_readiness", shared)
    monkeypatch.setitem(fleet.ready.__globals__, "active_pod_readiness", shared)
    pods = {"items": [pod(), pod()]}
    instances = {"items": [{"metadata": {"name": "private-instance"}}]}
    assert fleet.ready(pods, instances)
    assert site.observe(query=lambda kind, timeout: pods if kind == "pods" else instances)["activePods"] == 2
    assert calls == [pods, pods]


@pytest.mark.parametrize("instances", [[], [{"metadata": {"name": "one"}}, {"metadata": {"name": "two"}}]])
def test_fleet_still_requires_exactly_one_instance(instances):
    fleet = helper("observe-fleet-host")
    assert fleet.ready({"items": [pod()]}, {"items": instances}) is False


def test_site_observation_evaluates_before_sleep_and_accepts_multiple_instances():
    site = helper("observe-site-readiness")
    elapsed = [0]
    sleeps, calls = [], []
    pods = {"items": [pod(ready="False"), pod()]}
    instances = {"items": [{"metadata": {"name": name}} for name in ("one", "two")]}

    def query(kind, timeout):
        calls.append((kind, timeout))
        if kind == "pods":
            return copy.deepcopy(pods)
        return instances

    def sleep(duration):
        sleeps.append(duration)
        elapsed[0] += duration
        pods["items"][0] = pod()

    counts = site.observe(query=query, clock=lambda: elapsed[0], sleep=sleep)
    assert counts == {"namespacePods": 2, "activePods": 2, "readyActivePods": 2, "instanceCustomResources": 2}
    assert sleeps == [10] and len(calls) == 4
    assert all(0 < timeout <= 30 for _, timeout in calls)


def test_site_timeout_has_only_allowlisted_counts(capsys):
    site = helper("observe-site-readiness")
    assert site.TIMEOUT_MESSAGE == (
        "::error::AIO instance projection and active pod readiness did not converge within fifteen minutes."
    )
    elapsed = [0]
    sleeps = []
    pods = {"items": [pod(ready="False"), pod()]}
    pods["items"][0]["status"]["containerStatuses"] = [
        {"name": "private-container", "state": {"waiting": {"reason": reason, "message": "private-message"}}}
        for reason in ("CrashLoopBackOff", "private-reason")
    ]
    pods["items"][0]["status"]["initContainerStatuses"] = [
        {"state": {"waiting": {"reason": "ContainerCreating"}}},
    ]
    pods["items"].append(pod("private-phase"))
    instances = {"items": [{"metadata": {"name": "private-instance"}}]}
    calls = []

    def query(kind, timeout):
        calls.append((kind, timeout))
        return pods if kind == "pods" else instances

    def sleep(duration):
        sleeps.append(duration)
        elapsed[0] += duration

    with pytest.raises(TimeoutError):
        site.observe(query=query, clock=lambda: elapsed[0], sleep=sleep)
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        site.TIMEOUT_MESSAGE,
        json.dumps({"podPhases": {"Running": 2, "Other": 1},
                    "containerWaitingReasons": {"CrashLoopBackOff": 1, "Other": 1, "ContainerCreating": 1}},
                   sort_keys=True),
    ]
    assert captured.out == "" and "private-" not in captured.err
    assert sum(sleeps) == 900 and all(duration == 10 for duration in sleeps)
    assert len(calls) == 180 and all(0 < timeout <= 30 for _, timeout in calls)


def test_site_observation_does_not_accept_readiness_after_deadline(capsys):
    site = helper("observe-site-readiness")
    elapsed = [0]

    def query(kind, timeout):
        if kind == "pods":
            return {"items": [pod()]}
        elapsed[0] = 900
        return {"items": [{"metadata": {"name": "private-instance"}}]}

    with pytest.raises(TimeoutError):
        site.observe(query=query, clock=lambda: elapsed[0], sleep=lambda _: pytest.fail("Unexpected sleep."))
    assert site.TIMEOUT_MESSAGE in capsys.readouterr().err


@pytest.mark.parametrize("fault", [None, "command", "timeout", "invalid-json", "oversized"])
def test_site_query_captures_private_output_and_bounds_commands(monkeypatch, capsys, fault):
    site = helper("observe-site-readiness")

    def run(arguments, **kwargs):
        assert arguments == [
            "kubectl", "get", "pods", "--namespace", "azure-iot-operations", "--output", "json",
        ]
        assert kwargs == {"capture_output": True, "timeout": 17, "check": False}
        if fault == "timeout":
            raise subprocess.TimeoutExpired(arguments, 17, stderr=b"private-error")
        stdout = b'{"items":[]}'
        if fault == "invalid-json":
            stdout = b"private-invalid-json"
        elif fault == "oversized":
            stdout = b"x" * (2 * 1024 * 1024 + 1)
        return subprocess.CompletedProcess(arguments, int(fault == "command"), stdout, b"private-error")

    monkeypatch.setattr(subprocess, "run", run)
    if fault in {"invalid-json", "oversized"}:
        with pytest.raises(ValueError):
            site.query("pods", 17)
    else:
        assert site.query("pods", 17) == (None if fault else {"items": []})
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("observation", [None, {}, {"items": {}}, {"items": [None]}, {"items": [{}]}])
def test_site_instance_observation_rejects_invalid_shapes(observation):
    site = helper("observe-site-readiness")
    with pytest.raises(ValueError):
        site.instance_count(observation)


def test_site_entrypoint_writes_only_ready_counts(tmp_path, monkeypatch, capsys):
    site = helper("observe-site-readiness")
    output = tmp_path / "counts.json"

    def run(arguments, **kwargs):
        document = {"items": [pod()]} if arguments[2] == "pods" else {
            "items": [{"metadata": {"name": "private-instance"}}],
        }
        return subprocess.CompletedProcess(arguments, 0, json.dumps(document).encode(), b"private-error")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["observe-site-readiness.py", "--output", str(output)])
    assert site.main() == 0
    assert json.loads(output.read_text()) == {
        "namespacePods": 1, "activePods": 1, "readyActivePods": 1, "instanceCustomResources": 1,
    }
    assert "private-" not in output.read_text() + capsys.readouterr().out


@pytest.mark.parametrize("fault", ["crash-loop", "invalid-json", "invalid-pods"])
def test_site_entrypoint_leaves_no_receipt_when_observation_fails(tmp_path, monkeypatch, capsys, fault):
    site = helper("observe-site-readiness")
    output = tmp_path / "counts.json"
    elapsed = [0]

    def run(arguments, **kwargs):
        document = {"items": [pod(ready="False"), pod()]} if arguments[2] == "pods" else {
            "items": [{"metadata": {"name": "private-instance"}}],
        }
        if fault == "invalid-pods" and arguments[2] == "pods":
            document = {"items": [{"metadata": {"name": "private-pod"}, "status": []}]}
        stdout = b"private-invalid-json" if fault == "invalid-json" else json.dumps(document).encode()
        return subprocess.CompletedProcess(arguments, 0, stdout, b"private-error")

    def sleep(duration):
        elapsed[0] += duration

    observe = site.observe
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setitem(site.main.__globals__, "observe", lambda: observe(
        query=site.query, clock=lambda: elapsed[0], sleep=sleep,
    ))
    monkeypatch.setattr(sys, "argv", ["observe-site-readiness.py", "--output", str(output)])
    assert site.main() == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert captured.out == "" and "private-" not in captured.err
    if fault == "crash-loop":
        assert site.TIMEOUT_MESSAGE in captured.err
    else:
        assert captured.err == "::error::AIO readiness observation is invalid or unavailable.\n"

"""Provider completion and cooperative stopping use the real execution boundary."""

import json
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from siteops.executor import (
    DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS,
    ENGINE_TIMEOUT_SENTINEL,
    AzCliExecutor,
    DeploymentResult,
    KubectlResult,
    UnconfirmedCompletion,
    WaitResult,
    WaitState,
    _probe_arc_proxy_ready,
    _wait_or_stop,
)
from siteops.models import ArmTagCondition
from siteops.orchestrator import Orchestrator
from siteops.results import OperationStatus, OutcomeReasonCode, RunStatus
from siteops.runtime import RuntimePaths
from tests.test_orchestrator_results import _plan_result


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "subprocess.Popen", MagicMock(side_effect=AssertionError("No real process")),
    )
    executor = AzCliExecutor(
        tmp_path / "workspace",
        runtime_paths=RuntimePaths(temp_root=tmp_path / "scratch"),
    )
    executor.bind_tool_paths(
        azure_cli=tmp_path / "az", kubectl=tmp_path / "kubectl",
    )
    try:
        yield executor
    finally:
        executor.close()


def _deployment(executor, stop, *, parameters=None):
    return executor.deploy_resource_group(
        "subscription", "rg", Path("template.json"), parameters or {},
        "deployment", "step", "site", stop_requested=stop,
    )


@pytest.mark.parametrize(
    ("state", "unconfirmed"),
    [("Succeeded", None), ("Failed", None), ("Canceled", None)],
)
def test_terminal_observation_survives_a_simultaneous_stop(
    executor, monkeypatch, state, unconfirmed,
):
    stop = threading.Event()

    def observe(*args, **kwargs):
        stop.set()
        return True, json.dumps({"properties": {"provisioningState": state}}), ""

    monkeypatch.setattr(executor, "_run_az", observe)
    result = executor._poll_deployment([], [], "deployment", "step", "site", stop)

    assert result.success is (state == "Succeeded")
    assert result.unconfirmed is unconfirmed
    assert not result.stopped_before_start


@pytest.mark.parametrize(
    ("observation", "setting", "unconfirmed"),
    [
        ((False, "", "ResourceNotFound"), "DEPLOYMENT_NOTFOUND_GRACE_SECONDS",
         UnconfirmedCompletion.NEVER_VISIBLE),
        ((False, "", "AuthorizationFailed"), "DEPLOYMENT_OBSERVATION_GRACE_SECONDS",
         UnconfirmedCompletion.OBSERVATION_LOST),
        ((True, '{"properties":{"provisioningState":"Running"}}', ""),
         "DEFAULT_AZ_TIMEOUT_SECONDS", UnconfirmedCompletion.DEADLINE_EXCEEDED),
    ],
)
def test_observation_deadlines_do_not_claim_remote_failure(
    executor, monkeypatch, observation, setting, unconfirmed,
):
    monkeypatch.setattr(f"siteops.executor.{setting}", -1)
    monkeypatch.setattr(executor, "_run_az", lambda *a, **k: observation)

    result = executor._poll_deployment([], [], "deployment", "step", "site")

    assert not result.success
    assert result.unconfirmed is unconfirmed
    assert result.deployment_name == "deployment"


@pytest.mark.parametrize(
    ("message", "unconfirmed"),
    [("AuthorizationFailed", None),
     ("unrecognized client failure", UnconfirmedCompletion.SUBMIT_UNCLASSIFIED)],
)
def test_submit_rejection_is_distinct_from_unclassified_failure(
    executor, monkeypatch, message, unconfirmed,
):
    monkeypatch.setattr(executor, "_run_az", lambda *a, **k: (False, "", message))

    proceed, result = executor._submit_deployment([], "deployment", "step", "site")

    assert not proceed
    assert result.unconfirmed is unconfirmed
    assert not result.success


@pytest.mark.parametrize(
    ("message", "unconfirmed"),
    [
        ("AuthorizationFailed", None),
        ("InvalidTemplate", None),
        ("ResourceNotFound", None),
        ("unrecognized client failure", UnconfirmedCompletion.STOPPED_OBSERVING),
        ("ServiceUnavailable", UnconfirmedCompletion.STOPPED_OBSERVING),
        (
            ENGINE_TIMEOUT_SENTINEL.format(
                timeout=DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS,
            ),
            UnconfirmedCompletion.STOPPED_OBSERVING,
        ),
    ],
)
def test_submit_response_survives_a_simultaneous_stop(
    executor, monkeypatch, message, unconfirmed,
):
    stop = threading.Event()

    def submit(*args, **kwargs):
        stop.set()
        return False, "", message

    call = MagicMock(side_effect=submit)
    monkeypatch.setattr(executor, "_run_az", call)
    proceed, result = executor._submit_deployment(
        [], "deployment", "step", "site", stop,
    )

    assert not proceed
    assert not result.success
    assert not result.stopped_before_start
    assert result.unconfirmed is unconfirmed
    if unconfirmed is None:
        assert result.error == message
    call.assert_called_once()


@pytest.mark.parametrize("stop_at", ["deploy_resource_group", "_submit_deployment"])
def test_stop_before_provider_call_records_only_cancellation(
    executor, monkeypatch, tmp_path, stop_at,
):
    stop = threading.Event()
    orchestrator = Orchestrator(tmp_path)
    orchestrator.executor = executor
    original = getattr(executor, stop_at)

    def stop_before_call(*args, **kwargs):
        stop.set()
        return original(*args, **kwargs)

    command = MagicMock(side_effect=AssertionError("No provider call after stopping"))
    monkeypatch.setattr(executor, stop_at, stop_before_call)
    monkeypatch.setattr(executor, "_run_az", command)

    result = orchestrator.execute_plan(_plan_result(step_count=2), stop_requested=stop)

    assert result.status is RunStatus.CANCELLED
    assert result.interrupted
    assert result.exit_code == 130
    for operation in result.sites[0].operations:
        assert operation.status is OperationStatus.CANCELLED
        assert operation.reason.code is OutcomeReasonCode.CANCELLED_BEFORE_START
        assert "failed" not in operation.reason.local_message()
        assert "earlier submission" not in operation.reason.local_message()
    command.assert_not_called()


def test_stop_before_submission_allocates_nothing(executor, monkeypatch):
    stop = threading.Event()
    stop.set()
    call = MagicMock(side_effect=AssertionError("Must not submit"))
    monkeypatch.setattr(executor, "_run_az", call)

    result = _deployment(executor, stop, parameters={"secret": "synthetic"})

    assert result.stopped_before_start
    assert result.unconfirmed is None
    assert executor._scratch_dir is None
    call.assert_not_called()


def test_stopping_after_submit_retains_uncertain_completion(executor, monkeypatch):
    stop = threading.Event()

    def submit(*args, **kwargs):
        stop.set()
        return True, "", ""

    call = MagicMock(side_effect=submit)
    monkeypatch.setattr(executor, "_run_az", call)

    result = _deployment(executor, stop, parameters={"value": "synthetic"})

    assert not result.success
    assert result.unconfirmed is UnconfirmedCompletion.STOPPED_OBSERVING
    assert not result.stopped_before_start
    assert list(executor.tmp_dir.iterdir()) == []
    assert call.call_count == 1


def test_stop_wakes_submit_backoff_without_another_attempt(executor, monkeypatch):
    stop = threading.Event()
    call = MagicMock(return_value=(False, "", "ServiceUnavailable"))
    monkeypatch.setattr(executor, "_run_az", call)

    def wait(seconds):
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", wait)
    proceed, result = executor._submit_deployment(
        [], "deployment", "step", "site", stop,
    )

    assert not proceed
    assert result.unconfirmed is UnconfirmedCompletion.STOPPED_OBSERVING
    assert call.call_count == 1


@pytest.mark.parametrize("state", [WaitState.PENDING, WaitState.SATISFIED])
def test_stop_after_wait_observation_preserves_known_success(
    executor, monkeypatch, state,
):
    stop = threading.Event()

    def observe(*args):
        stop.set()
        return state, "ready" if state is WaitState.SATISFIED else "waiting", None

    monkeypatch.setattr(executor, "_evaluate_condition", observe)
    result = executor.wait_for_condition(
        ArmTagCondition(
            type="arm-tag", resource_id="/resource", tag_key="state", expected_value="ready",
        ),
        5, 30, "subscription", "wait", "site", stop_requested=stop,
    )

    assert result.success is (state is WaitState.SATISFIED)
    assert result.unconfirmed is (
        None if state is WaitState.SATISFIED else UnconfirmedCompletion.STOPPED_OBSERVING
    )


def test_cancelled_proxy_probe_never_starts_a_call(executor):
    process = MagicMock()
    stop = threading.Event()
    stop.set()

    assert not _probe_arc_proxy_ready(process, 47021, stop_requested=stop)
    process.poll.assert_not_called()
    assert _wait_or_stop(3600, stop)


def test_failed_kubectl_apply_does_not_claim_no_changes(executor, monkeypatch):
    monkeypatch.setattr(executor, "_arc_proxy", lambda *a, **k: nullcontext("fake-config"))
    monkeypatch.setattr(executor, "_run_kubectl", lambda *a, **k: (False, "", "connection lost"))

    result = executor.kubectl_apply(
        "cluster", "rg", "subscription", ["https://example.invalid/manifest"],
        "apply", "site",
    )

    assert not result.success
    assert result.unconfirmed is UnconfirmedCompletion.APPLY_INCOMPLETE


def test_real_executor_stop_is_recorded_by_typed_core(executor, monkeypatch, tmp_path):
    stop = threading.Event()
    orchestrator = Orchestrator(tmp_path)
    orchestrator.executor = executor
    plan = _plan_result(step_count=2)

    def command(args, **kwargs):
        if "create" in args:
            stop.set()
            return True, "", ""
        raise AssertionError("No new observations after stopping")

    monkeypatch.setattr(executor, "_run_az", command)
    result = orchestrator.execute_plan(plan, stop_requested=stop)

    assert result.interrupted
    assert result.exit_code == 130
    assert result.status is RunStatus.UNKNOWN
    first, second = result.sites[0].operations
    assert first.status is OperationStatus.UNKNOWN
    assert first.deployment_name
    assert second.status is OperationStatus.NOT_RUN
    assert executor._scratch_dir is None


@pytest.mark.parametrize("result_type", [DeploymentResult, KubectlResult, WaitResult])
@pytest.mark.parametrize(
    "flags",
    [
        {"success": True, "unconfirmed": UnconfirmedCompletion.OBSERVATION_LOST},
        {"success": True, "stopped_before_start": True},
        {"success": False, "stopped_before_start": True,
         "unconfirmed": UnconfirmedCompletion.OBSERVATION_LOST},
    ],
)
def test_provider_completion_cannot_contradict_itself(result_type, flags):
    keywords = {"step_name": "step", "site_name": "site", **flags}
    if result_type is DeploymentResult:
        keywords["deployment_name"] = "deployment"
    with pytest.raises(ValueError):
        result_type(**keywords)


@pytest.mark.parametrize("message", ["synthetic internal defect", "", " \t\n"])
def test_unexpected_later_failure_keeps_prior_success(
    executor, monkeypatch, tmp_path, message,
):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.executor = executor
    plan = _plan_result(step_count=3)
    calls = 0

    def deploy(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(message)
        return DeploymentResult(
            True, kwargs["step_name"], kwargs["site_name"], kwargs["deployment_name"],
            outputs={"value": {"type": "String", "value": "retained"}},
        )

    monkeypatch.setattr(executor, "deploy_resource_group", deploy)
    result = orchestrator.execute_plan(plan)

    assert result.exit_code == 1
    assert [op.status for op in result.sites[0].operations] == [
        OperationStatus.SUCCEEDED, OperationStatus.UNKNOWN, OperationStatus.NOT_RUN,
    ]
    assert result.sites[0].operations[0].copy_outputs()["value"]["value"] == "retained"
    assert result.sites[0].operations[1].reason.private_detail == (
        message if message.strip() else "RuntimeError"
    )
    assert calls == 2


def test_parallel_stop_joins_provider_workers_before_cleanup(
    executor, monkeypatch, tmp_path,
):
    orchestrator = Orchestrator(tmp_path)
    orchestrator.executor = executor
    plan = _plan_result(step_count=1, target_count=3, parallel_sites=2)
    stop = threading.Event()
    rendezvous = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    close = executor.close
    closed = []

    def command(args, **kwargs):
        nonlocal active
        if "create" in args:
            return True, "", ""
        with lock:
            active += 1
        try:
            rendezvous.wait(timeout=5)
            stop.set()
            return True, '{"properties":{"provisioningState":"Running"}}', ""
        finally:
            with lock:
                active -= 1

    def safe_close():
        assert active == 0
        closed.append(True)
        close()

    monkeypatch.setattr(executor, "_run_az", command)
    monkeypatch.setattr(executor, "close", safe_close)
    result = orchestrator.execute_plan(plan, stop_requested=stop)

    assert result.interrupted
    assert result.exit_code == 130
    assert [site.target for site in result.sites] == ["site-0", "site-1", "site-2"]
    assert [site.operations[0].status for site in result.sites] == [
        OperationStatus.UNKNOWN, OperationStatus.UNKNOWN, OperationStatus.CANCELLED,
    ]
    assert closed == [True]

"""Waiting progress remains bounded and belongs to the actual execution lifecycle."""

import threading
from concurrent.futures import wait as real_wait
from types import SimpleNamespace

from siteops.orchestrator import Orchestrator, _ProgressOwner, _TargetExecution
from siteops.planning import (
    OperationIdentity,
    OperationKind,
    PlanExecutionMode,
    PreparedTarget,
    TargetKind,
)
from siteops.reporting import TextProgressReporter
from siteops.results import (
    OperationResult,
    OperationStatus,
    ProgressEvent,
    ProgressEventKind,
    SiteResult,
)


def test_waiting_uses_elapsed_time_without_catch_up_spam_or_late_events():
    now = [0.0]
    events = []
    owner = _ProgressOwner(events.append, clock=lambda: now[0])
    operation = OperationIdentity(target="private-target", step="private-step")
    owner.emit(ProgressEvent(kind=ProgressEventKind.OPERATION_STARTED, operation=operation))
    for value in (0, 10, 59):
        now[0] = value
        owner.waiting()
    assert len(events) == 1
    for value in (60, 61, 200, 201):
        now[0] = value
        owner.waiting()
    waiting = [event for event in events if event.kind is ProgressEventKind.OPERATION_WAITING]
    assert [event.elapsed for event in waiting] == [60, 200]
    owner.emit(ProgressEvent(
        kind=ProgressEventKind.OPERATION_FINISHED, operation=operation,
        operation_status=OperationStatus.SUCCEEDED,
    ))
    now[0] = 1000
    owner.waiting()
    assert len(events) == 4


def test_redacted_waiting_does_not_publish_target_or_step_names():
    import io

    stream = io.StringIO()
    reporter = TextProgressReporter(stream, redacted=True)
    reporter(ProgressEvent(
        kind=ProgressEventKind.OPERATION_WAITING,
        operation=OperationIdentity(target="PRIVATE_TARGET", step="PRIVATE_STEP"),
        elapsed=120,
    ))
    output = stream.getvalue()
    assert "120" in output and "waiting" in output
    assert "PRIVATE" not in output and "%" not in output


def test_finished_target_and_observer_failure_stop_waiting(caplog):
    now = [0.0]
    events = []

    def observe(event):
        events.append(event)
        if event.kind is ProgressEventKind.OPERATION_WAITING:
            raise RuntimeError("PRIVATE_OBSERVER_DETAIL")

    owner = _ProgressOwner(observe, clock=lambda: now[0])
    first = OperationIdentity(target="finished", step="one")
    second = OperationIdentity(target="running", step="two")
    for operation in (first, second):
        owner.emit(ProgressEvent(kind=ProgressEventKind.OPERATION_STARTED, operation=operation))
    owner.finish_target("finished")
    now[0] = 60
    owner.waiting()
    now[0] = 120
    owner.waiting()
    waiting = [event for event in events if event.kind is ProgressEventKind.OPERATION_WAITING]
    assert len(waiting) == 1 and waiting[0].operation == second
    assert owner.failure is not None
    assert "Progress reporting failed" in caplog.text
    assert "PRIVATE_OBSERVER_DETAIL" not in caplog.text


def test_sequential_execution_uses_the_bounded_waiting_call_site(tmp_path, monkeypatch):
    now = [0.0]
    events = []
    owner = _ProgressOwner(events.append, clock=lambda: now[0])
    started, finish = threading.Event(), threading.Event()
    target = PreparedTarget(
        name="one", kind=TargetKind.RESOURCE_GROUP, subscription="sub",
        resource_group="rg", location="eastus", operations=(),
    )
    identity = OperationIdentity(target="one", step="controlled")
    orchestrator = Orchestrator(tmp_path)

    def execute(*args, **kwargs):
        owner.emit(ProgressEvent(kind=ProgressEventKind.OPERATION_STARTED, operation=identity))
        started.set()
        assert finish.wait(2), "Sequential work blocked the coordinator's progress loop."
        operation = OperationResult(
            identity=identity, kind=OperationKind.DEPLOYMENT,
            status=OperationStatus.SUCCEEDED, elapsed=65,
        )
        owner.emit(ProgressEvent(
            kind=ProgressEventKind.OPERATION_FINISHED, operation=identity,
            operation_status=OperationStatus.SUCCEEDED,
        ))
        return _TargetExecution(
            SiteResult.from_operations(target="one", kind=TargetKind.RESOURCE_GROUP,
                                       operations=(operation,), elapsed=65),
            {}, False,
        )

    waits = []

    def controlled_wait(futures, *, timeout, return_when):
        assert started.wait(2)
        waits.append(timeout)
        if len(waits) == 1:
            now[0] = 65
            return set(), set(futures)
        finish.set()
        return real_wait(futures, timeout=2, return_when=return_when)

    monkeypatch.setattr(orchestrator, "_execute_prepared_target", execute)
    monkeypatch.setattr("siteops.orchestrator.wait", controlled_wait)
    results, _, interrupted = orchestrator._run_prepared_targets(
        SimpleNamespace(max_parallel_sites=1), [target], "test", {},
        PlanExecutionMode.APPLY, progress=owner,
    )
    assert not interrupted and len(results) == 1
    assert len([event for event in events if event.kind is ProgressEventKind.OPERATION_WAITING]) == 1
    assert all(value <= 1 for value in waits)

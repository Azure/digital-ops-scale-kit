"""Unit tests for `tests.integration.helpers.kube.wait_for_cr_health`.

The health assertion is what makes a live run evidence that a dataflow works
rather than evidence that ARM accepted it, and every defect in it costs a full
end-to-end round trip to observe. Its logic is held here instead.

The contract it implements is the AIO controller's: it aggregates per-instance
reports and patches `status.healthState`, where `status` is one of `Available`,
`Degraded`, `Unavailable`, or `Unknown`. The block is absent until the first
patch, which lags a successful deploy by up to about two minutes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration.helpers import kube


def _cr(status: str | None = None, **fields):
    """A projected resource carrying the given health status, or none at all."""
    if status is None and not fields:
        return {"metadata": {"name": "flow"}, "status": {}}
    health = {"status": status, **fields} if status is not None else dict(fields)
    return {"metadata": {"name": "flow"}, "status": {"healthState": health}}


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    """Advance time quickly so no budget can turn a failure into a hang.

    `wait_for_cr_health` takes its default timeout as a default argument, which
    binds at import and cannot be monkeypatched. A test that forgets to pass a
    small budget would otherwise poll for the real one. Advancing the clock
    instead of shrinking the budget keeps the real constant readable, so the
    tests that assert on its value still see it.
    """
    clock = {"now": 0.0}

    def monotonic():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(kube.time, "monotonic", monotonic)
    monkeypatch.setattr(kube.time, "sleep", lambda _seconds: None)


@pytest.fixture
def no_sleep():
    """Retained for readability at call sites. The clock is already stubbed."""
    return None


# A small explicit budget, paired with the fast clock above. Ten stubbed
# seconds is longer than any test needs to observe a settled status and short
# enough that a timeout path resolves immediately.
_BUDGET = {"timeout": 10, "interval": 0}


def test_cleanup_delete_can_opt_out_of_waiting(monkeypatch):
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(kube.subprocess, "run", run)

    kube.delete_resource("pod", "probe", "ns", wait=False)

    assert "--wait=false" in captured["args"]


def test_cleanup_delete_waits_by_default(monkeypatch):
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(kube.subprocess, "run", run)

    kube.delete_resource("pod", "probe", "ns")

    assert "--wait=false" not in captured["args"]


def test_job_wait_requires_the_complete_condition(monkeypatch):
    captured = {}

    def run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(kube.subprocess, "run", run)
    monkeypatch.setattr(
        kube,
        "kubectl_json",
        lambda args: {"metadata": {"name": "trust"}},
    )

    result = kube.wait_for_job_complete("trust", "ns", timeout=15)

    assert result["metadata"]["name"] == "trust"
    assert "--for=condition=complete" in captured["args"]
    assert "--timeout=15s" in captured["args"]


def test_wait_for_cr_deleted_polls_until_not_found(monkeypatch):
    responses = [
        {"metadata": {"name": "flow"}},
        kube.KubectlError(
            "not found",
            1,
            "Error from server (NotFound): dataflows flow not found",
        ),
    ]

    def read(*_args, **_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(kube, "kubectl_json", read)

    kube.wait_for_cr_deleted(
        "dataflows",
        "flow",
        "ns",
        timeout=10,
        interval=0,
    )

    assert responses == []


def test_wait_for_cr_deleted_reports_a_resource_that_remains(monkeypatch):
    monkeypatch.setattr(
        kube,
        "kubectl_json",
        lambda *_args, **_kwargs: {"metadata": {"name": "flow"}},
    )

    with pytest.raises(kube.KubectlError, match="still exists"):
        kube.wait_for_cr_deleted(
            "dataflows",
            "flow",
            "ns",
            timeout=3,
            interval=0,
        )


def test_available_returns_the_health_block(monkeypatch, no_sleep):
    monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("Available", reasonCode="Ok"))

    health = kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)

    assert health["status"] == "Available"


def test_status_is_compared_case_insensitively(monkeypatch, no_sleep):
    """The controller's own test compares case-insensitively, so this matches it."""
    monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("available"))

    health = kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)

    assert health["status"] == "available"


def test_polls_until_health_becomes_available(monkeypatch, no_sleep):
    """Health lags a deploy, so an early unhealthy reading is not a failure."""
    readings = [_cr(), _cr("Unknown"), _cr("Degraded"), _cr("Available")]
    calls = {"n": 0}

    def _next(*_args, **_kwargs):
        cr = readings[min(calls["n"], len(readings) - 1)]
        calls["n"] += 1
        return cr

    monkeypatch.setattr(kube, "wait_for_cr", _next)

    health = kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)
    assert health["status"] == "Available"
    assert calls["n"] == len(readings), "stopped polling before health settled"


def test_unhealthy_reports_the_reason_and_message(monkeypatch, no_sleep):
    """The reason and message are what name the underlying fault."""
    monkeypatch.setattr(
        kube,
        "wait_for_cr",
        lambda *a, **k: _cr(
            "Unavailable",
            reasonCode="EndpointUnreachable",
            message="could not connect to the configured host",
        ),
    )

    with pytest.raises(AssertionError) as excinfo:
        kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)

    message = str(excinfo.value)
    assert "Unavailable" in message
    assert "EndpointUnreachable" in message
    assert "could not connect to the configured host" in message


def test_health_timeout_appends_allowlisted_runtime_summary(
    monkeypatch,
    no_sleep,
):
    monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr())

    with pytest.raises(AssertionError) as excinfo:
        kube.wait_for_cr_health(
            "dataflows",
            "flow",
            "ns",
            diagnostics=lambda: (
                "Runtime summary: profilePods=0, podPhases=Pending:1."
            ),
            **_BUDGET,
        )

    assert "profilePods=0" in str(excinfo.value)


def test_runtime_summary_contains_counts_without_names_or_messages(monkeypatch):
    private_name = "private-profile-pod"
    private_message = "private node could not schedule private workload"

    def read(args, **_kwargs):
        if args[1] == "pods":
            return {
                "items": [
                    {
                        "metadata": {
                            "name": private_name,
                            "labels": {"profile": "catalog-profile"},
                        },
                        "status": {
                            "phase": "Pending",
                            "conditions": [
                                {"reason": "Unschedulable"},
                            ],
                            "containerStatuses": [
                                {
                                    "state": {
                                        "waiting": {
                                            "reason": "ImagePullBackOff",
                                            "message": private_message,
                                        }
                                    }
                                }
                            ],
                        },
                    }
                ]
            }
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": private_message,
                }
            ]
        }

    monkeypatch.setattr(kube, "kubectl_json", read)

    summary = kube.dataflow_runtime_summary(
        "catalog-profile",
        "namespace",
    )

    assert "profilePods=1" in summary
    assert "Pending:1" in summary
    assert "unschedulablePods=1" in summary
    assert "ImagePullBackOff:1" in summary
    assert "FailedScheduling:1" in summary
    assert private_name not in summary
    assert private_message not in summary


def test_runtime_summary_maps_unrecognized_reasons_to_other(monkeypatch):
    private_reason = "private-profile-name"

    def read(args, **_kwargs):
        if args[1] == "pods":
            return {
                "items": [
                    {
                        "metadata": {},
                        "status": {
                            "phase": private_reason,
                            "containerStatuses": [
                                {
                                    "state": {
                                        "waiting": {
                                            "reason": private_reason,
                                        }
                                    }
                                }
                            ],
                        },
                    }
                ]
            }
        return {
            "items": [
                {
                    "type": "Warning",
                    "reason": private_reason,
                }
            ]
        }

    monkeypatch.setattr(kube, "kubectl_json", read)

    summary = kube.dataflow_runtime_summary(
        "catalog-profile",
        "namespace",
    )

    assert private_reason not in summary
    assert "podPhases=Other:1" in summary
    assert "waitingReasons=Other:1" in summary
    assert "warningEvents=Other:1" in summary


def test_never_reported_is_distinguished_from_unhealthy(monkeypatch, no_sleep):
    """Nothing reported and reported-unhealthy are different faults.

    An absent block means no instance ever reported, which points at the
    resource never running. A present unhealthy block points at what it says.
    """
    monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr())

    with pytest.raises(AssertionError) as excinfo:
        kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)

    assert "never reported a health status" in str(excinfo.value)


def test_a_degraded_resource_is_not_accepted(monkeypatch, no_sleep):
    """`Degraded` means some instances are not working, so it is not success."""
    monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("Degraded"))

    with pytest.raises(AssertionError):
        kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)


class TestHealthBudget:
    """The budget is sized for a resource reporting after its own creation.

    Each deployment creates its own resources, and health is written a cycle or
    two after creation rather than with it, so a later deployment cannot borrow
    an earlier one's propagation.
    """

    def test_the_budget_covers_propagation(self):
        """Propagation takes about two minutes, so a shorter budget would flake."""
        assert kube._HEALTH_TIMEOUT >= 240

    def test_the_budget_applies_to_every_call(self, monkeypatch, no_sleep):
        """No call is shortened by what an earlier one observed."""
        monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("Available"))
        kube.wait_for_cr_health("dataflows", "first", "ns", **_BUDGET)

        monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr())
        with pytest.raises(AssertionError) as excinfo:
            kube.wait_for_cr_health("dataflows", "second", "ns", **_BUDGET)
        assert f"within {_BUDGET['timeout']}s" in str(excinfo.value)

    def test_the_read_inside_the_loop_is_short(self, monkeypatch, no_sleep):
        """A long read would spend the whole loop budget on a single attempt.

        Bounded against the poll interval rather than the total budget. A read
        just under the total satisfies "shorter than the budget" while still
        consuming all of it before the second attempt.
        """
        captured = {}

        def _capture(_type, _name, _ns, **kwargs):
            captured.update(kwargs)
            return _cr("Available")

        monkeypatch.setattr(kube, "wait_for_cr", _capture)
        kube.wait_for_cr_health("dataflows", "flow", "ns", **_BUDGET)

        assert captured["timeout"] <= kube._HEALTH_TIMEOUT // 4

    def test_the_read_is_clamped_to_the_remaining_budget(self, monkeypatch, no_sleep):
        """The budget is a wall-clock bound, not a bound on when a read starts.

        A read that begins just before the deadline would otherwise run to its
        own timeout past it, so several assertions could overrun a live run's
        remaining time together.
        """
        seen = []

        def _capture(_type, _name, _ns, **kwargs):
            seen.append(kwargs["timeout"])
            return _cr()

        monkeypatch.setattr(kube, "wait_for_cr", _capture)
        with pytest.raises(AssertionError):
            kube.wait_for_cr_health("dataflows", "flow", "ns", timeout=5, interval=0)

        assert seen, "the read was never attempted"
        assert max(seen) <= 5, (
            f"a read was given {max(seen)}s inside a 5s budget, so the budget "
            f"is not a wall-clock bound"
        )

    def test_a_non_default_expected_status_is_honored(self, monkeypatch, no_sleep):
        """`expected` selects what to wait for, so a caller can await any status."""
        monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("Degraded"))

        health = kube.wait_for_cr_health(
            "dataflows", "flow", "ns", expected="Degraded", **_BUDGET
        )
        assert health["status"] == "Degraded"

    def test_a_non_default_expected_status_still_times_out(self, monkeypatch, no_sleep):
        """The same parameter has to reject a status it did not ask for."""
        monkeypatch.setattr(kube, "wait_for_cr", lambda *a, **k: _cr("Available"))

        with pytest.raises(AssertionError) as excinfo:
            kube.wait_for_cr_health(
                "dataflows", "flow", "ns", expected="Degraded", **_BUDGET
            )
        assert "expected 'Degraded'" in str(excinfo.value)


class TestWaitForCrTimeout:
    """A projection wait that expires says what it was waiting for.

    ARM accepting a write and the custom location projecting the resource are
    separate events, so the useful question when this expires is which of the
    two did not happen. The underlying kubectl error alone does not answer it.
    """

    def _expired(self, monkeypatch):
        err = kube.KubectlError("Error from server (NotFound): dataflows not found", 1, "stderr text")

        def _raise(_args, **_kwargs):
            raise err

        monkeypatch.setattr(kube, "kubectl_json", _raise)
        with pytest.raises(kube.KubectlError) as excinfo:
            kube.wait_for_cr("dataflows.x.io", "site-telemetry", "azure-iot-operations", timeout=1)
        return str(excinfo.value)

    def test_the_message_names_the_resource_and_namespace(self, monkeypatch):
        message = self._expired(monkeypatch)
        # One equality on the opening clause rather than three containment
        # checks, so the assertion pins the order the message names things.
        assert message.startswith(
            "'site-telemetry' (dataflows.x.io) did not appear in namespace "
            "'azure-iot-operations' within 1s."
        )

    def test_the_message_names_the_budget_and_the_two_possibilities(self, monkeypatch):
        message = self._expired(monkeypatch)
        assert "1s" in message
        assert "projected" in message

    def test_the_underlying_kubectl_error_is_kept(self, monkeypatch):
        """The kubectl text is what distinguishes a late projection from a broken read."""
        assert "NotFound" in self._expired(monkeypatch)

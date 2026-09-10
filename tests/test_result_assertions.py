"""Integration result diagnostics respect the publication boundary."""

import pytest

from siteops.planning import OperationIdentity, OperationKind, TargetKind
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    RunResult,
    SiteResult,
)
from tests.integration.conftest import _assert_deployed
from tests.integration.helpers.assertions import assert_step_succeeded


def _run(status, *, interrupted=False):
    reason = None
    if status is not OperationStatus.SUCCEEDED:
        reason = OutcomeReason(
            OutcomeReasonCode.CONDITION_FALSE
            if status is OperationStatus.SKIPPED
            else OutcomeReasonCode.OPERATION_FAILED,
            private_detail="opaque-provider-detail",
        )
    operation = OperationResult(
        identity=OperationIdentity("private-site", "deploy"),
        kind=OperationKind.DEPLOYMENT,
        status=status,
        elapsed=0.0,
        reason=reason,
    )
    site = SiteResult.from_operations(
        target="private-site",
        kind=TargetKind.RESOURCE_GROUP,
        operations=(operation,),
        elapsed=0.0,
    )
    return RunResult.from_sites((site,), elapsed=0.0, interrupted=interrupted)


@pytest.mark.parametrize("destination", ["local", "GITHUB_ACTIONS", "TF_BUILD"])
@pytest.mark.parametrize("boundary", ["run", "operation"])
def test_result_assertions_publish_only_fixed_failure_reasons(
    monkeypatch, destination, boundary,
):
    for marker in ("GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0" if destination == "local" else "")
    if destination != "local":
        monkeypatch.setenv(destination, "true")
    result = _run(OperationStatus.FAILED)

    with pytest.raises(AssertionError) as error:
        if boundary == "run":
            _assert_deployed(result, "sample")
        else:
            assert_step_succeeded(result, "private-site", "deploy")

    message = str(error.value)
    if destination == "local":
        assert "opaque-provider-detail" in message
        assert "private-site" in message
    else:
        assert "opaque-provider-detail" not in message
        assert "private-site" not in message
        assert "The operation failed." in message


def test_deployment_assertion_requires_nonempty_results():
    with pytest.raises(AssertionError, match="no target results"):
        _assert_deployed(RunResult.from_sites((), elapsed=0.0), "sample")


def test_deployment_assertion_requires_executed_success():
    with pytest.raises(AssertionError, match="no executed successes"):
        _assert_deployed(_run(OperationStatus.SKIPPED), "sample")


def test_deployment_assertion_rejects_interrupted_observed_success():
    with pytest.raises(AssertionError, match="deployment did not complete"):
        _assert_deployed(_run(OperationStatus.SUCCEEDED, interrupted=True), "sample")


def test_deployment_assertion_retains_a_successful_result():
    result = _run(OperationStatus.SUCCEEDED)
    assert _assert_deployed(result, "sample") is result

"""Provider evidence explains outcomes without changing their accounting."""

import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from siteops import diagnostics
from siteops.executor import AzCliExecutor, ToolCallResult, UnconfirmedCompletion
from siteops.planning import OperationIdentity, OperationKind, PlanProjection, TargetKind
from siteops.reporting import render_plain_run, serialize_run_json
from siteops.results import (
    OperationResult,
    OperationStatus,
    OutcomeReason,
    OutcomeReasonCode,
    RunResult,
    SiteResult,
)
from tests.test_content_diagnostics import _adapter

SENTINELS = ("FactoryBlue", "AuthorizationFailedPlant", "SubscriptionNotFoundFactory", "InvalidTemplateCustomer",
             "customerA", "idleTimeoutInMinutes")


@pytest.fixture(autouse=True)
def private_engine_scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEOPS_TEMP_DIR", str(tmp_path / "runtime"))


@pytest.mark.parametrize("code,category", [
    ("SubscriptionNotFound", "subscription-not-visible"), ("AuthorizationFailed", "authorization-failed"),
    ("InvalidTemplate", "template-invalid"), ("QuotaExceeded", "other"), ("FactoryBlue", "other"),
])
def test_structured_provider_codes_are_explicitly_admitted(code, category):
    node = {"code": code, "message": "FactoryBlue", "target": "AuthorizationFailed"}
    result = diagnostics.arm_failure([node])
    assert result.category == category
    assert result.azure_code == (code if category != "other" else None)


@pytest.mark.parametrize("children,category,code", [
    ([{"code": "InvalidTemplate"}, {"code": "InvalidTemplate"}], "template-invalid", "InvalidTemplate"),
    ([{"code": "InvalidTemplate"}, {"code": "AuthorizationFailed"}], "other", None),
    ([{"code": "AuthorizationFailed"}, {"code": "QuotaExceeded"}], "other", None),
    ([{"message": "AuthorizationFailed"}], "other", None),
])
def test_arm_causes_reduce_all_nodes_not_first(children, category, code):
    result = diagnostics.arm_failure([{"code": "DeploymentFailed", "details": children}])
    assert (result.category, result.azure_code) == (category, code)


@pytest.mark.parametrize("node", [
    {"code": True}, {"code": ["AuthorizationFailed"]}, {"code": "x" * 65},
    {"code": "AuthorizationFailed", "details": "FactoryBlue"},
    {"code": "DeploymentFailed", "additionalInfo": [{"code": "AuthorizationFailed"}]},
    {"resourceName": "AuthorizationFailed", "message": "AuthorizationFailed", "target": "AuthorizationFailed"},
    {"code": "authorizationfailed"},
    {"code": "FactoryBlue", "details": [{"code": "InvalidTemplate"}]},
])
def test_invalid_and_wrong_origin_arm_evidence_falls_back(node):
    failure = diagnostics.arm_failure([node])
    assert (failure.category, failure.azure_code) == ("other", None)


def test_arm_extraction_is_bounded_and_uses_builtin_shapes_only():
    cyclic = {"code": "DeploymentFailed"}
    cyclic["innererror"] = cyclic
    deep = {"code": "InvalidTemplate"}
    for _ in range(10):
        deep = {"code": "DeploymentFailed", "innererror": deep}
    broad = {"code": "DeploymentFailed", "details": [{"code": "InvalidTemplate"} for _ in range(65)]}

    class Hostile(dict):
        def get(self, *args):
            raise AssertionError("Custom accessor")

    for node in (cyclic, deep, broad, Hostile(code="AuthorizationFailed")):
        assert diagnostics.arm_failure([node]).category == "other"


@pytest.mark.parametrize("error,category", [
    (subprocess.TimeoutExpired("private", 1), "timeout"),
    (FileNotFoundError("FactoryBlue"), "tool-unavailable"),
])
@pytest.mark.parametrize("tool", ["az", "kubectl"])
def test_tool_exception_evidence_is_typed(tmp_workspace, tool, error, category):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = executor._kubectl_path = "private-tool"
    with patch("subprocess.run", side_effect=error):
        result = getattr(executor, "_run_" + tool)(["version"])
    assert not result.success
    assert result.diagnostic.category == category


@pytest.mark.parametrize("text,category", [
    ("Subscription 'FactoryBlue' not found", "subscription-not-visible"),
    ("AuthorizationFailed: customerA has no authorization", "authorization-failed"),
    ("InvalidTemplate: idleTimeoutInMinutes is invalid", "template-invalid"),
    ("Missing input parameters: customerA", "template-invalid"),
    ("resource customerA mentions AuthorizationFailedPlant", "other"),
])
def test_cli_category_never_synthesizes_an_azure_code(tmp_workspace, text, category):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = "private-az"
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", text)):
        result = executor._run_az(["deployment", "group", "create"])
    assert result.diagnostic.category == category
    assert result.diagnostic.azure_code is None


@pytest.mark.parametrize("text", [
    '{"message":"SubscriptionNotFound","target":"AuthorizationFailed"}',
    '{"code":"SubscriptionNotFound","resourceName":"AuthorizationFailed"}',
    '["SubscriptionNotFound"]', '"SubscriptionNotFound"',
    '{"error":{"code":"SubscriptionNotFound"}',
    '{"error":{"code":"SubscriptionNotFound","message":"' + "x" * 8192 + '"}}',
], ids=["message", "wrong-envelope", "array", "scalar", "malformed-json", "bounded-out"])
def test_cli_structured_or_bounded_out_errors_never_fall_back_to_text(tmp_workspace, text):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = "private-az"
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", text)):
        result = executor._run_az(["deployment", "group", "create"])
    assert result.diagnostic == diagnostics.OTHER_FAILURE


def _operation(status, name, failure=None):
    return OperationResult(
        OperationIdentity("FactoryBlue", name), OperationKind.DEPLOYMENT, status, 0,
        reason=None if status is OperationStatus.SUCCEEDED else OutcomeReason(OutcomeReasonCode.OPERATION_FAILED),
        failure=failure,
    )


def _run(operations, interrupted=False):
    site = SiteResult.from_operations(target="FactoryBlue", kind=TargetKind.RESOURCE_GROUP,
                                     operations=operations, elapsed=0)
    return RunResult.from_sites((site,), elapsed=0, interrupted=interrupted)


@pytest.mark.parametrize("projection", list(PlanProjection))
def test_failure_groups_reconcile_without_changing_counts(projection):
    failure = diagnostics.ProviderFailure("authorization-failed", "AuthorizationFailed")
    operations = (_operation(OperationStatus.FAILED, "one", failure),
                  _operation(OperationStatus.FAILED, "two", failure),
                  _operation(OperationStatus.UNKNOWN, "three", diagnostics.ProviderFailure("timeout")))
    document = json.loads(serialize_run_json(_run(operations), projection, engine_version="FactoryBlue"))
    section = document["summary"]["operations"]
    assert set(section["counts"]) == {"failed", "unknown", "succeeded", "skipped", "not-run", "cancelled"}
    groups = section["failureDiagnostics"]
    assert [(g["status"], g["category"], g["count"]) for g in groups] == [
        ("failed", "authorization-failed", 2), ("unknown", "timeout", 1),
    ]
    for status in ("failed", "unknown"):
        assert sum(g["count"] for g in groups if g["status"] == status) == section["counts"][status]
    for group in groups:
        entry = diagnostics.public_entry(diagnostics.resolve_member("provider." + group["category"]), None)
        for field in ("summary", "guidance", "documentation"):
            assert group[field].encode() == getattr(entry, field).encode()
    assert all(value not in json.dumps(groups) for value in SENTINELS)
    if projection is PlanProjection.LOCAL_PRIVATE:
        assert document["sites"][0]["operations"][0]["failure"]["category"] == "authorization-failed"
    else:
        assert "sites" not in document


@pytest.mark.parametrize("status", list(OperationStatus))
def test_only_failed_and_unknown_operations_receive_failure(status):
    operation = _operation(status, "step", diagnostics.ProviderFailure("timeout"))
    assert (operation.failure is not None) is (status in {OperationStatus.FAILED, OperationStatus.UNKNOWN})


def test_malformed_failure_input_cannot_run_a_formatter():
    class Hostile(ValueError):
        def __str__(self):
            raise AssertionError("Formatter called")
    for evidence in (Hostile(), ValueError(""), {}, None, diagnostics.ProviderFailure([], []),
                     diagnostics.ProviderFailure("authorization-failed", True),
                     diagnostics.ProviderFailure("authorization-failed", "SubscriptionNotFound")):
        assert _operation(OperationStatus.UNKNOWN, "step", evidence).failure.category == "other"


def test_plain_provider_summary_preserves_unknown_and_registry_guidance():
    result = _run((_operation(OperationStatus.UNKNOWN, "step", diagnostics.ProviderFailure("timeout")),))
    text = render_plain_run(result, redacted=True)
    assert "Deployment summary" in text and "Failure diagnostics" in text
    assert "unknown: timeout" in " ".join(text.split())
    assert "Confirm the provider state" in text and "FactoryBlue" not in text


@pytest.mark.parametrize("kind", ["DeploymentPlan", "DeploymentRun"])
def test_adapter_accepts_supported_old_plan_and_run(kind):
    if kind == "DeploymentPlan":
        document = {"apiVersion": "siteops/v1alpha1", "kind": kind, "projection": "publishable",
                    "engine": {"name": "siteops", "version": "future"}, "status": "planned",
                    "intent": "executable", "executable": True,
                    "summary": {"targetCount": 0, "operationCount": 0,
                                "dispositions": {"execute": 0, "skip": 0, "blocked": 0}, "diagnosticCount": 0},
                    "diagnostics": []}
    else:
        document = json.loads(serialize_run_json(RunResult.from_sites((), elapsed=0),
                                               PlanProjection.PUBLISHABLE, engine_version="future"))
        document["summary"]["operations"].pop("failureDiagnostics", None)
    document["diagnostics"] = [{"code": "future.code", "severity": "warning", "summary": "Older diagnostic."}]
    if kind == "DeploymentPlan":
        document["summary"]["diagnosticCount"] = 1
    logs, summary, valid = _adapter().render(document, kind, 0, "ado")
    assert valid and all("unavailable" not in line for line in logs + summary)
    assert "future.code: Older diagnostic." in "\n".join(logs)
    assert "Failure diagnostics" not in "\n".join(logs)


@pytest.mark.parametrize("root,children,category,code", [
    ({"code": "DeploymentFailed"}, [{"code": "InvalidTemplate"}], "template-invalid", "InvalidTemplate"),
    ({"code": "DeploymentFailed"}, [{"code": "AuthorizationFailed"}, {"code": "QuotaExceeded"}], "other", None),
    ({"code": "SubscriptionNotFound"}, None, "subscription-not-visible", "SubscriptionNotFound"),
])
def test_terminal_arm_evidence_survives_before_private_formatting(tmp_workspace, root, children, category, code):
    executor = AzCliExecutor(tmp_workspace)
    payload = {"properties": {"provisioningState": "Failed", "error": {**root, "message": "FactoryBlue"}}}
    operations = [{"properties": {"provisioningState": "Failed",
                                 "targetResource": {"resourceName": "AuthorizationFailed"},
                                 "statusMessage": {"error": child}}} for child in children or ()]
    responses = [ToolCallResult(True, json.dumps(payload))]
    responses.append(ToolCallResult(False, stderr="AuthorizationFailed: customerA") if children is None else
                     ToolCallResult(True, json.dumps(operations)))
    with patch.object(executor, "_run_az", side_effect=responses):
        result = executor._poll_deployment([], [], "private", "step", "FactoryBlue")
    assert not result.success and result.unconfirmed is None
    assert (result.failure.category, result.failure.azure_code) == (category, code)


def test_success_clears_submit_attempt_failures(tmp_workspace):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = "private-az"
    responses = [ToolCallResult(False, stderr="Command timed out after 300s", diagnostic=diagnostics.ProviderFailure("timeout")),
                 ToolCallResult(True, '{"properties":{"provisioningState":"Succeeded"}}')]
    with patch.object(executor, "_run_az", side_effect=responses), patch("siteops.executor._wait_or_stop"):
        result = executor._deploy([], [], [], {}, "deployment", "step", "site")
    assert result.success and result.failure is None


def test_observation_deadline_takes_precedence(tmp_workspace):
    executor = AzCliExecutor(tmp_workspace)
    failure = diagnostics.ProviderFailure("authorization-failed", "AuthorizationFailed")
    with patch.object(executor, "_run_az", return_value=ToolCallResult(False, diagnostic=failure)), \
            patch("siteops.executor.time.monotonic", side_effect=[0, 0, 1, 2]), \
            patch("siteops.executor.DEFAULT_AZ_TIMEOUT_SECONDS", 0):
        result = executor._poll_deployment([], [], "deployment", "step", "site")
    assert result.unconfirmed is UnconfirmedCompletion.DEADLINE_EXCEEDED
    assert result.failure.category == "timeout" and result.failure.azure_code is None


@pytest.mark.parametrize("fault", ["count", "bool", "status", "guidance", "azure", "duplicate"])
def test_adapter_rejects_malformed_failure_groups_but_preserves_core(fault):
    document = json.loads(serialize_run_json(
        _run((_operation(OperationStatus.UNKNOWN, "step", diagnostics.ProviderFailure("timeout")),)),
        PlanProjection.PUBLISHABLE, engine_version="future"))
    group = document["summary"]["operations"]["failureDiagnostics"][0]
    if fault == "duplicate":
        document["summary"]["operations"]["failureDiagnostics"].append(group.copy())
    else:
        key, value = {"count": ("count", 2), "bool": ("count", True), "status": ("status", "failed"),
                      "guidance": ("guidance", "FactoryBlue\x1b"), "azure": ("azureCode", ["AuthorizationFailed"])}[fault]
        group[key] = value
    logs, summary, valid = _adapter().render(document, "DeploymentRun", 1, "ado")
    assert valid and "Status unknown." in summary[0]
    assert _adapter().UNAVAILABLE in "\n".join(logs) and "FactoryBlue" not in "\n".join(logs)


@pytest.mark.parametrize("tool", ["az", "kubectl"])
def test_verbose_tool_logging_does_not_publish_commands_or_payloads(tmp_workspace, monkeypatch, caplog, tool):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = executor._kubectl_path = str(tmp_workspace / "FactoryBlue")
    caplog.set_level("DEBUG", logger="siteops.executor")
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "customerA", "FactoryBlue")):
        getattr(executor, "_run_" + tool)(["--file", "InvalidTemplateCustomer"])
    assert all(value not in caplog.text for value in SENTINELS)


@pytest.mark.parametrize("root,status_message,category", [
    ({}, {"error": {"code": "InvalidTemplate"}}, "other"),
    ({"code": "DeploymentFailed", "innererror": []}, {"error": {"code": "InvalidTemplate"}}, "other"),
    ({"code": "DeploymentFailed"}, {"code": "InvalidTemplate"}, "other"),
    ({"code": "DeploymentFailed"}, {"error": {"code": "InvalidTemplate"}}, "template-invalid"),
])
def test_operation_evidence_requires_the_error_envelope(tmp_workspace, root, status_message, category):
    executor = AzCliExecutor(tmp_workspace)
    operations = [{"properties": {"provisioningState": "Failed", "statusMessage": status_message,
                                 "targetResource": {"resourceName": "AuthorizationFailed"}}}]
    with patch.object(executor, "_run_az", return_value=ToolCallResult(True, json.dumps(operations))):
        detail, failure = executor._format_deployment_failure({"properties": {"error": root}}, [])
    assert detail
    assert failure.category == category
    assert failure.azure_code == ("InvalidTemplate" if category == "template-invalid" else None)


@pytest.mark.parametrize("stop", [False, True])
def test_last_observation_evidence_survives_unparseable_response_or_stop(tmp_workspace, monkeypatch, stop):
    executor = AzCliExecutor(tmp_workspace)
    signal = threading.Event()
    failure = diagnostics.ProviderFailure("authorization-failed", "AuthorizationFailed")
    responses = [ToolCallResult(False, stderr="private", diagnostic=failure),
                 ToolCallResult(True, "not-json")]

    def observe(*args, **kwargs):
        result = responses.pop(0)
        if stop:
            signal.set()
        return result

    monkeypatch.setattr("siteops.executor.time.monotonic", lambda: 0 if responses else 2)
    monkeypatch.setattr("siteops.executor.DEPLOYMENT_OBSERVATION_GRACE_SECONDS", 1)
    monkeypatch.setattr("siteops.executor._wait_or_stop", lambda *args: None)
    monkeypatch.setattr(executor, "_run_az", observe)
    result = executor._poll_deployment([], [], "deployment", "step", "site", signal)
    assert result.unconfirmed is (UnconfirmedCompletion.STOPPED_OBSERVING if stop else
                                  UnconfirmedCompletion.OBSERVATION_LOST)
    assert result.failure == failure


def test_wait_stopped_observation_retains_typed_evidence(tmp_workspace, monkeypatch):
    from siteops.executor import ConditionObservation, WaitState
    from tests.test_wait_step import _condition

    executor = AzCliExecutor(tmp_workspace)
    signal = threading.Event()
    failure = diagnostics.ProviderFailure("authorization-failed", "AuthorizationFailed")

    def observe(*args):
        signal.set()
        return ConditionObservation(WaitState.PENDING, error="FactoryBlue", diagnostic=failure)

    monkeypatch.setattr(executor, "_evaluate_condition", observe)
    result = executor.wait_for_condition(_condition(), 1, 1, "subscription", "step", "site", stop_requested=signal)
    assert result.unconfirmed is UnconfirmedCompletion.STOPPED_OBSERVING
    assert result.failure == failure


@pytest.mark.parametrize("kind", ["DeploymentPlan", "DeploymentRun"])
@pytest.mark.parametrize("fault", ["count-overflow", "bool-count", "missing-count", "totals", "exit", "shape"])
def test_adapter_validates_each_deployment_core(kind, fault):
    from siteops.planning import PlanBuildResult, PlanIntent, PlanStatus, serialize_plan
    from siteops.results import preparation_failure_result

    result = PlanBuildResult(PlanStatus.INVALID, False, None, intent=PlanIntent.EXECUTABLE)
    document = (serialize_plan(result, PlanProjection.PUBLISHABLE, engine_version="future") if kind == "DeploymentPlan"
                else json.loads(serialize_run_json(preparation_failure_result(result),
                                                 PlanProjection.PUBLISHABLE, engine_version="future")))
    code = 1
    if kind == "DeploymentPlan":
        if fault == "count-overflow":
            document["summary"]["composition"]["requirementCount"] = 2**63
        elif fault == "bool-count":
            document["summary"]["diagnosticCount"] = True
        elif fault == "missing-count":
            del document["summary"]["operationCount"]
        elif fault == "totals":
            document["summary"]["dispositions"]["blocked"] = 1
        elif fault == "exit":
            code = 0
        else:
            document["summary"]["composition"] = []
    else:
        section = document["summary"]["operations"]
        if fault == "count-overflow":
            section["total"] = 2**63
        elif fault == "bool-count":
            section["counts"]["not-run"] = True
        elif fault == "missing-count":
            del section["counts"]["unknown"]
        elif fault == "totals":
            section["counts"]["not-run"] = 1
        elif fault == "exit":
            document["exitCode"] = True
        else:
            document["summary"]["sites"] = []
    logs, summary, valid = _adapter().render(document, kind, code, "ado")
    assert not valid and summary == [_adapter().UNAVAILABLE]
    assert "result:" not in "\n".join(logs)


def test_adapter_rejects_present_empty_azure_code_without_losing_outcome():
    document = json.loads(serialize_run_json(
        _run((_operation(OperationStatus.UNKNOWN, "step", diagnostics.ProviderFailure("timeout")),)),
        PlanProjection.PUBLISHABLE, engine_version="future"))
    document["summary"]["operations"]["failureDiagnostics"][0]["azureCode"] = ""
    logs, summary, valid = _adapter().render(document, "DeploymentRun", 1, "github")
    assert valid and summary[0].startswith("Status unknown.")
    assert summary[1:] == [_adapter().UNAVAILABLE]
    assert "unknown: timeout" not in "\n".join(logs)


@pytest.mark.parametrize("kind", ["ValidationResult", "DeploymentPlan", "DeploymentRun"])
@pytest.mark.parametrize("platform", ["ado", "github"])
def test_malformed_optional_details_preserve_a_zero_exit_result(kind, platform):
    from siteops.diagnostics import ValidationResult
    from siteops.planning import PlanBuildResult, PlanIntent, PlanStatus, serialize_plan

    if kind == "ValidationResult":
        document = ValidationResult(()).document("publishable", "future")
    elif kind == "DeploymentPlan":
        document = serialize_plan(PlanBuildResult(PlanStatus.INVALID, False, None, intent=PlanIntent.EXECUTABLE),
                                  PlanProjection.PUBLISHABLE, engine_version="future")
        document.update(status="planned", executable=True)
    else:
        document = json.loads(serialize_run_json(RunResult.from_sites((), elapsed=0),
                                               PlanProjection.PUBLISHABLE, engine_version="future"))
    document["diagnostics"] = [{"code": "future.code", "severity": "warning",
                                "summary": "FactoryBlue\x1b"}]
    if "diagnosticCount" in document["summary"]:
        document["summary"]["diagnosticCount"] = 1
    core = _adapter()._core(document, kind, 0)
    logs, summary, valid = _adapter().render(document, kind, 0, platform)
    assert valid and summary == [core, _adapter().UNAVAILABLE]
    assert "FactoryBlue" not in "\n".join(logs + summary)


@pytest.mark.parametrize("redacted", [False, True])
def test_proxy_exception_keeps_local_scrubbing_and_public_registry_text(tmp_workspace, monkeypatch, caplog, redacted):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = "private-az"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", str(int(redacted)))
    with patch("siteops.executor._allocate_arc_port_slot", side_effect=RuntimeError("FactoryBlue\x1b[31m")), \
            caplog.at_level("ERROR", logger="siteops.executor"):
        with executor._arc_proxy("FactoryBlue", "customerA", "subscription") as session:
            assert session.kubeconfig is None
            assert session.diagnostic.category == "other"
    assert "\x1b" not in caplog.text
    assert ("FactoryBlue" in caplog.text) is (not redacted)


@pytest.mark.parametrize("code,category", [
    ("SubscriptionNotFound", "subscription-not-visible"),
    ("AuthorizationFailed", "authorization-failed"),
    ("InvalidTemplate", "template-invalid"),
])
def test_structured_cli_evidence_survives_wait_rejection(tmp_workspace, code, category):
    from tests.test_wait_step import _condition

    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = "private-az"
    error = json.dumps({"error": {"code": code, "message": "FactoryBlue", "target": "customerA"}})
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", error)):
        result = executor.wait_for_condition(_condition(), 1, 1, "subscription", "step", "site")
    assert not result.success and result.unconfirmed is None
    assert result.failure == diagnostics.ProviderFailure(category, code)


@pytest.mark.parametrize("code,category", [
    ("SubscriptionNotFound", "subscription-not-visible"), ("AuthorizationFailed", "authorization-failed"),
    ("InvalidTemplate", "template-invalid"), ("QuotaExceeded", "other"),
])
def test_real_executor_failure_reaches_run_and_both_renderers(tmp_workspace, monkeypatch, caplog, code, category):
    from siteops.orchestrator import Orchestrator
    from tests.test_orchestrator_results import _plan_result

    orchestrator = Orchestrator(tmp_workspace)
    orchestrator.executor._az_path = "private-az"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    caplog.set_level("DEBUG", logger="siteops.executor")
    node = {"code": code, "message": " ".join(SENTINELS), "target": "AuthorizationFailed"}
    responses = [
        ToolCallResult(True),
        ToolCallResult(True, json.dumps({"properties": {"provisioningState": "Failed",
                                                       "error": {"code": "DeploymentFailed"}}})),
        ToolCallResult(True, json.dumps([
            {"properties": {"provisioningState": "Failed", "statusMessage": {"error": node},
                            "targetResource": {"resourceName": "AuthorizationFailed"}}}
            for _ in range(2)
        ])),
    ]
    with patch.object(orchestrator.executor, "_run_az", side_effect=responses) as calls:
        result = orchestrator.execute_plan(_plan_result(step_count=2))
    assert result.exit_code == 1
    assert [op.status for op in result.sites[0].operations] == [OperationStatus.FAILED, OperationStatus.NOT_RUN]
    assert calls.call_count == 3
    for projection in PlanProjection:
        document = json.loads(serialize_run_json(result, projection, engine_version="future"))
        group, = document["summary"]["operations"]["failureDiagnostics"]
        assert group["count"] == 1 and group["category"] == category
        assert group.get("azureCode") == (code if category != "other" else None)
        entry = diagnostics.public_entry(diagnostics.resolve_member("provider." + category), None)
        for field in ("summary", "guidance", "documentation"):
            assert group[field].encode("utf-8") == getattr(entry, field).encode("utf-8")
        if projection is PlanProjection.PUBLISHABLE:
            for platform in ("ado", "github"):
                logs, summary, valid = _adapter().render(document, "DeploymentRun", result.exit_code, platform)
                assert valid and f"failed: {category}" in "\n".join(logs)
                assert all(value not in "\n".join(logs + summary) for value in SENTINELS)
        else:
            assert document["sites"][0]["operations"][0]["failure"]["category"] == category
            assert all(value not in json.dumps(document) for value in SENTINELS)
    assert all(value not in caplog.text for value in SENTINELS)


def test_interrupted_snapshot_retains_completed_evidence_and_normalizes_active_operation(tmp_workspace):
    from siteops.orchestrator import _TargetExecutionState
    from tests.test_orchestrator_results import _plan_result

    target, active_target = _plan_result(step_count=2, target_count=2).plan.targets
    checkpoint = _TargetExecutionState(target)
    failure = diagnostics.ProviderFailure("authorization-failed", "AuthorizationFailed")
    completed = OperationResult(target.operations[0].identity, OperationKind.DEPLOYMENT,
                                OperationStatus.FAILED, 0, reason=OutcomeReason(OutcomeReasonCode.OPERATION_FAILED),
                                failure=failure)
    checkpoint.record(completed, {})
    active = _TargetExecutionState(active_target)
    active.begin(active_target.operations[0], "deployment")
    result = RunResult.from_sites((checkpoint.snapshot_interrupted().site, active.snapshot_interrupted().site),
                                 elapsed=0, interrupted=True)
    assert result.exit_code == 130
    assert result.sites[0].operations[0].failure == failure
    assert result.sites[1].operations[0].failure == diagnostics.OTHER_FAILURE
    groups = json.loads(serialize_run_json(result, PlanProjection.PUBLISHABLE, engine_version="future"))[
        "summary"]["operations"]["failureDiagnostics"]
    assert [(g["status"], g["category"], g["count"]) for g in groups] == [
        ("failed", "authorization-failed", 1), ("unknown", "other", 1),
    ]


@pytest.mark.parametrize("category,code", [
    ("subscription-not-visible", "SubscriptionNotFound"), ("authorization-failed", "AuthorizationFailed"),
    ("template-invalid", "InvalidTemplate"), ("tool-unavailable", None), ("timeout", None), ("other", None),
])
def test_all_provider_public_text_ignores_producer_poison(category, code):
    failure = diagnostics.ProviderFailure(category, code)
    for field in ("summary", "public_summary", "guidance", "documentation", "serialized_detail"):
        object.__setattr__(failure, field, "FactoryBlue")
    result = _run((_operation(OperationStatus.FAILED, "step", failure),))
    entry = diagnostics.public_entry(diagnostics.resolve_member("provider." + category), None)
    for projection in PlanProjection:
        document = json.loads(serialize_run_json(result, projection, engine_version="future"))
        records = list(document["summary"]["operations"]["failureDiagnostics"])
        if projection is PlanProjection.LOCAL_PRIVATE:
            records.append(document["sites"][0]["operations"][0]["failure"])
        for record in records:
            for field in ("summary", "guidance", "documentation"):
                assert record[field].encode("utf-8") == getattr(entry, field).encode("utf-8")


@pytest.mark.parametrize("kind", ["DeploymentPlan", "DeploymentRun"])
@pytest.mark.parametrize("raw", [
    b'{"kind":"FactoryBlue","kind":"DeploymentRun"}', b"FactoryBlue", b'{"customerA":NaN}',
    b'{"customerA":' + b"9" * 100 + b"}", b"x" * (2 * 1024 * 1024 + 1),
], ids=["duplicate-key", "not-json", "non-finite", "oversized-integer", "oversized-file"])
def test_standalone_deployment_adapter_is_bounded_and_stdlib_only(tmp_path, kind, raw):
    helper = tmp_path / "render.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / "render-siteops-output.py", helper)
    (tmp_path / "siteops.py").write_text("raise AssertionError('Engine imported')")
    result_file = tmp_path / "private.json"
    result_file.write_bytes(raw)
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(helper), str(result_file),
                             "--kind", kind, "--exit-code", "17", "--platform", "github"],
                            cwd=tmp_path, env={**os.environ, "PYTHONSAFEPATH": "1"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and result.stderr == ""
    assert result.stdout == "::error::" + _adapter().UNAVAILABLE + "\n"


@pytest.mark.parametrize("status", ["succeeded", "skipped", "invalid"])
def test_adapter_rejects_run_status_that_conflicts_with_counts(status):
    document = json.loads(serialize_run_json(
        _run((_operation(OperationStatus.FAILED, "step"),)),
        PlanProjection.PUBLISHABLE, engine_version="future"))
    document["status"] = status
    code = 1 if status == "invalid" else 0
    document["exitCode"] = code
    _, summary, valid = _adapter().render(document, "DeploymentRun", code, "github")
    assert not valid and summary == [_adapter().UNAVAILABLE]


@pytest.mark.parametrize("platform", ["ado", "github"])
def test_adapter_encodes_provider_groups_and_ignores_extras(platform):
    document = json.loads(serialize_run_json(
        _run((_operation(OperationStatus.UNKNOWN, "step", diagnostics.ProviderFailure("timeout")),)),
        PlanProjection.PUBLISHABLE, engine_version="future"))
    group = document["summary"]["operations"]["failureDiagnostics"][0]
    group["guidance"] = "::error:: ##vso[task.logissue] %0A ;\n`code` [link] & <tag>"
    group["private"] = "FactoryBlue"
    logs, summary, valid = _adapter().render(document, "DeploymentRun", 1, platform)
    assert valid and ("%AZP250A" if platform == "ado" else "%250A") in "\n".join(logs)
    assert "%0A" in logs[-1] and "\\`code\\`" in "\n".join(summary)
    if platform == "ado":
        assert "%5D" in logs[-1] and "%3B" in logs[-1]
    assert all("FactoryBlue" not in line for line in logs + summary)


def test_verbose_parameter_and_wait_logging_omits_authored_values(tmp_workspace, monkeypatch, caplog):
    from tests.test_wait_step import _condition

    executor = AzCliExecutor(tmp_workspace, dry_run=True)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    caplog.set_level("DEBUG", logger="siteops.executor")
    path = executor._write_params_file({"FactoryBlue": "customerA"}, "InvalidTemplateCustomer", "site")
    assert "customerA" in path.read_text()
    executor.wait_for_condition(_condition(tag_key="FactoryBlue"), 1, 1, "subscription", "step", "site")
    executor.close()
    assert all(value not in caplog.text for value in SENTINELS)


def test_verbose_proxy_cleanup_does_not_publish_private_exceptions(tmp_workspace, monkeypatch, caplog):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = executor._kubectl_path = "private-tool"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    caplog.set_level("DEBUG", logger="siteops.executor")
    process = MagicMock()
    process.poll.return_value = None
    process.send_signal.side_effect = OSError("FactoryBlue")
    process.kill.side_effect = RuntimeError("customerA")
    with patch("subprocess.Popen", return_value=process), \
            patch("siteops.executor._ProxyOutputDrainer"), \
            patch("siteops.executor._probe_arc_proxy_ready", return_value=True), \
            patch("siteops.executor.os.getpgid", return_value=12345, create=True), \
            patch("siteops.executor.os.killpg", side_effect=OSError("FactoryBlue"), create=True):
        with executor._arc_proxy("cluster", "group", "subscription") as session:
            assert session.kubeconfig
    assert all(value not in caplog.text for value in SENTINELS)


@pytest.mark.parametrize("redacted", [False, True])
def test_proxy_private_error_survives_typed_result_without_publication(tmp_workspace, monkeypatch, caplog, redacted):
    executor = AzCliExecutor(tmp_workspace)
    executor._az_path = executor._kubectl_path = "private-tool"
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", str(int(redacted)))
    process, drainer = MagicMock(), MagicMock()
    process.poll.return_value = 1
    drainer.stderr = " ".join(SENTINELS)
    drainer.port_in_use = drainer.extension_missing = False
    caplog.set_level("DEBUG", logger="siteops.executor")
    with patch("subprocess.Popen", return_value=process), \
            patch("siteops.executor._ProxyOutputDrainer", return_value=drainer), \
            patch("siteops.executor._probe_arc_proxy_ready", return_value=False):
        with executor._arc_proxy("cluster", "group", "subscription") as session:
            assert all(value in session.error for value in SENTINELS)
    with patch.object(executor, "_arc_proxy", return_value=nullcontext(session)):
        result = executor.kubectl_apply("cluster", "group", "subscription", [], "step", "site")
    assert not result.success and result.error == session.error
    assert result.failure == diagnostics.OTHER_FAILURE
    operation = OperationResult(OperationIdentity("site", "step"), OperationKind.KUBECTL, OperationStatus.FAILED, 0,
                                reason=OutcomeReason(OutcomeReasonCode.OPERATION_FAILED, private_detail=result.error),
                                failure=result.failure)
    site = SiteResult.from_operations(target="site", kind=TargetKind.RESOURCE_GROUP, operations=(operation,), elapsed=0)
    run = RunResult.from_sites((site,), elapsed=0)
    for projection in PlanProjection:
        document = serialize_run_json(run, projection, engine_version="future")
        assert all(value not in document for value in SENTINELS)
    assert ("FactoryBlue" in caplog.text) is (not redacted)
    assert "FactoryBlue" in render_plain_run(run, redacted=False)


def test_existing_fleet_consumer_accepts_real_additive_run_counts():
    from tests.test_fleet_outcomes import deployment_run, module

    fixture = deployment_run()
    sites = tuple(SiteResult.from_operations(
        target=site["target"], kind=TargetKind.RESOURCE_GROUP, elapsed=0,
        operations=tuple(OperationResult(
            OperationIdentity(site["target"], operation["identity"]["step"]), OperationKind.DEPLOYMENT,
            OperationStatus(operation["status"]), 0,
            reason=OutcomeReason(OutcomeReasonCode.CONDITION_FALSE) if operation["status"] == "skipped" else None,
        ) for operation in site["operations"]),
    ) for site in fixture["sites"])
    document = json.loads(serialize_run_json(RunResult.from_sites(sites, elapsed=0),
                                           PlanProjection.LOCAL_PRIVATE, engine_version="1.0.0b1"))
    module("deploy-release-fleet").check_run(document, "1.0.0b1")
    assert document["summary"]["operations"]["failureDiagnostics"] == []
    assert sum(document["summary"]["operations"]["counts"].values()) == 18


def test_existing_published_e2e_receipt_accepts_additive_run(tmp_path):
    from tests.test_published_e2e_workflow import _embedded_python, _step_run

    block, = _embedded_python(_step_run("Deploy AIO through the published engine and package"))
    version = "1.0.0b1+build.42.1.g" + "a" * 12
    raw, receipt = tmp_path / "run.json", tmp_path / "receipt.json"
    raw.write_text(serialize_run_json(_run((_operation(OperationStatus.SUCCEEDED, "step"),)),
                                      PlanProjection.PUBLISHABLE, engine_version=version))
    result = subprocess.run([sys.executable, "-B", "-c", block, str(raw), str(receipt),
                             "v-test", "a" * 40, version], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    document = json.loads(receipt.read_text())
    assert document["siteCount"] == 1 and document["operationCount"] == 1
    assert document["succeededOperations"] == 1

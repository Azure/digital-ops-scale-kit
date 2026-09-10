"""Typed deployment result integration tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from siteops.compilation import (
    CompilationKey,
    DependencyCoverage,
    DependencyIdentity,
    PreparedTemplateUnit,
    SourceIdentity,
    TemplateCompilationIdentity,
    TemplateKind,
    VersionProvenance,
)
from siteops.executor import DeploymentResult, UnconfirmedCompletion
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    CapabilityKind,
    CapabilityProviderIdentity,
    CapabilityStatus,
    DeploymentOperation,
    DeploymentPlan,
    InputStatus,
    MappingValue,
    OperationIdentity,
    OperationKind,
    OperationScope,
    PlanBuildResult,
    PlanCapability,
    PlanDisposition,
    PlanExecutionMode,
    PlanIntent,
    PlanSkipReason,
    PlanStatus,
    PlanStep,
    PreparedOperation,
    PreparedTarget,
    SkipReasonCode,
    TargetKind,
)
from siteops.results import (
    OperationStatus,
    RunResult,
    RunStatus,
    SiteStatus,
)


def _template_unit() -> PreparedTemplateUnit:
    source = SourceIdentity(
        path=Path("template.json"),
        content_digest="source",
        size_bytes=1,
    )
    key = CompilationKey(
        source_path=source.path,
        source_content_digest=source.content_digest,
        template_kind=TemplateKind.ARM_JSON,
        compiler_fingerprint="arm-json",
        configuration_digest="none",
        invocation=("read-arm-json",),
    )
    return PreparedTemplateUnit(
        key=key,
        identity=TemplateCompilationIdentity(
            source=source,
            compiler_driver=None,
            compiler=None,
            configuration=None,
            dependencies=DependencyIdentity(
                coverage=DependencyCoverage.NOT_APPLICABLE,
            ),
            compiled_output_digest=source.content_digest,
        ),
        parameters=(),
    )


def _plan_result(
    *,
    step_count: int,
    target_count: int = 1,
    all_skipped: bool = False,
    parallel_sites: int = 1,
) -> PlanBuildResult:
    unit = _template_unit()
    described = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.DESCRIBED,
    )
    prepared = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(()),
        template_unit_key=unit.key,
    )
    steps = tuple(
        PlanStep(
            name=f"step-{index}",
            sequence=index + 1,
            kind=OperationKind.DEPLOYMENT,
            scope=OperationScope.RESOURCE_GROUP,
            details=described,
        )
        for index in range(step_count)
    )
    targets = tuple(
        PreparedTarget(
            name=f"site-{target_index}",
            kind=TargetKind.RESOURCE_GROUP,
            subscription="sub",
            resource_group=f"rg-{target_index}",
            location="eastus",
            operations=tuple(
                PreparedOperation(
                    identity=OperationIdentity(
                        target=f"site-{target_index}",
                        step=step.name,
                    ),
                    step=step,
                    disposition=(
                        PlanDisposition.SKIP
                        if all_skipped
                        else PlanDisposition.EXECUTE
                    ),
                    details=described if all_skipped else prepared,
                    skip_reason=(
                        PlanSkipReason(
                            code=SkipReasonCode.CONDITION_FALSE,
                            detail="condition not met",
                        )
                        if all_skipped
                        else None
                    ),
                )
                for step in steps
            ),
        )
        for target_index in range(target_count)
    )
    required_by = tuple(
        operation.identity
        for target in targets
        for operation in target.operations
        if operation.disposition is PlanDisposition.EXECUTE
    )
    capabilities = (
        (
            PlanCapability(
                kind=CapabilityKind.ARM_CONTROL_PLANE,
                status=CapabilityStatus.AVAILABLE,
                required_by=required_by,
                provider=CapabilityProviderIdentity(
                    name="azure-cli",
                    executable_path=Path("C:/tools/az.exe"),
                    version=None,
                    version_provenance=VersionProvenance.UNKNOWN,
                ),
            ),
        )
        if required_by
        else ()
    )
    plan = DeploymentPlan(
        manifest_name="typed-run",
        source_path=Path("manifests/typed-run.yaml"),
        intent=PlanIntent.EXECUTABLE,
        description=None,
        max_parallel_sites=parallel_sites,
        steps=steps,
        targets=targets,
        template_units=(unit,) if required_by else (),
        capabilities=capabilities,
    )
    return PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=True,
        plan=plan,
    )


def _success(**kwargs) -> DeploymentResult:
    return DeploymentResult(
        success=True,
        step_name=kwargs["step_name"],
        site_name=kwargs["site_name"],
        deployment_name=kwargs["deployment_name"],
    )


def test_typed_api_with_no_progress_writes_no_stdout(tmp_workspace, capsys):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=1)

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=_success,
    ):
        result = orchestrator.execute_plan(prepared, progress=None)

    assert capsys.readouterr().out == ""
    assert result.status is RunStatus.SUCCEEDED
    assert result.sites[0].operations[0].status is OperationStatus.SUCCEEDED


@pytest.mark.parametrize("message", ["closed progress stream", "", " \t\n"])
def test_progress_observer_failure_does_not_change_provider_outcome(
    tmp_workspace,
    caplog,
    message,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=1)

    def broken_progress(_event):
        raise RuntimeError(message)

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=_success,
    ):
        result = orchestrator.execute_plan(
            prepared,
            progress=broken_progress,
        )

    assert result.status is RunStatus.SUCCEEDED
    assert result.sites[0].operations[0].status is OperationStatus.SUCCEEDED
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "progress-reporting-failed"
    ]
    assert result.diagnostics[0].private_detail == (
        message if message.strip() else "RuntimeError"
    )
    assert "closed progress stream" not in caplog.text


def test_all_skipped_plan_has_explicit_skipped_status_and_exit_zero(
    tmp_workspace,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=2, all_skipped=True)

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=AssertionError("skipped operations must not execute"),
    ):
        result = orchestrator.execute_plan(prepared)

    assert result.status is RunStatus.SKIPPED
    assert result.exit_code == 0
    assert result.sites[0].status is SiteStatus.SKIPPED
    assert {
        operation.status
        for operation in result.sites[0].operations
    } == {OperationStatus.SKIPPED}


def test_partial_failure_accounts_for_remaining_operation_and_outputs(
    tmp_workspace,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=3)
    calls: list[str] = []

    def deploy(**kwargs):
        calls.append(kwargs["step_name"])
        if kwargs["step_name"] == "step-0":
            return DeploymentResult(
                success=True,
                step_name=kwargs["step_name"],
                site_name=kwargs["site_name"],
                deployment_name=kwargs["deployment_name"],
                outputs={
                    "retained": {
                        "type": "String",
                        "value": "available",
                    }
                },
            )
        return DeploymentResult(
            success=False,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
            error="provider rejected step-1",
        )

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=deploy,
    ):
        result = orchestrator.execute_plan(prepared)

    operations = result.sites[0].operations
    assert calls == ["step-0", "step-1"]
    assert [operation.status for operation in operations] == [
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.NOT_RUN,
    ]
    assert (
        operations[0].copy_outputs()["retained"]["value"]
        == "available"
    )
    assert sum(
        operation.status is OperationStatus.NOT_RUN
        for operation in operations
    ) == 1


def test_accepted_but_unobservable_deployment_is_unknown(
    tmp_workspace,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=2)

    uncertain = DeploymentResult(
        success=False,
        step_name="step-0",
        site_name="site-0",
        deployment_name="accepted-deployment",
        error=(
            "Observation timed out. ARM was not canceled and may still "
            "complete."
        ),
        unconfirmed=UnconfirmedCompletion.OBSERVATION_LOST,
    )

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        return_value=uncertain,
    ) as deploy:
        result = orchestrator.execute_plan(prepared)

    assert deploy.call_count == 1
    assert result.status is RunStatus.UNKNOWN
    assert result.exit_code == 1
    assert [operation.status for operation in result.sites[0].operations] == [
        OperationStatus.UNKNOWN,
        OperationStatus.NOT_RUN,
    ]
    assert (
        result.sites[0].operations[1].reason.code.value
        == "earlier-operation-unknown"
    )


def test_interrupt_retains_completed_output_and_marks_inflight_unknown(
    tmp_workspace,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=3)
    calls: list[str] = []

    def deploy(**kwargs):
        calls.append(kwargs["step_name"])
        if kwargs["step_name"] == "step-0":
            return DeploymentResult(
                success=True,
                step_name=kwargs["step_name"],
                site_name=kwargs["site_name"],
                deployment_name=kwargs["deployment_name"],
                outputs={
                    "retained": {
                        "type": "String",
                        "value": "available",
                    }
                },
            )
        raise KeyboardInterrupt("operator interrupted provider call")

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            side_effect=deploy,
        ),
        patch.object(orchestrator.executor, "close") as close,
    ):
        result = orchestrator.execute_plan(prepared)

    assert calls == ["step-0", "step-1"]
    assert result.interrupted is True
    assert result.status is RunStatus.UNKNOWN
    assert result.exit_code == 130
    assert [operation.status for operation in result.sites[0].operations] == [
        OperationStatus.SUCCEEDED,
        OperationStatus.UNKNOWN,
        OperationStatus.NOT_RUN,
    ]
    assert (
        result.sites[0].operations[0].copy_outputs()["retained"]["value"]
        == "available"
    )
    close.assert_called_once_with()


def test_parallel_interrupt_stops_new_operations_before_cleanup(
    tmp_workspace,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(
        step_count=2,
        target_count=3,
        parallel_sites=2,
    )
    started = threading.Event()
    lock = threading.Lock()
    active = 0
    calls: list[str] = []

    def deploy(**kwargs):
        nonlocal active
        with lock:
            active += 1
        try:
            calls.append(kwargs["step_name"])
            started.set()
            time.sleep(0.1)
            return _success(**kwargs)
        finally:
            with lock:
                active -= 1

    def interrupting_completion(_futures):
        assert started.wait(timeout=5)
        raise KeyboardInterrupt("operator stopped the rollout")
        yield

    def close():
        with lock:
            assert active == 0, "cleanup raced a live provider worker"

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            side_effect=deploy,
        ),
        patch(
            "siteops.orchestrator.as_completed",
            side_effect=interrupting_completion,
        ),
        patch.object(orchestrator.executor, "close", side_effect=close),
    ):
        result = orchestrator.execute_plan(prepared)

    assert result.interrupted is True
    assert calls
    assert set(calls) == {"step-0"}
    assert all(
        operation.status
        in {
            OperationStatus.SUCCEEDED,
            OperationStatus.CANCELLED,
        }
        for site in result.sites
        for operation in site.operations
    )


def test_executor_cleanup_runs_when_binding_raises(tmp_workspace):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=1)

    with (
        patch.object(
            orchestrator,
            "_bind_plan_capabilities",
            side_effect=RuntimeError("binding defect"),
        ),
        patch.object(orchestrator.executor, "close") as close,
        pytest.raises(RuntimeError, match="binding defect"),
    ):
        orchestrator.execute_plan(prepared)

    close.assert_called_once_with()


def test_deploy_builds_once_and_uses_typed_core(
    tmp_workspace,
    capsys,
):
    orchestrator = Orchestrator(tmp_workspace)
    prepared = _plan_result(step_count=1)
    expected = RunResult.from_sites((), elapsed=0.0, run_id="run")
    stop_requested = threading.Event()

    with (
        patch.object(
            orchestrator,
            "build_plan",
            return_value=prepared,
        ) as build_plan,
        patch.object(
            orchestrator,
            "execute_plan",
            return_value=expected,
        ) as execute_plan,
    ):
        actual = orchestrator.deploy(
            Path("manifest.yaml"),
            selector="environment=test",
            parallel_override=3,
            progress=None,
            stop_requested=stop_requested,
        )

    assert actual is expected
    build_plan.assert_called_once_with(
        Path("manifest.yaml"),
        "environment=test",
        intent=PlanIntent.EXECUTABLE,
        manifest=None,
        sites=None,
        parallel_override=3,
    )
    execute_plan.assert_called_once_with(
        prepared,
        mode=PlanExecutionMode.APPLY,
        progress=None,
        stop_requested=stop_requested,
    )
    assert capsys.readouterr().out == ""

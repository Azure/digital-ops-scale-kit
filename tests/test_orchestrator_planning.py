# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for prepared plan construction from workspace inputs."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from siteops.executor import DeploymentResult, WaitResult
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    ArmTagWaitOperation,
    DataReference,
    DeploymentOperation,
    InputStatus,
    KubectlOperation,
    OperationIdentity,
    PlanDisposition,
    PlanIntent,
    PlanNotExecutableError,
    PlanStatus,
    resolve_plan_value,
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for directory in ("manifests", "parameters", "sites", "templates"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    (workspace / "sites" / "test-site.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "test-site",
                "subscription": "sub",
                "resourceGroup": "rg-test",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "templates" / "first.json").write_text(
        json.dumps({"parameters": {}}),
        encoding="utf-8",
    )
    return workspace


def _write_manifest(
    workspace: Path,
    steps: list[dict],
    *,
    parallel: int = 1,
) -> Path:
    path = workspace / "manifests" / "test.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Manifest",
                "name": "test",
                "sites": ["test-site"],
                "parallel": parallel,
                "steps": steps,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_executable_plan_prepares_parameters_and_data_references(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps(
            {
                "parameters": {
                    "input": {"type": "string"},
                    "message": {"type": "string"},
                }
            }
        ),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        yaml.safe_dump(
            {
                "input": "{{ steps.first.outputs.resource.id }}",
                "message": (
                    "resource={{ steps.first.outputs.resource.id }}"
                ),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
        ],
    )

    result = Orchestrator(workspace).build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    second = result.plan.targets[0].operations[1]
    assert isinstance(second.details, DeploymentOperation)
    assert second.details.input_status is InputStatus.PREPARED
    reference = DataReference(
        source=OperationIdentity(target="test-site", step="first"),
        output_path=("resource", "id"),
    )
    assert second.data_references == (reference,)
    assert second.details.parameters is not None
    assert resolve_plan_value(
        second.details.parameters,
        {
            reference.source: {
                "resource": {
                    "type": "Object",
                    "value": {"id": "resource-id"},
                },
            }
        },
    ) == {
        "input": "resource-id",
        "message": "resource=resource-id",
    }


def test_executable_plan_rejects_later_step_reference(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps({"parameters": {"input": {"type": "string"}}}),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        'input: "{{ steps.third.outputs.value }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
            {"name": "third", "template": "templates/first.json"},
        ],
    )

    result = Orchestrator(workspace).build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is not None
    assert (
        result.plan.targets[0].operations[1].disposition
        is PlanDisposition.BLOCKED
    )
    assert "not an available prior operation" in result.diagnostics[0].detail


def test_executable_plan_preserves_deferred_top_level_parameter_name(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps({"parameters": {"known": {"type": "string"}}}),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        '"{{ steps.first.outputs.parameterName }}": value\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
        ],
    )

    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    second = result.plan.targets[0].operations[1]
    assert isinstance(second.details, DeploymentOperation)
    assert second.details.accepted_parameters == ("known",)
    assert second.data_references == (
        DataReference(
            source=OperationIdentity(
                target="test-site",
                step="first",
            ),
            output_path=("parameterName",),
        ),
    )

    calls: list[dict] = []

    def deploy(**kwargs):
        calls.append(kwargs)
        return DeploymentResult(
            success=True,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
            outputs=(
                {
                    "parameterName": {
                        "type": "String",
                        "value": "known",
                    }
                }
                if kwargs["step_name"] == "first"
                else {}
            ),
        )

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=deploy,
    ):
        orchestrator.execute_plan(result)

    assert calls[1]["parameters"] == {"known": "value"}


def test_executable_plan_converts_filter_failure_to_typed_diagnostic(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )

    with patch(
        "siteops.orchestrator.get_template_parameters",
        side_effect=ValueError("compiler failed"),
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.diagnostics[0].code == "operation-preparation.invalid"
    assert result.diagnostics[0].summary == "Operation preparation failed."
    assert result.diagnostics[0].detail == "compiler failed"


def test_executable_plan_prepares_kubectl_and_wait_values(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "apply",
                "type": "kubectl",
                "operation": "apply",
                "arc": {
                    "name": "{{ steps.first.outputs.clusterName }}",
                    "resourceGroup": "{{ site.resourceGroup }}",
                },
                "files": ["config/{{ steps.first.outputs.fileName }}.yaml"],
            },
            {
                "name": "wait",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.first.outputs.resourceId }}",
                    "tagKey": "state",
                    "expectedValue": (
                        "ready-{{ steps.first.outputs.runId }}"
                    ),
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )

    result = Orchestrator(workspace).build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.executable
    assert result.plan is not None
    apply = result.plan.targets[0].operations[1]
    wait = result.plan.targets[0].operations[2]
    assert isinstance(apply.details, KubectlOperation)
    assert apply.details.input_status is InputStatus.PREPARED
    assert isinstance(wait.details, ArmTagWaitOperation)
    assert wait.details.input_status is InputStatus.PREPARED
    assert {
        reference.output_path
        for reference in apply.data_references
    } == {
        ("clusterName",),
        ("fileName",),
    }
    assert {
        reference.output_path
        for reference in wait.data_references
    } == {
        ("resourceId",),
        ("runId",),
    }


def test_build_plan_applies_parallel_override(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
        parallel=2,
    )

    result = Orchestrator(workspace).build_plan(
        manifest_path,
        parallel_override=4,
    )

    assert result.plan is not None
    assert result.plan.max_parallel_sites == 4


def test_execution_uses_prepared_values_after_workspace_changes(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "first.json").write_text(
        json.dumps({"parameters": {"input": {"type": "string"}}}),
        encoding="utf-8",
    )
    parameter_path = workspace / "parameters" / "first.yaml"
    parameter_path.write_text("input: original\n", encoding="utf-8")
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "first",
                "template": "templates/first.json",
                "parameters": ["parameters/first.yaml"],
            }
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )
    parameter_path.write_text("input: changed\n", encoding="utf-8")
    manifest_path.write_text("invalid: after-plan\n", encoding="utf-8")
    (workspace / "sites" / "test-site.yaml").write_text(
        "invalid: after-plan\n",
        encoding="utf-8",
    )

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        return_value=DeploymentResult(
            success=True,
            step_name="first",
            site_name="test-site",
            deployment_name="deployment",
        ),
    ) as deploy:
        orchestrator.execute_plan(result)

    assert deploy.call_args.kwargs["parameters"] == {"input": "original"}


def test_deploy_uses_supplied_plan_without_rebuilding(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )
    orchestrator = Orchestrator(workspace)
    prepared = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    with (
        patch.object(
            orchestrator,
            "build_plan",
            side_effect=AssertionError("deployment rebuilt the plan"),
        ),
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="deployment",
            ),
        ),
    ):
        result = orchestrator.deploy(
            manifest_path,
            plan_result=prepared,
        )

    assert result["summary"]["failed"] == 0


def test_cross_scope_execution_resolves_prepared_subscription_output(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    (workspace / "sites" / "global.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "global",
                "subscription": "sub",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "sites" / "edge.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "edge",
                "subscription": "sub",
                "resourceGroup": "rg-edge",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "templates" / "local.json").write_text(
        json.dumps({"parameters": {"input": {"type": "string"}}}),
        encoding="utf-8",
    )
    (workspace / "parameters" / "local.yaml").write_text(
        'input: "{{ steps.global.outputs.value }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "global",
                "template": "templates/first.json",
                "scope": "subscription",
            },
            {
                "name": "local",
                "template": "templates/local.json",
                "parameters": ["parameters/local.yaml"],
            },
        ],
    )
    manifest_data = yaml.safe_load(manifest_path.read_text())
    manifest_data["sites"] = ["global", "edge"]
    manifest_path.write_text(
        yaml.safe_dump(manifest_data, sort_keys=False),
        encoding="utf-8",
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    with (
        patch.object(
            orchestrator.executor,
            "deploy_subscription",
            return_value=DeploymentResult(
                success=True,
                step_name="global",
                site_name="global",
                deployment_name="global-deployment",
                outputs={
                    "value": {
                        "type": "String",
                        "value": "from-subscription",
                    }
                },
            ),
        ),
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="local",
                site_name="edge",
                deployment_name="local-deployment",
            ),
        ) as deploy_local,
    ):
        orchestrator.execute_plan(result)

    assert deploy_local.call_args.kwargs["parameters"] == {
        "input": "from-subscription"
    }


def test_later_subscription_failure_keeps_available_prior_output(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    (workspace / "sites" / "global.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "global",
                "subscription": "sub",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "sites" / "edge.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "edge",
                "subscription": "sub",
                "resourceGroup": "rg-edge",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "templates" / "local.json").write_text(
        json.dumps({"parameters": {"input": {"type": "string"}}}),
        encoding="utf-8",
    )
    (workspace / "parameters" / "local.yaml").write_text(
        'input: "{{ steps.global-first.outputs.value }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "global-first",
                "template": "templates/first.json",
                "scope": "subscription",
            },
            {
                "name": "global-second",
                "template": "templates/first.json",
                "scope": "subscription",
            },
            {
                "name": "local",
                "template": "templates/local.json",
                "parameters": ["parameters/local.yaml"],
            },
        ],
    )
    manifest_data = yaml.safe_load(manifest_path.read_text())
    manifest_data["sites"] = ["global", "edge"]
    manifest_path.write_text(
        yaml.safe_dump(manifest_data, sort_keys=False),
        encoding="utf-8",
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    def deploy_subscription(**kwargs):
        if kwargs["step_name"] == "global-first":
            return DeploymentResult(
                success=True,
                step_name="global-first",
                site_name="global",
                deployment_name="first",
                outputs={
                    "value": {
                        "type": "String",
                        "value": "available",
                    }
                },
            )
        return DeploymentResult(
            success=False,
            step_name="global-second",
            site_name="global",
            deployment_name="second",
            error="later subscription operation failed",
        )

    with (
        patch.object(
            orchestrator.executor,
            "deploy_subscription",
            side_effect=deploy_subscription,
        ),
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="local",
                site_name="edge",
                deployment_name="local",
            ),
        ) as deploy_local,
    ):
        execution = orchestrator.execute_plan(result)

    assert execution["sites"]["global"]["status"] == "failed"
    assert execution["sites"]["edge"]["status"] == "success"
    assert deploy_local.call_args.kwargs["parameters"] == {
        "input": "available"
    }


def test_runtime_wait_resolution_failure_is_reported_on_the_step(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "wait",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.first.outputs.resourceId }}",
                    "tagKey": "state",
                    "expectedValue": "ready",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="first",
            ),
        ),
        patch.object(
            orchestrator.executor,
            "wait_for_condition",
            side_effect=AssertionError("an unresolved wait must not poll"),
        ),
    ):
        execution = orchestrator.execute_plan(result)

    target = execution["sites"]["test-site"]
    assert target["status"] == "failed"
    assert target["steps_completed"] == 1
    assert [step["status"] for step in target["steps"]] == [
        "success",
        "failed",
    ]
    assert "has no available outputs" in target["steps"][1]["error"]


def test_wait_execution_resolves_site_and_arm_output_values(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "wait",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": (
                        "/subscriptions/{{ site.subscription }}/"
                        "resourceGroups/{{ site.resourceGroup }}/machines/"
                        "{{ steps.first.outputs.machineName }}"
                    ),
                    "tagKey": "state",
                    "expectedValue": "ready",
                    "failurePattern": "failed-*",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )
    captured = {}

    def wait_for_condition(condition, **kwargs):
        captured["condition"] = condition
        captured["subscription"] = kwargs["subscription"]
        return WaitResult(
            success=True,
            step_name="wait",
            site_name="test-site",
        )

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="first",
                outputs={
                    "machineName": {
                        "type": "String",
                        "value": "arc-machine",
                    }
                },
            ),
        ),
        patch.object(
            orchestrator.executor,
            "wait_for_condition",
            side_effect=wait_for_condition,
        ),
    ):
        execution = orchestrator.execute_plan(result)

    assert execution["sites"]["test-site"]["status"] == "success"
    assert captured["condition"].resource_id == (
        "/subscriptions/sub/resourceGroups/rg-test/machines/arc-machine"
    )
    assert captured["condition"].failure_pattern == "failed-*"
    assert captured["subscription"] == "sub"


def test_false_subscription_step_without_target_omits_phase_one(
    tmp_path,
    capsys,
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "global",
                "template": "templates/first.json",
                "scope": "subscription",
                "when": "{{ site.properties.enableGlobal == true }}",
            }
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    execution = orchestrator.execute_plan(result)

    assert execution["summary"]["failed"] == 0
    assert "[Phase 1]" not in capsys.readouterr().out


def test_prepared_kubectl_operation_fails_closed_on_unknown_action(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "apply",
                "type": "kubectl",
                "operation": "apply",
                "arc": {
                    "name": "cluster",
                    "resourceGroup": "rg-cluster",
                },
                "files": ["config.yaml"],
            }
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )
    assert result.plan is not None
    target = result.plan.targets[0]
    operation = target.operations[0]
    assert isinstance(operation.details, KubectlOperation)
    changed_operation = replace(
        operation,
        details=replace(operation.details, operation="delete"),
    )
    changed_target = replace(
        target,
        operations=(changed_operation,),
    )
    changed_result = replace(
        result,
        plan=replace(result.plan, targets=(changed_target,)),
    )

    with patch.object(
        orchestrator.executor,
        "kubectl_apply",
        side_effect=AssertionError("unknown action must not apply"),
    ):
        execution = orchestrator.execute_plan(changed_result)

    assert execution["sites"]["test-site"]["status"] == "failed"
    assert (
        execution["sites"]["test-site"]["steps"][0]["error"]
        == "Unsupported kubectl operation: delete"
    )


def test_executable_plan_requires_subscription_target(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "global",
                "template": "templates/first.json",
                "scope": "subscription",
            }
        ],
    )

    result = Orchestrator(workspace).build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.status is PlanStatus.INVALID
    assert result.diagnostics[0].code == "subscription-target.missing"
    assert not result.executable


def test_execute_plan_rejects_describe_plan(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(manifest_path)

    with pytest.raises(PlanNotExecutableError):
        orchestrator.execute_plan(result)


def test_dry_run_preserves_chained_outputs_for_command_preview(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps({"parameters": {"input": {"type": "string"}}}),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        'input: "{{ steps.first.outputs.value }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
        ],
    )
    orchestrator = Orchestrator(workspace, dry_run=True)
    calls: list[dict] = []

    def deploy(**kwargs):
        calls.append(kwargs)
        return DeploymentResult(
            success=True,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
        )

    with patch.object(
        orchestrator.executor,
        "deploy_resource_group",
        side_effect=deploy,
    ):
        result = orchestrator.deploy(manifest_path)

    assert result["summary"]["failed"] == 0
    assert calls[1]["parameters"] == {
        "input": "{{ steps.first.outputs.value }}"
    }


def test_dry_run_preserves_chained_wait_for_no_poll_preview(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "wait",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "{{ steps.first.outputs.resourceId }}",
                    "tagKey": "state",
                    "expectedValue": "ready",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    orchestrator = Orchestrator(workspace, dry_run=True)
    captured = {}

    def preview_wait(condition, **kwargs):
        captured["resource_id"] = condition.resource_id
        return WaitResult(
            success=True,
            step_name="wait",
            site_name="test-site",
        )

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="first",
            ),
        ),
        patch.object(
            orchestrator.executor,
            "wait_for_condition",
            side_effect=preview_wait,
        ),
    ):
        execution = orchestrator.deploy(manifest_path)

    assert execution["sites"]["test-site"]["status"] == "success"
    assert captured["resource_id"] == (
        "{{ steps.first.outputs.resourceId }}"
    )


def test_redacted_wait_validation_omits_resolved_output_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "wait",
                "type": "wait",
                "condition": {
                    "type": "arm-tag",
                    "resourceId": "/resource",
                    "tagKey": "state",
                    "expectedValue": "{{ steps.first.outputs.state }}",
                    "failurePattern": "private-*",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    orchestrator = Orchestrator(workspace)
    result = orchestrator.build_plan(
        manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

    with (
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="first",
                outputs={
                    "state": {
                        "type": "String",
                        "value": "private-secret-state",
                    }
                },
            ),
        ),
        patch.object(
            orchestrator.executor,
            "wait_for_condition",
            side_effect=AssertionError("invalid wait must not poll"),
        ),
    ):
        execution = orchestrator.execute_plan(result)

    target = execution["sites"]["test-site"]
    output = capsys.readouterr().out
    assert target["status"] == "failed"
    assert target["steps"][1]["error"] == (
        "The resolved wait condition is invalid."
    )
    assert "private-secret-state" not in output
    assert "private-secret-state" not in str(target["steps"][1])

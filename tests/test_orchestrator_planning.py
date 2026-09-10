# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for prepared plan construction from workspace inputs."""

import json
import subprocess
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops.compilation import (
    CompilationFailure,
    CompilationFailureCode,
    TemplateCompilationSession,
)
from siteops.executor import DeploymentResult, KubectlResult, WaitResult
from siteops.models import Manifest
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    ArmTagWaitOperation,
    CapabilityKind,
    CapabilityStatus,
    DataReference,
    DeploymentOperation,
    InputStatus,
    KubectlOperation,
    LiteralValue,
    OperationIdentity,
    OutputValue,
    PlanDisposition,
    PlanIntent,
    PlanNotExecutableError,
    PlanStatus,
    SkipReasonCode,
    resolve_plan_value,
)
from siteops.results import OperationStatus, RunStatus, SiteStatus


def _arm_template(parameters=None):
    return {
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": parameters or {},
        "resources": [],
    }


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
        json.dumps(_arm_template()),
        encoding="utf-8",
    )
    (workspace / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n",
        encoding="utf-8",
    )
    return workspace


def _write_manifest(
    workspace: Path,
    steps: list[dict],
    *,
    parallel: int = 1,
    sites: list[str] | None = None,
) -> Path:
    path = workspace / "manifests" / "test.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Manifest",
                "name": "test",
                "sites": sites or ["test-site"],
                "parallel": parallel,
                "steps": steps,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class _RecordingToolRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.compile_count = 0
        self.compile_returncode = 0
        self.compile_stderr = ""
        self.bicep_version_returncode = 0

    def __call__(
        self,
        argv: tuple[str, ...],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"azure-cli": "2.87.0"}),
                stderr="",
            )
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(
                argv,
                self.bicep_version_returncode,
                stdout="Bicep CLI version 0.45.15 (commit)",
                stderr="",
            )
        if argv[1:3] != ("bicep", "build"):
            raise AssertionError(f"Unexpected local tool invocation: {argv}")
        self.compile_count += 1
        output_path = Path(argv[argv.index("--outfile") + 1])
        if self.compile_returncode == 0:
            output_path.write_text(
                json.dumps(_arm_template()),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            argv,
            self.compile_returncode,
            stdout="",
            stderr=self.compile_stderr,
        )


class _VersionOnlyToolRunner:
    """Provide deterministic local version probes without execution."""

    def __call__(
        self,
        argv: tuple[str, ...],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"azure-cli": "test"}),
                stderr="",
            )
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Bicep CLI version test",
                stderr="",
            )
        raise AssertionError(
            f"Unexpected local tool invocation: {argv}"
        )


@pytest.fixture(autouse=True)
def _deterministic_local_tool_session(monkeypatch, tmp_path):
    """Give ordinary plans fresh host-independent local tool sessions."""

    def resolve_tool(name):
        if name not in {"az", "kubectl"}:
            raise AssertionError(f"Unexpected local tool resolution: {name}")
        return str((tmp_path / "tools" / name).resolve())

    def create_session():
        return TemplateCompilationSession(
            command_runner=_VersionOnlyToolRunner(),
            tool_resolver=resolve_tool,
        )

    monkeypatch.setattr(
        "siteops.orchestrator.TemplateCompilationSession",
        create_session,
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        MagicMock(side_effect=AssertionError("No real process launches")),
    )


def test_shared_bicep_compiles_once_across_targets_and_execution(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "sites" / "second-site.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "second-site",
                "subscription": "sub",
                "resourceGroup": "rg-second",
                "location": "eastus",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (workspace / "templates" / "shared.bicep").write_text(
        "param location string = resourceGroup().location\n",
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [{"name": "shared", "template": "templates/shared.bicep"}],
        sites=["test-site", "second-site"],
    )
    runner = _RecordingToolRunner()
    az_path = tmp_path / "tools" / "az.exe"
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: (
            str(az_path) if name == "az" else None
        ),
    )
    orchestrator = Orchestrator(workspace)

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    assert runner.compile_count == 1
    assert len(result.plan.template_units) == 1
    unit_keys = {
        operation.details.template_unit_key
        for target in result.plan.targets
        for operation in target.operations
        if isinstance(operation.details, DeploymentOperation)
    }
    assert unit_keys == {result.plan.template_units[0].key}
    capabilities = {
        capability.kind: capability
        for capability in result.plan.capabilities
    }
    assert capabilities[CapabilityKind.ARM_CONTROL_PLANE].status is (
        CapabilityStatus.AVAILABLE
    )
    assert capabilities[CapabilityKind.BICEP_COMPILER].status is (
        CapabilityStatus.AVAILABLE
    )

    def deploy(**kwargs):
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
        execution = orchestrator.execute_plan(result)

    assert execution.status is RunStatus.SUCCEEDED
    assert runner.compile_count == 1


def test_first_run_bicep_build_proves_executable_preflight(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "first.bicep").write_text(
        "targetScope = 'resourceGroup'\n", encoding="utf-8"
    )
    manifest_path = _write_manifest(
        workspace, [{"name": "first", "template": "templates/first.bicep"}]
    )
    runner = _RecordingToolRunner()
    runner.bicep_version_returncode = 1
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: str(tmp_path / "tools" / "az.exe"),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession", return_value=session
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path, intent=PlanIntent.EXECUTABLE
        )

    assert result.executable
    assert result.diagnostics == ()
    assert runner.compile_count == 1
    assert result.plan is not None
    assert len(result.plan.template_units) == 1
    compiler = next(
        capability
        for capability in result.plan.capabilities
        if capability.kind is CapabilityKind.BICEP_COMPILER
    )
    assert compiler.status is CapabilityStatus.AVAILABLE


def test_skipped_bicep_requires_no_tool_or_compilation(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "skipped.bicep").write_text(
        "param location string\n",
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "skipped",
                "template": "templates/skipped.bicep",
                "when": "{{ site.properties.enabled == true }}",
            }
        ],
    )
    session = MagicMock(spec=TemplateCompilationSession)

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    assert result.plan.template_units == ()
    assert result.plan.capabilities == ()
    assert (
        result.plan.targets[0].operations[0].disposition
        is PlanDisposition.SKIP
    )
    assert session.method_calls == []


@pytest.mark.parametrize("intent", list(PlanIntent))
@pytest.mark.parametrize("supplied_models", [False, True])
def test_engine_validates_loaded_inputs_before_tool_preflight(
    tmp_path, intent, supplied_models
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "apply",
                "type": "kubectl",
                "operation": "apply",
                "arc": {"name": "cluster", "resourceGroup": "rg"},
                "files": ["missing.yaml"],
            },
        ],
    )
    orchestrator = Orchestrator(workspace)
    manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
    sites = [orchestrator.load_site("test-site")]
    with (
        patch(
            "siteops.orchestrator.Manifest.from_file", return_value=manifest
        ) as load_manifest,
        patch.object(
            orchestrator, "resolve_sites", return_value=sites
        ) as resolve_sites,
        patch.object(
            orchestrator, "validate", wraps=orchestrator.validate
        ) as validate,
        patch(
            "siteops.orchestrator.TemplateCompilationSession",
            side_effect=AssertionError("Invalid inputs must not probe tools"),
        ),
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=intent,
            manifest=manifest if supplied_models else None,
            sites=sites if supplied_models else None,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is None
    assert "Kubectl file not found: missing.yaml" in result.diagnostics[0].detail
    assert load_manifest.call_count == (0 if supplied_models else 1)
    assert resolve_sites.call_count == (0 if supplied_models else 1)
    validate.assert_called_once_with(
        manifest_path, None, manifest=manifest, sites=sites
    )


@pytest.mark.parametrize(
    (
        "site_value",
        "setup",
        "expected_error",
    ),
    [
        pytest.param(
            "HTTPS://example.invalid/manifest.yaml",
            None,
            None,
            id="uppercase-https",
        ),
        pytest.param(
            "HtTpS://example.invalid/manifest.yaml",
            None,
            None,
            id="mixed-case-https",
        ),
        pytest.param(
            "HtTp://example.invalid/manifest.yaml",
            None,
            "HTTP URLs not allowed",
            id="http-rejected",
        ),
        pytest.param(
            ["config.yaml"],
            None,
            "resolved to a non-string value",
            id="non-string",
        ),
        pytest.param(
            "../outside.yaml",
            "outside-file",
            "must stay within the workspace",
            id="workspace-escape",
        ),
        pytest.param(
            None,
            None,
            "did not resolve for site",
            id="unresolved",
        ),
        pytest.param(
            "missing-resolved.yaml",
            None,
            "Kubectl file not found",
            id="missing-local",
        ),
        pytest.param(
            "config.yaml",
            None,
            None,
            id="local-file",
        ),
        pytest.param(
            "configs",
            "local-directory",
            None,
            id="local-directory",
        ),
        pytest.param(
            None,
            "prior-output",
            None,
            id="deferred-prior-output",
        ),
    ],
)
def test_executable_plan_preflights_known_kubectl_inputs_before_tools(
    tmp_path,
    site_value,
    setup,
    expected_error,
):
    workspace = _workspace(tmp_path)
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    if site_value is not None:
        site["properties"] = {"kubectlFile": site_value}
    site_path.write_text(
        yaml.safe_dump(site, sort_keys=False),
        encoding="utf-8",
    )
    if setup == "outside-file":
        (workspace.parent / "outside.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\n",
            encoding="utf-8",
        )
    elif setup == "local-directory":
        (workspace / "configs").mkdir()

    steps = []
    if setup == "prior-output":
        steps.append(
            {
                "name": "first",
                "template": "templates/first.json",
            }
        )
    declared_path = (
        "{{ steps.first.outputs.kubectlFile }}"
        if setup == "prior-output"
        else "{{ site.properties.kubectlFile }}"
    )
    steps.append(
        {
            "name": "apply",
            "type": "kubectl",
            "operation": "apply",
            "arc": {
                "name": "cluster",
                "resourceGroup": "rg-cluster",
            },
            "files": [declared_path],
        }
    )
    manifest_path = _write_manifest(
        workspace,
        steps,
    )

    if expected_error is not None:
        session_patch = patch(
            "siteops.orchestrator.TemplateCompilationSession",
            side_effect=AssertionError("Invalid input must not probe tools"),
        )
    else:
        runner = _RecordingToolRunner()
        session = TemplateCompilationSession(
            command_runner=runner,
            tool_resolver=lambda name: str(
                tmp_path / "tools" / f"{name}.exe"
            ),
        )
        session_patch = patch(
            "siteops.orchestrator.TemplateCompilationSession",
            return_value=session,
        )

    with session_patch:
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    if expected_error is not None:
        assert result.status is PlanStatus.INVALID
        assert not result.executable
        assert result.plan is None
        assert len(result.diagnostics) == 1
        assert expected_error in result.diagnostics[0].detail
        return

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    operation = result.plan.targets[0].operations[-1]
    assert isinstance(operation.details, KubectlOperation)
    file_value = operation.details.files[0]
    if setup == "prior-output":
        assert isinstance(file_value, OutputValue)
        assert file_value.reference.source == OperationIdentity(
            target="test-site",
            step="first",
        )
        assert file_value.reference.output_path == ("kubectlFile",)
    else:
        assert file_value == LiteralValue(site_value)
    assert [call[1:] for call in runner.calls] == [
        ("version", "--output", "json")
    ]


@pytest.mark.parametrize(
    "enabled",
    [None, False],
    ids=["absent", "false"],
)
def test_skipped_kubectl_does_not_require_site_input_or_tools(
    tmp_path,
    enabled,
):
    workspace = _workspace(tmp_path)
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    if enabled is not None:
        site["properties"] = {"enableOptional": enabled}
    site_path.write_text(
        yaml.safe_dump(site, sort_keys=False),
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "optional-apply",
                "type": "kubectl",
                "operation": "apply",
                "when": "{{ site.properties.enableOptional }}",
                "arc": {
                    "name": "cluster",
                    "resourceGroup": "rg-cluster",
                },
                "files": ["{{ site.parameters.optionalManifest }}"],
            },
        ],
    )
    resolutions = []

    def resolve_tool(name):
        resolutions.append(name)
        return str(tmp_path / "tools" / f"{name}.exe")

    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=resolve_tool,
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    first, optional = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.EXECUTE
    assert optional.disposition is PlanDisposition.SKIP
    assert optional.skip_reason is not None
    assert optional.skip_reason.code is SkipReasonCode.CONDITION_FALSE
    assert [capability.kind for capability in result.plan.capabilities] == [
        CapabilityKind.ARM_CONTROL_PLANE
    ]
    assert resolutions == ["az"]


def test_skipped_kubectl_still_rejects_authored_http_url(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "optional-apply",
                "type": "kubectl",
                "operation": "apply",
                "when": "{{ site.properties.enableOptional }}",
                "arc": {
                    "name": "cluster",
                    "resourceGroup": "rg-cluster",
                },
                "files": [
                    "HtTp://example.invalid/"
                    "{{ site.parameters.optionalManifest }}"
                ],
            }
        ],
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid inputs must not probe tools"),
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is None
    assert "HTTP URLs not allowed" in result.diagnostics[0].detail


def test_mixed_fleet_validates_only_applicable_kubectl_site_inputs(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    enabled_site = {
        "apiVersion": "siteops/v1",
        "kind": "Site",
        "name": "enabled-site",
        "subscription": "sub",
        "resourceGroup": "rg-enabled",
        "location": "eastus",
        "properties": {"enableOptional": True},
        "parameters": {"optionalManifest": "missing-enabled.yaml"},
    }
    (workspace / "sites" / "enabled-site.yaml").write_text(
        yaml.safe_dump(enabled_site, sort_keys=False),
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {"name": "first", "template": "templates/first.json"},
            {
                "name": "optional-apply",
                "type": "kubectl",
                "operation": "apply",
                "when": "{{ site.properties.enableOptional }}",
                "arc": {
                    "name": "cluster",
                    "resourceGroup": "rg-cluster",
                },
                "files": ["{{ site.parameters.optionalManifest }}"],
            },
        ],
        sites=["test-site", "enabled-site"],
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid inputs must not probe tools"),
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is None
    assert len(result.diagnostics) == 1
    assert "missing-enabled.yaml" in result.diagnostics[0].detail
    assert "enabled-site" in result.diagnostics[0].detail
    assert "test-site" not in result.diagnostics[0].detail


def test_kubectl_site_input_uses_shared_scope_applicability(tmp_path):
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
                "files": ["{{ site.parameters.optionalManifest }}"],
            }
        ],
    )
    orchestrator = Orchestrator(workspace)
    session = MagicMock(spec=TemplateCompilationSession)
    with (
        patch.object(
            orchestrator,
            "_check_step_site_compatibility",
            return_value="target scope mismatch",
        ) as compatibility,
        patch(
            "siteops.orchestrator.TemplateCompilationSession",
            return_value=session,
        ),
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    operation = result.plan.targets[0].operations[0]
    assert operation.disposition is PlanDisposition.SKIP
    assert operation.skip_reason is not None
    assert operation.skip_reason.code is SkipReasonCode.SCOPE_MISMATCH
    assert compatibility.call_count == 2
    assert session.method_calls == []


@pytest.mark.parametrize(
    ("case", "schema", "site_parameters", "executable"),
    [
        pytest.param(
            "missing",
            {"requiredName": {"type": "string"}},
            {},
            False,
            id="missing",
        ),
        pytest.param(
            "supplied",
            {"requiredName": {"type": "string"}},
            {"requiredName": "value", "unused": "filtered"},
            True,
            id="supplied",
        ),
        pytest.param(
            "defaulted",
            {
                "requiredName": {
                    "type": "string",
                    "nullable": True,
                    "defaultValue": None,
                }
            },
            {},
            True,
            id="default-null",
        ),
        pytest.param(
            "defaulted",
            {
                "requiredName": {
                    "type": "bool",
                    "defaultValue": False,
                }
            },
            {},
            True,
            id="default-false",
        ),
        pytest.param(
            "defaulted",
            {
                "requiredName": {
                    "type": "int",
                    "defaultValue": 0,
                }
            },
            {},
            True,
            id="default-zero",
        ),
        pytest.param(
            "nullable",
            {
                "requiredName": {
                    "type": "string",
                    "nullable": True,
                }
            },
            {},
            True,
            id="nullable-without-default",
        ),
    ],
)
def test_executable_plan_enforces_known_required_parameters(
    tmp_path,
    case,
    schema,
    site_parameters,
    executable,
):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "first.json").write_text(
        json.dumps(_arm_template(schema)),
        encoding="utf-8",
    )
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    site["parameters"] = site_parameters
    site_path.write_text(
        yaml.safe_dump(site, sort_keys=False),
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.executable is executable
    assert result.plan is not None
    operation = result.plan.targets[0].operations[0]
    assert isinstance(operation.details, DeploymentOperation)
    assert operation.details.template_unit_key is not None
    assert len(result.plan.template_units) == 1
    if case == "missing":
        assert result.status is PlanStatus.INVALID
        assert operation.disposition is PlanDisposition.BLOCKED
        assert operation.skip_reason is not None
        assert (
            operation.skip_reason.code
            is SkipReasonCode.TARGET_PREPARATION_FAILED
        )
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "operation-preparation.invalid"
        ]
        assert "requiredName" in result.diagnostics[0].detail
    else:
        assert result.status is PlanStatus.PLANNED
        assert operation.disposition is PlanDisposition.EXECUTE
        assert operation.details.parameters is not None
        parameters = resolve_plan_value(
            operation.details.parameters,
            {},
        )
        assert "unused" not in parameters


def test_missing_required_parameter_blocks_dependent_consumer(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "first.json").write_text(
        json.dumps(
            _arm_template({"requiredName": {"type": "string"}})
        ),
        encoding="utf-8",
    )
    (workspace / "templates" / "second.json").write_text(
        json.dumps(_arm_template({"input": {"type": "string"}})),
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
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is not None
    first, second = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.BLOCKED
    assert first.skip_reason is not None
    assert (
        first.skip_reason.code
        is SkipReasonCode.TARGET_PREPARATION_FAILED
    )
    assert second.disposition is PlanDisposition.BLOCKED
    assert second.skip_reason is not None
    assert second.skip_reason.code is SkipReasonCode.DEPENDENCY_BLOCKED
    assert isinstance(first.details, DeploymentOperation)
    assert isinstance(second.details, DeploymentOperation)
    assert first.details.template_unit_key is not None
    assert second.details.template_unit_key is not None
    assert len(result.plan.template_units) == 2


@pytest.mark.parametrize(
    "command", ["validate", "plan", "describe", "validate-plan", "deploy", "dry-run"]
)
def test_commands_load_and_validate_inputs_once(tmp_path, command):
    from siteops.cli import cmd_deploy, cmd_plan, cmd_validate

    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace, [{"name": "first", "template": "templates/first.json"}]
    )
    orchestrator = Orchestrator(workspace)
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(tmp_path / "tools" / "az.exe"),
    )
    args = Namespace(
        manifest=manifest_path,
        workspace=workspace,
        selector=None,
        parallel=None,
        output="plain",
        projection=None,
        verbose=False,
        describe=command == "describe",
        dry_run=command == "dry-run",
        plan=command == "validate-plan",
    )
    handler = (
        cmd_validate
        if command in {"validate", "validate-plan"}
        else cmd_deploy
        if command in {"deploy", "dry-run"}
        else cmd_plan
    )
    with (
        patch(
            "siteops.orchestrator.Manifest.from_file", wraps=Manifest.from_file
        ) as load_manifest,
        patch.object(
            orchestrator, "resolve_sites", wraps=orchestrator.resolve_sites
        ) as resolve_sites,
        patch.object(
            orchestrator, "validate", wraps=orchestrator.validate
        ) as validate,
        patch(
            "siteops.orchestrator.TemplateCompilationSession", return_value=session
        ),
        patch.object(
            orchestrator.executor,
            "deploy_resource_group",
            return_value=DeploymentResult(
                success=True,
                step_name="first",
                site_name="test-site",
                deployment_name="test",
            ),
        ) as submit,
    ):
        exit_code = handler(args, orchestrator)

    assert exit_code == 0
    assert load_manifest.call_count == 1
    assert resolve_sites.call_count == 1
    assert validate.call_count == 1
    assert isinstance(validate.call_args.kwargs["manifest"], Manifest)
    assert submit.call_count == (1 if command == "deploy" else 0)


def test_explicit_sites_do_not_inherit_failed_inventory_scope(tmp_path):
    workspace = _workspace(tmp_path)
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    site["labels"] = {"environment": "dev"}
    site_path.write_text(yaml.safe_dump(site), encoding="utf-8")
    (workspace / "sites" / "bad.yaml").write_text(
        "name: bad\n\tlabels:\n", encoding="utf-8"
    )
    manifest_path = _write_manifest(
        workspace, [{"name": "first", "template": "templates/first.json"}]
    )
    orchestrator = Orchestrator(workspace)
    inventory_plan = orchestrator.build_plan(
        manifest_path, "environment=dev", intent=PlanIntent.EXECUTABLE
    )
    assert inventory_plan.status is PlanStatus.INVALID
    assert inventory_plan.diagnostics[0].code == "plan.target-set-incomplete"
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(tmp_path / "tools" / "az.exe"),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession", return_value=session
    ):
        explicit_plan = orchestrator.build_plan(
            manifest_path, intent=PlanIntent.EXECUTABLE
        )

    assert explicit_plan.executable
    assert explicit_plan.plan is not None
    assert [target.name for target in explicit_plan.plan.targets] == ["test-site"]


@pytest.mark.parametrize("contents", ["[]", "false", "0", '"text"'])
def test_invalid_parameter_document_fails_before_compilation(tmp_path, contents):
    workspace = _workspace(tmp_path)
    (workspace / "parameters" / "input.yaml").write_text(
        contents, encoding="utf-8"
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "first",
                "template": "templates/first.json",
                "parameters": ["parameters/input.yaml"],
            }
        ],
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid input must not compile"),
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path, intent=PlanIntent.EXECUTABLE
        )

    assert result.status is PlanStatus.INVALID
    assert "must contain a mapping" in result.diagnostics[0].detail


@pytest.mark.parametrize("command", ["validate", "plan"])
def test_unexpected_validation_errors_are_not_invalid_input(
    tmp_path, command, capsys
):
    from siteops.cli import cmd_plan, cmd_validate

    workspace = _workspace(tmp_path)
    (workspace / "parameters" / "input.yaml").write_text(
        "name: example\n", encoding="utf-8"
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "first",
                "template": "templates/first.json",
                "parameters": ["parameters/input.yaml"],
            }
        ],
    )
    args = Namespace(
        manifest=manifest_path,
        workspace=workspace,
        selector=None,
        plan=False,
        output="json" if command == "plan" else "plain",
        projection=None,
    )
    orchestrator = Orchestrator(workspace)
    handler = cmd_validate if command == "validate" else cmd_plan
    with (
        patch.object(
            orchestrator, "load_parameters", side_effect=RuntimeError("internal failure")
        ),
        pytest.raises(RuntimeError, match="internal failure"),
    ):
        handler(args, orchestrator)

    assert capsys.readouterr().out == ""


def test_arm_json_requires_azure_cli_without_bicep(tmp_path):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )
    runner = _RecordingToolRunner()
    resolutions: list[str] = []
    az_path = tmp_path / "tools" / "az.exe"

    def resolve(name: str) -> str | None:
        resolutions.append(name)
        return str(az_path) if name == "az" else None

    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=resolve,
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    assert resolutions == ["az"]
    assert runner.compile_count == 0
    assert [call[1:] for call in runner.calls] == [
        ("version", "--output", "json")
    ]
    assert [
        (capability.kind, capability.status)
        for capability in result.plan.capabilities
    ] == [
        (
            CapabilityKind.ARM_CONTROL_PLANE,
            CapabilityStatus.AVAILABLE,
        )
    ]


def test_missing_kubectl_blocks_only_its_operation(tmp_path):
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
                    "name": "cluster",
                    "resourceGroup": "rg-cluster",
                },
                "files": ["config.yaml"],
            },
        ],
    )
    runner = _RecordingToolRunner()
    az_path = tmp_path / "tools" / "az.exe"
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: (
            str(az_path) if name == "az" else None
        ),
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.plan is not None
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "capability.kubectl.missing"
    ]
    first, apply = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.EXECUTE
    assert apply.disposition is PlanDisposition.BLOCKED
    assert isinstance(apply.details, KubectlOperation)
    assert apply.details.input_status is InputStatus.PREPARED
    assert apply.skip_reason is not None
    assert (
        apply.skip_reason.code
        is SkipReasonCode.CAPABILITY_UNAVAILABLE
    )
    capabilities = {
        capability.kind: capability.status
        for capability in result.plan.capabilities
    }
    assert capabilities == {
        CapabilityKind.ARM_CONTROL_PLANE: CapabilityStatus.AVAILABLE,
        CapabilityKind.KUBECTL: CapabilityStatus.MISSING,
        CapabilityKind.ARC_PROXY: CapabilityStatus.UNKNOWN,
    }


def test_execution_binds_preflight_tool_paths(tmp_path):
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
    runner = _RecordingToolRunner()
    az_path = tmp_path / "tools" / "az.exe"
    kubectl_path = tmp_path / "tools" / "kubectl.exe"
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: {
            "az": str(az_path),
            "kubectl": str(kubectl_path),
        }.get(name),
    )
    orchestrator = Orchestrator(workspace)
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )
    preflight_calls = tuple(runner.calls)

    with (
        patch.object(
            orchestrator.executor,
            "kubectl_apply",
            return_value=KubectlResult(
                success=True,
                step_name="apply",
                site_name="test-site",
            ),
        ),
    ):
        execution = orchestrator.execute_plan(result)

    assert orchestrator.executor.az_path == str(az_path.resolve())
    assert orchestrator.executor.kubectl_path == str(
        kubectl_path.resolve()
    )
    assert execution.status is RunStatus.SUCCEEDED
    assert tuple(runner.calls) == preflight_calls


def test_failed_template_dependency_blocks_consumer_without_cascade(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "first.bicep").write_text(
        "output resourceId string = 'resource-id'\n",
        encoding="utf-8",
    )
    (workspace / "templates" / "second.json").write_text(
        json.dumps(_arm_template({"input": {"type": "string"}})),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        'input: "{{ steps.first.outputs.resourceId }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "first",
                "template": "templates/first.bicep",
            },
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
        ],
    )
    runner = _RecordingToolRunner()
    runner.compile_returncode = 1
    runner.compile_stderr = "BCP000: invalid source"
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: (
            str(tmp_path / "tools" / "az.exe")
            if name == "az"
            else None
        ),
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "compilation.failed"
    ]
    assert result.plan is not None
    first, second = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.BLOCKED
    assert first.skip_reason is not None
    assert first.skip_reason.code is SkipReasonCode.COMPILATION_FAILED
    assert second.disposition is PlanDisposition.BLOCKED
    assert second.skip_reason is not None
    assert second.skip_reason.code is SkipReasonCode.DEPENDENCY_BLOCKED
    assert second.data_references == (
        DataReference(
            source=OperationIdentity(
                target="test-site",
                step="first",
            ),
            output_path=("resourceId",),
        ),
    )
    assert isinstance(second.details, DeploymentOperation)
    assert second.details.template_unit_key is not None
    assert len(result.plan.template_units) == 1


def test_skipped_template_dependency_has_one_typed_diagnostic(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps(_arm_template({"input": {"type": "string"}})),
        encoding="utf-8",
    )
    (workspace / "parameters" / "second.yaml").write_text(
        'input: "{{ steps.first.outputs.resourceId }}"\n',
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "first",
                "template": "templates/first.json",
                "when": "{{ site.properties.enabled == true }}",
            },
            {
                "name": "second",
                "template": "templates/second.json",
                "parameters": ["parameters/second.yaml"],
            },
        ],
    )
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: (
            str(tmp_path / "tools" / "az.exe")
            if name == "az"
            else None
        ),
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "operation.dependency-blocked"
    ]
    assert result.plan is not None
    first, second = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.SKIP
    assert second.disposition is PlanDisposition.BLOCKED
    assert second.skip_reason is not None
    assert second.skip_reason.code is SkipReasonCode.DEPENDENCY_BLOCKED


def test_executable_plan_prepares_parameters_and_data_references(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps(
            _arm_template(
                {
                    "input": {"type": "string"},
                    "message": {"type": "string"},
                }
            )
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
        json.dumps(_arm_template({"input": {"type": "string"}})),
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
    assert result.plan is None
    assert result.diagnostics[0].code == "validation.failed"
    assert "runs later" in result.diagnostics[0].detail


@pytest.mark.parametrize("ci_marker", ["GITHUB_ACTIONS", "TF_BUILD"])
@pytest.mark.parametrize(
    ("resolved_name", "execution_succeeds", "redacted"),
    [
        pytest.param("known", True, False, id="required-name"),
        pytest.param("defaulted", False, False, id="defaulted-name"),
        pytest.param("nullable", False, False, id="nullable-name"),
        pytest.param(
            "defaulted",
            False,
            True,
            id="defaulted-name-redacted",
        ),
    ],
)
def test_executable_plan_preserves_deferred_top_level_parameter_name(
    tmp_path,
    monkeypatch,
    resolved_name,
    execution_succeeds,
    redacted,
    ci_marker,
):
    monkeypatch.setenv(ci_marker, "true")
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    workspace = _workspace(tmp_path)
    (workspace / "templates" / "second.json").write_text(
        json.dumps(
            _arm_template(
                {
                    "known": {"type": "string"},
                    "defaulted": {
                        "type": "string",
                        "defaultValue": "default",
                    },
                    "nullable": {
                        "type": "string",
                        "nullable": True,
                    },
                }
            )
        ),
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
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    second = result.plan.targets[0].operations[1]
    assert isinstance(second.details, DeploymentOperation)
    assert second.details.template_unit_key is not None
    assert result.plan.template_unit(
        second.details.template_unit_key
    ).parameter_names == frozenset(
        {"known", "defaulted", "nullable"}
    )
    assert {
        parameter.name
        for parameter in result.plan.template_unit(
            second.details.template_unit_key
        ).parameters
        if parameter.is_required
    } == {"known"}
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
                        "value": resolved_name,
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
        execution = orchestrator.execute_plan(result)

    if execution_succeeds:
        assert execution.status is RunStatus.SUCCEEDED
        assert calls[1]["parameters"] == {"known": "value"}
    else:
        assert execution.status is RunStatus.FAILED
        assert len(calls) == 1
        second = execution.sites[0].operations[1]
        assert second.status is OperationStatus.FAILED
        assert second.reason is not None
        if redacted:
            assert second.reason.summary == "The operation failed."
            assert "known" not in second.reason.summary
            assert "known" in second.reason.local_message()
        else:
            assert (
                "missing required template parameter"
                in second.reason.local_message()
            )


def test_executable_plan_converts_filter_failure_to_typed_diagnostic(
    tmp_path,
):
    workspace = _workspace(tmp_path)
    manifest_path = _write_manifest(
        workspace,
        [{"name": "first", "template": "templates/first.json"}],
    )

    runner = _RecordingToolRunner()
    session = TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: (
            str(tmp_path / "tools" / "az.exe")
            if name == "az"
            else None
        ),
    )
    failure = CompilationFailure(
        code=CompilationFailureCode.FAILED,
        summary="Template compilation failed.",
        detail="compiler failed",
    )
    with (
        patch(
            "siteops.orchestrator.TemplateCompilationSession",
            return_value=session,
        ),
        patch.object(session, "acquire", return_value=failure),
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert not result.executable
    assert result.diagnostics[0].code == "compilation.failed"
    assert result.diagnostics[0].summary == "Template compilation failed."
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


@pytest.mark.parametrize(
    ("cluster_name", "resource_group", "error"),
    [
        pytest.param("cluster", "rg-cluster", None, id="valid"),
        pytest.param(
            ["cluster"],
            "rg-cluster",
            "must resolve to a scalar value",
            id="cluster-non-scalar",
        ),
        pytest.param(
            "cluster",
            "",
            "must resolve to a non-empty value",
            id="resource-group-empty",
        ),
    ],
)
def test_executable_plan_preflights_known_kubectl_scalar_inputs(
    tmp_path,
    cluster_name,
    resource_group,
    error,
):
    workspace = _workspace(tmp_path)
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    site["parameters"] = {
        "clusterName": cluster_name,
        "clusterResourceGroup": resource_group,
    }
    site_path.write_text(
        yaml.safe_dump(site, sort_keys=False),
        encoding="utf-8",
    )
    manifest_path = _write_manifest(
        workspace,
        [
            {
                "name": "apply",
                "type": "kubectl",
                "operation": "apply",
                "arc": {
                    "name": "{{ site.parameters.clusterName }}",
                    "resourceGroup": (
                        "{{ site.parameters.clusterResourceGroup }}"
                    ),
                },
                "files": ["https://example.invalid/manifest.yaml"],
            }
        ],
    )
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.plan is not None
    operation = result.plan.targets[0].operations[0]
    if error is None:
        assert result.status is PlanStatus.PLANNED
        assert result.executable
        assert operation.disposition is PlanDisposition.EXECUTE
    else:
        assert result.status is PlanStatus.INVALID
        assert not result.executable
        assert operation.disposition is PlanDisposition.BLOCKED
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "operation-preparation.invalid"
        ]
        assert error in result.diagnostics[0].detail


@pytest.mark.parametrize(
    ("expected_value", "error"),
    [
        pytest.param("succeeded", None, id="non-overlapping"),
        pytest.param(
            "failed-check",
            "also matches failurePattern",
            id="overlap",
        ),
        pytest.param(
            ["failed-check"],
            "must resolve to a scalar value",
            id="non-scalar",
        ),
        pytest.param(
            "",
            "must resolve to a non-empty value",
            id="empty",
        ),
    ],
)
def test_executable_plan_validates_fully_known_wait_values(
    tmp_path,
    expected_value,
    error,
):
    workspace = _workspace(tmp_path)
    site_path = workspace / "sites" / "test-site.yaml"
    site = yaml.safe_load(site_path.read_text(encoding="utf-8"))
    site["parameters"] = {"waitExpected": expected_value}
    site_path.write_text(
        yaml.safe_dump(site, sort_keys=False),
        encoding="utf-8",
    )
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
                        "/subscriptions/sub/resourceGroups/rg-test/"
                        "providers/Microsoft.Example/items/example"
                    ),
                    "tagKey": "state",
                    "expectedValue": "{{ site.parameters.waitExpected }}",
                    "failurePattern": "failed-*",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = Orchestrator(workspace).build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.plan is not None
    first, wait = result.plan.targets[0].operations
    assert first.disposition is PlanDisposition.EXECUTE
    assert isinstance(first.details, DeploymentOperation)
    assert first.details.template_unit_key is not None
    assert len(result.plan.template_units) == 1
    if error is None:
        assert result.status is PlanStatus.PLANNED
        assert result.executable
        assert wait.disposition is PlanDisposition.EXECUTE
    else:
        assert result.status is PlanStatus.INVALID
        assert not result.executable
        assert wait.disposition is PlanDisposition.BLOCKED
        assert [diagnostic.code for diagnostic in result.diagnostics] == [
            "operation-preparation.invalid"
        ]
        assert error in result.diagnostics[0].detail


@pytest.mark.parametrize("ci_marker", ["GITHUB_ACTIONS", "TF_BUILD"])
@pytest.mark.parametrize(
    ("resolved_value", "execution_succeeds"),
    [
        pytest.param("succeeded", True, id="non-overlapping"),
        pytest.param("failed-check", False, id="overlap"),
    ],
)
def test_deferred_wait_guard_runs_after_arm_output_resolution(
    tmp_path,
    monkeypatch,
    resolved_value,
    execution_succeeds,
    ci_marker,
):
    monkeypatch.setenv(ci_marker, "true")
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
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
                        "/subscriptions/sub/resourceGroups/rg-test/"
                        "providers/Microsoft.Example/items/example"
                    ),
                    "tagKey": "state",
                    "expectedValue": (
                        "{{ steps.first.outputs.waitExpected }}"
                    ),
                    "failurePattern": "failed-*",
                },
                "timeoutMinutes": 5,
                "pollIntervalSeconds": 10,
            },
        ],
    )
    orchestrator = Orchestrator(workspace)
    session = TemplateCompilationSession(
        command_runner=_RecordingToolRunner(),
        tool_resolver=lambda name: str(
            tmp_path / "tools" / f"{name}.exe"
        ),
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = orchestrator.build_plan(
            manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    waits = []

    def wait_for_condition(condition, **kwargs):
        waits.append(condition)
        return WaitResult(
            success=True,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
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
                    "waitExpected": {
                        "type": "String",
                        "value": resolved_value,
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

    if execution_succeeds:
        assert execution.status is RunStatus.SUCCEEDED
        assert len(waits) == 1
        assert waits[0].expected_value == "succeeded"
    else:
        assert execution.status is RunStatus.FAILED
        assert waits == []
        wait = execution.sites[0].operations[1]
        assert wait.status is OperationStatus.FAILED
        assert wait.reason is not None
        assert "also matches failurePattern" in wait.reason.local_message()


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
        json.dumps(_arm_template({"input": {"type": "string"}})),
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

    assert result.status is RunStatus.SUCCEEDED


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
        json.dumps(_arm_template({"input": {"type": "string"}})),
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
        json.dumps(_arm_template({"input": {"type": "string"}})),
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

    by_site = {site.target: site for site in execution.sites}
    assert by_site["global"].status is SiteStatus.FAILED
    assert by_site["edge"].status is SiteStatus.SUCCEEDED
    assert by_site["global"].operations[0].status is (
        OperationStatus.SUCCEEDED
    )
    assert (
        by_site["global"].operations[0].copy_outputs()["value"][
            "value"
        ]
        == "available"
    )
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

    target = execution.sites[0]
    assert target.target == "test-site"
    assert target.status is SiteStatus.FAILED
    assert [operation.status for operation in target.operations] == [
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
    ]
    assert target.operations[1].reason is not None
    assert (
        "has no available outputs"
        in target.operations[1].reason.local_message()
    )


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

    assert execution.sites[0].status is SiteStatus.SUCCEEDED
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

    assert execution.status is RunStatus.SKIPPED
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

    operation = execution.sites[0].operations[0]
    assert execution.sites[0].status is SiteStatus.FAILED
    assert operation.reason is not None
    assert (
        operation.reason.local_message()
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
    assert result.diagnostics[0].code == "validation.failed"
    assert "no subscription-level site" in result.diagnostics[0].detail
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
        json.dumps(_arm_template({"input": {"type": "string"}})),
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

    assert result.status is RunStatus.SUCCEEDED
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

    assert execution.sites[0].status is SiteStatus.SUCCEEDED
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

    target = execution.sites[0]
    output = capsys.readouterr().out
    assert target.status is SiteStatus.FAILED
    assert target.operations[1].reason is not None
    assert (
        "private-secret-state"
        in target.operations[1].reason.local_message()
    )
    assert "private-secret-state" not in target.operations[1].reason.summary
    assert "private-secret-state" not in output
    assert "private-secret-state" not in repr(target.operations[1])

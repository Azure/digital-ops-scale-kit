"""End-to-end planning and execution tests for materialized workspace packages."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from siteops import package_builder
from siteops import workspace_package as package
from siteops.artifacts import ArtifactError, PayloadFile
from siteops.compilation import (
    DependencyCoverage,
    TemplateCompilationSession,
    TemplateKind,
)
from siteops.executor import DeploymentResult, KubectlResult
from siteops.models import DeploymentStep, Manifest
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    CapabilityKind,
    CompilationBinding,
    DeploymentOperation,
    PlanIntent,
    PlanProjection,
    PlanStatus,
    SubmissionMode,
    serialize_plan,
)
from siteops.results import RunStatus


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


class _PackageCompiler:
    def __call__(self, argv, timeout):
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
                0,
                stdout="Bicep CLI version 0.45.15 (fixture)",
                stderr="",
            )
        if argv[1:3] != ("bicep", "build"):
            raise AssertionError(f"Unexpected producer command: {argv}")
        output_path = Path(argv[argv.index("--outfile") + 1])
        output_path.write_text(
            json.dumps(
                _arm_template({"name": {"type": "string"}})
                | {
                    "metadata": {
                        "_generator": {
                            "name": "bicep",
                            "version": "0.45.15.0",
                            "templateHash": "fixture-root",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="",
            stderr="",
        )


class _ConsumerRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"azure-cli": "test"}),
                stderr="",
            )
        raise AssertionError("Acquired execution must not invoke Bicep.")


class _RecordingExecutor:
    def __init__(self, *, deployment_outputs=None):
        self.bound = None
        self.deployments = []
        self.kubectl = []
        self.closed = False
        self.deployment_outputs = deployment_outputs or {}

    def bind_tool_paths(self, *, azure_cli=None, kubectl=None):
        self.bound = (azure_cli, kubectl)

    def deploy_resource_group(self, **kwargs):
        self.deployments.append(kwargs)
        return DeploymentResult(
            success=True,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
            outputs=self.deployment_outputs.get(kwargs["step_name"], {}),
        )

    def deploy_subscription(self, **kwargs):
        return self.deploy_resource_group(**kwargs)

    def kubectl_apply(self, **kwargs):
        self.kubectl.append(kwargs)
        return KubectlResult(
            success=True,
            step_name=kwargs["step_name"],
            site_name=kwargs["site_name"],
        )

    def close(self):
        self.closed = True


def _operator_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "sites").mkdir(parents=True)
    (project / "sites.local").mkdir()
    (project / "sites" / "operator.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "operator",
                "subscription": "sub",
                "resourceGroup": "operator-rg",
                "location": "eastus",
                "labels": {"role": "target"},
                "parameters": {"name": "base"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project / "sites.local" / "operator.yaml").write_text(
        yaml.safe_dump(
            {"parameters": {"name": "overlay"}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return project


def _consumer_session(tmp_path: Path, runner: _ConsumerRunner):
    return TemplateCompilationSession(
        command_runner=runner,
        tool_resolver=lambda name: str(
            (tmp_path / "tools" / f"{name}.exe").resolve()
        ),
    )


def _compiled_binding(tmp_path: Path):
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "compiled-package-workspace"
    )
    shutil.copytree(fixture, snapshot / "workspace")
    workspace = snapshot / "workspace"
    manifest_path = workspace / "manifests" / "example" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["selector"] = "role=target"
    manifest["parameters"] = ["parameters/name.yaml"]
    manifest["steps"].append(
        {
            "name": "native",
            "template": "templates/native.json",
            "scope": "resourceGroup",
        }
    )
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    (workspace / "parameters").mkdir()
    (workspace / "parameters" / "name.yaml").write_text(
        "name: default\n",
        encoding="utf-8",
    )
    (workspace / "templates" / "native.json").write_text(
        json.dumps(_arm_template({"name": {"type": "string"}})),
        encoding="utf-8",
    )
    (workspace / "sites").mkdir()
    (workspace / "sites" / "packaged.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "packaged",
                "subscription": "package-sub",
                "resourceGroup": "package-rg",
                "location": "westus",
                "labels": {"role": "target"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    compiler = _PackageCompiler()
    tools = tmp_path / "producer-tools"
    tools.mkdir()
    azure_cli = tools / "az.cmd"
    bicep = tools / "bicep.exe"
    azure_cli.write_bytes(b"fixture")
    bicep.write_bytes(b"fixture")
    inspection = package_builder.build_package(
        snapshot,
        tmp_path / "package.zip",
        workspace="workspace",
        kit_id="example/acquired",
        version="preview-1",
        source_revision="fixture",
        siteops_range=">=1.0.0b1,<2",
        compilation_session_factory=lambda: (
            package_builder.create_producer_compilation_session(
                snapshot,
                control_root,
                azure_cli_path=azure_cli,
                bicep_path=bicep,
                command_runner=compiler,
            )
        ),
    )
    materialized = tmp_path / "materialized"
    package.extract_package(
        tmp_path / "package.zip",
        inspection.sha256,
        materialized,
    )
    binding = package.MaterializedPackageBinding.bind(
        inspection,
        materialized,
        "compiled-package-example",
    )
    return binding


def _manual_binding(
    tmp_path: Path,
    files: dict[str, bytes],
    mappings: tuple[package.PackageTemplateMapping, ...] = (),
    *,
    manifest: str = "manifests/main.yaml",
):
    payload = {
        f"workspace/{path}": content
        for path, content in files.items()
    }
    records = tuple(
        PayloadFile(
            path,
            hashlib.sha256(content).hexdigest(),
            len(content),
        )
        for path, content in payload.items()
    )
    metadata = package.WorkspacePackage(
        "example/manual",
        "preview-1",
        "fixture",
        "workspace",
        ">=1.0.0b1,<2",
        ("manifest/v1", package.COMPILED_TEMPLATE_FEATURE),
        records,
        package.workspace_tree_digest(records, "workspace"),
        mappings,
    )
    metadata = package.WorkspacePackage.from_document(metadata.document())
    archive_path = tmp_path / "manual.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            package_builder._zip_info(
                package.PACKAGE_NAME,
                zipfile.ZIP_STORED,
            ),
            package.json_bytes(metadata.document()),
        )
        for path, content in payload.items():
            archive.writestr(
                package_builder._zip_info(path, zipfile.ZIP_STORED),
                content,
            )
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    inspection = package.inspect_package(archive_path, digest)
    materialized = tmp_path / "manual-materialized"
    package.extract_package(archive_path, digest, materialized)
    return package.MaterializedPackageBinding.bind(
        inspection,
        materialized,
        manifest,
    )


def _native_mapping(path: str, content: bytes):
    digest = hashlib.sha256(content).hexdigest()
    return package.PackageTemplateMapping(
        source_path=path,
        source_kind=TemplateKind.ARM_JSON,
        source_sha256=digest,
        source_size=len(content),
        artifact_path=path,
        artifact_sha256=digest,
        artifact_size=len(content),
        producer_mode="native-arm-json",
        invocation=("read-arm-json",),
        driver=None,
        compiler=None,
        configuration=None,
        dependencies=package.PackageDependencyIdentity(
            DependencyCoverage.NOT_APPLICABLE
        ),
    )


def test_acquired_plan_and_deploy_use_mapped_arm_artifacts(tmp_path):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    provider = _RecordingExecutor()
    runner = _ConsumerRunner()
    session = _consumer_session(tmp_path, runner)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
        executor=provider,
    )

    with (
        patch(
            "siteops.orchestrator.TemplateCompilationSession",
            return_value=session,
        ),
        patch(
            "siteops.executor.subprocess.Popen",
            side_effect=AssertionError("No live subprocess is allowed."),
        ),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )
        execution = orchestrator.deploy(
            binding.manifest_path,
            plan_result=result,
        )

    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    assert result.plan.submission_mode is SubmissionMode.ARM_JSON
    assert (
        result.plan.compilation_binding
        is CompilationBinding.PACKAGE_ARTIFACT
    )
    assert [target.name for target in result.plan.targets] == ["operator"]
    assert CapabilityKind.BICEP_COMPILER not in {
        capability.kind
        for capability in result.plan.capabilities
    }
    operations = result.plan.targets[0].operations
    assert isinstance(operations[0].details, DeploymentOperation)
    assert operations[0].details.template == (
        binding.workspace / "templates" / "main.bicep"
    )
    assert operations[0].details.effective_template == (
        binding.workspace
        / ".siteops"
        / "compiled"
        / "v1"
        / "templates"
        / "main.bicep.json"
    )
    assert operations[1].details.effective_template is None
    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="test",
    )
    assert document["plan"]["submission"] == {
        "mode": "arm-json",
        "compilationBinding": "package-artifact",
    }
    first_details = document["plan"]["targets"][0]["operations"][0][
        "details"
    ]
    assert first_details["templatePath"].endswith(
        "templates/main.bicep"
    )
    assert first_details["effectiveTemplatePath"].endswith(
        ".siteops/compiled/v1/templates/main.bicep.json"
    )
    assert execution.status is RunStatus.SUCCEEDED
    assert [call["parameters"] for call in provider.deployments] == [
        {"name": "overlay"},
        {"name": "overlay"},
    ]
    assert [Path(call["template_path"]).suffix for call in provider.deployments] == [
        ".json",
        ".json",
    ]
    assert ".siteops" in provider.deployments[0]["template_path"].parts
    assert provider.deployments[1]["template_path"] == (
        binding.workspace / "templates" / "native.json"
    )
    assert all(call[1:] == ("version", "--output", "json") for call in runner.calls)
    assert provider.closed


def test_acquired_execution_requires_operator_site_root(tmp_path):
    binding = _compiled_binding(tmp_path)

    with pytest.raises(ValueError, match="separate operator Site"):
        Orchestrator(
            binding.workspace,
            materialized_package=binding,
        )


def test_missing_template_mapping_fails_before_tool_preflight(tmp_path):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    manifest = Manifest.from_file(
        binding.manifest_path,
        workspace_root=binding.workspace,
    )
    deployment = next(
        step
        for step in manifest.steps
        if isinstance(step, DeploymentStep)
    )
    deployment.template = "templates/unused-module.bicep"
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
    )
    sites = [orchestrator.load_site("operator")]

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid mapping must not probe tools."),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
            manifest=manifest,
            sites=sites,
        )

    assert result.status is PlanStatus.INVALID
    assert "no compiled artifact mapping" in result.diagnostics[0].detail


@pytest.mark.parametrize(
    "mutation",
    ["manifest-parameter", "step-parameter", "template"],
)
def test_acquired_loaded_models_reject_fixed_paths_outside_package(
    tmp_path,
    mutation,
):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("{}\n", encoding="utf-8")
    manifest = Manifest.from_file(
        binding.manifest_path,
        workspace_root=binding.workspace,
    )
    deployment = next(
        step
        for step in manifest.steps
        if isinstance(step, DeploymentStep)
    )
    if mutation == "manifest-parameter":
        manifest.parameters = ["../outside.yaml"]
    elif mutation == "step-parameter":
        deployment.parameters = [str(outside)]
    else:
        deployment.template = str(outside)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
    )
    sites = [orchestrator.load_site("operator")]

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid inputs must not probe tools."),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
            manifest=manifest,
            sites=sites,
        )

    assert result.status is PlanStatus.INVALID
    assert "package-relative path" in result.diagnostics[0].detail


def test_acquired_include_cannot_escape_materialized_workspace(tmp_path):
    manifest = yaml.safe_dump(
        {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "escape",
            "sites": ["operator"],
            "steps": [{"include": "../../outside.yaml"}],
        },
        sort_keys=False,
    ).encode()
    binding = _manual_binding(
        tmp_path,
        {"manifests/main.yaml": manifest},
    )
    project = _operator_project(tmp_path)

    result = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
    ).build_plan(
        binding.manifest_path,
        intent=PlanIntent.EXECUTABLE,
    )

    assert result.status is PlanStatus.INVALID
    assert "resolves outside the workspace root" in result.diagnostics[0].detail


def test_unexpected_materialized_file_stops_build_plan(tmp_path):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
    )
    (binding.workspace / "unexpected.yaml").write_text(
        "kind: ConfigMap\n",
        encoding="utf-8",
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Unexpected files must not probe tools."),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert "path inventory" in result.diagnostics[0].detail


def test_changed_artifact_stops_build_plan_before_tools(tmp_path):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
    )
    artifact = next(
        entry.artifact_path
        for entry in binding.inspection.metadata.templates
        if entry.source_kind is TemplateKind.BICEP
    )
    (binding.workspace / Path(*artifact.split("/"))).write_text(
        json.dumps(_arm_template()),
        encoding="utf-8",
    )

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Changed artifacts must not probe tools."),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert "declared identity" in result.diagnostics[0].detail


def test_changed_artifact_stops_deploy_before_provider_mutation(tmp_path):
    binding = _compiled_binding(tmp_path)
    project = _operator_project(tmp_path)
    provider = _RecordingExecutor()
    runner = _ConsumerRunner()
    session = _consumer_session(tmp_path, runner)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
        executor=provider,
    )
    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        return_value=session,
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )
    artifact = next(
        entry.artifact_path
        for entry in binding.inspection.metadata.templates
        if entry.source_kind is TemplateKind.BICEP
    )
    (binding.workspace / Path(*artifact.split("/"))).write_text(
        json.dumps(_arm_template()),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactError, match="declared identity"):
        orchestrator.deploy(
            binding.manifest_path,
            plan_result=result,
        )

    assert provider.deployments == []
    assert provider.kubectl == []
    assert provider.closed


def test_acquired_static_kubectl_url_fails_before_tool_preflight(tmp_path):
    manifest = yaml.safe_dump(
        {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "remote-kubectl",
            "selector": "role=target",
            "steps": [
                {
                    "name": "apply",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"name": "cluster", "resourceGroup": "rg"},
                    "files": ["https://example.invalid/manifest.yaml"],
                }
            ],
        },
        sort_keys=False,
    ).encode()
    binding = _manual_binding(
        tmp_path,
        {"manifests/main.yaml": manifest},
    )
    project = _operator_project(tmp_path)

    with patch(
        "siteops.orchestrator.TemplateCompilationSession",
        side_effect=AssertionError("Invalid kubectl input must not probe tools."),
    ):
        result = Orchestrator(
            binding.workspace,
            site_config_root=project,
            materialized_package=binding,
        ).build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )

    assert result.status is PlanStatus.INVALID
    assert "not remote URLs" in result.diagnostics[0].detail


@pytest.mark.parametrize(
    ("runtime_path", "expected_status", "kubectl_calls"),
    [
        ("config.yaml", RunStatus.SUCCEEDED, 1),
        ("https://example.invalid/config.yaml", RunStatus.FAILED, 0),
        ("../outside.yaml", RunStatus.FAILED, 0),
    ],
)
def test_acquired_runtime_kubectl_paths_are_checked_before_provider_mutation(
    tmp_path,
    runtime_path,
    expected_status,
    kubectl_calls,
):
    template = json.dumps(_arm_template()).encode()
    manifest = yaml.safe_dump(
        {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "runtime-kubectl",
            "selector": "role=target",
            "steps": [
                {
                    "name": "produce",
                    "template": "templates/producer.json",
                },
                {
                    "name": "apply",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"name": "cluster", "resourceGroup": "rg"},
                    "files": ["{{ steps.produce.outputs.manifestPath }}"],
                },
            ],
        },
        sort_keys=False,
    ).encode()
    binding = _manual_binding(
        tmp_path,
        {
            "manifests/main.yaml": manifest,
            "templates/producer.json": template,
            "config.yaml": b"apiVersion: v1\nkind: ConfigMap\n",
        },
        (_native_mapping("templates/producer.json", template),),
    )
    project = _operator_project(tmp_path)
    provider = _RecordingExecutor(
        deployment_outputs={
            "produce": {
                "manifestPath": {
                    "type": "String",
                    "value": runtime_path,
                }
            }
        }
    )
    runner = _ConsumerRunner()
    session = _consumer_session(tmp_path, runner)
    orchestrator = Orchestrator(
        binding.workspace,
        site_config_root=project,
        materialized_package=binding,
        executor=provider,
    )

    with (
        patch(
            "siteops.orchestrator.TemplateCompilationSession",
            return_value=session,
        ),
        patch(
            "siteops.executor.subprocess.Popen",
            side_effect=AssertionError("No live subprocess is allowed."),
        ),
    ):
        result = orchestrator.build_plan(
            binding.manifest_path,
            intent=PlanIntent.EXECUTABLE,
        )
        execution = orchestrator.deploy(
            binding.manifest_path,
            plan_result=result,
        )

    assert result.executable
    assert execution.status is expected_status
    assert len(provider.kubectl) == kubectl_calls
    if provider.kubectl:
        assert provider.kubectl[0]["files"] == ["config.yaml"]

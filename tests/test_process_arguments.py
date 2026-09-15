"""Literal argument delivery through native Windows batch launchers."""

import json
import os
import sys
import time
from unittest.mock import patch

import pytest

from siteops import package_builder
from siteops.compilation import TemplateCompilationSession, _run_command
from siteops.executor import AzCliExecutor
from siteops.orchestrator import Orchestrator
from siteops.planning import PlanIntent
from siteops.process_args import prepare_process_args
from siteops.results import RunStatus
from siteops.runtime import RuntimePaths


@pytest.fixture
def batch_tool(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Native Windows batch invocation")
    recorder = tmp_path / "argv.py"
    result_path = tmp_path / "argv.json"
    recorder.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "result = json.dumps(sys.argv[1:])\n"
        "Path(os.environ['SITEOPS_TEST_ARGV_PATH']).write_text(result, encoding='utf-8')\n"
        "print(result)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "tool.CMD"
    launcher.write_text(
        '@echo off\n'
        '@IF EXIST "%~dp0argv.py" (\n'
        f'  "{sys.executable}" "%~dp0argv.py" %*\n'
        ')\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SITEOPS_TEST_ARGV_PATH", str(result_path))
    monkeypatch.setenv("SITEOPS_TEST_EXPANSION", "OWNED_EXPANSION")
    return launcher, result_path


def _invoke(boundary, launcher, arguments):
    if boundary == "compiler":
        result = _run_command((str(launcher), *arguments), 10)
        return result.returncode == 0, result.stdout, result.stderr
    executor = AzCliExecutor(launcher.parent)
    executor.bind_tool_paths(azure_cli=launcher, kubectl=launcher)
    try:
        if boundary == "azure":
            return executor._run_az(arguments, timeout=10)
        return executor._run_kubectl(arguments, timeout=10)
    finally:
        executor.close()


@pytest.mark.parametrize("boundary", ["azure", "compiler", "kubectl"])
@pytest.mark.parametrize("value", [
    "ordinary",
    "template&echo.SITEOPS_TEST_INJECTED&rem.json",
    "literal|echo.SITEOPS_TEST_INJECTED",
    "parentheses(template)",
    "caret^value",
    "literal!SITEOPS_TEST_EXPANSION!",
    "directory\\",
    "directory with spaces\\",
    "",
])
def test_batch_arguments_reach_the_native_program_literally(batch_tool, boundary, value):
    launcher, result_path = batch_tool
    arguments = ["--input", value]
    success, stdout, stderr = _invoke(boundary, launcher, arguments)
    assert success, stderr
    assert json.loads(stdout) == arguments
    assert json.loads(result_path.read_text(encoding="utf-8")) == arguments


@pytest.mark.parametrize("boundary", ["azure", "compiler", "kubectl"])
@pytest.mark.parametrize("value", [
    "%SITEOPS_TEST_EXPANSION%",
    'literal"&echo.SITEOPS_TEST_INJECTED&"',
    "first\nsecond",
    pytest.param("x" * 9000, id="command-too-long"),
])
def test_unsupported_batch_text_fails_before_starting_a_process(batch_tool, boundary, value):
    launcher, result_path = batch_tool
    if boundary == "compiler":
        with pytest.raises(OSError, match="Windows batch"):
            _invoke(boundary, launcher, ["--input", value])
    else:
        success, stdout, stderr = _invoke(boundary, launcher, ["--input", value])
        assert not success
        assert stdout == ""
        assert "Windows batch" in stderr
        assert value not in stderr
    assert not result_path.exists()


def test_arc_proxy_batch_arguments_remain_literal(batch_tool, tmp_path):
    launcher, result_path = batch_tool
    cluster = "cluster&echo.SITEOPS_TEST_INJECTED&rem"
    executor = AzCliExecutor(
        tmp_path,
        runtime_paths=RuntimePaths(temp_root=tmp_path / "scratch"),
    )
    executor.bind_tool_paths(azure_cli=launcher)

    def observed(*args, **kwargs):
        deadline = time.monotonic() + 10
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert result_path.exists(), "The native recorder did not run."
        return False

    try:
        with (
            patch("siteops.executor._allocate_arc_port_slot", return_value=31001),
            patch("siteops.executor._release_arc_port_slot"),
            patch("siteops.executor._probe_arc_proxy_ready", side_effect=observed),
            executor._arc_proxy(cluster, "group", "subscription") as kubeconfig,
        ):
            assert kubeconfig is None
        arguments = json.loads(result_path.read_text(encoding="utf-8"))
        assert arguments[arguments.index("-n") + 1] == cluster
        assert "--file" in arguments
    finally:
        executor.close()


@pytest.mark.parametrize("value", [
    "literal&pipe|percent%",
    'literal"quote',
    "line\nvalue",
])
def test_native_executable_arguments_remain_unrestricted(value):
    result = _run_command((
        sys.executable, "-c",
        "import json, sys; print(json.dumps(sys.argv[1:]))",
        value,
    ), 10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [value]


@pytest.mark.parametrize("arguments", [[], (), "az --version"])
def test_process_preparation_requires_an_argument_vector(arguments):
    with pytest.raises(ValueError, match="nonempty argument vector"):
        prepare_process_args(arguments)


def test_acquired_package_path_reaches_native_batch_submission_unchanged(batch_tool, tmp_path):
    from siteops.workspace_package import MaterializedPackageBinding, extract_package

    launcher, result_path = batch_tool
    launcher.with_name("argv.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['SITEOPS_TEST_ARGV_PATH']).open('a', encoding='utf-8') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "if args == ['version', '--output', 'json']:\n"
        "    print(json.dumps({'azure-cli': '2.87.0'}))\n"
        "elif args[:3] == ['deployment', 'group', 'create']:\n"
        "    print('{}')\n"
        "elif args[:3] == ['deployment', 'group', 'show']:\n"
        "    print(json.dumps({'properties': {'provisioningState': 'Succeeded', 'outputs': {}}}))\n"
        "else:\n"
        "    raise SystemExit('Unexpected native fixture command')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    workspace = source / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "templates").mkdir()
    name = "template&echo.SITEOPS_TEST_INJECTED&rem.json"
    (workspace / "templates" / name).write_text(json.dumps({
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "resources": [],
    }), encoding="utf-8")
    (workspace / "manifests" / "main.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nname: native-package\nselector: name=operator\n"
        f"steps:\n  - name: deploy\n    template: templates/{name}\n    scope: resourceGroup\n",
        encoding="utf-8",
    )

    def session():
        return TemplateCompilationSession(
            tool_resolver=lambda tool: str(launcher) if tool == "az" else None,
        )

    archive = tmp_path / "package.zip"
    inspection = package_builder.build_package(
        source, archive, workspace="workspace", kit_id="example.native", version="1",
        source_revision="fixture", siteops_range=">=1.0.0b1,<2",
        compilation_session_factory=session,
    )
    content = tmp_path / "materialized"
    extract_package(archive, inspection.sha256, content)
    binding = MaterializedPackageBinding.bind(inspection, content, "native-package")
    project = tmp_path / "project"
    (project / "sites").mkdir(parents=True)
    (project / "sites" / "operator.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: operator\n"
        "subscription: sub\nresourceGroup: group\nlocation: eastus\n",
        encoding="utf-8",
    )
    orchestrator = Orchestrator(
        binding.workspace, site_config_root=project, materialized_package=binding,
    )
    with patch("siteops.orchestrator.TemplateCompilationSession", side_effect=session):
        plan = orchestrator.build_plan(binding.manifest_path, intent=PlanIntent.EXECUTABLE)
        result = orchestrator.deploy(binding.manifest_path, plan_result=plan)

    assert result.status is RunStatus.SUCCEEDED
    calls = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()]
    submission = next(args for args in calls if args[:3] == ["deployment", "group", "create"])
    assert submission[submission.index("--template-file") + 1] == str(
        binding.workspace / "templates" / name
    )

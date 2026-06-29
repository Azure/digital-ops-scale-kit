"""Unit tests for the Site Ops executor module.

Tests cover:
- Azure CLI command execution
- Kubectl command execution
- Parameter file generation
- File validation for kubectl
- Dry-run mode behavior
"""

import json
import os
import re
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from siteops.executor import (
    _ARC_PROXY_PORT_IN_USE_PATTERN,
    _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S,
    ARC_PROXY_MAX_PORT_RETRIES,
    ARC_PROXY_MAX_SLOTS,
    ARC_PROXY_PORT_BASE,
    ARC_PROXY_PORT_SPACING,
    DEFAULT_AZ_TIMEOUT_SECONDS,
    DEFAULT_KUBECTL_TIMEOUT_SECONDS,
    HTTPS_URL_PATTERN,
    AzCliExecutor,
    DeploymentResult,
    KubectlResult,
    _allocate_arc_port_slot,
    _allocated_arc_port_slots,
    _arc_port_lock,
    _compute_probe_phase_budget,
    _probe_arc_proxy_ready,
    _release_arc_port_slot,
    filter_parameters,
    get_template_parameters,
)


class TestDeploymentResult:
    """Tests for the DeploymentResult dataclass."""

    def test_successful_result(self):
        result = DeploymentResult(
            success=True,
            step_name="deploy-storage",
            site_name="dev-eastus",
            deployment_name="myapp-dev-20260102",
            outputs={"storageId": {"value": "storage-123", "type": "String"}},
        )
        assert result.success is True
        assert result.error is None
        assert "storageId" in result.outputs

    def test_failed_result(self):
        result = DeploymentResult(
            success=False,
            step_name="deploy-storage",
            site_name="dev-eastus",
            deployment_name="myapp-dev-20260102",
            error="Resource group not found",
        )
        assert result.success is False
        assert result.error == "Resource group not found"

    def test_outputs_defaults_to_empty_dict(self):
        result = DeploymentResult(
            success=True,
            step_name="test",
            site_name="site",
            deployment_name="deploy",
        )
        assert result.outputs == {}


class TestKubectlResult:
    """Tests for the KubectlResult dataclass."""

    def test_successful_result(self):
        result = KubectlResult(
            success=True,
            step_name="apply-config",
            site_name="dev-eastus",
        )
        assert result.success is True
        assert result.error is None

    def test_failed_result(self):
        result = KubectlResult(
            success=False,
            step_name="apply-config",
            site_name="dev-eastus",
            error="connection refused",
        )
        assert result.success is False
        assert "connection refused" in result.error


class TestHttpsUrlPattern:
    """Tests for the HTTPS URL validation pattern."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/config.yaml",
            "HTTPS://EXAMPLE.COM/CONFIG.YAML",
            "https://raw.githubusercontent.com/org/repo/main/file.yaml",
        ],
    )
    def test_valid_https_urls(self, url):
        assert HTTPS_URL_PATTERN.match(url) is not None

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/config.yaml",
            "ftp://example.com/config.yaml",
            "file:///path/to/file.yaml",
            "/local/path/file.yaml",
            "relative/path.yaml",
        ],
    )
    def test_invalid_urls(self, url):
        assert HTTPS_URL_PATTERN.match(url) is None


class TestAzCliExecutor:
    """Tests for the AzCliExecutor class."""

    def test_init(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        assert executor.workspace == tmp_workspace
        assert executor.dry_run is False

    def test_init_dry_run(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace, dry_run=True)
        assert executor.dry_run is True

    def test_tmp_dir_creation(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        tmp_dir = executor.tmp_dir

        assert tmp_dir.exists()
        assert tmp_dir == tmp_workspace / ".siteops" / "tmp"

    def test_tmp_dir_cached(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        tmp_dir1 = executor.tmp_dir
        tmp_dir2 = executor.tmp_dir

        assert tmp_dir1 is tmp_dir2

    def test_kubectl_path_cached(self, tmp_workspace):
        """Test that kubectl_path property lazy-caches shutil.which result."""
        executor = AzCliExecutor(workspace=tmp_workspace)

        with patch("shutil.which", return_value="/usr/local/bin/kubectl") as mock_which:
            path1 = executor.kubectl_path
            path2 = executor.kubectl_path

        assert path1 == "/usr/local/bin/kubectl"
        assert path2 == "/usr/local/bin/kubectl"
        # Should only call shutil.which once (cached)
        mock_which.assert_called_once_with("kubectl")


class TestAzCliExecutorRunAz:
    """Tests for Azure CLI command execution."""

    def test_run_az_success(self, tmp_workspace, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=["az", "version"],
            returncode=0,
            stdout='{"azure-cli": "2.50.0"}',
            stderr="",
        )

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            success, stdout, stderr = executor._run_az(["version"])

            assert success is True
            assert "azure-cli" in stdout
            mock_run.assert_called_once()

    def test_run_az_failure(self, tmp_workspace, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=["az", "bad-command"],
            returncode=1,
            stdout="",
            stderr="'bad-command' is not a valid command",
        )

        with patch("subprocess.run", return_value=mock_result):
            success, stdout, stderr = executor._run_az(["bad-command"])

            assert success is False
            assert "not a valid command" in stderr

    def test_run_az_not_found(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        # Set cached path to empty string - bypasses shutil.which and is falsy
        executor._az_path = ""

        success, stdout, stderr = executor._run_az(["version"])

        assert success is False
        assert "not found" in stderr

    def test_run_az_timeout(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        # Set cached path directly to bypass shutil.which
        executor._az_path = "/usr/bin/az"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="az", timeout=10)):
            success, stdout, stderr = executor._run_az(["long-command"], timeout=10)

        assert success is False
        assert "timed out" in stderr

    def test_run_az_generic_exception(self, tmp_workspace):
        """Test that unexpected exceptions are caught and returned as failure."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        executor._az_path = "/usr/bin/az"

        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            success, stdout, stderr = executor._run_az(["version"])

        assert success is False
        assert "Permission denied" in stderr

    def test_run_az_dry_run(self, tmp_workspace, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace, dry_run=True)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        with patch("subprocess.run") as mock_run:
            success, stdout, stderr = executor._run_az(["deployment", "create"])

            assert success is True
            assert stdout == "{}"
            mock_run.assert_not_called()


class TestAzCliExecutorRunKubectl:
    """Tests for kubectl command execution."""

    def test_run_kubectl_success(self, tmp_workspace, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_kubectl_path", "/usr/bin/kubectl")

        mock_result = subprocess.CompletedProcess(
            args=["kubectl", "version"],
            returncode=0,
            stdout="Client Version: v1.28.0",
            stderr="",
        )

        with patch("subprocess.run", return_value=mock_result):
            success, stdout, stderr = executor._run_kubectl(["version"])

            assert success is True
            assert "v1.28.0" in stdout

    def test_run_kubectl_not_found(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        # Set cached path to empty string - bypasses shutil.which and is falsy
        executor._kubectl_path = ""

        success, stdout, stderr = executor._run_kubectl(["version"])

        assert success is False
        assert "kubectl not found" in stderr

    def test_run_kubectl_dry_run(self, tmp_workspace, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace, dry_run=True)
        monkeypatch.setattr(executor, "_kubectl_path", "/usr/bin/kubectl")

        with patch("subprocess.run") as mock_run:
            success, stdout, stderr = executor._run_kubectl(["apply", "-f", "config.yaml"])

            assert success is True
            mock_run.assert_not_called()

    def test_run_kubectl_timeout(self, tmp_workspace):
        """Test that kubectl timeout is caught and returned as failure."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        executor._kubectl_path = "/usr/bin/kubectl"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=10)):
            success, stdout, stderr = executor._run_kubectl(["apply", "-f", "config.yaml"], timeout=10)

        assert success is False
        assert "timed out" in stderr

    def test_run_kubectl_generic_exception(self, tmp_workspace):
        """Test that unexpected exceptions are caught and returned as failure."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        executor._kubectl_path = "/usr/bin/kubectl"

        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            success, stdout, stderr = executor._run_kubectl(["version"])

        assert success is False
        assert "Permission denied" in stderr


class TestWriteParamsFile:
    """Tests for parameter file generation."""

    def test_write_params_file_basic(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        params = {"location": "eastus", "sku": "Standard"}
        params_path = executor._write_params_file(params, "deploy-step", "dev-site")

        assert params_path.exists()
        assert params_path.suffix == ".json"

        with open(params_path, encoding="utf-8") as f:
            content = json.load(f)

        assert "$schema" in content
        assert content["parameters"]["location"]["value"] == "eastus"
        assert content["parameters"]["sku"]["value"] == "Standard"

    def test_write_params_file_nested_values(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        params = {
            "tags": {"env": "dev", "team": "platform"},
            "config": {"nested": {"deep": "value"}},
        }
        params_path = executor._write_params_file(params, "step", "site")

        with open(params_path, encoding="utf-8") as f:
            content = json.load(f)

        assert content["parameters"]["tags"]["value"]["env"] == "dev"
        assert content["parameters"]["config"]["value"]["nested"]["deep"] == "value"

    def test_write_params_file_creates_tmp_dir(self, tmp_workspace):
        # Remove the .siteops directory if it exists
        siteops_dir = tmp_workspace / ".siteops"
        if siteops_dir.exists():
            import shutil

            shutil.rmtree(siteops_dir)

        executor = AzCliExecutor(workspace=tmp_workspace)
        executor._tmp_dir = None  # Reset cached value

        params_path = executor._write_params_file({"key": "value"}, "step", "site")

        assert params_path.parent.exists()


class TestValidateKubectlFile:
    """Tests for kubectl file path validation."""

    def test_https_url_valid(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        is_valid, error = executor._validate_kubectl_file("https://example.com/config.yaml")

        assert is_valid is True
        assert error is None

    def test_http_url_rejected(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        is_valid, error = executor._validate_kubectl_file("http://example.com/config.yaml")

        assert is_valid is False
        assert "HTTP URLs not allowed" in error

    def test_local_file_valid(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        # Create a valid file in workspace
        config_file = tmp_workspace / "configs" / "app.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("apiVersion: v1\nkind: ConfigMap")

        is_valid, error = executor._validate_kubectl_file("configs/app.yaml")

        assert is_valid is True
        assert error is None

    def test_local_file_not_found(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        is_valid, error = executor._validate_kubectl_file("nonexistent/file.yaml")

        assert is_valid is False
        assert "File not found" in error

    def test_path_traversal_rejected(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        is_valid, error = executor._validate_kubectl_file("../outside/workspace.yaml")

        assert is_valid is False
        assert "Path traversal not allowed" in error


def _show_result(state, outputs=None, error=None):
    """Build a (success, stdout, stderr) tuple mimicking `az deployment ... show`."""
    props = {"provisioningState": state}
    if outputs is not None:
        props["outputs"] = outputs
    if error is not None:
        props["error"] = error
    return (True, json.dumps({"properties": props}), "")


# A `--no-wait` submit returns empty stdout with returncode 0.
_SUBMIT_OK = (True, "", "")


class TestDeployResourceGroup:
    """Tests for resource group deployments."""

    def test_deploy_resource_group_success(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            _show_result(
                "Succeeded",
                outputs={"resourceId": {"type": "String", "value": "resource-123"}},
            ),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={"location": "eastus"},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is True
        assert result.outputs["resourceId"]["value"] == "resource-123"
        assert result.deployment_name == "test-deploy"

    def test_deploy_resource_group_success_after_running(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            _show_result("Running"),
            _show_result("Succeeded", outputs={}),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = executor.deploy_resource_group(
                    subscription="sub-123",
                    resource_group="rg-test",
                    template_path=sample_bicep_template,
                    parameters={},
                    deployment_name="test-deploy",
                    step_name="step-1",
                    site_name="site-1",
                )

        assert result.success is True
        assert result.outputs == {}

    def test_submit_uses_no_wait_then_show_polls(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        calls = []

        def record(args, timeout=None):
            calls.append(args)
            if "create" in args:
                return _SUBMIT_OK
            return _show_result("Succeeded", outputs={})

        with patch.object(executor, "_run_az", side_effect=record):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is True
        assert "--no-wait" in calls[0]
        assert calls[0][:3] == ["deployment", "group", "create"]
        assert calls[1][:3] == ["deployment", "group", "show"]
        assert "--no-wait" not in calls[1]

    def test_deploy_resource_group_failure(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Resource group 'rg-test' not found",
        )

        with patch("subprocess.run", return_value=mock_result):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "not found" in result.error

    def test_submit_permanent_failure_fails_fast(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """A deterministic submit rejection (bad template) fails fast without polling."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        def guard(args, timeout=None):
            if "show" in args:
                raise AssertionError("must not poll after a fail-fast submit error")
            return (False, "", "InvalidTemplate: the template resource is not valid")

        with patch.object(executor, "_run_az", side_effect=guard):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "InvalidTemplate" in result.error

    def test_submit_transient_retries_then_polls(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            (False, "", "status code: 503 ServiceUnavailable"),
            (False, "", "status code: 503 ServiceUnavailable"),
            _SUBMIT_OK,
            _show_result("Succeeded", outputs={}),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = executor.deploy_resource_group(
                    subscription="sub-123",
                    resource_group="rg-test",
                    template_path=sample_bicep_template,
                    parameters={},
                    deployment_name="test-deploy",
                    step_name="step-1",
                    site_name="site-1",
                )

        assert result.success is True

    def test_submit_timeout_falls_through_to_poll(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            (False, "", "Command timed out after 300s"),
            _show_result("Succeeded", outputs={}),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = executor.deploy_resource_group(
                    subscription="sub-123",
                    resource_group="rg-test",
                    template_path=sample_bicep_template,
                    parameters={},
                    deployment_name="test-deploy",
                    step_name="step-1",
                    site_name="site-1",
                )

        assert result.success is True

    def test_deploy_resource_group_dry_run(self, tmp_workspace, sample_bicep_template):
        executor = AzCliExecutor(workspace=tmp_workspace, dry_run=True)

        with patch("subprocess.run") as mock_run:
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={"location": "eastus"},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is True
        mock_run.assert_not_called()

    def test_failed_state_surfaces_operation_detail(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """A Failed deployment surfaces the per-operation root cause, not the shallow node."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        ops = [
            {
                "properties": {
                    "provisioningState": "Failed",
                    "targetResource": {"resourceType": "Microsoft.Storage/storageAccounts"},
                    "statusMessage": {
                        "error": {"code": "QuotaExceeded", "message": "Storage quota exceeded"}
                    },
                }
            },
            {"properties": {"provisioningState": "Succeeded"}},
        ]
        responses = [
            _SUBMIT_OK,
            _show_result(
                "Failed",
                error={
                    "code": "DeploymentFailed",
                    "message": "At least one resource deployment operation failed.",
                },
            ),
            (True, json.dumps(ops), ""),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "QuotaExceeded" in result.error
        assert "Storage quota exceeded" in result.error

    def test_failed_state_falls_back_to_properties_error(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            _show_result("Failed", error={"code": "PolicyViolation", "message": "Denied by policy"}),
            (True, json.dumps([]), ""),  # no failed operations available
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "PolicyViolation" in result.error
        assert "Denied by policy" in result.error

    def test_auth_error_during_poll_does_not_fail_deploy(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """A momentary auth error while polling must not fail an in-flight deployment."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            (False, "", "AADSTS700024: Client assertion is not within its valid time range"),
            _show_result("Succeeded", outputs={}),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = executor.deploy_resource_group(
                    subscription="sub-123",
                    resource_group="rg-test",
                    template_path=sample_bicep_template,
                    parameters={},
                    deployment_name="test-deploy",
                    step_name="step-1",
                    site_name="site-1",
                )

        assert result.success is True

    def test_unparseable_show_then_success(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            (True, "not json at all", ""),
            (True, '{"properties": {"outputs":', ""),
            _show_result("Succeeded", outputs={}),
        ]
        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.sleep"):
                result = executor.deploy_resource_group(
                    subscription="sub-123",
                    resource_group="rg-test",
                    template_path=sample_bicep_template,
                    parameters={},
                    deployment_name="test-deploy",
                    step_name="step-1",
                    site_name="site-1",
                )

        assert result.success is True

    def test_notfound_grace_exhausted_fails_visible(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            (False, "", "ResourceNotFound: deployment not found"),
            (False, "", "ResourceNotFound: deployment not found"),
        ]
        clock = iter([0.0, 10.0, 10.0, 200.0])

        def fake_monotonic():
            try:
                return next(clock)
            except StopIteration:
                return 200.0

        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.monotonic", side_effect=fake_monotonic):
                with patch("siteops.executor.time.sleep"):
                    result = executor.deploy_resource_group(
                        subscription="sub-123",
                        resource_group="rg-test",
                        template_path=sample_bicep_template,
                        parameters={},
                        deployment_name="test-deploy",
                        step_name="step-1",
                        site_name="site-1",
                    )

        assert result.success is False
        assert "never became visible" in result.error

    def test_observation_grace_exhausted_does_not_claim_failure(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """Losing observability for the grace window reports indeterminate, not a deploy failure."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            (False, "", "AADSTS700024: assertion expired"),
            (False, "", "AADSTS700024: assertion expired"),
        ]
        clock = iter([0.0, 10.0, 10.0, 700.0])

        def fake_monotonic():
            try:
                return next(clock)
            except StopIteration:
                return 700.0

        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.monotonic", side_effect=fake_monotonic):
                with patch("siteops.executor.time.sleep"):
                    result = executor.deploy_resource_group(
                        subscription="sub-123",
                        resource_group="rg-test",
                        template_path=sample_bicep_template,
                        parameters={},
                        deployment_name="test-deploy",
                        step_name="step-1",
                        site_name="site-1",
                    )

        assert result.success is False
        assert "Lost the ability to observe" in result.error
        assert "may still be running" in result.error

    def test_overall_deadline_does_not_claim_failure(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        responses = [
            _SUBMIT_OK,
            _show_result("Running"),
            _show_result("Running"),
        ]
        clock = iter([0.0, 1.0, 1.0, 3700.0, 3700.0])

        def fake_monotonic():
            try:
                return next(clock)
            except StopIteration:
                return 3700.0

        with patch.object(executor, "_run_az", side_effect=responses):
            with patch("siteops.executor.time.monotonic", side_effect=fake_monotonic):
                with patch("siteops.executor.time.sleep"):
                    result = executor.deploy_resource_group(
                        subscription="sub-123",
                        resource_group="rg-test",
                        template_path=sample_bicep_template,
                        parameters={},
                        deployment_name="test-deploy",
                        step_name="step-1",
                        site_name="site-1",
                    )

        assert result.success is False
        assert "did not reach a terminal state" in result.error

    def test_deploy_resource_group_az_not_found(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "")  # falsy, skips shutil.which

        with patch.object(executor, "_run_az", side_effect=AssertionError("must not run az when missing")):
            result = executor.deploy_resource_group(
                subscription="sub-123",
                resource_group="rg-test",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="test-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "Azure CLI (az) not found" in result.error


class TestDeploySubscription:
    """Tests for subscription-scoped deployments."""

    def test_deploy_subscription_success(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        calls = []

        def record(args, timeout=None):
            calls.append(args)
            if "create" in args:
                return _SUBMIT_OK
            return _show_result("Succeeded", outputs={})

        with patch.object(executor, "_run_az", side_effect=record):
            result = executor.deploy_subscription(
                subscription="sub-123",
                location="eastus",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="sub-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is True
        assert calls[0][:3] == ["deployment", "sub", "create"]
        assert calls[1][:3] == ["deployment", "sub", "show"]

    def test_deploy_subscription_failed_uses_sub_operation_list(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        calls = []

        def record(args, timeout=None):
            calls.append(args)
            if "create" in args:
                return _SUBMIT_OK
            if "operation" in args:
                ops = [
                    {
                        "properties": {
                            "provisioningState": "Failed",
                            "statusMessage": {"error": {"code": "BadRequest", "message": "nope"}},
                        }
                    }
                ]
                return (True, json.dumps(ops), "")
            return _show_result("Failed", error={"code": "DeploymentFailed", "message": "see operations"})

        with patch.object(executor, "_run_az", side_effect=record):
            result = executor.deploy_subscription(
                subscription="sub-123",
                location="eastus",
                template_path=sample_bicep_template,
                parameters={},
                deployment_name="sub-deploy",
                step_name="step-1",
                site_name="site-1",
            )

        assert result.success is False
        assert "BadRequest" in result.error
        ops_call = next(c for c in calls if "operation" in c)
        assert ops_call[:4] == ["deployment", "operation", "sub", "list"]


class TestKubectlApply:
    """Tests for kubectl apply operations."""

    def test_kubectl_apply_dry_run(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace, dry_run=True)

        result = executor.kubectl_apply(
            cluster_name="my-cluster",
            resource_group="rg-test",
            subscription="sub-123",
            files=["https://example.com/config.yaml"],
            step_name="apply-step",
            site_name="site-1",
        )

        assert result.success is True

    def test_kubectl_apply_invalid_file(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)

        result = executor.kubectl_apply(
            cluster_name="my-cluster",
            resource_group="rg-test",
            subscription="sub-123",
            files=["http://insecure.com/config.yaml"],  # HTTP not allowed
            step_name="apply-step",
            site_name="site-1",
        )

        assert result.success is False
        assert "HTTP URLs not allowed" in result.error

    def test_kubectl_apply_missing_kubectl(self, tmp_workspace):
        executor = AzCliExecutor(workspace=tmp_workspace)
        # Set cached paths directly to control behavior
        executor._kubectl_path = ""  # Empty string is falsy
        executor._az_path = "/usr/bin/az"

        # Create a valid file so validation passes
        config_file = tmp_workspace / "config.yaml"
        config_file.write_text("apiVersion: v1", encoding="utf-8")

        with patch.object(executor, "_arc_proxy") as mock_proxy:
            mock_proxy.return_value.__enter__ = MagicMock(return_value=True)
            mock_proxy.return_value.__exit__ = MagicMock(return_value=False)

            result = executor.kubectl_apply(
                cluster_name="my-cluster",
                resource_group="rg-test",
                subscription="sub-123",
                files=["config.yaml"],
                step_name="apply-step",
                site_name="site-1",
            )

        assert result.success is False
        assert "kubectl not found" in result.error


class TestTimeoutConstants:
    """Tests to verify timeout constants are properly defined."""

    def test_az_timeout_is_one_hour(self):
        assert DEFAULT_AZ_TIMEOUT_SECONDS == 3600

    def test_kubectl_timeout_is_ten_minutes(self):
        assert DEFAULT_KUBECTL_TIMEOUT_SECONDS == 600


class TestArcProxyPortAllocation:
    """Tests for Arc proxy port slot allocation."""

    def setup_method(self):
        """Clear allocated ports before each test."""
        with _arc_port_lock:
            _allocated_arc_port_slots.clear()

    def teardown_method(self):
        """Clear allocated ports after each test."""
        with _arc_port_lock:
            _allocated_arc_port_slots.clear()

    def test_allocate_first_slot(self):
        port = _allocate_arc_port_slot()
        assert port == ARC_PROXY_PORT_BASE  # 47021

    def test_allocate_sequential_slots(self):
        port1 = _allocate_arc_port_slot()
        port2 = _allocate_arc_port_slot()
        port3 = _allocate_arc_port_slot()

        assert port1 == ARC_PROXY_PORT_BASE
        assert port2 == ARC_PROXY_PORT_BASE + ARC_PROXY_PORT_SPACING
        assert port3 == ARC_PROXY_PORT_BASE + (2 * ARC_PROXY_PORT_SPACING)

    def test_release_and_reallocate(self):
        port1 = _allocate_arc_port_slot()
        _allocate_arc_port_slot()  # consume slot 1 so the released slot is reused first

        _release_arc_port_slot(port1)

        # Next allocation should reuse slot 0
        port3 = _allocate_arc_port_slot()
        assert port3 == port1

    def test_allocate_all_slots(self):
        ports = [_allocate_arc_port_slot() for _ in range(ARC_PROXY_MAX_SLOTS)]

        assert len(ports) == ARC_PROXY_MAX_SLOTS
        assert len(set(ports)) == ARC_PROXY_MAX_SLOTS  # All unique

    def test_allocate_exceeds_max_slots(self):
        # Allocate all slots
        for _ in range(ARC_PROXY_MAX_SLOTS):
            _allocate_arc_port_slot()

        # Next allocation should raise
        with pytest.raises(RuntimeError) as exc_info:
            _allocate_arc_port_slot()

        assert "No available Arc proxy slots" in str(exc_info.value)

    def test_port_base_avoids_default(self):
        # Ensure port base is not 47011 (default) to avoid race with internal port 47010
        assert ARC_PROXY_PORT_BASE > 47011

    def test_port_spacing_allows_internal_port(self):
        # Spacing must be > 1 to leave room for internal port (port - 1)
        assert ARC_PROXY_PORT_SPACING >= 2

    def test_release_invalid_port_is_safe(self):
        # Releasing a port that was never allocated should not raise
        _release_arc_port_slot(99999)  # Should not raise


class TestArcProxyPortInUseRetry:
    """Tests for `_arc_proxy` retry when `az connectedk8s proxy` exits with
    "Port X is already in use". The allocated slot may collide with a process
    outside the in-process allocator (stale proxy, unrelated tenant); the
    fix retries with the next slot up to `ARC_PROXY_MAX_PORT_RETRIES`.
    """

    def setup_method(self):
        with _arc_port_lock:
            _allocated_arc_port_slots.clear()

    def teardown_method(self):
        with _arc_port_lock:
            _allocated_arc_port_slots.clear()

    @pytest.fixture(autouse=True)
    def _block_real_signal_to_runner(self):
        """Block the executor's cleanup branch from issuing real OS signals
        when `proxy_process` is a MagicMock.

        `_arc_proxy`'s `finally` block runs the Unix cleanup path when
        `proxy_process.poll() is None`. With a MagicMock subprocess
        `mock.pid` is itself a MagicMock whose `__int__` coerces to 1, so an
        unpatched `os.killpg(os.getpgid(mock.pid), SIGTERM)` resolves to
        `os.killpg(getpgid(1), SIGTERM)`. On a GitHub-hosted Linux runner
        PID 1's process group includes the runner agent, so that SIGTERM
        terminates the runner and the job ends with
        `##[error]The operation was canceled` instead of a test failure.
        `create=True` keeps the patches valid on Windows test hosts where
        `os.killpg` and `os.getpgid` are not defined on the os module.
        """
        with patch("siteops.executor.os.killpg", create=True), \
             patch("siteops.executor.os.getpgid", create=True):
            yield

    def _make_popen_factory(self, sequence):
        """Build a subprocess.Popen replacement that yields a sequence of
        configured mock processes. Each entry is a dict like
        `{"poll": None | <exit code>, "stderr": "...stderr..."}`.
        `poll=None` means the process is still running.
        """
        mocks = []
        for entry in sequence:
            m = MagicMock()
            m.poll.return_value = entry["poll"]
            m.communicate.return_value = ("", entry.get("stderr", ""))
            mocks.append(m)
        return MagicMock(side_effect=mocks)

    def _executor(self):
        from pathlib import Path
        ex = AzCliExecutor(workspace=Path("/tmp/ws"), dry_run=False)
        ex._az_path = "/usr/bin/az"  # bypass lazy lookup
        return ex

    def test_port_in_use_pattern_matches_cli_error(self):
        assert _ARC_PROXY_PORT_IN_USE_PATTERN.search("ERROR: Port 47020 is already in use.")
        assert _ARC_PROXY_PORT_IN_USE_PATTERN.search("port 47010 is already in use")
        assert not _ARC_PROXY_PORT_IN_USE_PATTERN.search("ERROR: Some other failure")

    def test_retry_succeeds_on_second_attempt(self):
        """Port-in-use on first try, alive on second. Yields kubeconfig path after retry."""
        executor = self._executor()
        popen = self._make_popen_factory([
            {"poll": 1, "stderr": "ERROR: Port 47020 is already in use."},
            {"poll": None},
        ])
        # Probe returns False (proxy died) for the first attempt, True (proxy
        # responsive) for the second. Mocking the probe avoids real socket
        # and kubectl calls in this test.
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", side_effect=[False, True]):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert isinstance(kubeconfig, str)
                assert kubeconfig != ""
        # Two Popen calls (one per attempt)
        assert popen.call_count == 2

    def test_no_retry_on_non_port_error(self):
        """Non-port-in-use error: yield None immediately, no retry."""
        executor = self._executor()
        popen = self._make_popen_factory([
            {"poll": 1, "stderr": "ERROR: Authentication failed."},
            {"poll": None},  # would succeed if retry happened, but it should not
        ])
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", return_value=False):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert kubeconfig is None
        assert popen.call_count == 1

    def test_all_attempts_port_in_use_yields_none(self):
        """Every attempt hits port-in-use. After MAX_PORT_RETRIES, yield None."""
        executor = self._executor()
        popen = self._make_popen_factory([
            {"poll": 1, "stderr": "ERROR: Port 47020 is already in use."},
        ] * ARC_PROXY_MAX_PORT_RETRIES)
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", return_value=False):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert kubeconfig is None
        assert popen.call_count == ARC_PROXY_MAX_PORT_RETRIES

    def test_retry_releases_failed_slots(self):
        """Each failed retry must release its slot so subsequent allocations
        do not exhaust the slot pool unnecessarily."""
        executor = self._executor()
        popen = self._make_popen_factory([
            {"poll": 1, "stderr": "ERROR: Port 47020 is already in use."},
            {"poll": 1, "stderr": "ERROR: Port 47030 is already in use."},
            {"poll": None},
        ])
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", side_effect=[False, False, True]):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert isinstance(kubeconfig, str)
        # After exit, the successful slot is released too. No slots held.
        assert len(_allocated_arc_port_slots) == 0

    def test_probe_timeout_with_proxy_still_running_yields_none(self):
        """Probe returns False but proxy is still alive (bound but unresponsive).
        Engine must terminate the proxy and yield None without retrying."""
        executor = self._executor()
        # Proxy survives, but probe never confirms readiness.
        alive_mock = MagicMock()
        alive_mock.poll.return_value = None  # still running throughout
        alive_mock.wait.return_value = 0
        popen = MagicMock(return_value=alive_mock)
        # create=True lets the patch work on Windows where os.killpg / os.getpgid
        # are not defined as attributes. On Windows the production code uses
        # proxy_process.send_signal (already a MagicMock method) so the killpg
        # patches are never actually invoked.
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", return_value=False), \
             patch("siteops.executor.os.killpg", create=True) as mock_killpg, \
             patch("siteops.executor.os.getpgid", create=True, return_value=12345):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert kubeconfig is None
        # No retry: only one Popen call.
        assert popen.call_count == 1
        # Termination must have been attempted. A regression that drops
        # the signal/kill call would leak the proxy on every timeout.
        if os.name == "nt":
            assert alive_mock.send_signal.called, (
                "Windows cleanup branch must call proxy_process.send_signal"
            )
        else:
            assert mock_killpg.called, (
                "Unix cleanup branch must call os.killpg to terminate the proxy"
            )
        # The wait must run after the signal so the process is reaped.
        assert alive_mock.wait.called, "proxy_process.wait must be called to reap"

    def test_kubeconfig_temp_file_is_removed_on_exit(self):
        """Regression guard for the per-proxy kubeconfig cleanup.

        The kubeconfig holds a bearer token, so a removed unlink call
        would leak token-bearing temp files for the process lifetime
        without breaking any other test. Patches `os.unlink` and asserts
        it ran with the yielded path after the `with` block exits.
        """
        executor = self._executor()
        popen = self._make_popen_factory([{"poll": None}])
        with patch("siteops.executor.subprocess.Popen", popen), \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", return_value=True), \
             patch("siteops.executor.os.unlink") as mock_unlink:
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                assert isinstance(kubeconfig, str)
                yielded_path = kubeconfig
        mock_unlink.assert_called_once_with(yielded_path)

    def test_kubeconfig_path_threads_from_mkstemp_into_probe(self):
        """End-to-end wiring: the mkstemp path is passed to the probe
        as `kubeconfig_path` and is the same string yielded to the
        consumer. Guards against a regression where one of the three
        sites (mkstemp, probe kwarg, yield) drifts from the others.
        """
        executor = self._executor()
        popen = self._make_popen_factory([{"poll": None}])
        captured: dict = {}

        def recording_probe(proxy_process, port, *, kubectl_path=None, kubeconfig_path=None):
            captured["kubeconfig_path"] = kubeconfig_path
            return True

        with patch("siteops.executor.subprocess.Popen", popen) as mock_popen, \
             patch("siteops.executor.time.sleep"), \
             patch("siteops.executor.ARC_PROXY_STARTUP_WAIT", 0), \
             patch("siteops.executor._probe_arc_proxy_ready", side_effect=recording_probe):
            with executor._arc_proxy("cluster", "rg", "sub") as kubeconfig:
                yielded_path = kubeconfig

        assert isinstance(yielded_path, str) and yielded_path != ""
        assert captured["kubeconfig_path"] == yielded_path
        # The same path must also reach `az connectedk8s proxy --file <path>`
        # so the proxy writes to the isolated kubeconfig rather than the
        # ambient `~/.kube/config`.
        az_argv = mock_popen.call_args.args[0]
        assert "--file" in az_argv
        assert az_argv[az_argv.index("--file") + 1] == yielded_path


class TestProbeArcProxyReady:
    """Tests for `_probe_arc_proxy_ready`: TCP bind + kubectl readiness probe.

    The probe replaces a fixed-duration sleep so the engine can advance
    as soon as the proxy is responsive AND surface a clear failure when
    the port is bound but the upstream tunnel never establishes. Phase 2
    uses `kubectl get --raw /version` so the readiness signal mirrors
    the engine path the orchestrator runs for `apply`.
    """

    def _alive_proxy(self):
        m = MagicMock()
        m.poll.return_value = None
        return m

    def _dead_proxy(self, exit_code: int = 1):
        m = MagicMock()
        m.poll.return_value = exit_code
        return m

    def _sock_cm(self):
        sock = MagicMock()
        sock.__enter__ = MagicMock(return_value=sock)
        sock.__exit__ = MagicMock(return_value=False)
        return sock

    def _kubectl_run_result(self, returncode: int, stderr: str = ""):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = ""
        result.stderr = stderr
        return result

    def test_returns_true_when_tcp_and_kubectl_succeed_immediately(self):
        """TCP connects on first try, kubectl exits 0. Probe returns True."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.subprocess.run", return_value=self._kubectl_run_result(0)), \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/usr/bin/kubectl") is True

    def test_returns_false_when_proxy_dies_during_tcp_phase(self):
        """Proxy process exits before TCP bind succeeds. Probe returns False fast."""
        proxy = self._dead_proxy(exit_code=1)
        with patch("siteops.executor.socket.create_connection", side_effect=ConnectionRefusedError()), \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/usr/bin/kubectl") is False

    def test_returns_false_when_tcp_never_binds(self):
        """TCP refused for the full deadline. Probe must actually iterate
        the refusal-and-retry branch before returning False."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", side_effect=ConnectionRefusedError()) as mock_conn, \
             patch("siteops.executor.time.sleep"):
            # Small non-zero timeout. Mocks are no-ops so real wall-clock
            # elapses quickly while the loop exercises the refusal branch.
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=0.3, kubectl_path="/usr/bin/kubectl") is False
        # The TCP refusal branch must run at least once. A regression
        # that exits the loop before invoking create_connection would
        # silently turn this assertion into dead code.
        assert mock_conn.call_count >= 1, (
            f"socket.create_connection was never invoked "
            f"(call_count={mock_conn.call_count}). The TCP refusal loop "
            f"is not being exercised"
        )

    def test_returns_false_when_proxy_dies_during_readiness_phase(self):
        """TCP succeeds, then proxy dies before kubectl confirms readiness."""
        proxy = MagicMock()
        # poll() is checked once in the TCP loop (returns None to allow
        # connect) and again at the top of each readiness iteration. The
        # second readiness check returns a non-None exit code.
        proxy.poll.side_effect = [None, None, 1, 1, 1]
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.subprocess.run", return_value=self._kubectl_run_result(1, "unable to connect")), \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/usr/bin/kubectl") is False

    def test_returns_true_after_multiple_kubectl_failures_then_success(self):
        """The kubectl polling loop must iterate through transient failures
        and accept a later success. This is the whole point of the active
        probe."""
        proxy = self._alive_proxy()
        success = self._kubectl_run_result(0)
        failure = self._kubectl_run_result(1, "Unable to connect to the server: dial tcp 127.0.0.1:47021: connect: connection refused")
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch(
                 "siteops.executor.subprocess.run",
                 side_effect=[failure, failure, failure, success],
             ) as mock_run, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/usr/bin/kubectl") is True
        assert mock_run.call_count == 4

    def test_returns_false_when_kubectl_invocation_keeps_timing_out(self):
        """`subprocess.run` raising `TimeoutExpired` repeatedly counts as
        a transient failure. The probe keeps polling until the overall
        deadline elapses, then returns False."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch(
                 "siteops.executor.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=10),
             ) as mock_run, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=0.5, kubectl_path="/usr/bin/kubectl") is False
        # The polling loop runs at least one kubectl invocation before
        # the deadline elapses.
        assert mock_run.call_count >= 1

    def test_returns_false_when_kubectl_not_in_path(self):
        """The probe needs a real kubectl binary. When `shutil.which`
        returns None, the probe fails fast with a clear error rather than
        skipping the readiness signal. Explicit `subprocess.run` mock
        asserts the early-return guard runs before any subprocess call."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.shutil.which", return_value=None), \
             patch("siteops.executor.subprocess.run") as mock_run, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5) is False
        mock_run.assert_not_called()

    def test_uses_caller_supplied_kubectl_path_without_lookup(self):
        """A caller that has already resolved the kubectl path can pass
        it in to skip the `shutil.which` lookup."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.subprocess.run", return_value=self._kubectl_run_result(0)) as mock_run, \
             patch("siteops.executor.shutil.which") as mock_which, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/opt/kubectl") is True
        mock_which.assert_not_called()
        # The supplied path is the first element of the subprocess argv.
        called_cmd = mock_run.call_args.args[0]
        assert called_cmd[0] == "/opt/kubectl"

    def test_passes_kubeconfig_path_to_kubectl(self):
        """When the caller supplies a kubeconfig path, the probe passes it
        to kubectl as `--kubeconfig=<path>` so the readiness signal targets
        that specific kubeconfig file rather than the ambient context."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.subprocess.run", return_value=self._kubectl_run_result(0)) as mock_run, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(
                proxy,
                47021,
                timeout_s=5,
                kubectl_path="/opt/kubectl",
                kubeconfig_path="/tmp/arc-proxy.kubeconfig",
            ) is True
        called_cmd = mock_run.call_args.args[0]
        assert "--kubeconfig=/tmp/arc-proxy.kubeconfig" in called_cmd
        # The flag comes before the kubectl verb so it applies to the call.
        assert called_cmd.index("--kubeconfig=/tmp/arc-proxy.kubeconfig") < called_cmd.index("get")

    def test_omits_kubeconfig_flag_when_path_is_none(self):
        """Without a kubeconfig path the probe relies on default kubectl
        discovery, matching the pre-isolation behavior."""
        proxy = self._alive_proxy()
        with patch("siteops.executor.socket.create_connection", return_value=self._sock_cm()), \
             patch("siteops.executor.subprocess.run", return_value=self._kubectl_run_result(0)) as mock_run, \
             patch("siteops.executor.time.sleep"):
            assert _probe_arc_proxy_ready(proxy, 47021, timeout_s=5, kubectl_path="/opt/kubectl") is True
        called_cmd = mock_run.call_args.args[0]
        assert not any(arg.startswith("--kubeconfig") for arg in called_cmd)


class TestComputeProbePhaseBudget:
    """Tests for `_compute_probe_phase_budget`: the per-phase deadline split.

    Pure-function tests so the math is verified without depending on
    real wall-clock timing in the integration probe tests.
    """

    def test_default_production_budget_reserves_min_for_readiness(self):
        """At the production default (180s), TCP gets 170s and the kubectl
        readiness phase gets the reserved 10s minimum."""
        tcp, total = _compute_probe_phase_budget(180.0)
        assert tcp == 180.0 - _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S
        assert total == 180.0
        assert total - tcp == _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S

    def test_small_budget_splits_in_half(self):
        """A small user-supplied timeout splits 50/50 between phases.
        The min-budget cap never exceeds half the total, so both phases
        always get some time."""
        tcp, total = _compute_probe_phase_budget(5.0)
        assert tcp == 2.5
        assert total == 5.0

    def test_at_threshold_min_budget_equals_half(self):
        """At total = 2 * min, the cap exactly equals half."""
        threshold = 2 * _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S
        tcp, total = _compute_probe_phase_budget(threshold)
        assert tcp == threshold / 2
        assert total == threshold

    def test_just_above_threshold_caps_at_min_budget(self):
        """Just above 2 * min, the readiness reservation caps at the
        constant and TCP gets everything else."""
        total_input = 2 * _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S + 1.0
        tcp, total = _compute_probe_phase_budget(total_input)
        assert total - tcp == _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S
        assert tcp == total_input - _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S

    def test_zero_budget_gives_zero_to_both_phases(self):
        """A zero budget produces zero for both phases. Neither loop runs."""
        tcp, total = _compute_probe_phase_budget(0.0)
        assert tcp == 0.0
        assert total == 0.0


class TestGetTemplateParameters:
    """Tests for get_template_parameters() function."""

    def test_bicep_template_extracts_parameters(self, tmp_path):
        """Test that Bicep template parameters are extracted via az bicep build."""
        bicep_file = tmp_path / "test.bicep"
        bicep_file.write_text("param location string\nparam tags object\n")

        # Mock ARM JSON output from az bicep build
        arm_json = {
            "parameters": {
                "location": {"type": "string"},
                "tags": {"type": "object"},
            }
        }

        with (
            patch("siteops.executor.subprocess.run") as mock_run,
            patch("siteops.executor.shutil.which", return_value="/usr/bin/az"),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(arm_json),
                stderr="",
            )

            # Clear cache for this test
            get_template_parameters.cache_clear()

            result = get_template_parameters(str(bicep_file))

            assert result == frozenset({"location", "tags"})
            mock_run.assert_called_once()
            assert "bicep" in mock_run.call_args[0][0]
            assert "build" in mock_run.call_args[0][0]

    def test_arm_json_template_extracts_parameters(self, tmp_path):
        """Test that ARM JSON template parameters are parsed directly."""
        arm_file = tmp_path / "test.json"
        arm_json = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "parameters": {
                "storageAccountName": {"type": "string"},
                "location": {"type": "string"},
                "sku": {"type": "string", "defaultValue": "Standard_LRS"},
            },
            "resources": [],
        }
        arm_file.write_text(json.dumps(arm_json))

        # Clear cache for this test
        get_template_parameters.cache_clear()

        result = get_template_parameters(str(arm_file))

        assert result == frozenset({"storageAccountName", "location", "sku"})

    def test_arm_json_template_no_parameters(self, tmp_path):
        """Test ARM template with no parameters returns empty set."""
        arm_file = tmp_path / "empty.json"
        arm_json = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
            "resources": [],
        }
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        result = get_template_parameters(str(arm_file))

        assert result == frozenset()

    def test_file_not_found_raises_error(self):
        """Test that missing template raises FileNotFoundError."""
        get_template_parameters.cache_clear()

        with pytest.raises(FileNotFoundError, match="Template not found"):
            get_template_parameters("/nonexistent/path/template.bicep")

    def test_unsupported_extension_raises_error(self, tmp_path):
        """Test that unsupported file extensions raise ValueError."""
        yaml_file = tmp_path / "template.yaml"
        yaml_file.write_text("foo: bar")

        get_template_parameters.cache_clear()

        with pytest.raises(ValueError, match="Unsupported template format"):
            get_template_parameters(str(yaml_file))

    def test_bicep_compile_failure_raises_error(self, tmp_path):
        """Test that Bicep compilation failure raises ValueError."""
        bicep_file = tmp_path / "bad.bicep"
        bicep_file.write_text("invalid bicep syntax {{{{")

        with (
            patch("siteops.executor.subprocess.run") as mock_run,
            patch("siteops.executor.shutil.which", return_value="/usr/bin/az"),
        ):
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Error: Failed to compile",
            )

            get_template_parameters.cache_clear()

            with pytest.raises(ValueError, match="Failed to compile Bicep"):
                get_template_parameters(str(bicep_file))

    def test_invalid_json_raises_error(self, tmp_path):
        """Test that invalid JSON in ARM template raises ValueError."""
        arm_file = tmp_path / "invalid.json"
        arm_file.write_text("{ not valid json }")

        get_template_parameters.cache_clear()

        with pytest.raises(ValueError, match="Failed to parse ARM template"):
            get_template_parameters(str(arm_file))

    def test_results_are_cached(self, tmp_path):
        """Test that repeated calls use cached results."""
        arm_file = tmp_path / "cached.json"
        arm_json = {"parameters": {"foo": {"type": "string"}}}
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        # First call
        result1 = get_template_parameters(str(arm_file))
        # Modify file (shouldn't affect cached result)
        arm_file.write_text(json.dumps({"parameters": {"bar": {"type": "string"}}}))
        # Second call should return cached result
        result2 = get_template_parameters(str(arm_file))

        assert result1 == result2 == frozenset({"foo"})

    def test_az_cli_not_found_raises_error(self, tmp_path):
        """Test that missing Azure CLI raises ValueError for Bicep files."""
        bicep_file = tmp_path / "test.bicep"
        bicep_file.write_text("param location string")

        with patch("siteops.executor.shutil.which", return_value=None):
            get_template_parameters.cache_clear()

            with pytest.raises(ValueError, match="Azure CLI.*not found"):
                get_template_parameters(str(bicep_file))

    def test_bicep_invalid_json_output_raises_error(self, tmp_path):
        """Test that invalid JSON from az bicep build raises ValueError."""
        bicep_file = tmp_path / "test.bicep"
        bicep_file.write_text("param location string")

        with (
            patch("siteops.executor.subprocess.run") as mock_run,
            patch("siteops.executor.shutil.which", return_value="/usr/bin/az"),
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json at all",
                stderr="",
            )

            get_template_parameters.cache_clear()

            with pytest.raises(ValueError, match="Failed to parse compiled Bicep"):
                get_template_parameters(str(bicep_file))


class TestFilterParameters:
    """Tests for filter_parameters() function."""

    def test_filters_to_accepted_parameters(self, tmp_path):
        """Test that only accepted parameters are returned."""
        arm_file = tmp_path / "template.json"
        arm_json = {
            "parameters": {
                "location": {"type": "string"},
                "name": {"type": "string"},
            }
        }
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        params = {
            "location": "eastus",
            "name": "myresource",
            "extraParam": "should be filtered",
            "anotherExtra": {"nested": "value"},
        }

        result = filter_parameters(params, str(arm_file), "test-step")

        assert result == {"location": "eastus", "name": "myresource"}
        assert "extraParam" not in result
        assert "anotherExtra" not in result

    def test_returns_empty_when_no_params_match(self, tmp_path):
        """Test that empty dict is returned when no parameters match."""
        arm_file = tmp_path / "template.json"
        arm_json = {"parameters": {"foo": {"type": "string"}}}
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        params = {"bar": "value", "baz": "value"}

        result = filter_parameters(params, str(arm_file), "test-step")

        assert result == {}

    def test_returns_all_when_all_match(self, tmp_path):
        """Test that all parameters returned when all match template."""
        arm_file = tmp_path / "template.json"
        arm_json = {
            "parameters": {
                "location": {"type": "string"},
                "name": {"type": "string"},
                "tags": {"type": "object"},
            }
        }
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        params = {
            "location": "eastus",
            "name": "myresource",
            "tags": {"env": "dev"},
        }

        result = filter_parameters(params, str(arm_file), "test-step")

        assert result == params

    def test_handles_empty_input_parameters(self, tmp_path):
        """Test that empty input parameters returns empty dict."""
        arm_file = tmp_path / "template.json"
        arm_json = {"parameters": {"foo": {"type": "string"}}}
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        result = filter_parameters({}, str(arm_file), "test-step")

        assert result == {}

    def test_logs_filtered_parameters(self, tmp_path, caplog):
        """Test that filtered parameters are logged at debug level."""
        arm_file = tmp_path / "template.json"
        arm_json = {"parameters": {"accepted": {"type": "string"}}}
        arm_file.write_text(json.dumps(arm_json))

        get_template_parameters.cache_clear()

        params = {"accepted": "value", "rejected": "value"}

        import logging

        with caplog.at_level(logging.DEBUG, logger="siteops.executor"):
            result = filter_parameters(params, str(arm_file), "my-step")

        # Verify filtering worked
        assert result == {"accepted": "value"}
        assert "rejected" not in result


class TestUserAgentConfiguration:
    """Tests for Azure CLI User-Agent configuration."""

    def test_user_agent_set_on_import(self):
        """Verify AZURE_HTTP_USER_AGENT is set when executor module loads."""
        from siteops import __version__

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        assert f"siteops/{__version__}" in user_agent

    def test_user_agent_not_duplicated(self):
        """Verify User-Agent isn't duplicated on repeated configuration."""
        from siteops import __version__
        from siteops.executor import _configure_user_agent

        # Call configure again (simulates module reload)
        _configure_user_agent()
        _configure_user_agent()

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        # Count occurrences - should only appear once
        count = user_agent.count(f"siteops/{__version__}")
        assert count == 1, f"User-Agent duplicated: {user_agent}"

    def test_user_agent_appends_to_existing(self, monkeypatch):
        """Verify siteops agent is appended when other tools set User-Agent first."""
        from siteops import __version__
        from siteops.executor import _configure_user_agent

        # Simulate another tool setting the User-Agent before siteops loads
        monkeypatch.setenv("AZURE_HTTP_USER_AGENT", "other-tool/2.0")

        # Configure should append siteops agent
        _configure_user_agent()

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        assert "other-tool/2.0" in user_agent, "Original agent should be preserved"
        assert f"siteops/{__version__}" in user_agent, "Siteops agent should be appended"
        # Verify order: existing first, then siteops
        assert user_agent.index("other-tool/2.0") < user_agent.index("siteops/")

    def test_user_agent_format(self):
        """Verify User-Agent follows Azure SDK conventions."""
        from siteops import __version__

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        # Format should be "siteops/X.Y.Z"
        assert re.search(rf"siteops/{re.escape(__version__)}", user_agent)

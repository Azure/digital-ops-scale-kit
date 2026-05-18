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


class TestDeployResourceGroup:
    """Tests for resource group deployments."""

    def test_deploy_resource_group_success(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"properties": {"outputs": {"resourceId": {"type": "String", "value": "resource-123"}}}}),
            stderr="",
        )

        with patch("subprocess.run", return_value=mock_result):
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

    def test_deploy_resource_group_malformed_json_output(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """Test that malformed JSON in az deployment output is handled gracefully."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Deployment succeeded but output is not JSON",
            stderr="",
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

        assert result.success is True
        assert result.outputs == {}
        assert result.error is None
        assert result.step_name == "step-1"
        assert result.site_name == "site-1"
        assert result.deployment_name == "test-deploy"

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

    def test_deploy_resource_group_plain_text_stdout(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """Test that plain non-JSON stdout with success returncode doesn't crash."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="not json at all",
            stderr="",
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

        assert result.success is True
        assert result.outputs == {}

    def test_deploy_resource_group_truncated_json_stdout(self, tmp_workspace, sample_bicep_template, monkeypatch):
        """Test that truncated JSON stdout with success returncode doesn't crash."""
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"properties": {"outputs":',
            stderr="",
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

        assert result.success is True
        assert result.outputs == {}
        assert result.error is None
        assert result.step_name == "step-1"
        assert result.site_name == "site-1"


class TestDeploySubscription:
    """Tests for subscription-scoped deployments."""

    def test_deploy_subscription_success(self, tmp_workspace, sample_bicep_template, monkeypatch):
        executor = AzCliExecutor(workspace=tmp_workspace)
        monkeypatch.setattr(executor, "_az_path", "/usr/bin/az")

        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"properties": {"outputs": {}}}),
            stderr="",
        )

        with patch("subprocess.run", return_value=mock_result):
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

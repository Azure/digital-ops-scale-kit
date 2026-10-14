"""Pytest fixtures for Site Ops tests."""

import json
import subprocess

import pytest
import yaml


class _VersionOnlyToolRunner:
    """Answer local tool version probes and refuse every other invocation."""

    def __call__(self, argv: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"azure-cli": "2.87.0"}), stderr="")
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(argv, 0, stdout="Bicep CLI version test", stderr="")
        raise AssertionError(f"Unexpected local tool invocation: {argv}")


@pytest.fixture
def deterministic_local_tools(monkeypatch, tmp_path):
    """Give executable plans host-independent stand-ins for Azure CLI and kubectl.

    Executable preparation resolves `az` and reads its version. Without this,
    a test that deploys through the orchestrator depends on the host having
    Azure CLI on PATH.
    """
    from siteops.compilation import TemplateCompilationSession

    def resolve_tool(name):
        if name not in {"az", "kubectl"}:
            raise AssertionError(f"Unexpected local tool resolution: {name}")
        return str((tmp_path / "tools" / name).resolve())

    monkeypatch.setattr(
        "siteops.orchestrator.TemplateCompilationSession",
        lambda: TemplateCompilationSession(command_runner=_VersionOnlyToolRunner(), tool_resolver=resolve_tool),
    )


@pytest.fixture(autouse=True)
def default_non_integration_output_to_local(monkeypatch, request):
    """Keep unit and workspace output assertions independent of their host.

    GitHub Actions and Azure Pipelines enable redaction automatically. Most
    non-integration tests assert local operator output, so make that mode
    explicit. Integration tests retain the CI default because their logs and
    artifacts are published.
    """
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")


@pytest.fixture(autouse=True)
def default_arc_proxy_extension_probe_to_unknown(monkeypatch, request):
    """Keep executable planning independent of the host's Azure CLI extensions.

    Planning a kubectl step runs `az extension show` to check the connectedk8s
    extension. Unit tests record that check as inconclusive unless they set a
    result themselves. Integration tests run the real check.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    from siteops.executor import AzureCliExtensionProbe, AzureCliExtensionStatus

    monkeypatch.setattr(
        "siteops.orchestrator.probe_azure_cli_extension",
        lambda *_args, **_kwargs: AzureCliExtensionProbe(AzureCliExtensionStatus.UNKNOWN),
    )


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a minimal workspace structure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sites").mkdir()
    (workspace / "manifests").mkdir()
    (workspace / "templates").mkdir()
    (workspace / "parameters").mkdir()
    return workspace


@pytest.fixture
def sample_site_file(tmp_workspace):
    """Create a sample site file."""
    site_data = {
        "apiVersion": "siteops/v1",
        "kind": "Site",
        "name": "test-site",
        "subscription": "00000000-0000-0000-0000-000000000000",
        "resourceGroup": "rg-test",
        "location": "eastus",
        "labels": {"environment": "dev", "region": "eastus"},
    }
    site_path = tmp_workspace / "sites" / "test-site.yaml"
    with open(site_path, "w", encoding="utf-8") as f:
        yaml.dump(site_data, f)
    return site_path


@pytest.fixture
def sample_bicep_template(tmp_workspace):
    """Create a sample Bicep template."""
    template_content = """
param location string = resourceGroup().location
param name string

resource storageAccount 'Microsoft.Storage/storageAccounts@2021-02-01' = {
  name: name
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
}

output storageId string = storageAccount.id
"""
    template_path = tmp_workspace / "templates" / "test.bicep"
    with open(template_path, "w", encoding="utf-8") as f:
        f.write(template_content)
    return template_path


@pytest.fixture
def sample_manifest_file(tmp_workspace, sample_site_file, sample_bicep_template):
    """Create a sample manifest file."""
    manifest_data = {
        "apiVersion": "siteops/v1",
        "kind": "Manifest",
        "name": "test-manifest",
        "description": "Test manifest",
        "sites": ["test-site"],
        "steps": [
            {
                "name": "deploy-storage",
                "template": "templates/test.bicep",
                "scope": "resourceGroup",
            }
        ],
    }
    manifest_path = tmp_workspace / "manifests" / "test-manifest.yaml"
    with open(manifest_path, "w", encoding="utf-8") as f:
        yaml.dump(manifest_data, f)
    return manifest_path


@pytest.fixture
def complete_workspace(tmp_workspace, sample_site_file, sample_manifest_file, sample_bicep_template):
    """Create a complete workspace with all components."""
    # Add a parameter file
    params_data = {"location": "eastus", "name": "teststorage"}
    params_path = tmp_workspace / "parameters" / "test-params.yaml"
    with open(params_path, "w", encoding="utf-8") as f:
        yaml.dump(params_data, f)
    return tmp_workspace


@pytest.fixture
def multi_site_workspace(tmp_workspace, sample_bicep_template):
    """Create a workspace with multiple sites for selector testing."""
    sites = [
        {
            "name": "dev-eastus",
            "subscription": "00000000-0000-0000-0000-000000000001",
            "resourceGroup": "rg-dev-eastus",
            "location": "eastus",
            "labels": {"environment": "dev", "region": "eastus"},
        },
        {
            "name": "dev-westus",
            "subscription": "00000000-0000-0000-0000-000000000002",
            "resourceGroup": "rg-dev-westus",
            "location": "westus",
            "labels": {"environment": "dev", "region": "westus"},
        },
        {
            "name": "prod-eastus",
            "subscription": "00000000-0000-0000-0000-000000000003",
            "resourceGroup": "rg-prod-eastus",
            "location": "eastus",
            "labels": {"environment": "prod", "region": "eastus"},
        },
    ]

    for site in sites:
        site_path = tmp_workspace / "sites" / f"{site['name']}.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site, f)

    # Add a manifest
    manifest_data = {
        "name": "multi-site-manifest",
        "siteSelector": "environment=dev",
        "steps": [{"name": "step1", "template": "templates/test.bicep"}],
    }
    manifest_path = tmp_workspace / "manifests" / "multi-site.yaml"
    with open(manifest_path, "w", encoding="utf-8") as f:
        yaml.dump(manifest_data, f)

    return tmp_workspace


@pytest.fixture
def arm_template_file(tmp_workspace):
    """Create a sample ARM JSON template for parameter extraction tests."""
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"type": "string"},
            "name": {"type": "string"},
            "sku": {"type": "string", "defaultValue": "Standard"},
        },
        "resources": [],
        "outputs": {
            "resourceId": {
                "type": "string",
                "value": "[resourceId('Microsoft.Storage/storageAccounts', parameters('name'))]",
            }
        },
    }
    template_path = tmp_workspace / "templates" / "arm-template.json"
    with open(template_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)
    return template_path

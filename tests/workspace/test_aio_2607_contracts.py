"""Workspace contracts for the AIO 2607 API generation."""

import re
from pathlib import Path


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


def test_2607_create_module_uses_2026_07_api(workspace):
    module = _read(workspace, "templates/aio/modules/instance-2026-07-01.bicep")
    aio_resource_versions = re.findall(
        r"Microsoft\.IoTOperations/[^'@]+@([^']+)'",
        module,
    )

    assert aio_resource_versions
    assert set(aio_resource_versions) == {"2026-07-01"}


def test_2607_resolve_and_update_modules_use_2026_07_api(workspace):
    for relative_path in (
        "templates/aio/modules/resolve-instance-2026-07-01.bicep",
        "templates/aio/modules/update-instance-2026-07-01.bicep",
    ):
        module = _read(workspace, relative_path)
        assert "Microsoft.IoTOperations/instances@2026-07-01" in module
        assert "@2026-03-01" not in module


def test_2607_security_pki_defaults_apply_to_install_and_upgrade(workspace):
    create_module = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    for setting in (
        "connectors.values.securityPki.applicationUri",
        "connectors.values.securityPki.subjectName",
    ):
        assert setting in create_module
        assert setting in update_extensions
    assert "contains(aio.configurationSettings" in update_extensions
    assert "aio.extensionSuffix" in update_extensions
    assert "output aioApiVersionApplied string = aioApiVersion" in update_extensions


def test_2607_cert_manager_defaults_flow_through_release_metadata(workspace):
    release = _read(workspace, "parameters/aio-releases/2607.yaml")
    enablement = _read(workspace, "templates/aio/enablement.bicep")
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    assert 'trust-manager.secretTargets.enabled: "false"' in release
    assert 'trust-manager.secretTargets.authorizedSecretsAll: "false"' in release
    assert "certManagerConfigurationOverrides" in enablement
    assert "certManagerConfigurationOverrides" in update_extensions


def test_2607_dataflow_profile_waits_for_broker(workspace):
    module = _read(workspace, "templates/aio/modules/instance-2026-07-01.bicep")
    profile_start = module.index(
        "resource dataflowProfile 'Microsoft.IoTOperations/instances/dataflowProfiles@2026-07-01'"
    )
    endpoint_start = module.index("resource dataflowEndpoint ", profile_start)
    profile = module[profile_start:endpoint_start]

    assert "dependsOn: [" in profile
    assert "broker" in profile


def test_2607_broker_keeps_supported_log_diagnostics_shape(workspace):
    module = _read(workspace, "templates/aio/modules/instance-2026-07-01.bicep")
    broker_start = module.index(
        "resource broker 'Microsoft.IoTOperations/instances/brokers@2026-07-01'"
    )
    authn_start = module.index("resource brokerAuthn ", broker_start)
    broker = module[broker_start:authn_start]

    assert "diagnostics: {" in broker
    assert "logs: {" in broker
    assert "level: BROKER_CONFIG.logsLevel" in broker

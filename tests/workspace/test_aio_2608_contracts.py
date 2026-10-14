"""Workspace contracts for the AIO 2608 and 2609 releases."""

from pathlib import Path

import pytest
import yaml


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


def _release(workspace: Path, release: str) -> dict:
    path = workspace / "parameters" / "aio-releases" / f"{release}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("release_name", "aio", "connector", "cert_manager", "secret_store"), [
    ("2608", "1.4.73", "1.4.10", "1.0.0", "1.5.2"),
    ("2609", "1.4.112", "1.4.14", "1.1.2", "1.5.3"),
])
def test_release_metadata_matches_the_public_release(workspace, release_name, aio, connector, cert_manager, secret_store):
    release = _release(workspace, release_name)

    assert release["aioVersion"] == aio
    assert release["aioTrain"] == "stable"
    assert release["aioApiVersion"] == "2026-07-01"
    assert release["adrApiVersion"] == "2026-04-01"
    assert release["certManagerVersion"] == cert_manager
    assert release["certManagerTrain"] == "stable"
    assert release["secretStoreVersion"] == secret_store
    assert release["secretStoreTrain"] == "stable"
    assert release["aioReleaseConfiguration"] == {
        "extension": {"wasmGraphControllerMqttTrust": True},
        "resources": {"opcUaConnector": {"version": connector}},
    }


def test_2608_enables_only_its_release_specific_aio_settings(workspace):
    release_2607 = _release(workspace, "2607")
    release_2608 = _release(workspace, "2608")

    assert release_2607["aioReleaseConfiguration"] == {}
    assert release_2608["aioReleaseConfiguration"] == {
        "extension": {
            "wasmGraphControllerMqttTrust": True,
        },
        "resources": {
            "opcUaConnector": {
                "version": "1.4.10",
            },
        },
    }


def test_connector_template_module_is_shared_by_install_and_upgrade(workspace):
    create_module = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    upgrade_dispatcher = _read(
        workspace,
        "templates/aio/upgrade/deploy-release-resources.bicep",
    )
    upgrade_module = _read(
        workspace,
        "templates/aio/upgrade/modules/deploy-release-resources-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )
    connector_module = _read(
        workspace,
        "templates/aio/modules/opcua-connector-template-2026-07-01.bicep",
    )

    module_path = "opcua-connector-template-2026-07-01.bicep"
    assert module_path in create_module
    assert module_path in upgrade_module
    assert module_path not in update_extensions
    condition = "if (!empty(opcuaConnectorVersion) && opcUaFeatureMode != 'Disabled')"
    assert condition in create_module
    assert condition in upgrade_module
    assert "uniqueString(aioInstance.name)" in create_module
    assert "uniqueString(aioInstanceName)" in upgrade_module
    assert "aioApiVersion == '2026-07-01'" in upgrade_dispatcher
    assert "deploy-release-resources-2026-07-01.bicep" in upgrade_dispatcher
    assert "output connectorTemplateName" not in connector_module
    assert "opcUaConnectorTemplateName" not in upgrade_dispatcher
    assert "opcUaConnectorTemplateName" not in upgrade_module


def test_upgrade_manifest_separates_extensions_from_release_resources(workspace):
    manifest = yaml.safe_load(
        _read(workspace, "manifests/aio-upgrade/manifest.yaml")
    )
    steps = manifest["steps"]
    names = [step.get("name") for step in steps if "name" in step]
    assert names == [
        "resolve-extensions",
        "update-extensions",
        "deploy-release-resources",
    ]

    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )
    assert "akriConnectorTemplates" not in update_extensions
    assert "opcua-connector-template" not in update_extensions
    assert "param aioInstanceName" not in update_extensions
    assert "param customLocationId" not in update_extensions

    resolve_aio = _read(workspace, "templates/aio/resolve-aio.bicep")
    assert "output aioInstanceName" not in resolve_aio


def test_release_specific_settings_reach_the_selected_create_module(workspace):
    releases_dir = workspace / "parameters" / "aio-releases"
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )
    for release_path in sorted(releases_dir.glob("*.yaml")):
        release = yaml.safe_load(release_path.read_text(encoding="utf-8"))
        configuration = release["aioReleaseConfiguration"]
        if not configuration:
            continue

        module = _read(
            workspace,
            f"templates/aio/modules/instance-{release['aioApiVersion']}.bicep",
        )
        extension = configuration.get("extension", {})
        resources = configuration.get("resources", {})

        for setting in extension:
            assert setting in module, (
                f"{release_path.name}: selected create module does not "
                f"interpret extension setting {setting}"
            )
            assert setting in update_extensions, (
                f"{release_path.name}: extension updater does not interpret "
                f"release setting {setting}"
            )

        if resources:
            upgrade_module = (
                workspace
                / "templates"
                / "aio"
                / "upgrade"
                / "modules"
                / f"deploy-release-resources-{release['aioApiVersion']}.bicep"
            )
            assert upgrade_module.is_file(), (
                f"{release_path.name}: selected API generation has no typed "
                "upgrade release-resources module"
            )
            upgrade_module_text = upgrade_module.read_text(encoding="utf-8")
            for resource_kind in resources:
                assert resource_kind in module, (
                    f"{release_path.name}: selected create module does not "
                    f"interpret release resource {resource_kind}"
                )
                assert resource_kind in upgrade_module_text, (
                    f"{release_path.name}: selected upgrade module does not "
                    f"interpret release resource {resource_kind}"
                )

            connector = resources.get("opcUaConnector", {})
            if connector.get("version"):
                assert "opcua-connector-template-" in module, (
                    f"{release_path.name}: selected create module does not deploy "
                    "the configured OPC UA connector template"
                )
                assert "opcua-connector-template-" in upgrade_module_text, (
                    f"{release_path.name}: selected upgrade module does not deploy "
                    "the configured OPC UA connector template"
                )

        if extension.get("wasmGraphControllerMqttTrust"):
            for setting in (
                "dataFlows.values.wasmGraphController.mqttBroker.caCertConfigMapRef",
                "dataFlows.values.wasmGraphController.mqttBroker.caCertFileName",
            ):
                assert setting in module, (
                    f"{release_path.name}: selected create module does not "
                    f"apply {setting}"
                )


def test_release_configuration_consumers_use_the_grouped_contract(workspace):
    instance_modules = sorted(
        (workspace / "templates" / "aio" / "modules").glob("instance-*.bicep")
    )
    upgrade_modules = sorted(
        (workspace / "templates" / "aio" / "upgrade" / "modules").glob(
            "deploy-release-resources-*.bicep"
        )
    )
    assert instance_modules, "no versioned AIO instance modules were discovered"
    assert len(upgrade_modules) == len(instance_modules), (
        "release-resource generation modules do not match the install generations"
    )

    consumers = instance_modules + upgrade_modules + [
        workspace / "templates" / "aio" / "upgrade" / "update-extensions.bicep",
    ]

    for consumer in consumers:
        source = consumer.read_text(encoding="utf-8")
        assert "aioReleaseConfiguration.?aioConfigurationOverrides" not in source, (
            f"{consumer}: still reads the pre-grouped release configuration key"
        )


def test_connector_template_uses_the_released_supervisor_artifacts(workspace):
    module = _read(
        workspace,
        "templates/aio/modules/opcua-connector-template-2026-07-01.bicep",
    )

    assert (
        "Microsoft.IoTOperations/instances/akriConnectorTemplates@2026-07-01"
        in module
    )
    assert "azureiotoperationsconnectorforopcua-" in module
    assert "aio-connectors/opcua-metadata:${connectorVersion}" in module
    assert "imageName: 'azureiotoperations/aio-connectors/supervisor'" in module
    assert "tag: connectorVersion" in module


def test_2608_wasm_mqtt_trust_flows_through_install_and_upgrade(workspace):
    create_module = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    for setting in (
        "dataFlows.values.wasmGraphController.mqttBroker.caCertConfigMapRef",
        "dataFlows.values.wasmGraphController.mqttBroker.caCertFileName",
    ):
        assert setting in create_module
        assert setting in update_extensions
    assert "wasmGraphControllerMqttTrust" in create_module
    assert "wasmGraphControllerMqttTrust" in update_extensions


@pytest.mark.parametrize("release_name", ["2608", "2609"])
def test_cert_manager_defaults_match_the_release(workspace, release_name):
    release = _release(workspace, release_name)

    assert release["certManagerConfigurationOverrides"] == {
        "trust-manager.secretTargets.enabled": "false",
        "trust-manager.secretTargets.authorizedSecretsAll": "false",
    }

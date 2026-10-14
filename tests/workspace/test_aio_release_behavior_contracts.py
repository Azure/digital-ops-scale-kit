"""Release behavior that differs across AIO versions and API generations."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from siteops.compilation import TemplateCompilationSession
from siteops.models import Site
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DeploymentOperation,
    LiteralValue,
    PlanDisposition,
    PlanIntent,
    resolve_plan_value,
)
from tests.actions_expressions import parse
from tests.test_orchestrator_planning import _arm_template, _RecordingToolRunner
from tests.workspace.conftest import az_path


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


def _release(workspace: Path, release: str) -> dict:
    path = workspace / "parameters" / "aio-releases" / f"{release}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _arm_value(value, template, parameters):
    """Evaluate only the ARM functions used by the connector deployment guard."""
    if not isinstance(value, str) or not value.startswith("["):
        return value

    def try_get(value, *keys):
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        return value

    def parameter(name):
        if name in parameters:
            return parameters[name]
        declaration = template["parameters"][name]
        if "defaultValue" in declaration:
            return declaration["defaultValue"]
        assert declaration.get("nullable") is True, f"Missing required parameter: {name}"
        return None

    def evaluate(node):
        if node.kind == "literal":
            return node.value
        if node.kind == "index":
            container, key = (evaluate(child) for child in node.children)
            return container[key]
        assert node.kind == "call", node
        args = [evaluate(child) for child in node.children]
        functions = {
            "parameters": parameter,
            "variables": lambda name: _arm_value(template["variables"][name], template, parameters),
            "tryget": try_get,
            "coalesce": lambda *values: next((item for item in values if item is not None), None),
            "createobject": dict,
            "string": str,
            "empty": lambda item: not item,
            "not": lambda item: not item,
            "and": lambda *items: all(items),
            "equals": lambda left, right: left == right,
        }
        assert node.value in functions, f"Unsupported ARM guard function: {node.value}"
        return functions[node.value](*args)

    return evaluate(parse(value[1:-1]))


def _resources(template):
    resources = template.get("resources", {})
    return list(resources.values()) if isinstance(resources, dict) else resources


@pytest.fixture(scope="module", params=[
    "templates/aio/instance.bicep",
    "templates/aio/upgrade/deploy-release-resources.bicep",
], ids=["install", "upgrade"])
def connector_arm(request, workspace):
    result = subprocess.run(
        [az_path(), "bicep", "build", "--file", str(workspace / request.param), "--stdout"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("features", "configured", "expected"), [
    pytest.param(None, True, True, id="default"),
    pytest.param({}, True, True, id="empty-features"),
    pytest.param({"opcua": {"settings": {}}}, True, True, id="default-mode"),
    pytest.param({"opcua": {"mode": "Stable", "settings": {}}}, True, True, id="stable"),
    pytest.param({"opcua": {"mode": "Preview", "settings": {}}}, True, True, id="preview"),
    pytest.param({"opcua": {"mode": "Disabled", "settings": {}}}, True, False, id="disabled"),
    pytest.param({"connectors": {"mode": "Disabled", "settings": {}}}, True, False, id="legacy-disabled"),
    pytest.param({
        "opcua": {"mode": "Stable", "settings": {}},
        "connectors": {"mode": "Disabled", "settings": {}},
    }, True, True, id="canonical-stable-wins"),
    pytest.param({
        "opcua": {"mode": "Disabled", "settings": {}},
        "connectors": {"mode": "Stable", "settings": {}},
    }, True, False, id="canonical-disabled-wins"),
    pytest.param({
        "opcua": {"settings": {}},
        "connectors": {"mode": "Disabled", "settings": {}},
    }, True, True, id="canonical-default-wins"),
    pytest.param({"mqtt": {"mode": "Disabled", "settings": {}}}, True, True, id="other-disabled"),
    pytest.param(None, False, False, id="no-version"),
    pytest.param({"opcua": {"mode": "Stable", "settings": {}}}, False, False, id="stable-no-version"),
])
def test_opcua_template_respects_effective_feature_mode(connector_arm, features, configured, expected):
    parameters = {
        "aioApiVersion": "2026-07-01",
        "aioReleaseConfiguration": {"resources": {"opcUaConnector": {"version": "1.4.14"}}}
        if configured else {},
    }
    if features is not None:
        parameters["features"] = features
    connectors = []
    for module in _resources(connector_arm):
        template = module.get("properties", {}).get("template", {})
        for child in _resources(template):
            nested = child.get("properties", {}).get("template", {})
            if any(resource["type"] == "Microsoft.IoTOperations/instances/akriConnectorTemplates"
                   for resource in _resources(nested)):
                connectors.append((module, template, child))
    assert len(connectors) == 1, "Expected one typed OPC UA connector deployment"
    module, template, connector = connectors[0]
    assert _arm_value(module["condition"], connector_arm, parameters) is True
    forwarded = module["properties"]["parameters"]
    child_parameters = {
        name: _arm_value(forwarded[name]["value"], connector_arm, parameters)
        for name in ("features", "aioReleaseConfiguration") if name in forwarded
    }
    assert _arm_value(connector.get("condition", True), template, child_parameters) is expected


def test_upgrade_release_resources_use_resolved_instance_features(workspace):
    manifest = yaml.safe_load(_read(workspace, "manifests/aio-upgrade/manifest.yaml"))
    step = next(step for step in manifest["steps"] if step.get("name") == "deploy-release-resources")
    assert "parameters/inputs/aio-upgrade-deploy-release-resources.yaml" in step["parameters"]
    inputs = yaml.safe_load(_read(workspace, "parameters/inputs/aio-upgrade-deploy-release-resources.yaml"))
    assert inputs["features"] == "{{ steps.resolve-aio.outputs.features }}"


def test_aio_install_forwards_site_extension_settings_over_release_values(workspace, tmp_path, monkeypatch):
    overrides = {
        "aioConfigurationOverrides": {"example.setting": "site-aio"},
        "certManagerConfigurationOverrides": {"trust-manager.secretTargets.enabled": "true"},
        "secretStoreConfigurationOverrides": {"rotationPollIntervalInSeconds": "240"},
    }
    site = Site(
        name="settings", subscription="sub", resource_group="rg", location="eastus",
        properties={"aioRelease": "2608", "deployOptions": {
            "enableGlobalSite": False, "enableEdgeSite": False, "enableSecretSync": False, "enableCertManager": True,
        }},
        parameters={"clusterName": "cluster", **overrides},
    )
    runner = _RecordingToolRunner()

    def compile_locally(argv, timeout):
        result = runner(argv, timeout)
        if argv[1:3] == ("bicep", "build"):
            source = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
            declarations = re.findall(r"^param\s+(\w+)\s+(\S+)", source, re.MULTILINE)
            parameters = {
                name: {"type": kind if kind in {"string", "bool", "int", "array", "object"} else "object",
                       "defaultValue": None}
                for name, kind in declarations
            }
            Path(argv[argv.index("--outfile") + 1]).write_text(json.dumps(_arm_template(parameters)), encoding="utf-8")
        return result

    session = TemplateCompilationSession(command_runner=compile_locally, tool_resolver=lambda name: str(tmp_path / name))
    monkeypatch.setattr("siteops.orchestrator.TemplateCompilationSession", lambda: session)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=AssertionError("No real tools")))
    result = Orchestrator(workspace).build_plan(
        workspace / "manifests" / "aio-install" / "manifest.yaml", sites=[site], intent=PlanIntent.EXECUTABLE,
    )
    assert result.executable, result.diagnostics
    operations = {op.identity.step: op for op in result.plan.targets[0].operations}
    release = _release(workspace, "2608")
    for step, keys in (
        ("aio-instance", ("aioConfigurationOverrides",)),
        ("aio-enablement", ("certManagerConfigurationOverrides", "secretStoreConfigurationOverrides")),
    ):
        operation = operations[step]
        assert operation.disposition is PlanDisposition.EXECUTE
        assert isinstance(operation.details, DeploymentOperation)
        values = {entry.key.value: entry.value for entry in operation.details.parameters.entries
                  if isinstance(entry.key, LiteralValue)}
        for key in keys:
            expected = {**release.get(key, {}), **overrides[key]}
            assert key in values
            assert resolve_plan_value(values[key], {}) == expected


def test_schema_registry_namespace_fits_the_service_limit_for_every_guided_site_name(workspace):
    # DeviceRegistry rejects a schema registry namespace longer than 32 characters at preflight.
    template = _read(workspace, "templates/deps/schema-registry.bicep")
    contract = yaml.safe_load(_read(workspace, "manifests/aio-install/inputs.yaml"))
    site = next(item for item in contract["inputs"] if item["name"] == "siteName")
    common = yaml.safe_load(_read(workspace, "parameters/common/common.yaml"))
    suffix = common["schemaRegistryName"].removeprefix("{{ site.name }}")
    # The longest guided Site name produces a registry name over the limit, so the long form is reachable.
    assert site["maxLength"] + len(suffix) > 32
    assert re.search(r"(?m)^\s*namespace: schemaRegistryNamespace$", template)
    assert not re.search(r"(?m)^\s*namespace: schemaRegistryName$", template)
    # A name that fits stays unchanged, so existing registries keep their namespace.
    assert "length(schemaRegistryName) <= 32\n  ? schemaRegistryName" in template
    long_form = "'${toLower(take(schemaRegistryName, 26))}${take(uniqueString(resourceGroup().id, schemaRegistryName), 6)}'"
    assert long_form in template
    assert 26 + 6 == 32


def test_security_pki_defaults_follow_released_aio_behavior(workspace):
    create_2025 = _read(
        workspace,
        "templates/aio/modules/instance-2025-10-01.bicep",
    )
    create_2026_03 = _read(
        workspace,
        "templates/aio/modules/instance-2026-03-01.bicep",
    )
    create_2026_07 = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    application_uri = "connectors.values.securityPki.applicationUri"
    subject_name = "connectors.values.securityPki.subjectName"

    # The application URI starts with the 2026-03-01 generation. Subject name
    # starts in releases 2605 and 2606, then becomes a 2026-07-01 baseline.
    assert application_uri not in create_2025
    assert subject_name not in create_2025
    assert application_uri in create_2026_03
    assert subject_name in create_2026_03
    assert (
        "var configureSecurityPkiSubjectName = "
        "bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in create_2026_03
    )
    assert application_uri in create_2026_07
    assert subject_name in create_2026_07
    assert "deriveAioExtensionSuffix(clusterResourceId)" in create_2026_03

    for release in ("2603", "2604"):
        assert _release(workspace, release)["aioReleaseConfiguration"] == {}
    for release in ("2605", "2606"):
        extension = _release(workspace, release)["aioReleaseConfiguration"]["extension"]
        assert extension["securityPkiSubjectName"] is True
    for release in ("2607", "2608", "2609"):
        extension = (
            _release(workspace, release)["aioReleaseConfiguration"].get("extension", {})
        )
        assert "securityPkiSubjectName" not in extension

    assert (
        "var aioApplicationUriDefault = aioApiVersion == '2025-10-01'\n  ? {}"
        in update_extensions
    )
    assert (
        "var configureSecurityPkiSubjectName = aioApiVersion == '2026-07-01' "
        "|| bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in update_extensions
    )
    assert application_uri in update_extensions
    assert subject_name in update_extensions
    assert "contains(aio.configurationSettings" in update_extensions
    assert "aio.extensionSuffix" in update_extensions


def test_release_contract_does_not_opt_in_to_gds_manager(workspace):
    release_files = sorted(
        (workspace / "parameters" / "aio-releases").glob("*.yaml")
    )
    instance_modules = sorted(
        (workspace / "templates" / "aio" / "modules").glob("instance-*.bicep")
    )
    checked = release_files + instance_modules + [
        workspace / "templates" / "aio" / "upgrade" / "update-extensions.bicep"
    ]
    assert release_files and instance_modules

    for path in checked:
        assert "gdsManager" not in path.read_text(encoding="utf-8"), (
            f"{path}: Scale Kit configures published release requirements and "
            "does not opt in to GDS Manager"
        )

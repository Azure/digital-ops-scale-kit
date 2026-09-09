"""Executable plans for committed workspace entry points without cloud access.

Structural validation deliberately does not acquire schemas. These tests
exercise the real compiler and prepared inputs while allowing only local Azure
CLI version probes and Bicep builds. Missing local tools fail rather than
silently skipping this coverage.
"""

import copy
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from siteops.compilation import TemplateKind
from siteops.planning import (
    CapabilityKind,
    CapabilityStatus,
    DeploymentOperation,
    InputStatus,
    KubectlOperation,
    ListValue,
    LiteralValue,
    OutputValue,
    PlanDisposition,
    PlanIntent,
    PlanStatus,
    ResourceDisposition,
    SkipReasonCode,
)
from tests.workspace.conftest import az_path

_DIAGNOSTIC = re.compile(
    r"^.+\(\d+,\d+\)\s*:\s*(?:Error|Warning)\s+[\w-]+:",
    re.MULTILINE,
)
_TEMPLATES = {
    "resolve-aio": Path("templates/aio/resolve-aio.bicep"),
    "asset-resources": Path("templates/aio/assets/main.bicep"),
    "dataflow-resources": Path("templates/aio/dataflows/main.bicep"),
    "external-opc-ua-device": Path(
        "samples/resource-set-composition/external-provider.bicep"
    ),
}
_AIO_INSTALL_TEMPLATES = {
    "schema-registry": Path("templates/deps/schema-registry.bicep"),
    "adr-ns": Path("templates/deps/adr-ns.bicep"),
    "aio-enablement": Path("templates/aio/enablement.bicep"),
    "aio-instance": Path("templates/aio/instance.bicep"),
    "schema-registry-role": Path("templates/deps/schema-registry-role.bicep"),
    "resolve-aio": Path("templates/aio/resolve-aio.bicep"),
    "secretsync": Path("templates/secretsync/enable-secretsync.bicep"),
}


def _guard_local_compilation(
    monkeypatch,
    tmp_path,
    expected_templates,
):
    """Allow only Azure CLI version probes and local Bicep builds."""
    azure_cli = Path(az_path()).resolve()
    builds = []
    original_popen = subprocess.Popen
    original_run = subprocess.run

    def local_only_popen(argv, *args, **kwargs):
        assert not isinstance(argv, (str, bytes)), (
            "Shell commands are not permitted"
        )
        assert not kwargs.get("shell"), "Shell commands are not permitted"
        assert Path(argv[0]).resolve() == azure_cli, (
            f"Executable planning must not invoke kubectl or other tools: {argv}"
        )
        command = tuple(argv[1:])
        if command not in {
            ("version", "--output", "json"),
            ("bicep", "version"),
        }:
            assert len(command) == 6 and command[:3] == (
                "bicep",
                "build",
                "--file",
            ), f"Only local version probes and Bicep builds are allowed: {argv}"
            assert command[4] == "--outfile"
            source = Path(command[3]).resolve()
            assert source in expected_templates
            assert Path(command[5]).resolve().is_relative_to(
                tmp_path.resolve()
            )
            builds.append(source)
        return original_popen(argv, *args, **kwargs)

    def checked_run(argv, *args, **kwargs):
        result = original_run(argv, *args, **kwargs)
        if tuple(argv[1:3]) == ("bicep", "build"):
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            assert not _DIAGNOSTIC.search(output), output
        return result

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", local_only_popen)
    monkeypatch.setattr(subprocess, "run", checked_run)
    monkeypatch.setenv("AZURE_CORE_COLLECT_TELEMETRY", "0")
    return builds


@pytest.mark.parametrize(
    ("sample", "site_name", "selected_steps", "skipped_steps", "collections"),
    [
        pytest.param(
            "resource-set-basic",
            "catalog-basic",
            ["resolve-aio", "dataflow-resources"],
            ["asset-resources"],
            {"dataflows": 1},
            id="basic",
        ),
        pytest.param(
            "resource-set-composition",
            "catalog-composition",
            [
                "external-opc-plc-simulator",
                "external-opc-ua-device",
                "resolve-aio",
                "asset-resources",
                "dataflow-resources",
            ],
            [],
            {
                "devices": 1,
                "assets": 3,
                "dataflowEndpoints": 1,
                "dataflowProfiles": 1,
                "dataflows": 1,
            },
            id="composition",
        ),
    ],
)
def test_catalog_executable_plan(
    workspace,
    orchestrator,
    monkeypatch,
    tmp_path,
    sample,
    site_name,
    selected_steps,
    skipped_steps,
    collections,
):
    expected_templates = {
        (workspace / _TEMPLATES[step]).resolve()
        for step in selected_steps
        if step in _TEMPLATES
    }
    # Keep artifacts out of the workspace content. Guard at Popen as well as
    # run so even a regression to the executor's proxy path cannot start it.
    builds = _guard_local_compilation(
        monkeypatch,
        tmp_path,
        expected_templates,
    )
    result = orchestrator.build_plan(
        workspace / "samples" / sample / "manifest.yaml",
        intent=PlanIntent.EXECUTABLE,
    )

    assert not result.diagnostics, result.diagnostics
    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    plan = result.plan
    assert plan.intent is PlanIntent.EXECUTABLE
    assert [target.name for target in plan.targets] == [site_name]
    target = plan.targets[0]
    assert not target.diagnostics
    selected = {
        operation.identity.step: operation
        for operation in target.operations
        if operation.disposition is PlanDisposition.EXECUTE
    }
    assert selected
    assert list(selected) == selected_steps
    skipped = [
        operation for operation in target.operations
        if operation.disposition is not PlanDisposition.EXECUTE
    ]
    assert [operation.identity.step for operation in skipped] == skipped_steps
    for operation in skipped:
        assert operation.disposition is PlanDisposition.SKIP
        assert operation.skip_reason.code is SkipReasonCode.CONDITION_FALSE
        assert operation.details.template_unit_key is None

    assert Counter(builds) == Counter(expected_templates)
    units = {unit.key: unit for unit in plan.template_units}
    assert len(units) == len(expected_templates)
    assert {unit.identity.source.path for unit in units.values()} == expected_templates
    for operation in selected.values():
        details = operation.details
        assert details.input_status is InputStatus.PREPARED
        if not isinstance(details, DeploymentOperation):
            assert isinstance(details, KubectlOperation)
            continue
        unit = units[details.template_unit_key]
        assert unit.key.template_kind is TemplateKind.BICEP
        assert unit.identity.compiler is not None
        assert unit.identity.compiled_output_digest
        assert details.template == unit.identity.source.path
        assert details.parameters is not None
        assert all(isinstance(entry.key, LiteralValue) for entry in details.parameters.entries)
        parameters = {entry.key.value: entry.value for entry in details.parameters.entries}
        assert parameters
        assert parameters.keys() <= unit.parameter_names
        assert {
            parameter.name for parameter in unit.parameters
            if parameter.is_required
        } <= parameters.keys()
        if operation.identity.step in {"asset-resources", "dataflow-resources"}:
            assert isinstance(parameters["customLocationName"], OutputValue)
            reference = parameters["customLocationName"].reference
            assert reference.source == selected["resolve-aio"].identity
            assert reference.output_path == ("customLocationName",)
            family = (
                ("devices", "assets")
                if operation.identity.step == "asset-resources"
                else ("dataflowEndpoints", "dataflowProfiles", "dataflows")
            )
            for collection in family:
                assert isinstance(parameters[collection], ListValue)
                assert len(parameters[collection].items) == collections.get(collection, 0)
            if operation.identity.step == "dataflow-resources":
                assert "adrNamespaceName" not in parameters

    assert target.composition is not None
    assert Counter(
        resource.identity.collection
        for resource in target.composition.resources
        if resource.disposition is ResourceDisposition.APPLY
    ) == collections
    external = [
        resource for resource in target.composition.resources
        if resource.disposition is ResourceDisposition.EXTERNAL
    ]
    assert [resource.identity.collection for resource in external] == (
        ["devices"] if sample == "resource-set-composition" else []
    )

    deployments = {
        operation.identity for operation in selected.values()
        if isinstance(operation.details, DeploymentOperation)
    }
    expected_capabilities = {
        CapabilityKind.ARM_CONTROL_PLANE: (CapabilityStatus.AVAILABLE, deployments),
        CapabilityKind.BICEP_COMPILER: (CapabilityStatus.AVAILABLE, deployments),
    }
    if sample == "resource-set-composition":
        simulator = {selected["external-opc-plc-simulator"].identity}
        expected_capabilities.update({
            CapabilityKind.KUBECTL: (CapabilityStatus.AVAILABLE, simulator),
            CapabilityKind.ARC_PROXY: (CapabilityStatus.UNKNOWN, simulator),
        })
    assert {
        capability.kind: (capability.status, set(capability.required_by))
        for capability in plan.capabilities
    } == expected_capabilities


def test_aio_install_executable_plan_omits_nullable_instance_parameters(
    workspace,
    orchestrator,
    monkeypatch,
    tmp_path,
):
    expected_templates = {
        (workspace / template).resolve()
        for template in _AIO_INSTALL_TEMPLATES.values()
    }
    builds = _guard_local_compilation(
        monkeypatch,
        tmp_path,
        expected_templates,
    )
    site = copy.deepcopy(orchestrator.load_site("munich-dev"))
    site.properties["deployOptions"].update(
        {
            "enableSecretSync": True,
            "enableWorkloadIdentity": True,
        }
    )

    result = orchestrator.build_plan(
        workspace / "manifests" / "aio-install.yaml",
        intent=PlanIntent.EXECUTABLE,
        sites=[site],
    )

    assert not result.diagnostics, result.diagnostics
    assert result.status is PlanStatus.PLANNED
    assert result.executable
    assert result.plan is not None
    target = result.plan.targets[0]
    selected = {
        operation.identity.step: operation
        for operation in target.operations
        if operation.disposition is PlanDisposition.EXECUTE
    }
    assert list(selected) == list(_AIO_INSTALL_TEMPLATES)
    skipped = {
        operation.identity.step: operation
        for operation in target.operations
        if operation.disposition is PlanDisposition.SKIP
    }
    assert set(skipped) == {"global-edge-site", "edge-site"}
    assert skipped["global-edge-site"].skip_reason is not None
    assert (
        skipped["global-edge-site"].skip_reason.code
        is SkipReasonCode.SCOPE_MISMATCH
    )
    assert skipped["edge-site"].skip_reason is not None
    assert (
        skipped["edge-site"].skip_reason.code
        is SkipReasonCode.CONDITION_FALSE
    )
    assert Counter(builds) == Counter(expected_templates)

    details = selected["aio-instance"].details
    assert isinstance(details, DeploymentOperation)
    assert details.template_unit_key is not None
    assert details.parameters is not None
    unit = result.plan.template_unit(details.template_unit_key)
    schema = {parameter.name: parameter for parameter in unit.parameters}
    parameters = {
        entry.key.value: entry.value
        for entry in details.parameters.entries
        if isinstance(entry.key, LiteralValue)
    }
    assert {
        parameter.name
        for parameter in unit.parameters
        if parameter.is_required
    } <= parameters.keys()
    for name in ("features", "userAssignedIdentity"):
        assert schema[name].nullable
        assert not schema[name].has_default
        assert not schema[name].is_required
        assert name not in parameters

"""Executable plans for committed catalog samples, without Azure or cluster access.

Structural validation deliberately does not acquire schemas. These two entry
points exercise the real compiler and prepared inputs instead: two Bicep builds
for the basic sample and four for composition, including their local modules.
Missing local tools fail rather than silently skipping this coverage.
"""

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
    azure_cli = Path(az_path()).resolve()
    builds = []
    original_popen = subprocess.Popen
    original_run = subprocess.run

    def local_only_popen(argv, *args, **kwargs):
        assert not isinstance(argv, (str, bytes)), "Shell commands are not permitted"
        assert not kwargs.get("shell"), "Shell commands are not permitted"
        assert Path(argv[0]).resolve() == azure_cli, (
            f"Executable planning must not invoke kubectl or other tools: {argv}"
        )
        command = tuple(argv[1:])
        if command not in {("version", "--output", "json"), ("bicep", "version")}:
            assert len(command) == 6 and command[:3] == (
                "bicep", "build", "--file"
            ), f"Only local version probes and Bicep builds are allowed: {argv}"
            assert command[4] == "--outfile"
            source = Path(command[3]).resolve()
            assert source in expected_templates
            assert Path(command[5]).resolve().is_relative_to(tmp_path.resolve())
            builds.append(source)
        return original_popen(argv, *args, **kwargs)

    def checked_run(argv, *args, **kwargs):
        result = original_run(argv, *args, **kwargs)
        if tuple(argv[1:3]) == ("bicep", "build"):
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            assert not _DIAGNOSTIC.search(output), output
        return result

    # Keep artifacts out of the workspace content. Guard at Popen as well as
    # run so even a regression to the executor's proxy path cannot start it.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(subprocess, "Popen", local_only_popen)
    monkeypatch.setattr(subprocess, "run", checked_run)
    monkeypatch.setenv("AZURE_CORE_COLLECT_TELEMETRY", "0")
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
            if not parameter.has_default
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

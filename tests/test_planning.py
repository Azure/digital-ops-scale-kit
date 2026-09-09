# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for immutable prepared plan models."""

from pathlib import Path

import pytest

from siteops.compilation import (
    CompilationKey,
    DependencyCoverage,
    DependencyIdentity,
    PreparedTemplateUnit,
    SourceIdentity,
    TemplateCompilationIdentity,
    TemplateKind,
    TemplateParameter,
    VersionProvenance,
)
from siteops.planning import (
    ArmTagWaitOperation,
    CapabilityKind,
    CapabilityProviderIdentity,
    CapabilityStatus,
    CompositionReference,
    CompositionRequirement,
    CompositionResource,
    CompositionSource,
    DataReference,
    DeploymentOperation,
    DeploymentPlan,
    DiagnosticSeverity,
    InputStatus,
    InterpolatedValue,
    KubectlOperation,
    ListValue,
    LiteralValue,
    MappingEntry,
    MappingValue,
    OperationIdentity,
    OperationKind,
    OperationScope,
    OutputValue,
    PlanBuildResult,
    PlanCapability,
    PlanComposition,
    PlanDiagnostic,
    PlanDisposition,
    PlanExecutionMode,
    PlanIntent,
    PlanProjection,
    PlanSkipReason,
    PlanStatus,
    PlanStep,
    PlanValueResolutionError,
    PreparedOperation,
    PreparedTarget,
    ResourceDisposition,
    ResourceIdentity,
    SkipReasonCode,
    TargetKind,
    classify_plan_value,
    collect_data_references,
    render_plain_plan,
    resolve_plan_value,
    serialize_plan,
    serialize_plan_json,
)


def _template_unit(
    *,
    parameter: str = "connection",
    nullable: bool = False,
) -> PreparedTemplateUnit:
    source = SourceIdentity(
        path=Path("templates/main.json"),
        content_digest="source-digest",
        size_bytes=1,
    )
    key = CompilationKey(
        source_path=source.path,
        source_content_digest=source.content_digest,
        template_kind=TemplateKind.ARM_JSON,
        compiler_fingerprint="arm-json",
        configuration_digest="none",
        invocation=("read-arm-json",),
    )
    return PreparedTemplateUnit(
        key=key,
        identity=TemplateCompilationIdentity(
            source=source,
            compiler_driver=None,
            compiler=None,
            configuration=None,
            dependencies=DependencyIdentity(
                coverage=DependencyCoverage.NOT_APPLICABLE,
            ),
            compiled_output_digest=source.content_digest,
        ),
        parameters=(
            TemplateParameter(
                name=parameter,
                type="string",
                secure=False,
                has_default=False,
                nullable=nullable,
            ),
        ),
    )


def _deployment_details() -> DeploymentOperation:
    return DeploymentOperation(
        template=Path("templates/main.bicep"),
        input_status=InputStatus.DESCRIBED,
    )


def _operation(
    *,
    target: str = "munich",
    step: str = "deploy",
    sequence: int = 1,
) -> PreparedOperation:
    details = _deployment_details()
    plan_step = PlanStep(
        name=step,
        sequence=sequence,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=details,
    )
    return PreparedOperation(
        identity=OperationIdentity(target=target, step=step),
        step=plan_step,
        disposition=PlanDisposition.EXECUTE,
        details=details,
    )


def _target(
    *operations: PreparedOperation,
    name: str = "munich",
) -> PreparedTarget:
    return PreparedTarget(
        name=name,
        kind=TargetKind.RESOURCE_GROUP,
        subscription="sub",
        resource_group="rg-munich",
        location="eastus",
        operations=operations or (_operation(target=name),),
    )


def _tool_identity(
    *,
    provider: str = "azure-cli",
    path: Path = Path("tools/az"),
    version: str | None = "2.87.0",
) -> CapabilityProviderIdentity:
    return CapabilityProviderIdentity(
        name=provider,
        executable_path=path,
        version=version,
        version_provenance=(
            VersionProvenance.KNOWN
            if version is not None
            else VersionProvenance.UNKNOWN
        ),
    )


def test_operation_identity_uses_only_target_and_step():
    first = OperationIdentity(target="munich", step="deploy")
    second = OperationIdentity(target="munich", step="deploy")

    assert first == second
    assert hash(first) == hash(second)


@pytest.mark.parametrize(
    ("target", "step"),
    [
        ("", "deploy"),
        ("munich", ""),
        (" ", "deploy"),
        ("munich", " "),
    ],
)
def test_operation_identity_requires_non_empty_parts(target, step):
    with pytest.raises(ValueError, match="must be non-empty"):
        OperationIdentity(target=target, step=step)


def test_data_reference_preserves_logical_source_and_output_path():
    source = OperationIdentity(target="subscription-target", step="resolve")

    reference = DataReference.from_dotted_path(
        source,
        "resource.identity.id",
    )

    assert reference.source is source
    assert reference.output_path == ("resource", "identity", "id")


def test_plan_capability_requires_operations():
    with pytest.raises(ValueError, match="at least one operation"):
        PlanCapability(
            kind=CapabilityKind.ARM_CONTROL_PLANE,
            status=CapabilityStatus.UNKNOWN,
            required_by=(),
        )


def test_plan_capability_rejects_duplicate_operations():
    identity = OperationIdentity(target="munich", step="deploy")

    with pytest.raises(ValueError, match="must be unique"):
        PlanCapability(
            kind=CapabilityKind.ARM_CONTROL_PLANE,
            status=CapabilityStatus.UNKNOWN,
            required_by=(identity, identity),
        )


def test_available_plan_capability_requires_provider():
    with pytest.raises(ValueError, match="require a provider"):
        PlanCapability(
            kind=CapabilityKind.ARM_CONTROL_PLANE,
            status=CapabilityStatus.AVAILABLE,
            required_by=(
                OperationIdentity(target="munich", step="deploy"),
            ),
        )


def test_missing_plan_capability_rejects_provider():
    with pytest.raises(ValueError, match="Missing plan capabilities"):
        PlanCapability(
            kind=CapabilityKind.ARM_CONTROL_PLANE,
            status=CapabilityStatus.MISSING,
            required_by=(
                OperationIdentity(target="munich", step="deploy"),
            ),
            provider=_tool_identity(),
        )


def test_unknown_plan_capability_records_selected_provider():
    capability = PlanCapability(
        kind=CapabilityKind.ARC_PROXY,
        status=CapabilityStatus.UNKNOWN,
        required_by=(
            OperationIdentity(target="munich", step="apply"),
        ),
        provider=_tool_identity(),
    )

    assert capability.provider is not None


def test_deployment_plan_rejects_duplicate_capability_kinds():
    target = _target()
    capability = PlanCapability(
        kind=CapabilityKind.ARM_CONTROL_PLANE,
        status=CapabilityStatus.UNKNOWN,
        required_by=(target.operations[0].identity,),
        provider=_tool_identity(),
    )

    with pytest.raises(ValueError, match="kinds must be unique"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target,),
            capabilities=(capability, capability),
        )


def test_deployment_plan_rejects_unknown_capability_operation():
    target = _target()

    with pytest.raises(ValueError, match="unknown operation"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.UNKNOWN,
                    required_by=(
                        OperationIdentity(
                            target="other",
                            step="deploy",
                        ),
                    ),
                    provider=_tool_identity(),
                ),
            ),
        )


def test_skipped_operation_cannot_require_capability():
    operation = _operation()
    skipped = PreparedOperation(
        identity=operation.identity,
        step=operation.step,
        disposition=PlanDisposition.SKIP,
        details=operation.details,
        skip_reason=PlanSkipReason(
            code=SkipReasonCode.CONDITION_FALSE,
            detail="Condition not met.",
        ),
    )
    target = _target(skipped)

    with pytest.raises(ValueError, match="Skipped operations"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(skipped.step,),
            targets=(target,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.UNKNOWN,
                    required_by=(skipped.identity,),
                    provider=_tool_identity(),
                ),
            ),
        )


def test_describe_plan_cannot_carry_capabilities():
    target = _target()

    with pytest.raises(ValueError, match="Describe plans"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.UNKNOWN,
                    required_by=(target.operations[0].identity,),
                    provider=_tool_identity(),
                ),
            ),
        )


def test_executable_plan_requires_capability_coverage():
    unit = _template_unit()
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.DESCRIBED,
        ),
    )
    operation = PreparedOperation(
        identity=OperationIdentity(target="munich", step="deploy"),
        step=step,
        disposition=PlanDisposition.EXECUTE,
        details=DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.PREPARED,
            parameters=MappingValue(()),
            template_unit_key=unit.key,
        ),
    )

    with pytest.raises(ValueError, match="missing a required capability"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=1,
            steps=(step,),
            targets=(_target(operation),),
            template_units=(unit,),
        )


def test_executable_operation_template_matches_template_unit():
    unit = _template_unit()
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=DeploymentOperation(
            template=Path("templates/other.json"),
            input_status=InputStatus.DESCRIBED,
        ),
    )
    operation = PreparedOperation(
        identity=OperationIdentity(target="munich", step="deploy"),
        step=step,
        disposition=PlanDisposition.EXECUTE,
        details=DeploymentOperation(
            template=Path("templates/other.json"),
            input_status=InputStatus.PREPARED,
            parameters=MappingValue(()),
            template_unit_key=unit.key,
        ),
    )

    with pytest.raises(ValueError, match="must match its template unit"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=1,
            steps=(step,),
            targets=(_target(operation),),
            template_units=(unit,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.AVAILABLE,
                    required_by=(operation.identity,),
                    provider=_tool_identity(),
                ),
            ),
        )


def test_recursive_value_preserves_deferred_mapping_keys():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "dynamicKey")
    value = MappingValue(
        entries=(
            MappingEntry(
                key=OutputValue(reference),
                value=ListValue(
                    items=(
                        LiteralValue("prefix"),
                        InterpolatedValue(
                            parts=("value-", reference),
                        ),
                    )
                ),
            ),
        )
    )

    assert value.entries[0].key == OutputValue(reference)
    assert value.entries[0].value.items[1].parts == ("value-", reference)


def test_recursive_collections_normalize_to_immutable_tuples():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference(source=source, output_path=["value"])
    interpolated = InterpolatedValue(parts=["prefix-", reference])
    values = ListValue(items=[interpolated])
    mapping = MappingValue(
        entries=[MappingEntry(key=LiteralValue("key"), value=values)]
    )

    assert reference.output_path == ("value",)
    assert interpolated.parts == ("prefix-", reference)
    assert values.items == (interpolated,)
    assert mapping.entries == (
        MappingEntry(key=LiteralValue("key"), value=values),
    )


def test_literal_value_rejects_mutable_containers():
    with pytest.raises(TypeError, match="must be scalar"):
        LiteralValue({"mutable": True})


def test_interpolated_value_requires_a_data_reference():
    with pytest.raises(ValueError, match="data reference"):
        InterpolatedValue(parts=("literal-only",))


def test_classifier_distinguishes_full_and_embedded_output_references():
    source = OperationIdentity(target="munich", step="resolve")
    available = {"resolve": source}

    complete = classify_plan_value(
        "{{ steps.resolve.outputs.resource }}",
        available,
    )
    embedded = classify_plan_value(
        "prefix/{{ steps.resolve.outputs.resource.id }}/suffix",
        available,
    )

    assert complete == OutputValue(
        DataReference.from_dotted_path(source, "resource")
    )
    assert embedded == InterpolatedValue(
        (
            "prefix/",
            DataReference.from_dotted_path(source, "resource.id"),
            "/suffix",
        )
    )


def test_classifier_walks_lists_mapping_values_and_mapping_keys():
    source = OperationIdentity(target="munich", step="resolve")
    available = {"resolve": source}

    classified = classify_plan_value(
        {
            "{{ steps.resolve.outputs.key }}": [
                "{{ steps.resolve.outputs.first }}",
                {"nested": "value"},
            ]
        },
        available,
    )

    assert isinstance(classified, MappingValue)
    entry = classified.entries[0]
    assert entry.key == OutputValue(
        DataReference.from_dotted_path(source, "key")
    )
    assert isinstance(entry.value, ListValue)
    assert entry.value.items[0] == OutputValue(
        DataReference.from_dotted_path(source, "first")
    )


@pytest.mark.parametrize(
    "value",
    [
        "{{ steps.unknown.outputs.value }}",
        "{{ site.parameters.missing }}",
        "{ site.name }}",
        "{ steps.first.outputs.value }",
        "{{ steps.first.outputs.value }}}",
        "prefix {{ steps.resolve.outputs.value }",
    ],
)
def test_classifier_rejects_unavailable_or_malformed_templates(value):
    with pytest.raises(ValueError):
        classify_plan_value(value, {})


def test_classifier_rejects_later_step_reference():
    prior = OperationIdentity(target="munich", step="prior")

    with pytest.raises(ValueError, match="not an available prior operation"):
        classify_plan_value(
            "{{ steps.later.outputs.value }}",
            {"prior": prior},
        )


def test_classifier_does_not_treat_nested_rendered_data_as_a_template():
    value = "{'state': {'phase': 'succeeded'}}"

    assert classify_plan_value(value, {}) == LiteralValue(value)


def test_collect_data_references_deduplicates_in_first_appearance_order():
    source = OperationIdentity(target="munich", step="resolve")
    first = DataReference.from_dotted_path(source, "first")
    second = DataReference.from_dotted_path(source, "second")
    value = MappingValue(
        (
            MappingEntry(
                key=OutputValue(first),
                value=InterpolatedValue(("x", second, "y", first)),
            ),
        )
    )

    assert collect_data_references(value) == (first, second)


def test_resolve_plan_value_preserves_complex_whole_output():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "resource")
    value = OutputValue(reference)

    resolved = resolve_plan_value(
        value,
        {source: {"resource": {"id": "resource-id"}}},
    )

    assert resolved == {"id": "resource-id"}


def test_resolve_plan_value_unwraps_real_arm_output_envelopes():
    source = OperationIdentity(target="munich", step="resolve")
    outputs = {
        source: {
            "flat": {
                "type": "String",
                "value": "flat-value",
            },
            "resource": {
                "type": "Object",
                "value": {
                    "id": "resource-id",
                },
            },
        }
    }

    assert resolve_plan_value(
        OutputValue(DataReference.from_dotted_path(source, "flat")),
        outputs,
    ) == "flat-value"
    assert resolve_plan_value(
        OutputValue(DataReference.from_dotted_path(source, "resource.id")),
        outputs,
    ) == "resource-id"
    assert resolve_plan_value(
        InterpolatedValue(
            (
                "id=",
                DataReference.from_dotted_path(source, "flat"),
            )
        ),
        outputs,
    ) == "id=flat-value"


def test_resolve_plan_value_rejects_complex_embedded_output():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "resource")

    with pytest.raises(ValueError, match="cannot be embedded"):
        resolve_plan_value(
            InterpolatedValue(("prefix-", reference)),
            {source: {"resource": {"id": "resource-id"}}},
        )


def test_resolve_plan_value_rejects_non_string_deferred_key():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "key")
    value = MappingValue(
        (
            MappingEntry(
                key=OutputValue(reference),
                value=LiteralValue("value"),
            ),
        )
    )

    with pytest.raises(ValueError, match="must resolve to a string"):
        resolve_plan_value(value, {source: {"key": ["not", "text"]}})


def test_resolve_plan_value_rejects_duplicate_resolved_keys():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "key")
    value = MappingValue(
        (
            MappingEntry(
                key=LiteralValue("same"),
                value=LiteralValue("first"),
            ),
            MappingEntry(
                key=OutputValue(reference),
                value=LiteralValue("second"),
            ),
        )
    )

    with pytest.raises(ValueError, match="both resolve"):
        resolve_plan_value(value, {source: {"key": "same"}})


def test_resolve_plan_value_rejects_missing_output_path():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "missing.path")

    with pytest.raises(ValueError, match="has no output"):
        resolve_plan_value(OutputValue(reference), {source: {}})


def test_value_resolution_error_has_separate_publishable_text():
    source = OperationIdentity(target="private-site", step="private-step")
    reference = DataReference.from_dotted_path(source, "private.path")

    with pytest.raises(PlanValueResolutionError) as exc_info:
        resolve_plan_value(OutputValue(reference), {})

    assert "private-site" in str(exc_info.value)
    assert (
        exc_info.value.public_message
        == "A required prior-operation output is unavailable."
    )
    assert "private" not in exc_info.value.public_message


def test_preview_resolution_preserves_unavailable_output_reference():
    source = OperationIdentity(target="munich", step="resolve")
    reference = DataReference.from_dotted_path(source, "resource.id")

    resolved = resolve_plan_value(
        InterpolatedValue(("id=", reference)),
        {},
        mode=PlanExecutionMode.PREVIEW,
    )

    assert resolved == "id={{ steps.resolve.outputs.resource.id }}"


def test_prepared_operation_requires_matching_details():
    plan_step = PlanStep(
        name="apply",
        sequence=1,
        kind=OperationKind.KUBECTL,
        scope=OperationScope.TARGET,
        details=KubectlOperation(
            input_status=InputStatus.DESCRIBED,
            operation="apply",
            cluster_name=LiteralValue("cluster"),
            cluster_resource_group=LiteralValue("rg"),
            files=(LiteralValue("config.yaml"),),
        ),
    )
    with pytest.raises(TypeError, match="KubectlOperation"):
        PreparedOperation(
            identity=OperationIdentity(target="munich", step="apply"),
            step=plan_step,
            disposition=PlanDisposition.EXECUTE,
            details=_deployment_details(),
        )


def test_skipped_operation_requires_typed_reason():
    details = _deployment_details()
    with pytest.raises(ValueError, match="typed reason"):
        PreparedOperation(
            identity=OperationIdentity(target="munich", step="deploy"),
            step=PlanStep(
                name="deploy",
                sequence=1,
                kind=OperationKind.DEPLOYMENT,
                scope=OperationScope.RESOURCE_GROUP,
                details=details,
            ),
            disposition=PlanDisposition.SKIP,
            details=details,
        )


def test_execute_operation_rejects_skip_reason():
    details = _deployment_details()
    with pytest.raises(ValueError, match="cannot carry"):
        PreparedOperation(
            identity=OperationIdentity(target="munich", step="deploy"),
            step=PlanStep(
                name="deploy",
                sequence=1,
                kind=OperationKind.DEPLOYMENT,
                scope=OperationScope.RESOURCE_GROUP,
                details=details,
            ),
            disposition=PlanDisposition.EXECUTE,
            details=details,
            skip_reason=PlanSkipReason(
                code=SkipReasonCode.CONDITION_FALSE,
                detail="Condition did not match.",
            ),
        )


def test_non_deployment_operations_require_target_scope():
    wait = ArmTagWaitOperation(
        input_status=InputStatus.DESCRIBED,
        resource_id=LiteralValue("/resource"),
        tag_key=LiteralValue("state"),
        expected_value=LiteralValue("ready"),
        failure_pattern=None,
        timeout_minutes=5,
        poll_interval_seconds=10,
    )

    with pytest.raises(ValueError, match="target scope"):
        PlanStep(
            name="wait",
            sequence=1,
            kind=OperationKind.WAIT,
            scope=OperationScope.RESOURCE_GROUP,
            details=wait,
        )


def test_prepared_target_requires_operation_identity_to_match():
    with pytest.raises(ValueError, match="must match"):
        _target(_operation(target="seattle"))


def test_prepared_target_rejects_duplicate_operation_identity():
    operation = _operation()

    with pytest.raises(ValueError, match="Duplicate operation identity"):
        _target(operation, operation)


def test_prepared_target_rejects_duplicate_operation_sequence():
    with pytest.raises(ValueError, match="Duplicate operation sequence"):
        _target(
            _operation(step="first"),
            _operation(step="second"),
        )


def test_subscription_target_cannot_carry_resource_group():
    with pytest.raises(ValueError, match="cannot carry"):
        PreparedTarget(
            name="subscription-target",
            kind=TargetKind.SUBSCRIPTION,
            subscription="sub",
            resource_group="rg",
            location="eastus",
            operations=(),
        )


def test_subscription_target_rejects_empty_resource_group():
    with pytest.raises(ValueError, match="cannot carry"):
        PreparedTarget(
            name="subscription-target",
            kind=TargetKind.SUBSCRIPTION,
            subscription="sub",
            resource_group="",
            location="eastus",
            operations=(),
        )


def test_deployment_plan_rejects_duplicate_targets():
    target = _target()

    with pytest.raises(ValueError, match="must be unique"):
        DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target, target),
        )


def test_planned_result_requires_a_plan():
    with pytest.raises(ValueError, match="requires a plan"):
        PlanBuildResult(
            status=PlanStatus.PLANNED,
            executable=False,
            plan=None,
        )


def test_invalid_result_cannot_be_executable():
    with pytest.raises(ValueError, match="cannot be executable"):
        PlanBuildResult(
            status=PlanStatus.INVALID,
            executable=True,
            plan=None,
        )


def test_describe_plan_cannot_claim_executable():
    plan = DeploymentPlan(
        manifest_name="install",
        source_path=Path("manifests/install.yaml"),
        intent=PlanIntent.DESCRIBE,
        description=None,
        max_parallel_sites=1,
        steps=(_target().operations[0].step,),
        targets=(_target(),),
    )

    with pytest.raises(ValueError, match="executable preparation"):
        PlanBuildResult(
            status=PlanStatus.PLANNED,
            executable=True,
            plan=plan,
        )


def test_executable_result_rejects_error_diagnostics():
    unit = _template_unit()
    details = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(()),
        template_unit_key=unit.key,
    )
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.DESCRIBED,
        ),
    )
    target = PreparedTarget(
        name="munich",
        kind=TargetKind.RESOURCE_GROUP,
        subscription="sub",
        resource_group="rg-munich",
        location="eastus",
        operations=(
            PreparedOperation(
                identity=OperationIdentity(
                    target="munich",
                    step="deploy",
                ),
                step=step,
                disposition=PlanDisposition.EXECUTE,
                details=details,
            ),
        ),
    )
    plan = DeploymentPlan(
        manifest_name="install",
        source_path=Path("manifests/install.yaml"),
        intent=PlanIntent.EXECUTABLE,
        description=None,
        max_parallel_sites=1,
        steps=(step,),
        targets=(target,),
        template_units=(unit,),
        capabilities=(
            PlanCapability(
                kind=CapabilityKind.ARM_CONTROL_PLANE,
                status=CapabilityStatus.AVAILABLE,
                required_by=(target.operations[0].identity,),
                provider=_tool_identity(),
            ),
        ),
    )

    with pytest.raises(ValueError, match="error diagnostics"):
        PlanBuildResult(
            status=PlanStatus.PLANNED,
            executable=True,
            plan=plan,
            diagnostics=(
                PlanDiagnostic(
                    code="plan.invalid",
                    severity=DiagnosticSeverity.ERROR,
                    summary="Plan is invalid.",
                ),
            ),
        )


def test_local_private_projection_omits_parameter_values():
    secret = "SECRET_VALUE_SENTINEL"
    unit = _template_unit(nullable=True)
    details = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(
            (
                MappingEntry(
                    key=LiteralValue("connection"),
                    value=LiteralValue(secret),
                ),
            )
        ),
        template_unit_key=unit.key,
    )
    step = PlanStep(
        name="deploy",
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=DeploymentOperation(
            template=unit.identity.source.path,
            input_status=InputStatus.DESCRIBED,
        ),
    )
    target = PreparedTarget(
        name="munich",
        kind=TargetKind.RESOURCE_GROUP,
        subscription="sub",
        resource_group="rg",
        location="eastus",
        operations=(
            PreparedOperation(
                identity=OperationIdentity(
                    target="munich",
                    step="deploy",
                ),
                step=step,
                disposition=PlanDisposition.EXECUTE,
                details=details,
            ),
        ),
    )
    provider = _tool_identity()
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=True,
        plan=DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=1,
            steps=(step,),
            targets=(target,),
            template_units=(unit,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.AVAILABLE,
                    required_by=(target.operations[0].identity,),
                    provider=provider,
                ),
            ),
        ),
    )

    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )
    encoded = serialize_plan_json(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )

    parameter = document["plan"]["targets"][0]["operations"][0][
        "details"
    ]["parameters"][0]
    assert parameter == {
        "name": "connection",
        "expectedType": "string",
        "secure": False,
        "hasDefault": False,
        "nullable": True,
        "required": False,
        "resolution": "known",
        "dataReferences": [],
        "serialized": False,
    }
    assert document["plan"]["templateUnits"][0]["parameters"] == [
        {
            "name": "connection",
            "type": "string",
            "secure": False,
            "hasDefault": False,
            "nullable": True,
            "required": False,
        }
    ]
    assert secret not in encoded
    assert document["projection"] == "local-private"
    assert document["intent"] == "executable"
    assert document["plan"]["submission"] == {
        "mode": "source",
        "compilationBinding": "observed-not-enforced",
    }
    assert document["plan"]["capabilities"] == [
        {
            "kind": "arm-control-plane",
            "status": "available",
            "requiredBy": [
                {
                    "target": "munich",
                    "step": "deploy",
                }
            ],
            "provider": {
                "name": "azure-cli",
                "executablePath": (
                    provider.executable_path.as_posix()
                ),
                "version": "2.87.0",
                "versionProvenance": "known",
            },
        }
    ]


def test_local_private_projection_omits_raw_diagnostic_detail():
    secret = "PRIVATE_VALUE_SENTINEL"
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        diagnostics=(
            PlanDiagnostic(
                code="operation-preparation.invalid",
                severity=DiagnosticSeverity.ERROR,
                summary="Operation preparation failed.",
                detail=f"Invalid value: {secret}",
            ),
        ),
    )

    encoded = serialize_plan_json(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )
    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )

    assert secret not in encoded
    assert "detail" not in document["diagnostics"][0]


def test_local_private_projection_can_include_explicit_value_free_detail():
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        diagnostics=(
            PlanDiagnostic(
                code="subscription-target.missing",
                severity=DiagnosticSeverity.ERROR,
                summary="A required subscription target is missing.",
                detail="private detail",
                serialized_detail="Add one subscription-level site.",
            ),
        ),
    )

    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )

    assert document["diagnostics"][0]["detail"] == (
        "Add one subscription-level site."
    )


def test_publishable_and_redacted_plain_collapse_compiler_detail():
    secret = "PRIVATE_COMPILER_DETAIL_SENTINEL"
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        diagnostics=(
            PlanDiagnostic(
                code="compilation.module-unavailable",
                severity=DiagnosticSeverity.ERROR,
                summary="Template compilation failed.",
                detail=f"Private module {secret} could not be restored.",
            ),
        ),
    )

    document = serialize_plan(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )
    plain = render_plain_plan(result, redacted=True)

    assert document["diagnostics"] == [
        {
            "code": "plan.compilation-failed",
            "severity": "error",
            "summary": "Template compilation failed.",
        }
    ]
    assert secret not in serialize_plan_json(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )
    assert secret not in plain


def test_invalid_executable_plan_states_that_nothing_will_run():
    operation = _operation()
    blocked = PreparedOperation(
        identity=operation.identity,
        step=operation.step,
        disposition=PlanDisposition.BLOCKED,
        details=operation.details,
        skip_reason=PlanSkipReason(
            code=SkipReasonCode.CAPABILITY_UNAVAILABLE,
            detail="A required capability is unavailable.",
        ),
    )
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=1,
            steps=(blocked.step,),
            targets=(_target(blocked),),
        ),
    )

    rendered = render_plain_plan(result, redacted=False)

    assert "Status: invalid" in rendered
    assert "Executable: no" in rendered
    assert "No operations will be submitted from this plan." in rendered
    assert "Blocked: 1" in rendered


def test_describe_plan_discloses_missing_preflight():
    target = _target()
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=False,
        plan=DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target,),
        ),
    )

    rendered = render_plain_plan(result, redacted=False)

    assert "Preflight: not performed" in rendered
    assert "deployment capabilities were not checked" in rendered


def test_local_private_projection_omits_prepared_wait_values():
    secret = "PRIVATE_WAIT_VALUE_SENTINEL"
    details = ArmTagWaitOperation(
        input_status=InputStatus.PREPARED,
        resource_id=LiteralValue(secret),
        tag_key=LiteralValue(secret),
        expected_value=LiteralValue(secret),
        failure_pattern=LiteralValue(secret),
        timeout_minutes=5,
        poll_interval_seconds=10,
    )
    step = PlanStep(
        name="wait",
        sequence=1,
        kind=OperationKind.WAIT,
        scope=OperationScope.TARGET,
        details=details,
    )
    target = PreparedTarget(
        name="munich",
        kind=TargetKind.RESOURCE_GROUP,
        subscription="sub",
        resource_group="rg",
        location="eastus",
        operations=(
            PreparedOperation(
                identity=OperationIdentity(
                    target="munich",
                    step="wait",
                ),
                step=step,
                disposition=PlanDisposition.EXECUTE,
                details=details,
            ),
        ),
    )
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=True,
        plan=DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=None,
            max_parallel_sites=1,
            steps=(step,),
            targets=(target,),
            capabilities=(
                PlanCapability(
                    kind=CapabilityKind.ARM_CONTROL_PLANE,
                    status=CapabilityStatus.AVAILABLE,
                    required_by=(target.operations[0].identity,),
                    provider=_tool_identity(),
                ),
            ),
        ),
    )

    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )
    encoded = serialize_plan_json(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )

    details_document = document["plan"]["targets"][0]["operations"][0][
        "details"
    ]
    assert secret not in encoded
    assert details_document["valuesSerialized"] is False
    assert details_document["expectedValue"] == {
        "resolution": "known",
        "dataReferences": [],
        "serialized": False,
    }


def test_publishable_projection_is_a_strict_allowlist():
    secret = "PRIVATE_SENTINEL"
    diagnostic_secret = "PRIVATE_DIAGNOSTIC_SENTINEL"
    unit = _template_unit(parameter=secret)
    identity = ResourceIdentity(
        collection=secret,
        components=((secret, secret),),
    )
    composition = PlanComposition(
        sources=(
            CompositionSource(
                path=Path(f"{secret}/source.yaml"),
                selected_by=secret,
            ),
        ),
        resources=(
            CompositionResource(
                identity=identity,
                disposition=ResourceDisposition.EXTERNAL,
                source=Path(f"{secret}/resource.yaml"),
                reason=secret,
            ),
        ),
        references=(
            CompositionReference(
                rule_id=secret,
                source=identity,
                source_bindings=((secret, secret),),
                target=None,
                unverified_reason=secret,
            ),
        ),
        requirements=(
            CompositionRequirement(
                identity=identity,
                source=Path(f"{secret}/requirement.yaml"),
            ),
        ),
    )
    details = DeploymentOperation(
        template=unit.identity.source.path,
        input_status=InputStatus.PREPARED,
        parameters=MappingValue(
            (
                MappingEntry(
                    key=LiteralValue(secret),
                    value=LiteralValue(secret),
                ),
            )
        ),
        template_unit_key=unit.key,
    )
    step = PlanStep(
        name=secret,
        sequence=1,
        kind=OperationKind.DEPLOYMENT,
        scope=OperationScope.RESOURCE_GROUP,
        details=details,
        condition=secret,
    )
    target = PreparedTarget(
        name=secret,
        kind=TargetKind.RESOURCE_GROUP,
        subscription=secret,
        resource_group=secret,
        location=secret,
        operations=(
            PreparedOperation(
                identity=OperationIdentity(
                    target=secret,
                    step=secret,
                ),
                step=step,
                disposition=PlanDisposition.EXECUTE,
                details=details,
            ),
        ),
        composition=composition,
        diagnostics=(
            PlanDiagnostic(
                code=diagnostic_secret,
                severity=DiagnosticSeverity.ERROR,
                summary=diagnostic_secret,
                detail=diagnostic_secret,
            ),
        ),
    )
    capability = PlanCapability(
        kind=CapabilityKind.ARM_CONTROL_PLANE,
        status=CapabilityStatus.AVAILABLE,
        required_by=(target.operations[0].identity,),
        provider=_tool_identity(
            provider=secret,
            path=Path(secret) / "az",
            version=secret,
        ),
    )
    result = PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=DeploymentPlan(
            manifest_name=secret,
            source_path=Path(f"{secret}/manifest.yaml"),
            intent=PlanIntent.EXECUTABLE,
            description=secret,
            max_parallel_sites=1,
            steps=(step,),
            targets=(target,),
            template_units=(unit,),
            capabilities=(capability,),
            cli_selector=secret,
            manifest_selector=secret,
            composition_enabled=True,
        ),
        diagnostics=target.diagnostics,
    )

    document = serialize_plan(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )
    encoded = serialize_plan_json(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )
    redacted_plain = render_plain_plan(result, redacted=True)

    assert secret not in encoded
    assert secret not in redacted_plain
    assert diagnostic_secret not in redacted_plain
    assert set(document) == {
        "apiVersion",
        "kind",
        "projection",
        "status",
        "executable",
        "intent",
        "engine",
        "summary",
        "diagnostics",
    }
    assert document["diagnostics"] == [
        {
            "code": "plan.diagnostic",
            "severity": "error",
            "summary": "Plan processing reported a diagnostic.",
        }
    ]


@pytest.mark.parametrize("intent", list(PlanIntent))
@pytest.mark.parametrize("kind", [OperationKind.KUBECTL, OperationKind.WAIT])
@pytest.mark.parametrize("invalid", [False, True])
def test_redacted_plain_omits_authored_operation_values(intent, kind, invalid):
    secret = "PRIVATE_OPERATION_SENTINEL"
    input_status = (
        InputStatus.PREPARED
        if intent is PlanIntent.EXECUTABLE
        else InputStatus.DESCRIBED
    )
    if kind is OperationKind.KUBECTL:
        details = KubectlOperation(
            input_status=input_status,
            operation="apply",
            cluster_name=LiteralValue(secret),
            cluster_resource_group=LiteralValue(secret),
            files=(LiteralValue(f"https://{secret}/manifest.yaml"),),
        )
        kinds = (CapabilityKind.KUBECTL, CapabilityKind.ARC_PROXY)
    else:
        details = ArmTagWaitOperation(
            input_status=input_status,
            resource_id=LiteralValue(secret),
            tag_key=LiteralValue(secret),
            expected_value=LiteralValue(secret),
            failure_pattern=LiteralValue(secret),
            timeout_minutes=5,
            poll_interval_seconds=10,
        )
        kinds = (CapabilityKind.ARM_CONTROL_PLANE,)
    step = PlanStep(
        name=secret,
        sequence=1,
        kind=kind,
        scope=OperationScope.TARGET,
        details=details,
        condition=secret,
    )
    operation = PreparedOperation(
        identity=OperationIdentity(target=secret, step=secret),
        step=step,
        disposition=PlanDisposition.EXECUTE,
        details=details,
    )
    diagnostics = (
        (
            PlanDiagnostic(
                code=secret,
                severity=DiagnosticSeverity.ERROR,
                summary=secret,
                detail=secret,
            ),
        )
        if invalid
        else ()
    )
    plan = DeploymentPlan(
        manifest_name=secret,
        source_path=Path(secret) / "manifest.yaml",
        intent=intent,
        description=secret,
        max_parallel_sites=1,
        steps=(step,),
        targets=(
            PreparedTarget(
                name=secret,
                kind=TargetKind.RESOURCE_GROUP,
                subscription=secret,
                resource_group=secret,
                location=secret,
                operations=(operation,),
                diagnostics=diagnostics,
            ),
        ),
        capabilities=(
            tuple(
                PlanCapability(
                    kind=capability,
                    status=CapabilityStatus.AVAILABLE,
                    required_by=(operation.identity,),
                    provider=_tool_identity(provider=secret, version=secret),
                )
                for capability in kinds
            )
            if intent is PlanIntent.EXECUTABLE
            else ()
        ),
        cli_selector=secret,
        manifest_selector=secret,
    )
    result = PlanBuildResult(
        status=PlanStatus.INVALID if invalid else PlanStatus.PLANNED,
        executable=intent is PlanIntent.EXECUTABLE and not invalid,
        plan=plan,
        diagnostics=diagnostics,
    )

    published = render_plain_plan(result, redacted=True)
    private = render_plain_plan(result, redacted=False)
    document = serialize_plan(
        result, PlanProjection.PUBLISHABLE, engine_version="1.0.0b1"
    )

    assert secret not in published
    assert secret in private
    assert f"Status: {document['status']}" in published
    assert f"Intent: {document['intent']}" in published
    assert f"Sites: {document['summary']['targetCount']} selected" in published
    assert f"Proposed: {document['summary']['dispositions']['execute']} execute" in published
    if invalid:
        assert "Plan processing reported a diagnostic." in published


def test_redacted_empty_plan_omits_manifest_identity():
    secret = "PRIVATE_EMPTY_MANIFEST_SENTINEL"
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=False,
        plan=DeploymentPlan(
            manifest_name=secret,
            source_path=Path(secret),
            intent=PlanIntent.DESCRIBE,
            description=secret,
            max_parallel_sites=1,
            steps=(),
            targets=(),
            cli_selector=secret,
        ),
    )

    assert secret not in render_plain_plan(result, redacted=True)
    assert secret in render_plain_plan(result, redacted=False)


def test_preview_json_is_deterministic_and_identifies_projection():
    target = _target()
    plan = DeploymentPlan(
        manifest_name="install",
        source_path=Path("manifests/install.yaml"),
        intent=PlanIntent.DESCRIBE,
        description=None,
        max_parallel_sites=1,
        steps=(target.operations[0].step,),
        targets=(target,),
    )
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=False,
        plan=plan,
    )

    first = serialize_plan_json(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )
    second = serialize_plan_json(
        result,
        PlanProjection.PUBLISHABLE,
        engine_version="1.0.0b1",
    )

    assert first == second
    assert '"apiVersion": "siteops/v1alpha1"' in first
    assert '"kind": "DeploymentPlan"' in first
    assert '"intent": "describe"' in first
    assert '"projection": "publishable"' in first


def test_local_describe_plan_omits_submission_claim():
    target = _target()
    result = PlanBuildResult(
        status=PlanStatus.PLANNED,
        executable=False,
        plan=DeploymentPlan(
            manifest_name="install",
            source_path=Path("manifests/install.yaml"),
            intent=PlanIntent.DESCRIBE,
            description=None,
            max_parallel_sites=1,
            steps=(target.operations[0].step,),
            targets=(target,),
        ),
    )

    document = serialize_plan(
        result,
        PlanProjection.LOCAL_PRIVATE,
        engine_version="1.0.0b1",
    )

    assert document["intent"] == "describe"
    assert "submission" not in document["plan"]

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for template compilation identities and source snapshots."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from siteops.compilation import (
    BicepConfigurationIdentity,
    CompilationFailure,
    CompilationFailureCode,
    CompilationKey,
    CompiledTemplate,
    ConfigurationDiscovery,
    DependencyCoverage,
    DependencyIdentity,
    PreparedTemplateUnit,
    TemplateCompilationSession,
    TemplateKind,
    TemplateOutputError,
    ToolIdentity,
    VersionProvenance,
    arm_json_dependency_identity,
    detect_template_kind,
    discover_bicep_configuration,
    extract_compiler_template_hashes,
    extract_compiler_version,
    extract_template_parameters,
    read_source_snapshot,
    validate_arm_template,
)
from siteops.runtime import TEMP_DIR_ENV, RuntimePaths


def _arm_template(parameters=None):
    return {
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": parameters or {},
        "resources": [],
    }


def _tool_path(tmp_path: Path, name: str) -> str:
    """Return a platform-native absolute fake executable path."""
    return str((tmp_path / "tools" / name).resolve())


def _user_defined_template():
    template = _arm_template(
        {
            "configuration": {
                "$ref": "#/definitions/settingsAlias",
                "defaultValue": {"label": "demo"},
            },
            "credentialValue": {"$ref": "#/definitions/credential"},
            "credentials": {"$ref": "#/definitions/secretSettings"},
        }
    )
    template["languageVersion"] = "2.0"
    template["resources"] = {}
    template["definitions"] = {
        "settings": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
        },
        "settingsAlias": {"$ref": "#/definitions/settings"},
        "credential": {"type": "securestring"},
        "secretSettings": {
            "type": "secureObject",
            "properties": {"password": {"type": "string"}},
        },
    }
    return template


def test_source_snapshot_uses_exact_bytes_and_resolved_path(tmp_path):
    source = tmp_path / "templates" / "main.bicep"
    source.parent.mkdir()
    source.write_bytes(b"param name string\r\n")

    snapshot = read_source_snapshot(source)

    assert snapshot.identity.path == source.resolve()
    assert snapshot.identity.size_bytes == len(snapshot.content)
    assert snapshot.identity.content_digest == hashlib.sha256(
        b"param name string\r\n"
    ).hexdigest()


def test_source_edit_changes_snapshot_identity(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param first string\n", encoding="utf-8")
    first = read_source_snapshot(source)
    source.write_text("param second string\n", encoding="utf-8")

    second = read_source_snapshot(source)

    assert first.identity.content_digest != second.identity.content_digest


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("main.bicep", TemplateKind.BICEP),
        ("main.BICEP", TemplateKind.BICEP),
        ("main.json", TemplateKind.ARM_JSON),
    ],
)
def test_detect_template_kind(name, expected):
    assert detect_template_kind(Path(name)) is expected


def test_detect_template_kind_rejects_other_suffix():
    with pytest.raises(ValueError, match="Unsupported template format"):
        detect_template_kind(Path("main.yaml"))


def test_configuration_discovery_uses_nearest_ancestor(tmp_path):
    root_config = tmp_path / "bicepconfig.json"
    root_config.write_text('{"root": true}\n', encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_config = nested / "bicepconfig.json"
    nested_config.write_text('{"nested": true}\n', encoding="utf-8")
    source = nested / "templates" / "main.bicep"
    source.parent.mkdir()
    source.write_text("", encoding="utf-8")

    snapshot = discover_bicep_configuration(source)

    assert (
        snapshot.identity.discovery
        is ConfigurationDiscovery.NEAREST_FOUND
    )
    assert snapshot.identity.path == nested_config.resolve()
    assert snapshot.identity.content_digest == hashlib.sha256(
        nested_config.read_bytes()
    ).hexdigest()


def test_configuration_discovery_records_explicit_absence(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")

    snapshot = discover_bicep_configuration(source)

    assert snapshot.identity == BicepConfigurationIdentity(
        path=None,
        content_digest=None,
        discovery=ConfigurationDiscovery.NONE_FOUND,
    )
    assert snapshot.content is None


def test_template_parameter_schema_is_sorted_and_typed():
    schema = extract_template_parameters(
        {
            "parameters": {
                "secret": {
                    "type": "secureString",
                },
                "count": {
                    "type": "int",
                    "defaultValue": 1,
                },
            }
        }
    )

    assert [parameter.name for parameter in schema] == ["count", "secret"]
    assert schema[0].type == "int"
    assert schema[0].has_default
    assert not schema[0].nullable
    assert not schema[0].is_required
    assert not schema[0].secure
    assert schema[1].secure
    assert schema[1].is_required


@pytest.mark.parametrize(
    ("parameter_type", "secure"),
    [
        ("string", False),
        ("securestring", True),
        ("int", False),
        ("bool", False),
        ("object", False),
        ("secureObject", True),
        ("array", False),
    ],
)
@pytest.mark.parametrize("has_default", [False, True])
def test_template_parameter_schema_resolves_type_aliases(
    parameter_type,
    secure,
    has_default,
):
    parameter = {"$ref": "#/definitions/alias"}
    if has_default:
        parameter["defaultValue"] = None
    template = _arm_template({"value": parameter})
    template["languageVersion"] = "2.0"
    template["definitions"] = {
        "alias": {"$ref": "#/definitions/valueType"},
        "valueType": {"type": parameter_type, "nullable": True},
    }

    schema = extract_template_parameters(template)

    assert len(schema) == 1
    assert schema[0].name == "value"
    assert schema[0].type == parameter_type
    assert schema[0].secure is secure
    assert schema[0].has_default is has_default
    assert schema[0].nullable
    assert not schema[0].is_required
    assert parameter == (
        {"$ref": "#/definitions/alias", "defaultValue": None}
        if has_default
        else {"$ref": "#/definitions/alias"}
    )


@pytest.mark.parametrize(
    ("parameter", "definitions", "nullable"),
    [
        pytest.param(
            {"type": "string", "nullable": True},
            {},
            True,
            id="direct",
        ),
        pytest.param(
            {
                "$ref": "#/definitions/valueType",
                "nullable": True,
            },
            {"valueType": {"type": "string"}},
            True,
            id="parameter-wrapper",
        ),
        pytest.param(
            {"$ref": "#/definitions/valueType"},
            {"valueType": {"type": "string", "nullable": True}},
            True,
            id="definition",
        ),
        pytest.param(
            {"$ref": "#/definitions/alias"},
            {
                "alias": {
                    "$ref": "#/definitions/valueType",
                    "nullable": False,
                },
                "valueType": {"type": "string", "nullable": True},
            },
            False,
            id="nearer-false-overrides-terminal-true",
        ),
        pytest.param(
            {
                "$ref": "#/definitions/alias",
                "nullable": True,
            },
            {
                "alias": {
                    "$ref": "#/definitions/valueType",
                    "nullable": False,
                },
                "valueType": {"type": "string"},
            },
            True,
            id="parameter-true-overrides-alias-false",
        ),
    ],
)
def test_template_parameter_schema_resolves_effective_nullability(
    parameter,
    definitions,
    nullable,
):
    template = _arm_template({"value": parameter})
    template["languageVersion"] = "2.0"
    template["definitions"] = definitions

    schema = extract_template_parameters(template)

    assert schema[0].nullable is nullable
    assert schema[0].is_required is not nullable


@pytest.mark.parametrize(
    "body",
    [
        {"type": "string", "nullable": True, "defaultValue": None},
        {"type": "bool", "defaultValue": False},
        {"type": "int", "defaultValue": 0},
    ],
)
def test_template_parameter_schema_preserves_explicit_default_presence(
    body,
):
    schema = extract_template_parameters(
        _arm_template({"value": body})
    )

    assert schema[0].has_default
    assert not schema[0].is_required


@pytest.mark.parametrize(
    ("parameter", "definitions"),
    [
        pytest.param(
            {"type": "string", "nullable": None},
            {},
            id="null",
        ),
        pytest.param(
            {"type": "string", "nullable": 0},
            {},
            id="zero",
        ),
        pytest.param(
            {"type": "string", "nullable": 1},
            {},
            id="one",
        ),
        pytest.param(
            {"type": "string", "nullable": "true"},
            {},
            id="string",
        ),
        pytest.param(
            {"type": "string", "nullable": {}},
            {},
            id="object",
        ),
        pytest.param(
            {"type": "string", "nullable": []},
            {},
            id="array",
        ),
        pytest.param(
            {"$ref": "#/definitions/valueType"},
            {"valueType": {"type": "string", "nullable": "false"}},
            id="referenced-definition",
        ),
    ],
)
def test_template_parameter_schema_rejects_invalid_nullable_constraint(
    parameter,
    definitions,
):
    template = _arm_template({"value": parameter})
    template["languageVersion"] = "2.0"
    template["definitions"] = definitions

    with pytest.raises(TemplateOutputError, match="nullable constraint"):
        extract_template_parameters(template)


def test_template_parameter_schema_allows_recursive_object_properties():
    template = _arm_template({"value": {"$ref": "#/definitions/node"}})
    template["languageVersion"] = "2.0"
    template["definitions"] = {
        "node": {
            "type": "object",
            "properties": {
                "child": {"$ref": "#/definitions/node", "nullable": True},
            },
        },
    }

    schema = extract_template_parameters(template)

    assert schema[0].type == "object"
    assert not schema[0].secure
    assert not schema[0].has_default
    assert not schema[0].nullable
    assert schema[0].is_required


@pytest.mark.parametrize(
    ("parameter", "definitions"),
    [
        ({"$ref": None}, {}),
        ({"$ref": 1}, {}),
        ({"$ref": ""}, {}),
        ({"$ref": "https://example.invalid/types.json#/definitions/value"}, {}),
        ({"$ref": "types.json#/definitions/value"}, {}),
        ({"$ref": "#/parameters/value"}, {}),
        ({"$ref": "#/definitions/"}, {"": {"type": "string"}}),
        (
            {"$ref": "#/definitions/value/type"},
            {"value/type": {"type": "string"}},
        ),
        ({"$ref": "#/definitions/missing"}, {}),
        ({"$ref": "#/definitions/value"}, None),
        ({"$ref": "#/definitions/value"}, []),
        ({"$ref": "#/definitions/value"}, {"value": "string"}),
        ({"$ref": "#/definitions/value"}, {"value": []}),
        ({"$ref": "#/definitions/value"}, {"value": {}}),
        ({"$ref": "#/definitions/value"}, {"value": {"type": None}}),
        ({"$ref": "#/definitions/value"}, {"value": {"type": "unknown"}}),
        (
            {"type": "object", "$ref": "#/definitions/value"},
            {"value": {"type": "securestring"}},
        ),
    ],
)
def test_template_parameter_schema_rejects_invalid_type_references(
    parameter,
    definitions,
):
    template = _arm_template({"value": parameter})
    template["languageVersion"] = "2.0"
    template["definitions"] = definitions

    with pytest.raises(TemplateOutputError):
        extract_template_parameters(template)


@pytest.mark.parametrize(
    "definitions",
    [
        {"first": {"$ref": "#/definitions/first"}},
        {
            "first": {"$ref": "#/definitions/second"},
            "second": {"$ref": "#/definitions/first"},
        },
    ],
)
def test_template_parameter_schema_rejects_cyclic_type_aliases(definitions):
    template = _arm_template({"value": {"$ref": "#/definitions/first"}})
    template["languageVersion"] = "2.0"
    template["definitions"] = definitions

    with pytest.raises(TemplateOutputError, match="cyclic"):
        extract_template_parameters(template)


@pytest.mark.parametrize(
    "arm_json",
    [
        [],
        {"parameters": []},
        {"parameters": {"name": "string"}},
        {"parameters": {"name": {}}},
        {"parameters": {"name": {"type": ""}}},
        {"parameters": {"name": {"type": "not-an-arm-type"}}},
        {"parameters": {"name": {"type": 7}}},
    ],
)
def test_template_parameter_schema_rejects_invalid_output(arm_json):
    with pytest.raises(TemplateOutputError):
        extract_template_parameters(arm_json)


def test_template_parameter_schema_rejects_mixed_key_types():
    with pytest.raises(TemplateOutputError, match="parameter names"):
        extract_template_parameters(
            {
                "parameters": {
                    "name": {"type": "string"},
                    7: {"type": "int"},
                }
            }
        )


@pytest.mark.parametrize("arm_json", [{}, {"parameters": {}}])
def test_arm_template_validation_rejects_non_template_objects(arm_json):
    with pytest.raises(TemplateOutputError):
        validate_arm_template(arm_json)


def test_arm_template_validation_accepts_subscription_scope():
    template = _arm_template()
    template["$schema"] = (
        "https://schema.management.azure.com/schemas/2018-05-01/"
        "subscriptionDeploymentTemplate.json#"
    )

    validate_arm_template(template)


def test_compiler_template_hashes_exclude_root_and_deduplicate():
    arm_json = {
        "metadata": {
            "_generator": {
                "templateHash": "root",
            }
        },
        "resources": [
            {
                "properties": {
                    "template": {
                        "metadata": {
                            "_generator": {
                                "templateHash": "module-b",
                            }
                        }
                    }
                }
            },
            {
                "properties": {
                    "template": {
                        "metadata": {
                            "_generator": {
                                "templateHash": "module-a",
                            }
                        }
                    }
                }
            },
            {
                "metadata": {
                    "_generator": {
                        "templateHash": "module-a",
                    }
                }
            },
        ],
    }

    assert extract_compiler_template_hashes(arm_json) == (
        "module-a",
        "module-b",
    )


def test_compiler_template_hashes_ignore_malformed_metadata():
    assert extract_compiler_template_hashes(
        {
            "resources": [
                {
                    "metadata": "not-an-object",
                }
            ]
        }
    ) == ()


def test_compiler_version_uses_root_generator_metadata():
    assert extract_compiler_version(
        {
            "metadata": {
                "_generator": {
                    "name": "bicep",
                    "version": "0.45.15.0",
                }
            }
        }
    ) == "0.45.15.0"


def test_arm_json_linked_template_has_unknown_dependency_coverage():
    template = _arm_template()
    template["resources"] = [
        {
            "type": "Microsoft.Resources/deployments",
            "apiVersion": "2025-04-01",
            "name": "linked",
            "properties": {
                "templateLink": {
                    "uri": "https://example.invalid/template.json"
                }
            },
        }
    ]
    identity = arm_json_dependency_identity(
        template
    )

    assert identity.coverage is DependencyCoverage.UNKNOWN


def test_dependency_identity_normalizes_and_rejects_duplicates():
    identity = DependencyIdentity(
        coverage=DependencyCoverage.COMPILED_OUTPUT_ONLY,
        template_hashes=["module-a"],
    )

    assert identity.template_hashes == ("module-a",)
    with pytest.raises(ValueError, match="must be unique"):
        DependencyIdentity(
            coverage=DependencyCoverage.COMPILED_OUTPUT_ONLY,
            template_hashes=("module-a", "module-a"),
        )


def test_compilation_key_is_location_sensitive(tmp_path):
    first = CompilationKey(
        source_path=tmp_path / "a" / "main.bicep",
        source_content_digest="source",
        template_kind=TemplateKind.BICEP,
        compiler_fingerprint="compiler",
        configuration_digest="none",
        invocation=("az", "bicep", "build"),
    )
    second = CompilationKey(
        source_path=tmp_path / "b" / "main.bicep",
        source_content_digest="source",
        template_kind=TemplateKind.BICEP,
        compiler_fingerprint="compiler",
        configuration_digest="none",
        invocation=("az", "bicep", "build"),
    )

    assert first != second


class FakeCompiler:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.compile_count = 0
        self.compile_output = _arm_template(
            {
                "name": {
                    "type": "string",
                }
            }
        )
        self.compile_returncode = 0
        self.compile_stderr = ""
        self.on_compile = None
        self.bicep_version = "0.45.15 (commit)"
        self.bicep_version_result: (
            subprocess.CompletedProcess[str] | Exception | None
        ) = None

    def __call__(
        self,
        argv: tuple[str, ...],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"azure-cli": "2.87.0"}),
                stderr="",
            )
        if argv[1:] == ("bicep", "version"):
            if isinstance(self.bicep_version_result, Exception):
                raise self.bicep_version_result
            if self.bicep_version_result is not None:
                return self.bicep_version_result
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"Bicep CLI version {self.bicep_version}",
                stderr="",
            )
        self.compile_count += 1
        if self.on_compile is not None:
            self.on_compile()
        output_path = Path(argv[argv.index("--outfile") + 1])
        if self.compile_returncode == 0:
            output_path.write_text(
                json.dumps(self.compile_output),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            argv,
            self.compile_returncode,
            stdout="",
            stderr=self.compile_stderr,
        )


def test_arm_json_acquisition_requires_no_tool(tmp_path):
    source = tmp_path / "main.json"
    source.write_text(
        json.dumps(_arm_template({"name": {"type": "string"}})),
        encoding="utf-8",
    )
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: None,
    )

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert result.parameters[0].name == "name"
    assert compiler.calls == []


def test_invalid_arm_parameter_schema_is_typed_failure(tmp_path):
    source = tmp_path / "main.json"
    source.write_text(
        json.dumps(_arm_template({"name": {}})),
        encoding="utf-8",
    )

    result = TemplateCompilationSession().acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.OUTPUT_INVALID


@pytest.mark.parametrize("suffix", [".json", ".bicep"])
def test_user_defined_parameter_types_are_acquired(tmp_path, suffix):
    source = tmp_path / f"main{suffix}"
    template = _user_defined_template()
    source.write_text(
        json.dumps(template) if suffix == ".json" else "param value object\n",
        encoding="utf-8",
    )
    compiler = FakeCompiler()
    compiler.compile_output = template
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert session.acquire(source) is result
    assert [
        (
            parameter.name,
            parameter.type,
            parameter.secure,
            parameter.has_default,
            parameter.nullable,
            parameter.is_required,
        )
        for parameter in result.parameters
    ] == [
        ("configuration", "object", False, True, False, False),
        ("credentialValue", "securestring", True, False, False, True),
        ("credentials", "secureObject", True, False, False, True),
    ]
    assert compiler.compile_count == (1 if suffix == ".bicep" else 0)
    if suffix == ".json":
        assert compiler.calls == []


@pytest.mark.parametrize("suffix", [".json", ".bicep"])
def test_invalid_type_reference_is_cached_as_output_failure(tmp_path, suffix):
    source = tmp_path / f"main{suffix}"
    template = _user_defined_template()
    del template["definitions"]["settings"]
    source.write_text(
        json.dumps(template) if suffix == ".json" else "param value object\n",
        encoding="utf-8",
    )
    compiler = FakeCompiler()
    compiler.compile_output = template
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.OUTPUT_INVALID
    assert session.acquire(source) is result
    assert compiler.compile_count == (1 if suffix == ".bicep" else 0)


def test_bicep_unit_compiles_once_per_session(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param name string\n", encoding="utf-8")
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    first = session.acquire(source)
    second = session.acquire(source)

    assert isinstance(first, CompiledTemplate)
    assert second is first
    assert compiler.compile_count == 1
    assert first.identity.compiler is not None
    assert first.identity.compiler.version == "0.45.15 (commit)"


def test_compiled_template_produces_plan_safe_unit_without_arm_bytes(tmp_path):
    source = tmp_path / "main.json"
    source.write_text(
        json.dumps(_arm_template({"name": {"type": "string"}})),
        encoding="utf-8",
    )
    result = TemplateCompilationSession().acquire(source)
    assert isinstance(result, CompiledTemplate)

    unit = result.prepared_unit()

    assert isinstance(unit, PreparedTemplateUnit)
    assert unit.key == result.key
    assert unit.identity == result.identity
    assert unit.parameter_names == frozenset({"name"})
    assert not hasattr(unit, "arm_json_bytes")


def test_cache_hit_does_not_reread_source(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param name string\n", encoding="utf-8")
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )
    first = session.acquire(source)
    source.unlink()

    second = session.acquire(source)

    assert second is first


def test_missing_toolchain_is_resolved_once_across_templates(tmp_path):
    first = tmp_path / "first.bicep"
    second = tmp_path / "second.bicep"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    resolutions = 0

    def resolve(name: str) -> None:
        nonlocal resolutions
        resolutions += 1
        return None

    session = TemplateCompilationSession(tool_resolver=resolve)

    assert isinstance(session.acquire(first), CompilationFailure)
    assert isinstance(session.acquire(second), CompilationFailure)
    assert resolutions == 1


def test_azure_cli_and_bicep_share_one_resolved_tool(tmp_path):
    compiler = FakeCompiler()
    resolutions = []

    def resolve(name: str) -> str:
        resolutions.append(name)
        return _tool_path(tmp_path, name)

    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=resolve,
    )

    azure_cli = session.resolve_azure_cli()
    bicep = session.resolve_bicep_compiler()

    assert isinstance(azure_cli, ToolIdentity)
    assert isinstance(bicep, ToolIdentity)
    assert resolutions == ["az"]
    assert azure_cli.resolved_path == bicep.resolved_path


def test_kubectl_resolution_does_not_run_a_command(tmp_path):
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    kubectl = session.resolve_kubectl()

    assert isinstance(kubectl, ToolIdentity)
    assert kubectl.provider == "kubectl"
    assert compiler.calls == []


def test_missing_kubectl_is_cached():
    resolutions = 0

    def resolve(name: str) -> None:
        nonlocal resolutions
        resolutions += 1
        return None

    session = TemplateCompilationSession(tool_resolver=resolve)

    first = session.resolve_kubectl()
    second = session.resolve_kubectl()

    assert isinstance(first, CompilationFailure)
    assert second is first
    assert resolutions == 1


def test_new_session_recompiles_after_source_edit(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param first string\n", encoding="utf-8")
    first_compiler = FakeCompiler()
    first_session = TemplateCompilationSession(
        command_runner=first_compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )
    first = first_session.acquire(source)
    source.write_text("param second string\n", encoding="utf-8")
    second_compiler = FakeCompiler()
    second_session = TemplateCompilationSession(
        command_runner=second_compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    second = second_session.acquire(source)

    assert isinstance(first, CompiledTemplate)
    assert isinstance(second, CompiledTemplate)
    assert first.key != second.key
    assert second_compiler.compile_count == 1


def test_new_session_changes_key_after_configuration_edit(tmp_path):
    config = tmp_path / "bicepconfig.json"
    config.write_text('{"analyzers": {}}\n', encoding="utf-8")
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    first_session = TemplateCompilationSession(
        command_runner=FakeCompiler(),
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )
    first = first_session.acquire(source)
    config.write_text(
        '{"analyzers": {"core": {"enabled": false}}}\n',
        encoding="utf-8",
    )
    second_session = TemplateCompilationSession(
        command_runner=FakeCompiler(),
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    second = second_session.acquire(source)

    assert isinstance(first, CompiledTemplate)
    assert isinstance(second, CompiledTemplate)
    assert first.key.configuration_digest != (
        second.key.configuration_digest
    )
    assert first.key != second.key


def test_new_session_changes_key_with_compiler_version(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    first_compiler = FakeCompiler()
    first_session = TemplateCompilationSession(
        command_runner=first_compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )
    first = first_session.acquire(source)
    second_compiler = FakeCompiler()
    second_compiler.bicep_version = "0.46.0 (next)"
    second_session = TemplateCompilationSession(
        command_runner=second_compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    second = second_session.acquire(source)

    assert isinstance(first, CompiledTemplate)
    assert isinstance(second, CompiledTemplate)
    assert first.key.compiler_fingerprint != second.key.compiler_fingerprint
    assert first.key != second.key


def test_missing_azure_cli_is_cached_as_failure(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: None,
    )

    first = session.acquire(source)
    second = session.acquire(source)

    assert isinstance(first, CompilationFailure)
    assert first.code is CompilationFailureCode.TOOL_MISSING
    assert second is first
    assert compiler.calls == []


def test_unreadable_source_failure_is_cached_once(tmp_path):
    source = tmp_path / "missing.bicep"
    session = TemplateCompilationSession()

    first = session.acquire(source)
    source.write_text("", encoding="utf-8")
    second = session.acquire(source)

    assert isinstance(first, CompilationFailure)
    assert first.code is CompilationFailureCode.SOURCE_UNREADABLE
    assert second is first


def test_unsupported_template_failure_is_cached_once(tmp_path):
    source = tmp_path / "main.yaml"
    source.write_text("value: true\n", encoding="utf-8")
    session = TemplateCompilationSession()

    first = session.acquire(source)
    source.write_text("value: false\n", encoding="utf-8")
    second = session.acquire(source)

    assert isinstance(first, CompilationFailure)
    assert first.code is CompilationFailureCode.OUTPUT_INVALID
    assert second is first


def test_relative_tool_resolution_is_rejected(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: "az.exe",
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.TOOL_MISSING
    assert compiler.calls == []


def test_failed_azure_cli_probe_is_unavailable(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    calls = []

    def fail_version(
        argv: tuple[str, ...],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="broken",
        )

    session = TemplateCompilationSession(
        command_runner=fail_version,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.TOOL_UNAVAILABLE
    assert session.acquire(source) is result
    assert [argv[1:] for argv in calls] == [
        ("version", "--output", "json"),
    ]


@pytest.mark.parametrize(
    "probe_result",
    [
        pytest.param(
            subprocess.CompletedProcess(
                ("az", "bicep", "version"),
                1,
                stdout="Bicep CLI version 0.44.0 (stale)",
                stderr="Bicep CLI not found.",
            ),
            id="failed",
        ),
        pytest.param(
            subprocess.CompletedProcess(
                ("az", "bicep", "version"),
                0,
                stdout="Unexpected version output.",
                stderr="",
            ),
            id="unparseable",
        ),
        pytest.param(OSError("Version probe failed."), id="os-error"),
        pytest.param(
            subprocess.TimeoutExpired(("az", "bicep", "version"), 300),
            id="timeout",
        ),
    ],
)
@pytest.mark.parametrize(
    "emitted_version",
    [None, "0.45.15.0"],
    ids=["no-metadata", "emitted-version"],
)
def test_unknown_bicep_probe_allows_build(
    tmp_path,
    probe_result,
    emitted_version,
):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.bicep_version_result = probe_result
    if emitted_version is not None:
        compiler.compile_output["metadata"] = {
            "_generator": {
                "name": "bicep",
                "version": emitted_version,
            }
        }
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    observed_compiler = session.resolve_bicep_compiler()

    assert isinstance(observed_compiler, ToolIdentity)
    assert observed_compiler.version is None
    assert observed_compiler.version_provenance is VersionProvenance.UNKNOWN
    assert compiler.compile_count == 0

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert session.acquire(source) is result
    assert result.parameters[0].name == "name"
    assert result.identity.compiler is not None
    assert result.identity.compiler.version == emitted_version
    assert result.identity.compiler.version_provenance is (
        VersionProvenance.KNOWN
        if emitted_version is not None
        else VersionProvenance.UNKNOWN
    )
    assert result.identity.compiler_driver == session.resolve_azure_cli()
    assert compiler.compile_count == 1
    assert [argv[1:3] for argv in compiler.calls] == [
        ("version", "--output"),
        ("bicep", "version"),
        ("bicep", "build"),
    ]
    assert all(
        argv[0] == str(observed_compiler.resolved_path)
        for argv in compiler.calls
    )


@pytest.mark.parametrize(
    ("build_stderr", "expected_code"),
    [
        ("BCP000: invalid source", CompilationFailureCode.FAILED),
        (
            "BCP192: Unable to restore module.",
            CompilationFailureCode.MODULE_UNAVAILABLE,
        ),
    ],
)
def test_failed_bicep_probe_preserves_cached_build_failure(
    tmp_path,
    build_stderr,
    expected_code,
):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.bicep_version_result = subprocess.CompletedProcess(
        ("az", "bicep", "version"),
        1,
        stdout="",
        stderr="Bicep CLI not found.",
    )
    compiler.compile_returncode = 1
    compiler.compile_stderr = build_stderr
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is expected_code
    assert build_stderr in result.detail
    assert session.acquire(source) is result
    assert session.outcomes == (result,)
    assert compiler.compile_count == 1
    assert [argv[1:3] for argv in compiler.calls] == [
        ("version", "--output"),
        ("bicep", "version"),
        ("bicep", "build"),
    ]


def test_compiler_warnings_do_not_block_success(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param name string\n", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_stderr = (
        'Warning no-unused-params: Parameter "name" is declared but never used.'
    )
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert result.parameters[0].name == "name"
    assert compiler.compile_count == 1


def test_compiler_failure_is_cached(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_returncode = 1
    compiler.compile_stderr = "BCP000: invalid source"
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    first = session.acquire(source)
    second = session.acquire(source)

    assert isinstance(first, CompilationFailure)
    assert first.code is CompilationFailureCode.FAILED
    assert second is first
    assert compiler.compile_count == 1


def test_module_restore_failure_uses_typed_code(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_returncode = 1
    compiler.compile_stderr = "BCP192: Unable to restore module."
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.MODULE_UNAVAILABLE


def test_compiler_timeout_uses_typed_code(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")

    def timeout(
        argv: tuple[str, ...],
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        if argv[1:] in (
            ("version", "--output", "json"),
            ("bicep", "version"),
        ):
            return FakeCompiler()(argv, timeout_seconds)
        raise subprocess.TimeoutExpired(argv, timeout_seconds)

    session = TemplateCompilationSession(
        command_runner=timeout,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.TIMEOUT


def test_invalid_compiler_output_fails_closed(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_output = []
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.OUTPUT_INVALID


def test_source_change_during_compile_fails_closed(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("param first string\n", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.on_compile = lambda: source.write_text(
        "param second string\n",
        encoding="utf-8",
    )
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.INPUT_CHANGED
    assert session.acquire(source) is result
    assert session.outcomes == (result,)
    assert compiler.compile_count == 1


def test_source_change_during_failed_compile_reports_changed_input(
    tmp_path,
):
    source = tmp_path / "main.bicep"
    source.write_text("param first string\n", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_returncode = 1
    compiler.compile_stderr = "BCP000: invalid source"
    compiler.on_compile = lambda: source.write_text(
        "param second string\n",
        encoding="utf-8",
    )
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.INPUT_CHANGED
    assert session.acquire(source) is result
    assert session.outcomes == (result,)
    assert compiler.compile_count == 1


@pytest.mark.parametrize(
    ("initial_configuration", "mutation", "compile_returncode"),
    [
        pytest.param("present", "change", 0, id="change-successful-build"),
        pytest.param("absent", "add", 0, id="add-successful-build"),
        pytest.param("present", "remove", 1, id="remove-failed-build"),
    ],
)
def test_configuration_change_during_compile_fails_closed_and_is_cached(
    tmp_path,
    initial_configuration,
    mutation,
    compile_returncode,
):
    source = tmp_path / "main.bicep"
    source.write_text("param name string\n", encoding="utf-8")
    configuration = tmp_path / "bicepconfig.json"
    if initial_configuration == "present":
        configuration.write_text('{"analyzers": {}}\n', encoding="utf-8")

    def mutate_configuration():
        if mutation == "change":
            configuration.write_text(
                '{"analyzers": {"core": {"enabled": false}}}\n',
                encoding="utf-8",
            )
        elif mutation == "add":
            configuration.write_text('{"cloud": {}}\n', encoding="utf-8")
        else:
            configuration.unlink()

    compiler = FakeCompiler()
    compiler.compile_returncode = compile_returncode
    compiler.compile_stderr = (
        "BCP000: invalid source" if compile_returncode else ""
    )
    compiler.on_compile = mutate_configuration
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.INPUT_CHANGED
    assert session.acquire(source) is result
    assert session.outcomes == (result,)
    assert compiler.compile_count == 1


def test_unchanged_configuration_allows_compile_and_cache_reuse(tmp_path):
    configuration = tmp_path / "bicepconfig.json"
    configuration.write_text('{"analyzers": {}}\n', encoding="utf-8")
    source = tmp_path / "main.bicep"
    source.write_text("param name string\n", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.on_compile = lambda: None
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert session.acquire(source) is result
    assert result.identity.configuration is not None
    assert result.identity.configuration.path == configuration.resolve()
    assert compiler.compile_count == 1


def test_compiled_output_version_is_authoritative(tmp_path):
    source = tmp_path / "main.bicep"
    source.write_text("", encoding="utf-8")
    compiler = FakeCompiler()
    compiler.compile_output["metadata"] = {
        "_generator": {
            "name": "bicep",
            "version": "0.45.15.0",
        }
    }
    session = TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
    )

    result = session.acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert result.identity.compiler is not None
    assert result.identity.compiler.version == "0.45.15.0"
    assert (
        result.identity.compiler.version_provenance
        is VersionProvenance.KNOWN
    )


class RecordingCompiler(FakeCompiler):
    """A fake compiler that records where its output was asked to land."""

    def __init__(self):
        super().__init__()
        self.output_directories: list[Path] = []

    def __call__(self, argv, timeout):
        if "--outfile" in argv:
            output = Path(argv[argv.index("--outfile") + 1])
            self.output_directories.append(output.parent)
        return super().__call__(argv, timeout)


def _bicep_source(tmp_path: Path) -> Path:
    source = tmp_path / "sources" / "main.bicep"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("param name string\n", encoding="utf-8")
    return source


def _session(tmp_path: Path, compiler, temp_root: Path | None = None):
    return TemplateCompilationSession(
        command_runner=compiler,
        tool_resolver=lambda name: _tool_path(tmp_path, name),
        runtime_paths=(
            None
            if temp_root is None
            else RuntimePaths(temp_root=temp_root)
        ),
    )


def test_compiler_output_lands_under_the_selected_temp_root(tmp_path):
    """Compiler output is engine-owned and transient, so it does not go beside
    the authored template."""
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()

    result = _session(tmp_path, compiler, temp_root).acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert len(compiler.output_directories) == 1
    output_directory = compiler.output_directories[0]
    assert output_directory.parent == temp_root
    assert output_directory.name.startswith("siteops-bicep-")
    assert sorted(p.name for p in source.parent.iterdir()) == ["main.bicep"]


def test_the_environment_selects_the_compiler_temp_root(tmp_path, monkeypatch):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "from-environment"
    monkeypatch.setenv(TEMP_DIR_ENV, str(temp_root))
    compiler = RecordingCompiler()

    result = _session(tmp_path, compiler).acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert compiler.output_directories[0].parent == temp_root


def test_a_missing_temp_root_is_created_at_compile_time(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "not-yet" / "engine-temp"
    compiler = RecordingCompiler()
    session = _session(tmp_path, compiler, temp_root)

    assert not temp_root.exists(), "constructing a session creates no directory"

    session.acquire(source)

    assert temp_root.is_dir()


def test_compiler_output_is_removed_after_a_successful_compile(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()

    result = _session(tmp_path, compiler, temp_root).acquire(source)

    assert isinstance(result, CompiledTemplate)
    assert not compiler.output_directories[0].exists()
    assert list(temp_root.iterdir()) == []


def test_compiler_output_is_removed_after_a_failed_compile(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()
    compiler.compile_returncode = 1
    compiler.compile_stderr = "BCP000: invalid source"

    result = _session(tmp_path, compiler, temp_root).acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.FAILED
    assert list(temp_root.iterdir()) == []


def test_compiler_output_is_removed_after_a_timeout(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()

    def time_out():
        raise subprocess.TimeoutExpired(cmd=("az", "bicep", "build"), timeout=1)

    compiler.on_compile = time_out

    result = _session(tmp_path, compiler, temp_root).acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.TIMEOUT
    assert list(temp_root.iterdir()) == []


def test_compiler_output_is_removed_after_invalid_output(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()
    compiler.compile_output = {"contentVersion": "1.0.0.0"}

    result = _session(tmp_path, compiler, temp_root).acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.OUTPUT_INVALID
    assert list(temp_root.iterdir()) == []


def test_a_session_that_never_compiles_creates_no_directory(tmp_path):
    """Constructing a session, resolving tools, and acquiring ARM JSON are all
    preflight. None of them may allocate."""
    temp_root = tmp_path / "engine-temp"
    source = tmp_path / "sources" / "main.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(_arm_template({"name": {"type": "string"}})),
        encoding="utf-8",
    )
    compiler = RecordingCompiler()
    session = _session(tmp_path, compiler, temp_root)

    result = session.acquire(source)
    resolved = session.resolve_azure_cli()

    assert isinstance(result, CompiledTemplate)
    assert isinstance(resolved, ToolIdentity)
    assert not temp_root.exists()


def test_an_unusable_temp_root_is_a_typed_failure(tmp_path, monkeypatch):
    """A misconfigured redirect fails the template with a stable code rather
    than raising out of planning."""
    source = _bicep_source(tmp_path)
    monkeypatch.setenv(TEMP_DIR_ENV, "relative/path")
    compiler = RecordingCompiler()

    result = _session(tmp_path, compiler).acquire(source)

    assert isinstance(result, CompilationFailure)
    assert result.code is CompilationFailureCode.FAILED
    assert TEMP_DIR_ENV in result.detail
    assert compiler.compile_count == 0, "no compiler runs without a place to write"


def test_a_cached_outcome_is_reused_without_reallocating(tmp_path):
    source = _bicep_source(tmp_path)
    temp_root = tmp_path / "engine-temp"
    compiler = RecordingCompiler()
    session = _session(tmp_path, compiler, temp_root)

    first = session.acquire(source)
    second = session.acquire(source)

    assert second is first
    assert len(compiler.output_directories) == 1
    assert list(temp_root.iterdir()) == []

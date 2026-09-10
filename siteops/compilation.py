# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Immutable template compilation identities and source snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeAlias

from siteops.runtime import RuntimePathError, RuntimePaths, prepare_root

DEFAULT_COMPILATION_TIMEOUT_SECONDS = 300
_NO_CONFIGURATION_DIGEST = "none"
_ARM_JSON_COMPILER_FINGERPRINT = "arm-json"
_BICEP_VERSION_PATTERN = re.compile(
    r"Bicep CLI version\s+(\S+)(?:\s+\(([^)]+)\))?"
)
_DEPLOYMENT_SCHEMA_SUFFIXES = (
    "/deploymenttemplate.json#",
    "/subscriptiondeploymenttemplate.json#",
    "/managementgroupdeploymenttemplate.json#",
    "/tenantdeploymenttemplate.json#",
)
_ARM_PARAMETER_TYPES = frozenset(
    {
        "array",
        "bool",
        "int",
        "object",
        "secureobject",
        "securestring",
        "string",
    }
)

CommandRunner: TypeAlias = Callable[
    [tuple[str, ...], int],
    subprocess.CompletedProcess[str],
]
ToolResolver: TypeAlias = Callable[[str], str | None]


def _resolve_tool_from_path(name: str) -> str | None:
    """Resolve an executable only from absolute PATH entries."""
    path_value = os.environ.get("PATH")
    if not path_value:
        return None

    directories: list[Path] = []
    seen: set[str] = set()
    for raw_entry in path_value.split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        normalized = os.path.normcase(str(directory))
        if normalized in seen:
            continue
        seen.add(normalized)
        directories.append(directory)

    if os.name == "nt":
        raw_extensions = os.environ.get(
            "PATHEXT",
            os.pathsep.join((".COM", ".EXE", ".BAT", ".CMD")),
        )
        extensions = tuple(
            extension
            for extension in raw_extensions.split(os.pathsep)
            if extension
        )
        if Path(name).suffix.casefold() in {
            extension.casefold()
            for extension in extensions
        }:
            candidates = (name,)
        else:
            candidates = tuple(
                f"{name}{extension}"
                for extension in extensions
            )
    else:
        candidates = (name,)

    for directory in directories:
        for candidate_name in candidates:
            candidate = directory / candidate_name
            if candidate.is_file() and os.access(
                candidate,
                os.F_OK | os.X_OK,
            ):
                return str(candidate.resolve())
    return None


class TemplateKind(str, Enum):
    """Template source format."""

    BICEP = "bicep"
    ARM_JSON = "arm-json"


class VersionProvenance(str, Enum):
    """Confidence in a recorded tool version."""

    KNOWN = "known"
    UNKNOWN = "unknown"


class ConfigurationDiscovery(str, Enum):
    """How an effective Bicep configuration was found."""

    NEAREST_FOUND = "nearest-found"
    NONE_FOUND = "none-found"


class DependencyCoverage(str, Enum):
    """How completely dependency identity is represented."""

    NOT_APPLICABLE = "not-applicable"
    COMPILED_OUTPUT_ONLY = "compiled-output-only"
    PARTIAL = "partial"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class OutputDigestForm(str, Enum):
    """Byte representation used for a compiled output digest."""

    RAW_BYTES = "raw-bytes"


class CompilationFailureCode(str, Enum):
    """Stable category for an expected acquisition failure."""

    TOOL_MISSING = "compilation.tool-missing"
    TOOL_UNAVAILABLE = "compilation.tool-unavailable"
    SOURCE_UNREADABLE = "compilation.source-unreadable"
    TIMEOUT = "compilation.timeout"
    FAILED = "compilation.failed"
    MODULE_UNAVAILABLE = "compilation.module-unavailable"
    OUTPUT_INVALID = "compilation.output-invalid"
    INPUT_CHANGED = "compilation.input-changed"


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty.")


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class ToolIdentity:
    """Resolved local tool and its reported version."""

    provider: str
    resolved_path: Path
    version: str | None
    version_provenance: VersionProvenance

    def __post_init__(self) -> None:
        _require_text(self.provider, "Tool provider")
        object.__setattr__(
            self,
            "resolved_path",
            Path(self.resolved_path).resolve(),
        )
        if self.version is not None:
            _require_text(self.version, "Tool version")


@dataclass(frozen=True)
class SourceIdentity:
    """Private identity of exact template source bytes."""

    path: Path
    content_digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).resolve())
        _require_text(self.content_digest, "Source content digest")
        if self.size_bytes < 0:
            raise ValueError("Source size must be non-negative.")


@dataclass(frozen=True)
class SourceSnapshot:
    """Exact source bytes and their identity for one plan build."""

    identity: SourceIdentity
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("Source snapshot content must be bytes.")


@dataclass(frozen=True)
class BicepConfigurationIdentity:
    """Nearest effective Bicep configuration or explicit absence."""

    path: Path | None
    content_digest: str | None
    discovery: ConfigurationDiscovery

    def __post_init__(self) -> None:
        if self.discovery is ConfigurationDiscovery.NEAREST_FOUND:
            if self.path is None or self.content_digest is None:
                raise ValueError(
                    "A discovered Bicep configuration requires path and "
                    "content identity."
                )
            object.__setattr__(self, "path", Path(self.path).resolve())
            _require_text(
                self.content_digest,
                "Bicep configuration digest",
            )
        elif self.path is not None or self.content_digest is not None:
            raise ValueError(
                "An absent Bicep configuration cannot carry path or digest."
            )


@dataclass(frozen=True)
class ConfigurationSnapshot:
    """Configuration identity and bytes used by one compilation."""

    identity: BicepConfigurationIdentity
    content: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            self.identity.discovery
            is ConfigurationDiscovery.NEAREST_FOUND
        ) != (self.content is not None):
            raise ValueError(
                "Bicep configuration content must match its discovery state."
            )


@dataclass(frozen=True)
class TemplateParameter:
    """Parameter schema extracted from compiled ARM JSON."""

    name: str
    type: str | None
    secure: bool
    has_default: bool
    nullable: bool = False

    def __post_init__(self) -> None:
        _require_text(self.name, "Template parameter name")
        if self.type is not None:
            _require_text(self.type, "Template parameter type")
        if type(self.nullable) is not bool:
            raise TypeError("Template parameter nullable must be a boolean.")

    @property
    def is_required(self) -> bool:
        """Return whether deployment must supply this parameter."""
        return not self.has_default and not self.nullable


@dataclass(frozen=True)
class DependencyIdentity:
    """Compiler-emitted dependency hashes with explicit coverage."""

    coverage: DependencyCoverage
    template_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "template_hashes",
            tuple(self.template_hashes),
        )
        if len(self.template_hashes) != len(set(self.template_hashes)):
            raise ValueError("Dependency template hashes must be unique.")
        for template_hash in self.template_hashes:
            _require_text(template_hash, "Dependency template hash")
        if (
            self.coverage is DependencyCoverage.NOT_APPLICABLE
            and self.template_hashes
        ):
            raise ValueError(
                "Dependency hashes are not valid when coverage is not "
                "applicable."
            )


@dataclass(frozen=True)
class CompilationKey:
    """Content-addressed key for one location-sensitive compilation."""

    source_path: Path
    source_content_digest: str
    template_kind: TemplateKind
    compiler_fingerprint: str
    configuration_digest: str
    invocation: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_path",
            Path(self.source_path).resolve(),
        )
        object.__setattr__(self, "invocation", tuple(self.invocation))
        _require_text(
            self.source_content_digest,
            "Compilation source digest",
        )
        _require_text(
            self.compiler_fingerprint,
            "Compiler fingerprint",
        )
        _require_text(
            self.configuration_digest,
            "Configuration digest",
        )
        if not self.invocation:
            raise ValueError("Compilation invocation must be non-empty.")


@dataclass(frozen=True)
class TemplateCompilationIdentity:
    """Private identity observed from one successful compilation."""

    source: SourceIdentity
    compiler_driver: ToolIdentity | None
    compiler: ToolIdentity | None
    configuration: BicepConfigurationIdentity | None
    dependencies: DependencyIdentity
    compiled_output_digest: str
    output_digest_form: OutputDigestForm = OutputDigestForm.RAW_BYTES

    def __post_init__(self) -> None:
        _require_text(
            self.compiled_output_digest,
            "Compiled output digest",
        )


@dataclass(frozen=True)
class CompiledTemplate:
    """Successful compilation result retained by one session."""

    key: CompilationKey
    identity: TemplateCompilationIdentity
    parameters: tuple[TemplateParameter, ...]
    arm_json_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", tuple(self.parameters))
        if not isinstance(self.arm_json_bytes, bytes):
            raise TypeError("Compiled ARM content must be bytes.")
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Compiled template parameters must be unique.")

    def prepared_unit(self) -> PreparedTemplateUnit:
        """Return the plan-safe schema and identity without ARM content."""
        return PreparedTemplateUnit(
            key=self.key,
            identity=self.identity,
            parameters=self.parameters,
        )


@dataclass(frozen=True)
class PreparedTemplateUnit:
    """Plan-safe template schema and observed compilation identity."""

    key: CompilationKey
    identity: TemplateCompilationIdentity
    parameters: tuple[TemplateParameter, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", tuple(self.parameters))
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Prepared template parameters must be unique.")
        if self.key.source_path != self.identity.source.path:
            raise ValueError(
                "Prepared template key and identity source paths must match."
            )
        if (
            self.key.source_content_digest
            != self.identity.source.content_digest
        ):
            raise ValueError(
                "Prepared template key and identity source digests must "
                "match."
            )
        if self.key.template_kind is TemplateKind.BICEP:
            if (
                self.identity.compiler is None
                or self.identity.configuration is None
            ):
                raise ValueError(
                    "Prepared Bicep templates require compiler and "
                    "configuration identity."
                )
        elif (
            self.identity.compiler_driver is not None
            or self.identity.compiler is not None
            or self.identity.configuration is not None
        ):
            raise ValueError(
                "Prepared ARM JSON templates cannot carry Bicep toolchain "
                "identity."
            )

    @property
    def parameter_names(self) -> frozenset[str]:
        """Return the accepted deployment parameter names."""
        return frozenset(parameter.name for parameter in self.parameters)


@dataclass(frozen=True)
class CompilationFailure:
    """Expected compilation failure cached for one session."""

    code: CompilationFailureCode
    summary: str
    detail: str
    key: CompilationKey | None = None

    def __post_init__(self) -> None:
        _require_text(self.summary, "Compilation failure summary")
        _require_text(self.detail, "Compilation failure detail")


CompilationOutcome: TypeAlias = CompiledTemplate | CompilationFailure


class TemplateOutputError(ValueError):
    """Compiled ARM output does not contain a usable template object."""


def detect_template_kind(path: Path) -> TemplateKind:
    """Return the supported template kind for a path."""
    suffix = path.suffix.casefold()
    if suffix == ".bicep":
        return TemplateKind.BICEP
    if suffix == ".json":
        return TemplateKind.ARM_JSON
    raise ValueError(
        f"Unsupported template format: {path.suffix}. Expected .bicep or "
        ".json."
    )


def read_source_snapshot(path: Path) -> SourceSnapshot:
    """Read exact template bytes and compute their private identity."""
    resolved = path.resolve()
    content = resolved.read_bytes()
    return SourceSnapshot(
        identity=SourceIdentity(
            path=resolved,
            content_digest=sha256_bytes(content),
            size_bytes=len(content),
        ),
        content=content,
    )


def discover_bicep_configuration(source_path: Path) -> ConfigurationSnapshot:
    """Find the nearest ancestor `bicepconfig.json` for an entry file."""
    resolved = source_path.resolve()
    for directory in (resolved.parent, *resolved.parent.parents):
        candidate = directory / "bicepconfig.json"
        if not candidate.is_file():
            continue
        content = candidate.read_bytes()
        return ConfigurationSnapshot(
            identity=BicepConfigurationIdentity(
                path=candidate,
                content_digest=sha256_bytes(content),
                discovery=ConfigurationDiscovery.NEAREST_FOUND,
            ),
            content=content,
        )
    return ConfigurationSnapshot(
        identity=BicepConfigurationIdentity(
            path=None,
            content_digest=None,
            discovery=ConfigurationDiscovery.NONE_FOUND,
        )
    )


def _resolve_parameter_type_definition(
    body: dict[str, Any],
    definitions: Any,
    parameter_name: str,
) -> tuple[dict[str, Any], bool]:
    """Resolve local type aliases and their effective nullable modifier."""
    visited: set[str] = set()
    effective_nullable: bool | None = None
    while True:
        if "nullable" in body:
            nullable = body["nullable"]
            if type(nullable) is not bool:
                raise TemplateOutputError(
                    f"Compiled ARM parameter '{parameter_name}' has an "
                    "invalid nullable constraint. Expected a boolean."
                )
            # ARM/Bicep type modifiers on the closest $ref wrapper override
            # modifiers inherited from the referenced definition.
            if effective_nullable is None:
                effective_nullable = nullable
        if "$ref" not in body:
            return body, effective_nullable is True
        if "type" in body:
            raise TemplateOutputError(
                f"Compiled ARM parameter '{parameter_name}' cannot specify "
                "both a type and a type reference."
            )
        reference = body["$ref"]
        prefix = "#/definitions/"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise TemplateOutputError(
                f"Compiled ARM parameter '{parameter_name}' requires a "
                "local definitions type reference."
            )
        definition_name = reference[len(prefix):]
        if not definition_name or "/" in definition_name:
            raise TemplateOutputError(
                f"Compiled ARM parameter '{parameter_name}' has an invalid "
                "type reference."
            )
        if definition_name in visited:
            raise TemplateOutputError(
                f"Compiled ARM parameter '{parameter_name}' has a cyclic "
                "type reference."
            )
        visited.add(definition_name)
        definition = (
            definitions.get(definition_name)
            if isinstance(definitions, dict)
            else None
        )
        if not isinstance(definition, dict):
            raise TemplateOutputError(
                f"Compiled ARM parameter '{parameter_name}' references a "
                "missing or invalid type definition."
            )
        body = definition


def extract_template_parameters(
    arm_json: Any,
) -> tuple[TemplateParameter, ...]:
    """Extract a deterministic parameter schema from ARM JSON."""
    if not isinstance(arm_json, dict):
        raise TemplateOutputError(
            "Compiled ARM output must be a JSON object."
        )
    raw_parameters = arm_json.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        raise TemplateOutputError(
            "Compiled ARM output `parameters` must be a JSON object."
        )

    if any(
        not isinstance(name, str) or not name.strip()
        for name in raw_parameters
    ):
        raise TemplateOutputError(
            "Compiled ARM parameter names must be non-empty strings."
        )

    parameters: list[TemplateParameter] = []
    for name in sorted(raw_parameters):
        body = raw_parameters[name]
        if not isinstance(body, dict):
            raise TemplateOutputError(
                f"Compiled ARM parameter '{name}' must be a JSON object."
            )
        type_definition, nullable = _resolve_parameter_type_definition(
            body,
            arm_json.get("definitions"),
            name,
        )
        parameter_type = type_definition.get("type")
        if not isinstance(parameter_type, str):
            raise TemplateOutputError(
                f"Compiled ARM parameter '{name}' requires a string type."
            )
        normalized_type = parameter_type.casefold()
        if normalized_type not in _ARM_PARAMETER_TYPES:
            raise TemplateOutputError(
                f"Compiled ARM parameter '{name}' has unsupported type "
                f"{parameter_type!r}."
            )
        parameters.append(
            TemplateParameter(
                name=name,
                type=parameter_type,
                secure=normalized_type in {"securestring", "secureobject"},
                has_default="defaultValue" in body,
                nullable=nullable,
            )
        )
    return tuple(parameters)


def validate_arm_template(arm_json: Any) -> None:
    """Require an ARM deployment-template envelope."""
    if not isinstance(arm_json, dict):
        raise TemplateOutputError(
            "Compiled ARM output must be a JSON object."
        )
    schema = arm_json.get("$schema")
    if (
        not isinstance(schema, str)
        or not schema.strip()
        or not schema.casefold().endswith(_DEPLOYMENT_SCHEMA_SUFFIXES)
    ):
        raise TemplateOutputError(
            "ARM template `$schema` must identify a deployment template."
        )
    content_version = arm_json.get("contentVersion")
    if (
        not isinstance(content_version, str)
        or not content_version.strip()
    ):
        raise TemplateOutputError(
            "ARM template `contentVersion` must be a non-empty string."
        )
    resources = arm_json.get("resources")
    if not isinstance(resources, (list, dict)):
        raise TemplateOutputError(
            "ARM template `resources` must be an array or object."
        )


def extract_compiler_template_hashes(
    arm_json: Any,
) -> tuple[str, ...]:
    """Collect nested compiler-emitted template hashes deterministically."""
    hashes: set[str] = set()

    def visit(value: Any, *, root: bool = False) -> None:
        if isinstance(value, dict):
            if not root:
                metadata = value.get("metadata", {})
                generator = (
                    metadata.get("_generator", {})
                    if isinstance(metadata, dict)
                    else {}
                )
                template_hash = (
                    generator.get("templateHash")
                    if isinstance(generator, dict)
                    else None
                )
                if isinstance(template_hash, str) and template_hash.strip():
                    hashes.add(template_hash)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(arm_json, root=True)
    return tuple(sorted(hashes))


def extract_compiler_version(arm_json: Any) -> str | None:
    """Return the root compiler-emitted version when available."""
    if not isinstance(arm_json, dict):
        return None
    metadata = arm_json.get("metadata")
    if not isinstance(metadata, dict):
        return None
    generator = metadata.get("_generator")
    if not isinstance(generator, dict):
        return None
    version = generator.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version


def arm_json_dependency_identity(arm_json: Any) -> DependencyIdentity:
    """Classify whether ARM JSON carries unresolved linked templates."""
    linked_template_found = False

    def visit(value: Any) -> None:
        nonlocal linked_template_found
        if linked_template_found:
            return
        if isinstance(value, dict):
            resource_type = value.get("type")
            properties = value.get("properties")
            if (
                isinstance(resource_type, str)
                and resource_type.casefold()
                == "microsoft.resources/deployments"
                and isinstance(properties, dict)
                and isinstance(properties.get("templateLink"), dict)
            ):
                linked_template_found = True
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(arm_json)
    return DependencyIdentity(
        coverage=(
            DependencyCoverage.UNKNOWN
            if linked_template_found
            else DependencyCoverage.NOT_APPLICABLE
        )
    )


def _run_command(
    argv: tuple[str, ...],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


class TemplateCompilationSession:
    """Acquire each content-addressed template unit once."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner = _run_command,
        tool_resolver: ToolResolver = _resolve_tool_from_path,
        timeout_seconds: int = DEFAULT_COMPILATION_TIMEOUT_SECONDS,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Compilation timeout must be positive.")
        self._command_runner = command_runner
        self._tool_resolver = tool_resolver
        self._timeout_seconds = timeout_seconds
        # Engine-owned roots. Resolved on the first compiler allocation, so
        # constructing a session, resolving a tool, or acquiring an ARM JSON
        # template creates no directory and reads no environment.
        self._runtime_paths = runtime_paths
        self._sources: dict[Path, SourceSnapshot] = {}
        self._configurations: dict[Path, ConfigurationSnapshot] = {}
        self._path_failures: dict[Path, CompilationFailure] = {}
        self._outcomes: dict[CompilationKey, CompilationOutcome] = {}
        self._azure_cli: ToolIdentity | CompilationFailure | None = None
        self._bicep_toolchain: tuple[
            ToolIdentity,
            ToolIdentity,
            str,
        ] | CompilationFailure | None = None
        self._kubectl: ToolIdentity | CompilationFailure | None = None

    @property
    def outcomes(self) -> tuple[CompilationOutcome, ...]:
        """Return acquired outcomes in deterministic insertion order."""
        return (
            *self._path_failures.values(),
            *self._outcomes.values(),
        )

    def acquire(self, template_path: Path) -> CompilationOutcome:
        """Acquire schema and identity for one template."""
        resolved = template_path.resolve()
        cached_path_failure = self._path_failures.get(resolved)
        if cached_path_failure is not None:
            return cached_path_failure
        source = self._sources.get(resolved)
        try:
            if source is None:
                source = read_source_snapshot(resolved)
                self._sources[resolved] = source
        except OSError as error:
            failure = CompilationFailure(
                code=CompilationFailureCode.SOURCE_UNREADABLE,
                summary="Template source could not be read.",
                detail=f"Template source '{resolved}' could not be read: {error}",
            )
            self._path_failures[resolved] = failure
            return failure

        try:
            kind = detect_template_kind(resolved)
        except ValueError as error:
            failure = CompilationFailure(
                code=CompilationFailureCode.OUTPUT_INVALID,
                summary="Template format is not supported.",
                detail=str(error),
            )
            self._path_failures[resolved] = failure
            return failure

        if kind is TemplateKind.ARM_JSON:
            key = CompilationKey(
                source_path=resolved,
                source_content_digest=source.identity.content_digest,
                template_kind=kind,
                compiler_fingerprint=_ARM_JSON_COMPILER_FINGERPRINT,
                configuration_digest=_NO_CONFIGURATION_DIGEST,
                invocation=("read-arm-json",),
            )
            cached = self._outcomes.get(key)
            if cached is not None:
                return cached
            outcome = self._acquire_arm_json(source, key)
            self._outcomes[key] = outcome
            return outcome

        configuration = self._configurations.get(resolved)
        try:
            if configuration is None:
                configuration = discover_bicep_configuration(resolved)
                self._configurations[resolved] = configuration
        except OSError as error:
            failure = CompilationFailure(
                code=CompilationFailureCode.SOURCE_UNREADABLE,
                summary="Bicep configuration could not be read.",
                detail=(
                    f"Bicep configuration for '{resolved}' could not be "
                    f"read: {error}"
                ),
            )
            self._path_failures[resolved] = failure
            return failure

        toolchain = self._resolve_toolchain()
        if isinstance(toolchain, CompilationFailure):
            key = CompilationKey(
                source_path=resolved,
                source_content_digest=source.identity.content_digest,
                template_kind=kind,
                compiler_fingerprint=toolchain.code.value,
                configuration_digest=(
                    configuration.identity.content_digest
                    or _NO_CONFIGURATION_DIGEST
                ),
                invocation=("az", "bicep", "build"),
            )
            cached = self._outcomes.get(key)
            if cached is not None:
                return cached
            outcome = CompilationFailure(
                code=toolchain.code,
                summary=toolchain.summary,
                detail=toolchain.detail,
                key=key,
            )
            self._outcomes[key] = outcome
            return outcome

        azure_cli, compiler, fingerprint = toolchain
        key = CompilationKey(
            source_path=resolved,
            source_content_digest=source.identity.content_digest,
            template_kind=kind,
            compiler_fingerprint=fingerprint,
            configuration_digest=(
                configuration.identity.content_digest
                or _NO_CONFIGURATION_DIGEST
            ),
            invocation=("az", "bicep", "build"),
        )
        cached = self._outcomes.get(key)
        if cached is not None:
            return cached
        outcome = self._compile_bicep(
            source,
            configuration,
            key,
            azure_cli,
            compiler,
        )
        self._outcomes[key] = outcome
        return outcome

    def _resolve_toolchain(
        self,
    ) -> (
        tuple[ToolIdentity, ToolIdentity, str]
        | CompilationFailure
    ):
        if self._bicep_toolchain is not None:
            return self._bicep_toolchain

        azure_cli = self.resolve_azure_cli()
        if isinstance(azure_cli, CompilationFailure):
            self._bicep_toolchain = azure_cli
            return azure_cli
        resolved_az = azure_cli.resolved_path
        bicep_version = self._probe_bicep_version(str(resolved_az))
        compiler = ToolIdentity(
            provider="azure-cli-bicep",
            resolved_path=resolved_az,
            version=bicep_version,
            version_provenance=(
                VersionProvenance.KNOWN
                if bicep_version is not None
                else VersionProvenance.UNKNOWN
            ),
        )
        fingerprint = sha256_bytes(
            json.dumps(
                {
                    "azureCliPath": str(resolved_az),
                    "azureCliVersion": azure_cli.version,
                    "bicepVersion": bicep_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._bicep_toolchain = (azure_cli, compiler, fingerprint)
        return self._bicep_toolchain

    def resolve_azure_cli(
        self,
    ) -> ToolIdentity | CompilationFailure:
        """Resolve Azure CLI once without contacting Azure."""
        if self._azure_cli is not None:
            return self._azure_cli
        az_path = self._tool_resolver("az")
        if az_path is None:
            self._azure_cli = CompilationFailure(
                code=CompilationFailureCode.TOOL_MISSING,
                summary="Azure CLI is required.",
                detail=(
                    "Azure CLI (`az`) was not found on PATH. Install Azure "
                    "CLI and retry."
                ),
            )
            return self._azure_cli
        raw_path = Path(az_path)
        if not raw_path.is_absolute():
            self._azure_cli = CompilationFailure(
                code=CompilationFailureCode.TOOL_MISSING,
                summary="Azure CLI is required.",
                detail=(
                    "Azure CLI resolved to a relative path. Install it in "
                    "an absolute PATH location and retry."
                ),
            )
            return self._azure_cli
        resolved = raw_path.resolve()
        available, version = self._probe_azure_cli_version(str(resolved))
        if not available:
            self._azure_cli = CompilationFailure(
                code=CompilationFailureCode.TOOL_UNAVAILABLE,
                summary="Azure CLI is unavailable.",
                detail=(
                    "Azure CLI was found, but `az version` did not complete "
                    "successfully. Repair the installation and retry."
                ),
            )
            return self._azure_cli
        self._azure_cli = ToolIdentity(
            provider="azure-cli",
            resolved_path=resolved,
            version=version,
            version_provenance=(
                VersionProvenance.KNOWN
                if version is not None
                else VersionProvenance.UNKNOWN
            ),
        )
        return self._azure_cli

    def resolve_bicep_compiler(
        self,
    ) -> ToolIdentity | CompilationFailure:
        """Observe compiler provenance. Compilation determines availability."""
        toolchain = self._resolve_toolchain()
        if isinstance(toolchain, CompilationFailure):
            return toolchain
        return toolchain[1]

    def resolve_kubectl(
        self,
    ) -> ToolIdentity | CompilationFailure:
        """Resolve kubectl once without contacting a cluster."""
        if self._kubectl is not None:
            return self._kubectl
        kubectl_path = self._tool_resolver("kubectl")
        if kubectl_path is None:
            self._kubectl = CompilationFailure(
                code=CompilationFailureCode.TOOL_MISSING,
                summary="kubectl is required.",
                detail=(
                    "kubectl was not found on PATH. Install kubectl and retry."
                ),
            )
            return self._kubectl
        raw_path = Path(kubectl_path)
        if not raw_path.is_absolute():
            self._kubectl = CompilationFailure(
                code=CompilationFailureCode.TOOL_MISSING,
                summary="kubectl is required.",
                detail=(
                    "kubectl resolved to a relative path. Install it in an "
                    "absolute PATH location and retry."
                ),
            )
            return self._kubectl
        self._kubectl = ToolIdentity(
            provider="kubectl",
            resolved_path=raw_path.resolve(),
            version=None,
            version_provenance=VersionProvenance.UNKNOWN,
        )
        return self._kubectl

    def _probe_azure_cli_version(
        self,
        az_path: str,
    ) -> tuple[bool, str | None]:
        try:
            result = self._command_runner(
                (az_path, "version", "--output", "json"),
                self._timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            return False, None
        if result.returncode != 0:
            return False, None
        try:
            body = json.loads(result.stdout)
        except json.JSONDecodeError:
            return True, None
        version = body.get("azure-cli") if isinstance(body, dict) else None
        return (
            True,
            version
            if isinstance(version, str) and version.strip()
            else None,
        )

    def _probe_bicep_version(
        self,
        az_path: str,
    ) -> str | None:
        """Observe Bicep's version. Build may acquire a missing compiler."""
        try:
            result = self._command_runner(
                (az_path, "bicep", "version"),
                self._timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        match = _BICEP_VERSION_PATTERN.search(
            f"{result.stdout}\n{result.stderr}"
        )
        if match is None:
            return None
        version, commit = match.groups()
        return f"{version} ({commit})" if commit else version

    def _acquire_arm_json(
        self,
        source: SourceSnapshot,
        key: CompilationKey,
    ) -> CompilationOutcome:
        try:
            arm_json = json.loads(source.content.decode("utf-8-sig"))
            validate_arm_template(arm_json)
            parameters = extract_template_parameters(arm_json)
        except (UnicodeDecodeError, json.JSONDecodeError, TemplateOutputError) as error:
            return CompilationFailure(
                code=CompilationFailureCode.OUTPUT_INVALID,
                summary="ARM template output is invalid.",
                detail=f"ARM template '{source.identity.path}' is invalid: {error}",
                key=key,
            )
        return CompiledTemplate(
            key=key,
            identity=TemplateCompilationIdentity(
                source=source.identity,
                compiler_driver=None,
                compiler=None,
                configuration=None,
                dependencies=arm_json_dependency_identity(arm_json),
                compiled_output_digest=source.identity.content_digest,
            ),
            parameters=parameters,
            arm_json_bytes=source.content,
        )

    def _temporary_parent(self) -> Path:
        """Resolve, and prepare, the parent for compiler output.

        Called only when a template is actually compiled. A session that
        resolves tools, reads ARM JSON, or reports a cached outcome never
        creates a directory and never reads the environment.
        """
        if self._runtime_paths is None:
            self._runtime_paths = RuntimePaths.resolve()
        return prepare_root(self._runtime_paths.temp_root)

    def _compile_bicep(
        self,
        source: SourceSnapshot,
        configuration: ConfigurationSnapshot,
        key: CompilationKey,
        azure_cli: ToolIdentity,
        compiler: ToolIdentity,
    ) -> CompilationOutcome:
        try:
            temporary_parent = self._temporary_parent()
        except RuntimePathError as error:
            return CompilationFailure(
                code=CompilationFailureCode.FAILED,
                summary="Compiler scratch location is unusable.",
                detail=(
                    f"Template '{source.identity.path}' could not be "
                    f"compiled: {error}"
                ),
                key=key,
            )

        # Compiler output is engine-owned and transient, so it goes under the
        # engine temp root rather than beside the authored template. The
        # directory is unique and owner-only from creation, and is removed on
        # every exit from this block, including a failure or a timeout.
        with tempfile.TemporaryDirectory(
            prefix="siteops-bicep-",
            dir=temporary_parent,
        ) as temporary_directory:
            output_path = Path(temporary_directory) / "template.json"
            argv = (
                str(azure_cli.resolved_path),
                "bicep",
                "build",
                "--file",
                str(source.identity.path),
                "--outfile",
                str(output_path),
            )
            try:
                result = self._command_runner(
                    argv,
                    self._timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return CompilationFailure(
                    code=CompilationFailureCode.TIMEOUT,
                    summary="Template compilation timed out.",
                    detail=(
                        f"Template '{source.identity.path}' did not compile "
                        f"within {self._timeout_seconds}s."
                    ),
                    key=key,
                )
            except OSError as error:
                return CompilationFailure(
                    code=CompilationFailureCode.FAILED,
                    summary="Template compilation failed.",
                    detail=(
                        f"Template '{source.identity.path}' could not invoke "
                        f"the compiler: {error}"
                    ),
                    key=key,
                )

            changed = self._changed_input(source, configuration)
            if changed is not None:
                return CompilationFailure(
                    code=CompilationFailureCode.INPUT_CHANGED,
                    summary="Template input changed during compilation.",
                    detail=changed,
                    key=key,
                )

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                return CompilationFailure(
                    code=(
                        CompilationFailureCode.MODULE_UNAVAILABLE
                        if "BCP192" in detail
                        else CompilationFailureCode.FAILED
                    ),
                    summary="Template compilation failed.",
                    detail=(
                        f"Template '{source.identity.path}' failed to "
                        f"compile: {detail or '(no compiler output)'}"
                    ),
                    key=key,
                )

            try:
                output = output_path.read_bytes()
                arm_json = json.loads(output.decode("utf-8-sig"))
                validate_arm_template(arm_json)
                parameters = extract_template_parameters(arm_json)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TemplateOutputError,
            ) as error:
                return CompilationFailure(
                    code=CompilationFailureCode.OUTPUT_INVALID,
                    summary="Compiled template output is invalid.",
                    detail=(
                        f"Compiled output for '{source.identity.path}' is "
                        f"invalid: {error}"
                    ),
                    key=key,
                )

            return CompiledTemplate(
                key=key,
                identity=TemplateCompilationIdentity(
                    source=source.identity,
                    compiler_driver=azure_cli,
                    compiler=(
                        replace(
                            compiler,
                            version=emitted_version,
                            version_provenance=(
                                VersionProvenance.KNOWN
                            ),
                        )
                        if (
                            emitted_version := extract_compiler_version(
                                arm_json
                            )
                        )
                        is not None
                        else compiler
                    ),
                    configuration=configuration.identity,
                    dependencies=DependencyIdentity(
                        coverage=DependencyCoverage.COMPILED_OUTPUT_ONLY,
                        template_hashes=extract_compiler_template_hashes(
                            arm_json
                        ),
                    ),
                    compiled_output_digest=sha256_bytes(output),
                ),
                parameters=parameters,
                arm_json_bytes=output,
            )

    @staticmethod
    def _changed_input(
        source: SourceSnapshot,
        configuration: ConfigurationSnapshot,
    ) -> str | None:
        try:
            current_source = read_source_snapshot(source.identity.path)
        except OSError as error:
            return (
                f"Template source '{source.identity.path}' could not be "
                f"re-read after compilation: {error}"
            )
        if current_source.identity != source.identity:
            return (
                f"Template source '{source.identity.path}' changed during "
                "compilation."
            )

        try:
            current_configuration = discover_bicep_configuration(
                source.identity.path
            )
        except OSError as error:
            return (
                f"Bicep configuration for '{source.identity.path}' could not "
                f"be re-read after compilation: {error}"
            )
        if current_configuration.identity != configuration.identity:
            return (
                f"Bicep configuration for '{source.identity.path}' changed "
                "during compilation."
            )
        return None

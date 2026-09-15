# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Build complete workspace packages from a producer-owned source snapshot."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

import yaml

from siteops import workspace_package as package
from siteops import yamlio
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    checked_path,
    hash_file,
    is_link,
    open_regular_file,
    path_inventory,
    relative_artifact_path,
    require_node,
)
from siteops.browse import BrowseError, ContentReader
from siteops.compilation import (
    BicepCompilationOptions,
    CommandRunner,
    CompilationFailure,
    ConfigurationDiscovery,
    DependencyCoverage,
    TemplateCompilationSession,
    TemplateKind,
    VersionProvenance,
    resolve_tool_from_path,
)
from siteops.models import DeploymentStep, Manifest, _parse_manifest_spec
from siteops.runtime import RuntimePaths

logger = logging.getLogger(__name__)
CompilationSessionFactory = Callable[[], TemplateCompilationSession]


def _source_files(root: Path, workspace: str, companions: tuple[str, ...]) -> tuple[str, ...]:
    require_node(root, directory=True)
    if workspace != ".":
        checked_path(root, workspace, directory=True)
    files: set[str] = set()
    total_bytes = 0
    visited: set[str] = set()
    pending = [workspace, *companions]
    while pending:
        relative = pending.pop()
        if relative in visited:
            continue
        visited.add(relative)
        if len(visited) > package.MAX_NODES:
            raise ArtifactError("The source snapshot exceeds its path limit.")
        path = root if relative == "." else root.joinpath(*relative_artifact_path(relative).split("/"))
        try:
            info = path.lstat()
            if is_link(info):
                raise ArtifactError("A package source snapshot cannot contain links.")
            if relative != ".":
                checked_path(root, relative, directory=stat.S_ISDIR(info.st_mode))
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as children:
                    for child in children:
                        if len(pending) + len(visited) >= package.MAX_NODES:
                            raise ArtifactError("The source snapshot exceeds its path limit.")
                        pending.append(child.name if relative == "." else relative + "/" + child.name)
            else:
                if relative == package.PACKAGE_NAME:
                    raise ArtifactError("The source already contains reserved package metadata.")
                if info.st_size > package.MAX_FILE_BYTES:
                    raise ArtifactError("A package source file exceeds its byte limit.")
                total_bytes += info.st_size
                if total_bytes > package.MAX_TOTAL_BYTES:
                    raise ArtifactError("The package source exceeds its total byte limit.")
                if workspace == "." or relative.startswith(workspace + "/"):
                    package.check_configuration_filename(relative)
                files.add(relative)
                if len(files) > package.MAX_FILES:
                    raise ArtifactError("The source snapshot exceeds its file limit.")
        except OSError:
            raise ArtifactError("The package source snapshot could not be inspected.") from None
    path_inventory([package.PACKAGE_NAME, *files], limit=package.MAX_NODES)
    generated_prefix = package.workspace_payload_path(
        workspace,
        package.GENERATED_TEMPLATE_NAMESPACE,
    )
    if any(
        path == generated_prefix or path.startswith(generated_prefix + "/")
        for path in files
    ):
        raise ArtifactError(
            "The authored workspace cannot use the producer-owned template namespace."
        )
    return tuple(sorted(files))


def _record(root: Path, relative: str) -> tuple[PayloadFile, int]:
    digest = hashlib.sha256()
    compressor = zlib.compressobj(level=6, wbits=-15)
    compressed = 0
    size = 0
    with open_regular_file(checked_path(root, relative)) as stream:
        while chunk := stream.read(1024 * 1024):
            if size == 0 and chunk.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ArtifactError("Materialize Git LFS content before producing a workspace package.")
            size += len(chunk)
            if size > package.MAX_FILE_BYTES:
                raise ArtifactError("A package source file exceeds its byte limit.")
            digest.update(chunk)
            compressed += len(compressor.compress(chunk))
    compressed += len(compressor.flush())
    compression = (
        zipfile.ZIP_STORED if size > package.MAX_COMPRESSION_RATIO * compressed
        else zipfile.ZIP_DEFLATED
    )
    return PayloadFile(relative, digest.hexdigest(), size), compression


def _record_bytes(relative: str, content: bytes) -> tuple[PayloadFile, int]:
    if len(content) > package.MAX_FILE_BYTES:
        raise ArtifactError("A generated template artifact exceeds its byte limit.")
    compressor = zlib.compressobj(level=6, wbits=-15)
    compressed = len(compressor.compress(content)) + len(compressor.flush())
    compression = (
        zipfile.ZIP_STORED
        if len(content) > package.MAX_COMPRESSION_RATIO * compressed
        else zipfile.ZIP_DEFLATED
    )
    return (
        PayloadFile(relative, hashlib.sha256(content).hexdigest(), len(content)),
        compression,
    )


def _discover_template_sources(root: Path, workspace: str) -> tuple[str, ...]:
    workspace_path = root if workspace == "." else checked_path(root, workspace, directory=True)
    try:
        reader = ContentReader(workspace_path)
        entries = reader.inventory()
        if not reader.names_complete:
            raise ArtifactError(
                "Workspace deployment entries could not be inspected for package compilation."
            )
        manifests = {entry.path for entry in entries}
        sources: set[str] = set()

        def collect(manifest_path: str) -> set[str]:
            manifest = Manifest.from_file(
                workspace_path.joinpath(*manifest_path.split("/")),
                workspace_root=workspace_path,
            )
            result = set()
            for step in manifest.steps:
                if isinstance(step, DeploymentStep):
                    source = relative_artifact_path(step.template)
                    checked_path(workspace_path, source)
                    result.add(source)
            return result

        for manifest_path in sorted(manifests):
            sources.update(collect(manifest_path))
        for relative in _source_files(root, workspace, ()):
            path = root.joinpath(*relative.split("/"))
            manifest_path = path.relative_to(workspace_path).as_posix()
            if manifest_path in manifests:
                continue
            try:
                with open_regular_file(path) as stream:
                    raw = stream.read(package.MAX_FILE_BYTES + 1)
                if len(raw) > package.MAX_FILE_BYTES:
                    raise ArtifactError("A package source file exceeds its byte limit.")
                document = yamlio.load(raw.decode("utf-8"))
                if not isinstance(document, dict):
                    continue
                _parse_manifest_spec(document, path)
            except ArtifactError:
                raise
            except (ValueError, yaml.YAMLError):
                continue
            try:
                sources.update(collect(manifest_path))
            except (ValueError, yaml.YAMLError):
                if document.get("kind") == "Manifest":
                    raise
        return tuple(sorted(sources))
    except ArtifactError:
        raise
    except (BrowseError, OSError, ValueError):
        raise ArtifactError(
            "Workspace deployment entries or template paths are invalid."
        ) from None


def workspace_bicep_sources(root: Path, workspace: str) -> tuple[str, ...]:
    """Return Bicep deployment roots discovered throughout the workspace."""
    return tuple(
        source
        for source in _discover_template_sources(root, workspace)
        if Path(source).suffix.casefold() == ".bicep"
    )


def resolve_azure_cli_bicep_path() -> Path | None:
    """Resolve the already-installed Azure CLI Bicep binary without running it."""
    configured = os.environ.get("AZURE_CONFIG_DIR")
    config_root = Path(configured).expanduser() if configured else Path.home() / ".azure"
    name = "bicep.exe" if os.name == "nt" else "bicep"
    candidate = (config_root / "bin" / name).resolve()
    return candidate if candidate.is_file() else None


def create_producer_compilation_session(
    source_snapshot: Path,
    control_root: Path,
    *,
    azure_cli_path: Path | None = None,
    bicep_path: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> TemplateCompilationSession:
    """Create an isolated Azure CLI Bicep session for package production."""
    source_snapshot = source_snapshot.resolve()
    control_root = control_root.resolve()
    require_node(source_snapshot, directory=True)
    require_node(control_root, directory=True)
    if source_snapshot.parent != control_root:
        raise ArtifactError(
            "The package source snapshot must be a direct child of its compiler control root."
        )

    resolved_az = (
        Path(azure_cli_path)
        if azure_cli_path is not None
        else None
    )
    if resolved_az is not None and not resolved_az.is_absolute():
        raise ArtifactError("The Azure CLI path must be absolute.")
    if resolved_az is not None:
        resolved_az = resolved_az.resolve()
    if resolved_az is None:
        discovered_az = resolve_tool_from_path("az")
        resolved_az = Path(discovered_az).resolve() if discovered_az else None
    resolved_bicep = (
        Path(bicep_path)
        if bicep_path is not None
        else resolve_azure_cli_bicep_path()
    )
    if resolved_bicep is not None and not resolved_bicep.is_absolute():
        raise ArtifactError("The Bicep compiler path must be absolute.")
    if resolved_bicep is not None:
        resolved_bicep = resolved_bicep.resolve()
    if (
        resolved_az is None
        or not resolved_az.is_absolute()
        or not resolved_az.is_file()
    ):
        raise ArtifactError("Azure CLI must be installed at an absolute path.")
    if (
        resolved_bicep is None
        or not resolved_bicep.is_absolute()
        or not resolved_bicep.is_file()
    ):
        raise ArtifactError(
            "An existing Azure CLI Bicep installation is required for package production."
        )

    fallback_configuration = control_root / "bicepconfig.json"
    compiler_bin = control_root / "compiler-bin"
    azure_config = control_root / "azure-config"
    home = control_root / "home"
    temp_root = control_root / "temp"
    try:
        for directory in (compiler_bin, azure_config, home, temp_root):
            directory.mkdir(mode=0o700)
        with fallback_configuration.open("xb") as fallback:
            fallback.write(package.PRODUCER_DEFAULT_BICEP_CONFIGURATION)
        controlled_bicep = compiler_bin / (
            "bicep.exe" if os.name == "nt" else "bicep"
        )
        with (
            open_regular_file(resolved_bicep) as source,
            controlled_bicep.open("xb") as target,
        ):
            shutil.copyfileobj(source, target)
        controlled_bicep.chmod(0o700)
    except (FileExistsError, OSError):
        raise ArtifactError(
            "The controlled compiler environment could not be prepared."
        ) from None

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("AZURE_", "BICEP_", "ARM_"))
    }
    environment.update({
        "AZURE_BICEP_CHECK_VERSION": "false",
        "AZURE_BICEP_USE_BINARY_FROM_PATH": "true",
        "AZURE_CONFIG_DIR": str(azure_config),
        "AZURE_CORE_COLLECT_TELEMETRY": "false",
        "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
        "TMPDIR": str(temp_root),
        "PATH": (
            str(compiler_bin)
            + (os.pathsep + environment["PATH"] if environment.get("PATH") else "")
        ),
    })
    options = BicepCompilationOptions.controlled_producer(
        environment=environment,
        configuration_root=control_root,
        producer_default_configuration=fallback_configuration,
        bicep_executable_path=controlled_bicep,
    )
    return TemplateCompilationSession(
        command_runner=command_runner,
        tool_resolver=lambda name: str(resolved_az) if name == "az" else None,
        runtime_paths=RuntimePaths(temp_root=temp_root),
        bicep_options=options,
    )


def _package_tool(identity, label: str) -> package.PackageToolIdentity:
    if (
        identity is None
        or identity.version is None
        or identity.version_provenance is not VersionProvenance.KNOWN
    ):
        raise ArtifactError(f"Package {label} identity requires a known version.")
    return package.PackageToolIdentity(identity.provider, identity.version)


def _package_mapping(
    compiled,
    *,
    source_path: str,
    workspace_path: Path,
) -> tuple[package.PackageTemplateMapping, tuple[str, bytes] | None]:
    source_kind = compiled.key.template_kind
    source_identity = compiled.identity.source
    if source_kind is TemplateKind.ARM_JSON:
        return (
            package.PackageTemplateMapping(
                source_path=source_path,
                source_kind=source_kind,
                source_sha256=source_identity.content_digest,
                source_size=source_identity.size_bytes,
                artifact_path=source_path,
                artifact_sha256=compiled.identity.compiled_output_digest,
                artifact_size=len(compiled.arm_json_bytes),
                producer_mode="native-arm-json",
                invocation=compiled.key.invocation,
                driver=None,
                compiler=None,
                configuration=None,
                dependencies=package.PackageDependencyIdentity(
                    compiled.identity.dependencies.coverage,
                    compiled.identity.dependencies.template_hashes,
                ),
            ),
            None,
        )

    configuration = compiled.identity.configuration
    if configuration is None:
        raise ArtifactError("Package Bicep compilation omitted configuration identity.")
    if configuration.discovery is ConfigurationDiscovery.NEAREST_FOUND:
        try:
            configuration_path = configuration.path.relative_to(workspace_path).as_posix()
        except (AttributeError, ValueError):
            raise ArtifactError(
                "Package Bicep configuration must be inside the authored workspace."
            ) from None
        package_configuration = package.PackageConfigurationIdentity(
            configuration.discovery,
            configuration.content_digest or "",
            relative_artifact_path(configuration_path),
        )
    elif configuration.discovery is ConfigurationDiscovery.PRODUCER_DEFAULT:
        package_configuration = package.PackageConfigurationIdentity(
            configuration.discovery,
            configuration.content_digest or "",
        )
    else:
        raise ArtifactError("Package Bicep compilation requires explicit configuration identity.")
    if compiled.identity.dependencies.coverage is not DependencyCoverage.COMPILED_OUTPUT_ONLY:
        raise ArtifactError("Package Bicep dependency coverage is inconsistent.")
    artifact_path = package.compiled_template_path(source_path)
    artifact = (artifact_path, compiled.arm_json_bytes)
    return (
        package.PackageTemplateMapping(
            source_path=source_path,
            source_kind=source_kind,
            source_sha256=source_identity.content_digest,
            source_size=source_identity.size_bytes,
            artifact_path=artifact_path,
            artifact_sha256=compiled.identity.compiled_output_digest,
            artifact_size=len(compiled.arm_json_bytes),
            producer_mode="azure-cli-bicep",
            invocation=compiled.key.invocation,
            driver=_package_tool(compiled.identity.compiler_driver, "driver"),
            compiler=_package_tool(compiled.identity.compiler, "compiler"),
            configuration=package_configuration,
            dependencies=package.PackageDependencyIdentity(
                compiled.identity.dependencies.coverage,
                compiled.identity.dependencies.template_hashes,
            ),
        ),
        artifact,
    )


def _zip_info(path: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = compression
    return info


def _publish(source: Path, output: Path, expected: str) -> None:
    descriptor = os.open(
        output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as target, open_regular_file(source) as incoming:
            digest = hashlib.sha256()
            while chunk := incoming.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
            if digest.hexdigest() != expected:
                raise ArtifactError("The package changed before output publication.")
        complete = True
    finally:
        if not complete:
            try:
                output.unlink()
            except OSError:
                logger.warning("Incomplete package output cleanup could not be completed.")


def build_package(
    snapshot: Path,
    output: Path,
    *,
    workspace: str,
    kit_id: str,
    version: str,
    source_revision: str,
    siteops_range: str,
    companions: tuple[str, ...] = (),
    required_features: tuple[str, ...] = ("manifest/v1",),
    compilation_session_factory: CompilationSessionFactory | None = None,
) -> package.PackageInspection:
    """Build a complete workspace package from a prepared source snapshot.

    Callers supply an immutable reviewed snapshot. The Git producer command
    establishes it separately. This function creates no provenance assertion
    and never overwrites an existing output.
    """
    require_node(snapshot, directory=True)
    snapshot = snapshot.resolve()
    output = Path(os.path.abspath(output))
    require_node(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        raise ArtifactError("The package output already exists.")
    paths = _source_files(snapshot, workspace, companions)
    records = []
    compression = {}
    total = 0
    for relative in paths:
        entry, method = _record(snapshot, relative)
        records.append(entry)
        compression[relative] = method
        total += entry.size
        if total > package.MAX_TOTAL_BYTES:
            raise ArtifactError("The package source exceeds its total byte limit.")
    files = tuple(records)
    workspace_path = snapshot if workspace == "." else checked_path(
        snapshot,
        workspace,
        directory=True,
    )
    template_sources = _discover_template_sources(snapshot, workspace)
    requires_bicep = any(
        Path(source).suffix.casefold() == ".bicep"
        for source in template_sources
    )
    if requires_bicep:
        if compilation_session_factory is None:
            raise ArtifactError(
                "Bicep package production requires a controlled compilation session."
            )
        session = compilation_session_factory()
        if not session.bicep_options.is_controlled_producer:
            raise ArtifactError(
                "Bicep package production requires controlled compiler options."
            )
    else:
        session = TemplateCompilationSession()

    mappings = []
    generated: dict[str, bytes] = {}
    inventory = {entry.path: entry for entry in files}
    for source_path in template_sources:
        outcome = session.acquire(workspace_path.joinpath(*source_path.split("/")))
        if isinstance(outcome, CompilationFailure):
            raise ArtifactError(
                f"Template '{source_path}' could not be produced: {outcome.summary}"
            )
        mapping, artifact = _package_mapping(
            outcome,
            source_path=source_path,
            workspace_path=workspace_path,
        )
        source_entry = inventory.get(
            package.workspace_payload_path(workspace, source_path)
        )
        if source_entry is None or (
            source_entry.sha256,
            source_entry.size,
        ) != (
            mapping.source_sha256,
            mapping.source_size,
        ):
            raise ArtifactError("A template source changed during package production.")
        mappings.append(mapping)
        if artifact is not None:
            artifact_path, content = artifact
            package_path = package.workspace_payload_path(workspace, artifact_path)
            if package_path in inventory or package_path in generated:
                raise ArtifactError("A generated template artifact path is not unique.")
            entry, method = _record_bytes(package_path, content)
            records.append(entry)
            compression[package_path] = method
            generated[package_path] = content
            total += entry.size
            if total > package.MAX_TOTAL_BYTES:
                raise ArtifactError("The package source exceeds its total byte limit.")
    files = tuple(sorted(records, key=lambda entry: entry.path))
    features = (
        required_features
        if package.COMPILED_TEMPLATE_FEATURE in required_features
        else (*required_features, package.COMPILED_TEMPLATE_FEATURE)
    )
    metadata = package.WorkspacePackage(
        kit_id, version, source_revision, workspace, siteops_range, features,
        files, package.workspace_tree_digest(files, workspace), tuple(mappings),
    )
    metadata = package.WorkspacePackage.from_document(metadata.document())
    package.check_compatibility(metadata)
    raw = package.json_bytes(metadata.document())
    if len(raw) > package.MAX_METADATA_BYTES:
        raise ArtifactError("Workspace package metadata exceeds its byte limit.")
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".siteops-package-", delete=False) as temp:
            staged = Path(temp.name)
            with zipfile.ZipFile(temp, "w", allowZip64=False, compresslevel=6) as archive:
                archive.writestr(_zip_info(package.PACKAGE_NAME, zipfile.ZIP_STORED), raw)
                for entry in metadata.files:
                    info = _zip_info(entry.path, compression[entry.path])
                    info.file_size = entry.size
                    if entry.path in generated:
                        archive.writestr(info, generated[entry.path])
                        continue
                    digest = hashlib.sha256()
                    size = 0
                    with (
                        open_regular_file(checked_path(snapshot, entry.path)) as source,
                        archive.open(info, "w") as target,
                    ):
                        while chunk := source.read(min(1024 * 1024, entry.size - size + 1)):
                            size += len(chunk)
                            if size > entry.size:
                                raise ArtifactError("A package source changed during production.")
                            target.write(chunk)
                            digest.update(chunk)
                    if size != entry.size or digest.hexdigest() != entry.sha256:
                        raise ArtifactError("A package source changed during production.")
        _, digest = hash_file(staged, limit=package.MAX_ARCHIVE_BYTES)
        inspection = package.inspect_package(staged, digest)
        _publish(staged, output, digest)
        return inspection
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        raise ArtifactError("The workspace package could not be produced.") from None
    finally:
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                logger.warning("Package-production staging cleanup could not be completed.")

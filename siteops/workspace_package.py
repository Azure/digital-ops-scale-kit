# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workspace-package integrity and confined materialization.

These operations do not authenticate a publisher. Remote acquisition must
establish provenance under consumer-owned policy before materialization.
No manifest, Site, template or package-provided code is evaluated here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from siteops import __version__
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    checked_path,
    hash_file,
    hash_stream,
    is_link,
    open_regular_file,
    path_inventory,
    relative_artifact_path,
    require_node,
)
from siteops.compilation import (
    ConfigurationDiscovery,
    DependencyCoverage,
    TemplateKind,
    TemplateOutputError,
    arm_json_dependency_identity,
    extract_compiler_template_hashes,
    extract_compiler_version,
    extract_template_parameters,
    validate_arm_template,
)
from siteops.manifest_selection import is_explicit_manifest_path, select_manifest_path

PACKAGE_NAME = "siteops-package.json"
PACKAGE_API = "siteops/v1alpha1"
GENERATED_TEMPLATE_NAMESPACE = ".siteops/compiled"
GENERATED_TEMPLATE_ROOT = f"{GENERATED_TEMPLATE_NAMESPACE}/v1"
PRODUCER_DEFAULT_BICEP_CONFIGURATION = b"{}\n"
PRODUCER_DEFAULT_BICEP_CONFIGURATION_SHA256 = hashlib.sha256(
    PRODUCER_DEFAULT_BICEP_CONFIGURATION
).hexdigest()
MAX_FILES = 10000
MAX_NODES = 20000
MAX_TEMPLATE_MAPPINGS = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_CENTRAL_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
COMPILED_TEMPLATE_FEATURE = "compiled-templates/v1"
SUPPORTED_FEATURES = frozenset({
    "manifest/v1",
    "composition/v1",
    "manifest-selection/v1",
    COMPILED_TEMPLATE_FEATURE,
})
_SHA256 = re.compile(r"[0-9a-f]{64}")
logger = logging.getLogger(__name__)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ArtifactError("Workspace package metadata has missing or unsupported fields.")
    return value


def _text(value: Any, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > maximum
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ArtifactError("Workspace package text fields must be bounded and printable.")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArtifactError("Workspace package identities must be lowercase SHA-256 digests.")
    return value


def _strict_json(raw: bytes, *, label: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArtifactError(f"{label} contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ArtifactError(f"{label} contains a non-JSON number.")

    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ArtifactError):
            raise
        raise ArtifactError(f"{label} must be bounded UTF-8 JSON.") from None


def _engine_range(value: Any) -> str:
    text = _text(value)
    try:
        specifiers = SpecifierSet(text)
    except InvalidSpecifier:
        raise ArtifactError("The Site Ops compatibility range is invalid.") from None
    operators = {item.operator for item in specifiers}
    if "===" in operators or not (
        operators & {"==", "~="}
        or (operators & {">", ">="} and operators & {"<", "<="})
    ):
        raise ArtifactError("The Site Ops compatibility range must have lower and upper bounds.")
    return text


def json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def workspace_tree_digest(files: tuple[PayloadFile, ...], workspace: str) -> str:
    """Identify raw workspace files using sorted workspace-relative file records."""
    prefix = "" if workspace == "." else workspace + "/"
    records = [
        {**entry.document(), "path": entry.path.removeprefix(prefix)}
        for entry in sorted(files, key=lambda entry: entry.path)
        if entry.path.startswith(prefix)
    ]
    if not records:
        raise ArtifactError("The package contains no files in its declared workspace.")
    framed = b"siteops.workspace-tree/v1\x00" + json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(framed).hexdigest()


def workspace_payload_path(workspace: str, relative: str) -> str:
    """Return one package-relative path below the declared workspace."""
    relative = relative_artifact_path(relative)
    return relative if workspace == "." else f"{workspace}/{relative}"


def compiled_template_path(source_path: str) -> str:
    """Return the deterministic producer-owned artifact path for Bicep source."""
    source_path = relative_artifact_path(source_path)
    return relative_artifact_path(
        f"{GENERATED_TEMPLATE_ROOT}/{source_path}.json"
    )


@dataclass(frozen=True)
class PackageToolIdentity:
    provider: str
    version: str

    def document(self) -> dict[str, str]:
        return {"provider": self.provider, "version": self.version}


@dataclass(frozen=True)
class PackageConfigurationIdentity:
    discovery: ConfigurationDiscovery
    sha256: str
    path: str | None = None

    def document(self) -> dict[str, str]:
        document = {
            "discovery": self.discovery.value,
            "sha256": self.sha256,
        }
        if self.path is not None:
            document["path"] = self.path
        return document


@dataclass(frozen=True)
class PackageDependencyIdentity:
    coverage: DependencyCoverage
    template_hashes: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage.value,
            "templateHashes": list(self.template_hashes),
        }


@dataclass(frozen=True)
class PackageTemplateMapping:
    source_path: str
    source_kind: TemplateKind
    source_sha256: str
    source_size: int
    artifact_path: str
    artifact_sha256: str
    artifact_size: int
    producer_mode: str
    invocation: tuple[str, ...]
    driver: PackageToolIdentity | None
    compiler: PackageToolIdentity | None
    configuration: PackageConfigurationIdentity | None
    dependencies: PackageDependencyIdentity

    def document(self) -> dict[str, Any]:
        return {
            "source": {
                "path": self.source_path,
                "kind": self.source_kind.value,
                "sha256": self.source_sha256,
                "size": self.source_size,
            },
            "artifact": {
                "path": self.artifact_path,
                "kind": TemplateKind.ARM_JSON.value,
                "sha256": self.artifact_sha256,
                "size": self.artifact_size,
            },
            "producer": {
                "mode": self.producer_mode,
                "invocation": list(self.invocation),
                "driver": None if self.driver is None else self.driver.document(),
                "compiler": None if self.compiler is None else self.compiler.document(),
                "configuration": (
                    None
                    if self.configuration is None
                    else self.configuration.document()
                ),
                "dependencies": self.dependencies.document(),
            },
        }


def _file_identity(value: Any, *, include_kind: bool) -> tuple[str, str, int, str | None]:
    keys = {"path", "sha256", "size", "kind"} if include_kind else {"path", "sha256", "size"}
    row = _object(value, keys)
    path = relative_artifact_path(row["path"])
    size = row["size"]
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise ArtifactError("A template identity size is invalid or exceeds its limit.")
    kind = row.get("kind")
    if include_kind and kind not in {item.value for item in TemplateKind}:
        raise ArtifactError("A template source kind is unsupported.")
    return path, _digest(row["sha256"]), size, kind


def _package_tool(value: Any) -> PackageToolIdentity:
    row = _object(value, {"provider", "version"})
    return PackageToolIdentity(
        _text(row["provider"], maximum=128),
        _text(row["version"], maximum=128),
    )


def _package_configuration(value: Any) -> PackageConfigurationIdentity:
    if not isinstance(value, dict):
        raise ArtifactError("A Bicep template requires configuration identity.")
    discovery = value.get("discovery")
    if discovery == ConfigurationDiscovery.NEAREST_FOUND.value:
        row = _object(value, {"discovery", "path", "sha256"})
        return PackageConfigurationIdentity(
            ConfigurationDiscovery.NEAREST_FOUND,
            _digest(row["sha256"]),
            relative_artifact_path(row["path"]),
        )
    if discovery == ConfigurationDiscovery.PRODUCER_DEFAULT.value:
        row = _object(value, {"discovery", "sha256"})
        digest = _digest(row["sha256"])
        if digest != PRODUCER_DEFAULT_BICEP_CONFIGURATION_SHA256:
            raise ArtifactError(
                "The producer-default Bicep configuration identity is invalid."
            )
        return PackageConfigurationIdentity(
            ConfigurationDiscovery.PRODUCER_DEFAULT,
            digest,
        )
    raise ArtifactError("A package Bicep configuration identity is unsupported.")


def _effective_workspace_configuration(
    inventory: dict[str, PayloadFile],
    workspace_root: str,
    source_path: str,
) -> str | None:
    directory = PurePosixPath(source_path).parent
    while True:
        candidate = (
            "bicepconfig.json"
            if directory == PurePosixPath(".")
            else (directory / "bicepconfig.json").as_posix()
        )
        if workspace_payload_path(workspace_root, candidate) in inventory:
            return candidate
        if directory == PurePosixPath("."):
            return None
        directory = directory.parent


def _package_dependencies(value: Any) -> PackageDependencyIdentity:
    row = _object(value, {"coverage", "templateHashes"})
    try:
        coverage = DependencyCoverage(row["coverage"])
    except (TypeError, ValueError):
        raise ArtifactError("A template dependency coverage value is unsupported.") from None
    hashes = row["templateHashes"]
    if not isinstance(hashes, list) or len(hashes) > MAX_TEMPLATE_MAPPINGS:
        raise ArtifactError("A template dependency identity exceeds its limits.")
    template_hashes = tuple(
        _text(item, maximum=256)
        for item in hashes
    )
    if len(template_hashes) != len(set(template_hashes)):
        raise ArtifactError("A template dependency identity contains duplicate hashes.")
    return PackageDependencyIdentity(coverage, tuple(sorted(template_hashes)))


def _template_mapping(value: Any) -> PackageTemplateMapping:
    row = _object(value, {"source", "artifact", "producer"})
    source_path, source_sha256, source_size, source_kind_value = _file_identity(
        row["source"],
        include_kind=True,
    )
    artifact_path, artifact_sha256, artifact_size, artifact_kind = _file_identity(
        row["artifact"],
        include_kind=True,
    )
    if artifact_kind != TemplateKind.ARM_JSON.value:
        raise ArtifactError("Package template artifacts must be ARM JSON.")
    source_kind = TemplateKind(source_kind_value)
    producer = _object(
        row["producer"],
        {"mode", "invocation", "driver", "compiler", "configuration", "dependencies"},
    )
    mode = _text(producer["mode"], maximum=128)
    raw_invocation = producer["invocation"]
    if not isinstance(raw_invocation, list) or not 1 <= len(raw_invocation) <= 16:
        raise ArtifactError("A package template invocation is invalid.")
    invocation = tuple(_text(item, maximum=128) for item in raw_invocation)
    dependencies = _package_dependencies(producer["dependencies"])

    if source_path.startswith(f"{GENERATED_TEMPLATE_NAMESPACE}/"):
        raise ArtifactError("Template sources cannot use the producer-owned namespace.")
    if source_kind is TemplateKind.BICEP:
        if (
            not source_path.casefold().endswith(".bicep")
            or artifact_path != compiled_template_path(source_path)
            or mode != "azure-cli-bicep"
            or invocation != ("az", "bicep", "build", "--no-restore")
            or producer["driver"] is None
            or producer["compiler"] is None
            or producer["configuration"] is None
            or dependencies.coverage is not DependencyCoverage.COMPILED_OUTPUT_ONLY
        ):
            raise ArtifactError("A Bicep package template mapping is inconsistent.")
        driver = _package_tool(producer["driver"])
        compiler = _package_tool(producer["compiler"])
        if (
            driver.provider != "azure-cli"
            or compiler.provider != "azure-cli-bicep"
        ):
            raise ArtifactError("A Bicep package toolchain identity is unsupported.")
        configuration = _package_configuration(producer["configuration"])
    else:
        if (
            not source_path.casefold().endswith(".json")
            or artifact_path != source_path
            or source_sha256 != artifact_sha256
            or source_size != artifact_size
            or mode != "native-arm-json"
            or invocation != ("read-arm-json",)
            or producer["driver"] is not None
            or producer["compiler"] is not None
            or producer["configuration"] is not None
            or dependencies.coverage not in {
                DependencyCoverage.NOT_APPLICABLE,
                DependencyCoverage.UNKNOWN,
            }
            or dependencies.template_hashes
        ):
            raise ArtifactError("A native ARM package template mapping is inconsistent.")
        driver = None
        compiler = None
        configuration = None

    return PackageTemplateMapping(
        source_path=source_path,
        source_kind=source_kind,
        source_sha256=source_sha256,
        source_size=source_size,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        artifact_size=artifact_size,
        producer_mode=mode,
        invocation=invocation,
        driver=driver,
        compiler=compiler,
        configuration=configuration,
        dependencies=dependencies,
    )


@dataclass(frozen=True)
class WorkspacePackage:
    kit_id: str
    version: str
    source_revision: str
    workspace_root: str
    siteops_range: str
    required_features: tuple[str, ...]
    files: tuple[PayloadFile, ...]
    tree_sha256: str
    templates: tuple[PackageTemplateMapping, ...]

    def document(self) -> dict[str, Any]:
        return {
            "apiVersion": PACKAGE_API,
            "kind": "WorkspacePackage",
            "kit": {"id": self.kit_id, "version": self.version},
            "source": {"revision": self.source_revision},
            "workspace": {
                "root": self.workspace_root,
                "tree": {"algorithm": "sha256", "digest": self.tree_sha256},
            },
            "compatibility": {
                "siteops": self.siteops_range, "requiredFeatures": list(self.required_features),
            },
            "files": [entry.document() for entry in self.files],
            "templates": {
                "artifactRoot": GENERATED_TEMPLATE_ROOT,
                "entries": [entry.document() for entry in self.templates],
            },
        }

    @classmethod
    def from_document(cls, document: Any) -> WorkspacePackage:
        root = _object(document, {
            "apiVersion", "kind", "kit", "source", "workspace", "compatibility", "files",
            "templates",
        })
        if root["apiVersion"] != PACKAGE_API or root["kind"] != "WorkspacePackage":
            raise ArtifactError("The workspace package format is unsupported.")
        kit = _object(root["kit"], {"id", "version"})
        source = _object(root["source"], {"revision"})
        workspace = _object(root["workspace"], {"root", "tree"})
        tree = _object(workspace["tree"], {"algorithm", "digest"})
        if tree["algorithm"] != "sha256":
            raise ArtifactError("The workspace tree identity must use SHA-256.")
        workspace_root = workspace["root"]
        if workspace_root != ".":
            workspace_root = relative_artifact_path(workspace_root)
        compatibility = _object(root["compatibility"], {"siteops", "requiredFeatures"})
        features = compatibility["requiredFeatures"]
        if not isinstance(features, list) or len(features) > 64:
            raise ArtifactError("The required-feature inventory is invalid.")
        required_features = tuple(_text(item, maximum=128) for item in features)
        if len(set(required_features)) != len(required_features):
            raise ArtifactError("The required-feature inventory contains duplicates.")
        if COMPILED_TEMPLATE_FEATURE not in required_features:
            raise ArtifactError(
                "The package must require compiled-template mapping support."
            )
        entries = root["files"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            raise ArtifactError("The package file inventory exceeds its limits or is empty.")
        files = []
        total = 0
        for value in entries:
            row = _object(value, {"path", "sha256", "size"})
            path = relative_artifact_path(row["path"])
            if path == PACKAGE_NAME:
                raise ArtifactError("The package metadata cannot include itself in its payload.")
            size = row["size"]
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                raise ArtifactError("A package file size is invalid or exceeds its limit.")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ArtifactError("The package payload exceeds its total byte limit.")
            files.append(PayloadFile(path, _digest(row["sha256"]), size))
        path_inventory([PACKAGE_NAME, *(entry.path for entry in files)], limit=MAX_NODES)
        ordered = tuple(sorted(files, key=lambda entry: entry.path))
        expected_tree = _digest(tree["digest"])
        if workspace_tree_digest(ordered, workspace_root) != expected_tree:
            raise ArtifactError("The workspace tree identity does not match its file inventory.")
        template_document = _object(root["templates"], {"artifactRoot", "entries"})
        if template_document["artifactRoot"] != GENERATED_TEMPLATE_ROOT:
            raise ArtifactError("The producer-owned template namespace is unsupported.")
        template_entries = template_document["entries"]
        if not isinstance(template_entries, list) or len(template_entries) > MAX_TEMPLATE_MAPPINGS:
            raise ArtifactError("The package template mapping exceeds its limits.")
        templates = tuple(sorted(
            (_template_mapping(value) for value in template_entries),
            key=lambda entry: entry.source_path,
        ))
        source_paths = [entry.source_path for entry in templates]
        artifact_paths = [entry.artifact_path for entry in templates]
        if (
            len(source_paths) != len(set(source_paths))
            or len(artifact_paths) != len(set(artifact_paths))
        ):
            raise ArtifactError("Package template source and artifact paths must be unique.")
        inventory = {entry.path: entry for entry in ordered}
        for template in templates:
            source_record = inventory.get(
                workspace_payload_path(workspace_root, template.source_path)
            )
            artifact = inventory.get(
                workspace_payload_path(workspace_root, template.artifact_path)
            )
            if source_record is None or (
                source_record.sha256,
                source_record.size,
            ) != (
                template.source_sha256,
                template.source_size,
            ):
                raise ArtifactError("A template source identity does not match the file inventory.")
            if artifact is None or (
                artifact.sha256,
                artifact.size,
            ) != (
                template.artifact_sha256,
                template.artifact_size,
            ):
                raise ArtifactError("A template artifact identity does not match the file inventory.")
            configuration = template.configuration
            if template.source_kind is TemplateKind.BICEP:
                effective_configuration = _effective_workspace_configuration(
                    inventory,
                    workspace_root,
                    template.source_path,
                )
                if configuration is None:
                    raise ArtifactError(
                        "A Bicep template mapping requires configuration identity."
                    )
                if configuration.discovery is ConfigurationDiscovery.NEAREST_FOUND:
                    if configuration.path != effective_configuration:
                        raise ArtifactError(
                            "A Bicep configuration identity is not the effective "
                            "workspace configuration."
                        )
                    config = inventory.get(
                        workspace_payload_path(
                            workspace_root,
                            configuration.path or "",
                        )
                    )
                    if config is None or config.sha256 != configuration.sha256:
                        raise ArtifactError(
                            "A Bicep configuration identity does not match the file inventory."
                        )
                elif effective_configuration is not None:
                    raise ArtifactError(
                        "A producer-default configuration cannot replace an "
                        "authored workspace configuration."
                    )
        generated = {
            entry.path
            for entry in ordered
            if entry.path.startswith(
                workspace_payload_path(
                    workspace_root,
                    GENERATED_TEMPLATE_NAMESPACE,
                ) + "/"
            )
        }
        mapped_generated = {
            workspace_payload_path(workspace_root, entry.artifact_path)
            for entry in templates
            if entry.source_kind is TemplateKind.BICEP
        }
        if generated != mapped_generated:
            raise ArtifactError(
                "The producer-owned template namespace does not match its mappings."
            )
        return cls(
            _text(kit["id"], maximum=128), _text(kit["version"], maximum=128),
            _text(source["revision"]), workspace_root, _engine_range(compatibility["siteops"]),
            tuple(sorted(required_features)), ordered, expected_tree, templates,
        )


@dataclass(frozen=True)
class PackageInspection:
    """Content identities and compatibility, without publisher authentication."""

    metadata: WorkspacePackage
    sha256: str
    size: int
    metadata_sha256: str
    metadata_size: int


@dataclass(frozen=True)
class BoundPackageTemplate:
    """One authored template and the package artifact used for execution."""

    source_path: Path
    artifact_path: Path
    source_relative_path: str
    artifact_relative_path: str
    mapping: PackageTemplateMapping


def _materialized_paths(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            raise ArtifactError(
                "The materialized package inventory could not be read safely."
            ) from None
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            relative_artifact_path(relative)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                raise ArtifactError(
                    "A materialized package path could not be inspected."
                ) from None
            if is_link(info):
                raise ArtifactError(
                    "Materialized package paths must not use links or reparse points."
                )
            if child.is_dir(follow_symlinks=False):
                require_node(path, directory=True)
                try:
                    if path.resolve(strict=True) != path:
                        raise ArtifactError(
                            "Materialized package paths cannot use filesystem aliases."
                        )
                except OSError:
                    raise ArtifactError(
                        "A materialized package path could not be resolved."
                    ) from None
                directories.add(relative)
                stack.append(path)
                continue
            require_node(path, directory=False)
            files.add(relative)
    path_inventory(list(files), limit=MAX_NODES)
    return files, directories


def _validate_materialized_package(
    root: Path,
    inspection: PackageInspection,
) -> Path:
    root = Path(os.path.abspath(root))
    require_node(root, directory=True)
    try:
        if root.resolve(strict=True) != root:
            raise ArtifactError(
                "The materialized package root cannot use a filesystem alias."
            )
    except OSError:
        raise ArtifactError(
            "The materialized package root could not be resolved."
        ) from None

    expected = {
        PACKAGE_NAME: PayloadFile(
            PACKAGE_NAME,
            inspection.metadata_sha256,
            inspection.metadata_size,
        ),
        **{entry.path: entry for entry in inspection.metadata.files},
    }
    expected_directories = path_inventory(
        list(expected),
        limit=MAX_NODES,
    )
    actual_files, actual_directories = _materialized_paths(root)
    if actual_files != set(expected) or actual_directories != expected_directories:
        raise ArtifactError(
            "The materialized package does not match its declared path inventory."
        )

    for relative, identity in expected.items():
        path = checked_path(root, relative)
        size, digest = hash_file(path, limit=identity.size)
        if size != identity.size or digest != identity.sha256:
            raise ArtifactError(
                "A materialized package file does not match its declared identity."
            )

    metadata_path = checked_path(root, PACKAGE_NAME)
    try:
        with open_regular_file(metadata_path) as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
    except OSError:
        raise ArtifactError(
            "The materialized package metadata could not be read safely."
        ) from None
    if len(raw) != inspection.metadata_size:
        raise ArtifactError(
            "The materialized package metadata does not match its inspected identity."
        )
    if _load_metadata(raw) != inspection.metadata:
        raise ArtifactError(
            "The materialized package metadata differs from the inspected package."
        )
    check_compatibility(inspection.metadata)
    return root


@dataclass(frozen=True)
class MaterializedPackageBinding:
    """Bind one verified materialization and authored manifest to execution.

    The caller must verify source provenance independently and hold an
    immutable cache lease for this binding's full planning and execution
    lifetime. A receipt document or cache-shaped path is not authority.
    This class validates package bytes and inventory, but it is not an OS
    sandbox against another process running as the same user.
    """

    inspection: PackageInspection
    package_root: Path
    workspace: Path
    manifest_path: Path
    manifest_relative_path: str

    @classmethod
    def bind(
        cls,
        inspection: PackageInspection,
        package_root: Path,
        manifest: str | Path,
    ) -> MaterializedPackageBinding:
        """Validate a materialized package and resolve one canonical manifest."""
        root = _validate_materialized_package(package_root, inspection)
        workspace = (
            root
            if inspection.metadata.workspace_root == "."
            else checked_path(
                root,
                inspection.metadata.workspace_root,
                directory=True,
            )
        )

        if is_explicit_manifest_path(manifest):
            raw = str(manifest).replace("\\", "/")
            candidate = Path(raw)
            path = candidate if candidate.is_absolute() else workspace / candidate
        else:
            from siteops.browse import ContentReader

            reader = ContentReader(workspace)
            entries = reader.inventory()
            selection = str(manifest)
            relative = select_manifest_path(
                selection,
                (
                    (entry.name, entry.path)
                    for entry in entries
                    if entry.guidance.role != "partial"
                ),
                names_complete=reader.names_complete,
                filename_match=reader.filename_candidate(selection),
            )
            path = workspace / relative

        manifest_path, relative = _require_materialized_workspace_file(
            root,
            workspace,
            inspection.metadata,
            path,
        )
        return cls(
            inspection=inspection,
            package_root=root,
            workspace=workspace,
            manifest_path=manifest_path,
            manifest_relative_path=relative,
        )

    def validate(self) -> None:
        """Revalidate exact materialized bytes and inventory under the caller's lease."""
        _validate_materialized_package(self.package_root, self.inspection)

    def require_manifest(self, path: Path) -> Path:
        """Require the manifest selected when this binding was created."""
        resolved, _ = self.require_workspace_file(path)
        if resolved != self.manifest_path:
            raise ArtifactError(
                "The requested manifest does not match the acquired package binding."
            )
        return resolved

    def require_workspace_file(self, path: Path) -> tuple[Path, str]:
        """Verify one package workspace file before an engine parser opens it."""
        return _require_materialized_workspace_file(
            self.package_root,
            self.workspace,
            self.inspection.metadata,
            path,
        )

    def require_authored_file(self, reference: str, *, label: str) -> Path:
        """Resolve one package-authored relative file reference."""
        relative = self.authored_path(reference, label=label)
        path, _ = self.require_workspace_file(self.workspace / relative)
        return path

    def authored_path(self, reference: str, *, label: str) -> str:
        """Validate package-authored path syntax without opening a target."""
        return _authored_package_path(reference, label=label)

    def require_authored_path(
        self,
        reference: str,
        *,
        label: str,
    ) -> Path:
        """Resolve one package-authored regular file or declared directory."""
        relative = self.authored_path(reference, label=label)
        package_relative = workspace_payload_path(
            self.inspection.metadata.workspace_root,
            relative,
        )
        expected_directories = path_inventory(
            [PACKAGE_NAME, *(entry.path for entry in self.inspection.metadata.files)],
            limit=MAX_NODES,
        )
        if package_relative in expected_directories:
            self.validate()
            return checked_path(
                self.package_root,
                package_relative,
                directory=True,
            )
        path, _ = self.require_workspace_file(self.workspace / relative)
        return path

    def bind_template(self, reference: str) -> BoundPackageTemplate:
        """Return the authored source and its verified ARM JSON artifact."""
        relative = self.authored_path(
            reference,
            label="Deployment template",
        )
        return self._bind_template_relative(relative)

    def bind_template_path(self, path: Path) -> BoundPackageTemplate:
        """Bind an already-resolved authored template from a prepared plan."""
        _, relative = self.require_workspace_file(path)
        return self._bind_template_relative(relative)

    def _bind_template_relative(
        self,
        relative: str,
    ) -> BoundPackageTemplate:
        mapping = next(
            (
                entry
                for entry in self.inspection.metadata.templates
                if entry.source_path == relative
            ),
            None,
        )
        if mapping is None:
            raise ArtifactError(
                "The acquired deployment template has no compiled artifact mapping."
            )
        source, _ = self.require_workspace_file(self.workspace / mapping.source_path)
        artifact, _ = self.require_workspace_file(
            self.workspace / mapping.artifact_path
        )
        return BoundPackageTemplate(
            source_path=source,
            artifact_path=artifact,
            source_relative_path=mapping.source_path,
            artifact_relative_path=mapping.artifact_path,
            mapping=mapping,
        )


def _authored_package_path(reference: str, *, label: str) -> str:
    if not isinstance(reference, str):
        raise ArtifactError(f"{label} must be a package-relative path.")
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactError(
            f"{label} must be a package-relative path without '..' segments."
        )
    try:
        return relative_artifact_path(reference)
    except ArtifactError:
        raise ArtifactError(f"{label} must be a portable package-relative path.") from None


def _require_materialized_workspace_file(
    package_root: Path,
    workspace: Path,
    metadata: WorkspacePackage,
    path: Path,
) -> tuple[Path, str]:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(workspace).as_posix()
    except ValueError:
        raise ArtifactError(
            "An acquired workspace input resolves outside the package workspace."
        ) from None
    relative = relative_artifact_path(relative)
    package_relative = workspace_payload_path(metadata.workspace_root, relative)
    identity = next(
        (entry for entry in metadata.files if entry.path == package_relative),
        None,
    )
    if identity is None:
        raise ArtifactError(
            "An acquired workspace input is not present in the package inventory."
        )
    resolved = checked_path(package_root, package_relative)
    size, digest = hash_file(resolved, limit=identity.size)
    if size != identity.size or digest != identity.sha256:
        raise ArtifactError(
            "An acquired workspace input does not match its package identity."
        )
    return resolved, relative


def check_compatibility(
    metadata: WorkspacePackage,
    *,
    engine_version: str = __version__,
    features: frozenset[str] = SUPPORTED_FEATURES,
) -> None:
    try:
        compatible = SpecifierSet(metadata.siteops_range).contains(
            Version(engine_version), prereleases=True,
        )
    except (InvalidVersion, InvalidSpecifier):
        raise ArtifactError("The Site Ops compatibility declaration is invalid.") from None
    if not compatible:
        raise ArtifactError("This package requires a different Site Ops version.")
    if set(metadata.required_features) - features:
        raise ArtifactError("This package requires unsupported Site Ops features.")


def _load_metadata(raw: bytes) -> WorkspacePackage:
    if len(raw) > MAX_METADATA_BYTES:
        raise ArtifactError("Workspace package metadata exceeds its byte limit.")
    return WorkspacePackage.from_document(
        _strict_json(raw, label="Workspace package metadata")
    )


def _preflight_directory(stream: BinaryIO, size: int) -> None:
    """Bound the ZIP32 directory before ZipFile allocates entry objects."""
    if size < 22:
        raise ArtifactError("The package ZIP is truncated.")
    stream.seek(size - 22)
    signature, disk, start_disk, disk_entries, entries, central_size, offset, comment = struct.unpack(
        "<4s4H2IH", stream.read(22),
    )
    if (
        signature != b"PK\x05\x06" or disk or start_disk or disk_entries != entries or comment
        or entries == 0 or entries > MAX_FILES + 1
        or central_size > MAX_CENTRAL_BYTES or offset + central_size != size - 22
    ):
        raise ArtifactError("The package requires a bounded, single-volume ZIP32 directory.")
    stream.seek(offset)
    directory = stream.read(central_size)
    position = 0
    count = 0
    while position < len(directory):
        if len(directory) - position < 46 or directory[position:position + 4] != b"PK\x01\x02":
            raise ArtifactError("The package ZIP directory is malformed.")
        version = struct.unpack_from("<H", directory, position + 6)[0]
        name_size, extra_size, comment_size = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name_size + extra_size + comment_size
        count += 1
        if version > 20 or not 0 < name_size <= 2048 or count > MAX_FILES + 1:
            raise ArtifactError("The package ZIP directory exceeds its supported limits.")
    if position != central_size or count != entries:
        raise ArtifactError("The package ZIP directory count or size is inconsistent.")
    stream.seek(0)


def _preflight_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries or entries[0].filename != PACKAGE_NAME or entries[0].header_offset != 0:
        raise ArtifactError("The package ZIP must start with its metadata file.")
    total = 0
    for entry in entries:
        relative_artifact_path(entry.orig_filename)
        mode = entry.external_attr >> 16
        if (
            entry.filename != entry.orig_filename or entry.is_dir()
            or entry.volume != 0
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or entry.external_attr & (0x10 | 0x400) or entry.flag_bits & ~0x800
            or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or entry.extra or entry.comment
        ):
            raise ArtifactError("Package ZIP members must be regular files with supported encoding.")
        maximum = MAX_METADATA_BYTES if entry.filename == PACKAGE_NAME else MAX_FILE_BYTES
        if (
            entry.file_size > maximum
            or entry.file_size > MAX_COMPRESSION_RATIO * entry.compress_size
        ):
            raise ArtifactError("A package ZIP member exceeds its size or compression limit.")
        total += entry.file_size
        if total > MAX_TOTAL_BYTES + MAX_METADATA_BYTES:
            raise ArtifactError("The package ZIP exceeds its total expansion limit.")
    path_inventory([entry.filename for entry in entries], limit=MAX_NODES)
    return {entry.filename: entry for entry in entries}


def _read_metadata_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    """Bound decompression even when a ZIP header understates the decoded size."""
    data = bytearray()
    with archive.open(entry) as source:
        while chunk := source.read(min(1024 * 1024, MAX_METADATA_BYTES - len(data) + 1)):
            data.extend(chunk)
            if len(data) > MAX_METADATA_BYTES:
                raise ArtifactError("Workspace package metadata exceeds its byte limit.")
    return bytes(data)


def _read_template_artifact(
    archive: zipfile.ZipFile,
    entry: PayloadFile,
) -> bytes:
    data = bytearray()
    digest = hashlib.sha256()
    with archive.open(entry.path) as source:
        while chunk := source.read(min(1024 * 1024, entry.size - len(data) + 1)):
            data.extend(chunk)
            digest.update(chunk)
            if len(data) > entry.size:
                raise ArtifactError("A template artifact exceeds its declared byte count.")
    if len(data) != entry.size or digest.hexdigest() != entry.sha256:
        raise ArtifactError("A template artifact does not match its declared identity.")
    return bytes(data)


def _validate_template_artifacts(
    archive: zipfile.ZipFile,
    metadata: WorkspacePackage,
) -> None:
    inventory = {entry.path: entry for entry in metadata.files}
    for mapping in metadata.templates:
        entry = inventory[
            workspace_payload_path(
                metadata.workspace_root,
                mapping.artifact_path,
            )
        ]
        try:
            document = _strict_json(
                _read_template_artifact(archive, entry),
                label="A mapped template artifact",
            )
            validate_arm_template(document)
            extract_template_parameters(document)
        except (ArtifactError, TemplateOutputError):
            raise ArtifactError(
                "A mapped template artifact is not valid ARM deployment JSON."
            ) from None
        if mapping.source_kind is TemplateKind.BICEP:
            emitted_hashes = extract_compiler_template_hashes(document)
            if emitted_hashes != mapping.dependencies.template_hashes:
                raise ArtifactError(
                    "A mapped template dependency identity does not match its ARM artifact."
                )
            emitted_version = extract_compiler_version(document)
            if (
                emitted_version is not None
                and (
                    mapping.compiler is None
                    or emitted_version != mapping.compiler.version
                )
            ):
                raise ArtifactError(
                    "A mapped template compiler identity does not match its ARM artifact."
                )
        elif (
            arm_json_dependency_identity(document).coverage
            is not mapping.dependencies.coverage
        ):
            raise ArtifactError(
                "A native ARM dependency identity does not match its artifact."
            )


@contextmanager
def _open_package(path: Path, expected_sha256: str) -> Iterator[
    tuple[zipfile.ZipFile, PackageInspection, bytes]
]:
    expected_sha256 = _digest(expected_sha256)
    try:
        with open_regular_file(path) as stream:
            size, actual = hash_stream(stream, limit=MAX_ARCHIVE_BYTES)
            if actual != expected_sha256:
                raise ArtifactError("The package SHA-256 does not match the expected artifact.")
            _preflight_directory(stream, size)
            with zipfile.ZipFile(stream) as archive:
                entries = _preflight_members(archive)
                raw = _read_metadata_member(archive, entries[PACKAGE_NAME])
                metadata = _load_metadata(raw)
                if set(entries) != {PACKAGE_NAME, *(entry.path for entry in metadata.files)}:
                    raise ArtifactError("The package ZIP does not match its declared file inventory.")
                for entry in metadata.files:
                    if entries[entry.path].file_size != entry.size:
                        raise ArtifactError("A package ZIP file size differs from its declared size.")
                check_compatibility(metadata)
                _validate_template_artifacts(archive, metadata)
                yield archive, PackageInspection(
                    metadata=metadata,
                    sha256=actual,
                    size=size,
                    metadata_sha256=hashlib.sha256(raw).hexdigest(),
                    metadata_size=len(raw),
                ), raw
    except (EOFError, struct.error, zipfile.BadZipFile, zlib.error):
        raise ArtifactError("The package archive could not be read safely.") from None


def _copy_member(archive: zipfile.ZipFile, entry: PayloadFile, destination: BinaryIO | None) -> None:
    digest = hashlib.sha256()
    size = 0
    with archive.open(entry.path) as source:
        while chunk := source.read(min(1024 * 1024, entry.size - size + 1)):
            size += len(chunk)
            if size > entry.size:
                raise ArtifactError("A package file exceeds its declared byte count.")
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
    if size != entry.size or digest.hexdigest() != entry.sha256:
        raise ArtifactError("A package file does not match its declared SHA-256 and size.")


def inspect_package(path: Path, expected_sha256: str) -> PackageInspection:
    """Check exact archive bytes, every payload file and current-engine compatibility."""
    try:
        with _open_package(path, expected_sha256) as (archive, inspection, _):
            for entry in inspection.metadata.files:
                _copy_member(archive, entry, None)
    except OSError:
        raise ArtifactError("The package archive could not be read safely.") from None
    return inspection


def extract_package(path: Path, expected_sha256: str, destination: Path) -> PackageInspection:
    """Materialize into a new staging directory, never an existing cache or project.

    The caller owns provenance verification and atomic cache publication.
    Failure removes only files and directories created by this operation.
    Windows access controls are inherited from the caller's staging parent.
    """
    created_files: list[Path] = []
    created_directories: list[Path] = []
    complete = False
    destination = Path(os.path.abspath(destination))
    try:
        with _open_package(path, expected_sha256) as (archive, inspection, raw):
            require_node(destination.parent, directory=True)
            destination.mkdir(mode=0o700)
            created_directories.append(destination)
            root = destination.resolve()
            directories: set[Path] = {root}
            entries = (
                PayloadFile(PACKAGE_NAME, hashlib.sha256(raw).hexdigest(), len(raw)),
                *inspection.metadata.files,
            )
            for entry in entries:
                parts = entry.path.split("/")
                parent = root
                for component in parts[:-1]:
                    parent /= component
                    if parent not in directories:
                        parent.mkdir(mode=0o700)
                        created_directories.append(parent)
                        directories.add(parent)
                    require_node(parent, directory=True)
                    if parent.resolve(strict=True) != parent:
                        raise ArtifactError("Package extraction encountered a filesystem alias.")
                target = parent / parts[-1]
                descriptor = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                created_files.append(target)
                with os.fdopen(descriptor, "wb") as output:
                    _copy_member(archive, entry, output)
                    output.flush()
                    os.fsync(output.fileno())
                if target.resolve(strict=True) != target:
                    raise ArtifactError("Package extraction encountered a filesystem alias.")
        complete = True
        return inspection
    except OSError:
        raise ArtifactError("Package extraction requires a new writable staging directory.") from None
    finally:
        if not complete:
            cleanup_failed = False
            for target in reversed(created_files):
                try:
                    target.unlink()
                except OSError:
                    cleanup_failed = True
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    cleanup_failed = True
            if cleanup_failed:
                logger.warning("Incomplete package staging cleanup could not be completed.")

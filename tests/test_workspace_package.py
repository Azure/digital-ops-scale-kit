"""Workspace package identities, compatibility and confined staging."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tracemalloc
import zipfile
import zlib
from pathlib import Path

import pytest

from siteops import package_builder
from siteops import workspace_package as package
from siteops.artifacts import (
    ArtifactError,
    PayloadFile,
    hash_stream,
    open_regular_file,
    relative_artifact_path,
)
from siteops.compilation import (
    CompiledTemplate,
    DependencyCoverage,
    TemplateCompilationSession,
    TemplateKind,
)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    shutil.copytree(Path(__file__).parent / "fixtures" / "browse-workspace", root / "workspace")
    (root / "LICENSE").write_text("Fixture license\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("Companion guide\n", encoding="utf-8")
    return root


def _build(snapshot, output, **overrides):
    return package_builder.build_package(snapshot, output, **{
        "workspace": "workspace", "kit_id": "example/storage", "version": "preview-7",
        "source_revision": "feed:immutable-revision-7",
        "siteops_range": ">=1.0.0b1,<2", "companions": ("LICENSE", "docs"),
        **overrides,
    })


def _document(files=None):
    files = files or {"workspace/file.txt": b"payload"}
    records = tuple(PayloadFile(name, hashlib.sha256(data).hexdigest(), len(data)) for name, data in files.items())
    return package.WorkspacePackage(
        "example", "7", "opaque-revision", "workspace", ">=1.0.0b1,<2",
        ("manifest/v1", package.COMPILED_TEMPLATE_FEATURE), records,
        package.workspace_tree_digest(records, "workspace"), (),
    ).document()


def _native_document(content):
    path = "workspace/templates/main.template.json"
    record = PayloadFile(path, hashlib.sha256(content).hexdigest(), len(content))
    mapping = package.PackageTemplateMapping(
        source_path="templates/main.template.json",
        source_kind=TemplateKind.ARM_JSON,
        source_sha256=record.sha256,
        source_size=record.size,
        artifact_path="templates/main.template.json",
        artifact_sha256=record.sha256,
        artifact_size=record.size,
        producer_mode="native-arm-json",
        invocation=("read-arm-json",),
        driver=None,
        compiler=None,
        configuration=None,
        dependencies=package.PackageDependencyIdentity(
            DependencyCoverage.NOT_APPLICABLE
        ),
    )
    return package.WorkspacePackage(
        "example", "7", "opaque-revision", "workspace", ">=1.0.0b1,<2",
        ("manifest/v1", package.COMPILED_TEMPLATE_FEATURE), (record,),
        package.workspace_tree_digest((record,), "workspace"), (mapping,),
    ).document()


def _bicep_document():
    source_content = b"param name string\n"
    artifact_content = json.dumps({
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "metadata": {
            "_generator": {
                "name": "bicep",
                "version": "0.45.15",
                "templateHash": "root-hash",
            }
        },
        "resources": [],
    }).encode()
    source = PayloadFile(
        "workspace/templates/main.bicep",
        hashlib.sha256(source_content).hexdigest(),
        len(source_content),
    )
    artifact = PayloadFile(
        "workspace/.siteops/compiled/v1/templates/main.bicep.json",
        hashlib.sha256(artifact_content).hexdigest(),
        len(artifact_content),
    )
    mapping = package.PackageTemplateMapping(
        source_path="templates/main.bicep",
        source_kind=TemplateKind.BICEP,
        source_sha256=source.sha256,
        source_size=source.size,
        artifact_path=".siteops/compiled/v1/templates/main.bicep.json",
        artifact_sha256=artifact.sha256,
        artifact_size=artifact.size,
        producer_mode="azure-cli-bicep",
        invocation=("az", "bicep", "build", "--no-restore"),
        driver=package.PackageToolIdentity("azure-cli", "2.87.0"),
        compiler=package.PackageToolIdentity("azure-cli-bicep", "0.45.15"),
        configuration=package.PackageConfigurationIdentity(
            package.ConfigurationDiscovery.PRODUCER_DEFAULT,
            package.PRODUCER_DEFAULT_BICEP_CONFIGURATION_SHA256,
        ),
        dependencies=package.PackageDependencyIdentity(
            DependencyCoverage.COMPILED_OUTPUT_ONLY
        ),
    )
    records = (source, artifact)
    return package.WorkspacePackage(
        "example", "7", "opaque-revision", "workspace", ">=1.0.0b1,<2",
        ("manifest/v1", package.COMPILED_TEMPLATE_FEATURE), records,
        package.workspace_tree_digest(records, "workspace"), (mapping,),
    ).document(), {
        source.path: source_content,
        artifact.path: artifact_content,
    }


def _archive(path, document=None, files=None, *, info_mutator=None, raw_metadata=None):
    files = files or {"workspace/file.txt": b"payload"}
    document = document or _document(files)
    with zipfile.ZipFile(path, "w") as archive:
        info = package_builder._zip_info(package.PACKAGE_NAME, zipfile.ZIP_STORED)
        archive.writestr(info, raw_metadata if raw_metadata is not None else package.json_bytes(document))
        for name, data in files.items():
            info = package_builder._zip_info(name, zipfile.ZIP_STORED)
            if info_mutator:
                info_mutator(info)
            archive.writestr(info, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_package_preserves_workspace_and_companion_paths(snapshot, tmp_path):
    output = tmp_path / "package.zip"
    built = _build(snapshot, output)
    inspected = package.inspect_package(output, built.sha256)
    assert built == inspected
    assert built.metadata.source_revision == "feed:immutable-revision-7"
    assert built.metadata.workspace_root == "workspace"
    assert built.metadata.required_features == (
        package.COMPILED_TEMPLATE_FEATURE,
        "manifest/v1",
    )
    assert len(built.metadata.templates) == 1
    mapping = built.metadata.templates[0]
    assert mapping.source_kind is TemplateKind.ARM_JSON
    assert mapping.source_path == mapping.artifact_path == "templates/storage.template.json"
    assert not any("github" in key.casefold() for key in built.metadata.document())
    expected = {
        path.relative_to(snapshot).as_posix(): path.read_bytes()
        for path in snapshot.rglob("*") if path.is_file()
    }
    assert {entry.path for entry in built.metadata.files} == set(expected)
    destination = tmp_path / "materialized"
    assert package.extract_package(output, built.sha256, destination) == built
    for relative, data in expected.items():
        target = destination / relative
        assert target.read_bytes() == data
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert (destination / package.PACKAGE_NAME).is_file()


def test_production_is_deterministic_across_source_timestamps(snapshot, tmp_path):
    first = _build(snapshot, tmp_path / "first.zip")
    for path in snapshot.rglob("*"):
        if path.is_file():
            os.utime(path, (1000000000, 1000000000))
    second = _build(snapshot, tmp_path / "second.zip")
    assert first == second
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert archive.namelist()[0] == package.PACKAGE_NAME


class PackageCompiler:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[1:] == ("version", "--output", "json"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"azure-cli": "2.87.0"}),
                stderr="",
            )
        if argv[1:] == ("bicep", "version"):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="Bicep CLI version 0.45.15 (6a4a640fd8)",
                stderr="",
            )
        output_path = Path(argv[argv.index("--outfile") + 1])
        output_path.write_text(
            json.dumps({
                "$schema": (
                    "https://schema.management.azure.com/schemas/2019-04-01/"
                    "deploymentTemplate.json#"
                ),
                "contentVersion": "1.0.0.0",
                "metadata": {
                    "_generator": {
                        "name": "bicep",
                        "version": "0.45.15.0",
                        "templateHash": "root-hash",
                    }
                },
                "parameters": {"name": {"type": "string"}},
                "resources": [],
            }),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _controlled_factory(control_root, snapshot, tmp_path, compiler):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    azure_cli = tools / "az.cmd"
    bicep = tools / "bicep.exe"
    azure_cli.write_bytes(b"fixture")
    bicep.write_bytes(b"fixture")
    return lambda: package_builder.create_producer_compilation_session(
        snapshot,
        control_root,
        azure_cli_path=azure_cli,
        bicep_path=bicep,
        command_runner=compiler,
    )


def test_bicep_package_maps_only_manifest_template_roots(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "compiled-package-workspace"
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    shutil.copytree(fixture, snapshot / "workspace")
    compiler = PackageCompiler()

    built = package_builder.build_package(
        snapshot,
        tmp_path / "package.zip",
        workspace="workspace",
        kit_id="example/bicep",
        version="preview-1",
        source_revision="fixture",
        siteops_range=">=1.0.0b1,<2",
        compilation_session_factory=_controlled_factory(
            control_root,
            snapshot,
            tmp_path,
            compiler,
        ),
    )

    assert len(built.metadata.templates) == 1
    mapping = built.metadata.templates[0]
    assert mapping.source_path == "templates/main.bicep"
    assert mapping.artifact_path == (
        ".siteops/compiled/v1/templates/main.bicep.json"
    )
    assert mapping.configuration is not None
    assert (
        mapping.configuration.discovery.value
        == "producer-default"
    )
    assert mapping.dependencies.coverage is DependencyCoverage.COMPILED_OUTPUT_ONLY
    assert mapping.dependencies.template_hashes == ()
    assert all("unused-module.bicep" not in " ".join(call) for call in compiler.calls)
    build = next(call for call in compiler.calls if call[1:3] == ("bicep", "build"))
    assert build[3] == "--no-restore"
    assert str(control_root) not in json.dumps(built.metadata.document())

    destination = tmp_path / "materialized"
    package.extract_package(tmp_path / "package.zip", built.sha256, destination)
    artifact = destination / "workspace" / Path(*mapping.artifact_path.split("/"))
    acquired = TemplateCompilationSession().acquire(artifact)
    assert isinstance(acquired, CompiledTemplate)
    assert acquired.parameters[0].name == "name"


def test_bicep_package_is_deterministic_across_control_roots(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "compiled-package-workspace"
    results = []
    for name in ("first", "second"):
        control_root = tmp_path / name / "control"
        control_root.mkdir(parents=True)
        snapshot = control_root / "source"
        shutil.copytree(fixture, snapshot / "workspace")
        compiler = PackageCompiler()
        output = tmp_path / f"{name}.zip"
        result = package_builder.build_package(
            snapshot,
            output,
            workspace="workspace",
            kit_id="example/bicep",
            version="preview-1",
            source_revision="fixture",
            siteops_range=">=1.0.0b1,<2",
            compilation_session_factory=_controlled_factory(
                control_root,
                snapshot,
                tmp_path / name,
                compiler,
            ),
        )
        results.append((result, output.read_bytes()))

    assert results[0] == results[1]


def test_workspace_bicep_configuration_is_recorded(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "compiled-package-workspace"
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    shutil.copytree(fixture, snapshot / "workspace")
    configuration = snapshot / "workspace" / "bicepconfig.json"
    configuration.write_text('{"analyzers": {}}\n', encoding="utf-8")
    compiler = PackageCompiler()

    built = package_builder.build_package(
        snapshot,
        tmp_path / "package.zip",
        workspace="workspace",
        kit_id="example/bicep",
        version="preview-1",
        source_revision="fixture",
        siteops_range=">=1.0.0b1,<2",
        compilation_session_factory=_controlled_factory(
            control_root,
            snapshot,
            tmp_path,
            compiler,
        ),
    )

    identity = built.metadata.templates[0].configuration
    assert identity is not None
    assert identity.discovery.value == "nearest-found"
    assert identity.path == "bicepconfig.json"
    assert identity.sha256 == hashlib.sha256(configuration.read_bytes()).hexdigest()


def test_producer_session_isolates_configuration_cache_and_temp(tmp_path):
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    snapshot.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    azure_cli = tools / "az.cmd"
    bicep = tools / "bicep.exe"
    azure_cli.write_bytes(b"fixture")
    bicep.write_bytes(b"fixture")

    session = package_builder.create_producer_compilation_session(
        snapshot,
        control_root,
        azure_cli_path=azure_cli,
        bicep_path=bicep,
        command_runner=PackageCompiler(),
    )

    environment = dict(session.bicep_options.environment or ())
    assert session.bicep_options.is_controlled_producer
    assert environment["AZURE_BICEP_CHECK_VERSION"] == "false"
    assert environment["AZURE_BICEP_USE_BINARY_FROM_PATH"] == "true"
    assert environment["AZURE_CORE_COLLECT_TELEMETRY"] == "false"
    assert Path(environment["AZURE_CONFIG_DIR"]).parent == control_root
    assert Path(environment["HOME"]).parent == control_root
    assert Path(environment["TEMP"]).parent == control_root
    assert Path(environment["PATH"].split(os.pathsep)[0]).parent == control_root


@pytest.mark.parametrize("tool", ["azure-cli", "bicep"])
def test_producer_session_rejects_relative_explicit_tool_paths(tmp_path, tool):
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    snapshot.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    azure_cli = tools / "az.cmd"
    bicep = tools / "bicep.exe"
    azure_cli.write_bytes(b"fixture")
    bicep.write_bytes(b"fixture")
    kwargs = {
        "azure_cli_path": azure_cli,
        "bicep_path": bicep,
    }
    kwargs["azure_cli_path" if tool == "azure-cli" else "bicep_path"] = Path(
        "relative-tool"
    )

    with pytest.raises(ArtifactError, match="absolute"):
        package_builder.create_producer_compilation_session(
            snapshot,
            control_root,
            **kwargs,
        )


def test_real_aio_entries_produce_mapped_template_roots(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    control_root = tmp_path / "control"
    control_root.mkdir()
    snapshot = control_root / "source"
    shutil.copytree(
        repository / "workspaces" / "iot-operations",
        snapshot / "workspace",
    )
    compiler = PackageCompiler()

    built = package_builder.build_package(
        snapshot,
        tmp_path / "aio.zip",
        workspace="workspace",
        kit_id="example/aio",
        version="preview-1",
        source_revision="fixture",
        siteops_range=">=1.0.0b1,<2",
        compilation_session_factory=_controlled_factory(
            control_root,
            snapshot,
            tmp_path,
            compiler,
        ),
    )

    sources = {mapping.source_path for mapping in built.metadata.templates}
    assert {
        "templates/aio/enablement.bicep",
        "templates/aio/instance.bicep",
        "templates/deps/adr-ns.bicep",
        "templates/deps/schema-registry-role.bicep",
        "templates/deps/schema-registry.bicep",
        "templates/edge-site/main.bicep",
        "templates/edge-site/subscription.bicep",
    } <= sources
    assert "templates/aio/modules/instance-2025-10-01.bicep" not in sources
    instance = next(
        mapping
        for mapping in built.metadata.templates
        if mapping.source_path == "templates/aio/instance.bicep"
    )
    with zipfile.ZipFile(tmp_path / "aio.zip") as archive:
        artifact = archive.read(
            package.workspace_payload_path(
                built.metadata.workspace_root,
                instance.artifact_path,
            )
        )
    acquired = TemplateCompilationSession().acquire(
        _write_file(tmp_path / "instance.json", artifact)
    )
    assert isinstance(acquired, CompiledTemplate)
    assert acquired.parameters[0].name == "name"


def _write_file(path, content):
    path.write_bytes(content)
    return path


def test_tree_identity_uses_raw_workspace_bytes_not_companion_bytes(snapshot, tmp_path):
    manifest = snapshot / "workspace" / "manifests" / "storage" / "manifest.yaml"
    lf = manifest.read_bytes().replace(b"\r\n", b"\n")
    manifest.write_bytes(lf)
    before = _build(snapshot, tmp_path / "first.zip")
    (snapshot / "docs" / "guide.md").write_text("Changed guide\n", encoding="utf-8")
    companion_change = _build(snapshot, tmp_path / "second.zip")
    assert before.sha256 != companion_change.sha256
    assert before.metadata.tree_sha256 == companion_change.metadata.tree_sha256
    manifest.write_bytes(lf.replace(b"\n", b"\r\n"))
    workspace_change = _build(snapshot, tmp_path / "third.zip")
    assert workspace_change.metadata.tree_sha256 != companion_change.metadata.tree_sha256


def test_root_workspace_is_supported(snapshot, tmp_path):
    built = _build(snapshot / "workspace", tmp_path / "package.zip", workspace=".", companions=())
    assert built.metadata.workspace_root == "."
    assert "manifests/storage/manifest.yaml" in {entry.path for entry in built.metadata.files}


def test_highly_compressible_source_is_stored_without_weakening_consumer_limits(snapshot, tmp_path):
    (snapshot / "workspace" / "zeroes.bin").write_bytes(b"\0" * 128000)
    built = _build(snapshot, tmp_path / "package.zip")
    with zipfile.ZipFile(tmp_path / "package.zip") as archive:
        assert archive.getinfo("workspace/zeroes.bin").compress_type == zipfile.ZIP_STORED
    assert package.inspect_package(tmp_path / "package.zip", built.sha256) == built


def test_digest_is_checked_before_zip_parsing_or_destination_creation(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    _archive(path)
    monkeypatch.setattr(package.zipfile, "ZipFile", lambda *a, **k: pytest.fail("Parsed before digest"))
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, "0" * 64, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("mutate", [
    lambda document: document.update(apiVersion="unknown"),
    lambda document: document.update(hooks={"local": "PRIVATE_SENTINEL"}),
    lambda document: document.pop("templates"),
    lambda document: document["workspace"].update(root="../escape"),
    lambda document: document["workspace"]["tree"].update(algorithm="sha1"),
    lambda document: document["workspace"]["tree"].update(digest="0" * 64),
    lambda document: document["files"][0].update(size=True),
    lambda document: document["files"][0].update(sha256="not-a-digest"),
    lambda document: document["compatibility"].update(siteops=">=1.0"),
    lambda document: document["compatibility"].update(siteops=">=2,<3"),
    lambda document: document["compatibility"].update(requiredFeatures=["unknown/required"]),
    lambda document: document["compatibility"].update(requiredFeatures=["manifest/v1", "manifest/v1"]),
])
def test_invalid_metadata_and_compatibility_fail_before_materialization(tmp_path, mutate):
    document = _document()
    mutate(document)
    path = tmp_path / "package.zip"
    digest = _archive(path, document)
    with pytest.raises(ArtifactError) as failed:
        package.extract_package(path, digest, tmp_path / "staging")
    assert "PRIVATE_SENTINEL" not in str(failed.value)
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("raw", [
    b'{"kind":"WorkspacePackage","kind":"PRIVATE_SENTINEL"}',
    b'{"private":NaN}',
    b"\xff",
    b"[" * 3000,
])
def test_invalid_json_is_bounded_and_value_safe(tmp_path, raw):
    path = tmp_path / "package.zip"
    digest = _archive(path, raw_metadata=raw)
    with pytest.raises(ArtifactError) as failed:
        package.inspect_package(path, digest)
    assert "PRIVATE_SENTINEL" not in str(failed.value)


def test_malformed_mapped_arm_json_fails_before_materialization(tmp_path):
    files = {"workspace/templates/main.template.json": b"{}"}
    path = tmp_path / "package.zip"
    digest = _archive(path, _native_document(files[next(iter(files))]), files)

    with pytest.raises(ArtifactError, match="valid ARM"):
        package.extract_package(path, digest, tmp_path / "staging")

    assert not (tmp_path / "staging").exists()


def test_invalid_mapped_arm_parameter_schema_fails_before_materialization(tmp_path):
    content = json.dumps({
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": {"name": {}},
        "resources": [],
    }).encode()
    files = {"workspace/templates/main.template.json": content}
    path = tmp_path / "package.zip"
    digest = _archive(path, _native_document(content), files)

    with pytest.raises(ArtifactError, match="valid ARM"):
        package.inspect_package(path, digest)


def test_template_mapping_identity_must_match_file_inventory():
    content = json.dumps({
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "resources": [],
    }).encode()
    document = _native_document(content)
    document["templates"]["entries"][0]["source"]["sha256"] = "0" * 64
    document["templates"]["entries"][0]["artifact"]["sha256"] = "0" * 64

    with pytest.raises(ArtifactError, match="source identity"):
        package.WorkspacePackage.from_document(document)


def test_generated_namespace_requires_one_mapping_per_artifact():
    content = json.dumps({
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "resources": [],
    }).encode()
    files = {
        "workspace/file.txt": b"payload",
        "workspace/.siteops/compiled/v1/orphan.json": content,
    }
    records = tuple(
        PayloadFile(path, hashlib.sha256(value).hexdigest(), len(value))
        for path, value in files.items()
    )
    document = package.WorkspacePackage(
        "example", "7", "opaque-revision", "workspace", ">=1.0.0b1,<2",
        ("manifest/v1", package.COMPILED_TEMPLATE_FEATURE), records,
        package.workspace_tree_digest(records, "workspace"), (),
    ).document()

    with pytest.raises(ArtifactError, match="namespace"):
        package.WorkspacePackage.from_document(document)


def test_bicep_mapping_rejects_complete_dependency_claim():
    document, _ = _bicep_document()
    document["templates"]["entries"][0]["producer"]["dependencies"][
        "coverage"
    ] = "complete"

    with pytest.raises(ArtifactError, match="inconsistent"):
        package.WorkspacePackage.from_document(document)


def test_bicep_mapping_configuration_must_be_effective():
    document, _ = _bicep_document()
    source = document["templates"]["entries"][0]["source"]
    document["templates"]["entries"][0]["producer"]["configuration"] = {
        "discovery": "nearest-found",
        "path": source["path"],
        "sha256": source["sha256"],
    }

    with pytest.raises(ArtifactError, match="effective"):
        package.WorkspacePackage.from_document(document)


def test_producer_default_configuration_digest_is_fixed():
    document, _ = _bicep_document()
    document["templates"]["entries"][0]["producer"]["configuration"][
        "sha256"
    ] = "0" * 64

    with pytest.raises(ArtifactError, match="producer-default"):
        package.WorkspacePackage.from_document(document)


def test_template_mapping_paths_must_be_unique():
    document, _ = _bicep_document()
    document["templates"]["entries"].append(
        copy.deepcopy(document["templates"]["entries"][0])
    )

    with pytest.raises(ArtifactError, match="must be unique"):
        package.WorkspacePackage.from_document(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: document["templates"]["entries"][0]["producer"][
                "dependencies"
            ].update(templateHashes=["invented"]),
            "dependency identity",
        ),
        (
            lambda document: document["templates"]["entries"][0]["producer"][
                "compiler"
            ].update(version="0.46.0"),
            "compiler identity",
        ),
    ],
)
def test_compiler_derived_mapping_identity_must_match_artifact(
    tmp_path,
    mutation,
    message,
):
    document, files = _bicep_document()
    mutation(document)
    path = tmp_path / "package.zip"
    digest = _archive(path, document, files)

    with pytest.raises(ArtifactError, match=message):
        package.inspect_package(path, digest)


@pytest.mark.parametrize(
    "raw",
    [
        (
            b'{"$schema":"https://schema.management.azure.com/schemas/'
            b'2019-04-01/deploymentTemplate.json#","contentVersion":"1",'
            b'"resources":[],"resources":[]}'
        ),
        (
            b'{"$schema":"https://schema.management.azure.com/schemas/'
            b'2019-04-01/deploymentTemplate.json#","contentVersion":"1",'
            b'"resources":[],"value":NaN}'
        ),
    ],
)
def test_mapped_arm_json_uses_strict_json_parsing(tmp_path, raw):
    files = {"workspace/templates/main.template.json": raw}
    path = tmp_path / "package.zip"
    digest = _archive(path, _native_document(raw), files)

    with pytest.raises(ArtifactError, match="valid ARM"):
        package.inspect_package(path, digest)


def test_native_arm_dependency_coverage_must_match_artifact(tmp_path):
    content = json.dumps({
        "$schema": (
            "https://schema.management.azure.com/schemas/2019-04-01/"
            "deploymentTemplate.json#"
        ),
        "contentVersion": "1.0.0.0",
        "resources": [{
            "type": "Microsoft.Resources/deployments",
            "apiVersion": "2022-09-01",
            "name": "linked",
            "properties": {"templateLink": {"uri": "https://example.invalid/template.json"}},
        }],
    }).encode()
    document = _native_document(content)
    path = tmp_path / "package.zip"
    files = {"workspace/templates/main.template.json": content}
    digest = _archive(path, document, files)

    with pytest.raises(ArtifactError, match="dependency identity"):
        package.inspect_package(path, digest)


@pytest.mark.parametrize("path", [
    "../outside", "/absolute", "C:/drive", "C:drive", "a\\b", "a//b", "a/./b",
    "a/../b", "a.", "a ", "AUX.txt", "COM1", "COM\u00b9", "a:stream",
    "a?b", "a\x00b", "a\u202eb", "e\u0301.txt", "a/" + "b" * 256,
])
def test_package_paths_are_portable_and_confined(path):
    with pytest.raises(ArtifactError):
        relative_artifact_path(path)


@pytest.mark.parametrize("paths", [
    ("workspace/file", "workspace/FILE"),
    ("workspace/Dir/a", "workspace/dir/b"),
    ("workspace/file", "workspace/file/child"),
])
def test_case_aliases_and_file_directory_conflicts_are_rejected(tmp_path, paths):
    files = {path: b"data" for path in paths}
    path = tmp_path / "package.zip"
    digest = _archive(path, _document(files), files)
    with pytest.raises(ArtifactError, match="collision"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("mutate", [
    lambda info: setattr(info, "external_attr", (stat.S_IFLNK | 0o600) << 16),
    lambda info: setattr(info, "external_attr", (stat.S_IFIFO | 0o600) << 16),
    lambda info: setattr(info, "external_attr", info.external_attr | 0x400),
    lambda info: setattr(info, "extra", b"\x01\x00\x00\x00"),
    lambda info: setattr(info, "comment", b"comment"),
])
def test_zip_member_features_are_rejected_before_writing(tmp_path, mutate):
    path = tmp_path / "package.zip"
    digest = _archive(path, info_mutator=mutate)
    with pytest.raises(ArtifactError):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_actual_central_directory_count_is_checked_before_zip_allocation(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    _archive(path)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<HH", raw, len(raw) - 22 + 8, 1, 1)
    path.write_bytes(raw)
    monkeypatch.setattr(package.zipfile, "ZipFile", lambda *a, **k: pytest.fail("Allocated forged directory"))
    with pytest.raises(ArtifactError, match="count or size"):
        package.inspect_package(path, hashlib.sha256(raw).hexdigest())


def test_understated_metadata_size_cannot_cause_unbounded_decompression(tmp_path):
    path = tmp_path / "package.zip"
    metadata = package.json_bytes(_document())
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            package_builder._zip_info(package.PACKAGE_NAME, zipfile.ZIP_DEFLATED),
            metadata + b" " * (16 * 1024 * 1024),
        )
        archive.writestr(
            package_builder._zip_info("workspace/file.txt", zipfile.ZIP_STORED), b"payload",
        )
    raw = bytearray(path.read_bytes())
    central = struct.unpack_from("<I", raw, len(raw) - 22 + 16)[0]
    struct.pack_into("<I", raw, 22, len(metadata))
    struct.pack_into("<I", raw, central + 24, len(metadata))
    assert struct.unpack_from("<I", raw, central + 16)[0] != zlib.crc32(metadata)
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    tracemalloc.start()
    try:
        with pytest.raises(ArtifactError):
            package.extract_package(path, expected, tmp_path / "staging")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_NODES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES",
                                   "MAX_ARCHIVE_BYTES", "MAX_METADATA_BYTES", "MAX_CENTRAL_BYTES"])
def test_resource_limits_fail_before_materialization(tmp_path, monkeypatch, limit):
    path = tmp_path / "package.zip"
    digest = _archive(path)
    monkeypatch.setattr(package, limit, 0 if limit == "MAX_FILES" else 1)
    with pytest.raises(ArtifactError):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_compression_expansion_is_rejected_before_writing(tmp_path):
    files = {"workspace/zeroes": b"\0" * 128000}
    path = tmp_path / "package.zip"
    digest = _archive(
        path, _document(files), files,
        info_mutator=lambda info: setattr(info, "compress_type", zipfile.ZIP_DEFLATED),
    )
    with pytest.raises(ArtifactError, match="compression"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_changed_payload_is_removed_without_publishing_partial_content(tmp_path):
    path = tmp_path / "package.zip"
    document = _document({"workspace/file.txt": b"expected"})
    digest = _archive(path, document, {"workspace/file.txt": b"modified"})
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_existing_destination_and_output_are_preserved(snapshot, tmp_path):
    output = tmp_path / "package.zip"
    built = _build(snapshot, output)
    original = output.read_bytes()
    with pytest.raises(ArtifactError, match="already exists"):
        _build(snapshot, output)
    assert output.read_bytes() == original
    destination = tmp_path / "operator"
    destination.mkdir()
    sentinel = destination / "operator.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ArtifactError, match="new writable"):
        package.extract_package(output, built.sha256, destination)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_archive_change_during_extraction_invalidates_and_removes_staging(tmp_path, monkeypatch):
    path = tmp_path / "package.zip"
    digest = _archive(path)
    original = package._copy_member

    def changed(archive, entry, target):
        original(archive, entry, target)
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))

    monkeypatch.setattr(package, "_copy_member", changed)
    with pytest.raises(ArtifactError, match="changed"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_failed_cleanup_keeps_the_primary_integrity_error(tmp_path, monkeypatch, caplog):
    path = tmp_path / "package.zip"
    digest = _archive(path, _document({"workspace/file.txt": b"expected"}), {"workspace/file.txt": b"modified"})
    original = Path.unlink

    def failed(path, *args, **kwargs):
        if path.name == "file.txt":
            raise OSError("cleanup fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed)
    with pytest.raises(ArtifactError, match="SHA-256"):
        package.extract_package(path, digest, tmp_path / "staging")
    assert "cleanup" in caplog.text


def test_hardlinked_and_lfs_sources_fail_before_output(snapshot, tmp_path):
    original = snapshot / "workspace" / "manifests" / "storage" / "README.md"
    os.link(original, snapshot / "workspace" / "linked.md")
    with pytest.raises(ArtifactError, match="unlinked"):
        _build(snapshot, tmp_path / "package.zip")
    (snapshot / "workspace" / "linked.md").unlink()
    original.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    with pytest.raises(ArtifactError, match="Git LFS"):
        _build(snapshot, tmp_path / "package.zip")
    assert not (tmp_path / "package.zip").exists()


def test_source_changes_during_production_preserve_output_absence(snapshot, tmp_path, monkeypatch):
    original = package_builder._record

    def changed(root, relative):
        result = original(root, relative)
        if relative == "LICENSE":
            (root / relative).write_text("changed source", encoding="utf-8")
        return result

    monkeypatch.setattr(package_builder, "_record", changed)
    with pytest.raises(ArtifactError, match="changed"):
        _build(snapshot, tmp_path / "package.zip")
    assert not (tmp_path / "package.zip").exists()
    assert not list(tmp_path.glob(".siteops-package-*"))


def test_tree_digest_does_not_depend_on_file_order():
    document = _document({"workspace/z": b"z", "workspace/a": b"a"})
    reversed_document = copy.deepcopy(document)
    reversed_document["files"].reverse()
    assert package.WorkspacePackage.from_document(document) == package.WorkspacePackage.from_document(
        reversed_document
    )


def test_prerelease_engine_versions_use_pep440_comparison():
    metadata = package.WorkspacePackage.from_document(_document())
    package.check_compatibility(metadata, engine_version="1.0.0b1+build.123")
    with pytest.raises(ArtifactError, match="different"):
        package.check_compatibility(metadata, engine_version="1.0.0a9")
    with pytest.raises(ArtifactError, match="different"):
        package.check_compatibility(metadata, engine_version="2.0.0")


def test_negative_read_budget_does_not_read_a_stream():
    class Unreadable(io.BytesIO):
        def read(self, *args):
            pytest.fail("Read with invalid budget")

    with pytest.raises(ArtifactError, match="limit"):
        hash_stream(Unreadable(b"data"), limit=-1)


def test_file_context_preserves_errors_from_its_caller(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"payload")
    failure = OSError("caller write failure")
    with pytest.raises(OSError) as raised:
        with open_regular_file(path):
            raise failure
    assert raised.value is failure


def test_encrypted_zip_flags_are_rejected_before_materialization(tmp_path):
    path = tmp_path / "package.zip"
    _archive(path)
    raw = bytearray(path.read_bytes())
    central = struct.unpack_from("<I", raw, len(raw) - 22 + 16)[0]
    struct.pack_into("<H", raw, central + 8, 1)
    path.write_bytes(raw)
    with pytest.raises(ArtifactError, match="encoding"):
        package.extract_package(path, hashlib.sha256(raw).hexdigest(), tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_duplicate_zip_members_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "package.zip"
    _archive(path)
    with pytest.warns(UserWarning, match="Duplicate"):
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(package_builder._zip_info("workspace/file.txt", zipfile.ZIP_STORED), b"payload")
    with pytest.raises(ArtifactError, match="collision"):
        package.extract_package(path, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def _git(root, *arguments):
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_snapshot(snapshot):
    _git(snapshot, "init", "--quiet")
    _git(snapshot, "config", "user.name", "Package Test")
    _git(snapshot, "config", "user.email", "package@example.invalid")
    (snapshot / ".gitignore").write_text("ignored-secret\ngit.exe\n", encoding="utf-8")
    _git(snapshot, "add", ".")
    _git(snapshot, "commit", "--quiet", "-m", "package fixture")
    return snapshot, _git(snapshot, "rev-parse", "HEAD")


def _producer(root, sha, output, *extra):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build-workspace-package.py"
    return subprocess.run(
        [
            sys.executable, str(script), "--root", str(root),
            "--expected-source-sha", sha, "--workspace", "workspace",
            "--id", "example/storage", "--version", "preview-7",
            "--requires-siteops", ">=1.0.0b1,<2", "--include", "docs",
            "--license", "LICENSE", "--output", str(output), *extra,
        ],
        cwd=root, capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL, check=False,
    )


def test_git_producer_uses_reviewed_source_not_ignored_files_or_cwd_tools(git_snapshot, tmp_path):
    root, sha = git_snapshot
    (root / "ignored-secret").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (root / "git.exe").write_bytes(b"not an executable")
    result = _producer(root, sha, tmp_path / "package.zip")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["provenance"] == "not-established"
    inspected = package.inspect_package(tmp_path / "package.zip", receipt["sha256"])
    assert inspected.metadata.source_revision == sha
    assert "ignored-secret" not in {entry.path for entry in inspected.metadata.files}
    assert "PRIVATE_SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["dirty", "wrong-commit", "export-ignore"])
def test_git_producer_refuses_unidentified_or_incomplete_source(git_snapshot, tmp_path, fault):
    root, sha = git_snapshot
    if fault == "dirty":
        (root / "LICENSE").write_text("changed", encoding="utf-8")
    elif fault == "wrong-commit":
        sha = "0" * 40
    else:
        (root / ".gitattributes").write_text("workspace/** export-ignore\n", encoding="utf-8")
        _git(root, "add", ".gitattributes")
        _git(root, "commit", "--quiet", "-m", "excluded fixture")
        sha = _git(root, "rev-parse", "HEAD")
    result = _producer(root, sha, tmp_path / "package.zip")
    assert result.returncode == 1
    assert "Error:" in result.stderr
    assert not (tmp_path / "package.zip").exists()


def test_git_companion_paths_are_literal_not_glob_patterns(git_snapshot, tmp_path):
    root, _ = git_snapshot
    (root / "guide[one].md").write_text("literal guide", encoding="utf-8")
    (root / "guideo.md").write_text("different guide", encoding="utf-8")
    _git(root, "add", "guide[one].md", "guideo.md")
    _git(root, "commit", "--quiet", "-m", "literal fixture")
    sha = _git(root, "rev-parse", "HEAD")
    result = _producer(root, sha, tmp_path / "package.zip", "--include", "guide[one].md")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    paths = {
        entry.path for entry in package.inspect_package(tmp_path / "package.zip", receipt["sha256"]).metadata.files
    }
    assert "guide[one].md" in paths
    assert "guideo.md" not in paths


def test_git_producer_rejects_bicep_configuration_outside_workspace(
    git_snapshot,
    tmp_path,
):
    root, _ = git_snapshot
    manifest = root / "workspace" / "manifests" / "storage" / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "templates/storage.template.json",
            "templates/main.bicep",
        ),
        encoding="utf-8",
    )
    (root / "workspace" / "templates" / "main.bicep").write_text(
        "param name string\n",
        encoding="utf-8",
    )
    (root / "bicepconfig.json").write_text("{}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "bicep fixture")
    sha = _git(root, "rev-parse", "HEAD")

    result = _producer(root, sha, tmp_path / "package.zip")

    assert result.returncode == 1
    assert "inside the packaged workspace" in result.stderr
    assert not (tmp_path / "package.zip").exists()


def test_nested_workspace_configuration_takes_precedence_over_repository_root(
    git_snapshot,
):
    root, _ = git_snapshot
    nested = root / "workspace" / "templates"
    (nested / "main.bicep").write_text("param name string\n", encoding="utf-8")
    (nested / "bicepconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "bicepconfig.json").write_text(
        '{"analyzers": {"core": {"enabled": false}}}\n',
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "nested configuration")
    sha = _git(root, "rev-parse", "HEAD")
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "source_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("package_source_snapshot", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.require_workspace_configuration_boundary(
        root,
        sha,
        "workspace",
    )

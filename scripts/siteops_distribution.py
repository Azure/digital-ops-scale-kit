# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate the contents of a locally available Site Ops installation bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

_API_VERSION = "siteops.install/v1"
_KIND = "SiteOpsBundle"
_PACKAGE_NAME = "siteops"
_MANIFEST_NAME = "bundle.json"
INSTALLER_NAME = "siteops-install.py"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_PATH_LENGTH = 512
_MAX_STRING_LENGTH = 1024
_MAX_FILES = 1024
_MAX_TARGETS = 64
_MAX_WHEELS_PER_TARGET = 64
_MAX_FILE_SIZE = 1024 * 1024 * 1024
_MAX_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
_PYTHON = re.compile(r"3\.(?:[0-9]|[1-9][0-9])")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Emitted by the qualified uv 0.12.20 virtual environment creator.
_UV_STARTUP_FILES = {
    "_virtualenv.pth": "69ac3d8f27e679c81b94ab30b3b56e9cd138219b1ba94a1fa3606d5a76a1433d",
    "_virtualenv.py": "cfb3db86aaa53bb62b5ff764970bec2d71c9228590a0ebec57f6ec926cc0bf1a",
}
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class DistributionError(ValueError):
    """The bundle metadata or payload does not satisfy the distribution contract."""


class InstallationError(DistributionError):
    """A fixed native installation diagnostic suitable for the bootstrap."""

    def __init__(self, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class PayloadFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class BundleTarget:
    python: str
    platform: str
    wheels: tuple[str, ...]


@dataclass(frozen=True)
class BundleManifest:
    version: str
    base_version: str
    repository: str
    source_sha: str
    source_ref: str
    build_number: int
    build_attempt: int
    application_wheel: str
    targets: tuple[BundleTarget, ...]
    files: tuple[PayloadFile, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable public JSON shape."""
        return {
            "apiVersion": _API_VERSION,
            "kind": _KIND,
            "package": {
                "name": _PACKAGE_NAME,
                "version": self.version,
                "baseVersion": self.base_version,
                "wheel": self.application_wheel,
            },
            "source": {
                "repository": self.repository,
                "commit": self.source_sha,
                "ref": self.source_ref,
            },
            "build": {
                "number": self.build_number,
                "attempt": self.build_attempt,
            },
            "targets": [
                {
                    "python": target.python,
                    "platform": target.platform,
                    "wheels": list(target.wheels),
                }
                for target in self.targets
            ],
            "files": [
                {
                    "path": entry.path,
                    "sha256": entry.sha256,
                    "size": entry.size,
                }
                for entry in self.files
            ],
        }

    @classmethod
    def from_dict(cls, document: Any) -> BundleManifest:
        """Validate and parse a manifest document."""
        root = _object(
            document,
            {"apiVersion", "kind", "package", "source", "build", "targets", "files"},
            "manifest",
        )
        if root["apiVersion"] != _API_VERSION or root["kind"] != _KIND:
            raise DistributionError("The bundle API version or kind is not supported.")

        package = _object(
            root["package"], {"name", "version", "baseVersion", "wheel"}, "package",
        )
        if package["name"] != _PACKAGE_NAME:
            raise DistributionError("The bundle package name must be siteops.")
        version = _string(package["version"], "package.version", pattern=_VERSION)
        base_version = _string(
            package["baseVersion"], "package.baseVersion", pattern=_VERSION,
        )
        application_wheel = _path(package["wheel"], "package.wheel")
        if not application_wheel.endswith(".whl"):
            raise DistributionError("The application wheel path must end in .whl.")

        source = _object(root["source"], {"repository", "commit", "ref"}, "source")
        repository = _string(
            source["repository"], "source.repository", pattern=_REPOSITORY,
        )
        source_sha = _string(source["commit"], "source.commit", pattern=_SOURCE_SHA)
        source_ref = _string(source["ref"], "source.ref")

        build = _object(root["build"], {"number", "attempt"}, "build")
        build_number = _positive_integer(build["number"], "build.number")
        build_attempt = _positive_integer(build["attempt"], "build.attempt")

        target_values = _array(root["targets"], "targets", 1, _MAX_TARGETS)
        targets: list[BundleTarget] = []
        target_keys: set[tuple[str, str]] = set()
        for index, value in enumerate(target_values):
            item = _object(value, {"python", "platform", "wheels"}, f"targets[{index}]")
            python = _string(
                item["python"], f"targets[{index}].python", pattern=_PYTHON,
            )
            platform = _string(
                item["platform"], f"targets[{index}].platform", pattern=_PLATFORM,
            )
            key = (python, platform)
            if key in target_keys:
                raise DistributionError("Bundle target declarations must be unique.")
            target_keys.add(key)
            wheel_values = _array(
                item["wheels"],
                f"targets[{index}].wheels",
                1,
                _MAX_WHEELS_PER_TARGET,
            )
            wheels = tuple(
                _path(wheel, f"targets[{index}].wheels[{wheel_index}]")
                for wheel_index, wheel in enumerate(wheel_values)
            )
            if len({_normalized_path(wheel) for wheel in wheels}) != len(wheels):
                raise DistributionError("A bundle target cannot list a wheel more than once.")
            targets.append(BundleTarget(python=python, platform=platform, wheels=wheels))

        file_values = _array(root["files"], "files", 1, _MAX_FILES)
        files: list[PayloadFile] = []
        normalized_paths: set[str] = set()
        total_size = 0
        for index, value in enumerate(file_values):
            item = _object(value, {"path", "sha256", "size"}, f"files[{index}]")
            path = _path(item["path"], f"files[{index}].path")
            normalized = _normalized_path(path)
            if normalized in normalized_paths:
                raise DistributionError("Bundle file paths must be unique without case collisions.")
            normalized_paths.add(normalized)
            if path == _MANIFEST_NAME:
                raise DistributionError("The manifest cannot list itself as a payload file.")
            sha256 = _string(
                item["sha256"], f"files[{index}].sha256", pattern=_SHA256,
            )
            size = _bounded_integer(
                item["size"], f"files[{index}].size", 0, _MAX_FILE_SIZE,
            )
            total_size += size
            if total_size > _MAX_TOTAL_SIZE:
                raise DistributionError("The declared bundle payload is too large.")
            files.append(PayloadFile(path=path, sha256=sha256, size=size))

        inventory = {entry.path for entry in files}
        required = {
            "pylock.toml",
            INSTALLER_NAME,
            "LICENSE",
            "ThirdPartyNotices.txt",
            application_wheel,
        }
        missing = sorted(required - inventory)
        if missing:
            raise DistributionError(
                "The bundle inventory is missing required payload files: "
                + ", ".join(missing)
                + "."
            )
        for target in targets:
            if application_wheel not in target.wheels:
                raise DistributionError("Every bundle target must include the application wheel.")
            for wheel in target.wheels:
                if not wheel.endswith(".whl") or wheel not in inventory:
                    raise DistributionError(
                        "Bundle target wheels must reference inventoried .whl files."
                    )

        return cls(
            version=version,
            base_version=base_version,
            repository=repository,
            source_sha=source_sha,
            source_ref=source_ref,
            build_number=build_number,
            build_attempt=build_attempt,
            application_wheel=application_wheel,
            targets=tuple(targets),
            files=tuple(files),
        )


def load_manifest(root: Path) -> BundleManifest:
    """Load bundle.json with bounded input and duplicate-key rejection."""
    return _parse_manifest(_read_manifest_bytes(Path(root)))


def _parse_manifest(raw: bytes) -> BundleManifest:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise DistributionError("bundle.json is too large.")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DistributionError(f"The manifest contains a duplicate JSON key: {key}.")
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8")
        document = json.loads(text, object_pairs_hook=unique_object)
    except DistributionError:
        raise
    except (UnicodeError, ValueError) as error:
        raise DistributionError("bundle.json is not valid UTF-8 JSON.") from error
    return BundleManifest.from_dict(document)


def _zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = archive.infolist()
    if not 1 <= len(members) <= 65536:
        raise DistributionError("The archive inventory exceeds its item limit.")
    selected: dict[str, zipfile.ZipInfo] = {}
    identities: set[str] = set()
    total = 0
    for member in members:
        name = _path(member.filename.rstrip("/") if member.is_dir() else member.filename, "archive member")
        identity = _normalized_path(name)
        mode = stat.S_IFMT(member.external_attr >> 16)
        if identity in identities or mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise DistributionError("The archive contains linked, duplicate or unsupported members.")
        identities.add(identity)
        total += member.file_size
        if not 0 <= member.file_size <= _MAX_FILE_SIZE or total > _MAX_TOTAL_SIZE:
            raise DistributionError("The archive exceeds its size limit.")
        if not member.is_dir():
            selected[name] = member
    return selected


def admit_bundle(
    archive: Path, destination: Path, *, repository: str, source_sha: str, source_ref: str,
) -> BundleManifest:
    """Admit bytes whose external provenance the caller has already verified."""
    _require_node(archive, directory=False, label="authenticated archive")
    with zipfile.ZipFile(archive) as package:
        members = _zip_members(package)
        entry = members.get(_MANIFEST_NAME)
        if entry is None or entry.file_size > _MAX_MANIFEST_BYTES:
            raise DistributionError("The authenticated bundle has no bounded manifest.")
        raw = package.read(entry)
        manifest = _parse_manifest(raw)
        if (manifest.repository, manifest.source_sha, manifest.source_ref) != (
            repository, source_sha, source_ref,
        ):
            raise DistributionError("The authenticated bundle describes another source.")
        if set(members) != {_MANIFEST_NAME, *(item.path for item in manifest.files)}:
            raise DistributionError("The authenticated archive differs from its payload inventory.")
        if not destination.exists():
            destination.mkdir(mode=0o700)
        _require_node(destination, directory=True, label="retained bundle")
        if not any(destination.iterdir()):
            for name, member in members.items():
                target = destination.joinpath(*name.split("/"))
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output, package.open(member) as incoming:
                    while block := incoming.read(1024 * 1024):
                        output.write(block)
        if _read_manifest_bytes(destination) != raw:
            raise DistributionError("The retained manifest differs from the authenticated archive.")
    verify_payload(destination, manifest)
    return manifest


def _uv_configuration(environment: Path) -> dict[str, str]:
    _require_node(environment, directory=True, label="tool environment")
    configuration = environment / "pyvenv.cfg"
    if _require_node(configuration, directory=False, label="tool configuration").st_size > 65536:
        raise DistributionError("The tool configuration exceeds its byte limit.")
    settings = {}
    for line in configuration.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip() in settings:
            raise DistributionError("The tool configuration has an unsupported shape.")
        settings[key.strip()] = value.strip()
    return settings


def verify_installed_payload(
    bundle: Path, manifest: BundleManifest, environment: Path, *, python: str, platform: str,
) -> None:
    """Compare installed package files without importing the installed application."""
    verify_payload(bundle, manifest)
    target = select_target(manifest, python, platform)
    settings = _uv_configuration(environment)
    if (
        settings.get("include-system-site-packages") != "false"
        or settings.get("uv") != "0.12.20"
        or not settings.get("version_info", "").startswith(python + ".")
    ):
        raise DistributionError("The installed environment differs from the selected runtime or tool.")
    relative = ("Lib", "site-packages") if platform == "windows-x86_64" else (
        "lib", f"python{python}", "site-packages",
    )
    packages = environment
    for component in relative:
        packages /= component
        _require_node(packages, directory=True, label="installed package directory")
    expected: dict[str, tuple[int, str]] = {}
    distributions: set[str] = set()
    for wheel in target.wheels:
        with zipfile.ZipFile(bundle.joinpath(*wheel.split("/"))) as archive:
            for name, member in _zip_members(archive).items():
                parts = name.split("/")
                if any(part.endswith(".data") for part in parts):
                    raise DistributionError("The selected wheel needs an unsupported installation layout.")
                if parts[0].endswith(".dist-info"):
                    distributions.add(parts[0])
                    if parts[-1] in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}:
                        continue
                if name in expected:
                    raise DistributionError("Selected wheels cannot overwrite another package's files.")
                digest = hashlib.sha256()
                with archive.open(member) as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                expected[name] = (member.file_size, digest.hexdigest())
    hooks = _UV_STARTUP_FILES
    for name, digest in hooks.items():
        if _hash_regular_file(packages / name)[1] != digest:
            raise DistributionError("The installed environment hook differs from the qualified tool.")
    files, _ = _inventory(packages)
    for name, identity in expected.items():
        if name not in files or _hash_regular_file(packages.joinpath(*name.split("/"))) != identity:
            raise DistributionError("Installed package bytes differ from the authenticated wheels.")
    for name in files - expected.keys() - hooks.keys():
        parts = name.split("/")
        if len(parts) == 2 and parts[0] in distributions and parts[1] in {
            "RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json",
        }:
            continue
        bytecode = re.fullmatch(r"(.+)\.cpython-[0-9]+(?:\.opt-[12])?\.pyc", parts[-1])
        if len(parts) >= 2 and parts[-2] == "__pycache__" and bytecode:
            source = "/".join([*parts[:-2], bytecode[1] + ".py"])
            if source in expected or source in hooks:
                continue
        raise DistributionError("The installed environment contains unexpected package files.")


def _uv_package_directory(environment: Path, python: str) -> Path:
    parts = ("Lib", "site-packages") if os.name == "nt" else (
        "lib", f"python{python}", "site-packages",
    )
    directory = environment
    _require_node(directory, directory=True, label="uv tool environment")
    for part in parts:
        directory /= part
        _require_node(directory, directory=True, label="uv package directory")
    return directory


def _uv_command(environment: Path) -> Path:
    return environment / ("Scripts/siteops.exe" if os.name == "nt" else "bin/siteops")


def _verify_uv_command(command: Path, environment: Path) -> None:
    selected = _uv_command(environment)
    _require_node(selected.parent, directory=True, label="uv executable directory")
    _require_node(selected, directory=False, label="uv Site Ops executable")
    if os.name == "nt":
        matches = _hash_regular_file(command) == _hash_regular_file(selected)
    else:
        matches = command.is_symlink() and command.resolve(strict=True) == selected.absolute()
    if not matches:
        raise InstallationError(
            "The exposed command belongs to another installation. Remove it with its original manager.",
            exit_code=3,
        )


def _uv_wheel_identity(bundle: Path, manifest: BundleManifest) -> None:
    with zipfile.ZipFile(bundle / manifest.application_wheel) as wheel:
        members = _zip_members(wheel)
        metadata = [
            member for name, member in members.items()
            if len(name.split("/")) == 2 and name.endswith(".dist-info/METADATA")
        ]
        if len(metadata) != 1 or metadata[0].file_size > _MAX_MANIFEST_BYTES:
            raise DistributionError("The application wheel has no bounded package identity.")
        message = BytesParser().parsebytes(wheel.read(metadata[0]))
        if message.get_all("Name") != ["siteops"] or message.get_all("Version") != [manifest.version]:
            raise DistributionError("The application wheel differs from the selected Site Ops package.")


def _uv_matches_selection(environment: Path, bundle: Path, manifest: BundleManifest) -> bool:
    receipt = environment / "uv-receipt.toml"
    if _require_node(receipt, directory=False, label="uv tool receipt").st_size > _MAX_MANIFEST_BYTES:
        raise DistributionError("The native tool receipt exceeds its byte limit.")
    configuration = _uv_configuration(environment)
    version = re.match(r"^(3\.[0-9]{1,2})\.", configuration.get("version_info", ""))
    if version is None:
        raise DistributionError("The existing native runtime identity is invalid.")
    packages = _uv_package_directory(environment, version[1])
    for path in packages.iterdir():
        if (
            path.suffix == ".pth" and path.name != "_virtualenv.pth"
            or path.stem in {"sitecustomize", "usercustomize"}
        ):
            raise InstallationError(
                "The tool has unrecognized Python startup files. Inspect it before using uv tool uninstall siteops.",
                exit_code=4,
            )
    for name, digest in _UV_STARTUP_FILES.items():
        if _hash_regular_file(packages / name)[1] != digest:
            raise InstallationError(
                "The tool has unrecognized Python startup files. Inspect it before using uv tool uninstall siteops.",
                exit_code=4,
            )
    metadata = [path for path in packages.iterdir() if path.name.startswith("siteops-")
                and path.name.endswith(".dist-info")]
    if len(metadata) != 1:
        raise DistributionError("The existing Site Ops tool has an ambiguous package identity.")
    _require_node(metadata[0], directory=True, label="uv application metadata")
    origin = metadata[0] / "direct_url.json"
    if _require_node(origin, directory=False, label="uv application origin").st_size > 65536:
        raise DistributionError("The native application origin exceeds its byte limit.")
    try:
        document = json.loads(origin.read_bytes())
    except (ValueError, UnicodeError):
        raise DistributionError("The native application origin is invalid.") from None
    if not isinstance(document, dict) or not isinstance(document.get("url"), str):
        raise DistributionError("The native application origin is invalid.")
    selected = urlsplit(document["url"])
    return (
        selected.scheme == "file" and not selected.netloc
        and not selected.query and not selected.fragment
        and Path(url2pathname(selected.path)) == (bundle / manifest.application_wheel).absolute()
    )


def _uv_runtime_binding(environment: Path) -> None:
    configuration = _uv_configuration(environment)
    if Path(configuration.get("home", "")) != Path(sys._base_executable).parent:
        raise InstallationError(
            "The installed runtime differs from the selected runtime. "
            "Inspect the tool before using uv tool uninstall siteops, then retry.",
        )


def install_verified_bundle(
    archive: Path, bundle: Path, *, uv: Path, tool_directory: Path, command_directory: Path,
    repository: str, source_sha: str, source_ref: str, replace: bool = False,
    environment: Mapping[str, str] | None = None,
) -> BundleManifest:
    """Use native uv after caller-owned provenance, tool and storage admission."""
    if not all(path.is_absolute() for path in (archive, bundle, uv, tool_directory, command_directory)):
        raise InstallationError("Native installation requires absolute tool, bundle and storage paths.")
    manifest = admit_bundle(
        archive, bundle, repository=repository, source_sha=source_sha, source_ref=source_ref,
    )
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    platform = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    select_target(manifest, python, platform)
    _uv_wheel_identity(bundle, manifest)
    for directory in (tool_directory, command_directory):
        _require_node(directory, directory=True, label="selected uv storage")
    _require_node(uv, directory=False, label="selected uv executable")
    tool = tool_directory / "siteops"
    command = command_directory / ("siteops.exe" if os.name == "nt" else "siteops")
    existing = os.path.lexists(tool)
    if os.path.lexists(command):
        if not existing:
            raise InstallationError(
                "The exposed command belongs to another installation. Remove it with its original manager.",
                exit_code=3,
            )
        _verify_uv_command(command, tool)
    if existing:
        _uv_runtime_binding(tool)
        matching = _uv_matches_selection(tool, bundle, manifest)
        if not matching and not replace:
            raise InstallationError(
                "Another Site Ops selection is installed. Use --replace after review.", exit_code=2,
            )
        if not replace:
            verify_installed_payload(bundle, manifest, tool, python=python, platform=platform)
            _verify_uv_command(command, tool)
            return manifest
    child = {
        key: value for key, value in (os.environ if environment is None else environment).items()
        if not key.upper().startswith(("UV_", "PIP_", "PYTHON"))
        and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    child.update(UV_TOOL_DIR=str(tool_directory), UV_TOOL_BIN_DIR=str(command_directory))
    with tempfile.TemporaryDirectory(prefix="uv-install-", dir=bundle.parent) as scratch:
        child.update(TEMP=scratch, TMP=scratch, TMPDIR=scratch)
        with (Path(scratch) / "uv.log").open("wb") as log:
            try:
                result = subprocess.run(
                    [str(uv), "tool", "install", str(bundle / manifest.application_wheel),
                     "--with-requirements", str(bundle / "pylock.toml"),
                     "--find-links", str(bundle / "wheels"), "--python", sys._base_executable,
                     "--no-config", "--offline", "--no-index", "--no-build", "--no-cache",
                     "--no-python-downloads", "--link-mode", "copy", "--no-progress",
                     *(["--reinstall"] if existing else [])],
                    cwd=scratch, env=child, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, timeout=300, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise InstallationError("Native uv could not complete installation. Inspect the tool before retrying.") from None
            if result.returncode:
                raise InstallationError("Native uv installation failed. Inspect the tool before retrying.")
    _uv_runtime_binding(tool)
    verify_installed_payload(bundle, manifest, tool, python=python, platform=platform)
    _verify_uv_command(command, tool)
    return manifest


def verify_payload(root: Path, manifest: BundleManifest) -> None:
    """Verify the complete payload inventory without following filesystem links."""
    try:
        manifest = BundleManifest.from_dict(manifest.to_dict())
    except (AttributeError, TypeError) as error:
        raise DistributionError("The bundle manifest object is invalid.") from error
    root = Path(root)
    _require_node(root, directory=True, label="bundle root")
    actual_files, actual_directories = _inventory(root)
    expected_files = {_MANIFEST_NAME, *(entry.path for entry in manifest.files)}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        unexpected = sorted(actual_files - expected_files)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise DistributionError(
            "The bundle file inventory does not match: " + " and ".join(details) + "."
        )
    expected_directories = {
        parent
        for path in expected_files
        for parent in _parent_paths(path)
    }
    if actual_directories != expected_directories:
        raise DistributionError("The bundle contains an unexpected or missing directory.")

    _read_manifest_bytes(root)
    for entry in manifest.files:
        size, digest = _hash_regular_file(root / Path(*entry.path.split("/")))
        if size != entry.size:
            raise DistributionError(f"Bundle file size does not match the manifest: {entry.path}.")
        if digest != entry.sha256:
            raise DistributionError(f"Bundle file digest does not match the manifest: {entry.path}.")


def select_target(manifest: BundleManifest, python: str, platform: str) -> BundleTarget:
    """Select an exact declared Python and platform target."""
    for target in manifest.targets:
        if target.python == python and target.platform == platform:
            return target
    supported = ", ".join(
        f"Python {target.python} on {target.platform}" for target in manifest.targets
    )
    raise DistributionError(
        f"This bundle does not support Python {python} on {platform}. "
        f"Declared targets: {supported}."
    )


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DistributionError(f"{label} must be an object.")
    actual = set(value)
    if actual != keys:
        raise DistributionError(f"{label} must contain exactly: {', '.join(sorted(keys))}.")
    if any(not isinstance(key, str) for key in value):
        raise DistributionError(f"{label} keys must be strings.")
    return value


def _array(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise DistributionError(f"{label} must be an array.")
    if not minimum <= len(value) <= maximum:
        raise DistributionError(f"{label} has an unsupported item count.")
    return value


def _string(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_STRING_LENGTH:
        raise DistributionError(f"{label} must be a non-empty bounded string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DistributionError(f"{label} cannot contain control characters.")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise DistributionError(f"{label} has an unsupported format.")
    return value


def _positive_integer(value: Any, label: str) -> int:
    return _bounded_integer(value, label, 1, 2**63 - 1)


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise DistributionError(f"{label} must be an integer in the supported range.")
    return value


def _path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_LENGTH:
        raise DistributionError(f"{label} must be a bounded relative path.")
    if value != unicodedata.normalize("NFC", value):
        raise DistributionError(f"{label} must use normalized Unicode.")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise DistributionError(f"{label} must be a relative POSIX path.")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise DistributionError(f"{label} contains an unsafe path component.")
    for component in components:
        if component.endswith((" ", ".")) or ":" in component:
            raise DistributionError(f"{label} is not portable to Windows.")
        if any(ord(character) < 32 or ord(character) == 127 for character in component):
            raise DistributionError(f"{label} cannot contain control characters.")
        stem = component.split(".", 1)[0].upper()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise DistributionError(f"{label} uses a reserved Windows name.")
    return value


def _normalized_path(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _parent_paths(path: str) -> tuple[str, ...]:
    components = path.split("/")[:-1]
    return tuple("/".join(components[:index]) for index in range(1, len(components) + 1))


def _reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & marker)


def _require_node(path: Path, *, directory: bool, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise DistributionError(f"The {label} could not be accessed.") from error
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or _reparse(info) or not expected:
        raise DistributionError(f"The {label} must be a regular {'directory' if directory else 'file'}.")
    if not directory and getattr(info, "st_nlink", 1) != 1:
        raise DistributionError(f"The {label} must not be linked.")
    return info


def _read_manifest_bytes(root: Path) -> bytes:
    _require_node(root, directory=True, label="bundle root")
    path = root / _MANIFEST_NAME
    before = _require_node(path, directory=False, label="bundle manifest")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DistributionError("bundle.json could not be opened safely.") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _reparse(opened)
            or getattr(opened, "st_nlink", 1) != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise DistributionError("bundle.json changed while it was being read.")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise DistributionError("bundle.json could not be read.") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise DistributionError("bundle.json is too large.")
    return raw


def _inventory(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()

    def visit(directory: Path, prefix: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise DistributionError("The bundle inventory could not be read.") from error
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            _path(relative, "bundle entry")
            try:
                info = Path(entry.path).lstat()
            except OSError as error:
                raise DistributionError("A bundle entry could not be inspected.") from error
            if entry.is_symlink() or _reparse(info):
                raise DistributionError(f"Bundle entries cannot be links: {relative}.")
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(info.st_mode):
                if getattr(info, "st_nlink", 1) != 1:
                    raise DistributionError(f"Bundle entries cannot be linked: {relative}.")
                files.add(relative)
            else:
                raise DistributionError(f"Bundle entries must be regular files: {relative}.")

    visit(root, "")
    return files, directories


def _hash_regular_file(path: Path) -> tuple[int, str]:
    before = _require_node(path, directory=False, label="bundle payload file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DistributionError("A bundle payload file could not be opened safely.") from error
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _reparse(opened)
            or getattr(opened, "st_nlink", 1) != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise DistributionError("A bundle payload file changed while it was being verified.")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_FILE_SIZE:
                    raise DistributionError("A bundle payload file exceeds the supported size.")
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return size, digest.hexdigest()


def main() -> None:
    """Run the shared helper only after external archive and storage admission."""
    try:
        mode, archive, bundle, repository, commit, ref, *remaining = sys.argv[1:]
        counts = {"admit": 0, "installed": 1, "install": 3, "replace": 3}
        if mode not in counts or len(remaining) != counts[mode]:
            raise DistributionError("Unsupported installer helper operation.")
        root = Path(bundle)
        if mode in {"install", "replace"}:
            uv, tools, commands = map(Path, remaining)
            manifest = install_verified_bundle(
                Path(archive), root, repository=repository, source_sha=commit, source_ref=ref,
                uv=uv, tool_directory=tools, command_directory=commands, replace=mode == "replace",
            )
        else:
            manifest = admit_bundle(
                Path(archive), root, repository=repository, source_sha=commit, source_ref=ref,
            )
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
        select_target(manifest, python, platform)
        if mode == "installed":
            verify_installed_payload(
                root, manifest, Path(remaining[0]), python=python, platform=platform,
            )
        print(json.dumps({"version": manifest.version, "wheel": manifest.application_wheel}))
    except InstallationError as error:
        print(str(error), file=sys.stderr)
        sys.exit(error.exit_code)
    except (DistributionError, OSError, ValueError, zipfile.BadZipFile):
        sys.exit("The bundle or installed payload failed validation. Inspect the selected installation.")


if __name__ == "__main__":
    main()

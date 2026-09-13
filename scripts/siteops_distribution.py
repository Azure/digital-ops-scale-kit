# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate the contents of a locally available Site Ops installation bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_API_VERSION = "siteops.install/v1"
_KIND = "SiteOpsBundle"
_PACKAGE_NAME = "siteops"
_MANIFEST_NAME = "bundle.json"
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
    raw = _read_manifest_bytes(Path(root))

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

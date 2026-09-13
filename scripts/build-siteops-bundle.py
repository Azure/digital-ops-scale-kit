# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Build an identified Site Ops wheel and its deterministic installation bundle."""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from siteops_distribution import (
    BundleManifest,
    BundleTarget,
    DistributionError,
    PayloadFile,
    verify_payload,
)

_OUTPUT_NAME = "siteops-install.zip"
_RUNTIME_LOCK = "siteops-runtime-requirements.txt"
_BUILD_LOCK = "siteops-build-requirements.txt"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_LOCKED_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)"
)
_VERSION_LITERAL = re.compile(
    r'(?m)^__version__ = "(?P<version>[^"\r\n]+)"(?=\r?$)'
)
_PROJECT_DEPENDENCIES = re.compile(r"(?m)^dependencies = \[[^\r\n]*\](?=\r?$)")
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_TARGETS = (
    ("3.10", "windows-x86_64", "310", "win_amd64"),
    ("3.10", "linux-x86_64", "310", "manylinux2014_x86_64"),
    ("3.11", "windows-x86_64", "311", "win_amd64"),
    ("3.11", "linux-x86_64", "311", "manylinux2014_x86_64"),
    ("3.12", "windows-x86_64", "312", "win_amd64"),
    ("3.12", "linux-x86_64", "312", "manylinux2014_x86_64"),
    ("3.13", "windows-x86_64", "313", "win_amd64"),
    ("3.13", "linux-x86_64", "313", "manylinux2014_x86_64"),
    ("3.14", "windows-x86_64", "314", "win_amd64"),
    ("3.14", "linux-x86_64", "314", "manylinux2014_x86_64"),
)


class BuildError(RuntimeError):
    """An expected bundle production failure safe to report without private details."""


@dataclass(frozen=True)
class LockedRequirement:
    name: str
    version: str
    hashes: frozenset[str]


@dataclass(frozen=True)
class RuntimeWheel:
    path: Path
    name: str
    version: str
    sha256: str
    tags: frozenset[Any]
    requires_python: str
    dependencies: tuple[str, ...]


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_locked_requirements(path: Path) -> tuple[LockedRequirement, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BuildError("A committed dependency lock file could not be read.") from error
    logical: list[str] = []
    current = ""
    for source_line in lines:
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        fragment = line[:-1].rstrip() if continued else line
        current = f"{current} {fragment}".strip()
        if not continued:
            logical.append(current)
            current = ""
    if current:
        raise BuildError("A dependency lock file has an incomplete continuation.")

    requirements: list[LockedRequirement] = []
    names: set[str] = set()
    for line in logical:
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError as error:
            raise BuildError("A dependency lock entry could not be parsed.") from error
        if not tokens:
            continue
        match = _LOCKED_REQUIREMENT.fullmatch(tokens[0])
        if match is None:
            raise BuildError("Dependency lock entries must use exact versions.")
        name = _normalized_name(match["name"])
        if name in names:
            raise BuildError("A dependency lock file lists a package more than once.")
        names.add(name)
        hashes: set[str] = set()
        for token in tokens[1:]:
            prefix = "--hash=sha256:"
            if not token.startswith(prefix) or _SHA256.fullmatch(token[len(prefix):]) is None:
                raise BuildError("Dependency lock entries may contain only SHA256 hashes.")
            hashes.add(token[len(prefix):])
        if not hashes:
            raise BuildError("Every locked dependency must include a SHA256 hash.")
        requirements.append(
            LockedRequirement(
                name=name,
                version=match["version"],
                hashes=frozenset(hashes),
            )
        )
    if not requirements:
        raise BuildError("A dependency lock file cannot be empty.")
    return tuple(requirements)


def _validate_build_environment(requirements: tuple[LockedRequirement, ...]) -> None:
    expected = {requirement.name: requirement.version for requirement in requirements}
    for name, version in expected.items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise BuildError("Install the committed build requirements before producing a bundle.") from error
        if installed != version:
            raise BuildError("The active Python environment does not match the build requirements.")
    if set(expected) != {"pip", "setuptools", "packaging"}:
        raise BuildError("The build requirements must pin pip, setuptools, and packaging.")


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError("A required production tool could not complete.") from error
    return result


def _validate_repository(root: Path, expected_source_sha: str) -> None:
    if _SOURCE_SHA.fullmatch(expected_source_sha) is None:
        raise BuildError("The expected source commit must be a full lowercase Git SHA.")
    head = _run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        cwd=root,
        timeout=30,
    )
    if head.returncode != 0 or head.stdout.decode("ascii", errors="ignore").strip() != expected_source_sha:
        raise BuildError("The checked out commit does not match --expected-source-sha.")
    status = _run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        cwd=root,
        timeout=30,
    )
    if status.returncode != 0:
        raise BuildError("Git could not confirm the source state.")
    if status.stdout:
        raise BuildError("The source checkout has tracked or untracked changes.")


def _safe_archive_path(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/"):
        raise BuildError("The source archive contains an unsafe path.")
    stripped = name[:-1] if name.endswith("/") else name
    components = tuple(stripped.split("/"))
    if any(component in {"", ".", ".."} or ":" in component for component in components):
        raise BuildError("The source archive contains an unsafe path.")
    return components


def _export_tracked_source(root: Path, destination: Path, source_sha: str) -> None:
    archive = destination.parent / "source.zip"
    with archive.open("xb") as stream:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "archive", "--format=zip", source_sha],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BuildError("Git could not export the tracked source.") from error
    if result.returncode != 0:
        raise BuildError("Git could not export the tracked source.")
    destination.mkdir()
    normalized: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                components = _safe_archive_path(item.filename)
                relative = "/".join(components)
                key = relative.casefold()
                if key in normalized:
                    raise BuildError("The source archive contains colliding paths.")
                normalized.add(key)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise BuildError("The source archive cannot contain symbolic links.")
                target = destination.joinpath(*components)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if mode and not stat.S_ISREG(mode):
                    raise BuildError("The source archive contains a nonregular file.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as input_stream, target.open("xb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)
    except (OSError, zipfile.BadZipFile) as error:
        raise BuildError("The tracked source archive could not be extracted.") from error


def _read_utf8(path: Path, label: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise BuildError(f"The staged {label} could not be read as UTF-8.") from error


def _write_utf8(path: Path, value: str, label: str) -> None:
    try:
        path.write_bytes(value.encode("utf-8"))
    except OSError as error:
        raise BuildError(f"The staged {label} could not be updated.") from error


def _validate_source_dependencies(
    project_text: str, runtime_requirements: tuple[LockedRequirement, ...],
) -> frozenset[str]:
    try:
        import tomllib
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError as error:
        raise BuildError("Use Python 3.11 or newer with the committed build requirements.") from error
    try:
        project = tomllib.loads(project_text)["project"]
        declarations = project["dependencies"]
        if not isinstance(declarations, list) or any(
            not isinstance(value, str) for value in declarations
        ):
            raise BuildError("Source dependencies must be an array of requirement strings.")
        locked = {item.name: item for item in runtime_requirements}
        roots = set()
        for value in declarations:
            requirement = Requirement(value)
            selected = locked.get(_normalized_name(requirement.name))
            if (
                requirement.url or requirement.extras or requirement.marker
                or selected is None
                or Version(selected.version) not in requirement.specifier
            ):
                raise BuildError(
                    "The runtime lock must satisfy every declared source dependency. "
                    "Direct URLs, extras, and conditional source dependencies need an explicit policy."
                )
            roots.add(selected.name)
        return frozenset(roots)
    except (ValueError, KeyError, TypeError) as error:
        raise BuildError("The source project dependency metadata is invalid.") from error


def _derive_staged_source(
    source: Path,
    *,
    build_number: int,
    build_attempt: int,
    source_sha: str,
    runtime_requirements: tuple[LockedRequirement, ...],
    version_mode: str = "build",
) -> tuple[str, str, frozenset[str]]:
    if version_mode not in {"build", "source"}:
        raise BuildError("The version mode must be build or source.")
    pyproject_path = source / "pyproject.toml"
    pyproject = _read_utf8(pyproject_path, "project metadata")
    source_dependencies = _validate_source_dependencies(pyproject, runtime_requirements)
    init_path = source / "siteops" / "__init__.py"
    init_text = _read_utf8(init_path, "package version")
    matches = list(_VERSION_LITERAL.finditer(init_text))
    if len(matches) != 1:
        raise BuildError("The staged package must contain one exact __version__ literal.")
    base_version = matches[0]["version"]
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError as error:
        raise BuildError("The committed build requirements are not installed.") from error
    try:
        parsed_base = Version(base_version)
    except InvalidVersion as error:
        raise BuildError("The checked-in package version is not valid.") from error
    if str(parsed_base) != base_version or parsed_base.local is not None:
        raise BuildError("The checked-in package version must be canonical and have no local segment.")
    version = (
        base_version if version_mode == "source"
        else f"{base_version}+build.{build_number}.{build_attempt}.g{source_sha[:12]}"
    )
    try:
        if str(Version(version)) != version:
            raise BuildError("The derived package version is not canonical.")
    except InvalidVersion as error:
        raise BuildError("The derived package version is not valid.") from error
    updated_init = _VERSION_LITERAL.sub(f'__version__ = "{version}"', init_text)
    _write_utf8(init_path, updated_init, "package version")

    dependencies = [
        f"{requirement.name}=={requirement.version}"
        for requirement in runtime_requirements
    ]
    updated_project, replacements = _PROJECT_DEPENDENCIES.subn(
        "dependencies = " + json.dumps(dependencies),
        pyproject,
    )
    if replacements != 1:
        raise BuildError("The staged project must contain one direct dependency declaration.")
    _write_utf8(pyproject_path, updated_project, "project metadata")
    return base_version, version, source_dependencies


def _build_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PIP_") and key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
            "PIP_KEYRING_PROVIDER": "disabled",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_DATE_EPOCH": "315532800",
        }
    )
    return environment


def _fixed_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _normalize_wheel(path: Path) -> None:
    normalized_path = path.with_name(path.name + ".normalized")
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(
            normalized_path,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as destination:
            names = source.namelist()
            if len(names) != len(set(name.casefold() for name in names)):
                raise BuildError("The application wheel contains colliding paths.")
            for name in sorted(names):
                components = _safe_archive_path(name)
                if name.endswith("/") or len(components) == 0:
                    raise BuildError("The application wheel contains an unexpected directory entry.")
                source_info = source.getinfo(name)
                mode = source_info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise BuildError("The application wheel cannot contain symbolic links.")
                destination.writestr(_fixed_zip_info(name), source.read(name))
        normalized_path.replace(path)
    except BuildError:
        if normalized_path.exists():
            normalized_path.unlink()
        raise
    except (OSError, zipfile.BadZipFile) as error:
        if normalized_path.exists():
            normalized_path.unlink()
        raise BuildError("The application wheel could not be normalized.") from error


def _build_application_wheel(source: Path, wheel_directory: Path) -> Path:
    wheel_directory.mkdir()
    result = _run(
        [
            sys.executable,
            "-B",
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--no-build-isolation",
            str(source),
            "--wheel-dir",
            str(wheel_directory),
        ],
        cwd=source.parent,
        environment=_build_environment(),
        timeout=180,
    )
    if result.returncode != 0:
        raise BuildError("The application wheel build failed.")
    wheels = list(wheel_directory.glob("*.whl"))
    if len(wheels) != 1:
        raise BuildError("The application build must produce exactly one wheel.")
    _normalize_wheel(wheels[0])
    return wheels[0]


def _inspect_application_wheel(
    wheel: Path,
    *,
    source: Path,
    version: str,
    runtime_requirements: tuple[LockedRequirement, ...],
) -> str:
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.utils import canonicalize_name, parse_wheel_filename
    except ImportError as error:
        raise BuildError("The committed build requirements are not installed.") from error
    try:
        name, parsed_version, build, tags = parse_wheel_filename(wheel.name)
    except ValueError as error:
        raise BuildError("The application wheel has an invalid filename.") from error
    if canonicalize_name(str(name)) != "siteops" or str(parsed_version) != version or build:
        raise BuildError("The application wheel identity does not match the staged package.")
    if {str(tag) for tag in tags} != {"py3-none-any"}:
        raise BuildError("The application wheel must use only the py3-none-any tag.")

    try:
        _regular_file(wheel, "application wheel")
        content = wheel.read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(wheel_names) != 1 or len(entry_names) != 1:
                raise BuildError("The application wheel is missing required package metadata.")
            metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
            if metadata["Name"] != "siteops" or metadata["Version"] != version:
                raise BuildError("The application wheel metadata has the wrong identity.")
            try:
                python_requirement = SpecifierSet(metadata["Requires-Python"] or "")
            except InvalidSpecifier as error:
                raise BuildError("The application wheel Python requirement is invalid.") from error
            if not metadata["Requires-Python"] or any(
                python not in python_requirement for python, _, _, _ in _TARGETS
            ):
                raise BuildError("The application wheel Python requirement excludes a declared target.")
            actual_requirements: dict[str, str] = {}
            for value in metadata.get_all("Requires-Dist", []):
                try:
                    requirement = Requirement(value)
                except InvalidRequirement as error:
                    raise BuildError("The application wheel has invalid dependency metadata.") from error
                if requirement.extras:
                    raise BuildError("The application wheel has unsupported dependency metadata.")
                if requirement.marker is not None:
                    if "extra" not in str(requirement.marker):
                        raise BuildError("The application wheel has unsupported dependency metadata.")
                    continue
                actual_requirements[canonicalize_name(requirement.name)] = str(
                    requirement.specifier
                )
            expected_requirements = {
                requirement.name: f"=={requirement.version}"
                for requirement in runtime_requirements
            }
            if actual_requirements != expected_requirements:
                raise BuildError("The application wheel dependencies do not match the runtime lock.")
            wheel_metadata = archive.read(wheel_names[0]).decode("utf-8")
            if "Root-Is-Purelib: true" not in wheel_metadata or "Tag: py3-none-any" not in wheel_metadata:
                raise BuildError("The application wheel metadata does not declare a pure wheel.")
            entry_points = archive.read(entry_names[0]).decode("utf-8")
            if "siteops = siteops.cli:main" not in entry_points:
                raise BuildError("The application wheel does not expose the siteops command.")
            init_text = archive.read("siteops/__init__.py").decode("utf-8")
            matches = list(_VERSION_LITERAL.finditer(init_text))
            if len(matches) != 1 or matches[0]["version"] != version:
                raise BuildError("The packaged version does not match the wheel metadata.")
            prefix = metadata_names[0].removesuffix("METADATA")
            for notice in ("LICENSE", "ThirdPartyNotices.txt"):
                packaged = archive.read(prefix + "licenses/" + notice)
                if packaged != (source / notice).read_bytes():
                    raise BuildError("The application wheel does not retain its license notices.")
    except BuildError:
        raise
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise BuildError("The application wheel could not be inspected.") from error
    return hashlib.sha256(content).hexdigest()


def _download_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PIP_NO_INPUT": "1",
            "PIP_KEYRING_PROVIDER": "disabled",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    environment.pop("PIP_EXTRA_INDEX_URL", None)
    return environment


def _download_runtime_wheels(lock: Path, destination: Path) -> None:
    destination.mkdir()
    for _, _, python_tag, pip_platform in _TARGETS:
        result = _run(
            [
                sys.executable,
                "-B",
                "-m",
                "pip",
                "download",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-deps",
                "--no-cache-dir",
                "--implementation",
                "cp",
                "--python-version",
                python_tag,
                "--abi",
                f"cp{python_tag}",
                "--platform",
                pip_platform,
                "--dest",
                str(destination),
                "--requirement",
                str(lock),
            ],
            cwd=destination.parent,
            environment=_download_environment(),
            timeout=180,
        )
        if result.returncode != 0:
            raise BuildError("A required runtime wheel is unavailable from the configured package feed.")


def _regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BuildError(f"The {label} could not be accessed.") from error
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or reparse or not stat.S_ISREG(info.st_mode):
        raise BuildError(f"The {label} must be a regular file.")
    if getattr(info, "st_nlink", 1) != 1:
        raise BuildError(f"The {label} must not be linked.")


def _runtime_wheel(path: Path, locks: tuple[LockedRequirement, ...]) -> RuntimeWheel:
    _regular_file(path, "runtime wheel")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    matching_locks = [requirement for requirement in locks if digest in requirement.hashes]
    if len(matching_locks) != 1:
        raise BuildError("A runtime wheel does not match the committed hash pins.")
    requirement = matching_locks[0]
    try:
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.tags import parse_tag
        from packaging.utils import canonicalize_name, parse_wheel_filename
        from packaging.version import Version
    except ImportError as error:
        raise BuildError("The committed build requirements are not installed.") from error
    try:
        name, version, build, tags = parse_wheel_filename(path.name)
    except ValueError as error:
        raise BuildError("A runtime wheel has an invalid filename.") from error
    if (
        canonicalize_name(str(name)) != requirement.name
        or str(version) != requirement.version
        or build
    ):
        raise BuildError("A runtime wheel identity does not match its locked requirement.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise BuildError("A runtime wheel must contain one package metadata record.")
            prefix = metadata_names[0].removesuffix("METADATA")
            if archive.namelist().count(prefix + "RECORD") != 1:
                raise BuildError("A runtime wheel must contain one matching RECORD file.")
            if archive.testzip() is not None:
                raise BuildError("A runtime wheel contains corrupt archive members.")
            metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
            wheel_metadata = email.parser.BytesParser().parsebytes(archive.read(prefix + "WHEEL"))
            if (
                canonicalize_name(metadata["Name"] or "") != requirement.name
                or metadata["Version"] != requirement.version
            ):
                raise BuildError("Runtime wheel metadata does not match its locked identity.")
            declared_tags = {
                tag for value in wheel_metadata.get_all("Tag", []) for tag in parse_tag(value)
            }
            if declared_tags != set(tags):
                raise BuildError("Runtime wheel tags do not match its filename.")
            requires_python = metadata["Requires-Python"] or ""
            SpecifierSet(requires_python)
            locked = {item.name: item for item in locks}
            dependencies = []
            for value in metadata.get_all("Requires-Dist", []):
                dependency = Requirement(value)
                selected = locked.get(canonicalize_name(dependency.name))
                if (
                    dependency.url or dependency.extras or dependency.marker
                    or selected is None
                    or Version(selected.version) not in dependency.specifier
                ):
                    raise BuildError(
                        "Runtime wheel dependencies must be satisfied by unconditional "
                        "entries in the runtime lock."
                    )
                dependencies.append(selected.name)
    except (KeyError, ValueError, zipfile.BadZipFile) as error:
        raise BuildError("A runtime wheel metadata record could not be inspected.") from error
    return RuntimeWheel(
        path=path,
        name=requirement.name,
        version=requirement.version,
        sha256=digest,
        tags=frozenset(tags),
        requires_python=requires_python,
        dependencies=tuple(dependencies),
    )


def _matches_target(wheel: RuntimeWheel, python_tag: str, platform_name: str) -> bool:
    from packaging.tags import compatible_tags, cpython_tags

    python_version = (int(python_tag[0]), int(python_tag[1:]))
    platforms = (
        ["win_amd64"] if platform_name == "windows-x86_64"
        else ["manylinux2014_x86_64", "manylinux_2_17_x86_64"]
    )
    supported = {
        *cpython_tags(python_version, abis=[f"cp{python_tag}"], platforms=platforms),
        *compatible_tags(python_version, interpreter=f"cp{python_tag}", platforms=platforms),
    }
    return bool(wheel.tags & supported)


def _collect_runtime_wheels(
    wheelhouse: Path,
    locks: tuple[LockedRequirement, ...],
) -> dict[tuple[str, str], tuple[RuntimeWheel, ...]]:
    from packaging.specifiers import SpecifierSet

    try:
        info = wheelhouse.lstat()
        entries = list(wheelhouse.iterdir())
    except OSError as error:
        raise BuildError("The runtime wheelhouse could not be read.") from error
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(info.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise BuildError("The runtime wheelhouse must be a regular directory.")
    if any(entry.suffix.lower() != ".whl" for entry in entries):
        raise BuildError("The runtime wheelhouse may contain only wheel files.")
    wheels = [_runtime_wheel(entry, locks) for entry in sorted(entries)]
    selected: dict[tuple[str, str], tuple[RuntimeWheel, ...]] = {}
    used: set[Path] = set()
    for python, platform_name, python_tag, _ in _TARGETS:
        target_wheels = []
        for requirement in locks:
            matches = [
                wheel for wheel in wheels
                if wheel.name == requirement.name
                and _matches_target(wheel, python_tag, platform_name)
                and python in SpecifierSet(wheel.requires_python)
            ]
            if len(matches) != 1:
                raise BuildError(
                    f"The runtime wheelhouse must contain one {requirement.name} wheel "
                    f"for Python {python} on {platform_name}."
                )
            target_wheels.append(matches[0])
            used.add(matches[0].path)
        selected[(python, platform_name)] = tuple(target_wheels)
    if used != {wheel.path for wheel in wheels}:
        raise BuildError("The runtime wheelhouse contains an unsupported or duplicate wheel.")
    expected_names = {requirement.name for requirement in locks}
    if {wheel.name for wheel in wheels} != expected_names:
        raise BuildError("The runtime wheelhouse does not cover every locked dependency.")
    return selected


def _validate_runtime_closure(
    source_dependencies: frozenset[str],
    targets: dict[tuple[str, str], tuple[RuntimeWheel, ...]],
) -> None:
    for wheels in targets.values():
        dependencies = {wheel.name: wheel.dependencies for wheel in wheels}
        pending = list(source_dependencies)
        reachable: set[str] = set()
        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            if name not in dependencies:
                raise BuildError("A source dependency is missing from the target wheel set.")
            reachable.add(name)
            pending.extend(dependencies[name])
        if reachable != set(dependencies):
            unused = ", ".join(sorted(set(dependencies) - reachable))
            raise BuildError(
                "The runtime lock contains packages outside the source dependency closure: " + unused + "."
            )


def _copy_regular(
    source: Path, destination: Path, label: str, *, expected_sha256: str | None = None,
) -> None:
    _regular_file(source, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            digest = hashlib.sha256()
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
                digest.update(chunk)
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise BuildError(f"The {label} changed after verification.")
    except OSError as error:
        raise BuildError(f"The {label} could not be added to the bundle.") from error


def _payload_files(root: Path) -> tuple[PayloadFile, ...]:
    files: list[PayloadFile] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "bundle.json":
            continue
        _regular_file(path, "bundle payload file")
        content = path.read_bytes()
        files.append(
            PayloadFile(
                path=relative,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        )
    return tuple(files)


def _write_pylock(root: Path, targets: tuple[BundleTarget, ...]) -> None:
    from packaging.utils import parse_wheel_filename

    versions = sorted({tuple(map(int, target.python.split("."))) for target in targets})
    minimum, maximum = versions[0], versions[-1]
    environments = []
    for target in targets:
        system, machine = (
            ("win32", "AMD64") if target.platform == "windows-x86_64"
            else ("linux", "x86_64")
        )
        environments.append(
            f"implementation_name == 'cpython' and python_version == '{target.python}' "
            f"and sys_platform == '{system}' and platform_machine == '{machine}'"
        )
    lines = [
        'lock-version = "1.0"',
        'created-by = "siteops"',
        f'requires-python = ">={minimum[0]}.{minimum[1]},<{maximum[0]}.{maximum[1] + 1}"',
        "environments = " + json.dumps(environments),
    ]
    packages: dict[tuple[str, str], list[PayloadFile]] = {}
    for entry in _payload_files(root):
        if not entry.path.endswith(".whl"):
            continue
        name, version, _, _ = parse_wheel_filename(Path(entry.path).name)
        packages.setdefault((str(name), str(version)), []).append(entry)
    for (name, version), wheels in sorted(packages.items()):
        lines.extend([
            "", "[[packages]]", "name = " + json.dumps(name),
            "version = " + json.dumps(version),
        ])
        for wheel in wheels:
            lines.extend([
                "[[packages.wheels]]",
                "name = " + json.dumps(Path(wheel.path).name),
                "path = " + json.dumps(wheel.path),
                "size = " + str(wheel.size),
                "[packages.wheels.hashes]",
                "sha256 = " + json.dumps(wheel.sha256),
            ])
    _write_utf8(root / "pylock.toml", "\n".join(lines) + "\n", "installation lock")


def _assemble_bundle(
    *,
    source: Path,
    bundle_root: Path,
    application_wheel: Path,
    application_sha256: str,
    runtime_wheels: dict[tuple[str, str], tuple[RuntimeWheel, ...]],
    repository: str,
    source_sha: str,
    source_ref: str,
    build_number: int,
    build_attempt: int,
    base_version: str,
    version: str,
) -> BundleManifest:
    bundle_root.mkdir()
    for source_path, destination in (
        (source / "LICENSE", bundle_root / "LICENSE"),
        (source / "ThirdPartyNotices.txt", bundle_root / "ThirdPartyNotices.txt"),
    ):
        _copy_regular(source_path, destination, "required bundle source file")
    app_destination = bundle_root / "wheels" / application_wheel.name
    _copy_regular(
        application_wheel, app_destination, "application wheel",
        expected_sha256=application_sha256,
    )

    dependency_paths: dict[tuple[str, str], tuple[str, ...]] = {}
    copied_dependencies: set[Path] = set()
    for key, wheels in runtime_wheels.items():
        paths = []
        for wheel in wheels:
            destination = bundle_root / "wheels" / wheel.path.name
            if wheel.path not in copied_dependencies:
                _copy_regular(
                    wheel.path, destination, "runtime wheel", expected_sha256=wheel.sha256,
                )
                copied_dependencies.add(wheel.path)
            paths.append(destination.relative_to(bundle_root).as_posix())
        dependency_paths[key] = tuple(paths)

    application_path = app_destination.relative_to(bundle_root).as_posix()
    targets = tuple(
        BundleTarget(
            python=python,
            platform=platform_name,
            wheels=(application_path, *dependency_paths[(python, platform_name)]),
        )
        for python, platform_name, _, _ in _TARGETS
    )
    _write_pylock(bundle_root, targets)
    manifest = BundleManifest(
        version=version,
        base_version=base_version,
        repository=repository,
        source_sha=source_sha,
        source_ref=source_ref,
        build_number=build_number,
        build_attempt=build_attempt,
        application_wheel=application_path,
        targets=targets,
        files=_payload_files(bundle_root),
    )
    manifest = BundleManifest.from_dict(manifest.to_dict())
    try:
        (bundle_root / "bundle.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as error:
        raise BuildError("The bundle manifest could not be written.") from error
    verify_payload(bundle_root, manifest)
    return manifest


def _write_deterministic_zip(bundle_root: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(
            destination,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            paths = (item for item in bundle_root.rglob("*") if item.is_file())
            for path in sorted(
                paths,
                key=lambda item: item.relative_to(bundle_root).as_posix(),
            ):
                relative = path.relative_to(bundle_root).as_posix()
                _regular_file(path, "bundle payload file")
                archive.writestr(_fixed_zip_info(relative), path.read_bytes())
    except (OSError, zipfile.BadZipFile) as error:
        raise BuildError("The installation ZIP could not be created.") from error


def _publish(staged_file: Path, output: Path, expected_sha256: str) -> None:
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError as error:
        raise BuildError("The output path already exists.") from error
    except OSError as error:
        raise BuildError("The output path could not be created.") from error
    created = True
    try:
        with os.fdopen(descriptor, "wb") as destination, staged_file.open("rb") as source:
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise BuildError("An output artifact changed after verification.")
        created = False
    finally:
        if created:
            try:
                output.unlink()
            except OSError:
                print("Warning: the incomplete output could not be removed.", file=sys.stderr)


def _publish_artifacts(staged_zip: Path, staged_wheel: Path, output: Path, wheel_sha256: str) -> None:
    destinations = (output, output.with_name(staged_wheel.name))
    if any(path.exists() or path.is_symlink() for path in destinations):
        raise BuildError("An output artifact path already exists.")
    created: list[Path] = []
    complete = False
    try:
        for source, destination, digest in (
            (staged_zip, destinations[0], hashlib.sha256(staged_zip.read_bytes()).hexdigest()),
            (staged_wheel, destinations[1], wheel_sha256),
        ):
            _publish(source, destination, digest)
            created.append(destination)
        complete = True
    finally:
        if not complete:
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    print("Warning: an incomplete artifact set could not be removed.", file=sys.stderr)


def produce_bundle(
    *,
    root: Path,
    repository: str,
    source_ref: str,
    expected_source_sha: str,
    build_number: int,
    build_attempt: int,
    output: Path,
    wheelhouse: Path | None,
    download_dependencies: bool,
    version_mode: str = "build",
) -> None:
    """Produce a wheel and verified ZIP beside it from an exact clean Git checkout."""
    if output.name != _OUTPUT_NAME:
        raise BuildError(f"The output filename must be {_OUTPUT_NAME}.")
    if output.exists() or output.is_symlink():
        raise BuildError("The output path already exists.")
    if not output.parent.is_dir():
        raise BuildError("The output parent directory does not exist.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BuildError("--repository must use the OWNER/REPO form.")
    if not source_ref or len(source_ref) > 1024 or any(ord(value) < 32 for value in source_ref):
        raise BuildError("--source-ref must be a non-empty ref name.")
    if (
        not 1 <= build_number <= 2**63 - 1
        or not 1 <= build_attempt <= 2**63 - 1
    ):
        raise BuildError("Build number and attempt must be positive bounded integers.")
    if (wheelhouse is None) == (not download_dependencies):
        raise BuildError("Select exactly one runtime wheel source mode.")

    _validate_repository(root, expected_source_sha)

    with tempfile.TemporaryDirectory(prefix=".siteops-bundle-", dir=output.parent) as temporary:
        staging = Path(temporary)
        source = staging / "source"
        _export_tracked_source(root, source, expected_source_sha)
        runtime_lock_path = source / "scripts" / _RUNTIME_LOCK
        build_lock_path = source / "scripts" / _BUILD_LOCK
        runtime_requirements = _read_locked_requirements(runtime_lock_path)
        build_requirements = _read_locked_requirements(build_lock_path)
        _validate_build_environment(build_requirements)
        base_version, version, source_dependencies = _derive_staged_source(
            source,
            build_number=build_number,
            build_attempt=build_attempt,
            source_sha=expected_source_sha,
            runtime_requirements=runtime_requirements,
            version_mode=version_mode,
        )
        dependency_source = staging / "runtime-wheels" if download_dependencies else wheelhouse
        if dependency_source is None:
            raise BuildError("A runtime wheel source was not selected.")
        if download_dependencies:
            _download_runtime_wheels(runtime_lock_path, dependency_source)
        runtime_wheels = _collect_runtime_wheels(dependency_source, runtime_requirements)
        _validate_runtime_closure(source_dependencies, runtime_wheels)
        application_wheel = _build_application_wheel(source, staging / "application-wheel")
        application_sha256 = _inspect_application_wheel(
            application_wheel,
            source=source,
            version=version,
            runtime_requirements=runtime_requirements,
        )
        bundle_root = staging / "bundle"
        _assemble_bundle(
            source=source,
            bundle_root=bundle_root,
            application_wheel=application_wheel,
            application_sha256=application_sha256,
            runtime_wheels=runtime_wheels,
            repository=repository,
            source_sha=expected_source_sha,
            source_ref=source_ref,
            build_number=build_number,
            build_attempt=build_attempt,
            base_version=base_version,
            version=version,
        )
        staged_zip = staging / _OUTPUT_NAME
        _write_deterministic_zip(bundle_root, staged_zip)
        _publish_artifacts(
            staged_zip, bundle_root / "wheels" / application_wheel.name,
            output, application_sha256,
        )


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a Site Ops wheel and installation ZIP from an exact clean checkout.",
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--build-number", required=True, type=_positive)
    parser.add_argument("--build-attempt", required=True, type=_positive)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version-mode", choices=("build", "source"), default="build")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheelhouse", type=Path)
    source.add_argument("--download-dependencies", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        produce_bundle(
            root=root,
            repository=args.repository,
            source_ref=args.source_ref,
            expected_source_sha=args.expected_source_sha,
            build_number=args.build_number,
            build_attempt=args.build_attempt,
            output=args.output.absolute(),
            wheelhouse=args.wheelhouse.absolute() if args.wheelhouse else None,
            download_dependencies=args.download_dependencies,
            version_mode=args.version_mode,
        )
    except (BuildError, DistributionError) as error:
        print(f"Bundle production failed: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Bundle production failed because a required file could not be accessed.", file=sys.stderr)
        return 1
    print(f"Created {_OUTPUT_NAME} and its identical standalone Site Ops wheel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Readonly native fixtures copied into one test-owned uv qualification area."""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

UV_DIGESTS = {
    "win32": "a0d2742d49564a32488753b02e76276e7b5ef1b1ea8cf30bcbf06ee28f60cd73",
    "linux": "b8299463da6fa7da3b94464444d252d0afca8ac6c96cb229f1baf4012f365246",
}
LINUX_PYTHON_SHA256 = "68c6739376b65258dee5058ccf6777232fe38d31a578965ae8bda327ec7da3a8"
LINUX_PYTHON_SIZE = 30778566
LINUX_UV_ARCHIVE_SHA256 = "6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f"
LINUX_UV_ARCHIVE_SIZE = 19827214


def _required() -> bool:
    gate = "SITEOPS_REQUIRE_WINDOWS_UV" if os.name == "nt" else "SITEOPS_REQUIRE_LINUX_UV"
    return bool(os.environ.get("CI") or os.environ.get(gate) == "1")


def _unavailable(message: str) -> None:
    if _required():
        pytest.fail(message)
    pytest.skip(message)


def native_uv() -> Path:
    selected = os.environ.get("SITEOPS_TEST_UV")
    if not selected and not _required():
        selected = shutil.which("uv")
    if not selected:
        _unavailable("SITEOPS_TEST_UV must name the qualified native uv executable.")
    executable = Path(selected)
    expected = UV_DIGESTS.get(sys.platform)
    size = 42547504 if os.name == "nt" else 50437584
    if (not expected or not executable.is_file() or executable.stat().st_size != size
            or hashlib.sha256(executable.read_bytes()).hexdigest() != expected):
        _unavailable("SITEOPS_TEST_UV differs from the qualified native uv 0.12.20 build.")
    return executable.resolve()


def linux_archives() -> tuple[Path, Path]:
    if sys.platform != "linux":
        pytest.skip("Native published-action coverage requires Linux.")
    for tool in ("bash", "tar"):
        if shutil.which(tool) is None:
            _unavailable(f"Native published-action coverage requires {tool}.")
    selected = []
    for name, size, digest in (
        ("SITEOPS_TEST_UV_ARCHIVE", LINUX_UV_ARCHIVE_SIZE, LINUX_UV_ARCHIVE_SHA256),
        ("SITEOPS_TEST_UV_PYTHON_ARCHIVE", LINUX_PYTHON_SIZE, LINUX_PYTHON_SHA256),
    ):
        value = os.environ.get(name)
        archive = Path(value) if value else None
        if (archive is None or not archive.is_file() or archive.stat().st_size != size
                or hashlib.sha256(archive.read_bytes()).hexdigest() != digest):
            _unavailable(f"{name} must name the pinned native Linux archive.")
        selected.append(archive)
    return selected[0], selected[1]


def managed_python(destination: Path) -> Path:
    suffix = "windows-x86_64-none" if os.name == "nt" else "linux-x86_64-gnu"
    concrete = f"cpython-3.11.16-{suffix}"
    copied = destination / "python" / concrete
    local = os.environ.get("SITEOPS_TEST_PYTHON_ROOT") if not _required() else None
    if os.name == "nt":
        selected = os.environ.get("SITEOPS_TEST_UV_PYTHON_DIR")
        source = Path(selected) / concrete if selected else Path(local) if local else None
        if source is None or source.name != concrete or not (source / "python.exe").is_file():
            _unavailable("SITEOPS_TEST_UV_PYTHON_DIR must contain managed CPython 3.11.16.")
        shutil.copytree(source, copied, symlinks=True)
        executable = copied / "python.exe"
    else:
        selected = os.environ.get("SITEOPS_TEST_UV_PYTHON_ARCHIVE")
        archive = Path(selected) if selected else None
        if archive is None and local:
            source = Path(local)
            if source.name != concrete or not (source / "bin/python3.11").is_file():
                _unavailable("SITEOPS_TEST_PYTHON_ROOT must contain managed CPython 3.11.16.")
            shutil.copytree(source, copied, symlinks=True)
        else:
            if (archive is None or not archive.is_file()
                    or archive.stat().st_size != LINUX_PYTHON_SIZE
                    or hashlib.sha256(archive.read_bytes()).hexdigest() != LINUX_PYTHON_SHA256):
                _unavailable("SITEOPS_TEST_UV_PYTHON_ARCHIVE differs from pinned CPython 3.11.16.")
            staging = destination / "managed-python-stage"
            staging.mkdir()
            try:
                with tarfile.open(archive, "r:gz") as package:
                    members = package.getmembers()
                    if (not 1 <= len(members) <= 16384
                            or sum(item.size for item in members) > 512 * 1024 * 1024
                            or any(item.name != "python" and not item.name.startswith("python/")
                                   for item in members)):
                        pytest.fail("The pinned managed Python archive has an invalid inventory.")
                    package.extractall(staging, filter="data")
                copied.parent.mkdir()
                (staging / "python").rename(copied)
                staging.rmdir()
            except (OSError, tarfile.TarError):
                pytest.fail("The pinned managed Python archive could not be extracted.")
        executable = copied / "bin/python3.11"
    if not executable.is_file():
        pytest.fail("The copied managed Python executable is missing.")
    identity = subprocess.run(
        [str(executable), "-I", "-S", "-B", "-c",
         "import sys;print(sys.implementation.name, *sys.version_info[:3], "
         "sys.maxsize > 2**32, sys._base_executable)"],
        capture_output=True, text=True, timeout=20,
    )
    if identity.returncode or identity.stdout.strip() != f"cpython 3 11 16 True {executable}":
        pytest.fail("The copied managed Python does not have the qualified runtime identity.")
    return executable

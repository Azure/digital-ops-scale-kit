"""Exercise the built wheel rather than only its source metadata."""

import configparser
import email
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from siteops import __version__

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, supplied by pytest's dependencies.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("package-build")
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "ThirdPartyNotices.txt"):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(
        ROOT / "siteops", source / "siteops",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    environment = {
        **os.environ,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable, "-B", "-m", "pip", "wheel", "--no-index",
            "--no-deps", "--no-build-isolation", str(source),
            "--wheel-dir", str(root / "wheels"),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((root / "wheels").glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_build_backend_requires_spdx_metadata_support():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    backend = next(
        requirement
        for value in project["build-system"]["requires"]
        if (requirement := Requirement(value)).name == "setuptools"
    )
    assert "65.5.0" not in backend.specifier
    assert "77.0.3" in backend.specifier


def test_wheel_contains_the_engine_not_a_workspace(built_wheel):
    with zipfile.ZipFile(built_wheel) as wheel:
        names = wheel.namelist()
    assert "siteops/cli.py" in names
    assert "siteops/orchestrator.py" in names
    assert "siteops/results.py" in names
    assert all(
        name.startswith("siteops/")
        or name.startswith(f"siteops-{__version__}.dist-info/")
        for name in names
    )


def test_wheel_preserves_metadata_and_entry_point(built_wheel):
    with zipfile.ZipFile(built_wheel) as wheel:
        prefix = f"siteops-{__version__}.dist-info/"
        metadata = email.message_from_bytes(wheel.read(prefix + "METADATA"))
        entry_points = configparser.ConfigParser()
        entry_points.read_string(wheel.read(prefix + "entry_points.txt").decode("utf-8"))
    assert metadata["Name"] == "siteops"
    assert metadata["Version"] == __version__
    assert metadata["Requires-Python"] == ">=3.10"
    assert metadata["License-Expression"] == "MIT"
    assert entry_points["console_scripts"]["siteops"] == "siteops.cli:main"


def test_wheel_retains_both_license_notices(built_wheel):
    with zipfile.ZipFile(built_wheel) as wheel:
        prefix = f"siteops-{__version__}.dist-info/"
        metadata = email.message_from_bytes(wheel.read(prefix + "METADATA"))
        assert set(metadata.get_all("License-File", [])) == {
            "LICENSE", "ThirdPartyNotices.txt",
        }
        for name in ("LICENSE", "ThirdPartyNotices.txt"):
            assert wheel.read(prefix + "licenses/" + name) == (ROOT / name).read_bytes()

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
from tests.installed_runtime import install_engine, isolated_environment

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
        **isolated_environment(root / "build-state"),
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
    runtime = {
        Requirement(value).name.lower()
        for value in metadata.get_all("Requires-Dist", [])
        if Requirement(value).marker is None
    }
    assert runtime == {"pyyaml", "packaging"}
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


def test_third_party_license_blocks_remain_contiguous():
    text = (ROOT / "ThirdPartyNotices.txt").read_text(encoding="utf-8")
    pyyaml, packaging = text.split("License notice for packaging,", 1)
    assert "Permission is hereby granted" in pyyaml
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in pyyaml
    assert "CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE." in pyyaml
    assert "Copyright (c) Donald Stufft" in packaging
    assert "THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS" in packaging


@pytest.fixture(scope="module")
def installed_engine(built_wheel, tmp_path_factory):
    return install_engine(tmp_path_factory.mktemp("installed-engine"), built_wheel)


def test_installed_project_cache_and_offline_plan_surface(installed_engine):
    import json

    app = installed_engine
    prepared = subprocess.run(
        [str(app.python), "-I", str(ROOT / "tests" / "fixtures" / "prepare_installed_project.py"), str(app.root)],
        cwd=app.root / "unrelated", env=app.environment, capture_output=True, text=True, timeout=120,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    identities = json.loads(prepared.stdout)
    for tool, arguments in (("az", ["deployment", "group", "create"]), ("gh", ["auth", "token"])):
        name = tool + (".exe" if os.name == "nt" else "")
        refused = subprocess.run(
            [str(app.root / "tools" / name), *arguments],
            cwd=app.root / "unrelated", env=app.environment,
            capture_output=True, text=True, timeout=30,
        )
        assert refused.returncode != 0
    project = app.root / "project"
    site = project / "sites" / "one.yaml"
    before = site.read_bytes(), (project / "siteops.pin").read_bytes()
    (app.root / "workspace.zip").unlink()
    (app.root / "proof.jsonl").unlink()
    shutil.rmtree(app.root / "authored")
    (app.root / "unrelated" / "siteops.py").write_text("raise AssertionError('cwd code imported')\n")
    project_options = [
        "--project", str(project), "--trust-policy", str(app.root / "policy.json"),
        "--trusted-root", str(app.root / "trusted-root.json"),
    ]
    assert "--release-workspace" in app.run("project", "pin", "--help").stdout
    shown = json.loads(app.run("project", "show", str(project), "--output", "json").stdout)
    assert shown["content"]["package"]["sha256"] == identities["package"]
    sites = json.loads(app.run("--project", str(project), "sites", "--output", "json").stdout)
    assert sites[0]["subscription"] == "fixture-subscription"
    preview = json.loads(app.run(*project_options, "browse", "storage", "--offline", "--output", "json").stdout)
    assert preview["source"]["verification"] == "verified"
    app.run(*project_options, "validate", "storage", "--offline")
    for command in (["plan", "storage"], ["deploy", "storage", "--dry-run"]):
        result = json.loads(app.run(*project_options, *command, "--offline", "--output", "json").stdout)
        assert result["status"] == "planned"
    assert (site.read_bytes(), (project / "siteops.pin").read_bytes()) == before
    inventory = json.loads(app.run("cache", "list", "--output", "json").stdout)
    assert {entry["kind"] for entry in inventory["entries"]} == {"package", "proof"}
    removed = json.loads(app.run("cache", "remove", "proof", identities["proof"], "--output", "json").stdout)
    assert removed["entry"]["storageState"] == "removed"
    failed = app.run(*project_options, "plan", "storage", "--offline", expected=1)
    assert "pinned proof is not cached" in failed.stderr
    assert (site.read_bytes(), (project / "siteops.pin").read_bytes()) == before


def test_installed_worker_is_present_and_uses_its_fixed_protocol(installed_engine):
    import json

    app = installed_engine
    location = subprocess.run(
        [str(app.python), "-I", "-c", "from siteops import _http_asset_worker; print(_http_asset_worker.__file__)"],
        cwd=app.root / "unrelated", env=app.environment, capture_output=True, text=True, timeout=30,
    )
    assert location.returncode == 0, location.stderr
    worker = Path(location.stdout.strip())
    assert worker.is_relative_to(app.root / "application")
    result = subprocess.run(
        [str(app.python), "-I", "-S", str(worker)],
        cwd=app.root / "unrelated", env=app.environment,
        input=b'{"unsupported":"request"}', capture_output=True, timeout=30,
    )
    assert result.returncode == 1 and result.stderr == b""
    assert json.loads(result.stdout) == {"status": "input"}


def test_installed_command_environment_excludes_ambient_credentials(tmp_path, monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH"):
        monkeypatch.setenv(name, "test-only-ambient-value")
    environment = isolated_environment(tmp_path / "owned")
    assert all("test-only-ambient-value" not in value for value in environment.values())
    assert all(name not in environment for name in (
        "GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
    ))
    assert Path(environment["AZURE_CONFIG_DIR"]).is_relative_to(tmp_path)
    assert Path(environment["GH_CONFIG_DIR"]).is_relative_to(tmp_path)

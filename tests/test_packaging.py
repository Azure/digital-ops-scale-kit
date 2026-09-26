"""Exercise the built wheel rather than only its source metadata."""

import configparser
import email
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

from siteops import __version__
from tests.installed_runtime import build_engine_wheel, install_engine, isolated_environment

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, supplied by pytest's dependencies.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("package-build")
    return build_engine_wheel(root)


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
    assert "siteops/arm_resources.py" in names
    assert "siteops/arm_resources_azure_cli.py" in names
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
    assert "selected proof is not cached" in failed.stderr
    assert (site.read_bytes(), (project / "siteops.pin").read_bytes()) == before


def test_installed_engine_prepares_a_guided_aio_target(installed_engine):
    workspace = ROOT / "workspaces" / "iot-operations"
    app = installed_engine
    command = ("-w", str(workspace))
    inspected = json.loads(app.run(*command, "inputs", "aio-install", "--output", "json").stdout)
    assert next(field for field in inspected["inputs"] if field["name"] == "clusterName")[
        "status"
    ] == "required"

    planned = json.loads(app.run(
        *command, "plan", "aio-install", "--describe",
        "--input", "siteName=plant-one",
        "--input", "subscription=00000000-0000-0000-0000-000000000001",
        "--input", "resourceGroup=rg-existing",
        "--input", "location=eastus",
        "--input", "clusterName=existing-arc",
        "--input", "environment=dev",
        "--input", "country=US",
        "--output", "json",
    ).stdout)
    assert planned["status"] == "planned"
    assert [target["name"] for target in planned["plan"]["targets"]] == ["plant-one"]
    invalid = app.run(
        *command, "plan", "aio-install", "--describe",
        "--input", "cluster=not-an-arm-id", "--read-resources", "--output", "json",
        expected=1,
    )
    assert json.loads(invalid.stdout)["diagnostics"][0]["code"] == "inputs.resource.invalid-id"


@pytest.fixture(scope="module")
def installed_acquired_aio(built_wheel, tmp_path_factory):
    app = install_engine(tmp_path_factory.mktemp("acquired-aio"), built_wheel)
    prepared = subprocess.run(
        [
            str(app.python), "-I",
            str(ROOT / "tests" / "fixtures" / "prepare_installed_project.py"),
            str(app.root), str(ROOT / "workspaces" / "iot-operations"),
        ],
        cwd=app.root / "unrelated", env=app.environment,
        capture_output=True, text=True, timeout=120,
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    (app.root / "workspace.zip").unlink()
    (app.root / "proof.jsonl").unlink()
    shutil.rmtree(app.root / "controlled-build")
    (app.root / "unrelated" / "siteops.py").write_text(
        "raise AssertionError('Imported cwd engine')\n", encoding="utf-8",
    )
    return app


def test_installed_acquired_aio_manual_inputs_and_configured_site(installed_acquired_aio):
    app = installed_acquired_aio
    project = app.root / "project"
    options = (
        "--project", str(project),
        "--trust-policy", str(app.root / "policy.json"),
        "--trusted-root", str(app.root / "trusted-root.json"),
    )
    manual = {
        "siteName": "plant-one",
        "subscription": "00000000-0000-0000-0000-000000000001",
        "resourceGroup": "rg-existing",
        "location": "eastus",
        "clusterName": "existing-arc",
        "environment": "dev",
        "country": "US",
    }
    answers = app.root / "manual-inputs.yaml"
    answers.write_text(
        yaml.safe_dump({
            "apiVersion": "siteops.inputs/v1",
            "kind": "SiteInputValues",
            "values": manual,
        }),
        encoding="utf-8",
    )
    inspected = json.loads(app.run(
        *options, "inputs", "aio-install", "--offline", "--output", "json",
    ).stdout)
    assert {field["name"] for field in inspected["inputs"]} >= set(manual)
    app.run(
        *options, "validate", "aio-install", "--input-file", str(answers), "--offline",
    )
    file_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", "--input-file", str(answers),
        "--offline", "--output", "json",
    ).stdout)
    inline = [item for name, value in manual.items() for item in ("--input", f"{name}={value}")]
    inline_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline,
        "--offline", "--output", "json",
    ).stdout)
    assert file_plan["status"] == inline_plan["status"] == "planned"
    assert file_plan["plan"]["targets"] == inline_plan["plan"]["targets"]
    assert [target["name"] for target in file_plan["plan"]["targets"]] == ["plant-one"]
    saved = project / "sites" / "plant-one.yaml"
    app.run(
        *options, "inputs", "aio-install", "--input-file", str(answers),
        "--save-site", str(saved), "--offline",
    )
    assert saved.is_file()
    for selection in (
        ("--site-file", str(saved)),
        ("-l", "name=plant-one"),
    ):
        selected = json.loads(app.run(
            *options, "plan", "aio-install", "--describe", *selection,
            "--offline", "--output", "json",
        ).stdout)
        assert selected["status"] == "planned"
        assert [target["name"] for target in selected["plan"]["targets"]] == ["plant-one"]

    older = {
        **manual,
        "siteName": "plant-two",
        "resourceGroup": "rg-second",
        "clusterName": "arc-second",
        "aioRelease": "2607",
    }
    inline_older = [
        item for name, value in older.items() for item in ("--input", f"{name}={value}")
    ]
    preview = json.loads(app.run(
        *options, "inputs", "aio-install", *inline_older, "--offline", "--output", "json",
    ).stdout)
    assert preview["resolution"]["site"]["properties"]["aioRelease"] == "2607"
    older_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline_older,
        "--offline", "--output", "json",
    ).stdout)
    assert older_plan["status"] == "planned"
    assert [target["name"] for target in older_plan["plan"]["targets"]] == ["plant-two"]
    saved_older = project / "sites" / "plant-two.yaml"
    app.run(
        *options, "inputs", "aio-install", *inline_older,
        "--save-site", str(saved_older), "--offline",
    )
    assert yaml.safe_load(saved.read_text(encoding="utf-8"))["properties"]["aioRelease"] == "2608"
    assert yaml.safe_load(saved_older.read_text(encoding="utf-8"))["properties"]["aioRelease"] == "2607"
    fleet = json.loads(app.run(
        *options, "plan", "aio-install", "--describe",
        "-l", "environment=dev", "--offline", "--output", "json",
    ).stdout)
    assert fleet["status"] == "planned"
    assert fleet["summary"]["targetCount"] == 2
    assert {target["name"] for target in fleet["plan"]["targets"]} == {
        "plant-one", "plant-two",
    }
    assert fleet["plan"]["manifest"]["cliSelector"] == "environment=dev"

    cluster = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-existing/providers/Microsoft.Kubernetes/"
        "connectedClusters/existing-arc"
    )
    tool_context = app.root / "tool-context.json"
    context = json.loads(tool_context.read_text(encoding="utf-8"))
    context["allowedRead"] = {
        "id": cluster,
        "apiVersion": "2024-07-15-preview",
        "subscription": manual["subscription"],
    }
    tool_context.write_text(json.dumps(context), encoding="utf-8")
    resource_values = {
        "siteName": "plant-resource",
        "environment": "dev",
        "country": "US",
        "cluster": cluster,
    }
    resource_answers = app.root / "resource-inputs.yaml"
    resource_answers.write_text(yaml.safe_dump({
        "apiVersion": "siteops.inputs/v1",
        "kind": "SiteInputValues",
        "values": resource_values,
    }), encoding="utf-8")
    file_resource_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe",
        "--input-file", str(resource_answers), "--read-resources",
        "--offline", "--output", "json",
    ).stdout)
    inline_resource = [
        item for name, value in resource_values.items()
        for item in ("--input", f"{name}={value}")
    ]
    inline_resource_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline_resource,
        "--read-resources", "--offline", "--output", "json",
    ).stdout)
    assert file_resource_plan["status"] == inline_resource_plan["status"] == "planned"
    assert file_resource_plan["plan"]["targets"] == inline_resource_plan["plan"]["targets"]
    assert [target["name"] for target in file_resource_plan["plan"]["targets"]] == [
        "plant-resource"
    ]


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

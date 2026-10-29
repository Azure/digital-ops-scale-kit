"""Exercise the built wheel rather than only its source metadata."""

import configparser
import email
import json
import os
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

from siteops import __version__
from tests import installed_runtime
from tests.installed_runtime import build_engine_wheel, install_engine, isolated_environment
from tests.yaml_documents import cached_safe_load

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, supplied by pytest's dependencies.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent
pytest_plugins = ("pytester",)


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


def test_both_ci_lint_jobs_run_the_ruff_pinned_by_the_dev_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = [Requirement(value) for value in project["project"]["optional-dependencies"]["dev"]
            if Requirement(value).name == "ruff"]
    assert len(ruff) == 1 and str(ruff[0].specifier).startswith("==")
    command = "ruff check siteops tests scripts"
    github = cached_safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    steps = github["jobs"]["lint"]["steps"]
    setup = [step for step in steps if step.get("uses") == "./.github/actions/setup-siteops"]
    assert [step["with"] for step in setup] == [{"install-dev": "true"}]
    assert [step["run"] for step in steps if "ruff" in step.get("run", "")] == [command]
    ado = cached_safe_load((ROOT / ".pipelines/ci.yaml").read_text(encoding="utf-8"))
    steps = next(job for job in ado["jobs"] if job["job"] == "lint")["steps"]
    setup = [step for step in steps if step.get("template") == "templates/setup-siteops.yaml"]
    assert [step["parameters"] for step in setup] == [{"installDev": True}]
    assert [step["script"] for step in steps if "ruff" in step.get("script", "")] == [command]
    for setup in (".github/actions/setup-siteops/action.yaml", ".pipelines/templates/setup-siteops.yaml"):
        text = (ROOT / setup).read_text(encoding="utf-8")
        assert 'pip install -e ".[dev]"' in text and "ruff" not in text


def test_both_ci_unit_jobs_run_one_selection_and_list_the_slowest_tests():
    github = cached_safe_load((ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    ado = cached_safe_load((ROOT / ".pipelines/ci.yaml").read_text(encoding="utf-8"))
    commands = [
        shlex.split(next(step["run"] for step in github["jobs"]["test"]["steps"] if step.get("name") == "Run unit tests")),
        shlex.split(next(step["script"] for job in ado["jobs"] if job["job"] == "test"
                         for step in job["steps"] if step.get("displayName") == "Run unit tests")),
    ]
    assert commands[0] == commands[1]
    assert commands[0][:5] == ["pytest", "tests/", "-m", "not integration", "-v"]
    assert "--cov=siteops" in commands[0] and "--durations=25" in commands[0]


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
    preview = json.loads(app.run(*project_options, "browse", "storage", "--offline-content", "--output", "json").stdout)
    assert preview["source"]["verification"] == "verified"
    app.run(*project_options, "validate", "storage", "--offline-content")
    for command, intent in (
        (("plan", "storage"), "executable"),
        (("plan", "storage", "--describe"), "describe"),
    ):
        result = json.loads(app.run(*project_options, *command, "--offline-content", "--output", "json").stdout)
        assert result["status"] == "planned"
        assert result["intent"] == intent
    assert (site.read_bytes(), (project / "siteops.pin").read_bytes()) == before
    inventory = json.loads(app.run("cache", "list", "--output", "json").stdout)
    assert {entry["kind"] for entry in inventory["entries"]} == {"package", "proof"}
    removed = json.loads(app.run("cache", "remove", "proof", identities["proof"], "--output", "json").stdout)
    assert removed["entry"]["storageState"] == "removed"
    failed = app.run(*project_options, "plan", "storage", "--offline-content", expected=1)
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
        "--input", "cluster=not-an-arm-id", "--output", "json",
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
        *options, "inputs", "aio-install", "--offline-content", "--output", "json",
    ).stdout)
    assert {field["name"] for field in inspected["inputs"]} >= set(manual)
    app.run(
        *options, "validate", "aio-install", "--input-file", str(answers), "--offline-content",
    )
    file_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", "--input-file", str(answers),
        "--offline-content", "--output", "json",
    ).stdout)
    inline = [item for name, value in manual.items() for item in ("--input", f"{name}={value}")]
    inline_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline,
        "--offline-content", "--output", "json",
    ).stdout)
    assert file_plan["status"] == inline_plan["status"] == "planned"
    assert file_plan["plan"]["targets"] == inline_plan["plan"]["targets"]
    assert [target["name"] for target in file_plan["plan"]["targets"]] == ["plant-one"]
    saved = project / "sites" / "plant-one.yaml"
    app.run(
        *options, "inputs", "aio-install", "--input-file", str(answers),
        "--save-site", str(saved), "--offline-content",
    )
    assert saved.is_file()
    for selection in (
        ("--site-file", str(saved)),
        ("-l", "name=plant-one"),
    ):
        selected = json.loads(app.run(
            *options, "plan", "aio-install", "--describe", *selection,
            "--offline-content", "--output", "json",
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
        *options, "inputs", "aio-install", *inline_older, "--offline-content", "--output", "json",
    ).stdout)
    assert preview["resolution"]["site"]["properties"]["aioRelease"] == "2607"
    older_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline_older,
        "--offline-content", "--output", "json",
    ).stdout)
    assert older_plan["status"] == "planned"
    assert [target["name"] for target in older_plan["plan"]["targets"]] == ["plant-two"]
    saved_older = project / "sites" / "plant-two.yaml"
    app.run(
        *options, "inputs", "aio-install", *inline_older,
        "--save-site", str(saved_older), "--offline-content",
    )
    assert yaml.safe_load(saved.read_text(encoding="utf-8"))["properties"]["aioRelease"] == "2609"
    assert yaml.safe_load(saved_older.read_text(encoding="utf-8"))["properties"]["aioRelease"] == "2607"
    fleet = json.loads(app.run(
        *options, "plan", "aio-install", "--describe",
        "-l", "environment=dev", "--offline-content", "--output", "json",
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
        "--input-file", str(resource_answers),
        "--offline-content", "--output", "json",
    ).stdout)
    inline_resource = [
        item for name, value in resource_values.items()
        for item in ("--input", f"{name}={value}")
    ]
    inline_resource_plan = json.loads(app.run(
        *options, "plan", "aio-install", "--describe", *inline_resource,
        "--offline-content", "--output", "json",
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
    assert worker.is_relative_to(app.application)
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


@pytest.mark.parametrize("change", ["engine = 2  # changed by a test\n", "engine = 2\n"])
def test_shared_application_is_reused_only_while_unchanged_and_complete(tmp_path, monkeypatch, change):
    monkeypatch.setattr(installed_runtime, "_SESSION_APPLICATIONS", {})
    installs = []

    def install():
        application = tmp_path / f"application-{len(installs)}"
        interpreter = installed_runtime._interpreter(application)
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"python")
        (application / "engine.py").write_text("engine = 1\n", encoding="utf-8")
        installs.append(application)
        return application

    first = installed_runtime._shared_application("wheel", install)
    # A bytecode cache written while the engine runs is not a change.
    (first / "__pycache__").mkdir()
    (first / "__pycache__" / "engine.pyc").write_bytes(b"cache")
    assert installed_runtime._shared_application("wheel", install) == first
    assert len(installs) == 1
    # A change of the same size counts too.
    (first / "engine.py").write_text(change, encoding="utf-8")
    with pytest.raises(AssertionError, match="Treat InstalledEngine.application as read-only"):
        installed_runtime._shared_application("wheel", install)
    installed_runtime._interpreter(first).unlink()
    assert installed_runtime._shared_application("wheel", install) == installs[1] != first


def test_parallel_workers_copy_the_controller_wheels_without_building_them(tmp_path, monkeypatch):
    prebuilt = tmp_path / "prebuilt"
    for kind, name in (("engine", "siteops-1.0-py3-none-any.whl"), ("runtime", "pyyaml-6.0-py3-none-any.whl")):
        (prebuilt / kind).mkdir(parents=True)
        (prebuilt / kind / name).write_bytes(kind.encode())
    monkeypatch.setattr(installed_runtime, "_SESSION_FILES", {})
    monkeypatch.setattr(installed_runtime, "_PREBUILT", None)
    monkeypatch.delenv(installed_runtime.RUNTIME_WHEELHOUSE, raising=False)
    # Any build or download would fail.
    monkeypatch.setattr(installed_runtime, "subprocess", None)
    installed_runtime.use_prebuilt(prebuilt)
    for caller in ("first", "second"):
        (tmp_path / caller).mkdir()
        wheel = build_engine_wheel(tmp_path / caller)
        assert wheel == tmp_path / caller / "wheels" / "siteops-1.0-py3-none-any.whl"
        assert wheel.read_bytes() == b"engine"
        runtime = installed_runtime.runtime_wheels(tmp_path / caller)
        assert runtime.parent == tmp_path / caller
        assert [path.read_bytes() for path in runtime.iterdir()] == [b"runtime"]


def test_parallel_controller_leaves_out_wheels_it_cannot_provide(tmp_path, monkeypatch):
    def pip(argv, **_):
        if "download" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        wheels = Path(argv[argv.index("--wheel-dir") + 1])
        wheels.mkdir()
        (wheels / "siteops-1.0-py3-none-any.whl").write_bytes(b"engine")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(installed_runtime, "_SESSION_FILES", {})
    monkeypatch.setattr(installed_runtime, "_PREBUILT", None)
    monkeypatch.delenv(installed_runtime.RUNTIME_WHEELHOUSE, raising=False)
    monkeypatch.setattr(installed_runtime, "subprocess", type("Pip", (), {"run": staticmethod(pip)}))
    installed_runtime.prebuild(tmp_path / "prebuilt")
    assert [path.read_bytes() for path in (tmp_path / "prebuilt" / "engine").iterdir()] == [b"engine"]
    # Each worker then attempts the download itself and reports it where a test needs it.
    assert not (tmp_path / "prebuilt" / "runtime").exists()


def test_parallel_runs_keep_each_listed_module_on_one_worker():
    pytest.importorskip("xdist")
    from tests import conftest

    assert all((ROOT / module).is_file() for module in conftest.SINGLE_WORKER_MODULES)
    grouped, single = "tests/test_packaging.py", "tests/test_cli.py"
    assert grouped in conftest.SINGLE_WORKER_MODULES and single not in conftest.SINGLE_WORKER_MODULES

    class Config:
        option = type("Option", (), {"loadscopereorder": True})

        def getvalue(self, name):
            return {"dist": "load", "tx": ["popen", "popen"]}[name]

    class Node:
        shutting_down = False

        def __init__(self, name):
            self.gateway = type("Gateway", (), {"id": name})
            self.sent = []

        def send_runtest_some(self, indices):
            self.sent.extend(indices)

        def shutdown(self):
            self.shutting_down = True

    scheduler = conftest.pytest_xdist_make_scheduler(config=Config(), log=None)
    collection = [f"{module}::test_{number}" for module in (grouped, single) for number in range(3)]
    nodes = [Node("gw0"), Node("gw1")]
    for node in nodes:
        scheduler.add_node(node)
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    done = set()
    while not scheduler.tests_finished:
        node, index = next((node, index) for node in nodes for index in node.sent if (node, index) not in done)
        done.add((node, index))
        scheduler.mark_test_complete(node, index)
    workers = {
        module: {node.gateway.id for node in nodes for index in node.sent if collection[index].startswith(module)}
        for module in (grouped, single)
    }
    assert sorted(index for node in nodes for index in node.sent) == list(range(6))
    assert len(workers[grouped]) == 1 and len(workers[single]) == 2


@pytest.fixture
def scheduled_suite(pytester, monkeypatch):
    """A small suite whose parallel runs use this repository's scheduler with one grouped module."""
    pytest.importorskip("xdist")
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    pytester.makeini("[pytest]\n")
    pytester.makeconftest(
        "import pytest\n"
        "from tests.conftest import module_group_scheduler\n\n"
        "@pytest.hookimpl(optionalhook=True)\n"
        "def pytest_xdist_make_scheduler(config, log):\n"
        "    return module_group_scheduler(config, log, frozenset({'test_grouped.py'}))\n"
    )
    pytester.makepyfile(test_grouped=(
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', range(4))\n"
        "def test_grouped(n):\n"
        "    pass\n"
    ))
    return pytester


def test_parallel_runs_report_a_crashed_worker_and_run_the_rest(scheduled_suite):
    scheduled_suite.makepyfile(test_single=(
        "import os\n"
        "import time\n"
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', range(24))\n"
        "def test_single(n):\n"
        "    time.sleep(0.05)\n"
        "    if n == 9:\n"
        "        os._exit(3)\n"
    ))
    result = scheduled_suite.runpytest_subprocess("-n", "2", "-p", "no:cacheprovider", timeout=120)
    result.assert_outcomes(passed=27, failed=1)
    result.stdout.fnmatch_lines(["*crashed while running*test_single.py::test_single?9?*"])

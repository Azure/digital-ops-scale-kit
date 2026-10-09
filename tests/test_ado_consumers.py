"""Exercise consumer selection and actual task bodies without deployment or source authority."""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.ado_helpers import TEMPLATES, nodes, step
from tests.installed_runtime import isolated_environment
from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
RESOURCE = {
    "SITEOPS_AUTOMATION_REF": "refs/tags/v1.0.0b7",
    "SITEOPS_AUTOMATION_VERSION": SOURCE,
    "SITEOPS_AUTOMATION_REPOSITORY": "example/content",
    "SITEOPS_AUTOMATION_PROVIDER": "GitHub",
}


@pytest.fixture
def installer(monkeypatch):
    spec = importlib.util.spec_from_file_location("consumer_installer", ROOT / "scripts/install-siteops-consumer.py")
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unexpected live process."))
    return helper


@pytest.mark.parametrize("release", ["v1.0.0b7", "siteops/v1.2.3"])
@pytest.mark.parametrize("explicit", [False, True])
def test_selection_is_exact_without_a_compatible_version_search(installer, release, explicit):
    environment = (
        {"SITEOPS_RELEASE": release, "SITEOPS_SOURCE_COMMIT": SOURCE, "SITEOPS_REPOSITORY": "example/content"}
        if explicit else {**RESOURCE, "SITEOPS_AUTOMATION_REF": "refs/tags/" + release}
    )
    selected = installer.select(environment)
    assert (selected.repository, selected.release, selected.commit) == ("example/content", release, SOURCE)
    assert selected.inferred is not explicit


def test_explicit_source_identity_does_not_depend_on_the_callers_repository(installer):
    selected = installer.select({
        "SITEOPS_RELEASE": "siteops/v1.2.3", "SITEOPS_SOURCE_COMMIT": SOURCE,
        "SITEOPS_AUTOMATION_PROVIDER": "TfsGit", "SITEOPS_AUTOMATION_REF": "refs/heads/main",
        "SITEOPS_AUTOMATION_REPOSITORY": "private/caller", "SITEOPS_AUTOMATION_VERSION": "b" * 40,
    })
    assert selected.repository == "Azure/digital-ops-scale-kit"
    assert not selected.inferred


@pytest.mark.parametrize("changed", [
    {"SITEOPS_AUTOMATION_REF": "refs/heads/main"},
    {"SITEOPS_AUTOMATION_REF": "refs/pull/12/merge"},
    {"SITEOPS_AUTOMATION_PROVIDER": "TfsGit"},
    {"SITEOPS_AUTOMATION_VERSION": "$(unexpanded-version)"},
    {"SITEOPS_RELEASE": "v1.0.0b7"}, {"SITEOPS_SOURCE_COMMIT": SOURCE},
    {"SITEOPS_REPOSITORY": "other/publisher"}, {"SITEOPS_AUTOMATION_REPOSITORY": "https://private@host/repo"},
])
def test_unsupported_selection_does_not_fall_back_to_checkout_install(installer, changed):
    with pytest.raises(installer.ConsumerSetupError):
        installer.select({**RESOURCE, **changed})


@pytest.mark.parametrize("release", ["v1.0.0b7", "siteops/v1.2.3"])
@pytest.mark.parametrize("fault", ["none", "checkout", "bootstrap", "version"])
def test_consumer_calls_only_verified_bootstrap_with_private_scoped_state(
    installer, tmp_path, monkeypatch, capsys, release, fault,
):
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="posix"))
    source = tmp_path / "automation"
    (source / "scripts" / "bootstrap").mkdir(parents=True)
    (source / "scripts" / "bootstrap" / "siteops-bootstrap.sh").write_text("fixture")
    selected = installer.select({**RESOURCE, "SITEOPS_AUTOMATION_REF": "refs/tags/" + release})
    calls = []

    def runner(arguments, *, cwd, logs, name, timeout, environment):
        calls.append((name, arguments))
        assert cwd == source and 0 < timeout <= 1200
        assert "GH_TOKEN" not in environment and "SYSTEM_ACCESSTOKEN" not in environment
        assert "AZURE_CLIENT_SECRET" not in environment and "PYTHONPATH" not in environment
        assert environment["UV_DEFAULT_INDEX"] == "https://approved.example.invalid/simple/"
        assert Path(environment["UV_TOOL_DIR"]).parent == logs.parent
        assert Path(environment["UV_TOOL_BIN_DIR"]).parent == logs.parent
        if name == "source-check":
            assert arguments == ["git", "-C", str(source), "rev-parse", "HEAD"]
            output = "b" * 40 if fault == "checkout" else SOURCE
        elif name == "bootstrap":
            assert arguments == [
                "bash", str(source / "scripts/bootstrap/siteops-bootstrap.sh"),
                "--release" if release.startswith("siteops/") else "--content-release", release,
                "--source-commit", SOURCE, "--repository", "example/content", "--yes",
            ]
            output = "private-provider-diagnostic"
        else:
            assert name == "version"
            assert arguments == [str(logs.parent / "bin" / "siteops"), "--version"]
            output = "private-version-marker" if fault == "version" else "siteops 1.2.3"
        (logs / f"{name}.out").write_text(output)
        (logs / f"{name}.err").write_text("private-provider-marker")
        return 23 if fault == "bootstrap" and name == "bootstrap" else 0

    environment = {
        "PATH": "fixture-bin", "GH_TOKEN": "private-token", "SYSTEM_ACCESSTOKEN": "private-token",
        "AZURE_CLIENT_SECRET": "private-token", "PYTHONPATH": "private-source",
        "UV_DEFAULT_INDEX": "https://approved.example.invalid/simple/",
    }
    if fault != "none":
        with pytest.raises(installer.ConsumerSetupError) as caught:
            installer.install(selected, source, tmp_path, environment=environment, runner=runner)
        assert caught.value.code == (23 if fault == "bootstrap" else 1)
        assert "private-" not in str(caught.value)
    else:
        command = installer.install(selected, source, tmp_path, environment=environment, runner=runner)
        assert command.is_relative_to(tmp_path)
    assert len(calls) == {"checkout": 1, "bootstrap": 2, "version": 3, "none": 3}[fault]
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err


@pytest.mark.parametrize("source,development", [("", "False"), ("wheel", "True")])
def test_unselected_or_conflicting_setup_stops_before_pip(tmp_path, source, development):
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin/pip", "#!/usr/bin/env bash\necho unexpected > pip-called\nexit 99\n")
    result = run_script(step(TEMPLATES / "setup-siteops.yaml", "Install Site Ops")["script"], tmp_path, {
        "INSTALL_DEV": development, "SITEOPS_SOURCE": source,
        "SITEOPS_RELEASE": "", "SITEOPS_SOURCE_COMMIT": "", "SITEOPS_REPOSITORY": "",
        "SOURCE_DIRECTORY": bash_path(tmp_path), "STATE_PARENT": bash_path(tmp_path),
    }, shell_options=())
    assert result.returncode != 0
    assert not (tmp_path / "pip-called").exists()


def test_setup_dispatches_release_selection_without_installing_into_controller_python(tmp_path):
    (tmp_path / "bin").mkdir()
    source = tmp_path / "automation"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/install-siteops-consumer.py").write_text("controlled")
    write_executable(tmp_path / "bin/pip", "#!/usr/bin/env bash\necho pip > unexpected\nexit 99\n")
    write_executable(tmp_path / "bin/python3", """#!/usr/bin/env bash
[[ "$1 $2 $3" == '-I -S -B' ]] || exit 98
[[ "$SITEOPS_RELEASE" == v1.0.0b7 && "$SITEOPS_SOURCE_COMMIT" == aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]] || exit 97
printf '%s\\n' "$@" > arguments
exit 0
""")
    result = run_script(step(TEMPLATES / "setup-siteops.yaml", "Install Site Ops")["script"], tmp_path, {
        "INSTALL_DEV": "False", "SITEOPS_SOURCE": "", "SITEOPS_RELEASE": "v1.0.0b7",
        "SITEOPS_SOURCE_COMMIT": SOURCE, "SITEOPS_REPOSITORY": "",
        "SOURCE_DIRECTORY": bash_path(source), "STATE_PARENT": bash_path(tmp_path),
    }, shell_options=())
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "unexpected").exists()
    assert (tmp_path / "arguments").read_text().splitlines() == [
        "-I", "-S", "-B", bash_path(source / "scripts/install-siteops-consumer.py"),
        "--source-directory", bash_path(source), "--state-parent", bash_path(tmp_path),
    ]


def test_consumer_stages_share_exact_runtime_metadata_and_no_validation_azure_authority():
    context = yaml.safe_load((TEMPLATES / "siteops-context.yaml").read_text())["variables"]
    fields = context["${{ else }}"]
    for field, suffix in (
        ("REF", "ref"), ("VERSION", "version"), ("REPOSITORY", "name"), ("PROVIDER", "type"),
    ):
        assert fields["SITEOPS_AUTOMATION_" + field] == (
            "${{ format('$[ resources.repositories[''{0}'']." + suffix + " ]', parameters.templateRepository) }}"
        )
    for filename in ("siteops-deploy.yaml", "siteops-validate.yaml"):
        document = yaml.safe_load((TEMPLATES / filename).read_text())
        assert document["stages"][0]["variables"][0]["template"] == "siteops-context.yaml"
        all_nodes = list(nodes(TEMPLATES / filename))
        setup = next(node for node in all_nodes if node.get("template") == "setup-siteops.yaml")
        for field in ("templateRepository", "release", "sourceCommit", "repository", "siteopsSource"):
            assert setup["parameters"][field] == "${{ parameters." + field + " }}"
        if filename == "siteops-validate.yaml":
            assert not any(node.get("task", "").startswith("AzureCLI") or "environment" in node for node in all_nodes)
            assert not any(param["name"] in {"serviceConnection", "keepAzSessionActive"} for param in document["parameters"])


def test_custom_pipeline_examples_use_each_platforms_supported_selection():
    guide = (ROOT / "docs/ci-cd-setup.md").read_text(encoding="utf-8")
    custom = guide.split("### Custom deployment workflow", 1)[1].split("### Setup templates", 1)[0]
    github, ado = [yaml.safe_load(body) for body in re.findall(r"```yaml\n(.*?)```", custom, re.DOTALL)]
    flow = yaml.safe_load((ROOT / ".github/workflows/_siteops-deploy.yaml").read_text())
    supported = set(flow.get("on", flow.get(True))["workflow_call"]["inputs"])
    assert set(github["jobs"]["deploy"]["with"]) <= supported
    template = yaml.safe_load((TEMPLATES / "siteops-deploy.yaml").read_text())
    arguments = ado["stages"][0]["parameters"]
    assert set(arguments) <= {parameter["name"] for parameter in template["parameters"]}
    assert arguments["release"] == "<reviewed-release>"
    assert arguments["sourceCommit"] == "<full-release-source-commit>"
    assert "siteopsSource" not in arguments
    assert ado["pool"]["vmImage"] == "ubuntu-24.04"


def test_contributor_pipelines_request_development_installation_explicitly():
    for name in ("ci.yaml", "integration-test.yaml"):
        setups = [node for node in nodes(ROOT / ".pipelines" / name)
                  if node.get("template") == "templates/setup-siteops.yaml"]
        assert setups and all(node.get("parameters", {}).get("installDev") is True for node in setups)
    preparation = yaml.safe_load((ROOT / ".pipelines/deploy.yaml").read_text())["stages"][0]
    assert not any(node.get("template") == "templates/setup-siteops.yaml"
                   for node in preparation["jobs"][0]["steps"])


@pytest.mark.parametrize("site_file,selector,expected", [
    ("", "environment=dev", 0), ("target.yaml", "", 0), ("target.yaml", "environment=dev", 0),
    ("missing.yaml", "", 1), ("operator/../target.yaml", "environment=dev", 1),
    ("target.yaml", "environment=dev;id", 1),
])
def test_site_file_and_selector_validation_is_explicit(tmp_path, site_file, selector, expected):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "operator").mkdir()
    (tmp_path / "target.yaml").write_text("{}")
    result = run_script(step(TEMPLATES / "siteops-inputs.yaml", "Validate inputs")["script"], tmp_path, {
        "WORKSPACE": "workspace", "MANIFEST": "manifests/install.yaml",
        "SITE_FILE": site_file, "SELECTOR": selector,
    }, shell_options=())
    assert result.returncode == expected


def _github_validate_inputs():
    flow = yaml.safe_load((ROOT / ".github/workflows/_siteops-deploy.yaml").read_text(encoding="utf-8"))
    steps = [entry for job in flow["jobs"].values() for entry in job.get("steps", [])]
    return next(entry["run"] for entry in steps if entry.get("name") == "Validate inputs")


@pytest.mark.parametrize("platform", ["ado", "github"])
@pytest.mark.parametrize("field,value,expected", [
    ("WORKSPACE", "/opt/content", 1), ("MANIFEST", "/opt/content/manifest.yaml", 1),
    ("WORKSPACE", "D:\\content", 1), ("MANIFEST", "\\\\server\\share\\manifest.yaml", 1),
    ("MANIFEST", "manifests/../install.yaml", 1), ("MANIFEST", "manifests/install.yaml", 0),
])
def test_workspace_and_manifest_paths_must_be_relative(tmp_path, platform, field, value, expected):
    (tmp_path / "workspace").mkdir()
    environment = {"WORKSPACE": "workspace", "MANIFEST": "manifests/install.yaml", "SITE_FILE": "", "SELECTOR": ""}
    environment[field] = value
    if platform == "github":
        script = _github_validate_inputs()
        environment = {f"INPUT_{name}": entry for name, entry in environment.items()}
    else:
        script = step(TEMPLATES / "siteops-inputs.yaml", "Validate inputs")["script"]
    result = run_script(script, tmp_path, environment, shell_options=())
    assert result.returncode == expected
    assert ("must be relative to the checkout" in result.stdout) is bool(expected)


@pytest.mark.parametrize("site_file", [
    "existing absolute file", "\\\\server\\share\\site.yaml", "C:/agent/site.yaml", "c:\\agent\\site.yaml",
])
def test_site_file_path_must_be_relative(tmp_path, site_file):
    (tmp_path / "workspace").mkdir()
    target = tmp_path / "target.yaml"
    target.write_text("{}")
    if site_file == "existing absolute file":
        site_file = bash_path(target)
    result = run_script(step(TEMPLATES / "siteops-inputs.yaml", "Validate inputs")["script"], tmp_path, {
        "WORKSPACE": "workspace", "MANIFEST": "manifests/install.yaml",
        "SITE_FILE": site_file, "SELECTOR": "environment=dev",
    }, shell_options=())
    assert result.returncode == 1
    assert "must be relative to the checkout" in result.stdout


@pytest.mark.parametrize("site_file,selector", [
    ("", "environment=dev"), ("operator files/site.yaml", ""),
    ("operator files/site.yaml", "environment=dev"),
])
@pytest.mark.parametrize("exit_code", [0, 17])
def test_structural_validation_preserves_target_arguments_and_diagnostics(
    tmp_path, site_file, selector, exit_code,
):
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin/siteops", """#!/usr/bin/env bash
printf '%s\\n' "$@" > arguments
echo PRIVATE_DIAGNOSTIC >&2
exit "$EXPECTED_EXIT"
""")
    result = run_script(step(TEMPLATES / "siteops-validate.yaml", "Validate caller content")["script"], tmp_path, {
        "WORKSPACE": "caller workspace", "MANIFEST": "manifests/custom.yaml", "SITE_FILE": site_file,
        "SELECTOR": selector, "EXPECTED_EXIT": str(exit_code),
        "STATE_PARENT": bash_path(tmp_path),
    }, shell_options=())
    assert result.returncode == exit_code
    expected = ["-w", "caller workspace", "validate", "manifests/custom.yaml"]
    expected += ["-l", selector] if selector else []
    expected += ["--site-file", site_file] if site_file else []
    assert (tmp_path / "arguments").read_text().splitlines() == expected
    assert "PRIVATE_DIAGNOSTIC" not in result.stdout + result.stderr


def test_override_generator_runs_without_siteops_or_pyyaml(tmp_path):
    script = tmp_path / "generate.py"
    script.write_bytes((ROOT / "scripts/generate-site-overrides.py").read_bytes())
    environment = {**isolated_environment(tmp_path / "state"), "TF_BUILD": "true", "SITEOPS_REDACT_OUTPUT": "1"}
    result = subprocess.run([
        sys.executable, "-I", "-S", str(script), str(tmp_path / "workspace"),
    ], input='{"private-site":{"parameters.flag":true,"parameters.count":2,"parameters.text":"yes","properties.items":[1,null]}}',
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    document = yaml.safe_load((tmp_path / "workspace/sites.local/private-site.yaml").read_text())
    assert document == {"parameters": {"flag": True, "count": 2, "text": "yes"}, "properties": {"items": [1, None]}}
    assert "private-site" not in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "posix", reason="The released-engine pipeline adapter runs on Linux.")
def test_actual_consumer_entrypoint_needs_no_installed_engine_or_cloud_credentials(tmp_path):
    source = tmp_path / "automation"
    (source / "scripts/bootstrap").mkdir(parents=True)
    for name in ("install-siteops-consumer.py", "fleet_process.py"):
        (source / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    write_executable(source / "scripts/bootstrap/siteops-bootstrap.sh", """#!/usr/bin/env bash
set -euo pipefail
[[ "$*" == '--content-release v1.0.0b7 --source-commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --repository example/content --yes' ]]
[[ -z "${GH_TOKEN:-}${SYSTEM_ACCESSTOKEN:-}${AZURE_CLIENT_SECRET:-}" ]]
printf '#!/usr/bin/env bash\\necho siteops 1.2.3\\n' > "$UV_TOOL_BIN_DIR/siteops"
chmod 700 "$UV_TOOL_BIN_DIR/siteops"
""")
    binary = tmp_path / "fake-tools"
    binary.mkdir()
    write_executable(binary / "git", f"#!/usr/bin/env bash\n[[ \"$3 $4\" == 'rev-parse HEAD' ]] || exit 99\necho {SOURCE}\n")
    environment = {
        **isolated_environment(tmp_path / "state"), **RESOURCE,
        "PATH": os.pathsep.join((str(binary), "/usr/bin", "/bin")),
        "SYSTEM_ACCESSTOKEN": "private-token", "GH_TOKEN": "private-token",
        "AZURE_CLIENT_SECRET": "private-token",
    }
    result = subprocess.run([
        sys.executable, "-I", "-S", "-B", str(source / "scripts/install-siteops-consumer.py"),
        "--source-directory", str(source), "--state-parent", str(tmp_path),
    ], env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "siteops 1.2.3" in result.stdout and "##vso[task.prependpath]" in result.stdout
    assert "private-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("explicit", [None, "", "0", "false", "1", "yes", "unknown"])
@pytest.mark.parametrize("ci", [None, "GITHUB_ACTIONS", "TF_BUILD"])
def test_standalone_override_redaction_matches_the_engine_contract(monkeypatch, explicit, ci):
    from siteops.sanitize import is_redaction_enabled

    spec = importlib.util.spec_from_file_location("standalone_overrides", ROOT / "scripts/generate-site-overrides.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    for key in ("SITEOPS_REDACT_OUTPUT", "GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(key, raising=False)
    if explicit is not None:
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", explicit)
    if ci:
        monkeypatch.setenv(ci, "true")
    assert helper.is_redaction_enabled() is is_redaction_enabled()

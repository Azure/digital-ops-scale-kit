"""Execute Azure Pipelines steps with their native Bash error semantics."""

import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.ado_helpers import TEMPLATES
from tests.ado_helpers import step as _step
from tests.native_uv_consumers import _required, _unavailable
from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / ".pipelines" / "templates" / "setup-siteops.yaml"
DEPLOY = ROOT / ".pipelines" / "templates" / "siteops-deploy.yaml"
INTEGRATION = ROOT / ".pipelines" / "integration-test.yaml"
CI = ROOT / ".pipelines" / "ci.yaml"
NATIVE_FIXTURES = ROOT / "tests" / "fixtures" / "prepare-native-uv.sh"


@pytest.mark.parametrize("path", [ROOT / ".pipelines" / "deploy.yaml", DEPLOY, INTEGRATION])
def test_wif_session_refresh_is_explicit_opt_in(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    parameter = next((row for row in data["parameters"] if row["name"] == "keepAzSessionActive"), None)
    assert parameter is not None
    assert parameter["type"] == "boolean" and parameter["default"] is False
    if path.name == "deploy.yaml":
        stage = next(row for row in data["stages"] if row.get("template") == "templates/siteops-deploy.yaml")
        assert stage["parameters"]["keepAzSessionActive"] == "${{ parameters.keepAzSessionActive }}"
    else:
        step = _step(path, "Run integration tests" if path == INTEGRATION else "Prepare executable plan and deploy")
        assert step["inputs"]["keepAzSessionActive"] == "${{ parameters.keepAzSessionActive }}"
        assert step["inputs"].get("addSpnToEnvironment", False) is False
        assert step["inputs"].get("useGlobalConfig", False) is False


def test_wif_consumer_examples_explicitly_select_task_session_refresh():
    guide = (ROOT / "docs" / "ci-cd-setup.md").read_text(encoding="utf-8")
    custom = guide.split("### Custom deployment workflow", 1)[1].split("### Setup templates", 1)[0]
    cross_repo = guide.split("### Reference the deployment template from another repository", 1)[1].split(
        "### ADO project setup", 1,
    )[0]
    for section in (custom, cross_repo):
        assert "keepAzSessionActive: true" in section
    assert "The default remains `false`" in guide
    assert "experimental" in guide.lower()
    assert "No separate login, token refresh, or" not in guide
    refresh = next(line for line in guide.splitlines() if line.startswith("| **Token Refresh** |"))
    ado_refresh = refresh.split("|")[3]
    assert "Opt in" in ado_refresh
    assert "`keepAzSessionActive`" in ado_refresh
    assert "WIF" in ado_refresh


def _expression(condition, values):
    match = re.fullmatch(r"not\(in\(parameters\.(\w+), '', ' '\)\)", condition)
    if match:
        return values[match[1]] not in ("", " ")
    match = re.fullmatch(r"eq\(parameters\.(\w+), '([^']*)'\)", condition)
    if match:
        return values[match[1]] == match[2]
    raise AssertionError(f"Unsupported template condition: {condition}")


def _expand(node, values, variables=None):
    """Expand the conditional mapping forms the entry pipelines use, without an Azure service call."""
    if isinstance(node, str):
        context = {"parameters": values, "variables": variables or {}}
        return re.sub(
            r"\$\{\{ (parameters|variables)\.(\w+) \}\}",
            lambda match: str(context[match[1]][match[2]]) if match[1] == "parameters"
            else str(context["variables"].get(match[2], "")),
            node,
        )
    if not isinstance(node, dict):
        return node
    result, taken = {}, None
    for key, value in node.items():
        branch = re.fullmatch(r"\$\{\{ (if|elseif) (.+) \}\}", key)
        if branch or key == "${{ else }}":
            if branch is None or branch[1] == "elseif":
                assert taken is not None, key
            else:
                taken = False
            if not taken and (branch is None or _expression(branch[2], values)):
                result.update(_expand(value, values, variables))
                taken = True
            if branch is None:
                taken = None
            continue
        taken = None
        result[key] = _expand(value, values, variables)
    return result


def _variables(document, values):
    """Flatten list-level `${{ if }}` insertion in a pipeline's root variables."""
    rows = []
    for item in document["variables"]:
        key = next(iter(item)) if len(item) == 1 else None
        branch = re.fullmatch(r"\$\{\{ if (.+) \}\}", key) if isinstance(key, str) else None
        if branch is None:
            rows.append(item)
        elif _expression(branch[1], values):
            rows.extend(item[key])
    return rows


def _deploy_stage(text=None, **selected):
    document = yaml.safe_load(text or (ROOT / ".pipelines" / "deploy.yaml").read_text(encoding="utf-8"))
    values = {row["name"]: row.get("default") for row in document["parameters"]} | selected
    names = {row["name"]: row["value"] for row in _variables(document, values) if "name" in row}
    stage = next(row for row in document["stages"] if row.get("template") == "templates/siteops-deploy.yaml")
    return _expand(stage["parameters"], values, names)


_ENVIRONMENTS = ["dev", "staging", "prod"]
_SETTINGS_BLOCK = """  - ${{{{ if eq(parameters.environment, '{0}') }}}}:
    - group: siteops-secrets
    - name: siteopsServiceConnection
      value: azure-siteops
"""


@pytest.mark.parametrize("path", [ROOT / ".pipelines" / "deploy.yaml", INTEGRATION])
def test_environment_settings_pair_each_environment_with_its_own_resources(path):
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert not [row["name"] for row in document["parameters"] if row.get("type") == "object"]
    environment = next(row for row in document["parameters"] if row["name"] == "environment")
    assert environment["values"] == _ENVIRONMENTS
    distinct = text
    for name in _ENVIRONMENTS:
        block = _SETTINGS_BLOCK.format(name)
        assert text.count(block) == 1, name
        distinct = distinct.replace(block, block.replace("siteops-secrets", f"{name}-secrets").replace(
            "azure-siteops", f"{name}-connection"))
    edited = yaml.safe_load(distinct)
    for name in _ENVIRONMENTS:
        rows = _variables(edited, {"environment": name})
        assert rows[0] == {"name": "SITE_OVERRIDES", "value": ""}
        assert [row for row in rows if "group" in row] == [{"group": f"{name}-secrets"}]
        assert [row["value"] for row in rows if row.get("name") == "siteopsServiceConnection"] == [
            f"{name}-connection"
        ]
        if path == INTEGRATION:
            task = _step(path, "Run integration tests")
            assert task["inputs"]["azureSubscription"] == "${{ variables.siteopsServiceConnection }}"
        else:
            stage = _deploy_stage(distinct, environment=name)
            assert stage["serviceConnection"] == f"{name}-connection"
            assert stage["environment"] == name


def test_deploy_stage_takes_its_service_connection_from_the_environment_settings():
    document = yaml.safe_load((ROOT / ".pipelines" / "deploy.yaml").read_text(encoding="utf-8"))
    stage = next(row for row in document["stages"] if row.get("template") == "templates/siteops-deploy.yaml")
    assert stage["parameters"]["serviceConnection"] == "${{ variables.siteopsServiceConnection }}"
    assert stage["parameters"]["environment"] == "${{ parameters.environment }}"


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
@pytest.mark.parametrize("additional", [" ", "country=US"])
@pytest.mark.parametrize("site_file", [" ", "operator/site.yaml"])
def test_deploy_site_file_keeps_the_environment_label_requirement(environment, additional, site_file):
    stage = _deploy_stage(environment=environment, selector=additional, siteFile=site_file)
    assert stage["environment"] == environment
    assert stage["selector"] == f"environment={environment}" + (
        f",{additional}" if additional.strip() else ""
    )
    assert stage.get("siteFile") == (site_file if site_file.strip() else None)


@pytest.mark.parametrize("sample", ["resource-set-basic", "resource-set-composition"])
def test_resource_set_sample_site_file_keeps_the_sample_label_requirement(sample):
    stage = _deploy_stage(manifest=f"samples/{sample}/manifest.yaml", siteFile="operator/site.yaml")
    assert stage["siteFile"] == "operator/site.yaml"
    assert stage["selector"] == f"environment=sample,sample={sample}"


def test_deploy_template_passes_the_label_requirement_with_the_site_file_through_env():
    task = _step(DEPLOY, "Prepare executable plan and deploy")
    assert task["env"]["SELECTOR"] == "${{ parameters.selector }}"
    assert task["env"]["SITE_FILE"] == "${{ parameters.siteFile }}"
    script = task["inputs"]["inlineScript"]
    assert "${{" not in script
    for arguments in ("PLAN_ARGS", "CMD_ARGS"):
        assert f'[[ -n "$SELECTOR" ]] && {arguments}+=(-l "$SELECTOR")' in script
        assert f'[[ -n "$SITE_FILE" ]] && {arguments}+=(--site-file "$SITE_FILE")' in script


def test_queue_time_parameters_cannot_select_an_engine_source():
    for path in (ROOT / ".pipelines").glob("*.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        names = {row["name"] for row in document.get("parameters", [])}
        assert "siteopsSource" not in names, path.name
    assert "siteopsSource" not in _deploy_stage()
    assert {"release", "sourceCommit"} <= {
        row["name"] for row in yaml.safe_load((ROOT / ".pipelines" / "deploy.yaml").read_text())["parameters"]
    }
    for path in (DEPLOY, SETUP):
        names = {row["name"] for row in yaml.safe_load(path.read_text(encoding="utf-8"))["parameters"]}
        assert "siteopsSource" in names, path.name


@pytest.mark.parametrize(("upgrade", "install"), [(0, 0), (31, 0), (0, 32), (31, 32)])
def test_setup_stops_on_the_first_failed_installation(tmp_path, upgrade, install):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "pip", """#!/usr/bin/env bash
case "$*" in
  'install --upgrade pip') echo upgrade >> calls; exit "$UPGRADE_EXIT" ;;
  'install -e .[dev]') echo install >> calls; exit "$INSTALL_EXIT" ;;
  *) exit 98 ;;
esac
""")
    result = run_script(
        _step(SETUP, "Install Site Ops")["script"], tmp_path,
        {"SITEOPS_SOURCE": "", "INSTALL_DEV": "True",
         "SITEOPS_RELEASE": "", "SITEOPS_SOURCE_COMMIT": "", "SITEOPS_REPOSITORY": "",
         "UPGRADE_EXIT": str(upgrade), "INSTALL_EXIT": str(install)},
        shell_options=(),
    )
    assert result.returncode == (upgrade or install), result.stdout + result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == (
        ["upgrade"] if upgrade else ["upgrade", "install"]
    )


@pytest.mark.parametrize(("code", "value", "expected"), [
    (0, "synthetic-cache", 0), (31, "", 31), (0, "", 1),
])
def test_cache_lookup_requires_a_successful_nonempty_result(tmp_path, code, value, expected):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "pip", """#!/usr/bin/env bash
[[ "$*" == 'cache dir' ]] || exit 98
printf '%s\\n' "$CACHE_VALUE"
exit "$CACHE_EXIT"
""")
    result = run_script(
        _step(SETUP, "Resolve pip cache directory")["script"], tmp_path,
        {"CACHE_EXIT": str(code), "CACHE_VALUE": value}, shell_options=(),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("task.setvariable" in result.stdout) is (expected == 0)


@pytest.mark.parametrize(("generator", "masker", "value", "expected"), [
    (0, 0, "synthetic-value", 0), (32, 0, "synthetic-value", 32),
    (0, 33, "", 33), (0, 0, "", 0),
])
def test_override_preparation_preserves_failures_and_empty_values(
    tmp_path, generator, masker, value, expected,
):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "python3", """#!/usr/bin/env bash
case "$*" in
  '-I -S -B ./scripts/generate-site-overrides.py workspace') echo generator >> calls; exit "$GENERATOR_EXIT" ;;
  '-I -S -B ./scripts/mask-site-overrides.py azure-pipelines') echo masker >> calls; exit "$MASK_EXIT" ;;
  *) exit 98 ;;
esac
""")
    result = run_script(
        _step(DEPLOY, "Setup site overrides")["script"], tmp_path,
        {"WORKSPACE": "workspace", "TEMPLATE_ROOT": ".", "SITE_OVERRIDES": '{"site":{}}',
         "GENERATOR_EXIT": str(generator), "MASK_EXIT": str(masker), "MASK_VALUE": value},
        shell_options=(),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == (
        ["masker"] if masker else ["masker", "generator"]
    )


def test_integration_masking_accepts_an_empty_string_value(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "mask-site-overrides.py",
                    tmp_path / "scripts" / "mask-site-overrides.py")
    python = shlex.quote(Path(sys.executable).absolute().as_posix())
    write_executable(binary / "python3", f'#!/usr/bin/env bash\nexec {python} "$@"\n')
    result = run_script(
        _step(INTEGRATION, "Mask secret values")["script"], tmp_path,
        {"SITE_OVERRIDES": '{"site":{"optional":""}}'}, shell_options=(),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_github_ci_runs_for_ado_definition_changes(event):
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yaml").read_text())
    triggers = data.get("on", data.get(True))
    assert ".pipelines/**" in triggers[event]["paths"]


def test_ado_unit_lane_requires_the_shared_native_linux_fixtures():
    data = yaml.safe_load(CI.read_text())
    job = next(job for job in data["jobs"] if job["job"] == "test")
    assert job["pool"]["vmImage"] == "ubuntu-24.04"
    steps = job["steps"]
    names = [step.get("displayName") for step in steps]
    assert names.index("Prepare native Linux uv fixtures") < names.index("Run unit tests")
    script = _step(CI, "Prepare native Linux uv fixtures")["script"]
    assert 'bash tests/fixtures/prepare-native-uv.sh "$fixtures"' in script
    for name, suffix in (
        ("SITEOPS_TEST_UV", "$fixtures/native/uv"),
        ("SITEOPS_TEST_UV_ARCHIVE", "$fixtures/uv.tar.gz"),
        ("SITEOPS_TEST_UV_PYTHON_ARCHIVE", "$fixtures/cpython.tar.gz"),
        ("SITEOPS_REQUIRE_LINUX_UV", "1"),
    ):
        assert f"variable={name}]{suffix}" in script


def test_ado_requires_native_fixtures_without_an_explicit_ci_flag(monkeypatch):
    for name in ("CI", "SITEOPS_REQUIRE_LINUX_UV", "SITEOPS_REQUIRE_WINDOWS_UV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TF_BUILD", "True")
    assert _required()
    with pytest.raises(pytest.fail.Exception, match="missing native input"):
        _unavailable("missing native input")


@pytest.mark.parametrize("pipeline", [CI, INTEGRATION], ids=["unit", "integration"])
def test_test_publication_requires_results_and_preserves_failure(pipeline):
    inputs = _step(pipeline, "Publish test results")["inputs"]
    assert inputs["failTaskOnFailedTests"] is True
    assert inputs["failTaskOnMissingResultsFile"] is True


@pytest.mark.parametrize(("download", "digest", "extract", "expected"), [
    (0, 0, 0, 0), (31, 0, 0, 31), (0, 32, 0, 32), (0, 0, 33, 33),
])
def test_native_fixture_preparation_stops_before_using_failed_inputs(
    tmp_path, download, digest, extract, expected,
):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "curl", """#!/usr/bin/env bash
echo download >> calls
[[ "$*" == *"--proto =https --proto-redir =https --tlsv1.2"* ]] || exit 98
exit "$DOWNLOAD_EXIT"
""")
    write_executable(binary / "sha256sum", """#!/usr/bin/env bash
[[ "$*" == '--check --status' ]] || exit 98
while IFS= read -r line; do :; done
echo digest >> calls
exit "$DIGEST_EXIT"
""")
    write_executable(binary / "tar", """#!/usr/bin/env bash
[[ "$*" == '-xzf '* ]] || exit 98
echo extract >> calls
exit "$EXTRACT_EXIT"
""")
    write_executable(binary / "openssl", "#!/usr/bin/env bash\nexit 98\n")
    script = 'set -- "$FIXTURE_ROOT"\n' + NATIVE_FIXTURES.read_text(encoding="utf-8")
    result = run_script(
        script, tmp_path,
        {"FIXTURE_ROOT": bash_path(tmp_path / "fixtures"),
         "DOWNLOAD_EXIT": str(download), "DIGEST_EXIT": str(digest),
         "EXTRACT_EXIT": str(extract)}, shell_options=(),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == (
        ["download"] if download else
        ["download", "download", "digest"] if digest else
        ["download", "download", "digest", "extract"]
    )


@pytest.mark.parametrize("platform", ["github", "ado"])
@pytest.mark.parametrize("case", ["valid", "empty", "missing-directory", "discovery-failure"])
def test_manifest_discovery_requires_a_complete_nonempty_inventory(tmp_path, platform, case):
    binary = tmp_path / "bin"
    binary.mkdir()
    workspace = tmp_path / "workspaces" / "iot-operations"
    (workspace / "manifests" / "first").mkdir(parents=True)
    (workspace / "samples" / "second").mkdir(parents=True)
    if case != "empty":
        (workspace / "samples" / "second" / "manifest.yaml").write_text("kind: Manifest\n")
    if case == "missing-directory":
        (workspace / "manifests" / "first").rmdir()
        (workspace / "manifests").rmdir()
    if case == "discovery-failure":
        write_executable(binary / "find", "#!/usr/bin/env bash\nexit 37\n")
    write_executable(binary / "siteops", "#!/usr/bin/env bash\necho called >> calls\n")
    summary = tmp_path / "summary.md"
    github = ROOT / ".github" / "workflows" / "ci.yaml"
    script = _step(github if platform == "github" else CI,
                   "Find manifests" if platform == "github" else "Validate manifest structure")
    body = script.get("run", script.get("script"))
    body = body.replace("$(Build.ArtifactStagingDirectory)", bash_path(tmp_path))
    result = run_script(
        body, tmp_path,
        {"WORKSPACE_DIR": "workspaces/iot-operations", "GITHUB_OUTPUT": bash_path(summary)},
        shell_options=("-e", "-o", "pipefail") if platform == "github" else (),
    )
    assert (result.returncode == 0) is (case == "valid"), (result.stdout, result.stderr)
    if case != "valid":
        assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize("platform", ["github", "ado"])
@pytest.mark.parametrize("integration", [False, True])
def test_override_masks_encode_values_as_single_logging_commands(tmp_path, platform, integration):
    binary = tmp_path / "bin"
    binary.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("generate-site-overrides.py", "mask-site-overrides.py"):
        source = ROOT / "scripts" / name
        if source.exists():
            shutil.copyfile(source, scripts / name)
    python = shlex.quote(Path(sys.executable).absolute().as_posix())
    write_executable(binary / "python3", f'#!/usr/bin/env bash\nexec {python} "$@"\n')
    value = "secret%0A\r\n##vso[task.complete result=Succeeded;]marker"
    event = tmp_path / "event.json"
    event.write_text('{"inputs":{"selector":""}}')
    if platform == "ado":
        path = INTEGRATION if integration else DEPLOY
    else:
        path = ROOT / ".github" / "workflows" / (
            "integration-test.yaml" if integration else "_siteops-deploy.yaml"
        )
    step = _step(path, "Mask secret values" if integration else "Setup site overrides")
    result = run_script(
        step.get("run", step.get("script")), tmp_path,
        {"SITE_OVERRIDES": json.dumps({"test-site": {"parameters.secret": value}}),
         "WORKSPACE": "workspace", "INPUT_WORKSPACE": "workspace", "SITEOPS_REDACT_OUTPUT": "1",
         "TEMPLATE_ROOT": ".",
         "GITHUB_EVENT_PATH": event.as_posix()},
        shell_options=("-e", "-o", "pipefail") if platform == "github" else (),
    )
    assert result.returncode == 0, result.stderr
    prefix = "::add-mask::" if platform == "github" else "##vso[task.setsecret]"
    encoded = value.replace("%", "%25" if platform == "github" else "%AZP25")
    encoded = encoded.replace("\r", "%0D").replace("\n", "%0A")
    assert prefix + encoded in result.stdout.splitlines()
    assert not any(line.startswith("##vso[task.complete") for line in result.stdout.splitlines())
    assert "\r" not in result.stdout
    assert result.stderr == ""


@pytest.fixture
def masking():
    spec = importlib.util.spec_from_file_location("masking", ROOT / "scripts" / "mask-site-overrides.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("raw", ["", "  ", "{}", '{"site":{"empty":"","values":[]}}'])
def test_masking_preserves_empty_optional_input(masking, raw):
    commands = masking.mask_commands(raw, "azure-pipelines")
    assert commands == (["##vso[task.setsecret]site"] if '"site"' in raw else [])


@pytest.mark.parametrize("raw", [
    "private-input", '["private-input"]', '{"site":"private-input"}',
    '{"site":{"value":"private-input\\u0000"}}',
])
def test_masking_rejects_invalid_input_without_emitting_it(masking, monkeypatch, capsys, raw):
    monkeypatch.setenv("SITE_OVERRIDES", raw)
    monkeypatch.setattr(sys, "argv", ["mask-site-overrides.py", "azure-pipelines"])
    assert masking.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not be masked" in captured.err
    assert "private-input" not in captured.err


@pytest.mark.parametrize("path", [ROOT / ".pipelines" / "deploy.yaml", INTEGRATION])
def test_optional_overrides_default_precedes_the_selected_secret_group(path):
    variables = yaml.safe_load(path.read_text())["variables"]
    index = next(i for i, value in enumerate(variables) if value.get("name") == "SITE_OVERRIDES")

    def selects_group(value):
        rows = next(iter(value.values())) if len(value) == 1 else None
        return "group" in value or isinstance(rows, list) and any("group" in row for row in rows)

    groups = [i for i, value in enumerate(variables) if selects_group(value)]
    assert groups and all(index < group for group in groups)
    assert variables[index]["value"] == ""


def test_reusable_template_separates_the_callers_workspace_from_pinned_tooling(tmp_path):
    data = yaml.safe_load(DEPLOY.read_text())
    defaults = {parameter["name"]: parameter.get("default") for parameter in data["parameters"]}
    assert defaults["templateRepository"] == "self"
    stage = data["stages"][0]
    assert stage["variables"] == [{
        "template": "siteops-context.yaml", "parameters": {"templateRepository": "${{ parameters.templateRepository }}"},
    }]
    variables = yaml.safe_load((TEMPLATES / "siteops-context.yaml").read_text())["variables"]
    assert variables["${{ if eq(parameters.templateRepository, 'self') }}"] == {
        "SITEOPS_TEMPLATE_ROOT": "$(Build.SourcesDirectory)",
        "SITEOPS_WORKSPACE_ROOT": "$(Build.SourcesDirectory)",
    }
    assert variables["${{ else }}"]["SITEOPS_TEMPLATE_ROOT"] == "$(Pipeline.Workspace)/s/siteops-automation"
    assert variables["${{ else }}"]["SITEOPS_WORKSPACE_ROOT"] == "$(Pipeline.Workspace)/s/siteops-inputs"
    steps = stage["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
    assert steps[0]["template"] == "siteops-checkout.yaml"
    checkouts = yaml.safe_load((TEMPLATES / "siteops-checkout.yaml").read_text())["steps"]
    assert checkouts[0]["checkout"] == "self"
    assert checkouts[0]["${{ if ne(parameters.templateRepository, 'self') }}"]["path"] == "s/siteops-inputs"
    checkout = checkouts[1]["${{ if ne(parameters.templateRepository, 'self') }}"][0]
    assert checkout["checkout"] == "${{ parameters.templateRepository }}"
    assert checkout["path"] == "s/siteops-automation"
    assert checkout["persistCredentials"] is False
    setup = next(step for step in steps if step.get("template") == "setup-siteops.yaml")
    assert setup["parameters"]["sourceDirectory"] == "$(SITEOPS_TEMPLATE_ROOT)"
    for name in ("Validate inputs", "Setup site overrides"):
        assert _step(DEPLOY, name)["workingDirectory"] == "$(SITEOPS_WORKSPACE_ROOT)"
    assert _step(DEPLOY, "Prepare executable plan and deploy")["inputs"]["workingDirectory"] == (
        "$(SITEOPS_WORKSPACE_ROOT)"
    )

    caller = tmp_path / "customer workspace"
    tooling = tmp_path / "pinned tooling"
    (caller / "deployment").mkdir(parents=True)
    (caller / "bin").mkdir()
    (tooling / "scripts").mkdir(parents=True)
    for name in ("mask-site-overrides.py", "generate-site-overrides.py"):
        shutil.copyfile(ROOT / "scripts" / name, tooling / "scripts" / name)
    python = shlex.quote(Path(sys.executable).absolute().as_posix())
    write_executable(caller / "bin" / "python3", f'#!/usr/bin/env bash\nexec {python} "$@"\n')
    result = run_script(
        _step(DEPLOY, "Setup site overrides")["script"], caller,
        {"TEMPLATE_ROOT": bash_path(tooling), "WORKSPACE": "deployment", "SITEOPS_REDACT_OUTPUT": "1",
         "SITE_OVERRIDES": '{"customer-site":{"parameters.clusterName":"cluster"}}'},
        shell_options=(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    generated = caller / "deployment" / "sites.local" / "customer-site.yaml"
    assert yaml.safe_load(generated.read_text()) == {"parameters": {"clusterName": "cluster"}}
    assert not (tooling / "deployment").exists()
    assert not (caller / "scripts").exists()


@pytest.mark.parametrize("spec", [
    "", "https://example.invalid/siteops.whl",
    "siteops @ git+https://example.invalid/scalekit.git@v0.0.0",
])
def test_setup_retains_local_and_external_consumer_installation_routes(tmp_path, spec):
    step = _step(SETUP, "Install Site Ops")
    assert step["workingDirectory"] == "${{ parameters.sourceDirectory }}"
    data = yaml.safe_load(SETUP.read_text())
    defaults = {parameter["name"]: parameter.get("default") for parameter in data["parameters"]}
    assert defaults["sourceDirectory"] == "$(Build.SourcesDirectory)"
    assert defaults["siteopsSource"] == ""
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "pip", """#!/usr/bin/env bash
if [[ "$#" == 3 && "$1" == install && "$2" == --upgrade && "$3" == pip ]]; then
  exit 0
fi
if [[ "$#" == 3 && "$1" == install && "$2" == -e && "$3" == '.[dev]' ]] ||
   [[ "$#" == 2 && "$1" == install && "$2" == "$EXPECTED_SOURCE" && -n "$2" ]]; then
  printf '%s\\n' "$@" > installed
else
  exit 98
fi
""")
    result = run_script(
        step["script"], tmp_path,
        {"SITEOPS_SOURCE": spec, "INSTALL_DEV": "False" if spec else "True", "EXPECTED_SOURCE": spec,
         "SITEOPS_RELEASE": "", "SITEOPS_SOURCE_COMMIT": "", "SITEOPS_REPOSITORY": ""},
        shell_options=(),
    )
    assert result.returncode == 0
    assert (tmp_path / "installed").read_text().splitlines() == (
        ["install", spec] if spec else ["install", "-e", ".[dev]"]
    )


@pytest.mark.parametrize("legacy_verbose", [False, True])
def test_consumer_ci_validation_uses_global_verbosity_without_provider_calls(
    complete_workspace, monkeypatch, legacy_verbose,
):
    from siteops.cli import main
    from siteops.orchestrator import Orchestrator

    def reject_execution(*args, **kwargs):
        pytest.fail("Structural consumer validation must not prepare or execute provider operations.")

    monkeypatch.setattr(Orchestrator, "build_plan", reject_execution)
    monkeypatch.setattr(subprocess, "Popen", reject_execution)
    arguments = ["siteops", "-w", str(complete_workspace), "validate", "manifests/test-manifest.yaml"]
    if legacy_verbose:
        arguments.append("-v")
    else:
        arguments.insert(1, "-v")
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == (2 if legacy_verbose else 0)

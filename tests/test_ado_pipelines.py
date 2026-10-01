"""Execute Azure Pipelines steps with their native Bash error semantics."""

from pathlib import Path

import pytest
import yaml

from tests.native_uv_consumers import _required, _unavailable
from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / ".pipelines" / "templates" / "setup-siteops.yaml"
DEPLOY = ROOT / ".pipelines" / "templates" / "siteops-deploy.yaml"
INTEGRATION = ROOT / ".pipelines" / "integration-test.yaml"
CI = ROOT / ".pipelines" / "ci.yaml"
NATIVE_FIXTURES = ROOT / "tests" / "fixtures" / "prepare-native-uv.sh"


def _step(path: Path, name: str) -> dict:
    def walk(node):
        if isinstance(node, dict):
            if node.get("displayName") == name:
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    matches = list(walk(yaml.safe_load(path.read_text(encoding="utf-8"))))
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(("upgrade", "install"), [(0, 0), (31, 0), (0, 32), (31, 32)])
def test_setup_stops_on_the_first_failed_installation(tmp_path, upgrade, install):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "pip", """#!/usr/bin/env bash
case "$*" in
  'install --upgrade pip') echo upgrade >> calls; exit "$UPGRADE_EXIT" ;;
  'install -e .') echo install >> calls; exit "$INSTALL_EXIT" ;;
  *) exit 98 ;;
esac
""")
    result = run_script(
        _step(SETUP, "Install Site Ops")["script"], tmp_path,
        {"SITEOPS_SOURCE": "", "INSTALL_DEV": "False",
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
[[ "$*" == 'scripts/generate-site-overrides.py workspace' ]] || exit 98
echo generator >> calls
exit "$GENERATOR_EXIT"
""")
    write_executable(binary / "jq", """#!/usr/bin/env bash
[[ "$*" == '-r .. | strings' ]] || exit 98
echo masker >> calls
printf '%s\\n' "$MASK_VALUE"
exit "$MASK_EXIT"
""")
    result = run_script(
        _step(DEPLOY, "Setup site overrides")["script"], tmp_path,
        {"WORKSPACE": "workspace", "SITE_OVERRIDES": '{"site":{}}',
         "GENERATOR_EXIT": str(generator), "MASK_EXIT": str(masker), "MASK_VALUE": value},
        shell_options=(),
    )
    assert result.returncode == expected, result.stdout + result.stderr
    if generator:
        assert (tmp_path / "calls").read_text().splitlines() == ["generator"]


def test_integration_masking_accepts_an_empty_string_value(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "jq", "#!/usr/bin/env bash\nprintf '\\n'\n")
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

"""Execute fleet setup and input guards with closed command providers."""

import json
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import bash_path, run_script, write_executable

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = yaml.safe_load((ROOT / ".github/actions/create-k3s-cluster/action.yaml").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/e2e-test.yaml").read_text())


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_k3s_installer_keeps_acceptance_diagnostics_private_without_hiding_failure(tmp_path, private, failure):
    binary = tmp_path / "bin"
    binary.mkdir()
    write_executable(binary / "curl", """#!/usr/bin/env bash
printf '%s\\n' 'echo PRIVATE_INSTALL_OUT' 'echo PRIVATE_INSTALL_ERR >&2' 'exit "$INSTALL_RESULT"'
""")
    script = next(step["run"] for step in CLUSTER["runs"]["steps"] if step.get("name") == "Install k3s")
    result = run_script(script, tmp_path, {
        "PRIVATE_DIAGNOSTICS": "true" if private else "false",
        "RUNNER_TEMP": bash_path(tmp_path), "K3S_VERSION": "synthetic-version",
        "INSTALL_RESULT": "23" if failure else "0",
    })
    assert result.returncode == (1 if private and failure else 23 if failure else 0)
    assert ("PRIVATE_INSTALL" in result.stdout + result.stderr) is not private
    if private:
        assert "PRIVATE_INSTALL_OUT" in (tmp_path / "k3s-private" / "install.out").read_text()
        assert "PRIVATE_INSTALL_ERR" in (tmp_path / "k3s-private" / "install.err").read_text()


@pytest.mark.parametrize("api_ready", [False, True])
def test_private_host_readiness_does_not_publish_journal_or_node_values(tmp_path, api_ready):
    binary = tmp_path / "bin"
    binary.mkdir()
    (tmp_path / "k3s-private").mkdir()
    write_executable(binary / "kubectl", """#!/usr/bin/env bash
if [[ "$*" == 'get --raw=/readyz' ]]; then exit "$API_RESULT"; fi
echo PRIVATE_NODE_NAME
""")
    write_executable(binary / "sudo", "#!/usr/bin/env bash\necho PRIVATE_JOURNAL_VALUE\n")
    write_executable(binary / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    script = next(step["run"] for step in CLUSTER["runs"]["steps"]
                  if step.get("name") == "Wait for k3s API + node Ready")
    result = run_script(script, tmp_path, {
        "PRIVATE_DIAGNOSTICS": "true", "RUNNER_TEMP": bash_path(tmp_path),
        "API_RESULT": "0" if api_ready else "1",
    })
    assert result.returncode == (0 if api_ready else 1)
    assert "PRIVATE_" not in result.stdout + result.stderr
    retained = tmp_path / "k3s-private" / ("nodes.out" if api_ready else "journal.out")
    assert "PRIVATE_" in retained.read_text()


@pytest.mark.parametrize("field,value", [
    ("resource-group", "private-group"), ("cluster-name", "private-cluster"),
    ("skip-teardown", "true"), ("keep-cluster-alive-minutes", "5"),
    ("published-release", "other-release"), ("tests", "aio-install"),
])
def test_fleet_scope_refuses_single_site_or_cleanup_bypasses_without_printing_values(
    tmp_path, monkeypatch, capsys, field, value,
):
    script = WORKFLOW["jobs"]["fleet-request"]["steps"][0]["run"]
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"inputs": {"scenario": "fleet", "fleet-candidate": "fixture", field: value}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    with pytest.raises(SystemExit):
        exec(compile(script, "<fleet-request>", "exec"), {})
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err


@pytest.mark.parametrize("mode,prior,expected", [
    ("fleet", ("", ""), True), ("fleet", ("42", "1"), False),
    ("fleet-cleanup", ("42", "1"), True), ("fleet-cleanup", ("", ""), False),
])
def test_fleet_request_distinguishes_new_acceptance_and_original_run_cleanup(
    tmp_path, monkeypatch, mode, prior, expected,
):
    script = WORKFLOW["jobs"]["fleet-request"]["steps"][0]["run"]
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"inputs": {
        "scenario": mode, "fleet-candidate": "fixture", "fleet-original-run": prior[0],
        "fleet-original-attempt": prior[1],
    }}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    if expected:
        exec(compile(script, "<fleet-request>", "exec"), {})
    else:
        with pytest.raises(SystemExit):
            exec(compile(script, "<fleet-request>", "exec"), {})

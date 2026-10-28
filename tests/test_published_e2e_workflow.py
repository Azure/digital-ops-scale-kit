# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from siteops.reporting import _KIND as DEPLOYMENT_KIND
from tests.acceptance_helpers import calls, double_environment, install_doubles
from tests.actions_expressions import dispatch_inputs, evaluate, job_runs, truthy
from tests.native_bundle import NETWORK_BLOCK, publish_assets
from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_uv_consumers import linux_archives
from tests.shell_helpers import run_script
from tests.yaml_documents import cached_safe_load

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-test.yaml"
ACTION = ROOT / ".github" / "actions" / "setup-published-siteops" / "action.yaml"
CI = ROOT / ".github" / "workflows" / "ci.yaml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _action() -> str:
    return ACTION.read_text(encoding="utf-8")


def _step_run(name: str) -> str:
    workflow = cached_safe_load(_workflow())
    return next(
        item["run"]
        for item in workflow["jobs"]["e2e"]["steps"]
        if item.get("name") == name
    )


def _parse_inputs(**changes: str) -> subprocess.CompletedProcess[str]:
    workflow = cached_safe_load(_workflow())
    step = next(
        item
        for item in workflow["jobs"]["prep"]["steps"]
        if item.get("name") == "Parse inputs"
    )
    match = re.search(r"<<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY", step["run"], re.S)
    assert match is not None
    environment = {
        "INPUT_SCENARIO": "aio",
        "INPUT_CANDIDATE_SET": "false",
        "INPUT_RELEASES": "2609",
        "INPUT_RG": "",
        "INPUT_CLUSTER": "",
        "INPUT_UPGRADE_TO": "",
        "INPUT_SECRET_SYNC_MODES": "enabled",
        "INPUT_OPCUA_MODES": "Stable",
        "INPUT_TESTS": "",
        "INPUT_PUBLISHED_RELEASE": "",
        "INPUT_PUBLISHED_SOURCE_SHA": "",
        "INPUT_PUBLISHED_JOURNEY": "configured",
        "INPUT_SKIP_TEARDOWN": "false",
        "INPUT_KEEP_ALIVE": "0",
        "RUN_ID": "42",
        **changes,
    }
    target = {
        "resource-group": environment.pop("INPUT_RG"),
        "cluster-name": environment.pop("INPUT_CLUSTER"),
        "custom-locations-oid": environment.pop("INPUT_OID", ""),
    }
    with tempfile.TemporaryDirectory() as directory:
        event_path = Path(directory) / "event.json"
        event_path.write_text(
            json.dumps({"inputs": target}), encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-c", match.group(1)],
            cwd=ROOT,
            env={**os.environ, **environment, "GITHUB_EVENT_PATH": str(event_path)},
            capture_output=True,
            text=True,
            check=False,
        )


def _embedded_python(run: str) -> list[str]:
    return re.findall(r"<<'PY'\n(.*?)\n\s*PY(?:\n|$)", run, re.S)


def _bash_executable() -> Path:
    if os.name == "nt":
        bash = Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe"
        if not bash.is_file():
            pytest.skip("Git Bash is needed for Windows workflow checks.")
        return bash
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("Bash is needed for workflow checks.")
    return Path(resolved)


@pytest.mark.parametrize(("step", "minimum_python"), [
    ("Mask operator target inputs", 1),
    ("Compute names", 0),
    ("Snapshot RG resources (persistent mode)", 0),
    ("Preflight Arc cluster name is unused (persistent mode)", 0),
    ("Prepare guided AIO answers and plan", 5),
    ("Check published guided disabled Site routes", 2),
    ("Deploy AIO through the published engine and package", 1),
    ("Observe bounded AIO readiness", 3),
    ("Teardown (persistent mode, delta cleanup, keep RG)", 0),
    ("Select exact candidate inputs", 0),
    ("Bind candidate inputs and owned Site names", 0),
    ("Install the candidate engine outside the checkout", 1),
    ("Preflight the Site resource group", 0),
    ("Prepare the Site resource group", 0),
    ("Create the existing Key Vault", 1),
    ("Enable Secret Sync on the existing instance", 3),
    ("Remove Site resources created by this attempt", 0),
    ("Resolve E2E_LOCATION", 0),
    ("Record the Site case outcome", 0),
])
def test_guided_workflow_shell_and_embedded_python_parse_without_execution(
    step, minimum_python,
):
    run = _step_run(step)
    checked = subprocess.run(
        [str(_bash_executable()), "-n"],
        input=run, capture_output=True, text=True, timeout=15, check=False,
    )
    assert checked.returncode == 0, checked.stderr
    blocks = _embedded_python(run)
    assert len(blocks) >= minimum_python
    for number, block in enumerate(blocks, start=1):
        compile(block, f"{step} embedded Python {number}", "exec")


def test_published_mode_is_explicit_and_bounded():
    workflow = _workflow()

    for value in (
        "published-release:",
        "published-source-sha:",
        "published-journey:",
        "published-release and published-source-sha must be supplied together.",
        "Published E2E requires exactly one aio-releases entry.",
        "Published E2E requires an existing resource group",
        "Published E2E requires secret-sync-modes=disabled.",
        "Published E2E requires tests=aio-install.",
        "Published E2E cannot skip teardown.",
        "Published E2E cannot keep the cluster alive.",
    ):
        assert value in workflow


def _e2e_results(request="success", prep="success", fleet="success", groups="success", e2e="success"):
    return {"fleet-request": request, "prep": prep, "fleet": fleet, "site-groups": groups, "e2e": e2e}


@pytest.mark.parametrize(("inputs", "needs", "cancelled", "expected"), [
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "e2e", "acceptance"}, id="release-acceptance"),
    pytest.param({"scenario": "aio", "candidate": ""}, _e2e_results(request="skipped", groups="skipped"), False,
                 {"prep", "e2e"}, id="ordinary-aio-after-skipped-optional-jobs"),
    pytest.param({"scenario": "aio", "candidate": "selected"}, _e2e_results(request="skipped"), False,
                 {"prep", "site-groups", "e2e"}, id="standalone-candidate-aio"),
    pytest.param({"scenario": "fleet", "candidate": "selected"}, _e2e_results(prep="skipped", groups="skipped"), False,
                 {"fleet-request", "fleet"}, id="fleet-after-skipped-prep"),
    pytest.param({"scenario": "fleet-cleanup", "candidate": ""}, _e2e_results(prep="skipped", groups="skipped"), False,
                 {"fleet-request", "fleet-cleanup"}, id="fleet-cleanup"),
    pytest.param({"scenario": "site-cleanup", "candidate": ""}, _e2e_results(prep="skipped", groups="skipped"), False,
                 {"fleet-request", "fleet-cleanup"}, id="site-cleanup"),
    pytest.param({"scenario": "windows-installer-preflight", "candidate": ""},
                 _e2e_results(request="skipped", prep="skipped", groups="skipped"), False,
                 set(), id="windows-probe-does-not-start-acceptance-work"),
    pytest.param({"scenario": "release-acceptance", "candidate": ""}, _e2e_results(prep="failure", fleet="skipped"), False,
                 {"fleet-request", "prep", "acceptance"}, id="missing-candidate-rejected-by-prep"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(request="failure", prep="skipped", fleet="skipped"), False,
                 {"fleet-request", "site-groups", "acceptance"}, id="request-rejected"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(request="skipped", prep="skipped", fleet="skipped"), False,
                 {"fleet-request", "site-groups", "acceptance"}, id="request-skipped"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(request="cancelled", prep="skipped", fleet="skipped"), False,
                 {"fleet-request", "site-groups", "acceptance"}, id="request-cancelled"),
    # Direct results are independent here so that a passed prep cannot mask a rejected request.
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(request="failure"), False,
                 {"fleet-request", "site-groups", "e2e", "acceptance"}, id="request-rejected-despite-passed-prep"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(request="skipped"), False,
                 {"fleet-request", "site-groups", "e2e", "acceptance"}, id="request-skipped-despite-passed-prep"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(prep="failure"), False,
                 {"fleet-request", "prep", "site-groups", "acceptance"}, id="prep-rejected-despite-passed-request"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(prep="skipped"), False,
                 {"fleet-request", "prep", "site-groups", "acceptance"}, id="prep-skipped-despite-passed-request"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results(prep="cancelled"), False,
                 {"fleet-request", "prep", "site-groups", "acceptance"}, id="prep-cancelled"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(groups="failure"), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "acceptance"}, id="site-groups-rejected"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(groups="skipped"), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "acceptance"}, id="site-groups-skipped"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(groups="cancelled"), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "acceptance"}, id="site-groups-cancelled"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(fleet="failure"), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "e2e", "acceptance"}, id="aggregate-fleet-failure"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"}, _e2e_results(e2e="failure"), False,
                 {"fleet-request", "prep", "fleet", "site-groups", "e2e", "acceptance"}, id="aggregate-site-failure"),
    pytest.param({"scenario": "release-acceptance", "candidate": "selected"},
                 _e2e_results("failure", "skipped", "skipped", "skipped", "skipped"), True,
                 {"acceptance"}, id="cancelled-release-still-aggregates"),
    pytest.param({"scenario": "aio", "candidate": "selected"}, _e2e_results(), True, set(), id="cancelled-aio"),
    pytest.param({"scenario": "fleet", "candidate": "selected"}, _e2e_results(), True, set(), id="cancelled-fleet"),
    pytest.param({"scenario": "fleet-cleanup", "candidate": ""}, _e2e_results(), True, set(), id="cancelled-fleet-cleanup"),
    pytest.param({"scenario": "site-cleanup", "candidate": ""}, _e2e_results(), True, set(), id="cancelled-site-cleanup"),
    pytest.param({"scenario": "fleet-cleanup", "candidate": ""}, _e2e_results(request="failure", prep="skipped"), False,
                 {"fleet-request"}, id="fleet-cleanup-request-rejected"),
    pytest.param({"scenario": "site-cleanup", "candidate": ""}, _e2e_results(request="skipped", prep="skipped"), False,
                 {"fleet-request"}, id="site-cleanup-request-skipped"),
    pytest.param({"scenario": "fleet", "candidate": ""}, _e2e_results(request="failure", prep="skipped"), False,
                 {"fleet-request"}, id="fleet-request-rejected"),
    pytest.param({"scenario": "fleet", "candidate": ""}, _e2e_results(prep="failure"), False,
                 {"fleet-request", "fleet"}, id="ordinary-fleet-does-not-require-prep"),
    pytest.param({"scenario": "aio", "candidate": ""}, _e2e_results(request="failure", groups="failure"), False,
                 {"prep", "e2e"}, id="ordinary-aio-does-not-require-optional-admissions"),
    pytest.param({"scenario": "aio", "candidate": ""}, _e2e_results(prep="failure"), False,
                 {"prep"}, id="ordinary-aio-prep-rejected"),
    pytest.param({"scenario": "aio", "candidate": "selected"}, _e2e_results(groups="failure"), False,
                 {"prep", "site-groups"}, id="standalone-candidate-needs-site-groups"),
])
def test_e2e_jobs_gate_work_on_scenarios_and_required_admissions(inputs, needs, cancelled, expected):
    workflow = cached_safe_load(_workflow())
    declared = {
        "fleet-request": [],
        "prep": "fleet-request",
        "fleet": ["fleet-request", "prep"],
        "fleet-cleanup": "fleet-request",
        "site-groups": [],
        "e2e": ["prep", "site-groups"],
        "acceptance": ["fleet-request", "fleet", "prep", "site-groups", "e2e"],
    }
    outputs = {"prep": {"candidate-mode": "true" if inputs["candidate"] else "false"}}
    for job_id, dependencies in declared.items():
        assert workflow["jobs"][job_id].get("needs", []) == dependencies
        keys = [dependencies] if isinstance(dependencies, str) else dependencies
        assert job_runs(
            workflow, job_id, inputs=dispatch_inputs(workflow, inputs),
            needs={key: needs[key] for key in keys},
            outputs={key: value for key, value in outputs.items() if key in keys},
            cancelled=cancelled,
        ) is (job_id in expected), job_id


def _e2e_step(selector):
    steps = cached_safe_load(_workflow())["jobs"]["e2e"]["steps"]
    if selector.startswith("download:"):
        path = "${{ runner.temp }}/fleet/" + selector.removeprefix("download:")
        return next(step for step in steps if step.get("with", {}).get("path") == path)
    return next(step for step in steps if selector in (step.get("name"), step.get("id"), step.get("uses")))


@pytest.mark.parametrize(("selected", "expected"), [
    pytest.param({}, True, id="ordinary-source-templates"),
    pytest.param({"published-release": "v1.0.0"}, False, id="published-release"),
    pytest.param({"candidate": "selected"}, False, id="candidate"),
    pytest.param({"candidate": "selected", "published-release": "v1.0.0"}, False, id="both-transports"),
])
def test_prep_validates_templates_only_without_a_candidate_or_published_release(selected, expected):
    workflow = cached_safe_load(_workflow())
    step = next(step for step in workflow["jobs"]["prep"]["steps"] if step.get("name") == "Validate Bicep templates")
    assert truthy(evaluate(step["if"], {"inputs": dispatch_inputs(workflow, selected)})) is expected


@pytest.mark.parametrize("selector", [
    pytest.param("tools", id="candidate-tools"),
    pytest.param("select", id="candidate-selection"),
    pytest.param("download:admission", id="candidate-admission"),
    pytest.param("download:plan", id="candidate-plan"),
    pytest.param("download:inventory", id="candidate-inventory"),
    pytest.param("download:engine", id="candidate-engine-assets"),
    pytest.param("download:workspaces", id="candidate-workspace-assets"),
    pytest.param("Bind candidate inputs and owned Site names", id="candidate-binding"),
    pytest.param("candidate-engine", id="candidate-installation"),
    pytest.param("site-preflight", id="site-preflight"),
    pytest.param("Retain Site ownership", id="site-ownership"),
    pytest.param("Prepare the Site resource group", id="site-preparation"),
])
@pytest.mark.parametrize(("candidate_mode", "expected"), [
    pytest.param("true", True, id="candidate"),
    pytest.param("false", False, id="ordinary"),
    pytest.param("", False, id="missing-mode"),
])
def test_candidate_setup_and_site_preparation_run_only_for_candidate_mode(selector, candidate_mode, expected):
    assert truthy(evaluate(_e2e_step(selector)["if"], {"env": {"CANDIDATE_MODE": candidate_mode}})) is expected


@pytest.mark.parametrize(("candidate", "published", "expected"), [
    pytest.param("true", "true", False, id="candidate-uses-no-public-release"),
    pytest.param("true", "false", False, id="candidate-without-published-mode"),
    pytest.param("false", "true", True, id="published-release"),
    pytest.param("false", "false", False, id="source-mode"),
    pytest.param("", "true", True, id="ordinary-without-candidate-output"),
    pytest.param("false", "", False, id="missing-published-output"),
])
def test_public_release_setup_and_enrollment_exclude_candidate_cases(candidate, published, expected):
    steps = cached_safe_load(_workflow())["jobs"]["e2e"]["steps"]
    guarded = [
        step for step in steps
        if any(token in json.dumps(step) for token in ("setup-published-siteops", "source enroll", "project pin"))
    ]
    assert {step.get("name") or step.get("uses") for step in guarded} == {
        "./.github/actions/setup-published-siteops", "Prepare the published workspace project",
    }
    contexts = {"env": {"CANDIDATE_MODE": candidate}, "needs": {"prep": {"outputs": {"published-mode": published}}}}
    for step in guarded:
        assert truthy(evaluate(step["if"], contexts)) is expected


@pytest.mark.parametrize(("persistent", "candidate", "snapshot", "create"), [
    pytest.param("false", "false", False, True, id="ordinary-ephemeral"),
    pytest.param("true", "false", True, False, id="ordinary-persistent"),
    pytest.param("false", "true", True, False, id="candidate-ephemeral"),
    pytest.param("true", "true", True, False, id="candidate-persistent"),
])
def test_group_snapshot_covers_candidates_and_creation_excludes_them(persistent, candidate, snapshot, create):
    contexts = {"env": {"PERSISTENT_RG": persistent, "CANDIDATE_MODE": candidate}}
    assert truthy(evaluate(_e2e_step("Snapshot RG resources (persistent mode)")["if"], contexts)) is snapshot
    assert truthy(evaluate(_e2e_step("Create resource group (ephemeral mode)")["if"], contexts)) is create


@pytest.mark.parametrize(("candidate", "debug_user", "expected"), [
    pytest.param("false", "operator", True, id="ordinary-with-debug-user"),
    pytest.param("false", "", False, id="ordinary-without-debug-user"),
    pytest.param("true", "operator", False, id="candidate-with-debug-user"),
    pytest.param("true", "", False, id="candidate-without-debug-user"),
])
def test_debug_cluster_access_requires_an_operator_outside_candidate_mode(candidate, debug_user, expected):
    step = _e2e_step("Grant debug user cluster-admin on k3s (Arc proxy access)")
    assert truthy(evaluate(
        step["if"], {"env": {"CANDIDATE_MODE": candidate, "DEBUG_USER_OID": debug_user}},
    )) is expected


@pytest.mark.parametrize("selector", [
    pytest.param("existing-vault", id="create-existing-vault"),
    pytest.param("existing-enable", id="enable-existing-secret-sync"),
])
@pytest.mark.parametrize(("candidate", "slot", "expected"), [
    pytest.param("true", "existing", True, id="candidate-existing"),
    pytest.param("true", "enabled", False, id="candidate-enabled"),
    pytest.param("true", "disabled", False, id="candidate-disabled"),
    pytest.param("false", "existing", False, id="ordinary-existing"),
    pytest.param("false", "enabled", False, id="ordinary-enabled"),
    pytest.param("false", "disabled", False, id="ordinary-disabled"),
])
def test_existing_vault_setup_is_reserved_for_the_candidate_existing_case(selector, candidate, slot, expected):
    assert truthy(evaluate(
        _e2e_step(selector)["if"],
        {"env": {"CANDIDATE_MODE": candidate}, "matrix": {"secret-sync-mode": slot}},
    )) is expected


@pytest.mark.parametrize(("published", "candidate", "cancelled", "prior_result", "expected"), [
    pytest.param("true", "false", False, "success", True, id="ordinary-published"),
    pytest.param("true", "true", False, "success", False, id="candidate-has-its-own-receipt"),
    pytest.param("false", "false", False, "success", False, id="source-mode"),
    pytest.param("", "false", False, "success", False, id="missing-published-output"),
    pytest.param("true", "false", True, "failure", True, id="retain-cancelled-published-result"),
    pytest.param("true", "false", False, "skipped", True, id="retain-skipped-published-result"),
    pytest.param("true", "true", True, "failure", False, id="no-public-receipt-for-cancelled-candidate"),
])
def test_published_receipts_survive_failure_but_never_replace_candidate_receipts(
    published, candidate, cancelled, prior_result, expected,
):
    steps = cached_safe_load(_workflow())["jobs"]["e2e"]["steps"]
    step = next(step for step in steps if step.get("with", {}).get("name", "").startswith("published-e2e-"))
    contexts = {
        "env": {"CANDIDATE_MODE": candidate},
        "needs": {"prep": {"outputs": {"published-mode": published}}, "previous": {"result": prior_result}},
    }
    assert truthy(evaluate(step["if"], contexts, cancelled=cancelled)) is expected


@pytest.mark.parametrize(("persistent", "candidate", "skip", "cancelled", "expected"), [
    pytest.param("false", "false", False, False, True, id="ordinary-ephemeral"),
    pytest.param("true", "false", False, False, False, id="ordinary-persistent"),
    pytest.param("false", "true", False, False, False, id="candidate-ephemeral"),
    pytest.param("true", "true", False, False, False, id="candidate-persistent"),
    pytest.param("false", "false", True, False, False, id="operator-keeps-group"),
    pytest.param("false", "false", False, True, True, id="cleanup-after-cancellation"),
    pytest.param("false", "true", False, True, False, id="cancelled-candidate-uses-owned-cleanup"),
])
def test_ephemeral_teardown_cleans_ordinary_runs_without_deleting_candidate_groups(
    persistent, candidate, skip, cancelled, expected,
):
    workflow = cached_safe_load(_workflow())
    contexts = {
        "env": {"PERSISTENT_RG": persistent, "CANDIDATE_MODE": candidate},
        "inputs": dispatch_inputs(workflow, skip_teardown=skip),
        "needs": {"previous": {"result": "failure"}},
    }
    assert truthy(evaluate(
        _e2e_step("Teardown (ephemeral mode, delete RG)")["if"], contexts, cancelled=cancelled,
    )) is expected


def test_windows_capability_probe_is_opt_in_without_azure_authority():
    workflow = cached_safe_load(_workflow())
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["scenario"]["type"] == "choice"
    assert inputs["scenario"]["default"] == "aio"
    assert inputs["scenario"]["options"] == [
        "aio", "release-acceptance", "fleet", "fleet-cleanup", "site-cleanup",
        "windows-installer-preflight",
    ]

    jobs = workflow["jobs"]
    assert jobs["prep"]["needs"] == "fleet-request"
    assert jobs["e2e"]["needs"] == ["prep", "site-groups"]
    probe = jobs["windows-installer-preflight"]
    assert probe["if"] == "inputs.scenario == 'windows-installer-preflight'"
    assert probe["runs-on"] == "windows-2025"
    assert probe["permissions"] == {}
    assert not probe.get("environment")
    assert probe["timeout-minutes"] <= 10
    steps = probe["steps"]
    assert len(steps) == 1
    assert steps[0]["shell"] == "powershell"
    run = steps[0]["run"]
    for capability in ("winget.exe", "python.exe", "gh.exe", "SymbolicLink"):
        assert capability in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "throw" in run
    assert "azure/login" not in str(probe)
    assert "AZURE_" not in str(probe)
    assert "actions/checkout" not in str(probe)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser is needed.")
def test_windows_capability_probe_parses_in_native_powershell(tmp_path):
    workflow = cached_safe_load(_workflow())
    probe = workflow["jobs"]["windows-installer-preflight"]["steps"][0]["run"]
    script = tmp_path / "windows-installer-preflight.ps1"
    script.write_text(probe, encoding="utf-8")
    parsed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$tokens=$null;$errors=$null;"
            "[Management.Automation.Language.Parser]::ParseFile("
            "$env:TEST_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null;"
            "if ($errors) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }",
        ],
        env={**os.environ, "TEST_SCRIPT": str(script)},
        capture_output=True, text=True, timeout=20,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr


def test_windows_bootstrap_ci_requires_native_symlink_rejection_checks():
    workflow = cached_safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["windows-bootstrap"]
    assert job["runs-on"] == "windows-2025"
    assert job["permissions"] == {"contents": "read"}
    assert "id-token" not in str(job)
    assert not job.get("environment")
    assert job["timeout-minutes"] <= 30
    assert job["env"]["PIP_INDEX_URL"] == "https://packagefeedproxy.microsoft.io/pypi/simple/"
    assert job["env"]["SITEOPS_REQUIRE_WINDOWS_SYMLINK_REJECTION"] == "1"
    assert any("actions/checkout@" in step.get("uses", "") for step in job["steps"])
    assert any("actions/setup-python@" in step.get("uses", "") for step in job["steps"])
    preparation = next(step["run"] for step in job["steps"] if step.get("name") == "Prepare private test state")
    assert "icacls.exe" in preparation
    assert "GITHUB_ENV" in preparation
    test = next(step["run"] for step in job["steps"] if step.get("name") == "Windows bootstrap tests")
    assert "test_bootstrap_scripts.py" in test
    assert "test_uv_bootstrap_windows.py" in test
    assert "windows-pipx-launcher.py" not in test
    assert "-k 'windows or powershell'" in test
    assert "--basetemp" in test
    assert "azure/login" not in str(job)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser is needed.")
@pytest.mark.parametrize(
    ("workflow", "job", "step_name"),
    [
        (CI, "windows-bootstrap", "Install test dependencies"),
        (CI, "windows-bootstrap", "Prepare private test state"),
        (CI, "windows-bootstrap", "Windows bootstrap tests"),
    ],
)
def test_windows_automated_launcher_steps_parse_in_native_powershell(
    tmp_path, workflow, job, step_name,
):
    steps = cached_safe_load(workflow.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    script = tmp_path / "check-step.ps1"
    script.write_text(
        next(step["run"] for step in steps if step.get("name") == step_name),
        encoding="utf-8",
    )
    parsed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$tokens=$null;$errors=$null;"
            "[Management.Automation.Language.Parser]::ParseFile("
            "$env:TEST_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null;"
            "if ($errors) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }",
        ],
        env={**os.environ, "TEST_SCRIPT": str(script)},
        capture_output=True, text=True, timeout=20,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr


def test_operator_target_inputs_are_masked_before_step_headers():
    jobs = cached_safe_load(_workflow())["jobs"]
    prep = next(
        step for step in jobs["prep"]["steps"]
        if step.get("name") == "Parse inputs"
    )
    assert "INPUT_RG" not in prep["env"]
    assert "INPUT_CLUSTER" not in prep["env"]
    assert "GITHUB_EVENT_PATH" in prep["run"]
    assert 'print(f"rg_in=' not in prep["run"]
    assert 'print(f"cluster_in=' not in prep["run"]
    assert "rg-in" not in jobs["prep"]["outputs"]
    assert "cluster-in" not in jobs["prep"]["outputs"]

    steps = jobs["e2e"]["steps"]
    mask = steps[0]
    assert mask["name"] == "Mask operator target inputs"
    assert mask["id"] == "target-inputs"
    assert "GITHUB_EVENT_PATH" in mask["run"]
    assert "inputs.resource-group" not in mask["run"]
    assert "inputs.cluster-name" not in mask["run"]
    compute = next(step for step in steps if step.get("name") == "Compute names")
    assert steps.index(mask) < steps.index(compute)
    assert compute["env"]["RG_IN"] == "${{ steps.target-inputs.outputs.rg_in }}"
    assert compute["env"]["CL_IN"] == "${{ steps.target-inputs.outputs.cluster_in }}"
    assert "needs.prep.outputs.rg-in" not in _workflow()
    assert "needs.prep.outputs.cluster-in" not in _workflow()


@pytest.mark.parametrize(
    ("rg", "cluster", "expected_mask", "expected_outputs"),
    [
        (
            " rg-private-marker ",
            " arc-private-marker ",
            ["::add-mask::rg-private-marker", "::add-mask::arc-private-marker"],
            "rg_in=rg-private-marker\ncluster_in=arc-private-marker\n",
        ),
        (
            "rg%0Aprivate-marker",
            "arc%0Dprivate-marker",
            ["::add-mask::rg%250Aprivate-marker", "::add-mask::arc%250Dprivate-marker"],
            "rg_in=rg%0Aprivate-marker\ncluster_in=arc%0Dprivate-marker\n",
        ),
        (
            "rg%250Aprivate-marker",
            "",
            ["::add-mask::rg%25250Aprivate-marker"],
            "rg_in=rg%250Aprivate-marker\ncluster_in=\n",
        ),
        ("", "", [], "rg_in=\ncluster_in=\n"),
    ],
)
def test_target_input_mask_precedes_local_step_outputs(
    tmp_path, rg, cluster, expected_mask, expected_outputs,
):
    event_path = tmp_path / "event.json"
    outputs = tmp_path / "outputs.txt"
    event_path.write_text(
        json.dumps({"inputs": {"resource-group": rg, "cluster-name": cluster}}),
        encoding="utf-8",
    )
    block = _embedded_python(_step_run("Mask operator target inputs"))
    assert len(block) == 1
    result = subprocess.run(
        [sys.executable, "-c", block[0]],
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_OUTPUT": str(outputs),
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected_mask
    assert outputs.read_text(encoding="utf-8") == expected_outputs


@pytest.mark.parametrize(
    ("rg", "cluster"),
    [
        ("rg-private\n::warning::forged", ""),
        ("", "arc-private\r\n::error::forged"),
        (17, ""),
    ],
)
def test_target_input_mask_rejects_unsafe_values_without_echo(tmp_path, rg, cluster):
    event_path = tmp_path / "event.json"
    outputs = tmp_path / "outputs.txt"
    event_path.write_text(
        json.dumps({"inputs": {"resource-group": rg, "cluster-name": cluster}}),
        encoding="utf-8",
    )
    block = _embedded_python(_step_run("Mask operator target inputs"))
    result = subprocess.run(
        [sys.executable, "-c", block[0]],
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_OUTPUT": str(outputs),
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "private" not in result.stderr
    assert not outputs.exists()


@pytest.mark.parametrize(
    ("published", "cluster_in"),
    [("true", ""), ("false", "arc-private-marker")],
)
@pytest.mark.parametrize("opcua_mode", ["Stable", "Disabled"])
def test_computed_site_mask_preserves_published_and_source_targets(
    tmp_path, published, cluster_in, opcua_mode,
):
    output = tmp_path / "names.txt"
    result = subprocess.run(
        [str(_bash_executable()), "-c", _step_run("Compute names")],
        env={
            **os.environ,
            "RG_IN": "rg%0Aprivate-marker" if published == "true" else "rg-private-marker",
            "CL_IN": cluster_in,
            "RELEASE": "2608",
            "SECRET_SYNC_MODE": "disabled",
            "E2E_OPCUA_MODE": opcua_mode,
            "RUN_ID": "1234567890",
            "RUN_ATTEMPT": "1",
            "GITHUB_OUTPUT": _bash_path(output),
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
    )
    assert values["rg"] == (
        "rg%0Aprivate-marker" if published == "true" else "rg-private-marker"
    )
    if cluster_in:
        assert values["cluster"] == cluster_in
    else:
        assert values["cluster"].startswith("e2e-")
    assert values["site_name"].startswith("e2e-")
    assert values["site_name"].endswith("-opcua-off") is (opcua_mode == "Disabled")
    assert len(values["site_name"]) <= 59
    if published == "true":
        assert values["cluster"] == values["site_name"]
    assert result.stdout.splitlines() == [f"::add-mask::{values['site_name']}"]


def test_persistent_target_concurrency_key_hides_identifier():
    first = _parse_inputs(INPUT_RG="rg-private-marker")
    repeated = _parse_inputs(INPUT_RG="rg-private-marker")
    different_case = _parse_inputs(INPUT_RG="RG-PRIVATE-MARKER")
    other = _parse_inputs(INPUT_RG="rg-other-marker")
    assert all(
        result.returncode == 0
        for result in (first, repeated, different_case, other)
    )
    key = re.search(r"^rg_key=(.+)$", first.stdout, re.M).group(1)
    assert re.fullmatch(r"persistent-[0-9a-f]{64}", key)
    assert "private-marker" not in first.stdout
    assert key in repeated.stdout
    assert key in different_case.stdout
    assert key not in other.stdout


def test_published_input_contract_accepts_only_the_bounded_shape():
    result = _parse_inputs(
        INPUT_SECRET_SYNC_MODES="disabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_RG="rg-example",
    )

    assert result.returncode == 0, result.stderr
    assert "published_mode=true" in result.stdout
    assert "published_release=v0.0.4.dev20260919" in result.stdout
    assert f"published_source_sha={'a' * 40}" in result.stdout
    assert "max_parallel=1" in result.stdout
    assert "persistent=true" in result.stdout
    assert re.search(r"^rg_key=persistent-[0-9a-f]{64}$", result.stdout, re.M)
    assert "rg-example" not in result.stdout


def test_published_guided_journey_accepts_bounded_enabled_and_disabled_modes():
    result = _parse_inputs(
        INPUT_SECRET_SYNC_MODES="disabled,enabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_PUBLISHED_JOURNEY="guided",
        INPUT_RG="rg-example",
    )
    assert result.returncode == 0, result.stderr
    assert "published_journey=guided" in result.stdout
    assert 'secret_sync_modes=["disabled", "enabled"]' in result.stdout
    assert "max_parallel=1" in result.stdout


def test_guided_published_run_requires_persistent_snapshot_before_observation():
    workflow = cached_safe_load(_workflow())
    job = workflow["jobs"]["e2e"]
    assert job["needs"] == ["prep", "site-groups"]
    assert job["env"]["PERSISTENT_RG"] == "${{ needs.prep.outputs.persistent }}"
    steps = job["steps"]
    snapshot = next(
        step for step in steps
        if step.get("name") == "Snapshot RG resources (persistent mode)"
    )
    assert steps.index(snapshot) < next(
        index for index, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/connect-arc"
    )
    assert steps.index(snapshot) < next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Observe bounded AIO readiness"
    )

    options = {
        "INPUT_SECRET_SYNC_MODES": "disabled,enabled",
        "INPUT_TESTS": "aio-install",
        "INPUT_PUBLISHED_RELEASE": "v0.0.5.dev20260925",
        "INPUT_PUBLISHED_SOURCE_SHA": "a" * 40,
        "INPUT_PUBLISHED_JOURNEY": "guided",
    }
    rejected = _parse_inputs(**options, INPUT_RG="")
    assert rejected.returncode != 0
    assert not rejected.stdout
    assert "Published E2E requires an existing resource group" in rejected.stderr

    admitted = _parse_inputs(**options, INPUT_RG="rg-example")
    assert admitted.returncode == 0, admitted.stderr
    assert "persistent=true" in admitted.stdout
    assert "published_journey=guided" in admitted.stdout


def test_guided_published_journey_is_not_inferred_from_source_checkout():
    result = _parse_inputs(INPUT_PUBLISHED_JOURNEY="guided")
    assert result.returncode != 0
    assert "published-journey requires published-release" in result.stderr


def test_guided_published_journey_pins_qualified_aio_version():
    result = _parse_inputs(
        INPUT_RELEASES="2607",
        INPUT_SECRET_SYNC_MODES="enabled",
        INPUT_TESTS="aio-install",
        INPUT_PUBLISHED_RELEASE="v0.0.4.dev20260919",
        INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_PUBLISHED_JOURNEY="guided",
        INPUT_RG="rg-example",
    )
    assert result.returncode != 0
    assert "Published guided E2E currently requires aio-releases=2609" in result.stderr


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"INPUT_PUBLISHED_SOURCE_SHA": ""},
            "published-release and published-source-sha must be supplied together",
        ),
        (
            {"INPUT_PUBLISHED_RELEASE": "v0.0.4.dev20260919\npersistent=false"},
            "published-release must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RELEASES": "2608\npersistent=false"},
            "aio-releases entry must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RG": "rg-example\npersistent=false"},
            "resource-group must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_CLUSTER": "name\npersistent=false"},
            "cluster-name must be bounded text without whitespace or controls",
        ),
        (
            {"INPUT_RELEASES": "2607,2608"},
            "requires exactly one aio-releases entry",
        ),
        (
            {"INPUT_RG": ""},
            "requires an existing resource group",
        ),
        (
            {"INPUT_CLUSTER": "named"},
            "requires an existing resource group",
        ),
        (
            {"INPUT_UPGRADE_TO": "2607"},
            "does not support the upgrade phase",
        ),
        (
            {"INPUT_SECRET_SYNC_MODES": "enabled"},
            "requires secret-sync-modes=disabled",
        ),
        (
            {"INPUT_TESTS": ""},
            "requires tests=aio-install",
        ),
        (
            {"INPUT_SKIP_TEARDOWN": "true"},
            "cannot skip teardown",
        ),
        (
            {"INPUT_KEEP_ALIVE": "1"},
            "cannot keep the cluster alive",
        ),
    ],
)
def test_published_input_contract_rejects_scope_expansion(changes, message):
    values = {
        "INPUT_SECRET_SYNC_MODES": "disabled",
        "INPUT_TESTS": "aio-install",
        "INPUT_PUBLISHED_RELEASE": "v0.0.4.dev20260919",
        "INPUT_PUBLISHED_SOURCE_SHA": "a" * 40,
        "INPUT_RG": "rg-example",
        **changes,
    }
    result = _parse_inputs(**values)

    assert result.returncode != 0
    assert message in result.stderr


def test_source_input_contract_keeps_existing_parallel_default():
    result = _parse_inputs()

    assert result.returncode == 0, result.stderr
    assert "published_mode=false" in result.stdout
    assert "max_parallel=20" in result.stdout


@pytest.mark.parametrize(("secret_sync_modes", "tests"), [
    ("enabled", ""),
    ("disabled,enabled", "aio-upgrade"),
])
def test_source_e2e_accepts_2609_install_and_upgrade(secret_sync_modes, tests):
    result = _parse_inputs(
        INPUT_RELEASES="2608,2609", INPUT_UPGRADE_TO="2609",
        INPUT_SECRET_SYNC_MODES=secret_sync_modes, INPUT_TESTS=tests,
    )
    assert result.returncode == 0, result.stderr
    outputs = _outputs(result)
    assert json.loads(outputs["versions"]) == ["2608", "2609"]
    assert json.loads(outputs["secret_sync_modes"]) == secret_sync_modes.split(",")
    assert outputs["upgrade_to"] == "2609"
    assert outputs["published_mode"] == outputs["candidate_mode"] == "false"
    assert outputs["pytest_paths"] == (
        "tests/integration/test_aio_upgrade_manifest.py" if tests else "tests/integration/"
    )
    for release, upgrades in (("2608", True), ("2609", False)):
        context = {
            "needs": {"prep": {"result": "success", "outputs": {"upgrade-to": outputs["upgrade_to"]}}},
            "matrix": {"aio-release": release},
        }
        for name in ("Re-render E2E site at upgrade-to", "Run upgrade-phase integration tests"):
            assert truthy(evaluate(_e2e_step(name)["if"], context)) is upgrades


def test_default_aio_release_is_consistent_across_site_guided_and_acceptance_routes():
    workspace = ROOT / "workspaces" / "iot-operations"
    base = yaml.safe_load((workspace / "sites" / "base-site.yaml").read_text(encoding="utf-8"))
    guided = yaml.safe_load((workspace / "manifests" / "aio-install" / "inputs.yaml").read_text(encoding="utf-8"))
    secretsync = yaml.safe_load((workspace / "manifests" / "secretsync" / "inputs.yaml").read_text(encoding="utf-8"))
    workflow = cached_safe_load(_workflow())
    defaults = workflow[True]["workflow_dispatch"]["inputs"]
    selected = [
        base["properties"]["aioRelease"],
        next(item["default"] for item in guided["inputs"] if item["name"] == "aioRelease"),
        secretsync["siteDefaults"]["properties"]["aioRelease"],
        defaults["aio-releases"]["default"],
    ]
    assert selected == ["2609"] * len(selected)
    assert json.loads(_outputs(_parse_inputs())["versions"]) == ["2609"]
    accepted = _parse_inputs(INPUT_SCENARIO="release-acceptance", INPUT_CANDIDATE_SET="true")
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(_outputs(accepted)["versions"]) == ["2609"]
    guided_run = _parse_inputs(
        INPUT_PUBLISHED_RELEASE="v1.0.0b7", INPUT_PUBLISHED_SOURCE_SHA="a" * 40,
        INPUT_PUBLISHED_JOURNEY="guided", INPUT_RG="rg-example", INPUT_TESTS="aio-install",
    )
    assert guided_run.returncode == 0, guided_run.stderr
    assert json.loads(_outputs(guided_run)["versions"]) == ["2609"]


def test_source_e2e_can_cover_both_opcua_modes_with_secretsync_and_upgrade():
    result = _parse_inputs(
        INPUT_RELEASES="2608,2609", INPUT_UPGRADE_TO="2609", INPUT_OPCUA_MODES="Stable,Disabled",
        INPUT_TESTS="aio-install,enable-secretsync,sync-secrets,aio-upgrade",
    )
    assert result.returncode == 0, result.stderr
    outputs = _outputs(result)
    assert json.loads(outputs["opcua_modes"]) == ["Stable", "Disabled"]
    assert json.loads(outputs["versions"]) == ["2608", "2609"]
    assert outputs["upgrade_to"] == "2609"
    assert json.loads(outputs["secret_sync_modes"]) == ["enabled"]
    workflow = cached_safe_load(_workflow())
    assert workflow["jobs"]["e2e"]["strategy"]["matrix"]["opcua-mode"] == (
        "${{ fromJSON(needs.prep.outputs.opcua-modes) }}"
    )
    assert workflow["jobs"]["e2e"]["env"]["E2E_OPCUA_MODE"] == "${{ matrix.opcua-mode }}"
    job = workflow["jobs"]["e2e"]
    for mode in ("Stable", "Disabled"):
        context = {
            "needs": {"prep": {"outputs": {"candidate-mode": "false", "upgrade-to": "2609"}}},
            "matrix": {"aio-release": "2608", "secret-sync-mode": "enabled", "opcua-mode": mode},
        }
        name = evaluate(job["name"], context)
        assert name == f"e2e (2608, enabled{', OPC UA Disabled' if mode == 'Disabled' else ''})"
        artifacts = [
            step["with"]["name"] for step in job["steps"]
            if step.get("with", {}).get("name", "").startswith("e2e-results-")
        ]
        assert len(artifacts) == 2
        rendered = [
            re.sub(r"\$\{\{(.*?)\}\}", lambda match: str(evaluate(match[1], context)), name)
            for name in artifacts
        ]
        assert all(name.endswith(f"-opcua-{mode}") for name in rendered)


@pytest.mark.parametrize(("changes", "message"), [
    ({"INPUT_OPCUA_MODES": "Preview"}, "opcua-modes must select"),
    ({"INPUT_OPCUA_MODES": "Disabled"}, "Disabled OPC UA requires explicit tests"),
    ({"INPUT_OPCUA_MODES": "Disabled", "INPUT_TESTS": "opc-ua-solution"}, "Disabled OPC UA requires explicit tests"),
    ({"INPUT_OPCUA_MODES": "Stable,Disabled", "INPUT_TESTS": "aio-install",
      "INPUT_CLUSTER": "arc-fixed"}, "cluster-name cannot be combined"),
    ({"INPUT_OPCUA_MODES": "Disabled", "INPUT_CANDIDATE_SET": "true"},
     "Candidate acceptance selects its own Site cases"),
])
def test_opcua_matrix_rejects_invalid_or_incompatible_source_scope(changes, message):
    result = _parse_inputs(**changes)
    assert result.returncode != 0
    assert message in result.stderr


def test_published_engine_is_authenticated_and_cannot_come_from_checkout():
    action = _action()

    for value in (
        "gh attestation verify",
        ".github/workflows/_siteops-distribution.yaml",
        ".github/workflows/release.yaml",
        "refs/heads/main",
        '\"runnerEnvironment\": \"self-hosted\"',
        "--source-digest \"$EXPECTED_SOURCE_SHA\"",
        "--signer-digest \"$EXPECTED_SOURCE_SHA\"",
        '"$app_python" -I -S -B "$STATE/helper/siteops-install.py" install',
        '"$STATE/download/siteops-install.zip" "$STATE/bundle"',
        '"$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"',
        "Published E2E imported Site Ops from checkout.",
    ):
        assert value in action
    assert "pip install -e" not in action


def test_published_setup_uses_a_pinned_uv_and_fresh_packaged_helper():
    action = _action()
    install = yaml.safe_load(action)["runs"]["steps"][-1]["run"]
    parsed = subprocess.run(
        [str(_bash_executable()), "-n"], input=install,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    assert "6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f" in action
    assert '"$STATE/uv" python install 3.11.16' in action
    assert "UV_TOOL_DIR" in action and "UV_TOOL_BIN_DIR" in action
    assert '"$app_python" -I -S -B "$STATE/helper/siteops-install.py" install' in action
    assert "siteops-install.zip" in action
    assert "pipx" not in action.lower()
    assert "pip install" not in action


def _bind_controller_python(bin_dir):
    python = bin_dir / "python"
    python.write_text(
        f"#!/usr/bin/env bash\nexec {shlex.quote(Path(sys.executable).as_posix())} \"$@\"\n",
        encoding="utf-8", newline="\n",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)


def test_published_setup_rejects_foreign_release_before_tool_acquisition(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _bind_controller_python(bin_dir)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $2\" == 'api repos/example/publisher/releases/tags/test-v1' ]]; then\n"
        "  printf '%s\\n' '{\"tag_name\":\"test-v1\",\"draft\":true,\"assets\":[]}'\n"
        "elif [[ \"$1 $2\" == 'api repos/example/publisher/git/ref/tags/test-v1' ]]; then\n"
        "  printf '%s\\n' '{\"object\":{\"type\":\"commit\","
        "\"sha\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}}'\n"
        "else\n"
        "  printf 'unexpected gh call\\n' > \"$TEST_ESCAPE\"\n"
        "  exit 91\n"
        "fi\n",
        encoding="utf-8", newline="\n",
    )
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\nprintf 'unexpected curl call\\n' > \"$TEST_ESCAPE\"\nexit 92\n",
        encoding="utf-8", newline="\n",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    action = cached_safe_load(_action())["runs"]["steps"][-1]["run"]
    script = tmp_path / "published.sh"
    script.write_text(action, encoding="utf-8", newline="\n")
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    }
    environment.update({
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "TEST_ESCAPE": str(tmp_path / "unexpected-call"),
        "STATE": str(tmp_path / "state"),
        "RUNNER_TEMP": str(tmp_path),
        "EXPECTED_REPOSITORY": "example/publisher",
        "GITHUB_REPOSITORY": "example/publisher",
        "EXPECTED_RELEASE": "test-v1",
        "EXPECTED_SOURCE_SHA": "a" * 40,
        "GH_TOKEN": "fixture",
    })
    # Git Bash starts the step's processes slowly on a busy host, so the limit only stops a hang.
    result = subprocess.run(
        [str(_bash_executable()), str(script)],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode != 0
    assert "published release does not target the expected commit" in result.stderr
    assert not (tmp_path / "unexpected-call").exists()
    assert not (tmp_path / "state" / "bundle").exists()


@pytest.fixture(scope="module")
def published_native_archives():
    return linux_archives()


@pytest.mark.skipif(sys.platform != "linux", reason="The published action uses native Linux uv.")
@pytest.mark.parametrize("missing", ["SITEOPS_TEST_UV_ARCHIVE", "SITEOPS_TEST_UV_PYTHON_ARCHIVE"])
def test_native_published_setup_required_archives_fail_closed(
    published_native_archives, monkeypatch, missing,
):
    assert all(path.is_file() for path in published_native_archives)
    monkeypatch.setenv("SITEOPS_REQUIRE_LINUX_UV", "1")
    monkeypatch.delenv(missing)
    with pytest.raises(pytest.fail.Exception, match=missing):
        linux_archives()


@pytest.mark.skipif(sys.platform != "linux", reason="The published action uses native Linux uv.")
@pytest.mark.parametrize("foreign_command", [False, True])
def test_native_published_setup_installs_only_with_an_unoccupied_command(
    tmp_path, bundle_factory, published_native_archives, foreign_command,
):
    uv_archive, python_archive = published_native_archives
    root, manifest = bundle_factory(92)
    archive, _ = publish_assets(root, manifest, tmp_path / "assets")
    proof = archive.with_name(archive.name + ".attestation.jsonl")
    proof.write_text('{"fixture":"proof"}\n', encoding="utf-8")
    state = tmp_path / "published-state"
    release = {
        "tag_name": "test-v1",
        "draft": False,
        "assets": [
            {
                "name": asset.name,
                "size": asset.stat().st_size,
                "digest": "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest(),
            }
            for asset in (archive, proof)
        ],
    }
    (tmp_path / "release.json").write_text(json.dumps(release), encoding="utf-8")
    (tmp_path / "tag.json").write_text(
        json.dumps({"object": {"type": "commit", "sha": manifest.source_sha}}),
        encoding="utf-8",
    )
    certificate = {
        "subjectAlternativeName": (
            "https://github.com/example/publisher/.github/workflows/"
            "_siteops-distribution.yaml@refs/heads/main"
        ),
        "issuer": "https://token.actions.githubusercontent.com",
        "sourceRepositoryURI": "https://github.com/example/publisher",
        "sourceRepositoryDigest": manifest.source_sha,
        "sourceRepositoryRef": "refs/heads/main",
        "buildSignerDigest": manifest.source_sha,
        "buildConfigURI": "https://github.com/example/publisher/.github/workflows/release.yaml@refs/heads/main",
        "buildConfigDigest": manifest.source_sha,
        "runnerEnvironment": "self-hosted",
    }
    (tmp_path / "verification.json").write_text(
        json.dumps([{
            "verificationResult": {
                "mediaType": "application/vnd.dev.sigstore.verificationresult+json;version=0.1",
                "signature": {"certificate": certificate},
            },
        }]),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _bind_controller_python(bin_dir)
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$1 ${2:-}" in
  "api repos/example/publisher/releases/tags/test-v1")
    cat "$TEST_RELEASE" ;;
  "api repos/example/publisher/git/ref/tags/test-v1")
    cat "$TEST_TAG" ;;
  "release download")
    [[ "$3" == test-v1 ]] || exit 91
    while (($#)); do
      if [[ "$1" == --dir ]]; then
        [[ "$2" == "$STATE/download" ]] || exit 92
        cp "$TEST_BUNDLE" "$TEST_PROOF" "$2/"
        exit 0
      fi
      shift
    done
    exit 93 ;;
  "attestation verify")
    [[ "$3" == "$STATE/download/siteops-install.zip" ]] || exit 94
    cat "$TEST_VERIFICATION" ;;
  "attestation trusted-root")
    printf '{"trusted":"fixture"}\\n' ;;
  *) printf 'unexpected gh call\\n' > "$TEST_ESCAPE"; exit 95 ;;
esac
""",
        encoding="utf-8", newline="\n",
    )
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[[ " $* " == *"https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-unknown-linux-gnu.tar.gz"* ]] || exit 96
output=""
while (($#)); do
  if [[ "$1" == --output ]]; then output="$2"; shift 2; continue; fi
  shift
done
[[ "$output" == "$STATE/uv.tar.gz" ]] || exit 97
cp "$TEST_UV_ARCHIVE" "$output"
tar -xzf "$TEST_PYTHON_ARCHIVE" -C "$STATE/python"
mv "$STATE/python/python" "$STATE/python/cpython-3.11.16-linux-x86_64-gnu"
if [[ "${TEST_FOREIGN_COMMAND:-0}" == 1 ]]; then
  printf '#!/usr/bin/env bash\\nprintf "FOREIGN_EXECUTED" >> "$TEST_ESCAPE"\\nexit 99\\n' > "$STATE/bin/siteops"
  chmod 700 "$STATE/bin/siteops"
fi
""",
        encoding="utf-8", newline="\n",
    )
    for tool in (gh, curl):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "published.sh"
    script.write_text(
        cached_safe_load(_action())["runs"]["steps"][-1]["run"],
        encoding="utf-8", newline="\n",
    )
    checkout = tmp_path / "source-checkout"
    checkout.mkdir()
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    }
    environment.update({
        **NETWORK_BLOCK,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "EXPECTED_REPOSITORY": manifest.repository,
        "GITHUB_REPOSITORY": manifest.repository,
        "EXPECTED_RELEASE": "test-v1",
        "EXPECTED_SOURCE_SHA": manifest.source_sha,
        "RUNNER_TEMP": str(tmp_path),
        "STATE": str(state),
        "GH_TOKEN": "fixture",
        "GITHUB_WORKSPACE": str(checkout),
        "GITHUB_ENV": str(tmp_path / "github-env"),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "TEST_RELEASE": str(tmp_path / "release.json"),
        "TEST_TAG": str(tmp_path / "tag.json"),
        "TEST_VERIFICATION": str(tmp_path / "verification.json"),
        "TEST_BUNDLE": str(archive),
        "TEST_PROOF": str(proof),
        "TEST_UV_ARCHIVE": str(uv_archive),
        "TEST_PYTHON_ARCHIVE": str(python_archive),
        "TEST_ESCAPE": str(tmp_path / "foreign-executed"),
        "TEST_FOREIGN_COMMAND": "1" if foreign_command else "0",
    })
    result = subprocess.run(
        [str(_bash_executable()), "--noprofile", "--norc", str(script)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=300,
    )
    assert not (tmp_path / "foreign-executed").exists()
    assert str(tmp_path) not in result.stdout + result.stderr
    if foreign_command:
        assert result.returncode != 0
        assert "authenticated published bundle could not be installed" in result.stdout
        assert (state / "bin/siteops").read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
        assert not (state / "tools/siteops").exists()
        assert not (tmp_path / "github-output").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "github-output").read_text(encoding="utf-8") == (
            f"version={manifest.version}\n"
        )
        assert (state / "installed.json").is_file()
        assert json.loads((state / "installed.json").read_text(encoding="utf-8")) == {
            "version": manifest.version,
            "wheel": manifest.application_wheel,
        }
        assert "version_info = 3.11.16" in (
            state / "tools/siteops/pyvenv.cfg"
        ).read_text(encoding="utf-8")
        assert (tmp_path / "github-path").read_text(encoding="utf-8") == str(state / "bin") + "\n"
        assert f"SITEOPS_E2E_ENGINE_VERSION={manifest.version}\n" in (
            tmp_path / "github-env"
        ).read_text(encoding="utf-8")


def test_published_setup_finishes_before_azure_provisioning():
    workflow = _workflow()

    setup = workflow.index("uses: ./.github/actions/setup-published-siteops")
    project = workflow.index("- name: Prepare the published workspace project")
    cluster = workflow.index("uses: ./.github/actions/create-k3s-cluster")
    login = workflow.index("uses: azure/login@")
    plan = workflow.index("- name: Render and plan the published-package operator Site")
    connect = workflow.index("uses: ./.github/actions/connect-arc")
    assert setup < project < cluster < login < plan < connect


def test_published_workspace_uses_project_pin_and_separate_sites():
    workflow = _workflow()

    step = workflow[
        workflow.index("- name: Prepare the published workspace project"):
        workflow.index("uses: ./.github/actions/create-k3s-cluster")
    ]
    for value in (
        'mkdir \"$project\" \"$gh_config\"',
        "unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN",
        "project pin \"$project\"",
        "SITEOPS_REDACT_OUTPUT=0 siteops",
        '--source \"github:$GITHUB_REPOSITORY\"',
        '--release \"$RELEASE\"',
        "--release-workspace workspaces/iot-operations",
        "Project pin created operator Site content.",
    ):
        assert value in step
    assert 'mkdir "$project" "$cache"' not in step

    assert 'if [[ "$PUBLISHED_JOURNEY" == "guided" ]]' in step
    assert 'export XDG_CONFIG_HOME="$RUNNER_TEMP/published-source-config"' in step
    assert '"XDG_CONFIG_HOME=$XDG_CONFIG_HOME" >> "$GITHUB_ENV"' in step
    assert 'source enroll guided --source "github:$GITHUB_REPOSITORY"' in step
    assert "if ! siteops \\\n" in step.split(
        'source enroll guided --source "github:$GITHUB_REPOSITORY"'
    )[0]
    assert 'trust_args=(--approved-source guided)' in step
    assert 'trust_args=(--trust-policy "$SITEOPS_E2E_POLICY"' in step
    assert '"${trust_args[@]}"' in step

    plan_step = workflow[
        workflow.index("- name: Render and plan the published-package operator Site"):
        workflow.index("- name: Preflight Arc cluster name is unused")
    ]
    for value in (
        "--template tests/e2e/sites/e2e-published.yaml.tmpl",
        'mkdir \"$SITEOPS_E2E_PROJECT/sites\"',
        "plan aio-install",
        "--describe",
        "--offline-content",
        "Published package planning changed the operator Site.",
    ):
        assert value in plan_step
    assert "--read-resources" not in plan_step
    assert "raw.find" not in plan_step


def test_configured_published_plan_requires_one_describe_document(tmp_path):
    blocks = _embedded_python(_step_run("Render and plan the published-package operator Site"))
    assert len(blocks) == 1
    plan = tmp_path / "plan.json"
    document = {
        "apiVersion": "siteops/v1alpha1", "kind": "DeploymentPlan",
        "projection": "publishable", "status": "planned",
        "intent": "describe", "executable": False,
        "engine": {"version": "1.0.0b1"},
        "summary": {"targetCount": 1, "operationCount": 9},
    }

    def check():
        return subprocess.run(
            [sys.executable, "-c", blocks[0], str(plan), "1.0.0b1"],
            capture_output=True, text=True, check=False,
        )

    plan.write_text(json.dumps(document), encoding="utf-8")
    assert check().returncode == 0
    plan.write_text("safe progress\n" + json.dumps(document), encoding="utf-8")
    assert check().returncode != 0
    document["intent"] = "executable"
    plan.write_text(json.dumps(document), encoding="utf-8")
    assert check().returncode != 0


def test_guided_published_plan_waits_for_arc_and_keeps_private_data_local():
    workflow = _workflow()
    connected = workflow.index("uses: ./.github/actions/connect-arc")
    guided = workflow.index("- name: Prepare guided AIO answers and plan")
    deployed = workflow.index("- name: Deploy AIO through the published engine and package")
    assert connected < guided < deployed
    step = _step_run("Prepare guided AIO answers and plan")
    for value in (
        "umask 077",
        "published-answers.json",
        "--project \"$SITEOPS_E2E_PROJECT\"",
        "--approved-source guided",
        "--offline-content",
        "SITEOPS_REDACT_OUTPUT=0",
        "expected_steps",
        "read-required",
        "requirement-unmet",
    ):
        assert value in step
    assert "--read-resources \\" not in step
    assert "cat \"$RUNNER_TEMP/" not in step
    assert " -w workspaces/" not in step


def test_guided_disabled_cell_checks_manual_inline_and_configured_site_without_reads():
    workflow = cached_safe_load(_workflow())
    steps = workflow["jobs"]["e2e"]["steps"]
    control = next(
        item for item in steps
        if item.get("name") == "Check published guided disabled Site routes"
    )
    assert control["if"] == (
        "needs.prep.outputs.published-mode == 'true' && "
        "needs.prep.outputs.published-journey == 'guided' && "
        "matrix.secret-sync-mode == 'disabled'"
    )
    assert steps.index(control) > next(
        index for index, item in enumerate(steps)
        if item.get("name") == "Prepare guided AIO answers and plan"
    )
    assert steps.index(control) < next(
        index for index, item in enumerate(steps)
        if item.get("name") == "Deploy AIO through the published engine and package"
    )
    assert control["env"]["E2E_LOCATION"] == "${{ steps.loc.outputs.location }}"
    run = control["run"]
    for value in (
        'published-manual-answers.json',
        '--input-file "$RUNNER_TEMP/published-manual-answers.json"',
        '--input "siteName=$E2E_SITE_NAME"',
        'inputs aio-install --offline-content',
        '--save-site "$site"',
        '-l "name=$E2E_SITE_NAME"',
        'SITEOPS_REDACT_OUTPUT=0 siteops',
        'published-guided-plan.json',
        'site_before="$(sha256sum "$site"',
        'site_after="$(sha256sum "$site"',
    ):
        assert value in run
    assert "--read-resources" not in run
    assert "cat \"$RUNNER_TEMP/" not in run
    assert " -w workspaces/" not in run


def test_guided_disabled_manual_answers_are_complete_and_resource_free(tmp_path):
    script = _embedded_python(_step_run("Check published guided disabled Site routes"))[0]
    resource_id = (
        "/subscriptions/fixture/resourceGroups/fixture-rg/providers/"
        "Microsoft.Kubernetes/connectedClusters/fixture-arc"
    )
    source = {
        "apiVersion": "siteops.inputs/v1",
        "kind": "SiteInputValues",
        "values": {
            "siteName": "fixture-site", "subscription": None, "resourceGroup": None,
            "location": None, "clusterName": None, "environment": "e2e",
            "country": "US", "enableSecretSync": False, "aioRelease": "2608",
            "brokerMemoryProfile": "Low", "cluster": resource_id,
        },
    }
    environment = {
        "RUNNER_TEMP": str(tmp_path), "E2E_SUBSCRIPTION": "fixture",
        "E2E_RESOURCE_GROUP": "fixture-rg", "E2E_LOCATION": "eastus",
        "E2E_CLUSTER_NAME": "fixture-arc",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    (tmp_path / "published-answers.json").write_text(
        json.dumps(source), encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    manual = json.loads((tmp_path / "published-manual-answers.json").read_text(
        encoding="utf-8",
    ))
    assert manual["values"]["cluster"] is None
    assert manual["values"]["subscription"] == "fixture"
    assert manual["values"]["resourceGroup"] == "fixture-rg"
    assert manual["values"]["location"] == "eastus"
    assert manual["values"]["clusterName"] == "fixture-arc"
    assert manual["values"]["enableSecretSync"] is False
    assert manual["values"]["siteName"] == source["values"]["siteName"]
    assert resource_id not in result.stdout + result.stderr

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    source["values"]["enableSecretSync"] = True
    (invalid_root / "published-answers.json").write_text(
        json.dumps(source), encoding="utf-8",
    )
    invalid = subprocess.run(
        [sys.executable, "-c", script],
        env={**environment, "RUNNER_TEMP": str(invalid_root)},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert invalid.returncode != 0
    assert "Guided disabled answers are invalid." in invalid.stderr
    assert resource_id not in invalid.stderr
    assert not (invalid_root / "published-manual-answers.json").exists()


def test_guided_disabled_plan_comparison_rejects_wrong_target_and_operations(tmp_path):
    scripts = _embedded_python(_step_run("Check published guided disabled Site routes"))
    comparison = next(script for script in scripts if "expected_operations" in script)
    site = "private-site"
    steps = (
        "global-edge-site", "edge-site", "schema-registry", "adr-ns",
        "aio-enablement", "aio-instance", "schema-registry-role", "resolve-aio",
        "secretsync",
    )

    def document(selection):
        return {
            "apiVersion": "siteops/v1alpha1",
            "kind": "DeploymentPlan",
            "status": "planned",
            "executable": True,
            "engine": {"version": "fixture-engine"},
            "summary": {"targetCount": 1, "operationCount": 9},
            "plan": {
                "manifest": (
                    {"cliSelector": f"name={site}"}
                    if selection == "configured" else
                    {"targetSelection": "explicit-site", "cliSelector": None}
                ),
                "submission": {"mode": "arm-json", "compilationBinding": "package-artifact"},
                "targets": [{
                    "name": site,
                    "kind": "resource-group",
                    "subscription": "fixture",
                    "resourceGroup": "fixture-rg",
                    "location": "eastus",
                    "operations": [{
                        "identity": {"target": site, "step": step},
                        "kind": "deployment", "scope": "resource-group",
                        "disposition": "skip" if step in {
                            "global-edge-site", "edge-site", "resolve-aio", "secretsync",
                        } else "execute",
                    } for step in steps],
                }],
            },
        }

    files = {
        "published-guided-plan.json": document("resource"),
        "published-manual-plan.json": document("manual"),
        "published-inline-plan.json": document("inline"),
        "published-configured-plan.json": document("configured"),
    }
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    environment = {
        "RUNNER_TEMP": str(tmp_path),
        "E2E_SITE_NAME": site,
        "SITEOPS_E2E_ENGINE_VERSION": "fixture-engine",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]

    def compare():
        return subprocess.run(
            [sys.executable, "-c", comparison], env=environment,
            capture_output=True, text=True, timeout=15, check=False,
        )

    assert compare().returncode == 0
    files["published-manual-plan.json"]["plan"]["targets"][0]["name"] = "wrong-site"
    (tmp_path / "published-manual-plan.json").write_text(
        json.dumps(files["published-manual-plan.json"]), encoding="utf-8",
    )
    wrong_target = compare()
    assert wrong_target.returncode != 0
    assert "wrong-site" not in wrong_target.stderr

    files["published-manual-plan.json"] = document("manual")
    (tmp_path / "published-manual-plan.json").write_text(
        json.dumps(files["published-manual-plan.json"]), encoding="utf-8",
    )
    files["published-configured-plan.json"]["plan"]["targets"][0]["operations"][5][
        "disposition"
    ] = "skip"
    (tmp_path / "published-configured-plan.json").write_text(
        json.dumps(files["published-configured-plan.json"]), encoding="utf-8",
    )
    wrong_operation = compare()
    assert wrong_operation.returncode != 0
    assert site not in wrong_operation.stderr

    files["published-configured-plan.json"] = document("configured")
    files["published-configured-plan.json"]["plan"]["manifest"]["cliSelector"] = None
    (tmp_path / "published-configured-plan.json").write_text(
        json.dumps(files["published-configured-plan.json"]), encoding="utf-8",
    )
    wrong_selector = compare()
    assert wrong_selector.returncode != 0
    assert site not in wrong_selector.stderr


def test_guided_published_deploy_uses_answers_and_read_gate():
    step = _step_run("Deploy AIO through the published engine and package")
    assert 'PUBLISHED_JOURNEY' in step
    assert '--input-file "$RUNNER_TEMP/published-answers.json"' in step
    assert "--read-resources" not in step
    assert "--yes" in step
    assert 'name=$SITE_NAME' in step
    assert 'trust_args=(--approved-source guided)' in step


def test_published_deploy_failure_reports_only_codes_and_counts(tmp_path):
    failed = {
        "apiVersion": "siteops/v1alpha1", "kind": DEPLOYMENT_KIND, "projection": "publishable", "status": "failed",
        "exitCode": 1, "engine": {"name": "siteops", "version": "1.0.0b7"},
        "summary": {"sites": {"total": 1, "counts": {"failed": 1}},
                    "operations": {"total": 3, "counts": {"failed": 1, "skipped": 2, "succeeded": 0}}},
        "diagnostics": [{"code": "run.diagnostic", "severity": "error", "summary": "private-marker summary"},
                        {"code": "has spaces private-marker", "severity": "error", "summary": "x"}],
    }
    history = [
        {"timestamp": "2026-10-10T20:02:14.1234567+00:00", "state": "Failed", "code": "InvalidTemplateDeployment",
         "details": ["StorageAccountNameInvalid", "private-marker detail with spaces"]},
        {"timestamp": "2026-10-10T19:00:00+00:00", "state": "Failed", "code": "OlderAttempt", "details": []},
        {"timestamp": "2026-10-10T20:03:00Z", "state": "Succeeded", "code": None, "details": None},
    ]
    install_doubles(tmp_path, {
        "siteops": [{"match": ["deploy", "aio-install"], "code": 1, "stdout": json.dumps(failed),
                     "stderr": "provider message rg-private-marker\n"}],
        "fleet-python": [{"match": ["scripts/coordinate-release-fleet.py", "policy"], "stdout": "policy\n"}],
        "az": [{"match": ["deployment", "group", "list"], "stdout": json.dumps(history)}],
    })
    (tmp_path / "bin" / "python").write_text(
        f"#!/usr/bin/env bash\nexec {shlex.quote(Path(sys.executable).as_posix())} \"$@\"\n", encoding="utf-8",
        newline="\n",
    )
    result = run_script(_step_run("Deploy AIO through the published engine and package"), tmp_path, {
        **double_environment(tmp_path), "RUNNER_TEMP": tmp_path.as_posix(), "PUBLISHED_JOURNEY": "guided",
        "SITEOPS_E2E_PROJECT": (tmp_path / "project").as_posix(), "SITEOPS_E2E_ENGINE_VERSION": "1.0.0b7",
        "RELEASE": "", "SOURCE_SHA": "a" * 40, "SITE_NAME": "site-private-marker",
        "SITEOPS_E2E_TRUST": "policy", "SITEOPS_E2E_STATE": (tmp_path / "state").as_posix(),
        "FLEET_PYTHON": "fleet-python", "E2E_RESOURCE_GROUP": "rg-private-marker",
        # 2026-10-10T20:00:00Z
        "JOB_START_EPOCH": "1791662400",
    })
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "::error::The published Site Ops deployment failed." in result.stdout
    assert "Deployment result: status failed, operations failed 1, skipped 2." in result.stdout
    assert "Deployment diagnostics: run.diagnostic, unrecognized." in result.stdout
    assert ("Azure deployment: Failed, error InvalidTemplateDeployment, details StorageAccountNameInvalid, "
            "unrecognized.") in result.stdout
    assert "OlderAttempt" not in output
    assert "private-marker" not in output


@pytest.mark.parametrize(("mode", "journey", "expected"), [
    pytest.param("candidate", "guided", ["--trust-policy", "{policy}/policy.json", "--trusted-root", "{policy}/root.json"],
                 id="candidate-policy-without-published-trust"),
    pytest.param("published", "guided", ["--approved-source", "guided"], id="published-guided"),
    pytest.param("published", "configured", ["--trust-policy", "{state}/policy.json", "--trusted-root",
                                             "{state}/trusted-root.json"], id="published-configured"),
])
def test_published_deploy_reads_only_the_trust_inputs_its_mode_provides(tmp_path, mode, journey, expected):
    # Each setup route exports its own trust inputs, and the step runs with `set -u`.
    policy, state = (tmp_path / "policy-1").as_posix(), (tmp_path / "published").as_posix()
    version = "1.0.0b7+build.42.1.g" + "a" * 12
    document = {
        "apiVersion": "siteops/v1alpha1", "kind": DEPLOYMENT_KIND, "projection": "publishable",
        "status": "succeeded", "exitCode": 0, "engine": {"name": "siteops", "version": version},
        "summary": {"sites": {"total": 1, "counts": {"succeeded": 1}},
                    "operations": {"total": 5, "counts": {"succeeded": 5}}},
    }
    install_doubles(tmp_path, {
        "siteops": [{"match": ["deploy", "aio-install", "--yes", "--offline-content"], "stdout": json.dumps(document)}],
        "fleet-python": [{"match": ["scripts/coordinate-release-fleet.py", "policy"], "stdout": policy + "\n"}],
    })
    (tmp_path / "bin" / "python").write_text(
        f"#!/usr/bin/env bash\nexec {shlex.quote(Path(sys.executable).as_posix())} \"$@\"\n", encoding="utf-8",
        newline="\n",
    )
    environment = {
        **double_environment(tmp_path), "RUNNER_TEMP": tmp_path.as_posix(), "PUBLISHED_JOURNEY": journey,
        "SITEOPS_E2E_PROJECT": (tmp_path / "project").as_posix(), "SITEOPS_E2E_ENGINE_VERSION": version,
        "RELEASE": "" if mode == "candidate" else "v1.0.0b7", "SOURCE_SHA": "a" * 40, "SITE_NAME": "site-one",
    }
    if mode == "candidate":
        environment.update({"SITEOPS_E2E_TRUST": "policy", "SITEOPS_E2E_STATE": (tmp_path / "state").as_posix(),
                            "FLEET_PYTHON": "fleet-python"})
    else:
        environment.update({"SITEOPS_E2E_POLICY": f"{state}/policy.json",
                            "SITEOPS_E2E_TRUSTED_ROOT": f"{state}/trusted-root.json"})
    result = run_script(_step_run("Deploy AIO through the published engine and package"), tmp_path, environment)
    assert result.returncode == 0, result.stdout + result.stderr
    deploys = [arguments for name, arguments in calls(tmp_path) if name == "siteops"]
    assert len(deploys) == 1
    trust = [value.format(policy=policy, state=state) for value in expected]
    assert deploys[0][deploys[0].index("--project") + 2:deploys[0].index("deploy")] == trust
    target = (["--input-file", (tmp_path / "published-answers.json").as_posix()] if journey == "guided"
              else ["-l", "name=site-one"])
    assert all(token in deploys[0] for token in target)


def test_published_deploy_uses_only_the_pin_offline():
    workflow = _workflow()

    deploy = workflow[
        workflow.index("- name: Deploy AIO through the published engine and package"):
        workflow.index("- name: Observe bounded AIO readiness")
    ]
    for value in (
        '--project \"$SITEOPS_E2E_PROJECT\"',
        '--trust-policy \"$SITEOPS_E2E_POLICY\"',
        '--trusted-root \"$SITEOPS_E2E_TRUSTED_ROOT\"',
        "deploy aio-install",
        "--yes",
        "--offline-content",
        '"apiVersion") != "siteops/v1alpha1"',
        f'"kind") != "{DEPLOYMENT_KIND}"',
        '"projection") != "publishable"',
        '"status") != "succeeded"',
        'result.get("engine", {}).get("version") != sys.argv[5]',
    ):
        assert value in deploy
    assert "workspaces/iot-operations" not in deploy
    assert "raw.find" not in deploy


def test_published_deployment_receipt_accepts_the_real_run_envelope(tmp_path):
    blocks = _embedded_python(
        _step_run("Deploy AIO through the published engine and package")
    )
    assert len(blocks) == 1
    version = "1.0.0b1+build.42.1.g" + "a" * 12
    source = "a" * 40
    raw = tmp_path / "deployment.txt"
    output = tmp_path / "receipt.json"
    document = {
        "apiVersion": "siteops/v1alpha1",
        "kind": DEPLOYMENT_KIND,
        "projection": "publishable",
        "status": "succeeded",
        "exitCode": 0,
        "engine": {"name": "siteops", "version": version},
        "summary": {
            "sites": {"total": 1, "counts": {"succeeded": 1}},
            "operations": {"total": 5, "counts": {"succeeded": 5}},
        },
    }
    raw.write_text(json.dumps(document), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            blocks[0],
            str(raw),
            str(output),
            "v-test",
            source,
            version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert receipt == {
        "apiVersion": "siteops.e2e/v1",
        "kind": "PublishedPackageDeployment",
        "release": "v-test",
        "sourceSha": source,
        "engineVersion": version,
        "siteCount": 1,
        "operationCount": 5,
        "succeededOperations": 5,
        "status": "succeeded",
    }

    raw.write_text("safe progress\n" + json.dumps(document), encoding="utf-8")
    noisy = subprocess.run(
        [sys.executable, "-c", blocks[0], str(raw), str(output),
         "v-test", source, version],
        capture_output=True, text=True, check=False,
    )
    assert noisy.returncode != 0
    assert "JSONDecodeError" in noisy.stderr

    document["kind"] = "DeploymentResult"
    raw.write_text(json.dumps(document), encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            "-c",
            blocks[0],
            str(raw),
            str(output),
            "v-test",
            source,
            version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "deployment result is incomplete" in rejected.stderr


def test_default_source_e2e_remains_separate():
    workflow = _workflow()

    assert (
        "- uses: ./.github/actions/setup-siteops\n"
        "        if: needs.prep.outputs.published-mode != 'true'\n"
    ) in workflow
    assert (
        "- name: Run E2E integration tests\n"
        "        if: needs.prep.outputs.published-mode != 'true'\n"
    ) in workflow
    assert (
        "- uses: actions/upload-artifact@"
        in workflow
        and "always() && needs.prep.outputs.published-mode != 'true'" in workflow
    )


@pytest.mark.parametrize(("path", "manifest"), [
    (".github/workflows/_siteops-deploy.yaml", "$INPUT_MANIFEST"),
    (".pipelines/templates/siteops-deploy.yaml", "$MANIFEST"),
])
def test_deployment_wrappers_approve_only_the_run_not_the_plan(path, manifest):
    workflow = cached_safe_load((ROOT / path).read_text(encoding="utf-8"))
    text = (ROOT / path).read_text(encoding="utf-8")
    assert workflow and f'plan "{manifest}"' in text
    plan, deploy = text.split(f'deploy "{manifest}"', 1)
    assert "--yes" not in plan
    arguments = deploy.split(")", 1)[0]
    assert re.search(r"(?m)^\s+--yes$", arguments)
    assert "--output json" in arguments
    assert "--projection publishable" in arguments


def test_published_readiness_is_bounded_and_existing_teardown_is_retained():
    workflow = _workflow()

    for value in (
        "microsoft.iotoperations/instances",
        "microsoft.deviceregistry/schemaregistries",
        "microsoft.deviceregistry/namespaces",
        "python scripts/observe-site-readiness.py",
        "published-kubernetes-readiness.json",
        '"activePods": kubernetes["activePods"]',
        '"readyActivePods": kubernetes["readyActivePods"]',
        "PublishedPackageReadiness",
        "Snapshot RG resources (persistent mode)",
        "Teardown (persistent mode, delta cleanup, keep RG)",
        "comm -23",
        "operator-owned",
    ):
        assert value in workflow


def test_2609_qualification_documents_a_second_full_stable_dispatch():
    guide = (ROOT / "docs" / "e2e-testing.md").read_text(encoding="utf-8")
    dispatches = []
    for block in re.findall(r"```bash\n(.*?)```", guide, re.DOTALL):
        tokens = shlex.split(block.replace("\\\n", " "))
        if tokens[:4] != ["gh", "workflow", "run", "e2e-test.yaml"]:
            continue
        fields = dict(tokens[index + 1].split("=", 1) for index, token in enumerate(tokens) if token == "-f")
        if fields.get("aio-releases") == "2609" and fields.get("opcua-modes") == "Stable":
            dispatches.append(fields)
    assert len(dispatches) == 1
    fields = dispatches[0]
    assert fields.get("tests", "") == ""
    assert not fields.get("published-release") and not fields.get("candidate")
    result = _parse_inputs(INPUT_RELEASES=fields["aio-releases"], INPUT_OPCUA_MODES=fields["opcua-modes"])
    assert result.returncode == 0, result.stderr
    assert _outputs(result)["pytest_paths"] == "tests/integration/"


def test_published_guide_explains_private_persistent_cleanup_diagnostics():
    guide = " ".join(
        (ROOT / "docs" / "e2e-testing.md").read_text(encoding="utf-8").lower().split()
    )
    assert (
        "for the published persistent snapshot and teardown, public logs and "
        "summaries report fixed reasons and aggregate counts"
    ) in guide
    assert "those steps keep resource id lists and provider diagnostics in private runner files" in guide


def _bash_path(path: Path) -> str:
    return f"/{path.drive[0].lower()}{path.as_posix()[2:]}" if os.name == "nt" else str(path)


def _run_persistent_step(name: str, tmp_path: Path, mode: str):
    bash = _bash_executable()
    tools = tmp_path / "tools"
    tools.mkdir()
    resource_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/"
        "resourceGroups/rg-private-marker/providers/Microsoft.DeviceRegistry/"
        "schemaRegistries/private-resource"
    )
    az = tools / "az"
    az.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_AZ_LOG"
if [[ "$1 $2" == "resource list" ]]; then
  if [[ "$FAKE_AZ_MODE" == "snapshot-failure" ]]; then
    printf 'provider error for %s\\n' "$FAKE_AZ_RESOURCE_ID" >&2
    exit 1
  fi
  if [[ "$FAKE_AZ_MODE" == "snapshot-success" ]]; then
    printf '%s\\n' "$FAKE_AZ_RESOURCE_ID"
    exit 0
  fi
  n=0
  [[ ! -f "$FAKE_AZ_COUNT" ]] || n=$(cat "$FAKE_AZ_COUNT")
  printf '%s\\n' "$((n + 1))" > "$FAKE_AZ_COUNT"
  if [[ "$FAKE_AZ_MODE" == "teardown-failure" || "$n" == "0" ]]; then
    printf '%s\\n' "$FAKE_AZ_RESOURCE_ID"
  fi
  exit 0
fi
if [[ "$1 $2" == "resource delete" ]]; then
  if [[ "$FAKE_AZ_MODE" == "teardown-failure" ]]; then
    printf 'provider error for %s\\n' "$FAKE_AZ_RESOURCE_ID" >&2
    exit 1
  fi
  printf 'provider accepted %s\\n' "$FAKE_AZ_RESOURCE_ID"
  exit 0
fi
if [[ "$1 $2" == "extension add" ]]; then
  exit 0
fi
if [[ "$1 $2" == "connectedk8s list" ]]; then
  if [[ "$FAKE_AZ_MODE" == "preflight-failure" ]]; then
    printf 'provider error for %s\\n' "$FAKE_AZ_RESOURCE_ID" >&2
    exit 1
  fi
  printf '0\\n'
  exit 0
fi
printf 'Unexpected Azure command\\n' >&2
exit 99
""", encoding="utf-8", newline="\n")
    az.chmod(0o700)
    bash_tools = bash.parent.parent / "usr" / "bin" if os.name == "nt" else bash.parent
    env = {
        "HOME": _bash_path(tmp_path),
        "PATH": os.pathsep.join((str(tools), str(bash_tools))),
        "RUNNER_TEMP": _bash_path(tmp_path),
        "RG": "rg-private-marker",
        "CLUSTER": "arc-private-marker",
        "GITHUB_STEP_SUMMARY": _bash_path(tmp_path / "summary.md"),
        "FAKE_AZ_RESOURCE_ID": resource_id,
        "FAKE_AZ_LOG": _bash_path(tmp_path / "az-calls.log"),
        "FAKE_AZ_COUNT": _bash_path(tmp_path / "az-count.txt"),
        "FAKE_AZ_MODE": mode,
        "SystemRoot": os.environ.get("SystemRoot", ""),
    }
    run = _step_run(name)
    if name == "Teardown (persistent mode, delta cleanup, keep RG)":
        assert run.count("sleep 30") == 2
        run = run.replace("sleep 30", ":")
    # The teardown passes start many short processes, which Git Bash runs slowly on a busy host.
    result = subprocess.run(
        [str(bash), "-c", run], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    summary = tmp_path / "summary.md"
    public = result.stdout + result.stderr + (
        summary.read_text(encoding="utf-8") if summary.exists() else ""
    )
    return result, public, resource_id


def test_published_target_ids_are_masked_before_snapshot_and_arc():
    steps = cached_safe_load(_workflow())["jobs"]["e2e"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Mask operator target inputs") < names.index("Compute names")
    assert names.index("Compute names") < names.index(
        "Snapshot RG resources (persistent mode)"
    )
    compute = steps[names.index("Compute names")]
    assert "PUBLISHED_MODE" not in compute["env"]
    assert "::add-mask::$SN" in compute["run"]
    assert compute["run"].index('echo "::add-mask::$SN"') < (
        compute["run"].index('echo "cluster=$CL" >> "$GITHUB_OUTPUT"')
    )
    assert steps.index(compute) < next(
        index for index, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/connect-arc"
    )


def test_published_arc_connection_keeps_provider_errors_private():
    workflow = cached_safe_load(_workflow())
    action = cached_safe_load(
        (
            ROOT / ".github" / "actions" / "connect-arc" / "action.yaml"
        ).read_text(encoding="utf-8")
    )
    connect = next(
        step for step in workflow["jobs"]["e2e"]["steps"]
        if step.get("uses") == "./.github/actions/connect-arc"
    )
    assert connect["with"]["private-provider-errors"] == (
        "${{ needs.prep.outputs.published-mode }}"
    )
    assert action["inputs"]["private-provider-errors"]["default"] == "false"
    for name in (
        "Connect cluster to Arc + enable features",
        "Wait for Arc Connected status (initial)",
        "Enable OIDC issuer + workload identity",
        "Capture OIDC issuer URL",
        "Wait for Arc Connected status (post-restart)",
    ):
        step = next(item for item in action["runs"]["steps"] if item.get("name") == name)
        assert step["env"]["PRIVATE_PROVIDER_ERRORS"] == (
            "${{ inputs.private-provider-errors }}"
        )


@pytest.mark.parametrize("mode", ["preflight-failure", "preflight-unused"])
def test_persistent_arc_preflight_keeps_provider_errors_private(tmp_path, mode):
    name = "Preflight Arc cluster name is unused (persistent mode)"
    run = _step_run(name)
    assert "umask 077" in run.split("if ! COUNT=")[0]
    result, public, resource_id = _run_persistent_step(name, tmp_path, mode)
    assert "rg-private-marker" not in public
    assert resource_id not in public
    assert result.returncode == (1 if mode == "preflight-failure" else 0)
    assert "connectedk8s list" in (
        tmp_path / "az-calls.log"
    ).read_text(encoding="utf-8")
    diagnostic = tmp_path / "published-arc-preflight.err"
    if mode == "preflight-failure":
        assert "could not be checked" in public
        assert resource_id in diagnostic.read_text(encoding="utf-8")
        if os.name != "nt":
            assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    else:
        assert diagnostic.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("mode", ["snapshot-success", "snapshot-failure"])
def test_persistent_snapshot_keeps_identifiers_and_provider_errors_private(tmp_path, mode):
    result, public, resource_id = _run_persistent_step(
        "Snapshot RG resources (persistent mode)", tmp_path, mode,
    )
    assert "rg-private-marker" not in public
    assert resource_id not in public
    snapshot = tmp_path / "e2e-teardown" / "pre-ids.txt"
    if mode == "snapshot-success":
        assert result.returncode == 0
        assert resource_id in snapshot.read_text(encoding="utf-8")
        assert "1 pre-existing resource(s)" in public
        if os.name != "nt":
            assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    else:
        assert result.returncode != 0
        assert not snapshot.exists()
        assert "could not be captured" in public
        assert resource_id in (snapshot.parent / "snapshot.err").read_text(
            encoding="utf-8",
        )


@pytest.mark.parametrize("mode", ["teardown-missing", "teardown-success", "teardown-failure"])
def test_persistent_teardown_reports_without_public_resource_identity(tmp_path, mode):
    snapshot = tmp_path / "e2e-teardown" / "pre-ids.txt"
    if mode != "teardown-missing":
        snapshot.parent.mkdir()
        snapshot.write_text(
            "/subscriptions/00000000-0000-0000-0000-000000000001/"
            "resourceGroups/rg-private-marker/providers/Microsoft.Kubernetes/"
            "connectedClusters/arc-private-marker\n", encoding="utf-8",
        )
    result, public, resource_id = _run_persistent_step(
        "Teardown (persistent mode, delta cleanup, keep RG)", tmp_path, mode,
    )
    assert result.returncode == 0
    assert "rg-private-marker" not in public
    assert "arc-private-marker" not in public
    assert "private-resource" not in public
    assert resource_id not in public
    calls = tmp_path / "az-calls.log"
    if mode == "teardown-missing":
        assert not calls.exists()
        assert "Snapshot file missing" in public
    else:
        assert "resource delete" in calls.read_text(encoding="utf-8")
        assert "rest --method DELETE" not in calls.read_text(encoding="utf-8")
        assert ("incomplete" in public) is (mode == "teardown-failure")
        suffix = "err" if mode == "teardown-failure" else "out"
        private = list(snapshot.parent.glob(f"delete-*.{suffix}"))
        assert private and any(
            resource_id in path.read_text(encoding="utf-8") for path in private
        )


def test_guided_enabled_readiness_requires_live_secret_sync_resources():
    step = _step_run("Observe bounded AIO readiness")
    for value in (
        "E2E_JOURNEY",
        "E2E_ENABLE_SECRET_SYNC",
        "microsoft.secretsynccontroller/azurekeyvaultsecretproviderclasses",
        "microsoft.managedidentity/userassignedidentities",
        "microsoft.keyvault/vaults",
        "az identity federated-credential list",
        "--api-version 2026-07-01",
        "defaultSecretProviderClassRef",
        "secretSyncEnabled",
        "published-owned-resources.json",
        "e2e-teardown/pre-ids.txt",
    ):
        assert value in step


def test_guided_enabled_observation_uses_run_delta_instead_of_site_tags(tmp_path):
    scripts = _embedded_python(_step_run("Observe bounded AIO readiness"))
    selection = next(script for script in scripts if "for resource_type, label in" in script)
    ownership = next((script for script in scripts if "prior_ids" in script), None)
    assert ownership is not None, "The published observer does not bind resources to this run."

    old_instance = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.IoTOperations/instances/old"
    old_identity = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/old"
    new_instance = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.IoTOperations/instances/new"
    new_identity = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/new"
    new_spc = (
        "/subscriptions/example/resourceGroups/rg/providers/"
        "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses/new"
    )
    resources = [
        {"id": old_instance, "name": "old", "type": "Microsoft.IoTOperations/instances",
         "tags": {"site": "test-site"}},
        {"id": old_identity, "name": "old", "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
         "tags": {"site": "test-site"}},
        {"id": new_instance, "name": "new", "type": "Microsoft.IoTOperations/instances"},
        {"id": new_identity, "name": "new", "type": "Microsoft.ManagedIdentity/userAssignedIdentities"},
        {"id": new_spc, "name": "new",
         "type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses"},
    ]
    observed = tmp_path / "published-resources.json"
    observed.write_text(json.dumps(resources), encoding="utf-8")
    snapshot = tmp_path / "pre-ids.txt"
    snapshot.write_text(old_instance.upper() + "\n" + old_identity + "\n", encoding="utf-8")
    owned = tmp_path / "published-owned-resources.json"
    environment = {"RUNNER_TEMP": str(tmp_path), "E2E_SITE_NAME": "test-site"}
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]

    def run(script: str, *paths: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", script, *(str(path) for path in paths)],
            env=environment, capture_output=True, text=True, timeout=15, check=False,
        )

    missing = run(ownership, observed, tmp_path / "missing-snapshot", owned)
    assert missing.returncode != 0
    assert "ownership snapshot is unavailable" in missing.stderr
    assert not owned.exists()

    malformed = tmp_path / "malformed-resources.json"
    malformed.write_text(json.dumps([*resources, {
        "id": new_identity + "-invalid",
        "name": None,
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
    }]), encoding="utf-8")
    invalid_output = tmp_path / "invalid-owned.json"
    invalid = run(ownership, malformed, snapshot, invalid_output)
    assert invalid.returncode != 0
    assert "Azure resource observation is invalid." in invalid.stderr
    assert "new-invalid" not in invalid.stderr
    assert not invalid_output.exists()

    created = run(ownership, observed, snapshot, owned)
    assert created.returncode == 0, "The run-owned resource observation failed."
    assert {item["id"] for item in json.loads(owned.read_text(encoding="utf-8"))} == {
        new_instance, new_identity, new_spc,
    }
    chosen = run(selection, owned)
    assert chosen.returncode == 0
    assert chosen.stdout.splitlines() == ["new", new_instance, new_spc]

    current = json.loads(owned.read_text(encoding="utf-8"))
    owned.write_text(json.dumps([item for item in current if item["id"] != new_spc]), encoding="utf-8")
    missing_class = run(selection, owned)
    assert missing_class.returncode != 0
    assert "Guided Secret Sync provider class selection is incomplete." in missing_class.stderr

    owned.write_text(json.dumps([
        item for item in current if item["type"] == "Microsoft.ManagedIdentity/userAssignedIdentities"
    ]), encoding="utf-8")
    missing_instance = run(selection, owned)
    assert missing_instance.returncode != 0
    assert "Guided Secret Sync AIO instance selection is incomplete." in missing_instance.stderr

    owned.write_text(json.dumps([*current, {
        "id": new_identity + "-duplicate",
        "name": "second",
        "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
    }]), encoding="utf-8")
    duplicate = run(selection, owned)
    assert duplicate.returncode != 0
    assert "Guided Secret Sync managed identity selection is incomplete." in duplicate.stderr
    assert "new-duplicate" not in duplicate.stderr


def _run_readiness_receipt(tmp_path, mode, *, provider_vault=None, owned_vault="kvcreated"):
    script = _embedded_python(_step_run("Observe bounded AIO readiness"))[-1]
    site = "private-site-name"
    spc_id = "/subscriptions/private/spc/private-name"
    resources = [
        {"type": "Microsoft.IoTOperations/instances", "name": "private-instance",
         "id": "/subscriptions/private/instance/private-name"},
        {"type": "Microsoft.DeviceRegistry/schemaRegistries"},
        {"type": "Microsoft.DeviceRegistry/namespaces"},
    ]
    if mode != "disabled":
        resources.extend([
            {"type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses",
             "id": spc_id},
            {"type": "Microsoft.ManagedIdentity/userAssignedIdentities",
             "id": "/subscriptions/private/identity/private-name"},
            {"type": "Microsoft.KeyVault/vaults", "name": owned_vault,
             "id": f"/subscriptions/private/vault/{owned_vault}"},
        ])
        (tmp_path / "published-federated.json").write_text(
            json.dumps([{"name": "private-credential"}]), encoding="utf-8",
        )
        (tmp_path / "published-aio-instance.json").write_text(
            json.dumps({"properties": {
                "defaultSecretProviderClassRef": {"resourceId": spc_id},
            }}),
            encoding="utf-8",
        )
        (tmp_path / "published-spc.json").write_text(json.dumps({"properties": {
            "keyvaultName": provider_vault or owned_vault.upper(),
        }}), encoding="utf-8")
    owned_resources = list(resources)
    if mode == "disabled":
        resources.append({
            "type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses",
            "id": "/subscriptions/private/spc/from-prior-run",
            "tags": {"site": site},
        })
    pods = {"items": [{
        "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True"},
        ]},
    }, {"status": {"phase": "Succeeded"}}]}
    instances = {"items": [{"metadata": {"name": "private-instance"}}]}
    observer = runpy.run_path(str(ROOT / "scripts" / "observe-site-readiness.py"))
    counts = observer["observe"](
        query=lambda kind, timeout: pods if kind == "pods" else instances,
        sleep=lambda _: pytest.fail("Ready observations must not sleep."),
    )
    for filename, document in (
        ("resources.json", resources),
        ("kubernetes.json", counts),
    ):
        (tmp_path / filename).write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "readiness.json"
    (tmp_path / "published-owned-resources.json").write_text(
        json.dumps(owned_resources), encoding="utf-8",
    )
    environment = {
        "RUNNER_TEMP": str(tmp_path), "E2E_SITE_NAME": site,
        "E2E_JOURNEY": "guided",
        "E2E_ENABLE_SECRET_SYNC": "false" if mode == "disabled" else "true",
        "E2E_SECRET_SYNC_MODE": mode,
    }
    if mode == "existing":
        environment["FLEET_VAULT_NAME"] = "kvexisting"
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    result = subprocess.run(
        [
            sys.executable, "-c", script,
            str(tmp_path / "resources.json"),
            str(tmp_path / "kubernetes.json"),
            str(output),
        ],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    public = result.stdout + result.stderr
    for value in (spc_id, site, owned_vault, "kvexisting", *([provider_vault] if provider_vault else [])):
        assert value not in public
    return result, output, spc_id, site


@pytest.mark.parametrize(("mode", "owned_vault"), [
    ("disabled", "kvcreated"), ("enabled", "kvcreated"), ("existing", "kvexisting"),
])
def test_guided_readiness_receipt_asserts_enabled_state_without_private_ids(
    tmp_path, mode, owned_vault,
):
    result, output, spc_id, site = _run_readiness_receipt(tmp_path, mode, owned_vault=owned_vault)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert (receipt["namespacePods"], receipt["activePods"], receipt["readyActivePods"]) == (2, 1, 1)
    assert receipt["instanceCustomResources"] == 1
    assert receipt["secretSyncEnabled"] is (mode != "disabled")
    assert spc_id not in json.dumps(receipt)
    assert site not in json.dumps(receipt)
    assert owned_vault not in json.dumps(receipt)
    if mode != "disabled":
        assert receipt["spcBoundToInstance"] is True
        assert receipt["federatedCredentials"] == 1
        assert receipt["vaultBinding"] == ("existing" if mode == "existing" else "created")
    else:
        assert "federatedCredentials" not in receipt
        assert "vaultBinding" not in receipt


@pytest.mark.parametrize(("mode", "provider_vault", "owned_vault"), [
    ("enabled", "kvother", "kvcreated"),
    ("existing", "kvcreated", "kvcreated"),
    ("existing", "kvexisting", "kvcreated"),
])
def test_guided_readiness_rejects_a_provider_class_bound_to_another_vault(
    tmp_path, mode, provider_vault, owned_vault,
):
    result, output, _, _ = _run_readiness_receipt(
        tmp_path, mode, provider_vault=provider_vault, owned_vault=owned_vault,
    )
    assert result.returncode != 0
    assert "uses a vault other than the expected owned vault" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_answers_use_a_private_file_with_no_implicit_site(tmp_path, enabled):
    block = _embedded_python(_step_run("Prepare guided AIO answers and plan"))[0]
    environment = {
        "RUNNER_TEMP": str(tmp_path),
        "E2E_SITE_NAME": "example-one",
        "E2E_SUBSCRIPTION": "00000000-0000-0000-0000-000000000001",
        "E2E_RESOURCE_GROUP": "rg-example",
        "E2E_CLUSTER_NAME": "arc-example",
        "E2E_AIO_RELEASE": "2608",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    invoked = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert invoked.returncode == 0, invoked.stderr
    assert not invoked.stdout
    values = json.loads(
        (tmp_path / "published-answers.json").read_text(encoding="utf-8")
    )["values"]
    assert values["siteName"] == "example-one"
    assert values["cluster"].endswith("/connectedClusters/arc-example")
    assert values["enableSecretSync"] is enabled
    assert values["brokerMemoryProfile"] == "Low"
    assert all(values[key] is None for key in (
        "subscription", "resourceGroup", "location", "clusterName",
    ))
    if os.name == "posix":
        assert (tmp_path / "published-answers.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_private_plan_assertion_requires_expected_operations(
    tmp_path, enabled,
):
    block = _embedded_python(_step_run("Prepare guided AIO answers and plan"))[-1]
    expected_steps = {
        "global-edge-site": "skip", "edge-site": "skip",
        "schema-registry": "execute", "adr-ns": "execute",
        "aio-enablement": "execute", "aio-instance": "execute",
        "schema-registry-role": "execute",
        "resolve-aio": "execute" if enabled else "skip",
        "secretsync": "execute" if enabled else "skip",
    }
    plan = {
        "status": "planned", "executable": True,
        "engine": {"version": "test-build"},
        "plan": {
            "manifest": {"targetSelection": "explicit-site"},
            "submission": {"mode": "arm-json", "compilationBinding": "package-artifact"},
            "targets": [{"operations": [
                {"identity": {"step": name}, "disposition": disposition}
                for name, disposition in expected_steps.items()
            ]}],
        },
    }
    path = tmp_path / "published-guided-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    environment = {
        "RUNNER_TEMP": str(tmp_path), "SITEOPS_E2E_ENGINE_VERSION": "test-build",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    selected = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert selected.returncode == 0, selected.stderr
    plan["plan"]["targets"][0]["operations"][-1]["disposition"] = (
        "skip" if enabled else "execute"
    )
    path.write_text(json.dumps(plan), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, "-c", block],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert rejected.returncode != 0
    assert "unexpected operations" in rejected.stderr


def _outputs(result):
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


@pytest.mark.parametrize("scenario", ["release-acceptance", "aio"])
def test_candidate_request_selects_three_parallel_guided_cases(scenario):
    result = _parse_inputs(INPUT_SCENARIO=scenario, INPUT_CANDIDATE_SET="true")
    assert result.returncode == 0, result.stderr
    outputs = _outputs(result)
    assert {key: outputs[key] for key in (
        "candidate_mode", "secret_sync_modes", "max_parallel", "published_mode", "published_journey",
        "persistent", "versions", "published_release", "rg_key",
    )} == {
        "candidate_mode": "true", "secret_sync_modes": '["disabled", "enabled", "existing"]',
        "max_parallel": "3", "published_mode": "true", "published_journey": "guided", "persistent": "false",
        "versions": '["2609"]', "published_release": "", "rg_key": "ephemeral-42",
    }
    assert _outputs(_parse_inputs())["candidate_mode"] == "false"
    refused = _parse_inputs(INPUT_SCENARIO="release-acceptance")
    assert refused.returncode != 0 and "requires the exact candidate selection" in refused.stderr


@pytest.mark.parametrize(("changes", "message"), [
    ({"INPUT_RG": "rg-private-marker"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_CLUSTER": "arc-private-marker"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_OID": "oid-private-marker"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_TESTS": "aio-install"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_RELEASES": "2607"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_SECRET_SYNC_MODES": "enabled,disabled", "INPUT_TESTS": "aio-upgrade"},
     "Candidate acceptance selects its own Site cases"),
    ({"INPUT_SKIP_TEARDOWN": "true"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_KEEP_ALIVE": "5"}, "Candidate acceptance selects its own Site cases"),
    ({"INPUT_PUBLISHED_RELEASE": "v1.0.0", "INPUT_PUBLISHED_SOURCE_SHA": "a" * 40},
     "Published E2E requires an existing resource group"),
])
def test_candidate_request_refuses_single_site_overrides_without_echoing_them(changes, message):
    result = _parse_inputs(INPUT_SCENARIO="release-acceptance", INPUT_CANDIDATE_SET="true", **changes)
    assert result.returncode != 0
    assert not result.stdout
    assert message in result.stderr
    assert "private-marker" not in result.stderr


def test_candidate_cases_use_masked_owned_names_and_a_distinct_existing_suffix(tmp_path):
    output = tmp_path / "names.txt"
    result = subprocess.run(
        [str(_bash_executable()), "-c", _step_run("Compute names")],
        env={**os.environ, "RG_IN": "", "CL_IN": "", "RELEASE": "2608", "SECRET_SYNC_MODE": "existing",
             "RUN_ID": "1234567890", "RUN_ATTEMPT": "2", "GITHUB_OUTPUT": _bash_path(output),
             "FLEET_RESOURCE_GROUP": "rg-siteops-site-private", "FLEET_CLUSTER_NAME": "arc-siteops-site-private"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["rg"] == "rg-siteops-site-private"
    assert values["cluster"] == "arc-siteops-site-private"
    assert values["site_name"] == "e2e-34567890-2-2608-sync-existing"
    assert "private" not in result.stdout


def test_candidate_steps_never_read_enroll_or_pin_a_public_release():
    job = cached_safe_load(_workflow())["jobs"]["e2e"]
    steps = job["steps"]
    engine = next(step for step in steps if step.get("id") == "candidate-engine")
    for value in ("scripts/qualify-workspace-engine.py", "--project-workspace workspaces/iot-operations",
                  "--expected-runner-environment self-hosted", "Candidate E2E imported Site Ops from checkout.",
                  "SITEOPS_E2E_TRUST=policy", 'rmdir "$project/sites"', 'echo "$state/command" >> "$GITHUB_PATH"'):
        assert value in engine["run"]
    assert "pip install" not in engine["run"] and "application/bin\" >> \"$GITHUB_PATH" not in engine["run"]
    names = [step.get("name") or step.get("uses") for step in steps]
    for earlier, later in (
        ("Mask operator target inputs", "Bind candidate inputs and owned Site names"),
        ("Select exact candidate inputs", "Bind candidate inputs and owned Site names"),
        ("Bind candidate inputs and owned Site names", "Compute names"),
        ("Install the candidate engine outside the checkout", "./.github/actions/create-k3s-cluster"),
        ("Validate Custom Locations RP object ID", "Preflight the Site resource group"),
        ("Snapshot RG resources (persistent mode)", "./.github/actions/connect-arc"),
        ("Deploy AIO through the published engine and package", "Create the existing Key Vault"),
        ("Enable Secret Sync on the existing instance", "Observe bounded AIO readiness"),
    ):
        assert names.index(earlier) < names.index(later), (earlier, later)
    assert job["permissions"] == {"contents": "read", "actions": "read", "id-token": "write"}


def _guided_rules(policy, *, plan):
    trust = ["--trust-policy", f"{policy}/policy.json"]
    return {
        "siteops": [
            {"match": ["inputs", "aio-install", "--example", *trust], "forbid": ["--approved-source"],
             "write": {"--example": "cluster: null\nsiteName: example\n"},
             "stdout": json.dumps({"kind": "SiteInputContract", "inputs": [
                 {"name": "cluster", "type": "azureResourceId"},
                 {"name": "location", "derivableFrom": ["cluster"]}]})},
            {"match": ["inputs", "aio-install", "--input-file", *trust], "forbid": ["--approved-source"],
             "code": 2, "stderr": "inputs.resource.read-required\n"},
            {"match": ["deploy", "aio-install", *trust], "forbid": ["--approved-source"], "code": 1,
             "stdout": json.dumps({"diagnostics": [{"code": "inputs.resource.requirement-unmet"}]})},
            {"match": ["plan", "aio-install", *trust], "forbid": ["--approved-source"], "stdout": json.dumps(plan)},
        ],
        "az": [{"match": ["deployment", "group", "list"], "stdout": "[]"}],
        "fleet-python": [{"match": ["scripts/coordinate-release-fleet.py", "policy"], "stdout": policy + "\n"}],
    }


@pytest.mark.parametrize("mode", ["disabled", "existing"])
def test_candidate_guided_preparation_uses_fresh_policy_and_refuses_unready_secret_sync_only_when_disabled(
    tmp_path, mode,
):
    project = tmp_path / "project"
    project.mkdir()
    policy = (tmp_path / "policy-1").as_posix()
    steps = {"global-edge-site": "skip", "edge-site": "skip", "schema-registry": "execute", "adr-ns": "execute",
             "aio-enablement": "execute", "aio-instance": "execute", "schema-registry-role": "execute",
             "resolve-aio": "skip", "secretsync": "skip"}
    plan = {"status": "planned", "executable": True, "engine": {"version": "1.0.0b7"}, "plan": {
        "manifest": {"targetSelection": "explicit-site"},
        "submission": {"mode": "arm-json", "compilationBinding": "package-artifact"},
        "targets": [{"operations": [{"identity": {"step": step}, "disposition": value}
                                    for step, value in steps.items()]}]}}
    install_doubles(tmp_path, _guided_rules(policy, plan=plan))
    result = run_script(_step_run("Prepare guided AIO answers and plan"), tmp_path, {
        **double_environment(tmp_path), "RUNNER_TEMP": tmp_path.as_posix(),
        "SITEOPS_E2E_PROJECT": project.as_posix(), "SITEOPS_E2E_TRUST": "policy",
        "SITEOPS_E2E_STATE": (tmp_path / "state").as_posix(), "FLEET_PYTHON": "fleet-python",
        "SITEOPS_E2E_ENGINE_VERSION": "1.0.0b7", "E2E_SITE_NAME": "site-private-marker",
        "E2E_SUBSCRIPTION": "00000000-0000-0000-0000-000000000001", "E2E_RESOURCE_GROUP": "rg-private-marker",
        "E2E_CLUSTER_NAME": "arc-private-marker", "E2E_AIO_RELEASE": "2608",
        "E2E_ENABLE_SECRET_SYNC": "false", "E2E_SECRET_SYNC_MODE": mode,
    })
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = calls(tmp_path)
    commands = [arguments for name, arguments in recorded if name == "siteops"]
    deploys = [arguments for arguments in commands if "deploy" in arguments]
    assert len(deploys) == (1 if mode == "disabled" else 0)
    assert all("--approved-source" not in arguments for arguments in commands)
    assert sum(name == "fleet-python" for name, _ in recorded) == 1
    assert all(line.startswith("::add-mask::") for line in result.stdout.splitlines() if "private-marker" in line)
    assert "private-marker" not in result.stderr


def _existing_vault_rules(*, resources, create_code=0):
    return {"az": [
        {"match": ["resource", "list", "--output", "json"], "stdout": json.dumps(resources)},
        {"match": ["keyvault", "create", "--enable-rbac-authorization", "true"], "code": create_code,
         "stderr": "provider detail vault-private-marker%0A\r\n"},
    ]}


@pytest.mark.parametrize("fault", [None, "secret-sync-present", "no-new-instance", "create-failed"])
def test_existing_case_creates_its_vault_only_after_proving_secret_sync_absent(tmp_path, fault):
    prior = "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/old"
    instance = "/subscriptions/x/resourceGroups/rg/providers/Microsoft.IoTOperations/instances/private-marker"
    resources = [{"id": prior, "type": "Microsoft.Storage/storageAccounts"}]
    if fault != "no-new-instance":
        resources.append({"id": instance, "type": "Microsoft.IoTOperations/instances"})
    if fault == "secret-sync-present":
        resources.append({"id": instance + "-spc",
                          "type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses"})
    (tmp_path / "e2e-teardown").mkdir()
    (tmp_path / "e2e-teardown" / "pre-ids.txt").write_text(prior.upper() + "\n", encoding="utf-8")
    install_doubles(tmp_path, _existing_vault_rules(resources=resources, create_code=int(fault == "create-failed")))
    result = run_script(_step_run("Create the existing Key Vault"), tmp_path, {
        **double_environment(tmp_path), "RUNNER_TEMP": tmp_path.as_posix(), "RG": "rg-private-marker",
        "LOCATION": "eastus2", "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
        "FLEET_VAULT_NAME": "kv-private-marker", "FLEET_RUN_ID": "60", "FLEET_RUN_ATTEMPT": "1",
    })
    public = result.stdout + result.stderr
    assert "private-marker" not in public
    created = [arguments for name, arguments in calls(tmp_path) if "create" in arguments]
    if fault is None:
        assert result.returncode == 0, public
        assert (tmp_path / "existing-instance.txt").read_text(encoding="utf-8") == instance
        assert created == [[
            "keyvault", "create", "--subscription", "00000000-0000-0000-0000-000000000001",
            "--resource-group", "rg-private-marker", "--name", "kv-private-marker", "--location", "eastus2",
            "--enable-rbac-authorization", "true", "--tags", "managedBy=siteops-site-acceptance", "runId=60",
            "runAttempt=1", "--only-show-errors", "--output", "none"]]
    else:
        assert result.returncode != 0
        assert bool(created) is (fault == "create-failed")
        if fault == "create-failed":
            assert "could not be created in the owned resource group" in public
            assert "vault-private-marker" in (tmp_path / "existing-vault.err").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", [None, "extra-operation", "deploy-failed"])
def test_existing_case_enables_secret_sync_with_the_precreated_vault(tmp_path, fault):
    policy = (tmp_path / "policy-1").as_posix()
    operations = [{"identity": {"step": "resolve-aio"}, "disposition": "execute"},
                  {"identity": {"step": "secretsync"}, "disposition": "execute"}]
    if fault == "extra-operation":
        operations.append({"identity": {"step": "aio-instance"}, "disposition": "execute"})
    plan = {"status": "planned", "executable": True, "engine": {"version": "1.0.0b7"},
            "plan": {"submission": {"mode": "arm-json", "compilationBinding": "package-artifact"},
                     "targets": [{"operations": operations}]}}
    run = {"apiVersion": "siteops/v1alpha1", "kind": "DeploymentRun", "projection": "publishable",
           "status": "failed" if fault == "deploy-failed" else "succeeded", "exitCode": 0,
           "engine": {"version": "1.0.0b7"},
           "summary": {"sites": {"total": 1, "counts": {"succeeded": 1}},
                       "operations": {"total": 2, "counts": {"succeeded": 2}}}}
    trust = ["--trust-policy", f"{policy}/policy.json", "--trusted-root", f"{policy}/root.json"]
    install_doubles(tmp_path, {
        "siteops": [
            {"match": ["plan", "secretsync", "--offline-content", *trust], "forbid": ["--approved-source"],
             "stdout": json.dumps(plan)},
            {"match": ["deploy", "secretsync", "--offline-content", "--yes", *trust],
             "forbid": ["--approved-source"], "stdout": json.dumps(run)},
        ],
        "fleet-python": [{"match": ["policy"], "stdout": policy + "\n"}],
    })
    instance = "/subscriptions/x/resourceGroups/rg-private-marker/providers/Microsoft.IoTOperations/instances/i"
    (tmp_path / "existing-instance.txt").write_text(instance, encoding="utf-8")
    result = run_script(_step_run("Enable Secret Sync on the existing instance"), tmp_path, {
        **double_environment(tmp_path), "RUNNER_TEMP": tmp_path.as_posix(),
        "SITEOPS_E2E_PROJECT": (tmp_path / "project").as_posix(), "SITEOPS_E2E_STATE": "state",
        "SITEOPS_E2E_ENGINE_VERSION": "1.0.0b7", "FLEET_PYTHON": "fleet-python",
        "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001", "E2E_RESOURCE_GROUP": "rg-private-marker",
        "FLEET_VAULT_NAME": "kv-private-marker", "E2E_SITE_NAME": "site-private-marker",
    })
    assert "private-marker" not in result.stdout + result.stderr
    answers = json.loads((tmp_path / "existing-answers.json").read_text(encoding="utf-8"))["values"]
    assert answers == {
        "instance": instance, "siteName": "site-private-marker", "environment": "e2e", "country": "US",
        "existingVault": "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-private-marker"
                         "/providers/Microsoft.KeyVault/vaults/kv-private-marker",
    }
    deploys = [arguments for name, arguments in calls(tmp_path) if name == "siteops" and "deploy" in arguments]
    assert result.returncode == (0 if fault is None else 1)
    assert len(deploys) == (0 if fault == "extra-operation" else 1)
    if os.name == "posix":
        assert (tmp_path / "existing-answers.json").stat().st_mode & 0o777 == 0o600


def test_candidate_deployment_receipt_names_prepublication_transport(tmp_path):
    block = _embedded_python(_step_run("Deploy AIO through the published engine and package"))[0]
    raw, output = tmp_path / "deployment.txt", tmp_path / "receipt.json"
    raw.write_text(json.dumps({
        "apiVersion": "siteops/v1alpha1", "kind": DEPLOYMENT_KIND, "projection": "publishable",
        "status": "succeeded", "exitCode": 0, "engine": {"version": "1.0.0b7"},
        "summary": {"sites": {"total": 1, "counts": {"succeeded": 1}},
                    "operations": {"total": 7, "counts": {"succeeded": 7}}},
    }), encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", block, str(raw), str(output), "", "a" * 40, "1.0.0b7"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["transport"] == "prepublication" and "release" not in receipt


def _select_site_groups(site="", fleet=""):
    workflow = cached_safe_load(_workflow())
    job = workflow["jobs"]["site-groups"]
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        result = subprocess.run(
            [sys.executable, "-c", job["steps"][0]["run"]],
            env={**os.environ, "SITE_GROUP": site, "FLEET_GROUPS": fleet,
                 "RUN_ID": "42", "GITHUB_OUTPUT": str(output)},
            capture_output=True, text=True, check=False,
        )
        values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, values


def test_site_group_selection_reads_only_environment_secrets_and_publishes_no_names():
    job = cached_safe_load(_workflow())["jobs"]["site-groups"]
    assert job["environment"] == "${{ inputs.environment }}"
    assert job["permissions"] == {}
    assert set(job["outputs"]) == {"groups", "rg-key", "max-parallel"}
    assert len(job["steps"]) == 1 and "uses" not in job["steps"][0]
    assert job["steps"][0]["env"]["SITE_GROUP"] == "${{ secrets.E2E_SITE_RESOURCE_GROUP }}"
    assert job["steps"][0]["env"]["FLEET_GROUPS"] == "${{ secrets.E2E_FLEET_RESOURCE_GROUPS }}"


@pytest.mark.parametrize(("site", "fleet", "expected"), [
    ("", "", {"groups": "ephemeral", "rg_key": "ephemeral-42", "max_parallel": "3"}),
    ("", "rg-fleet-one,rg-fleet-two", {"groups": "ephemeral", "rg_key": "ephemeral-42", "max_parallel": "3"}),
    ("RG-Private-Marker", "rg-fleet-one,rg-fleet-two", {"groups": "persistent", "max_parallel": "1"}),
    ("rg-private-marker", "", {"groups": "persistent", "max_parallel": "1"}),
    # Overlap is refused for every candidate Site run, standalone or release acceptance.
    ("rg-private-marker", "rg-private-marker,rg-fleet-two", None),
    ("rg-private-marker", "rg-fleet-one,RG-PRIVATE-MARKER", None),
    ("rg private-marker", "", None),
    ("rg-private-marker.", "", None),
])
def test_site_groups_select_parallel_ephemeral_or_serialized_persistent_cases(site, fleet, expected):
    result, values = _select_site_groups(site, fleet)
    assert "private-marker" not in result.stdout + result.stderr + json.dumps(values)
    if expected is None:
        assert result.returncode != 0 and not values
        return
    assert result.returncode == 0, result.stderr
    if expected["groups"] == "persistent":
        # Persistent cases share the key that persistent E2E uses for the same group.
        ordinary = _outputs(_parse_inputs(INPUT_RG=site.lower()))["rg_key"]
        expected = {**expected, "rg_key": ordinary}
    assert values == expected


def test_site_cases_schedule_from_site_groups_and_keep_ordinary_dispatches_unchanged():
    job = cached_safe_load(_workflow())["jobs"]["e2e"]
    assert job["strategy"]["max-parallel"] == (
        "${{ fromJSON(needs.site-groups.outputs.max-parallel || needs.prep.outputs.max-parallel) }}")
    group = job["concurrency"]["group"]
    assert group.startswith("e2e-${{ needs.site-groups.outputs.rg-key || needs.prep.outputs.rg-key }}")
    assert "needs.site-groups.outputs.groups == 'persistent'" in group
    assert job["env"]["E2E_SITE_RESOURCE_GROUP"] == (
        "${{ needs.prep.outputs.candidate-mode == 'true' && secrets.E2E_SITE_RESOURCE_GROUP || '' }}")
    steps = {step.get("name"): step for step in job["steps"]}
    assert steps["Teardown (persistent mode, delta cleanup, keep RG)"]["if"] == (
        "always() && env.PERSISTENT_RG == 'true' && !inputs.skip-teardown")
    assert '--expect-groups "$SITE_GROUPS"' in steps["Bind candidate inputs and owned Site names"]["run"]
    assert '"$CANDIDATE_MODE" == "true"' in steps["Resolve E2E_LOCATION"]["run"]


@pytest.mark.parametrize(("persistent", "candidate", "expected"), [
    ("false", "true", "westus3"), ("true", "false", "westus3"), ("false", "false", "eastus2"),
])
def test_candidate_cases_take_their_region_from_the_selected_group(tmp_path, persistent, candidate, expected):
    install_doubles(tmp_path, {"az": [{"match": ["group", "show", "--query", "location"], "stdout": "westus3\n"}]})
    output = tmp_path / "location.txt"
    result = run_script(_step_run("Resolve E2E_LOCATION"), tmp_path, {
        **double_environment(tmp_path), "PERSISTENT_RG": persistent, "CANDIDATE_MODE": candidate,
        "AUTO_LOC": "eastus2", "RG": "rg-private-marker", "GITHUB_OUTPUT": output.as_posix(),
    })
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"location={expected}\n"

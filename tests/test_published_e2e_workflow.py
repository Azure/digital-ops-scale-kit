# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from siteops.reporting import _KIND as DEPLOYMENT_KIND

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-test.yaml"
ACTION = ROOT / ".github" / "actions" / "setup-published-siteops" / "action.yaml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _action() -> str:
    return ACTION.read_text(encoding="utf-8")


def _step_run(name: str) -> str:
    workflow = yaml.safe_load(_workflow())
    return next(
        item["run"]
        for item in workflow["jobs"]["e2e"]["steps"]
        if item.get("name") == name
    )


def _parse_inputs(**changes: str) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(_workflow())
    step = next(
        item
        for item in workflow["jobs"]["prep"]["steps"]
        if item.get("name") == "Parse inputs"
    )
    match = re.search(r"<<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY", step["run"], re.S)
    assert match is not None
    environment = {
        "INPUT_RELEASES": "2608",
        "INPUT_RG": "",
        "INPUT_CLUSTER": "",
        "INPUT_UPGRADE_TO": "",
        "INPUT_SECRET_SYNC_MODES": "enabled",
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


def test_operator_target_inputs_are_masked_before_step_headers():
    jobs = yaml.safe_load(_workflow())["jobs"]
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
def test_computed_site_mask_preserves_published_and_source_targets(
    tmp_path, published, cluster_in,
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
    workflow = yaml.safe_load(_workflow())
    job = workflow["jobs"]["e2e"]
    assert job["needs"] == "prep"
    assert job["env"]["PERSISTENT_RG"] == "${{ needs.prep.outputs.persistent }}"
    steps = job["steps"]
    snapshot = next(
        step for step in steps
        if step.get("name") == "Snapshot RG resources (persistent mode)"
    )
    assert snapshot["if"] == "env.PERSISTENT_RG == 'true'"
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
    assert "Published guided E2E currently requires aio-releases=2608" in result.stderr


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
        "--require-hashes",
        "--no-index",
        "$STATE/bundle/pylock.toml",
        "pipx==$PIPX_VERSION",
        "pipx\" install siteops",
        "pipx\" upgrade-shared",
        "Published E2E imported Site Ops from checkout.",
    ):
        assert value in action
    assert "pip install -e" not in action


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
        "validate aio-install",
        "--plan",
        "--offline",
        "Published package planning changed the operator Site.",
    ):
        assert value in plan_step


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
        "--read-resources",
        "--offline",
        "SITEOPS_REDACT_OUTPUT=0",
        "expected_steps",
        "read-required",
        "requirement-unmet",
    ):
        assert value in step
    assert "cat \"$RUNNER_TEMP/" not in step
    assert " -w workspaces/" not in step


def test_guided_disabled_cell_checks_manual_inline_and_configured_site_without_reads():
    workflow = yaml.safe_load(_workflow())
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
        'inputs aio-install --offline',
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
    assert "--read-resources" in step
    assert 'name=$SITE_NAME' in step
    assert 'trust_args=(--approved-source guided)' in step


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
        "--offline",
        '"apiVersion") != "siteops/v1alpha1"',
        f'"kind") != "{DEPLOYMENT_KIND}"',
        '"projection") != "publishable"',
        '"status") != "succeeded"',
        'result.get("engine", {}).get("version") != sys.argv[5]',
    ):
        assert value in deploy
    assert "workspaces/iot-operations" not in deploy


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
    raw.write_text("safe progress\n" + json.dumps(document), encoding="utf-8")

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


def test_published_readiness_is_bounded_and_existing_teardown_is_retained():
    workflow = _workflow()

    for value in (
        "microsoft.iotoperations/instances",
        "microsoft.deviceregistry/schemaregistries",
        "microsoft.deviceregistry/namespaces",
        "for _ in {1..60}",
        'status.get("phase") == "Running"',
        'condition.get("type") == "Ready"',
        "AIO instance projection and operator readiness did not converge within five minutes.",
        "PublishedPackageReadiness",
        "Snapshot RG resources (persistent mode)",
        "Teardown (persistent mode, delta cleanup, keep RG)",
        "comm -23",
        "operator-owned",
    ):
        assert value in workflow


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
    result = subprocess.run(
        [str(bash), "-c", run], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    summary = tmp_path / "summary.md"
    public = result.stdout + result.stderr + (
        summary.read_text(encoding="utf-8") if summary.exists() else ""
    )
    return result, public, resource_id


def test_published_target_ids_are_masked_before_snapshot_and_arc():
    steps = yaml.safe_load(_workflow())["jobs"]["e2e"]["steps"]
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
    workflow = yaml.safe_load(_workflow())
    action = yaml.safe_load(
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
    selection = next(script for script in scripts if "for resource_type in" in script)
    ownership = next((script for script in scripts if "prior_ids" in script), None)
    assert ownership is not None, "The published observer does not bind resources to this run."

    old_instance = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.IoTOperations/instances/old"
    old_identity = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/old"
    new_instance = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.IoTOperations/instances/new"
    new_identity = "/subscriptions/example/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/new"
    resources = [
        {"id": old_instance, "name": "old", "type": "Microsoft.IoTOperations/instances",
         "tags": {"site": "test-site"}},
        {"id": old_identity, "name": "old", "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
         "tags": {"site": "test-site"}},
        {"id": new_instance, "name": "new", "type": "Microsoft.IoTOperations/instances"},
        {"id": new_identity, "name": "new", "type": "Microsoft.ManagedIdentity/userAssignedIdentities"},
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
        new_instance, new_identity,
    }
    chosen = run(selection, owned)
    assert chosen.returncode == 0
    assert chosen.stdout.splitlines() == ["new", new_instance]

    current = json.loads(owned.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize("enabled", [False, True])
def test_guided_readiness_receipt_asserts_enabled_state_without_private_ids(
    tmp_path, enabled,
):
    script = _embedded_python(_step_run("Observe bounded AIO readiness"))[-1]
    site = "private-site-name"
    spc_id = "/subscriptions/private/spc/private-name"
    resources = [
        {"type": "Microsoft.IoTOperations/instances", "name": "private-instance",
         "id": "/subscriptions/private/instance/private-name"},
        {"type": "Microsoft.DeviceRegistry/schemaRegistries"},
        {"type": "Microsoft.DeviceRegistry/namespaces"},
    ]
    if enabled:
        resources.extend([
            {"type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses",
             "id": spc_id},
            {"type": "Microsoft.ManagedIdentity/userAssignedIdentities",
             "id": "/subscriptions/private/identity/private-name"},
            {"type": "Microsoft.KeyVault/vaults",
             "id": "/subscriptions/private/vault/private-name"},
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
    owned_resources = list(resources)
    if not enabled:
        resources.append({
            "type": "Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses",
            "id": "/subscriptions/private/spc/from-prior-run",
            "tags": {"site": site},
        })
    pods = {"items": [{
        "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True"},
        ]},
    }]}
    instances = {"items": [{}]}
    for filename, document in (
        ("resources.json", resources),
        ("pods.json", pods),
        ("instances.json", instances),
    ):
        (tmp_path / filename).write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "readiness.json"
    (tmp_path / "published-owned-resources.json").write_text(
        json.dumps(owned_resources), encoding="utf-8",
    )
    environment = {
        "RUNNER_TEMP": str(tmp_path), "E2E_SITE_NAME": site,
        "E2E_JOURNEY": "guided",
        "E2E_ENABLE_SECRET_SYNC": "true" if enabled else "false",
    }
    if os.name == "nt":
        environment["SystemRoot"] = os.environ["SystemRoot"]
    result = subprocess.run(
        [
            sys.executable, "-c", script,
            str(tmp_path / "resources.json"),
            str(tmp_path / "pods.json"),
            str(tmp_path / "instances.json"),
            str(output),
        ],
        env=environment, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["secretSyncEnabled"] is enabled
    assert spc_id not in json.dumps(receipt)
    assert site not in json.dumps(receipt)
    if enabled:
        assert receipt["spcBoundToInstance"] is True
        assert receipt["federatedCredentials"] == 1
    else:
        assert "federatedCredentials" not in receipt


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

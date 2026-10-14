"""Keep release artifact work on admitted 1ES pools and ordinary checks on public runners."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.shell_helpers import run_script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_windows_uv_gate_requires_qualified_native_fixtures_before_tests():
    job = workflow("ci.yaml")["jobs"]["windows-bootstrap"]
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare private test state") < names.index("Prepare native uv fixtures")
    setup = next(step["run"] for step in steps if step.get("name") == "Prepare native uv fixtures")
    run = next(step["run"] for step in steps if step.get("name") == "Windows bootstrap tests")
    assert setup.index("Get-FileHash") < setup.index("Expand-Archive") < setup.index("python install")
    assert "95f9bc30fbb3574d276e28ac4a6de932d25153645853d13da8c21eec3bc88d06" in setup
    assert "3.11.16" in setup and "--no-bin --no-registry" in setup
    for variable in (
        "SITEOPS_TEST_UV", "SITEOPS_TEST_UV_ARCHIVE", "SITEOPS_TEST_UV_PYTHON_DIR",
        "SITEOPS_REQUIRE_WINDOWS_UV",
    ):
        assert variable in setup
    assert "tests\\test_uv_bootstrap_windows.py" in run
    assert job["permissions"] == {"contents": "read"}


def test_linux_unit_gate_requires_native_archives_on_the_supported_host():
    job = workflow("ci.yaml")["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-24.04"
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Prepare native Linux uv fixtures") < names.index("Run unit tests")
    setup = next(
        step["run"] for step in job["steps"]
        if step.get("name") == "Prepare native Linux uv fixtures"
    )
    for variable in (
        "SITEOPS_TEST_UV", "SITEOPS_TEST_UV_ARCHIVE",
        "SITEOPS_TEST_UV_PYTHON_ARCHIVE", "SITEOPS_REQUIRE_LINUX_UV",
    ):
        assert variable in setup
    assert 'bash tests/fixtures/prepare-native-uv.sh "$fixtures"' in setup
    fixture = (ROOT / "tests" / "fixtures" / "prepare-native-uv.sh").read_text()
    assert "6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f" in fixture
    assert "68c6739376b65258dee5058ccf6777232fe38d31a578965ae8bda327ec7da3a8" in fixture
    assert fixture.index("sha256sum --check") < fixture.index("tar -xzf")
    assert "command -v openssl" in fixture


def test_windows_gate_runs_native_consumers_and_generated_entries():
    steps = workflow("ci.yaml")["jobs"]["windows-bootstrap"]["steps"]
    names = [step.get("name") for step in steps]
    assert "Windows installation consumers" in names
    assert names.index("Prepare native uv fixtures") < names.index("Windows installation consumers")
    run = next(step["run"] for step in steps if step.get("name") == "Windows installation consumers")
    for module in ("test_uv_installation.py", "test_distribution_workflows.py", "test_release_workflow.py"):
        assert f"tests\\{module}" in run
    for selection in (
        "test_uv_installation", "native_qualification", "matrix_runtime_admission",
        "native_verified_installation_step", "native_online_installation_step",
        "native_installation_steps", "generated_powershell_bootstrap",
        "required_native_consumer_fixtures", "windows_qualification",
    ):
        assert selection in run.split("-k ", 1)[1]
    assert '--basetemp="$env:SITEOPS_WINDOWS_TEST_TEMP-consumers"' in run
    assert "$LASTEXITCODE -ne 0" in run


ADMISSION = workflow("_release-runner.yaml")
SELECT = ADMISSION["jobs"]["select"]
STEP = SELECT["steps"][0]
ENTRY_GUARD = (
    "inputs.release-pool != '' && inputs.release-pool == vars.SITEOPS_RELEASE_POOL && "
    "inputs.expected-source-sha == github.sha && "
    "vars.SITEOPS_RELEASE_PROVENANCE_READY == 'true' && vars.SITEOPS_RELEASE_RUNNER_MODE == 'scaleset' && "
    "(github.workflow_ref == format('{0}/.github/workflows/ci.yaml@{1}', github.repository, github.ref) || "
    "github.workflow_ref == format('{0}/.github/workflows/release.yaml@{1}', github.repository, github.ref)) && "
    "(github.event_name == 'workflow_dispatch' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/main'))"
)


def admit(tmp_path, **changes):
    output = tmp_path / "output"
    output.write_text("existing=value\n")
    environment = {
        "RELEASE_PROVENANCE_READY": "true",
        "RELEASE_POOL": "example-release-pool",
        "RELEASE_IMAGE": "example-image",
        "RUNNER_MODE": "",
        "RELEASE_MODE": "preview",
        "EXPECTED_SOURCE_SHA": "a" * 40,
        "SOURCE_SHA": "a" * 40,
        "SOURCE_REF": "refs/heads/preview",
        "SOURCE_REPOSITORY": "example/content",
        "SOURCE_IS_FORK": "false",
        "CALLER_WORKFLOW": "example/content/.github/workflows/ci.yaml@refs/heads/preview",
        "CALLER_EVENT": "workflow_dispatch",
        "GITHUB_OUTPUT": str(output),
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
        **changes,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", STEP["run"]],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=15,
    )
    return result, output.read_text()


@pytest.mark.parametrize("mode,event,ref,pool", [
    ("preview", "workflow_dispatch", "refs/heads/feature/release", "fork-pool"),
    ("preview", "workflow_dispatch", "refs/heads/main", "origin-pool"),
    ("release", "workflow_dispatch", "refs/heads/main", "origin-pool"),
    ("release", "push", "refs/heads/main", "fork-pool"),
])
def test_admission_selects_only_the_configured_pool(tmp_path, mode, event, ref, pool):
    caller = "ci.yaml" if mode == "preview" else "release.yaml"
    result, output = admit(
        tmp_path, RELEASE_MODE=mode, CALLER_EVENT=event, SOURCE_REF=ref, RELEASE_POOL=pool,
        RUNNER_MODE="scaleset",
        CALLER_WORKFLOW=f"example/content/.github/workflows/{caller}@{ref}",
    )
    assert result.returncode == 0, result.stderr
    assert output == f"existing=value\npool={pool}\n"


@pytest.mark.parametrize("changes,diagnostic", [
    ({"CALLER_EVENT": "pull_request"}, "explicit dispatch"),
    ({"CALLER_EVENT": "pull_request_target"}, "explicit dispatch"),
    ({"CALLER_EVENT": "workflow_run"}, "explicit dispatch"),
    ({"CALLER_EVENT": "push"}, "explicit dispatch"),
    ({"SOURCE_REF": "refs/tags/v1", "CALLER_WORKFLOW": "example/content/.github/workflows/ci.yaml@refs/tags/v1"}, "reviewed branch"),
    ({"EXPECTED_SOURCE_SHA": "b" * 40}, "current source commit"),
    ({"EXPECTED_SOURCE_SHA": "main"}, "current source commit"),
    ({"RELEASE_MODE": "unreviewed"}, "supported CI or Release"),
    ({"CALLER_WORKFLOW": "other/content/.github/workflows/ci.yaml@refs/heads/preview"}, "supported CI or Release"),
    ({"CALLER_WORKFLOW": "example/content/.github/workflows/unreviewed.yaml@refs/heads/preview"}, "supported CI or Release"),
    ({"RELEASE_MODE": "release", "CALLER_WORKFLOW": "example/content/.github/workflows/release.yaml@refs/heads/preview"}, "on main"),
    ({"RELEASE_POOL": ""}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "pool\npool=another"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "pool,other"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "$(touch injected)"}, "SITEOPS_RELEASE_POOL"),
    ({"RELEASE_POOL": "a" * 101}, "SITEOPS_RELEASE_POOL"),
])
def test_admission_rejects_before_emitting_a_runner_selection(tmp_path, changes, diagnostic):
    result, output = admit(tmp_path, **changes)
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert output == "existing=value\n"
    assert not (tmp_path / "injected").exists()


def test_admission_has_no_source_or_privileged_capability():
    assert SELECT["runs-on"] == "ubuntu-24.04"
    assert SELECT["permissions"] == {}
    assert SELECT["timeout-minutes"] == 5
    assert len(SELECT["steps"]) == 1
    assert STEP["shell"] == "python"
    assert STEP["env"]["RELEASE_POOL"] == "${{ vars.SITEOPS_RELEASE_POOL }}"
    assert STEP["env"]["RELEASE_IMAGE"] == "${{ vars.SITEOPS_RELEASE_IMAGE }}"
    assert STEP["env"]["RUNNER_MODE"] == "${{ vars.SITEOPS_RELEASE_RUNNER_MODE }}"
    assert STEP["env"]["SOURCE_IS_FORK"] == "${{ github.event.repository.fork }}"
    assert "RUNNER_MODE" not in ADMISSION[True]["workflow_call"]["inputs"]
    assert all("uses" not in step for step in SELECT["steps"])
    assert "RELEASE_POOL" not in ADMISSION[True]["workflow_call"]["inputs"]


@pytest.mark.parametrize("ready", ["false", "", "True", "1"])
def test_pending_provenance_qualification_blocks_before_worker_allocation(tmp_path, ready):
    result, output = admit(tmp_path, RELEASE_PROVENANCE_READY=ready)
    assert result.returncode != 0
    assert "SITEOPS_RELEASE_PROVENANCE_READY=true" in result.stderr
    assert output == "existing=value\n"


def test_production_admission_requires_explicit_repository_opt_in(tmp_path):
    assert STEP["env"]["RELEASE_PROVENANCE_READY"] == "${{ vars.SITEOPS_RELEASE_PROVENANCE_READY }}"
    result, output = admit(
        tmp_path, RELEASE_PROVENANCE_READY="", RUNNER_MODE="scaleset",
    )
    assert result.returncode != 0 and "not enabled" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("name,secured,public", [
    ("_release-candidate.yaml", {"prepare", "workspace-assets", "engine-input", "engine-reference", "review"}, {"workspace-qualify", "workspace-qualified", "admit"}),
    ("_siteops-distribution.yaml", {"build", "attest"}, {"qualify", "summary"}),
    ("_workspace-distribution.yaml", {"build", "attest"}, set()),
    ("release.yaml", {"publish"}, {"accept"}),
    ("ci.yaml", set(), {"lint", "test", "linux-bootstrap", "windows-bootstrap", "validate", "overview"}),
])
def test_runner_placement_follows_artifact_authority(name, secured, public):
    document = workflow(name)
    selected = {key for key, job in document["jobs"].items() if "runs-on" in job}
    assert selected == secured | public
    for key in secured:
        job = document["jobs"][key]
        labels = job["runs-on"]
        pool = "needs.release-runner.outputs.pool" if name == "release.yaml" else "inputs.release-pool"
        assert labels == ["${{ " + pool + " }}"]
        assert job["steps"][0] == workflow("_siteops-distribution.yaml")["jobs"]["build"]["steps"][0]
    for key in public:
        assert document["jobs"][key]["runs-on"] in {
            "ubuntu-latest", "ubuntu-24.04", "ubuntu-26.04", "windows-2025", "${{ matrix.os }}",
        }


@pytest.mark.parametrize("platform,runner,allowed", [
    ("linux", "self-hosted", True), ("linux", "github-hosted", False), ("win32", "self-hosted", False),
])
def test_artifact_runtime_guard_precedes_source_and_signing(tmp_path, platform, runner, allowed):
    guard = workflow("_siteops-distribution.yaml")["jobs"]["build"]["steps"][0]
    assert guard["name"] == "Require the admitted runner class"
    assert guard["shell"] == "python"
    assert guard["env"] == {"RUNNER_CLASS": "${{ runner.environment }}"}
    script = f"import sys\nsys.platform = {platform!r}\n" + guard["run"]
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], cwd=tmp_path,
        env={"RUNNER_CLASS": runner, **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name,entry", [
    ("_release-candidate.yaml", "prepare"),
    ("_siteops-distribution.yaml", "build"),
    ("_workspace-distribution.yaml", "build"),
])
def test_direct_reusable_calls_cannot_schedule_untrusted_events(name, entry):
    document = workflow(name)
    assert document[True]["workflow_call"]["inputs"]["release-pool"]["required"] is True
    assert " ".join(document["jobs"][entry]["if"].split()) == ENTRY_GUARD
    for key in ("attest",):
        if key in document["jobs"]:
            assert document["jobs"][key]["needs"] == entry
            assert "always()" not in document["jobs"][key].get("if", "")


def test_every_release_entry_point_requires_runner_admission():
    ci, release, candidate = (workflow(name) for name in ("ci.yaml", "release.yaml", "_release-candidate.yaml"))
    for document, mode in ((ci, "preview"), (release, "release")):
        gate = document["jobs"]["release-runner"]
        assert gate["uses"] == "./.github/workflows/_release-runner.yaml"
        assert gate["with"]["mode"] == (
            "${{ inputs.run-mode == 'runner-check' && 'check' || 'preview' }}"
            if mode == "preview" else mode
        )
        assert gate["permissions"] == {"contents": "read"}
    assert "github.event_name == 'workflow_dispatch'" in ci["jobs"]["release-runner"]["if"]
    assert ci["jobs"]["installer-check"]["needs"] == "release-runner"
    assert "release-runner" in ci["jobs"]["release-preview"]["needs"]
    assert release["jobs"]["candidate"]["needs"] == "release-runner"
    assert release["jobs"]["publish"]["needs"] == ["release-runner", "candidate", "accept"]
    for document, key in ((ci, "installer-check"), (ci, "release-preview"), (release, "candidate")):
        assert document["jobs"][key]["with"]["release-pool"] == "${{ needs.release-runner.outputs.pool }}"
    for key in ("distribution", "workspace-build"):
        assert candidate["jobs"][key]["with"]["release-pool"] == "${{ inputs.release-pool }}"


@pytest.mark.parametrize("event,allowed", [("workflow_dispatch", True), ("pull_request", False), ("pull_request_target", False), ("push", False)])
def test_runner_check_is_the_only_nonsigning_gate_exception(tmp_path, event, allowed):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RELEASE_PROVENANCE_READY="false", CALLER_EVENT=event,
    )
    assert (result.returncode == 0) is allowed, result.stderr
    assert ("pool=" in output) is allowed
    assert ("labels=" in output) is allowed


@pytest.mark.parametrize("image", ["", "image\npool=other", "image,other", "owner/image", "$(touch injected)", "a" * 101])
def test_runner_check_rejects_invalid_image_selection_before_allocation(tmp_path, image):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RELEASE_PROVENANCE_READY="false", RELEASE_IMAGE=image,
    )
    assert result.returncode != 0 and "SITEOPS_RELEASE_IMAGE" in result.stderr
    assert output == "existing=value\n"
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize("routing,expected", [
    ("", ["self-hosted", "1ES.Pool=example-release-pool", "1ES.ImageOverride=example-image"]),
    ("legacy", ["self-hosted", "1ES.Pool=example-release-pool", "1ES.ImageOverride=example-image"]),
    ("scaleset", ["example-release-pool"]),
])
def test_runner_check_emits_exact_labels_for_the_configured_integration(tmp_path, routing, expected):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RELEASE_PROVENANCE_READY="false", RUNNER_MODE=routing,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.splitlines())
    assert values == {
        "existing": "value", "pool": "example-release-pool",
        "labels": json.dumps(expected, separators=(",", ":")),
    }
    assert json.loads(values["labels"]) == expected


def test_scale_set_check_uses_the_pool_image_without_demand_labels(tmp_path):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RELEASE_PROVENANCE_READY="false",
        RUNNER_MODE="scaleset", RELEASE_IMAGE="unused\nPRIVATE_SENTINEL",
    )
    assert result.returncode == 0, result.stderr
    assert 'labels=["example-release-pool"]\n' in output
    assert "PRIVATE_SENTINEL" not in output + result.stdout + result.stderr
    assert "configured image" in result.stdout


@pytest.mark.parametrize("routing", ["true", "ScaleSet", "automatic", "scaleset\npool=other"])
def test_invalid_runner_mode_fails_without_fallback_or_output(tmp_path, routing):
    result, output = admit(tmp_path, RELEASE_MODE="check", RUNNER_MODE=routing)
    assert result.returncode != 0 and "SITEOPS_RELEASE_RUNNER_MODE" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("mode", ["preview", "release"])
def test_scale_set_mode_alone_cannot_enable_release_artifact_jobs(tmp_path, mode):
    caller = "ci.yaml" if mode == "preview" else "release.yaml"
    result, output = admit(
        tmp_path, RELEASE_MODE=mode, RUNNER_MODE="scaleset", RELEASE_PROVENANCE_READY="false",
        SOURCE_REF="refs/heads/main",
        CALLER_WORKFLOW=f"example/content/.github/workflows/{caller}@refs/heads/main",
    )
    assert result.returncode != 0 and "not enabled" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("routing", ["", "legacy"])
def test_release_artifacts_require_the_qualified_routing(tmp_path, routing):
    result, output = admit(tmp_path, RUNNER_MODE=routing, RELEASE_PROVENANCE_READY="true")
    assert result.returncode != 0 and "requires SITEOPS_RELEASE_RUNNER_MODE=scaleset" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target", "push", "workflow_run"])
def test_scale_set_check_preserves_event_admission(tmp_path, event):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RUNNER_MODE="scaleset",
        RELEASE_PROVENANCE_READY="false", CALLER_EVENT=event,
    )
    assert result.returncode != 0 and "explicit dispatch" in result.stderr
    assert output == "existing=value\n"


@pytest.mark.parametrize("pool", ["", 'pool","self-hosted', "pool\nother", "pool,other"])
def test_scale_set_check_cannot_inject_additional_labels(tmp_path, pool):
    result, output = admit(
        tmp_path, RELEASE_MODE="check", RUNNER_MODE="scaleset", RELEASE_POOL=pool,
    )
    assert result.returncode != 0 and "SITEOPS_RELEASE_POOL" in result.stderr
    assert output == "existing=value\n"


def test_attestation_admission_emits_only_the_fork_scale_set_label(tmp_path):
    result, output = admit(
        tmp_path, RELEASE_MODE="attestation", SOURCE_IS_FORK="true",
        RUNNER_MODE="scaleset", RELEASE_PROVENANCE_READY="false", RELEASE_IMAGE="unused",
    )
    assert result.returncode == 0, result.stderr
    assert output == 'existing=value\npool=example-release-pool\nlabels=["example-release-pool"]\n'
    assert ADMISSION[True]["workflow_call"]["outputs"]["labels"]["value"] == "${{ jobs.select.outputs.labels }}"
    assert SELECT["outputs"]["labels"] == "${{ steps.select.outputs.labels }}"


@pytest.mark.parametrize("changes", [
    {"SOURCE_IS_FORK": "false"}, {"SOURCE_IS_FORK": ""}, {"SOURCE_IS_FORK": "True"},
    {"CALLER_EVENT": "pull_request"}, {"CALLER_EVENT": "pull_request_target"},
    {"CALLER_EVENT": "push"}, {"CALLER_EVENT": "workflow_run"},
    {"RUNNER_MODE": "legacy"}, {"RUNNER_MODE": ""}, {"RUNNER_MODE": "automatic"},
    {"EXPECTED_SOURCE_SHA": "b" * 40}, {"SOURCE_SHA": "b" * 40},
    {"RELEASE_POOL": ""}, {"RELEASE_POOL": 'pool","self-hosted'},
    {"SOURCE_REPOSITORY": "other/content"},
    {"CALLER_WORKFLOW": "example/content/.github/workflows/other.yaml@refs/heads/preview"},
    {"SOURCE_REF": "refs/tags/v1", "CALLER_WORKFLOW": "example/content/.github/workflows/ci.yaml@refs/tags/v1"},
])
def test_attestation_admission_rejects_before_allocating_or_signing(tmp_path, changes):
    environment = {
        "RELEASE_MODE": "attestation", "SOURCE_IS_FORK": "true",
        "RUNNER_MODE": "scaleset", "RELEASE_PROVENANCE_READY": "false", **changes,
    }
    result, output = admit(tmp_path, **environment)
    assert result.returncode != 0 and "::error::" in result.stderr
    assert output == "existing=value\n"


def test_runner_check_jobs_have_no_source_or_signing_permissions():
    for key in ("probe", "recheck"):
        job = ADMISSION["jobs"][key]
        assert job["permissions"] == {}
        assert job["if"] == "inputs.mode == 'check'"
        assert job["timeout-minutes"] <= 10
        assert all("uses" not in step for step in job["steps"])
        assert job["runs-on"] == "${{ fromJSON(needs.select.outputs.labels) }}"
    assert ADMISSION["jobs"]["probe"]["needs"] == "select"
    assert ADMISSION["jobs"]["recheck"]["needs"] == ["select", "probe"]
    assert ADMISSION["jobs"]["probe"]["name"] != ADMISSION["jobs"]["recheck"]["name"]
    ci = workflow("ci.yaml")
    for job in ("installer-check", "release-preview"):
        assert ci["jobs"][job]["if"] == (
            "${{ github.event_name == 'workflow_dispatch' && inputs.run-mode == '" + job + "' }}"
        )


BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def probe_environment(tmp_path, monkeypatch):
    output, summary = tmp_path / "outputs", tmp_path / "summary"
    for key, value in {
        "RUNNER_CLASS": "self-hosted", "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": "PRIVATE_SENTINEL", "AZURE_CLIENT_SECRET": "PRIVATE_SENTINEL",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "platform", "linux")
    read = Path.read_text

    def boot_read(path, *args, **kwargs):
        return BOOT if path == Path("/proc/sys/kernel/random/boot_id") else read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boot_read)
    return output, summary


@pytest.mark.parametrize("fault", [None, "missing", "tool-error", "old-gh", "timeout"])
def test_runner_probe_uses_closed_version_commands_and_private_profiles(probe_environment, tmp_path, monkeypatch, fault):
    output, summary = probe_environment
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: None if fault == "missing" else name)

    def run(argv, **options):
        expected = {
            "git": (["git", "--version"], "git version 2.50.0\n"),
            "gh": (["gh", "--version"], "gh version 2.94.0\n" if fault == "old-gh" else "gh version 2.101.0\n"),
            "az": (["az", "version", "--query", '"azure-cli"', "--output", "tsv"], "2.90.0\n"),
        }
        assert argv == expected[argv[0]][0]
        assert options["timeout"] == 30 and options["stdin"] == subprocess.DEVNULL
        environment = options["env"]
        assert "GH_TOKEN" not in environment and "AZURE_CLIENT_SECRET" not in environment
        assert Path(environment["HOME"]).is_relative_to(tmp_path)
        assert Path(environment["AZURE_CONFIG_DIR"]).is_relative_to(Path(environment["HOME"]))
        assert Path(environment["GH_CONFIG_DIR"]).is_relative_to(Path(environment["HOME"]))
        assert environment["AZURE_CORE_COLLECT_TELEMETRY"] == "false"
        calls.append(argv)
        if fault == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 7 if fault == "tool-error" else 0, expected[argv[0]][1], "PRIVATE_SENTINEL")

    monkeypatch.setattr(subprocess, "run", run)
    script = ADMISSION["jobs"]["probe"]["steps"][0]["run"]
    if fault is None:
        exec(compile(script, "<runner-probe>", "exec"), {})
        assert len(calls) == 3
        text = summary.read_text()
        assert "2.101.0" in text and "PRIVATE_SENTINEL" not in text and BOOT not in text
        assert output.read_text() == "boot-session=" + hashlib.sha256(("42:1:" + BOOT).encode()).hexdigest() + "\n"
    else:
        with pytest.raises(SystemExit, match="::error::") as failure:
            exec(compile(script, "<runner-probe>", "exec"), {})
        assert "PRIVATE_SENTINEL" not in str(failure.value)
        assert not output.exists() and not summary.exists()


@pytest.mark.parametrize("previous,success", [
    (hashlib.sha256(("42:1:" + BOOT).encode()).hexdigest(), False),
    ("b" * 64, True),
    ("", False),
])
def test_runner_session_comparison_reports_only_the_observed_boundary(probe_environment, monkeypatch, previous, success):
    _, summary = probe_environment
    monkeypatch.setenv("PREVIOUS_BOOT_SESSION", previous)
    script = ADMISSION["jobs"]["recheck"]["steps"][0]["run"]
    if success:
        exec(compile(script, "<runner-recheck>", "exec"), {})
        text = summary.read_text()
        assert "different boot sessions" in text
        assert "does not establish Trusted Launch" in text
        assert BOOT not in text and previous not in text
    else:
        with pytest.raises(SystemExit, match="::error::"):
            exec(compile(script, "<runner-recheck>", "exec"), {})
        assert not summary.exists()


def test_workflow_job_environment_does_not_use_a_direct_runner_context():
    for path in WORKFLOWS.glob("*.yaml"):
        document = workflow(path.name)
        for name, job in document.get("jobs", {}).items():
            for key, value in job.get("env", {}).items():
                assert not re.search(r"\$\{\{\s*runner\.", str(value)), f"{path.name}:{name}:{key}"


@pytest.mark.parametrize("file,job,step,expected", [
    ("_release-candidate.yaml", "workspace-assets", "Initialize private collection home", {
        "HOME": "workspace-verification-home",
    }),
    ("_release-candidate.yaml", "engine-input", "Initialize private engine selection home", {
        "HOME": "engine-selection-home",
    }),
    ("_workspace-distribution.yaml", "build", "Initialize private workspace paths", {
        "HOME": "workspace-home", "AZURE_CONFIG_DIR": "workspace-azure",
        "WORKSPACE_PYTHON": "workspace-tools/bin/python",
    }),
])
def test_private_runtime_paths_are_initialized_before_source_steps(tmp_path, file, job, step, expected):
    selected = workflow(file)["jobs"][job]
    assert selected["steps"][0]["name"] == "Require the admitted runner class"
    assert selected["steps"][1]["name"] == step
    environment_file = tmp_path / "environment"
    runtime = tmp_path / "runner temp"
    runtime.mkdir()
    result = run_script(
        selected["steps"][1]["run"], tmp_path,
        {"RUNNER_TEMP": runtime.as_posix(), "GITHUB_ENV": environment_file.as_posix()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = dict(line.split("=", 1) for line in environment_file.read_text().splitlines())
    assert values == {key: runtime.as_posix() + "/" + path for key, path in expected.items()}
    assert (runtime / expected["HOME"]).is_dir()
    if "AZURE_CONFIG_DIR" in expected:
        assert (runtime / expected["AZURE_CONFIG_DIR"]).is_dir()


@pytest.mark.parametrize("mode,visible,hidden", [
    ("ci-only", (), ("Runner admission", "Installer", "Preview", "Non-release attestation")),
    ("runner-check", ("Runner admission and diagnostics",), ("Installer", "Preview", "Non-release attestation")),
    ("attestation-check", ("Non-release attestation",), ("Runner admission", "Installer", "Preview")),
    ("installer-check", ("Runner admission", "Installer"), ("Preview", "Non-release attestation")),
    ("release-preview", ("Runner admission", "Preview"), ("Installer", "Non-release attestation")),
])
def test_ci_overview_reports_only_the_selected_path(tmp_path, monkeypatch, mode, visible, hidden):
    job = workflow("ci.yaml")["jobs"]["overview"]
    assert job["if"] == "always()"
    assert job["permissions"] == {}
    assert job["runs-on"] == "ubuntu-latest"
    assert job["needs"] == [
        "lint", "test", "linux-bootstrap", "windows-bootstrap", "validate", "release-runner",
        "installer-check", "release-preview", "attestation-check",
    ]
    assert len(job["steps"]) == 1 and "uses" not in job["steps"][0]
    summary = tmp_path / "summary"
    values = {
        "CI_MODE": mode, "SOURCE_SHA": "a" * 40,
        "LINT_RESULT": "success", "TEST_RESULT": "failure", "VALIDATE_RESULT": "success",
        "LINUX_RESULT": "success", "WINDOWS_RESULT": "failure",
        "RUNNER_RESULT": "success", "INSTALLER_RESULT": "skipped", "PREVIEW_RESULT": "cancelled",
        "ATTESTATION_RESULT": "failure",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    exec(compile(job["steps"][0]["run"], "<ci-overview>", "exec"), {})
    text = summary.read_text()
    assert "| Lint | Passed |" in text and "| Unit tests | Failed |" in text
    assert "| Manifests | Passed |" in text
    assert ("| Linux bootstrap (Ubuntu 26.04) | Passed |" in text) is (mode == "ci-only")
    assert ("| Windows bootstrap | Failed |" in text) is (mode == "ci-only")
    for label in visible:
        assert "| " + label + " |" in text
    for label in hidden:
        assert "| " + label + " |" not in text
    if mode == "installer-check":
        assert "| Installer | Skipped |" in text
    if mode == "release-preview":
        assert "| Preview | Cancelled |" in text
    if mode == "attestation-check":
        assert "| Non-release attestation | Failed |" in text
        assert "signs public, non-release evidence" in text


@pytest.mark.parametrize("changes", [
    {"CI_MODE": "unreviewed"},
    {"SOURCE_SHA": "unsafe | source"},
    {"TEST_RESULT": "untrusted | result"},
    {"LINUX_RESULT": "untrusted | result"},
    {"WINDOWS_RESULT": "untrusted | result"},
])
def test_ci_overview_does_not_publish_unsupported_values(tmp_path, monkeypatch, changes):
    summary = tmp_path / "summary"
    summary.write_text("existing\n")
    values = {
        "CI_MODE": "ci-only", "SOURCE_SHA": "a" * 40,
        "LINT_RESULT": "success", "TEST_RESULT": "success", "LINUX_RESULT": "success",
        "WINDOWS_RESULT": "success", "VALIDATE_RESULT": "success",
        "GITHUB_STEP_SUMMARY": str(summary), **changes,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match="::error::"):
        exec(compile(workflow("ci.yaml")["jobs"]["overview"]["steps"][0]["run"], "<ci-overview>", "exec"), {})
    assert summary.read_text() == "existing\n"

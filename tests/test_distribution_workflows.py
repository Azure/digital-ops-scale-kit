"""Guards for the Site Ops build, attestation, and qualification workflows.

The trust boundary lives in workflow structure: which job may execute repository
source, which job may sign, which bytes reach qualification and publication, and
which policy values the verification commands pin. These tests read the workflow
documents directly and run the trust-critical shell and Python snippets against
fakes, so a change that widens the boundary fails here.

`on` parses as the boolean True, since YAML 1.1 treats it as a keyword.
"""

import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.native_bundle import (
    NETWORK_BLOCK,
    native_only,
    publish_assets,
)
from tests.native_bundle import (
    bundle_factory as bundle_factory,
)
from tests.native_uv_consumers import managed_python, native_uv
from tests.shell_helpers import (
    bash_path as _bash_path,
)
from tests.shell_helpers import (
    required_bash as _required_bash,
)
from tests.shell_helpers import (
    run_script as _run_script,
)
from tests.shell_helpers import (
    write_executable as _write_executable,
)
from tests.verification_helpers import verified_observation

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE_PATH = WORKFLOWS / "_siteops-distribution.yaml"
CANDIDATE_PATH = WORKFLOWS / "_release-candidate.yaml"
RELEASE_PATH = WORKFLOWS / "release.yaml"
CI_PATH = WORKFLOWS / "ci.yaml"
GUIDE_PATH = REPO_ROOT / "docs" / "install-siteops.md"

ON = True
ARCHIVE_NAME = "siteops-install.zip"
BOOTSTRAP_PS1 = "siteops-bootstrap.ps1"
BOOTSTRAP_SH = "siteops-bootstrap.sh"
ATTESTATION_SUFFIX = ".attestation.jsonl"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
SIGNER_WORKFLOW = ".github/workflows/_siteops-distribution.yaml"
QUALIFIED_PLATFORMS = ("ubuntu-24.04", "windows-2025")
QUALIFIED_PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
WHEEL_NAME = "siteops-1.0.0b1+build.42.1.gcccccccccccc-py3-none-any.whl"

ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
}

VERIFY_FLAGS = (
    "--bundle",
    "--repo",
    "--cert-identity",
    "--signer-digest",
    "--source-digest",
    "--source-ref",
    "--cert-oidc-issuer",
    "--predicate-type",
    "--format",
)


def _document(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


REUSABLE = _document(REUSABLE_PATH)
CANDIDATE = _document(CANDIDATE_PATH)
RELEASE = _document(RELEASE_PATH)
CI = _document(CI_PATH)


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _script(job: dict, name: str) -> str:
    return _step(job, name)["run"]


def _step_names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job["steps"]]


def _all_steps(document: dict):
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            yield step


def test_ci_rehearsal_requires_an_explicit_manual_request_and_source_commit():
    inputs = CI[ON]["workflow_dispatch"]["inputs"]
    assert inputs["run-mode"]["type"] == "choice"
    assert inputs["run-mode"]["options"] == ["ci-only", "runner-check", "attestation-check", "installer-check", "release-preview"]
    assert inputs["run-mode"]["default"] == "ci-only"
    assert inputs["expected-source-sha"]["type"] == "string"
    assert inputs["expected-source-sha"]["required"] is False
    assert inputs["expected-source-sha"]["default"] == ""
    assert inputs["release-file"]["type"] == "string"
    assert inputs["release-file"]["required"] is False
    assert inputs["release-file"]["default"] == (
        ".github/release-examples/workspace-preview/release.json"
    )
    job = CI["jobs"]["installer-check"]
    assert job["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && "
        "inputs.run-mode == 'installer-check' }}"
    )
    assert job["uses"] == "./.github/workflows/_siteops-distribution.yaml"
    assert job["with"] == {
        "expected-source-sha": "${{ inputs.expected-source-sha }}",
        "release-pool": "${{ needs.release-runner.outputs.pool }}",
    }
    assert "steps" not in job
    assert "secrets" not in job
    assert job["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }


def test_ci_rehearsal_preserves_normal_ci_permissions_and_cannot_promote():
    assert CI["permissions"] == {"contents": "read"}
    for name in ("lint", "test", "validate"):
        assert CI["jobs"][name]["permissions"] == {"contents": "read"}
    assert all(job.get("permissions", {}).get("contents") != "write" for job in CI["jobs"].values())
    assert "release.yaml" not in yaml.safe_dump(CI["jobs"]["installer-check"])
    release = CI["jobs"]["release-preview"]
    assert release["needs"] == ["lint", "test", "validate", "release-runner"]
    assert release["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.run-mode == 'release-preview' }}"
    )
    assert release["uses"] == "./.github/workflows/_release-candidate.yaml"
    assert release["with"] == {
        "expected-source-sha": "${{ inputs.expected-source-sha }}",
        "intent": "${{ inputs.release-file }}",
        "dry-run": True,
        "release-pool": "${{ needs.release-runner.outputs.pool }}",
    }
    assert release["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }


def _fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    """Install a recording `gh` and a `python3` shim on a test-owned PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "gh-invocations.log"
    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
{
  printf '=== gh ===\\n'
  for argument in "$@"; do printf '%s\\n' "$argument"; done
} >> "$FAKE_GH_LOG"

if [[ "$1 $2" == "attestation verify" ]]; then
  if [[ -n "${FAKE_GH_VERIFICATION:-}" ]]; then cat "$FAKE_GH_VERIFICATION"; fi
  if [[ "$3" == *.whl ]]; then
    exit "${FAKE_GH_WHEEL_EXIT:-${FAKE_GH_ATTESTATION_EXIT:-0}}"
  fi
  if [[ "$3" == *.ps1 || "$3" == *.sh ]]; then
    exit "${FAKE_GH_BOOTSTRAP_EXIT:-${FAKE_GH_ATTESTATION_EXIT:-0}}"
  fi
  exit "${FAKE_GH_ATTESTATION_EXIT:-0}"
fi
if [[ "$1" == "api" ]]; then
  if [[ -n "${FAKE_GH_API_RESPONSE:-}" ]]; then
    cat "$FAKE_GH_API_RESPONSE"
  fi
  exit "${FAKE_GH_API_EXIT:-0}"
fi
exit 1
""",
    )
    for name in ("python", "python3"):
        _write_executable(
            bin_dir / name,
            """#!/usr/bin/env bash
exec "$FAKE_PYTHON" "$@"
""",
        )
    return bin_dir, log


def _invocations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls: list[list[str]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "=== gh ===":
            calls.append([])
        else:
            calls[-1].append(line)
    return calls


# --- Source execution and signing capability stay in separate jobs ------------


def test_build_job_executes_source_without_signing_capability():
    build = REUSABLE["jobs"]["build"]
    assert build["permissions"] == {"contents": "read"}
    assert build["runs-on"] == ["${{ inputs.release-pool }}"]
    assert "environment" not in build
    assert all(step.get("uses", "").split("@")[0] != "actions/attest" for step in build["steps"])


def test_signer_job_holds_signing_capability_without_source_execution():
    attest = REUSABLE["jobs"]["attest"]
    assert attest["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }
    used = {step["uses"].split("@")[0] for step in attest["steps"] if "uses" in step}
    assert used == {
        "actions/download-artifact",
        "actions/attest",
        "actions/upload-artifact",
    }
    for step in attest["steps"]:
        script = step.get("run", "")
        assert "scripts/" not in script
        assert "git " not in script


def test_build_checks_out_the_asserted_event_commit_without_credentials():
    checkout = _step(REUSABLE["jobs"]["build"], "Checkout the event commit")
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "repository": "${{ github.repository }}",
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
        "clean": True,
        "fetch-depth": 1,
    }


def test_expected_source_sha_is_only_an_assertion():
    inputs = REUSABLE[ON]["workflow_call"]["inputs"]
    assert set(inputs) == {"expected-source-sha", "version-mode", "report-summary", "release-pool"}
    assert REUSABLE[ON]["workflow_call"]["inputs"]["version-mode"]["default"] == "build"
    assert inputs["report-summary"] == {
        "description": "Write the aggregate distribution report to the workflow summary.",
        "required": False,
        "type": "boolean",
        "default": True,
    }
    for step in _all_steps(REUSABLE):
        if "uses" in step:
            rendered = yaml.safe_dump(step)
            assert "expected-source-sha" not in rendered
    assertion = _script(REUSABLE["jobs"]["build"], "Assert the expected source commit")
    assert '"$EXPECTED_SOURCE_SHA" != "$SOURCE_SHA"' in assertion
    assert "^[0-9a-f]{40}$" in assertion


def test_build_job_passes_the_event_identity_to_the_producer():
    build = REUSABLE["jobs"]["build"]
    assert build["env"]["SOURCE_SHA"] == "${{ github.sha }}"
    requirements = _script(build, "Install the pinned build requirements")
    assert "--require-hashes" in requirements
    assert "scripts/siteops-build-requirements.txt" in requirements
    script = _script(build, "Build the installation bundle")
    assert "python scripts/build-siteops-bundle.py" in script
    for flag, value in (
        ("--repository", '"$SOURCE_REPOSITORY"'),
        ("--source-ref", '"$SOURCE_REF"'),
        ("--expected-source-sha", '"$SOURCE_SHA"'),
        ("--build-number", '"$BUILD_NUMBER"'),
        ("--build-attempt", '"$BUILD_ATTEMPT"'),
        ("--output", '"$archive"'),
    ):
        assert f"{flag} {value}" in script
    assert "--download-dependencies" in script


def test_the_build_publishes_wheel_bundle_and_exact_bootstrap_scripts():
    build = REUSABLE["jobs"]["build"]
    assert build["outputs"]["wheel-name"] == "${{ steps.build.outputs.wheel-name }}"
    assert build["outputs"]["wheel-sha256"] == "${{ steps.build.outputs.wheel-sha256 }}"
    script = _script(build, "Build the installation bundle")
    assert "${#produced[@]} -ne 4" in script
    assert 'git show "$SOURCE_SHA:scripts/bootstrap/$script"' in script
    assert '"wheels/" + wheel.name' in script
    assert "is not the archive member byte for byte" in script
    assert 'document["package"]["wheel"] != member' in script
    assert "^siteops-[A-Za-z0-9._+!-]+-py3-none-any\\.whl$" in script
    for output in (
        "archive-sha256", "wheel-name", "wheel-sha256",
        "bootstrap-ps1-sha256", "bootstrap-sh-sha256", "staging-path",
    ):
        assert f'echo "{output}=' in script
    upload = _step(build, "Upload the build artifacts")["with"]
    assert upload["path"] == "${{ steps.build.outputs.staging-path }}"


# --- The attested bytes are the exact build output ---------------------------


def test_attestation_binds_the_exact_build_artifact_of_this_run():
    attest = REUSABLE["jobs"]["attest"]
    download = _step(attest, "Download the build artifacts")["with"]
    assert download["artifact-ids"] == "${{ needs.build.outputs.artifact-id }}"
    assert download["run-id"] == "${{ github.run_id }}"
    assert download["repository"] == "${{ github.repository }}"
    assert download["digest-mismatch"] == "error"

    payload = _script(attest, "Confirm the staged payload")
    assert "${#entries[@]} -ne 4" in payload
    assert "$EXPECTED_ARCHIVE_SHA256" in payload
    assert "$EXPECTED_WHEEL_SHA256" in payload
    assert "$EXPECTED_BOOTSTRAP_PS1_SHA256" in payload
    assert "$EXPECTED_BOOTSTRAP_SH_SHA256" in payload


def test_each_published_asset_is_signed_independently():
    attest = REUSABLE["jobs"]["attest"]
    subjects = [
        step for step in attest["steps"] if step.get("uses", "").startswith("actions/attest@")
    ]
    assert [step["name"] for step in subjects] == [
        "Attest archive provenance",
        "Attest wheel provenance",
        "Attest PowerShell bootstrap provenance",
        "Attest Bash bootstrap provenance",
    ]
    assert [step["with"]["subject-path"] for step in subjects] == [
        f"${{{{ runner.temp }}}}/siteops-subject/{ARCHIVE_NAME}",
        "${{ runner.temp }}/siteops-subject/${{ needs.build.outputs.wheel-name }}",
        f"${{{{ runner.temp }}}}/siteops-subject/{BOOTSTRAP_PS1}",
        f"${{{{ runner.temp }}}}/siteops-subject/{BOOTSTRAP_SH}",
    ]
    for step in subjects:
        # Omitting every predicate input selects SLSA build provenance.
        assert set(step["with"]) == {"subject-path", "show-summary"}
        assert step["with"]["show-summary"] is False

    staging = _script(attest, "Stage the attested bytes")
    assert "${#staged[@]} -ne 8" in staging
    assert '"$ARCHIVE_BUNDLE_PATH"' in staging
    assert '"$WHEEL_BUNDLE_PATH"' in staging
    assert '"$PS1_BUNDLE_PATH"' in staging
    assert '"$SH_BUNDLE_PATH"' in staging


def test_staging_uploads_never_overwrite_and_fail_on_empty_input():
    uploads = [
        step
        for step in _all_steps(REUSABLE)
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 2
    for upload in uploads:
        assert upload["with"]["overwrite"] is False
        assert upload["with"]["if-no-files-found"] == "error"
        assert "${{ github.run_id }}" in upload["with"]["name"] or (
            upload["with"]["name"].startswith("${{ steps.names.outputs")
        )


def test_every_action_is_pinned_to_the_reviewed_commit():
    for document in (REUSABLE, CANDIDATE, RELEASE, CI):
        for step in _all_steps(document):
            reference = step.get("uses")
            if reference is None or reference.startswith("./"):
                continue
            action, _, sha = reference.partition("@")
            assert action in ACTION_PINS, action
            assert sha == ACTION_PINS[action], action


# --- Qualification verifies the same bytes before touching them --------------


def test_qualification_runs_on_hosted_windows_and_linux_without_write_access():
    qualify = REUSABLE["jobs"]["qualify"]
    assert qualify["strategy"]["matrix"]["os"] == list(QUALIFIED_PLATFORMS)
    assert qualify["strategy"]["matrix"]["python"] == list(QUALIFIED_PYTHONS)
    assert _step(qualify, "Setup Python")["with"]["python-version"] == "${{ matrix.python }}"
    assert qualify["name"] == "Qualify bundle (${{ matrix.os }}, Python ${{ matrix.python }})"
    assert qualify["runs-on"] == "${{ matrix.os }}"
    assert qualify["permissions"] == {"contents": "read", "actions": "read"}
    assert qualify["needs"] == ["build", "attest"]


def test_qualification_uses_the_cell_runtime_and_packaged_uv_helper():
    qualify = REUSABLE["jobs"]["qualify"]
    tooling = _script(qualify, "Install the external qualification tooling")
    verified = _script(qualify, "Install Site Ops from the verified lock")
    online = _script(qualify, "Install Site Ops from the standalone wheel")
    assert qualify["env"]["MATRIX_PYTHON"] == "${{ matrix.python }}"
    assert '"$uv" python install "$MATRIX_PYTHON"' in tooling
    assert "UV_PYTHON_INSTALL_DIR" in tooling
    assert "sys._base_executable" in tooling
    assert "siteops-install.py" in verified
    assert '"$APP_PYTHON" -I -S -B "$helper" "$mode"' in verified
    assert 'install_locked install install "$extract"' in verified
    assert 'install_locked repair replace "$extract"' in verified
    assert '"$SITEOPS_UV" tool uninstall siteops' in verified
    assert '"$SITEOPS_UV" tool install "$wheel"' in online
    assert "--no-python-downloads" in online
    assert "pipx" not in tooling + verified + online


def test_qualification_keeps_windows_state_under_one_private_user_profile_root():
    qualify = REUSABLE["jobs"]["qualify"]
    tooling = _script(qualify, "Install the external qualification tooling")
    assert '[Environment]::GetFolderPath("UserProfile")' in tooling
    assert "SITEOPS_QUALIFICATION_ROOT=$native_root" in tooling
    assert "GITHUB_RUN_ID" in tooling and "GITHUB_RUN_ATTEMPT" in tooling
    assert "ROOT_ANCESTOR_ACL" in tooling
    assert 'owned="$temp/siteops-qualification"' in tooling
    for name in (
        "Install with the signed Bash bootstrap",
        "Install Site Ops from the verified lock",
        "Install Site Ops from the standalone wheel",
    ):
        script = _script(qualify, name)
        assert 'owned="${SITEOPS_QUALIFICATION_ROOT//\\\\//}"' in script
        assert 'owned="$temp/siteops-qualification"' not in script
        assert (
            'download="$temp/siteops-download"' in script
            or 'archive="$temp/siteops-download/' in script
            or 'wheel="$temp/siteops-download/' in script
        )
    windows = _script(qualify, "Install with the signed PowerShell bootstrap")
    assert "$owned = $env:SITEOPS_QUALIFICATION_ROOT" in windows
    assert "Join-Path $env:RUNNER_TEMP 'siteops-qualification'" not in windows
    for directory in ("home", "bootstrap-profile", "bootstrap-temp", "python", "cache", "tooling"):
        assert directory in windows
    assert "$download = Join-Path $env:RUNNER_TEMP 'siteops-download'" in windows


def test_windows_native_scratch_and_retained_bundle_use_the_selected_root():
    qualify = REUSABLE["jobs"]["qualify"]
    tooling = _script(qualify, "Install the external qualification tooling")
    verified = _script(qualify, "Install Site Ops from the verified lock")
    online = _script(qualify, "Install Site Ops from the standalone wheel")
    assert 'mkdir "$owned/temp"' in tooling
    for script in (tooling, verified, online):
        assert 'export TEMP="$owned/temp" TMP="$owned/temp" TMPDIR="$owned/temp"' in script
        assert 'export LOCALAPPDATA="$owned/home"' in script
        assert 'if [[ "$RUNNER_OS" == "Windows" ]]; then' in script
    assert 'cp -R "$extract" "$owned/verified-bundle"' in verified
    assert 'extract="$owned/verified-bundle"' in verified
    assert verified.index('extract="$owned/verified-bundle"') < verified.index(
        'install_locked install install "$extract"',
    )
    assert 'wheel="$temp/siteops-download/$WHEEL_NAME"' in online
    windows = _script(qualify, "Install with the signed PowerShell bootstrap")
    assert "$env:TMP = $env:TEMP" in windows
    assert "$env:TMPDIR = $env:TEMP" in windows


def test_qualification_verifies_before_it_extracts():
    names = _step_names(REUSABLE["jobs"]["qualify"])
    assert names.index(    "Verify installation assets before use") < names.index(
        "Extract the verified bundle"
    )
    assert names.index("Extract the verified bundle") < names.index(
        "Confirm the bundle describes this build"
    )
    assert names.index("Confirm the bundle describes this build") < names.index(
        "Install the external qualification tooling"
    )
    assert names.index("Install the external qualification tooling") < names.index(
        "Install Site Ops from the verified lock"
    )
    for name in ("Install with the signed Bash bootstrap", "Install with the signed PowerShell bootstrap"):
        assert names.index("Install the external qualification tooling") < names.index(name)
        assert names.index(name) < names.index("Install Site Ops from the verified lock")
    assert names.index("Install Site Ops from the verified lock") < names.index(
        "Install Site Ops from the standalone wheel"
    )


def test_qualification_runs_both_signed_scripts_with_private_preseeded_assets():
    qualify = REUSABLE["jobs"]["qualify"]
    for name, platform, argument in (
        ("Install with the signed Bash bootstrap", "ubuntu-24.04", "--yes"),
        ("Install with the signed PowerShell bootstrap", "windows-2025", "-Yes"),
    ):
        step = _step(qualify, name)
        assert step["if"] == f"matrix.os == '{platform}'"
        if platform == "windows-2025":
            assert step["shell"] == "powershell"
        script = step["run"]
        if platform == "ubuntu-24.04":
            assert '"$download/$BOOTSTRAP_SH"' in script
            assert '"$download/${ARCHIVE_NAME}${ATTESTATION_SUFFIX}"' in script
        else:
            assert "$env:BOOTSTRAP_PS1" in script
            assert "$env:ARCHIVE_NAME + $env:ATTESTATION_SUFFIX" in script
        assert argument in script
        assert "install-downloads" in script and "Rechecking the retained release" in script
        assert "SOURCE_SHA" in script and "SOURCE_REF" in script
        assert "GH_CONFIG_DIR" in script and "UV_TOOL_DIR" in script
        assert "siteops" in script and "PACKAGE_VERSION" in script
        assert "GH_TOKEN" in script and "GITHUB_TOKEN" in script
        assert "az login" not in script and "--with-azure-cli" not in script
    bash_step = _script(qualify, "Install with the signed Bash bootstrap")
    assert 'bash "$download/$BOOTSTRAP_SH"' in bash_step
    windows_step = _script(qualify, "Install with the signed PowerShell bootstrap")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File" in windows_step


def test_every_qualification_consumer_checks_the_matrix_app_runtime():
    qualify = REUSABLE["jobs"]["qualify"]
    for name in (
        "Install with the signed Bash bootstrap",
        "Install with the signed PowerShell bootstrap",
        "Install Site Ops from the standalone wheel",
    ):
        script = _script(qualify, name)
        assert "pyvenv.cfg" in script and "MATRIX_PYTHON" in script
        assert script.index("pyvenv.cfg") < script.index("--version")
        assert "APP_PYTHON" in script and "version_info" in script
    verified = _script(qualify, "Install Site Ops from the verified lock")
    assert '"$APP_PYTHON" -I -S -B "$helper"' in verified
    assert '"$SITEOPS_UV" tool uninstall siteops' in verified


@pytest.mark.parametrize(("caller", "source_ref", "accepted"), [
    ("ci.yaml", "refs/heads/feat/siteops-guided-inputs", True),
    ("release.yaml", "refs/heads/main", True),
    ("other.yaml", "refs/heads/feat/siteops-guided-inputs", False),
])
def test_signed_bash_qualification_resolves_caller_before_branch_path(
    tmp_path, caller, source_ref, accepted,
):
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed Bash bootstrap")
    block = "caller=" + script.split("caller=", 1)[1].split("release=", 1)[0]
    result = _run_script(
        "set -euo pipefail\n" + block + "printf '%s\\n' \"$caller\"\n",
        tmp_path,
        {
            "BUILDER_IDENTITY": (
                f"https://github.com/example/publisher/.github/workflows/{caller}@{source_ref}"
            ),
        },
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    if accepted:
        assert result.stdout.strip() == caller


def test_windows_bootstrap_qualification_captures_native_stderr_then_checks_exit():
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    invocation = script.index("& powershell.exe -NoProfile -ExecutionPolicy Bypass -File")
    assert "$ErrorActionPreference = 'Continue'" in script[invocation - 170:invocation]
    assert "$bootstrapStatus = $LASTEXITCODE" in script[invocation:invocation + 310]
    assert "$ErrorActionPreference = $previousPreference" in script[invocation:invocation + 420]
    assert "if ($bootstrapStatus -ne 0)" in script


def test_windows_bootstrap_qualification_selects_private_user_profile_before_cache():
    tooling = _script(REUSABLE["jobs"]["qualify"], "Install the external qualification tooling")
    assert '[Environment]::GetFolderPath("UserProfile")' in tooling
    assert "SITEOPS_QUALIFICATION_ROOT=$native_root" in tooling
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    assert "$owned = $env:SITEOPS_QUALIFICATION_ROOT" in script
    selection = "$env:LOCALAPPDATA = Join-Path $owned 'bootstrap-profile'"
    assert selection in script
    assert script.index(selection) < script.index('$cache = Join-Path $env:LOCALAPPDATA')
    assert script.index("$bootstrapData = Join-Path $env:LOCALAPPDATA 'siteops'") < script.index(
        '$cache = Join-Path $env:LOCALAPPDATA',
    )
    assert script.index("& icacls.exe $directory /inheritance:r") < script.index(
        '$cache = Join-Path $env:LOCALAPPDATA',
    )
    assert script.index("& icacls.exe $directory /setowner") < script.index(
        '$cache = Join-Path $env:LOCALAPPDATA',
    )
    assert "$env:UV_PYTHON_INSTALL_DIR = Join-Path $owned 'python'" in script


def test_windows_bootstrap_qualification_owns_every_preloaded_cache_directory():
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    preload = script.split("$cache = Join-Path $env:LOCALAPPDATA", 1)[1].split(
        "Copy-Item -LiteralPath", 1,
    )[0]
    assert "foreach ($directory in @($cacheRoot, $cache))" in preload
    assert '& icacls.exe $directory /setowner "*$sid"' in preload
    assert "-Force" not in preload
    assert script.index(preload) < script.index("& powershell.exe -NoProfile -ExecutionPolicy Bypass")
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    lines = {line.strip() for line in bootstrap.splitlines()}
    # The preloaded directories are exactly the unmanaged cache roots the bootstrap admits.
    assert "$cacheRoot = Join-Path $data 'install-downloads'" in lines
    assert "Require-PrivateDataRoot $cacheRoot" in lines
    assert "Require-PrivateDataRoot $cache" in lines


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL ancestry needs PowerShell.")
def test_windows_qualification_profile_ancestry_before_tool_selection(tmp_path):
    source = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    helper = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    assert helper is not None
    boundary = "\n    try {\n        $exists = Test-Path -LiteralPath $Path"
    assert boundary in helper.group(0)
    ancestor_guard = helper.group(0).split(boundary, 1)[0]
    wrapper = tmp_path / "runner-ancestry.ps1"
    wrapper.write_text(
        "$ErrorActionPreference='Stop'\n"
        "function Fail([string]$message) { throw $message }\n"
        + ancestor_guard
        + "\n    'RUNNER_ANCESTRY_ADMITTED'\n}\n"
        "Require-PrivateDataRoot $env:TEST_QUALIFICATION_ROOT -Managed\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
        ],
        env={
            **os.environ,
            "TEST_QUALIFICATION_ROOT": str(tmp_path / "siteops-qualification-42-1"),
        },
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RUNNER_ANCESTRY_ADMITTED" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL rules need Windows PowerShell 5.1.")
@pytest.mark.parametrize(
    ("preexisting", "parent_rights"),
    [(False, "RX"), (True, "RX"), (False, "M")],
)
def test_windows_bootstrap_qualification_protects_data_root_and_ancestry(
    tmp_path, preexisting, parent_rights,
):
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    setup = script.split(
        "$env:HOME = Join-Path $owned 'home'\n", 1,
    )[1].split("if ($env:BUILDER_IDENTITY -notmatch", 1)[0]
    source = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    helper = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    assert helper is not None
    preload = "$cache = Join-Path $env:LOCALAPPDATA" + script.split(
        "$cache = Join-Path $env:LOCALAPPDATA", 1,
    )[1].split("Copy-Item -LiteralPath", 1)[0]
    profile = tmp_path / "profile"
    profile.mkdir()
    grant = subprocess.run(
        ["icacls.exe", str(profile), "/grant", f"*S-1-5-32-545:(OI)(CI){parent_rights}"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot set the fixture ACL.")
    try:
        owned = profile / "owned"
        owned.mkdir()
        existing = owned / "bootstrap-profile"
        if preexisting:
            existing.mkdir()
            (existing / "sentinel").write_text("keep", encoding="utf-8")
        wrapper = tmp_path / "qualify-root.ps1"
        wrapper.write_text(
            "$ErrorActionPreference='Stop'\n"
            "$owned=$env:TEST_OWNED\n"
            "$profileHome=$env:TEST_PROFILE_HOME\n"
            + "$env:HOME = Join-Path $owned 'home'\n" + setup
            + "\nfunction Fail([string]$message) { throw \"Site Ops installation: $message\" }\n"
            + helper.group(0)
            + "\n$selectionId = 'fixture'\n" + preload
            + "Require-PrivateDataRoot (Join-Path $env:LOCALAPPDATA 'siteops')\n"
            "Require-PrivateDataRoot $cacheRoot\n"
            "Require-PrivateDataRoot $cache\n"
            "'PRIVATE_ROOT_ACCEPTED'\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            cwd=tmp_path,
            env={
                **os.environ,
                "TEST_PROFILE_HOME": str(profile),
                "TEST_OWNED": str(owned),
                "PYTHON": sys.executable,
            },
            capture_output=True, text=True, timeout=30,
        )
        if preexisting:
            assert result.returncode != 0
            assert "The Windows qualification data root was not fresh." in result.stderr
            assert (existing / "sentinel").read_text(encoding="utf-8") == "keep"
        elif parent_rights == "M":
            assert result.returncode != 0
            assert "ROOT_ANCESTOR_ACL" in result.stderr
        else:
            assert result.returncode == 0, "The preseeded qualification root was not private."
            assert "PRIVATE_ROOT_ACCEPTED" in result.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(profile), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


def _run_windows_bootstrap_qualifier(
    tmp_path: Path,
    child: str,
    extra_env: dict[str, str],
    *,
    success_marker: bool = False,
) -> subprocess.CompletedProcess[str]:
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    block = script.split("$log = Join-Path $owned 'logs\\bootstrap-ps1.log'\n", 1)[1].split(
        "if ((Get-Content -LiteralPath $log -Raw) -notmatch", 1,
    )[0]
    (tmp_path / "logs").mkdir()
    fake = tmp_path / "bootstrap.ps1"
    fake.write_text(
        "param([string]$Release,[string]$SourceCommit,[string]$Repository,"
        "[string]$SourceRef,[string]$Caller,[switch]$Yes)\n" + child,
        encoding="utf-8",
    )
    wrapper = tmp_path / "qualify.ps1"
    wrapper.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$owned=$env:TEST_ROOT;$download=$owned\n"
        "$script=Join-Path $download $env:BOOTSTRAP_PS1\n"
        "$log=Join-Path $owned 'logs\\bootstrap-ps1.log'\n"
        "$release='siteops/v0.0.0-ci';$caller='ci.yaml'\n"
        + block + ("\n'CAPTURE_ACCEPTED'\n" if success_marker else "\n"),
        encoding="utf-8",
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path,
        env={
            **os.environ, "TEST_ROOT": str(tmp_path), "BOOTSTRAP_PS1": fake.name,
            "SOURCE_SHA": "c" * 40, "SOURCE_REPOSITORY": "example/publisher",
            "SOURCE_REF": "refs/heads/feat/siteops-guided-inputs",
            **extra_env,
        },
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Native PowerShell 5.1 redirects the private child log.")
@pytest.mark.parametrize(
    ("private_message", "public_message"),
    [
        *[
            (
                f"Configure a private Site Ops data root. {code} PRIVATE_PATH",
                f"The signed PowerShell bootstrap rejected the isolated data root ({code}).",
            )
            for code in (
                "ROOT_PATH", "ROOT_ANCESTOR_TYPE", "ROOT_ANCESTOR_OWNER",
                "ROOT_ANCESTOR_ACL", "ROOT_DATA_CREATE", "ROOT_DATA_TYPE",
                "ROOT_DATA_OWNER", "ROOT_DATA_ACL",
            )
        ],
        (
            "Configure a private Site Ops data root. PRIVATE_PATH",
            "The signed PowerShell bootstrap rejected the isolated data root.",
        ),
    ],
)
def test_windows_bootstrap_qualification_reports_only_bounded_failure(
    tmp_path, private_message, public_message,
):
    result = _run_windows_bootstrap_qualifier(
        tmp_path,
        "function Fail([string]$message) { throw \"Site Ops installation: $message\" }\n"
        "Fail $env:TEST_PRIVATE_MESSAGE\n",
        {"TEST_PRIVATE_MESSAGE": private_message},
    )
    assert result.returncode != 0
    assert public_message in result.stderr
    assert "PRIVATE_PATH" not in result.stdout + result.stderr
    assert "PRIVATE_URL" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native PowerShell 5.1 formats root errors.")
def test_windows_bootstrap_qualification_classifies_real_acl_failure(tmp_path):
    source = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    helper = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    assert helper is not None
    shared = tmp_path / "shared"
    shared.mkdir()
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the fixture ACL.")
    try:
        result = _run_windows_bootstrap_qualifier(
            tmp_path,
            "function Fail([string]$message) { throw \"Site Ops installation: $message\" }\n"
            + helper.group(0)
            + "\nRequire-PrivateDataRoot $env:TEST_DATA_ROOT\n",
            {"TEST_DATA_ROOT": str(shared / "siteops")},
        )
        assert result.returncode != 0
        assert "rejected the isolated data root (ROOT_ANCESTOR_ACL)" in result.stderr
        assert str(shared) not in result.stdout + result.stderr
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native stderr needs Windows PowerShell 5.1.")
@pytest.mark.parametrize(("bootstrap_exit", "emit_diagnostic"), [(0, True), (7, True), (7, False)])
def test_windows_qualification_preserves_child_exit_with_private_stderr(
    tmp_path, bootstrap_exit, emit_diagnostic,
):
    result = _run_windows_bootstrap_qualifier(
        tmp_path,
        "if ($env:TEST_EMIT_DIAGNOSTIC -eq '1') { "
        "[Console]::Error.WriteLine('controlled benign diagnostic') }\n"
        "exit [int]$env:TEST_BOOTSTRAP_EXIT\n",
        {
            "TEST_BOOTSTRAP_EXIT": str(bootstrap_exit),
            "TEST_EMIT_DIAGNOSTIC": "1" if emit_diagnostic else "0",
        },
        success_marker=True,
    )
    assert (result.returncode == 0) is (bootstrap_exit == 0), result.stdout + result.stderr
    if bootstrap_exit:
        assert "did not install the verified build" in result.stderr
        assert "CAPTURE_ACCEPTED" not in result.stdout
    else:
        assert "CAPTURE_ACCEPTED" in result.stdout
        assert "controlled benign diagnostic" not in result.stdout + result.stderr
    if emit_diagnostic:
        assert "controlled benign diagnostic" in (tmp_path / "logs" / "bootstrap-ps1.log").read_text(
            encoding="utf-16",
        )


def test_bootstrap_qualification_shells_parse():
    bash_step = _script(REUSABLE["jobs"]["qualify"], "Install with the signed Bash bootstrap")
    parsed = subprocess.run(
        [str(_required_bash()), "-n"], input=bash_step, text=True, capture_output=True, timeout=20,
    )
    assert parsed.returncode == 0, parsed.stderr
    if sys.platform == "win32":
        windows_step = _script(
            REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap",
        )
        parsed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "$tokens=$null;$errors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseInput("
                "[Console]::In.ReadToEnd(),[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1}",
            ],
            input=windows_step, text=True, capture_output=True, timeout=20,
        )
        assert parsed.returncode == 0, parsed.stderr


def test_qualification_policy_pins_the_caller_source_and_local_signer():
    qualify = REUSABLE["jobs"]["qualify"]
    environment = qualify["env"]
    assert environment["SOURCE_REPOSITORY"] == "${{ github.repository }}"
    assert environment["SOURCE_SHA"] == "${{ github.sha }}"
    assert environment["SOURCE_REF"] == "${{ github.ref }}"
    assert environment["WHEEL_NAME"] == "${{ needs.build.outputs.wheel-name }}"
    assert environment["PACKAGE_VERSION"] == "${{ needs.build.outputs.package-version }}"
    # A local reusable reference binds to the caller's own event commit, so the
    # signer digest is that commit and the identity carries the caller ref.
    assert environment["SIGNER_DIGEST"] == "${{ github.sha }}"
    assert environment["BUILDER_IDENTITY"] == "https://github.com/${{ github.workflow_ref }}"
    assert environment["SIGNER_IDENTITY"] == (
        f"https://github.com/${{{{ github.repository }}}}/{SIGNER_WORKFLOW}@${{{{ github.ref }}}}"
    )
    script = _script(qualify, "Verify installation assets before use")
    assert '"$download/$BOOTSTRAP_PS1"' in script
    assert '"$download/$BOOTSTRAP_SH"' in script
    for flag in VERIFY_FLAGS:
        assert flag in script


def test_qualification_requires_the_runner_verifier_capabilities():
    script = _script(
        REUSABLE["jobs"]["qualify"], "Confirm the GitHub CLI verification capabilities"
    )
    assert "command -v gh" in script
    for flag in VERIFY_FLAGS:
        assert flag in script
    assert "curl" not in script and "install" not in script


def test_qualification_isolates_tooling_state_under_the_selected_root():
    for name in (
        "Install Site Ops from the verified lock",
        "Install Site Ops from the standalone wheel",
    ):
        script = _script(REUSABLE["jobs"]["qualify"], name)
        for variable in (
            "HOME",
            "UV_TOOL_DIR",
            "UV_TOOL_BIN_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "UV_CACHE_DIR",
        ):
            assert f'export {variable}="$' in script
        assert 'owned="${SITEOPS_QUALIFICATION_ROOT//\\\\//}"' in script
        assert "unset PYTHONPATH PYTHONHOME" in script
        # Raw tool output stays in private run files, never in the job log.
        assert '> "$logs/' in script
        checked = [line for line in script.splitlines() if "--version" in line and "observed=" in line]
        assert checked, name
        for line in checked:
            assert 'observed="$("$command_bin/$command_name" --version' in line, line
            assert '2> "$logs/' in line, line


def test_qualification_installs_both_supported_paths_with_native_uv():
    verified = _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the verified lock")
    assert 'with zipfile.ZipFile(sys.argv[1]) as archive' in verified
    assert 'siteops-install.py").open("xb")' in verified
    for label in ("install", "repeat", "repair", "tampered", "uninstall"):
        assert label in verified
    assert 'install_locked repair replace "$extract"' in verified
    assert 'if install_locked tampered replace "$owned/tampered"' in verified
    assert '"$SITEOPS_UV" tool uninstall siteops --no-config --offline' in verified
    assert 'siteops $PACKAGE_VERSION' in verified
    assert '"$command_bin/siteops"*' in verified

    online = _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the standalone wheel")
    assert '"$SITEOPS_UV" tool install "$wheel"' in online
    assert '--default-index "$index" --no-config --no-build --no-cache' in online
    assert "--no-python-downloads --link-mode copy" in online
    assert 'wheel="$temp/siteops-download/$WHEEL_NAME"' in online
    assert 'siteops $PACKAGE_VERSION' in online
    assert online.index("unset \"$name\"") < online.index('export UV_TOOL_DIR=')


def test_the_qualification_tooling_pins_native_uv_and_each_managed_runtime():
    script = _script(REUSABLE["jobs"]["qualify"], "Install the external qualification tooling")
    for value in (
        "95f9bc30fbb3574d276e28ac4a6de932d25153645853d13da8c21eec3bc88d06",
        "6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f",
        "a0d2742d49564a32488753b02e76276e7b5ef1b1ea8cf30bcbf06ee28f60cd73",
        "b8299463da6fa7da3b94464444d252d0afca8ac6c96cb229f1baf4012f365246",
        "sys._base_executable",
        "APP_PYTHON=$app_python",
    ):
        assert value in script
    assert "pip install" not in script and "pipx" not in script
    assert "packagefeedproxy" not in script


@native_only
def test_required_native_consumer_fixtures_fail_instead_of_skipping(tmp_path, monkeypatch):
    gate = "SITEOPS_REQUIRE_WINDOWS_UV" if os.name == "nt" else "SITEOPS_REQUIRE_LINUX_UV"
    python_input = (
        "SITEOPS_TEST_UV_PYTHON_DIR" if os.name == "nt"
        else "SITEOPS_TEST_UV_PYTHON_ARCHIVE"
    )
    monkeypatch.setenv(gate, "1")
    monkeypatch.delenv("SITEOPS_TEST_UV", raising=False)
    monkeypatch.delenv(python_input, raising=False)
    monkeypatch.setenv("SITEOPS_TEST_PYTHON_ROOT", str(tmp_path / "unselected"))
    with pytest.raises(pytest.fail.Exception, match="SITEOPS_TEST_UV"):
        native_uv()
    with pytest.raises(pytest.fail.Exception, match=python_input):
        managed_python(tmp_path / "missing")

    wrong = tmp_path / "invalid-native"
    wrong.write_bytes(b"not a native tool or managed runtime")
    monkeypatch.setenv("SITEOPS_TEST_UV", str(wrong))
    monkeypatch.setenv(python_input, str(wrong))
    with pytest.raises(pytest.fail.Exception, match="qualified native uv"):
        native_uv()
    with pytest.raises(pytest.fail.Exception, match=python_input):
        managed_python(tmp_path / "invalid")
    assert not (tmp_path / "invalid" / "python").exists()


@native_only
@pytest.mark.parametrize("minor", QUALIFIED_PYTHONS)
def test_matrix_runtime_admission_uses_the_actual_managed_interpreter(native_runtime, minor):
    tooling = _script(REUSABLE["jobs"]["qualify"], "Install the external qualification tooling")
    blocks = re.findall(r"<<'PY'\n(.*?)\n\s*PY(?:\n|$)", tooling, re.S)
    assert len(blocks) == 2
    for number, block in enumerate(blocks, start=1):
        compile(block, f"qualification tooling block {number}", "exec")
    runtime = blocks[-1]
    owned = native_runtime.parents[2] if os.name == "nt" else native_runtime.parents[3]
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", "-B", "-", str(owned), minor,
            "Windows" if os.name == "nt" else "Linux",
        ],
        input=runtime, capture_output=True, text=True, timeout=40,
    )
    if minor == "3.11":
        assert result.returncode == 0, result.stderr
        assert Path(result.stdout.strip()) == native_runtime
    else:
        assert result.returncode != 0
        assert "inventory is ambiguous" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="The pinned Windows archive needs native Windows.")
@pytest.mark.parametrize("case", ["valid", "tampered", "occupied", "unsafe-parent"])
def test_native_qualification_prepares_a_pinned_uv_and_cell_runtime(
    tmp_path, native_runtime, case,
):
    archive = os.environ.get("SITEOPS_TEST_UV_ARCHIVE")
    if not archive:
        if os.environ.get("CI") or os.environ.get("SITEOPS_REQUIRE_WINDOWS_UV") == "1":
            pytest.fail("SITEOPS_TEST_UV_ARCHIVE must name the pinned Windows uv release.")
        pytest.skip("A readonly pinned Windows uv release archive is required.")
    selected = Path(archive)
    assert selected.is_file()
    supplied = tmp_path / "uv-windows.zip"
    supplied.write_bytes(selected.read_bytes() + (b"changed" if case == "tampered" else b""))
    temp = tmp_path / "runner temp"
    temp.mkdir()
    profile = tmp_path / "p"
    profile.mkdir()
    owned = profile / "siteops-qualification-42-1"
    if case == "occupied":
        owned.mkdir()
        (owned / "keep").write_text("not this run", encoding="utf-8")
    if case == "unsafe-parent":
        grant = subprocess.run(
            ["icacls.exe", str(profile), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
            capture_output=True, text=True, timeout=20,
        )
        if grant.returncode:
            pytest.fail("The unsafe profile fixture ACL could not be set.")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
output=""
while (($#)); do
  if [[ "$1" == --output ]]; then output="$2"; shift 2; continue; fi
  shift
done
[[ "$output" == "$TEST_NATIVE_ROOT/uv-windows.zip" ]] || exit 91
cp "$TEST_UV_ARCHIVE" "$output"
cp -R "$TEST_MANAGED_RUNTIME" "$TEST_NATIVE_ROOT/python/"
""",
    )
    environment = {
        "RUNNER_TEMP": _bash_path(temp),
        "RUNNER_OS": "Windows",
        "MATRIX_PYTHON": "3.11",
        "PYTHON": str(Path(sys.executable)),
        "GITHUB_ENV": _bash_path(tmp_path / "github-env"),
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "TEST_PROFILE_HOME": str(profile),
        "TEST_NATIVE_ROOT": str(owned).replace("\\", "/"),
        "TEST_UV_ARCHIVE": _bash_path(supplied),
        "TEST_MANAGED_RUNTIME": _bash_path(native_runtime.parent),
        **NETWORK_BLOCK,
    }
    script = _script(REUSABLE["jobs"]["qualify"], "Install the external qualification tooling")
    discovery = '[Environment]::GetFolderPath("UserProfile")'
    assert script.count(discovery) == 1
    script = script.replace(discovery, "$env:TEST_PROFILE_HOME")
    wrapper = tmp_path / "qualification-tooling.sh"
    exports = [
        f'export PATH={shlex.quote(_bash_path(bin_dir))}:"$PATH"',
        *(f"export {name}={shlex.quote(value)}" for name, value in environment.items()),
    ]
    _write_executable(wrapper, "\n".join([*exports, script]))
    private_log = tmp_path / "qualification-tooling.log"
    try:
        with private_log.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                [
                    str(_required_bash()), "--noprofile", "--norc",
                    "-e", "-o", "pipefail", _bash_path(wrapper),
                ],
                cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT, timeout=180,
            )
    finally:
        if case == "unsafe-parent":
            subprocess.run(
                ["icacls.exe", str(profile), "/remove:g", "*S-1-5-32-545"],
                capture_output=True, text=True, timeout=20, check=True,
            )
    output = private_log.read_text(encoding="utf-8")
    if case == "tampered":
        assert result.returncode != 0
        assert "native uv archive differs" in output
        assert not (owned / "tooling" / "uv.exe").exists()
        assert not (tmp_path / "github-env").exists()
    elif case in {"occupied", "unsafe-parent"}:
        assert result.returncode != 0
        assert "qualification profile root could not be admitted" in output
        assert not (tmp_path / "github-env").exists()
        assert not (owned / "uv-windows.zip").exists()
        if case == "occupied":
            assert (owned / "keep").read_text(encoding="utf-8") == "not this run"
        else:
            assert not owned.exists()
    else:
        assert result.returncode == 0, output
        assert (owned / "temp").is_dir()
        values = dict(line.split("=", 1) for line in (
            tmp_path / "github-env"
        ).read_text(encoding="utf-8").splitlines())
        app_python = Path(values["APP_PYTHON"])
        assert app_python.is_relative_to(owned / "python")
        assert values["SITEOPS_QUALIFICATION_ROOT"] == str(owned)
        assert values["SITEOPS_UV"] == str(owned / "tooling" / "uv.exe").replace("\\", "/")
        observed = subprocess.run(
            [str(app_python), "-I", "-S", "-B", "-c",
             "import sys; print(*sys.version_info[:2], sys._base_executable)"],
            capture_output=True, text=True, timeout=20,
        )
        assert observed.returncode == 0
        assert observed.stdout.strip() == f"3 11 {app_python}"


def test_the_bundle_keeps_native_installation_separate_from_bootstrap_scripts():
    """The authenticated bundle carries the shared native install helper."""
    scripts = REPO_ROOT / "scripts"
    assert not (scripts / "install-siteops.py").exists()
    # The pipeline adapter delegates to the bootstrap, not a second native installer.
    assert {path.name for path in scripts.glob("install*.py")} == {"install-siteops-consumer.py"}
    assert (scripts / "bootstrap" / BOOTSTRAP_PS1).is_file()
    assert (scripts / "bootstrap" / BOOTSTRAP_SH).is_file()
    retired = ("`install.py`", "siteops_distribution", "--store-dir", "SiteOpsInstallationResult")
    for path in (REUSABLE_PATH, GUIDE_PATH):
        text = path.read_text(encoding="utf-8")
        for name in retired:
            assert name not in text, f"{path.name}: {name}"
    assert 'siteops-install.py' in _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the verified lock")
    guide = _guide_text()
    assert "siteops-install.py" in guide
    assert not re.search(r"\bpipx\s+(?:install|upgrade-shared)\b", guide)


def test_qualification_consumes_only_the_retained_bundle_payload():
    """The verified path may depend on nothing the producer stopped shipping."""
    steps = [step.get("run", "") for step in REUSABLE["jobs"]["qualify"]["steps"]]
    qualify = "\n".join(steps)
    for member in ("bundle.json", "pylock.toml", "wheels/$WHEEL_NAME"):
        assert member in qualify, member
    referenced = {
        match.rstrip('"\\').split("/")[-1]
        for match in re.findall(r"\$extract/[^\s\"']*", qualify)
    }
    assert referenced == set(), referenced
    assert '"$helper" "$mode" "$archive" "$bundle"' in qualify


def _guide_text() -> str:
    return GUIDE_PATH.read_text(encoding="utf-8")


def _guide_anchors() -> set[str]:
    anchors = set()
    for line in _guide_text().splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            anchors.add(
                "".join(character for character in heading if character.isalnum() or character in " -")
                .replace(" ", "-")
            )
    return anchors


def test_release_guidance_links_resolve_inside_the_guide():
    anchors = _guide_anchors()
    referenced = set()
    for path in (
        CANDIDATE_PATH, RELEASE_PATH, REPO_ROOT / "docs" / "releasing.md",
        REPO_ROOT / "scripts" / "render-siteops-release.py",
    ):
        text = path.read_text(encoding="utf-8")
        for fragment in text.split("install-siteops.md#")[1:]:
            referenced.add(fragment.split('"')[0].split(")")[0].split("'")[0].strip())
    assert referenced, "Release guidance must deep-link into the installation guide."
    assert referenced <= anchors, referenced - anchors


def test_distribution_summary_uses_only_fixed_outputs_and_job_conclusions():
    summary = REUSABLE["jobs"]["summary"]
    assert summary["needs"] == ["build", "attest", "qualify"]
    assert summary["if"] == "always()"
    assert summary["permissions"] == {"contents": "read", "actions": "read"}
    report = _step(summary, "Aggregate the distribution result")
    assert report["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert report["env"]["REPORT_SUMMARY"] == "${{ inputs.report-summary }}"
    script = report["run"]
    assert (
        "actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100"
        in script
    )
    assert 'cell = expected.get(job["name"].rsplit(" / ", 1)[-1])' in script
    assert "logs/" not in script
    assert "stdout" not in script
    assert "stderr" not in script
    assert REUSABLE["env"]["SITEOPS_REDACT_OUTPUT"] == "1"
    assert "Both attested assets" not in script
    assert "siteops-bootstrap.sh" in script and "siteops-bootstrap.ps1" in script


def test_package_downloads_use_the_configured_feed_without_a_public_fallback():
    feed = REUSABLE["env"]["PACKAGE_INDEX_URL"]
    assert feed.startswith("https://")
    assert "pypi.org" not in feed
    for job, step in (
        ("build", "Install the pinned build requirements"),
        ("build", "Build the installation bundle"),
    ):
        environment = _step(REUSABLE["jobs"][job], step)["env"]
        assert environment["PIP_INDEX_URL"] == "${{ env.PACKAGE_INDEX_URL }}"
        assert environment["PIP_KEYRING_PROVIDER"] == "disabled"
        assert environment["PIP_NO_INPUT"] == "1"
        assert environment["PIP_EXTRA_INDEX_URL"] == ""
        assert environment["PIP_CONFIG_FILE"] == "/dev/null"
    online = _step(REUSABLE["jobs"]["qualify"], "Install Site Ops from the standalone wheel")
    assert online["env"]["UV_DEFAULT_INDEX"] == "${{ env.PACKAGE_INDEX_URL }}"
    assert '--default-index "$index" --no-config' in online["run"]
    assert "pypi.org" not in online["run"]


def test_the_signer_identity_confirmation_is_recorded():
    header = REUSABLE_PATH.read_text(encoding="utf-8").split("name: _Site Ops")[0]
    assert SIGNER_WORKFLOW in header
    assert "subject alternative name" in header
    assert "before any public release" in header


def test_distribution_never_reaches_azure_or_a_cluster():
    for path in (REUSABLE_PATH, CANDIDATE_PATH, RELEASE_PATH):
        text = path.read_text(encoding="utf-8")
        for command in ("az ", "kubectl", "azure/login", "AZURE_CLIENT_ID"):
            assert command not in text, path.name


def test_reusable_owns_the_only_distribution_summary():
    assert sum(
        "GITHUB_STEP_SUMMARY" in step.get("run", "")
        for step in _all_steps(REUSABLE)
    ) == 1
    assert "Publish the qualification summary" not in _step_names(REUSABLE["jobs"]["qualify"])
    assert "Record the build identity" not in _step_names(REUSABLE["jobs"]["attest"])


def test_distribution_outputs_include_artifact_url_and_qualification_matrix():
    outputs = REUSABLE[ON]["workflow_call"]["outputs"]
    assert set(outputs) == {
        "package-version",
        "build-number",
        "build-attempt",
        "archive-name",
        "archive-sha256",
        "wheel-name",
        "wheel-sha256",
        "staging-artifact-id",
        "staging-artifact-name",
        "staging-artifact-url",
        "qualification-matrix",
    }
    assert outputs["wheel-name"]["value"] == "${{ jobs.build.outputs.wheel-name }}"
    assert outputs["wheel-sha256"]["value"] == "${{ jobs.build.outputs.wheel-sha256 }}"
    attest = REUSABLE["jobs"]["attest"]
    assert attest["outputs"]["staging-artifact-url"] == "${{ steps.stage.outputs.artifact-url }}"
    assert outputs["staging-artifact-url"]["value"] == (
        "${{ jobs.attest.outputs.staging-artifact-url }}"
    )
    assert outputs["qualification-matrix"]["value"] == (
        "${{ jobs.summary.outputs.qualification-matrix }}"
    )


# --- Verification identity stays consistent ---------------------------------


def test_verification_never_falls_back_to_a_pattern_identity():
    # gh marks --cert-identity, --cert-identity-regex, --signer-repo, and
    # --signer-workflow mutually exclusive, and the exact identity is the
    # strongest of the four, so it is the only signer identity flag used.
    for path in (REUSABLE_PATH, RELEASE_PATH, GUIDE_PATH):
        text = path.read_text(encoding="utf-8")
        assert "--cert-identity-regex" not in text
        assert "--signer-workflow" not in text
        assert "--signer-repo" not in text
        assert "--cert-identity" in text


def test_the_archive_name_is_the_same_literal_everywhere():
    for path in (REUSABLE_PATH, RELEASE_PATH):
        document = _document(path)
        assert document["env"]["ARCHIVE_NAME"] == ARCHIVE_NAME
        assert document["env"]["ATTESTATION_SUFFIX"] == ATTESTATION_SUFFIX
        assert document["env"]["OIDC_ISSUER"] == OIDC_ISSUER
        assert document["env"]["PREDICATE_TYPE"] == PREDICATE_TYPE


# --- Extracted snippets, exercised against fakes -----------------------------


def _qualification_jobs() -> list[dict]:
    return [
        {
            "name": f"Qualify bundle ({platform}, Python {python})",
            "status": "completed",
            "conclusion": "success",
        }
        for python in QUALIFIED_PYTHONS
        for platform in QUALIFIED_PLATFORMS
    ]


def _expected_matrix(state: str) -> list[dict[str, str]]:
    """Return the qualification matrix the summary encodes when every cell agrees."""
    return [{"python": python, "linux": state, "windows": state} for python in QUALIFIED_PYTHONS]


def _run_distribution_summary(
    tmp_path: Path,
    jobs: list[dict],
    *,
    report_summary: bool = True,
    build_result: str = "success",
    attest_result: str = "success",
    qualify_result: str = "success",
    api_exit: str = "0",
):
    _, log = _fake_tools(tmp_path)
    response = tmp_path / "jobs.json"
    response.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    output = tmp_path / "github-output.txt"
    summary = tmp_path / "github-summary.md"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    artifact_available = attest_result == "success"
    result = _run_script(
        _script(REUSABLE["jobs"]["summary"], "Aggregate the distribution result"),
        tmp_path,
        {
            "FAKE_GH_LOG": _bash_path(log),
            "FAKE_GH_API_RESPONSE": _bash_path(response),
            "FAKE_GH_API_EXIT": api_exit,
            "FAKE_PYTHON": _python_executable_path(),
            "GITHUB_REPOSITORY": "example/publisher",
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_OUTPUT": _bash_path(output),
            "GITHUB_STEP_SUMMARY": _bash_path(summary),
            "REPORT_SUMMARY": str(report_summary).lower(),
            "BUILD_RESULT": build_result,
            "ATTEST_RESULT": attest_result,
            "QUALIFY_RESULT": qualify_result,
            "PACKAGE_VERSION": "1.0.0b1+build.42.2.gcccccccccccc",
            "ARCHIVE_NAME_VALUE": ARCHIVE_NAME,
            "ARCHIVE_SHA256": "d" * 64,
            "WHEEL_NAME_VALUE": WHEEL_NAME,
            "WHEEL_SHA256": "e" * 64,
            "STAGING_ARTIFACT_ID": "987" if artifact_available else "",
            "STAGING_ARTIFACT_NAME": (
                "siteops-install-staging-42-2" if artifact_available else ""
            ),
            "STAGING_ARTIFACT_URL": (
                "https://github.com/example/publisher/actions/runs/42/artifacts/987"
                if artifact_available
                else ""
            ),
            "SOURCE_SHA": "c" * 40,
            "SOURCE_REF": "refs/heads/main",
            "RUNNER_TEMP": _bash_path(runner_temp),
        },
    )
    encoded = None
    if output.exists():
        encoded = output.read_text(encoding="utf-8").split("=", 1)[1].strip()
    return result, encoded, summary, log


def test_distribution_summary_reports_the_complete_success_matrix(tmp_path):
    result, encoded, summary_path, log = _run_distribution_summary(
        tmp_path, _qualification_jobs()
    )

    expected = _expected_matrix("passed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert encoded == json.dumps(expected, separators=(",", ":"))
    assert json.loads(encoded) == expected
    assert _invocations(log) == [
        [
            "api",
            "repos/example/publisher/actions/runs/42/attempts/2/jobs?per_page=100",
        ]
    ]

    summary = summary_path.read_text(encoding="utf-8")
    assert summary.count("## Site Ops installer check") == 1
    assert "Version: <code>1.0.0b1+build.42.2.gcccccccccccc</code>" in summary
    assert "four attested assets" in summary
    assert "siteops-bootstrap.sh" in summary and "siteops-bootstrap.ps1" in summary
    assert (
        "[Download the attested installation assets]"
        "(https://github.com/example/publisher/actions/runs/42/artifacts/987)"
        in summary
    )
    for python in QUALIFIED_PYTHONS:
        assert f"| {python} | passed | passed |" in summary
    for value in (
        "Source SHA",
        "Source ref",
        "Archive SHA-256",
        "Wheel SHA-256",
        WHEEL_NAME,
        "Run attempt",
        "Artifact ID",
        "Artifact name",
    ):
        assert value in summary


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("failed", "failed"),
        ("missing", "not-run"),
        ("cancelled", "cancelled"),
        ("ambiguous", "unknown"),
        ("partial", "unknown"),
    ],
)
def test_distribution_summary_reports_nonpassing_cells_honestly(tmp_path, case, expected):
    jobs = _qualification_jobs()
    target = "Qualify bundle (ubuntu-24.04, Python 3.12)"
    selected = next(job for job in jobs if job["name"] == target)
    if case == "failed":
        selected["conclusion"] = "failure"
    elif case == "missing":
        jobs.remove(selected)
    elif case == "cancelled":
        selected["conclusion"] = "cancelled"
    elif case == "ambiguous":
        jobs.append(
            {
                "name": "Qualify bundle (ubuntu-24.04, Python 3.12)",
                "status": "completed",
                "conclusion": "success",
            }
        )
    else:
        selected["status"] = "in_progress"
        selected["conclusion"] = None

    result, encoded, summary_path, _ = _run_distribution_summary(
        tmp_path, jobs, qualify_result="failure"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    matrix = json.loads(encoded)
    assert matrix[2] == {"python": "3.12", "linux": expected, "windows": "passed"}
    assert len(matrix) == len(QUALIFIED_PYTHONS)
    summary = summary_path.read_text(encoding="utf-8")
    assert f"| 3.12 | {expected} | passed |" in summary
    assert "One or more qualification cells did not pass." in summary


@pytest.mark.parametrize("prefix", ["Rehearse distribution", "Rehearse release / Candidate installation"])
def test_distribution_summary_accepts_real_reusable_workflow_job_names(tmp_path, prefix):
    jobs = _qualification_jobs()
    for job in jobs:
        job["name"] = prefix + " / " + job["name"]
    result, encoded, _, _ = _run_distribution_summary(tmp_path, jobs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(row["linux"] == row["windows"] == "passed" for row in json.loads(encoded))


def test_distribution_summary_refuses_ambiguous_success_claim(tmp_path):
    jobs = _qualification_jobs()
    jobs.append(
        {
            "name": "Distribute / Qualify bundle (ubuntu-24.04, Python 3.10)",
            "status": "completed",
            "conclusion": "failure",
        }
    )

    result, encoded, _, _ = _run_distribution_summary(tmp_path, jobs)

    assert result.returncode != 0
    assert json.loads(encoded)[0] == {
        "python": "3.10",
        "linux": "unknown",
        "windows": "passed",
    }


@pytest.mark.parametrize(
    ("build_result", "attest_result", "guidance"),
    [
        ("failure", "skipped", "Review the Build bundle job"),
        ("success", "failure", "Review the Attest bundle job"),
    ],
)
def test_distribution_summary_keeps_earlier_failure_visible(
    tmp_path, build_result, attest_result, guidance
):
    result, encoded, summary_path, _ = _run_distribution_summary(
        tmp_path,
        [],
        build_result=build_result,
        attest_result=attest_result,
        qualify_result="skipped",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == _expected_matrix("not-run")
    summary = summary_path.read_text(encoding="utf-8")
    assert guidance in summary
    assert "not available for download" in summary
    assert REUSABLE["jobs"]["summary"]["if"] == "always()"
    assert "continue-on-error" not in REUSABLE["jobs"]["summary"]


def test_report_summary_false_suppresses_only_presentation(tmp_path):
    summary_path = tmp_path / "github-summary.md"
    summary_path.write_text("existing summary\n", encoding="utf-8")
    result, encoded, returned_summary, log = _run_distribution_summary(
        tmp_path, _qualification_jobs(), report_summary=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == _expected_matrix("passed")
    assert returned_summary.read_text(encoding="utf-8") == "existing summary\n"
    assert _invocations(log)
    assert REUSABLE["jobs"]["summary"]["needs"] == ["build", "attest", "qualify"]
    assert "if" not in REUSABLE["jobs"]["qualify"]


def test_distribution_summary_fails_without_job_conclusions_and_publishes_nothing(tmp_path):
    # A returned body cannot stand in for a failed read of the job conclusions.
    result, encoded, summary, _ = _run_distribution_summary(
        tmp_path, _qualification_jobs(), api_exit="1"
    )

    assert result.returncode != 0
    assert json.loads(encoded) == _expected_matrix("unknown")
    assert not summary.exists()


@pytest.mark.parametrize(
    ("expected", "actual", "code"),
    [
        ("a" * 40, "a" * 40, 0),
        ("a" * 40, "b" * 40, 1),
        ("A" * 40, "A" * 40, 1),
        ("abc", "abc", 1),
        ("", "a" * 40, 1),
    ],
)
def test_source_assertion_accepts_only_the_matching_event_commit(tmp_path, expected, actual, code):
    script = _script(REUSABLE["jobs"]["build"], "Assert the expected source commit")
    result = _run_script(
        script,
        tmp_path,
        {"EXPECTED_SOURCE_SHA": expected, "SOURCE_SHA": actual, "VERSION_MODE": "build"},
    )
    assert result.returncode == code, result.stdout + result.stderr


def _qualification_exports(tmp_path: Path, log: Path) -> dict[str, str]:
    evidence = tmp_path / "verified-observations.json"
    evidence.write_text(json.dumps([verified_observation(
        "example/publisher", "c" * 40, "refs/heads/main",
        SIGNER_WORKFLOW, ".github/workflows/ci.yaml",
    )]), encoding="utf-8")
    return {
        "FAKE_GH_LOG": _bash_path(log),
        "FAKE_GH_VERIFICATION": _bash_path(evidence),
        "FAKE_PYTHON": Path(sys.executable).as_posix(),
        "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX,
        "WHEEL_NAME": WHEEL_NAME,
        "BOOTSTRAP_PS1": BOOTSTRAP_PS1,
        "BOOTSTRAP_SH": BOOTSTRAP_SH,
        "SOURCE_REPOSITORY": "example/publisher",
        "SOURCE_SHA": "c" * 40,
        "SOURCE_REF": "refs/heads/main",
        "SIGNER_DIGEST": "c" * 40,
        "BUILDER_IDENTITY": "https://github.com/example/publisher/.github/workflows/ci.yaml@refs/heads/main",
        "SIGNER_IDENTITY": (
            f"https://github.com/example/publisher/{SIGNER_WORKFLOW}@refs/heads/main"
        ),
        "OIDC_ISSUER": OIDC_ISSUER,
        "PREDICATE_TYPE": PREDICATE_TYPE,
        "VERSION_MODE": "build",
    }


def _staged_download(tmp_path: Path, *, missing: str | None = None) -> Path:
    download = tmp_path / "temp" / "siteops-download"
    download.mkdir(parents=True)
    staged = {
        ARCHIVE_NAME: b"archive bytes",
        ARCHIVE_NAME + ATTESTATION_SUFFIX: b"{}\n",
        WHEEL_NAME: b"wheel bytes",
        WHEEL_NAME + ATTESTATION_SUFFIX: b"{}\n",
        BOOTSTRAP_PS1: b"PowerShell script bytes",
        BOOTSTRAP_PS1 + ATTESTATION_SUFFIX: b"{}\n",
        BOOTSTRAP_SH: b"Bash script bytes",
        BOOTSTRAP_SH + ATTESTATION_SUFFIX: b"{}\n",
    }
    for name, content in staged.items():
        if name != missing:
            (download / name).write_bytes(content)
    return download


def _expected_verification(asset: str) -> list[str]:
    return [
        "attestation",
        "verify",
        asset,
        "--bundle",
        asset + ATTESTATION_SUFFIX,
        "--repo",
        "example/publisher",
        "--cert-identity",
        f"https://github.com/example/publisher/{SIGNER_WORKFLOW}@refs/heads/main",
        "--signer-digest",
        "c" * 40,
        "--source-digest",
        "c" * 40,
        "--source-ref",
        "refs/heads/main",
        "--cert-oidc-issuer",
        OIDC_ISSUER,
        "--predicate-type",
        PREDICATE_TYPE,
        "--hostname", "github.com", "--digest-alg", "sha256", "--format", "json",
    ]


def test_qualification_verification_passes_the_exact_policy_to_the_runner_cli(tmp_path):
    _, log = _fake_tools(tmp_path)
    download = _staged_download(tmp_path)
    script = _script(REUSABLE["jobs"]["qualify"], "Verify installation assets before use")
    result = _run_script(script, tmp_path, _qualification_exports(tmp_path, log))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _invocations(log) == [
        _expected_verification(_bash_path(download / ARCHIVE_NAME)),
        _expected_verification(_bash_path(download / WHEEL_NAME)),
        _expected_verification(_bash_path(download / BOOTSTRAP_PS1)),
        _expected_verification(_bash_path(download / BOOTSTRAP_SH)),
    ]


@pytest.mark.parametrize(
    ("failing", "attempted"), [("archive", 1), ("wheel", 2), ("bootstrap", 3)],
)
def test_qualification_stops_when_verification_fails(tmp_path, failing, attempted):
    _, log = _fake_tools(tmp_path)
    _staged_download(tmp_path)
    # The wheel case fails only the second subject, so a partial verification
    # cannot pass, and the attempt count shows a failed subject stops the step.
    variable = {
        "archive": "FAKE_GH_ATTESTATION_EXIT",
        "wheel": "FAKE_GH_WHEEL_EXIT",
        "bootstrap": "FAKE_GH_BOOTSTRAP_EXIT",
    }[failing]
    exports = {**_qualification_exports(tmp_path, log), variable: "1"}
    script = _script(REUSABLE["jobs"]["qualify"], "Verify installation assets before use")
    result = _run_script(script, tmp_path, exports)
    assert result.returncode != 0
    assert len(_invocations(log)) == attempted


@pytest.mark.parametrize("field", [
    "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
    "sourceRepositoryRef", "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
])
def test_native_qualification_checks_each_observed_claim(tmp_path, field):
    _, log = _fake_tools(tmp_path)
    _staged_download(tmp_path)
    exports = _qualification_exports(tmp_path, log)
    path = tmp_path / "verified-observations.json"
    observations = json.loads(path.read_bytes())
    changed = copy.deepcopy(observations[0])
    changed["verificationResult"]["signature"]["certificate"][field] = "PRIVATE_WRONG"
    observations.append(changed)
    path.write_text(json.dumps(observations))
    result = _run_script(
        _script(REUSABLE["jobs"]["qualify"], "Verify installation assets before use"), tmp_path, exports,
    )
    assert result.returncode != 0
    assert "certificate does not match" in result.stdout + result.stderr
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr
    assert len(_invocations(log)) == 1


@pytest.mark.parametrize(
    "missing",
    [ARCHIVE_NAME + ATTESTATION_SUFFIX, WHEEL_NAME, WHEEL_NAME + ATTESTATION_SUFFIX,
     BOOTSTRAP_PS1, BOOTSTRAP_PS1 + ATTESTATION_SUFFIX, BOOTSTRAP_SH,
     BOOTSTRAP_SH + ATTESTATION_SUFFIX],
)
def test_qualification_stops_when_an_asset_or_proof_is_missing(tmp_path, missing):
    _, log = _fake_tools(tmp_path)
    _staged_download(tmp_path, missing=missing)
    script = _script(REUSABLE["jobs"]["qualify"], "Verify installation assets before use")
    result = _run_script(script, tmp_path, _qualification_exports(tmp_path, log))
    assert result.returncode != 0
    assert _invocations(log) == []


def test_extraction_targets_an_owned_empty_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "python-invocations.log"
    _write_executable(
        bin_dir / "fake-python",
        """#!/usr/bin/env bash
for argument in "$@"; do printf '%s\\n' "$argument"; done >> "$FAKE_PYTHON_LOG"
mkdir -p "$5"
printf 'extracted\\n' > "$5/bundle.json"
""",
    )
    download = _staged_download(tmp_path)
    stale = tmp_path / "temp" / "siteops-verified"
    stale.mkdir()
    (stale / "operator-file").write_text("stale", encoding="utf-8")

    script = _script(REUSABLE["jobs"]["qualify"], "Extract the verified bundle")
    result = _run_script(
        script,
        tmp_path,
        {
            "FAKE_PYTHON_LOG": _bash_path(log),
            "PYTHON": _bash_path(bin_dir / "fake-python"),
            "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
            "ARCHIVE_NAME": ARCHIVE_NAME,
        },
    )

    assert result.returncode != 0
    assert "extraction directory must be fresh" in result.stdout
    assert (stale / "operator-file").read_text(encoding="utf-8") == "stale"
    assert not log.exists()

    fresh = tmp_path / "fresh"
    (fresh / "siteops-download").mkdir(parents=True)
    shutil.copy2(download / ARCHIVE_NAME, fresh / "siteops-download" / ARCHIVE_NAME)
    result = _run_script(
        script,
        tmp_path,
        {
            "FAKE_PYTHON_LOG": _bash_path(log),
            "PYTHON": _bash_path(bin_dir / "fake-python"),
            "RUNNER_TEMP": _bash_path(fresh),
            "ARCHIVE_NAME": ARCHIVE_NAME,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "zipfile",
        "-e",
        _bash_path(fresh / "siteops-download" / ARCHIVE_NAME),
        _bash_path(fresh / "siteops-verified"),
    ]
    assert (fresh / "siteops-verified" / "bundle.json").is_file()


def _bundle_document(**overrides) -> dict:
    document = {
        "apiVersion": "siteops.install/v1",
        "kind": "SiteOpsBundle",
        "package": {
            "name": "siteops",
            "version": "1.0.0b1+build.42.1.gcccccccccccc",
            "baseVersion": "1.0.0b1",
            "wheel": "wheels/" + WHEEL_NAME,
        },
        "source": {
            "repository": "example/publisher",
            "commit": "c" * 40,
            "ref": "refs/heads/main",
        },
        "build": {"number": 42, "attempt": 1},
        "targets": [],
        "files": [],
    }
    document.update(overrides)
    return document


def _verified_assets(
    tmp_path: Path,
    document: dict,
    *,
    wheel: bytes = b"one application build",
    standalone: bytes | None = None,
    lock: bool = True,
) -> None:
    verified = tmp_path / "temp" / "siteops-verified"
    (verified / "wheels").mkdir(parents=True)
    (verified / "bundle.json").write_text(json.dumps(document), encoding="utf-8")
    (verified / "wheels" / WHEEL_NAME).write_bytes(wheel)
    if lock:
        (verified / "pylock.toml").write_text('lock-version = "1.0"\n', encoding="utf-8")
    download = tmp_path / "temp" / "siteops-download"
    download.mkdir(parents=True, exist_ok=True)
    (download / WHEEL_NAME).write_bytes(wheel if standalone is None else standalone)


def _consistency_exports(tmp_path: Path, *, mode: str = "build") -> dict[str, str]:
    return {
        "PYTHON": _python_executable_path(),
        "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        "SOURCE_REPOSITORY": "example/publisher",
        "SOURCE_SHA": "c" * 40,
        "SOURCE_REF": "refs/heads/main",
        "BUILD_NUMBER": "42",
        "BUILD_ATTEMPT": "1",
        "VERSION_MODE": mode,
        "WHEEL_NAME": WHEEL_NAME,
        "PACKAGE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc",
    }


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({}, 0),
        (
            {
                "source": {
                    "repository": "example/publisher",
                    "commit": "d" * 40,
                    "ref": "refs/heads/main",
                }
            },
            1,
        ),
        (
            {
                "source": {
                    "repository": "attacker/fork",
                    "commit": "c" * 40,
                    "ref": "refs/heads/main",
                }
            },
            1,
        ),
        ({"build": {"number": 43, "attempt": 1}}, 1),
        (
            {
                "package": {
                    "name": "siteops",
                    "version": "1.0.0b1",
                    "baseVersion": "1.0.0b1",
                    "wheel": "wheels/" + WHEEL_NAME,
                }
            },
            1,
        ),
        (
            {
                "package": {
                    "name": "siteops",
                    "version": "1.0.0b1+build.42.1.gcccccccccccc",
                    "baseVersion": "1.0.0b1",
                    "wheel": "wheels/siteops-other-py3-none-any.whl",
                }
            },
            1,
        ),
        ({"kind": "OtherBundle"}, 1),
    ],
)
def test_bundle_consistency_check_matches_this_build(tmp_path, overrides, code):
    _verified_assets(tmp_path, _bundle_document(**overrides))
    script = _script(REUSABLE["jobs"]["qualify"], "Confirm the bundle describes this build")
    result = _run_script(script, tmp_path, _consistency_exports(tmp_path))
    assert result.returncode == code, result.stdout + result.stderr


@pytest.mark.parametrize("difference", ["standalone", "lock"])
def test_bundle_consistency_rejects_assets_that_disagree(tmp_path, difference):
    _verified_assets(
        tmp_path,
        _bundle_document(),
        standalone=b"a different application build" if difference == "standalone" else None,
        lock=difference != "lock",
    )
    script = _script(REUSABLE["jobs"]["qualify"], "Confirm the bundle describes this build")
    result = _run_script(script, tmp_path, _consistency_exports(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr


def test_bundle_consistency_accepts_an_independent_source_version(tmp_path):
    document = _bundle_document()
    document["package"]["version"] = document["package"]["baseVersion"] = "1.1.0"
    _verified_assets(tmp_path, document)
    exports = _consistency_exports(tmp_path, mode="source")
    exports["PACKAGE_VERSION"] = "1.1.0"
    result = _run_script(
        _script(REUSABLE["jobs"]["qualify"], "Confirm the bundle describes this build"),
        tmp_path,
        exports,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _python_executable_path() -> str:
    return _bash_path(Path(sys.executable))


def _qualification_root(temp: Path) -> Path:
    if os.name == "nt":
        return temp.parent / "p" / "q"
    return temp / "siteops-qualification"


def _native_exports(temp: Path, version: str, app_python: Path, uv: Path) -> dict[str, str]:
    owned = _qualification_root(temp)
    return {
        "RUNNER_TEMP": str(temp),
        "RUNNER_OS": "Windows" if os.name == "nt" else "Linux",
        "SITEOPS_QUALIFICATION_ROOT": str(owned),
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "PYTHON": str(Path(sys.executable)),
        "APP_PYTHON": str(app_python),
        "SITEOPS_UV": str(uv),
        "UV_TOOL_DIR": str(owned / "verified-tools"),
        "UV_TOOL_BIN_DIR": str(owned / "verified-bin"),
        "UV_PYTHON_INSTALL_DIR": str(owned / "python"),
        "UV_CACHE_DIR": str(owned / "cache"),
        "SOURCE_REPOSITORY": "example/publisher",
        "SOURCE_SHA": "a" * 40,
        "SOURCE_REF": "refs/heads/main",
        "MATRIX_PYTHON": "3.11",
        "PACKAGE_VERSION": version,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        **NETWORK_BLOCK,
    }


@pytest.fixture(scope="session")
def native_runtime(tmp_path_factory):
    owned = tmp_path_factory.mktemp("native-uv-runtime")
    return managed_python(owned)


def _prepared_runner_area(
    tmp_path: Path, bundle: Path, manifest, app_python: Path,
) -> tuple[Path, Path, Path]:
    """Copy native prerequisites into the same private directories as the workflow."""
    temp = tmp_path / "runner temporary"
    temp.mkdir()
    owned = _qualification_root(temp)
    (owned / "logs").mkdir(parents=True)
    (owned / "home").mkdir()
    for name in ("verified-tools", "verified-bin", "online-tools", "online-bin", "cache"):
        (owned / name).mkdir()
    if os.name == "nt":
        (owned / "temp").mkdir()
    shutil.copytree(bundle, temp / "siteops-verified")
    publish_assets(bundle, manifest, temp / "siteops-download")
    concrete = app_python.parent if os.name == "nt" else app_python.parent.parent
    copied = owned / "python" / concrete.name
    shutil.copytree(concrete, copied, symlinks=True)
    app_python = copied / ("python.exe" if os.name == "nt" else "bin/python3.11")
    tooling = owned / "tooling"
    tooling.mkdir()
    selected = native_uv()
    uv = tooling / selected.name
    shutil.copy2(selected, uv)
    return temp, app_python, uv


def _native_step_run(tmp_path: Path, bundle_factory, native_runtime: Path, number: int):
    """Return one built bundle, its prepared runner area, and the step's inputs."""
    root, manifest = bundle_factory(number)
    temp, app_python, uv = _prepared_runner_area(tmp_path, root, manifest, native_runtime)
    exports = _native_exports(temp, manifest.version, app_python, uv)
    exports["WHEEL_NAME"] = Path(manifest.application_wheel).name
    return root, manifest, temp, exports


@native_only
def test_native_verified_installation_step_runs_the_published_recipe(
    tmp_path,
    bundle_factory,
    native_runtime,
):
    _, _, temp, exports = _native_step_run(tmp_path, bundle_factory, native_runtime, 81)
    assert Path(exports["APP_PYTHON"]).resolve() != Path(sys._base_executable).resolve()
    assert Path(exports["APP_PYTHON"]).is_relative_to(_qualification_root(temp) / "python")

    result = _run_script(
        _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the verified lock"),
        tmp_path,
        exports,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    command = _qualification_root(temp) / "verified-bin" / (
        "siteops.exe" if os.name == "nt" else "siteops"
    )
    assert not command.exists()
    assert (_qualification_root(temp) / "tampered").exists()
    if os.name == "nt":
        assert (_qualification_root(temp) / "verified-bundle" / "bundle.json").is_file()
    logs = _qualification_root(temp) / "logs"
    assert {"install.log", "repeat.log", "repair.log", "tampered.log", "uninstall.log",
            "install.json", "repeat.json", "repair.json"} <= {
        path.name for path in logs.iterdir()
    }
    # Raw tool output stays in the private run files.
    assert "changed after the producer" not in result.stdout


@native_only
def test_native_verified_installation_step_fails_on_a_changed_payload(
    tmp_path,
    bundle_factory,
    native_runtime,
):
    _, manifest, temp, exports = _native_step_run(tmp_path, bundle_factory, native_runtime, 82)
    wheel = temp / "siteops-verified" / manifest.application_wheel
    with wheel.open("ab") as stream:
        stream.write(b"changed before the step installed anything")

    result = _run_script(
        _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the verified lock"),
        tmp_path,
        exports,
    )

    assert result.returncode != 0
    assert "authenticated verified bundle could not be installed" in result.stdout + result.stderr
    assert not (_qualification_root(temp) / "verified-tools" / "siteops").exists()


@native_only
@pytest.mark.parametrize("command_exit", [0, 43])
def test_native_online_installation_step_installs_the_standalone_wheel(
    tmp_path,
    bundle_factory,
    native_runtime,
    command_exit,
):
    root, manifest, temp, exports = _native_step_run(
        tmp_path, bundle_factory, native_runtime, 83,
    )
    download = temp / "siteops-download"
    assert (download / exports["WHEEL_NAME"]).is_file()
    index = tmp_path / "index"
    dependency = index / "siteops-fixture-dependency"
    dependency.mkdir(parents=True)
    wheel = next((root / "wheels").glob("siteops_fixture_dependency-*.whl"))
    shutil.copy2(wheel, dependency / wheel.name)
    (dependency / "index.html").write_text(
        f'<a href="{wheel.name}">{wheel.name}</a>\n', encoding="utf-8",
    )
    exports["UV_DEFAULT_INDEX"] = index.as_uri()

    script = _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the standalone wheel")
    if command_exit:
        script = script.replace(
            'if ! observed="$("$command_bin/$command_name" --version',
            'command_name=absent\nif ! observed="$("$command_bin/$command_name" --version',
        )
    result = _run_script(
        script,
        tmp_path,
        exports,
    )

    if command_exit:
        assert result.returncode != 0
        assert "online command could not be executed" in result.stdout
        assert "No such file" not in result.stdout + result.stderr
        assert (
            _qualification_root(temp) / "logs" / "online-version.log"
        ).read_text()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    command = _qualification_root(temp) / "online-bin" / (
        "siteops.exe" if os.name == "nt" else "siteops"
    )
    assert not command.exists()


@native_only
@pytest.mark.parametrize("fault", ["version", "execution"])
def test_native_installation_steps_reject_an_unexpected_exposed_version(
    tmp_path,
    bundle_factory,
    native_runtime,
    fault,
):
    _, manifest, temp, exports = _native_step_run(tmp_path, bundle_factory, native_runtime, 84)
    if fault == "version":
        exports["PACKAGE_VERSION"] = manifest.version + ".unexpected"

    script = _script(REUSABLE["jobs"]["qualify"], "Install Site Ops from the verified lock")
    if fault == "execution":
        script = script.replace(
            'expect_working_command "installation"',
            'command_name=absent\nexpect_working_command "installation"',
        )
    result = _run_script(
        script,
        tmp_path,
        exports,
    )

    assert result.returncode != 0
    expected = {
        "version": "reported an unexpected version",
        "execution": "could not be executed",
    }[fault]
    assert expected in result.stdout + result.stderr
    if fault == "execution":
        assert (
            _qualification_root(temp) / "logs" / "version-installation.log"
        ).read_text()




# --- The published asset set, exercised as shell -----------------------------


def _producer_shim(tmp_path: Path, archive: Path, wheel: Path | None, extra: str | None = None):
    """Stand in for the producer so the workflow's own validation can run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(
        bin_dir / "python",
        """#!/usr/bin/env bash
if [[ "$1" == "scripts/build-siteops-bundle.py" ]]; then
  output=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--output" ]]; then
      output="$2"
    fi
    shift
  done
  staging="$(dirname "$output")"
  cp "$FAKE_ARCHIVE" "$output"
  if [[ -n "${FAKE_WHEEL:-}" ]]; then
    cp "$FAKE_WHEEL" "$staging/$(basename "$FAKE_WHEEL")"
  fi
  if [[ -n "${FAKE_EXTRA:-}" ]]; then
    printf 'unexpected build output\\n' > "$staging/$FAKE_EXTRA"
  fi
  exit 0
fi
exec "$REAL_PYTHON" "$@"
""",
    )
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
[[ "$1" == show && "$2" == "$SOURCE_SHA:scripts/bootstrap/$BOOTSTRAP_PS1" ]] &&
  { cat "$FAKE_BOOTSTRAP_DIR/$BOOTSTRAP_PS1"; exit 0; }
[[ "$1" == show && "$2" == "$SOURCE_SHA:scripts/bootstrap/$BOOTSTRAP_SH" ]] &&
  { cat "$FAKE_BOOTSTRAP_DIR/$BOOTSTRAP_SH"; exit 0; }
exit 1
""",
    )
    exports = {
        "REAL_PYTHON": _python_executable_path(),
        "FAKE_ARCHIVE": _bash_path(archive),
        "FAKE_WHEEL": _bash_path(wheel) if wheel else "",
        "FAKE_EXTRA": extra or "",
        "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "BOOTSTRAP_PS1": BOOTSTRAP_PS1,
        "BOOTSTRAP_SH": BOOTSTRAP_SH,
        "FAKE_BOOTSTRAP_DIR": _bash_path(REPO_ROOT / "scripts" / "bootstrap"),
        "SOURCE_REPOSITORY": "example/publisher",
        "SOURCE_REF": "refs/heads/main",
        "SOURCE_SHA": "c" * 40,
        "BUILD_NUMBER": "42",
        "BUILD_ATTEMPT": "1",
        "VERSION_MODE": "build",
        "GITHUB_OUTPUT": _bash_path(tmp_path / "github-output.txt"),
    }
    (tmp_path / "temp").mkdir(exist_ok=True)
    return exports


def _step_outputs(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "github-output.txt"
    if not path.exists():
        return {}
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def test_build_step_publishes_and_records_exact_installation_assets(tmp_path, bundle_factory):
    root, manifest = bundle_factory(91)
    archive, wheel = publish_assets(root, manifest, tmp_path / "published")

    result = _run_script(
        _script(REUSABLE["jobs"]["build"], "Build the installation bundle"),
        tmp_path,
        _producer_shim(tmp_path, archive, wheel),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    outputs = _step_outputs(tmp_path)
    assert outputs["archive-name"] == ARCHIVE_NAME
    assert outputs["wheel-name"] == wheel.name
    assert outputs["package-version"] == manifest.version
    assert outputs["archive-sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert outputs["wheel-sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    for name, key in ((BOOTSTRAP_PS1, "bootstrap-ps1-sha256"), (BOOTSTRAP_SH, "bootstrap-sh-sha256")):
        assert outputs[key] == hashlib.sha256(
            (REPO_ROOT / "scripts" / "bootstrap" / name).read_bytes()
        ).hexdigest()
    assert outputs["staging-path"].endswith("/siteops-build")
    staging = Path(tmp_path / "temp" / "siteops-build")
    assert sorted(path.name for path in staging.iterdir()) == sorted(
        [ARCHIVE_NAME, wheel.name, BOOTSTRAP_PS1, BOOTSTRAP_SH]
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing-wheel", "content other than its declared installation assets"),
        ("extra-file", "content other than its declared installation assets"),
        ("unsupported-name", "content other than its declared installation assets"),
        ("absent-member", "does not contain the standalone wheel"),
        ("changed-bytes", "byte for byte"),
    ],
)
def test_build_step_refuses_an_inconsistent_asset_pair(tmp_path, bundle_factory, case, message):
    root, manifest = bundle_factory(92)
    archive, wheel = publish_assets(root, manifest, tmp_path / "published")
    extra = None
    if case == "missing-wheel":
        wheel = None
    elif case == "extra-file":
        extra = "siteops-notes.txt"
    elif case == "unsupported-name":
        renamed = wheel.with_name("siteops-installer.whl")
        wheel = wheel.rename(renamed)
    elif case == "absent-member":
        renamed = wheel.with_name("siteops-9.9.9-py3-none-any.whl")
        wheel = wheel.rename(renamed)
    else:
        with wheel.open("ab") as stream:
            stream.write(b"changed after the archive was written")

    result = _run_script(
        _script(REUSABLE["jobs"]["build"], "Build the installation bundle"),
        tmp_path,
        _producer_shim(tmp_path, archive, wheel, extra),
    )

    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert "wheel-sha256" not in _step_outputs(tmp_path)


def _staged_subject(tmp_path: Path, wheel_name: str = WHEEL_NAME) -> dict[str, str]:
    subject = tmp_path / "temp" / "siteops-subject"
    subject.mkdir(parents=True)
    (subject / ARCHIVE_NAME).write_bytes(b"archive bytes")
    (subject / wheel_name).write_bytes(b"wheel bytes")
    for script in (BOOTSTRAP_PS1, BOOTSTRAP_SH):
        (subject / script).write_bytes(script.encode("ascii"))
    for name in ("archive-proof", "wheel-proof", "ps1-proof", "sh-proof"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    return {
        "RUNNER_TEMP": _bash_path(tmp_path / "temp"),
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX,
        "WHEEL_NAME": wheel_name,
        "BOOTSTRAP_PS1": BOOTSTRAP_PS1,
        "BOOTSTRAP_SH": BOOTSTRAP_SH,
        "EXPECTED_ARCHIVE_SHA256": hashlib.sha256(b"archive bytes").hexdigest(),
        "EXPECTED_WHEEL_SHA256": hashlib.sha256(b"wheel bytes").hexdigest(),
        "EXPECTED_BOOTSTRAP_PS1_SHA256": hashlib.sha256(BOOTSTRAP_PS1.encode("ascii")).hexdigest(),
        "EXPECTED_BOOTSTRAP_SH_SHA256": hashlib.sha256(BOOTSTRAP_SH.encode("ascii")).hexdigest(),
        "ARCHIVE_BUNDLE_PATH": _bash_path(tmp_path / "archive-proof"),
        "WHEEL_BUNDLE_PATH": _bash_path(tmp_path / "wheel-proof"),
        "PS1_BUNDLE_PATH": _bash_path(tmp_path / "ps1-proof"),
        "SH_BUNDLE_PATH": _bash_path(tmp_path / "sh-proof"),
    }


def test_signing_stages_exactly_the_eight_published_files(tmp_path):
    exports = _staged_subject(tmp_path)
    attest = REUSABLE["jobs"]["attest"]

    confirmed = _run_script(_script(attest, "Confirm the staged payload"), tmp_path, exports)
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr

    staged = _run_script(_script(attest, "Stage the attested bytes"), tmp_path, exports)
    assert staged.returncode == 0, staged.stdout + staged.stderr
    assert sorted(path.name for path in (tmp_path / "temp" / "siteops-staging").iterdir()) == (
        sorted(
            [
                ARCHIVE_NAME,
                ARCHIVE_NAME + ATTESTATION_SUFFIX,
                WHEEL_NAME,
                WHEEL_NAME + ATTESTATION_SUFFIX,
                BOOTSTRAP_PS1,
                BOOTSTRAP_PS1 + ATTESTATION_SUFFIX,
                BOOTSTRAP_SH,
                BOOTSTRAP_SH + ATTESTATION_SUFFIX,
            ]
        )
    )


@pytest.mark.parametrize(
    "case", ["extra-subject", "changed-archive", "changed-wheel", "missing-wheel",
             "changed-script", "missing-script"]
)
def test_signing_refuses_a_payload_that_is_not_the_build_output(tmp_path, case):
    exports = _staged_subject(tmp_path)
    subject = tmp_path / "temp" / "siteops-subject"
    if case == "extra-subject":
        (subject / "unexpected.txt").write_text("extra", encoding="utf-8")
    elif case == "changed-archive":
        (subject / ARCHIVE_NAME).write_bytes(b"replaced archive bytes")
    elif case == "changed-wheel":
        (subject / WHEEL_NAME).write_bytes(b"replaced wheel bytes")
    elif case == "changed-script":
        (subject / BOOTSTRAP_SH).write_bytes(b"replaced script bytes")
    elif case == "missing-script":
        (subject / BOOTSTRAP_PS1).unlink()
    else:
        (subject / WHEEL_NAME).unlink()

    result = _run_script(
        _script(REUSABLE["jobs"]["attest"], "Confirm the staged payload"), tmp_path, exports
    )
    assert result.returncode != 0


@pytest.mark.parametrize("empty", ["archive-proof", "wheel-proof", "ps1-proof", "sh-proof"])
def test_signing_refuses_an_empty_detached_proof(tmp_path, empty):
    exports = _staged_subject(tmp_path)
    (tmp_path / empty).write_text("", encoding="utf-8")
    result = _run_script(
        _script(REUSABLE["jobs"]["attest"], "Stage the attested bytes"), tmp_path, exports
    )
    assert result.returncode != 0
    assert "attestation bundle is empty" in result.stdout + result.stderr

def test_every_run_block_matches_its_declared_shell(tmp_path):
    blocks = []
    for name, document in (
        (REUSABLE_PATH.name, REUSABLE),
        (CANDIDATE_PATH.name, CANDIDATE),
        (RELEASE_PATH.name, RELEASE),
    ):
        for job_id, job in document["jobs"].items():
            for step in job.get("steps", []):
                if "run" in step:
                    shell = (
                        step.get("shell")
                        or job.get("defaults", {}).get("run", {}).get("shell")
                        or document.get("defaults", {}).get("run", {}).get("shell")
                        or "bash"
                    )
                    blocks.append((f"{name}:{job_id}:{step['name']}", shell, step["run"]))
    assert blocks

    for label, shell, script in blocks:
        if shell == "python":
            compile(script, label, "exec")
            continue
        if shell == "powershell":
            assert label == (
                "_siteops-distribution.yaml:qualify:Install with the signed PowerShell bootstrap"
            )
            continue
        assert shell == "bash", f"Add syntax coverage for {label}: {shell}"
        path = tmp_path / "block.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run(
            [str(_required_bash()), "--noprofile", "--norc", "-n", _bash_path(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, f"{label}: {result.stderr}"

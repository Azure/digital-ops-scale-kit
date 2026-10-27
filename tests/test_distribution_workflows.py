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
import itertools
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

from tests.actions_expressions import evaluate, truthy
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
EXTRA_CELLS = (
    "Qualify bundle (ubuntu-26.04, Python 3.11)",
    "Qualify bundle (windows-2025, Python 3.11, standard user)",
)
STANDARD_USER_STEP = "Install as a standard user with the signed PowerShell bootstrap"
WHEEL_NAME = "siteops-1.0.0b1+build.42.1.gcccccccccccc-py3-none-any.whl"

ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
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
    assert qualify["strategy"]["matrix"] == {
        "os": list(QUALIFIED_PLATFORMS),
        "python": list(QUALIFIED_PYTHONS),
        "account": ["runner"],
        "include": [
            {"os": "ubuntu-26.04", "python": "3.11"},
            {"os": "windows-2025", "python": "3.11", "account": "standard"},
        ],
    }
    assert qualify["strategy"]["fail-fast"] is False
    assert _step(qualify, "Setup Python")["with"]["python-version"] == "${{ matrix.python }}"
    assert qualify["name"] == (
        "Qualify bundle (${{ matrix.os }}, Python ${{ matrix.python }}"
        "${{ matrix.account == 'standard' && ', standard user' || '' }})"
    )
    assert qualify["runs-on"] == "${{ matrix.os }}"
    assert qualify["permissions"] == {"contents": "read", "actions": "read"}
    assert qualify["needs"] == ["build", "attest"]


def _matrix_cells(matrix: dict) -> list[dict]:
    """Expand a literal matrix with GitHub's include rules, which never merge into added cells."""
    base = {key: value for key, value in matrix.items() if key != "include"}
    originals = [dict(zip(base, values, strict=True)) for values in itertools.product(*base.values())]
    cells = [dict(cell) for cell in originals]
    added = []
    for entry in matrix.get("include", []):
        targets = [
            cell for original, cell in zip(originals, cells, strict=True)
            if all(original[key] == value for key, value in entry.items() if key in original)
        ]
        for cell in targets:
            cell.update(entry)
        if not targets:
            added.append(dict(entry))
    return cells + added


def _cell_name(cell: dict) -> str:
    suffix = ", standard user" if cell.get("account") == "standard" else ""
    return f"Qualify bundle ({cell['os']}, Python {cell['python']}{suffix})"


def _all_cell_names() -> list[str]:
    return [
        f"Qualify bundle ({platform}, Python {python})"
        for python in QUALIFIED_PYTHONS for platform in QUALIFIED_PLATFORMS
    ] + list(EXTRA_CELLS)


def test_matrix_expansion_follows_the_documented_include_rules():
    assert _matrix_cells({
        "fruit": ["apple", "pear"], "animal": ["cat", "dog"],
        "include": [{"color": "green"}, {"color": "pink", "animal": "cat"},
                    {"fruit": "apple", "shape": "circle"}, {"fruit": "banana"},
                    {"fruit": "banana", "animal": "cat"}],
    }) == [
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"},
    ]


def test_qualification_matrix_adds_ubuntu_26_04_and_standard_user_cells():
    matrix = REUSABLE["jobs"]["qualify"]["strategy"]["matrix"]
    names = [_cell_name(cell) for cell in _matrix_cells(matrix)]
    assert sorted(names) == sorted(_all_cell_names())
    assert len(set(names)) == len(names)
    # Without the base account key, the standard user entry would replace the Windows 3.11 cell.
    merged = [_cell_name(cell) for cell in _matrix_cells({
        key: value for key, value in matrix.items() if key != "account"
    })]
    assert "Qualify bundle (windows-2025, Python 3.11)" not in merged
    assert len(merged) == len(names) - 1


@pytest.mark.parametrize("cell", [
    pytest.param(cell, id=_cell_name(cell))
    for cell in _matrix_cells(REUSABLE["jobs"]["qualify"]["strategy"]["matrix"])
])
def test_each_qualification_cell_runs_only_its_installation_path(cell):
    qualify = REUSABLE["jobs"]["qualify"]
    shared = [
        "Confirm the GitHub CLI verification capabilities", "Setup Python", "Download the attested assets",
        "Verify installation assets before use", "Extract the verified bundle",
        "Confirm the bundle describes this build",
    ]
    runner = ["Install the external qualification tooling"]
    after = ["Install Site Ops from the verified lock", "Install Site Ops from the standalone wheel"]
    expected = {
        "ubuntu": shared + runner + ["Install with the signed Bash bootstrap"] + after,
        "windows": shared + runner + ["Install with the signed PowerShell bootstrap"] + after,
        "standard": shared + [STANDARD_USER_STEP],
    }
    kind = "standard" if cell.get("account") == "standard" else cell["os"].split("-")[0]
    contexts = {
        "matrix": cell,
        "runner": {"os": "Windows" if cell["os"].startswith("windows-") else "Linux"},
    }
    steps = [step["name"] for step in qualify["steps"] if truthy(evaluate(step.get("if", "true"), contexts))]
    assert steps == expected[kind]


@pytest.mark.parametrize(("runner", "account", "expected"), [
    pytest.param("Linux", "runner", {"bash", "tooling", "lock", "wheel"}, id="linux-runner"),
    pytest.param("Windows", "runner", {"powershell", "tooling", "lock", "wheel"}, id="windows-runner"),
    pytest.param("Windows", "standard", {"standard"}, id="windows-standard-user"),
    pytest.param("Linux", None, {"bash", "tooling", "lock", "wheel"}, id="linux-missing-account"),
    pytest.param("Windows", None, {"powershell", "tooling", "lock", "wheel"}, id="windows-missing-account"),
    pytest.param("macOS", "runner", {"tooling", "lock", "wheel"}, id="other-runner-has-no-bootstrap"),
    pytest.param("macOS", "standard", set(), id="standard-user-requires-windows"),
])
def test_qualification_installation_guards_use_runner_os_and_account(runner, account, expected):
    names = {
        "bash": "Install with the signed Bash bootstrap",
        "powershell": "Install with the signed PowerShell bootstrap",
        "standard": STANDARD_USER_STEP,
        "tooling": "Install the external qualification tooling",
        "lock": "Install Site Ops from the verified lock",
        "wheel": "Install Site Ops from the standalone wheel",
    }
    contexts = {"runner": {"os": runner}, "matrix": {} if account is None else {"account": account}}
    for key, name in names.items():
        assert truthy(evaluate(_step(REUSABLE["jobs"]["qualify"], name)["if"], contexts)) is (key in expected), name


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
    for directory in ("home", "bootstrap-profile", "python", "cache", "tooling"):
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
    # The bootstrap stages privately under its data root, so the runner's temporary directory stays as is.
    assert "$env:TEMP" not in windows and "$env:TMP" not in windows
    assert windows.index("Rechecking the retained release without downloading") < windows.index(
        "(Join-Path $bootstrapData 'install-staging')",
    )


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
    assert names.index("Confirm the bundle describes this build") < names.index(STANDARD_USER_STEP)
    assert names.index("Install Site Ops from the verified lock") < names.index(
        "Install Site Ops from the standalone wheel"
    )


def test_qualification_runs_both_signed_scripts_with_private_preseeded_assets():
    qualify = REUSABLE["jobs"]["qualify"]
    for name, platform, argument in (
        ("Install with the signed Bash bootstrap", "ubuntu-24.04", "--yes"),
        (
            "Install with the signed PowerShell bootstrap", "windows-2025", "-Yes",
        ),
    ):
        step = _step(qualify, name)
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
    assert 'script="$download/$BOOTSTRAP_SH"' in bash_step
    assert 'bash "$script"' in bash_step
    windows_step = _script(qualify, "Install with the signed PowerShell bootstrap")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File" in windows_step


def _run_linux_bootstrap_cell(tmp_path, python, fault="none"):
    source = _script(REUSABLE["jobs"]["qualify"], "Install with the signed Bash bootstrap")
    block = 'script="$download/$BOOTSTRAP_SH"\n' + source.split(
        'script="$download/$BOOTSTRAP_SH"\n', 1,
    )[1].split("if ! grep", 1)[0]
    download = tmp_path / "download"
    download.mkdir()
    (tmp_path / "logs").mkdir()
    log = tmp_path / "arguments.txt"
    _write_executable(download / BOOTSTRAP_SH, r'''#!/usr/bin/env bash
{
  printf '=== gh ===\n'
  printf '%s\n' "$@"
} >> "$TEST_ARGUMENTS"
if [[ " $* " == *" --dry-run "* ]]; then
  case "$TEST_PREVIEW_FAULT" in
    fail) exit 9 ;;
    missing) exit 0 ;;
    wrong) echo 'Site Ops installation: Discovered source commit: wrong.'; exit 0 ;;
    write) mkdir -p "$XDG_DATA_HOME/siteops" ;;
    duplicate) printf 'Site Ops installation: Discovered source commit: %s.\n' "$SOURCE_SHA" ;;
  esac
  printf 'Site Ops installation: Discovered source commit: %s.\n' "$SOURCE_SHA"
fi
''')
    for name in (ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
        (download / name).write_bytes(b"staged signed asset")
    adapter = (
        'mkdir() { if [[ "$1" == -m && "$2" == 0700 && "$3" == -- ]]; then command mkdir -- "$4"; '
        'elif [[ "$1" == -p && $# == 2 ]]; then command mkdir -p -- "$2"; else return 99; fi; }\n'
        if sys.platform == "win32" else ""
    )
    result = _run_script(
        'set -euo pipefail\n' + adapter + 'download="$TEST_DOWNLOAD";owned="$TEST_ROOT"\n'
        "release=siteops/v0.0.0-ci\ncaller=ci.yaml\n" + block,
        tmp_path,
        {
            "TEST_DOWNLOAD": _bash_path(download), "TEST_ROOT": _bash_path(tmp_path),
            "TEST_ARGUMENTS": _bash_path(log), "BOOTSTRAP_SH": BOOTSTRAP_SH,
            "XDG_DATA_HOME": _bash_path(tmp_path / "data"),
            "TEST_PREVIEW_FAULT": fault,
            "ARCHIVE_NAME": ARCHIVE_NAME, "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX,
            "MATRIX_PYTHON": python, "SOURCE_SHA": "c" * 40, "SOURCE_REPOSITORY": "example/publisher",
            "SOURCE_REF": "refs/heads/feature",
        },
    )
    return result, log, download


@pytest.mark.parametrize("python", QUALIFIED_PYTHONS)
def test_linux_runner_cells_select_discovery_only_for_python_311(tmp_path, python):
    result, log, download = _run_linux_bootstrap_cell(tmp_path, python)
    assert result.returncode == 0, result.stdout + result.stderr
    pinned = ["--release", "siteops/v0.0.0-ci", "--source-commit", "c" * 40,
        "--repository", "example/publisher", "--source-ref", "refs/heads/feature",
        "--caller", "ci.yaml", "--yes"]
    preview = [
        "--release", "siteops/v0.0.0-ci", "--repository", "example/publisher",
        "--source-ref", "refs/heads/feature", "--caller", "ci.yaml", "--dry-run",
    ]
    assert _invocations(log) == ([preview, pinned] if python == "3.11" else [pinned])
    assert (tmp_path / "bootstrap-source" / BOOTSTRAP_SH).exists() is (python == "3.11")
    if python == "3.11":
        for name in (BOOTSTRAP_SH, ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
            assert (tmp_path / "bootstrap-source" / name).read_bytes() == (download / name).read_bytes()


@pytest.mark.parametrize("fault", ["fail", "missing", "wrong", "write", "duplicate"])
def test_linux_preview_must_pin_the_source_without_creating_data(tmp_path, fault):
    result, log, _ = _run_linux_bootstrap_cell(tmp_path, "3.11", fault)
    assert result.returncode != 0
    assert len(_invocations(log)) == 1
    expected = "did not verify the preview" if fault == "fail" else (
        "preview did not preserve the selected source and data"
    )
    assert expected in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Runner invocation uses native PowerShell 5.1.")
@pytest.mark.parametrize("python", QUALIFIED_PYTHONS)
def test_windows_runner_cells_select_discovery_only_for_python_311(tmp_path, python):
    child = (
        '[IO.File]::AppendAllText($env:TEST_ARGUMENTS, '
        '((ConvertTo-Json -InputObject $PSBoundParameters -Compress) + "`n"))\n'
        'if ($DryRun) { Write-Host "Site Ops installation: Discovered source commit: $env:SOURCE_SHA." }\nexit 0\n'
    )
    log = tmp_path / "arguments.json"
    for name in (ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
        (tmp_path / name).write_bytes(b"staged signed asset")
    result = _run_windows_bootstrap_qualifier(
        tmp_path, child,
        {"MATRIX_PYTHON": python, "TEST_ARGUMENTS": str(log),
         "ARCHIVE_NAME": ARCHIVE_NAME, "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX},
        success_marker=True, stage_source=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == (2 if python == "3.11" else 1)
    arguments = calls[-1]
    assert arguments["SourceCommit"] == "c" * 40
    assert arguments["Release"] == "siteops/v0.0.0-ci"
    assert arguments["Repository"] == "example/publisher"
    assert arguments["SourceRef"] == "refs/heads/feat/siteops-guided-inputs"
    assert arguments["Caller"] == "ci.yaml"
    assert arguments["Yes"]["IsPresent"]
    assert "DryRun" not in arguments
    if python == "3.11":
        preview = calls[0]
        assert preview["DryRun"]["IsPresent"]
        assert "SourceCommit" not in preview and "Yes" not in preview
        assert {key: value for key, value in preview.items() if key != "DryRun"} == {
            key: value for key, value in arguments.items() if key not in {"SourceCommit", "Yes"}
        }
    assert (tmp_path / "bootstrap-source").exists() is (python == "3.11")
    if python == "3.11":
        assert (tmp_path / "bootstrap-source" / "bootstrap.ps1").read_bytes() == (tmp_path / "bootstrap.ps1").read_bytes()
        for name in (ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
            assert (tmp_path / "bootstrap-source" / name).read_bytes() == (tmp_path / name).read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="Preview qualification runs native PowerShell 5.1.")
@pytest.mark.parametrize("fault", ["fail", "missing", "wrong", "write", "duplicate"])
def test_windows_preview_must_pin_the_source_without_creating_data(tmp_path, fault):
    log = tmp_path / "arguments.jsonl"
    for name in (ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
        (tmp_path / name).write_bytes(b"staged signed asset")
    child = r'''
[IO.File]::AppendAllText($env:TEST_ARGUMENTS, ((ConvertTo-Json -InputObject $PSBoundParameters -Compress) + "`n"))
if (-not $DryRun) { throw 'The preview boundary allowed installation.' }
switch ($env:TEST_PREVIEW_FAULT) {
    'fail' { exit 9 }
    'missing' { exit 0 }
    'wrong' { Write-Host 'Site Ops installation: Discovered source commit: wrong.'; exit 0 }
    'write' { New-Item -ItemType Directory -Path (Join-Path $env:TEST_ROOT 'profile\siteops\unexpected')|Out-Null }
    'duplicate' { Write-Host "Site Ops installation: Discovered source commit: $env:SOURCE_SHA." }
}
Write-Host "Site Ops installation: Discovered source commit: $env:SOURCE_SHA."
exit 0
'''
    result = _run_windows_bootstrap_qualifier(
        tmp_path, child,
        {"MATRIX_PYTHON": "3.11", "TEST_ARGUMENTS": str(log), "TEST_PREVIEW_FAULT": fault,
         "ARCHIVE_NAME": ARCHIVE_NAME, "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX},
        stage_source=True,
    )
    assert result.returncode != 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    expected = "did not verify the preview" if fault == "fail" else (
        "preview did not preserve the selected source and data"
    )
    assert expected in " ".join((result.stdout + result.stderr).split())


def test_standard_user_cell_keeps_the_explicit_commit_route():
    source = _script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP)
    assert "-SourceCommit $SourceCommit -Repository $Repository -SourceRef $SourceRef" in source
    assert "'-SourceCommit', $env:SOURCE_SHA" in source
    assert "sourceArguments" not in source and "MATRIX_PYTHON" not in source
    matrix = REUSABLE["jobs"]["qualify"]["strategy"]["matrix"]
    assert matrix["python"] == list(QUALIFIED_PYTHONS)
    assert {"os": "ubuntu-26.04", "python": "3.11"} in matrix["include"]
    assert {"os": "windows-2025", "python": "3.11", "account": "standard"} in matrix["include"]


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
    invocation = script.index("& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script")
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
    assert script.index(preload) < script.index("& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script")
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    lines = {line.strip() for line in bootstrap.splitlines()}
    # The preloaded directories are exactly the unmanaged cache roots the bootstrap admits.
    assert "$cacheRoot = Join-Path $data 'install-downloads'" in lines
    assert "Require-PrivateDataRoot $cacheRoot" in lines
    assert "Require-PrivateDataRoot $cache" in lines


def _windows_acl_reader() -> str:
    source = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    reader = re.search(r"(?ms)^function Read-NodeAcl\([^\n]*\) \{.*?^\}", source)
    assert reader is not None
    return reader.group(0) + "\n"


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
        + _windows_acl_reader()
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
            + _windows_acl_reader()
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
    stage_source: bool = False,
) -> subprocess.CompletedProcess[str]:
    script = _script(REUSABLE["jobs"]["qualify"], "Install with the signed PowerShell bootstrap")
    block = script.split("$log = Join-Path $owned 'logs\\bootstrap-ps1.log'\n", 1)[1].split(
        "if ((Get-Content -LiteralPath $log -Raw) -notmatch", 1,
    )[0]
    (tmp_path / "logs").mkdir()
    fake = tmp_path / "bootstrap.ps1"
    fake.write_text(
        "param([string]$Release,[string]$SourceCommit,[string]$Repository,"
        "[string]$SourceRef,[string]$Caller,[switch]$Yes,[switch]$DryRun)\n" + child,
        encoding="utf-8",
    )
    staging = (
        script.split("$release = 'siteops/v0.0.0-ci'\n", 1)[1].split("$selection = ", 1)[0]
        if stage_source else ""
    )
    wrapper = tmp_path / "qualify.ps1"
    wrapper.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$owned=$env:TEST_ROOT;$download=$owned\n"
        "$bootstrapData=Join-Path $owned 'profile\\siteops'\n"
        "New-Item -ItemType Directory -Path $bootstrapData -Force|Out-Null\n"
        "$script=Join-Path $download $env:BOOTSTRAP_PS1\n"
        "$release='siteops/v0.0.0-ci';$caller='ci.yaml'\n"
        + staging
        + "$log=Join-Path $owned 'logs\\bootstrap-ps1.log'\n"
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
            + _windows_acl_reader()
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


# --- Standard user qualification ---------------------------------------------

# Closed doubles for the account, process and profile commands. Functions take
# precedence over cmdlets, so the real step body runs unchanged after them.
# CmdletBinding rejects any parameter a double does not declare.
_STANDARD_USER_DOUBLES = r"""
$record = [ordered]@{ calls = [Collections.Generic.List[string]]::new(); start = $null }
function Save-Record { $record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $env:TEST_RECORD -Encoding UTF8 }
function Get-LocalUser {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$Name)
    $record.calls.Add("Get-LocalUser $Name"); Save-Record
    if ($env:TEST_SCENARIO -eq 'existing-user') { [pscustomobject]@{ Name = $Name } }
}
function New-LocalUser {
    [CmdletBinding()] param(
        [Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][Security.SecureString]$Password,
        [switch]$PasswordNeverExpires, [switch]$UserMayNotChangePassword, [switch]$AccountNeverExpires,
        [ValidateLength(0, 48)][string]$Description
    )
    if (-not ($PasswordNeverExpires -and $UserMayNotChangePassword -and $AccountNeverExpires)) { throw 'Unexpected account policy.' }
    $record.calls.Add("New-LocalUser $Name"); Save-Record
    [pscustomobject]@{ SID = [Security.Principal.SecurityIdentifier]'S-1-5-32-545' }
}
function Add-LocalGroupMember {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$Group, [Parameter(Mandatory = $true)][string]$Member)
    $record.calls.Add("Add-LocalGroupMember $Group $Member"); Save-Record
}
function Get-LocalGroupMember {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$SID)
    $record.calls.Add("Get-LocalGroupMember $SID"); Save-Record
    if ($env:TEST_SCENARIO -eq 'administrator-member') { [pscustomobject]@{ SID = [Security.Principal.SecurityIdentifier]'S-1-5-32-545' } }
}
function Start-Process {
    [CmdletBinding()] param(
        [Parameter(Mandatory = $true)][string]$FilePath, [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][pscredential]$Credential, [switch]$LoadUserProfile,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory, [switch]$PassThru
    )
    $secret = $Credential.GetNetworkCredential().Password
    $leaks = @()
    if (($ArgumentList -join ' ').Contains($secret)) { $leaks += 'arguments' }
    if (@(Get-ChildItem Env: | Where-Object { $_.Value -and $_.Value.Contains($secret) }).Count) { $leaks += 'environment' }
    foreach ($file in Get-ChildItem -LiteralPath $WorkingDirectory -File -Recurse) {
        if ([IO.File]::ReadAllText($file.FullName).Contains($secret)) { $leaks += 'staging' }
    }
    $rights = 0
    foreach ($item in @(Get-Item -LiteralPath $WorkingDirectory) + @(Get-ChildItem -LiteralPath $WorkingDirectory)) {
        foreach ($rule in $item.GetAccessControl().GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.IdentityReference.Value -eq 'S-1-5-32-545') { $rights = $rights -bor [int]$rule.FileSystemRights }
        }
    }
    Copy-Item -LiteralPath (Join-Path $WorkingDirectory 'standard-user.ps1') -Destination $env:TEST_WRAPPER
    $record.start = [ordered]@{
        file = $FilePath; arguments = @($ArgumentList); user = $Credential.UserName; secret = $secret
        loadProfile = [bool]$LoadUserProfile; directory = $WorkingDirectory; passThru = [bool]$PassThru
        leaks = @($leaks); userRights = $rights
        staged = [string[]]@(Get-ChildItem -LiteralPath $WorkingDirectory | ForEach-Object { $_.Name } | Sort-Object)
    }
    Save-Record
    $local = Join-Path $env:TEST_PROFILE 'AppData\Local'
    $results = Join-Path $local 'siteops-qualification'
    $staging = Join-Path $local 'siteops\install-staging'
    New-Item -ItemType Directory -Path $results, $staging -Force | Out-Null
    $log = "Site Ops installation: Rechecking the retained release without downloading its assets.`n" +
        "Site Ops installation: Provisioning uv-managed CPython 3.11.16 without command aliases or registry changes.`n"
    $result = [ordered]@{ user = 'S-1-5-32-545'; administrator = $false; version = "siteops $env:PACKAGE_VERSION" }
    $exit = 0
    switch ($env:TEST_SCENARIO) {
        'bootstrap-failure' { $log = "Site Ops installation failed: $env:TEST_MESSAGE PRIVATE_PATH"; $exit = 1 }
        'administrator' { $result.administrator = $true }
        'cache-not-used' { $log = $log.Replace('Rechecking the retained release without downloading', 'Downloading') }
        'runtime' { $log = $log.Replace('CPython 3.11.16', 'CPython 3.12.4') }
        'leftover' { New-Item -ItemType File -Path (Join-Path $staging 'PRIVATE_PATH') | Out-Null }
        'version' { $result.version = 'siteops 0.0.0' }
        'missing-result' { $result = $null }
    }
    Set-Content -LiteralPath (Join-Path $results 'bootstrap.log') -Value $log
    if ($result) { $result | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $results 'result.json') }
    $process = [pscustomobject]@{ Id = 4242; ExitCode = $exit; HasExited = $env:TEST_SCENARIO -ne 'timeout'; Handle = 1 }
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($Milliseconds) $this.HasExited }
    return $process
}
function Get-ItemProperty {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if ($LiteralPath -cne 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\S-1-5-32-545') {
        throw 'Unexpected registry read.'
    }
    [pscustomobject]@{ ProfileImagePath = $env:TEST_PROFILE }
}
function Get-CimInstance {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$ClassName, [Parameter(Mandatory = $true)][string]$Filter)
    if ($ClassName -eq 'Win32_Process') {
        # A system process is always present. Only the account's own process may be stopped.
        if ($env:TEST_CLEANUP -eq 'process-query-fails') { throw 'Synthetic process query failure.' }
        $items = @([pscustomobject]@{ ProcessId = 900; Owner = 'S-1-5-18' })
        if ($env:TEST_CLEANUP -eq 'lingering-process' -or
            ($env:TEST_CLEANUP -eq 'stopped-process' -and -not $stopped.Contains(777))) {
            $items += [pscustomobject]@{ ProcessId = 777; Owner = 'S-1-5-32-545' }
        }
        return $items
    }
    $record.calls.Add("Get-CimInstance $ClassName $Filter"); Save-Record
    [pscustomobject]@{ SID = 'S-1-5-32-545' }
}
$stopped = [Collections.Generic.List[int]]::new()
function Invoke-CimMethod {
    [CmdletBinding()] param([Parameter(Mandatory = $true)]$InputObject, [Parameter(Mandatory = $true)][string]$MethodName)
    if ($MethodName -cne 'GetOwnerSid') { throw 'Unexpected CIM method.' }
    [pscustomobject]@{ Sid = $InputObject.Owner }
}
function Stop-Process {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][int]$Id, [switch]$Force)
    $record.calls.Add("Stop-Process $Id"); Save-Record
    $stopped.Add($Id)
}
function Start-Sleep { [CmdletBinding()] param([Parameter(Mandatory = $true)][int]$Seconds) }
function Remove-CimInstance {
    [CmdletBinding()] param([Parameter(Mandatory = $true, ValueFromPipeline = $true)]$InputObject)
    process {
        $record.calls.Add('Remove-CimInstance ' + $InputObject.SID); Save-Record
        if ($env:TEST_CLEANUP -eq 'profile-remains') { throw 'Synthetic profile removal failure.' }
    }
}
function Remove-LocalUser {
    [CmdletBinding()] param([Parameter(Mandatory = $true)][string]$SID)
    $record.calls.Add("Remove-LocalUser $SID"); Save-Record
    if ($env:TEST_CLEANUP -eq 'account-remains') { throw 'Synthetic account removal failure.' }
}
function taskkill.exe { $record.calls.Add('taskkill ' + ($args -join ' ')); Save-Record }
# Runs the real icacls, except that a staging scenario refuses the grant, grants the user write access,
# adds the explicit Administrators rule an elevated runner gives a new folder, or adds an untrusted reader.
function icacls.exe {
    if ($env:TEST_SCENARIO -eq 'staging-grant') { $global:LASTEXITCODE = 5; return }
    $arguments = @(foreach ($argument in $args) {
        if ($env:TEST_SCENARIO -eq 'staging-access') { $argument -replace ':\(OI\)\(CI\)RX$', ':(OI)(CI)M' } else { $argument }
    })
    $native = (Get-Command icacls.exe -CommandType Application | Select-Object -First 1).Source
    & $native @arguments
    $status = $LASTEXITCODE
    $extra = @{ 'elevated-folder' = '*S-1-5-32-544:F'; 'other-principal' = '*S-1-5-11:R' }[$env:TEST_SCENARIO]
    if ($extra -and $status -eq 0) { & $native $args[0] /grant $extra *> $null; $status = $LASTEXITCODE }
    $global:LASTEXITCODE = $status
}
"""


def _standard_user_selection() -> str:
    identity = "\0".join(["example/publisher", "siteops/v0.0.0-ci", "c" * 40, "refs/heads/main", "release.yaml"])
    return hashlib.sha256((identity + "\0").encode()).hexdigest()


def _run_standard_user_step(tmp_path: Path, scenario: str, **extra: str):
    runner_temp = tmp_path / "runner"
    download = runner_temp / "siteops-download"
    download.mkdir(parents=True)
    for name in (BOOTSTRAP_PS1, ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX):
        if scenario == "staging-copy" and name.endswith(ATTESTATION_SUFFIX):
            continue
        (download / name).write_text("synthetic " + name, encoding="utf-8")
    if scenario == "existing-staging":
        (runner_temp / "siteops-standard-user").mkdir()
    script = tmp_path / "step.ps1"
    script.write_text(
        _STANDARD_USER_DOUBLES + _script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP), encoding="utf-8-sig",
    )
    record = tmp_path / "record.json"
    # A PowerShell 7 parent's module path hides Windows PowerShell's own modules, unlike a runner step.
    inherited = {name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"}
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={
            **inherited, "RUNNER_TEMP": str(runner_temp), "TEST_SCENARIO": scenario,
            "TEST_RECORD": str(record), "TEST_PROFILE": str(tmp_path / "profile"),
            "TEST_WRAPPER": str(tmp_path / "staged-wrapper.ps1"),
            "SOURCE_SHA": "c" * 40, "SOURCE_REPOSITORY": "example/publisher", "SOURCE_REF": "refs/heads/main",
            "BUILDER_IDENTITY": "https://github.com/example/publisher/.github/workflows/release.yaml@refs/heads/main",
            "PACKAGE_VERSION": "1.0.0b1+build.42.1.gcccccccccccc", "ARCHIVE_NAME": ARCHIVE_NAME,
            "ATTESTATION_SUFFIX": ATTESTATION_SUFFIX, "BOOTSTRAP_PS1": BOOTSTRAP_PS1, **extra,
        },
    )
    recorded = json.loads(record.read_text(encoding="utf-8-sig")) if record.exists() else {"calls": [], "start": None}
    return result, recorded, runner_temp / "siteops-standard-user"


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
def test_windows_qualification_standard_user_installs_from_read_only_staging(tmp_path):
    result, recorded, staging = _run_standard_user_step(tmp_path, "success")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "A standard user installed the verified bundle" in result.stdout
    start = recorded["start"]
    secret = start["secret"]
    # The password is masked before any other use and appears nowhere else.
    assert len(secret) == 36 and secret.endswith("Aa1!")
    lines = result.stdout.splitlines()
    mask = next(index for index, line in enumerate(lines) if secret in line)
    assert lines[mask] == "::add-mask::" + secret
    assert secret not in "\n".join(lines[mask + 1:]) + result.stderr
    assert start["leaks"] == []
    assert start["user"] == "siteops-standard"
    assert start["loadProfile"] is True and start["passThru"] is True
    assert start["file"].lower().endswith("\\windowspowershell\\v1.0\\powershell.exe")
    assert start["directory"] == str(staging)
    assert start["arguments"] == [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(staging / "standard-user.ps1"),
        "-SourceCommit", "c" * 40, "-Repository", "example/publisher", "-SourceRef", "refs/heads/main",
        "-Caller", "release.yaml", "-Selection", _standard_user_selection(),
    ]
    assert sorted(start["staged"]) == sorted([ARCHIVE_NAME, ARCHIVE_NAME + ATTESTATION_SUFFIX, BOOTSTRAP_PS1, "standard-user.ps1"])
    step = _script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP)
    reviewed = step.split("$wrapper = @'\n", 1)[1].split("\n'@\n", 1)[0]
    staged_wrapper = (tmp_path / "staged-wrapper.ps1").read_text(encoding="utf-8-sig")
    assert staged_wrapper.replace("\r\n", "\n").strip() == reviewed.strip()
    # Read and execute only, independent of the step's own access check.
    assert start["userRights"] & 0x200A9 == 0x200A9
    assert start["userRights"] & 0x500D0156 == 0
    assert recorded["calls"][:3] == [
        "Get-LocalUser siteops-standard", "New-LocalUser siteops-standard",
        "Add-LocalGroupMember Users siteops-standard",
    ]
    assert recorded["calls"][-3:] == [
        "Get-CimInstance Win32_UserProfile SID = 'S-1-5-32-545'",
        "Remove-CimInstance S-1-5-32-545", "Remove-LocalUser S-1-5-32-545",
    ]
    assert not staging.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
@pytest.mark.parametrize(("scenario", "category", "message"), [
    *[
        ("bootstrap-failure", code, f"Configure a private Site Ops data root. {code} Use trusted directories.")
        for code in ("ROOT_PATH", "ROOT_ANCESTOR_TYPE", "ROOT_ANCESTOR_OWNER", "ROOT_ANCESTOR_ACL",
                     "ROOT_DATA_CREATE", "ROOT_DATA_TYPE", "ROOT_DATA_OWNER", "ROOT_DATA_ACL")
    ],
    *[
        ("bootstrap-failure", code, f"Choose a private Windows tool location. {code} Use trusted directories.")
        for code in ("TOOL_PATH", "TOOL_TYPE", "TOOL_OWNER", "TOOL_ACL")
    ],
    ("bootstrap-failure", "GH_ADMISSION", "The GitHub CLI executable must be owned by an administrator or the current user."),
    ("bootstrap-failure", "GH_VERSION", "GitHub CLI 2.95 or newer is required."),
    ("bootstrap-failure", "BOOTSTRAP", "An unclassified failure."),
    ("timeout", "TIMEOUT", ""),
    ("administrator", "NOT_STANDARD", ""),
    ("administrator-member", "NOT_STANDARD", ""),
    ("cache-not-used", "CACHE_NOT_USED", ""),
    ("runtime", "RUNTIME", ""),
    ("leftover", "STAGING_LEFT", ""),
    ("staging-grant", "STAGING_GRANT", ""),
    ("staging-copy", "STAGING_COPY", ""),
    ("staging-access", "STAGING_ACCESS", ""),
    ("other-principal", "STAGING_ACCESS", ""),
    ("version", "VERSION", ""),
    ("missing-result", "RESULT", ""),
])
def test_windows_qualification_standard_user_reports_fixed_failure_categories(
    tmp_path, scenario, category, message,
):
    result, recorded, staging = _run_standard_user_step(tmp_path, scenario, TEST_MESSAGE=message)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"The standard user installation did not pass ({category})." in output
    assert "PRIVATE_PATH" not in output
    assert "A standard user installed the verified bundle" not in output
    assert ("taskkill /PID 4242 /T /F" in recorded["calls"]) is (scenario == "timeout")
    assert recorded["calls"][-1] == "Remove-LocalUser S-1-5-32-545"
    assert not staging.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
@pytest.mark.parametrize("fault", ["lingering-process", "process-query-fails", "profile-remains", "account-remains"])
def test_windows_qualification_standard_user_fails_when_cleanup_is_incomplete(tmp_path, fault):
    result, recorded, staging = _run_standard_user_step(tmp_path, "success", TEST_CLEANUP=fault)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "The standard user installation did not pass (CLEANUP)." in output
    assert "A standard user installed the verified bundle" not in output
    # One failed removal never skips the others.
    assert recorded["calls"][-2:] == ["Remove-CimInstance S-1-5-32-545", "Remove-LocalUser S-1-5-32-545"]
    assert "Stop-Process 900" not in recorded["calls"]
    assert not staging.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
def test_windows_qualification_standard_user_stops_only_the_accounts_remaining_processes(tmp_path):
    result, recorded, _ = _run_standard_user_step(tmp_path, "success", TEST_CLEANUP="stopped-process")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "A standard user installed the verified bundle" in result.stdout
    assert recorded["calls"].count("Stop-Process 777") == 1
    assert "Stop-Process 900" not in recorded["calls"]
    assert recorded["calls"].index("Stop-Process 777") < recorded["calls"].index("Remove-CimInstance S-1-5-32-545")


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
def test_windows_qualification_standard_user_keeps_the_earlier_failure_category(tmp_path):
    result, _, _ = _run_standard_user_step(tmp_path, "version", TEST_CLEANUP="profile-remains")
    output = result.stdout + result.stderr
    assert "The standard user installation did not pass (VERSION)." in output
    assert "(CLEANUP)" not in output
    assert "::warning::The temporary standard user, its processes, profile or staging remain" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
def test_windows_qualification_standard_user_trusts_the_administrators_rule_of_an_elevated_folder(tmp_path):
    # An elevated job creates the staging folder with an explicit Administrators rule.
    result, recorded, _ = _run_standard_user_step(tmp_path, "elevated-folder")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "A standard user installed the verified bundle" in result.stdout
    assert recorded["start"]["userRights"] & 0x500D0156 == 0


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
def test_windows_qualification_standard_user_reports_staging_access_by_class(tmp_path):
    result, _, staging = _run_standard_user_step(tmp_path, "staging-access")
    output = result.stdout + result.stderr
    assert "The standard user installation did not pass (STAGING_ACCESS)." in output
    lines = [line for line in result.stdout.splitlines() if line.startswith("Staging access: ")]
    # A local folder is owned by the job account. An elevated runner makes Administrators the owner.
    assert any(re.fullmatch(r"Staging access: folder owner (job-account|administrators)\.", line) for line in lines)
    # The user's modify grant is the reason the check failed, reported as a class and a rights mask.
    assert any(re.fullmatch(r"Staging access: folder rule standard-user Allow 0x[0-9A-F]+ explicit\.", line)
               for line in lines)
    assert any(line.startswith(f"Staging access: {BOOTSTRAP_PS1} rule standard-user Allow ") for line in lines)
    diagnostics = "\n".join(lines)
    assert "S-1-" not in diagnostics and str(tmp_path) not in diagnostics
    assert not staging.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="The standard user step runs in Windows PowerShell 5.1.")
@pytest.mark.parametrize(("scenario", "category", "extra"), [
    ("existing-user", "ACCOUNT", {}),
    ("existing-staging", "STAGING_EXISTS", {}),
    ("input", "INPUT", {"SOURCE_REF": "refs/heads/main extra"}),
])
def test_windows_qualification_standard_user_keeps_what_it_did_not_create(tmp_path, scenario, category, extra):
    result, recorded, staging = _run_standard_user_step(tmp_path, scenario, **extra)
    assert result.returncode != 0
    assert f"The standard user installation did not pass ({category})." in result.stdout + result.stderr
    assert not any(call.startswith(("New-LocalUser", "Remove-LocalUser", "Remove-CimInstance"))
                   for call in recorded["calls"])
    assert "::add-mask::" not in result.stdout
    assert staging.exists() is (scenario == "existing-staging")


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell 5.1 parses the step.")
def test_windows_qualification_standard_user_step_structure():
    from tests.powershell_ast import describe

    step = _step(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP)
    assert step["shell"] == "powershell" and "env" not in step
    outer = describe(step["run"])
    assert outer["errors"] == []
    wrapper_source = [item["value"] for item in outer["hereStrings"] if item["target"] == "$wrapper"]
    assert len(wrapper_source) == 1
    wrapper = describe(wrapper_source[0])
    assert wrapper["errors"] == []
    commands = {item["name"].lower() for item in outer["commands"] + wrapper["commands"]}
    assert not commands & {"register-scheduledtask", "new-scheduledtaskaction", "schtasks", "schtasks.exe"}

    def uses(name):
        return [item for item in outer["variables"] if item["name"] == name]

    password = [item for item in uses("password") if not item["assigned"]]
    assert [(item["parent"], item["command"], item["parentText"]) for item in password] == [
        ("ExpandableStringExpressionAst", "Write-Host", '"::add-mask::$password"'),
        ("InvokeMemberExpressionAst", "", "$password.ToCharArray()"),
    ]
    assert all(item["command"] in {"", "New-LocalUser"} for item in uses("secure"))
    assert {item["command"] for item in uses("credential") if not item["assigned"]} == {"Start-Process"}
    assert not any(item["assignmentTarget"].lower().startswith("$env:") for item in outer["variables"])
    arguments = next(item for item in outer["variables"] if item["name"] == "arguments" and item["assigned"])
    assert "$env:SOURCE_SHA" in arguments["parentText"]
    assert not re.search(r"\$(password|secure|credential)\b", arguments["parentText"])
    start = [item for item in outer["commands"] if item["name"] == "Start-Process"]
    assert len(start) == 1
    assert set(start[0]["parameters"]) == {
        "FilePath", "ArgumentList", "Credential", "LoadUserProfile", "WorkingDirectory", "PassThru",
    }
    grants = [" ".join(item["text"].split()) for item in outer["commands"] if item["name"] == "icacls.exe"]
    assert grants == [
        "& icacls.exe $Path /inheritance:r /grant:r \"*${Owner}:(OI)(CI)F\" '*S-1-5-18:(OI)(CI)F' ` "
        "\"*${User}:(OI)(CI)RX\" *> $null"
    ]
    cleanup = [text for text in outer["finallyBlocks"] if "Remove-LocalUser" in text]
    assert len(cleanup) == 1
    for fragment in ("taskkill.exe", "Get-StandardUserProcessIds $userSid", "Stop-Process -Id $id",
                     "Remove-CimInstance", "Remove-LocalUser -SID $userSid",
                     "Remove-Item -LiteralPath $staging", "$secure.Dispose()"):
        assert fragment in cleanup[0]
    calls = [item["text"] for item in outer["commands"] if item["name"] == "Stop-StandardUser"]
    categories = {re.fullmatch(r"Stop-StandardUser '([A-Z_]+)'", text).group(1)
                  for text in calls if "Get-BootstrapFailure" not in text}
    assert categories == {"INPUT", "ACCOUNT", "NOT_STANDARD", "STAGING_EXISTS", "STAGING_CREATE", "STAGING_GRANT",
                          "STAGING_COPY", "STAGING_ACCESS", "LAUNCH", "TIMEOUT", "RESULT",
                          "CACHE_NOT_USED", "RUNTIME", "STAGING_LEFT", "VERSION", "CLEANUP"}
    bootstrap = [item for item in wrapper["commands"] if "-Release" in item["text"]]
    assert len(bootstrap) == 1
    assert "(Join-Path $PSScriptRoot 'siteops-bootstrap.ps1') -Release 'siteops/v0.0.0-ci'" in bootstrap[0]["text"]
    assert set(bootstrap[0]["parameters"]) >= {"File", "Release", "SourceCommit", "Repository", "SourceRef",
                                                 "Caller", "Yes"}


def test_standard_user_classification_matches_the_bootstrap_messages():
    step = _script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP)
    bootstrap = (REPO_ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for fragment in (
        'Configure a private Site Ops data root. $Code', 'Choose a private Windows tool location. $Code',
        'The GitHub CLI executable must be owned by an administrator',
        'GitHub CLI 2.95 or newer is required', 'Rechecking the retained release without downloading',
        "Provisioning uv-managed CPython 3.11.",
    ):
        assert fragment.replace("$Code", "") in bootstrap, fragment
    for code in re.findall(r"Reject '((?:ROOT|TOOL)_[A-Z_]+)'", bootstrap):
        assert f"'{code}'" in step, code
    # The staged names are the workflow's own asset names.
    assert "'siteops-install.zip', 'siteops-install.zip.attestation.jsonl'" in step
    assert REUSABLE["env"]["ARCHIVE_NAME"] + REUSABLE["env"]["ATTESTATION_SUFFIX"] == (
        "siteops-install.zip.attestation.jsonl"
    )
    assert "(Join-Path $PSScriptRoot 'siteops-bootstrap.ps1')" in step
    assert REUSABLE["env"]["BOOTSTRAP_PS1"] == "siteops-bootstrap.ps1"
    assert "Register-ScheduledTask" not in step and "schtasks" not in step


@pytest.mark.skipif(sys.platform != "win32", reason="The GitHub CLI preflight reads Windows ACLs.")
def test_windows_qualification_standard_user_preflight_reports_fixed_classes():
    from tests.powershell_ast import describe, function_source, run

    outer = describe(_script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP))
    functions = function_source(outer, "Get-PrincipalClass") + "\n" + function_source(
        outer, "Write-GitHubCliPreflight",
    )
    result = run(
        functions + "\n$job = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
        "foreach ($sid in @('S-1-5-32-544', 'S-1-5-18', "
        "'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464', 'S-1-3-4', $job, 'S-1-5-32-545')) {\n"
        "    Write-Host ('class ' + (Get-PrincipalClass $sid $job))\n}\n"
        "Write-GitHubCliPreflight $job\n",
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:6] == [f"class {name}" for name in (
        "administrators", "system", "trustedinstaller", "creator-owner", "job-account", "other",
    )]
    classes = r"(administrators|system|trustedinstaller|creator-owner|job-account|other)"
    pattern = re.compile(
        rf"GitHub CLI preflight: (gh\.exe is not on the machine PATH\.|"
        rf"(gh\.exe|parent [0-9]+) (could not be read\.|owner {classes}, writers (none|{classes}( {classes})*)\.))"
    )
    assert lines[6:] and all(pattern.fullmatch(line) for line in lines[6:]), lines[6:]


@pytest.mark.skipif(sys.platform != "win32", reason="The staging grant uses Windows ACLs.")
@pytest.mark.parametrize(("extra_grant", "expected"), [
    (None, "True"), ("*S-1-5-32-545:(OI)(CI)M", "False"), ("*S-1-1-0:(OI)(CI)RX", "False"),
])
def test_windows_qualification_standard_user_staging_is_read_and_execute_only(tmp_path, extra_grant, expected):
    from tests.powershell_ast import describe, function_source, run

    outer = describe(_script(REUSABLE["jobs"]["qualify"], STANDARD_USER_STEP))
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "siteops-bootstrap.ps1").write_text("synthetic", encoding="utf-8")
    extra = f"& icacls.exe $path /grant '{extra_grant}' *> $null\n" if extra_grant else ""
    result = run(
        function_source(outer, "Grant-StagingAccess") + "\n" + function_source(outer, "Test-StagingAccess") + "\n"
        f"$path = '{staging}'\n$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
        "if (-not (Grant-StagingAccess $path $owner 'S-1-5-32-545')) { throw 'grant failed' }\n"
        + extra + "Write-Host (Test-StagingAccess $path $owner 'S-1-5-32-545')\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_bootstrap_qualification_shells_parse():
    bash_step = _script(REUSABLE["jobs"]["qualify"], "Install with the signed Bash bootstrap")
    parsed = subprocess.run(
        [str(_required_bash()), "-n"], input=bash_step, text=True, capture_output=True, timeout=20,
    )
    assert parsed.returncode == 0, parsed.stderr
    if sys.platform == "win32":
        for name in ("Install with the signed PowerShell bootstrap", STANDARD_USER_STEP):
            windows_step = _script(REUSABLE["jobs"]["qualify"], name)
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
    assert "gh api --paginate --slurp" in script
    assert (
        "actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT/jobs?per_page=100"
        in script
    )
    assert 'name = job["name"].rsplit(" / ", 1)[-1]' in script
    assert "cell = expected.get(name)" in script
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
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in _all_cell_names()
    ]


def _expected_matrix(state: str, **cells) -> list[dict[str, str]]:
    """Return the qualification matrix the summary encodes, with optional per-cell overrides."""
    rows = []
    for python in QUALIFIED_PYTHONS:
        extra = state if python == "3.11" else "n/a"
        row = {"python": python, "linux": state, "ubuntu-26.04": extra, "windows": state,
               "windows-standard-user": extra}
        row.update(cells.get(python, {}))
        rows.append(row)
    return rows


def _run_distribution_summary(
    tmp_path: Path,
    jobs: list[dict],
    *,
    report_summary: bool = True,
    build_result: str = "success",
    attest_result: str = "success",
    qualify_result: str = "success",
    api_exit: str = "0",
    page_size: int = 100,
):
    _, log = _fake_tools(tmp_path)
    response = tmp_path / "jobs.json"
    # gh api --paginate --slurp returns one document per page.
    pages = [{"jobs": jobs[index:index + page_size]} for index in range(0, max(len(jobs), 1), page_size)]
    response.write_text(json.dumps(pages), encoding="utf-8")
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
            "--paginate",
            "--slurp",
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
    assert "| Python | Ubuntu 24.04 | Ubuntu 26.04 | Windows | Windows standard user |" in summary
    for python in QUALIFIED_PYTHONS:
        extra = "passed" if python == "3.11" else "n/a"
        assert f"| {python} | passed | {extra} | passed | {extra} |" in summary
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

    assert result.returncode != 0
    assert "Every expected qualification cell must pass exactly once." in result.stdout + result.stderr
    matrix = json.loads(encoded)
    assert matrix == _expected_matrix("passed", **{"3.12": {"linux": expected}})
    summary = summary_path.read_text(encoding="utf-8")
    assert f"| 3.12 | {expected} | n/a | passed | n/a |" in summary
    assert "One or more qualification cells did not pass." in summary


@pytest.mark.parametrize("cell", [*EXTRA_CELLS, "Qualify bundle (windows-2025, Python 3.11)"])
@pytest.mark.parametrize(("case", "status"), [("missing", "not-run"), ("failure", "failed")])
def test_distribution_summary_requires_every_declared_cell(tmp_path, cell, case, status):
    jobs = _qualification_jobs()
    selected = next(job for job in jobs if job["name"] == cell)
    if case == "missing":
        jobs.remove(selected)
    else:
        selected["conclusion"] = case
    # The aggregate qualify result alone cannot stand in for a missing cell.
    result, encoded, summary_path, _ = _run_distribution_summary(tmp_path, jobs)

    assert result.returncode != 0
    column = {
        EXTRA_CELLS[0]: "ubuntu-26.04", EXTRA_CELLS[1]: "windows-standard-user",
    }.get(cell, "windows")
    assert json.loads(encoded) == _expected_matrix("passed", **{"3.11": {column: status}})
    assert "One or more qualification cells did not pass." in summary_path.read_text(encoding="utf-8")


def test_distribution_summary_rejects_a_cell_outside_the_declared_matrix(tmp_path):
    jobs = _qualification_jobs() + [
        {"name": "Qualify bundle (ubuntu-22.04, Python 3.11)", "status": "completed", "conclusion": "success"},
    ]
    result, encoded, summary_path, _ = _run_distribution_summary(tmp_path, jobs)
    assert result.returncode != 0
    assert json.loads(encoded) == _expected_matrix("passed")
    assert "outside the expected cells" in summary_path.read_text(encoding="utf-8")
    # Unrelated jobs in the same run remain valid neighbors.
    neighbors = _qualification_jobs() + [
        {"name": name, "status": "completed", "conclusion": "failure"}
        for name in ("Build bundle", "Qualify workspace engine (windows, Python 3.11)")
    ]
    (tmp_path / "neighbors").mkdir()
    result, _, _, _ = _run_distribution_summary(tmp_path / "neighbors", neighbors)
    assert result.returncode == 0, result.stdout + result.stderr


def test_distribution_summary_reads_every_page_of_jobs(tmp_path):
    result, encoded, _, _ = _run_distribution_summary(tmp_path, _qualification_jobs(), page_size=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == _expected_matrix("passed")


@pytest.mark.parametrize("prefix", ["Rehearse distribution", "Rehearse release / Candidate installation"])
def test_distribution_summary_accepts_real_reusable_workflow_job_names(tmp_path, prefix):
    jobs = _qualification_jobs()
    for job in jobs:
        job["name"] = prefix + " / " + job["name"]
    result, encoded, _, _ = _run_distribution_summary(tmp_path, jobs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(encoded) == _expected_matrix("passed")


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
        "ubuntu-26.04": "n/a",
        "windows": "passed",
        "windows-standard-user": "n/a",
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

    # The summary still explains the earlier failure, and its own job fails with the run.
    assert result.returncode != 0
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
    # Suppressing presentation never suppresses the completeness requirement.
    (tmp_path / "incomplete").mkdir()
    result, _, _, _ = _run_distribution_summary(
        tmp_path / "incomplete", _qualification_jobs()[:-1], report_summary=False
    )
    assert result.returncode != 0


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
            assert label in {
                "_siteops-distribution.yaml:qualify:Install with the signed PowerShell bootstrap",
                f"_siteops-distribution.yaml:qualify:{STANDARD_USER_STEP}",
            }
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

"""Exercise the native installation guide's command and path contracts."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shell_helpers import bash_path, run_script, write_executable
from tests.verification_helpers import verified_observation

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "install-siteops.md"


def test_root_quickstart_connects_install_to_aio_without_hiding_fleet_use():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    journey = readme.split("## Quick start\n", 1)[1].split("\n## Browse deployment choices", 1)[0]
    for phrase in (
        "docs/install-siteops.md",
        "docs/install-siteops.md#install-the-release-wheel",
        "docs/install-siteops.md#bootstrap-from-https",
        "selected release's generated installation instructions",
        "independently",
        "private terminal",
        "--yes",
        "docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance",
        "enableSecretSync=true",
        "siteops --approved-source official project pin",
        "docs/targeting.md",
        "docs/getting-started.md",
    ):
        assert phrase in journey
    first = next(
        line for line in journey.splitlines()
        if line.startswith("siteops deploy aio-install --source")
    )
    assert shlex.split(first) == [
        "siteops", "deploy", "aio-install", "--source", "official@<release>",
        "--input", "cluster=<Arc-cluster-resource-ID>",
    ]
    assert journey.index(first) < journey.index("siteops --approved-source official project pin")
    assert journey.index("#install-the-release-wheel") < journey.index(first)
    assert journey.index("#bootstrap-from-https") < journey.index(first)
    assert "plan aio-install -l name=plant-two,name=plant-three" in journey
    assert "siteops inputs aio-install --example" not in journey
    assert "--read-resources" not in journey
    assert "siteops-bootstrap.sh" not in journey


def test_workspace_guides_begin_with_the_installed_deployment_route():
    workspace = GUIDE.parent.parent / "workspaces" / "iot-operations"
    for path in (workspace / "README.md", workspace / "manifests" / "aio-install" / "README.md"):
        text = path.read_text(encoding="utf-8")
        commands = [line for line in text.splitlines() if line.startswith("siteops ")]
        assert shlex.split(commands[0]) == [
            "siteops", "deploy", "aio-install", "--source", "official@<release>",
            "--input", "cluster=<Arc-cluster-resource-ID>",
        ]
        assert "local checkout" in text
        assert any(" -w workspaces/iot-operations " in line for line in commands)


def test_quickstart_separates_release_installation_and_checkout_browsing():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "not yet published" not in readme
    assert "siteops --approved-source official --project factory browse aio-install" in readme
    assert "require a local checkout" in readme
    assert "does not sign you in" in readme
    assert "docs/install-siteops.md" in readme


def test_hosted_bootstrap_guidance_matches_host_behavior():
    guide = GUIDE.read_text(encoding="utf-8")
    bash = (GUIDE.parent.parent / "scripts/bootstrap/siteops-bootstrap.sh").read_text(
        encoding="utf-8",
    )
    powershell = (GUIDE.parent.parent / "scripts/bootstrap/siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    for phrase in (
        "Azure Cloud Shell", "Ubuntu 26.04", "administrator rights or install OS packages",
        "user private group", "uv-managed Python", "UV_PYTHON_INSTALL_MIRROR", "Codespace", "k3d",
        "Arc-connected",
    ):
        assert phrase in guide
    for removed in ("--with-azure-cli", "-WithAzureCli", "managed Azure Linux 3"):
        assert removed not in guide
    for script in (bash, powershell):
        assert "Command directory:" in script
    assert "export PATH=" in guide and "$env:PATH" in guide


def test_project_source_renewal_and_saved_site_guide_are_executable():
    projects = (GUIDE.parent / "projects.md").read_text(encoding="utf-8")
    guided = (GUIDE.parent / "guided-inputs.md").read_text(encoding="utf-8")
    assert "siteops source enroll --help" in projects
    assert "siteops source remove official" in projects
    assert "siteops source show official" in projects
    assert "30 days" in projects and "renewed-policy.json" in projects
    assert "source.profile-expired" in projects and "no automatic renewal" in projects
    assert "mkdir -p ./factory/sites" in guided
    assert "--input-file ./aio-inputs.yaml --read-resources --save-site" in guided
    assert "cluster: null" in guided
    assert "-l name=plant-two,name=plant-three" in guided
    assert guided.count("--input-file ./fleet-inputs.yaml") >= 2
    assert "existingVault" in guided
    assert "enableSecretSync: false" in guided
    assert "name=plant-one,name=plant-two" not in guided
    normalized = " ".join(guided.split())
    assert "separate fleet deployment" in normalized and "already runs AIO" in normalized


def test_guided_guide_selects_release_per_inline_site_then_configured_fleet():
    guided = (GUIDE.parent / "guided-inputs.md").read_text(encoding="utf-8")
    assert 'deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>" --input enableSecretSync=true' in guided
    assert 'deploy secretsync --source "official@<release>" --input "instance=<AIO-instance-resource-ID>"' in guided
    assert "--input aioRelease=2608" in guided
    assert "--input aioRelease=2607" in guided
    assert "--input siteName=plant-2608" in guided
    assert "--input siteName=plant-2607" in guided
    assert 'deploy aio-install --source "official@<release>" --input siteName=plant-2608' in guided
    assert 'deploy aio-install --source "official@<release>" --input siteName=plant-2607' in guided
    assert "--input aioRelease=2608 --read-resources" not in guided
    assert "--input aioRelease=2607 --read-resources" not in guided
    assert "plan aio-install -l environment=dev" in guided
    assert "deploy aio-install -l environment=dev" in guided
    assert "every configured Site labeled `dev`" in guided
    assert "already runs AIO" in guided


def test_reference_distinguishes_required_inputs_from_resource_derivation():
    reference = (GUIDE.parent / "manifest-reference.md").read_text(encoding="utf-8")
    targeting = (GUIDE.parent / "targeting.md").read_text(encoding="utf-8")
    configuration = (GUIDE.parent / "site-configuration.md").read_text(encoding="utf-8")
    assert "unless `required: false`" in reference
    assert "`cluster: null`" in reference
    assert "SiteInputContract" in reference and "`siteops.inputs/` namespace" in reference
    assert "Unrelated sample wiring" in reference and "declared contract" in reference
    assert "already runs AIO" in targeting
    assert "checks Site identities before writing" in configuration


def test_release_guide_distinguishes_bootstrap_assets_from_older_engine_references():
    guide = (GUIDE.parent / "releasing.md").read_text(encoding="utf-8")
    reference = guide.split("### Release content against an existing engine", 1)[1].split(
        "### Include an engine build in a content prerelease", 1
    )[0]
    combined = guide.split("### Include an engine build in a content prerelease", 1)[1].split(
        "## Release fields and defaults", 1
    )[0]
    combined = " ".join(combined.split())
    assert "four asset" in reference and "bootstrap" in reference
    assert "siteops-bootstrap.sh" in combined
    assert "siteops-bootstrap.ps1" in combined
    assert "a separate detached proof for each" in combined


@pytest.mark.parametrize("download_succeeds", [False, True])
def test_installer_guide_waits_for_full_https_download_before_execution(tmp_path, download_succeeds):
    block = _block(_section("### Bootstrap from HTTPS"), "bash")
    block = block.replace("<approved-Site-Ops-release>", "siteops/v1.0.0b1").replace(
        "<full-source-commit>", "c" * 40,
    ).replace("<approved-release-tag>", "siteops/v1.0.0b1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "curl", """#!/usr/bin/env bash
[[ "$*" == *"--proto =https"* && "$*" == *"--tlsv1.2"* ]] || exit 99
[[ "$*" == *"releases/download/siteops%2Fv1.0.0b1/siteops-bootstrap.sh"* ]] || exit 99
while (($#)); do
  if [[ "$1" == "--output" ]]; then target="$2"; shift 2; else shift; fi
done
[[ "$TEST_DOWNLOAD_SUCCEEDS" == 1 ]] || exit 7
printf 'printf SCRIPT_RAN\\\\n\\n' > "$target"
""")
    result = run_script(
        block, tmp_path, {
            "TMPDIR": bash_path(tmp_path),
            "TEST_DOWNLOAD_SUCCEEDS": "1" if download_succeeds else "0",
        },
    )
    assert (result.returncode == 0) is download_succeeds, result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is download_succeeds


def test_preview_migration_describes_rejections_not_aliases():
    migration = (GUIDE.parent / "migrating.md").read_text(encoding="utf-8")
    for old, replacement in (
        ("`validate <manifest> --plan`", "`plan <manifest> --describe`"),
        ("`deploy <manifest> --dry-run`", "`plan <manifest>`"),
        ("`--offline`", "`--offline-content`"),
    ):
        assert old in migration and replacement in migration
    assert "unrecognized arguments" in migration
    assert "Noninteractive deployment" in migration
    assert "source.profile-expired" in migration
    packages = (GUIDE.parent / "workspace-packages.md").read_text(encoding="utf-8")
    assert "`uv tool install <wheel-url>`" in packages
    assert "[installation guide](install-siteops.md)" in packages


def _section(heading: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split(heading + "\n", 1)[1]
    return re.split(r"\n#{1,3} ", section, maxsplit=1)[0]


def _block(section: str, language: str) -> str:
    return re.search(rf"```{language}\n(.*?)\n```", section, re.DOTALL).group(1)


@pytest.mark.parametrize("heading", [
    "### Bootstrap from HTTPS", "### Verify the bootstrap script",
])
def test_pasted_bootstrap_blocks_stop_before_execution_after_a_failure(heading):
    section = _section(heading)
    windows = _block(section, "powershell")
    assert windows.lstrip().startswith('& {\n')
    assert '$ErrorActionPreference = "Stop"' in windows
    assert windows.index('$ErrorActionPreference = "Stop"') < windows.index('curl.exe')
    assert windows.rstrip().endswith('}')
    bash = _block(section, "bash")
    assert bash.lstrip().startswith('(\n')
    assert 'set -euo pipefail' in bash
    assert bash.index('set -euo pipefail') < bash.index('curl ')
    assert bash.rstrip().endswith(')')


@pytest.mark.parametrize("heading", [
    "### Bootstrap from HTTPS", "### Verify the bootstrap script",
])
def test_bootstrap_bash_examples_parse_without_executing_external_tools(tmp_path, heading):
    section = _section(heading)
    body = _block(section, "bash")
    script = tmp_path / "example.sh"
    script.write_text(body, encoding="utf-8", newline="\n")
    bash = shutil.which("bash")
    if sys.platform == "win32":
        from tests.shell_helpers import required_bash

        bash = str(required_bash())
    result = subprocess.run(
        [bash, "-n", bash_path(script)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--source-commit" in body and "--release" in body
    assert "--tlsv1.2" in body


@pytest.mark.parametrize("heading", [
    "### Bootstrap from HTTPS", "### Verify the bootstrap script",
])
def test_bootstrap_windows_examples_parse(tmp_path, heading):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable.")
    body = _block(_section(heading), "powershell")
    example = tmp_path / "example.ps1"
    example.write_text(body, encoding="utf-8")
    parser = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile($env:TEST_SCRIPT,"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1}"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", parser],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "TEST_SCRIPT": str(example)},
    )
    assert result.returncode == 0, result.stderr
    assert "--tlsv1.2" in body


@pytest.mark.parametrize("verified", [False, True])
def test_verified_bootstrap_guide_runs_script_only_after_matching_proof(tmp_path, verified):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "curl", """#!/usr/bin/env bash
while (($#)); do
  if [[ "$1" == "--output" ]]; then output="$2"; shift 2; else shift; fi
done
printf 'echo SCRIPT_RAN\\n' > "$output"
""")
    write_executable(bin_dir / "gh", """#!/usr/bin/env bash
[[ "$1 $2" == "attestation verify" ]] || exit 99
printf '%s\\n' "$@" > "$TEST_GH_ARGUMENTS"
printf '%s\\n' "$TEST_VERIFIED"
""")
    log = tmp_path / "gh-arguments.txt"
    result = run_script(
        _block(_section("### Verify the bootstrap script"), "bash"),
        tmp_path, {
            "TEST_GH_ARGUMENTS": bash_path(log),
            "TEST_VERIFIED": "true" if verified else "false",
        },
    )
    assert (result.returncode == 0) is verified
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "--cert-identity" in log.read_text(encoding="utf-8")
    assert "--source-digest" in log.read_text(encoding="utf-8")
    assert "--signer-digest" in log.read_text(encoding="utf-8")
    assert "--bundle" in log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
@pytest.mark.parametrize(("heading", "verified", "install_exit"), [
    ("### Bootstrap from HTTPS", True, 0),
    ("### Bootstrap from HTTPS", True, 7),
    ("### Verify the bootstrap script", False, 0),
    ("### Verify the bootstrap script", True, 0),
    ("### Verify the bootstrap script", True, 7),
])
def test_windows_bootstrap_guide_stops_on_proof_or_installation_failure(
    tmp_path, heading, verified, install_exit,
):
    section = _section(heading)
    body = _block(section, "powershell").replace(
        "<approved-release-tag>", "siteops/v1.0.0b1",
    ).replace("<full-source-commit>", "c" * 40)
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", "c" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not verified:
        observation["verificationResult"]["signature"]["certificate"]["runnerEnvironment"] = "PRIVATE_WRONG"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps([observation]), encoding="utf-8")
    script = tmp_path / "verified-example.ps1"
    script.write_text(
        """function icacls { $global:LASTEXITCODE = 0 }
function curl.exe {
    $target = $args[[array]::IndexOf($args, '--output') + 1]
    Set-Content -LiteralPath $target -Value 'test bytes'
    $global:LASTEXITCODE = 0
}
function gh.exe {
    Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw
    $global:LASTEXITCODE = 0
}
function powershell.exe { 'SCRIPT_RAN'; $global:LASTEXITCODE = [int]$env:TEST_INSTALL_EXIT }
""" + body, encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env={
            **os.environ, "TEMP": str(tmp_path), "TEST_EVIDENCE": str(evidence),
            "TEST_INSTALL_EXIT": str(install_exit),
        },
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is (verified and install_exit == 0), result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


def test_windows_guide_requires_copied_launcher_and_preserves_pipx_migration():
    guide = GUIDE.read_text(encoding="utf-8")
    assert "regular copied `siteops.exe`" in guide
    assert "rejects file symlinks" in guide
    assert "`pipx uninstall siteops`" in guide
    assert "pipx install siteops" not in guide

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
INSTALL_HEADING = "### 1. Install Site Ops and approve the official source\n"
CURL_FLAGS = [
    "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
    "--tlsv1.2",
]


def test_root_quickstart_connects_install_to_aio_without_hiding_fleet_use():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    journey = readme.split("## Quick start\n", 1)[1].split("\n## Browse deployment choices", 1)[0]
    prose = " ".join(journey.split())
    for phrase in (
        "https://github.com/Azure/digital-ops-scale-kit/releases)",
        "replace `<release>` below with that tag",
        "Run the commands from your home directory or another directory that only you can change.",
        "Linux x64, including Azure Cloud Shell and GitHub Codespaces",
        "Windows x64, Windows PowerShell",
        "**Already have uv**",
        "**Workspace content**",
        "A release that contains only content links to the engine release to use instead.",
        "docs/install-siteops.md#choose-an-installation-route",
        "To verify the script before it runs, or to pin a reviewed source commit",
        "https://github.com/cli/cli#installation",
        "Ubuntu 24.04's, are older",
        "only from a source you approve",
        "approves the `official` content source for 30 days",
        "can renew with `siteops source enroll official`",
        "signed by the publisher's release workflow from its main branch",
        "runs it before anything is authenticated",
        "locked because official releases are immutable",
        "pins the commit discovered from the verified engine provenance",
        "use the release notes command described in the [installation guide]",
        "does not sign you in",
        "asks before it changes anything",
        "--yes",
        "docs/guided-inputs.md#check-the-result",
        "docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance",
        "enableSecretSync=true",
        "siteops --approved-source official project pin",
        "docs/targeting.md",
        "docs/getting-started.md",
    ):
        assert phrase in prose
    assert "releases/latest" not in journey
    # Provenance detail stays in the installation guide, outside the first reading path.
    assert "attestation" not in journey
    first = next(
        line for line in journey.splitlines()
        if line.startswith("siteops deploy aio-install --source")
    )
    assert shlex.split(first) == [
        "siteops", "deploy", "aio-install", "--source", "official@<release>",
        "--input", "cluster=<Arc-cluster-resource-ID>",
    ]
    assert journey.index(first) < journey.index("siteops --approved-source official project pin")
    assert (
        journey.index("Before you start, have:")
        < journey.index(INSTALL_HEADING)
        < journey.index("### 2. Deploy AIO")
        < journey.index(first)
        < journey.index("### 3. Scale out on the same model")
    )
    assert journey.index("#choose-an-installation-route") < journey.index(first)
    assert "plan aio-install -l name=plant-two,name=plant-three" in journey
    assert "siteops inputs aio-install --example" not in journey
    assert "--read-resources" not in journey
    install = journey.split(INSTALL_HEADING, 1)[1].split("\n### 2.", 1)[0]
    assert "| Your machine |" not in install
    assert "--source-commit" not in _block(install, "bash")
    assert "-SourceCommit" not in _block(install, "powershell")
    # Only the recommendation names a release. The commands stay the same between releases.
    for language in ("bash", "powershell"):
        assert not re.search(r"\bv[0-9]+\.[0-9]+", _block(install, language))
    recommended = re.search(
        r"Recommended for this quickstart: \[([^\]<>]+)\]"
        r"\(https://github\.com/Azure/digital-ops-scale-kit/releases/tag/([^)\s]+)\)",
        prose,
    )
    assert recommended and recommended[1] == recommended[2].replace("%2F", "/")
    assert "**Install Site Ops** and **Workspace content**" in prose
    assert "**Already have uv** section, then run `siteops source enroll official`" in prose
    assert "--source-commit" not in install and "-SourceCommit" not in install
    assert "### 4." not in journey


def test_readme_bash_install_command_uses_the_real_optional_commit_interface():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split(INSTALL_HEADING, 1)[1].split("\n### 2.", 1)[0]
    lexer = shlex.shlex(_block(section, "bash").replace("\\\n", ""), posix=True, punctuation_chars=";&")
    lexer.whitespace_split = True
    tokens = list(lexer)
    separator = tokens.index("&&")
    commands = [tokens[:separator], tokens[separator + 1:]]
    assert commands == [
        ["curl", *CURL_FLAGS, "--output", "siteops-bootstrap.sh",
         "https://github.com/Azure/digital-ops-scale-kit/releases/download/<release>/siteops-bootstrap.sh"],
        ["bash", "siteops-bootstrap.sh", "--release", "<release>", "--enroll-source", "official"],
    ]
    source = (GUIDE.parent.parent / "scripts" / "bootstrap" / commands[1][1]).read_text(encoding="utf-8")
    options = source.split("while (($#)); do\n", 1)[1].split("\ndone\n", 1)[0]
    assert set(commands[1][2::2]) <= set(re.findall(r"--[a-z-]+", options))
    assert "if $commit_supplied; then\n" in source
    assert "discover_source_commit" in source


@pytest.mark.parametrize("download_exit", [0, 22])
def test_readme_bash_download_failure_never_executes_partial_script(tmp_path, download_exit):
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split(INSTALL_HEADING, 1)[1].split("\n### 2.", 1)[0]
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "curl", """#!/usr/bin/env bash
[[ "$*" == "--fail --silent --show-error --location --proto =https --proto-redir =https --tlsv1.2 --output siteops-bootstrap.sh https://github.com/Azure/digital-ops-scale-kit/releases/download/v1.2.3/siteops-bootstrap.sh" ]] || exit 99
printf 'echo BOOTSTRAP_RAN\\n' > siteops-bootstrap.sh
exit "$TEST_DOWNLOAD_EXIT"
""")
    result = run_script(
        _block(section, "bash").replace("<release>", "v1.2.3"),
        tmp_path, {"TEST_DOWNLOAD_EXIT": str(download_exit)}, shell_options=(),
    )
    assert (result.returncode == 0) is (download_exit == 0)
    assert ("BOOTSTRAP_RAN" in result.stdout) is (download_exit == 0)


def test_readme_bash_install_command_pasted_unedited_fails_as_a_download(tmp_path):
    # Unquoted, `<release>` would be read as a shell redirection rather than a URL.
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split(INSTALL_HEADING, 1)[1].split("\n### 2.", 1)[0]
    (tmp_path / "bin").mkdir()
    write_executable(tmp_path / "bin" / "curl", """#!/usr/bin/env bash
printf '%s\\n' "${@: -1}" > curl-url.txt
exit 22
""")
    result = run_script(_block(section, "bash"), tmp_path, {}, shell_options=())
    assert result.returncode == 22
    assert (tmp_path / "curl-url.txt").read_text(encoding="utf-8").strip() == (
        "https://github.com/Azure/digital-ops-scale-kit/releases/download/<release>/siteops-bootstrap.sh"
    )
    assert "No such file" not in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="The Windows README command uses native PowerShell 5.1.")
def test_readme_powershell_install_command_uses_local_file_and_real_options(tmp_path):
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split(INSTALL_HEADING, 1)[1].split("\n### 2.", 1)[0]
    command = tmp_path / "readme.ps1"
    command.write_text(_block(section, "powershell"), encoding="utf-8")
    parser = r"""
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:TEST_COMMAND,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'The README command does not parse.'}
$commands=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.CommandAst]},$true) |
    ForEach-Object { [pscustomobject]@{
        name=$_.GetCommandName()
        text=$_.Extent.Text
        arguments=@($_.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })
    } })
$script=[Management.Automation.Language.Parser]::ParseFile($env:TEST_BOOTSTRAP,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'The bootstrap does not parse.'}
@{
    commands=$commands
    options=@($script.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
    guard=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.IfStatementAst]},$true))[0].Clauses[0].Item1.Extent.Text
} | ConvertTo-Json -Depth 8 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", parser],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
        env={**os.environ, "TEST_COMMAND": str(command),
             "TEST_BOOTSTRAP": str(GUIDE.parent.parent / "scripts" / "bootstrap" / "siteops-bootstrap.ps1")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert [item["name"] for item in parsed["commands"]] == ["curl.exe", "powershell.exe"]
    assert parsed["commands"][0]["arguments"] == [
        "--fail", "--silent", "--show-error", "--location", "--proto", "'=https'", "--proto-redir", "'=https'",
        "--tlsv1.2", "--output", "siteops-bootstrap.ps1",
        '"https://github.com/Azure/digital-ops-scale-kit/releases/download/<release>/siteops-bootstrap.ps1"',
    ]
    assert parsed["guard"] == "$LASTEXITCODE -eq 0"
    assert parsed["commands"][1]["arguments"] == [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r".\siteops-bootstrap.ps1", "-Release", '"<release>"',
        "-EnrollSource", "official",
    ]
    assert {"Release", "SourceCommit", "EnrollSource"} <= set(parsed["options"])


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
    assert "siteops --approved-source official --project ./factory browse aio-install" in readme
    assert "require a local checkout" in readme
    assert "does not sign you in" in readme
    assert "can renew with `siteops source enroll official`." in " ".join(readme.split())
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
        "user private group", "Python managed by uv", "UV_PYTHON_INSTALL_MIRROR", "Codespace", "k3d",
        "connected to Azure Arc",
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
    assert "```text\nsiteops source enroll official\n```" in projects
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


def test_https_bootstrap_guide_defers_to_the_generated_release_command():
    section = _section("### Bootstrap from HTTPS")
    prose = " ".join(section.split())
    assert "<approved-release-tag>" not in section
    assert _block(section, "bash").splitlines()[-1] == 'bash siteops-bootstrap.sh --release "<release>"'
    assert "Assemble an approved selection manually" not in GUIDE.read_text(encoding="utf-8")
    for phrase in (
        "`Install Site Ops` section",
        "checks the exact size and SHA-256",
        "do not run `gh auth login` or `az login`",
        "download public release assets anonymously",
        "execution policy only for the child process",
        "approved managed installation path",
    ):
        assert phrase in prose
    for phrase in (
        "40 lowercase hexadecimal", "existing verification again", "locked because official releases are immutable",
        "no signed tag field", "authenticated bundle metadata and wheel bytes",
        "preview verifies discovery and the pinned proof", "creates no Site Ops files",
        "refuses if the evidence is absent", "does not check the script's digest or publisher independently",
        "publication requirement, not a bootstrap check",
        "Normal installation without a commit always downloads the evidence from the release location into private staging",
        "Only the preview reads evidence beside the script", "refuses rather than downloading",
        "the release download location selects which signed reference is used, as for the engine route",
        "commit from a trusted channel", "Keep that commit rather than enabling discovery",
    ):
        assert phrase in prose
    assert "Hosted qualification" not in section and "live CI cell" not in section
    assert "Normal installation also accepts" not in section
    assert "locally staged evidence" not in section
    assert "Download fully before running. Never pipe a remote response into a shell." in prose


def test_short_guide_downloads_enforce_the_same_https_policy_as_release_notes():
    section = _section("### Bootstrap from HTTPS")
    bash = shlex.split(_block(section, "bash").replace("\\\n", "").split("&&", 1)[0])
    assert bash == [
        "curl", *CURL_FLAGS, "--output", "siteops-bootstrap.sh",
        "https://github.com/Azure/digital-ops-scale-kit/releases/download/<release>/siteops-bootstrap.sh",
    ]
    windows = _block(section, "powershell")
    assert "curl.exe --fail --silent --show-error --location --proto '=https' --proto-redir '=https'" in windows
    assert "--tlsv1.2 --output siteops-bootstrap.ps1" in windows
    content = _section("### Install the engine selected by a content release")
    assert "If you authenticated the script independently, keep the source commit" in " ".join(content.split())
    assert "locally staged reference" not in content
    assert "With an explicit commit, repeat installation" in " ".join(content.split())


def test_aio_migration_distinguishes_the_b6_pin_from_the_optional_2608_release():
    migration = (GUIDE.parent / "migrating.md").read_text(encoding="utf-8")
    section = migration.split("### Default AIO release\n", 1)[1].split("\n### ", 1)[0]
    prose = " ".join(section.split())
    assert re.findall(r'properties\.aioRelease: "(\d+)"', section) == ["2607", "2608"]
    assert 'Pin `properties.aioRelease: "2607"` to stay on the v1.0.0b6 release' in prose
    assert '`properties.aioRelease: "2608"` to select that release' in prose
    assert "Moving off 2607 adds the OPC UA connector template unless OPC UA is disabled" in prose
    assert "`Disabled`" in section and "`connectors`" in section
    assert "for in-place upgrades of existing instances instead of rerunning installation" in prose


def test_preview_migration_describes_rejections_not_aliases():
    migration = (GUIDE.parent / "migrating.md").read_text(encoding="utf-8")
    for old, replacement in (
        ("`validate <manifest> --plan`", "`plan <manifest> --describe`"),
        ("`deploy <manifest> --dry-run`", "`plan <manifest>`"),
    ):
        assert old in migration and replacement in migration
    assert "unrecognized arguments" in migration
    assert "Noninteractive deployment" in migration
    packages = (GUIDE.parent / "workspace-packages.md").read_text(encoding="utf-8")
    assert "`uv tool install <wheel-url>`" in packages
    assert "[installation guide](install-siteops.md)" in packages


def _section(heading: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split(heading + "\n", 1)[1]
    return re.split(r"\n#{1,3} ", section, maxsplit=1)[0]


def _block(section: str, language: str) -> str:
    return re.search(rf"```{language}\n(.*?)\n```", section, re.DOTALL).group(1)


def test_pasted_bootstrap_blocks_stop_before_execution_after_a_failure():
    section = _section("### Verify the bootstrap script")
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


def test_bootstrap_bash_examples_parse_without_executing_external_tools(tmp_path):
    section = _section("### Verify the bootstrap script")
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


def test_bootstrap_windows_examples_parse(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable.")
    body = _block(_section("### Verify the bootstrap script"), "powershell")
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
@pytest.mark.parametrize(("verified", "install_exit"), [(False, 0), (True, 0), (True, 7)])
def test_windows_bootstrap_guide_stops_on_proof_or_installation_failure(
    tmp_path, verified, install_exit,
):
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", "c" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not verified:
        observation["verificationResult"]["signature"]["certificate"]["runnerEnvironment"] = "PRIVATE_WRONG"
    result = _run_windows_verified_guide(tmp_path, observation, install_exit)
    assert (result.returncode == 0) is (verified and install_exit == 0), result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


def _run_windows_verified_guide(tmp_path, observation, install_exit=0):
    body = _block(_section("### Verify the bootstrap script"), "powershell").replace(
        "<approved-release-tag>", "siteops/v1.0.0b1",
    ).replace("<full-source-commit>", "c" * 40)
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
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env={
            **os.environ, "TEMP": str(tmp_path), "TEST_EVIDENCE": str(evidence),
            "TEST_INSTALL_EXIT": str(install_exit),
        },
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="The guide uses native PowerShell 5.1 JSON comparisons.")
@pytest.mark.parametrize("suffix", ["", "\u0000", "\u00ad", "\u200b"], ids=["exact", "nul", "soft-hyphen", "zero-width"])
@pytest.mark.parametrize("field", ["sourceRepositoryURI", "mediaType"])
def test_windows_verified_guide_refuses_ignorable_json_characters(tmp_path, suffix, field):
    item = verified_observation(
        "Azure/digital-ops-scale-kit", "c" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    target = item["verificationResult"] if field == "mediaType" else item["verificationResult"]["signature"]["certificate"]
    target[field] += suffix
    result = _run_windows_verified_guide(tmp_path, item)
    assert (result.returncode == 0) is (suffix == ""), result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is (suffix == "")


def test_windows_guide_requires_copied_launcher_and_preserves_pipx_migration():
    guide = GUIDE.read_text(encoding="utf-8")
    assert "regular copied `siteops.exe`" in guide
    assert "rejects file symlinks" in guide
    assert "`pipx uninstall siteops`" in guide
    assert "pipx install siteops" not in guide

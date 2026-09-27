"""Exercise the native installation guide's command and path contracts."""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from tests.shell_helpers import bash_path, run_script, write_executable
from tests.verification_helpers import verified_observation

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


GUIDE = Path(__file__).resolve().parent.parent / "docs" / "install-siteops.md"


def test_root_quickstart_connects_install_to_aio_without_hiding_fleet_use():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    journey = readme.split("## Quick start\n", 1)[1].split("\n## Browse deployment choices", 1)[0]
    for phrase in (
        "docs/install-siteops.md#bootstrap-from-https",
        "docs/install-siteops.md#verify-the-bootstrap-script",
        "siteops --approved-source official project pin",
        "inputs aio-install --example",
        "plan aio-install",
        "deploy aio-install",
        "--read-resources",
        "docs/targeting.md",
        "docs/getting-started.md",
    ):
        assert phrase in journey
    assert journey.index("project pin") < journey.index("inputs aio-install --example")
    assert journey.index("inputs aio-install --example") < journey.index("plan aio-install")
    assert journey.index("plan aio-install") < journey.index("deploy aio-install")
    assert journey.index("deploy aio-install") < journey.index(
        "plan aio-install -l name=plant-two,name=plant-three"
    )
    assert "generated example includes `cluster: null`" in " ".join(journey.split())
    for verb in ("plan", "deploy"):
        command = next(
            line for line in journey.splitlines() if f" {verb} aio-install " in line
        )
        assert "--approved-source official" in command
        assert "--input-file aio-inputs.yaml" in command
        assert "--read-resources" in command
    assert "&&\n    bash \"$script\"" in journey


def test_quickstart_distinguishes_unpublished_bootstrap_and_checkout_browsing():
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "not yet published" in readme
    assert "siteops --approved-source official --project factory browse aio-install" in readme
    assert "require a local checkout" in readme
    assert "az login" in readme


def test_hosted_bootstrap_guidance_matches_managed_host_behavior():
    guide = GUIDE.read_text(encoding="utf-8")
    bash = (GUIDE.parent.parent / "scripts/bootstrap/siteops-bootstrap.sh").read_text(
        encoding="utf-8",
    )
    powershell = (GUIDE.parent.parent / "scripts/bootstrap/siteops-bootstrap.ps1").read_text(
        encoding="utf-8",
    )
    for phrase in (
        "Azure Cloud Shell", "Azure Linux 3", "without `sudo`", "virtualenv",
        "approved HTTPS Python index", "Codespace", "k3d", "Arc-connected",
    ):
        assert phrase in guide
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
    assert "--input aioRelease=2608 --read-resources" in guided
    assert "--input aioRelease=2607 --read-resources" in guided
    assert "--input siteName=plant-2608" in guided
    assert "--input siteName=plant-2607" in guided
    assert "deploy aio-install --input siteName=plant-2608" in guided
    assert "deploy aio-install --input siteName=plant-2607" in guided
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
def test_root_quickstart_waits_for_full_https_download_before_execution(tmp_path, download_succeeds):
    readme = (GUIDE.parent.parent / "README.md").read_text(encoding="utf-8")
    journey = readme.split("## Quick start\n", 1)[1].split("\n## Browse deployment choices", 1)[0]
    block = re.search(r"```bash\n(.*?)\n```", journey, re.DOTALL).group(1)
    block = block.replace("<approved-Site-Ops-release>", "siteops/v1.0.0b1").replace(
        "<full-source-commit>", "c" * 40,
    )
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
    assert not list(tmp_path.glob("tmp.*"))


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


def test_online_transition_selects_a_release_wheel_instead_of_an_index_package():
    section = _section("## Select another build, repair, or remove")
    manifest = tomllib.loads(_block(section, "toml"))
    [declaration] = manifest["dependency-groups"]["siteops"]
    requirement = Requirement(declaration)
    assert requirement.name == "siteops"
    assert requirement.url.startswith(
        "https://github.com/Azure/digital-ops-scale-kit/releases/download/",
    )
    assert requirement.url.endswith("/siteops-<version>-py3-none-any.whl")
    assert not requirement.specifier
    assert "lock" not in manifest["tool"]["pipx"]["tools"]["siteops"]
    assert "PIP_ONLY_BINARY=:all:" in section
    assert "PIPX_FETCH_PYTHON=never" in section


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
@pytest.mark.parametrize("verified", [False, True])
def test_verified_bootstrap_windows_requires_the_certificate_before_execution(tmp_path, verified):
    section = _section("### Verify the bootstrap script")
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
function powershell.exe { 'SCRIPT_RAN'; $global:LASTEXITCODE = 0 }
""" + body, encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env={**os.environ, "TEMP": str(tmp_path), "TEST_EVIDENCE": str(evidence)},
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is verified, result.stdout + result.stderr
    assert ("SCRIPT_RAN" in result.stdout) is verified
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("state", [
    "verified", "rejected", "existing", "subjectAlternativeName", "issuer",
    "sourceRepositoryURI", "sourceRepositoryDigest", "sourceRepositoryRef",
    "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
    "runner-case", "runner-type", "media-type",
])
def test_guide_authenticates_before_creating_retained_files(tmp_path, shell, state):
    if shell == "powershell" and (sys.platform != "win32" or not shutil.which("pwsh")):
        pytest.skip("The PowerShell retention example uses Windows identity and ACL tools.")
    download = tmp_path / "download with spaces"
    download.mkdir()
    archive = download / "siteops-install.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("payload.txt", "authenticated test payload")
    (download / "siteops-install.zip.attestation.jsonl").write_bytes(b"opaque fixture proof")
    identity = hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "data" / "siteops" / "bundles" / identity
    if state == "existing":
        destination.mkdir(parents=True)
        (destination / "operator.txt").write_text("preserve")
    body = _block(_section("### Authenticate and retain the bundle"), shell)
    body = body.replace(
        "<download directory>", str(download) if shell == "powershell" else bash_path(download),
    ).replace("<full source commit from the selected official release>", "a" * 40)
    code = 9 if state == "rejected" else 0
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", "a" * 40, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    observations = [observation]
    if state not in {"verified", "rejected", "existing"}:
        changed = copy.deepcopy(observation)
        certificate = changed["verificationResult"]["signature"]["certificate"]
        if state == "runner-case":
            certificate["runnerEnvironment"] = "SELF-HOSTED"
        elif state == "runner-type":
            certificate["runnerEnvironment"] = ["self-hosted"]
        elif state == "media-type":
            changed["verificationResult"]["mediaType"] = [changed["verificationResult"]["mediaType"]]
        else:
            certificate[state] = "PRIVATE_WRONG"
        observations.append(changed)
    evidence = tmp_path / "observations.json"
    evidence.write_text(json.dumps(observations), encoding="utf-8")
    arguments = tmp_path / "arguments.json"
    if shell == "powershell":
        script = tmp_path / "retain.ps1"
        script.write_text(
            f"""function gh {{
    ConvertTo-Json -InputObject @($args) -Compress | Set-Content -LiteralPath $env:TEST_ARGUMENTS
    Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw
    $global:LASTEXITCODE = {code}
}}
""" + body, encoding="utf-8",
        )
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-File", str(script)], cwd=tmp_path,
            env={
                **os.environ, "LOCALAPPDATA": str(tmp_path / "data"),
                "TEST_ARGUMENTS": str(arguments), "TEST_EVIDENCE": str(evidence),
            },
            capture_output=True, text=True, timeout=30,
        )
    else:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        write_executable(bin_dir / "gh", f"""#!/usr/bin/env bash
[[ "$1 $2" == "attestation verify" ]] || exit 99
printf '%s\\n' "$@" > "$TEST_ARGUMENTS"
cat "$TEST_EVIDENCE"
exit {code}
""")
        result = run_script(
            'python3() { "$TEST_PYTHON" "$@"; }\n' + body,
            tmp_path, {
                "TEST_PYTHON": Path(sys.executable).as_posix(),
                "TEST_ARGUMENTS": bash_path(arguments), "TEST_EVIDENCE": bash_path(evidence),
                "XDG_DATA_HOME": bash_path(tmp_path / "data"),
            },
        )
    argv = (
        json.loads(arguments.read_text(encoding="utf-8-sig"))
        if shell == "powershell" else arguments.read_text().splitlines()
    )
    expected = {
        "--repo": "Azure/digital-ops-scale-kit",
        "--cert-identity": "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main",
        "--source-ref": "refs/heads/main", "--source-digest": "a" * 40, "--signer-digest": "a" * 40,
        "--cert-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--predicate-type": "https://slsa.dev/provenance/v1", "--hostname": "github.com",
        "--digest-alg": "sha256", "--format": "json",
    }
    assert argv[:2] == ["attestation", "verify"]
    assert argv[2] == (str(archive) if shell == "powershell" else bash_path(archive))
    assert argv[argv.index("--bundle") + 1] == argv[2] + ".attestation.jsonl"
    assert "--deny-self-hosted-runners" not in argv
    for flag, value in expected.items():
        assert argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value
    if state == "verified":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (destination / "payload.txt").read_text() == "authenticated test payload"
    else:
        assert result.returncode != 0
        assert not (destination / "payload.txt").exists()
        if state != "existing":
            assert not destination.exists()
        else:
            assert (destination / "operator.txt").read_text() == "preserve"
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.parametrize("language", ["powershell", "bash"])
def test_locked_install_explicitly_sets_the_path_returned_from_scoped_extraction(language):
    section = _section("### Install from the verified lock")
    command = _block(section, language)
    variable = "$bundle" if language == "powershell" else "bundle"
    assert command.startswith(variable + (" = " if language == "powershell" else "="))
    assert "<retained bundle directory printed by the extraction step>" in command
    assert 'pipx install siteops --lock "$bundle' in command


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\owned tools\wheels", "file:///C:/owned%20tools/wheels"),
        ("/tmp/owned tools/wheels", "file:///tmp/owned%20tools/wheels"),
    ],
)
def test_powershell_wheelhouse_uri_handles_windows_and_unix_paths(tmp_path, path, expected):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("Executing the PowerShell guide requires pwsh.")
    body = _block(_section("### Provision the pipx backend that reads the lock"), "powershell")
    assignment = next(line for line in body.splitlines() if line.strip().startswith("$wheelhouse ="))
    script = tmp_path / "wheelhouse.ps1"
    script.write_text(
        "param([string]$tools)\n" + assignment + "\n$wheelhouse\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script), path],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("download_exit", [0, 23])
def test_powershell_backend_recipe_preserves_space_paths_and_stops_on_failure(
    tmp_path, download_exit,
):
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("Executing the PowerShell guide requires pwsh.")
    temporary = tmp_path / "temporary files with spaces"
    temporary.mkdir()
    capture = tmp_path / "pipx.json"
    body = _block(_section("### Provision the pipx backend that reads the lock"), "powershell")
    script = tmp_path / "backend.ps1"
    script.write_text(
        f"""
function python {{ $global:LASTEXITCODE = {download_exit} }}
function pipx {{
    ConvertTo-Json -InputObject @($args) -Compress |
        Set-Content -LiteralPath $env:SITEOPS_GUIDE_CAPTURE -Encoding utf8
    $global:LASTEXITCODE = 0
}}
""" + body,
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(script)],
        cwd=tmp_path,
        env={**os.environ, "TEMP": str(temporary), "SITEOPS_GUIDE_CAPTURE": str(capture)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if download_exit:
        assert result.returncode != 0
        assert "backend wheel could not be downloaded" in result.stderr
        assert not capture.exists()
    else:
        assert result.returncode == 0, result.stderr
        arguments = json.loads(capture.read_text(encoding="utf-8-sig"))
        assert arguments[:2] == ["upgrade-shared", "--pip-args"]
        assert "--force-reinstall" in arguments[2]
        uri = arguments[2].split("--find-links=", 1)[1]
        assert uri.startswith("file:///")
        assert "%20" in uri and " " not in uri

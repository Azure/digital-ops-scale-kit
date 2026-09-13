"""Exercise the native installation guide's command and path contracts."""

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

from tests.shell_helpers import bash_path, run_script

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


GUIDE = Path(__file__).resolve().parent.parent / "docs" / "install-siteops.md"


def _section(heading: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split(heading + "\n", 1)[1]
    return re.split(r"\n#{1,3} ", section, maxsplit=1)[0]


def _block(section: str, language: str) -> str:
    return re.search(rf"```{language}\n(.*?)\n```", section, re.DOTALL).group(1)


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


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("state", ["verified", "rejected", "existing"])
def test_guide_authenticates_before_creating_retained_files(tmp_path, shell, state):
    if shell == "powershell" and (sys.platform != "win32" or not shutil.which("pwsh")):
        pytest.skip("The PowerShell retention example uses Windows identity and ACL tools.")
    download = tmp_path / "download with spaces"
    download.mkdir()
    archive = download / "siteops-install.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("payload.txt", "authenticated test payload")
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
    if shell == "powershell":
        script = tmp_path / "retain.ps1"
        script.write_text(
            f"function gh {{ $global:LASTEXITCODE = {code} }}\n" + body, encoding="utf-8",
        )
        result = subprocess.run(
            [shutil.which("pwsh"), "-NoProfile", "-File", str(script)], cwd=tmp_path,
            env={**os.environ, "LOCALAPPDATA": str(tmp_path / "data")},
            capture_output=True, text=True, timeout=30,
        )
    else:
        result = run_script(
            f'gh() {{ return {code}; }}\npython3() {{ "$TEST_PYTHON" "$@"; }}\n' + body,
            tmp_path, {
                "TEST_PYTHON": Path(sys.executable).as_posix(),
                "XDG_DATA_HOME": bash_path(tmp_path / "data"),
            },
        )
    if state == "verified":
        assert result.returncode == 0, result.stdout + result.stderr
        assert (destination / "payload.txt").read_text() == "authenticated test payload"
    else:
        assert result.returncode != 0
        assert not (destination / "payload.txt").exists()
        if state == "rejected":
            assert not destination.exists()
        else:
            assert (destination / "operator.txt").read_text() == "preserve"


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

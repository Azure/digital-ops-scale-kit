"""Check the two platform entry scripts without contacting package or cloud services."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.shell_helpers import bash_path, required_bash
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "bootstrap"
SOURCE_SHA = "c" * 40


def test_private_pipx_venvs_are_created_at_their_retained_paths():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert '"${venv_tool[@]}" "$tools/pipx"' in bash
    assert "$tools/pipx-stage" not in bash
    assert "& $python -m venv $installed" in powershell
    assert "Move-Item -LiteralPath $staged" not in powershell


def test_managed_azure_linux_uses_existing_os_tools_without_sudo():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert "azurelinux:3.0" in bash
    assert 'if [[ "$platform" == azurelinux ]]; then' in bash
    assert "Managed Azure Linux" in bash
    assert "python3 -m virtualenv" in bash
    assert '"${venv_tool[@]}" "$staging/backend-tools"' in bash


def test_pipx_managed_directories_are_isolated_before_package_operations():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert 'PIPX_HOME="$data/pipx"' in bash
    assert 'PIPX_BIN_DIR="$data/bin"' in bash
    assert 'PIPX_SHARED_LIBS="$data/pipx/shared"' in bash
    assert 'PIPX_MAN_DIR="$data/man"' in bash
    assert 'PIPX_COMPLETION_DIR="$data/completions"' in bash
    assert bash.index('export PIPX_HOME PIPX_BIN_DIR PIPX_SHARED_LIBS PIPX_MAN_DIR') < bash.index(
        '"$pipx_bin" list --output json',
    )


def test_bootstrap_requires_configured_secure_index_before_python_download():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert 'pip_configuration="$("$1" -m pip config list 2>/dev/null)"' in bash
    assert "parsed.scheme != \"https\"" in bash
    assert bash.index('  require_approved_python_index "$tools/pipx/bin/python" install\n') < bash.index(
        '"$tools/pipx/bin/python" -m pip install',
    )
    assert bash.rindex(
        '  require_approved_python_index "$staging/backend-tools/bin/python" download\n',
    ) < bash.index(
        '"$staging/backend-tools/bin/python" -m pip download',
    )
    assert '> "$staging/pip-download.log" 2>&1' in bash


def test_windows_bootstrap_checks_python_index_before_tool_downloads():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "function Require-ApprovedPythonIndex(" in powershell
    assert powershell.index("Require-ApprovedPythonIndex $toolPython install") < powershell.index(
        "& $toolPython -m pip install",
    )
    assert powershell.index("Require-ApprovedPythonIndex $backendPython download") < powershell.index(
        "& $backendPython -m pip download",
    )


def _windows_python_index_wrapper(tmp_path: Path) -> Path:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    helper = re.search(r"(?ms)^function Require-ApprovedPythonIndex\([^\n]*\) \{.*?^\}", source)
    assert helper, "The Windows Python feed admission helper is missing."
    wrapper = tmp_path / "check-index.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + helper.group(0) + "\n"
        "$ErrorActionPreference = 'Stop'\n"
        "Require-ApprovedPythonIndex $env:TEST_PYTHON $env:TEST_COMMAND\n"
        "'INDEX_ACCEPTED'\n",
        encoding="utf-8",
    )
    return wrapper


@pytest.mark.skipif(sys.platform != "win32", reason="Native pip config requires Windows PowerShell 5.1.")
@pytest.mark.parametrize(
    ("index", "extra", "find_links", "trusted_host", "accepted"),
    [
        ("https://packages.example.invalid/simple/", "", "", "", True),
        ("", "", "", "", False),
        ("http://packages.example.invalid/simple/", "", "", "", False),
        ("https://packages.example.invalid/simple/", "https://other.example.invalid/simple/", "", "", False),
        ("https://packages.example.invalid/simple/", "", "https://wheels.example.invalid/", "", False),
        ("https://packages.example.invalid/simple/", "", "", "packages.example.invalid", False),
    ],
)
def test_windows_bootstrap_python_index_check(
    tmp_path, index, extra, find_links, trusted_host, accepted,
):
    wrapper = _windows_python_index_wrapper(tmp_path)
    env = {
        **os.environ,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_INDEX_URL": index,
        "PIP_EXTRA_INDEX_URL": extra,
        "PIP_FIND_LINKS": find_links,
        "PIP_TRUSTED_HOST": trusted_host,
        "TEST_PYTHON": sys.executable,
        "TEST_COMMAND": "install",
    }
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("INDEX_ACCEPTED" in result.stdout) is accepted
    assert "packages.example.invalid" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native pip config requires Windows PowerShell 5.1.")
@pytest.mark.parametrize(
    ("command", "global_index", "install_index", "download_index", "accepted"),
    [
        ("download", "https://approved.example.invalid/simple", "https://approved.example.invalid/simple",
         "http://other.example.invalid/simple", False),
        ("download", "http://other.example.invalid/simple", "https://approved.example.invalid/simple",
         None, False),
        ("download", "http://other.example.invalid/simple", "http://other.example.invalid/simple",
         "https://approved.example.invalid/simple", True),
        ("install", "https://approved.example.invalid/simple", "https://approved.example.invalid/simple",
         "http://other.example.invalid/simple", True),
        ("install", "https://approved.example.invalid/simple", "http://other.example.invalid/simple",
         "https://approved.example.invalid/simple", False),
    ],
)
def test_windows_bootstrap_index_check_uses_effective_command_section(
    tmp_path, command, global_index, install_index, download_index, accepted,
):
    wrapper = _windows_python_index_wrapper(tmp_path)
    config = tmp_path / "pip.ini"
    config.write_text(
        "[global]\nindex-url = " + global_index + "\n"
        "[install]\nindex-url = " + install_index + "\n"
        + ("[download]\nindex-url = " + download_index + "\n" if download_index else ""),
        encoding="utf-8",
    )
    env = {
        **os.environ, "PIP_CONFIG_FILE": str(config),
        "TEST_PYTHON": sys.executable, "TEST_COMMAND": command,
    }
    for name in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"):
        env.pop(name, None)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is accepted, "The command-specific index check disagreed."
    assert ("INDEX_ACCEPTED" in result.stdout) is accepted
    assert "example.invalid" not in result.stdout + result.stderr


def test_bash_bootstrap_admits_private_data_root_before_retained_tool_use():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert bash.index('require_private_data_root "$data"') < bash.index(
        'pipx_bin="$(command -v pipx || true)"',
    )


def test_windows_bootstrap_admits_private_data_root_before_retained_tool_use():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "function Require-PrivateDataRoot(" in powershell
    assert powershell.index("Require-PrivateDataRoot $data") < powershell.index(
        "$candidate = Join-Path $env:LOCALAPPDATA 'Programs\\Python\\Python312\\python.exe'",
    )
    assert powershell.index("Require-PrivateDataRoot $data") < powershell.index(
        "if (Test-Path -LiteralPath $privatePipx -PathType Leaf)",
    )


def test_windows_bootstrap_owns_new_data_root_before_private_acl_and_admission():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    create = powershell.split("function Require-PrivateDataRoot(", 1)[1].split(
        "function Require-PrivateExecutablePath(", 1,
    )[0].split("if (-not $exists)", 1)[1]
    assert '& icacls.exe $Path /setowner "*$sid"' in create
    assert create.index('& icacls.exe $Path /setowner "*$sid"') < create.index(
        '& icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F"',
    )
    assert "if (-not $owned) {" in create
    assert "Reject 'ROOT_DATA_OWNER'" in create


def test_windows_bootstrap_checks_managed_executables_before_running_them():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert powershell.index("Require-PrivateExecutablePath $candidate $env:LOCALAPPDATA") < (
        powershell.index("$candidateVersion = & $candidate -c")
    )
    assert powershell.index("Require-PrivateExecutablePath $pipx $data") < (
        powershell.index("$pipxVersion = if ($pipx)")
    )
    assert powershell.index("Require-PrivateExecutablePath $toolPython $data") < (
        powershell.index("Require-ApprovedPythonIndex $toolPython install")
    )
    assert powershell.index("Require-PrivateExecutablePath $backendPython $download") < (
        powershell.index("Require-ApprovedPythonIndex $backendPython download")
    )
    assert powershell.index(
        "Require-PrivateExecutablePath $expectedCommand $binDir $expectedTarget $pipxHome",
    ) < (
        powershell.index("$siteops = Native 'siteops.exe'")
    )
    assert "Join-Path $pipxHome 'venvs\\siteops\\Scripts\\siteops.exe'" in powershell
    assert "$siteops = $validatedTarget" in powershell
    assert powershell.index("$siteops = $validatedTarget") < powershell.index("& $siteops --version")


def _windows_private_root_wrapper(tmp_path: Path, setup: str = "") -> Path:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    helper = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    assert helper, "The Windows private data-root helper is missing."
    wrapper = tmp_path / "check-data-root.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + setup
        + helper.group(0) + "\n"
        "$ErrorActionPreference = 'Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "'PRIVATE_ROOT_ACCEPTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_selected_tool_wrapper(tmp_path: Path, kind: str) -> Path:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    root = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    tool = re.search(r"(?ms)^function Require-PrivateExecutablePath\([^\n]*\) \{.*?^\}", source)
    assert root
    if kind == "pipx":
        selection = source.split("$pipx = Native 'pipx.exe'\n", 1)[1].split(
            "if ($pipxVersion -cne '1.17.2')", 1,
        )[0]
        setup = (
            "function Native([string]$name) { return $null }\n"
            "$data=$env:TEST_DATA_ROOT\n"
            "Require-PrivateDataRoot $data\n"
        )
    else:
        candidate = "        $candidate = Join-Path $env:LOCALAPPDATA 'Programs\\Python\\Python312\\python.exe'"
        selection = candidate + source.split(candidate, 1)[1].split(
            "\n    }\n    if (-not $python)", 1,
        )[0]
        setup = (
            "$env:LOCALAPPDATA=$env:TEST_LOCALAPPDATA\n"
            "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
            "$python=$null\n"
        )
    wrapper = tmp_path / f"select-{kind}.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + root.group(0) + "\n"
        + (tool.group(0) + "\n" if tool else "")
        + "$ErrorActionPreference='Stop'\n"
        + setup
        + "$ErrorActionPreference='Continue'\n"
        + selection + "\n"
        + "'TOOL_SELECTION_COMPLETED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_tool_probe(
    wrapper: Path, root: Path, local_appdata: Path,
    executable: Path | None = None, private_root: Path | None = None,
    expected_target: Path | None = None, target_root: Path | None = None,
):
    env = {
        **os.environ,
        "TEST_DATA_ROOT": str(root),
        "TEST_LOCALAPPDATA": str(local_appdata),
    }
    if executable is not None:
        env["TEST_TOOL"] = str(executable)
        env["TEST_PRIVATE_ROOT"] = str(private_root)
    assert expected_target is None or target_root is not None
    env["TEST_EXPECTED_TARGET"] = str(expected_target) if expected_target is not None else ""
    env["TEST_TARGET_ROOT"] = str(target_root) if target_root is not None else ""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=wrapper.parent,
        env=env,
        capture_output=True, text=True, timeout=20,
    )


def _windows_tool_path_wrapper(tmp_path: Path) -> Path:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    root = re.search(r"(?ms)^function Require-PrivateDataRoot\([^\n]*\) \{.*?^\}", source)
    tool = re.search(r"(?ms)^function Require-PrivateExecutablePath\([^\n]*\) \{.*?^\}", source)
    assert root and tool
    wrapper = tmp_path / "check-tool.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + root.group(0) + "\n" + tool.group(0) + "\n"
        + "$ErrorActionPreference='Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "$selected=Require-PrivateExecutablePath $env:TEST_TOOL $env:TEST_PRIVATE_ROOT "
        "$env:TEST_EXPECTED_TARGET $env:TEST_TARGET_ROOT\n"
        "if ($env:TEST_EXPECTED_TARGET -and "
        "(Get-Item -LiteralPath $env:TEST_TOOL -Force).LinkType -eq 'SymbolicLink' -and "
        "$selected -cne $env:TEST_EXPECTED_TARGET) { throw 'Wrong target' }\n"
        "'TOOL_ADMITTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_file_link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except OSError as error:
        if error.winerror == 1314:
            if os.environ.get("SITEOPS_REQUIRE_WINDOWS_LINK") == "1":
                pytest.fail("Required Windows file symlink capability is unavailable.")
            pytest.skip("This local host cannot create a Windows file symlink.")
        raise


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["pipx", "python"])
def test_windows_bootstrap_rejects_untrusted_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    if kind == "pipx":
        shared = root / "tools"
        executable = shared / "pipx" / "Scripts" / "pipx.exe"
        executable.parent.mkdir(parents=True)
        rights = "(OI)(CI)M"
    else:
        shared = local_appdata
        executable = shared / "Programs" / "Python" / "Python312" / "python.exe"
        rights = "(OI)(CI)(WD,AD)"
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", f"*S-1-5-32-545:{rights}"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic fixture ACL.")
    try:
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shutil.which("where.exe"), executable)
        selected = _windows_tool_probe(_windows_selected_tool_wrapper(tmp_path, kind), root, local_appdata)
        assert selected.returncode != 0
        assert "TOOL_ACL" in selected.stderr
        assert "TOOL_SELECTION_COMPLETED" not in selected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["pipx", "python"])
def test_windows_bootstrap_accepts_private_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    if kind == "pipx":
        executable = root / "tools" / "pipx" / "Scripts" / "pipx.exe"
    else:
        executable = local_appdata / "Programs" / "Python" / "Python312" / "python.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    selected = _windows_tool_probe(_windows_selected_tool_wrapper(tmp_path, kind), root, local_appdata)
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert "TOOL_SELECTION_COMPLETED" in selected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_accepts_private_copied_siteops_launcher(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    shutil.copy2(shutil.which("where.exe"), launcher)

    accepted = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "TOOL_ADMITTED" in accepted.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_accepts_private_pipx_style_linked_launcher(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    target = root / "pipx" / "venvs" / "siteops" / "Scripts" / "siteops.exe"
    target.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, target)

    accepted = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
        target, root / "pipx",
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "TOOL_ADMITTED" in accepted.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_launcher_link_to_unrelated_executable(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    unrelated = root / "pipx" / "venvs" / "other" / "Scripts" / "siteops.exe"
    unrelated.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), unrelated)
    expected = root / "pipx" / "venvs" / "siteops" / "Scripts" / "siteops.exe"
    expected.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), expected)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, unrelated)

    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
        expected, root / "pipx",
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
@pytest.mark.parametrize("writable", ["launcher", "target"])
def test_windows_bootstrap_rejects_linked_launcher_with_writable_ancestor(tmp_path, writable):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target = root / "pipx" / "venvs" / "siteops" / "Scripts" / "siteops.exe"
    target.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, target)
    shared = bin_dir if writable == "launcher" else target.parent
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic target ACL.")
    try:
        rejected = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
            target, root / "pipx",
        )
        assert rejected.returncode != 0
        assert "TOOL_ACL" in rejected.stderr
        assert "TOOL_ADMITTED" not in rejected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_linked_tool_without_selected_target(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target = root / "pipx" / "venvs" / "siteops" / "Scripts" / "siteops.exe"
    target.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, target)
    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_linked_launcher_with_reparse_target_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target_directory = root / "pipx" / "venvs" / "other" / "Scripts"
    target_directory.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target_directory / "siteops.exe")
    alias = root / "pipx" / "venvs" / "siteops"
    try:
        alias.symlink_to(target_directory.parent, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(target_directory.parent)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    target = alias / "Scripts" / "siteops.exe"
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, target)
    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
        target, root / "pipx",
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
def test_windows_bootstrap_rejects_untrusted_executable_file_even_under_private_parent(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    wrapper = _windows_tool_path_wrapper(tmp_path)
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    executable = root / "tools" / "pipx" / "Scripts" / "pipx.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    grant = subprocess.run(
        ["icacls.exe", str(executable), "/grant", "*S-1-5-32-545:M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic executable ACL.")
    try:
        rejected = _windows_tool_probe(wrapper, root, local_appdata, executable, root)
        assert rejected.returncode != 0
        assert "TOOL_ACL" in rejected.stderr
        assert "TOOL_ADMITTED" not in rejected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(executable), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows junction rules need Windows.")
def test_windows_bootstrap_rejects_linked_executable_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    wrapper = _windows_tool_path_wrapper(tmp_path)
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    external = tmp_path / "elsewhere"
    (external / "pipx" / "Scripts").mkdir(parents=True)
    alias = root / "tools"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(external)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    executable = alias / "pipx" / "Scripts" / "pipx.exe"
    shutil.copy2(shutil.which("where.exe"), executable)
    rejected = _windows_tool_probe(wrapper, root, local_appdata, executable, root)
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
def test_windows_bootstrap_accepts_create_only_ancestor_above_private_tool_root(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    executable = root / "tools" / "pipx" / "Scripts" / "pipx.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    grant = subprocess.run(
        ["icacls.exe", str(tmp_path), "/grant", "*S-1-5-32-545:(WD,AD)"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic ancestor ACL.")
    try:
        accepted = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata, executable, root,
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "TOOL_ADMITTED" in accepted.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(tmp_path), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL and junction rules need Windows.")
def test_windows_bootstrap_rejects_shared_root_without_changing_it(tmp_path):
    wrapper = _windows_private_root_wrapper(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    valid = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(safe / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "PRIVATE_ROOT_ACCEPTED" in valid.stdout
    assert (safe / "siteops").is_dir()
    repeated = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(safe / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr

    shared = tmp_path / "shared"
    shared.mkdir()
    users = "*S-1-5-32-545"
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", f"{users}:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the fixture directory ACL.")
    try:
        rejected = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(shared / "siteops")},
            capture_output=True, text=True, timeout=20,
        )
        assert rejected.returncode != 0
        assert "private Site Ops data root" in rejected.stderr
        assert "ROOT_ANCESTOR_ACL" in rejected.stderr
        assert "PRIVATE_ROOT_ACCEPTED" not in rejected.stdout
        assert not (shared / "siteops").exists()
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", users],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse rules need Windows.")
def test_windows_bootstrap_rejects_symlinked_data_ancestor(tmp_path):
    wrapper = _windows_private_root_wrapper(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(private, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(private)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    rejected = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(alias / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert rejected.returncode != 0
    assert "private Site Ops data root" in rejected.stderr
    assert "ROOT_ANCESTOR_TYPE" in rejected.stderr
    assert not (private / "siteops").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows filesystem errors need PowerShell 5.1.")
@pytest.mark.parametrize(
    ("case", "category"),
    [
        ("invalid-path", "ROOT_PATH"),
        ("missing-ancestor", "ROOT_ANCESTOR_TYPE"),
        ("ancestor-error", "ROOT_ANCESTOR_TYPE"),
        ("root-probe-error", "ROOT_DATA_TYPE"),
        ("creation-error", "ROOT_DATA_CREATE"),
        ("owner-error", "ROOT_DATA_OWNER"),
        ("protection-error", "ROOT_DATA_ACL"),
    ],
)
def test_windows_bootstrap_root_filesystem_errors_are_bounded(tmp_path, case, category):
    marker = "PRIVATE_DATA_PATH_MARKER"
    target = tmp_path / "siteops"
    setup = ""
    if case == "invalid-path":
        target = Path(f"C:\\{marker}<\\siteops")
    elif case == "missing-ancestor":
        target = tmp_path / f"missing-{marker}" / "siteops"
    elif case == "ancestor-error":
        setup = f"function Get-Item {{ throw '{marker}' }}\n"
    elif case == "root-probe-error":
        setup = f"function Test-Path {{ throw '{marker}' }}\n"
    elif case == "creation-error":
        setup = f"function New-Item {{ throw '{marker}' }}\n"
    elif case == "owner-error":
        setup = (
            f"function icacls.exe {{ Write-Error '{marker}'; "
            "$global:LASTEXITCODE = 5 }\n"
        )
    else:
        setup = (
            "function icacls.exe { if ($args -contains '/setowner') { "
            "$global:LASTEXITCODE = 0; return }; "
            f"Write-Error '{marker}'; $global:LASTEXITCODE = 5 }}\n"
        )
    wrapper = _windows_private_root_wrapper(tmp_path, setup)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(target)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert category in result.stderr
    assert marker not in result.stdout + result.stderr
    assert "PRIVATE_ROOT_ACCEPTED" not in result.stdout


def test_existing_siteops_build_is_rejected_before_any_shared_backend_change():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert bash.index('if [[ -n "$recorded"') < bash.index('"$pipx_bin" upgrade-shared')
    assert powershell.index('if ($recorded -and $recorded -ine') < powershell.index(
        "'upgrade-shared', '--pip-args'",
    )
    assert 'if $replace && [[ -n "$recorded" ]]' in bash
    assert 'if ($Replace -and $recorded) { $install += \'--force\' }' in powershell
    assert 'siteops --version' in bash and "siteops.exe" in powershell


def test_windows_native_verification_parses_json_without_powershell_51_jq_quoting():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "--jq" not in powershell
    assert "ConvertFrom-Json" in powershell
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    verified = guide.split("### Verify the bootstrap script", 1)[1].split("## Before you start", 1)[0]
    windows = verified.split("Windows PowerShell:", 1)[1]
    assert "--jq" not in windows
    assert "ConvertFrom-Json" in windows


def test_windows_azure_cli_is_selected_independently_of_gh_and_policy_time_is_portable():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    gh_branch = powershell.split("if ($ghVersion -cnotmatch", 1)[1].split("\n}\n", 1)[0]
    assert "if ($WithAzureCli" not in gh_branch
    assert "if ($WithAzureCli -and -not (AzureCli))" in powershell
    assert ".ToString('o')" not in powershell
    assert "yyyy-MM-ddTHH:mm:ss.ffffffzzz" in powershell


@pytest.mark.skipif(sys.platform != "win32", reason="This checks native PowerShell 5.1 argument handling.")
@pytest.mark.parametrize("accepted", [False, True])
def test_windows_bootstrap_checks_native_verifier_certificate_before_bundle_use(
    tmp_path, accepted,
):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = script.split('    $signer = "', 1)[1].split("    $bundleId =", 1)[0]
    block = '    $signer = "' + block
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", SOURCE_SHA, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not accepted:
        observation["verificationResult"]["signature"]["certificate"]["buildConfigURI"] = "PRIVATE_WRONG"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps([observation]), encoding="utf-8")
    wrapper = tmp_path / "verify.ps1"
    wrapper.write_text(
        """function gh.exe { Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw; $global:LASTEXITCODE = 0 }
function Stage([string]$message) { }
function Fail([string]$message) { throw $message }
$ErrorActionPreference = 'Stop'
$gh = 'gh.exe'
$archive = 'unopened.zip'
$Repository = 'Azure/digital-ops-scale-kit'
$SourceRef = 'refs/heads/main'
$Caller = 'release.yaml'
$SourceCommit = '""" + SOURCE_SHA + "'\n" + block + "\n'CERTIFICATE_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env={**os.environ, "TEST_EVIDENCE": str(evidence)},
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("CERTIFICATE_ACCEPTED" in result.stdout) is accepted
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
def test_windows_retained_release_key_binds_exact_selection(tmp_path):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = script.split('    $identity = (', 1)[1].split('    $cache = Join-Path', 1)[0]
    block = '    $identity = (' + block
    statement = (
        "$Repository='Azure/digital-ops-scale-kit';$Release='siteops/v1.0.0b1';"
        "$SourceCommit='" + SOURCE_SHA + "';$SourceRef='refs/heads/main';"
        "$Caller='release.yaml';$data='unused';"
        + block + "\n$cacheId\n"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", statement],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    selected = (
        "Azure/digital-ops-scale-kit", "siteops/v1.0.0b1",
        SOURCE_SHA, "refs/heads/main", "release.yaml",
    )
    expected = hashlib.sha256(("\0".join(selected) + "\0").encode()).hexdigest()
    assert result.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Native stderr handling needs PowerShell 5.1.")
@pytest.mark.parametrize("list_exit", [0, 7])
def test_windows_bootstrap_inspects_empty_pipx_without_ignoring_real_failure(
    tmp_path, list_exit,
):
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = source.split("    $root = Join-Path $data 'bundles'\n", 1)[1].split(
        "    $mainPackage =", 1,
    )[0]
    helper = re.search(r"(?ms)^function InvokePipx\([^\n]*\) \{.*?^\}", source)
    assert helper
    fake = tmp_path / "pipx.cmd"
    fake.write_text(
        '@echo off\n'
        'echo {"venvs":{}}\n'
        'echo nothing has been installed with pipx 1>&2\n'
        f'exit /b {list_exit}\n',
        encoding="ascii",
    )
    wrapper = tmp_path / "inspect-pipx.ps1"
    wrapper.write_text(
        "function Fail([string]$message) { throw \"Site Ops installation: $message\" }\n"
        + helper.group(0) + "\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$pipx = $env:TEST_PIPX\n"
        + block + "\n'EMPTY_HOME_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_PIPX": str(fake)},
        capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is (list_exit == 0), result.stdout + result.stderr
    if list_exit:
        assert "current pipx installation could not be inspected" in result.stderr
    else:
        assert "EMPTY_HOME_ACCEPTED" in result.stdout
        assert "nothing has been installed" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native pipx messages need PowerShell 5.1.")
@pytest.mark.parametrize("failing", ["", "install"])
def test_windows_pipx_operations_check_exit_without_emitting_native_warnings(
    tmp_path, failing,
):
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    helper = re.search(r"(?ms)^function InvokePipx\([^\n]*\) \{.*?^\}", source)
    assert helper, "The pipx native exit and output gate is missing."
    fake = tmp_path / "pipx.cmd"
    fake.write_text(
        "@echo off\n"
        "if \"%1\"==\"--version\" echo 1.17.2\n"
        "if \"%1\"==\"list\" echo {\"venvs\":{}}\n"
        "if \"%1\"==\"runpip\" echo pip 26.2.1 from controlled fixture\n"
        "if \"%1\"==\"environment\" echo C:\\private\\bin\n"
        "echo controlled benign pipx warning 1>&2\n"
        "if \"%TEST_FAIL_PIPX%\"==\"%1\" exit /b 7\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    wrapper = tmp_path / "pipx-operations.ps1"
    wrapper.write_text(
        "function Fail([string]$message) { throw \"Site Ops installation: $message\" }\n"
        + helper.group(0) + "\n"
        "$ErrorActionPreference = 'Stop';$pipx = $env:TEST_PIPX\n"
        "$version = InvokePipx @('--version') 'Version check failed.'\n"
        "$listing = InvokePipx @('list', '--output', 'json') 'Inspection failed.'\n"
        "$null = InvokePipx @('upgrade-shared') 'Backend failed.'\n"
        "$null = InvokePipx @('install', 'siteops') 'Installation failed.'\n"
        "$backend = InvokePipx @('runpip', 'siteops', '--version') 'Reader failed.'\n"
        "$null = InvokePipx @('ensurepath') 'Path failed.'\n"
        "$binDir = InvokePipx @('environment', '--value', 'PIPX_BIN_DIR') 'Location failed.'\n"
        "if ($version -ne '1.17.2' -or $listing -notmatch 'venvs' -or "
        "$backend -notmatch '^pip 26\\.2\\.1 ' -or $binDir -ne 'C:\\private\\bin') { throw 'Unexpected result' }\n"
        "'PIPX_OPERATIONS_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path,
        env={**os.environ, "TEST_PIPX": str(fake), "TEST_FAIL_PIPX": failing},
        capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) is (not failing), result.stdout + result.stderr
    if failing:
        assert "Installation failed." in result.stderr
        assert "PIPX_OPERATIONS_ACCEPTED" not in result.stdout
    else:
        assert "PIPX_OPERATIONS_ACCEPTED" in result.stdout
        assert "controlled benign pipx warning" not in result.stdout + result.stderr


def test_bash_is_portable_lf_and_parses():
    script = SCRIPTS / "siteops-bootstrap.sh"
    content = script.read_bytes()
    assert content.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in content
    result = subprocess.run(
        [str(required_bash()), "-n", bash_path(script)],
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_retained_bundle_accepts_matching_files_but_rejects_tampering(tmp_path):
    script = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    found = re.search(
        r'if ! python3 - "\$bundle" <<\'PY\'\n'
        r"(.*?)\nPY\n  then\n"
        r'    fail "The retained bundle contents differ from the authenticated archive\."\n'
        r"  fi", script, flags=re.DOTALL,
    )
    assert found, "The retained bundle verifier is missing."
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    files = {"pylock.toml": b"locked bytes\n", "wheels/fixture.whl": b"wheel bytes"}
    entries = []
    for name, data in files.items():
        destination = bundle / name
        destination.write_bytes(data)
        entries.append({
            "path": name, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    (bundle / "bundle.json").write_text(json.dumps({"files": entries}), encoding="utf-8")

    def verify():
        return subprocess.run(
            [sys.executable, "-c", found.group(1), str(bundle)],
            cwd=tmp_path, capture_output=True, text=True, timeout=20,
        )

    assert verify().returncode == 0
    (bundle / "pylock.toml").write_bytes(b"altered bytes\n")
    assert verify().returncode != 0
    (bundle / "pylock.toml").write_bytes(files["pylock.toml"])
    (bundle / "unexpected").write_bytes(b"operator file")
    assert verify().returncode != 0
    (bundle / "unexpected").unlink()
    assert verify().returncode == 0


def test_both_scripts_keep_the_azure_and_source_boundaries_explicit():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for script in (bash, powershell):
        assert "gh auth login" not in script and "az login" not in script
        assert "siteops deploy" not in script
        assert "attestation verify" in script
        assert "siteops-install.zip.attestation.jsonl" in script
        assert "--cert-identity" in script and "--source-digest" in script
        assert "--tlsv1.2" in script
        assert "buildConfigURI" in script and "runnerEnvironment" in script
        assert script.index("attestation verify") < script.index("--lock")


def test_bootstrap_preview_keeps_source_names_private_in_redacted_output():
    for path in (SCRIPTS / "siteops-bootstrap.sh", SCRIPTS / "siteops-bootstrap.ps1"):
        script = path.read_text(encoding="utf-8")
        assert "SITEOPS_REDACT_OUTPUT" in script
        assert "An explicitly selected source will be enrolled after installation." in script
        assert "with an approved source. Authenticate to Azure separately." in script
    if sys.platform != "win32":
        return
    script = SCRIPTS / "siteops-bootstrap.ps1"
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
            "-EnrollSource", "private-name", "-DryRun",
        ],
        env={**os.environ, "SITEOPS_REDACT_OUTPUT": "1"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private-name" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Ubuntu preview runs on the Linux runner.")
def test_bash_preview_and_unattended_refusal_do_not_acquire_tools(tmp_path):
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or 'VERSION_ID="24.04"' not in os_release:
        pytest.skip("Requires Ubuntu 24.04.")
    script = SCRIPTS / "siteops-bootstrap.sh"
    arguments = ["bash", str(script), "--release", "siteops/v1.0.0b1", "--source-commit", SOURCE_SHA]
    env = {**os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "state")}
    preview = subprocess.run(
        [*arguments, "--dry-run"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        arguments, input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "pass --yes" in denied.stderr
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="The Ubuntu runner exercises the script journey.")
@pytest.mark.parametrize("venv_without_pip", [False, True])
def test_ubuntu_bootstrap_recovers_and_requires_explicit_replacement(tmp_path, venv_without_pip):
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or 'VERSION_ID="24.04"' not in os_release:
        pytest.skip("Requires Ubuntu 24.04.")
    harness = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
    script = SCRIPTS / "siteops-bootstrap.sh"
    result = subprocess.run(
        [str(required_bash()), bash_path(harness), bash_path(script)],
        cwd=tmp_path, env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C",
            "TEST_NO_PIP_IN_VENV": "1" if venv_without_pip else "0",
        }, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no network" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="The Ubuntu runner exercises the script journey.")
def test_ubuntu_bootstrap_keeps_a_compatible_pipx_backend(tmp_path):
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    if "ID=ubuntu" not in os_release or 'VERSION_ID="24.04"' not in os_release:
        pytest.skip("Requires Ubuntu 24.04.")
    harness = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
    script = SCRIPTS / "siteops-bootstrap.sh"
    result = subprocess.run(
        [str(required_bash()), bash_path(harness), bash_path(script)],
        cwd=tmp_path, env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C",
            "TEST_HARNESS_SHARED_OK": "1",
        }, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backend was preserved" in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="The Ubuntu runner exercises managed-host flow.")
@pytest.mark.parametrize(("existing_pipx", "venv_without_pip"), [(False, False), (True, True)])
def test_managed_azure_linux_bootstrap_isolates_pipx_and_uses_virtualenv(
    tmp_path, existing_pipx, venv_without_pip,
):
    source = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert source.count(". /etc/os-release\n") == 1
    fixture = ROOT / "tests" / "fixtures" / "bootstrap-azurelinux3-os-release"
    bootstrap = tmp_path / "bootstrap.sh"
    bootstrap.write_text(source.replace(". /etc/os-release\n", f'. "{fixture}"\n'), encoding="utf-8")
    harness = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
    result = subprocess.run(
        [str(required_bash()), bash_path(harness), bash_path(bootstrap)],
        cwd=tmp_path, env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C",
            "TEST_HARNESS_MANAGED": "1",
            "TEST_HARNESS_NO_PIPX": "0" if existing_pipx else "1",
            "TEST_NO_PIP_IN_VENV": "1" if venv_without_pip else "0",
        }, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no network" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_and_invalid_identity_do_not_acquire_tools(tmp_path):
    script = SCRIPTS / "siteops-bootstrap.ps1"
    path = shutil.which("pwsh") or shutil.which("powershell")
    if path is None:
        pytest.skip("PowerShell is unavailable.")
    arguments = [
        path, "-NoProfile", "-File", str(script),
        "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
    ]
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "state")}
    preview = subprocess.run(
        [*arguments, "-DryRun"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        [*arguments[:-1], "bad", "-Yes"], input="", text=True,
        capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "Select an exact release" in denied.stderr
    assert not (tmp_path / "state").exists()

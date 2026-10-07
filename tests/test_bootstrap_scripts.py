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


def test_ubuntu_fixture_uses_a_pinned_acr_base_and_nonroot_python():
    source = (ROOT / "tests" / "fixtures" / "Dockerfile.bootstrap-ubuntu").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"FROM ubuntu\.azurecr\.io/ubuntu:noble@sha256:[0-9a-f]{64}",
        source.splitlines()[0],
    )
    assert "apt-get install -y --no-install-recommends python3 python3-venv" in source
    assert "USER 65534:65534" in source
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in source


def test_bash_bootstrap_gates_on_capability_without_elevation():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert "getconf GNU_LIBC_VERSION" in bash
    for removed in ("/etc/os-release", "VERSION_ID", "sudo", "apt-get", "--with-azure-cli"):
        assert removed not in bash


def test_bash_bootstrap_admits_private_data_root_before_retained_tool_use():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    for invocation in ("\n  prepare_runtime\n", "\nprepare_runtime\n"):
        assert bash.index('admit_directory "$data" private') < bash.index(invocation)
    runtime = re.search(r"(?ms)^prepare_runtime\(\) \{.*?^\}", bash).group()
    assert "\n  select_uv\n" in runtime


def test_windows_bootstrap_admits_private_data_root_before_retained_tool_use():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "function Require-PrivateDataRoot(" in powershell
    assert powershell.index("Require-PrivateDataRoot $data") < powershell.index(
        "$uv = Select-Uv $data $download $curl",
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


def test_bootstrap_failures_are_distinguishable_from_progress():
    bash = {line.strip() for line in (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8").splitlines()}
    assert "fail() { printf 'Site Ops installation failed: %s\\n' \"$1\" >&2; exit 1; }" in bash
    assert "stage() { printf 'Site Ops installation: %s\\n' \"$1\"; }" in bash
    powershell = {
        line.strip() for line in (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8").splitlines()
    }
    assert 'function Fail([string]$Message) { throw "Site Ops installation failed: $Message" }' in powershell
    assert 'function Stage([string]$Message) { Write-Host "Site Ops installation: $Message" }' in powershell


def test_windows_bootstrap_creates_retained_bundle_as_user_before_helper_extraction():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    lines = [line.strip() for line in powershell.splitlines()]
    declared = lines.index("$bundle = Join-Path $root $bundleId")
    helper = next(index for index, line in enumerate(lines) if line.startswith("$installed = Check-Payload @("))
    # The helper would otherwise create the bundle with the elevated token's default owner.
    assert "Require-PrivateDataRoot $bundle" in lines[declared + 1:helper]
    assert not any("Test-Path -LiteralPath $bundle" in line for line in lines)

def test_windows_bootstrap_checks_managed_executables_before_running_them():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert powershell.index("Require-PrivateExecutablePath $available (Split-Path -Parent $available) -Optional") < (
        powershell.index("Stage 'Keep: the other uv installation unchanged.")
    )
    runtime = powershell.split("function Select-ManagedPython(", 1)[1].split("\n}\n", 1)[0]
    assert runtime.index("Require-PrivateRuntimeTree (Split-Path -Parent $python) $Directory $python") < (
        runtime.index("& $python -I -S -B")
    )
    assert "python', 'list'" not in runtime
    assert powershell.index("Require-PrivateRuntimeTree $toolHome $toolDirectory") < powershell.index(
        "$installed = Check-Payload @($mode",
    )
    assert powershell.index("Require-PrivateExecutablePath $expectedCommand $commandDirectory") < (
        powershell.index("$siteops = Native 'siteops.exe'")
    )
    assert "Join-Path $toolHome 'Scripts\\siteops.exe'" in powershell
    assert "$validatedTarget" not in powershell
    assert powershell.index("Require-PrivateExecutablePath $expectedCommand $commandDirectory") < (
        powershell.index("& $siteops --version")
    )


def _windows_functions(*names: str) -> str:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    bodies = []
    for name in ("Read-NodeAcl", *names):
        body = re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source)
        assert body, f"The Windows {name} helper is missing."
        bodies.append(body.group(0))
    return "\n".join(bodies) + "\n"


def _windows_private_root_wrapper(tmp_path: Path, setup: str = "") -> Path:
    wrapper = tmp_path / "check-data-root.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + setup
        + _windows_functions("Require-PrivateDataRoot")
        + "$ErrorActionPreference = 'Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "'PRIVATE_ROOT_ACCEPTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_tool_probe(
    wrapper: Path, root: Path, local_appdata: Path,
    executable: Path | None = None, private_root: Path | None = None,
):
    env = {
        **os.environ,
        "TEST_DATA_ROOT": str(root),
        "TEST_LOCALAPPDATA": str(local_appdata),
    }
    if executable is not None:
        env["TEST_TOOL"] = str(executable)
        env["TEST_PRIVATE_ROOT"] = str(private_root)
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=wrapper.parent,
        env=env,
        capture_output=True, text=True, timeout=20,
    )


def _windows_tool_path_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "check-tool.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + _windows_functions("Require-PrivateDataRoot", "Require-PrivateExecutablePath")
        + "$ErrorActionPreference='Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "Require-PrivateExecutablePath $env:TEST_TOOL $env:TEST_PRIVATE_ROOT\n"
        "'TOOL_ADMITTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_file_link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except OSError as error:
        if error.winerror == 1314:
            if os.environ.get("SITEOPS_REQUIRE_WINDOWS_SYMLINK_REJECTION") == "1":
                pytest.fail("Required Windows file symlink capability is unavailable.")
            pytest.skip("This local host cannot create a Windows file symlink.")
        raise


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["uv", "runtime"])
def test_windows_bootstrap_rejects_untrusted_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    if kind == "uv":
        shared = root / "tools"
        executable = shared / "uv" / "0.12.20" / "uv.exe"
        executable.parent.mkdir(parents=True)
        rights = "(OI)(CI)M"
    else:
        shared = local_appdata
        executable = shared / "uv" / "python" / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
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
        selected = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata,
            executable, root if kind == "uv" else shared / "uv" / "python",
        )
        assert selected.returncode != 0
        assert "TOOL_ACL" in selected.stderr
        assert "TOOL_ADMITTED" not in selected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["uv", "runtime"])
def test_windows_bootstrap_accepts_private_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    if kind == "uv":
        executable = root / "tools" / "uv" / "0.12.20" / "uv.exe"
        private_root = root
    else:
        private_root = local_appdata / "uv" / "python"
        executable = private_root / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    selected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, executable, private_root,
    )
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert "TOOL_ADMITTED" in selected.stdout


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
def test_windows_bootstrap_rejects_file_link_to_foreign_command(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    unrelated = root / "tools" / "other" / "Scripts" / "siteops.exe"
    unrelated.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), unrelated)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, unrelated)

    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_copied_launcher_with_writable_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    shutil.copy2(shutil.which("where.exe"), launcher)
    grant = subprocess.run(
        ["icacls.exe", str(bin_dir), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic target ACL.")
    try:
        rejected = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
        )
        assert rejected.returncode != 0
        assert "TOOL_ACL" in rejected.stderr
        assert "TOOL_ADMITTED" not in rejected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(bin_dir), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_file_link_even_to_private_tool(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target = root / "tools" / "siteops" / "Scripts" / "siteops.exe"
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
def test_windows_bootstrap_rejects_reparse_command_directory_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target_directory = root / "tools" / "other" / "Scripts"
    target_directory.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target_directory / "siteops.exe")
    alias = root / "tools" / "siteops"
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
    launcher = alias / "Scripts" / "siteops.exe"
    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, root / "tools",
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
    executable = root / "tools" / "other" / "Scripts" / "siteops.exe"
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
    (external / "other" / "Scripts").mkdir(parents=True)
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
    executable = alias / "other" / "Scripts" / "siteops.exe"
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
    executable = root / "tools" / "other" / "Scripts" / "siteops.exe"
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
    assert "mode=replace" in bash and 'check_payload "$mode" "$archive" "$bundle"' in bash
    assert '2) fail "Another Site Ops selection is installed. Use --replace after review." ;;' in bash
    assert "$mode = if ($Replace) { 'replace' } else { 'install' }" in powershell
    assert "Check-Payload @($mode, $archive, $bundle" in powershell
    assert '"$siteops" --version' in bash and "siteops.exe" in powershell


def test_windows_native_verification_parses_json_without_powershell_51_jq_quoting():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "--jq" not in powershell
    assert "ConvertFrom-Json" in powershell
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    verified = guide.split("### Verify the bootstrap script", 1)[1].split("## Before you start", 1)[0]
    windows = verified.split("Windows PowerShell:", 1)[1]
    assert "--jq" not in windows
    assert "ConvertFrom-Json" in windows


def test_windows_prerequisites_are_checked_not_installed_and_policy_time_is_portable():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for removed in ("WithAzureCli", "WinGet", "winget", "GetEnvironmentVariable('PATH'",
                    "Ensure-UvStorage $env:TEMP", "Join-Path $env:TEMP"):
        assert removed not in powershell
    lines = [line.strip() for line in powershell.splitlines()]
    resolved = lines.index("$gh = Select-GitHubCli")
    assert lines.count("$gh = Select-GitHubCli") == 1
    assert resolved < lines.index("if ($DryRun) {") < lines.index("if (-not $Yes) {")
    selection = powershell.split("function Select-GitHubCli() {", 1)[1].split("\n}\n", 1)[0]
    assert selection.index("Require-PrivateExecutablePath $path $path -Optional") < selection.index(
        "& $path version",
    )
    assert "& $gh attestation verify" in powershell and "& $gh attestation trusted-root" in powershell
    assert "Stage 'Azure CLI was not found. Install it before deploying: https://aka.ms/installazurecli'" in lines
    assert ".ToString('o')" not in powershell
    assert "yyyy-MM-ddTHH:mm:ss.ffffffzzz" in powershell


@pytest.mark.skipif(sys.platform != "win32", reason="This checks native PowerShell 5.1 argument handling.")
@pytest.mark.parametrize("accepted", [False, True])
def test_windows_bootstrap_checks_native_verifier_certificate_before_bundle_use(
    tmp_path, accepted,
):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^function Verify-ReleaseAsset\([^\n]*\) \{.*?^\}", script).group()
    assert "Verify-ReleaseAsset $archive $engineCommit $engineRef $engineCaller '_siteops-distribution.yaml'" in script
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
$SourceCommit = '""" + SOURCE_SHA + "'\n" + block
        + "\nVerify-ReleaseAsset $archive $SourceCommit $SourceRef $Caller '_siteops-distribution.yaml'\n'CERTIFICATE_ACCEPTED'\n",
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
    block = re.search(r"(?ms)^function Get-SelectionKey\([^\n]*\) \{.*?^\}", script).group()
    statement = (
        "$Repository='Azure/digital-ops-scale-kit';$Release='siteops/v1.0.0b1';"
        "$SourceCommit='" + SOURCE_SHA + "';$SourceRef='refs/heads/main';"
        "$Caller='release.yaml';$data='unused';"
        + block + "\nGet-SelectionKey $Release $SourceCommit $SourceRef $Caller\n"
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
        if script is bash:
            assert script.index("attestation verify") < script.index('check_payload "$mode"')
        else:
            assert script.index("attestation verify") < script.index("Check-Payload @($mode")


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


@pytest.mark.skipif(sys.platform != "linux", reason="The Linux preview runs on the Linux runner.")
def test_bash_preview_and_unattended_refusal_do_not_acquire_tools(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text("#!/usr/bin/env bash\necho 'gh version 2.95.0 (fixture)'\n", encoding="utf-8")
    for path, mode in ((tmp_path, 0o700), (tools, 0o755), (gh, 0o755)):
        path.chmod(mode)
    script = SCRIPTS / "siteops-bootstrap.sh"
    arguments = ["bash", str(script), "--release", "siteops/v1.0.0b1", "--source-commit", SOURCE_SHA]
    env = {
        **os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "state"),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }
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
    assert "Uses the installed GitHub CLI" in preview.stdout
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        [*arguments[:-1], "bad", "-Yes"], input="", text=True,
        capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "Select an exact release" in denied.stderr
    # The admitted GitHub CLI may keep its own state; the preview creates no Site Ops state.
    assert not (tmp_path / "state" / "siteops").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_requires_github_cli_without_installing_it(tmp_path):
    system = Path(os.environ["SystemRoot"]) / "System32"
    result = subprocess.run(
        [
            str(system / "WindowsPowerShell" / "v1.0" / "powershell.exe"), "-NoProfile",
            "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "siteops-bootstrap.ps1"),
            "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA, "-DryRun",
        ],
        env={**os.environ, "PATH": str(system), "LOCALAPPDATA": str(tmp_path / "state")},
        input="", text=True, capture_output=True, timeout=20,
    )
    assert result.returncode != 0
    # Windows PowerShell wraps long errors at its console width.
    message = "GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry."
    assert "".join(message.split()) in "".join(result.stderr.split())
    assert "Preview only" not in result.stdout
    assert not (tmp_path / "state").exists()

"""Native Windows admission for shared uv storage and prerequisite tools."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_bundle import publish_assets

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows ACLs.")


def _functions(*names):
    source = SCRIPT.read_text(encoding="utf-8")
    bodies = []
    for name in dict.fromkeys(("Read-NodeAcl", *names)):
        found = re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source)
        assert found, f"The production {name} helper is missing."
        bodies.append(found.group())
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Utility\\"
        "Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop\n"
        "function Fail([string]$message) { throw $message }\n" + "\n".join(bodies)
    )


def _runtime_functions(*names):
    source = SCRIPT.read_text(encoding="utf-8")
    helpers = tuple(
        name
        for name in ("Get-ManagedPythonCandidates", "Require-PrivateRuntimeTree")
        if f"function {name}(" in source
    )
    return _functions(*names, *helpers)


def _shell(name):
    program = shutil.which(name)
    if program is None:
        pytest.skip(f"{name} is not installed on this host.")
    return program


def _run(wrapper, tmp_path, *, program="powershell.exe", **values):
    environment = {
        key: item
        for key, item in os.environ.items()
        if not key.upper().startswith(("UV_", "PYTHON"))
    }
    environment.update({key: str(value) for key, value in values.items()})
    return subprocess.run(
        [program, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _native_fixture():
    executable = os.environ.get("SITEOPS_TEST_UV")
    archive = os.environ.get("SITEOPS_TEST_UV_ARCHIVE")
    if (
        not executable
        or not archive
        or not Path(executable).is_file()
        or not Path(archive).is_file()
    ):
        if os.environ.get("CI") or os.environ.get("SITEOPS_REQUIRE_WINDOWS_UV") == "1":
            pytest.fail("Required qualified Windows uv and archive fixtures are unavailable.")
        pytest.skip("Qualified native uv and archive fixtures are not installed on this host.")
    return Path(executable), Path(archive)


def _managed_fixture():
    selected = os.environ.get("SITEOPS_TEST_UV_PYTHON_DIR")
    directory = Path(selected) if selected else None
    if (
        directory is None
        or not (directory / "cpython-3.11.16-windows-x86_64-none" / "python.exe").is_file()
    ):
        if os.environ.get("CI") or os.environ.get("SITEOPS_REQUIRE_WINDOWS_UV") == "1":
            pytest.fail("The required managed Python fixture is unavailable.")
        pytest.skip("No qualified concrete managed Python fixture is installed.")
    return directory


@pytest.mark.parametrize(
    ("managed", "rights", "accepted"),
    [(False, "", True), (False, "RX", False), (True, "RX", True), (True, "M", False)],
)
def test_native_storage_distinguishes_readers_from_writers(tmp_path, managed, rights, accepted):
    directory = tmp_path / "storage"
    wrapper = tmp_path / "storage.ps1"
    wrapper.write_text(
        _functions("Require-PrivateDataRoot")
        + "\nRequire-PrivateDataRoot $env:TEST_DIRECTORY\n"
        + (
            "\n& icacls.exe $env:TEST_DIRECTORY /grant '*S-1-5-32-545:(OI)(CI)"
            + rights
            + "' | Out-Null\n"
            "if ($LASTEXITCODE -ne 0) { throw 'Fixture ACL setup failed.' }\n"
            if rights
            else ""
        )
        + "\nRequire-PrivateDataRoot $env:TEST_DIRECTORY"
        + (" -Managed" if managed else "")
        + "\n'STORAGE_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env={**os.environ, "TEST_DIRECTORY": str(directory)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("STORAGE_ACCEPTED" in result.stdout) is accepted
    if not accepted:
        assert "ROOT_DATA_ACL" in result.stderr


@pytest.mark.parametrize(
    ("setting", "value", "accepted"),
    [
        ("UV_INSECURE_HOST", "private.example.invalid", False),
        ("UV_PYTHON_DOWNLOADS_JSON_URL", "https://private.example.invalid/catalog.json", False),
        ("UV_PYTHON_INSTALL_MIRROR", "http://private.example.invalid/python", False),
        ("UV_PYTHON_INSTALL_MIRROR", "https://private.example.invalid/python", True),
        ("UV_INDEX", "https://private.example.invalid/simple", True),
    ],
)
def test_native_uv_invocation_isolates_policy_and_restores_caller_settings(
    tmp_path,
    setting,
    value,
    accepted,
):
    tool = tmp_path / "uv.cmd"
    marker = tmp_path / "called"
    tool.write_text(
        "@echo off\n"
        "if defined UV_INDEX exit /b 9\n"
        "if defined UV_CONFIG_FILE exit /b 9\n"
        'if not "%UV_TOOL_DIR%"=="%TEST_TOOL_DIR%" exit /b 9\n'
        'echo ran>"%TEST_MARKER%"\n'
        "echo uv 0.12.20 fixture\nexit /b 0\n",
        encoding="ascii",
    )
    wrapper = tmp_path / "invoke.ps1"
    wrapper.write_text(
        _functions("Invoke-Uv")
        + "\n$result = Invoke-Uv $env:TEST_UV @('--version')\n"
        + "if ([Environment]::GetEnvironmentVariable($env:TEST_SETTING) -cne $env:TEST_VALUE) "
        + "{ throw 'The caller environment changed.' }\n'UV_ACCEPTED'\n",
        encoding="utf-8",
    )
    environment = {
        key: item for key, item in os.environ.items() if not key.upper().startswith("UV_")
    }
    environment.update(
        {
            setting: value,
            "TEST_SETTING": setting,
            "TEST_VALUE": value,
            "TEST_UV": str(tool),
            "TEST_MARKER": str(marker),
            "TEST_TOOL_DIR": str(tmp_path / "tools"),
            "UV_TOOL_DIR": str(tmp_path / "tools"),
            "UV_CONFIG_FILE": str(tmp_path / "unselected.toml"),
        }
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert marker.exists() is accepted
    assert value not in result.stdout + result.stderr


def test_windows_install_uses_the_authenticated_helper_and_native_uv():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "function Select-Uv(" in source
    assert "function Select-ManagedPython(" in source
    assert "95f9bc30fbb3574d276e28ac4a6de932d25153645853d13da8c21eec3bc88d06" in source
    assert "a0d2742d49564a32488753b02e76276e7b5ef1b1ea8cf30bcbf06ee28f60cd73" in source
    assert "Check-Payload @($mode, $archive, $bundle" in source
    assert source.index("attestation verify") < source.index(
        "$installerHelper = Get-InstallerHelper"
    )
    assert source.index("$installerHelper = Get-InstallerHelper") < source.index(
        "Check-Payload @($mode, $archive, $bundle"
    )
    assert "uv tool uninstall siteops" in source
    assert "pipx" not in source.lower()
    assert "Python.Python." not in source


def test_windows_install_admits_storage_and_runtime_before_execution():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Ensure-UvStorage $toolDirectory" in source
    assert "Ensure-UvStorage $commandDirectory" in source
    assert "Ensure-UvStorage $pythonDirectory" in source
    runtime_selection = source.split("function Select-ManagedPython(", 1)[1].split("\n}\n", 1)[0]
    assert runtime_selection.index(
        "Require-PrivateRuntimeTree (Split-Path -Parent $python) $Directory $python"
    ) < runtime_selection.index("& $python -I -S -B")
    assert "'python', 'list'" not in runtime_selection
    assert source.index(
        "Require-PrivateRuntimeTree $toolHome $toolDirectory"
    ) < source.index("$installed = Check-Payload")
    assert "uv python install" not in source
    assert "'--no-bin', '--no-registry'" in source
    assert "cpython-3.11.16-windows-x86_64-none" in source


def test_windows_retained_proof_and_archive_paths_are_admitted_before_verification():
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.index("Require-PrivateDataRoot $cacheRoot") < source.index(
        "if (Test-Path -LiteralPath $cache) {", source.index("$cacheRoot =")
    )
    assert source.index("Require-PrivateDataRoot $cache") < source.index(
        "Get-ChildItem -LiteralPath $cache -Force"
    )
    assert source.index("Require-PrivateExecutablePath $asset $assets") < source.index(
        "Verify-ReleaseAsset $archive $engineCommit"
    )
    assert source.index("Require-PrivateExecutablePath $path $referenceAssets") < source.index(
        "Verify-ReleaseAsset $referencePath $SourceCommit"
    )
    assert "[IO.File]::Move((Join-Path $download $name), (Join-Path $cache $name))" in source
    assert "[IO.File]::Copy($source, $target" not in source


@pytest.mark.parametrize(
    ("tamper", "expected"),
    [
        ("", "ARCHIVE_ACCEPTED"),
        ("archive", "native uv archive differs"),
        ("executable", "retained native uv executable differs"),
    ],
)
def test_native_pinned_archive_and_executable_bytes(tmp_path, tamper, expected):
    executable, archive = _native_fixture()
    wrapper = tmp_path / "pinned.ps1"
    wrapper.write_text(
        _functions("Require-PrivateExecutablePath", "Assert-UvArchive", "Assert-PinnedUv")
        + """
$target = Join-Path $env:TEST_HOME 'uv.exe'
Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination (Join-Path $env:TEST_HOME 'uv-windows.zip')
if ($env:TEST_TAMPER -eq 'archive') {
    [IO.File]::AppendAllText((Join-Path $env:TEST_HOME 'uv-windows.zip'), 'x')
}
Assert-UvArchive (Join-Path $env:TEST_HOME 'uv-windows.zip') $target
if ($env:TEST_TAMPER -eq 'executable') { [IO.File]::AppendAllText($target, 'x') }
Assert-PinnedUv $target $env:TEST_HOME
'ARCHIVE_ACCEPTED'
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        TEST_HOME=tmp_path,
        TEST_ARCHIVE=archive,
        TEST_TAMPER=tamper,
    )
    assert (result.returncode == 0) is (not tamper), result.stdout + result.stderr
    assert expected in (result.stdout if not tamper else result.stderr)


@pytest.mark.parametrize(
    ("available", "conflict", "expected"),
    [
        (True, False, "Keep: the other uv installation unchanged"),
        (False, False, "MAINTENANCE_EXPOSED"),
        (False, True, "retained native uv executable differs"),
    ],
)
def test_native_uv_selection_preserves_other_tools_and_exposes_only_when_absent(
    tmp_path,
    available,
    conflict,
    expected,
):
    executable, archive = _native_fixture()
    wrapper = tmp_path / "select-uv.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot",
            "Ensure-UvStorage",
            "Require-PrivateExecutablePath",
            "Assert-UvArchive",
            "Assert-PinnedUv",
            "Select-Uv",
        )
        + """
function Stage([string]$message) { Write-Host $message }
function Native([string]$name) {
    if ($env:TEST_EXISTING) { return $env:TEST_EXISTING }
    return $null
}
$env:USERPROFILE = $env:TEST_PROFILE
$data = Join-Path $env:TEST_HOME 'siteops'
Require-PrivateDataRoot $data
$cache = Join-Path $data 'tools\\uv\\0.12.20'
Ensure-UvStorage $cache
Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination (Join-Path $cache 'uv-windows.zip')
Copy-Item -LiteralPath $env:TEST_QUALIFIED -Destination (Join-Path $cache 'uv.exe')
$selected = Select-Uv $data $env:TEST_HOME $null
if ($selected -cne (Join-Path $cache 'uv.exe')) { throw 'Wrong uv selected.' }
$exposed = Join-Path $env:USERPROFILE '.local\\bin\\uv.exe'
if ($env:TEST_EXISTING -and (Test-Path -LiteralPath $exposed)) {
    throw 'Another uv was replaced.'
}
if (-not $env:TEST_EXISTING -and -not $env:TEST_CONFLICT) {
    Assert-PinnedUv $exposed (Split-Path -Parent $exposed)
    'MAINTENANCE_EXPOSED'
}
""",
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    profile.mkdir()
    existing = ""
    if available:
        existing = tmp_path / "existing" / "uv.exe"
        existing.parent.mkdir()
        shutil.copy2(shutil.which("where.exe"), existing)
    if conflict:
        maintenance = profile / ".local" / "bin" / "uv.exe"
        maintenance.parent.mkdir(parents=True)
        shutil.copy2(shutil.which("where.exe"), maintenance)
    result = _run(
        wrapper,
        tmp_path,
        TEST_HOME=tmp_path,
        TEST_PROFILE=profile,
        TEST_ARCHIVE=archive,
        TEST_QUALIFIED=executable,
        TEST_EXISTING=existing,
        TEST_CONFLICT="1" if conflict else "",
    )
    assert (result.returncode == 0) is (not conflict), result.stdout + result.stderr
    assert expected in (result.stderr if conflict else result.stdout)
    if available:
        assert existing.read_bytes() == Path(shutil.which("where.exe")).read_bytes()
    if conflict:
        assert maintenance.read_bytes() == Path(shutil.which("where.exe")).read_bytes()


@pytest.mark.parametrize("ambient", ["writable", "writable-qualified", "file-link"])
def test_native_uv_selection_keeps_unadmitted_ambient_tool_and_uses_pinned_cache(
    tmp_path, ambient,
):
    executable, archive = _native_fixture()
    wrapper = tmp_path / "select.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot", "Ensure-UvStorage", "Require-PrivateExecutablePath",
            "Assert-UvArchive", "Assert-PinnedUv", "Select-Uv",
        )
        + """
function Native([string]$name) { return $env:TEST_AMBIENT }
function Stage([string]$message) { Write-Host $message }
$data = Join-Path $env:TEST_HOME 'siteops'
Require-PrivateDataRoot $data
$cache = Join-Path $data 'tools\\uv\\0.12.20'
Ensure-UvStorage $cache
Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination (Join-Path $cache 'uv-windows.zip')
Copy-Item -LiteralPath $env:TEST_QUALIFIED -Destination (Join-Path $cache 'uv.exe')
$selected = Select-Uv $data $env:TEST_HOME $null
if ($selected -cne (Join-Path $cache 'uv.exe')) { throw 'Wrong uv selected.' }
'PINNED_CACHE_SELECTED'
""",
        encoding="utf-8",
    )
    outside = tmp_path / "other"
    outside.mkdir()
    ambient_uv = outside / "uv.exe"
    if ambient in {"writable", "writable-qualified"}:
        if ambient == "writable-qualified":
            shutil.copy2(executable, ambient_uv)
        else:
            ambient_uv.write_bytes(b"another tool; never execute this marker")
        grant = subprocess.run(
            ["icacls.exe", str(outside), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
            capture_output=True, text=True, timeout=20,
        )
        if grant.returncode:
            pytest.skip("The test user cannot change the synthetic ambient-tool ACL.")
    else:
        try:
            ambient_uv.symlink_to(executable)
        except OSError as error:
            if error.winerror == 1314 and os.environ.get("SITEOPS_REQUIRE_WINDOWS_SYMLINK_REJECTION") != "1":
                pytest.skip("This host cannot create the required file link.")
            raise
    before = ambient_uv.read_bytes() if ambient == "writable" else None
    try:
        result = _run(
            wrapper, tmp_path, TEST_HOME=tmp_path, TEST_ARCHIVE=archive,
            TEST_QUALIFIED=executable, TEST_AMBIENT=ambient_uv,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Keep: the other uv installation unchanged" in result.stdout
        assert "PINNED_CACHE_SELECTED" in result.stdout
        if before is not None:
            assert ambient_uv.read_bytes() == before
        elif ambient == "writable-qualified":
            assert ambient_uv.read_bytes() == executable.read_bytes()
        else:
            assert ambient_uv.is_symlink() and ambient_uv.resolve() == executable
    finally:
        if ambient in {"writable", "writable-qualified"}:
            subprocess.run(
                ["icacls.exe", str(outside), "/remove:g", "*S-1-5-32-545"],
                capture_output=True, text=True, check=True, timeout=20,
            )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("other_state", ["incomplete", "writable"])
def test_native_runtime_selection_ignores_unrelated_candidate(tmp_path, existing, other_state):
    source = _managed_fixture() / "cpython-3.11.16-windows-x86_64-none"
    managed = tmp_path / "managed"
    selected = managed / source.name
    shutil.copytree(source, selected)
    other = managed / "cpython-3.12.7-windows-x86_64-none"
    other.mkdir()
    marker = other / "operator-file"
    marker.write_bytes(b"unrelated runtime")
    if other_state == "writable":
        shutil.copy2(shutil.which("where.exe"), other / "python.exe")
        grant = subprocess.run(
            ["icacls.exe", str(other), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
            capture_output=True, text=True, timeout=20,
        )
        if grant.returncode:
            pytest.skip("The test user cannot change the unrelated runtime ACL.")
    tools = tmp_path / "tools"
    tools.mkdir()
    if existing:
        current = tools / "siteops"
        current.mkdir()
        (current / "pyvenv.cfg").write_text(
            f"version_info = 3.11.16\nhome = {selected}\n", encoding="utf-8",
        )
    wrapper = tmp_path / "select-python.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot", "Require-PrivateExecutablePath", "Select-ManagedPython",
        )
        + """
function Stage([string]$message) { }
function Invoke-Uv { throw 'Unselected uv operation.' }
$python = Select-ManagedPython 'unused' $env:TEST_MANAGED $env:TEST_TOOLS
if ($python -cne $env:TEST_SELECTED) { throw 'Wrong runtime selected.' }
'SAFE_RUNTIME_SELECTED'
""",
        encoding="utf-8",
    )
    try:
        result = _run(
            wrapper, tmp_path, TEST_MANAGED=managed, TEST_TOOLS=tools,
            TEST_SELECTED=selected / "python.exe",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SAFE_RUNTIME_SELECTED" in result.stdout
        assert marker.read_bytes() == b"unrelated runtime"
    finally:
        if other_state == "writable":
            subprocess.run(
                ["icacls.exe", str(other), "/remove:g", "*S-1-5-32-545"],
                capture_output=True, text=True, check=True, timeout=20,
            )


def test_native_runtime_selection_refuses_unsafe_selected_candidate(tmp_path):
    source = _managed_fixture() / "cpython-3.11.16-windows-x86_64-none"
    managed = tmp_path / "managed"
    selected = managed / source.name
    shutil.copytree(source, selected)
    executable = selected / "python.exe"
    grant = subprocess.run(
        ["icacls.exe", str(executable), "/grant", "*S-1-5-32-545:M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The test user cannot change the selected runtime ACL.")
    wrapper = tmp_path / "unsafe-python.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot", "Require-PrivateExecutablePath", "Select-ManagedPython",
        )
        + """
function Stage([string]$message) { }
function Invoke-Uv { throw 'Unselected uv operation.' }
Select-ManagedPython 'unused' $env:TEST_MANAGED $env:TEST_TOOLS
""",
        encoding="utf-8",
    )
    try:
        result = _run(wrapper, tmp_path, TEST_MANAGED=managed, TEST_TOOLS=tmp_path / "tools")
        assert result.returncode != 0
        assert "TOOL_ACL" in result.stderr
    finally:
        subprocess.run(
            ["icacls.exe", str(executable), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.parametrize("existing", [False, True])
def test_native_managed_runtime_uses_concrete_patch_path_and_existing_binding(tmp_path, existing):
    executable, _ = _native_fixture()
    managed = _managed_fixture()
    concrete = managed / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
    tools = tmp_path / "tools"
    tools.mkdir()
    wrapper = tmp_path / "python.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot",
            "Require-PrivateExecutablePath",
            "Invoke-Uv",
            "Select-ManagedPython",
        )
        + """
function Stage([string]$message) { }
$env:UV_PYTHON_INSTALL_DIR = $env:TEST_PYTHON_DIR
$python = Select-ManagedPython $env:TEST_UV $env:TEST_PYTHON_DIR $env:TEST_TOOLS
if ($python -cne $env:TEST_CONCRETE) { throw 'The minor alias was selected.' }
'CONCRETE_PYTHON_ACCEPTED'
""",
        encoding="utf-8",
    )
    if existing:
        current = tools / "siteops"
        current.mkdir()
        (current / "pyvenv.cfg").write_text(
            f"version_info = 3.11.16\nhome = {concrete.parent}\n",
            encoding="utf-8",
        )
    result = _run(
        wrapper,
        tmp_path,
        TEST_PYTHON_DIR=managed,
        TEST_UV=executable,
        TEST_TOOLS=tools,
        TEST_CONCRETE=concrete,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CONCRETE_PYTHON_ACCEPTED" in result.stdout


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, "HELPER_INSTALLED"),
        (2, "Use -Replace"),
        (3, "exposed command belongs"),
        (4, "unrecognized Python startup"),
    ],
)
def test_native_fresh_archive_helper_runs_with_isolated_python_and_maps_exit(
    tmp_path,
    exit_code,
    expected,
):
    python = _managed_fixture() / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
    archive = tmp_path / "bundle.zip"
    helper = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['TEST_INVOCATION']).write_text(json.dumps(sys.argv[1:]))\n"
        f"if {exit_code}: raise SystemExit({exit_code})\n"
        "print(json.dumps({'version':'1.0.0','wheel':'wheels/siteops-1.0.0.whl'}))\n"
    )
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("siteops-install.py", helper)
    wrapper = tmp_path / "install.ps1"
    wrapper.write_text(
        _functions("Require-PrivateExecutablePath", "Get-InstallerHelper", "Check-Payload")
        + """
$python = $env:TEST_PYTHON
$installerHelper = Get-InstallerHelper $env:TEST_ARCHIVE $env:TEST_HOME
Require-PrivateExecutablePath $installerHelper $env:TEST_HOME
$mode = if ($env:TEST_REPLACE) { 'replace' } else { 'install' }
$installed = Check-Payload @($mode, $env:TEST_ARCHIVE, (Join-Path $env:TEST_HOME 'bundle'),
    'example/siteops', ('c'*40), 'refs/heads/main',
    $env:TEST_UV, (Join-Path $env:TEST_HOME 'tools'), (Join-Path $env:TEST_HOME 'bin'))
if ($installed.version -cne '1.0.0') { throw 'Wrong helper result.' }
'HELPER_INSTALLED'
""",
        encoding="utf-8",
    )
    invocation = tmp_path / "invocation.json"
    result = _run(
        wrapper,
        tmp_path,
        TEST_PYTHON=python,
        TEST_ARCHIVE=archive,
        TEST_HOME=tmp_path,
        TEST_UV=tmp_path / "uv.exe",
        TEST_INVOCATION=invocation,
        TEST_REPLACE="1",
    )
    assert (result.returncode == 0) is (exit_code == 0), result.stdout + result.stderr
    assert expected in (result.stdout if exit_code == 0 else result.stderr)
    assert json.loads(invocation.read_text(encoding="utf-8"))[0] == "replace"


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_installer_helper_entries_are_compared_ordinally(tmp_path, shell):
    program = _shell(shell)
    cases = {
        "exact": ((), "EXTRACTED"),
        # An entry spelled with U+00AD is another file, not a second copy of a selected name.
        "soft-hyphen": (("bun\u00addle.json", "siteops-instal\u00adl.py"), "EXTRACTED"),
        "metadata-letter-case": (("BUNDLE.JSON",), "The bundle has no valid version metadata."),
        "helper-letter-case": (
            ("SITEOPS-INSTALL.PY",),
            "The authenticated release has no valid installer helper.",
        ),
    }
    for name, (extra, _) in cases.items():
        (tmp_path / name).mkdir()
        with zipfile.ZipFile(tmp_path / f"{name}.zip", "w") as package:
            for entry in ("bundle.json", "siteops-install.py", *extra):
                package.writestr(entry, '{"package": {"version": "1.0.0"}}')
    wrapper = tmp_path / "entries.ps1"
    wrapper.write_text(
        _functions("Get-InstallerHelper")
        + """
foreach ($name in $env:TEST_CASES.Split(',')) {
    $result = try {
        $helper = Get-InstallerHelper (Join-Path $env:TEST_HOME "$name.zip") (Join-Path $env:TEST_HOME $name) '1.0.0'
        if ($helper -cne (Join-Path (Join-Path $env:TEST_HOME $name) 'siteops-install.py')) { throw 'Wrong helper.' }
        'EXTRACTED'
    } catch { "$_" }
    $name + '=' + $result
}
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper, tmp_path, program=program, TEST_HOME=tmp_path, TEST_CASES=",".join(cases)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert dict(line.split("=", 1) for line in result.stdout.splitlines()) == {
        name: expected for name, (_, expected) in cases.items()
    }


def test_native_qualified_uv_is_reused_without_creating_a_tooling_cache(tmp_path):
    executable, _ = _native_fixture()
    wrapper = tmp_path / "reuse.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot",
            "Ensure-UvStorage",
            "Require-PrivateExecutablePath",
            "Assert-UvArchive",
            "Assert-PinnedUv",
            "Select-Uv",
        )
        + """
function Native([string]$name) { return $env:TEST_UV }
function Stage([string]$message) { Write-Host $message }
$data = Join-Path $env:TEST_HOME 'siteops'
Require-PrivateDataRoot $data
$selected = Select-Uv $data $env:TEST_HOME $null
if ($selected -cne $env:TEST_UV -or (Test-Path -LiteralPath (Join-Path $data 'tools'))) {
    throw 'The existing qualified uv was not preserved.'
}
'QUALIFIED_REUSED'
""",
        encoding="utf-8",
    )
    result = _run(wrapper, tmp_path, TEST_UV=executable, TEST_HOME=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUALIFIED_REUSED" in result.stdout


@pytest.mark.parametrize("tamper", [False, True])
def test_native_uv_acquisition_uses_the_pinned_archive_without_other_downloads(tmp_path, tamper):
    _, archive = _native_fixture()
    profile = tmp_path / "profile"
    profile.mkdir()
    wrapper = tmp_path / "acquire.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot",
            "Ensure-UvStorage",
            "Require-PrivateExecutablePath",
            "Assert-UvArchive",
            "Assert-PinnedUv",
            "Select-Uv",
        )
        + """
function Stage([string]$message) { }
function Native([string]$name) { return $null }
function ControlledCurl {
    if ($args[-1] -cne 'https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-pc-windows-msvc.zip') {
        throw 'Unexpected download target.'
    }
    $index = [array]::IndexOf($args, '--output')
    if ($index -lt 0) { throw 'Missing bounded download target.' }
    Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination $args[$index + 1]
    if ($env:TEST_TAMPER) { [IO.File]::AppendAllText($args[$index + 1], 'x') }
    $global:LASTEXITCODE = 0
}
$env:USERPROFILE = $env:TEST_PROFILE
$data = Join-Path $env:TEST_HOME 'siteops'
Require-PrivateDataRoot $data
$download = Join-Path $env:TEST_HOME 'download'
Require-PrivateDataRoot $download
$uv = Select-Uv $data $download 'ControlledCurl'
Assert-PinnedUv $uv (Split-Path -Parent $uv)
'PINNED_ACQUISITION_ACCEPTED'
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        TEST_HOME=tmp_path,
        TEST_PROFILE=profile,
        TEST_ARCHIVE=archive,
        TEST_TAMPER="1" if tamper else "",
    )
    assert (result.returncode == 0) is (not tamper), result.stdout + result.stderr
    if tamper:
        assert "native uv archive differs" in result.stderr
        assert not (profile / ".local" / "bin" / "uv.exe").exists()
    else:
        assert "PINNED_ACQUISITION_ACCEPTED" in result.stdout
        assert (profile / ".local" / "bin" / "uv.exe").is_file()


def test_native_incomplete_managed_python_refuses_before_install(tmp_path):
    directory = tmp_path / "python"
    concrete = directory / "cpython-3.11.16-windows-x86_64-none"
    concrete.mkdir(parents=True)
    marker = concrete / "operator-file"
    marker.write_text("unchanged", encoding="ascii")
    wrapper = tmp_path / "incomplete.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot", "Require-PrivateExecutablePath", "Select-ManagedPython"
        )
        + """
function Invoke-Uv([string]$tool, [string[]]$arguments) {
    if ($arguments -contains 'install') { throw 'UNEXPECTED_PYTHON_DOWNLOAD' }
    return '[]'
}
function Stage([string]$message) { }
Select-ManagedPython 'unused' $env:TEST_PYTHON_DIR $env:TEST_TOOLS
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        TEST_PYTHON_DIR=directory,
        TEST_TOOLS=tmp_path / "tools",
    )
    assert result.returncode != 0
    assert "uv-managed Python installation is incomplete" in result.stderr
    assert "UNEXPECTED_PYTHON_DOWNLOAD" not in result.stderr
    assert marker.read_text(encoding="ascii") == "unchanged"


@pytest.mark.parametrize("existing", ["", "home", "letter-case", "soft-hyphen"])
def test_existing_siteops_runtime_wins_over_the_default_managed_patch(tmp_path, existing):
    tools = tmp_path / "tools"
    tools.mkdir()
    python_directory = tmp_path / "managed"
    previous = python_directory / "cpython-3.12.7-windows-x86_64-none"
    for version in ("3.11.16", "3.12.7"):
        concrete = python_directory / f"cpython-{version}-windows-x86_64-none"
        concrete.mkdir(parents=True)
        shutil.copy2(shutil.which("where.exe"), concrete / "python.exe")
    if existing:
        # A home spelled with U+00AD names another folder, so it binds no available runtime.
        home = {
            "home": str(previous),
            "letter-case": str(previous).upper(),
            "soft-hyphen": str(previous).replace("none", "n\u00adone"),
        }[existing]
        current = tools / "siteops"
        current.mkdir()
        (current / "pyvenv.cfg").write_text(
            f"version_info = 3.12.7\nhome = {home}\n",
            encoding="utf-8",
        )
    source = SCRIPT.read_text(encoding="utf-8")
    selection = source.split("function Select-ManagedPython(", 1)[1].split(
        "    $python = $selected.Python",
        1,
    )[0]
    wrapper = tmp_path / "binding.ps1"
    wrapper.write_text(
        _runtime_functions("Require-PrivateDataRoot", "Require-PrivateExecutablePath")
        + "\n"
        + _REPORTED_FAIL
        + "function Invoke-Uv { throw 'Unexpected uv inventory execution.' }\n"
        + "function Stage([string]$message) { }\n"
        + "function Select-ManagedPython("
        + selection
        + """
$expected = Join-Path $Directory ('cpython-' + $env:TEST_EXPECTED + '-windows-x86_64-none\\python.exe')
if ($selected.Version -cne $env:TEST_EXPECTED -or $selected.Python -cne $expected) {
    throw 'The selected managed runtime differs from the expected concrete patch.'
}
}
Select-ManagedPython 'unused' $env:TEST_PYTHON_DIR $env:TEST_TOOLS
'EXISTING_RUNTIME_REUSED'
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        TEST_PYTHON_DIR=python_directory,
        TEST_TOOLS=tools,
        TEST_EXPECTED="3.12.7" if existing else "3.11.16",
    )
    if existing == "soft-hyphen":
        assert result.returncode == 1, result.stdout + result.stderr
        assert (
            "FAIL: The existing Site Ops runtime is not an available uv-managed Python."
            in result.stdout
        )
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXISTING_RUNTIME_REUSED" in result.stdout


def test_absent_managed_python_is_provisioned_without_aliases_or_registry(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    selection = source.split("function Select-ManagedPython(", 1)[1].split(
        "    $python = $selected.Python", 1
    )[0]
    wrapper = tmp_path / "provision.ps1"
    wrapper.write_text(
        _runtime_functions("Require-PrivateDataRoot", "Require-PrivateExecutablePath")
        + """
function Stage([string]$message) { }
function Invoke-Uv([string]$tool, [string[]]$arguments) {
    if ($arguments -contains 'install') {
        if ($arguments -cnotcontains '3.11.16' -or
            $arguments -cnotcontains '--no-bin' -or
            $arguments -cnotcontains '--no-registry') { throw 'Unsafe runtime provisioning.' }
        $concrete = Join-Path $env:TEST_PYTHON_DIR 'cpython-3.11.16-windows-x86_64-none'
        New-Item -ItemType Directory -Path $concrete -ErrorAction Stop | Out-Null
        Copy-Item -LiteralPath $env:TEST_DUMMY_PYTHON -Destination (Join-Path $concrete 'python.exe')
        [IO.File]::WriteAllText($env:TEST_MARKER, 'provisioned')
        return ''
    }
    throw 'Unexpected uv inventory execution.'
}
"""
        + "function Select-ManagedPython("
        + selection
        + """
if ($selected.Version -cne '3.11.16' -or
    $selected.Python -cne (Join-Path $Directory 'cpython-3.11.16-windows-x86_64-none\\python.exe')) {
    throw 'The concrete runtime was not selected.'
}
}
Select-ManagedPython 'unused' $env:TEST_PYTHON_DIR $env:TEST_TOOLS
'PROVISION_ACCEPTED'
""",
        encoding="utf-8",
    )
    marker = tmp_path / "provisioned"
    result = _run(
        wrapper,
        tmp_path,
        TEST_PYTHON_DIR=tmp_path / "python",
        TEST_TOOLS=tmp_path / "tools",
        TEST_DUMMY_PYTHON=shutil.which("where.exe"),
        TEST_MARKER=marker,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROVISION_ACCEPTED" in result.stdout
    assert marker.read_text(encoding="ascii") == "provisioned"


def test_native_unsafe_explicit_tool_storage_refuses_before_uv(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    selection = source.split("    foreach ($name in @('UV_TOOL_DIR'", 1)[1].split(
        "    $uv = Select-Uv",
        1,
    )[0]
    shared = tmp_path / "shared"
    shared.mkdir()
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic fixture ACL.")
    try:
        wrapper = tmp_path / "unsafe.ps1"
        wrapper.write_text(
            _functions("Require-PrivateDataRoot", "Ensure-UvStorage")
            + """
function Select-Uv { [IO.File]::WriteAllText($env:TEST_MARKER, 'executed') }
$env:UV_TOOL_DIR = $env:TEST_UNSAFE
"""
            + "    foreach ($name in @('UV_TOOL_DIR'"
            + selection
            + "\n    $uv = Select-Uv\n",
            encoding="utf-8",
        )
        marker = tmp_path / "executed"
        result = _run(
            wrapper,
            tmp_path,
            TEST_UNSAFE=shared / "tools",
            TEST_MARKER=marker,
        )
        assert result.returncode != 0
        assert "ROOT_ANCESTOR_ACL" in result.stderr
        assert not marker.exists()
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )


def test_native_production_install_block_executes_the_actual_verified_helper(
    bundle_factory,
    tmp_path,
):
    executable, _ = _native_fixture()
    managed = _managed_fixture()
    root, manifest = bundle_factory()
    archive, _ = publish_assets(root, manifest, tmp_path / "assets")
    source = SCRIPT.read_text(encoding="utf-8")
    install = source.split("    $installerHelper = Get-InstallerHelper $archive $download $engineVersion\n", 1)[
        1
    ].split(
        '    Stage "Command directory:',
        1,
    )[0]
    download = tmp_path / "staging"
    data = tmp_path / "siteops"
    wrapper = tmp_path / "production-install.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Get-InstallerHelper",
            "Check-Payload",
            "Require-PrivateDataRoot",
            "Ensure-UvStorage",
            "Invoke-Uv",
            "Require-PrivateExecutablePath",
            "Assert-UvArchive",
            "Assert-PinnedUv",
            "Select-Uv",
            "Select-ManagedPython",
        )
        + """
function Native([string]$name) {
    if ($name -ceq 'uv.exe') { return $env:TEST_UV }
    if ($name -ceq 'siteops.exe') { return (Join-Path $env:UV_TOOL_BIN_DIR 'siteops.exe') }
    throw 'Unexpected native command.'
}
function Stage([string]$message) { Write-Host $message }
Require-PrivateDataRoot $env:TEST_DOWNLOAD
Require-PrivateDataRoot $env:TEST_DATA
$archive = $env:TEST_ARCHIVE
$download = $env:TEST_DOWNLOAD
$data = $env:TEST_DATA
$curl = $null
$Repository = 'example/publisher'
$SourceCommit = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$SourceRef = 'refs/heads/main'
$engineCommit = $SourceCommit
$engineRef = $SourceRef
$engineVersion = ''
$Replace = $env:TEST_REPLACE -eq '1'
"""
        + "    $installerHelper = Get-InstallerHelper $archive $download $engineVersion\n"
        + install
        + "\n'PRODUCTION_INSTALL_ACCEPTED'\n",
        encoding="utf-8",
    )
    env = dict(
        TEST_UV=executable,
        TEST_ARCHIVE=archive,
        TEST_DOWNLOAD=download,
        TEST_DATA=data,
        UV_TOOL_DIR=tmp_path / "uv" / "tools",
        UV_TOOL_BIN_DIR=tmp_path / "uv" / "bin",
        UV_PYTHON_INSTALL_DIR=managed,
    )
    result = _run(wrapper, tmp_path, **env, TEST_REPLACE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PRODUCTION_INSTALL_ACCEPTED" in result.stdout
    assert (tmp_path / "uv" / "bin" / "siteops.exe").is_file()
    assert (data / "bundles").is_dir()
    configuration = tmp_path / "uv" / "tools" / "siteops" / "pyvenv.cfg"
    initial = configuration.stat().st_mtime_ns
    repeated = _run(
        wrapper,
        tmp_path,
        **{**env, "TEST_DOWNLOAD": tmp_path / "repeat"},
        TEST_REPLACE="",
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert configuration.stat().st_mtime_ns == initial
    package = tmp_path / "uv" / "tools" / "siteops" / "Lib" / "site-packages" / "siteops" / "__init__.py"
    damaged = package.read_bytes() + b"\n# damaged installed payload\n"
    package.write_bytes(damaged)
    without_repair = _run(
        wrapper,
        tmp_path,
        **{**env, "TEST_DOWNLOAD": tmp_path / "without-repair"},
        TEST_REPLACE="",
    )
    assert without_repair.returncode != 0
    assert "bundle or installed payload failed validation" in without_repair.stderr
    replacement = _run(
        wrapper,
        tmp_path,
        **{**env, "TEST_DOWNLOAD": tmp_path / "replacement"},
        TEST_REPLACE="1",
    )
    assert replacement.returncode == 0, replacement.stdout + replacement.stderr
    assert "PRODUCTION_INSTALL_ACCEPTED" in replacement.stdout
    assert package.read_bytes() != damaged


@pytest.mark.parametrize(
    ("fault", "expected"),
    [("library", "RUNTIME_ACL"), ("directory", "RUNTIME_ACL"), ("junction", "RUNTIME_TYPE")],
)
def test_native_python_inventory_does_not_execute_before_runtime_admission(
    tmp_path, fault, expected
):
    managed = tmp_path / "managed"
    concrete = managed / "cpython-3.11.16-windows-x86_64-none"
    concrete.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), concrete / "python.exe")
    library_directory = concrete / "DLLs"
    library_directory.mkdir()
    library = library_directory / "python311.dll"
    library.write_bytes(b"synthetic library")
    unsafe = library if fault == "library" else library_directory
    if fault == "junction":
        outside = tmp_path / "outside"
        outside.mkdir()
        junction = concrete / "redirected"
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_JUNCTION "
                "-Target $env:TEST_OUTSIDE -ErrorAction Stop | Out-Null",
            ],
            env={**os.environ, "TEST_JUNCTION": str(junction), "TEST_OUTSIDE": str(outside)},
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    else:
        subprocess.run(
            [
                "icacls.exe", str(unsafe), "/grant",
                "*S-1-5-32-545:(OI)(CI)M" if fault == "directory" else "*S-1-5-32-545:M",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    wrapper = tmp_path / "inventory.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot", "Require-PrivateExecutablePath", "Select-ManagedPython"
        )
        + """
function Stage([string]$message) { }
function Invoke-Uv([string]$tool, [string[]]$arguments) {
    if ($arguments -contains 'list') {
        [IO.File]::WriteAllText($env:TEST_MARKER, 'interpreter inventory executed')
        return $env:TEST_RECORD
    }
    throw 'Unexpected uv operation.'
}
Select-ManagedPython 'unused' $env:TEST_MANAGED $env:TEST_TOOLS
""",
        encoding="utf-8",
    )
    record = {
        "key": "cpython-3.11.16-windows-x86_64-none",
        "version": "3.11.16",
        "version_parts": {"major": 3, "minor": 11, "patch": 16},
        "implementation": "cpython",
        "arch": "x86_64",
        "os": "windows",
        "variant": "default",
        "libc": "none",
    }
    marker = tmp_path / "unadmitted-inventory"
    try:
        result = _run(
            wrapper,
            tmp_path,
            TEST_MANAGED=managed,
            TEST_TOOLS=tmp_path / "tools",
            TEST_MARKER=marker,
            TEST_RECORD=json.dumps([record]),
        )
        assert result.returncode != 0
        assert expected in result.stderr
        assert not marker.exists()
    finally:
        if fault == "junction":
            junction.rmdir()
            assert outside.is_dir()
        else:
            subprocess.run(
                ["icacls.exe", str(unsafe), "/remove:g", "*S-1-5-32-545"],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )


@pytest.mark.parametrize("previous", ["external", "writable-interpreter", "writable-library", "safe"])
def test_native_replacement_admits_previous_tool_before_uv(tmp_path, previous):
    managed = _managed_fixture()
    concrete = managed / "cpython-3.11.16-windows-x86_64-none"
    tool_directory = tmp_path / "tools"
    tool = tool_directory / "siteops"
    scripts = tool / "Scripts"
    scripts.mkdir(parents=True)
    interpreter = scripts / "python.exe"
    shutil.copy2(shutil.which("where.exe"), interpreter)
    library = scripts / "python311.dll"
    library.write_bytes(b"synthetic library")
    home = concrete if previous != "external" else tmp_path / "external"
    (tool / "pyvenv.cfg").write_text(
        f"version_info = 3.11.16\nhome = {home}\n", encoding="utf-8"
    )
    unsafe = {
        "writable-interpreter": interpreter,
        "writable-library": library,
    }.get(previous)
    if unsafe:
        subprocess.run(
            ["icacls.exe", str(unsafe), "/grant", "*S-1-5-32-545:M"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    source = SCRIPT.read_text(encoding="utf-8")
    callsite = source.split("    $python = Select-ManagedPython $uv $pythonDirectory", 1)[1].split(
        "    $version = $installed.version", 1
    )[0]
    wrapper = tmp_path / "replacement.ps1"
    wrapper.write_text(
        _runtime_functions(
            "Require-PrivateDataRoot", "Require-PrivateExecutablePath", "Select-ManagedPython"
        )
        + """
function Stage([string]$message) { }
function Invoke-Uv([string]$tool, [string[]]$arguments) {
    if ($arguments -contains 'list') { return $env:TEST_RECORD }
    throw 'Unexpected uv operation.'
}
function Check-Payload([string[]]$arguments) {
    [IO.File]::WriteAllText($env:TEST_MARKER, 'native install reached')
    return [pscustomobject]@{ version = '1.0.0'; wheel = 'wheels/siteops-1.0.0.whl' }
}
$uv = 'unused'
$pythonDirectory = $env:TEST_MANAGED
$toolDirectory = $env:TEST_TOOLS
$toolHome = Join-Path $toolDirectory 'siteops'
$Replace = $true
$archive = 'unused'
$bundle = 'unused'
$Repository = 'example/publisher'
$SourceCommit = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
$SourceRef = 'refs/heads/main'
"""
        + "    $python = Select-ManagedPython $uv $pythonDirectory"
        + callsite
        + "\n'REPLACEMENT_ACCEPTED'\n",
        encoding="utf-8",
    )
    record = {
        "key": "cpython-3.11.16-windows-x86_64-none",
        "version": "3.11.16",
        "version_parts": {"major": 3, "minor": 11, "patch": 16},
        "implementation": "cpython",
        "arch": "x86_64",
        "os": "windows",
        "variant": "default",
        "libc": "none",
    }
    marker = tmp_path / "native-install"
    try:
        result = _run(
            wrapper,
            tmp_path,
            TEST_MANAGED=managed,
            TEST_TOOLS=tool_directory,
            TEST_RECORD=json.dumps([record]),
            TEST_MARKER=marker,
        )
        if previous == "safe":
            assert result.returncode == 0, result.stdout + result.stderr
            assert marker.read_text(encoding="ascii") == "native install reached"
        else:
            assert result.returncode != 0
            expected = (
                "not an available uv-managed Python" if previous == "external"
                else "TOOL_ACL" if previous == "writable-interpreter" else "RUNTIME_ACL"
            )
            assert expected in result.stderr
            assert not marker.exists()
    finally:
        if unsafe:
            subprocess.run(
                ["icacls.exe", str(unsafe), "/remove:g", "*S-1-5-32-545"],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )


@pytest.mark.parametrize(("override", "tamper"), [(False, False), (True, False), (True, True)])
def test_native_maintenance_copy_is_reused_only_when_pinned(tmp_path, override, tamper):
    executable, archive = _native_fixture()
    data = tmp_path / "siteops"
    profile = tmp_path / "profile"
    profile.mkdir()
    bin_directory = tmp_path / "override-bin" if override else profile / ".local" / "bin"
    bin_directory.mkdir(parents=True)
    maintenance = bin_directory / "uv.exe"
    shutil.copy2(shutil.which("where.exe") if tamper else executable, maintenance)
    wrapper = tmp_path / "maintenance.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot", "Ensure-UvStorage", "Require-PrivateExecutablePath",
            "Assert-UvArchive", "Assert-PinnedUv", "Select-Uv",
        )
        + """
function Native([string]$name) { return $null }
function Stage([string]$message) { Write-Host $message }
$env:USERPROFILE = $env:TEST_PROFILE
$data = $env:TEST_DATA
Require-PrivateDataRoot $data
$cache = Join-Path $data 'tools\\uv\\0.12.20'
Ensure-UvStorage $cache
Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination (Join-Path $cache 'uv-windows.zip')
Copy-Item -LiteralPath $env:TEST_QUALIFIED -Destination (Join-Path $cache 'uv.exe')
$selected = Select-Uv $data $env:TEST_DATA $null
if ($selected -cne $env:TEST_MAINTENANCE) { throw 'Wrong maintenance command selected.' }
'MAINTENANCE_REUSED'
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        TEST_PROFILE=profile,
        TEST_DATA=data,
        TEST_MAINTENANCE=maintenance,
        TEST_ARCHIVE=archive,
        TEST_QUALIFIED=executable,
        **({"UV_TOOL_BIN_DIR": bin_directory} if override else {}),
    )
    if tamper:
        assert result.returncode != 0
        assert "retained native uv executable differs" in result.stderr
        assert maintenance.read_bytes() == Path(shutil.which("where.exe")).read_bytes()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MAINTENANCE_REUSED" in result.stdout


def test_native_first_exposure_honors_explicit_uv_tool_bin_dir(tmp_path):
    executable, archive = _native_fixture()
    data = tmp_path / "siteops"
    profile = tmp_path / "profile"
    profile.mkdir()
    bin_directory = tmp_path / "override-bin"
    wrapper = tmp_path / "expose.ps1"
    wrapper.write_text(
        _functions(
            "Require-PrivateDataRoot", "Ensure-UvStorage", "Require-PrivateExecutablePath",
            "Assert-UvArchive", "Assert-PinnedUv", "Select-Uv",
        )
        + """
function Native([string]$name) { return $null }
function Stage([string]$message) { Write-Host $message }
$env:USERPROFILE = $env:TEST_PROFILE
$data = $env:TEST_DATA
Require-PrivateDataRoot $data
$cache = Join-Path $data 'tools\\uv\\0.12.20'
Ensure-UvStorage $cache
Copy-Item -LiteralPath $env:TEST_ARCHIVE -Destination (Join-Path $cache 'uv-windows.zip')
Copy-Item -LiteralPath $env:TEST_QUALIFIED -Destination (Join-Path $cache 'uv.exe')
$selected = Select-Uv $data $env:TEST_DATA $null
if ($selected -cne (Join-Path $data 'tools\\uv\\0.12.20\\uv.exe')) {
    throw 'Wrong native uv selected.'
}
'EXPOSURE_ACCEPTED'
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper, tmp_path, TEST_PROFILE=profile, TEST_DATA=data, UV_TOOL_BIN_DIR=bin_directory,
        TEST_ARCHIVE=archive, TEST_QUALIFIED=executable,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXPOSURE_ACCEPTED" in result.stdout
    assert (bin_directory / "uv.exe").read_bytes() == executable.read_bytes()
    assert not (profile / ".local" / "bin" / "uv.exe").exists()


# Long failures wrap differently in each edition's error view, so wrappers report them on stdout.
_REPORTED_FAIL = "function Fail([string]$message) { Write-Host ('FAIL: ' + $message); exit 1 }\n"


_GH_OUTCOMES = {
    "accepted": "GH_SELECTED",
    "missing": "FAIL: GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry.",
    "old": "FAIL: GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry.",
    "writable": (
        "FAIL: The GitHub CLI executable must be owned by an administrator or the current user "
        "and protected from other users."
    ),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
@pytest.mark.parametrize("case", sorted(_GH_OUTCOMES))
def test_native_github_cli_is_an_admitted_prerequisite(tmp_path, shell, case):
    program = _shell(shell)
    tools = tmp_path / "gh"
    tools.mkdir()
    gh = tools / "gh.cmd"
    gh.write_text(
        "@echo off\n"
        'echo ran>"%TEST_MARKER%"\n'
        f"echo gh version {'2.94.1' if case == 'old' else '2.95.0'} (fixture)\n",
        encoding="ascii",
    )
    wrapper = tmp_path / "gh.ps1"
    wrapper.write_text(
        _functions("Require-PrivateExecutablePath", "Select-GitHubCli")
        + "\n" + _REPORTED_FAIL
        + """
function Native([string]$name) {
    if ($name -cne 'gh.exe') { throw 'Unexpected native command.' }
    if ($env:TEST_GH) { return $env:TEST_GH }
    return $null
}
$selected = Select-GitHubCli
if ($selected -cne $env:TEST_GH) { throw 'The admitted GitHub CLI path was not returned.' }
'GH_SELECTED'
""",
        encoding="utf-8",
    )
    if case == "writable":
        grant = subprocess.run(
            ["icacls.exe", str(tools), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
            capture_output=True, text=True, timeout=20,
        )
        if grant.returncode:
            pytest.skip("The test user cannot change the synthetic GitHub CLI ACL.")
    marker = tmp_path / "executed"
    try:
        result = _run(
            wrapper, tmp_path, program=program,
            TEST_GH="" if case == "missing" else gh, TEST_MARKER=marker,
        )
    finally:
        if case == "writable":
            subprocess.run(
                ["icacls.exe", str(tools), "/remove:g", "*S-1-5-32-545"],
                capture_output=True, text=True, check=True, timeout=20,
            )
    assert (result.returncode == 0) is (case == "accepted"), result.stdout + result.stderr
    assert _GH_OUTCOMES[case] in result.stdout
    # The version probe runs only after the file and its directories pass admission.
    assert marker.exists() is (case in {"accepted", "old"})


# Each case selects bin\tool.exe beneath its own folder with one spelling of the private root.
_ORDINAL_ROOTS = {
    "exact": ("{bin}", "ADMITTED"),
    "letter-case": ("{upper}", "ADMITTED"),
    # Culture comparisons ignore U+00AD, although it names another file or directory.
    "soft-hyphen-file": ("{bin}\\too\u00adl.exe", "TOOL_PATH"),
    "soft-hyphen-root": ("{folder}\\b\u00adin", "TOOL_PATH"),
    "dotless-i": ("{folder}\\b\u0131n", "TOOL_PATH"),
    "dotted-i": ("{folder}\\B\u0130N", "TOOL_PATH"),
    # The root folder takes the stricter rights of the tool root, whatever its letter case.
    "writable-root": ("{upper}", "TOOL_ACL"),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
@pytest.mark.parametrize("culture", ["en-US", "tr-TR"])
def test_native_executable_root_is_compared_ordinally(tmp_path, shell, culture):
    program = _shell(shell)
    cases = []
    for name, (root, _) in _ORDINAL_ROOTS.items():
        directory = tmp_path / name / "bin"
        directory.mkdir(parents=True)
        (directory / "tool.exe").write_bytes(b"inert")
        cases.append(
            {
                "name": name,
                "tool": str(directory / "tool.exe"),
                "root": root.format(
                    bin=directory, upper=str(directory).upper(), folder=directory.parent
                ),
            }
        )
    writable = tmp_path / "writable-root" / "bin"
    # Users may add files to this folder only, which matters only inside a tool root.
    grant = subprocess.run(
        ["icacls.exe", str(writable), "/grant", "*S-1-5-32-545:(WD)"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if grant.returncode:
        pytest.skip("The test user cannot change the synthetic tool root ACL.")
    wrapper = tmp_path / "roots.ps1"
    wrapper.write_text(
        _functions("Require-PrivateExecutablePath")
        + r"""
$culture = [Globalization.CultureInfo]::new($env:TEST_CULTURE)
[Threading.Thread]::CurrentThread.CurrentCulture = $culture
[Threading.Thread]::CurrentThread.CurrentUICulture = $culture
'UPPER_I=' + [int][char]'i'.ToUpper()
$cases = $env:TEST_CASES | ConvertFrom-Json
foreach ($case in $cases) {
    $result = try {
        Require-PrivateExecutablePath $case.tool $case.root
        'ADMITTED'
    } catch {
        [regex]::Match("$_", 'TOOL_[A-Z]+').Value
    }
    $case.name + '=' + $result
}
""",
        encoding="utf-8",
    )
    try:
        result = _run(
            wrapper, tmp_path, program=program, TEST_CULTURE=culture, TEST_CASES=json.dumps(cases)
        )
    finally:
        subprocess.run(
            ["icacls.exe", str(writable), "/remove:g", "*S-1-5-32-545"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    # Turkish uppercases i to U+0130, so this shows the culture applies.
    assert lines[0] == f"UPPER_I={0x130 if culture == 'tr-TR' else ord('I')}"
    assert dict(line.split("=", 1) for line in lines[1:]) == {
        name: expected for name, (_, expected) in _ORDINAL_ROOTS.items()
    }


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_staging_is_private_per_run_and_replaces_temporary_storage(tmp_path, shell):
    program = _shell(shell)
    source = SCRIPT.read_text(encoding="utf-8")
    prefix = "$staging = Join-Path $data 'install-staging'\n"
    # The staging setup ends where source discovery begins.
    setup = prefix + source.split(prefix, 1)[1].split("    if (-not $commitSupplied) {\n", 1)[0]
    cleanup = "finally {\n" + source.split("\nfinally {\n", 1)[1]
    wrapper = tmp_path / "staging.ps1"
    wrapper.write_text(
        _functions("Require-PrivateDataRoot")
        + "\n$data = $env:TEST_DATA\nRequire-PrivateDataRoot $data\n"
        + setup
        + """
    $acl = Read-NodeAcl $download -Directory
    'RUN=' + $download
    'RUN_OWNED=' + ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ceq
        [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    'RUN_PROTECTED=' + $acl.AreAccessRulesProtected
    'TEMP=' + $env:TEMP
    'TMP=' + $env:TMP
    'TMPDIR=' + $env:TMPDIR
    [IO.File]::WriteAllText((Join-Path $env:TEMP 'scratch'), 'private')
}
"""
        + cleanup
        + "\n'AFTER_TEMP=' + $env:TEMP\n'AFTER_TMPDIR=' + $env:TMPDIR\n",
        encoding="utf-8",
    )
    data = tmp_path / "siteops"
    environment = {"TEST_DATA": data, "TEMP": tmp_path, "TMP": tmp_path, "TMPDIR": "caller-tmpdir"}
    first = _run(wrapper, tmp_path, program=program, **environment)
    assert first.returncode == 0, first.stdout + first.stderr

    staging = data / "install-staging"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="ascii")
    stale = staging / "stale-plain"
    stale.mkdir()
    (stale / "partial.zip").write_bytes(b"partial")
    linked = staging / "stale-linked"
    (linked / "nested").mkdir(parents=True)
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(linked / "nested" / "link"), str(outside)],
        capture_output=True, text=True, check=True, timeout=20,
    )
    stale_file = staging / "stale-file"
    stale_file.write_bytes(b"partial")
    fresh = staging / "fresh"
    fresh.mkdir()
    expired = time.time() - 2 * 86400
    for path in (stale, linked, stale_file):
        os.utime(path, (expired, expired))

    second = _run(wrapper, tmp_path, program=program, **environment)
    assert second.returncode == 0, second.stdout + second.stderr
    values = dict(line.split("=", 1) for line in second.stdout.splitlines() if "=" in line)
    run = Path(values["RUN"])
    assert run.parent == staging
    assert values["RUN_OWNED"] == "True" and values["RUN_PROTECTED"] == "True"
    assert values["TEMP"] == values["TMP"] == values["TMPDIR"] == str(run / "temp")
    assert values["AFTER_TEMP"] == str(tmp_path) and values["AFTER_TMPDIR"] == "caller-tmpdir"
    assert not run.exists()
    assert not stale.exists() and not stale_file.exists()
    assert fresh.is_dir()
    # Removing a stale entry never follows a link out of private staging.
    assert (outside / "keep.txt").read_text(encoding="ascii") == "keep"


@pytest.mark.parametrize("occupied", [False, True])
def test_native_retained_downloads_move_into_new_private_caches(tmp_path, occupied):
    source = SCRIPT.read_text(encoding="utf-8")
    start = "    if ($assets -eq $download) {\n"
    retain = start + source.split(start, 1)[1].split(
        "\n    if ($EnrollSource) {\n        if ($env:SITEOPS_REDACT_OUTPUT", 1,
    )[0]
    wrapper = tmp_path / "retain.ps1"
    wrapper.write_text(
        _functions("Require-PrivateDataRoot")
        + "\n" + _REPORTED_FAIL
        + """
$data = $env:TEST_DATA
Require-PrivateDataRoot $data
$download = Join-Path $data 'staging'
Require-PrivateDataRoot $download
$referenceDownload = Join-Path $download 'reference'
Require-PrivateDataRoot $referenceDownload
foreach ($name in @('siteops-install.zip', 'siteops-install.zip.attestation.jsonl')) {
    [IO.File]::WriteAllText((Join-Path $download $name), $name)
}
foreach ($name in @('siteops-engine.json', 'siteops-engine.json.attestation.jsonl')) {
    [IO.File]::WriteAllText((Join-Path $referenceDownload $name), $name)
}
$assets = $download
$cache = Join-Path $data 'release'
$referenceCache = Join-Path $data 'reference'
if ($env:TEST_OCCUPIED) { Require-PrivateDataRoot $cache }
"""
        + retain
        + "\n'RETAINED'\n",
        encoding="utf-8",
    )
    data = tmp_path / "siteops"
    result = _run(wrapper, tmp_path, TEST_DATA=data, TEST_OCCUPIED="1" if occupied else "")
    bundle = ("siteops-install.zip", "siteops-install.zip.attestation.jsonl")
    reference = ("siteops-engine.json", "siteops-engine.json.attestation.jsonl")
    if occupied:
        assert result.returncode != 0
        assert "FAIL: The retained release location changed during installation." in result.stdout
        assert not any((data / "release").iterdir())
        assert all((data / "staging" / name).is_file() for name in bundle)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RETAINED" in result.stdout
    for cache, staged, names in (
        (data / "release", data / "staging", bundle),
        (data / "reference", data / "staging" / "reference", reference),
    ):
        assert sorted(item.name for item in cache.iterdir()) == sorted(names)
        assert all((cache / name).read_text(encoding="ascii") == name for name in names)
        assert not any((staged / name).exists() for name in names)


# The stubs model the saved user PATH as its registry kind and raw text. The uv stub
# writes the way uv 0.12.20 does: it prepends the raw text and always saves REG_EXPAND_SZ.
_PATH_STUBS = r"""
function Stage([string]$message) { Write-Host "STAGE $message" }
$script:userPath = $env:TEST_USER_PATH
$script:userKind = if ($env:TEST_USER_KIND) { $env:TEST_USER_KIND } else { 'ExpandString' }
$script:uvCalls = 0
function Read-SavedPath([string]$Scope) {
    if ($Scope -ceq 'Machine') { return $env:TEST_MACHINE_PATH }
    if ($script:userKind -ceq 'Binary') { return $null }
    if ($script:userKind -ceq 'ExpandString') { return [Environment]::ExpandEnvironmentVariables($script:userPath) }
    return $script:userPath
}
function Read-RawUserPath() {
    if ($script:userKind -ceq 'Binary') { return $null }
    return @{ Kind = $script:userKind; Value = $script:userPath }
}
function Invoke-Uv([string]$Executable, [string[]]$Arguments) {
    $script:uvCalls++
    if ($Executable -cne $env:TEST_UV -or ($Arguments -join ' ') -cne 'tool update-shell') {
        throw 'Unexpected uv call.'
    }
    $tail = $script:userPath
    switch -CaseSensitive ($env:TEST_UV_RESULT) {
        'fail' { throw 'Site Ops installation failed: uv' }
        'unchanged' { return }
        'plain' { $script:userKind = 'String' }
        'expand' { $script:userKind = 'ExpandString'; $tail = [Environment]::ExpandEnvironmentVariables($tail) }
        default { $script:userKind = 'ExpandString' }
    }
    $script:userPath = if ($tail) { $env:TEST_DIRECTORY + ';' + $tail } else { $env:TEST_DIRECTORY }
}
"""

# Console code pages differ, so wrappers print characters outside printable ASCII as \uXXXX.
_SHOW_TEXT = r"""
function Show-Text([string]$text) {
    return [regex]::Replace($text, '[^ -~]', { param($found) '\u{0:X4}' -f [int][char]$found.Value })
}
"""


def _shown(text):
    return "".join(character if " " <= character <= "~" else f"\\u{ord(character):04X}" for character in text)


@pytest.mark.parametrize(
    ("case", "kind", "saved", "uv_result", "state", "calls"),
    [
        ("absent", "ExpandString", "C:\\Tools;{other}", "prepend", "added", 1),
        ("plain-text", "String", "C:\\Tools;{other}", "prepend", "added", 1),
        ("no-value", "None", "", "prepend", "added", 1),
        ("expandable-names", "ExpandString", "%SystemRoot%\\Tools;C:\\Tools", "prepend", "added", 1),
        ("literal-names", "String", "%SystemRoot%\\Tools;C:\\Tools", "prepend", "literal", 0),
        ("named-directory", "ExpandString", "C:\\Tools", "prepend", "directory", 0),
        ("present", "ExpandString", "C:\\Tools;{directory}", "fail", "present", 0),
        ("present-variant", "String", "C:\\Tools;{upper}\\", "fail", "present", 0),
        ("present-by-name", "ExpandString", "%TEST_HOME%\\.local\\bin", "fail", "present", 0),
        ("present-forward-slashes", "ExpandString", "C:\\Tools;{slashes}", "fail", "present", 0),
        ("present-unc", "ExpandString", "C:\\Tools;{unc}", "fail", "present", 0),
        # Entries relative to the current drive depend on where a terminal starts, so the absolute one is added.
        ("root-relative", "ExpandString", "C:\\Tools;{root_relative}", "prepend", "added", 1),
        ("drive-relative", "ExpandString", "C:\\Tools;{drive_relative}", "prepend", "added", 1),
        # Command discovery skips these spellings, and uv adds the directory when its text is absent.
        ("soft-hyphen", "ExpandString", "C:\\Tools;{soft_hyphen}", "prepend", "added", 1),
        ("byte-order-mark", "String", "C:\\Tools;{byte_order_mark}", "prepend", "added", 1),
        # uv 0.12.20 treats the directory as present when its text appears anywhere in the value.
        ("substring", "ExpandString", "{directory}2", "unchanged", "unchanged", 1),
        ("substring-plain-text", "String", "C:\\Tools;{directory}2", "unchanged", "unchanged", 1),
        ("quoted", "String", 'C:\\Tools;"{directory}"', "unchanged", "unchanged", 1),
        ("trailing-space", "ExpandString", "C:\\Tools;{directory} ", "unchanged", "unchanged", 1),
        ("uv-fails", "ExpandString", "C:\\Tools", "fail", "failed", 1),
        ("binary", "Binary", "", "prepend", "failed", 0),
        ("saved-as-plain-text", "ExpandString", "C:\\Tools", "plain", "failed", 1),
        ("other-entries-changed", "ExpandString", "%SystemRoot%\\Tools;C:\\Tools", "expand", "failed", 1),
    ],
)
def test_native_command_directory_is_saved_once_through_uv(tmp_path, case, kind, saved, uv_result, state, calls):
    home = tmp_path / ("a%TEST_HOME%b" if case == "named-directory" else "home")
    directory = home / ".local" / "bin"
    directory.mkdir(parents=True)
    (directory / "siteops.exe").write_bytes(b"inert")
    # The administrative share names this folder through the server on this host.
    unc = PureWindowsPath(f"\\\\localhost\\{directory.drive[0]}$", *directory.parts[1:])
    if case == "present-unc" and not Path(unc, "siteops.exe").is_file():
        pytest.skip("This host offers no administrative share for a UNC spelling of the folder.")
    wrapper = tmp_path / "save-path.ps1"
    wrapper.write_text(
        _functions(
            "Test-QualifiedPath", "Get-SearchedFolder", "Get-FileIdentity", "Test-SameDirectory",
            "Save-CommandDirectory",
        )
        + _PATH_STUBS
        + _SHOW_TEXT
        + "$state = Save-CommandDirectory $env:TEST_UV $env:TEST_DIRECTORY\n"
        + "\"STATE=$state CALLS=$script:uvCalls\"\n"
        + "\"KIND=$script:userKind\"\n"
        + "\"USER=$(Show-Text $script:userPath)\"\n",
        encoding="utf-8",
    )
    user_path = saved.format(
        directory=directory, upper=str(directory).upper(), other=tmp_path / "other",
        slashes=directory.as_posix(), root_relative=str(directory)[len(directory.drive):],
        drive_relative=directory.drive + str(directory.relative_to(tmp_path)) if case == "drive-relative" else "",
        soft_hyphen=directory.parent / "b\u00adin", byte_order_mark=directory.parent / "b\ufeffin", unc=unc,
    )
    result = _run(
        wrapper, tmp_path, TEST_UV="C:\\uv\\uv.exe", TEST_DIRECTORY=directory, TEST_HOME=home,
        TEST_USER_PATH=user_path, TEST_USER_KIND=kind, TEST_UV_RESULT=uv_result, TEST_MACHINE_PATH="",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"STATE={state} CALLS={calls}\n" in result.stdout
    if state == "added":
        expected = f"{directory};{user_path}" if user_path else str(directory)
        assert f"KIND=ExpandString\nUSER={_shown(expected)}\n" in result.stdout
    elif state == "unchanged" or not calls:
        assert f"KIND={kind}\nUSER={_shown(user_path)}\n" in result.stdout


# Each spelling names the command directory σbin. Its sibling ςbin holds another siteops.exe. Final sigma and
# sigma differ in NTFS names, although the ordinal comparison of .NET 10 treats them as one letter in two cases.
_SEARCHED_SPELLINGS = {
    "exact": ("{directory}", "ours"),
    "letter-case": ("{upper}", "ours"),
    "trailing-separator": ("{directory}\\", "ours"),
    "forward-slashes": ("{slashes}", "ours"),
    "leading-space": (" {directory}", "ours"),
    "trailing-dot": ("{directory}.", "ours"),
    "dot-segment": ("{directory}\\.", "ours"),
    "parent-segment": ("{directory}\\..\\\u03c3bin", "ours"),
    "doubled-separator": ("{parent}\\\\\u03c3bin", "ours"),
    "quoted": ('"{directory}"', "none"),
    "trailing-space": ("{directory} ", "none"),
    "soft-hyphen": ("{parent}\\\u03c3b\u00adin", "none"),
    "byte-order-mark": ("{parent}\\\u03c3b\ufeffin", "none"),
    "final-sigma": ("{parent}\\\u03c2bin", "other"),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_counted_command_directory_is_one_discovery_searches(tmp_path, shell):
    program = _shell(shell)
    directory = tmp_path / "home" / ".local" / "\u03c3bin"
    directory.mkdir(parents=True)
    # Discovery only reads the command. Nothing runs it.
    (directory / "siteops.exe").write_text("ours", encoding="ascii")
    (directory.parent / "\u03c2bin").mkdir()
    (directory.parent / "\u03c2bin" / "siteops.exe").write_text("other", encoding="ascii")
    entries = {
        name: spelling.format(
            directory=directory,
            upper=str(directory).upper(),
            slashes=directory.as_posix(),
            parent=directory.parent,
        )
        for name, (spelling, _) in _SEARCHED_SPELLINGS.items()
    }
    wrapper = tmp_path / "discovery.ps1"
    wrapper.write_text(
        _functions(
            "Test-QualifiedPath",
            "Get-SearchedFolder",
            "Get-FileIdentity",
            "Test-SameDirectory",
            "Save-CommandDirectory",
        )
        + _PATH_STUBS
        + r"""
$entries = $env:TEST_ENTRIES | ConvertFrom-Json
foreach ($name in $entries.PSObject.Properties.Name) {
    $script:userPath = $entries.$name
    $script:userKind = 'String'
    $script:uvCalls = 0
    $state = Save-CommandDirectory $env:TEST_UV $env:TEST_DIRECTORY
    $env:PATH = $entries.$name
    $command = Get-Command 'siteops.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $found = if ($command) { [IO.File]::ReadAllText($command.Source) } else { 'none' }
    "$name=$state $script:uvCalls $found"
}
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        program=program,
        TEST_ENTRIES=json.dumps(entries),
        TEST_DIRECTORY=directory,
        TEST_UV="C:\\uv\\uv.exe",
        TEST_UV_RESULT="unchanged",
        TEST_MACHINE_PATH="",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The saver counts an entry, and asks uv for nothing, exactly when discovery finds this command through it.
    assert dict(line.split("=", 1) for line in result.stdout.splitlines()) == {
        name: "present 0 ours" if found == "ours" else f"unchanged 1 {found}"
        for name, (_, found) in _SEARCHED_SPELLINGS.items()
    }


@pytest.mark.parametrize(
    ("state", "reason", "uv_step"),
    [
        ("literal", "Your user PATH is unchanged because it is saved as plain text that contains %", False),
        ("directory", "Your user PATH is unchanged because the command directory contains %", False),
        ("unchanged", "Your user PATH is unchanged because uv found the directory text inside another entry", False),
        ("failed", "Your user PATH could not be updated.", True),
        ("kept", "Your saved PATH is unchanged.", True),
    ],
)
def test_native_unchanged_user_path_names_the_reason_and_safe_steps(tmp_path, state, reason, uv_step):
    source = SCRIPT.read_text(encoding="utf-8")
    block = re.search(
        r"(?ms)^    \$pathState = if \(\$NoModifyPath\).*?(?=^    \$earlier = \$null)", source,
    )
    assert block, "The production PATH report is missing."
    wrapper = tmp_path / "report.ps1"
    wrapper.write_text(
        _functions("Write-PathSteps")
        + _PATH_STUBS
        + "function Save-CommandDirectory([string]$Uv, [string]$Directory) { return $env:TEST_STATE }\n"
        + "$NoModifyPath = $env:TEST_STATE -ceq 'kept'\n"
        + "$uv = $env:TEST_UV\n$commandDirectory = $env:TEST_DIRECTORY\n"
        + block.group(),
        encoding="utf-8",
    )
    result = _run(
        wrapper, tmp_path, TEST_STATE=state, TEST_UV="C:\\uv\\uv.exe", TEST_DIRECTORY=tmp_path / "bin",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.removeprefix("STAGE ") for line in result.stdout.splitlines()]
    assert lines[0].startswith(reason)
    assert lines[1].startswith("To use siteops in this terminal, run: ")
    assert ("tool update-shell" in lines[2]) is uv_step
    assert len(lines) == 3


# Each case names the registry read that fails, the -NoModifyPath choice, the saved user PATH and the lines after
# the command directory. STEPS stands for the two printed PATH commands, and OTHER for the other siteops.exe.
_ADVISORY_CASES = {
    "saved": ("", False, "other", ["Saved the command directory to your user PATH."]),
    "kept": (
        "",
        True,
        "other",
        ["Your saved PATH is unchanged.", "STEPS", "Warning: new terminals run OTHER"],
    ),
    "saved-path-read": (
        "saved",
        False,
        "other",
        [
            "Your user PATH could not be checked.",
            "STEPS",
            "Your saved PATH could not be checked for another siteops",
        ],
    ),
    "kept-saved-path-read": (
        "saved",
        True,
        "other",
        [
            "Your saved PATH is unchanged.",
            "STEPS",
            "Your saved PATH could not be checked for another siteops",
        ],
    ),
    "raw-read-before-uv": (
        "raw-before",
        False,
        "other",
        ["Your user PATH could not be checked.", "STEPS", "Warning: new terminals run OTHER"],
    ),
    "raw-read-after-uv": (
        "raw-after",
        False,
        "other",
        ["Your user PATH could not be checked.", "STEPS"],
    ),
}
# When the file identity helper cannot be compiled or used, only the exact command directory text counts as this
# installation, and no other command can be told apart from it.
_BLOCKED_CASES = {
    "saved": ("", False, "other", ["Saved the command directory to your user PATH."]),
    "present": ("", False, "ours", ["Your user PATH already includes the command directory."]),
    "kept": ("", True, "other", ["Your saved PATH is unchanged.", "STEPS"]),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
@pytest.mark.parametrize("compiler", ["available", "blocked", "mismatched"])
def test_native_path_steps_report_failures_and_continue(tmp_path, shell, compiler):
    program = _shell(shell)
    cases = _ADVISORY_CASES if compiler == "available" else _BLOCKED_CASES
    directory = tmp_path / "bin"
    other = tmp_path / "other"
    for folder in (directory, other):
        folder.mkdir()
        (folder / "siteops.exe").write_bytes(folder.name.encode("ascii"))
    source = SCRIPT.read_text(encoding="utf-8")
    block = re.search(
        r"(?ms)^    Stage \"Command directory: \$commandDirectory\.\"\n.*?"
        r"^    Stage 'For native removal, run uv tool uninstall siteops\.'\n",
        source.replace("\r\n", "\n"),
    )
    assert block, "The production PATH report is missing."
    wrapper = tmp_path / "advisory.ps1"
    wrapper.write_text(
        _functions(
            "Test-QualifiedPath",
            "Get-SearchedFolder",
            "Get-FileIdentity",
            "Test-SameDirectory",
            "Save-CommandDirectory",
            "Find-EarlierSiteOps",
            "Write-PathSteps",
        )
        + r"""
function Stage([string]$message) { Write-Host "STAGE $message" }
function Denied { throw [Security.SecurityException]::new('Registry read is denied.') }
function Read-SavedPath([string]$Scope) {
    if ($case.fault -ceq 'saved') { Denied }
    if ($Scope -ceq 'Machine') { return '' }
    return $script:userPath
}
function Read-RawUserPath() {
    $script:rawReads++
    if (($case.fault -ceq 'raw-before' -and $script:rawReads -eq 1) -or
        ($case.fault -ceq 'raw-after' -and $script:rawReads -eq 2)) { Denied }
    return @{ Kind = 'ExpandString'; Value = $script:userPath }
}
# uv prepends the command directory to the saved user PATH.
function Invoke-Uv([string]$Executable, [string[]]$Arguments) {
    $script:userPath = $commandDirectory + ';' + $script:userPath
}
if ($env:TEST_COMPILER -ceq 'blocked') {
    function Add-Type { throw 'Compiling is blocked on this host.' }
} elseif ($env:TEST_COMPILER -ceq 'mismatched') {
    # An earlier helper with the same name in this session has no Of method.
    Add-Type -TypeDefinition 'public static class SiteOpsFileIdentity { }'
}
$uv = 'C:\uv\uv.exe'
$commandDirectory = $env:TEST_DIRECTORY
foreach ($case in ($env:TEST_CASES | ConvertFrom-Json)) {
    Write-Host "CASE $($case.name)"
    $NoModifyPath = $case.kept
    $script:userPath = if ($case.user -ceq 'ours') { $commandDirectory } else { $env:TEST_OTHER }
    $script:rawReads = 0
"""
        + block.group()
        + """
    Write-Host 'CONTINUED'
}
""",
        encoding="utf-8",
    )
    tests = [
        {"name": name, "fault": fault, "kept": kept, "user": user}
        for name, (fault, kept, user, _) in cases.items()
    ]
    result = _run(
        wrapper,
        tmp_path,
        program=program,
        TEST_CASES=json.dumps(tests),
        TEST_DIRECTORY=directory,
        TEST_OTHER=other,
        TEST_COMPILER=compiler,
        PATHEXT=".COM;.EXE;.BAT;.CMD",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reported = {}
    for line in result.stdout.splitlines():
        if line.startswith("CASE "):
            name = line.removeprefix("CASE ")
            reported[name] = []
        elif line.startswith("STAGE ") and not line.startswith("STAGE Command directory:"):
            reported[name].append(line.removeprefix("STAGE "))
        elif line == "CONTINUED":
            reported[name].append(line)
    steps = (
        "To use siteops in this terminal, run: ",
        "For new terminals, add the directory to your user PATH",
    )
    removal = "For native removal, run uv tool uninstall siteops."
    for name, (_, _, _, expected) in cases.items():
        lines = reported[name]
        assert lines[-2:] == [removal, "CONTINUED"], name
        prefixes = []
        for line in expected:
            if line == "STEPS":
                prefixes += steps
            else:
                prefixes.append(line.replace("OTHER", str(other / "siteops.exe")))
        assert len(lines) == len(prefixes) + 2, (name, lines)
        assert all(line.startswith(prefix) for line, prefix in zip(lines, prefixes)), (name, lines)


_EARLIER_CASES = {
    "machine-first": ("{machine}", "{ours};{later}", {"machine": "siteops.exe"}, "machine"),
    "user-before": ("", "{before};{ours}", {"before": "siteops.cmd"}, "before"),
    "ours-first": ("", "{ours};{later}", {"later": "siteops.exe"}, None),
    "ours-in-machine": (
        "{ours};{machine}",
        "{before}",
        {"machine": "siteops.exe", "before": "siteops.exe"},
        None,
    ),
    "absent": ("", "{later}", {"later": "siteops.exe"}, "later"),
    "relative": (".;{ours}", "", {".": "siteops.exe"}, None),
    "directory": ("{before};{ours}", "", {"before": "siteops.exe/"}, None),
    # A new terminal on this drive runs this installation through the entry relative to the drive root.
    "ours-root-relative": ("", "{ours_root_relative};{later}", {"later": "siteops.exe"}, None),
    "ours-trailing-dot": ("", "{ours}.;{later}", {"later": "siteops.exe"}, None),
    # Discovery skips a quoted or space padded entry, so a later command runs instead of this installation.
    "ours-quoted": ("", '"{ours}";{later}', {"later": "siteops.exe"}, "later"),
    "ours-trailing-space": ("", "{ours} ;{later}", {"later": "siteops.exe"}, "later"),
    "ours-quoted-only": ("", '"{ours}"', {}, None),
    # For the same reason, another installation behind such an entry never runs first.
    "before-quoted": ("", '"{before}";{ours}', {"before": "siteops.cmd"}, None),
    "before-trailing-space": ("", "{before} ;{ours}", {"before": "siteops.cmd"}, None),
    "machine-quoted": ('"{machine}"', "{ours}", {"machine": "siteops.exe"}, None),
    # Discovery ignores spaces before an entry, so that installation runs first.
    "before-leading-space": ("", " {before};{ours}", {"before": "siteops.cmd"}, "before"),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_earlier_siteops_follows_new_terminal_order(tmp_path, shell):
    program = _shell(shell)
    cases, expected = [], {}
    for case, (machine, user, files, found) in _EARLIER_CASES.items():
        folders = {name: tmp_path / case / name for name in ("machine", "before", "ours", "later")}
        # A relative entry names the folder where the wrapper runs.
        folders["."] = tmp_path
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        (folders["ours"] / "siteops.exe").write_bytes(b"selected")
        for name, filename in files.items():
            target = folders[name] / filename.rstrip("/")
            if filename.endswith("/"):
                target.mkdir()
            else:
                target.write_bytes(b"other")
        values = {key: str(value) for key, value in folders.items() if key != "."}
        values["ours_root_relative"] = values["ours"][len(folders["ours"].drive) :]
        cases.append(
            {
                "name": case,
                "directory": values["ours"],
                "machine": machine.format(**values),
                "user": user.format(**values),
            }
        )
        expected[case] = "" if found is None else str(folders[found] / files[found])
    wrapper = tmp_path / "earlier.ps1"
    wrapper.write_text(
        _functions("Get-SearchedFolder", "Get-FileIdentity", "Find-EarlierSiteOps")
        + _PATH_STUBS
        + r"""
foreach ($case in ($env:TEST_CASES | ConvertFrom-Json)) {
    $env:TEST_MACHINE_PATH = $case.machine
    $script:userPath = $case.user
    $found = Find-EarlierSiteOps $case.directory
    $case.name + '=' + $found
}
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        program=program,
        TEST_CASES=json.dumps(cases),
        TEST_UV="unused",
        PATHEXT=".COM;.EXE;.BAT;.CMD",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert dict(line.split("=", 1) for line in result.stdout.splitlines()) == expected


# Each case lists the saved user PATH and which file identity Windows does not report.
_UNKNOWN_IDENTITIES = {
    "installed-unknown": ("{ours}", "installed", None),
    "found-unknown": ("{ours}", "found", None),
    "other-installed-unknown": ("{other};{ours}", "installed", None),
    "other-found-unknown": ("{other};{ours}", "found", None),
    "other-known": ("{other};{ours}", "", "other"),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_earlier_siteops_needs_both_identities(tmp_path, shell):
    program = _shell(shell)
    cases, expected = [], {}
    for case, (user, unknown, found) in _UNKNOWN_IDENTITIES.items():
        folders = {name: tmp_path / case / name for name in ("ours", "other")}
        for name, folder in folders.items():
            folder.mkdir(parents=True)
            (folder / "siteops.exe").write_bytes(name.encode("ascii"))
        values = {name: str(folder) for name, folder in folders.items()}
        cases.append(
            {
                "name": case,
                "directory": values["ours"],
                "user": user.format(**values),
                "unknown": unknown,
            }
        )
        expected[case] = "" if found is None else str(folders[found] / "siteops.exe")
    wrapper = tmp_path / "unknown.ps1"
    wrapper.write_text(
        _functions("Get-SearchedFolder", "Get-FileIdentity", "Find-EarlierSiteOps")
        + _PATH_STUBS
        + r"""
${function:Get-KnownFileIdentity} = ${function:Get-FileIdentity}
function Get-FileIdentity([string]$Path) {
    $script:identities++
    # The search identifies the installed command before any command it finds.
    $installed = $script:identities -eq 1
    if (($case.unknown -ceq 'installed' -and $installed) -or ($case.unknown -ceq 'found' -and -not $installed)) {
        return ''
    }
    return Get-KnownFileIdentity $Path
}
$env:TEST_MACHINE_PATH = ''
foreach ($case in ($env:TEST_CASES | ConvertFrom-Json)) {
    $script:userPath = $case.user
    $script:identities = 0
    $found = Find-EarlierSiteOps $case.directory
    $case.name + '=' + $found
}
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        program=program,
        TEST_CASES=json.dumps(cases),
        TEST_UV="unused",
        PATHEXT=".COM;.EXE;.BAT;.CMD",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Only a different installation with a known identity is named.
    assert dict(line.split("=", 1) for line in result.stdout.splitlines()) == expected


# Each case lists the saved user PATH and the files beyond this installation's siteops.exe.
_SCRIPT_CASES = {
    "script-before": ("{before};{ours}", {"before": ["siteops.ps1"]}),
    "script-before-command": ("{before};{ours}", {"before": ["siteops.cmd", "siteops.ps1"]}),
    "script-beside-ours": ("{ours}", {"ours": ["siteops.ps1"]}),
    "script-after": ("{ours};{later}", {"later": ["siteops.ps1"]}),
    "script-quoted": ('"{before}";{ours}', {"before": ["siteops.ps1"]}),
    "command-before": ("{before};{ours}", {"before": ["siteops.cmd"]}),
}


@pytest.mark.parametrize("shell", ["powershell.exe", "pwsh"])
def test_native_earlier_siteops_script_follows_discovery(tmp_path, shell):
    program = _shell(shell)
    cases = []
    for case, (user, files) in _SCRIPT_CASES.items():
        folders = {name: tmp_path / case / name for name in ("before", "ours", "later")}
        for folder in folders.values():
            folder.mkdir(parents=True)
        (folders["ours"] / "siteops.exe").write_bytes(b"ours")
        for name, filenames in files.items():
            for filename in filenames:
                # Discovery only resolves the script. Nothing runs it.
                (folders[name] / filename).write_text("exit 1\n", encoding="ascii")
        values = {name: str(folder) for name, folder in folders.items()}
        cases.append({"name": case, "directory": values["ours"], "user": user.format(**values)})
    wrapper = tmp_path / "script.ps1"
    wrapper.write_text(
        _functions("Get-SearchedFolder", "Get-FileIdentity", "Find-EarlierSiteOps")
        + _PATH_STUBS
        + r"""
$env:TEST_MACHINE_PATH = ''
foreach ($case in ($env:TEST_CASES | ConvertFrom-Json)) {
    $script:userPath = $case.user
    $found = Find-EarlierSiteOps $case.directory
    $env:PATH = $case.user
    $runs = (Get-Command siteops -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    $case.name + '=' + $found + '|' + $runs
}
""",
        encoding="utf-8",
    )
    result = _run(
        wrapper,
        tmp_path,
        program=program,
        TEST_CASES=json.dumps(cases),
        TEST_UV="unused",
        PATHEXT=".COM;.EXE;.BAT;.CMD",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reported = dict(line.split("=", 1) for line in result.stdout.splitlines())
    expected = {
        "script-before": "before\\siteops.ps1",
        "script-before-command": "before\\siteops.ps1",
        "script-beside-ours": "ours\\siteops.ps1",
        "script-after": "",
        "script-quoted": "",
        "command-before": "before\\siteops.cmd",
    }
    for case in _SCRIPT_CASES:
        found, runs = reported[case].split("|")
        ours = str(tmp_path / case / "ours" / "siteops.exe")
        # The warning names exactly the command a new terminal runs instead of this installation.
        assert found == ("" if runs == ours else runs), case
        assert found == (str(tmp_path / case / expected[case]) if expected[case] else ""), case


def test_native_saved_path_reader_matches_the_windows_environment(tmp_path):
    wrapper = tmp_path / "reader.ps1"
    wrapper.write_text(
        _functions("Read-SavedPath")
        + """
foreach ($scope in @('Machine', 'User')) {
    $expected = [Environment]::GetEnvironmentVariable('Path', $scope)
    if ($null -eq $expected) { $expected = '' }
    if ((Read-SavedPath $scope) -cne $expected) { throw "The $scope reader differs." }
}
'READER_MATCHES'
""",
        encoding="utf-8",
    )
    result = _run(wrapper, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "READER_MATCHES" in result.stdout


def test_native_raw_user_path_reader_keeps_names_unexpanded(tmp_path):
    import winreg

    output = tmp_path / "raw.json"
    wrapper = tmp_path / "raw-reader.ps1"
    wrapper.write_text(
        _functions("Read-RawPath", "Read-RawUserPath")
        + "$raw = Read-RawUserPath\n"
        + "$text = if ($null -eq $raw) { 'null' } else { $raw | ConvertTo-Json -Compress }\n"
        + "[IO.File]::WriteAllText($env:TEST_OUTPUT, $text, (New-Object Text.UTF8Encoding $false))\n",
        encoding="utf-8",
    )
    result = _run(wrapper, tmp_path, TEST_OUTPUT=output)
    assert result.returncode == 0, result.stdout + result.stderr
    kinds = {winreg.REG_SZ: "String", winreg.REG_EXPAND_SZ: "ExpandString"}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        expected = {"Kind": "None", "Value": ""}
    else:
        expected = {"Kind": kinds[kind], "Value": value} if kind in kinds else None
    assert json.loads(output.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize(
    ("names", "kind", "expected"),
    [
        ("Path", "ExpandString", "KIND=ExpandString VALUE=%TEST_NAME%\\Tools;C:\\Tools"),
        ("PATH", "String", "KIND=String VALUE=%TEST_NAME%\\Tools;C:\\Tools"),
        ("Other", "String", "KIND=None VALUE="),
        ("Path", "MultiString", "NOT_TEXT"),
    ],
)
def test_native_raw_path_reader_asks_for_unexpanded_names(tmp_path, names, kind, expected):
    wrapper = tmp_path / "raw-path.ps1"
    wrapper.write_text(
        _functions("Read-RawPath")
        + r"""
$script:disposed = $false
$key = New-Object PSObject
$key | Add-Member ScriptMethod GetValueNames { , @($env:TEST_NAMES) }
$key | Add-Member ScriptMethod GetValueKind { param($name) [Microsoft.Win32.RegistryValueKind]$env:TEST_KIND }
$key | Add-Member ScriptMethod GetValue {
    param($name, $default, $options)
    $raw = '%TEST_NAME%\Tools;C:\Tools'
    # A registry key expands REG_EXPAND_SZ names unless the caller asks for the raw text.
    if ($options -ne [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) {
        return [Environment]::ExpandEnvironmentVariables($raw)
    }
    return $raw
}
$key | Add-Member ScriptMethod Dispose { $script:disposed = $true }
$raw = Read-RawPath $key
if (-not $script:disposed) { throw 'The key stayed open.' }
if ($null -eq $raw) { 'NOT_TEXT' } else { "KIND=$($raw.Kind) VALUE=$($raw.Value)" }
""",
        encoding="utf-8",
    )
    result = _run(wrapper, tmp_path, TEST_NAMES=names, TEST_KIND=kind, TEST_NAME="C:\\Expanded")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


_STEPS_CHECK = r"""
Invoke-Expression $env:TEST_SESSION
if ($env:Path.Split(';')[0] -cne $env:TEST_DIRECTORY) { throw 'The session command added another directory.' }
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($env:TEST_SAVED, [ref]$null, [ref]$errors)
if ($errors) { throw 'The saved PATH command does not parse.' }
$command = $ast.EndBlock.Statements[0].PipelineElements[0]
$words = @($command.CommandElements | ForEach-Object { $_.Value })
if ($command.InvocationOperator -ne 'Ampersand' -or ($words -join '|') -cne ($env:TEST_UV + '|tool|update-shell')) {
    throw 'The saved PATH command runs something else.'
}
'STEPS_RUN'
"""


@pytest.mark.parametrize(("folder", "tool"), [
    ("it's bin", "o'tool"),
    # PowerShell also ends a single quoted string at U+2018 to U+201B.
    ("O\u2019Brien \u2018bin\u201b", "o\u201atool"),
])
def test_native_manual_path_steps_run_as_printed(tmp_path, folder, tool):
    directory = tmp_path / folder
    uv = tmp_path / tool / "uv.exe"
    printed = tmp_path / "steps.txt"
    wrapper = tmp_path / "steps.ps1"
    wrapper.write_text(
        _functions("Write-PathSteps")
        + _PATH_STUBS
        # Keep the printed text exact, whatever the console code page.
        + "function Stage([string]$message) {\n"
        + "    [IO.File]::AppendAllText($env:TEST_PRINTED, $message + \"`n\", (New-Object Text.UTF8Encoding $false))\n"
        + "}\n"
        + "Write-PathSteps $env:TEST_UV $env:TEST_DIRECTORY\n",
        encoding="utf-8",
    )
    result = _run(wrapper, tmp_path, TEST_UV=uv, TEST_DIRECTORY=directory, TEST_PRINTED=printed)
    assert result.returncode == 0, result.stdout + result.stderr
    session, saved = printed.read_text(encoding="utf-8").splitlines()
    session_prefix = "To use siteops in this terminal, run: "
    saved_prefix = "For new terminals, add the directory to your user PATH, for example with: "
    assert session.startswith(session_prefix) and saved.startswith(saved_prefix)
    check = tmp_path / "check.ps1"
    check.write_text(_STEPS_CHECK, encoding="utf-8")
    applied = _run(
        check, tmp_path, TEST_SESSION=session.removeprefix(session_prefix),
        TEST_SAVED=saved.removeprefix(saved_prefix), TEST_UV=uv, TEST_DIRECTORY=directory,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "STEPS_RUN" in applied.stdout

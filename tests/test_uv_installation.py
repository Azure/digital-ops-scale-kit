"""Native uv consumption retains independent bundle and installed payload checks."""

import builtins
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import native_bundle
from tests.installed_runtime import isolated_environment
from tests.native_bundle import DEPENDENCY_NAME, publish_assets
from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_uv_consumers import native_uv
from tests.shell_helpers import bash_path, run_script

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import siteops_distribution as distribution  # noqa: E402


def test_fixture_lock_generation_does_not_require_the_build_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_bundle, "sys", SimpleNamespace(version_info=(3, 10), modules=sys.modules),
    )
    original_import = builtins.__import__

    def import_without_toml(name, *args, **kwargs):
        if name in {"tomllib", "tomli"}:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_toml)
    root, manifest = native_bundle.bundle_factory.__wrapped__(tmp_path, monkeypatch)(1)
    assert (root / "pylock.toml").is_file()
    assert any(target.python == "3.10" for target in manifest.targets)


def test_every_supported_target_selects_the_same_locked_wheels(bundle_factory):
    root, manifest = bundle_factory(1)
    lock = (root / "pylock.toml").read_text(encoding="utf-8")
    for target in manifest.targets:
        assert f"python_version == '{target.python}'" in lock
    assert lock.count("[[packages]]") == 2
    assert f'name = "{DEPENDENCY_NAME}"' in lock
    assert os.name != "nt" or "\\" not in lock


def _platform_helpers(shell):
    suffix = ".sh" if shell == "bash" else ".ps1"
    script = (SCRIPTS / "bootstrap" / ("siteops-bootstrap" + suffix)).read_text(encoding="utf-8")
    if shell == "bash":
        return "extract_installer_helper() {" + script.split(
            "extract_installer_helper() {", 1,
        )[1].split('release=""', 1)[0]
    return "\n".join(
        re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", script).group(0)
        for name in ("Get-InstallerHelper", "Check-Payload")
    )


def test_platform_helper_extraction_follows_provenance_admission():
    bash = (SCRIPTS / "bootstrap" / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    windows = (SCRIPTS / "bootstrap" / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert bash.index('[[ "$verified" == true ]]') < bash.index('installer_helper="$(extract_installer_helper)"')
    assert windows.index("$value -isnot [string] -or $value -cne $expected[$key]") < windows.index(
        "$installerHelper = Get-InstallerHelper $archive $download",
    )
    for script in (bash, windows):
        assert "BEGIN GENERATED PAYLOAD VALIDATOR" not in script


@pytest.fixture
def bundle(bundle_factory, tmp_path):
    root, manifest = bundle_factory()
    archive, _ = publish_assets(root, manifest, tmp_path / "assets")
    return root, manifest, archive


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("altered", ["none", "wheel", "helper"])
def test_packaged_payload_helper_executes_the_shared_admission(bundle, tmp_path, shell, altered):
    root, manifest, archive = bundle
    retained = tmp_path / "retained"
    shutil.copytree(root, retained)
    if altered == "wheel":
        (retained / manifest.application_wheel).write_bytes(b"changed wheel")
    elif altered == "helper":
        (retained / "siteops-install.py").write_text("raise SystemExit('UNVERIFIED_HELPER_RAN')\n")
    body = _platform_helpers(shell)
    helper_dir = tmp_path / "fresh-helper"
    helper_dir.mkdir()
    arguments = ["admit", str(archive), str(retained),
                 manifest.repository, manifest.source_sha, manifest.source_ref]
    if shell == "bash":
        import shlex

        result = run_script(
            f'python={shlex.quote(bash_path(Path(sys.executable)))}\n'
            + f"archive={shlex.quote(str(archive))}\nstaging={shlex.quote(str(helper_dir))}\n"
            + body + '\ninstaller_helper="$(extract_installer_helper)"\ncheck_payload '
            + shlex.join(arguments),
            tmp_path, {},
        )
    else:
        powershell = shutil.which("powershell")
        if powershell is None:
            pytest.skip("Native Windows PowerShell is unavailable.")
        wrapper = tmp_path / "payload.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "function Fail([string]$message) { throw $message }\n"
            "$python = $env:TEST_PYTHON\n" + body
            + "\n$installerHelper = Get-InstallerHelper $env:TEST_ARCHIVE $env:TEST_HELPER_DIR\n"
            + "\nCheck-Payload @('admit', $env:TEST_ARCHIVE, $env:TEST_RETAINED, "
            + "'example/publisher', '" + manifest.source_sha + "', 'refs/heads/main')\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            env={**os.environ, "TEST_PYTHON": sys.executable,
                 "TEST_ARCHIVE": str(archive), "TEST_RETAINED": str(retained),
                 "TEST_HELPER_DIR": str(helper_dir)},
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
    assert (result.returncode == 0) is (altered == "none"), result.stdout + result.stderr
    assert "UNVERIFIED_HELPER_RAN" not in result.stdout + result.stderr
    if altered == "none":
        assert manifest.version in result.stdout
    else:
        assert "payload failed validation" in result.stderr


def test_authenticated_archive_admission_reuses_payload_validation(bundle, tmp_path):
    root, manifest, archive = bundle
    destination = tmp_path / "retained"
    admit = getattr(distribution, "admit_bundle", None)
    assert callable(admit), "The shared authenticated archive admission boundary is missing."
    selected = admit(
        archive, destination, repository=manifest.repository,
        source_sha=manifest.source_sha, source_ref=manifest.source_ref,
    )
    assert selected == manifest
    assert (destination / manifest.application_wheel).read_bytes() == (
        root / manifest.application_wheel
    ).read_bytes()
    admit(archive, destination, repository=manifest.repository,
          source_sha=manifest.source_sha, source_ref=manifest.source_ref)
    payload = destination / manifest.application_wheel
    payload.write_bytes(b"altered retained wheel")
    with pytest.raises(distribution.DistributionError):
        admit(archive, destination, repository=manifest.repository,
              source_sha=manifest.source_sha, source_ref=manifest.source_ref)
    assert payload.read_bytes() == b"altered retained wheel"


@pytest.mark.parametrize("fault", ["source", "traversal", "case-collision", "manifest", "payload"])
def test_archive_admission_refuses_before_publishing_content(bundle, tmp_path, fault):
    _, manifest, archive = bundle
    selected = archive
    if fault != "source":
        selected = tmp_path / "changed.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(selected, "w") as changed:
            for member in original.infolist():
                body = original.read(member)
                if fault == "manifest" and member.filename == "bundle.json":
                    body = body.replace(b'"kind":', b'"kind": "other", "kind":', 1)
                if fault == "payload" and member.filename == manifest.application_wheel:
                    body += b"altered"
                changed.writestr(member, body)
            if fault == "traversal":
                changed.writestr("../outside", b"invalid")
            if fault == "case-collision":
                changed.writestr("BUNDLE.json", b"invalid")
    admit = getattr(distribution, "admit_bundle", None)
    assert callable(admit), "The shared archive admission boundary is missing."
    destination = tmp_path / "selected"
    with pytest.raises(distribution.DistributionError):
        admit(selected, destination, repository=manifest.repository,
              source_sha="b" * 40 if fault == "source" else manifest.source_sha,
              source_ref=manifest.source_ref)
    assert not (tmp_path / "outside").exists()


@pytest.fixture
def native_state(tmp_path):
    uv = native_uv()
    environment = isolated_environment(tmp_path / "state")
    home = tmp_path / "native"
    environment.update({
        "UV_TOOL_DIR": str(home / "tools"), "UV_TOOL_BIN_DIR": str(home / "bin"),
        "UV_CACHE_DIR": str(home / "cache"), "UV_PYTHON_INSTALL_DIR": str(home / "python"),
        "UV_NO_PROGRESS": "1", "UV_PYTHON_DOWNLOADS": "never",
    })
    version = subprocess.run(
        [str(uv), "--version"], env=environment, capture_output=True, text=True, timeout=20,
    )
    assert version.returncode == 0
    assert version.stdout.startswith("uv 0.12.20 "), version.stdout
    for name in ("tools", "bin"):
        (home / name).mkdir(parents=True)
    return Path(uv), home, environment


@pytest.mark.parametrize("fixture", ["missing", "wrong-version"])
def test_required_native_uv_fixture_fails_before_execution(tmp_path, monkeypatch, fixture):
    selected = tmp_path / ("uv.cmd" if fixture == "wrong-version" else "uv.exe")
    if fixture == "wrong-version":
        selected.write_text("@echo off\r\necho uv 0.12.19 (test fixture)\r\n", encoding="ascii")
    monkeypatch.setenv("SITEOPS_TEST_UV", str(selected))
    monkeypatch.setenv(
        "SITEOPS_REQUIRE_WINDOWS_UV" if os.name == "nt" else "SITEOPS_REQUIRE_LINUX_UV",
        "1",
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("Unadmitted uv was executed."),
    )
    with pytest.raises(pytest.fail.Exception, match="qualified native uv 0.12.20"):
        native_state.__wrapped__(tmp_path)


@pytest.fixture
def native_installation(bundle, tmp_path, native_state):
    root, manifest, _ = bundle
    uv, home, environment = native_state
    arguments = [
        uv, "tool", "install", str(root / manifest.application_wheel),
        "--with-requirements", str(root / "pylock.toml"),
        "--find-links", str(root / "wheels"), "--python", sys._base_executable,
        "--no-config", "--offline", "--no-index", "--no-build", "--no-cache",
        "--no-python-downloads", "--link-mode", "copy",
    ]
    distribution.verify_payload(root, manifest)
    installed = subprocess.run(
        arguments, env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert installed.returncode == 0, installed.stderr
    return root, manifest, home, arguments, environment


@pytest.mark.parametrize("fault", ["none", "application", "dependency", "metadata", "pth", "runtime"])
def test_installed_bytes_are_checked_before_running_the_command(native_installation, fault):
    root, manifest, home, _, environment = native_installation
    tool = home / "tools" / "siteops"
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    windows = os.name == "nt"
    packages = tool / ("Lib/site-packages" if windows else f"lib/python{python}/site-packages")
    if fault in {"application", "dependency"}:
        module = "siteops" if fault == "application" else "siteops_fixture_dependency"
        (packages / module / "cli.py").write_text("raise RuntimeError('unverified')\n")
    elif fault == "metadata":
        next(packages.glob("siteops-*.dist-info/METADATA")).write_text("Name: other\n")
    elif fault == "pth":
        (packages / "unexpected.pth").write_text("import unverified\n")
    elif fault == "runtime":
        config = tool / "pyvenv.cfg"
        config.write_text(config.read_text().replace(
            "include-system-site-packages = false", "include-system-site-packages = true",
        ))
    verify = getattr(distribution, "verify_installed_payload", None)
    assert callable(verify), "Installed bytes need an independent check before application execution."
    if fault != "none":
        with pytest.raises(distribution.DistributionError):
            verify(
                root,
                manifest,
                tool,
                python=python,
                platform="windows-x86_64" if windows else "linux-x86_64",
            )

        return
    verify(
        root,
        manifest,
        tool,
        python=python,
        platform="windows-x86_64" if windows else "linux-x86_64",
    )
    command = home / "bin" / ("siteops.exe" if windows else "siteops")
    result = subprocess.run(
        [str(command), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"siteops {manifest.version}"
    verify(
        root,
        manifest,
        tool,
        python=python,
        platform="windows-x86_64" if windows else "linux-x86_64",
    )


def _install(bundle, state, *, replace=False):
    root, manifest, archive = bundle
    uv, home, environment = state
    install = getattr(distribution, "install_verified_bundle", None)
    assert callable(install), "The verified uv lifecycle entry point is missing."
    return install(
        archive,
        root,
        uv=uv,
        tool_directory=home / "tools",
        command_directory=home / "bin",
        repository=manifest.repository,
        source_sha=manifest.source_sha,
        source_ref=manifest.source_ref,
        replace=replace,
        environment=environment,
    )


def test_verified_native_lifecycle_preserves_other_tools_and_requires_replacement(
    bundle,
    native_state,
    bundle_factory,
    tmp_path,
    monkeypatch,
):
    _, manifest, _ = bundle
    uv, home, environment = native_state
    other = home / "tools" / "other-tool"
    other.mkdir()
    (other / "operator-file").write_bytes(b"preserve")
    command = home / "bin" / ("siteops.exe" if os.name == "nt" else "siteops")
    assert _install(bundle, native_state).version == manifest.version
    marker = command.stat().st_mtime_ns
    assert _install(bundle, native_state).version == manifest.version
    assert command.stat().st_mtime_ns == marker
    upgraded, update = bundle_factory(2)
    archive, _ = publish_assets(upgraded, update, tmp_path / "upgrade-assets")
    with pytest.raises(distribution.DistributionError, match="replace"):
        _install((upgraded, update, archive), native_state)
    assert (
        _install((upgraded, update, archive), native_state, replace=True).version == update.version
    )
    assert _install(bundle, native_state, replace=True).version == manifest.version
    observed = subprocess.run(
        [str(command), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert observed.returncode == 0 and observed.stdout.strip() == f"siteops {manifest.version}"
    removed = subprocess.run(
        [str(uv), "tool", "uninstall", "siteops", "--no-config"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert removed.returncode == 0
    assert not command.exists()
    assert (other / "operator-file").read_bytes() == b"preserve"


@pytest.mark.skipif(os.name != "nt", reason="uv copies Windows launcher bytes.")
def test_changed_copied_launcher_bytes_refuse_reuse_before_execution(bundle, native_state):
    _install(bundle, native_state)
    _, home, _ = native_state
    command = home / "bin" / ("siteops.exe" if os.name == "nt" else "siteops")
    command.write_bytes(b"changed copied command; not an admitted executable")
    with pytest.raises(distribution.DistributionError, match="another installation"):
        _install(bundle, native_state)
    assert command.read_bytes() == b"changed copied command; not an admitted executable"


def test_native_reinstall_replaces_verified_requirements_and_index_policy(
    bundle, native_state, bundle_factory, tmp_path,
):
    _install(bundle, native_state)
    uv, home, environment = native_state
    replacement, manifest = bundle_factory(2)
    receipt = home / "tools" / "siteops" / "uv-receipt.toml"
    assert "no-index = true" in receipt.read_text(encoding="utf-8")
    result = subprocess.run(
        [
            str(uv), "tool", "install", str(replacement / manifest.application_wheel),
            "--python", sys._base_executable, "--no-build", "--reinstall",
            "--offline", "--find-links", str(replacement / "wheels"), "--no-cache", "--no-config",
        ],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = receipt.read_text(encoding="utf-8")
    assert "no-index = true" not in recorded
    assert 'name = "siteops-fixture-dependency"' not in recorded
    command = home / "bin" / ("siteops.exe" if os.name == "nt" else "siteops")
    observed = subprocess.run(
        [str(command), "--version"], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert observed.returncode == 0 and observed.stdout.strip() == f"siteops {manifest.version}"
    assert _install(bundle, native_state, replace=True).version == bundle[1].version


@pytest.mark.parametrize("replace", [False, True])
def test_verified_install_never_overwrites_an_unrelated_exposed_command(
    bundle,
    native_state,
    replace,
):
    _, home, _ = native_state
    command = home / "bin" / ("siteops.exe" if os.name == "nt" else "siteops")
    command.write_bytes(b"an unrelated application")
    with pytest.raises(distribution.DistributionError, match="another installation"):
        _install(bundle, native_state, replace=replace)
    assert command.read_bytes() == b"an unrelated application"
    assert not (home / "tools" / "siteops").exists()


def test_invalid_payload_never_reaches_uv_and_preserves_the_installed_tool(
    bundle,
    native_state,
    monkeypatch,
):
    root, manifest, _ = bundle
    _install(bundle, native_state)
    _, home, _ = native_state
    installed = (
        home
        / "tools"
        / "siteops"
        / (
            "Lib/site-packages/siteops/cli.py"
            if os.name == "nt"
            else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/siteops/cli.py"
        )
    )
    expected = installed.read_bytes()
    wheel = root / manifest.application_wheel
    with zipfile.ZipFile(wheel) as archive:
        members = [(member, archive.read(member)) for member in archive.infolist()]
    with zipfile.ZipFile(wheel, "w") as archive:
        for member, content in members:
            if member.filename == "siteops/cli.py":
                content += b"\n# Changed without updating the authenticated payload hash.\n"
            archive.writestr(member, content)
    monkeypatch.setattr(
        distribution.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("uv must not consume an unadmitted payload."),
    )
    with pytest.raises(distribution.DistributionError):
        _install(bundle, native_state, replace=True)
    assert installed.read_bytes() == expected


def test_changed_installed_payload_requires_explicit_native_repair(bundle, native_state):
    _install(bundle, native_state)
    _, home, _ = native_state
    packages = (
        home
        / "tools"
        / "siteops"
        / (
            "Lib/site-packages"
            if os.name == "nt"
            else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
    )
    installed = packages / "siteops" / "cli.py"
    installed.write_bytes(b"changed installed application")
    with pytest.raises(distribution.DistributionError):
        _install(bundle, native_state)
    _install(bundle, native_state, replace=True)
    assert b"changed installed application" != installed.read_bytes()


def test_repeated_install_requires_the_selected_runtime_binding(bundle, native_state, monkeypatch):
    _install(bundle, native_state)
    uv, home, environment = native_state
    configuration = home / "tools" / "siteops" / "pyvenv.cfg"
    lines = configuration.read_text().splitlines()
    configuration.write_text("\n".join(
        f"home = {home / 'different-runtime'}" if line.startswith("home =") else line
        for line in lines
    ) + "\n")
    with monkeypatch.context() as patch:
        patch.setattr(
            distribution.subprocess, "run",
            lambda *args, **kwargs: pytest.fail("An inconsistent runtime reached native replacement."),
        )
        messages = []
        for replace in (False, True):
            with pytest.raises(distribution.DistributionError, match="runtime") as refused:
                _install(bundle, native_state, replace=replace)
            messages.append(str(refused.value))
        assert all("uv tool uninstall siteops" in message for message in messages)
    assert "different-runtime" in configuration.read_text()
    removed = subprocess.run(
        [str(uv), "tool", "uninstall", "siteops", "--no-config", "--offline"],
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    # Native removal can remove empty storage. The caller prepares it again.
    for name in ("tools", "bin"):
        (home / name).mkdir(mode=0o700, exist_ok=True)
    _install(bundle, native_state)
    assert "different-runtime" not in configuration.read_text()


def test_native_repair_refuses_unknown_startup_files_before_uv(bundle, native_state, monkeypatch):
    _install(bundle, native_state)
    _, home, _ = native_state
    packages = home / "tools" / "siteops" / (
        "Lib/site-packages" if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    marker = home / "unverified-hook-ran"
    (packages / "extra.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('not admitted')\n",
    )
    monkeypatch.setattr(
        distribution.subprocess, "run",
        lambda *a, **kw: pytest.fail("Unrecognized startup files must stop native installation."),
    )
    with pytest.raises(distribution.InstallationError, match="uv tool uninstall siteops"):
        _install(bundle, native_state, replace=True)
    assert not marker.exists()
    assert (packages / "extra.pth").exists()


def test_verified_install_ignores_package_policy_overrides(bundle, native_state):
    _, _, environment = native_state
    environment.update({
        "UV_FIND_LINKS": "https://unapproved.example.invalid/wheels",
        "UV_INDEX": "https://unapproved.example.invalid/simple",
        "UV_PYTHON": "not-the-selected-runtime",
        "UV_NO_BINARY": "1",
        "UV_LINK_MODE": "symlink",
        "PYTHONPATH": "unapproved-modules",
    })
    assert _install(bundle, native_state).version == bundle[1].version


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_packaged_native_install_and_replacement_use_the_verified_path(
    bundle, native_state, bundle_factory, tmp_path, shell,
):
    import shlex

    uv, home, environment = native_state
    body = _platform_helpers(shell)

    def invoke(selected, replace=False):
        import tempfile

        root, manifest, archive = selected
        helper_dir = Path(tempfile.mkdtemp(prefix="helper-", dir=tmp_path))
        arguments = [
            "replace" if replace else "install", str(archive), str(root),
            manifest.repository, manifest.source_sha, manifest.source_ref,
            str(uv), str(home / "tools"), str(home / "bin"),
        ]
        if shell == "bash":
            return run_script(
                f"python={shlex.quote(bash_path(Path(sys.executable)))}\n"
                + f"archive={shlex.quote(str(archive))}\nstaging={shlex.quote(str(helper_dir))}\n"
                + body + '\ninstaller_helper="$(extract_installer_helper)"\ncheck_payload '
                + shlex.join(arguments),
                tmp_path, environment,
            )
        powershell = shutil.which("powershell")
        if powershell is None:
            pytest.skip("Native Windows PowerShell is unavailable.")
        wrapper = tmp_path / "install.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "function Fail([string]$message) { throw $message }\n"
            "$python = $env:TEST_PYTHON\n" + body
            + "\n$installerHelper = Get-InstallerHelper $env:TEST_ARCHIVE $env:TEST_HELPER_DIR\n"
            + "\nCheck-Payload @(" + ",".join(
                "'" + value.replace("'", "''") + "'" for value in arguments
            ) + ")\n", encoding="utf-8",
        )
        return subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            env={**environment, "TEST_PYTHON": sys.executable, "TEST_ARCHIVE": str(archive),
                 "TEST_HELPER_DIR": str(helper_dir)}, cwd=tmp_path,
            capture_output=True, text=True, timeout=60,
        )

    first = invoke(bundle)
    assert first.returncode == 0, first.stdout + first.stderr
    newer, manifest = bundle_factory(2)
    archive, _ = publish_assets(newer, manifest, tmp_path / "next-assets")
    refused = invoke((newer, manifest, archive))
    assert refused.returncode != 0
    assert "replace" in refused.stderr.casefold()
    replaced = invoke((newer, manifest, archive), replace=True)
    assert replaced.returncode == 0, replaced.stdout + replaced.stderr
    assert manifest.version in replaced.stdout

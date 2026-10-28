"""Build and consume the real engine wheel with isolated state and explicit tool doubles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tests.native_bundle import write_wheel

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_WHEELHOUSE = "SITEOPS_TEST_RUNTIME_WHEELHOUSE"
# The engine source and runtime lock are fixed for a test session, so each is built or
# downloaded once and every caller receives its own copy of those bytes.
_SESSION_FILES: dict[str, dict[str, bytes]] = {}
# A parallel run builds them once in the controller, and each worker reads them from here.
_PREBUILT: Path | None = None


def _session_copy(kind: str, destination: Path) -> list[Path] | None:
    files = _SESSION_FILES.get(kind)
    if files is None and _PREBUILT is not None and (_PREBUILT / kind).is_dir():
        files = _SESSION_FILES[kind] = {path.name: path.read_bytes() for path in (_PREBUILT / kind).iterdir()}
    if files is None:
        return None
    destination.mkdir()
    for name, content in files.items():
        (destination / name).write_bytes(content)
    return sorted(destination / name for name in files)


def build_engine_wheel(root: Path) -> Path:
    """Build the actual engine with the declared local backend and no package-index access."""
    copied = _session_copy("engine", root / "wheels")
    if copied is not None:
        return copied[0]
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "ThirdPartyNotices.txt"):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(ROOT / "siteops", source / "siteops", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = {
        **isolated_environment(root / "build-state"),
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
         str(source), "--wheel-dir", str(root / "wheels")],
        cwd=root, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((root / "wheels").glob("*.whl"))
    assert len(wheels) == 1
    _SESSION_FILES["engine"] = {wheels[0].name: wheels[0].read_bytes()}
    return wheels[0]


def isolated_environment(root: Path) -> dict[str, str]:
    """Keep application state and tool lookup inside the test's owned directories."""
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {
            "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATHEXT",
            "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
        }
    }
    for name in ("home", "appdata", "localappdata", "temp", "tools", "azure", "github"):
        (root / name).mkdir(parents=True, exist_ok=True)
    environment.update({
        "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
        "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
        "TEMP": str(root / "temp"), "TMP": str(root / "temp"), "TMPDIR": str(root / "temp"),
        "AZURE_CONFIG_DIR": str(root / "azure"), "GH_CONFIG_DIR": str(root / "github"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1",
        "PIP_KEYRING_PROVIDER": "disabled", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "SITEOPS_CACHE_DIR": str(root / "cache"), "SITEOPS_REDACT_OUTPUT": "0",
        "SITEOPS_TEST_TOOL_CONTEXT": str(root / "tool-context.json"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "all_proxy": "http://127.0.0.1:9", "no_proxy": "",
    })
    return environment


def runtime_wheels(root: Path) -> Path:
    supplied = os.environ.get(RUNTIME_WHEELHOUSE)
    if supplied:
        path = Path(supplied)
        assert path.is_absolute() and path.is_dir(), f"{RUNTIME_WHEELHOUSE} must name an existing absolute directory."
        return path
    destination = root / "runtime-wheels"
    if _session_copy("runtime", destination) is not None:
        return destination
    destination.mkdir()
    allowed = {
        "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "PROGRAMDATA",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in allowed or key.upper().startswith("PIP_")
    }
    environment["PIP_KEYRING_PROVIDER"] = "disabled"
    # Dependency provisioning uses the configured approved feed, before isolated application execution.
    result = subprocess.run([
        sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
        "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--requirement", str(ROOT / "scripts" / "siteops-runtime-requirements.txt"),
        "--dest", str(destination),
    ], cwd=root, env=environment, capture_output=True, timeout=300)
    assert result.returncode == 0, (
        "Locked runtime wheels are unavailable. Supply SITEOPS_TEST_RUNTIME_WHEELHOUSE for offline execution."
    )
    _SESSION_FILES["runtime"] = {path.name: path.read_bytes() for path in destination.iterdir()}
    return destination


def prebuild(root: Path) -> None:
    """Build the engine wheel and fetch the runtime wheels into `root` for parallel workers.

    A file set that cannot be provisioned here is left out. Each worker then repeats the
    attempt and reports the failure in the test that needs it, as a serial run does.
    """
    root.mkdir()
    for kind, provision in (("engine", build_engine_wheel), ("runtime", runtime_wheels)):
        state = root / f"{kind}-state"
        state.mkdir()
        try:
            provision(state)
        except Exception:
            continue
        if kind in _SESSION_FILES:
            (root / kind).mkdir()
            for name, content in _SESSION_FILES[kind].items():
                (root / kind / name).write_bytes(content)


def use_prebuilt(root: Path) -> None:
    """Take the controller's files from `root` on first use instead of building them again."""
    global _PREBUILT
    _PREBUILT = root


@dataclass(frozen=True)
class InstalledEngine:
    """An engine installed for one caller.

    `root`, `environment` and the tool copies belong to the caller. `application` is
    shared by every caller installed from the same wheel in this session, so treat it as
    read-only. The next install of that wheel fails when its files changed.
    """

    root: Path
    python: Path
    command: Path
    environment: dict[str, str]
    runtime_wheelhouse: Path
    application: Path

    def run(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.command), *map(str, arguments)], cwd=self.root / "unrelated",
            env=self.environment, capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result


# The installed application depends only on the engine wheel and the runtime lock, so each
# wheel is installed once per session. Every caller still gets its own state, tools and checks.
_SESSION_APPLICATIONS: dict[str, tuple[Path, tuple[tuple[str, int, str], ...]]] = {}


def _interpreter(application: Path) -> Path:
    return application / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _application_files(application: Path) -> tuple[tuple[str, int, str], ...]:
    """Name, size and SHA-256 of each installed file. Running the engine may add bytecode caches."""
    return tuple(sorted(
        (path.relative_to(application).as_posix(), path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in application.rglob("*") if path.is_file() and "__pycache__" not in path.parts
    ))


def _shared_application(key: str, install: Callable[[], Path]) -> Path:
    """Reuse an unchanged application, and reinstall one that is no longer complete."""
    shared = _SESSION_APPLICATIONS.get(key)
    if shared is not None and _interpreter(shared[0]).is_file():
        application, files = shared
        assert _application_files(application) == files, (
            "A test changed the shared installed application. Treat InstalledEngine.application as read-only."
        )
        return application
    application = install()
    _SESSION_APPLICATIONS[key] = (application, _application_files(application))
    return application


def _install_application(application: Path, wheel: Path, wheels: Path, environment: dict[str, str],
                         unrelated: Path) -> Path:
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(application)],
        cwd=unrelated, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    binary = application / ("Scripts" if os.name == "nt" else "bin")
    python = binary / ("python.exe" if os.name == "nt" else "python")
    installed = subprocess.run([
        sys.executable, "-m", "pip", "--python", str(python), "install",
        "--quiet", "--no-index", "--only-binary=:all:", "--require-hashes",
        "--find-links", str(wheels), "--requirement", str(ROOT / "scripts" / "siteops-runtime-requirements.txt"),
    ], cwd=unrelated, env=environment, capture_output=True, text=True, timeout=120)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    tools = application.parent / "siteops_installed_tools-1.0-py3-none-any.whl"
    write_wheel(
        tools, name="siteops-installed-tools", version="1.0", module="siteops_installed_tools",
        entry_point="siteops-test-tool",
        cli_source=(ROOT / "tests" / "fixtures" / "installed_tool.py").read_text(encoding="utf-8"),
    )
    installed = subprocess.run([
        sys.executable, "-m", "pip", "--python", str(python), "install",
        "--quiet", "--no-index", "--no-deps", str(wheel), str(tools),
    ], cwd=unrelated, env=environment, capture_output=True, text=True, timeout=120)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return application


def install_engine(root: Path, wheel: Path) -> InstalledEngine:
    environment = isolated_environment(root)
    unrelated = root / "unrelated"
    unrelated.mkdir()
    wheels = runtime_wheels(root)
    application = _shared_application(
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
        lambda: _install_application(root / "application", wheel, wheels, environment, unrelated),
    )
    binary = application / ("Scripts" if os.name == "nt" else "bin")
    python = _interpreter(application)
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("gh", "az"):
        shutil.copy2(binary / f"siteops-test-tool{suffix}", root / "tools" / f"{name}{suffix}")
    environment["PATH"] = os.pathsep.join((str(root / "tools"), str(binary)))
    imported = subprocess.run(
        [str(python), "-I", "-c",
         "import json,sys,siteops; print(json.dumps({'module':siteops.__file__,'path':sys.path}))"],
        cwd=unrelated, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
    identity = json.loads(imported.stdout)
    assert Path(identity["module"]).is_relative_to(application)
    assert str(ROOT) not in identity["path"]
    return InstalledEngine(root, python, binary / f"siteops{suffix}", environment, wheels, application)

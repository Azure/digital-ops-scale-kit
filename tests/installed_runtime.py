"""Build and consume the real engine wheel with isolated state and explicit tool doubles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tests.native_bundle import write_wheel

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_WHEELHOUSE = "SITEOPS_TEST_RUNTIME_WHEELHOUSE"


def isolated_environment(root: Path) -> dict[str, str]:
    """Keep application state and tool lookup inside the test's owned directories."""
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATHEXT"}
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
    return destination


@dataclass(frozen=True)
class InstalledEngine:
    root: Path
    python: Path
    command: Path
    environment: dict[str, str]

    def run(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.command), *map(str, arguments)], cwd=self.root / "unrelated",
            env=self.environment, capture_output=True, text=True, timeout=90,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return result


def install_engine(root: Path, wheel: Path) -> InstalledEngine:
    environment = isolated_environment(root)
    unrelated = root / "unrelated"
    unrelated.mkdir()
    wheels = runtime_wheels(root)
    application = root / "application"
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
    tools = root / "siteops_installed_tools-1.0-py3-none-any.whl"
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
    return InstalledEngine(root, python, binary / f"siteops{suffix}", environment)

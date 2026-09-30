"""Check a real pipx-created Windows launcher against the bootstrap path guard."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "bootstrap" / "siteops-bootstrap.ps1"
INDEX = "https://packagefeedproxy.microsoft.io/pypi/simple/"


def run(command, *, cwd, env, log, timeout=180):
    with log.open("wb") as output:
        try:
            result = subprocess.run(
                command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{log.stem} exceeded its time limit.") from None
    if result.returncode:
        messages = {
            "check-private-data-root.log": (
                r"Configure a private Site Ops data root\. "
                r"(ROOT_(?:PATH|ANCESTOR_(?:TYPE|OWNER|ACL)|"
                r"DATA_(?:CREATE|TYPE|OWNER|ACL)))\b"
            ),
            "check-bootstrap-guard.log": (
                r"Choose a private Windows tool location\. "
                r"(TOOL_(?:PATH|TYPE|OWNER|ACL))\b"
            ),
        }
        if log.name in messages:
            with log.open("r", encoding="utf-8", errors="replace") as private_log:
                diagnostic = private_log.read(65536)
            category = re.search(messages[log.name], diagnostic)
            if category is not None:
                raise RuntimeError(f"{log.stem} rejected {category.group(1)}.")
        raise RuntimeError(f"{log.stem} failed with exit code {result.returncode}.")


def helper(source, name):
    found = re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source)
    if found is None:
        raise RuntimeError(f"The bootstrap {name} helper is unavailable.")
    return found.group(0)


def main():
    if sys.platform != "win32":
        raise RuntimeError("This launcher test requires a native Windows host.")
    if os.environ.get("PIP_INDEX_URL") != INDEX or os.environ.get("PIP_CONFIG_FILE") != "NUL":
        raise RuntimeError("The launcher test requires the configured approved Python feed.")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        raise RuntimeError("The launcher test needs a bounded hosted run identity.")
    profile = Path(os.environ["USERPROFILE"])
    if not profile.is_absolute():
        raise RuntimeError("The Windows runner has no absolute user profile.")
    root = profile / f"siteops-pipx-probe-{run_id}-{attempt}"
    if root.exists() or root.is_symlink():
        raise RuntimeError("The launcher test requires a new private directory.")
    root.mkdir()
    try:
        env = os.environ.copy()
        for name in (
            "GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
            "AZURE_SUBSCRIPTION_ID", "PYTHONHOME", "PYTHONPATH",
        ):
            env.pop(name, None)
        env.update({
            "PIP_INDEX_URL": INDEX,
            "PIP_CONFIG_FILE": "NUL",
            "PIP_EXTRA_INDEX_URL": "",
            "PIP_FIND_LINKS": "",
            "PIP_TRUSTED_HOST": "",
            "PIP_NO_INPUT": "1",
            "PIP_KEYRING_PROVIDER": "disabled",
            "PIPX_HOME": str(root / "pipx"),
            "PIPX_BIN_DIR": str(root / "bin"),
            "PIPX_MAN_DIR": str(root / "man"),
            "PIPX_COMPLETION_DIR": str(root / "completions"),
            "PIPX_SHARED_LIBS": str(root / "pipx-shared"),
            "PIPX_DEFAULT_PYTHON": sys.executable,
        })
        sid = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if sid.returncode or not re.fullmatch(r"S-[0-9-]+", sid.stdout.strip()):
            raise RuntimeError("The Windows runner user identity is unavailable.")
        logs = root / "logs"
        logs.mkdir()
        run(
            ["icacls.exe", str(root), "/setowner", f"*{sid.stdout.strip()}"],
            cwd=root, env=env, log=logs / "set-owner.log", timeout=20,
        )
        run(
            ["icacls.exe", str(root), "/inheritance:r",
             "/grant:r", f"*{sid.stdout.strip()}:(OI)(CI)F"],
            cwd=root, env=env, log=logs / "protect-root.log", timeout=20,
        )
        data = root / "siteops"
        data.mkdir()
        run(
            ["icacls.exe", str(data), "/setowner", f"*{sid.stdout.strip()}"],
            cwd=root, env=env, log=logs / "set-data-owner.log", timeout=20,
        )
        run(
            ["icacls.exe", str(data), "/inheritance:r",
             "/grant:r", f"*{sid.stdout.strip()}:(OI)(CI)F"],
            cwd=root, env=env, log=logs / "protect-data-root.log", timeout=20,
        )
        tooling = root / "tooling"
        run(
            [sys.executable, "-m", "venv", str(tooling)],
            cwd=root, env=env, log=logs / "create-tooling.log",
        )
        python = tooling / "Scripts" / "python.exe"
        pipx = tooling / "Scripts" / "pipx.exe"
        run(
            [str(python), "-m", "pip", "install", "--no-cache-dir",
             "--only-binary=:all:", "pipx==1.17.2"],
            cwd=root, env=env, log=logs / "install-pipx.log",
        )
        version = subprocess.run(
            [str(pipx), "--version"], cwd=root, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=20, check=False,
        )
        if version.returncode or version.stdout.strip() != "1.17.2":
            raise RuntimeError("The launcher test did not select pipx 1.17.2.")
        for key, selected in (("PIPX_HOME", root / "pipx"), ("PIPX_BIN_DIR", root / "bin")):
            observed = subprocess.run(
                [str(pipx), "environment", "--value", key],
                cwd=root, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=20, check=False,
            )
            if observed.returncode or Path(observed.stdout.strip()) != selected:
                raise RuntimeError(f"pipx did not select the isolated {key} directory.")
        run(
            [str(pipx), "install", "pipx==1.17.2", "--backend", "pip",
             "--fetch-python", "never", "--skip-maintenance", "--app",
             "pipx", "--pip-args", "--only-binary=:all: --no-cache-dir"],
            cwd=root, env=env, log=logs / "install-test-app.log",
        )
        app = root / "bin" / "pipx.exe"
        venv = root / "pipx" / "venvs" / "pipx"
        expected = venv / "Scripts" / "pipx.exe"
        if not app.is_file() or not expected.is_file():
            raise RuntimeError("pipx did not expose the selected installed command.")
        if not app.is_symlink():
            raise RuntimeError("pipx did not create a file symlink on this capable Windows host.")
        if not app.resolve().samefile(expected):
            raise RuntimeError("The pipx file symlink does not target the selected installation.")
        installed = subprocess.run(
            [str(venv / "Scripts" / "python.exe"), "-I", "-c",
             "import importlib.metadata, pipx; "
             "print(importlib.metadata.version('pipx')); print(pipx.__file__)"],
            cwd=root, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30, check=False,
        )
        installed_lines = installed.stdout.splitlines()
        if (
            installed.returncode
            or len(installed.stdout) > 4096
            or len(installed_lines) != 2
            or not Path(installed_lines[1]).resolve().is_relative_to(venv.resolve())
        ):
            raise RuntimeError("The selected package is not installed inside the pipx environment.")
        if installed_lines[0] != "1.17.2":
            raise RuntimeError("The selected application version differs from pipx 1.17.2.")
        command = subprocess.run(
            [str(app), "--version"], cwd=root, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=45, check=False,
        )
        if command.returncode or command.stdout.strip() != "1.17.2":
            raise RuntimeError("The launcher did not run the selected installed pipx application.")
        source = BOOTSTRAP.read_text(encoding="utf-8")
        prefix = (
            'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
            + helper(source, "Require-PrivateDataRoot") + "\n"
            + helper(source, "Require-PrivateExecutablePath") + "\n"
            + "$ErrorActionPreference='Stop'\n"
        )
        root_wrapper = root / "check-private-data-root.ps1"
        root_wrapper.write_text(
            prefix + "Require-PrivateDataRoot $env:SITEOPS_PROBE_DATA\n'ROOT_ADMITTED'\n",
            encoding="utf-8",
        )
        wrapper = root / "check-bootstrap-guard.ps1"
        wrapper.write_text(
            prefix + "$selected=Require-PrivateExecutablePath $env:SITEOPS_PROBE_APP "
            + "$env:PIPX_BIN_DIR $env:SITEOPS_PROBE_EXPECTED_TARGET "
            + "$env:SITEOPS_PROBE_TARGET_ROOT\n"
            + "if ($selected -cne $env:SITEOPS_PROBE_EXPECTED_TARGET) "
            + "{ throw 'The linked launcher did not select the installed application.' }\n"
            + "$observed=& $selected --version\n"
            + "if ($LASTEXITCODE -ne 0 -or $observed -cne '1.17.2') "
            + "{ throw 'The selected installed application did not run.' }\n"
            + "'LAUNCHER_ADMITTED'\n",
            encoding="utf-8",
        )
        env["SITEOPS_PROBE_DATA"] = str(data)
        env["SITEOPS_PROBE_APP"] = str(app)
        env["SITEOPS_PROBE_EXPECTED_TARGET"] = str(expected)
        env["SITEOPS_PROBE_TARGET_ROOT"] = str(venv.parent.parent)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(
                "## Windows pipx launcher check\n\n"
                "The approved Python feed supplied pipx 1.17.2. It installed "
                "its own CLI into isolated state and exposed a real file "
                "symlink to that application. No Site Ops engine wheel, "
                "release proof or Azure deployment was assessed.\n\n"
            )
        run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(root_wrapper)],
            cwd=root, env=env, log=logs / "check-private-data-root.log", timeout=45,
        )
        run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(wrapper)],
            cwd=root, env=env, log=logs / "check-bootstrap-guard.log", timeout=45,
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write("The bootstrap's selected executable guard admitted that launcher.\n")
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()

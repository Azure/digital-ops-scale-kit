"""Portable helpers for isolated workflow shell tests."""

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def required_bash() -> Path:
    """Resolve native Bash without selecting the Windows WSL launcher."""
    if sys.platform == "win32":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        for candidate in (
            program_files / "Git" / "bin" / "bash.exe",
            Path(r"C:\Program Files\Git\bin\bash.exe"),
        ):
            if candidate.is_file():
                return candidate
        raise AssertionError(
            "Git Bash is required to validate workflow shell semantics on Windows."
        )
    resolved = shutil.which("bash")
    assert resolved, "Bash is required to validate workflow shell semantics."
    return Path(resolved)


def bash_path(path: Path) -> str:
    """Return a native or Git Bash path for an absolute local path."""
    resolved = path.resolve()
    if sys.platform != "win32":
        return resolved.as_posix()
    posix = resolved.as_posix()
    return f"/{resolved.drive[0].lower()}{posix[2:]}"


def write_executable(path: Path, content: str) -> None:
    """Write a test-owned executable with shell-compatible line endings."""
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_script(
    script: str, tmp_path: Path, exports: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run one workflow step's shell with exported inputs and no profile state.

    A `bin` directory under `tmp_path` is placed first on `PATH`, so a test can
    hand the step recording or failing stand-ins for the tools it calls. The
    step runs under the same `-e -o pipefail` options the runner applies, from
    `tmp_path`, and its completed process is returned unchecked.
    """
    bin_dir = tmp_path / "bin"
    preamble = []
    if bin_dir.is_dir():
        preamble.append(f'export PATH={shlex.quote(bash_path(bin_dir))}:"$PATH"')
    preamble.extend(f"export {name}={shlex.quote(value)}" for name, value in exports.items())
    script_path = tmp_path / "workflow-step.sh"
    write_executable(script_path, "\n".join((*preamble, script)))
    return subprocess.run(
        [
            str(required_bash()),
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            bash_path(script_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

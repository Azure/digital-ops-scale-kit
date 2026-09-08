"""Portable helpers for isolated workflow shell tests."""

import os
import shutil
import stat
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

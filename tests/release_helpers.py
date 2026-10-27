"""Owned Git repositories and fixed release command paths for release tests."""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "prepare-siteops-release.py"
ZERO_SHA = "0" * 40
REPOSITORY = "example/releases"
SOURCE_REF = "refs/heads/main"
HEADLINE = "Release highlights"


def _git(
    repository: Path, *arguments: str, input_bytes: bytes | None = None, environment: dict[str, str] | None = None,
) -> str:
    input_arguments = (
        {"stdin": subprocess.DEVNULL} if input_bytes is None else {"input": input_bytes}
    )
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True,
        timeout=30, check=False, env=environment, **input_arguments,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return create_repository(tmp_path / "repository")


# Tests can redirect the temporary directory, so keep the location chosen at import.
_TEMPORARY_ROOT = tempfile.gettempdir()
_TEMPLATE: Path | None = None
# The process needs only these to start Git. Every other setting comes from the template.
_GIT_PROCESS = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR"}


def _template() -> Path:
    """Initialize one repository per session with the settings each test copies."""
    global _TEMPLATE
    if _TEMPLATE is None or not (_TEMPLATE / ".git").is_dir():
        parent = Path(tempfile.mkdtemp(prefix="siteops-release-repository-", dir=_TEMPORARY_ROOT))
        atexit.register(shutil.rmtree, parent, True)
        template = parent / "repository"
        template.mkdir()
        # A fixed environment, so the first caller's Git settings never reach later repositories.
        environment = {key: value for key, value in os.environ.items() if key.upper() in _GIT_PROCESS}
        environment.update(
            HOME=str(parent), XDG_CONFIG_HOME=str(parent / "config"), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        )
        _git(template, "init", "--quiet", environment=environment)
        for key, value in (("user.name", "Release Intent Test"), ("user.email", "release-intent@example.invalid"),
                           ("core.autocrlf", "false")):
            _git(template, "config", key, value, environment=environment)
        _TEMPLATE = template
    return _TEMPLATE


def create_repository(root: Path) -> Path:
    """Create a new test-owned repository with deterministic local Git settings."""
    root.mkdir()
    shutil.copytree(_template() / ".git", root / ".git")
    return root


def _write_source_version(repository: Path, source: str) -> None:
    path = repository / "siteops" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="")


def _write_record(
    repository: Path, declaration: dict | bytes, *, name: str = "candidate",
    notes: bytes | None = b"## Changes\n\nReviewed release notes.\n",
) -> tuple[bytes, bytes | None]:
    directory = repository / "releases" / name
    directory.mkdir(parents=True, exist_ok=True)
    raw = (
        declaration if isinstance(declaration, bytes)
        else json.dumps({"headline": HEADLINE, **declaration}, separators=(",", ":")).encode("utf-8")
    )
    (directory / "release.json").write_bytes(raw)
    if notes is not None:
        (directory / "notes.md").write_bytes(notes)
    return raw, notes


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _run_cli(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(CLI), *arguments], cwd=repository,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
    )

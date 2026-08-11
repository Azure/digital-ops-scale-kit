"""Fixtures for workspace content tests.

These tests validate the actual committed workspace content (manifests,
parameters, sites, templates) is internally consistent. They use the real
workspaces/iot-operations/ directory, not synthetic fixtures.
"""

import shutil
from pathlib import Path

import pytest

from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"


def az_path() -> str:
    """Resolve the Azure CLI launcher.

    `shutil.which` is required rather than the bare name, since the launcher is
    `az.cmd` on Windows and `subprocess.run` does not resolve it without a
    shell. Mirrors `get_template_parameters` in `siteops/executor.py`.

    Deliberately fails rather than skipping when the CLI is absent. An
    environment-shaped skip would let this coverage disappear silently, and the
    same CLI is already required by `scripts/validate-bicep.ps1` and by the
    Bicep extraction test in `tests/test_orchestrator_unresolved.py`.
    """
    path = shutil.which("az")
    assert path, (
        "Azure CLI (`az`) not found on PATH. This test compiles Bicep and "
        "cannot run without it."
    )
    return path


@pytest.fixture(scope="module")
def workspace() -> Path:
    """Path to the IoT Operations workspace."""
    assert WORKSPACE_PATH.is_dir(), f"Workspace not found: {WORKSPACE_PATH}"
    return WORKSPACE_PATH


@pytest.fixture(scope="module")
def orchestrator(workspace: Path) -> Orchestrator:
    """Orchestrator configured for the real workspace."""
    return Orchestrator(workspace)

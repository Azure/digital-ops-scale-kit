"""Fixtures for workspace content tests.

These tests validate the actual committed workspace content (manifests,
parameters, sites, templates) is internally consistent. They use the real
workspaces/iot-operations/ directory, not synthetic fixtures.
"""

from pathlib import Path

import pytest

from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"


@pytest.fixture(scope="module")
def workspace() -> Path:
    """Path to the IoT Operations workspace."""
    assert WORKSPACE_PATH.is_dir(), f"Workspace not found: {WORKSPACE_PATH}"
    return WORKSPACE_PATH


@pytest.fixture(scope="module")
def orchestrator(workspace: Path) -> Orchestrator:
    """Orchestrator configured for the real workspace."""
    return Orchestrator(workspace)

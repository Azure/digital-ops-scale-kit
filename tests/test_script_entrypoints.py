"""Workflow helper scripts import their sibling modules under any interpreter path policy."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_FILES = sorted([*(ROOT / ".github").rglob("*.yaml"), *(ROOT / ".github").rglob("*.yml")])
INVOKED = sorted({
    name for path in WORKFLOW_FILES
    for name in re.findall(r"scripts/([A-Za-z0-9_-]+\.py)\b", path.read_text(encoding="utf-8"))
})


def test_workflows_invoke_the_acceptance_helpers():
    assert {"manage-release-fleet.py", "aggregate-release-acceptance.py", "coordinate-release-fleet.py",
            "observe-site-readiness.py"} <= set(INVOKED)


@pytest.mark.parametrize("name", INVOKED)
def test_invoked_script_imports_without_the_implicit_script_directory(tmp_path, name):
    # Candidate Site cases set PYTHONSAFEPATH, so Python never adds the script directory to sys.path.
    # runpy executes the module body without its main guard and adds no path of its own.
    probe = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='workflow_import_check')"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT / "scripts" / name)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr

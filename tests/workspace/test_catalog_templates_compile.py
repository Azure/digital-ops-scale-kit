"""Catalog family entry points compile.

`scripts/validate-bicep.ps1` compiles every template in the workspace, and the
E2E workflow runs it before provisioning. Both are CI-side. This runs in the
fast suite so a family template that stopped compiling is caught while it is
being written rather than on push.

Cost is bounded by design. A Bicep module is compiled as part of its parent, so
building `templates/aio/<family>/main.bicep` also builds every module
the family composes, with a child's diagnostics reported against the child's own
file and line. One `az` invocation therefore covers a whole family no matter how
many resource kinds it grows, and adding a family adds exactly one invocation.

Warnings are failures here, matching `validate-bicep.ps1`. `az bicep build`
exits zero on warnings, and BCP081 (no types available for a resource type and
API version) is warning severity, so warnings are failures here
by.
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.workspace import catalog_harness as harness
from tests.workspace.conftest import az_path

# `<file>(LINE,COL) : Error BCP104: ...` or the same at Warning severity. The
# code is matched loosely because linter rules report a named code such as
# `no-unused-params` rather than a `BCP` number, and those are warnings this
# repository treats as failures.
_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?)\((\d+),\d+\)\s*:\s*(Error|Warning)\s+([\w-]+):\s*(.*)$"
)

# The CLI prints this on stderr when a newer Bicep exists. It carries the word
# "warning" in some locales and never carries a source location, so it is
# matched by name rather than filtered by severity.
_UPGRADE_NOTICE = "A new Bicep release is available"


def _family_entry_points(workspace: Path) -> list[Path]:
    """Every catalog family's composing template.

    Discovered by location rather than listed, so a family added under
    `templates/aio/<family>/` is compiled without editing this file. A family
    is exactly a subdirectory of `templates/aio/` carrying a `main.bicep`.

    Deliberately not read from `catalog_harness.CATALOG_FAMILIES`. A family
    template that compiles is worth checking before anyone registers a spec for
    it, and the registry is held against this same layout in
    `test_catalog_family_contracts.py`.
    """
    return [
        workspace / "templates" / "aio" / family / "main.bicep"
        for family in harness.template_family_dirs(workspace)
    ]


def test_at_least_one_family_entry_point_is_discovered(workspace):
    """Discovery is by glob, so a moved directory would silently cover nothing.

    Without this the parametrized test below would collect zero cases and the
    suite would still report success.
    """
    found = _family_entry_points(workspace)
    assert found, (
        "No catalog family entry point found under templates/aio/*/main.bicep. "
        "Either the families moved, in which case update _family_entry_points, "
        "or the compile coverage below is running against nothing."
    )


@pytest.mark.parametrize(
    "entry_point",
    _family_entry_points(Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"),
    ids=lambda p: p.parent.name,
)
def test_family_entry_point_compiles(entry_point: Path):
    """The family and every per-generation module it routes to compile clean."""
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [
                az_path(),
                "bicep",
                "build",
                "--file",
                str(entry_point),
                "--outfile",
                str(Path(tmp) / "family.json"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    diagnostics = [
        line.strip()
        for line in (result.stderr or "").splitlines()
        if _DIAGNOSTIC.match(line.strip()) and _UPGRADE_NOTICE not in line
    ]

    # A failure that produced no parsed diagnostic is a toolchain failure rather
    # than a clean compile. Without this, a Bicep download failure in CI would
    # report success having compiled nothing.
    assert result.returncode == 0 or diagnostics, (
        f"`az bicep build` failed on {entry_point.name} without emitting a "
        f"parseable diagnostic, so nothing was validated.\n"
        f"exit={result.returncode}\n{(result.stderr or '').strip()[:2000]}"
    )

    assert not diagnostics, (
        f"{entry_point.parent.name}/{entry_point.name} does not compile clean. "
        f"A diagnostic reported against another file is from a template this "
        f"family composes as a module.\n  " + "\n  ".join(diagnostics)
    )

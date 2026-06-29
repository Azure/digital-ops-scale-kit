"""Per-API-version Bicep module parity tests for the AIO upgrade flow.

Per-version modules (e.g. `instance-2026-03-01.bicep`, `instance-2025-10-01.bicep`)
are typically copied from the previous version and minimally diverged when a new
API version ships. Unintentional drift (a fix applied to one version but not
mirrored) is a real risk as the module count grows.

These tests enforce the public-surface contract: param names, output names,
and exported function names must match across versions of the same module
family. Schema-level differences (resource bodies, internal helpers) are
expected and out of scope here.
"""

import re
from pathlib import Path

import pytest

WORKSPACE_PATH = (
    Path(__file__).parent.parent.parent
    / "workspaces"
    / "iot-operations"
)
AIO_MODULES_DIR = WORKSPACE_PATH / "templates" / "aio" / "modules"
DEPS_MODULES_DIR = WORKSPACE_PATH / "templates" / "deps" / "modules"

# Matches a Bicep top-level declaration. Captures the kind and the name.
# Allows optional decorators on the previous line via re.MULTILINE on the
# anchored ^.
_BICEP_PARAM_PATTERN = re.compile(r"^param\s+([A-Za-z_]\w*)\b", re.MULTILINE)
_BICEP_OUTPUT_PATTERN = re.compile(r"^output\s+([A-Za-z_]\w*)\b", re.MULTILINE)
_BICEP_FUNC_PATTERN = re.compile(r"^func\s+([A-Za-z_]\w*)\b", re.MULTILINE)


def _extract_surface(path: Path) -> dict[str, set[str]]:
    """Return the public surface of a Bicep module: param, output, and func
    declarations. Comments and resource bodies are intentionally excluded.
    """
    text = path.read_text(encoding="utf-8")
    return {
        "params": set(_BICEP_PARAM_PATTERN.findall(text)),
        "outputs": set(_BICEP_OUTPUT_PATTERN.findall(text)),
        "funcs": set(_BICEP_FUNC_PATTERN.findall(text)),
    }


def _paired_modules(directory: Path, prefix: str) -> list[Path]:
    """Return all `<prefix>-<api-version>.bicep` modules in directory, sorted."""
    return sorted(directory.glob(f"{prefix}-*.bicep"))


def _assert_surface_parity(modules: list[Path]) -> None:
    """Assert all modules share the same param, output, and func name sets.

    Uses union-vs-symmetric-difference rather than pairwise-against-baseline:
    when 3+ modules are compared and a single one is the outlier, this
    surfaces the outlier directly instead of forcing the reader to infer
    which side of N-1 baseline failures is the actual drift.
    """
    if len(modules) < 2:
        pytest.skip(f"Need 2+ modules to compare, got {len(modules)}")
    surfaces = {m: _extract_surface(m) for m in modules}
    for kind in ("params", "outputs", "funcs"):
        union: set[str] = set().union(*(s[kind] for s in surfaces.values()))
        outliers: dict[str, set[str]] = {}
        for m, surface in surfaces.items():
            missing = union - surface[kind]
            if missing:
                outliers[m.name] = missing
        assert not outliers, (
            f"Module surface drift ({kind}) across {[m.name for m in modules]}.\n"
            f"Union of all names: {sorted(union)}\n"
            f"Missing per module:\n"
            + "\n".join(
                f"  {name}: missing {sorted(missing)}"
                for name, missing in outliers.items()
            )
        )


class TestAioInstanceModuleParity:
    """The `instance-<api-version>.bicep` modules under templates/aio/modules/
    must expose the same parameters, outputs, and exported funcs across
    versions. Resource bodies and per-version schema details are out of scope."""

    def test_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "instance"))

    def test_resolve_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "resolve-instance"))

    def test_update_instance_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(AIO_MODULES_DIR, "update-instance"))


class TestAdrNamespaceModuleParity:
    """The `adr-ns-<api-version>.bicep` modules under templates/deps/modules/
    must expose the same parameters, outputs, and exported funcs across
    versions."""

    def test_adr_ns_modules_share_surface(self):
        _assert_surface_parity(_paired_modules(DEPS_MODULES_DIR, "adr-ns"))

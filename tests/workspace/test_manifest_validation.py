"""Tests that all workspace manifests pass validation."""

from pathlib import Path

import yaml

from siteops.models import Manifest
from siteops.orchestrator import Orchestrator


def _all_manifest_files(workspace: Path) -> list[Path]:
    """Discover every Manifest YAML across `manifests/`, `samples/`, `scenarios/`.

    Centralized so validation and structural tests stay aligned as the layout
    grows. The samples sweep filters to the manifest convention
    (`manifest.yaml` + `_*.yaml` partials) and skips parameter files like
    `inputs.yaml` and `outputs.yaml`.
    """
    found: list[Path] = []
    for sub in ("manifests", "scenarios"):
        d = workspace / sub
        if d.is_dir():
            found.extend(sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")))
    samples = workspace / "samples"
    if samples.is_dir():
        for sample_dir in sorted(samples.iterdir()):
            if not sample_dir.is_dir():
                continue
            for ext in ("yaml", "yml"):
                # Standalone sample manifest.
                manifest = sample_dir / f"manifest.{ext}"
                if manifest.is_file():
                    found.append(manifest)
                # Partials in the sample dir (filename prefixed `_`).
                found.extend(sorted(sample_dir.glob(f"_*.{ext}")))
    return found


class TestManifestValidation:
    """Every manifest in the workspace should validate without errors."""

    def test_all_manifests_discovered(self, workspace):
        """Sanity check: workspace has manifests to validate."""
        manifests = _all_manifest_files(workspace)
        assert len(manifests) >= 1, "No manifests found in workspace"

    def test_aio_fundamentals_validates(self, workspace, orchestrator):
        """_aio-fundamentals.yaml (internal partial) should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "_aio-fundamentals.yaml")
        assert errors == [], f"_aio-fundamentals.yaml validation errors: {errors}"

    def test_aio_install_validates(self, workspace, orchestrator):
        """aio-install.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "aio-install.yaml")
        assert errors == [], f"aio-install.yaml validation errors: {errors}"

    def test_secretsync_validates(self, workspace, orchestrator):
        """secretsync.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "secretsync.yaml")
        assert errors == [], f"secretsync.yaml validation errors: {errors}"

    def test_aio_upgrade_validates(self, workspace, orchestrator):
        """aio-upgrade.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "aio-upgrade.yaml")
        assert errors == [], f"aio-upgrade.yaml validation errors: {errors}"

    def test_opc_ua_solution_validates(self, workspace, orchestrator):
        """samples/opc-ua-solution/manifest.yaml should validate."""
        errors = orchestrator.validate(workspace / "samples" / "opc-ua-solution" / "manifest.yaml")
        assert errors == [], f"opc-ua-solution validation errors: {errors}"

    def test_aio_with_opc_ua_scenario_validates(self, workspace, orchestrator):
        """scenarios/aio-with-opc-ua.yaml should validate (composes via include)."""
        errors = orchestrator.validate(workspace / "scenarios" / "aio-with-opc-ua.yaml")
        assert errors == [], f"aio-with-opc-ua scenario validation errors: {errors}"

    def test_no_duplicate_step_names_in_any_manifest(self, workspace, orchestrator):
        """No manifest (post-include flatten) should have duplicate step names."""
        for manifest_path in _all_manifest_files(workspace):
            manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            step_names = [s.name for s in manifest.steps]
            duplicates = [n for n in step_names if step_names.count(n) > 1]
            assert duplicates == [], (
                f"{manifest_path.relative_to(workspace)} has duplicate step "
                f"names: {set(duplicates)}"
            )

    def test_partial_manifests_use_underscore_prefix(self, workspace):
        """A manifest authored to be `include:`-d (a partial) must use the `_`
        filename prefix.

        Convention: any YAML in `manifests/` that is `include:`-d by another
        manifest must be named `_<topic>.yaml`. Standalone manifests (intended
        for `siteops deploy`) do not start with `_`. The same applies to
        `samples/<name>/_partial.yaml`.

        Detection: walk every manifest under `manifests/`, `scenarios/`, and
        `samples/<name>/`; collect the include targets; assert each target's
        basename starts with `_`.
        """
        offenders: list[str] = []
        for manifest_path in _all_manifest_files(workspace):
            with open(manifest_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if not raw:
                continue
            for step in raw.get("steps", []) or []:
                if not isinstance(step, dict):
                    continue
                target = step.get("include")
                if not target:
                    continue
                # Resolve relative to including manifest's directory.
                resolved = (manifest_path.parent / target).resolve()
                if not resolved.name.startswith("_"):
                    offenders.append(
                        f"{manifest_path.relative_to(workspace)} includes "
                        f"{target!r} (resolved: {resolved.name}); included "
                        f"files must be partials with the `_` prefix"
                    )
        assert offenders == [], "Partial-prefix violations:\n" + "\n".join(offenders)

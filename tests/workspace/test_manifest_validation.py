"""Tests that all workspace manifests pass validation."""

from pathlib import Path

import yaml

from siteops.models import Manifest


def _all_manifest_files(workspace: Path) -> list[Path]:
    """Discover every Manifest YAML across `manifests/` and `samples/`.

    Centralized so validation and structural tests stay aligned as the layout
    grows. The samples sweep filters to the manifest convention
    (`manifest.yaml` + `_*.yaml` partials) and skips parameter files like
    `inputs.yaml` and `outputs.yaml`.
    """
    found: list[Path] = []
    manifests = workspace / "manifests"
    if manifests.is_dir():
        found.extend(
            sorted(manifests.glob("*.yaml")) + sorted(manifests.glob("*.yml"))
        )
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
    # Partials that live beside the implementation they wrap, such as the host
    # lifecycle steps under `templates/<area>/<impl>/`.
    templates = workspace / "templates"
    if templates.is_dir():
        for ext in ("yaml", "yml"):
            found.extend(sorted(templates.rglob(f"_*.{ext}")))
    return found


class TestManifestValidation:
    """Every manifest in the workspace should validate without errors."""

    def test_all_manifests_discovered(self, workspace):
        """Sanity check: workspace has manifests to validate."""
        manifests = _all_manifest_files(workspace)
        assert len(manifests) >= 1, "No manifests found in workspace"

    def test_every_manifest_validates(self, workspace, orchestrator):
        """Every deployable manifest validates.

        Sweeping discovery rather than naming files means a manifest added later is
        covered on arrival instead of when someone remembers to add a test.

        Partials are excluded because they are not independently deployable. One that
        chains off an upstream step cannot resolve that reference until a composing
        manifest supplies the producer, so validating it alone reports a failure that
        is by design. Partials are still covered structurally by the underscore and
        composition tests below, and through the entry points that include them.
        """
        failures: dict[str, list] = {}
        for manifest in _all_manifest_files(workspace):
            if manifest.name.startswith("_"):
                continue
            errors = orchestrator.validate(manifest)
            if errors:
                failures[str(manifest.relative_to(workspace))] = errors

        assert not failures, "manifest validation errors:\n" + "\n".join(
            f"  {name}: {errs}" for name, errs in sorted(failures.items())
        )

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

        Detection: walk every manifest under `manifests/` and
        `samples/<name>/`, collect the include targets, and assert each
        target's basename starts with `_`.
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
                        f"{target!r} (resolved: {resolved.name}). Included "
                        f"files must be partials with the `_` prefix"
                    )
        assert offenders == [], "Partial-prefix violations:\n" + "\n".join(offenders)

    def test_every_partial_is_composed_somewhere(self, workspace):
        """Every leaf partial (`_*.yaml`) must be `include:`-d by at least one
        other manifest. A partial with no consumers is dead code.

        Detection: collect every `_*.yaml` file across `manifests/` and
        `samples/<name>/`, then collect every `include:` target referenced
        anywhere. Assert each partial appears in the referenced set.

        Why: partials are never deployed standalone (their `_` prefix marks
        them as composable fragments only). An orphaned partial silently
        accumulates and tests / refactors keep updating it without anyone
        noticing it's unreferenced.
        """
        all_partials: set[Path] = set()
        all_includes: set[Path] = set()
        for manifest_path in _all_manifest_files(workspace):
            if manifest_path.name.startswith("_"):
                all_partials.add(manifest_path.resolve())
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
                resolved = (manifest_path.parent / target).resolve()
                all_includes.add(resolved)

        orphans = sorted(p.relative_to(workspace) for p in all_partials - all_includes)
        assert not orphans, (
            "Orphaned partials (no manifest composes them):\n"
            + "\n".join(f"  {p}" for p in orphans)
        )

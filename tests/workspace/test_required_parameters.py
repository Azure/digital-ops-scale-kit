"""Every deployment step receives the parameters its template requires.

The engine drops any parameter a template does not declare, and a template
parameter with no value fails at ARM preflight rather than at authoring time.
Between those two, a parameter file that stops being attached, or a manifest
that never attached one, is invisible until a deploy reaches the site.

This resolves the same three tiers `Orchestrator.resolve_parameters` merges,
for every (manifest, site, step) combination the workspace can produce, and
compares the result against the parameters each step's template requires.

Parameter names are read from the Bicep source rather than from a compiled
template, so the whole sweep costs no `az` invocations.
"""

import re
from pathlib import Path

import yaml

from siteops.models import Manifest
from siteops.orchestrator import Orchestrator
from tests.workspace.test_manifest_validation import _all_manifest_files

# `param name type` with an optional `= default`. A parameter with a default,
# or a nullable type, does not have to be supplied.
_PARAM_DECL = re.compile(r"^\s*param\s+(\w+)\s+([^=\n]+?)(=\s*[^\n]+)?$", re.MULTILINE)

# Parameters a site supplies through an overlay rather than through committed
# content. A `sites.local/` overlay is deliberately uncommitted, so the
# committed workspace cannot satisfy these and this sweep would report them on
# every run. Each is validated where it is consumed instead.
_OVERLAY_SUPPLIED = frozenset(
    {
        "machineName",
        "customLocationsOid",
    }
)


def _template_requirements(template: Path) -> set[str]:
    """Parameter names a template requires the caller to supply."""
    required: set[str] = set()
    for match in _PARAM_DECL.finditer(template.read_text(encoding="utf-8")):
        name, type_expr, default = match.group(1), match.group(2), match.group(3)
        if default is not None:
            continue
        if type_expr.strip().endswith("?"):
            # A nullable parameter is satisfied by being absent.
            continue
        required.add(name)
    return required


def _keys_from(path: Path) -> set[str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return set()
    return set(data.keys()) if isinstance(data, dict) else set()


def _supplied_keys(
    workspace: Path,
    orchestrator: Orchestrator,
    manifest: Manifest,
    step,
    site,
) -> set[str]:
    """Parameter names reaching a step, across the three merge tiers."""
    supplied: set[str] = set()

    manifest_parameters, _, _ = orchestrator._resolve_manifest_parameters(
        manifest,
        site,
    )
    supplied |= set(manifest_parameters)

    supplied |= set(site.get_all_parameters().keys())

    for param_path in getattr(step, "parameters", []) or []:
        resolved = manifest.resolve_parameter_path(param_path, site)
        supplied |= _keys_from(workspace / resolved)

    return supplied


class TestRequiredParametersAreSatisfied:
    """Each step's template gets every parameter it requires, for every site."""

    def test_every_step_receives_its_required_parameters(self, workspace):
        orchestrator = Orchestrator(workspace)
        checked = 0
        failures: list[str] = []

        for manifest_path in _all_manifest_files(workspace):
            manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
            try:
                sites = orchestrator.resolve_sites(manifest)
            except Exception:
                # A manifest with no targeting of its own is deployed with a
                # CLI selector, and is covered through the manifests that
                # compose it.
                continue
            if not sites:
                continue

            for step in manifest.steps:
                template_name = getattr(step, "template", None)
                if not template_name:
                    continue
                template = workspace / template_name
                if not template.exists() or template.suffix != ".bicep":
                    continue

                required = _template_requirements(template) - _OVERLAY_SUPPLIED
                if not required:
                    continue

                for site in sites:
                    checked += 1
                    missing = required - _supplied_keys(
                        workspace,
                        orchestrator,
                        manifest,
                        step,
                        site,
                    )
                    if missing:
                        failures.append(
                            f"{manifest_path.relative_to(workspace)} | site "
                            f"'{site.name}' | step '{step.name}' | "
                            f"{template_name} is missing {sorted(missing)}"
                        )

        assert checked > 0, (
            "No (manifest, site, step) combinations were checked, so this test "
            "would pass without examining anything."
        )
        assert not failures, (
            "A step's template requires a parameter nothing supplies for that "
            "site. The deploy reaches ARM preflight and fails there.\n  "
            + "\n  ".join(failures)
        )

    def test_overlay_allowlist_is_still_needed(self, workspace):
        """Each allowlisted name is still a required parameter somewhere.

        The allowlist exists to skip parameters a `sites.local/` overlay
        supplies. An entry that no template requires any more is stale, and
        keeping it would silently excuse a future parameter of the same name.
        """
        required_anywhere: set[str] = set()
        for template in (workspace / "templates").rglob("*.bicep"):
            required_anywhere |= _template_requirements(template)

        stale = sorted(_OVERLAY_SUPPLIED - required_anywhere)
        assert not stale, (
            f"These names are allowlisted as overlay-supplied but no template "
            f"requires them any more: {stale}. Remove them from the allowlist."
        )

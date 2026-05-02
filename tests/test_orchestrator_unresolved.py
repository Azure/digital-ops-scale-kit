"""Tests for the unresolved-template hard-fail safeguard in `resolve_parameters`.

This guard exists so that a malformed `{{ ... }}` reference (e.g. typo'd
step name, missing site path, unreachable output) is reported before the
literal token is sent to ARM. Three behaviors must hold:

1. Unresolved templates in template-accepted params raise `ValueError`
   in non-dry-run mode.
2. Unresolved templates in params the template does NOT accept are
   silently filtered out and do not raise (filter-then-check ordering).
3. Dry-run mode downgrades the failure to a warning so dry-run plans can
   render `{{ steps.X.outputs.Y }}` placeholders without real outputs.
4. When `filter_parameters` itself raises (e.g. Bicep build unavailable),
   the unresolved-check is skipped to avoid masking the real upstream
   failure with a misleading "unresolved templates" error.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
import yaml

from siteops.models import DeploymentStep, Manifest, Site
from siteops.orchestrator import Orchestrator


def _write_template(workspace, name: str, params: list[str]) -> str:
    """Write a minimal Bicep template that declares the given params."""
    body = "\n".join(f"param {p} string" for p in params)
    path = workspace / "templates" / f"{name}.bicep"
    path.write_text(body + "\n", encoding="utf-8")
    return f"templates/{name}.bicep"


def _make_manifest(workspace, step_name: str, template_rel: str) -> Manifest:
    manifest_data = {
        "apiVersion": "siteops/v1",
        "kind": "Manifest",
        "name": "unresolved-test",
        "sites": ["test-site"],
        "steps": [
            {
                "name": step_name,
                "template": template_rel,
                "scope": "resourceGroup",
            }
        ],
    }
    manifest_path = workspace / "manifests" / "unresolved-test.yaml"
    manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
    return Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)


def _make_site() -> Site:
    return Site(
        name="test-site",
        subscription="00000000-0000-0000-0000-000000000000",
        resource_group="rg-test",
        location="eastus",
        labels={},
    )


class TestUnresolvedTemplateGuard:
    """Hard-fail behavior for `{{ ... }}` tokens that survive resolution."""

    def test_unresolved_in_accepted_param_raises(self, tmp_workspace):
        """A surviving `{{ ... }}` in a template-accepted param must raise."""
        template_rel = _write_template(tmp_workspace, "accepts-name", ["name"])
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        # Inject a parameter file whose value references a non-existent step.
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match="Unresolved template"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

    def test_unresolved_in_filtered_out_param_does_not_raise(self, tmp_workspace):
        """A `{{ ... }}` left on a param the template does NOT accept is
        filtered out before the unresolved-check and must not raise.

        This verifies the filter-then-check ordering: common.yaml-injected
        defaults (e.g. `siteAddress.country`) targeting non-consuming steps
        must not break deployment.
        """
        template_rel = _write_template(tmp_workspace, "accepts-name", ["name"])
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {
            "name": "valid-value",
            "extraneous": "{{ steps.unrelated.outputs.id }}",
        }

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        params = orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

        assert params == {"name": "valid-value"}
        assert "extraneous" not in params

    def test_unresolved_warns_in_dry_run(self, tmp_workspace, caplog):
        """Dry-run mode downgrades the unresolved-check to a warning."""
        template_rel = _write_template(tmp_workspace, "accepts-name", ["name"])
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace, dry_run=True)
        step = manifest.steps[0]

        with caplog.at_level(logging.WARNING, logger="siteops.orchestrator"):
            params = orchestrator.resolve_parameters(
                step, site, manifest, step_outputs={}
            )

        assert any("Unresolved template" in r.message for r in caplog.records)
        # Dry-run preserves the literal token rather than raising.
        assert "{{ steps.missing.outputs.id }}" in str(params)

    def test_filter_failure_skips_unresolved_check(self, tmp_workspace, caplog):
        """When `filter_parameters` raises, skip the unresolved-check so the
        filter failure surfaces instead of being masked by a misleading
        'unresolved templates' error.
        """
        template_rel = _write_template(tmp_workspace, "accepts-name", ["name"])
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        # Param contains an unresolved token that *would* trip the check if
        # filtering had run successfully.
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with patch(
            "siteops.orchestrator.filter_parameters",
            side_effect=ValueError("simulated bicep build failure"),
        ):
            with caplog.at_level(logging.WARNING, logger="siteops.orchestrator"):
                # Must NOT raise ValueError("Unresolved template ..."); the
                # caller is expected to surface the underlying filter error
                # at the deployment step instead.
                params = orchestrator.resolve_parameters(
                    step, site, manifest, step_outputs={}
                )

        # Filter warning was emitted and explicitly mentions the precheck skip.
        assert any(
            "Could not filter parameters" in r.message
            and "skipping unresolved-template precheck" in r.message
            for r in caplog.records
        )
        # No "Unresolved template" error was raised or logged.
        assert not any("Unresolved template" in r.message for r in caplog.records)
        # Unfiltered token is preserved (caller's deploy will surface root cause).
        assert "{{ steps.missing.outputs.id }}" in str(params)

    def test_unresolved_in_nested_list_raises(self, tmp_workspace):
        """Unresolved tokens inside a list value are detected (recursive walk).

        Bicep array params (e.g. tags, allowlists) accept lists, and an
        unresolved template buried inside one would otherwise reach ARM as a
        literal string element.
        """
        body = "param tags array\n"
        path = tmp_workspace / "templates" / "accepts-tags.bicep"
        path.write_text(body, encoding="utf-8")
        manifest = _make_manifest(tmp_workspace, "deploy", "templates/accepts-tags.bicep")
        site = _make_site()
        site.parameters = {"tags": ["ok", "{{ steps.missing.outputs.id }}", "also-ok"]}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match=r"Unresolved template.*tags\[1\]"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

    def test_unresolved_in_nested_dict_raises(self, tmp_workspace):
        """Unresolved tokens inside an object value are detected (recursive walk)."""
        body = "param config object\n"
        path = tmp_workspace / "templates" / "accepts-config.bicep"
        path.write_text(body, encoding="utf-8")
        manifest = _make_manifest(tmp_workspace, "deploy", "templates/accepts-config.bicep")
        site = _make_site()
        site.parameters = {
            "config": {
                "outer": "fine",
                "nested": {"inner": "{{ steps.missing.outputs.id }}"},
            }
        }

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match=r"Unresolved template.*config\.nested\.inner"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

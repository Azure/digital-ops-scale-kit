"""Tests for manifest validation.

Covers:
- Basic manifest validation
- Manifest-level parameter validation
- Step output reference validation
- Self-reference detection with auto-filter awareness
"""

import json
from unittest.mock import patch

import yaml

from siteops.orchestrator import Orchestrator


class TestValidation:
    """Tests for manifest validation."""

    def test_validate_success(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        errors = orchestrator.validate(manifest_path)
        assert errors == []

    def test_validate_missing_template(self, tmp_workspace, sample_site_file):
        orchestrator = Orchestrator(tmp_workspace)

        manifest_data = {
            "name": "bad-manifest",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "nonexistent.bicep"}],
        }
        manifest_path = tmp_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("Template not found" in e for e in errors)

    def test_validate_missing_step_parameters(self, complete_workspace):
        """Test that missing step parameter files are caught."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "bad-manifest",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "parameters": ["nonexistent.yaml"],
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("Parameter file not found" in e for e in errors)

    def test_validate_no_sites_matched(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "no-match",
            "siteSelector": "nonexistent=value",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "no-match.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("No sites matched" in e for e in errors)

    def test_validate_generic_manifest_passes(self, complete_workspace):
        """A manifest with no `sites:` and no `selector:` is a valid library
        manifest. `validate` should pass without surfacing the missing
        targeting (deploy enforces that separately)."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "generic",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "generic.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert errors == []

    def test_validate_duplicate_non_name_selector_key_surfaces_error(self, complete_workspace):
        """Selector parse errors (e.g. duplicate non-name key) appear in the
        validation error list rather than being silently swallowed."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "test",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "test.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path, selector="env=prod,env=dev")
        assert any("may only appear once" in e for e in errors)

    def test_validate_selector_parse_error_does_not_short_circuit(self, complete_workspace):
        """A selector parse error must NOT skip the other validation
        checks. Operator iterating on a broken manifest deserves to see
        every issue in one pass, not fix the typo and discover the next
        problem on re-run."""
        orchestrator = Orchestrator(complete_workspace)

        # Manifest has BOTH a selector typo AND a missing template.
        manifest_data = {
            "name": "multi-error",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/missing.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "multi.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path, selector="env=prod,env=dev")
        # Both errors must surface so the operator fixes them in one pass.
        assert any("may only appear once" in e for e in errors)
        assert any("Template not found" in e for e in errors)

    def test_validate_selector_parse_error_suppresses_no_match_diagnostic(
        self, complete_workspace
    ):
        """When the selector itself fails to parse, the no-match
        diagnostic is redundant noise. The parse error is the cause."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "selector-typo",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "selector-typo.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path, selector="env=prod,env=dev")
        # Parse error must surface.
        assert any("may only appear once" in e for e in errors)
        # The "matched no sites" diagnostic must NOT also surface.
        assert not any("matched no sites" in e for e in errors)
        assert not any("No sites matched" in e for e in errors)

    def test_validate_non_selector_value_error_still_shows_no_match(
        self, complete_workspace
    ):
        """A non-selector ValueError (e.g. overlay-rename) must NOT
        suppress the no-match diagnostic. Only SelectorParseError
        does."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "no-match",
            "siteSelector": "nonexistent=value",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "no-match-cli.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        # CLI selector parses cleanly but matches zero sites in the
        # workspace.
        errors = orchestrator.validate(manifest_path, selector="environment=nope")
        # Rich diagnostic surfaces.
        assert any("matched no sites" in e for e in errors)

    def test_validate_unresolved_site_in_manifest_returns_error_not_traceback(
        self, complete_workspace
    ):
        """A manifest `sites:` entry that does not resolve to a workspace
        file must surface as a validation error, not a `FileNotFoundError`
        traceback. `validate` must catch `OSError` alongside `ValueError`."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "missing-site",
            "sites": ["does-not-exist"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "missing-site.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        # Must not raise.
        errors = orchestrator.validate(manifest_path)
        assert any("does-not-exist" in e for e in errors)

    def test_validate_invalid_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "bad-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "invalid condition syntax",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)

        assert len(errors) > 0
        assert any("when" in e.lower() or "condition" in e.lower() or "parse" in e.lower() for e in errors)

    # --- Manifest-level parameter validation tests ---

    def test_validate_manifest_parameters_exist(self, tmp_path):
        """Test that existing manifest parameter files pass validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file = workspace / "parameters" / "common.yaml"
        params_file.write_text("location: eastus\nenvironment: dev\n")

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/common.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter file" in e]
        assert param_errors == []

    def test_validate_manifest_parameters_missing_file(self, tmp_path):
        """Test that missing manifest parameter file is caught."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/nonexistent.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Manifest parameter file not found" in e for e in errors)
        assert any("nonexistent.yaml" in e for e in errors)

    def test_validate_manifest_parameters_invalid_yaml(self, tmp_path):
        """Test that invalid YAML in manifest parameter file is caught."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file = workspace / "parameters" / "invalid.yaml"
        params_file.write_text(
            """
location: eastus
  invalid indentation: broken
    this: is not valid yaml
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/invalid.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Invalid manifest parameter file" in e for e in errors)
        assert any("invalid.yaml" in e for e in errors)

    def test_validate_multiple_manifest_parameters(self, tmp_path):
        """Test validation with multiple manifest parameter files (one missing)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file1 = workspace / "parameters" / "common.yaml"
        params_file1.write_text("location: eastus")

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/common.yaml
  - parameters/missing.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Manifest parameter file not found" in e for e in errors)
        assert any("missing.yaml" in e for e in errors)
        assert not any("common.yaml" in e for e in errors)

    def test_validate_manifest_parameters_empty_list(self, tmp_path):
        """Test that empty manifest parameters list passes validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters: []
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter" in e]
        assert param_errors == []

    def test_validate_manifest_parameters_no_field(self, tmp_path):
        """Test that missing parameters field passes validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter" in e]
        assert param_errors == []

    def test_validate_truthy_condition_syntax(self, complete_workspace):
        """Test that truthy condition syntax passes validation."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "truthy-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.deployOptions.enabled }}",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "truthy.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        condition_errors = [e for e in errors if "condition" in e.lower() or "when" in e.lower()]
        assert condition_errors == [], f"Unexpected condition errors: {condition_errors}"

    def test_validate_unquoted_boolean_condition_syntax(self, complete_workspace):
        """Test that unquoted boolean condition syntax passes validation."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "boolean-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.includeSolution == true }}",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "boolean.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        condition_errors = [e for e in errors if "condition" in e.lower() or "when" in e.lower()]
        assert condition_errors == [], f"Unexpected condition errors: {condition_errors}"

    def test_validate_no_steps_defined(self, tmp_workspace, sample_site_file):
        """Test that manifests with no steps produce an error."""
        orchestrator = Orchestrator(tmp_workspace)

        manifest_path = tmp_workspace / "manifests" / "empty-steps.yaml"
        manifest_path.write_text(
            """
name: empty-steps
sites:
  - test-site
steps: []
"""
        )

        errors = orchestrator.validate(manifest_path)
        assert any("no steps" in e.lower() for e in errors)

    def test_validate_rg_missing_for_rg_scoped_step(self, tmp_workspace):
        """Test that RG-scoped steps error when no RG-level sites exist and
        subscription-scoped steps are absent (sites without resourceGroup are
        subscription-level and skip RG-scoped steps, but the manifest should
        still have at least one RG-level site to be meaningful)."""
        # Create a subscription-level site only (no resourceGroup)
        (tmp_workspace / "sites" / "sub-only-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: sub-only-site
subscription: "00000000-0000-0000-0000-000000000000"
location: eastus
"""
        )

        # Create template
        template_path = tmp_workspace / "templates" / "test.bicep"
        template_path.write_text("param location string")

        # Manifest with only RG-scoped steps but only subscription-level sites
        manifest_path = tmp_workspace / "manifests" / "rg-check.yaml"
        manifest_path.write_text(
            """
name: rg-check
sites:
  - sub-only-site
steps:
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)
        # Subscription-level sites are skipped for RG-scoped steps.
        # No error is produced because the site is exempt, not invalid.
        # This validates the exemption logic works correctly.
        assert not any("missing 'resourceGroup'" in e for e in errors)

    def test_validate_duplicate_step_names(self, complete_workspace):
        """Test behavior when manifest has duplicate step names."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "dup-steps",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "deploy-infra",
                    "template": "templates/test.bicep",
                },
                {
                    "name": "deploy-infra",
                    "template": "templates/test.bicep",
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "dup.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        dup_errors = [e for e in errors if "duplicate" in e.lower()]
        assert len(dup_errors) == 1
        assert "deploy-infra" in dup_errors[0]


    def test_validate_dynamic_parameter_path_resolved(self, complete_workspace):
        """Validation should resolve {{ site.properties.* }} in parameter paths."""
        orchestrator = Orchestrator(complete_workspace)

        # Create a site with a property and a matching parameter file
        site_data = {
            "name": "test-site",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "properties": {"variant": "standard"},
        }
        (complete_workspace / "sites" / "test-site.yaml").write_text(yaml.dump(site_data))

        # Create the version-specific parameter file
        variant_dir = complete_workspace / "parameters" / "variants"
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "standard.yaml").write_text(yaml.dump({"someParam": "value"}))

        manifest_data = {
            "name": "dynamic-path-test",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "deploy",
                    "template": "templates/test.bicep",
                    "parameters": [
                        "parameters/variants/{{ site.properties.variant }}.yaml",
                    ],
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "dynamic-path.yaml"
        manifest_path.write_text(yaml.dump(manifest_data))

        errors = orchestrator.validate(manifest_path)
        param_errors = [e for e in errors if "variants" in e]
        assert param_errors == [], f"Dynamic path should resolve: {param_errors}"

    def test_validate_dynamic_parameter_path_missing_file(self, complete_workspace):
        """Validation should report missing files for resolved dynamic paths."""
        orchestrator = Orchestrator(complete_workspace)

        site_data = {
            "name": "test-site-missing",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "properties": {"variant": "nonexistent"},
        }
        (complete_workspace / "sites" / "test-site-missing.yaml").write_text(yaml.dump(site_data))

        manifest_data = {
            "name": "dynamic-path-missing",
            "sites": ["test-site-missing"],
            "steps": [
                {
                    "name": "deploy",
                    "template": "templates/test.bicep",
                    "parameters": [
                        "parameters/variants/{{ site.properties.variant }}.yaml",
                    ],
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "dynamic-path-missing.yaml"
        manifest_path.write_text(yaml.dump(manifest_data))

        errors = orchestrator.validate(manifest_path)
        param_errors = [e for e in errors if "nonexistent" in e]
        assert len(param_errors) == 1
        assert "test-site-missing" in param_errors[0]

    def test_validate_dynamic_manifest_level_parameter_path(self, complete_workspace):
        """Validation should resolve dynamic paths in manifest-level parameters."""
        orchestrator = Orchestrator(complete_workspace)

        site_data = {
            "name": "test-site-manifest-dyn",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "properties": {"variant": "standard"},
        }
        (complete_workspace / "sites" / "test-site-manifest-dyn.yaml").write_text(yaml.dump(site_data))

        variant_dir = complete_workspace / "parameters" / "variants"
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "standard.yaml").write_text(yaml.dump({"someParam": "value"}))

        manifest_data = {
            "name": "manifest-dyn-path",
            "sites": ["test-site-manifest-dyn"],
            "parameters": [
                "parameters/variants/{{ site.properties.variant }}.yaml",
            ],
            "steps": [
                {
                    "name": "deploy",
                    "template": "templates/test.bicep",
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "manifest-dyn.yaml"
        manifest_path.write_text(yaml.dump(manifest_data))

        errors = orchestrator.validate(manifest_path)
        param_errors = [e for e in errors if "variants" in e]
        assert param_errors == [], f"Manifest-level dynamic path should resolve: {param_errors}"

    def test_validate_dynamic_parameter_path_invalid_yaml(self, complete_workspace):
        """Validation should report invalid YAML in resolved dynamic parameter files."""
        orchestrator = Orchestrator(complete_workspace)

        site_data = {
            "name": "test-site-bad-yaml",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "properties": {"variant": "broken"},
        }
        (complete_workspace / "sites" / "test-site-bad-yaml.yaml").write_text(yaml.dump(site_data))

        variant_dir = complete_workspace / "parameters" / "variants"
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "broken.yaml").write_text("{ invalid yaml: [unclosed")

        manifest_data = {
            "name": "dyn-path-bad-yaml",
            "sites": ["test-site-bad-yaml"],
            "steps": [
                {
                    "name": "deploy",
                    "template": "templates/test.bicep",
                    "parameters": [
                        "parameters/variants/{{ site.properties.variant }}.yaml",
                    ],
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "dyn-bad-yaml.yaml"
        manifest_path.write_text(yaml.dump(manifest_data))

        errors = orchestrator.validate(manifest_path)
        yaml_errors = [e for e in errors if "Invalid" in e and "broken" in e]
        assert len(yaml_errors) == 1

    def test_validate_dynamic_parameter_path_checks_output_refs(self, complete_workspace):
        """Validation should check output references in resolved dynamic parameter files."""
        orchestrator = Orchestrator(complete_workspace)

        site_data = {
            "name": "test-site-outref",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "properties": {"variant": "with-refs"},
        }
        (complete_workspace / "sites" / "test-site-outref.yaml").write_text(yaml.dump(site_data))

        variant_dir = complete_workspace / "parameters" / "variants"
        variant_dir.mkdir(parents=True, exist_ok=True)
        (variant_dir / "with-refs.yaml").write_text(yaml.dump({
            "someId": "{{ steps.nonexistent-step.outputs.id }}"
        }))

        manifest_data = {
            "name": "dyn-path-outref",
            "sites": ["test-site-outref"],
            "steps": [
                {
                    "name": "deploy",
                    "template": "templates/test.bicep",
                    "parameters": [
                        "parameters/variants/{{ site.properties.variant }}.yaml",
                    ],
                },
            ],
        }
        manifest_path = complete_workspace / "manifests" / "dyn-outref.yaml"
        manifest_path.write_text(yaml.dump(manifest_data))

        errors = orchestrator.validate(manifest_path)
        ref_errors = [e for e in errors if "nonexistent-step" in e]
        assert len(ref_errors) >= 1


class TestKubectlValidation:
    """Tests for kubectl step validation."""

    def test_validate_kubectl_http_url_rejected(self, tmp_workspace, sample_site_file):
        """Test that HTTP URLs in kubectl files are rejected."""
        orchestrator = Orchestrator(tmp_workspace)

        manifest_path = tmp_workspace / "manifests" / "kubectl-http.yaml"
        manifest_path.write_text(
            """
name: kubectl-http
sites:
  - test-site
steps:
  - name: apply-step
    type: kubectl
    operation: apply
    arc:
      name: my-cluster
      resourceGroup: rg-test
    files:
      - http://insecure.example.com/deployment.yaml
"""
        )

        errors = orchestrator.validate(manifest_path)
        assert any("HTTP URLs not allowed" in e for e in errors)

    def test_validate_kubectl_https_url_accepted(self, tmp_workspace, sample_site_file):
        """Test that HTTPS URLs in kubectl files pass validation."""
        orchestrator = Orchestrator(tmp_workspace)

        manifest_path = tmp_workspace / "manifests" / "kubectl-https.yaml"
        manifest_path.write_text(
            """
name: kubectl-https
sites:
  - test-site
steps:
  - name: apply-step
    type: kubectl
    operation: apply
    arc:
      name: my-cluster
      resourceGroup: rg-test
    files:
      - https://raw.githubusercontent.com/example/repo/main/deploy.yaml
"""
        )

        errors = orchestrator.validate(manifest_path)
        # No kubectl-related errors expected
        assert not any("HTTP URLs not allowed" in e for e in errors)
        assert not any("Kubectl file not found" in e for e in errors)

    def test_validate_kubectl_missing_local_file(self, tmp_workspace, sample_site_file):
        """Test that missing local kubectl files are caught."""
        orchestrator = Orchestrator(tmp_workspace)

        manifest_path = tmp_workspace / "manifests" / "kubectl-missing.yaml"
        manifest_path.write_text(
            """
name: kubectl-missing
sites:
  - test-site
steps:
  - name: apply-step
    type: kubectl
    operation: apply
    arc:
      name: my-cluster
      resourceGroup: rg-test
    files:
      - manifests/nonexistent.yaml
"""
        )

        errors = orchestrator.validate(manifest_path)
        assert any("Kubectl file not found" in e for e in errors)

    def test_validate_kubectl_existing_local_file(self, tmp_workspace, sample_site_file):
        """Test that existing local kubectl files pass validation."""
        orchestrator = Orchestrator(tmp_workspace)

        # Create a local file that the kubectl step references
        kubectl_file = tmp_workspace / "manifests" / "deployment.yaml"
        kubectl_file.write_text("apiVersion: apps/v1\nkind: Deployment\n")

        manifest_path = tmp_workspace / "manifests" / "kubectl-local.yaml"
        manifest_path.write_text(
            """
name: kubectl-local
sites:
  - test-site
steps:
  - name: apply-step
    type: kubectl
    operation: apply
    arc:
      name: my-cluster
      resourceGroup: rg-test
    files:
      - manifests/deployment.yaml
"""
        )

        errors = orchestrator.validate(manifest_path)
        assert not any("Kubectl file not found" in e for e in errors)

    def test_validate_kubectl_template_in_file_path_skipped(self, tmp_workspace, sample_site_file):
        """Test that kubectl file paths with templates are skipped during validation."""
        orchestrator = Orchestrator(tmp_workspace)

        manifest_path = tmp_workspace / "manifests" / "kubectl-template.yaml"
        manifest_path.write_text(
            """
name: kubectl-template
sites:
  - test-site
steps:
  - name: apply-step
    type: kubectl
    operation: apply
    arc:
      name: my-cluster
      resourceGroup: rg-test
    files:
      - "{{ site.parameters.kubectlFile }}"
"""
        )

        errors = orchestrator.validate(manifest_path)
        # Template paths should be skipped, not treated as missing files
        assert not any("Kubectl file not found" in e for e in errors)


class TestStepOutputReferenceValidation:
    """Tests for {{ steps.X.outputs.Y }} reference validation."""

    def _create_test_workspace(self, tmp_workspace, manifest_yaml, param_files):
        """Helper to create workspace with manifest and parameter files."""
        (tmp_workspace / "manifests").mkdir(exist_ok=True)
        (tmp_workspace / "templates").mkdir(exist_ok=True)
        (tmp_workspace / "parameters").mkdir(exist_ok=True)
        (tmp_workspace / "sites").mkdir(exist_ok=True)

        (tmp_workspace / "manifests" / "test.yaml").write_text(manifest_yaml)
        (tmp_workspace / "templates" / "test.bicep").write_text("// bicep template")
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            "name: test-site\n"
            "subscription: '00000000-0000-0000-0000-000000000000'\n"
            "resourceGroup: rg-test\n"
            "location: eastus\n"
        )

        for path, content in param_files.items():
            param_path = tmp_workspace / path
            param_path.parent.mkdir(parents=True, exist_ok=True)
            param_path.write_text(content)

        return tmp_workspace / "manifests" / "test.yaml"

    def test_valid_reference_to_prior_step(self, tmp_workspace):
        """Reference to a step that runs earlier should pass validation."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    parameters: [parameters/step1.yaml]
  - name: step2
    template: templates/test.bicep
    parameters: [parameters/step2.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": "param1: value1",
                "parameters/step2.yaml": 'resourceId: "{{ steps.step1.outputs.id }}"',
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors, f"Expected no errors, got: {errors}"

    def test_reference_to_nonexistent_step(self, tmp_workspace):
        """Reference to a step that doesn't exist should fail."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {"parameters/step1.yaml": 'value: "{{ steps.nonexistent.outputs.id }}"'},
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'nonexistent'" in errors[0]
        assert "step1" in errors[0]

    def test_reference_to_later_step(self, tmp_workspace):
        """Reference to a step that runs later should fail."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: first
    template: templates/test.bicep
    parameters: [parameters/first.yaml]
  - name: second
    template: templates/test.bicep
    parameters: [parameters/second.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/first.yaml": 'value: "{{ steps.second.outputs.id }}"',
                "parameters/second.yaml": "param: value",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "runs later" in errors[0]

    def test_nested_references_in_dict(self, tmp_workspace):
        """References nested in dict structures should be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
config:
  nested:
    deep:
      value: "{{ steps.unknown.outputs.id }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'unknown'" in errors[0]

    def test_references_in_list(self, tmp_workspace):
        """References in list items should be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
items:
  - "{{ steps.missing1.outputs.a }}"
  - static-value
  - "{{ steps.missing2.outputs.b }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 2
        assert any("'missing1'" in e for e in errors)
        assert any("'missing2'" in e for e in errors)

    def test_multiple_references_in_single_string(self, tmp_workspace):
        """Multiple references in one string should all be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
  - name: step2
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step2.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": "param: value",
                # step2 refs step1 (valid) and unknown (invalid) in same string
                "parameters/step2.yaml": 'combined: "{{ steps.step1.outputs.a }}-{{ steps.unknown.outputs.b }}"',
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'unknown'" in errors[0]

    def test_valid_chain_of_three_steps(self, tmp_workspace):
        """Chain of valid references across multiple steps should pass."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: create-storage
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/storage.yaml]
  - name: create-registry
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/registry.yaml]
  - name: create-instance
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/instance.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/storage.yaml": "name: storage",
                "parameters/registry.yaml": 'storageId: "{{ steps.create-storage.outputs.id }}"',
                "parameters/instance.yaml": """
storageId: "{{ steps.create-storage.outputs.id }}"
registryId: "{{ steps.create-registry.outputs.id }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors, f"Expected no errors, got: {errors}"

    def test_no_references_passes(self, tmp_workspace):
        """Parameters without step references should pass."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
simpleValue: hello
nestedConfig:
  key: value
  list: [a, b, c]
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors

    def test_site_variable_references_ignored(self, tmp_workspace):
        """{{ site.X }} references should not be flagged as step reference errors."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
location: "{{ site.location }}"
name: "{{ site.name }}"
cluster: "{{ site.labels.clusterName }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors

    def test_self_reference_allowed_when_filtered(self, tmp_workspace):
        """Self-references should be allowed if auto-filtering will remove them."""
        template = tmp_workspace / "templates" / "simple.json"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(json.dumps({
            "parameters": {
                "location": {"type": "string"},
                "name": {"type": "string"},
            }
        }))

        params = tmp_workspace / "parameters" / "chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('location: eastus\nname: my-resource\nfilteredParam: "{{ steps.my-step.outputs.id }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: my-step
    template: templates/simple.json
    parameters: [parameters/chaining.yaml]
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert not self_ref_errors, f"Unexpected self-reference errors: {self_ref_errors}"

    def test_self_reference_error_when_template_accepts_param(self, tmp_workspace):
        """Self-references should error if template accepts the parameter."""
        # Template that DOES accept instanceName
        template = tmp_workspace / "templates" / "instance.json"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(json.dumps({
            "parameters": {
                "clusterName": {"type": "string"},
                "instanceName": {"type": "string"},
            }
        }))

        # Parameter file with self-reference to a param the template accepts
        params = tmp_workspace / "parameters" / "bad-chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('clusterName: my-cluster\ninstanceName: "{{ steps.my-step.outputs.name }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: my-step
    template: templates/instance.json
    parameters:
      - parameters/bad-chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        # SHOULD error - template accepts instanceName, so self-ref is invalid
        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert self_ref_errors, f"Expected self-reference error, got: {errors}"

    def test_self_reference_conservative_when_template_unreadable(self, tmp_workspace):
        """Self-references should error if template params can't be extracted."""
        # Create template that will fail to parse
        template = tmp_workspace / "templates" / "bad.bicep"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("param location string")

        params = tmp_workspace / "parameters" / "chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('location: eastus\nselfRef: "{{ steps.my-step.outputs.id }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: my-step
    template: templates/bad.bicep
    parameters:
      - parameters/chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)

        # Mock get_template_parameters to simulate extraction failure
        with patch("siteops.executor.get_template_parameters", side_effect=ValueError("Mock failure")):
            errors = orchestrator.validate(manifest)

        # SHOULD error - can't verify auto-filtering, be conservative
        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert self_ref_errors, f"Expected conservative self-reference error, got: {errors}"

    def test_shared_chaining_file_with_multiple_steps(self, tmp_workspace):
        """A shared chaining.yaml should work when self-refs are auto-filtered."""
        # Template for aio-instance (does NOT accept aioInstanceName - it generates it)
        aio_template = tmp_workspace / "templates" / "aio-instance.json"
        aio_template.parent.mkdir(parents=True, exist_ok=True)
        aio_template.write_text(json.dumps({
            "parameters": {
                "clusterName": {"type": "string"},
                "schemaRegistryId": {"type": "string"},
            }
        }))

        # Template for quickstart (DOES accept aioInstanceName)
        quickstart_template = tmp_workspace / "templates" / "quickstart.json"
        quickstart_template.write_text(json.dumps({
            "parameters": {
                "aioInstanceName": {"type": "string"},
                "clusterName": {"type": "string"},
            }
        }))

        # Shared chaining file with outputs from various steps
        chaining = tmp_workspace / "parameters" / "chaining.yaml"
        chaining.parent.mkdir(parents=True, exist_ok=True)
        chaining.write_text(
            """
# Outputs for aio-instance step
schemaRegistryId: "{{ steps.schema-registry.outputs.id }}"

# Outputs from aio-instance (used by later steps)
aioInstanceName: "{{ steps.aio-instance.outputs.name }}"
"""
        )

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: schema-registry
    template: templates/aio-instance.json
  - name: aio-instance
    template: templates/aio-instance.json
    parameters:
      - parameters/chaining.yaml
  - name: quickstart
    template: templates/quickstart.json
    parameters:
      - parameters/chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        # aio-instance step:
        #   - schemaRegistryId refs schema-registry (valid - prior step)
        #   - aioInstanceName refs self BUT template doesn't accept it (filtered)
        # quickstart step:
        #   - aioInstanceName refs aio-instance (valid - prior step)
        #   - schemaRegistryId refs schema-registry (valid - prior step)
        assert not errors, f"Expected no errors, got: {errors}"

class TestSubscriptionScopedValidation:
    """Tests for subscription-scoped step validation."""

    def test_subscription_step_without_subscription_site(self, tmp_workspace, sample_bicep_template):
        """Error when subscription-scoped step has no subscription-level site."""
        # Create RG-level site only
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - rg-site
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert any("subscription-level site" in e for e in errors)
        assert any("subscription-scoped steps" in e for e in errors)

    def test_subscription_step_with_subscription_site(self, tmp_workspace, sample_bicep_template):
        """No error when subscription-scoped step has subscription-level site."""
        # Create subscription-level site (no resourceGroup)
        (tmp_workspace / "sites" / "sub-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: sub-site
subscription: "00000000-0000-0000-0000-000000000001"
location: eastus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - sub-site
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should not have subscription-level site errors
        assert not any("subscription-level site" in e for e in errors)

    def test_multiple_subscription_sites_same_subscription(self, tmp_workspace, sample_bicep_template):
        """Error when multiple subscription-level sites exist for same subscription."""
        sub_id = "00000000-0000-0000-0000-000000000001"

        # Create two subscription-level sites with same subscription
        (tmp_workspace / "sites" / "sub-site-1.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site-1
subscription: "{sub_id}"
location: eastus
"""
        )

        (tmp_workspace / "sites" / "sub-site-2.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site-2
subscription: "{sub_id}"
location: westus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - sub-site-1
  - sub-site-2
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert any("multiple subscription-level sites" in e.lower() for e in errors)

    def test_mixed_sites_valid_hierarchy(self, tmp_workspace, sample_bicep_template):
        """Valid when subscription-level and RG-level sites exist for same subscription."""
        sub_id = "00000000-0000-0000-0000-000000000001"

        # Create subscription-level site
        (tmp_workspace / "sites" / "sub-site.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site
subscription: "{sub_id}"
location: eastus
"""
        )

        # Create RG-level site with same subscription
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "{sub_id}"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with both subscription and RG-scoped steps
        manifest_path = tmp_workspace / "manifests" / "mixed.yaml"
        manifest_path.write_text(
            """
name: mixed
sites:
  - sub-site
  - rg-site
steps:
  - name: sub-step
    template: templates/test.bicep
    scope: subscription
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should not have subscription-level site errors
        assert not any("subscription-level site" in e for e in errors)
        assert not any("multiple subscription-level sites" in e.lower() for e in errors)
        # Subscription-level sites should NOT trigger "missing resourceGroup" for RG-scoped steps
        assert not any("missing 'resourceGroup'" in e for e in errors)

    def test_no_subscription_step_validation_skipped(self, tmp_workspace, sample_bicep_template):
        """No validation errors when manifest has no subscription-scoped steps."""
        # Create RG-level site only
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with only RG-scoped steps
        manifest_path = tmp_workspace / "manifests" / "rg-only.yaml"
        manifest_path.write_text(
            """
name: rg-only
sites:
  - rg-site
steps:
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should have no subscription-related errors
        assert not any("subscription" in e.lower() for e in errors)

    def test_subscription_step_skipped_when_condition_false(
        self, tmp_workspace, sample_bicep_template
    ):
        """No error when subscription-scoped step has `when` condition that evaluates to false."""
        # Create RG-level site with property that would skip the subscription step
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
properties:
  deployOptions:
    enableGlobalSite: false
"""
        )

        # Create manifest with conditional subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "conditional-sub.yaml"
        manifest_path.write_text(
            """
name: conditional-sub
sites:
  - rg-site
steps:
  - name: global-edge-site
    template: templates/test.bicep
    scope: subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should NOT error because the subscription step would be skipped anyway
        assert not any("subscription-level site" in e for e in errors)

    def test_subscription_step_required_when_condition_true(
        self, tmp_workspace, sample_bicep_template
    ):
        """Error when subscription-scoped step has `when` condition that evaluates to true."""
        # Create RG-level site with property that would execute the subscription step
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
properties:
  deployOptions:
    enableGlobalSite: true
"""
        )

        # Create manifest with conditional subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "conditional-sub.yaml"
        manifest_path.write_text(
            """
name: conditional-sub
sites:
  - rg-site
steps:
  - name: global-edge-site
    template: templates/test.bicep
    scope: subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # SHOULD error because the subscription step would execute
        assert any("subscription-level site" in e for e in errors)

    def test_subscription_step_required_when_any_site_condition_true(
        self, tmp_workspace, sample_bicep_template
    ):
        """Error when any RG-level site's condition evaluates to true."""
        # Create two RG-level sites - one would skip, one would execute
        (tmp_workspace / "sites" / "skip-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: skip-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-skip
location: eastus
properties:
  deployOptions:
    enableGlobalSite: false
"""
        )
        (tmp_workspace / "sites" / "run-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: run-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-run
location: eastus
properties:
  deployOptions:
    enableGlobalSite: true
"""
        )

        # Create manifest with conditional subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "mixed-conditions.yaml"
        manifest_path.write_text(
            """
name: mixed-conditions
sites:
  - skip-site
  - run-site
steps:
  - name: global-edge-site
    template: templates/test.bicep
    scope: subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # SHOULD error because at least one site would execute the subscription step
        assert any("subscription-level site" in e for e in errors)

    def test_subscription_step_multiple_steps_all_skipped(
        self, tmp_workspace, sample_bicep_template
    ):
        """No error when multiple subscription-scoped steps all have false conditions."""
        # Create RG-level site with all conditions false
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
properties:
  deployOptions:
    enableGlobalSite: false
    enableEdgeSite: false
"""
        )

        # Create manifest with multiple conditional subscription-scoped steps
        manifest_path = tmp_workspace / "manifests" / "multi-sub-steps.yaml"
        manifest_path.write_text(
            """
name: multi-sub-steps
sites:
  - rg-site
steps:
  - name: global-edge-site
    template: templates/test.bicep
    scope: subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"
  - name: another-sub-step
    template: templates/test.bicep
    scope: subscription
    when: "{{ site.properties.deployOptions.enableEdgeSite }}"
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should NOT error because all subscription steps would be skipped
        assert not any("subscription-level site" in e for e in errors)
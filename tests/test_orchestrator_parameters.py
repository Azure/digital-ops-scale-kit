"""Tests for parameter resolution and template variable substitution.

Covers:
- Site variable resolution ({{ site.X }})
- Step output chaining ({{ steps.X.outputs.Y }})
- Properties resolution ({{ site.properties.X }})
- Condition evaluation
- Manifest-level parameter merging
"""

import json
import logging

from siteops.models import Manifest, Site
from siteops.orchestrator import Orchestrator


class TestTemplateResolution:
    """Tests for template variable substitution."""

    def test_resolve_site_variables(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="my-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="westus",
            labels={"env": "prod"},
        )

        value = "Resource in {{ site.location }} for {{ site.labels.env }}"
        result = orchestrator._resolve_template_strings(value, site)

        assert result == "Resource in westus for prod"

    def test_resolve_nested_dict(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        value = {
            "location": "{{ site.location }}",
            "tags": {"site": "{{ site.name }}"},
        }
        result = orchestrator._resolve_template_strings(value, site)

        assert result["location"] == "eastus"
        assert result["tags"]["site"] == "test"

    def test_resolve_list(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        value = ["{{ site.name }}", "static", "{{ site.location }}"]
        result = orchestrator._resolve_template_strings(value, site)

        assert result == ["test", "static", "eastus"]


class TestStepOutputChaining:
    """Tests for {{ steps.X.outputs.Y }} resolution."""

    def test_resolve_step_output_simple(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"deploy-storage": {"storageId": "storage-123"}}

        value = "{{ steps.deploy-storage.outputs.storageId }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "storage-123"

    def test_resolve_step_output_nested(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {
            "deploy-network": {
                "vnet": {"value": {"id": "vnet-123"}, "type": "Object"},
            },
        }

        value = "{{ steps.deploy-network.outputs.vnet.id }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "vnet-123"

    def test_resolve_step_output_in_string(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"step1": {"name": "myresource"}}

        value = "Resource: {{ steps.step1.outputs.name }} is ready"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "Resource: myresource is ready"

    def test_resolve_step_output_missing(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {}

        value = "{{ steps.missing.outputs.value }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == value

    def test_resolve_complex_output_type(self, complete_workspace):
        """When entire value is a template, return the actual type (list/dict)."""
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"step1": {"ids": ["id-1", "id-2", "id-3"]}}

        value = "{{ steps.step1.outputs.ids }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == ["id-1", "id-2", "id-3"]

    def test_deep_nested_output_three_plus_levels(self, complete_workspace):
        """Test resolving output nested 3+ levels deep."""
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {
            "deploy": {
                "config": {
                    "value": {"nested": {"deep": {"value": "found"}}},
                    "type": "Object",
                },
            },
        }

        value = "{{ steps.deploy.outputs.config.nested.deep.value }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "found"

    def test_output_with_missing_mid_path(self, complete_workspace):
        """Test resolving output when an intermediate key is missing."""
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {
            "deploy": {
                "config": {
                    "value": {"a": "b"},
                    "type": "Object",
                },
            },
        }

        value = "{{ steps.deploy.outputs.config.nonexistent.subfield }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        # Missing path should leave the template unresolved
        assert result == value


class TestConditionEvaluation:
    """Tests for when condition evaluation."""

    def test_no_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(name="test", subscription="sub", resource_group="rg", location="eastus")

        assert orchestrator._evaluate_condition(None, site) is True
        assert orchestrator._evaluate_condition("", site) is True

    def test_equals_condition_match(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "prod"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == 'prod' }}", site)
        assert result is True

    def test_equals_condition_no_match(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == 'prod' }}", site)
        assert result is False

    def test_not_equals_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env != 'prod' }}", site)
        assert result is True

    def test_missing_label_treated_as_empty(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == '' }}", site)
        assert result is True

    def test_properties_condition_equals_true(self, complete_workspace):
        """Test {{ site.properties.path == true }} with boolean true."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": True}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution == true }}", site)
        assert result is True

    def test_properties_condition_equals_false(self, complete_workspace):
        """Test {{ site.properties.path == false }} with boolean false."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": False}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution == false }}", site)
        assert result is True

    def test_properties_condition_not_equals(self, complete_workspace):
        """Test {{ site.properties.path != 'value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"tier": "standard"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.tier != 'premium' }}", site)
        assert result is True

    def test_properties_condition_nested_path(self, complete_workspace):
        """Test {{ site.properties.deep.nested.path == 'value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deep": {"nested": {"path": "expected"}}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deep.nested.path == 'expected' }}", site)
        assert result is True

    def test_properties_condition_missing_path(self, complete_workspace):
        """Test condition with missing property path returns False for == comparisons."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={},
        )

        # Missing property compared to 'true' should not match (actual_value is "")
        result = orchestrator._evaluate_condition("{{ site.properties.nonexistent == true }}", site)
        assert result is False

    def test_properties_condition_quoted_string(self, complete_workspace):
        """Test {{ site.properties.path == 'string-value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"environment": "production"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.environment == 'production' }}", site)
        assert result is True

    def test_properties_condition_double_quotes(self, complete_workspace):
        """Test {{ site.properties.path == "value" }} with double quotes."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"name": "my-resource"},
        )

        result = orchestrator._evaluate_condition('{{ site.properties.name == "my-resource" }}', site)
        assert result is True


class TestTruthyConditionEvaluation:
    """Tests for truthy condition evaluation (no comparison operator)."""

    def test_truthy_boolean_true(self, complete_workspace):
        """Test {{ site.properties.path }} with boolean True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"enabled": True},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.enabled }}", site)
        assert result is True

    def test_truthy_boolean_false(self, complete_workspace):
        """Test {{ site.properties.path }} with boolean False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"enabled": False},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.enabled }}", site)
        assert result is False

    def test_truthy_nested_boolean(self, complete_workspace):
        """Test {{ site.properties.nested.path }} with nested boolean."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": True}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution }}", site)
        assert result is True

    def test_truthy_string_non_empty(self, complete_workspace):
        """Test truthy check with non-empty string returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "something"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is True

    def test_truthy_string_empty(self, complete_workspace):
        """Test truthy check with empty string returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_false(self, complete_workspace):
        """Test truthy check with string 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_false_uppercase(self, complete_workspace):
        """Test truthy check with string 'FALSE' returns False (case-insensitive)."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "FALSE"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_zero(self, complete_workspace):
        """Test truthy check with string '0' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "0"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_number_nonzero(self, complete_workspace):
        """Test truthy check with non-zero number returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"count": 5},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.count }}", site)
        assert result is True

    def test_truthy_number_zero(self, complete_workspace):
        """Test truthy check with zero returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"count": 0},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.count }}", site)
        assert result is False

    def test_truthy_list_non_empty(self, complete_workspace):
        """Test truthy check with non-empty list returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"items": ["a", "b"]},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.items }}", site)
        assert result is True

    def test_truthy_list_empty(self, complete_workspace):
        """Test truthy check with empty list returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"items": []},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.items }}", site)
        assert result is False

    def test_truthy_dict_non_empty(self, complete_workspace):
        """Test truthy check with non-empty dict returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"config": {"key": "value"}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.config }}", site)
        assert result is True

    def test_truthy_dict_empty(self, complete_workspace):
        """Test truthy check with empty dict returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"config": {}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.config }}", site)
        assert result is False

    def test_truthy_none_value(self, complete_workspace):
        """Test truthy check with None (missing path) returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.nonexistent }}", site)
        assert result is False

    def test_truthy_with_array_index(self, complete_workspace):
        """Test truthy check with array index path."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"endpoints": [{"enabled": True}, {"enabled": False}]},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.endpoints[0].enabled }}", site)
        assert result is True

        result = orchestrator._evaluate_condition("{{ site.properties.endpoints[1].enabled }}", site)
        assert result is False

    def test_truthy_float_nonzero(self, complete_workspace):
        """Test truthy check with non-zero float returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"ratio": 0.5},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.ratio }}", site)
        assert result is True

    def test_truthy_float_zero(self, complete_workspace):
        """Test truthy check with float 0.0 returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"ratio": 0.0},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.ratio }}", site)
        assert result is False

    def test_truthy_labels_not_supported(self, complete_workspace):
        """Test that truthy check on labels returns True for any non-empty label."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "true"},
        )

        # Labels are always strings, so truthy check treats non-empty strings as True
        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is True

    def test_truthy_labels_empty_string(self, complete_workspace):
        """Test that truthy check on empty label string returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"flag": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.flag }}", site)
        assert result is False

    def test_truthy_labels_string_false(self, complete_workspace):
        """Test that truthy check on label with string 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is False


class TestLabelsTruthyConditionEvaluation:
    """Tests for truthy condition evaluation on labels."""

    def test_truthy_label_non_empty(self, complete_workspace):
        """Test truthy check on non-empty label returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "true"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is True

    def test_truthy_label_empty_string(self, complete_workspace):
        """Test truthy check on empty label returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"flag": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.flag }}", site)
        assert result is False

    def test_truthy_label_string_false(self, complete_workspace):
        """Test truthy check on label 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is False

    def test_truthy_label_string_zero(self, complete_workspace):
        """Test truthy check on label '0' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"count": "0"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.count }}", site)
        assert result is False

    def test_truthy_label_missing(self, complete_workspace):
        """Test truthy check on missing label returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.nonexistent }}", site)
        assert result is False


class TestPropertiesResolution:
    """Tests for site.properties template resolution."""

    def test_resolve_simple_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"apiEndpoint": "https://api.example.com"},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.apiEndpoint }}", site)
        assert result == "https://api.example.com"

    def test_resolve_nested_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"mqtt": {"broker": "mqtt://10.0.1.50:1883", "port": 1883}},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.mqtt.broker }}", site)
        assert result == "mqtt://10.0.1.50:1883"

    def test_resolve_array_index_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={
                "endpoints": [
                    {"host": "10.0.1.100", "port": 4840},
                    {"host": "10.0.1.101", "port": 4840},
                ]
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.endpoints[0].host }}", site)
        assert result == "10.0.1.100"

    def test_resolve_entire_array_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"endpoints": [{"host": "10.0.1.100"}, {"host": "10.0.1.101"}]},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.endpoints }}", site)
        assert result == [{"host": "10.0.1.100"}, {"host": "10.0.1.101"}]

    def test_resolve_entire_object_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"mqtt": {"broker": "mqtt://10.0.1.50:1883", "port": 1883}},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.mqtt }}", site)
        assert result == {"broker": "mqtt://10.0.1.50:1883", "port": 1883}

    def test_resolve_property_embedded_in_string(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"host": "10.0.1.100", "port": 4840},
        )

        result = orchestrator._resolve_template_strings(
            "opc.tcp://{{ site.properties.host }}:{{ site.properties.port }}", site
        )
        assert result == "opc.tcp://10.0.1.100:4840"

    def test_resolve_missing_property_unchanged(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.nonexistent }}", site)
        assert result == "{{ site.properties.nonexistent }}"


class TestResolveParametersManifestLevel:
    """Tests for manifest-level parameter resolution and filtering."""

    def _setup_workspace(self, tmp_path):
        """Create standard workspace structure."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "sites").mkdir()
        (workspace / "manifests").mkdir()
        return workspace

    def _create_site(self, workspace, content):
        """Create site file."""
        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(content)

    def _create_template(self, workspace, params):
        """Create ARM JSON template with specified parameters."""
        template_file = workspace / "templates" / "test.json"
        template_file.write_text(json.dumps({"parameters": params}))

    def test_manifest_parameters_merged_before_step_parameters(self, tmp_path):
        """Test that manifest parameters are merged before step parameters."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            "location: westus\nenvironment: shared\nsharedValue: from-manifest\n"
        )
        (workspace / "parameters" / "step.yaml").write_text("environment: step-override\nstepOnlyValue: from-step\n")

        self._create_template(
            workspace,
            {
                "location": {"type": "string"},
                "environment": {"type": "string"},
                "sharedValue": {"type": "string"},
                "stepOnlyValue": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
    parameters: [parameters/step.yaml]
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["environment"] == "step-override"
        assert result["sharedValue"] == "from-manifest"
        assert result["stepOnlyValue"] == "from-step"
        assert result["location"] == "westus"

    def test_manifest_parameters_resolved_with_site_variables(self, tmp_path):
        """Test that {{ site.X }} templates in manifest params are resolved."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
labels:
  environment: dev
  clusterName: arc-dev
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            """
location: "{{ site.location }}"
environment: "{{ site.labels.environment }}"
clusterName: "{{ site.labels.clusterName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "location": {"type": "string"},
                "environment": {"type": "string"},
                "clusterName": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["location"] == "eastus"
        assert result["environment"] == "dev"
        assert result["clusterName"] == "arc-dev"

    def test_parameters_filtered_to_template_accepted(self, tmp_path):
        """Test that parameters are filtered to what the template accepts."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            "location: eastus\nextraManifestParam: should-be-filtered\n"
        )
        (workspace / "parameters" / "step.yaml").write_text("name: my-resource\nextraStepParam: also-filtered\n")

        self._create_template(
            workspace,
            {"location": {"type": "string"}, "name": {"type": "string"}},
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
    parameters: [parameters/step.yaml]
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result == {"location": "eastus", "name": "my-resource"}
        assert "extraManifestParam" not in result
        assert "extraStepParam" not in result

    def test_full_merge_order_manifest_site_step(self, tmp_path):
        """Test the complete merge order: manifest → site → step.

        Verifies that:
        - Manifest provides base defaults
        - Site overrides manifest values
        - Step overrides both manifest and site values
        """
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  fromManifest: site-override
  fromSite: site-value
  fromAll: site-wins
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            "fromManifest: manifest-value\nfromAll: manifest-value\n"
        )
        (workspace / "parameters" / "step.yaml").write_text("fromAll: step-wins\nfromStep: step-value\n")

        self._create_template(
            workspace,
            {
                "fromManifest": {"type": "string"},
                "fromSite": {"type": "string"},
                "fromStep": {"type": "string"},
                "fromAll": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
    parameters: [parameters/step.yaml]
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        # Manifest value, overridden by site
        assert result["fromManifest"] == "site-override"
        # Site-only value
        assert result["fromSite"] == "site-value"
        # Step-only value
        assert result["fromStep"] == "step-value"
        # All three levels define this - step wins
        assert result["fromAll"] == "step-wins"

    def test_site_parameters_override_manifest_parameters(self, tmp_path):
        """Test that site.parameters override manifest parameters."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  siteParam: from-site
  sharedParam: site-value
""",
        )

        (workspace / "parameters" / "common.yaml").write_text("sharedParam: manifest-value\n")

        self._create_template(
            workspace,
            {
                "siteParam": {"type": "string"},
                "sharedParam": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["siteParam"] == "from-site"
        # Site params override manifest params (more specific wins)
        assert result["sharedParam"] == "site-value"

    def test_missing_manifest_parameter_file_logs_warning(self, tmp_path, caplog):
        """Test that missing manifest parameter file logs a warning."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        self._create_template(workspace, {})

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/nonexistent.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        with caplog.at_level(logging.WARNING):
            orchestrator.resolve_parameters(step, site, manifest, {})

        assert any("not found" in record.message.lower() for record in caplog.records)

    def test_deep_merge_for_manifest_parameters(self, tmp_path):
        """Test that manifest parameters use deep merge for nested objects."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        # First manifest params file with base values
        (workspace / "parameters" / "common.yaml").write_text(
            """
tags:
  managedBy: siteops
  team: platform
config:
  retries: 3
"""
        )

        # Second manifest params file that extends
        (workspace / "parameters" / "shared.yaml").write_text(
            """
tags:
  environment: dev
config:
  timeout: 30
"""
        )

        self._create_template(
            workspace,
            {
                "tags": {"type": "object"},
                "config": {"type": "object"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters:
  - parameters/common.yaml
  - parameters/shared.yaml
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        # Deep merge should combine nested objects
        assert result["tags"] == {
            "managedBy": "siteops",
            "team": "platform",
            "environment": "dev",
        }
        assert result["config"] == {
            "retries": 3,
            "timeout": 30,
        }


class TestParametersResolution:
    """Tests for site.parameters template resolution."""

    def _setup_workspace(self, tmp_path):
        """Create standard workspace structure."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "sites").mkdir()
        (workspace / "manifests").mkdir()
        return workspace

    def _create_site(self, workspace, content):
        """Create site file."""
        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(content)

    def _create_template(self, workspace, params):
        """Create ARM JSON template with specified parameters."""
        template_file = workspace / "templates" / "test.json"
        template_file.write_text(json.dumps({"parameters": params}))

    def test_resolve_simple_parameter(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-arc-cluster"},
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.clusterName }}", site)
        assert result == "my-arc-cluster"

    def test_resolve_nested_parameter(self, tmp_workspace):
        """Test resolving a nested site parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "brokerConfig": {
                    "memoryProfile": "Medium",
                    "frontendReplicas": 2,
                }
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.brokerConfig.memoryProfile }}", site)
        assert result == "Medium"

    def test_resolve_entire_object_parameter(self, tmp_workspace):
        """Test resolving an entire object parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "brokerConfig": {
                    "memoryProfile": "Medium",
                    "frontendReplicas": 2,
                }
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.brokerConfig }}", site)
        assert result == {"memoryProfile": "Medium", "frontendReplicas": 2}

    def test_resolve_parameter_embedded_in_string(self, tmp_workspace):
        """Test resolving a parameter embedded in a string."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster", "customLocationName": "my-cl"},
        )

        result = orchestrator._resolve_template_strings(
            "Cluster: {{ site.parameters.clusterName }}, Location: {{ site.parameters.customLocationName }}",
            site,
        )
        assert result == "Cluster: my-cluster, Location: my-cl"

    def test_resolve_missing_parameter_unchanged(self, tmp_workspace):
        """Test that missing parameters are left unchanged."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={},
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.nonexistent }}", site)
        assert result == "{{ site.parameters.nonexistent }}"

    def test_resolve_parameter_in_nested_dict(self, tmp_workspace):
        """Test resolving parameters in nested dict structures."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster"},
        )

        value = {
            "resourceId": "/subscriptions/{{ site.subscription }}/clusters/{{ site.parameters.clusterName }}",
            "nested": {
                "cluster": "{{ site.parameters.clusterName }}",
            },
        }
        result = orchestrator._resolve_template_strings(value, site)

        assert result["resourceId"] == "/subscriptions/sub-123/clusters/my-cluster"
        assert result["nested"]["cluster"] == "my-cluster"

    def test_resolve_parameter_in_list(self, tmp_workspace):
        """Test resolving parameters in list structures."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster"},
        )

        value = ["{{ site.parameters.clusterName }}", "static", "{{ site.name }}"]
        result = orchestrator._resolve_template_strings(value, site)

        assert result == ["my-cluster", "static", "test-site"]

    def test_resolve_entire_array_parameter(self, tmp_workspace):
        """Test resolving an entire array parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "endpoints": [
                    {"host": "10.0.1.100", "port": 4840},
                    {"host": "10.0.1.101", "port": 4840},
                ]
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.endpoints }}", site)
        assert result == [
            {"host": "10.0.1.100", "port": 4840},
            {"host": "10.0.1.101", "port": 4840},
        ]

    def test_resolve_parameter_with_overlay(self, tmp_workspace):
        """Test that parameters from overlay are resolved correctly."""
        # Create base site
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: base-cluster
"""
        )

        # Create overlay with parameter override
        (tmp_workspace / "sites.local").mkdir(exist_ok=True)
        (tmp_workspace / "sites.local" / "test-site.yaml").write_text(
            """
parameters:
  clusterName: overlay-cluster
"""
        )

        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = orchestrator.load_site("test-site")

        result = orchestrator._resolve_template_strings("{{ site.parameters.clusterName }}", site)
        assert result == "overlay-cluster"

    def test_site_parameters_template_in_manifest_params(self, tmp_path):
        """Test that {{ site.parameters.X }} in manifest params are resolved."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: my-arc-cluster
  customLocationName: my-cl
""",
        )

        # Parameter file uses {{ site.parameters.X }}
        (workspace / "parameters" / "common.yaml").write_text(
            """
clusterName: "{{ site.parameters.clusterName }}"
customLocationName: "{{ site.parameters.customLocationName }}"
resourceId: "/subscriptions/{{ site.subscription }}/clusters/{{ site.parameters.clusterName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "clusterName": {"type": "string"},
                "customLocationName": {"type": "string"},
                "resourceId": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["clusterName"] == "my-arc-cluster"
        assert result["customLocationName"] == "my-cl"
        assert result["resourceId"] == "/subscriptions/00000000-0000-0000-0000-000000000000/clusters/my-arc-cluster"

    def test_site_overlay_parameters_resolved_in_manifest_params(self, tmp_path):
        """Test that site overlay parameters are resolved in manifest parameter files.

        This is the exact scenario that failed in CI: SITE_OVERRIDES creates
        sites.local/site.yaml with parameters.clusterName override, and
        manifest parameters reference {{ site.parameters.clusterName }}.
        """
        workspace = self._setup_workspace(tmp_path)

        # Base site with placeholder values
        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: placeholder-cluster
  customLocationName: placeholder-cl
""",
        )

        # Local overlay (simulates SITE_OVERRIDES in CI)
        (workspace / "sites.local").mkdir(exist_ok=True)
        (workspace / "sites.local" / "test-site.yaml").write_text(
            """
parameters:
  clusterName: real-cluster-from-overlay
"""
        )

        # Parameter file references site parameters
        (workspace / "parameters" / "common.yaml").write_text(
            """
clusterName: "{{ site.parameters.clusterName }}"
customLocationName: "{{ site.parameters.customLocationName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "clusterName": {"type": "string"},
                "customLocationName": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml", workspace_root=workspace)
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        # Overlay value should be used, not placeholder
        assert result["clusterName"] == "real-cluster-from-overlay"
        # Non-overridden value preserved from base
        assert result["customLocationName"] == "placeholder-cl"

class TestSubscriptionOutputExtraction:
    """Tests for extracting outputs from subscription-scoped step results."""

    def test_extract_subscription_outputs_basic(self, complete_workspace):
        """Test that outputs are correctly extracted from step results."""
        orchestrator = Orchestrator(complete_workspace)

        result = {
            "site": "sub-site",
            "status": "success",
            "steps": [
                {
                    "step": "shared-resource",
                    "status": "success",
                    "outputs": {"resourceId": "/subscriptions/123/resource"},
                },
                {
                    "step": "another-step",
                    "status": "success",
                    "outputs": {"value": "test"},
                },
            ],
        }

        subscription_outputs = {}
        orchestrator._extract_subscription_outputs(result, "sub-123", subscription_outputs)

        assert "sub-123" in subscription_outputs
        assert subscription_outputs["sub-123"]["shared-resource"] == {"resourceId": "/subscriptions/123/resource"}
        assert subscription_outputs["sub-123"]["another-step"] == {"value": "test"}

    def test_extract_subscription_outputs_skips_failed_steps(self, complete_workspace):
        """Test that failed steps don't have outputs extracted."""
        orchestrator = Orchestrator(complete_workspace)

        result = {
            "site": "sub-site",
            "status": "failed",
            "steps": [
                {
                    "step": "success-step",
                    "status": "success",
                    "outputs": {"value": "ok"},
                },
                {
                    "step": "failed-step",
                    "status": "failed",
                    "error": "Something went wrong",
                },
            ],
        }

        subscription_outputs = {}
        orchestrator._extract_subscription_outputs(result, "sub-123", subscription_outputs)

        assert "success-step" in subscription_outputs["sub-123"]
        assert "failed-step" not in subscription_outputs["sub-123"]

    def test_extract_subscription_outputs_skips_empty_outputs(self, complete_workspace):
        """Test that steps with no outputs are skipped."""
        orchestrator = Orchestrator(complete_workspace)

        result = {
            "site": "sub-site",
            "status": "success",
            "steps": [
                {
                    "step": "no-output-step",
                    "status": "success",
                    "outputs": {},
                },
                {
                    "step": "with-output",
                    "status": "success",
                    "outputs": {"id": "123"},
                },
            ],
        }

        subscription_outputs = {}
        orchestrator._extract_subscription_outputs(result, "sub-123", subscription_outputs)

        assert "no-output-step" not in subscription_outputs["sub-123"]
        assert "with-output" in subscription_outputs["sub-123"]

    def test_extract_subscription_outputs_multiple_subscriptions(self, complete_workspace):
        """Test outputs are keyed by subscription ID correctly."""
        orchestrator = Orchestrator(complete_workspace)

        subscription_outputs = {}

        result1 = {
            "site": "sub-site-1",
            "status": "success",
            "steps": [{"step": "step1", "status": "success", "outputs": {"id": "a"}}],
        }
        orchestrator._extract_subscription_outputs(result1, "sub-111", subscription_outputs)

        result2 = {
            "site": "sub-site-2",
            "status": "success",
            "steps": [{"step": "step1", "status": "success", "outputs": {"id": "b"}}],
        }
        orchestrator._extract_subscription_outputs(result2, "sub-222", subscription_outputs)

        assert subscription_outputs["sub-111"]["step1"]["id"] == "a"
        assert subscription_outputs["sub-222"]["step1"]["id"] == "b"


class TestCrossScopeOutputResolution:
    """Tests for resolving outputs across subscription/RG scope boundaries."""

    def test_resolve_output_from_subscription_outputs(self, complete_workspace):
        """Test that subscription outputs can be resolved for RG-level sites."""
        orchestrator = Orchestrator(complete_workspace)

        step_outputs = {}  # Per-site outputs (empty for this test)
        subscription_outputs = {
            "sub-123": {
                "shared-resource": {"resourceId": "/subscriptions/123/shared"}
            }
        }

        value = "{{ steps.shared-resource.outputs.resourceId }}"
        result = orchestrator._resolve_step_outputs(
            value, step_outputs, subscription_outputs, "sub-123"
        )

        assert result == "/subscriptions/123/shared"

    def test_per_site_outputs_take_precedence(self, complete_workspace):
        """Test that per-site outputs override subscription outputs."""
        orchestrator = Orchestrator(complete_workspace)

        step_outputs = {
            "shared-resource": {"resourceId": "/per-site-value"}
        }
        subscription_outputs = {
            "sub-123": {
                "shared-resource": {"resourceId": "/subscription-value"}
            }
        }

        value = "{{ steps.shared-resource.outputs.resourceId }}"
        result = orchestrator._resolve_step_outputs(
            value, step_outputs, subscription_outputs, "sub-123"
        )

        # Per-site should win
        assert result == "/per-site-value"

    def test_subscription_output_fallback(self, complete_workspace):
        """Test fallback to subscription outputs when per-site not found."""
        orchestrator = Orchestrator(complete_workspace)

        step_outputs = {
            "rg-step": {"value": "rg-only"}
        }
        subscription_outputs = {
            "sub-123": {
                "sub-step": {"value": "sub-only"}
            }
        }

        # RG step output
        result1 = orchestrator._resolve_step_outputs(
            "{{ steps.rg-step.outputs.value }}",
            step_outputs, subscription_outputs, "sub-123"
        )
        assert result1 == "rg-only"

        # Subscription step output
        result2 = orchestrator._resolve_step_outputs(
            "{{ steps.sub-step.outputs.value }}",
            step_outputs, subscription_outputs, "sub-123"
        )
        assert result2 == "sub-only"

    def test_cross_scope_nested_output(self, complete_workspace):
        """Test nested output paths work with cross-scope resolution."""
        orchestrator = Orchestrator(complete_workspace)

        subscription_outputs = {
            "sub-123": {
                "registry": {
                    "schema": {"id": "schema-123", "name": "my-schema"}
                }
            }
        }

        value = "{{ steps.registry.outputs.schema.id }}"
        result = orchestrator._resolve_step_outputs(
            value, {}, subscription_outputs, "sub-123"
        )

        assert result == "schema-123"

    def test_cross_scope_complex_type_output(self, complete_workspace):
        """Test that complex types (arrays, objects) are returned correctly."""
        orchestrator = Orchestrator(complete_workspace)

        subscription_outputs = {
            "sub-123": {
                "enablement": {
                    "extensionIds": ["ext-1", "ext-2", "ext-3"]
                }
            }
        }

        value = "{{ steps.enablement.outputs.extensionIds }}"
        result = orchestrator._resolve_step_outputs(
            value, {}, subscription_outputs, "sub-123"
        )

        assert result == ["ext-1", "ext-2", "ext-3"]

    def test_wrong_subscription_not_resolved(self, complete_workspace):
        """Test outputs from different subscription aren't accidentally resolved."""
        orchestrator = Orchestrator(complete_workspace)

        subscription_outputs = {
            "sub-AAA": {
                "step1": {"value": "from-AAA"}
            }
        }

        # Request with different subscription ID
        value = "{{ steps.step1.outputs.value }}"
        result = orchestrator._resolve_step_outputs(
            value, {}, subscription_outputs, "sub-BBB"
        )

        # Should remain unresolved
        assert result == value


class TestGroupSitesBySubscription:
    """Tests for the _group_sites_by_subscription static method."""

    def test_group_mixed_sites(self, complete_workspace):
        """Test grouping sites into subscription-level and RG-level."""
        from siteops.models import Site

        sites = [
            Site(name="sub-site", subscription="sub-123", resource_group="", location="eastus"),
            Site(name="rg-site-1", subscription="sub-123", resource_group="rg-1", location="eastus"),
            Site(name="rg-site-2", subscription="sub-123", resource_group="rg-2", location="eastus"),
        ]

        groups = Orchestrator._group_sites_by_subscription(sites)

        sub_sites, rg_sites = groups["sub-123"]
        assert len(sub_sites) == 1
        assert sub_sites[0].name == "sub-site"
        assert len(rg_sites) == 2
        assert {s.name for s in rg_sites} == {"rg-site-1", "rg-site-2"}

    def test_group_multiple_subscriptions(self, complete_workspace):
        """Test grouping with multiple subscriptions."""
        from siteops.models import Site

        sites = [
            Site(name="sub-A", subscription="AAA", resource_group="", location="eastus"),
            Site(name="rg-A", subscription="AAA", resource_group="rg", location="eastus"),
            Site(name="sub-B", subscription="BBB", resource_group="", location="westus"),
            Site(name="rg-B", subscription="BBB", resource_group="rg", location="westus"),
        ]

        groups = Orchestrator._group_sites_by_subscription(sites)

        assert len(groups) == 2
        sub_A, rg_A = groups["AAA"]
        sub_B, rg_B = groups["BBB"]

        assert sub_A[0].name == "sub-A"
        assert rg_A[0].name == "rg-A"
        assert sub_B[0].name == "sub-B"
        assert rg_B[0].name == "rg-B"

    def test_group_only_rg_sites(self, complete_workspace):
        """Test grouping when no subscription-level sites exist."""
        from siteops.models import Site

        sites = [
            Site(name="rg-1", subscription="sub-123", resource_group="rg-1", location="eastus"),
            Site(name="rg-2", subscription="sub-123", resource_group="rg-2", location="eastus"),
        ]

        groups = Orchestrator._group_sites_by_subscription(sites)

        sub_sites, rg_sites = groups["sub-123"]
        assert len(sub_sites) == 0
        assert len(rg_sites) == 2


class TestSubscriptionFailureIsolation:
    """Tests for subscription-level failure isolation in two-phase deployment."""

    def test_get_subscription_step_names(self, tmp_workspace, sample_bicep_template):
        """Test extracting subscription-scoped step names from manifest."""
        manifest_path = tmp_workspace / "manifests" / "mixed.yaml"
        manifest_path.write_text(
            """
name: mixed
sites:
  - test-site
steps:
  - name: sub-step-1
    template: templates/test.bicep
    scope: subscription
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
  - name: sub-step-2
    template: templates/test.bicep
    scope: subscription
"""
        )

        from siteops.models import Manifest

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        step_names = Orchestrator._get_subscription_step_names(manifest)

        assert step_names == {"sub-step-1", "sub-step-2"}
        assert "rg-step" not in step_names

    def test_references_any_step_finds_reference(self):
        """Test detecting step output references in parameters."""
        value = {"id": "{{ steps.shared.outputs.resourceId }}"}
        assert Orchestrator._references_any_step(value, {"shared"}) is True
        assert Orchestrator._references_any_step(value, {"other"}) is False

    def test_references_any_step_nested_dict(self):
        """Test detecting step references in nested structures."""
        value = {
            "config": {
                "nested": {
                    "ref": "{{ steps.deep-step.outputs.value }}"
                }
            }
        }
        assert Orchestrator._references_any_step(value, {"deep-step"}) is True
        assert Orchestrator._references_any_step(value, {"shallow-step"}) is False

    def test_references_any_step_in_list(self):
        """Test detecting step references in list values."""
        value = ["static", "{{ steps.list-step.outputs.item }}"]
        assert Orchestrator._references_any_step(value, {"list-step"}) is True

    def test_references_any_step_no_references(self):
        """Test no false positives on values without step references."""
        assert Orchestrator._references_any_step("plain string", {"any"}) is False
        assert Orchestrator._references_any_step({"key": "value"}, {"any"}) is False
        assert Orchestrator._references_any_step(123, {"any"}) is False

    def test_site_depends_on_subscription_outputs_with_dependency(
        self, tmp_workspace, sample_bicep_template
    ):
        """Test site dependency detection when parameter file references subscription step."""
        # Create site
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "sub-123"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create parameter file that references subscription step
        (tmp_workspace / "parameters" / "chaining.yaml").write_text(
            """
sharedId: "{{ steps.sub-step.outputs.resourceId }}"
"""
        )

        # Create manifest with subscription and RG steps
        manifest_path = tmp_workspace / "manifests" / "test.yaml"
        manifest_path.write_text(
            """
name: test
sites:
  - rg-site
steps:
  - name: sub-step
    template: templates/test.bicep
    scope: subscription
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
    parameters:
      - parameters/chaining.yaml
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        from siteops.models import Manifest

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        site = orchestrator.load_site("rg-site")
        sub_step_names = {"sub-step"}

        assert orchestrator._site_depends_on_subscription_outputs(
            manifest, site, sub_step_names
        ) is True

    def test_site_depends_on_subscription_outputs_no_dependency(
        self, tmp_workspace, sample_bicep_template
    ):
        """Test site dependency detection when no subscription outputs are referenced."""
        # Create site
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "sub-123"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create parameter file that only references RG-scoped steps
        (tmp_workspace / "parameters" / "chaining.yaml").write_text(
            """
localId: "{{ steps.rg-step-1.outputs.id }}"
"""
        )

        # Create manifest with subscription and RG steps
        manifest_path = tmp_workspace / "manifests" / "test.yaml"
        manifest_path.write_text(
            """
name: test
sites:
  - rg-site
steps:
  - name: sub-step
    template: templates/test.bicep
    scope: subscription
  - name: rg-step-1
    template: templates/test.bicep
    scope: resourceGroup
  - name: rg-step-2
    template: templates/test.bicep
    scope: resourceGroup
    parameters:
      - parameters/chaining.yaml
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        from siteops.models import Manifest

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        site = orchestrator.load_site("rg-site")
        sub_step_names = {"sub-step"}

        assert orchestrator._site_depends_on_subscription_outputs(
            manifest, site, sub_step_names
        ) is False

    def test_site_depends_checks_manifest_level_params(
        self, tmp_workspace, sample_bicep_template
    ):
        """Test that manifest-level parameters are also checked for dependencies."""
        # Create site
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "sub-123"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest-level parameter file with subscription reference
        (tmp_workspace / "parameters" / "common.yaml").write_text(
            """
sharedId: "{{ steps.sub-step.outputs.resourceId }}"
"""
        )

        manifest_path = tmp_workspace / "manifests" / "test.yaml"
        manifest_path.write_text(
            """
name: test
sites:
  - rg-site
parameters:
  - parameters/common.yaml
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
        from siteops.models import Manifest

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        site = orchestrator.load_site("rg-site")
        sub_step_names = {"sub-step"}

        assert orchestrator._site_depends_on_subscription_outputs(
            manifest, site, sub_step_names
        ) is True


class TestComplexOutputHandling:
    """Tests for handling complex outputs (list/dict) in string contexts."""

    def test_complex_output_in_string_context_warns(self, tmp_workspace, sample_bicep_template, caplog):
        """Test that embedding a complex output in a string logs a warning."""
        import logging

        # Create site
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: sub-123
resourceGroup: rg-test
location: eastus
"""
        )

        orchestrator = Orchestrator(tmp_workspace)

        # Simulate step outputs with a complex value (list)
        step_outputs = {"setup": {"items": ["a", "b", "c"]}}

        # Try to embed the list in a string context (should warn and leave unresolved)
        value = "Items are: {{ steps.setup.outputs.items }} - done"

        with caplog.at_level(logging.WARNING):
            result = orchestrator._resolve_step_outputs(value, step_outputs)

        # The complex output should not be embedded - original template preserved
        assert "{{ steps.setup.outputs.items }}" in result
        assert "Cannot embed complex output" in caplog.text

    def test_complex_output_standalone_resolves(self, tmp_workspace, sample_bicep_template):
        """Test that a complex output as the sole value resolves correctly."""
        # Create site
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: sub-123
resourceGroup: rg-test
location: eastus
"""
        )

        orchestrator = Orchestrator(tmp_workspace)

        # Simulate step outputs with a complex value
        step_outputs = {"setup": {"config": {"key": "value", "nested": True}}}

        # Complex output as standalone value (entire string is the template)
        value = "{{ steps.setup.outputs.config }}"

        result = orchestrator._resolve_step_outputs(value, step_outputs)

        # Standalone complex outputs should resolve to the dict
        assert result == {"key": "value", "nested": True}


class TestPropertyPathEdgeCases:
    """Tests for _resolve_property_path edge cases."""

    def test_null_in_path_traversal(self, tmp_workspace):
        """Test that None value mid-path returns None."""
        orchestrator = Orchestrator(tmp_workspace)

        obj = {"level1": {"level2": None}}
        result = orchestrator._resolve_property_path(obj, "level1.level2.level3")

        assert result is None

    def test_array_index_out_of_bounds(self, tmp_workspace):
        """Test that out-of-bounds array index returns None."""
        orchestrator = Orchestrator(tmp_workspace)

        obj = {"items": ["a", "b"]}
        result = orchestrator._resolve_property_path(obj, "items[5]")

        assert result is None

    def test_array_index_on_non_list(self, tmp_workspace):
        """Test that array index on non-list value returns None."""
        orchestrator = Orchestrator(tmp_workspace)

        obj = {"items": "not-a-list"}
        result = orchestrator._resolve_property_path(obj, "items[0]")

        assert result is None

    def test_array_key_not_in_dict(self, tmp_workspace):
        """Test that array notation with key not in dict returns None."""
        orchestrator = Orchestrator(tmp_workspace)

        obj = {"other": [1, 2, 3]}
        result = orchestrator._resolve_property_path(obj, "missing[0]")

        assert result is None

    def test_missing_dict_key(self, tmp_workspace):
        """Test that missing dict key returns None."""
        orchestrator = Orchestrator(tmp_workspace)

        obj = {"exists": "yes"}
        result = orchestrator._resolve_property_path(obj, "missing")

        assert result is None


class TestResolveStepOutputsRecursion:
    """Tests for dict/list recursion in _resolve_step_outputs."""

    def test_resolve_outputs_in_nested_dict(self, tmp_workspace):
        """Test that step output references in nested dicts are resolved."""
        orchestrator = Orchestrator(tmp_workspace)

        step_outputs = {"step1": {"id": "resource-123"}}
        value = {
            "config": {
                "resourceId": "{{ steps.step1.outputs.id }}",
                "static": "unchanged",
            }
        }

        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result["config"]["resourceId"] == "resource-123"
        assert result["config"]["static"] == "unchanged"

    def test_resolve_outputs_in_list(self, tmp_workspace):
        """Test that step output references in lists are resolved."""
        orchestrator = Orchestrator(tmp_workspace)

        step_outputs = {"step1": {"id": "resource-123"}}
        value = ["{{ steps.step1.outputs.id }}", "static", 42]

        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == ["resource-123", "static", 42]

    def test_resolve_outputs_non_string_passthrough(self, tmp_workspace):
        """Test that non-string/dict/list values pass through unchanged."""
        orchestrator = Orchestrator(tmp_workspace)

        assert orchestrator._resolve_step_outputs(42, {}) == 42
        assert orchestrator._resolve_step_outputs(True, {}) is True
        assert orchestrator._resolve_step_outputs(None, {}) is None

    def test_resolve_unresolved_output_in_embedded_string(self, tmp_workspace):
        """Test that unresolved output in embedded string preserves the template."""
        orchestrator = Orchestrator(tmp_workspace)

        step_outputs = {"step1": {"id": "resource-123"}}
        value = "prefix-{{ steps.missing.outputs.val }}-suffix"

        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "prefix-{{ steps.missing.outputs.val }}-suffix"


class TestParameterTemplateFallbacks:
    """Tests for unresolvable template fallback behavior."""

    def test_unresolvable_parameter_in_embedded_string(self, tmp_workspace):
        """Test that unresolvable {{ site.parameters.X }} in embedded context is preserved."""
        orchestrator = Orchestrator(tmp_workspace)

        result = orchestrator._resolve_parameters_templates(
            "prefix-{{ site.parameters.missing }}-suffix",
            {},
        )

        assert result == "prefix-{{ site.parameters.missing }}-suffix"

    def test_unresolvable_property_in_embedded_string(self, tmp_workspace):
        """Test that unresolvable {{ site.properties.X }} in embedded context is preserved."""
        orchestrator = Orchestrator(tmp_workspace)

        result = orchestrator._resolve_properties_templates(
            "prefix-{{ site.properties.missing }}-suffix",
            {},
        )

        assert result == "prefix-{{ site.properties.missing }}-suffix"

    def test_complex_property_serialized_in_embedded_string(self, tmp_workspace):
        """Test that complex types (dict/list) are JSON-serialized when embedded in a string."""
        orchestrator = Orchestrator(tmp_workspace)

        result = orchestrator._resolve_properties_templates(
            "data={{ site.properties.config }}",
            {"config": {"key": "value"}},
        )

        assert 'data={"key": "value"}' == result


class TestConditionEdgeCases:
    """Tests for condition evaluation edge cases and operators."""

    def test_invalid_condition_syntax_returns_true(self, tmp_workspace):
        """Test that invalid condition syntax returns True (permissive) at runtime."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(name="test", subscription="sub", resource_group="rg", location="eastus")

        # This doesn't match CONDITION_PATTERN
        result = orchestrator._evaluate_condition("not a valid condition", site)
        assert result is True

    def test_unknown_field_type_returns_true(self, tmp_workspace):
        """Test that unknown field prefix returns True (permissive)."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(name="test", subscription="sub", resource_group="rg", location="eastus")

        # "custom.field" doesn't start with "labels." or "properties."
        # This should not match CONDITION_PATTERN at all, so returns True
        result = orchestrator._evaluate_condition("{{ site.custom.field == 'x' }}", site)
        assert result is True

    def test_not_equals_operator_on_properties(self, tmp_workspace):
        """Test != operator on site.properties for string comparison."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"env": "staging"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.env != 'prod' }}", site)
        assert result is True

        result = orchestrator._evaluate_condition("{{ site.properties.env != 'staging' }}", site)
        assert result is False

    def test_enable_secret_sync_truthy_true(self, tmp_workspace):
        """Test truthy evaluation of enableSecretSync set to True."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"enableSecretSync": True}},
        )

        result = orchestrator._evaluate_condition(
            "{{ site.properties.deployOptions.enableSecretSync }}", site
        )
        assert result is True

    def test_enable_secret_sync_truthy_false(self, tmp_workspace):
        """Test truthy evaluation of enableSecretSync set to False."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"enableSecretSync": False}},
        )

        result = orchestrator._evaluate_condition(
            "{{ site.properties.deployOptions.enableSecretSync }}", site
        )
        assert result is False

    def test_missing_intermediate_property_path_returns_falsy(self, tmp_workspace):
        """Test that missing intermediate key 'deployOptions' returns False."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={},
        )

        result = orchestrator._evaluate_condition(
            "{{ site.properties.deployOptions.enableSecretSync }}", site
        )
        assert result is False

    def test_string_false_treated_as_falsy(self, tmp_workspace):
        """Test that string 'false' is treated as falsy in truthy context."""
        orchestrator = Orchestrator(tmp_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"flag": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.flag }}", site)
        # The string "false" is treated as falsy (case-insensitive check)
        assert result is False
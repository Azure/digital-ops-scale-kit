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
from unittest.mock import patch

import pytest
import yaml

from siteops.composition import CompositionError
from siteops.models import (
    AnyCondition,
    DeploymentStep,
    Manifest,
    MultipleSubscriptionSitesError,
    ParameterSelectionError,
    ParameterSource,
    Site,
)
from siteops.orchestrator import Orchestrator
from siteops.planning import PlanDisposition, PlanStatus


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


class TestConditionEvaluationOnAMissingProperty:
    """How a gate behaves when the site does not carry the property at all.

    These pin current behavior rather than assert what it ought to be. The
    operators are asymmetric: `==` and the truthy form fail closed (the step is
    skipped), while `!=` fails open (the step runs). A gate written as
    `!= 'none'` therefore runs on a site that declares nothing, which is the
    opposite of what the author usually means.

    Changing this would silently alter the meaning of every existing `!=` gate,
    including any in customer manifests, so it is documented here instead. The
    concrete harm in this workspace is closed elsewhere: a catalog family whose
    gate opens for a site with no selection then fails on the unresolved
    declaration path rather than deploying an empty set.
    """

    def _site(self, **properties):
        return Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties=properties,
        )

    def test_equals_fails_closed(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(unrelated="value")

        assert (
            orchestrator._evaluate_condition(
                "{{ site.properties.missing == 'yes' }}", site
            )
            is False
        )

    def test_truthy_fails_closed(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(unrelated="value")

        assert (
            orchestrator._evaluate_condition("{{ site.properties.missing }}", site)
            is False
        )

    def test_not_equals_fails_open(self, complete_workspace):
        """A missing property compares as empty string, which differs from any
        literal, so the step runs."""
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(unrelated="value")

        assert (
            orchestrator._evaluate_condition(
                "{{ site.properties.missing != 'none' }}", site
            )
            is True
        )

    def test_not_equals_on_a_nested_missing_path_fails_open(self, complete_workspace):
        """The catalog's gate shape: a nested selection key the site omits."""
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(resourceSets={})

        assert (
            orchestrator._evaluate_condition(
                "{{ site.properties.resourceSets.dataflows != 'none' }}", site
            )
            is True
        )

    def test_malformed_condition_fails_open(self, complete_workspace):
        """An expression the evaluator cannot parse runs the step.

        Manifest loading rejects a malformed `when:` on both a step and an
        include, so this is the residual behavior for anything that reaches the
        evaluator another way.
        """
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(gate="on")

        assert orchestrator._evaluate_condition("{{ nonsense }}", site) is True

    def test_unknown_field_root_fails_open(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = self._site(gate="on")

        assert (
            orchestrator._evaluate_condition("{{ site.unknown.thing == 'x' }}", site)
            is True
        )


class TestConditionEvaluation:
    """Tests for when condition evaluation."""

    def test_no_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(name="test", subscription="sub", resource_group="rg", location="eastus")

        assert orchestrator._evaluate_condition(None, site) is True
        assert orchestrator._evaluate_condition("", site) is True

    def test_any_condition_runs_when_one_expression_is_truthy(
        self,
        complete_workspace,
    ):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={
                "resourceSets": {
                    "devices": [],
                    "assets": ["bakery-assets"],
                }
            },
        )

        result = orchestrator._evaluate_condition(
            AnyCondition(
                (
                    "{{ site.properties.resourceSets.devices }}",
                    "{{ site.properties.resourceSets.assets }}",
                )
            ),
            site,
        )

        assert result is True

    def test_any_condition_skips_when_every_expression_is_false(
        self,
        complete_workspace,
    ):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"resourceSets": {"devices": [], "assets": []}},
        )

        result = orchestrator._evaluate_condition(
            AnyCondition(
                (
                    "{{ site.properties.resourceSets.devices }}",
                    "{{ site.properties.resourceSets.assets }}",
                )
            ),
            site,
        )

        assert result is False

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


class TestUnresolvedParameterPath:
    """A site-selected parameter file that names nothing fails the step.

    `parameters/<area>/{{ site.properties.X }}.yaml` lets a site choose which
    file to load. Two ways that goes wrong, and both would otherwise deploy the
    step with those parameters absent and report success: the site does not
    carry the property, or it carries a value naming a file that does not exist.
    At fleet scale either is a silent partial deployment.
    """

    def _workspace_with_selected_path(
        self,
        tmp_path,
        site_properties: str,
        parameter_path: str = "parameters/{{ site.properties.setName }}.yaml",
    ):
        workspace = tmp_path / "workspace"
        for sub in ("parameters", "templates", "sites", "manifests"):
            (workspace / sub).mkdir(parents=True)

        (workspace / "sites" / "test-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: test-site\n"
            'subscription: "00000000-0000-0000-0000-000000000000"\n'
            "resourceGroup: rg-test\nlocation: eastus\n" + site_properties
        )
        (workspace / "parameters" / "chosen.yaml").write_text('selected: "yes"\n')
        (workspace / "templates" / "test.json").write_text(
            json.dumps({"parameters": {"selected": {"type": "string"}}})
        )
        (workspace / "manifests" / "test.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: test\n"
            "sites: [test-site]\n"
            f"parameters: [{json.dumps(parameter_path)}]\n"
            "steps:\n  - name: test-step\n    template: templates/test.json\n"
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "test.yaml", workspace_root=workspace
        )
        return orchestrator, manifest, orchestrator.load_site("test-site")

    def test_resolved_path_loads_normally(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: chosen\n"
        )
        result = orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})
        assert result["selected"] == "yes"

    def test_missing_property_raises_rather_than_skipping(self, tmp_path):
        """The site has no `setName`, so the path cannot resolve."""
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  unrelated: value\n"
        )
        with pytest.raises(ParameterSelectionError, match="does not carry the property"):
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

    def test_a_selected_file_that_does_not_exist_raises(self, tmp_path):
        """The site names a set that is not there, which a typo produces.

        This resolves cleanly, so nothing is left unsubstituted to notice. The
        file simply is not there, and skipping it deploys empty.
        """
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: chosn\n"
        )
        with pytest.raises(ParameterSelectionError, match="does not exist"):
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

    def test_the_typo_error_names_the_resolved_file(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: chosn\n"
        )
        with pytest.raises(ParameterSelectionError) as excinfo:
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})
        assert "chosn" in str(excinfo.value)

    def test_a_fixed_path_that_is_missing_still_only_warns(self, tmp_path, caplog):
        """A path with no variable is the manifest author's own input.

        Only site-selected paths fail closed, so an optional fixed file keeps
        the behavior it had.
        """
        workspace = tmp_path / "workspace"
        for sub in ("parameters", "templates", "sites", "manifests"):
            (workspace / sub).mkdir(parents=True)

        (workspace / "sites" / "test-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: test-site\n"
            'subscription: "00000000-0000-0000-0000-000000000000"\n'
            "resourceGroup: rg-test\nlocation: eastus\n"
        )
        (workspace / "templates" / "test.json").write_text(
            json.dumps({"parameters": {"selected": {"type": "string"}}})
        )
        (workspace / "manifests" / "test.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: test\n"
            "sites: [test-site]\n"
            "parameters: [parameters/absent.yaml]\n"
            "steps:\n  - name: test-step\n    template: templates/test.json\n"
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "test.yaml", workspace_root=workspace
        )
        site = orchestrator.load_site("test-site")

        with caplog.at_level(logging.WARNING):
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})
        assert any("not found" in r.message for r in caplog.records)

    def test_error_names_the_site_and_the_path(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  unrelated: value\n"
        )
        with pytest.raises(ParameterSelectionError) as excinfo:
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})
        message = str(excinfo.value)
        assert "test-site" in message
        assert "setName" in message

    def test_dynamic_absolute_path_is_rejected(self, tmp_path):
        outside = tmp_path / "outside.yaml"
        outside.write_text('selected: "outside"\n')
        site_properties = f"properties:\n  setPath: {json.dumps(str(outside))}\n"
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path,
            site_properties,
            "{{ site.properties.setPath }}",
        )

        with pytest.raises(ParameterSelectionError, match="must be relative"):
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

    def test_dynamic_traversal_is_rejected_even_when_target_is_inside(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: nested/../chosen\n"
        )

        with pytest.raises(ParameterSelectionError, match=r"must not contain '\.\.'"):
            orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

    def test_dynamic_nested_subdirectory_is_supported(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: regions/eu/chosen\n"
        )
        selected = orchestrator.workspace / "parameters" / "regions" / "eu" / "chosen.yaml"
        selected.parent.mkdir(parents=True)
        selected.write_text('selected: "nested"\n')

        result = orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

        assert result["selected"] == "nested"

    def test_repeated_dots_inside_a_filename_are_supported(self, tmp_path):
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: release..candidate\n"
        )
        selected = (
            orchestrator.workspace
            / "parameters"
            / "release..candidate.yaml"
        )
        selected.write_text('selected: "dotted"\n')

        result = orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

        assert result["selected"] == "dotted"

    def test_fixed_absolute_path_remains_supported(self, tmp_path):
        outside = tmp_path / "trusted-runtime-parameters.yaml"
        outside.write_text('selected: "yes"\n')
        orchestrator, manifest, site = self._workspace_with_selected_path(
            tmp_path,
            "",
            str(outside),
        )

        result = orchestrator.resolve_parameters(manifest.steps[0], site, manifest, {})

        assert result["selected"] == "yes"

    def test_validation_rejects_dynamic_traversal(self, tmp_path):
        orchestrator, manifest, _ = self._workspace_with_selected_path(
            tmp_path, "properties:\n  setName: nested/../chosen\n"
        )

        errors = orchestrator.validate(orchestrator.workspace / "manifests" / "test.yaml")

        assert any("must not contain '..'" in error for error in errors)

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


class TestManifestParameterSourceExpansion:
    def _setup_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "sites").mkdir()
        (workspace / "manifests").mkdir()
        return workspace

    def _create_site(self, workspace, content):
        (workspace / "sites" / "test-site.yaml").write_text(content)

    def _create_template(self, workspace, params):
        (workspace / "templates" / "test.json").write_text(
            json.dumps({"parameters": params})
        )

    def _workspace(self, tmp_path, selection):
        workspace = tmp_path / "workspace"
        for name in ("manifests", "parameters/dataflows", "sites", "templates"):
            (workspace / name).mkdir(parents=True, exist_ok=True)
        (workspace / "sites" / "test-site.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "siteops/v1",
                    "kind": "Site",
                    "name": "test-site",
                    "subscription": "00000000-0000-0000-0000-000000000000",
                    "resourceGroup": "rg-test",
                    "location": "eastus",
                    "properties": {
                        "resourceSets": {
                            "dataflows": selection,
                        }
                    },
                },
                sort_keys=False,
            )
        )
        (workspace / "templates" / "test.json").write_text(
            json.dumps(
                {
                    "parameters": {
                        "first": {"type": "string"},
                        "second": {"type": "string"},
                    }
                }
            )
        )
        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters:
  - path: "parameters/dataflows/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.dataflows }}"
steps:
  - name: deploy
    template: templates/test.json
"""
        )
        return workspace

    def _resolve(self, workspace):
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "test.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")
        return orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

    def test_expands_ordered_resource_set_files(self, tmp_path):
        workspace = self._workspace(tmp_path, ["shared", "site"])
        (workspace / "parameters" / "dataflows" / "shared.yaml").write_text(
            "first: shared\n"
        )
        (workspace / "parameters" / "dataflows" / "site.yaml").write_text(
            "second: site\n"
        )

        assert self._resolve(workspace) == {
            "first": "shared",
            "second": "site",
        }

    def test_absent_resource_set_key_expands_to_no_files(self, tmp_path):
        workspace = self._workspace(tmp_path, [])
        site_path = workspace / "sites" / "test-site.yaml"
        site = yaml.safe_load(site_path.read_text())
        del site["properties"]["resourceSets"]["dataflows"]
        site_path.write_text(yaml.safe_dump(site, sort_keys=False))

        assert self._resolve(workspace) == {}

    @pytest.mark.parametrize("selection", ["site", "none"])
    def test_legacy_scalar_has_a_migration_error(self, tmp_path, selection):
        workspace = self._workspace(tmp_path, selection)

        with pytest.raises(ParameterSelectionError, match="legacy scalar"):
            self._resolve(workspace)

    def test_scalar_path_reports_the_list_to_for_each_migration(self, tmp_path):
        workspace = self._workspace(tmp_path, ["shared"])
        (workspace / "parameters" / "dataflows" / "shared.yaml").write_text(
            "first: shared\n"
        )
        manifest_path = workspace / "manifests" / "test.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["parameters"] = [
            "parameters/dataflows/"
            "{{ site.properties.resourceSets.dataflows }}.yaml"
        ]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        with pytest.raises(
            ParameterSelectionError,
            match="ordered list.*path.*forEach",
        ):
            self._resolve(workspace)

    def test_null_selection_is_not_treated_as_empty(self, tmp_path):
        workspace = self._workspace(tmp_path, None)

        with pytest.raises(ParameterSelectionError, match="sets .* to null"):
            self._resolve(workspace)

    @pytest.mark.parametrize("resource_sets", [None, [], "site-devices"])
    def test_malformed_resource_sets_container_is_rejected(
        self,
        tmp_path,
        resource_sets,
    ):
        workspace = self._workspace(tmp_path, [])
        site_path = workspace / "sites" / "test-site.yaml"
        site = yaml.safe_load(site_path.read_text())
        site["properties"]["resourceSets"] = resource_sets
        site_path.write_text(yaml.safe_dump(site, sort_keys=False))

        errors = Orchestrator(workspace).validate(
            workspace / "manifests" / "test.yaml"
        )
        assert any("must be a mapping" in error for error in errors)
        with pytest.raises(ParameterSelectionError, match="must be a mapping"):
            self._resolve(workspace)

    def test_duplicate_selection_is_rejected(self, tmp_path):
        workspace = self._workspace(tmp_path, ["site", "site"])

        with pytest.raises(ParameterSelectionError, match="more than once"):
            self._resolve(workspace)

    def test_redacted_validation_suppresses_selected_set_details(
        self,
        tmp_path,
        monkeypatch,
    ):
        workspace = self._workspace(tmp_path, ["private-device-set"])
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        errors = Orchestrator(workspace).validate(
            workspace / "manifests" / "test.yaml"
        )

        assert errors == [
            "Parameter file selection failed. Re-run locally with output "
            "redaction disabled for site and path details."
        ]

    def _composition_workspace(self, tmp_path):
        workspace = tmp_path / "composition-workspace"
        for name in (
            "contracts",
            "manifests",
            "parameters/devices",
            "parameters/assets",
            "sites",
            "templates",
        ):
            (workspace / name).mkdir(parents=True, exist_ok=True)
        (workspace / "contracts" / "catalog.yaml").write_text(
            """
apiVersion: siteops/v1
kind: ParameterComposition
name: test
collections:
  devices:
    path: devices
    identity:
      name: name
    members:
      inboundEndpoints:
        path: properties.endpoints.inbound
        shape: map
  assets:
    path: assets
    identity:
      name: name
references:
  - id: asset-device
    source:
      collection: assets
      select: properties.deviceRef
      bind:
        device: deviceName
        endpoint: endpointName
    target:
      collection: devices
      match:
        name: device
      member:
        name: inboundEndpoints
        match:
          key: endpoint
"""
        )
        (workspace / "templates" / "resources.json").write_text(
            json.dumps(
                {
                    "parameters": {
                        "devices": {"type": "array"},
                        "assets": {"type": "array"},
                    }
                }
            )
        )
        (workspace / "parameters" / "devices" / "shared.yaml").write_text(
            """
devices:
  - name: plant-opc
    properties:
      endpoints:
        inbound:
          opc: {}
"""
        )
        (workspace / "parameters" / "assets" / "bakery.yaml").write_text(
            """
assets:
  - name: oven
    properties:
      deviceRef:
        deviceName: plant-opc
        endpointName: opc
"""
        )
        (workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
properties:
  resourceSets:
    devices: [shared]
    assets: [bakery]
"""
        )
        (workspace / "manifests" / "resources.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: resources
sites: [test-site]
parameterCompositions:
  - contracts/catalog.yaml
parameters:
  - path: "parameters/devices/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.devices }}"
    collections: [devices]
  - path: "parameters/assets/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.assets }}"
    collections: [assets]
steps:
  - name: resources
    template: templates/resources.json
"""
        )
        return workspace

    @staticmethod
    def _add_unverified_endpoint_rule(workspace):
        contract_path = workspace / "contracts" / "catalog.yaml"
        contract = yaml.safe_load(contract_path.read_text())
        contract["references"].append(
            {
                "id": "asset-endpoint-name",
                "source": {
                    "collection": "assets",
                    "select": "properties.deviceRef",
                    "bind": {
                        "endpoint": "endpointName",
                    },
                },
                "unverified": "Recorded for an external inventory.",
            }
        )
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))

    def test_orchestrator_composes_and_validates_resource_references(
        self,
        tmp_path,
    ):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")

        result = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

        assert [entry["name"] for entry in result["devices"]] == ["plant-opc"]
        assert [entry["name"] for entry in result["assets"]] == ["oven"]

    def test_validate_reports_composed_reference_failure(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        asset_path = workspace / "parameters" / "assets" / "bakery.yaml"
        asset_path.write_text(
            asset_path.read_text().replace("deviceName: plant-opc", "deviceName: missing")
        )

        errors = Orchestrator(workspace).validate(
            workspace / "manifests" / "resources.yaml"
        )

        assert any("does not resolve to devices" in error for error in errors)

    def test_redacted_validation_suppresses_composition_details(
        self,
        tmp_path,
        monkeypatch,
    ):
        workspace = self._composition_workspace(tmp_path)
        asset_path = workspace / "parameters" / "assets" / "bakery.yaml"
        asset_path.write_text(
            asset_path.read_text().replace(
                "deviceName: plant-opc",
                "deviceName: private-device",
            )
        )
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        errors = Orchestrator(workspace).validate(
            workspace / "manifests" / "resources.yaml"
        )

        assert errors == [
            "Resource composition failed. Re-run locally with output "
            "redaction disabled for source and identity details."
        ]

    def test_redacted_deploy_error_suppresses_composition_details(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        message = Orchestrator._reportable_deploy_error(
            CompositionError("private-set references private-resource")
        )

        assert "private-set" not in message
        assert message.startswith("Resource composition failed")

    def test_redacted_deploy_error_suppresses_parameter_selection_details(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        message = Orchestrator._reportable_deploy_error(
            ParameterSelectionError("private-site selected private-set")
        )

        assert "private-set" not in message
        assert message.startswith("Parameter file selection failed")

    def test_redacted_deploy_error_uses_typed_value_resolution_summary(
        self,
        monkeypatch,
    ):
        from siteops.planning import PlanValueResolutionError

        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
        error = PlanValueResolutionError(
            detail="private-site resolved private-parameter",
            public_message=(
                "A deferred parameter name is not accepted by the "
                "deployment template."
            ),
        )

        message = Orchestrator._reportable_deploy_error(
            error,
            "private-site",
        )

        assert message == (
            "A deferred parameter name is not accepted by the deployment "
            "template."
        )

    def test_site_parameters_cannot_replace_composed_collection(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        site_path = workspace / "sites" / "test-site.yaml"
        site = yaml.safe_load(site_path.read_text())
        site["parameters"] = {"devices": []}
        site_path.write_text(yaml.safe_dump(site, sort_keys=False))
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )

        errors = orchestrator.validate(
            workspace / "manifests" / "resources.yaml"
        )
        assert any("site.parameters" in error for error in errors)
        with pytest.raises(CompositionError, match="site.parameters"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_site_parameters_cannot_carry_nested_siteops_metadata(
        self,
        tmp_path,
    ):
        workspace = self._composition_workspace(tmp_path)
        site_path = workspace / "sites" / "test-site.yaml"
        site = yaml.safe_load(site_path.read_text())
        site["parameters"] = {
            "wrapper": {"_siteops": {"requires": {}}}
        }
        site_path.write_text(yaml.safe_dump(site, sort_keys=False))
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )

        errors = orchestrator.validate(
            workspace / "manifests" / "resources.yaml"
        )
        assert any("site.parameters" in error for error in errors)
        with pytest.raises(CompositionError, match="site.parameters"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_step_parameters_cannot_carry_nested_siteops_metadata(
        self,
        tmp_path,
    ):
        workspace = self._composition_workspace(tmp_path)
        step_parameters = workspace / "parameters" / "step.yaml"
        step_parameters.write_text(
            "wrapper:\n  _siteops:\n    requires: {}\n"
        )
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        manifest.steps[0].parameters.append("parameters/step.yaml")

        manifest_path = workspace / "manifests" / "resources.yaml"
        raw = yaml.safe_load(manifest_path.read_text())
        raw["steps"][0]["parameters"] = ["parameters/step.yaml"]
        manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        errors = orchestrator.validate(manifest_path)
        assert any("Step parameter file" in error for error in errors)
        with pytest.raises(CompositionError, match="Step parameter file"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_composed_writer_requires_a_selected_consumer_step(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        (workspace / "templates" / "resources.json").write_text(
            json.dumps({"parameters": {"assets": {"type": "array"}}})
        )
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )

        with pytest.raises(CompositionError, match="no selected deployment step"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_missing_fixed_governed_source_fails_deploy_resolution(
        self,
        tmp_path,
    ):
        workspace = self._composition_workspace(tmp_path)
        manifest_path = workspace / "manifests" / "resources.yaml"
        manifest_data = yaml.safe_load(manifest_path.read_text())
        manifest_data["parameters"] = [
            {
                "path": "parameters/devices/missing.yaml",
                "collections": ["devices"],
            }
        ]
        manifest_path.write_text(
            yaml.safe_dump(manifest_data, sort_keys=False)
        )
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace)

        with pytest.raises(CompositionError, match="does not exist"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_reference_provider_step_must_precede_consumer(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        (workspace / "templates" / "devices.json").write_text(
            json.dumps({"parameters": {"devices": {"type": "array"}}})
        )
        (workspace / "templates" / "assets.json").write_text(
            json.dumps({"parameters": {"assets": {"type": "array"}}})
        )
        manifest_path = workspace / "manifests" / "resources.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["steps"] = [
            {"name": "assets", "template": "templates/assets.json"},
            {"name": "devices", "template": "templates/devices.json"},
        ]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        orchestrator = Orchestrator(workspace)
        parsed = Manifest.from_file(manifest_path, workspace_root=workspace)

        with pytest.raises(CompositionError, match="after consumer collection"):
            orchestrator.resolve_parameters(
                parsed.steps[0],
                orchestrator.load_site("test-site"),
                parsed,
                {},
            )

    def test_plan_shows_composed_resources_and_apply_semantics(
        self,
        tmp_path,
        capsys,
    ):
        workspace = self._composition_workspace(tmp_path)

        Orchestrator(workspace).show_plan(
            workspace / "manifests" / "resources.yaml"
        )

        output = capsys.readouterr().out
        assert "Resource composition:" in output
        assert "selected by sites/test-site.yaml" in output
        assert "apply     devices[name='plant-opc']" in output
        assert "apply     assets[name='oven']" in output
        assert "assets[name='oven'] -> devices[name='plant-opc']" in output
        assert "/inboundEndpoints[key='opc']" in output
        assert "does not delete existing resources" in output

    def test_build_plan_is_silent_and_does_not_compile_templates(
        self,
        tmp_path,
        capsys,
    ):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)

        with patch(
            "siteops.orchestrator.get_template_parameters",
            side_effect=AssertionError("describe planning compiled a template"),
        ):
            result = orchestrator.build_plan(
                workspace / "manifests" / "resources.yaml"
            )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert result.status is PlanStatus.PLANNED
        assert result.plan is not None
        assert result.plan.targets[0].composition is not None
        assert {
            operation.disposition
            for operation in result.plan.targets[0].operations
        } == {PlanDisposition.EXECUTE}

    def test_plan_shows_unverified_binding_values(self, tmp_path, capsys):
        workspace = self._composition_workspace(tmp_path)
        self._add_unverified_endpoint_rule(workspace)

        Orchestrator(workspace).show_plan(
            workspace / "manifests" / "resources.yaml"
        )

        output = capsys.readouterr().out
        assert "endpoint='opc'" in output
        assert "recorded, not verified" in output
        assert "Recorded for an external inventory." in output

    def test_redacted_plan_hides_unverified_binding_values(
        self,
        tmp_path,
        capsys,
        monkeypatch,
    ):
        workspace = self._composition_workspace(tmp_path)
        self._add_unverified_endpoint_rule(workspace)
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        Orchestrator(workspace).show_plan(
            workspace / "manifests" / "resources.yaml"
        )

        output = capsys.readouterr().out
        assert "endpoint='opc'" not in output
        assert "Recorded for an external inventory." not in output
        assert "1 recorded reference(s)" in output

    def test_plan_hides_absolute_extra_site_roots(self, tmp_path):
        origin = str(tmp_path / "private" / "sites" / "seattle-dev.yaml")
        assert (
            Orchestrator._reportable_composition_origin(origin)
            == "<extra-sites>/seattle-dev.yaml"
        )

    def test_redacted_plan_suppresses_resource_identities(
        self,
        tmp_path,
        capsys,
        monkeypatch,
    ):
        workspace = self._composition_workspace(tmp_path)
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        Orchestrator(workspace).show_plan(
            workspace / "manifests" / "resources.yaml",
            selector="name=test-site",
        )

        output = capsys.readouterr().out
        assert "1 selected set(s)" not in output
        assert "2 selected source(s)" in output
        assert "plant-opc" not in output
        assert "oven" not in output
        assert "parameters/devices" not in output
        assert "test-site" not in output
        assert "eastus" not in output
        assert "name=test-site" not in output
        assert "applied resource(s)" in output

    def test_composition_cache_tracks_site_selection_changes(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")
        site.properties["resourceSets"] = {"devices": [], "assets": []}

        empty = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )
        site.properties["resourceSets"] = {
            "devices": ["shared"],
            "assets": ["bakery"],
        }
        selected = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

        assert empty["devices"] == []
        assert empty["assets"] == []
        assert [entry["name"] for entry in selected["devices"]] == [
            "plant-opc"
        ]
        assert [entry["name"] for entry in selected["assets"]] == ["oven"]

    def test_composition_cache_tracks_manifest_source_changes(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")

        selected = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )
        manifest.parameters.clear()
        empty = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

        assert [entry["name"] for entry in selected["devices"]] == [
            "plant-opc"
        ]
        assert [entry["name"] for entry in selected["assets"]] == ["oven"]
        assert empty["devices"] == []
        assert empty["assets"] == []

    def test_composition_cache_rechecks_step_parameter_tiers(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")
        orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

        step_path = workspace / "parameters" / "step.yaml"
        step_path.write_text("devices: []\n")
        manifest.steps[0].parameters.append("parameters/step.yaml")

        with pytest.raises(CompositionError, match="Step parameter file"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                site,
                manifest,
                {},
            )

    def test_composition_cache_accepts_heterogeneous_site_mapping_keys(
        self,
        tmp_path,
    ):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        site = orchestrator.load_site("test-site")
        site.properties["freeform"] = {
            1: "numeric",
            "one": "text",
        }

        result = orchestrator.resolve_parameters(
            manifest.steps[0],
            site,
            manifest,
            {},
        )

        assert [entry["name"] for entry in result["devices"]] == ["plant-opc"]
        assert [entry["name"] for entry in result["assets"]] == ["oven"]

    def test_same_resolved_source_cannot_be_selected_twice(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        manifest.parameters.insert(
            0,
            ParameterSource(
                path="parameters/devices/shared.yaml",
                collections=("devices",),
            ),
        )

        with pytest.raises(ParameterSelectionError, match="resolve to .* more than once"):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_composed_collection_has_one_selected_deployment_step(self, tmp_path):
        workspace = self._composition_workspace(tmp_path)
        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(
            workspace / "manifests" / "resources.yaml",
            workspace_root=workspace,
        )
        manifest.steps.append(
            DeploymentStep(
                name="resources-again",
                template="templates/resources.json",
            )
        )

        with pytest.raises(
            CompositionError,
            match="more than one selected deployment step",
        ):
            orchestrator.resolve_parameters(
                manifest.steps[0],
                orchestrator.load_site("test-site"),
                manifest,
                {},
            )

    def test_plan_continues_after_one_site_has_an_invalid_selection(
        self,
        tmp_path,
        capsys,
    ):
        workspace = self._composition_workspace(tmp_path)
        first = yaml.safe_load(
            (workspace / "sites" / "test-site.yaml").read_text()
        )
        first["name"] = "invalid-site"
        first["properties"]["resourceSets"]["assets"] = ["missing"]
        (workspace / "sites" / "invalid-site.yaml").write_text(
            yaml.safe_dump(first, sort_keys=False)
        )
        manifest_path = workspace / "manifests" / "resources.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["sites"] = ["invalid-site", "test-site"]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        Orchestrator(workspace).show_plan(manifest_path)

        output = capsys.readouterr().out
        assert "invalid-site" in output
        assert "does not exist" in output
        assert "apply     devices[name='plant-opc']" in output
        assert "Total: 1 operation(s)" in output

    def test_redacted_plan_aggregates_selection_errors(
        self,
        tmp_path,
        capsys,
        monkeypatch,
    ):
        workspace = self._composition_workspace(tmp_path)
        invalid = yaml.safe_load(
            (workspace / "sites" / "test-site.yaml").read_text()
        )
        invalid["name"] = "invalid-site"
        invalid["properties"]["resourceSets"]["assets"] = ["private-missing"]
        (workspace / "sites" / "invalid-site.yaml").write_text(
            yaml.safe_dump(invalid, sort_keys=False)
        )
        manifest_path = workspace / "manifests" / "resources.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["sites"] = ["invalid-site", "test-site"]
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")

        Orchestrator(workspace).show_plan(manifest_path)

        output = capsys.readouterr().out
        assert "invalid-site" not in output
        assert "test-site" not in output
        assert "private-missing" not in output
        assert "parameters/assets" not in output
        assert "1 site(s): Parameter file selection failed" in output
        assert "2 selected source(s)" in output
        assert "Total: 1 operation(s)" in output

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

class TestMultipleSubscriptionLevelSites:
    """Deploy rejects an ambiguous subscription-level site rather than picking one.

    Subscription-scoped steps run once per subscription and their outputs feed
    every resource-group site under it, so two candidates have no correct
    resolution. `validate` reports this, but `deploy` does not run `validate`,
    so silently taking the first would deploy the rest of the fleet against
    outputs from a site the operator never named.
    """

    def _manifest_with_a_subscription_step(self):
        return Manifest(
            name="test",
            description="",
            sites=[],
            steps=[
                DeploymentStep(
                    name="global",
                    template="templates/global.bicep",
                    scope="subscription",
                ),
                DeploymentStep(name="local", template="templates/local.bicep"),
            ],
        )

    def _sites(self, count: int):
        subscription_level = [
            Site(name=f"global-{i}", subscription="sub-123", resource_group="", location="eastus")
            for i in range(count)
        ]
        return subscription_level + [
            Site(name="rg-site", subscription="sub-123", resource_group="rg-1", location="eastus")
        ]

    def test_two_candidates_raise(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        manifest = self._manifest_with_a_subscription_step()

        with pytest.raises(MultipleSubscriptionSitesError, match="multiple"):
            orchestrator.deploy(
                manifest_path=complete_workspace / "manifests" / "test.yaml",
                manifest=manifest,
                sites=self._sites(2),
            )

    def test_the_error_names_every_candidate(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        manifest = self._manifest_with_a_subscription_step()

        with pytest.raises(MultipleSubscriptionSitesError) as excinfo:
            orchestrator.deploy(
                manifest_path=complete_workspace / "manifests" / "test.yaml",
                manifest=manifest,
                sites=self._sites(2),
            )
        message = str(excinfo.value)
        assert "global-0" in message
        assert "global-1" in message

    def test_one_candidate_is_accepted(self, complete_workspace):
        """The guard rejects ambiguity, not subscription-scoped steps."""
        orchestrator = Orchestrator(complete_workspace)
        manifest = self._manifest_with_a_subscription_step()

        target_result = {
            "site": "x",
            "status": "success",
            "error": None,
            "steps_completed": 2,
            "steps_skipped": 0,
            "steps_total": 2,
            "elapsed": 0.0,
            "steps": [],
        }
        with (
            patch(
                "siteops.orchestrator.get_template_parameters",
                return_value=frozenset(),
            ),
            patch.object(
                orchestrator,
                "_execute_prepared_target",
                return_value=(target_result, {}),
            ),
        ):
            result = orchestrator.deploy(
                manifest_path=complete_workspace / "manifests" / "test.yaml",
                manifest=manifest,
                sites=self._sites(1),
            )

        assert result["summary"]["failed"] == 0


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

"""Unit tests for Site Ops data models.

Tests cover:
- Site loading and validation
- Manifest parsing and step types
- Selector parsing
- Condition pattern matching
- Error handling for invalid inputs
"""

import re
from pathlib import Path

import pytest
import yaml

from siteops.models import (
    CONDITION_PATTERN,
    AnyCondition,
    ArcCluster,
    DeploymentStep,
    KubectlStep,
    Manifest,
    ParallelConfig,
    ParameterSource,
    SelectorParseError,
    Site,
    _validate_resource,
    parse_selector,
)


class TestParseSelector:
    """Tests for the parse_selector function."""

    def test_empty_selector(self):
        assert parse_selector("") == {}

    def test_none_selector(self):
        # Handle None gracefully
        assert parse_selector(None) == {}

    def test_single_label(self):
        result = parse_selector("environment=prod")
        assert result == {"environment": ["prod"]}

    def test_multiple_labels(self):
        result = parse_selector("environment=prod,region=eastus")
        assert result == {"environment": ["prod"], "region": ["eastus"]}

    def test_labels_with_spaces(self):
        result = parse_selector(" environment = prod , region = eastus ")
        assert result == {"environment": ["prod"], "region": ["eastus"]}

    def test_value_with_special_chars(self):
        result = parse_selector("cluster=my-cluster-01")
        assert result == {"cluster": ["my-cluster-01"]}

    def test_value_with_equals_sign(self):
        # Second = should be part of value
        result = parse_selector("tag=key=value")
        assert result == {"tag": ["key=value"]}

    def test_key_without_value_raises(self):
        """A term with no `=` is rejected rather than dropped.

        Dropping it silently widens the selection. A lone bare term parses to
        an empty dict, which matches every site, so `-l seattle-dev` would
        deploy fleet-wide instead of to one site.
        """
        with pytest.raises(SelectorParseError, match="not in `key=value` form"):
            parse_selector("valid=yes,invalid")

    def test_bare_term_suggests_the_name_key(self):
        with pytest.raises(SelectorParseError, match=r"name=seattle-dev"):
            parse_selector("seattle-dev")

    def test_trailing_comma_is_not_a_bare_term(self):
        assert parse_selector("environment=prod,") == {"environment": ["prod"]}

    def test_name_or_combines_duplicate_values(self):
        result = parse_selector("name=a,name=b")
        assert result == {"name": ["a", "b"]}

    def test_name_dedups_repeated_values(self):
        result = parse_selector("name=a,name=b,name=a")
        assert result == {"name": ["a", "b"]}

    def test_non_name_duplicate_key_raises(self):
        with pytest.raises(ValueError, match="may only appear once"):
            parse_selector("env=prod,env=dev")

    def test_non_name_duplicate_key_error_mentions_name_rule(self):
        with pytest.raises(ValueError, match=r"`name=`"):
            parse_selector("region=eastus,region=westus")

    def test_name_and_other_keys_combine(self):
        result = parse_selector("name=a,name=b,env=prod")
        assert result == {"name": ["a", "b"], "env": ["prod"]}

    def test_trailing_comma_ignored(self):
        # Empty parts after comma split are silently skipped
        result = parse_selector("env=prod,")
        assert result == {"env": ["prod"]}

    def test_double_comma_ignored(self):
        result = parse_selector("env=prod,,name=a")
        assert result == {"env": ["prod"], "name": ["a"]}

    def test_empty_key_raises(self):
        """A term like `=foo` has no key. Reject so a typo (e.g. an
        unset shell variable) does not silently match zero sites."""
        from siteops.models import SelectorParseError

        with pytest.raises(SelectorParseError, match="empty key"):
            parse_selector("=foo")

    def test_empty_value_raises(self):
        """A term like `name=` has no value. Reject so an empty
        environment variable expansion (e.g. `-l env=`) is loud."""
        from siteops.models import SelectorParseError

        with pytest.raises(SelectorParseError, match="empty value"):
            parse_selector("name=")


class TestMergeSelectorStrings:
    """Tests for the _merge_selector_strings helper."""

    def test_none(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings(None) is None

    def test_empty_list(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings([]) is None

    def test_single_string(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings(["env=prod"]) == "env=prod"

    def test_multiple_strings(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings(["env=prod", "name=a"]) == "env=prod,name=a"

    def test_empty_strings_filtered(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings(["", "env=prod", ""]) == "env=prod"

    def test_all_empty_returns_none(self):
        from siteops.models import _merge_selector_strings
        assert _merge_selector_strings(["", ""]) is None

    def test_round_trip_with_parse_enforces_name_rule(self):
        """Repeated -l name= values across strings OR-combine via merged parse."""
        from siteops.models import _merge_selector_strings
        merged = _merge_selector_strings(["name=a", "name=b", "name=a"])
        assert parse_selector(merged) == {"name": ["a", "b"]}

    def test_round_trip_with_parse_enforces_non_name_error(self):
        """Repeated non-name keys across strings raise via merged parse."""
        from siteops.models import _merge_selector_strings
        merged = _merge_selector_strings(["env=prod", "env=dev"])
        with pytest.raises(ValueError, match="may only appear once"):
            parse_selector(merged)


class TestNormalizeSiteIdentifier:
    """Tests for the _normalize_site_identifier helper."""

    def test_basename_passthrough(self):
        from siteops.models import _normalize_site_identifier
        assert _normalize_site_identifier("munich-dev") == "munich-dev"

    def test_relative_path_passthrough(self):
        from siteops.models import _normalize_site_identifier
        assert _normalize_site_identifier("regions/eu/munich") == "regions/eu/munich"

    def test_backslash_normalized_to_forward_slash(self):
        from siteops.models import _normalize_site_identifier
        assert (
            _normalize_site_identifier("regions\\eu\\munich")
            == "regions/eu/munich"
        )

    def test_empty_string_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match="must not be empty"):
            _normalize_site_identifier("")

    def test_leading_dot_slash_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match=r"must not start with `\./`"):
            _normalize_site_identifier("./regions/eu/munich")

    def test_leading_slash_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match="must be relative"):
            _normalize_site_identifier("/regions/eu/munich")

    def test_trailing_slash_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match=r"must not end with `/`"):
            _normalize_site_identifier("regions/eu/")

    def test_dotdot_segment_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match=r"must not contain `\.\.`"):
            _normalize_site_identifier("regions/../etc/passwd")

    def test_dot_segment_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match=r"must not contain `\.`"):
            _normalize_site_identifier("regions/./eu/munich")

    def test_double_slash_rejected(self):
        from siteops.models import _normalize_site_identifier
        with pytest.raises(ValueError, match="empty path segments"):
            _normalize_site_identifier("regions//eu/munich")


class TestConditionPattern:
    """Tests for the CONDITION_PATTERN regex."""

    @pytest.mark.parametrize(
        "condition",
        [
            "{{ site.labels.env == 'prod' }}",
            '{{ site.labels.env == "prod" }}',
            "{{ site.labels.env != 'dev' }}",
            "{{site.labels.env=='prod'}}",  # No spaces
            "{{  site.labels.my-label == 'value'  }}",  # Extra spaces
            "{{ site.labels.label_name == 'value' }}",  # Underscore in label
            "{{ site.labels.env == '' }}",  # Empty string comparison
            # New patterns for properties
            "{{ site.properties.enabled == true }}",  # Unquoted boolean
            "{{ site.properties.enabled == false }}",  # Unquoted boolean
            "{{ site.properties.tier == 'standard' }}",  # Quoted string
            "{{ site.properties.nested.path == 'value' }}",  # Nested path
            "{{ site.properties.items[0].name == 'first' }}",  # Array index
            "{{ site.properties.enabled }}",  # Truthy check (no operator)
            "{{ site.properties.deployOptions.includeSolution }}",  # Nested truthy
            "{{ site.properties.endpoints[0].active }}",  # Array truthy
        ],
    )
    def test_valid_conditions(self, condition):
        assert CONDITION_PATTERN.fullmatch(condition.strip()) is not None

    @pytest.mark.parametrize(
        "condition",
        [
            "site.labels.env == 'prod'",  # Missing braces
            "{{ site.env == 'prod' }}",  # Missing labels/properties
            "{{ site.labels.env = 'prod' }}",  # Single equals
            "{{ site.labels.env > 'prod' }}",  # Invalid operator
            "{{ site.name == 'prod' }}",  # Not a label or property
            "{{ site.labels.env == prod }}",  # Unquoted non-boolean value
            "{{ site.properties.enabled == yes }}",  # Unquoted non-boolean
            "{{ site.parameters.value == 'x' }}",  # Parameters not supported in conditions
        ],
    )
    def test_invalid_conditions(self, condition):
        assert CONDITION_PATTERN.fullmatch(condition.strip()) is None

    def test_condition_captures_groups_labels(self):
        """Verify regex captures label name, operator, and value."""
        match = CONDITION_PATTERN.fullmatch("{{ site.labels.myKey == 'myValue' }}")
        assert match is not None
        assert match.group(1) == "labels.myKey"
        assert match.group(2) == "=="
        assert match.group(3) == "myValue"
        assert match.group(4) is None  # No unquoted boolean

    def test_condition_captures_groups_properties(self):
        """Verify regex captures property path, operator, and value."""
        match = CONDITION_PATTERN.fullmatch("{{ site.properties.deployOptions.enabled == true }}")
        assert match is not None
        assert match.group(1) == "properties.deployOptions.enabled"
        assert match.group(2) == "=="
        assert match.group(3) is None  # No quoted string
        assert match.group(4) == "true"  # Unquoted boolean

    def test_condition_captures_groups_truthy(self):
        """Verify regex captures for truthy check (no operator)."""
        match = CONDITION_PATTERN.fullmatch("{{ site.properties.enabled }}")
        assert match is not None
        assert match.group(1) == "properties.enabled"
        assert match.group(2) is None  # No operator
        assert match.group(3) is None  # No quoted value
        assert match.group(4) is None  # No unquoted boolean

    def test_condition_captures_nested_property_truthy(self):
        """Verify regex captures nested property path for truthy check."""
        match = CONDITION_PATTERN.fullmatch("{{ site.properties.deployOptions.includeSolution }}")
        assert match is not None
        assert match.group(1) == "properties.deployOptions.includeSolution"
        assert match.group(2) is None

    def test_condition_captures_array_index(self):
        """Verify regex captures array index notation."""
        match = CONDITION_PATTERN.fullmatch("{{ site.properties.endpoints[0].host == 'localhost' }}")
        assert match is not None
        assert match.group(1) == "properties.endpoints[0].host"
        assert match.group(2) == "=="
        assert match.group(3) == "localhost"

    def test_condition_captures_labels_truthy(self):
        """Verify regex captures for labels truthy check (no operator)."""
        match = CONDITION_PATTERN.fullmatch("{{ site.labels.enabled }}")
        assert match is not None
        assert match.group(1) == "labels.enabled"
        assert match.group(2) is None  # No operator
        assert match.group(3) is None  # No quoted value
        assert match.group(4) is None  # No unquoted boolean


class TestDeploymentStepConditionValidation:
    """Tests for DeploymentStep condition validation with new syntax."""

    def test_valid_truthy_condition(self):
        """Test that truthy condition syntax is accepted."""
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            when="{{ site.properties.enabled }}",
        )
        assert step.when == "{{ site.properties.enabled }}"

    def test_valid_nested_truthy_condition(self):
        """Test that nested truthy condition syntax is accepted."""
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            when="{{ site.properties.deployOptions.includeSolution }}",
        )
        assert step.when == "{{ site.properties.deployOptions.includeSolution }}"

    def test_valid_unquoted_boolean_condition(self):
        """Test that unquoted boolean condition syntax is accepted."""
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            when="{{ site.properties.enabled == true }}",
        )
        assert step.when == "{{ site.properties.enabled == true }}"

    def test_invalid_condition_helpful_error(self):
        """Test that invalid condition shows helpful error message."""
        with pytest.raises(ValueError) as exc_info:
            DeploymentStep(
                name="test",
                template="test.bicep",
                when="invalid condition",
            )
        error_msg = str(exc_info.value)
        assert "truthy check" in error_msg
        assert "site.properties.path" in error_msg

    def test_structured_any_condition_is_normalized(self):
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            when={
                "any": [
                    "{{ site.properties.resourceSets.devices }}",
                    "{{ site.properties.resourceSets.assets }}",
                ]
            },
        )

        assert step.when == AnyCondition(
            (
                "{{ site.properties.resourceSets.devices }}",
                "{{ site.properties.resourceSets.assets }}",
            )
        )

    @pytest.mark.parametrize(
        "when",
        [
            {"all": ["{{ site.properties.enabled }}"]},
            {"any": []},
            {"any": [7]},
        ],
    )
    def test_invalid_structured_condition_is_rejected(self, when):
        with pytest.raises(ValueError, match="structured 'when' condition"):
            DeploymentStep(
                name="test",
                template="test.bicep",
                when=when,
            )


class TestKubectlStepConditionValidation:
    """Tests for KubectlStep condition validation with new syntax."""

    def test_valid_truthy_condition(self):
        """Test that truthy condition syntax is accepted."""
        step = KubectlStep(
            name="test",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config.yaml"],
            when="{{ site.properties.deploySimulator }}",
        )
        assert step.when == "{{ site.properties.deploySimulator }}"

    def test_valid_unquoted_boolean_condition(self):
        """Test that unquoted boolean condition syntax is accepted."""
        step = KubectlStep(
            name="test",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config.yaml"],
            when="{{ site.properties.includeOpcPlcSimulator == true }}",
        )
        assert step.when == "{{ site.properties.includeOpcPlcSimulator == true }}"


class TestValidateResource:
    """Tests for the _validate_resource function."""

    def test_valid_resource_with_defaults(self):
        data = {"name": "test"}
        result = _validate_resource(data, "Site", Path("test.yaml"))
        assert result == "siteops/v1"

    def test_valid_resource_explicit_version(self):
        data = {"apiVersion": "siteops/v1", "kind": "Site"}
        result = _validate_resource(data, "Site", Path("test.yaml"))
        assert result == "siteops/v1"

    def test_invalid_api_version(self):
        data = {"apiVersion": "siteops/v2"}
        with pytest.raises(ValueError, match="Unsupported apiVersion"):
            _validate_resource(data, "Site", Path("test.yaml"))

    def test_mismatched_kind(self):
        data = {"kind": "Manifest"}
        with pytest.raises(ValueError, match="Invalid kind"):
            _validate_resource(data, "Site", Path("test.yaml"))

    def test_kind_not_required(self):
        # Kind is optional - no error if omitted
        data = {"apiVersion": "siteops/v1"}
        result = _validate_resource(data, "Site", Path("test.yaml"))
        assert result == "siteops/v1"


class TestValidateResourceMultipleKinds:
    """Tests for _validate_resource with multiple expected kinds."""

    def test_accepts_single_kind_as_string(self):
        data = {"kind": "Site"}
        result = _validate_resource(data, "Site", Path("test.yaml"))
        assert result == "siteops/v1"

    def test_accepts_kind_from_list(self):
        data = {"kind": "SiteTemplate"}
        result = _validate_resource(data, ["Site", "SiteTemplate"], Path("test.yaml"))
        assert result == "siteops/v1"

    def test_rejects_kind_not_in_list(self):
        data = {"kind": "Manifest"}
        with pytest.raises(ValueError, match="Expected one of.*Site.*SiteTemplate"):
            _validate_resource(data, ["Site", "SiteTemplate"], Path("test.yaml"))

    def test_single_kind_error_message(self):
        data = {"kind": "Manifest"}
        with pytest.raises(ValueError, match="Expected 'Site'"):
            _validate_resource(data, "Site", Path("test.yaml"))


class TestSite:
    """Tests for the Site dataclass."""

    def test_from_file_flat_format(self, tmp_path):
        site_data = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": "my-site",
            "subscription": "sub-123",
            "resourceGroup": "rg-test",
            "location": "eastus",
            "labels": {"env": "dev"},
        }
        site_path = tmp_path / "site.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        site = Site.from_file(site_path)

        assert site.name == "my-site"
        assert site.subscription == "sub-123"
        assert site.resource_group == "rg-test"
        assert site.location == "eastus"
        assert site.labels == {"env": "dev"}

    def test_from_file_k8s_format(self, tmp_path):
        site_data = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "metadata": {
                "name": "my-site",
                "labels": {"env": "prod"},
            },
            "spec": {
                "subscription": "sub-456",
                "resourceGroup": "rg-prod",
                "location": "westus",
            },
        }
        site_path = tmp_path / "site.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        site = Site.from_file(site_path)

        assert site.name == "my-site"
        assert site.subscription == "sub-456"
        assert site.location == "westus"
        assert site.labels == {"env": "prod"}

    def test_from_file_uses_filename_as_default_name(self, tmp_path):
        site_data = {
            "subscription": "sub-123",
            "location": "eastus",
        }
        site_path = tmp_path / "inferred-name.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        site = Site.from_file(site_path)
        assert site.name == "inferred-name"

    def test_from_file_missing_required_field(self, tmp_path):
        site_data = {"name": "incomplete", "location": "eastus"}  # Missing subscription
        site_path = tmp_path / "site.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        with pytest.raises(ValueError, match="Missing required field 'subscription'"):
            Site.from_file(site_path)

    def test_from_file_empty_file(self, tmp_path):
        site_path = tmp_path / "empty.yaml"
        site_path.write_text("")

        with pytest.raises(ValueError, match="Empty or invalid"):
            Site.from_file(site_path)

    def test_from_file_with_parameters(self, tmp_path):
        site_data = {
            "name": "param-site",
            "subscription": "sub-123",
            "location": "eastus",
            "parameters": {
                "commonTag": "shared-value",
                "nested": {"key": "value"},
            },
        }
        site_path = tmp_path / "site.yaml"
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        site = Site.from_file(site_path)
        assert site.parameters["commonTag"] == "shared-value"
        assert site.parameters["nested"]["key"] == "value"

    def test_matches_selector_empty(self):
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )
        assert site.matches_selector({}) is True

    def test_matches_selector_match(self):
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev", "region": "eastus"},
        )
        assert site.matches_selector({"env": ["dev"]}) is True
        assert site.matches_selector({"env": ["dev"], "region": ["eastus"]}) is True

    def test_matches_selector_no_match(self):
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )
        assert site.matches_selector({"env": ["prod"]}) is False
        assert site.matches_selector({"nonexistent": ["value"]}) is False

    def test_matches_selector_partial_match_fails(self):
        """All selector labels must match."""
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )
        # Matches env but not region
        assert site.matches_selector({"env": ["dev"], "region": ["westus"]}) is False

    def test_get_all_parameters_returns_copy(self):
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            parameters={"key": "value"},
        )
        params = site.get_all_parameters()
        params["new_key"] = "new_value"

        # Original should be unchanged
        assert "new_key" not in site.parameters

    def test_repr(self):
        site = Site(
            name="test-site",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        repr_str = repr(site)
        assert "test-site" in repr_str
        assert "eastus" in repr_str

    def test_is_subscription_level_with_resource_group(self):
        """Site with resource_group is NOT subscription-level."""
        site = Site(
            name="test",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
        )
        assert site.is_subscription_level is False

    def test_is_subscription_level_without_resource_group(self):
        """Site without resource_group IS subscription-level."""
        site = Site(
            name="test",
            subscription="sub-123",
            resource_group="",  # Empty string
            location="eastus",
        )
        assert site.is_subscription_level is True

    def test_is_subscription_level_none_resource_group(self):
        """Site with None resource_group IS subscription-level."""
        site = Site(
            name="test",
            subscription="sub-123",
            resource_group=None,  # None
            location="eastus",
        )
        assert site.is_subscription_level is True


class TestDeploymentStep:
    """Tests for the DeploymentStep dataclass."""

    def test_valid_step(self):
        step = DeploymentStep(
            name="deploy-infra",
            template="templates/main.bicep",
            parameters=["params/main.yaml"],
            scope="resourceGroup",
        )
        assert step.name == "deploy-infra"
        assert step.scope == "resourceGroup"

    def test_default_scope(self):
        step = DeploymentStep(name="test", template="test.bicep")
        assert step.scope == "resourceGroup"

    def test_default_parameters_empty_list(self):
        step = DeploymentStep(name="test", template="test.bicep")
        assert step.parameters == []

    def test_subscription_scope(self):
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            scope="subscription",
        )
        assert step.scope == "subscription"

    def test_invalid_scope(self):
        with pytest.raises(ValueError, match="Invalid scope"):
            DeploymentStep(name="test", template="test.bicep", scope="invalid")

    def test_valid_when_condition(self):
        step = DeploymentStep(
            name="test",
            template="test.bicep",
            when="{{ site.labels.env == 'prod' }}",
        )
        assert step.when == "{{ site.labels.env == 'prod' }}"

    def test_invalid_when_condition(self):
        with pytest.raises(ValueError, match="Invalid 'when' condition"):
            DeploymentStep(
                name="test",
                template="test.bicep",
                when="invalid condition",
            )

    def test_when_none_is_valid(self):
        step = DeploymentStep(name="test", template="test.bicep", when=None)
        assert step.when is None


class TestKubectlStep:
    """Tests for the KubectlStep dataclass."""

    def test_valid_apply_step(self):
        step = KubectlStep(
            name="apply-config",
            operation="apply",
            arc=ArcCluster(name="my-cluster", resource_group="rg"),
            files=["config.yaml"],
        )
        assert step.operation == "apply"
        assert step.arc.name == "my-cluster"

    def test_invalid_operation(self):
        with pytest.raises(ValueError, match="Invalid kubectl operation"):
            KubectlStep(
                name="test",
                operation="delete",  # Not supported yet
                arc=ArcCluster(name="cluster", resource_group="rg"),
                files=["config.yaml"],
            )

    def test_empty_files(self):
        with pytest.raises(ValueError, match="must specify at least one file"):
            KubectlStep(
                name="test",
                operation="apply",
                arc=ArcCluster(name="cluster", resource_group="rg"),
                files=[],
            )

    def test_multiple_files(self):
        step = KubectlStep(
            name="test",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config1.yaml", "config2.yaml", "https://example.com/config.yaml"],
        )
        assert len(step.files) == 3

    def test_valid_when_condition(self):
        step = KubectlStep(
            name="test",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config.yaml"],
            when="{{ site.labels.k8s == 'true' }}",
        )
        assert step.when is not None

    def test_invalid_when_condition(self):
        with pytest.raises(ValueError, match="Invalid 'when' condition"):
            KubectlStep(
                name="test",
                operation="apply",
                arc=ArcCluster(name="cluster", resource_group="rg"),
                files=["config.yaml"],
                when="bad condition",
            )


class TestArcCluster:
    """Tests for the ArcCluster dataclass."""

    def test_basic_creation(self):
        arc = ArcCluster(name="my-cluster", resource_group="my-rg")
        assert arc.name == "my-cluster"
        assert arc.resource_group == "my-rg"

    def test_template_variables_allowed(self):
        """Arc cluster fields can contain template variables."""
        arc = ArcCluster(
            name="{{ site.labels.clusterName }}",
            resource_group="{{ site.resourceGroup }}",
        )
        assert "{{" in arc.name
        assert "{{" in arc.resource_group


class TestManifest:
    """Tests for the Manifest dataclass."""

    def test_from_file_basic(self, tmp_path):
        manifest_data = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "test-manifest",
            "description": "Test description",
            "sites": ["site-a", "site-b"],
            "steps": [
                {
                    "name": "step-1",
                    "template": "templates/main.bicep",
                    "scope": "resourceGroup",
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        assert manifest.name == "test-manifest"
        assert manifest.description == "Test description"
        assert manifest.sites == ["site-a", "site-b"]
        assert len(manifest.steps) == 1
        assert isinstance(manifest.steps[0], DeploymentStep)

    def test_from_file_with_kubectl_step(self, tmp_path):
        manifest_data = {
            "name": "kubectl-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "apply-config",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {
                        "name": "{{ site.labels.cluster }}",
                        "resourceGroup": "{{ site.resourceGroup }}",
                    },
                    "files": ["https://example.com/config.yaml"],
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        assert len(manifest.steps) == 1
        assert isinstance(manifest.steps[0], KubectlStep)
        assert manifest.steps[0].operation == "apply"

    def test_from_file_mixed_steps(self, tmp_path):
        """Manifest can have both deployment and kubectl steps."""
        manifest_data = {
            "name": "mixed-manifest",
            "sites": ["site-a"],
            "steps": [
                {"name": "bicep-step", "template": "main.bicep"},
                {
                    "name": "kubectl-step",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"name": "cluster", "resourceGroup": "rg"},
                    "files": ["config.yaml"],
                },
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        assert len(manifest.steps) == 2
        assert isinstance(manifest.steps[0], DeploymentStep)
        assert isinstance(manifest.steps[1], KubectlStep)

    def test_from_file_with_site_selector(self, tmp_path):
        manifest_data = {
            "name": "selector-manifest",
            "siteSelector": "environment=prod",
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.site_selector == "environment=prod"
        assert manifest.sites == []

    def test_from_file_with_nested_path_in_sites(self, tmp_path):
        """Path-form site identifiers in `sites:` are normalized."""
        manifest_data = {
            "name": "nested-manifest",
            "sites": ["regions/eu/munich", "flat-site"],
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.sites == ["regions/eu/munich", "flat-site"]

    def test_from_file_normalizes_backslash_in_sites(self, tmp_path):
        """Backslash paths in `sites:` are normalized to forward slashes."""
        manifest_data = {
            "name": "backslash-manifest",
            "sites": ["regions\\eu\\munich"],
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.sites == ["regions/eu/munich"]

    def test_from_file_rejects_dotdot_in_sites(self, tmp_path):
        """Path traversal in `sites:` raises a clear parse error."""
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["../escape"],
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="Invalid site identifier"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_parallel_mode(self, tmp_path):
        manifest_data = {
            "name": "parallel-manifest",
            "sites": ["site-a", "site-b"],
            "parallel": True,
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.parallel.is_unlimited is True

    def test_from_file_parallel_defaults_false(self, tmp_path):
        manifest_data = {
            "name": "default-manifest",
            "sites": ["site-a"],
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.parallel.is_sequential is True

    def test_from_file_uses_filename_as_default_name(self, tmp_path):
        manifest_data = {
            "sites": ["site-a"],
            "steps": [{"name": "step-1", "template": "test.bicep"}],
        }
        manifest_path = tmp_path / "my-deployment.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert manifest.name == "my-deployment"

    def test_from_file_missing_step_name(self, tmp_path):
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [{"template": "test.bicep"}],  # Missing name
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="missing required field 'name'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_deployment_missing_template(self, tmp_path):
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [{"name": "step-1"}],  # Missing template
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="missing 'template'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_kubectl_missing_arc(self, tmp_path):
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "bad-kubectl",
                    "type": "kubectl",
                    "operation": "apply",
                    "files": ["config.yaml"],
                    # Missing arc
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="missing 'arc' configuration"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_kubectl_missing_files(self, tmp_path):
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "bad-kubectl",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"name": "cluster", "resourceGroup": "rg"},
                    # Missing files
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="missing 'files'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_kubectl_missing_operation(self, tmp_path):
        """Kubectl step without operation field should raise ValueError."""
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "bad-kubectl",
                    "type": "kubectl",
                    # Missing operation
                    "arc": {"name": "cluster", "resourceGroup": "rg"},
                    "files": ["config.yaml"],
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="missing 'operation'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_kubectl_arc_missing_name(self, tmp_path):
        """Kubectl step with arc config missing name should raise ValueError."""
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "bad-kubectl",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"resourceGroup": "rg"},  # Missing name
                    "files": ["config.yaml"],
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="must have 'name' and 'resourceGroup'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_kubectl_arc_missing_resource_group(self, tmp_path):
        """Kubectl step with arc config missing resourceGroup should raise ValueError."""
        manifest_data = {
            "name": "bad-manifest",
            "sites": ["site-a"],
            "steps": [
                {
                    "name": "bad-kubectl",
                    "type": "kubectl",
                    "operation": "apply",
                    "arc": {"name": "cluster"},  # Missing resourceGroup
                    "files": ["config.yaml"],
                }
            ],
        }
        manifest_path = tmp_path / "manifest.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        with pytest.raises(ValueError, match="must have 'name' and 'resourceGroup'"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_empty_file(self, tmp_path):
        manifest_path = tmp_path / "empty.yaml"
        manifest_path.write_text("")

        with pytest.raises(ValueError, match="Empty or invalid"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_unknown_top_level_key_with_did_you_mean(self, tmp_path):
        """A typo close to a known field should error with a `did you mean` hint."""
        manifest_path = tmp_path / "typo.yaml"
        manifest_path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Manifest\n"
            "name: typo\n"
            "site:\n"           # singular: typo for `sites:`
            "  - munich-dev\n"
            "steps:\n"
            "  - name: x\n"
            "    template: t.bicep\n"
        )
        with pytest.raises(ValueError) as exc:
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        msg = str(exc.value)
        assert "unknown top-level key" in msg
        assert "`site`" in msg
        assert "did you mean `sites`" in msg

    def test_from_file_unknown_top_level_key_no_suggestion(self, tmp_path):
        """A key with no close match should error without a suggestion."""
        manifest_path = tmp_path / "novel.yaml"
        manifest_path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Manifest\n"
            "name: novel\n"
            "completely_unrelated_field: 42\n"
            "selector: env=dev\n"
            "steps:\n"
            "  - name: x\n"
            "    template: t.bicep\n"
        )
        with pytest.raises(ValueError) as exc:
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        msg = str(exc.value)
        assert "`completely_unrelated_field`" in msg
        # No suggestion since no known key is close to this string.
        assert "did you mean" not in msg

    def test_from_file_selector_typo_caught(self, tmp_path):
        """`selctor:` (missing 'e') should suggest `selector`."""
        manifest_path = tmp_path / "selctor.yaml"
        manifest_path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Manifest\n"
            "name: typo\n"
            "selctor: env=dev\n"
            "steps:\n"
            "  - name: x\n"
            "    template: t.bicep\n"
        )
        with pytest.raises(ValueError) as exc:
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)
        assert "did you mean `selector`" in str(exc.value)

    def test_from_file_unknown_metadata_key_in_nested_shape(self, tmp_path):
        """K8s-style nested envelope: unknown metadata key is rejected too."""
        manifest_path = tmp_path / "nested.yaml"
        manifest_path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Manifest\n"
            "metadata:\n"
            "  name: nested\n"
            "  annotations: {foo: bar}\n"   # unknown metadata key
            "spec:\n"
            "  selector: env=dev\n"
            "  steps:\n"
            "    - name: x\n"
            "      template: t.bicep\n"
        )
        with pytest.raises(ValueError, match="unknown metadata key"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_from_file_unknown_spec_key_in_nested_shape(self, tmp_path):
        """K8s-style nested envelope: unknown spec key is rejected."""
        manifest_path = tmp_path / "nested.yaml"
        manifest_path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Manifest\n"
            "metadata:\n"
            "  name: nested\n"
            "spec:\n"
            "  selector: env=dev\n"
            "  steps:\n"
            "    - name: x\n"
            "      template: t.bicep\n"
            "  bogus_spec_field: 42\n"
        )
        with pytest.raises(ValueError, match="unknown spec key"):
            Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

    def test_resolve_parameter_path_simple(self):
        manifest = Manifest(
            name="test",
            description="",
            sites=[],
            steps=[],
        )
        site = Site(
            name="dev-eastus",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            labels={"env": "dev"},
        )

        result = manifest.resolve_parameter_path("params/common.yaml", site)
        assert result == "params/common.yaml"

    def test_resolve_parameter_path_with_templates(self):
        manifest = Manifest(
            name="test",
            description="",
            sites=[],
            steps=[],
        )
        site = Site(
            name="dev-eastus",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            labels={"env": "dev"},
        )

        result = manifest.resolve_parameter_path(
            "params/{{ site.name }}/{{ site.labels.env }}.yaml",
            site,
        )
        assert result == "params/dev-eastus/dev.yaml"

    def test_resolve_parameter_path_all_variables(self):
        manifest = Manifest(name="test", description="", sites=[], steps=[])
        site = Site(
            name="prod-westus",
            subscription="sub-456",
            resource_group="rg-prod",
            location="westus",
            labels={},
        )

        path = "{{ site.location }}/{{ site.resourceGroup }}/{{ site.subscription }}.yaml"
        result = manifest.resolve_parameter_path(path, site)
        assert result == "westus/rg-prod/sub-456.yaml"

    def test_resolve_parameter_path_with_properties(self):
        """Test {{ site.properties.<path> }} resolution in parameter file paths."""
        manifest = Manifest(name="test", description="", sites=[], steps=[])
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            properties={"aioRelease": "2603"},
        )

        result = manifest.resolve_parameter_path(
            "parameters/aio-releases/{{ site.properties.aioRelease }}.yaml",
            site,
        )
        assert result == "parameters/aio-releases/2603.yaml"

    def test_resolve_parameter_path_with_nested_properties(self):
        """Test nested property path resolution."""
        manifest = Manifest(name="test", description="", sites=[], steps=[])
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            properties={"config": {"variant": "standard"}},
        )

        result = manifest.resolve_parameter_path(
            "parameters/{{ site.properties.config.variant }}/defaults.yaml",
            site,
        )
        assert result == "parameters/standard/defaults.yaml"

    def test_resolve_parameter_path_with_missing_property(self):
        """Unresolvable property path should leave template as-is."""
        manifest = Manifest(name="test", description="", sites=[], steps=[])
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            properties={},
        )

        path = "parameters/{{ site.properties.nonexistent }}/defaults.yaml"
        result = manifest.resolve_parameter_path(path, site)
        assert result == path

    def test_resolve_parameter_path_mixed_templates(self):
        """Test mixing site.properties with other template variables."""
        manifest = Manifest(name="test", description="", sites=[], steps=[])
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-dev",
            location="eastus",
            labels={"environment": "dev"},
            properties={"aioRelease": "2603"},
        )

        result = manifest.resolve_parameter_path(
            "parameters/{{ site.labels.environment }}/{{ site.properties.aioRelease }}.yaml",
            site,
        )
        assert result == "parameters/dev/2603.yaml"


class TestSiteProperties:
    """Tests for Site properties field."""

    def test_site_with_properties(self, tmp_path):
        site_file = tmp_path / "site-with-props.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: dev-eastus
subscription: "sub-123"
location: eastus
resourceGroup: "rg-dev"
properties:
  mqtt:
    broker: mqtt://10.0.1.50:1883
    topic: devices/telemetry
  deviceEndpoints:
    - name: opc-server-1
      host: 10.0.1.100
      port: 4840
    - name: opc-server-2
      host: 10.0.1.101
      port: 4840
  maxRetries: 3
""",
            encoding="utf-8",
        )

        site = Site.from_file(site_file)

        assert site.properties["mqtt"]["broker"] == "mqtt://10.0.1.50:1883"
        assert site.properties["deviceEndpoints"][0]["host"] == "10.0.1.100"
        assert site.properties["maxRetries"] == 3

    def test_site_without_properties(self, tmp_path):
        site_file = tmp_path / "site-no-props.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: dev-eastus
subscription: "sub-123"
location: eastus
""",
            encoding="utf-8",
        )

        site = Site.from_file(site_file)

        assert site.properties == {}

    def test_site_properties_in_spec_format(self, tmp_path):
        site_file = tmp_path / "site-spec.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
metadata:
  name: dev-eastus
spec:
  subscription: "sub-123"
  location: eastus
  resourceGroup: "rg-dev"
  properties:
    endpoint: https://api.example.com
""",
            encoding="utf-8",
        )

        site = Site.from_file(site_file)

        assert site.properties["endpoint"] == "https://api.example.com"


class TestManifestParameters:
    """Tests for manifest-level parameters field."""

    def test_manifest_with_parameters_field(self, tmp_path):
        """Test that manifest.parameters field is parsed correctly."""
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test-manifest
description: Test manifest with parameters

sites:
  - site-a

parameters:
  - parameters/common.yaml
  - parameters/shared.yaml

steps:
  - name: deploy
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)

        assert manifest.parameters == ["parameters/common.yaml", "parameters/shared.yaml"]

    def test_manifest_parameter_source_object_is_typed(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test-manifest
sites: [site-a]
parameters:
  - path: "parameters/devices/{{ item }}.yaml"
    forEach: "{{ site.properties.resourceSets.devices }}"
    collections: [devices]
steps:
  - name: deploy
    template: templates/test.bicep
"""
        )

        manifest = Manifest.from_file(
            manifest_file,
            workspace_root=manifest_file.parent,
        )

        assert manifest.parameters == [
            ParameterSource(
                path="parameters/devices/{{ item }}.yaml",
                for_each="{{ site.properties.resourceSets.devices }}",
                collections=("devices",),
                declared_in=manifest_file,
            )
        ]

    @pytest.mark.parametrize(
        ("source", "message"),
        [
            (
                {"path": "parameters/{{ item }}.yaml"},
                "declares no `forEach`",
            ),
            (
                {
                    "path": "parameters/static.yaml",
                    "forEach": "{{ site.properties.resourceSets.devices }}",
                },
                "contains no `{{ item }}`",
            ),
            (
                {
                    "path": "parameters/{{ item }}.yaml",
                    "forEach": "{{ site.properties.resourceSets.devices }}",
                    "collections": ["devices", "devices"],
                },
                "duplicate name 'devices'",
            ),
            (
                {
                    "path": (
                        "parameters/devices/"
                        "{{ site.properties.resourceSets.devices }}.yaml"
                    ),
                    "collections": ["devices"],
                },
                "dynamic path without `forEach`",
            ),
        ],
    )
    def test_invalid_manifest_parameter_source_is_rejected(
        self,
        tmp_path,
        source,
        message,
    ):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "siteops/v1",
                    "kind": "Manifest",
                    "name": "test-manifest",
                    "sites": ["site-a"],
                    "parameters": [source],
                    "steps": [
                        {
                            "name": "deploy",
                            "template": "templates/test.bicep",
                        }
                    ],
                },
                sort_keys=False,
            )
        )

        with pytest.raises(ValueError, match=re.escape(message)):
            Manifest.from_file(
                manifest_file,
                workspace_root=manifest_file.parent,
            )

    def test_manifest_without_parameters_field(self, tmp_path):
        """Test that missing parameters field defaults to empty list."""
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test-manifest
description: Test manifest without parameters

sites:
  - site-a

steps:
  - name: deploy
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)

        assert manifest.parameters == []

    def test_manifest_with_empty_parameters_list(self, tmp_path):
        """Test that empty parameters list is handled correctly."""
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test-manifest
description: Test manifest with empty parameters

sites:
  - site-a

parameters: []

steps:
  - name: deploy
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)

        assert manifest.parameters == []


class TestParallelConfig:
    """Tests for the ParallelConfig dataclass."""

    def test_default_is_sequential(self):
        config = ParallelConfig()
        assert config.sites == 1
        assert config.is_sequential is True
        assert config.is_unlimited is False

    def test_explicit_sequential(self):
        config = ParallelConfig(sites=1)
        assert config.is_sequential is True
        assert config.max_workers == 1

    def test_unlimited(self):
        config = ParallelConfig(sites=0)
        assert config.is_unlimited is True
        assert config.is_sequential is False
        assert config.max_workers is None

    def test_limited_concurrency(self):
        config = ParallelConfig(sites=3)
        assert config.is_sequential is False
        assert config.is_unlimited is False
        assert config.max_workers == 3

    def test_negative_sites_raises_error(self):
        with pytest.raises(ValueError, match="must be >= 0"):
            ParallelConfig(sites=-1)

    def test_str_unlimited(self):
        config = ParallelConfig(sites=0)
        assert str(config) == "unlimited"

    def test_str_sequential(self):
        config = ParallelConfig(sites=1)
        assert str(config) == "sequential"

    def test_str_limited(self):
        config = ParallelConfig(sites=5)
        assert str(config) == "max 5"


class TestParallelConfigFromValue:
    """Tests for ParallelConfig.from_value() factory method."""

    def test_from_none(self):
        config = ParallelConfig.from_value(None)
        assert config.sites == 1
        assert config.is_sequential is True

    def test_from_true(self):
        config = ParallelConfig.from_value(True)
        assert config.sites == 0
        assert config.is_unlimited is True

    def test_from_false(self):
        config = ParallelConfig.from_value(False)
        assert config.sites == 1
        assert config.is_sequential is True

    def test_from_int_zero(self):
        config = ParallelConfig.from_value(0)
        assert config.sites == 0
        assert config.is_unlimited is True

    def test_from_int_positive(self):
        config = ParallelConfig.from_value(3)
        assert config.sites == 3
        assert config.max_workers == 3

    def test_from_dict_with_sites(self):
        config = ParallelConfig.from_value({"sites": 5})
        assert config.sites == 5

    def test_from_dict_default_sites(self):
        config = ParallelConfig.from_value({})
        assert config.sites == 1

    def test_from_dict_invalid_sites_type(self):
        with pytest.raises(ValueError, match="must be an integer"):
            ParallelConfig.from_value({"sites": "three"})

    def test_from_invalid_type(self):
        with pytest.raises(ValueError, match="Invalid parallel value"):
            ParallelConfig.from_value("invalid")

    def test_from_list_invalid(self):
        with pytest.raises(ValueError, match="Invalid parallel value"):
            ParallelConfig.from_value([1, 2, 3])


class TestManifestParallelConfig:
    """Tests for parallel config in Manifest parsing."""

    def test_manifest_parallel_int(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
parallel: 3
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.sites == 3
        assert manifest.parallel.max_workers == 3

    def test_manifest_parallel_true(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
parallel: true
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.is_unlimited is True

    def test_manifest_parallel_false(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
parallel: false
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.is_sequential is True

    def test_manifest_parallel_object(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
parallel:
  sites: 2
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.sites == 2

    def test_manifest_parallel_zero_unlimited(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
parallel: 0
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.is_unlimited is True

    def test_manifest_parallel_default_sequential(self, tmp_path):
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [site-a]
steps:
  - name: step1
    template: test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_file, workspace_root=manifest_file.parent)
        assert manifest.parallel.is_sequential is True


class TestSiteSelector:
    """Tests for Site.matches_selector method."""

    def test_matches_selector_by_label(self):
        """Test matching by label."""
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={"environment": "dev", "region": "us"},
        )

        assert site.matches_selector({"environment": ["dev"]}) is True
        assert site.matches_selector({"environment": ["prod"]}) is False
        assert site.matches_selector({"environment": ["dev"], "region": ["us"]}) is True
        assert site.matches_selector({"environment": ["dev"], "region": ["eu"]}) is False

    def test_matches_selector_by_name(self):
        """Test matching by site name."""
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={"environment": "dev"},
        )

        assert site.matches_selector({"name": ["munich-dev"]}) is True
        assert site.matches_selector({"name": ["seattle-dev"]}) is False

    def test_matches_selector_by_name_or_combines(self):
        """`name` accepts multiple values OR-combined."""
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={},
        )

        assert site.matches_selector({"name": ["munich-dev", "seattle-dev"]}) is True
        assert site.matches_selector({"name": ["seattle-dev", "berlin-dev"]}) is False

    def test_matches_selector_name_and_label(self):
        """Test matching by both name and label."""
        site = Site(
            name="munich-dev",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={"environment": "dev"},
        )

        assert site.matches_selector({"name": ["munich-dev"], "environment": ["dev"]}) is True
        assert site.matches_selector({"name": ["munich-dev"], "environment": ["prod"]}) is False
        assert site.matches_selector({"name": ["seattle-dev"], "environment": ["dev"]}) is False

    def test_matches_selector_empty(self):
        """Test that empty selector matches all sites."""
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={"environment": "dev"},
        )

        assert site.matches_selector({}) is True

    def test_matches_selector_missing_label(self):
        """Test that missing label doesn't match."""
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels={},
        )

        assert site.matches_selector({"environment": ["dev"]}) is False


class TestKeyWrittenWithNoValue:
    """A key present with nothing after the colon parses as `None`.

    `dict.get(key, default)` returns that `None`, because the default applies
    only when the key is absent. Every field below declares a default factory,
    which asserts the field is never `None`, so the declaration is what these
    hold the models to.
    """

    def _site_file(self, tmp_path, body: str) -> Path:
        path = tmp_path / "empty-keys.yaml"
        path.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Site\n"
            "name: empty-keys\n"
            "subscription: sub-123\n"
            "location: eastus\n"
            "resourceGroup: rg-test\n" + body,
            encoding="utf-8",
        )
        return path

    def test_site_empty_mappings_load_as_empty(self, tmp_path):
        """`labels:`, `properties:` and `parameters:` with no value."""
        site = Site.from_file(self._site_file(tmp_path, "labels:\nproperties:\nparameters:\n"))

        assert site.labels == {}
        assert site.properties == {}
        assert site.parameters == {}

    def test_site_with_empty_labels_still_selects(self, tmp_path):
        """Without the fix this raises `AttributeError` inside the selector.

        A single site written this way took down selection for the whole
        fleet, since `matches_selector` runs against every candidate.
        """
        site = Site.from_file(self._site_file(tmp_path, "labels:\n"))

        assert site.matches_selector({"environment": ["dev"]}) is False

    def test_site_empty_resource_group_is_subscription_level(self, tmp_path):
        """`resourceGroup:` with no value means the site is subscription-level.

        The declared type is `str`, so a `None` here reaches every reader that
        formats or compares it as one.
        """
        path = tmp_path / "sub-level.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: sub-level\n"
            "subscription: sub-123\nlocation: eastus\nresourceGroup:\n",
            encoding="utf-8",
        )

        site = Site.from_file(path)

        assert site.resource_group == ""
        assert site.is_subscription_level is True

    def test_direct_construction_is_held_to_the_declaration(self):
        """The guarantee cannot depend on having come through a loader."""
        site = Site(
            name="direct",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            labels=None,
            properties=None,
            parameters=None,
        )

        assert site.labels == {}
        assert site.properties == {}
        assert site.parameters == {}
        assert site.matches_selector({"environment": ["dev"]}) is False

    def test_manifest_empty_sites_list(self, tmp_path):
        """Without the fix this raises `TypeError` while iterating `None`."""
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\nsites:\n"
            "steps:\n  - name: s1\n    template: t.bicep\n",
            encoding="utf-8",
        )

        manifest = Manifest.from_file(path, workspace_root=tmp_path)

        assert manifest.sites == []

    def test_manifest_empty_parameters_list(self, tmp_path):
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\nparameters:\n"
            "steps:\n  - name: s1\n    template: t.bicep\n",
            encoding="utf-8",
        )

        manifest = Manifest.from_file(path, workspace_root=tmp_path)

        assert manifest.parameters == []

    def test_step_empty_parameters_list(self, tmp_path):
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\n"
            "steps:\n  - name: s1\n    template: t.bicep\n    parameters:\n",
            encoding="utf-8",
        )

        manifest = Manifest.from_file(path, workspace_root=tmp_path)

        assert manifest.steps[0].parameters == []

    def test_wrong_type_names_the_file_and_the_key(self, tmp_path):
        """A wrong type is still rejected, and says where to look.

        Normalizing a null must not soften this into acceptance.
        """
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\nsites: not-a-list\n"
            "steps:\n  - name: s1\n    template: t.bicep\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"'sites' in .* must be a list, got str"):
            Manifest.from_file(path, workspace_root=tmp_path)

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                "metadata: just-a-string\nspec:\n  subscription: s\n  location: eastus\n",
                r"'metadata' in site .* must be a mapping, got str",
            ),
            (
                "metadata:\n  name: n\nspec: just-a-string\n",
                r"'spec' in site .* must be a mapping, got str",
            ),
        ],
        ids=["metadata", "spec"],
    )
    def test_site_envelope_must_be_a_mapping(self, tmp_path, body, expected):
        """A wrong-typed envelope reached `'str' object has no attribute 'get'`."""
        path = tmp_path / "s.yaml"
        path.write_text(f"apiVersion: siteops/v1\nkind: Site\n{body}", encoding="utf-8")

        with pytest.raises(ValueError, match=expected):
            Site.from_file(path)

    def test_manifest_envelope_must_be_a_mapping(self, tmp_path):
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nmetadata:\n  name: m\nspec: just-a-string\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"'spec' in manifest .* must be a mapping, got str"):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_site_empty_spec_reports_the_missing_field(self, tmp_path):
        """`spec:` with no value must not become an unindexable `None`."""
        path = tmp_path / "s.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: n\nspec:\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"Missing required field 'subscription'"):
            Site.from_file(path)


class TestSiteParseIsShared:
    """`from_file` and the orchestrator's resolved path are one parse.

    Two copies of the shape handling drifted apart in exactly one way that
    mattered: a rule added to one held for only one. These pin the equivalence
    so a change to either has to keep it.
    """

    def test_from_file_and_from_data_agree(self, tmp_path):
        body = (
            "apiVersion: siteops/v1\nkind: Site\nname: agree\n"
            "subscription: sub-123\nlocation: eastus\nresourceGroup: rg-test\n"
            "labels:\n  environment: dev\nproperties:\n  resourceSets:\n    dataflows: set-a\n"
        )
        path = tmp_path / "agree.yaml"
        path.write_text(body, encoding="utf-8")

        from_file = Site.from_file(path)
        from_data = Site.from_data(yaml.safe_load(body), source=path, default_name=path.stem)

        assert from_file == from_data

    def test_nested_envelope_agrees_too(self, tmp_path):
        body = (
            "apiVersion: siteops/v1\nkind: Site\n"
            "metadata:\n  name: nested\n  labels:\n    environment: prod\n"
            "spec:\n  subscription: sub-123\n  location: eastus\n  resourceGroup: rg-test\n"
        )
        path = tmp_path / "nested.yaml"
        path.write_text(body, encoding="utf-8")

        from_file = Site.from_file(path)
        from_data = Site.from_data(yaml.safe_load(body), source=path, default_name=path.stem)

        assert from_file == from_data
        assert from_file.labels == {"environment": "prod"}


class TestSiteEnvelopeIsClosed:
    """The site envelope rejects a key no parser reads.

    A misspelled envelope key used to load clean and contribute nothing, so a
    site deployed with defaults and reported success. Resource sets made
    `properties` decide what a site deploys, which is what raised the cost.

    These pin the rules directly. Disabling all three checks previously left
    the site suite green, because nothing asserted them.
    """

    FLAT = (
        "apiVersion: siteops/v1\nkind: Site\nname: munich-dev\n"
        "subscription: sub-123\nlocation: eastus\n"
    )
    NESTED = (
        "apiVersion: siteops/v1\nkind: Site\n"
        "metadata:\n  name: munich-dev\n"
        "spec:\n  subscription: sub-123\n  location: eastus\n"
    )

    def _write(self, tmp_path, body: str) -> Path:
        path = tmp_path / "munich-dev.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_unknown_flat_key_is_rejected_with_a_suggestion(self, tmp_path):
        path = self._write(tmp_path, self.FLAT + "paramaters:\n  a: b\n")

        with pytest.raises(ValueError) as exc:
            Site.from_file(path)

        message = str(exc.value)
        assert "unknown top-level key" in message
        assert "`paramaters`" in message
        assert "did you mean `parameters`?" in message

    def test_unknown_key_names_the_site_not_the_manifest(self, tmp_path):
        """A site reporting itself as a manifest sends the reader to the wrong
        file and the wrong allowed-key list."""
        path = self._write(tmp_path, self.FLAT + "widgets: 3\n")

        with pytest.raises(ValueError, match=r"^Site '"):
            Site.from_file(path)

    def test_unknown_key_is_rejected_in_the_nested_shape(self, tmp_path):
        """The envelope is closed in both shapes, at both levels."""
        path = self._write(tmp_path, self.NESTED + "  paramaters:\n    a: b\n")

        with pytest.raises(ValueError, match=r"unknown spec key"):
            Site.from_file(path)

    def test_unknown_metadata_key_is_rejected(self, tmp_path):
        path = self._write(
            tmp_path,
            "apiVersion: siteops/v1\nkind: Site\n"
            "metadata:\n  name: munich-dev\n  lables:\n    a: b\n"
            "spec:\n  subscription: sub-123\n  location: eastus\n",
        )

        with pytest.raises(ValueError, match=r"unknown metadata key"):
            Site.from_file(path)

    @pytest.mark.parametrize("field", ["subscription", "location"])
    def test_required_field_absent_is_rejected(self, tmp_path, field):
        lines = [ln for ln in self.FLAT.splitlines() if not ln.startswith(f"{field}:")]
        path = self._write(tmp_path, "\n".join(lines) + "\n")

        with pytest.raises(ValueError, match=rf"Missing required field '{field}'"):
            Site.from_file(path)

    @pytest.mark.parametrize("field", ["subscription", "location"])
    @pytest.mark.parametrize("empty", ["", "   ", "\n"], ids=["null", "blank", "newline"])
    def test_required_field_present_without_a_value_is_rejected(
        self, tmp_path, field, empty
    ):
        """Presence is not enough. A key with nothing after the colon parses as
        null and reached a command line as the string `None`."""
        body = self.FLAT.replace(f"{field}: sub-123", f"{field}:{empty}")
        body = body.replace(f"{field}: eastus", f"{field}:{empty}")
        path = self._write(tmp_path, body)

        with pytest.raises(ValueError, match=rf"'{field}'.*(no value|Missing required)"):
            Site.from_file(path)

    @pytest.mark.parametrize("key", ["labels", "properties", "parameters"])
    @pytest.mark.parametrize(
        ("value", "kind"),
        [("not-a-map", "str"), ("- a\n  - b", "list"), ("7", "int")],
        ids=["str", "list", "int"],
    )
    def test_open_container_must_be_a_mapping_flat(self, tmp_path, key, value, kind):
        """Open as to which keys it carries, closed as to being a mapping,
        since every reader indexes into it."""
        if kind == "list":
            body = self.FLAT + f"{key}:\n  - a\n  - b\n"
        else:
            body = self.FLAT + f"{key}: {value}\n"
        path = self._write(tmp_path, body)

        with pytest.raises(ValueError, match=rf"'{key}' in site .* must be a mapping"):
            Site.from_file(path)

    @pytest.mark.parametrize("key", ["properties", "parameters"])
    def test_open_container_must_be_a_mapping_nested(self, tmp_path, key):
        path = self._write(tmp_path, self.NESTED + f"  {key}: not-a-map\n")

        with pytest.raises(ValueError, match=rf"'{key}' in site .* must be a mapping"):
            Site.from_file(path)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("name", "\n  - one\n  - two"),
            ("name", " 2607"),
            ("subscription", " 12345"),
            ("location", "\n  - eastus"),
            ("resourceGroup", " 7"),
            ("inherits", "\n  - base.yaml"),
        ],
        ids=["name-list", "name-int", "subscription-int", "location-list", "rg-int", "inherits-list"],
    )
    def test_a_text_field_must_be_text(self, tmp_path, key, value):
        """A name becomes a dictionary key while the workspace index is built,
        and a list is not hashable, so it failed before any message could name
        the file. The others reach a command line or a path join. The error
        says to quote the value, since an unquoted release number such as 2607
        is the way this is usually reached."""
        body = self.FLAT
        if f"{key}:" in body:
            line = [ln for ln in body.splitlines() if ln.startswith(f"{key}:")][0]
            body = body.replace(line + "\n", f"{key}:{value}\n")
        else:
            body = body + f"{key}:{value}\n"
        path = self._write(tmp_path, body)

        with pytest.raises(ValueError, match=rf"'{key}' in site .* must be text"):
            Site.from_file(path)

    def test_a_quoted_numeric_name_is_accepted(self, tmp_path):
        """The rule is about the type, not the characters. A site named for a
        release is legitimate as long as it is quoted."""
        body = self.FLAT.replace("name: munich-dev\n", "name: '2607'\n")
        path = self._write(tmp_path, body)

        assert Site.from_file(path).name == "2607"

    @pytest.mark.parametrize(
        ("value", "kind"),
        [("2607", "int"), ("true", "bool"), ("1.5", "float")],
        ids=["int", "bool", "float"],
    )
    def test_a_label_value_must_be_text(self, tmp_path, value, kind):
        """A selector compares text, so a label of any other type matches
        nothing. It is rejected rather than coerced, since coercing would make
        a site start matching a selector it never matched and change what a
        deployment targets."""
        path = self._write(tmp_path, self.FLAT + f"labels:\n  release: {value}\n")

        with pytest.raises(ValueError, match=rf"Label 'release'.*must be text, got {kind}"):
            Site.from_file(path)

    def test_a_quoted_label_value_is_accepted(self, tmp_path):
        path = self._write(tmp_path, self.FLAT + "labels:\n  release: '2607'\n")

        assert Site.from_file(path).labels == {"release": "2607"}

    def test_a_label_value_must_be_text_nested(self, tmp_path):
        path = self._write(
            tmp_path, self.NESTED.replace("  name: munich-dev\n", "  name: munich-dev\n  labels:\n    release: 2607\n")
        )

        with pytest.raises(ValueError, match=r"Label 'release'.*must be text"):
            Site.from_file(path)

    def test_a_text_field_must_be_text_nested(self, tmp_path):
        path = self._write(
            tmp_path, self.NESTED.replace("  name: munich-dev\n", "  name:\n  - one\n  - two\n")
        )

        with pytest.raises(ValueError, match=r"'name' in site .* must be text"):
            Site.from_file(path)

    def test_description_is_accepted_and_not_read(self, tmp_path):
        """Manifests carry one, so a site that already has it must keep
        loading now that unknown keys are rejected."""
        path = self._write(tmp_path, self.FLAT + "description: the munich plant\n")

        site = Site.from_file(path)

        assert site.name == "munich-dev"
        assert not hasattr(site, "description")

    def test_a_bare_name_falls_back_to_the_filename(self, tmp_path):
        """`name:` with no value parsed as null and reached `Site` as `None`,
        which broke sorting and interpolation far from this file."""
        path = self._write(tmp_path, self.FLAT.replace("name: munich-dev", "name:"))

        site = Site.from_file(path)

        assert site.name == "munich-dev"
        assert sorted([site], key=lambda s: s.name)


class TestSiteValidationCoversResolvedData:
    """Validation runs on merged data, so inheritance and overlays are covered.

    The engine merges a base file, its inherit chain, and any overlay before
    constructing the model, so a rule added at construction needs no separate
    overlay handling. That is only true while validation stays on the merged
    path, which is what these pin.
    """

    @staticmethod
    def _orchestrator(workspace: Path):
        from siteops.orchestrator import Orchestrator

        return Orchestrator(workspace)

    def _workspace(self, tmp_path) -> Path:
        workspace = tmp_path / "workspace"
        (workspace / "sites" / "shared").mkdir(parents=True)
        (workspace / "sites" / "shared" / "base.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\nname: base\n"
            "subscription: sub-123\nlocation: eastus\n",
            encoding="utf-8",
        )
        return workspace

    def test_unknown_key_contributed_by_a_parent_is_rejected(self, tmp_path):
        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "shared" / "base.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\nname: base\n"
            "subscription: sub-123\nlocation: eastus\nparamaters:\n  a: b\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\n"
            "inherits: shared/base.yaml\nresourceGroup: rg-munich\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"unknown top-level key"):
            self._orchestrator(workspace).load_site("munich")

    def test_unknown_key_contributed_by_an_overlay_is_rejected(self, tmp_path):
        """`sites.local/` is gitignored, so a typo there reaches no test that
        reads committed content only."""
        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\n"
            "subscription: sub-123\nlocation: eastus\nresourceGroup: rg-munich\n",
            encoding="utf-8",
        )
        (workspace / "sites.local").mkdir()
        (workspace / "sites.local" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\nparamaters:\n  a: b\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"unknown top-level key"):
            self._orchestrator(workspace).load_site("munich")

    def test_a_required_value_may_come_from_a_parent(self, tmp_path):
        """The check runs after the merge, so a child may leave one to its
        template. Rejecting here would break inheritance."""
        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\n"
            "inherits: shared/base.yaml\nresourceGroup: rg-munich\n",
            encoding="utf-8",
        )

        site = self._orchestrator(workspace).load_site("munich")

        assert site.subscription == "sub-123"
        assert site.location == "eastus"

    def test_an_overlay_that_blanks_a_required_value_is_rejected(self, tmp_path):
        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\n"
            "subscription: sub-123\nlocation: eastus\nresourceGroup: rg-munich\n",
            encoding="utf-8",
        )
        (workspace / "sites.local").mkdir()
        (workspace / "sites.local" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\nsubscription:\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"'subscription'.*no value"):
            self._orchestrator(workspace).load_site("munich")


class TestStepCollectionsAreTypeChecked:
    """A step collection written as a single string is rejected.

    YAML accepts `parameters: parameters/common.yaml` where a list is meant,
    and the engine then iterates the string one character at a time, looking
    for parameter files named `p`, `a`, `r`. The error has to name the file and
    the key instead.
    """

    def _manifest(self, tmp_path, body: str) -> Path:
        (tmp_path / "manifests").mkdir(exist_ok=True)
        path = tmp_path / "manifests" / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\n" + body, encoding="utf-8"
        )
        return path

    def test_a_string_step_parameters_is_rejected(self, tmp_path):
        path = self._manifest(
            tmp_path,
            "steps:\n  - name: s\n    template: t.bicep\n    parameters: parameters/common.yaml\n",
        )

        with pytest.raises(ValueError, match=r"'parameters' in .* must be a list, got str"):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_string_kubectl_files_is_rejected(self, tmp_path):
        path = self._manifest(
            tmp_path,
            "steps:\n  - name: s\n    type: kubectl\n    operation: apply\n"
            "    arc:\n      name: c\n      resourceGroup: rg\n    files: configs/app.yaml\n",
        )

        with pytest.raises(ValueError, match=r"'files' in .* must be a list, got str"):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_string_steps_is_rejected(self, tmp_path):
        path = self._manifest(tmp_path, "steps: not-a-list\n")

        with pytest.raises(ValueError, match=r"'steps' in .* must be a list, got str"):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_list_still_loads(self, tmp_path):
        path = self._manifest(
            tmp_path,
            "steps:\n  - name: s\n    template: t.bicep\n"
            "    parameters:\n      - parameters/common.yaml\n",
        )

        manifest = Manifest.from_file(path, workspace_root=tmp_path)

        assert manifest.steps[0].parameters == ["parameters/common.yaml"]

    def test_an_omitted_collection_is_empty(self, tmp_path):
        path = self._manifest(tmp_path, "steps:\n  - name: s\n    template: t.bicep\n")

        manifest = Manifest.from_file(path, workspace_root=tmp_path)

        assert manifest.steps[0].parameters == []

    def test_a_manifest_metadata_that_is_not_a_mapping_is_rejected(self, tmp_path):
        path = self._manifest(tmp_path, "")
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nmetadata: not-a-map\n"
            "spec:\n  steps:\n    - name: s\n      template: t.bicep\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"'metadata' in manifest .* must be a mapping"):
            Manifest.from_file(path, workspace_root=tmp_path)


class TestInheritsInsideSpecIsRejected:
    """`spec.inherits` is rejected rather than silently ignored.

    `inherits` is read at the top level, so a site that declared it inside
    `spec` loaded clean and quietly dropped everything its parent supplied.
    Both shapes inherit from the top level, so the placement is what is wrong.
    """

    def test_inherits_inside_spec_is_rejected_by_name(self, tmp_path):
        path = tmp_path / "child.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: child\n"
            "spec:\n  inherits: shared/base.yaml\n  subscription: sub\n  location: eastus\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"`inherits` inside `spec`"):
            Site.from_file(path)

    def test_the_message_points_at_the_placement_that_works(self, tmp_path):
        path = tmp_path / "child.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: child\n"
            "spec:\n  inherits: shared/base.yaml\n  subscription: sub\n  location: eastus\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match=r"top level"):
            Site.from_file(path)

    def test_the_flat_shape_still_inherits(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "sites" / "shared").mkdir(parents=True)
        (workspace / "sites" / "shared" / "base.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\nname: base\n"
            "subscription: sub-123\nlocation: eastus\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "child.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: child\n"
            "inherits: shared/base.yaml\nresourceGroup: rg-child\n",
            encoding="utf-8",
        )

        from siteops.orchestrator import Orchestrator

        site = Orchestrator(workspace).load_site("child")

        assert site.subscription == "sub-123"


class TestTheIndexPathDefersToValidation:
    """Building the workspace index must not crash on a malformed file.

    The index reads `metadata.name` before any site is validated, so a
    malformed envelope surfaced there as an attribute error naming neither the
    file nor the key.
    """

    def test_a_malformed_metadata_reports_the_file_and_key(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "sites" / "n.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nmetadata: not-a-map\n"
            "spec:\n  subscription: sub\n  location: eastus\n",
            encoding="utf-8",
        )

        from siteops.orchestrator import Orchestrator

        with pytest.raises(ValueError, match=r"'metadata' in site .* must be a mapping"):
            Orchestrator(workspace).load_site("n")

    @pytest.mark.parametrize(
        "body",
        [
            "apiVersion: siteops/v1\nkind: Site\nname:\n  - one\n  - two\n"
            "subscription: sub\nlocation: eastus\n",
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name:\n    - one\n"
            "spec:\n  subscription: sub\n  location: eastus\n",
        ],
        ids=["flat", "nested"],
    )
    def test_a_name_that_is_not_text_reports_the_file_and_key(self, tmp_path, body):
        """The index keys a dictionary by whatever `name` holds, so a list
        failed as an unhashable key before the file could be named."""
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "sites" / "n.yaml").write_text(body, encoding="utf-8")

        from siteops.orchestrator import Orchestrator

        with pytest.raises(ValueError, match=r"'name' in site .* must be text"):
            Orchestrator(workspace).load_site("n")


class TestTheSiteKeySetsStayInStep:
    """The two shapes describe one contract, so their key sets must agree.

    Only the flat set is pinned elsewhere, by the test comparing it against the
    documented list. A field added to a nested set alone would be accepted in
    one shape and rejected in the other, and nothing would say so.
    """

    def test_the_nested_sets_partition_the_flat_set(self):
        from siteops.models import (
            _SITE_FLAT_KNOWN_KEYS,
            _SITE_NESTED_METADATA_KEYS,
            _SITE_NESTED_SPEC_KEYS,
        )

        # `apiVersion` and `kind` stay at the top in both shapes. Everything
        # else the flat shape reads has to appear in exactly one nested
        # container, or the two shapes accept different files.
        envelope = {"apiVersion", "kind"}
        nested_total = _SITE_NESTED_METADATA_KEYS | _SITE_NESTED_SPEC_KEYS | envelope

        # `inherits` is deliberately flat-only, since no loader reads it from a
        # spec. It is the one field the nested shape does not carry.
        assert _SITE_FLAT_KNOWN_KEYS - nested_total == {"inherits"}
        assert nested_total - _SITE_FLAT_KNOWN_KEYS == set()

    def test_the_containers_do_not_overlap(self):
        from siteops.models import _SITE_NESTED_METADATA_KEYS, _SITE_NESTED_SPEC_KEYS

        assert not (_SITE_NESTED_METADATA_KEYS & _SITE_NESTED_SPEC_KEYS), (
            "a key in both containers would be accepted in either place, and "
            "only one of them is read"
        )

    def test_the_nested_top_level_carries_only_the_envelope(self):
        from siteops.models import _SITE_NESTED_TOP_KEYS

        assert _SITE_NESTED_TOP_KEYS == {"apiVersion", "kind", "metadata", "spec"}


class TestASiteErrorNamesTheFilesBehindIt:
    """A site is checked after its inherit chain and overlays are merged.

    The name in the error is the site, so on its own it points at none of the
    files that could hold the offending key. One shared parent produces the
    same error for every site that inherits it, and an overlay directory is
    not part of what CI checks out.
    """

    BASE = (
        "apiVersion: siteops/v1\nkind: SiteTemplate\n"
        "subscription: sub\nlocation: eastus\n"
    )

    def _workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "sites.local").mkdir()
        return workspace

    def test_a_key_from_a_parent_template_names_the_parent(self, tmp_path):
        from siteops.orchestrator import Orchestrator

        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "base-site.yaml").write_text(
            self.BASE + "paramaters:\n  a: b\n", encoding="utf-8"
        )
        (workspace / "sites" / "munich-dev.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich-dev\n"
            "inherits: base-site.yaml\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as exc:
            Orchestrator(workspace).load_site("munich-dev")

        assert "sites/base-site.yaml" in str(exc.value)

    def test_a_key_from_an_overlay_names_the_overlay(self, tmp_path):
        """`sites.local/` is gitignored, so the file CI never sees is exactly
        the one an operator needs named."""
        from siteops.orchestrator import Orchestrator

        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "munich-dev.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich-dev\n"
            "subscription: sub\nlocation: eastus\n",
            encoding="utf-8",
        )
        (workspace / "sites.local" / "munich-dev.yaml").write_text(
            "paramaters:\n  a: b\n", encoding="utf-8"
        )

        with pytest.raises(ValueError) as exc:
            Orchestrator(workspace).load_site("munich-dev")

        assert "sites.local/munich-dev.yaml" in str(exc.value)

    def test_a_valid_site_still_reuses_the_inherit_chain_memo(self, tmp_path):
        """Collecting the file list runs on the failure path only. Doing it
        during every load would read a shared parent once per site."""
        from siteops.orchestrator import Orchestrator

        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "base-site.yaml").write_text(self.BASE, encoding="utf-8")
        for name in ("munich-dev", "seattle-dev"):
            (workspace / "sites" / f"{name}.yaml").write_text(
                f"apiVersion: siteops/v1\nkind: Site\nname: {name}\n"
                f"inherits: base-site.yaml\n",
                encoding="utf-8",
            )

        orchestrator = Orchestrator(workspace)
        orchestrator.load_site("munich-dev")
        orchestrator.load_site("seattle-dev")

        assert orchestrator._inherited_data_cache, (
            "the inherit chain was not memoized, so a shared parent is reparsed "
            "for every site that inherits it"
        )


class TestARejectedKeyIsExplainedNotJustNamed:
    """A rejection is only useful if it says what to do next.

    A key can be rejected for three different reasons, and the closest-spelling
    suggestion is the right answer for only one of them. Offering it for the
    others points at a field that is real, different, and also wrong.
    """

    def _write(self, tmp_path, body):
        path = tmp_path / "munich-dev.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            (
                "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: n\n"
                "spec:\n  description: x\n  subscription: s\n  location: eastus\n",
                "belongs under `metadata`",
            ),
            (
                "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: n\n"
                "  subscription: s\nspec:\n  subscription: s\n  location: eastus\n",
                "belongs under `spec`",
            ),
        ],
        ids=["description-in-spec", "subscription-in-metadata"],
    )
    def test_a_real_field_in_the_wrong_container_is_told_where_it_goes(
        self, tmp_path, body, expected
    ):
        with pytest.raises(ValueError) as exc:
            Site.from_file(self._write(tmp_path, body))

        assert expected in str(exc.value)
        assert "did you mean" not in str(exc.value)

    @pytest.mark.parametrize(
        "body",
        [
            "apiVersion: siteops/v1\nkind: Site\nname: n\nsubscription: s\n"
            "location: eastus\nannotations:\n  owner: platform\n",
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: n\n"
            "  annotations:\n    owner: platform\n"
            "spec:\n  subscription: s\n  location: eastus\n",
        ],
        ids=["flat", "nested"],
    )
    def test_a_kubernetes_key_this_engine_does_not_read_is_named_as_such(
        self, tmp_path, body
    ):
        """`annotations` is recognizable to anyone who writes Kubernetes and is
        read by nothing here. The answer is where operator metadata goes, which
        no spelling suggestion can express."""
        with pytest.raises(ValueError) as exc:
            Site.from_file(self._write(tmp_path, body))

        message = str(exc.value)
        assert "not read by siteops" in message
        assert "`labels`" in message and "`properties`" in message

    def test_a_file_carrying_both_shapes_is_told_it_carries_both(self, tmp_path):
        """Reporting three real field names as unknown keys says they are not
        real, which is the opposite of the problem."""
        body = (
            "apiVersion: siteops/v1\nkind: Site\nname: n\nsubscription: s\n"
            "location: eastus\nspec:\n  resourceGroup: rg\n"
        )

        with pytest.raises(ValueError) as exc:
            Site.from_file(self._write(tmp_path, body))

        message = str(exc.value)
        assert "mixes the two site shapes" in message
        for field in ("`name`", "`subscription`", "`location`"):
            assert field in message
        assert "unknown" not in message

    def test_a_blank_required_key_is_told_that_removing_it_is_an_option(self, tmp_path):
        """The operator who hits this was usually inheriting the value already,
        and the blank key is what defeated it."""
        body = (
            "apiVersion: siteops/v1\nkind: Site\nname: n\nsubscription:\n"
            "location: eastus\n"
        )

        with pytest.raises(ValueError) as exc:
            Site.from_file(self._write(tmp_path, body))

        assert "remove the key entirely" in str(exc.value)

    def test_a_plain_typo_still_gets_the_spelling_suggestion(self, tmp_path):
        """The suggestion is right for the case it was written for, so the
        other paths must not have replaced it."""
        body = (
            "apiVersion: siteops/v1\nkind: Site\nname: n\nsubscription: s\n"
            "location: eastus\nparamaters:\n  a: b\n"
        )

        with pytest.raises(ValueError) as exc:
            Site.from_file(self._write(tmp_path, body))

        assert "did you mean `parameters`?" in str(exc.value)


class TestInheritsIsReadAtTheTopLevelOfEitherShape:
    """Inheritance is about where `inherits` sits, not which shape the file uses.

    The envelope shape inherits exactly as the flat shape does, provided
    `inherits:` sits beside `apiVersion` and `kind`. Telling a reader that the
    envelope cannot inherit sends them to rewrite a file when moving one line
    is the fix.
    """

    def _workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        return workspace

    def test_an_envelope_site_inherits_from_an_envelope_template(self, tmp_path):
        from siteops.orchestrator import Orchestrator

        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "base-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\n"
            "spec:\n  subscription: sub-parent\n  location: eastus\n"
            "  properties:\n    fromParent: true\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\ninherits: base-site.yaml\n"
            "metadata:\n  name: munich\nspec:\n  resourceGroup: rg-munich\n",
            encoding="utf-8",
        )

        site = Orchestrator(workspace).load_site("munich")

        assert site.subscription == "sub-parent"
        assert site.location == "eastus"
        assert site.properties == {"fromParent": True}
        assert site.resource_group == "rg-munich"

    def test_inherits_inside_spec_says_to_move_it_up(self, tmp_path):
        """The placement is wrong, not the shape."""
        path = tmp_path / "munich.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Site\nmetadata:\n  name: munich\n"
            "spec:\n  inherits: base-site.yaml\n  subscription: s\n  location: eastus\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as exc:
            Site.from_file(path)

        message = str(exc.value)
        assert "top level" in message
        assert "flat" not in message, (
            "the envelope shape inherits too, so the error must not name a shape"
        )

    def test_inherits_that_is_not_a_path_names_the_file_and_the_key(self, tmp_path):
        """`inherits` is read while the merge is assembled, before any model
        exists, so a list reached a path join and raised a bare TypeError that
        named neither the file nor the key and took the whole listing down."""
        from siteops.orchestrator import Orchestrator

        workspace = self._workspace(tmp_path)
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\n"
            "inherits:\n  - a.yaml\n  - b.yaml\nsubscription: s\nlocation: eastus\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError) as exc:
            Orchestrator(workspace).load_site("munich")

        message = str(exc.value)
        assert "'inherits'" in message
        assert "munich" in message
        assert "list" in message


class TestTheMergedFileListReadsInMergeOrder:
    """The order is the precedence, so it has to be the real one.

    A reader uses the list to decide which file wins. Recording each file as it
    was opened put a child ahead of the parent it inherits from, which reverses
    that.
    """

    def test_parents_come_first_then_the_base_then_the_overlay(self, tmp_path):
        from siteops.orchestrator import Orchestrator

        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "sites.local").mkdir()
        (workspace / "sites" / "grandparent.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\nsubscription: s\nlocation: eastus\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "parent.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\ninherits: grandparent.yaml\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "munich.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: munich\ninherits: parent.yaml\n",
            encoding="utf-8",
        )
        (workspace / "sites.local" / "munich.yaml").write_text(
            "paramaters:\n  a: b\n", encoding="utf-8"
        )

        with pytest.raises(ValueError) as exc:
            Orchestrator(workspace).load_site("munich")

        listed = str(exc.value).split("Merged from:")[1]
        positions = [
            listed.index(name)
            for name in (
                "sites/grandparent.yaml",
                "sites/parent.yaml",
                "sites/munich.yaml",
                "sites.local/munich.yaml",
            )
        ]
        assert positions == sorted(positions), (
            f"files are not listed in merge order: {listed.strip()}"
        )


class TestAListEntryIsHeldToItsType:
    """A list of paths or identifiers is unusable if one entry is not text.

    YAML turns an unquoted release or version into a number, so this is reached
    by writing `sites: [2607]` rather than `sites: ["2607"]`. A site named that
    way was dropped without a word, and a parameter path that way raised from
    whatever first joined it to a directory.
    """

    def _manifest(self, tmp_path, body: str) -> Path:
        path = tmp_path / "m.yaml"
        path.write_text(
            "apiVersion: siteops/v1\nkind: Manifest\nname: m\n" + body, encoding="utf-8"
        )
        return path

    def test_a_site_entry_that_is_not_text_is_reported(self, tmp_path):
        """Dropping it silently means deploying to fewer sites than asked for,
        which is the worst outcome for a fleet command."""
        path = self._manifest(
            tmp_path,
            "sites: [2607, other]\nsteps:\n  - name: s\n    template: t.bicep\n",
        )

        with pytest.raises(ValueError, match=r"Entry 0 of 'sites'.*must be str, got int"):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_step_parameter_entry_that_is_not_text_is_reported(self, tmp_path):
        path = self._manifest(
            tmp_path,
            "sites: [a]\nsteps:\n  - name: s\n    template: t.bicep\n    parameters: [123]\n",
        )

        with pytest.raises(
            ValueError, match=r"Entry 0 of 'parameters'.*must be str, got int"
        ):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_manifest_parameter_entry_that_is_not_text_is_reported(self, tmp_path):
        path = self._manifest(
            tmp_path,
            "sites: [a]\nparameters: [7]\nsteps:\n  - name: s\n    template: t.bicep\n",
        )

        with pytest.raises(
            ValueError,
            match=r"parameters\[0\] must be a path string or a mapping",
        ):
            Manifest.from_file(path, workspace_root=tmp_path)

    def test_a_quoted_numeric_entry_is_accepted(self, tmp_path):
        """The rule is the type, not the characters."""
        path = self._manifest(
            tmp_path,
            "sites: ['2607']\nsteps:\n  - name: s\n    template: t.bicep\n",
        )

        assert Manifest.from_file(path, workspace_root=tmp_path).sites == ["2607"]

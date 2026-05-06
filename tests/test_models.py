"""Unit tests for Site Ops data models.

Tests cover:
- Site loading and validation
- Manifest parsing and step types
- Selector parsing
- Condition pattern matching
- Error handling for invalid inputs
"""

from pathlib import Path

import pytest
import yaml

from siteops.models import (
    CONDITION_PATTERN,
    ArcCluster,
    DeploymentStep,
    KubectlStep,
    Manifest,
    ParallelConfig,
    Site,
    parse_selector,
    _validate_resource,
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

    def test_key_without_value(self):
        # Edge case: key without = should be ignored
        result = parse_selector("valid=yes,invalid")
        assert result == {"valid": ["yes"]}

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

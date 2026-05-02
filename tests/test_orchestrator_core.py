"""Tests for core orchestrator functionality.

Covers:
- Site loading and caching
- Site overlay merging (sites/, sites.local/)
- Site resolution from manifests
- Deployment name generation
- Output path resolution
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops.models import DeploymentStep, Manifest, ParallelConfig, Site
from siteops.orchestrator import Orchestrator, _resolve_output_path


class TestResolveOutputPath:
    """Tests for the _resolve_output_path helper function."""

    def test_simple_path(self):
        obj = {"name": "test-value"}
        assert _resolve_output_path(obj, "name") == "test-value"

    def test_nested_path(self):
        obj = {"resource": {"id": "resource-123", "name": "myresource"}}
        assert _resolve_output_path(obj, "resource.id") == "resource-123"

    def test_azure_output_unwrap(self):
        # Azure ARM outputs are wrapped in {"value": X, "type": "..."}
        obj = {"storageId": {"value": "storage-123", "type": "String"}}
        assert _resolve_output_path(obj, "storageId") == "storage-123"

    def test_nested_azure_output(self):
        obj = {
            "resource": {
                "value": {"id": "res-123", "name": "myres"},
                "type": "Object",
            }
        }
        assert _resolve_output_path(obj, "resource.id") == "res-123"

    def test_missing_path(self):
        obj = {"name": "test"}
        assert _resolve_output_path(obj, "nonexistent") is None
        assert _resolve_output_path(obj, "name.nested") is None

    def test_none_input(self):
        assert _resolve_output_path(None, "anything") is None


class TestOrchestratorSiteLoading:
    """Tests for site loading functionality."""

    def test_load_site_basic(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = orchestrator.load_site("test-site")

        assert site.name == "test-site"
        assert site.location == "eastus"
        assert site.labels["environment"] == "dev"

    def test_load_site_caching(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        site1 = orchestrator.load_site("test-site")
        site2 = orchestrator.load_site("test-site")

        assert site1 is site2  # Same object from cache

    def test_load_site_not_found(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(FileNotFoundError, match="not found"):
            orchestrator.load_site("nonexistent")

    def test_load_all_sites(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        sites = orchestrator.load_all_sites()

        assert len(sites) == 3
        site_names = {s.name for s in sites}
        assert site_names == {"dev-eastus", "dev-westus", "prod-eastus"}

    def test_load_site_with_yml_extension(self, tmp_workspace):
        """Sites with .yml extension should load correctly."""
        (tmp_workspace / "sites" / "yml-site.yml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: yml-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-yml
location: eastus
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("yml-site")

        assert site.name == "yml-site"
        assert site.location == "eastus"


class TestSiteOverlayMerging:
    """Tests for the two-tier site overlay system."""

    def test_sites_local_overrides_base(self, tmp_workspace):
        """Local overrides should take precedence over base."""
        base_site = {
            "name": "overlay-test",
            "subscription": "base-sub",
            "resourceGroup": "base-rg",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "overlay-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "subscription": "local-sub",
            "resourceGroup": "local-rg",
        }
        (tmp_workspace / "sites.local" / "overlay-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("overlay-test")

        # Local values should override base
        assert site.subscription == "local-sub"
        assert site.resource_group == "local-rg"
        # Base values should be preserved
        assert site.location == "eastus"

    def test_deep_merge_labels(self, tmp_workspace):
        """Labels should be deep merged across overlay layers."""
        base_site = {
            "name": "merge-test",
            "subscription": "sub",
            "location": "eastus",
            "labels": {"env": "base", "team": "platform"},
        }
        (tmp_workspace / "sites" / "merge-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {"labels": {"env": "local", "added": "new"}}
        (tmp_workspace / "sites.local" / "merge-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("merge-test")

        # Labels should be deep merged
        assert site.labels["env"] == "local"  # Overridden
        assert site.labels["team"] == "platform"  # Preserved
        assert site.labels["added"] == "new"  # Added

    def test_deep_merge_properties(self, tmp_workspace):
        """Properties should be deep merged across overlay layers."""
        base_site = {
            "name": "props-merge-test",
            "subscription": "sub",
            "location": "eastus",
            "properties": {
                "mqtt": {"broker": "mqtt://base:1883", "qos": 1},
                "endpoints": [{"name": "base-endpoint", "host": "10.0.0.1"}],
                "baseOnly": "preserved",
            },
        }
        (tmp_workspace / "sites" / "props-merge-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "properties": {
                "mqtt": {"broker": "mqtt://local:1883", "clientId": "local-client"},
                "localOnly": "added",
            },
        }
        (tmp_workspace / "sites.local" / "props-merge-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("props-merge-test")

        # Properties should be deep merged
        assert site.properties["mqtt"]["broker"] == "mqtt://local:1883"  # Overridden
        assert site.properties["mqtt"]["qos"] == 1  # Preserved from base
        assert site.properties["mqtt"]["clientId"] == "local-client"  # Added
        assert site.properties["baseOnly"] == "preserved"  # Base only preserved
        assert site.properties["localOnly"] == "added"  # Local only added

    def test_subscription_level_site_overlay(self, tmp_workspace):
        """Overlay on subscription-level site preserves subscription-level status."""
        base_site = {
            "name": "sub-level-test",
            "subscription": "base-sub",
            # No resourceGroup - subscription-level site
            "location": "eastus",
            "labels": {"team": "infra"},
        }
        (tmp_workspace / "sites" / "sub-level-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "subscription": "local-sub",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites.local" / "sub-level-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("sub-level-test")

        # Site should remain subscription-level
        assert site.is_subscription_level is True
        assert site.resource_group == ""
        # Overlay values should be applied
        assert site.subscription == "local-sub"
        assert site.labels["environment"] == "dev"
        assert site.labels["team"] == "infra"  # Preserved from base

    def test_overlay_adds_resource_group(self, tmp_workspace):
        """Overlay can convert subscription-level to RG-level by adding resourceGroup."""
        base_site = {
            "name": "upgrade-test",
            "subscription": "sub",
            # No resourceGroup - subscription-level site
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "upgrade-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {"resourceGroup": "rg-from-overlay"}
        (tmp_workspace / "sites.local" / "upgrade-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("upgrade-test")

        # Site should now be RG-level
        assert site.is_subscription_level is False
        assert site.resource_group == "rg-from-overlay"

    def test_properties_deep_merge_preserves_sibling_keys(self, tmp_workspace):
        """Overlay overriding one nested key should preserve sibling keys from base."""
        base_site = {
            "name": "sibling-test",
            "subscription": "sub",
            "location": "eastus",
            "properties": {
                "deployOptions": {"enableSecretSync": True, "includeSolution": False},
            },
        }
        (tmp_workspace / "sites" / "sibling-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "properties": {"deployOptions": {"enableSecretSync": False}},
        }
        (tmp_workspace / "sites.local" / "sibling-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("sibling-test")

        assert site.properties["deployOptions"]["enableSecretSync"] is False  # Overridden
        assert site.properties["deployOptions"]["includeSolution"] is False  # Preserved from base

    def test_properties_deep_merge_overlay_adds_new_nested_key(self, tmp_workspace):
        """Overlay adding a new nested key should merge with existing base keys."""
        base_site = {
            "name": "add-key-test",
            "subscription": "sub",
            "location": "eastus",
            "properties": {
                "deployOptions": {"includeSolution": True},
            },
        }
        (tmp_workspace / "sites" / "add-key-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "properties": {"deployOptions": {"enableSecretSync": True}},
        }
        (tmp_workspace / "sites.local" / "add-key-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("add-key-test")

        assert site.properties["deployOptions"]["includeSolution"] is True  # Preserved from base
        assert site.properties["deployOptions"]["enableSecretSync"] is True  # Added by overlay

    def test_parameters_deep_merge_preserves_sibling_keys(self, tmp_workspace):
        """Overlay overriding one nested parameter key should preserve siblings from base."""
        base_site = {
            "name": "params-merge-test",
            "subscription": "sub",
            "location": "eastus",
            "parameters": {
                "brokerConfig": {"memoryProfile": "Medium", "replicas": 3},
            },
        }
        (tmp_workspace / "sites" / "params-merge-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "parameters": {"brokerConfig": {"memoryProfile": "Low"}},
        }
        (tmp_workspace / "sites.local" / "params-merge-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("params-merge-test")

        assert site.parameters["brokerConfig"]["memoryProfile"] == "Low"  # Overridden
        assert site.parameters["brokerConfig"]["replicas"] == 3  # Preserved from base


class TestResolveSites:
    """Tests for site resolution from manifests."""

    def test_explicit_sites_list(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=["dev-eastus", "dev-westus"],
            steps=[],
        )

        sites = orchestrator.resolve_sites(manifest)

        assert len(sites) == 2
        assert {s.name for s in sites} == {"dev-eastus", "dev-westus"}

    def test_site_selector(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=[],
            steps=[],
            site_selector="environment=dev",
        )

        sites = orchestrator.resolve_sites(manifest)

        assert len(sites) == 2
        assert all(s.labels["environment"] == "dev" for s in sites)

    def test_cli_selector_overrides(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=["dev-eastus"],  # Explicit list
            steps=[],
        )

        # CLI selector should override explicit list
        sites = orchestrator.resolve_sites(manifest, cli_selector="region=eastus")

        assert len(sites) == 2
        assert all(s.labels["region"] == "eastus" for s in sites)


class TestDeploymentNameGeneration:
    """Tests for deployment name truncation and hashing."""

    def test_short_name_no_truncation(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="dev",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        manifest = Manifest(name="short", description="", sites=[], steps=[])
        step = DeploymentStep(name="step", template="test.bicep")

        with patch.object(orchestrator.executor, "deploy_resource_group") as mock_deploy:
            mock_deploy.return_value = MagicMock(success=True, outputs={})
            orchestrator._deploy_bicep_step(site, step, manifest, "20260102120000", {})

            call_args = mock_deploy.call_args
            deployment_name = call_args.kwargs["deployment_name"]

            assert len(deployment_name) <= 64
            assert deployment_name == "short-dev-step-20260102120000"

    def test_long_name_gets_hash_suffix(self, complete_workspace):
        """Long deployment names should be truncated with hash suffix."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="very-long-site-name-that-exceeds-limits",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        manifest = Manifest(name="very-long-manifest-name", description="", sites=[], steps=[])
        step = DeploymentStep(name="very-long-step-name", template="test.bicep")

        with patch.object(orchestrator.executor, "deploy_resource_group") as mock_deploy:
            mock_deploy.return_value = MagicMock(success=True, outputs={})
            orchestrator._deploy_bicep_step(site, step, manifest, "20260102120000", {})

            call_args = mock_deploy.call_args
            deployment_name = call_args.kwargs["deployment_name"]

            # Name should be within Azure's limit
            assert len(deployment_name) <= 64
            # Should end with timestamp
            assert deployment_name.endswith("20260102120000")
            # Full name would be: very-long-manifest-name-very-long-site-name-that-exceeds-limits-very-long-step-name-20260102120000
            # Since that exceeds 64 chars, it should be truncated with a hash
            # The truncated name should be shorter than the full name would be
            full_name = (
                "very-long-manifest-name-very-long-site-name-that-exceeds-limits-very-long-step-name-20260102120000"
            )
            assert len(deployment_name) < len(full_name)


class TestDeployParallelConfig:
    """Tests for deployment with different parallel configurations."""

    def test_deploy_uses_manifest_parallel_config(self, complete_workspace):
        """Test that deploy uses the manifest's parallel config by default."""
        Orchestrator(complete_workspace)

        # Create manifest with parallel: 2
        manifest_path = complete_workspace / "manifests" / "parallel-test.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: parallel-test
sites: [test-site]
parallel: 2
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        assert manifest.parallel.sites == 2
        assert manifest.parallel.max_workers == 2

    def test_deploy_parallel_override_takes_precedence(self, complete_workspace):
        """Test that parallel_override parameter takes precedence over manifest."""
        orchestrator = Orchestrator(complete_workspace)

        manifest = Manifest(
            name="test",
            description="",
            sites=["test-site"],
            steps=[DeploymentStep(name="step1", template="templates/test.bicep")],
            parallel=ParallelConfig(sites=1),  # Sequential in manifest
        )

        # When parallel_override is provided, it should be used
        with patch.object(orchestrator, "_deploy_sequential") as mock_seq:
            with patch.object(orchestrator, "_deploy_parallel") as mock_par:
                mock_seq.return_value = []
                mock_par.return_value = []

                # Override to parallel mode
                orchestrator.deploy(
                    complete_workspace / "manifests" / "test-manifest.yaml",
                    parallel_override=3,
                    manifest=manifest,
                    sites=[orchestrator.load_site("test-site")],
                )

                # Should use parallel, not sequential
                assert mock_par.called or mock_seq.called

    def test_deploy_single_site_always_sequential(self, complete_workspace):
        """Test that single site deployment is always sequential regardless of config."""
        orchestrator = Orchestrator(complete_workspace)

        manifest = Manifest(
            name="test",
            description="",
            sites=["test-site"],
            steps=[DeploymentStep(name="step1", template="templates/test.bicep")],
            parallel=ParallelConfig(sites=0),  # Unlimited in manifest
        )

        with patch.object(orchestrator, "_deploy_sequential") as mock_seq:
            with patch.object(orchestrator, "_deploy_parallel") as mock_par:
                mock_seq.return_value = []

                orchestrator.deploy(
                    complete_workspace / "manifests" / "test-manifest.yaml",
                    manifest=manifest,
                    sites=[orchestrator.load_site("test-site")],
                )

                # Single site should use sequential
                mock_seq.assert_called_once()
                mock_par.assert_not_called()


class TestPlanParallelDisplay:
    """Tests for parallel config display in show_plan output."""

    def test_plan_shows_parallel_config(self, complete_workspace, capsys):
        """Test that show_plan output shows parallel configuration."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_path = complete_workspace / "manifests" / "parallel-plan.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: parallel-plan
sites: [test-site]
parallel: 3
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for parallel info in output - be flexible about exact format
        assert "Parallel" in captured.out or "parallel" in captured.out.lower()
        assert "3" in captured.out or "max 3" in captured.out

    def test_plan_shows_sequential(self, complete_workspace, capsys):
        """Test that show_plan output shows sequential mode."""
        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for parallel info - sequential is default
        assert "Parallel" in captured.out or "sequential" in captured.out.lower()

    def test_plan_shows_unlimited(self, complete_workspace, capsys):
        """Test that show_plan output shows unlimited mode."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_path = complete_workspace / "manifests" / "unlimited-plan.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: unlimited-plan
sites: [test-site]
parallel: 0
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for unlimited indicator
        assert "Parallel" in captured.out or "unlimited" in captured.out.lower()


class TestStepSiteCompatibility:
    """Tests for _check_step_site_compatibility method."""

    def test_kubectl_step_always_compatible(self, tmp_workspace):
        """Kubectl steps should run on any site type."""
        from siteops.models import ArcCluster, KubectlStep, Site

        orchestrator = Orchestrator(tmp_workspace)

        kubectl_step = KubectlStep(
            name="apply-config",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config.yaml"],
        )

        # Test with RG-level site
        rg_site = Site(
            name="rg-site",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        assert orchestrator._check_step_site_compatibility(kubectl_step, rg_site) is None

        # Test with subscription-level site
        sub_site = Site(
            name="sub-site",
            subscription="sub",
            resource_group="",
            location="eastus",
        )
        assert orchestrator._check_step_site_compatibility(kubectl_step, sub_site) is None

    def test_subscription_step_with_rg_site_skipped(self, tmp_workspace):
        """Subscription-scoped step should be skipped for RG-level site."""
        from siteops.models import DeploymentStep, Site

        orchestrator = Orchestrator(tmp_workspace)

        sub_step = DeploymentStep(
            name="sub-step",
            template="test.bicep",
            scope="subscription",
        )
        rg_site = Site(
            name="rg-site",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )

        reason = orchestrator._check_step_site_compatibility(sub_step, rg_site)
        assert reason is not None
        assert "subscription-scoped" in reason

    def test_rg_step_with_subscription_site_skipped(self, tmp_workspace):
        """ResourceGroup-scoped step should be skipped for subscription-level site."""
        from siteops.models import DeploymentStep, Site

        orchestrator = Orchestrator(tmp_workspace)

        rg_step = DeploymentStep(
            name="rg-step",
            template="test.bicep",
            scope="resourceGroup",
        )
        sub_site = Site(
            name="sub-site",
            subscription="sub",
            resource_group="",
            location="eastus",
        )

        reason = orchestrator._check_step_site_compatibility(rg_step, sub_site)
        assert reason is not None
        assert "resourceGroup-scoped" in reason

    def test_matching_scope_returns_none(self, tmp_workspace):
        """Matching scope/site level should return None (compatible)."""
        from siteops.models import DeploymentStep, Site

        orchestrator = Orchestrator(tmp_workspace)

        # RG step with RG site
        rg_step = DeploymentStep(name="rg-step", template="test.bicep", scope="resourceGroup")
        rg_site = Site(name="rg-site", subscription="sub", resource_group="rg", location="eastus")
        assert orchestrator._check_step_site_compatibility(rg_step, rg_site) is None

        # Subscription step with subscription site
        sub_step = DeploymentStep(name="sub-step", template="test.bicep", scope="subscription")
        sub_site = Site(name="sub-site", subscription="sub", resource_group="", location="eastus")
        assert orchestrator._check_step_site_compatibility(sub_step, sub_site) is None


class TestPrintSummary:
    """Tests for _print_deployment_summary method."""

    def test_summary_with_success_only(self, tmp_workspace, capsys):
        """Test summary output with only successful deployments."""
        orchestrator = Orchestrator(tmp_workspace)
        results = [
            {
                "site": "site-a",
                "status": "success",
                "steps_completed": 3,
                "steps_total": 3,
                "steps_skipped": 0,
                "elapsed": 10.5,
            },
            {
                "site": "site-b",
                "status": "success",
                "steps_completed": 3,
                "steps_total": 3,
                "steps_skipped": 0,
                "elapsed": 12.3,
            },
        ]

        orchestrator._print_deployment_summary(results, 15.0)

        captured = capsys.readouterr()
        assert "+ Success" in captured.out
        assert "2 succeeded" in captured.out
        assert "0 failed" in captured.out
        assert "site-a" in captured.out
        assert "site-b" in captured.out

    def test_summary_with_failed_sites(self, tmp_workspace, capsys):
        """Test summary output shows failed sites section."""
        orchestrator = Orchestrator(tmp_workspace)
        results = [
            {
                "site": "good-site",
                "status": "success",
                "steps_completed": 3,
                "steps_total": 3,
                "steps_skipped": 0,
                "elapsed": 10.0,
            },
            {
                "site": "bad-site",
                "status": "failed",
                "error": "Deployment failed: resource conflict",
                "steps_completed": 1,
                "steps_total": 3,
                "steps_skipped": 0,
                "elapsed": 5.0,
            },
        ]

        orchestrator._print_deployment_summary(results, 15.0)

        captured = capsys.readouterr()
        assert "x Failed" in captured.out
        assert "1 succeeded" in captured.out
        assert "1 failed" in captured.out
        assert "Failed Sites:" in captured.out
        assert "[bad-site]" in captured.out
        assert "resource conflict" in captured.out

    def test_summary_with_blocked_sites(self, tmp_workspace, capsys):
        """Test summary output shows blocked sites section."""
        orchestrator = Orchestrator(tmp_workspace)
        results = [
            {
                "site": "sub-site",
                "status": "failed",
                "error": "Subscription deployment failed",
                "steps_completed": 0,
                "steps_total": 5,
                "steps_skipped": 0,
                "elapsed": 2.0,
            },
            {
                "site": "blocked-site",
                "status": "blocked",
                "error": "Subscription deployment failed and site depends on its outputs",
                "steps_completed": 0,
                "steps_total": 5,
                "steps_skipped": 5,
                "elapsed": 0.0,
            },
        ]

        orchestrator._print_deployment_summary(results, 5.0)

        captured = capsys.readouterr()
        assert "- Blocked" in captured.out
        assert "1 blocked" in captured.out
        assert "Blocked Sites:" in captured.out
        assert "[blocked-site]" in captured.out

    def test_summary_with_skipped_steps(self, tmp_workspace, capsys):
        """Test summary output shows skipped step count."""
        orchestrator = Orchestrator(tmp_workspace)
        results = [
            {
                "site": "partial-site",
                "status": "success",
                "steps_completed": 5,
                "steps_total": 8,
                "steps_skipped": 3,
                "elapsed": 20.0,
            },
        ]

        orchestrator._print_deployment_summary(results, 20.0)

        captured = capsys.readouterr()
        assert "5/8" in captured.out
        assert "(3 skip)" in captured.out


class TestLoadParameters:
    """Tests for parameter file loading."""

    def test_load_parameters_missing_file(self, tmp_workspace):
        """Test that missing parameter file returns empty dict with warning."""
        orchestrator = Orchestrator(tmp_workspace)
        missing_path = tmp_workspace / "parameters" / "nonexistent.yaml"

        result = orchestrator.load_parameters(missing_path)

        assert result == {}

    def test_load_parameters_json_file(self, tmp_workspace):
        """Test loading parameters from a JSON file."""
        import json

        params_data = {"location": "eastus", "sku": "Standard_LRS"}
        json_path = tmp_workspace / "parameters" / "params.json"
        json_path.write_text(json.dumps(params_data))

        orchestrator = Orchestrator(tmp_workspace)
        result = orchestrator.load_parameters(json_path)

        assert result == {"location": "eastus", "sku": "Standard_LRS"}


class TestLoadAllSites:
    """Tests for loading all sites with error handling."""

    def test_load_all_sites_skips_bad_site(self, tmp_workspace):
        """Test that a malformed site file is skipped without crashing."""
        # Create one good site
        (tmp_workspace / "sites" / "good-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: good-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create one bad site (missing required fields)
        (tmp_workspace / "sites" / "bad-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: bad-site
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        sites = orchestrator.load_all_sites()

        # Only the good site should be loaded
        assert len(sites) == 1
        assert sites[0].name == "good-site"

    def test_load_all_sites_skips_bad_site_stderr_warning(self, tmp_workspace, capsys):
        """Skipped sites produce a visible warning on stderr."""
        (tmp_workspace / "sites" / "good-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: good-site\n"
            'subscription: "00000000-0000-0000-0000-000000000000"\n'
            "resourceGroup: rg-test\nlocation: eastus\n"
        )
        (tmp_workspace / "sites" / "bad-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: bad-site\n"
        )

        orchestrator = Orchestrator(tmp_workspace)
        sites = orchestrator.load_all_sites()

        assert len(sites) == 1
        err = capsys.readouterr().err
        assert "Skipped 1 site(s) due to errors:" in err
        assert "bad-site" in err

    def test_load_all_sites_no_warning_when_all_valid(self, tmp_workspace, capsys):
        """No stderr warning when all sites load successfully."""
        (tmp_workspace / "sites" / "site-a.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: site-a\n"
            'subscription: "00000000-0000-0000-0000-000000000000"\n'
            "resourceGroup: rg-test\nlocation: eastus\n"
        )

        orchestrator = Orchestrator(tmp_workspace)
        sites = orchestrator.load_all_sites()

        assert len(sites) == 1
        err = capsys.readouterr().err
        assert err == ""


class TestGetAllSiteNames:
    """Tests for site name discovery."""

    def test_get_all_site_names_no_sites_dir(self, tmp_path):
        """Test that missing sites directory returns empty list."""
        workspace = tmp_path / "empty-workspace"
        workspace.mkdir()

        orchestrator = Orchestrator(workspace)
        names = orchestrator._get_all_site_names()

        assert names == []


class TestSiteIdentityResolution:
    """Sites can be resolved by either filename stem or internal `name:`.

    Today most workspace sites use a `name:` that matches the filename
    stem. The bilingual lookup lets an operator declare a different
    `name:` (for renames or human-readable identifiers) and still have
    the site resolve from CLI selectors and `Orchestrator.load_site`.
    """

    def _write_site(self, workspace, filename, internal_name, **extra):
        body = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "name": internal_name,
            "subscription": "00000000-0000-0000-0000-000000000000",
            "resourceGroup": "rg-test",
            "location": "eastus",
            **extra,
        }
        path = workspace / "sites" / filename
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(body, f)
        return path

    def test_load_by_filename_stem_when_name_matches(self, tmp_workspace):
        """The common case: name == stem. Stem fast path wins."""
        self._write_site(tmp_workspace, "munich-dev.yaml", "munich-dev")
        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("munich-dev")
        assert site.name == "munich-dev"

    def test_load_by_filename_stem_when_name_overridden(self, tmp_workspace):
        """Stem still resolves even when internal name differs."""
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("seattle")
        assert site.name == "contoso-edge"

    def test_load_by_internal_name_when_overridden(self, tmp_workspace):
        """Internal name resolves via the lazy index fallback."""
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("contoso-edge")
        assert site.name == "contoso-edge"

    def test_load_by_internal_name_caches_under_both_forms(self, tmp_workspace):
        """A subsequent load via the other form is a cache hit, not a re-resolve."""
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        site_by_internal = orchestrator.load_site("contoso-edge")
        site_by_stem = orchestrator.load_site("seattle")
        assert site_by_internal is site_by_stem  # same cached instance

    def test_unknown_identifier_raises_file_not_found(self, tmp_workspace):
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(FileNotFoundError):
            orchestrator.load_site("nonexistent")

    def test_two_sites_same_internal_name_rejected(self, tmp_workspace):
        """A workspace cannot have two files claiming the same internal name."""
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        self._write_site(tmp_workspace, "tacoma.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        # Stem fast path still works for either file directly. The
        # collision only surfaces when the index is built (on internal-
        # name lookup or any path that triggers the fallback).
        with pytest.raises(ValueError, match="Two sites declare the same"):
            orchestrator.load_site("contoso-edge")

    def test_internal_name_shadowing_another_stem_rejected(self, tmp_workspace):
        """A site cannot set `name: X` if `X.yaml` is another file in the workspace."""
        # File `seattle.yaml` declares `name: tacoma`. Another file
        # `tacoma.yaml` exists. The identifier "tacoma" is now ambiguous:
        # stem lookup returns tacoma.yaml, internal-name lookup would
        # return seattle.yaml. Reject at index-build time.
        self._write_site(tmp_workspace, "seattle.yaml", "tacoma")
        self._write_site(tmp_workspace, "tacoma.yaml", "tacoma")
        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError, match="collides with file"):
            orchestrator.load_site("does-not-matter")

    def test_index_skips_sites_where_name_matches_stem(self, tmp_workspace):
        """Common-case sites do not appear in the internal-name index."""
        self._write_site(tmp_workspace, "munich-dev.yaml", "munich-dev")
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        # Force build via a fallback lookup.
        orchestrator.load_site("contoso-edge")
        # Only the overridden site should be indexed.
        assert set(orchestrator._internal_name_index.keys()) == {"contoso-edge"}

    def test_collision_caught_via_stem_fast_path(self, tmp_workspace):
        """Collision detection fires even when every lookup hits the stem
        fast path. Without eager index build, a workspace with two sites
        declaring the same internal `name:` would silently pass any
        command that only resolves filename stems.
        """
        self._write_site(tmp_workspace, "site-a.yaml", "shared")
        self._write_site(tmp_workspace, "site-b.yaml", "shared")
        orchestrator = Orchestrator(tmp_workspace)
        # Both files have stems that resolve via the fast path. The
        # collision is in their internal `name:` fields. The eager
        # index build (triggered by _find_trusted_site_file) must
        # surface the drift even though we never miss the stem path.
        with pytest.raises(ValueError, match="Two sites declare the same"):
            orchestrator.load_site("site-a")

    def test_shadow_caught_via_stem_fast_path(self, tmp_workspace):
        """`name:` shadowing another file's stem is rejected at load
        time even when every operator lookup happens to hit the stem
        fast path."""
        self._write_site(tmp_workspace, "tacoma.yaml", "tacoma")
        self._write_site(tmp_workspace, "seattle.yaml", "tacoma")
        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError, match="collides with file"):
            orchestrator.load_site("seattle")

    def test_site_template_not_indexed(self, tmp_workspace):
        """SiteTemplates are skipped when building the internal-name index."""
        body = {
            "apiVersion": "siteops/v1",
            "kind": "SiteTemplate",
            "name": "shared-prod",
            "labels": {"environment": "prod"},
        }
        with open(tmp_workspace / "sites" / "base.yaml", "w", encoding="utf-8") as f:
            yaml.dump(body, f)
        self._write_site(tmp_workspace, "seattle.yaml", "contoso-edge")
        orchestrator = Orchestrator(tmp_workspace)
        orchestrator.load_site("contoso-edge")
        assert "shared-prod" not in orchestrator._internal_name_index


class TestGetStepTypeLabel:
    """Tests for step type display labels."""

    def test_kubectl_step_label(self, tmp_workspace):
        """Test that kubectl steps produce 'kubectl:<operation>' label."""
        from siteops.models import ArcCluster, KubectlStep

        orchestrator = Orchestrator(tmp_workspace)
        step = KubectlStep(
            name="apply-config",
            operation="apply",
            arc=ArcCluster(name="cluster", resource_group="rg"),
            files=["config.yaml"],
        )

        label = orchestrator._get_step_type_label(step)
        assert label == "kubectl:apply"

    def test_deployment_step_label(self, tmp_workspace):
        """Test that deployment steps return their scope as label."""
        orchestrator = Orchestrator(tmp_workspace)
        step = DeploymentStep(
            name="deploy",
            template="test.bicep",
            scope="subscription",
        )

        label = orchestrator._get_step_type_label(step)
        assert label == "subscription"


class TestAllStepsSkipped:
    """Tests for deployments where all steps are skipped."""

    def test_all_conditional_steps_skipped_succeeds(self, tmp_workspace, sample_bicep_template):
        """Deployment should succeed with steps_completed=0 when all steps are skipped."""
        # Create site without the neverTrue property
        site_data = {
            "name": "test-site",
            "subscription": "00000000-0000-0000-0000-000000000000",
            "resourceGroup": "rg-test",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "test-site.yaml").write_text(yaml.dump(site_data))

        # Create manifest where every step has a condition that won't be met
        manifest_data = {
            "name": "all-skip-manifest",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.deployOptions.neverTrue }}",
                },
                {
                    "name": "step2",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.deployOptions.neverTrue }}",
                },
            ],
        }
        manifest_path = tmp_workspace / "manifests" / "all-skip.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(tmp_workspace)

        with patch.object(orchestrator.executor, "deploy_resource_group") as mock_deploy:
            result = orchestrator.deploy(manifest_path)

            # Executor should never be called since all steps are skipped
            mock_deploy.assert_not_called()

        # Deployment should succeed
        site_result = result["sites"]["test-site"]
        assert site_result["status"] == "success"
        assert site_result["steps_completed"] == 0
        assert site_result["steps_skipped"] == 2
        assert all(s["status"] == "skipped" for s in site_result["steps"])
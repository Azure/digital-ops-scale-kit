"""Tests that site inheritance resolves correctly and consistently."""

from collections import defaultdict
from pathlib import Path

import yaml

from siteops.orchestrator import Orchestrator


# All deployOptions defined in base-site.yaml
EXPECTED_DEPLOY_OPTIONS = {
    "enableGlobalSite",
    "enableEdgeSite",
    "enableSecretSync",
    "enableCertManager",
}


class TestSiteInheritanceResolution:
    """Every site should load cleanly with complete inherited configuration."""

    def _get_site_names(self, workspace: Path) -> list[str]:
        """Get all Site (not SiteTemplate) names from the workspace."""
        sites_dir = workspace / "sites"
        names = []
        for f in sorted(sites_dir.glob("*.yaml")):
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and data.get("kind") != "SiteTemplate":
                names.append(data.get("name", f.stem))
        return names

    def test_all_sites_load(self, workspace, orchestrator):
        """Every Site file should load without errors."""
        site_names = self._get_site_names(workspace)
        assert len(site_names) >= 1, "No sites found"

        for name in site_names:
            site = orchestrator.load_site(name)
            assert site.name == name
            assert site.subscription, f"{name}: missing subscription"
            assert site.location, f"{name}: missing location"

    def test_all_sites_have_complete_deploy_options(self, workspace, orchestrator):
        """Every site should inherit all deployOptions from base-site.yaml."""
        site_names = self._get_site_names(workspace)

        for name in site_names:
            site = orchestrator.load_site(name)
            deploy_options = site.properties.get("deployOptions", {})
            actual_keys = set(deploy_options.keys())
            missing = EXPECTED_DEPLOY_OPTIONS - actual_keys
            assert missing == set(), (
                f"{name}: missing deployOptions keys after inheritance: {missing}"
            )

    def test_base_site_defines_all_deploy_options(self, workspace):
        """base-site.yaml should define every expected deployOptions key."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        deploy_options = data.get("properties", {}).get("deployOptions", {})
        actual_keys = set(deploy_options.keys())
        missing = EXPECTED_DEPLOY_OPTIONS - actual_keys
        assert missing == set(), (
            f"base-site.yaml missing deployOptions keys: {missing}"
        )

    def test_shared_templates_inherit_base(self, workspace):
        """All shared SiteTemplates should inherit from base-site.yaml."""
        shared_dir = workspace / "sites" / "shared"
        if not shared_dir.is_dir():
            return

        for f in sorted(shared_dir.glob("*.yaml")):
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            inherits = data.get("inherits", "")
            assert "base-site" in inherits, (
                f"shared/{f.name} does not inherit from base-site.yaml: inherits={inherits}"
            )

    def test_no_site_has_placeholder_subscription(self, workspace, orchestrator):
        """Sites should not have obviously placeholder subscription IDs."""
        site_names = self._get_site_names(workspace)

        for name in site_names:
            site = orchestrator.load_site(name)
            assert site.subscription != "", f"{name}: empty subscription"
            # Allow the 00000000 placeholder since committed sites use it
            # (real values come from sites.local/ overlays)


class TestSiteInvariants:
    """Fleet-level invariants that catch real configuration mistakes early."""

    def _get_sites(self, workspace: Path, orchestrator: Orchestrator):
        """Yield (name, site) for every committed Site (not SiteTemplate)."""
        sites_dir = workspace / "sites"
        for f in sorted(sites_dir.glob("*.yaml")):
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if not data or data.get("kind") == "SiteTemplate":
                continue
            name = data.get("name", f.stem)
            yield name, orchestrator.load_site(name)

    def test_no_two_sites_share_subscription_and_resource_group(self, workspace, orchestrator):
        """Two sites with the same (subscription, resourceGroup) would step on each other."""
        seen: dict[tuple[str, str], list[str]] = defaultdict(list)
        for name, site in self._get_sites(workspace, orchestrator):
            if not site.resource_group:
                continue  # subscription-scoped site, no RG
            key = (site.subscription, site.resource_group)
            seen[key].append(name)

        collisions = {k: v for k, v in seen.items() if len(v) > 1}
        assert not collisions, (
            f"Multiple sites share the same (subscription, resourceGroup): {dict(collisions)}. "
            f"Each site must own a distinct resource group within its subscription."
        )

    def test_labels_are_strings(self, workspace, orchestrator):
        """Site labels must be string-valued. Selector parsing assumes it."""
        for name, site in self._get_sites(workspace, orchestrator):
            for key, value in site.labels.items():
                assert isinstance(value, str), (
                    f"{name}: label '{key}' is {type(value).__name__} ({value!r}); "
                    f"labels must be strings (selector parsing breaks on non-strings)."
                )

    def test_subscription_scoped_sites_carry_scope_label(self, workspace, orchestrator):
        """Subscription-level sites (no resourceGroup) should carry scope=subscription
        so manifests can target them with `selector: scope=subscription`.
        """
        for name, site in self._get_sites(workspace, orchestrator):
            if site.resource_group:
                continue  # RG-scoped site
            scope_label = site.labels.get("scope")
            assert scope_label == "subscription", (
                f"{name}: subscription-scoped site (no resourceGroup) is missing "
                f"`labels.scope: subscription` (got {scope_label!r}). Without this "
                f"label the site cannot be targeted by manifests using "
                f"`selector: scope=subscription`."
            )

    def test_e2e_fallback_inherits_resolves(self, tmp_path, workspace):
        """A site file in an extras dir with `inherits: base-site.yaml` should
        resolve via the workspace fallback (this is what the e2e workflow
        relies on when the rendered site lives in a tmp dir)."""
        (tmp_path / "rendered").mkdir()
        site_file = tmp_path / "rendered" / "fallback-test-site.yaml"
        site_file.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Site\n"
            "name: fallback-test-site\n"
            "inherits: base-site.yaml\n"
            "subscription: '00000000-0000-0000-0000-000000000000'\n"
            "resourceGroup: rg-fallback\n"
            "location: eastus\n"
        )

        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(workspace, extra_trusted_sites_dirs=[tmp_path / "rendered"])
        site = orch.load_site("fallback-test-site")
        assert site.name == "fallback-test-site"
        # The base inherits should have been applied: aioRelease comes from base-site.yaml.
        assert site.properties.get("aioRelease") == "2603"

    def test_extras_dir_overlays_workspace_site_with_same_name(self, tmp_path, workspace):
        """When an extras dir contains a site file with the same name as one
        in `sites/`, the extras dir version overlays the base: fields declared
        in the overlay win on conflict.

        Used by E2E and per-deployment override workflows: a rendered site
        in a tmp dir adjusts the workspace's checked-in version for the
        duration of the run. `inherits:` on the overlay is stripped (the
        base's inheritance chain is preserved).
        """
        from siteops.orchestrator import Orchestrator as Orch

        # Use a known RG-scoped site as the baseline.
        site_name = "chicago-staging"
        baseline = Orch(workspace).load_site(site_name)
        assert baseline.location, f"Baseline {site_name} missing location"

        # Author an extras-dir overlay with a marker location.
        (tmp_path / "rendered").mkdir()
        overlay = tmp_path / "rendered" / f"{site_name}.yaml"
        overlay.write_text(
            "apiVersion: siteops/v1\n"
            "kind: Site\n"
            f"name: {site_name}\n"
            "location: westus2-overlay-marker\n"
        )

        orch = Orch(workspace, extra_trusted_sites_dirs=[tmp_path / "rendered"])
        loaded = orch.load_site(site_name)
        assert loaded.location == "westus2-overlay-marker", (
            f"Extras-dir overlay for {site_name} did not take precedence "
            f"(got location={loaded.location!r}, expected the marker)."
        )
        # Inheritance chain is preserved (aioRelease comes from base-site.yaml
        # via the workspace site's inherits: base-site.yaml).
        assert loaded.properties.get("aioRelease") == baseline.properties.get("aioRelease")

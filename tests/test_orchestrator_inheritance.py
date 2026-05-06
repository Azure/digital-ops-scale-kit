"""Tests for site inheritance and SiteTemplate handling.

Covers:
- Basic site inheritance
- Chained inheritance
- Circular inheritance detection
- SiteTemplate exclusion from site discovery
- Local overlay interaction with inheritance
"""

import pytest
import yaml

from siteops.orchestrator import Orchestrator


class TestSiteInheritance:
    """Tests for site inheritance via the `inherits` field."""

    def test_basic_inheritance(self, tmp_workspace):
        """Site should inherit values from parent template."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()
        base_template = {
            "apiVersion": "siteops/v1",
            "kind": "SiteTemplate",
            "subscription": "inherited-sub",
            "labels": {"team": "platform", "managedBy": "siteops"},
            "properties": {"mqtt": {"qos": 1}},
        }
        (shared_dir / "base-site.yaml").write_text(yaml.dump(base_template))

        site = {
            "apiVersion": "siteops/v1",
            "kind": "Site",
            "inherits": "../shared/base-site.yaml",
            "name": "dev-eastus",
            "location": "eastus",
            "resourceGroup": "dev-eastus-rg",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites" / "dev-eastus.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("dev-eastus")

        # Inherited values
        assert loaded.subscription == "inherited-sub"
        assert loaded.properties["mqtt"]["qos"] == 1
        # Merged labels
        assert loaded.labels["team"] == "platform"
        assert loaded.labels["managedBy"] == "siteops"
        assert loaded.labels["environment"] == "dev"
        # Site-specific values
        assert loaded.name == "dev-eastus"
        assert loaded.location == "eastus"
        assert loaded.resource_group == "dev-eastus-rg"

    def test_inheritance_with_override(self, tmp_workspace):
        """Site values should override inherited values."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()
        base_template = {
            "subscription": "base-sub",
            "location": "westus",
            "labels": {"environment": "base"},
        }
        (shared_dir / "base.yaml").write_text(yaml.dump(base_template))

        site = {
            "inherits": "../shared/base.yaml",
            "name": "override-test",
            "location": "eastus",  # Override inherited location
            "labels": {"environment": "dev"},  # Override inherited label
        }
        (tmp_workspace / "sites" / "override-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("override-test")

        assert loaded.subscription == "base-sub"  # Inherited
        assert loaded.location == "eastus"  # Overridden
        assert loaded.labels["environment"] == "dev"  # Overridden

    def test_chained_inheritance(self, tmp_workspace):
        """Sites should support chained inheritance (A inherits B inherits C)."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        # Grandparent template
        grandparent = {
            "kind": "SiteTemplate",
            "subscription": "org-sub",
            "labels": {"org": "contoso"},
        }
        (shared_dir / "org-base.yaml").write_text(yaml.dump(grandparent))

        # Parent template that inherits from grandparent
        parent = {
            "kind": "SiteTemplate",
            "inherits": "org-base.yaml",
            "labels": {"team": "platform"},
            "properties": {"tier": "standard"},
        }
        (shared_dir / "team-base.yaml").write_text(yaml.dump(parent))

        # Site that inherits from parent
        site = {
            "inherits": "../shared/team-base.yaml",
            "name": "chained-test",
            "location": "eastus",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites" / "chained-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("chained-test")

        # From grandparent
        assert loaded.subscription == "org-sub"
        assert loaded.labels["org"] == "contoso"
        # From parent
        assert loaded.labels["team"] == "platform"
        assert loaded.properties["tier"] == "standard"
        # From site
        assert loaded.labels["environment"] == "dev"
        assert loaded.location == "eastus"

    def test_inheritance_with_local_overlay(self, tmp_workspace):
        """Local overlay should apply after inheritance."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()
        base_template = {
            "subscription": "template-sub",
            "labels": {"team": "platform"},
        }
        (shared_dir / "base.yaml").write_text(yaml.dump(base_template))

        # Base site with inheritance
        site = {
            "inherits": "../shared/base.yaml",
            "name": "overlay-inherit-test",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "overlay-inherit-test.yaml").write_text(yaml.dump(site))

        # Local overlay (should override inherited values)
        (tmp_workspace / "sites.local").mkdir()
        local = {"subscription": "local-sub"}
        (tmp_workspace / "sites.local" / "overlay-inherit-test.yaml").write_text(yaml.dump(local))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("overlay-inherit-test")

        # Local overlay wins over inheritance
        assert loaded.subscription == "local-sub"
        # Inherited labels preserved
        assert loaded.labels["team"] == "platform"

    def test_circular_inheritance_detection(self, tmp_workspace):
        """Circular inheritance should raise ValueError."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        # A inherits B
        a = {"inherits": "b.yaml", "location": "eastus"}
        (shared_dir / "a.yaml").write_text(yaml.dump(a))

        # B inherits A (circular!)
        b = {"inherits": "a.yaml", "subscription": "sub"}
        (shared_dir / "b.yaml").write_text(yaml.dump(b))

        # Site inherits A
        site = {
            "inherits": "../shared/a.yaml",
            "name": "circular-test",
            "subscription": "sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "circular-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError, match="Circular inheritance detected"):
            orchestrator.load_site("circular-test")

    def test_self_inheritance_detection(self, tmp_workspace):
        """Self-referencing inheritance should raise ValueError."""
        site = {
            "inherits": "self-ref.yaml",  # References itself
            "name": "self-ref",
            "subscription": "sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "self-ref.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError, match="Circular inheritance detected"):
            orchestrator.load_site("self-ref")

    def test_missing_inherited_file(self, tmp_workspace):
        """Missing inherited file should raise FileNotFoundError."""
        site = {
            "inherits": "../shared/nonexistent.yaml",
            "name": "missing-parent",
            "subscription": "sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "missing-parent.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(FileNotFoundError, match="Inherited file not found"):
            orchestrator.load_site("missing-parent")

    def test_invalid_kind_inheritance(self, tmp_workspace):
        """Inheriting from invalid kind should raise ValueError."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        # Create a Manifest (invalid for inheritance)
        manifest = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "not-a-template",
        }
        (shared_dir / "manifest.yaml").write_text(yaml.dump(manifest))

        site = {
            "inherits": "../shared/manifest.yaml",
            "name": "invalid-inherit",
            "subscription": "sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "invalid-inherit.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError):
            orchestrator.load_site("invalid-inherit")

    def test_no_inheritance(self, tmp_workspace):
        """Sites without inherits should work normally."""
        site = {
            "name": "no-inherit",
            "subscription": "direct-sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "no-inherit.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("no-inherit")

        assert loaded.subscription == "direct-sub"
        assert loaded.location == "eastus"

    def test_inherit_from_site_kind_rejected(self, tmp_workspace):
        """Inheriting from kind: Site is rejected. Inherits parents must be SiteTemplate.

        A `kind: Site` parent would chain deployable sites where editing one
        silently changes the other. Use SiteTemplate for any reusable base.
        """
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        # Base site (deliberately the wrong kind for inheritance)
        base_site = {
            "kind": "Site",
            "subscription": "shared-sub",
            "location": "westus",
            "labels": {"shared": "true"},
        }
        (shared_dir / "base-site.yaml").write_text(yaml.dump(base_site))

        site = {
            "inherits": "../shared/base-site.yaml",
            "name": "inherit-from-site",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "inherit-from-site.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        with pytest.raises(ValueError, match="must be SiteTemplate"):
            orchestrator.load_site("inherit-from-site")

    def test_local_overlay_cannot_add_inheritance(self, tmp_workspace):
        """Local overlay should not be able to add inheritance to a site that doesn't have it."""
        # Base site without inheritance
        site = {
            "name": "no-inherit-site",
            "subscription": "base-sub",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "no-inherit-site.yaml").write_text(yaml.dump(site))

        # Create a template to try to inherit from
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()
        template = {
            "kind": "SiteTemplate",
            "subscription": "template-sub",
            "labels": {"from": "template"},
        }
        (shared_dir / "base.yaml").write_text(yaml.dump(template))

        # Local overlay trying to add inheritance (should be ignored or error)
        (tmp_workspace / "sites.local").mkdir()
        local = {
            "inherits": "../shared/base.yaml",
            "location": "westus",
        }
        (tmp_workspace / "sites.local" / "no-inherit-site.yaml").write_text(yaml.dump(local))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("no-inherit-site")

        # The local overlay's location override should apply
        assert loaded.location == "westus"
        # But inheritance should NOT be applied from local overlay
        # (subscription should remain from base site, not template)
        assert loaded.subscription == "base-sub"
        assert loaded.labels.get("from") != "template"

    def test_deep_merge_properties_with_inheritance(self, tmp_workspace):
        """Properties should be deep merged through inheritance chain."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        # Base template with nested properties
        base_template = {
            "kind": "SiteTemplate",
            "subscription": "inherited-sub",
            "properties": {
                "mqtt": {"broker": "mqtt://base:1883", "qos": 1},
                "baseOnly": "from-base",
            },
        }
        (shared_dir / "base.yaml").write_text(yaml.dump(base_template))

        # Site that inherits and adds/overrides properties
        site = {
            "inherits": "../shared/base.yaml",
            "name": "props-inherit-test",
            "location": "eastus",
            "properties": {
                "mqtt": {"broker": "mqtt://site:1883", "clientId": "site-client"},
                "siteOnly": "from-site",
            },
        }
        (tmp_workspace / "sites" / "props-inherit-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("props-inherit-test")

        # Verify deep merge behavior
        assert loaded.properties["mqtt"]["broker"] == "mqtt://site:1883"  # Overridden
        assert loaded.properties["mqtt"]["qos"] == 1  # Preserved from base
        assert loaded.properties["mqtt"]["clientId"] == "site-client"  # Added by site
        assert loaded.properties["baseOnly"] == "from-base"  # Preserved from base
        assert loaded.properties["siteOnly"] == "from-site"  # Added by site

    def test_inheritance_preserves_sibling_deploy_options(self, tmp_workspace):
        """Child overriding one deployOptions key should preserve siblings from parent."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        parent_template = {
            "kind": "SiteTemplate",
            "subscription": "inherited-sub",
            "properties": {
                "deployOptions": {"enableSecretSync": False, "includeSolution": True},
            },
        }
        (shared_dir / "parent.yaml").write_text(yaml.dump(parent_template))

        site = {
            "inherits": "../shared/parent.yaml",
            "name": "deploy-opts-test",
            "location": "eastus",
            "properties": {
                "deployOptions": {"enableSecretSync": True},
            },
        }
        (tmp_workspace / "sites" / "deploy-opts-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("deploy-opts-test")

        assert loaded.properties["deployOptions"]["enableSecretSync"] is True  # Overridden
        assert loaded.properties["deployOptions"]["includeSolution"] is True  # Preserved from parent

    def test_three_level_inheritance_preserves_deep_properties(self, tmp_workspace):
        """Properties from all three levels should be preserved through deep merge."""
        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()

        grandparent = {
            "kind": "SiteTemplate",
            "subscription": "org-sub",
            "properties": {
                "deployOptions": {"enableSecretSync": False},
            },
        }
        (shared_dir / "grandparent.yaml").write_text(yaml.dump(grandparent))

        parent = {
            "kind": "SiteTemplate",
            "inherits": "grandparent.yaml",
            "properties": {
                "deployOptions": {"includeSolution": True},
            },
        }
        (shared_dir / "parent.yaml").write_text(yaml.dump(parent))

        site = {
            "inherits": "../shared/parent.yaml",
            "name": "three-level-test",
            "location": "eastus",
            "properties": {
                "deployOptions": {"enableOpcPlcSimulator": True},
            },
        }
        (tmp_workspace / "sites" / "three-level-test.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        loaded = orchestrator.load_site("three-level-test")

        assert loaded.properties["deployOptions"]["enableSecretSync"] is False  # From grandparent
        assert loaded.properties["deployOptions"]["includeSolution"] is True  # From parent
        assert loaded.properties["deployOptions"]["enableOpcPlcSimulator"] is True  # From site

    def test_inherited_template_parsed_once_for_n_sites(self, tmp_workspace):
        """N sites that inherit from one template parse the template once.

        Without the per-orchestrator memo, `_load_inherited_data` re-reads
        and re-parses the same SiteTemplate file for every site that
        inherits it. This regresses load time on workspaces with many
        sites sharing a base template.
        """
        from unittest.mock import patch

        shared_dir = tmp_workspace / "shared"
        shared_dir.mkdir()
        base_template = {
            "apiVersion": "siteops/v1",
            "kind": "SiteTemplate",
            "subscription": "inherited-sub",
            "labels": {"managedBy": "siteops"},
        }
        (shared_dir / "base.yaml").write_text(yaml.dump(base_template))

        for i in range(5):
            site = {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "inherits": "../shared/base.yaml",
                "name": f"site-{i}",
                "resourceGroup": f"rg-{i}",
                "location": "eastus",
            }
            (tmp_workspace / "sites" / f"site-{i}.yaml").write_text(yaml.dump(site))

        orchestrator = Orchestrator(tmp_workspace)
        original_safe_load = yaml.safe_load
        with patch("siteops.orchestrator.yaml.safe_load", side_effect=original_safe_load) as m:
            for i in range(5):
                orchestrator.load_site(f"site-{i}")

        # Each site file is parsed once; the shared template is parsed
        # once total despite being inherited 5 times.
        base_path = (shared_dir / "base.yaml").resolve()
        # safe_load gets file objects, not paths. Count opens of the
        # template path instead via the cache state.
        assert base_path in orchestrator._inherited_data_cache


class TestSiteTemplateExclusion:
    """Tests for SiteTemplate handling in site discovery and loading."""

    def test_get_all_site_names_excludes_site_templates(self, tmp_workspace):
        """SiteTemplate files should be excluded from site discovery."""
        # Create a regular site
        (tmp_workspace / "sites" / "prod-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: prod-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-prod
location: eastus
"""
        )

        # Create a site template (should be excluded)
        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site
parameters:
  brokerConfig:
    memoryProfile: Medium
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        site_names = orchestrator._get_all_site_names()

        assert "prod-site" in site_names
        assert "base-site" not in site_names

    def test_load_site_rejects_site_template(self, tmp_workspace):
        """Attempting to load a SiteTemplate as a Site should raise ValueError."""
        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site
"""
        )

        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match="SiteTemplate.*cannot be deployed"):
            orchestrator.load_site("base-site")

    def test_load_all_sites_excludes_templates(self, tmp_workspace):
        """load_all_sites should not include SiteTemplate files."""
        (tmp_workspace / "sites" / "dev-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: dev-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-dev
location: eastus
"""
        )

        (tmp_workspace / "sites" / "prod-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: prod-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-prod
location: westus
"""
        )

        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        sites = orchestrator.load_all_sites()

        site_names = {s.name for s in sites}
        assert site_names == {"dev-site", "prod-site"}
        assert "base-site" not in site_names

    def test_is_site_template_returns_false_on_parse_error(self, tmp_workspace):
        """_is_site_template should return False for unparseable files."""
        (tmp_workspace / "sites" / "bad-file.yaml").write_text("this is: not: valid: yaml: {{{")

        orchestrator = Orchestrator(tmp_workspace)
        result = orchestrator._is_site_template(tmp_workspace / "sites" / "bad-file.yaml")

        assert result is False

    def test_site_inherits_from_site_template(self, tmp_workspace):
        """Sites should successfully inherit from SiteTemplates."""
        # Create template
        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site
parameters:
  brokerConfig:
    memoryProfile: Medium
    replicas: 2
"""
        )

        # Create site that inherits
        (tmp_workspace / "sites" / "prod-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: prod-site
inherits: base-site.yaml
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-prod
location: westus
parameters:
  brokerConfig:
    replicas: 4
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("prod-site")

        assert site.name == "prod-site"
        assert site.parameters["brokerConfig"]["memoryProfile"] == "Medium"  # inherited
        assert site.parameters["brokerConfig"]["replicas"] == 4  # overridden

    def test_site_template_with_labels_inherited(self, tmp_workspace):
        """SiteTemplate labels should be inherited by child sites."""
        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site
labels:
  managedBy: siteops
  platform: azure-iot-operations
"""
        )

        (tmp_workspace / "sites" / "dev-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: dev-site
inherits: base-site.yaml
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-dev
location: eastus
labels:
  environment: dev
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("dev-site")

        # Inherited labels
        assert site.labels["managedBy"] == "siteops"
        assert site.labels["platform"] == "azure-iot-operations"
        # Site-specific labels
        assert site.labels["environment"] == "dev"

class TestSiteProvenance:
    """Tests for `load_site_with_provenance` per-key origin tracking."""

    def _write_yaml(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml
        with open(path, "w", encoding="utf-8") as f:
            _yaml.dump(body, f)

    def test_inherits_chain_attributes_each_leaf(self, tmp_workspace):
        """Each leaf points to the file in the chain that supplied the value."""
        self._write_yaml(
            tmp_workspace / "sites" / "shared" / "base.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "SiteTemplate",
                "name": "base",
                "subscription": "default-sub",
                "labels": {"managedBy": "siteops", "team": "platform"},
                "properties": {"deployOptions": {"a": True, "b": True}},
            },
        )
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "munich",
                "inherits": "shared/base.yaml",
                "resourceGroup": "rg-munich",
                "location": "eastus",
                "labels": {"environment": "dev"},
                "properties": {"deployOptions": {"b": False}},
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        _, prov = orch.load_site_with_provenance("munich")
        assert prov["subscription"].endswith("base.yaml")
        assert prov["resourceGroup"].endswith("munich.yaml")
        assert prov["location"].endswith("munich.yaml")
        assert prov["labels.managedBy"].endswith("base.yaml")
        assert prov["labels.team"].endswith("base.yaml")
        assert prov["labels.environment"].endswith("munich.yaml")
        assert prov["properties.deployOptions.a"].endswith("base.yaml")
        # Override path: child wins on b but inherited a stays attributed to base.
        assert prov["properties.deployOptions.b"].endswith("munich.yaml")

    def test_overlay_overrides_attributed_to_overlay(self, tmp_workspace):
        """`sites.local/<name>.yaml` overlays attribute the leaves it touches."""
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "munich",
                "subscription": "trusted-sub",
                "resourceGroup": "rg-trusted",
                "location": "eastus",
            },
        )
        self._write_yaml(
            tmp_workspace / "sites.local" / "munich.yaml",
            {
                "subscription": "overlay-sub",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        _, prov = orch.load_site_with_provenance("munich")
        assert prov["subscription"].endswith("sites.local/munich.yaml")
        assert prov["resourceGroup"].endswith("sites/munich.yaml")
        assert prov["location"].endswith("sites/munich.yaml")

    def test_load_with_provenance_returns_same_site_as_load_site(self, tmp_workspace):
        """The returned Site matches what `load_site` would return."""
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "munich",
                "subscription": "sub",
                "resourceGroup": "rg",
                "location": "eastus",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        site_via_load = orch.load_site("munich")
        site_via_prov, _ = orch.load_site_with_provenance("munich")
        assert site_via_prov.name == site_via_load.name
        assert site_via_prov.subscription == site_via_load.subscription

    def test_provenance_for_k8s_envelope_site(self, tmp_workspace):
        """Sites authored with the K8s envelope (`metadata:`/`spec:`)
        get prov keys in the flat-shape view so display lookups
        succeed regardless of on-disk shape."""
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "metadata": {
                    "name": "munich",
                    "labels": {"environment": "dev"},
                },
                "spec": {
                    "subscription": "envelope-sub",
                    "resourceGroup": "rg-envelope",
                    "location": "eastus",
                    "properties": {
                        "deployOptions": {"enableSecretSync": True},
                    },
                },
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        _, prov = orch.load_site_with_provenance("munich")
        # Flat-shape lookups must succeed even though the on-disk file
        # used `spec:` and `metadata:` envelopes.
        assert prov["subscription"].endswith("munich.yaml")
        assert prov["resourceGroup"].endswith("munich.yaml")
        assert prov["location"].endswith("munich.yaml")
        assert prov["labels.environment"].endswith("munich.yaml")
        assert prov["properties.deployOptions.enableSecretSync"].endswith("munich.yaml")
        # Envelope-only keys must NOT leak through.
        assert "spec" not in prov
        assert "metadata" not in prov
        assert "spec.subscription" not in prov

    def test_overlay_renaming_site_rejected(self, tmp_workspace):
        """Overlays in `sites.local/` cannot rename the site. Identity
        is set by the base file and the workspace indexes are built
        from base files, so an overlay rename produces a site
        unfindable through any index."""
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "munich",
                "subscription": "trusted-sub",
                "resourceGroup": "rg-trusted",
                "location": "eastus",
            },
        )
        self._write_yaml(
            tmp_workspace / "sites.local" / "munich.yaml",
            {
                "name": "munich-renamed",
                "subscription": "overlay-sub",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        import pytest as _pytest
        with _pytest.raises(ValueError, match="cannot rename the site"):
            orch.load_site("munich")

    def test_overlay_restating_same_name_allowed(self, tmp_workspace):
        """Overlays may restate the site's existing name (common when
        an overlay file mirrors the full base shape)."""
        self._write_yaml(
            tmp_workspace / "sites" / "munich.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": "munich",
                "subscription": "trusted-sub",
                "resourceGroup": "rg-trusted",
                "location": "eastus",
            },
        )
        self._write_yaml(
            tmp_workspace / "sites.local" / "munich.yaml",
            {
                "name": "munich",  # Same as base. Allowed.
                "subscription": "overlay-sub",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        site = orch.load_site("munich")
        assert site.name == "munich"
        assert site.subscription == "overlay-sub"

    def test_overlay_renames_site_when_base_omits_name(self, tmp_workspace):
        """When the base file relies on the basename default and the
        overlay introduces a different `name:`, the rejection must
        still fire. Without this, the overlay would set `Site.name` to
        a value not found in any workspace index."""
        # Base file has NO explicit `name:`. Identity defaults to the
        # basename ("mysite").
        self._write_yaml(
            tmp_workspace / "sites" / "mysite.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "subscription": "trusted-sub",
                "resourceGroup": "rg-trusted",
                "location": "eastus",
            },
        )
        # Overlay introduces a different name.
        self._write_yaml(
            tmp_workspace / "sites.local" / "mysite.yaml",
            {
                "name": "renamed",
                "subscription": "overlay-sub",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        import pytest as _pytest
        with _pytest.raises(ValueError, match="cannot rename the site"):
            orch.load_site("mysite")

    def test_overlay_restating_basename_default_allowed(self, tmp_workspace):
        """When the base file omits `name:`, an overlay may still
        restate the basename (the implicit identity)."""
        self._write_yaml(
            tmp_workspace / "sites" / "mysite.yaml",
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "subscription": "trusted-sub",
                "resourceGroup": "rg-trusted",
                "location": "eastus",
            },
        )
        self._write_yaml(
            tmp_workspace / "sites.local" / "mysite.yaml",
            {
                "name": "mysite",  # Matches the basename default. Allowed.
                "subscription": "overlay-sub",
            },
        )
        from siteops.orchestrator import Orchestrator
        orch = Orchestrator(tmp_workspace)
        site = orch.load_site("mysite")
        assert site.name == "mysite"
        assert site.subscription == "overlay-sub"

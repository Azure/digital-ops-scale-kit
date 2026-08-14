"""Tests that site inheritance resolves correctly and consistently."""

import difflib
import re
from collections import defaultdict
from pathlib import Path

import yaml

from siteops.orchestrator import Orchestrator

# Every deployOptions key base-site.yaml defines. Kept in step with the site by
# test_base_site_defines_all_deploy_options, which compares both directions.
EXPECTED_DEPLOY_OPTIONS = {
    "enableGlobalSite",
    "enableEdgeSite",
    "enableSecretSync",
    "enableCertManager",
    "enableWorkloadIdentity",
    "allowKubernetesMinorUpgrade",
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
        """base-site.yaml and the expected set must match exactly.

        Comparing both directions keeps the constant honest. A one-way check lets a
        newly shipped toggle go unguarded, which is how `enableWorkloadIdentity` and
        `allowKubernetesMinorUpgrade` shipped without inheritance coverage.
        """
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        deploy_options = data.get("properties", {}).get("deployOptions", {})
        actual_keys = set(deploy_options.keys())

        assert actual_keys == EXPECTED_DEPLOY_OPTIONS, (
            "base-site.yaml deployOptions and EXPECTED_DEPLOY_OPTIONS disagree. "
            f"missing from base-site: {sorted(EXPECTED_DEPLOY_OPTIONS - actual_keys)}; "
            f"unguarded in base-site: {sorted(actual_keys - EXPECTED_DEPLOY_OPTIONS)}"
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
        assert site.properties.get("aioRelease") == "2607"

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


class TestSiteKeysAreConsumed:
    """Every key a committed site declares is one the workspace reads.

    The site envelope is closed by `validate_site_keys`, but `properties` and
    `parameters` hold operator-defined content and stay open, so a typo inside
    them contributes nothing and the site deploys with defaults. That is the
    costly shape: `resorceSets` selects no family and `aioReleese` picks no
    release, both without a diagnostic.

    The expected set is derived from the workspace rather than hand-maintained,
    so it cannot go stale. A site key is consumed through one of two channels:

    1. An interpolation somewhere in the workspace, `{{ site.properties.X }}`
       or `{{ site.parameters.X }}`, including inside a path or a `when:`.
    2. A template parameter of the same name, which the engine auto-fills from
       site parameters without any interpolation. This channel is why the
       expected set cannot be read off the interpolations alone.

    Committed content only. A `sites.local/` overlay is gitignored and never
    reaches this test, so an operator's own keys are unaffected.
    """

    _REFERENCE = re.compile(r"site\.(properties|parameters)\.([A-Za-z0-9_.\[\]]+)")
    _TEMPLATE_PARAM = re.compile(r"^param\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)

    def _referenced_paths(self, workspace: Path) -> set[str]:
        """Dotted paths the workspace interpolates, outside the sites tree."""
        found: set[str] = set()
        for f in workspace.rglob("*"):
            if f.suffix not in (".yaml", ".yml", ".bicep") or "/sites/" in f.as_posix():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for kind, path in self._REFERENCE.findall(text):
                found.add(f"{kind}.{path.split('[')[0].rstrip('.')}")
        return found

    def _template_parameter_names(self, workspace: Path) -> set[str]:
        """Names every workspace template declares, for the auto-fill channel."""
        names: set[str] = set()
        for f in workspace.rglob("*.bicep"):
            names.update(
                self._TEMPLATE_PARAM.findall(f.read_text(encoding="utf-8", errors="replace"))
            )
        return names

    def _declared_paths(self, data: dict) -> set[str]:
        """Dotted paths a site file declares, to two levels."""
        found: set[str] = set()

        def walk(prefix: str, node, depth: int) -> None:
            if not isinstance(node, dict) or depth >= 2:
                return
            for key, value in node.items():
                found.add(f"{prefix}.{key}")
                walk(f"{prefix}.{key}", value, depth + 1)

        for container in ("properties", "parameters"):
            walk(container, data.get(container) or {}, 0)
        return found

    def _orphan_keys(self, workspace: Path) -> list[str]:
        """Site keys the workspace never reads, as reportable strings.

        The whole decision lives here, so a test pinning an edge of the rule
        runs this code rather than a copy of it that can drift from it.
        """
        referenced = self._referenced_paths(workspace)
        template_params = self._template_parameter_names(workspace)

        orphans: list[str] = []
        for site_file in sorted(workspace.glob("sites/**/*.yaml")):
            with open(site_file, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            for path in sorted(self._declared_paths(data)):
                if any(
                    ref == path or ref.startswith(path + ".") or path.startswith(ref + ".")
                    for ref in referenced
                ):
                    continue
                segments = path.split(".")
                # The auto-fill channel merges `site.parameters` into a step's
                # parameters and keeps what the template declares, so it
                # reaches `parameters` only. Applying it to `properties`
                # excused a key that nothing fills and nothing interpolates.
                #
                # A path below an auto-filled object parameter stays excused,
                # since the whole object is passed and this test cannot see
                # which keys the template's own schema uses.
                if (
                    len(segments) > 1
                    and segments[0] == "parameters"
                    and segments[1] in template_params
                ):
                    continue
                suggestion = difflib.get_close_matches(
                    segments[-1], sorted({r.split(".")[-1] for r in referenced} | template_params),
                    n=1, cutoff=0.7,
                )
                hint = f", did you mean `{suggestion[0]}`?" if suggestion else ""
                orphans.append(f"{site_file.name}: `{path}`{hint}")
        return orphans

    def test_no_committed_site_declares_a_key_nothing_reads(self, workspace):
        """A key no template and no interpolation reads is almost always a typo."""
        assert self._referenced_paths(workspace), (
            "Found no site references, so this test would pass vacuously"
        )
        assert self._template_parameter_names(workspace), (
            "Found no template parameters, so the auto-fill channel is unchecked"
        )

        orphans = self._orphan_keys(workspace)

        assert not orphans, (
            "A committed site declares a key nothing in the workspace reads, which "
            "contributes nothing at deploy and leaves the site on defaults. Fix the "
            "spelling, reference it from a manifest or parameter file, or add a "
            "template parameter of that name:\n  " + "\n  ".join(orphans)
        )


class TestDocumentedSiteContractMatchesTheEngine:
    """`docs/site-configuration.md` prints the allowed site keys.

    A published list goes stale the moment a key is added, and a reader has no
    way to tell. This drifted once already, when `description` was accepted
    and the documented error text still showed the old list.
    """

    def test_the_documented_allowed_keys_match_the_engine(self):
        from siteops.models import _SITE_FLAT_KNOWN_KEYS

        doc = (
            Path(__file__).parent.parent.parent / "docs" / "site-configuration.md"
        ).read_text(encoding="utf-8")

        match = re.search(r"Allowed: \[([^\]]+)\]", doc, re.S)
        assert match, (
            "docs/site-configuration.md no longer shows an `Allowed: [...]` "
            "list. If the example error was removed, remove this test with it."
        )

        documented = sorted(
            item.strip().strip("'")
            for item in match.group(1).replace("\n", " ").split(",")
        )

        assert documented == sorted(_SITE_FLAT_KNOWN_KEYS), (
            "The allowed site keys in docs/site-configuration.md no longer "
            "match the engine. Update the example error text."
        )


class TestTheConsumerCheckDoesNotExcuseTooMuch:
    """The auto-fill channel reaches `parameters`, and nothing else.

    Properties are read by interpolation, never merged into a step's
    parameters, so a template parameter of the same name says nothing about a
    properties key being read.

    Each case runs the real orphan check over a workspace built to isolate one
    edge of the rule. Restating the rule here instead would pin the restatement,
    and the committed workspace on its own cannot isolate these cases.
    """

    def _workspace(self, tmp_path: Path, container: str) -> Path:
        """A workspace whose only site declares `clusterName` under `container`.

        A template declares `clusterName`, so the auto-fill channel is live. An
        unrelated interpolation keeps the reference sweep non-empty, so a case
        cannot pass by finding nothing at all.
        """
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        (workspace / "templates" / "cluster.bicep").write_text(
            "param clusterName string\n", encoding="utf-8"
        )
        (workspace / "manifests" / "m.yaml").write_text(
            "name: m\n"
            "steps:\n"
            "  - name: s\n"
            "    template: templates/cluster.bicep\n"
            "    when: \"{{ site.properties.environment }} == 'dev'\"\n",
            encoding="utf-8",
        )
        (workspace / "sites" / "plant.yaml").write_text(
            yaml.dump(
                {
                    "name": "plant",
                    "subscription": "sub",
                    "location": "eastus",
                    container: {"clusterName": "aio-plant"},
                }
            ),
            encoding="utf-8",
        )
        return workspace

    def test_a_properties_key_is_not_excused_by_a_template_parameter(self, tmp_path):
        """The engine never fills `properties` from a template parameter, so a
        name collision there says nothing about the key being read."""
        orphans = TestSiteKeysAreConsumed()._orphan_keys(
            self._workspace(tmp_path, "properties")
        )

        assert any("properties.clusterName" in orphan for orphan in orphans), (
            "a properties key was excused by a same-named template parameter, "
            f"got: {orphans}"
        )

    def test_a_parameters_key_is_still_excused(self, tmp_path):
        """The channel is real, so narrowing it must not close it."""
        orphans = TestSiteKeysAreConsumed()._orphan_keys(
            self._workspace(tmp_path, "parameters")
        )

        assert not orphans, (
            f"a parameters key the auto-fill channel supplies was reported: {orphans}"
        )

    def test_an_invented_parameters_key_is_reported(self, tmp_path):
        """The excuse is a name match, not the container alone."""
        workspace = self._workspace(tmp_path, "parameters")
        (workspace / "sites" / "plant.yaml").write_text(
            yaml.dump(
                {
                    "name": "plant",
                    "subscription": "sub",
                    "location": "eastus",
                    "parameters": {"notARealKeyAnywhere": "x"},
                }
            ),
            encoding="utf-8",
        )

        orphans = TestSiteKeysAreConsumed()._orphan_keys(workspace)

        assert any("parameters.notARealKeyAnywhere" in orphan for orphan in orphans)

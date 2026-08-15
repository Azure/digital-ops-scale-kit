"""Unit tests for the Site Ops CLI module.

Tests cover:
- Argument parsing
- Command routing
- Output formatting
- Exit codes
"""

import os
import re
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops.cli import (
    cmd_deploy,
    cmd_sites,
    cmd_validate,
    main,
    resolve_manifest_path,
    setup_logging,
)


class TestResolveManifestPath:
    """Tests for manifest path resolution."""

    def test_relative_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        result = resolve_manifest_path(Path("manifests/deploy.yaml"), workspace)

        assert result == workspace / "manifests" / "deploy.yaml"

    def test_absolute_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        absolute_path = tmp_path / "other" / "manifest.yaml"

        result = resolve_manifest_path(absolute_path, workspace)

        assert result == absolute_path

    def test_current_dir_relative(self, tmp_path):
        workspace = tmp_path

        result = resolve_manifest_path(Path("manifest.yaml"), workspace)

        assert result == workspace / "manifest.yaml"


class TestSetupLogging:
    """Tests for logging configuration.

    `setup_logging` reconfigures process-wide state, so each case restores what
    it changed. Leaving the root logger at DEBUG with a stale handler, or the
    executor logger pinned, changes what any later test sees through `caplog`.
    """

    @pytest.fixture(autouse=True)
    def _restore_logging(self):
        import logging

        root = logging.getLogger()
        executor = logging.getLogger("siteops.executor")
        saved = (root.level, root.handlers[:], executor.level)
        try:
            yield
        finally:
            root.setLevel(saved[0])
            root.handlers[:] = saved[1]
            executor.setLevel(saved[2])

    def test_setup_logging_default(self):
        import logging

        setup_logging(verbose=False)

        # Executor logger should be WARNING level when not verbose
        executor_logger = logging.getLogger("siteops.executor")
        assert executor_logger.level == logging.WARNING

    def test_setup_logging_verbose(self):
        import logging

        # Reset the executor logger level before testing verbose mode
        executor_logger = logging.getLogger("siteops.executor")
        executor_logger.setLevel(logging.NOTSET)

        # Reset root logger handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        setup_logging(verbose=True)

        # In verbose mode, executor logger should NOT be set to WARNING
        # (it remains NOTSET so it inherits DEBUG from root)
        assert executor_logger.level == logging.NOTSET


class TestCmdValidate:
    """Tests for the validate command."""

    def test_validate_success(self, complete_workspace, capsys):
        """Test successful validation returns exit code 0."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "valid" in captured.out.lower()

    def test_validate_manifest_not_found(self, complete_workspace, capsys):
        """Test validate with missing manifest returns exit code 1."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = Path("nonexistent.yaml")
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Manifest not found" in captured.err

    def test_validate_failure(self, complete_workspace, capsys):
        """Test validation failure returns exit code 1."""
        from siteops.orchestrator import Orchestrator

        # Create manifest with missing template
        manifest_data = {
            "name": "invalid",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "nonexistent.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "invalid.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "✗" in captured.out
        assert "Template not found" in captured.out

    def test_validate_plan_shows_the_deployment_plan(self, complete_workspace, capsys):
        """`validate --plan` shows the deployment plan after validation.

        This asked for `verbose` and got a plan, because a mock argument bag
        returns a truthy child for an attribute the test never set. The flag
        that renders a plan is `plan`.
        """
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.plan = True

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        # Should show validation success
        assert "✓" in captured.out
        # Should show deployment plan
        assert "DEPLOYMENT PLAN" in captured.out
        assert "Sites" in captured.out
        assert "Steps" in captured.out

    def test_validate_library_manifest_prints_note(self, complete_workspace, capsys):
        """A library/partial manifest (no `sites:` and no `selector:`)
        validates ✓ but prints a Note explaining `-l` will be required
        at deploy time. Eliminates the validate-passes-then-deploy-
        fails surprise class."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "library",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "library.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Manifest is valid" in captured.out
        assert "library manifest" in captured.out
        assert "-l" in captured.out

    def test_validate_verbose_library_manifest_no_traceback(
        self, complete_workspace, capsys
    ):
        """`validate --plan` on a library manifest (no `sites:` and no
        `selector:`) prints ✓ + Note and exits 0. Previously
        `show_plan` re-resolved sites and re-raised NoTargetingError
        as a traceback after the success print."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "library",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "library.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.plan = True

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Manifest is valid" in captured.out
        assert "library manifest" in captured.out
        # No traceback or NoTargetingError leaked from show_plan.
        assert "Traceback" not in captured.out and "Traceback" not in captured.err
        assert "NoTargetingError" not in captured.out
        assert "NoTargetingError" not in captured.err

    def test_validate_verbose_not_shown_on_failure(self, complete_workspace, capsys):
        """Test plan is not shown when validation fails."""
        from siteops.orchestrator import Orchestrator

        # Create invalid manifest
        manifest_data = {
            "name": "invalid",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "nonexistent.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "invalid.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.plan = True

        exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        # Should show failure
        assert "✗" in captured.out
        # Should NOT show deployment plan
        assert "DEPLOYMENT PLAN" not in captured.out

    def test_validate_with_selector(self, complete_workspace):
        """Test validate passes selector to orchestrator."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = "environment=test"

        with patch.object(orchestrator, "validate") as mock_validate:
            mock_validate.return_value = []  # No errors

            cmd_validate(args, orchestrator)

            call_kwargs = mock_validate.call_args.kwargs
            assert call_kwargs["selector"] == "environment=test"


class TestCmdSites:
    """Tests for the sites command."""

    def test_sites_list_all(self, complete_workspace, capsys):
        """Test listing all sites."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.name = None
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-site" in captured.out
        assert "Available Sites" in captured.out

    def test_sites_with_selector(self, multi_site_workspace, capsys):
        """Test filtering sites by selector."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(multi_site_workspace)

        args = Namespace()
        args.name = None
        args.workspace = multi_site_workspace
        args.selector = "environment=dev"

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "dev-eastus" in captured.out
        assert "dev-westus" in captured.out
        assert "prod-eastus" not in captured.out

    def test_sites_no_match(self, complete_workspace, capsys):
        """When the operator passed a selector and got nothing, exit
        non-zero so wrapper scripts surface the failure. Bare `sites`
        on an empty workspace stays exit 0."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.name = None
        args.workspace = complete_workspace
        args.selector = "nonexistent=value"

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "No sites matched" in captured.err

    def test_sites_path_form_name_resolves(self, tmp_path, capsys):
        """`siteops sites regions/eu/munich-dev` resolves the nested
        site via the trusted-file fast path. Without parity to deploy,
        the path-form would compare against `Site.name` (basename only)
        and return no match."""
        from siteops.orchestrator import Orchestrator

        # Build a workspace with one nested site.
        sites = tmp_path / "sites" / "regions" / "eu"
        sites.mkdir(parents=True)
        (tmp_path / "manifests").mkdir()
        (sites / "munich-dev.yaml").write_text(
            "apiVersion: siteops/v1\n"
            "kind: Site\n"
            "name: munich-dev\n"
            "subscription: 00000000-0000-0000-0000-000000000000\n"
            "resourceGroup: rg-munich-dev\n"
            "location: westeurope\n",
            encoding="utf-8",
        )

        orchestrator = Orchestrator(tmp_path)

        args = Namespace()
        args.name = "regions/eu/munich-dev"
        args.workspace = tmp_path
        args.selector = None
        args.render = False
        args.show_sources = False

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "munich-dev" in captured.out

    def test_sites_empty_workspace(self, tmp_path, capsys):
        """Test no sites in workspace."""
        from siteops.orchestrator import Orchestrator

        # Create minimal workspace structure
        (tmp_path / "sites").mkdir()
        (tmp_path / "manifests").mkdir()

        orchestrator = Orchestrator(tmp_path)

        args = Namespace()
        args.name = None
        args.workspace = tmp_path
        args.selector = None

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No sites found" in captured.out

    def test_sites_shows_labels(self, complete_workspace, capsys):
        """Test sites output includes labels."""
        from siteops.orchestrator import Orchestrator

        # Add labels to test site
        site_path = complete_workspace / "sites" / "test-site.yaml"
        with open(site_path, "r", encoding="utf-8") as f:
            site_data = yaml.safe_load(f)
        site_data["labels"] = {"environment": "test", "region": "eastus"}
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.name = None
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "labels:" in captured.out
        assert "environment: test" in captured.out

    def test_sites_shows_properties(self, complete_workspace, capsys):
        """Test sites output includes properties by default."""
        from siteops.orchestrator import Orchestrator

        # Add properties to test site
        site_path = complete_workspace / "sites" / "test-site.yaml"
        with open(site_path, "r", encoding="utf-8") as f:
            site_data = yaml.safe_load(f)
        site_data["properties"] = {"mqtt": {"broker": "localhost"}}
        with open(site_path, "w", encoding="utf-8") as f:
            yaml.dump(site_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.name = None
        args.workspace = complete_workspace
        args.selector = None

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "properties:" in captured.out
        assert "mqtt" in captured.out

    def test_sites_positional_name(self, multi_site_workspace, capsys):
        """Positional name scopes to one site, equivalent to -l name=<NAME>."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(multi_site_workspace)

        args = Namespace()
        args.name = None
        args.workspace = multi_site_workspace
        args.name = "dev-eastus"
        args.selector = None
        args.render = False

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "dev-eastus" in captured.out
        assert "dev-westus" not in captured.out
        assert "prod-eastus" not in captured.out

    def test_sites_positional_and_selector_rejected(self, multi_site_workspace, capsys):
        """Combining positional name and -l name= is rejected to avoid ambiguity."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(multi_site_workspace)

        args = Namespace()
        args.name = None
        args.workspace = multi_site_workspace
        args.name = "dev-eastus"
        args.selector = "name=prod-eastus"
        args.render = False

        exit_code = cmd_sites(args, orchestrator)

        assert exit_code == 1
        assert "either the positional `name` or `-l name=" in capsys.readouterr().err


class TestCmdDeploy:
    """Tests for the deploy command."""

    def test_deploy_success(self, complete_workspace):
        """Test successful deployment returns exit code 0."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        with patch.object(orchestrator, "deploy") as mock_deploy:
            mock_deploy.return_value = {
                "sites": {"test-site": {"status": "success"}},
                "summary": {"total": 1, "succeeded": 1, "failed": 0, "elapsed": 1.0},
            }

            exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 0

    def test_deploy_manifest_not_found(self, complete_workspace, capsys):
        """Test deploy with missing manifest returns exit code 1."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = Path("nonexistent.yaml")
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Manifest not found" in captured.err

    def test_deploy_no_sites_matched(self, complete_workspace, capsys):
        """Test deploy with no matching sites returns exit code 0."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "no-match",
            "siteSelector": "nonexistent=value",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "no-match.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No sites matched" in captured.out

    def test_deploy_generic_manifest_no_selector_errors(self, complete_workspace, capsys):
        """Generic manifest (no targeting) without `-l` is a hard error."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "generic",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "generic.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "has no targeting" in captured.err

    def test_deploy_duplicate_non_name_selector_key_errors(self, complete_workspace, capsys):
        """Duplicate non-name selector key surfaces as exit 1 with clear error."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "test",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "test.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = "env=prod,env=dev"
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "may only appear once" in captured.err

    def test_deploy_unresolved_site_in_manifest_exits_cleanly(self, complete_workspace, capsys):
        """A manifest `sites:` entry that does not resolve to a workspace
        file must surface as a clean error and exit 1, not a Python
        traceback. `FileNotFoundError` is `OSError`, not `ValueError`."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "missing-site",
            "sites": ["does-not-exist"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "missing-site.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "does-not-exist" in captured.err

    def test_deploy_malformed_yaml_exits_cleanly(self, complete_workspace, capsys):
        """A manifest with broken YAML must exit 1 with a one-line
        Error, not a 30-line yaml.YAMLError traceback. Manifest.from_file
        raises yaml.YAMLError before resolve_sites; widen the try."""
        from siteops.orchestrator import Orchestrator

        # Tab inside indentation breaks the YAML parser.
        manifest_path = complete_workspace / "manifests" / "broken.yaml"
        manifest_path.write_text(
            "name: broken\nsites:\n\t- test-site\nsteps:\n  - name: x\n    template: t.bicep\n",
            encoding="utf-8",
        )

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err
        assert "Traceback" not in captured.err

    def test_deploy_cli_selector_no_match_errors_with_diagnostic(self, complete_workspace, capsys):
        """CLI selector matching zero workspace sites exits 1 with a
        diagnostic listing the workspace label values."""
        from siteops.orchestrator import Orchestrator

        # Manifest exists; CLI selector overrides and matches nothing.
        manifest_data = {
            "name": "test",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "test.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = "nonexistent=value"
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "matched no sites" in captured.err
        # Diagnostic should mention the missing label so the operator
        # sees the typo.
        assert "nonexistent" in captured.err

    def test_deploy_cli_name_typo_errors_with_workspace_names(
        self, complete_workspace, capsys
    ):
        """CLI `-l name=X` for an unknown name lists workspace site names."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "test",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "test.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = "name=does-not-exist"
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "does-not-exist" in captured.err
        # The diagnostic should list at least one real workspace site.
        assert "test-site" in captured.err

    def test_deploy_fails_when_a_site_in_scope_will_not_load(
        self, multi_site_workspace, capsys
    ):
        """A selector resolves against every site, so one that fails to load
        leaves the target set smaller than the selector names. Deploying the
        remainder would report success for a fleet the operator did not get."""
        from siteops.orchestrator import Orchestrator

        # A tab inside indentation breaks the YAML parser.
        broken = multi_site_workspace / "sites" / "broken-site.yaml"
        broken.write_text("name: broken-site\n\tlabels:\n", encoding="utf-8")

        orchestrator = Orchestrator(multi_site_workspace)
        manifest_path = multi_site_workspace / "manifests" / "multi-site.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = multi_site_workspace
        args.selector = None
        args.parallel = None

        with patch.object(orchestrator, "deploy") as mock_deploy:
            exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1
        mock_deploy.assert_not_called()
        captured = capsys.readouterr()
        assert "broken-site" in captured.err
        assert "target set is incomplete" in captured.err

    def test_deploy_no_steps(self, complete_workspace, capsys):
        """Test deploy with no steps returns exit code 0."""
        from siteops.orchestrator import Orchestrator

        manifest_data = {
            "name": "no-steps",
            "sites": ["test-site"],
            "steps": [],
        }
        manifest_path = complete_workspace / "manifests" / "no-steps.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        orchestrator = Orchestrator(complete_workspace)

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no steps" in captured.out.lower()

    def test_deploy_failure_returns_exit_code_1(self, complete_workspace):
        """Test failed deployment returns exit code 1."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = None

        with patch.object(orchestrator, "deploy") as mock_deploy:
            mock_deploy.return_value = {
                "sites": {"test-site": {"status": "failed", "error": "Deployment error"}},
                "summary": {"total": 1, "succeeded": 0, "failed": 1, "elapsed": 1.0},
            }

            exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 1

    def test_deploy_with_parallel_override(self, complete_workspace):
        """Test deploy passes parallel override to orchestrator."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = None
        args.parallel = 3

        with patch.object(orchestrator, "deploy") as mock_deploy:
            mock_deploy.return_value = {
                "sites": {},
                "summary": {"total": 1, "succeeded": 1, "failed": 0, "elapsed": 1.0},
            }

            cmd_deploy(args, orchestrator)

            call_kwargs = mock_deploy.call_args.kwargs
            assert call_kwargs["parallel_override"] == 3

    def test_deploy_negative_parallel_rejected(self, complete_workspace, capsys):
        """Negative --parallel value is rejected at argparse time."""
        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                "manifests/test-manifest.yaml",
                "--parallel",
                "-1",
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 2  # argparse error exit code
        assert "--parallel must be >= 0" in capsys.readouterr().err

    def test_deploy_parallel_max_alias(self, complete_workspace):
        """`--parallel max` is accepted and parses to the same value as `0`."""
        for alias in ("max", "auto", "0"):
            with patch.object(
                sys,
                "argv",
                [
                    "siteops",
                    "-w",
                    str(complete_workspace),
                    "deploy",
                    "manifests/test-manifest.yaml",
                    "--parallel",
                    alias,
                ],
            ):
                with patch("siteops.cli.cmd_deploy") as mock_cmd:
                    mock_cmd.return_value = 0
                    with pytest.raises(SystemExit):
                        main()
                    args = mock_cmd.call_args[0][0]
                    assert args.parallel == 0, f"alias {alias!r} did not parse to 0"

    def test_deploy_parallel_invalid_string_rejected(self, complete_workspace, capsys):
        """A non-int, non-alias string for --parallel is rejected."""
        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                "manifests/test-manifest.yaml",
                "--parallel",
                "bogus",
            ],
        ):
            with pytest.raises(SystemExit):
                main()
        assert "must be a non-negative integer or 'max' / 'auto'" in capsys.readouterr().err

    def test_deploy_with_selector(self, complete_workspace):
        """Test deploy passes selector to orchestrator."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        args = Namespace()
        args.manifest = manifest_path
        args.workspace = complete_workspace
        args.selector = "environment=dev"
        args.parallel = None

        with patch.object(orchestrator, "deploy") as mock_deploy:
            mock_deploy.return_value = {
                "sites": {},
                "summary": {"total": 1, "succeeded": 1, "failed": 0, "elapsed": 1.0},
            }

            cmd_deploy(args, orchestrator)

            call_kwargs = mock_deploy.call_args.kwargs
            assert call_kwargs["selector"] == "environment=dev"


class TestMainArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_help_shows_commands(self, capsys):
        """Test help shows available commands."""
        with patch.object(sys, "argv", ["siteops", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert "deploy" in captured.out
        assert "validate" in captured.out
        assert "sites" in captured.out

    def test_version_flag(self, capsys):
        """Test --version shows version."""
        from siteops import __version__

        with patch.object(sys, "argv", ["siteops", "--version"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_deploy_requires_manifest(self, complete_workspace, capsys):
        """Test deploy command requires manifest argument."""
        with patch.object(
            sys,
            "argv",
            ["siteops", "-w", str(complete_workspace), "deploy"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code != 0

    def test_validate_requires_manifest(self, complete_workspace, capsys):
        """Test validate command requires manifest argument."""
        with patch.object(
            sys,
            "argv",
            ["siteops", "-w", str(complete_workspace), "validate"],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code != 0

    def test_deploy_dry_run_flag(self, complete_workspace):
        """Test --dry-run flag is parsed correctly."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                str(manifest_path),
                "--dry-run",
            ],
        ):
            with patch("siteops.cli.Orchestrator") as MockOrchestrator:
                mock_instance = MagicMock()
                mock_instance.resolve_sites.return_value = []
                MockOrchestrator.return_value = mock_instance

                with pytest.raises(SystemExit):
                    main()

                # Verify Orchestrator was created with dry_run=True
                MockOrchestrator.assert_called_once()
                call_kwargs = MockOrchestrator.call_args.kwargs
                assert call_kwargs["dry_run"] is True

    def test_deploy_parallel_flag(self, complete_workspace):
        """Test -p/--parallel flag is parsed correctly."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                str(manifest_path),
                "-p",
                "5",
            ],
        ):
            with patch("siteops.cli.cmd_deploy") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.parallel == 5

    def test_deploy_selector_flag(self, complete_workspace):
        """Test -l/--selector flag is parsed correctly."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                str(manifest_path),
                "-l",
                "env=prod",
            ],
        ):
            with patch("siteops.cli.cmd_deploy") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.selector == "env=prod"

    def test_deploy_selector_flag_repeatable(self, complete_workspace):
        """Multiple -l flags merge into a single comma-joined string."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "deploy",
                str(manifest_path),
                "-l",
                "name=a",
                "-l",
                "name=b",
                "-l",
                "env=prod",
            ],
        ):
            with patch("siteops.cli.cmd_deploy") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                # Joined in CLI order; downstream parse_selector applies
                # name-OR / non-name-error rules.
                assert args.selector == "name=a,name=b,env=prod"

    def test_validate_plan_flag(self, complete_workspace):
        """`--plan` asks for the deployment plan. It used to be `-v`, which
        also raised log verbosity, so one flag meant two unrelated things."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "validate",
                str(manifest_path),
                "--plan",
            ],
        ):
            with patch("siteops.cli.cmd_validate") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.plan is True
                # Asking for the plan does not turn on debug logging.
                assert args.verbose is False

    def test_verbose_is_global_and_reaches_every_subcommand(self, complete_workspace):
        """`-v` is global, so `deploy` can have it. Without that, a dry run
        could not show the commands it would have run."""
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-v",
                "-w",
                str(complete_workspace),
                "deploy",
                str(manifest_path),
                "--dry-run",
            ],
        ):
            with patch("siteops.cli.cmd_deploy") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.verbose is True
                assert args.dry_run is True

    def test_sites_show_sources_flag(self, complete_workspace):
        """`--show-sources` names what it does. It used to be spelled `-v`."""
        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "sites",
                "--show-sources",
            ],
        ):
            with patch("siteops.cli.cmd_sites") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.show_sources is True

    def test_sites_selector_flag(self, complete_workspace):
        """Test sites -l flag is parsed correctly."""
        with patch.object(
            sys,
            "argv",
            [
                "siteops",
                "-w",
                str(complete_workspace),
                "sites",
                "-l",
                "region=eastus",
            ],
        ):
            with patch("siteops.cli.cmd_sites") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.selector == "region=eastus"

    def test_workspace_default_when_no_discovery(self, tmp_path, monkeypatch):
        """When -w is omitted and cwd has no workspaces/ shape, defaults to cwd."""
        monkeypatch.chdir(tmp_path)
        with patch.object(
            sys,
            "argv",
            ["siteops", "sites"],
        ):
            with patch("siteops.cli.cmd_sites") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.workspace == tmp_path.resolve()

    def test_workspace_auto_discovered_from_workspaces_dir(self, tmp_path, monkeypatch):
        """When cwd has workspaces/<single>/ with workspace shape, auto-discover it."""
        ws = tmp_path / "workspaces" / "iot-operations"
        (ws / "sites").mkdir(parents=True)
        (ws / "manifests").mkdir()
        monkeypatch.chdir(tmp_path)
        with patch.object(
            sys,
            "argv",
            ["siteops", "sites"],
        ):
            with patch("siteops.cli.cmd_sites") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                assert args.workspace == ws.resolve()

    def test_workspace_auto_discovery_ambiguous_falls_back(self, tmp_path, monkeypatch):
        """When workspaces/ contains multiple workspace-shaped dirs, fall back to cwd."""
        for name in ("a", "b"):
            ws = tmp_path / "workspaces" / name
            (ws / "sites").mkdir(parents=True)
            (ws / "manifests").mkdir()
        monkeypatch.chdir(tmp_path)
        with patch.object(
            sys,
            "argv",
            ["siteops", "sites"],
        ):
            with patch("siteops.cli.cmd_sites") as mock_cmd:
                mock_cmd.return_value = 0
                with pytest.raises(SystemExit):
                    main()

                args = mock_cmd.call_args[0][0]
                # Ambiguous discovery: caller falls back to cwd, which here
                # is not itself a valid workspace but is still passed through
                # for the orchestrator to error on (preserves prior behavior).
                assert args.workspace == tmp_path.resolve()


class TestUserAgentConfiguration:
    """Tests for Azure CLI User-Agent configuration."""

    def test_user_agent_set_on_import(self):
        """Verify AZURE_HTTP_USER_AGENT is set when executor module loads."""
        from siteops import __version__

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        assert f"siteops/{__version__}" in user_agent

    def test_user_agent_not_duplicated(self):
        """Verify User-Agent isn't duplicated on repeated configuration."""
        from siteops import __version__
        from siteops.executor import _configure_user_agent

        _configure_user_agent()
        _configure_user_agent()

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        count = user_agent.count(f"siteops/{__version__}")
        assert count == 1, f"User-Agent duplicated: {user_agent}"

    def test_user_agent_appends_to_existing(self, monkeypatch):
        """Verify siteops agent is appended when other tools set User-Agent first."""
        from siteops import __version__
        from siteops.executor import _configure_user_agent

        monkeypatch.setenv("AZURE_HTTP_USER_AGENT", "other-tool/2.0")
        _configure_user_agent()

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        assert "other-tool/2.0" in user_agent
        assert f"siteops/{__version__}" in user_agent

    def test_user_agent_format(self):
        """Verify User-Agent follows Azure SDK conventions."""
        from siteops import __version__

        user_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")
        assert re.search(rf"siteops/{re.escape(__version__)}", user_agent)


class TestPrintValue:
    """Tests for _print_value helper function."""

    def test_print_simple_dict(self, capsys):
        """Test printing a simple flat dictionary."""
        from siteops.cli import _print_value

        _print_value({"key1": "value1", "key2": 42}, indent=0)

        captured = capsys.readouterr()
        assert "key1: value1" in captured.out
        assert "key2: 42" in captured.out

    def test_print_nested_dict(self, capsys):
        """Test printing a nested dictionary."""
        from siteops.cli import _print_value

        _print_value(
            {
                "outer": {
                    "inner": "value",
                    "number": 123,
                }
            },
            indent=0,
        )

        captured = capsys.readouterr()
        assert "outer:" in captured.out
        assert "inner: value" in captured.out
        assert "number: 123" in captured.out

    def test_print_deeply_nested_dict(self, capsys):
        """Test printing a deeply nested dictionary."""
        from siteops.cli import _print_value

        _print_value(
            {
                "level1": {
                    "level2": {
                        "level3": {
                            "deepValue": "found",
                        }
                    }
                }
            },
            indent=0,
        )

        captured = capsys.readouterr()
        assert "level1:" in captured.out
        assert "level2:" in captured.out
        assert "level3:" in captured.out
        assert "deepValue: found" in captured.out

    def test_print_simple_list(self, capsys):
        """Test printing a simple list (inline)."""
        from siteops.cli import _print_value

        _print_value({"items": ["a", "b", "c"]}, indent=0)

        captured = capsys.readouterr()
        assert "items: ['a', 'b', 'c']" in captured.out

    def test_print_complex_list(self, capsys):
        """Test printing a list of dictionaries."""
        from siteops.cli import _print_value

        _print_value(
            {
                "endpoints": [
                    {"host": "10.0.1.100", "port": 4840},
                    {"host": "10.0.1.101", "port": 4840},
                ]
            },
            indent=0,
        )

        captured = capsys.readouterr()
        assert "endpoints:" in captured.out
        assert "[0]:" in captured.out
        assert "host: 10.0.1.100" in captured.out
        assert "port: 4840" in captured.out
        assert "[1]:" in captured.out
        assert "host: 10.0.1.101" in captured.out

    def test_print_empty_list(self, capsys):
        """Test printing an empty list."""
        from siteops.cli import _print_value

        _print_value({"items": []}, indent=0)

        captured = capsys.readouterr()
        assert "items: []" in captured.out

    def test_print_mixed_structure(self, capsys):
        """Test printing a mixed structure with dicts and lists."""
        from siteops.cli import _print_value

        _print_value(
            {
                "brokerConfig": {
                    "memoryProfile": "Medium",
                    "frontendReplicas": 2,
                },
                "tags": ["env:dev", "team:platform"],
                "clusterName": "my-cluster",
            },
            indent=0,
        )

        captured = capsys.readouterr()
        assert "brokerConfig:" in captured.out
        assert "memoryProfile: Medium" in captured.out
        assert "frontendReplicas: 2" in captured.out
        assert "tags: ['env:dev', 'team:platform']" in captured.out
        assert "clusterName: my-cluster" in captured.out

    def test_print_with_indentation(self, capsys):
        """Test that indentation is applied correctly."""
        from siteops.cli import _print_value

        _print_value({"key": "value"}, indent=4)

        captured = capsys.readouterr()
        assert "    key: value" in captured.out

    def test_print_bare_list_with_dicts(self, capsys):
        """Test printing a bare list of dicts (not wrapped in a dict key)."""
        from siteops.cli import _print_value

        _print_value(
            [{"name": "item1", "value": 10}, {"name": "item2", "value": 20}],
            indent=0,
        )

        captured = capsys.readouterr()
        # Bare list with dicts should show indexed items
        assert "[0]:" in captured.out
        assert "[1]:" in captured.out
        assert "name: item1" in captured.out
        assert "name: item2" in captured.out

    def test_print_bare_list_simple(self, capsys):
        """Test printing a bare list of simple values."""
        from siteops.cli import _print_value

        _print_value(["alpha", "beta", "gamma"], indent=0)

        captured = capsys.readouterr()
        # Bare list with simple values should show dash-prefixed items
        assert "- alpha" in captured.out
        assert "- beta" in captured.out
        assert "- gamma" in captured.out

    def test_print_scalar_value(self, capsys):
        """Test printing a scalar value directly."""
        from siteops.cli import _print_value

        _print_value("just a string", indent=2)

        captured = capsys.readouterr()
        assert "  just a string" in captured.out


class TestCmdSitesParameterDisplay:
    """Tests for parameter display in cmd_sites."""

    def test_sites_shows_parameters_as_key_values(self, tmp_path, capsys, monkeypatch):
        """Test that parameters are shown as key-value pairs, not just keys."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: my-cluster
  customLocationName: my-cl
  defaultDataflowInstanceCount: 1
"""
        )

        import sys
        from argparse import Namespace

        from siteops.cli import cmd_sites
        from siteops.orchestrator import Orchestrator

        monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(workspace), "sites"])

        orchestrator = Orchestrator(workspace)
        args = Namespace(selector=None, show_sources=False)

        cmd_sites(args, orchestrator)

        captured = capsys.readouterr()
        # Should show actual values, not just keys
        assert "clusterName: my-cluster" in captured.out
        assert "customLocationName: my-cl" in captured.out
        assert "defaultDataflowInstanceCount: 1" in captured.out
        # Should NOT show as array of keys
        assert "['clusterName'" not in captured.out

    def test_sites_shows_nested_parameters(self, tmp_path, capsys, monkeypatch):
        """Test that nested parameters are displayed with proper structure."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: my-cluster
  brokerConfig:
    memoryProfile: Medium
    frontendReplicas: 2
    backendWorkers: 4
"""
        )

        from argparse import Namespace

        from siteops.cli import cmd_sites
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(workspace)
        args = Namespace(selector=None, show_sources=False)

        cmd_sites(args, orchestrator)

        captured = capsys.readouterr()
        # Should show nested structure
        assert "brokerConfig:" in captured.out
        assert "memoryProfile: Medium" in captured.out
        assert "frontendReplicas: 2" in captured.out
        assert "backendWorkers: 4" in captured.out

    def test_sites_shows_overlay_values(self, tmp_path, capsys):
        """Test that overlay values are displayed (merged correctly)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "sites.local").mkdir()

        # Base site with placeholder values
        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-placeholder
location: eastus
parameters:
  clusterName: placeholder-cluster
"""
        )

        # Overlay with real values
        overlay_file = workspace / "sites.local" / "test-site.yaml"
        overlay_file.write_text(
            """
subscription: "real-subscription-id"
resourceGroup: rg-real
parameters:
  clusterName: real-cluster
"""
        )

        from argparse import Namespace

        from siteops.cli import cmd_sites
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(workspace)
        args = Namespace(selector=None, show_sources=False)

        cmd_sites(args, orchestrator)

        captured = capsys.readouterr()
        # Should show overlay values, not base values
        assert "real-subscription-id" in captured.out
        assert "rg-real" in captured.out
        assert "clusterName: real-cluster" in captured.out
        # Should NOT show placeholder values
        assert "00000000-0000-0000-0000-000000000000" not in captured.out
        assert "rg-placeholder" not in captured.out
        assert "placeholder-cluster" not in captured.out


class TestCmdSitesShowSourcesProvenance:
    """`--show-sources` annotates every leaf with its source file."""

    def test_show_sources_annotates_every_leaf(self, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        (workspace / "sites" / "shared").mkdir(parents=True)
        (workspace / "sites" / "shared" / "base.yaml").write_text(
            """
apiVersion: siteops/v1
kind: SiteTemplate
name: base
subscription: shared-sub
labels:
  team: platform
properties:
  defaultRelease: r1
""",
            encoding="utf-8",
        )
        (workspace / "sites" / "munich.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: munich
inherits: shared/base.yaml
resourceGroup: rg-munich
location: eastus
labels:
  environment: dev
""",
            encoding="utf-8",
        )

        from argparse import Namespace

        from siteops.cli import cmd_sites
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(workspace)
        args = Namespace(name=None, selector="name=munich", show_sources=True, render=False)

        cmd_sites(args, orchestrator)

        captured = capsys.readouterr()
        # Inherited values point at the shared template.
        assert "subscription:   shared-sub" in captured.out
        assert "shared/base.yaml" in captured.out
        # Site-defined values point at the leaf file.
        assert "rg-munich" in captured.out
        assert "sites/munich.yaml" in captured.out
        # Inherited and site-defined labels are both annotated.
        assert "team: platform" in captured.out
        assert "environment: dev" in captured.out

    def test_non_verbose_skips_provenance(self, tmp_path, capsys):
        """The bare listing skips the provenance walk to stay fast."""
        workspace = tmp_path / "workspace"
        (workspace / "sites").mkdir(parents=True)
        (workspace / "sites" / "munich.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: munich
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg
location: eastus
""",
            encoding="utf-8",
        )

        from argparse import Namespace

        from siteops.cli import cmd_sites
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(workspace)
        args = Namespace(name=None, selector=None, show_sources=False, render=False)

        cmd_sites(args, orchestrator)

        captured = capsys.readouterr()
        # No origin annotation in non-verbose output.
        assert "# sites/" not in captured.out


class TestPlanOutputIsSeparateFromLogVerbosity:
    """Asking for a plan and asking for debug logs are different requests.

    They used to be one flag, which meant a plan on a published surface also
    raised log verbosity there, and `deploy` could ask for neither. These use a
    real `Namespace` rather than a mock argument bag, because an unset
    attribute on a mock is a truthy child mock, so a mock cannot tell the
    difference between a flag that was passed and one that was not.
    """

    def _args(self, workspace, manifest, **overrides):
        from argparse import Namespace

        values = {
            "manifest": manifest,
            "workspace": workspace,
            "selector": None,
            "plan": False,
            "verbose": False,
        }
        values.update(overrides)
        return Namespace(**values)

    @pytest.mark.parametrize(
        ("plan", "verbose", "expect_plan"),
        [(False, False, False), (True, False, True), (False, True, False), (True, True, True)],
        ids=["neither", "plan-only", "verbose-only", "both"],
    )
    def test_validate_shows_a_plan_for_plan_not_for_verbose(
        self, complete_workspace, plan, verbose, expect_plan
    ):
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest = complete_workspace / "manifests" / "test-manifest.yaml"
        args = self._args(complete_workspace, manifest, plan=plan, verbose=verbose)

        with patch.object(orchestrator, "show_plan") as show_plan:
            exit_code = cmd_validate(args, orchestrator)

        assert exit_code == 0
        assert show_plan.called is expect_plan

    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "real-run"])
    def test_deploy_shows_a_plan_only_on_a_dry_run(self, complete_workspace, dry_run):
        """A dry run reports what a real run would do, so it shows the plan
        without being asked. A real run does the thing instead of describing
        it, and its own output covers what happened."""
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest = complete_workspace / "manifests" / "test-manifest.yaml"
        args = self._args(complete_workspace, manifest, dry_run=dry_run, parallel=None)

        summary = {"summary": {"failed": 0, "succeeded": 1}, "sites": {}}
        with patch.object(orchestrator, "show_plan") as show_plan, \
             patch.object(orchestrator, "deploy", return_value=summary):
            exit_code = cmd_deploy(args, orchestrator)

        assert exit_code == 0
        assert show_plan.called is dry_run


class TestVerboseSaysWhereTheOutputMoved:
    """`-v` sets log verbosity and nothing else.

    It used to select the deployment plan on `validate` and the source
    annotations on `sites`. The obvious retry after the old spelling is
    rejected is to move `-v` earlier, which succeeds and prints nothing, so a
    job that existed to print a plan passes while emitting none. The note goes
    to stderr so a pipeline reading stdout is unaffected.
    """

    def _validate_args(self, workspace, manifest, *, verbose, plan):
        args = Namespace()
        args.manifest = manifest
        args.workspace = workspace
        args.selector = None
        args.verbose = verbose
        args.plan = plan
        return args

    @pytest.mark.parametrize(
        ("verbose", "plan", "expected"),
        [(True, False, True), (True, True, False), (False, False, False)],
        ids=["verbose-without-plan", "verbose-with-plan", "neither"],
    )
    def test_validate_names_the_flag_that_prints_a_plan(
        self, complete_workspace, capsys, verbose, plan, expected
    ):
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(complete_workspace)
        manifest = complete_workspace / "manifests" / "test-manifest.yaml"
        args = self._validate_args(complete_workspace, manifest, verbose=verbose, plan=plan)

        with patch.object(orchestrator, "show_plan"):
            cmd_validate(args, orchestrator)

        assert ("--plan" in capsys.readouterr().err) is expected

    @pytest.mark.parametrize(
        ("verbose", "show_sources", "expected"),
        [(True, False, True), (True, True, False), (False, False, False)],
        ids=["verbose-without-flag", "verbose-with-flag", "neither"],
    )
    def test_sites_names_the_flag_that_shows_sources(
        self, complete_workspace, capsys, verbose, show_sources, expected
    ):
        from siteops.orchestrator import Orchestrator

        args = Namespace()
        args.workspace = complete_workspace
        args.site = None
        args.selector = None
        args.resolve = False
        args.verbose = verbose
        args.show_sources = show_sources

        cmd_sites(args, Orchestrator(complete_workspace))

        assert ("--show-sources" in capsys.readouterr().err) is expected


class TestAWorkspaceWhoseSitesAllFailIsDiagnosedAsSuch:
    """An empty result has two causes and they need different answers.

    A workspace with no site files needs one adding. A workspace whose site
    files were all rejected needs those files fixing, and telling the operator
    to add a site sends them to write a file they already have.
    """

    @pytest.fixture
    def broken_workspace(self, tmp_workspace):
        """A workspace whose only site carries a key the engine does not read."""
        (tmp_workspace / "sites" / "plant-east.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: plant-east\n"
            "subscription: sub\nlocation: eastus\nparamaters:\n  a: b\n",
            encoding="utf-8",
        )
        return tmp_workspace

    def _args(self, workspace, **overrides):
        args = Namespace()
        args.workspace = workspace
        args.site = None
        args.selector = None
        args.resolve = False
        args.verbose = False
        args.show_sources = False
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_sites_reports_the_rejected_files_and_exits_nonzero(
        self, broken_workspace, capsys
    ):
        from siteops.orchestrator import Orchestrator

        exit_code = cmd_sites(self._args(broken_workspace), Orchestrator(broken_workspace))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "plant-east" in captured.err
        assert "No sites found in workspace" not in captured.out

    def test_an_empty_workspace_still_reports_an_empty_workspace(
        self, tmp_workspace, capsys
    ):
        """The other cause. Reporting rejected files here would be as wrong as
        the reverse."""
        from siteops.orchestrator import Orchestrator

        exit_code = cmd_sites(self._args(tmp_workspace), Orchestrator(tmp_workspace))

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "No sites found in workspace" in captured.out

    def test_the_selector_explanation_names_the_rejected_files(self, broken_workspace):
        from siteops.orchestrator import Orchestrator

        orchestrator = Orchestrator(broken_workspace)
        explanation = orchestrator.explain_no_match("environment=dev")

        assert "plant-east" in explanation
        assert "Add a site file" not in explanation

    def test_naming_a_rejected_site_reports_it_rather_than_raising(
        self, broken_workspace, capsys
    ):
        """`siteops sites <name>` is the direct way to ask why one site was
        rejected, so it has to answer rather than raise through as a
        traceback."""
        from siteops.orchestrator import Orchestrator

        args = self._args(broken_workspace)
        args.name = "plant-east"

        exit_code = cmd_sites(args, Orchestrator(broken_workspace))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "paramaters" in captured.err
        assert "Traceback" not in captured.err

    def test_a_missing_inherits_target_is_reported_rather_than_raising(
        self, tmp_workspace, capsys
    ):
        """A missing parent raises `FileNotFoundError`, not `ValueError`, so a
        handler covering only the latter still leaves a traceback."""
        from siteops.orchestrator import Orchestrator

        (tmp_workspace / "sites" / "plant-east.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: plant-east\n"
            "inherits: no-such-template.yaml\n",
            encoding="utf-8",
        )
        args = self._args(tmp_workspace)
        args.name = "plant-east"

        exit_code = cmd_sites(args, Orchestrator(tmp_workspace))

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "no-such-template.yaml" in captured.err
        assert "Traceback" not in captured.err

    def test_show_sources_names_the_file_each_value_came_from(
        self, tmp_workspace, capsys
    ):
        """The provenance helper has its own tests. This is the command that
        has to call it, which is where an operator reads the answer."""
        from siteops.orchestrator import Orchestrator

        (tmp_workspace / "sites" / "base-site.yaml").write_text(
            "apiVersion: siteops/v1\nkind: SiteTemplate\n"
            "subscription: sub-from-parent\nlocation: eastus\n",
            encoding="utf-8",
        )
        (tmp_workspace / "sites" / "plant-east.yaml").write_text(
            "apiVersion: siteops/v1\nkind: Site\nname: plant-east\n"
            "inherits: base-site.yaml\nresourceGroup: rg-plant-east\n",
            encoding="utf-8",
        )
        args = self._args(tmp_workspace, show_sources=True)

        exit_code = cmd_sites(args, Orchestrator(tmp_workspace))

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "base-site.yaml" in captured.out, (
            "the inherited value must name the file it came from"
        )

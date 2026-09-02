"""Redaction of environment-identifying values in engine output.

`siteops/sanitize.py` decides what a published surface may carry. These tests
pin both halves: that a scrub removes every identifying shape while leaving the
diagnostic intact, and that it applies exactly where the destination is public.

Every test that reads the mode controls the environment explicitly. The suite
itself runs under `GITHUB_ACTIONS` in CI, which turns redaction on, so a test
asserting the local default would otherwise pass locally and fail in CI.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from siteops.orchestrator import Orchestrator
from siteops.sanitize import (
    REDACT_ENV,
    is_redaction_enabled,
    scrub,
    scrub_command,
    scrub_command_for_output,
    scrub_for_output,
    scrub_site_for_output,
)

# A realistic ARM resource id, the shape that appears throughout a deployment
# error. The GUID is a documentation placeholder, and the names around it are
# the identifying parts a scrub has to remove.
SUBSCRIPTION = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/contoso-munich-rg"
    f"/providers/Microsoft.IoTOperations/instances/munich-aio"
    f"/dataflowEndpoints/fabric-out"
)

# Assembled from fragments so the file carries no literal that reads as a
# credential to a scanner.
JWT = ".".join(["eyJ" + "hbGciOiJSUzI1NiJ9", "eyJ" + "hdWQiOiJhaW8i", "c2lnbmF0dXJl"])

_CI_MARKERS = ("GITHUB_ACTIONS", "TF_BUILD")


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every variable that decides the redaction mode."""
    for name in (REDACT_ENV, *_CI_MARKERS):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestScrubRemovesIdentities:
    """A scrub leaves nothing that identifies a tenant or an environment."""

    def test_resource_id_keeps_the_type_and_drops_the_names(self):
        result = scrub(f"The resource {RESOURCE_ID} was not found.")

        assert "<Microsoft.IoTOperations/instances/dataflowEndpoints>" in result
        assert SUBSCRIPTION not in result
        assert "contoso-munich-rg" not in result
        assert "munich-aio" not in result
        assert "fabric-out" not in result

    def test_resource_group_id_without_a_provider(self):
        scrubbed = scrub(f"/subscriptions/{SUBSCRIPTION}/resourceGroups/contoso-rg")

        assert scrubbed == "<resource-group>"
        assert "contoso-rg" not in scrubbed

    def test_subscription_id_alone(self):
        assert scrub(f"/subscriptions/{SUBSCRIPTION}") == "<subscription>"

    def test_bare_guid(self):
        assert scrub(f"Principal {SUBSCRIPTION} lacks permission.") == (
            "Principal <guid> lacks permission."
        )

    def test_uppercase_guid(self):
        assert SUBSCRIPTION.upper() not in scrub(f"tenant={SUBSCRIPTION.upper()}")

    def test_bearer_token(self):
        assert scrub(f"Bearer {JWT} was rejected") == "Bearer <token> was rejected"

    def test_azure_service_host_loses_its_resource_name(self):
        scrubbed = scrub("Could not reach contoso-vault.vault.azure.net:443")

        assert "contoso-vault" not in scrubbed
        assert "<host>.vault.azure.net" in scrubbed

    def test_private_endpoint_host_loses_its_resource_name(self):
        """A private endpoint adds a label, so replacing only the first leaves the name."""
        scrubbed = scrub("Failed to connect to mystorage.privatelink.blob.core.windows.net:443")

        assert "mystorage" not in scrubbed
        assert scrubbed == "Failed to connect to <host>.blob.core.windows.net:443"

    def test_key_vault_private_endpoint_host(self):
        scrubbed = scrub("kv-01.privatelink.vaultcore.azure.net timed out")

        assert "kv-01" not in scrubbed
        assert "<host>.vaultcore.azure.net" in scrubbed

    def test_extension_resource_id_reports_the_type_that_failed(self):
        """An extension id carries two `providers` segments, and the second is the type.

        Every AIO install and upgrade failure has this shape, so taking the first
        would name the cluster rather than the extension.
        """
        extension_id = (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg"
            f"/providers/Microsoft.Kubernetes/connectedClusters/cl-eu"
            f"/providers/Microsoft.KubernetesConfiguration/extensions/azure-iot-operations"
        )
        scrubbed = scrub(extension_id)

        assert scrubbed == "<Microsoft.KubernetesConfiguration/extensions>"

    def test_resource_group_named_in_quotes(self):
        """ARM quotes the group rather than emitting a path when a resource is absent."""
        scrubbed = scrub(
            "The Resource 'Microsoft.IoTOperations/instances/aio-inst' under "
            "resource group 'rg-site-01' was not found."
        )

        assert "rg-site-01" not in scrubbed
        assert "resource group '<resource-group>'" in scrubbed

    def test_resource_group_with_no_closing_quote(self):
        """An unterminated quote redacts the name without consuming the message.

        A message is not always well formed. Matching to the next apostrophe
        anywhere would swallow the diagnostic, and requiring the closing quote
        would leave the name in place.
        """
        scrubbed = scrub(
            "resource group 'rg-site-01 was not found. Check the 'name' argument."
        )

        assert "rg-site-01" not in scrubbed
        assert "was not found" in scrubbed
        assert "'name'" in scrubbed

    def test_onelake_host_is_distinguishable_from_generic_fabric(self):
        """The longest matching suffix wins, so the service stays identifiable.

        Both hosts have their customer-chosen labels removed either way. Naming
        the specific service is what makes the remaining text diagnostic. The
        assertion is an equality rather than a containment, so it pins where the
        surviving suffix sits rather than only that it appears somewhere.
        """
        onelake = scrub("abfss://ws@contoso.onelake.dfs.fabric.microsoft.com/tbl")

        assert "contoso" not in onelake
        assert onelake == "abfss://ws@<host>.onelake.dfs.fabric.microsoft.com/tbl"

    def test_private_endpoint_host_drops_the_account_label(self):
        """A private endpoint adds a label, so only the suffix may survive."""
        scrubbed = scrub("contoso-store.privatelink.blob.core.windows.net")

        assert "contoso-store" not in scrubbed
        assert scrubbed == "<host>.blob.core.windows.net"

    def test_several_identities_in_one_message(self):
        text = (
            f"BadRequest: deployment to {RESOURCE_ID} failed for principal "
            f"{SUBSCRIPTION} against contoso-store.blob.core.windows.net"
        )
        scrubbed = scrub(text)

        assert SUBSCRIPTION not in scrubbed
        assert "contoso-munich-rg" not in scrubbed
        assert "contoso-store" not in scrubbed


class TestScrubKeepsDiagnostics:
    """What makes a failure actionable survives."""

    def test_error_code_and_message_survive(self):
        scrubbed = scrub(
            f"InvalidTemplateDeployment: The template deployment failed. "
            f"Resource {RESOURCE_ID} is invalid."
        )

        assert "InvalidTemplateDeployment" in scrubbed
        assert "The template deployment failed." in scrubbed

    def test_in_cluster_host_is_left_alone(self):
        """The shipped sample's broker host is not an Azure service domain."""
        text = "dial tcp aio-broker.azure-iot-operations:18883: connection refused"

        assert scrub(text) == text

    def test_a_message_with_nothing_to_scrub_is_unchanged(self):
        text = "BadRequest: endpointType 'NotAReal' is not a supported value."

        assert scrub(text) == text

    def test_the_separator_after_a_resource_id_survives(self):
        """A colon ends the id rather than being eaten with it."""
        scrubbed = scrub(
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg"
            f"/providers/Microsoft.Web/sites/mysite: BadRequest"
        )

        assert scrubbed == "<Microsoft.Web/sites>: BadRequest"

    def test_scrubbing_is_idempotent(self):
        once = scrub(f"failed at {RESOURCE_ID} for {SUBSCRIPTION}")

        assert scrub(once) == once

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_input_passes_through(self, value):
        assert scrub(value) == value


class TestRedactionMode:
    """Redaction follows the destination, not the text."""

    def test_off_for_a_local_run(self, clean_env):
        assert is_redaction_enabled() is False
        assert scrub_for_output(RESOURCE_ID) == RESOURCE_ID

    @pytest.mark.parametrize("marker", _CI_MARKERS)
    def test_on_in_a_published_environment(self, clean_env, marker):
        """A workflow added later is covered without opting in."""
        clean_env.setenv(marker, "true")

        assert is_redaction_enabled() is True
        assert SUBSCRIPTION not in scrub_for_output(RESOURCE_ID)

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_opt_in(self, clean_env, value):
        clean_env.setenv(REDACT_ENV, value)

        assert is_redaction_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_explicit_opt_out_wins_over_the_environment(self, clean_env, value):
        """An operator debugging a self-hosted runner turns it off deliberately."""
        clean_env.setenv("GITHUB_ACTIONS", "true")
        clean_env.setenv(REDACT_ENV, value)

        assert is_redaction_enabled() is False
        assert scrub_for_output(RESOURCE_ID) == RESOURCE_ID

    def test_an_unrecognized_value_falls_back_to_the_environment(self, clean_env):
        clean_env.setenv("GITHUB_ACTIONS", "true")
        clean_env.setenv(REDACT_ENV, "maybe")

        assert is_redaction_enabled() is True


class TestOrchestratorAppliesRedaction:
    """The engine's failure reporting routes through the scrubber.

    The tests above prove the scrubber works. These prove it is wired in, which
    is a separate failure: removing the call from `orchestrator.py` leaves every
    test above passing while a failed deploy publishes resource ids again.
    """

    @staticmethod
    def _orchestrator():
        """An instance without running `__init__`, which needs a workspace."""
        return Orchestrator.__new__(Orchestrator)

    def test_site_failure_result_is_scrubbed(self, clean_env):
        clean_env.setenv(REDACT_ENV, "1")
        site = SimpleNamespace(name="munich-prod")
        manifest = SimpleNamespace(steps=[])

        result = Orchestrator._site_failure_result(
            site,
            manifest,
            (
                "Unexpected error in deployment "
                "'manifest-munich-prod-step-20260902000000': "
                f"could not read {RESOURCE_ID}"
            ),
        )

        assert SUBSCRIPTION not in result["error"]
        assert "contoso-munich-rg" not in result["error"]
        assert "munich-prod" not in result["error"]
        assert "manifest-<site>-step" in result["error"]
        assert "Unexpected error" in result["error"]

    def test_site_failure_result_keeps_detail_for_a_local_run(self, clean_env):
        site = SimpleNamespace(name="munich-prod")
        manifest = SimpleNamespace(steps=[])

        result = Orchestrator._site_failure_result(site, manifest, RESOURCE_ID)

        assert result["error"] == RESOURCE_ID

    def test_failed_site_summary_is_scrubbed(self, clean_env, capsys):
        clean_env.setenv(REDACT_ENV, "1")
        results = [
            {
                "site": "munich-prod",
                "status": "failed",
                "error": f"BadRequest: {RESOURCE_ID} is invalid",
                "steps_completed": 0,
                "steps_skipped": 0,
                "steps_total": 1,
                "elapsed": 1.0,
                "steps": [],
            }
        ]

        self._orchestrator()._print_deployment_summary(results, 1.0)

        printed = capsys.readouterr().out
        assert SUBSCRIPTION not in printed
        assert "contoso-munich-rg" not in printed
        assert "BadRequest" in printed

    def test_blocked_site_summary_is_scrubbed(self, clean_env, capsys):
        clean_env.setenv(REDACT_ENV, "1")
        results = [
            {
                "site": "munich-prod",
                "status": "blocked",
                "error": f"upstream failed at {RESOURCE_ID}",
                "steps_completed": 0,
                "steps_skipped": 0,
                "steps_total": 1,
                "elapsed": 0.0,
                "steps": [],
            }
        ]

        self._orchestrator()._print_deployment_summary(results, 1.0)

        printed = capsys.readouterr().out
        assert SUBSCRIPTION not in printed
        assert "contoso-munich-rg" not in printed

    def test_a_failed_step_is_scrubbed_in_the_log_and_the_step_result(
        self, clean_env, capsys, tmp_path, monkeypatch
    ):
        """The deploy path itself, not just the pre-step failure builder.

        This is the call site that produces the live failure log and the
        `steps[].error` entry a run artifact carries, and it is why the E2E
        workflow sets redaction on for the whole job.
        """
        clean_env.setenv(REDACT_ENV, "1")

        from siteops.executor import DeploymentResult
        from siteops.models import DeploymentStep

        orch = self._orchestrator()
        step = DeploymentStep(name="aio-instance", template="templates/x.bicep")
        manifest = SimpleNamespace(name="m", steps=[step], parameters=[])
        site = SimpleNamespace(
            name="munich-prod", subscription="s", resource_group="rg", properties={}, parameters={}
        )

        monkeypatch.setattr(
            type(orch),
            "_execute_step",
            lambda self, *a, **k: DeploymentResult(
                success=False,
                step_name="aio-instance",
                site_name="munich-prod",
                deployment_name="d",
                error=(
                    "Deployment 'm-munich-prod-aio-instance-ts' failed. "
                    f"BadRequest: {RESOURCE_ID} is invalid"
                ),
            ),
        )
        monkeypatch.setattr(
            type(orch),
            "_check_step_site_compatibility",
            lambda self, *a, **k: None,
        )

        result = orch._deploy_site(manifest, site, "ts", parallel_mode=False)

        step_error = result["steps"][0]["error"]
        assert SUBSCRIPTION not in step_error
        assert "contoso-munich-rg" not in step_error
        assert "munich-prod" not in step_error
        assert "m-<site>-aio-instance-ts" in step_error
        assert "BadRequest" in step_error
        output = capsys.readouterr().out
        assert SUBSCRIPTION not in output
        assert "munich-prod" not in output
        assert "[<site>]" in output

    def test_a_local_step_log_keeps_the_site_name(
        self, clean_env, capsys, monkeypatch
    ):
        """Local output keeps the identity that lets an operator act."""
        from siteops.executor import DeploymentResult
        from siteops.models import DeploymentStep

        orch = self._orchestrator()
        step = DeploymentStep(name="aio-instance", template="templates/x.bicep")
        manifest = SimpleNamespace(name="m", steps=[step], parameters=[])
        site = SimpleNamespace(
            name="munich-prod",
            subscription="s",
            resource_group="rg",
            properties={},
            parameters={},
        )

        monkeypatch.setattr(
            type(orch),
            "_execute_step",
            lambda self, *a, **k: DeploymentResult(
                success=True,
                step_name="aio-instance",
                site_name="munich-prod",
                deployment_name="d",
            ),
        )
        monkeypatch.setattr(
            type(orch),
            "_check_step_site_compatibility",
            lambda self, *a, **k: None,
        )

        orch._deploy_site(manifest, site, "ts", parallel_mode=False)

        assert "[munich-prod]" in capsys.readouterr().out

    def test_a_redacted_empty_plan_omits_the_selector(
        self,
        clean_env,
        tmp_workspace,
        capsys,
    ):
        clean_env.setenv(REDACT_ENV, "1")
        manifest_path = tmp_workspace / "manifests" / "private-selector.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: private-selector
selector: "name=private-site"
steps: []
""",
            encoding="utf-8",
        )

        Orchestrator(tmp_workspace).show_plan(manifest_path)

        output = capsys.readouterr().out
        assert "No sites matched" in output
        assert "private-site" not in output

    def test_a_local_empty_plan_keeps_the_selector(
        self,
        clean_env,
        tmp_workspace,
        capsys,
    ):
        manifest_path = tmp_workspace / "manifests" / "private-selector.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: private-selector
selector: "name=private-site"
steps: []
""",
            encoding="utf-8",
        )

        Orchestrator(tmp_workspace).show_plan(manifest_path)

        assert "Manifest selector: name=private-site" in capsys.readouterr().out

    def test_site_load_failure_uses_a_generic_published_diagnostic(
        self,
        clean_env,
        tmp_workspace,
        caplog,
        capsys,
    ):
        clean_env.setenv(REDACT_ENV, "1")
        site_path = tmp_workspace / "sites" / "private-site.yaml"
        site_path.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: private-site
subscription:
resourceGroup: private-resource-group
location: eastus
""",
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING):
            orchestrator = Orchestrator(tmp_workspace)
            assert orchestrator.load_all_sites() == []

        published = caplog.text + capsys.readouterr().err
        assert "private-site" not in published
        assert "private-resource-group" not in published
        assert str(site_path) not in published
        assert "Site configuration could not be loaded" in published
        assert orchestrator.skipped_sites[0][0] == "private-site"

    @pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
    def test_an_unexpected_site_failure_keeps_internal_identity_only(
        self,
        clean_env,
        tmp_workspace,
        caplog,
        monkeypatch,
        parallel,
    ):
        from siteops.models import ParallelConfig, Site

        clean_env.setenv(REDACT_ENV, "1")
        orchestrator = Orchestrator(tmp_workspace)
        manifest = SimpleNamespace(name="m", steps=[])
        site = Site(
            name="private-site",
            subscription="s",
            resource_group="rg",
            location="eastus",
        )

        def fail(*args, **kwargs):
            raise RuntimeError(
                "Deployment 'm-private-site-step-ts' failed for private-site"
            )

        monkeypatch.setattr(orchestrator, "_deploy_site", fail)
        with caplog.at_level(logging.ERROR):
            if parallel:
                result = orchestrator._deploy_parallel(
                    manifest,
                    [site],
                    "ts",
                    ParallelConfig(sites=1),
                )
            else:
                result = orchestrator._deploy_sequential(
                    manifest,
                    [site],
                    "ts",
                )

        assert result[0]["site"] == "private-site"
        assert "private-site" not in result[0]["error"]
        assert "m-<site>-step-ts" in result[0]["error"]
        assert "private-site" not in caplog.text

    def test_deployment_command_and_timeout_warning_hide_the_site(
        self,
        clean_env,
        tmp_workspace,
        caplog,
        monkeypatch,
    ):
        from siteops.executor import (
            DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS,
            ENGINE_TIMEOUT_SENTINEL,
            AzCliExecutor,
        )

        clean_env.setenv(REDACT_ENV, "1")
        executor = AzCliExecutor(tmp_workspace, dry_run=True)
        executor._az_path = "az"

        with caplog.at_level(logging.INFO, logger="siteops.executor"):
            executor._run_az(
                [
                    "deployment",
                    "group",
                    "create",
                    "--name",
                    "m-private-site-step-ts",
                ],
                site_name="private-site",
            )

        assert "private-site" not in caplog.text
        assert "m-<site>-step-ts" in caplog.text

        monkeypatch.setattr(
            executor,
            "_run_az",
            lambda *args, **kwargs: (
                False,
                "",
                ENGINE_TIMEOUT_SENTINEL.format(
                    timeout=DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS
                ),
            ),
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            executor._submit_deployment(
                [],
                "m-private-site-step-ts",
                "step",
                "private-site",
            )

        assert "private-site" not in caplog.text
        assert "m-<site>-step-ts" in caplog.text

    def test_integration_failure_does_not_reinsert_the_site_name(
        self,
        clean_env,
    ):
        from tests.integration.conftest import _assert_deployed

        clean_env.setenv(REDACT_ENV, "1")
        result = {
            "summary": {"failed": 1},
            "sites": {
                "private-site": {
                    "error": "Deployment 'm-private-site-step-ts' failed"
                }
            },
        }

        with pytest.raises(AssertionError) as excinfo:
            _assert_deployed(result, "sample")

        message = str(excinfo.value)
        assert "private-site" not in message
        assert "m-<site>-step-ts" in message

    def test_validation_error_omits_the_selected_site_name(
        self,
        clean_env,
        tmp_workspace,
    ):
        import json

        clean_env.setenv(REDACT_ENV, "1")
        for name in ("private-site-a", "private-site-b"):
            (tmp_workspace / "sites" / f"{name}.yaml").write_text(
                f"""
apiVersion: siteops/v1
kind: Site
name: {name}
subscription: sub
location: eastus
labels:
  environment: dev
""",
                encoding="utf-8",
            )
        (tmp_workspace / "templates" / "empty.json").write_text(
            json.dumps(
                {
                    "$schema": (
                        "https://schema.management.azure.com/schemas/"
                        "2019-04-01/deploymentTemplate.json#"
                    ),
                    "contentVersion": "1.0.0.0",
                    "resources": [],
                }
            ),
            encoding="utf-8",
        )
        manifest_path = tmp_workspace / "manifests" / "validate.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: validate
sites: [private-site-a, private-site-b]
steps:
  - name: deploy
    template: templates/empty.json
    scope: subscription
""",
            encoding="utf-8",
        )

        errors = Orchestrator(tmp_workspace).validate(manifest_path)

        assert errors
        assert all("private-site-a" not in error for error in errors)
        assert all("private-site-b" not in error for error in errors)
        assert any("<site>" in error for error in errors)

    def test_contextual_scrubbing_keeps_the_local_error(self, clean_env):
        error = "Deployment 'm-private-site-step-ts' failed"

        assert scrub_site_for_output(error, "private-site") == error

    def test_contextual_scrubbing_does_not_replace_a_name_inside_words(
        self,
        clean_env,
    ):
        clean_env.setenv(REDACT_ENV, "1")

        assert scrub_site_for_output("Site s succeeded", "s") == (
            "Site <site> succeeded"
        )


class TestCommandScrubbing:
    """A command line is redacted from its argument vector, not after joining.

    The engine logs the commands it runs, and a dry run in CI is exactly where
    an operator previews them. A value can contain a space, since a
    subscription accepts a display name, and once the vector is joined that
    space is indistinguishable from the separator.
    """

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["az", "--resource-group", "rg-plant-01"], ["az", "--resource-group", "<resource-group>"]),
            (["az", "-g", "rg-plant-01"], ["az", "-g", "<resource-group>"]),
            (["az", "--resource-group=rg-plant-01"], ["az", "--resource-group=<resource-group>"]),
            (["az", "--subscription", "Contoso Prod"], ["az", "--subscription", "<subscription>"]),
            (["az", "--subscription=Contoso Prod"], ["az", "--subscription=<subscription>"]),
        ],
        ids=["long", "short", "equals", "spaced-value", "equals-spaced"],
    )
    def test_an_identifying_flag_value_is_replaced(self, argv, expected):
        assert scrub_command(argv) == expected

    @pytest.mark.parametrize(
        "argv",
        [
            ["az", "--name", "prod-g", "eastus"],
            ["az", "--diagnostic-g", "verbose"],
            ["az", "deployment", "group", "create", "--name", "rg-like-name"],
        ],
        ids=["value-ends-in-dash-g", "flag-ends-in-dash-g", "unrelated-flag"],
    )
    def test_a_token_that_merely_ends_in_a_flag_is_untouched(self, argv):
        """Matching on whole tokens is what makes this exact. A pattern without
        a token boundary replaced the word after any token ending in `-g`,
        which destroys a diagnostic without protecting anything."""
        assert scrub_command(argv) == argv

    def test_a_flag_without_a_value_does_not_consume_the_next_flag(self):
        argv = ["az", "x", "--subscription", "--output", "json"]

        assert scrub_command(argv) == argv

    def test_a_flag_after_a_valueless_flag_is_still_scrubbed(self):
        """The token following a valueless flag is classified in its own right.
        Treating it as merely 'not the value' left the `=` form untouched."""
        argv = ["az", "x", "-g", "--subscription=Contoso Prod"]

        assert scrub_command(argv) == ["az", "x", "-g", "--subscription=<subscription>"]

    def test_a_kubeconfig_path_is_replaced(self):
        """A per-proxy kubeconfig lands in the OS temp directory, whose path
        carries the account name on Windows."""
        argv = ["kubectl", "--kubeconfig=/tmp/siteops-abc/kubeconfig", "get", "nodes"]

        assert scrub_command(argv) == [
            "kubectl", "--kubeconfig=<kubeconfig>", "get", "nodes",
        ]

    def test_repeated_flags_are_each_replaced(self):
        argv = ["az", "-g", "rg-one", "--resource-group", "rg-two", "--subscription", "s"]

        assert scrub_command(argv) == [
            "az", "-g", "<resource-group>",
            "--resource-group", "<resource-group>",
            "--subscription", "<subscription>",
        ]

    def test_the_rendered_line_is_plain_when_redaction_is_off(self, monkeypatch):
        """Local output stays copy-pasteable with real values, which is the
        whole reason redaction is conditional."""
        monkeypatch.setenv(REDACT_ENV, "false")

        rendered = scrub_command_for_output(["az", "-g", "rg-plant-01"])

        assert rendered == "az -g rg-plant-01"

    def test_the_rendered_line_is_redacted_when_publishing(self, monkeypatch):
        monkeypatch.setenv(REDACT_ENV, "true")

        rendered = scrub_command_for_output(
            ["az", "-g", "rg-plant-01", "--subscription", "Contoso Prod"]
        )

        assert rendered == "az -g <resource-group> --subscription <subscription>"

    def test_text_rules_still_apply_to_the_rest_of_the_line(self, monkeypatch):
        """Structural replacement covers flag values. Everything else, such as
        a resource id inside a path, still goes through `scrub`."""
        monkeypatch.setenv(REDACT_ENV, "true")

        rendered = scrub_command_for_output(["az", "resource", "show", "--ids", RESOURCE_ID])

        assert SUBSCRIPTION not in rendered
        assert "rg-contoso-munich" not in rendered

    def test_a_dry_run_command_line_is_scrubbed_where_it_is_logged(
        self, clean_env, caplog
    ):
        """The engine's own call site, not the helper behind it.

        A dry run in CI is where an operator previews the commands, so this is
        the line that carries a subscription display name and a resource group
        into a published log. Rendering the vector after joining it, or joining
        it without rendering, both leave the value in place while every helper
        test stays green.
        """
        from siteops.executor import AzCliExecutor

        clean_env.setenv(REDACT_ENV, "1")
        # `az_path` resolves from PATH, so it is pinned here rather than left to
        # whether the Azure CLI happens to be installed on the runner.
        clean_env.setattr(AzCliExecutor, "az_path", property(lambda self: "az"))
        executor = AzCliExecutor(Path("."), dry_run=True)

        with caplog.at_level(logging.INFO, logger="siteops.executor"):
            executor._run_az(
                [
                    "deployment",
                    "group",
                    "create",
                    "--subscription",
                    "Contoso Prod",
                    "-g",
                    "rg-contoso-munich",
                ]
            )

        assert "Contoso Prod" not in caplog.text
        assert "rg-contoso-munich" not in caplog.text
        assert "<subscription>" in caplog.text
        assert "<resource-group>" in caplog.text


class TestUserPrincipalNames:
    """An address identifies a person, and Azure echoes one back.

    `lastModifiedBy` was one of the values in a real disclosure, and the Arc
    proxy failure tail this engine retains can carry the same shape.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "lastModifiedBy jane.operator@contoso.com",
            "principal admin_user@contoso.onmicrosoft.com was denied",
            "AADSTS500011: user+tag@sub.contoso.co.uk not found",
            "lastModifiedBy alice_fabrikam.com#EXT#@contoso.onmicrosoft.com",
        ],
        ids=["dotted", "underscore", "plus-and-subdomain", "guest"],
    )
    def test_the_local_part_is_removed(self, text):
        scrubbed = scrub(text)

        assert "@" in scrubbed, "the domain is kept, since it is what stays diagnostic"
        assert "<user>@" in scrubbed
        local_part = text.split("@")[0].split()[-1]
        assert local_part not in scrubbed

    def test_the_domain_survives_so_the_message_stays_useful(self):
        assert scrub("owner jane@contoso.com") == "owner <user>@contoso.com"

    def test_text_without_an_address_is_untouched(self):
        text = "deployment failed at step aio-enablement"

        assert scrub(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "abfss://my-ws@account.dfs.core.windows.net/data",
            "abfss://ws@account.dfs.core.windows.net",
            "https://svc-account@host.example.com/path",
            "mqtt://broker.contoso.io:8883",
        ],
        ids=["hyphenated-userinfo", "plain-userinfo", "https-userinfo", "no-userinfo"],
    )
    def test_a_url_authority_is_left_to_the_host_rule(self, text):
        """The userinfo of a URL is not a person, and rewriting part of it
        produced an address that no longer resolved. Everything before the
        authority has to survive so the operator can still act on the message."""
        scrubbed = scrub(text)

        assert "<user>" not in scrubbed
        assert scrubbed.startswith(text.split("://")[0] + "://")

    def test_scrubbing_an_already_scrubbed_message_changes_nothing(self):
        """Output can pass a boundary twice, and a second pass that rewrote the
        placeholder would corrupt a message the first pass made safe."""
        once = scrub("owner jane@contoso.com denied on host.contoso.blob.core.windows.net")

        assert scrub(once) == once

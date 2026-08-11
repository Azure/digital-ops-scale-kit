"""Redaction of environment-identifying values in engine output.

`siteops/sanitize.py` decides what a published surface may carry. These tests
pin both halves: that a scrub removes every identifying shape while leaving the
diagnostic intact, and that it applies exactly where the destination is public.

Every test that reads the mode controls the environment explicitly. The suite
itself runs under `GITHUB_ACTIONS` in CI, which turns redaction on, so a test
asserting the local default would otherwise pass locally and fail in CI.
"""

from types import SimpleNamespace

import pytest

from siteops.orchestrator import Orchestrator
from siteops.sanitize import (
    REDACT_ENV,
    is_redaction_enabled,
    scrub,
    scrub_for_output,
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
            site, manifest, f"Unexpected error: could not read {RESOURCE_ID}"
        )

        assert SUBSCRIPTION not in result["error"]
        assert "contoso-munich-rg" not in result["error"]
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
                error=f"BadRequest: {RESOURCE_ID} is invalid",
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
        assert "BadRequest" in step_error
        assert SUBSCRIPTION not in capsys.readouterr().out

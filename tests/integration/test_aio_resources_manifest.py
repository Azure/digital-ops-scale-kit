"""Integration tests for the config-driven catalog entry point.

`manifests/aio-resources.yaml` is the fleet route: a site names a committed set
through `properties.resourceSets.<family>`, the catalog resolves that to a
declaration file under `parameters/<family>/`, and the family gate opens.

`test_dataflow_sample_manifest.py` already qualifies the dataflow templates
through a manifest-attached declaration, so the assertions here are about the
selection mechanism instead: that the gate opened, that the set the site named
is what deployed, and that per-site values in the declaration resolved to this
site rather than shipping as literal placeholder text.
"""

import pytest

from tests.integration.conftest import CATALOG_SET, WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_succeeded,
    skip_unless_health_is_reported,
)
from tests.integration.helpers.kube import KubectlError, wait_for_cr, wait_for_cr_health

pytestmark = [pytest.mark.integration]

AIO_RESOURCES_MANIFEST = WORKSPACE_PATH / "manifests" / "aio-resources.yaml"

CATALOG_STEP = "dataflow-resources"

# Declared by parameters/dataflows/site-telemetry.yaml.
SET_ENDPOINT_NAME = "site-telemetry-out"
SET_DATAFLOW_NAME = "site-telemetry"

# The set omits `dataflowProfiles`, so its dataflow runs in the pool the
# instance template creates.
INSTANCE_OWNED_PROFILE = "default"


class TestCatalogSelection:
    """A site's `resourceSets` selection is what drives the deploy."""

    def test_all_sites_succeeded(self, aio_resources_result):
        """Covers the summary's failure count, which counts these same sites."""
        assert aio_resources_result["summary"]["failed"] == 0
        for name, site in aio_resources_result["sites"].items():
            assert site["status"] == "success", (
                f"Site '{name}' failed: {site.get('error')}"
            )

    def test_the_gate_opened_and_deployed_the_selected_set(self, aio_resources_result):
        """The family ran, and what it reported is what the named set declares.

        Two failures share this assertion. `_evaluate_condition` fails open, so
        a gate that never opens leaves the manifest succeeding having deployed
        nothing. And a path that resolved to some other declaration would also
        deploy clean. Offline tests cover the gate and the path in isolation.
        Only a deploy shows that the file which resolved is the file that
        reached the resource provider.
        """
        for name in aio_resources_result["sites"]:
            step = assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            endpoints = assert_output_exists(step, "endpointNames")
            dataflows = assert_output_exists(step, "dataflowNames")

            assert SET_ENDPOINT_NAME in endpoints, (
                f"Site '{name}' selected set '{CATALOG_SET}' but reported "
                f"endpoints {endpoints}, which does not include "
                f"'{SET_ENDPOINT_NAME}'."
            )
            assert SET_DATAFLOW_NAME in dataflows, (
                f"Site '{name}' selected set '{CATALOG_SET}' but reported "
                f"dataflows {dataflows}."
            )

    def test_omitted_key_creates_nothing(self, aio_resources_result):
        """The set declares no profiles, so the family creates none.

        An omitted declaration key has to reach the template as an empty array
        rather than as a missing parameter, which would fail the deploy.
        """
        for name in aio_resources_result["sites"]:
            step = assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            assert assert_output_exists(step, "profileNames") == [], (
                f"Site '{name}': the selected set declares no profiles, so the "
                f"family should report none."
            )


class TestPerSiteValuesResolve:
    """A site value inside a declaration resolves to the site that deployed it.

    One committed file deployed fleet-wide with per-site values is the reason
    the selection mechanism exists. An unresolved `{{ site.name }}` would deploy
    clean and put every site on one topic, which no ARM-level assertion catches.

    kubectl reads whichever single cluster the kubeconfig routes to, so these
    assert against the resource on that cluster rather than looping over every
    deployed site. The topic is checked to belong to one of the sites this run
    deployed, which is site-independent and still catches both an unresolved
    template and a value that resolved to the wrong thing.
    """

    def test_destination_topic_carries_a_deployed_site_name(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        deployed = set(aio_resources_result["sites"])
        try:
            dataflow = wait_for_cr(
                "dataflows.connectivity.iotoperations.azure.com",
                SET_DATAFLOW_NAME,
                aio_namespace,
            )
        except KubectlError as e:
            pytest.fail(
                f"Dataflow '{SET_DATAFLOW_NAME}' from the selected set is not "
                f"on the cluster: {e}"
            )

        destinations = [
            op.get("destinationSettings", {}).get("dataDestination")
            for op in dataflow.get("spec", {}).get("operations", [])
            if op.get("operationType") == "Destination"
        ]
        assert destinations, "Projected dataflow has no Destination operation."

        for destination in destinations:
            assert "{{" not in destination, (
                f"Destination topic '{destination}' still carries an "
                f"unresolved template, so every site would publish to the same "
                f"literal topic."
            )
            owner = destination.rsplit("/", 1)[-1]
            assert owner in deployed, (
                f"Destination topic '{destination}' ends in '{owner}', which "
                f"is not one of the sites this run deployed ({sorted(deployed)}). "
                f"The per-site value resolved to something else."
            )


class TestProfilePlacement:
    """A dataflow lands in the pool the declaration selected.

    `dataflowProfileRefs` reports the resolved `profileRef ?? defaultProfileName`
    for each entry, which is the same expression the resource name is built
    from. A successful deploy reporting `default` is therefore what shows the
    resource was created under `dataflowProfiles/default` rather than under a
    pool the declaration never named.
    """

    def test_omitted_profile_ref_resolves_to_the_default_pool(self, aio_resources_result):
        """The selected set declares no `profileRef`, so it uses `default`."""
        for name in aio_resources_result["sites"]:
            step = assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            placements = assert_output_exists(step, "dataflowProfileRefs")

            assert placements == [INSTANCE_OWNED_PROFILE], (
                f"Site '{name}': the selected set declares one dataflow with no "
                f"`profileRef`, so it should run in '{INSTANCE_OWNED_PROFILE}'. "
                f"Reported placements: {placements}."
            )

    def test_instance_owned_profile_survives_the_deploy(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        """The catalog deploy leaves the `default` profile the instance owns alone.

        The instance template creates and sizes this profile. A declaration
        naming it would give one resource two writers, and the dataflow this set
        declares runs in it, so its spec surviving is what the dataflow depends
        on.
        """
        profile = wait_for_cr(
            "dataflowprofiles.connectivity.iotoperations.azure.com",
            INSTANCE_OWNED_PROFILE,
            aio_namespace,
        )
        instance_count = profile.get("spec", {}).get("instanceCount")

        assert isinstance(instance_count, int), (
            f"The instance-owned '{INSTANCE_OWNED_PROFILE}' profile reports "
            f"instanceCount={instance_count!r}. The instance template sizes this "
            f"pool, so a missing count means a catalog deploy overwrote a "
            f"resource it does not own."
        )


class TestCatalogDataflowHealth:
    """The dataflow the site selected reports healthy, not merely created.

    This is the fleet route: a site names a set, the catalog resolves it, and
    the family deploys. Every other assertion here reads the projected `spec`,
    which matches the declaration whether or not the dataflow runs. AIO
    aggregates per-instance reports from the data plane into
    `status.healthState`, so this is what proves the deploy produced a working
    dataflow rather than an accepted one.
    """

    def test_selected_dataflow_reports_available(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        for name in aio_resources_result["sites"]:
            step = assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            skip_unless_health_is_reported(step)
        wait_for_cr_health(
            "dataflows.connectivity.iotoperations.azure.com",
            SET_DATAFLOW_NAME,
            aio_namespace,
        )

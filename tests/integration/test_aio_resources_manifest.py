"""Integration tests for the config-driven catalog entry point.

`manifests/aio-resources.yaml` is the fleet route: a site composes ordered sets
through `properties.resourceSets.<area>`, the catalog resolves each set to a
definition file under `parameters/<area>/`, and the deployment gate opens.

`test_dataflow_sample_manifest.py` already qualifies the dataflow templates
through a manifest-attached declaration, so the assertions here are about the
selection mechanism instead: that each gate opened, that the sets the site named
are what deployed, and that per-site values in the declarations resolved to this
site rather than shipping as literal placeholder text.

Every resource area is selected in one deploy. Devices and assets share one
internally ordered step, followed by the independently gated dataflow step.
"""

import pytest

from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_step_succeeded,
    skip_unless_health_is_reported,
)
from tests.integration.helpers.kube import KubectlError, wait_for_cr, wait_for_cr_health
from tests.integration.helpers.releases import load_aio_release

pytestmark = [pytest.mark.integration]

AIO_RESOURCES_MANIFEST = WORKSPACE_PATH / "manifests" / "aio-resources.yaml"

CATALOG_STEP = "dataflow-resources"
ASSET_CATALOG_STEP = "asset-resources"

# Declared by parameters/dataflows/site-telemetry.yaml.
SET_ENDPOINT_NAME = "site-telemetry-out"
SET_DATAFLOW_NAME = "site-telemetry"

# Declared by parameters/assets/site-assets.yaml.
SET_DEVICE_NAME = "site-opc-ua"
SET_ASSET_NAME = "site-oven"
SET_DEVICE_ENDPOINT_NAME = "opc-ua-connector-0"

# The set omits `dataflowProfiles`, so its dataflow runs in the pool the
# instance template creates.
INSTANCE_OWNED_PROFILE = "default"

# Devices and assets project into this group, with kinds `Device` and `Asset`.
DEVICE_CR_TYPE = "devices.namespaces.deviceregistry.microsoft.com"
ASSET_CR_TYPE = "assets.namespaces.deviceregistry.microsoft.com"


def _projected(resource_type: str, name: str, namespace: str) -> dict:
    """One projected custom resource, failing with what the deploy claimed.

    ARM accepting the write and the custom location projecting the resource are
    separate events, so the read waits. A resource that never appears means the
    deploy reported a creation the cluster never received, which reads better as
    a named failure than as a kubectl error.
    """
    try:
        return wait_for_cr(resource_type, name, namespace)
    except KubectlError as e:
        pytest.fail(
            f"'{name}' ({resource_type}) from the selected set is not on the "
            f"cluster: {e}"
        )


class TestCatalogSelection:
    """A site's `resourceSets` selection is what drives the deploy."""

    def test_all_sites_succeeded(self, aio_resources_result):
        """Covers the summary's failure count, which counts these same sites."""
        assert aio_resources_result["summary"]["failed"] == 0
        for name, site in aio_resources_result["sites"].items():
            assert site["status"] == "success", (
                f"Site '{name}' failed: {site.get('error')}"
            )

    def test_selected_family_steps_ran(self, aio_resources_result):
        """Both deployment families ran for the selected resource areas."""
        for name in aio_resources_result["sites"]:
            assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            assert_step_succeeded(
                aio_resources_result,
                name,
                ASSET_CATALOG_STEP,
            )


class TestPerSiteValuesResolve:
    """A site value inside a declaration resolves to the site that deployed it.

    One committed file deployed fleet-wide with per-site values is the reason
    the selection mechanism exists.     An unresolved `{{ site.name }}` would fail before deployment. These live
    checks prove the resolved value reached the provider.

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

    def test_asset_display_name_carries_a_deployed_site_name(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        """The asset's operator-visible name resolved to this site.

        The declaration sets `displayName` from `{{ site.name }}`, which is what
        an operator reads in the portal to tell one site's oven from another's.
        The runtime rejects an unresolved template. This assertion proves the
        resolved label reached the provider.
        """
        deployed = set(aio_resources_result["sites"])
        asset = _projected(ASSET_CR_TYPE, SET_ASSET_NAME, aio_namespace)

        display_name = asset.get("spec", {}).get("displayName")
        assert isinstance(display_name, str) and display_name, (
            f"Asset '{SET_ASSET_NAME}' projects displayName "
            f"{display_name!r}, so the per-site value it carries cannot be read."
        )
        assert "{{" not in display_name, (
            f"Asset displayName '{display_name}' still carries an unresolved "
            f"template, so every site would show the same label."
        )
        assert any(site in display_name for site in deployed), (
            f"Asset displayName '{display_name}' names none of the sites this "
            f"run deployed ({sorted(deployed)}). The per-site value resolved to "
            f"something else."
        )

    def test_asset_dataset_topic_carries_a_deployed_site_name(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        """The dataset destination topic resolved to this site.

        This is the fan-out the asset family is sold on: one committed
        declaration, and each site publishing its oven data under its own topic.
        """
        deployed = set(aio_resources_result["sites"])
        asset = _projected(ASSET_CR_TYPE, SET_ASSET_NAME, aio_namespace)

        topics = [
            destination.get("configuration", {}).get("topic")
            for dataset in asset.get("spec", {}).get("datasets", [])
            for destination in dataset.get("destinations", [])
        ]
        topics = [topic for topic in topics if isinstance(topic, str)]
        assert topics, (
            f"Projected asset '{SET_ASSET_NAME}' carries no dataset destination "
            f"topic, so its telemetry has nowhere to go."
        )

        for topic in topics:
            assert "{{" not in topic, (
                f"Dataset topic '{topic}' still carries an unresolved template, "
                f"so every site would publish to the same literal topic."
            )
            assert any(f"/{site}/" in topic for site in deployed), (
                f"Dataset topic '{topic}' names none of the sites this run "
                f"deployed ({sorted(deployed)}). The per-site value resolved to "
                f"something else."
            )


class TestProjectedAssetTopology:
    """The device and the asset reached the cluster, bound to each other.

    These assertions read what the custom location actually projected, which is
    the only place the binding is visible as the connector sees it. A device
    that is not enabled presents no endpoint, and an asset whose `deviceRef`
    names an endpoint that is not there is created and never served.
    """

    def test_the_device_is_enabled_and_presents_its_inbound_endpoint(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        device = _projected(DEVICE_CR_TYPE, SET_DEVICE_NAME, aio_namespace)
        spec = device.get("spec", {})

        assert spec.get("enabled") is True, (
            f"Device '{SET_DEVICE_NAME}' projects enabled="
            f"{spec.get('enabled')!r}. A device that is not enabled presents no "
            f"endpoint, and the connector then skips every asset on it."
        )

        inbound = spec.get("endpoints", {}).get("inbound", {})
        assert SET_DEVICE_ENDPOINT_NAME in inbound, (
            f"Device '{SET_DEVICE_NAME}' projects inbound endpoints "
            f"{sorted(inbound)}, which does not include "
            f"'{SET_DEVICE_ENDPOINT_NAME}'. The asset binds to that name."
        )

    def test_the_asset_is_enabled_and_bound_to_the_declared_endpoint(
        self, aio_resources_result, aio_namespace, kubectl_available
    ):
        asset = _projected(ASSET_CR_TYPE, SET_ASSET_NAME, aio_namespace)
        spec = asset.get("spec", {})

        assert spec.get("enabled") is True, (
            f"Asset '{SET_ASSET_NAME}' projects enabled={spec.get('enabled')!r}, "
            f"so it is created but never served."
        )

        device_ref = spec.get("deviceRef", {})
        assert device_ref.get("deviceName") == SET_DEVICE_NAME, (
            f"Asset '{SET_ASSET_NAME}' projects deviceRef.deviceName "
            f"{device_ref.get('deviceName')!r}, not '{SET_DEVICE_NAME}'. The "
            f"asset would read through a device this deploy did not create."
        )
        assert device_ref.get("endpointName") == SET_DEVICE_ENDPOINT_NAME, (
            f"Asset '{SET_ASSET_NAME}' projects deviceRef.endpointName "
            f"{device_ref.get('endpointName')!r}, not "
            f"'{SET_DEVICE_ENDPOINT_NAME}'."
        )

class TestInstanceOwnedProfile:
    """The catalog leaves the default profile owned by the instance intact."""

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
        self,
        aio_resources_result,
        aio_namespace,
        kubectl_available,
        orchestrator,
    ):
        for name in aio_resources_result["sites"]:
            assert_step_succeeded(aio_resources_result, name, CATALOG_STEP)
            _, release = load_aio_release(orchestrator, name, WORKSPACE_PATH)
            skip_unless_health_is_reported(release.get("aioApiVersion"))
        wait_for_cr_health(
            "dataflows.connectivity.iotoperations.azure.com",
            SET_DATAFLOW_NAME,
            aio_namespace,
        )

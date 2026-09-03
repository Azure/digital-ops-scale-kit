"""Live qualification for resource sets selected through site configuration."""

import copy
import json
import os
import uuid

import pytest

from tests.integration.conftest import (
    WORKSPACE_PATH,
    _assert_deployed,
    _resolve_or_fail,
)
from tests.integration.helpers.assertions import (
    assert_step_succeeded,
    skip_unless_health_is_reported,
)
from tests.integration.helpers.kube import (
    apply_manifest,
    delete_resource,
    get_pod_logs,
    wait_for_cr,
    wait_for_cr_health,
    wait_for_deployment_ready,
    wait_for_job_complete,
    wait_for_pod_phase,
    wait_for_service_endpoints,
)
from tests.integration.helpers.mqtt import (
    mqtt_roundtrip_pod_manifest,
    mqtt_subscriber_pod_manifest,
)
from tests.integration.helpers.releases import load_aio_release

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SITEOPS_TEST_KUBECONFIG"),
        reason="Resource-set sample qualification runs on the E2E cluster.",
    ),
]

_BASIC_MANIFEST = (
    WORKSPACE_PATH / "samples" / "resource-set-basic" / "manifest.yaml"
)
_ADVANCED_MANIFEST = (
    WORKSPACE_PATH / "samples" / "resource-set-composition" / "manifest.yaml"
)

_DEVICE_TYPE = "devices.namespaces.deviceregistry.microsoft.com"
_ASSET_TYPE = "assets.namespaces.deviceregistry.microsoft.com"
_ENDPOINT_TYPE = "dataflowendpoints.connectivity.iotoperations.azure.com"
_PROFILE_TYPE = "dataflowprofiles.connectivity.iotoperations.azure.com"
_DATAFLOW_TYPE = "dataflows.connectivity.iotoperations.azure.com"

_BASIC_DATAFLOW = "basic-routing"
_EXTERNAL_DEVICE = "external-opc-ua"
_MANAGED_DEVICE = "composition-opc-ua"
_MANAGED_OVEN = "composition-oven"
_MANAGED_BOILER = "composition-boiler"
_EXTERNAL_OVEN = "composition-external-oven"
_MANAGED_ASSETS = (
    _MANAGED_OVEN,
    _MANAGED_BOILER,
    _EXTERNAL_OVEN,
)
_ADVANCED_ENDPOINT = "catalog-mqtt-out"
_ADVANCED_PROFILE = "catalog-profile"
_ADVANCED_DATAFLOW = "catalog-routing"

_SIMULATOR_DEPLOYMENT = "resource-set-opc-plc"
_SIMULATOR_SERVICE = "resource-set-opc-plc"
_SIMULATOR_TRUST_JOB = "resource-set-opc-plc-trust"


def _deploy_with_committed_sample_selection(
    orchestrator,
    selector,
    manifest_path,
    sample_site_name,
):
    if not selector:
        pytest.skip(
            "Resource-set sample qualification needs an explicit live-site "
            "selector. The committed sample sites carry placeholder identity."
        )

    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    if len(sites) != 1:
        pytest.fail(
            "Resource-set live qualification requires exactly one selected "
            "site because the assertions read one cluster."
        )
    sample_site = orchestrator.load_site(sample_site_name)
    sample_resource_sets = copy.deepcopy(
        sample_site.properties["resourceSets"]
    )
    original = {
        id(site): copy.deepcopy(site.properties)
        for site in sites
    }
    for site in sites:
        site.properties["resourceSets"] = copy.deepcopy(sample_resource_sets)

    try:
        result = orchestrator.deploy(
            manifest_path=manifest_path,
            manifest=manifest,
            sites=sites,
        )
        return _assert_deployed(result, manifest_path.parent.name)
    finally:
        for site in sites:
            site.properties.clear()
            site.properties.update(original[id(site)])


@pytest.fixture(scope="session")
def resource_set_basic_result(
    orchestrator,
    selector,
    aio_install_result,
):
    return _deploy_with_committed_sample_selection(
        orchestrator,
        selector,
        _BASIC_MANIFEST,
        "catalog-basic",
    )


@pytest.fixture(scope="session")
def resource_set_composition_result(
    orchestrator,
    selector,
    aio_install_result,
):
    return _deploy_with_committed_sample_selection(
        orchestrator,
        selector,
        _ADVANCED_MANIFEST,
        "catalog-composition",
    )


def _single_site_name(result: dict) -> str:
    names = list(result["sites"])
    assert len(names) == 1, (
        "Resource-set live qualification reads one cluster and therefore "
        "requires exactly one selected site."
    )
    return names[0]


def _delete_mqtt_clients(namespace: str, pod_names: tuple[str, ...], sa_name: str) -> None:
    for pod_name in pod_names:
        delete_resource("pod", pod_name, namespace, wait=False)
    delete_resource("serviceaccount", sa_name, namespace, wait=False)


class TestResourceSetBasicSample:
    """One selected set produces one healthy, observable dataflow."""

    def test_sample_steps_succeed(self, resource_set_basic_result):
        site_name = _single_site_name(resource_set_basic_result)
        assert_step_succeeded(
            resource_set_basic_result,
            site_name,
            "resolve-aio",
        )
        assert_step_succeeded(
            resource_set_basic_result,
            site_name,
            "dataflow-resources",
        )

    def test_dataflow_reports_available(
        self,
        resource_set_basic_result,
        aio_namespace,
        kubectl_available,
        orchestrator,
    ):
        site_name = _single_site_name(resource_set_basic_result)
        _, release = load_aio_release(
            orchestrator,
            site_name,
            WORKSPACE_PATH,
        )
        skip_unless_health_is_reported(release.get("aioApiVersion"))
        wait_for_cr_health(
            _DATAFLOW_TYPE,
            _BASIC_DATAFLOW,
            aio_namespace,
        )

    def test_unique_message_reaches_the_destination(
        self,
        resource_set_basic_result,
        aio_namespace,
        kubectl_available,
    ):
        site_name = _single_site_name(resource_set_basic_result)
        nonce = uuid.uuid4().hex
        source_topic = (
            f"azure-iot-operations/data/siteops-samples/basic/{nonce}"
        )
        destination_topic = f"siteops-samples/{site_name}/basic"
        payload = json.dumps(
            {"siteopsProbe": nonce},
            separators=(",", ":"),
        )
        suffix = nonce[:8]
        sa_name = f"resource-set-basic-{suffix}"
        pod_name = f"resource-set-basic-{suffix}"

        _delete_mqtt_clients(
            aio_namespace,
            (pod_name,),
            sa_name,
        )
        try:
            apply_manifest(
                mqtt_roundtrip_pod_manifest(
                    sa_name=sa_name,
                    pod_name=pod_name,
                    namespace=aio_namespace,
                    source_topic=source_topic,
                    destination_topic=destination_topic,
                    payload=payload,
                    wait_seconds=180,
                )
            )
            wait_for_pod_phase(pod_name, aio_namespace, timeout=240)
            received = get_pod_logs(pod_name, aio_namespace)
            if nonce not in received:
                raise AssertionError(
                    "The beginner roundtrip completed without the current "
                    "run's probe value."
                )
        finally:
            _delete_mqtt_clients(
                aio_namespace,
                (pod_name,),
                sa_name,
            )


class TestResourceSetCompositionSample:
    """Inherited sets resolve to healthy provider and routing behavior."""

    def test_all_sample_steps_succeed(self, resource_set_composition_result):
        site_name = _single_site_name(resource_set_composition_result)
        for step_name in (
            "external-opc-ua-device",
            "external-opc-plc-simulator",
            "resolve-aio",
            "asset-resources",
            "dataflow-resources",
        ):
            assert_step_succeeded(
                resource_set_composition_result,
                site_name,
                step_name,
            )

    def test_external_and_managed_topology_projects(
        self,
        resource_set_composition_result,
        aio_namespace,
        kubectl_available,
    ):
        wait_for_job_complete(_SIMULATOR_TRUST_JOB, aio_namespace)
        external = wait_for_cr(
            _DEVICE_TYPE,
            _EXTERNAL_DEVICE,
            aio_namespace,
        )
        external_inbound = (
            external.get("spec", {})
            .get("endpoints", {})
            .get("inbound", {})
        )
        assert external_inbound.get("opc-ua-connector-0", {}).get(
            "endpointType"
        ) == "Microsoft.OpcUa"

        wait_for_cr(_DEVICE_TYPE, _MANAGED_DEVICE, aio_namespace)
        for asset_name in _MANAGED_ASSETS:
            asset = wait_for_cr(_ASSET_TYPE, asset_name, aio_namespace)
            device_ref = asset.get("spec", {}).get("deviceRef", {})
            expected_device = (
                _EXTERNAL_DEVICE
                if asset_name == _EXTERNAL_OVEN
                else _MANAGED_DEVICE
            )
            assert device_ref.get("deviceName") == expected_device, (
                f"Asset '{asset_name}' did not project the device selected by "
                "the committed composition."
            )

        wait_for_cr(_ENDPOINT_TYPE, _ADVANCED_ENDPOINT, aio_namespace)
        wait_for_cr(_PROFILE_TYPE, _ADVANCED_PROFILE, aio_namespace)
        wait_for_cr(_DATAFLOW_TYPE, _ADVANCED_DATAFLOW, aio_namespace)

    def test_dataflow_reports_available(
        self,
        resource_set_composition_result,
        aio_namespace,
        kubectl_available,
        orchestrator,
    ):
        site_name = _single_site_name(resource_set_composition_result)
        _, release = load_aio_release(
            orchestrator,
            site_name,
            WORKSPACE_PATH,
        )
        skip_unless_health_is_reported(release.get("aioApiVersion"))
        wait_for_cr_health(
            _DATAFLOW_TYPE,
            _ADVANCED_DATAFLOW,
            aio_namespace,
        )

    def test_every_asset_topic_is_routed_independently(
        self,
        resource_set_composition_result,
        aio_namespace,
        kubectl_available,
    ):
        site_name = _single_site_name(resource_set_composition_result)
        wait_for_deployment_ready(
            _SIMULATOR_DEPLOYMENT,
            aio_namespace,
        )
        wait_for_service_endpoints(
            _SIMULATOR_SERVICE,
            aio_namespace,
        )

        suffixes = ("oven", "boiler", "external-oven")
        run_suffix = uuid.uuid4().hex[:8]
        pod_names = tuple(
            f"resource-set-{index}-{run_suffix}"
            for index in range(len(suffixes))
        )
        sa_name = f"resource-set-advanced-{run_suffix}"

        try:
            for pod_name, suffix in zip(pod_names, suffixes, strict=True):
                source_topic = (
                    "azure-iot-operations/data/"
                    f"{site_name}/resource-set-composition/{suffix}"
                )
                destination_topic = (
                    f"catalog/{site_name}/{source_topic}"
                )
                apply_manifest(
                    mqtt_subscriber_pod_manifest(
                        sa_name=sa_name,
                        pod_name=pod_name,
                        namespace=aio_namespace,
                        topic=destination_topic,
                        wait_seconds=360,
                    )
                )

            for pod_name in pod_names:
                wait_for_pod_phase(
                    pod_name,
                    aio_namespace,
                    timeout=420,
                )
                assert get_pod_logs(pod_name, aio_namespace).strip(), (
                    "An advanced-sample destination produced no MQTT payload."
                )
        finally:
            _delete_mqtt_clients(aio_namespace, pod_names, sa_name)

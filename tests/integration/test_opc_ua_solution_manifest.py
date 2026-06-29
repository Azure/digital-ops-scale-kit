"""Integration tests for the opc-ua-solution sample manifest."""

import json
import time

import pytest

from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_succeeded,
    find_step,
)
from tests.integration.helpers.kube import (
    KubectlError,
    apply_manifest,
    delete_resource,
    get_pod_logs,
    kubectl_json,
    wait_for_deployment_ready,
    wait_for_pod_phase,
    wait_for_service_endpoints,
)
from tests.integration.helpers.mqtt import mqtt_subscriber_pod_manifest
from tests.integration.helpers.opcua import dump_opc_ua_connector_status

pytestmark = [pytest.mark.integration]

OPC_UA_SOLUTION_MANIFEST = WORKSPACE_PATH / "samples" / "opc-ua-solution" / "manifest.yaml"

# The simulator manifest applied by the opc-plc-simulator step (pinned ref in
# samples/opc-ua-solution/_partial.yaml). The names below are fixed by that
# upstream manifest and would only change with a deliberate pinned-ref bump.
SIMULATOR_DEPLOYMENT_NAME = "opc-plc-000000"
SIMULATOR_SERVICE_NAME = "opcplc-000000"

# The oven asset / dataflow names are stamped by samples/opc-ua-solution/template.bicep.
OVEN_ASSET_NAME = "oven"
OVEN_DATAFLOW_NAME = "opc-ua-solution-oven-dataflow"
OVEN_MQTT_TOPIC = "azure-iot-operations/data/oven"
EXPECTED_OVEN_DATA_KEYS = {"Temperature", "EnergyUse", "Weight"}


class TestOpcUaSolutionDeployment:
    """Validate that samples/opc-ua-solution/manifest.yaml deploys successfully."""

    def test_no_failures(self, opc_ua_solution_result):
        assert opc_ua_solution_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            site = opc_ua_solution_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"

    def test_opc_ua_solution_step_succeeds(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")

    def test_event_hub_outputs(self, opc_ua_solution_result):
        """Dataflow destination must be reachable. Surface the Event Hub
        name/namespace that downstream consumers (e.g., tests, dashboards)
        key off. Catches template regressions where the output object
        shape changes silently."""
        for name in opc_ua_solution_result["sites"]:
            step = assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")
            event_hub = assert_output_exists(step, "eventHub")
            assert isinstance(event_hub, dict), (
                f"Site '{name}': eventHub output is not an object: {event_hub!r}"
            )
            for key in ("name", "namespace"):
                assert event_hub.get(key), (
                    f"Site '{name}': eventHub.{key} missing "
                    f"(keys: {sorted(event_hub.keys())})"
                )

    def test_resolved_extension_name_output(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            step = assert_step_succeeded(opc_ua_solution_result, name, "opc-ua-solution")
            assert_output_exists(step, "resolvedExtensionName")


class TestOpcUaSolutionSimulator:
    """Validate that the opc-plc-simulator step deploys successfully.

    The simulator is part of the sample's core layer and runs unconditionally
    when the sample is deployed.
    """

    def test_simulator_succeeds(self, opc_ua_solution_result):
        for name in opc_ua_solution_result["sites"]:
            step = find_step(opc_ua_solution_result, name, "opc-plc-simulator")
            assert step["status"] == "success", (
                f"Site '{name}': opc-plc-simulator status was {step['status']}"
            )


class TestOpcUaSolutionIdempotency:
    """Validate that re-deploying produces the same results."""

    def test_redeploy_preserves_outputs(
        self, orchestrator, selector, opc_ua_solution_result
    ):
        """Event Hub name/namespace must be stable across redeploys. A change
        indicates resources were recreated, which breaks any consumer that
        cached the endpoint."""
        result2 = orchestrator.deploy(
            manifest_path=OPC_UA_SOLUTION_MANIFEST,
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0

        for name in opc_ua_solution_result["sites"]:
            step1 = find_step(opc_ua_solution_result, name, "opc-ua-solution")
            step2 = find_step(result2, name, "opc-ua-solution")
            eh1 = assert_output_exists(step1, "eventHub")
            eh2 = assert_output_exists(step2, "eventHub")
            assert eh1 == eh2, (
                f"Site '{name}': eventHub output changed on redeploy "
                f"({eh1!r} -> {eh2!r})"
            )


class TestOpcUaSolutionCrossManifestJoin:
    """Validate the sample resolves to the AIO instance produced by this
    run's aio-install, not some pre-existing instance in the resource
    group. Closes a class of "wrong-target" defects that per-manifest
    tests cannot see.
    """

    def test_resolved_extension_matches_install(
        self, aio_install_result, opc_ua_solution_result
    ):
        """The sample's resolved AIO Arc extension name must equal the
        name the install just produced. A mismatch means the sample
        resolved to a different AIO instance and any subsequent
        assertions about deployment correctness are off-target."""
        for name in opc_ua_solution_result["sites"]:
            sample_step = find_step(opc_ua_solution_result, name, "opc-ua-solution")
            install_step = find_step(aio_install_result, name, "aio-instance")

            sample_ext = assert_output_exists(sample_step, "resolvedExtensionName")
            install_ext = assert_output_exists(install_step, "aioExtension")
            install_ext_name = (
                install_ext.get("name") if isinstance(install_ext, dict) else None
            )

            assert install_ext_name, (
                f"Site '{name}': aio-install aio-instance.aioExtension has no "
                f"`name` field (got {install_ext!r})"
            )
            assert sample_ext == install_ext_name, (
                f"Site '{name}': opc-ua-solution.resolvedExtensionName "
                f"({sample_ext!r}) does not match aio-install's "
                f"aio-instance.aioExtension.name ({install_ext_name!r}). "
                f"The sample resolved to a different AIO instance."
            )


class TestOpcUaSolutionSimulatorRuntime:
    """Validate the OPC PLC simulator is actually running on the cluster.

    The base `TestOpcUaSolutionSimulator` only confirms the kubectl apply
    step returned OK. A successful apply does not guarantee the pod
    became Ready (image pull, cert-manager Issuer, projected volume, or
    selector typos can all leave the apply 'green' but the simulator
    down). These tests close that gap.
    """

    def test_simulator_deployment_ready(
        self, opc_ua_solution_result, aio_namespace, kubectl_available
    ):
        """The opc-plc-000000 Deployment must reach `readyReplicas` >= 1."""
        for name in opc_ua_solution_result["sites"]:
            try:
                wait_for_deployment_ready(
                    SIMULATOR_DEPLOYMENT_NAME,
                    aio_namespace,
                    min_ready_replicas=1,
                    timeout=300,
                    interval=10,
                )
            except (TimeoutError, KubectlError) as e:
                pytest.fail(
                    f"Site '{name}': simulator deployment "
                    f"`{SIMULATOR_DEPLOYMENT_NAME}` in `{aio_namespace}` "
                    f"never became Ready: {e}"
                )

    def test_simulator_service_has_endpoints(
        self, opc_ua_solution_result, aio_namespace, kubectl_available
    ):
        """The opcplc-000000 Service selector must resolve to >=1 ready pod
        endpoint. A Service with zero endpoints means the AIO OPC UA
        connector can resolve the DNS name but every TCP connect refuses.

        Short poll absorbs the EndpointSlice controller's propagation
        lag after the deployment becomes Ready.
        """
        for name in opc_ua_solution_result["sites"]:
            try:
                wait_for_service_endpoints(
                    SIMULATOR_SERVICE_NAME,
                    aio_namespace,
                    min_addresses=1,
                    timeout=60,
                    interval=2,
                )
            except TimeoutError as e:
                pytest.fail(
                    f"Site '{name}': Service `{SIMULATOR_SERVICE_NAME}` in "
                    f"`{aio_namespace}` never got a Ready endpoint. The "
                    f"Service selector does not match any Running+Ready "
                    f"pods. {e}"
                )


class TestOpcUaSolutionDataflowRuntime:
    """Validate the dataflow CR projected from ARM is healthy on the cluster.

    The Bicep step succeeding only means ARM accepted the PUT. The
    custom-location-backed projection to a cluster-side dataflow CR is
    where misconfiguration (e.g., endpoint authentication problems)
    surfaces. Status schema is intentionally release-version-tolerant:
    we assert the CR exists and is not in an obviously-failed phase.
    """

    def test_dataflow_cr_present_on_cluster(
        self, opc_ua_solution_result, aio_namespace, kubectl_available
    ):
        for name in opc_ua_solution_result["sites"]:
            try:
                dataflow = kubectl_json(
                    [
                        "get",
                        "dataflows.connectivity.iotoperations.azure.com",
                        OVEN_DATAFLOW_NAME,
                        "-n",
                        aio_namespace,
                    ]
                )
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': dataflow CR `{OVEN_DATAFLOW_NAME}` not "
                    f"retrievable in `{aio_namespace}`: {e}"
                )
            mode = dataflow.get("spec", {}).get("mode")
            assert mode == "Enabled", (
                f"Site '{name}': dataflow `{OVEN_DATAFLOW_NAME}` is not "
                f"Enabled (mode={mode!r})."
            )


class TestOpcUaSolutionDataFlowing:
    """Prove data is flowing from the simulator through the AIO MQTT broker.

    Subscribes to the oven dataset's MQTT topic via an ephemeral pod and
    asserts at least one message arrives with the expected oven schema.
    Proves the full upstream half of the data path: simulator → OPC UA
    connector → broker. The dataflow → Event Hub egress is not asserted
    here. That is the cloud-side scope deferred to a follow-up.
    """

    SA_NAME = "scalekit-mqtt-test-client"
    POD_NAME = "scalekit-mqtt-test-client"
    # Wall-clock budgets. The OPC UA connector cold-start can take several
    # minutes after the asset is created: AIO operator reconciles, connector
    # pod schedules, connector establishes the OPC UA session, polling
    # warms up, then the first MQTT publish lands. Empirically 2603 needs
    # more than 180s on the first deploy. 360s for mosquitto_sub -W gives
    # comfortable headroom. POD_TIMEOUT_SECONDS must exceed it to leave
    # room for apk install plus pod scheduling.
    SUBSCRIBE_WAIT_SECONDS = 360
    POD_TIMEOUT_SECONDS = 600

    @pytest.mark.skip(
        reason=(
            "Skipped pending follow-up. Namespace-asset connector does not "
            "launch a per-asset connector pod on AIO 2603 within the test "
            "budget. Tracked as `opc-ua-connector-namespace-asset-launch`."
        )
    )
    def test_oven_telemetry_observed_on_mqtt(
        self, opc_ua_solution_result, aio_namespace, kubectl_available
    ):
        for name in opc_ua_solution_result["sites"]:
            manifest = mqtt_subscriber_pod_manifest(
                sa_name=self.SA_NAME,
                pod_name=self.POD_NAME,
                namespace=aio_namespace,
                topic=OVEN_MQTT_TOPIC,
                wait_seconds=self.SUBSCRIBE_WAIT_SECONDS,
            )
            # Best-effort cleanup of any residue from a previous attempt
            # (e.g., a prior failed test left the pod behind).
            delete_resource("pod", self.POD_NAME, aio_namespace)
            try:
                apply_manifest(manifest)
                # Wait briefly for the API server to project the pod and
                # for the kubelet to schedule it. The MQTT wait itself
                # happens INSIDE the pod (apk add + mosquitto_sub -W).
                time.sleep(5)
                try:
                    wait_for_pod_phase(
                        self.POD_NAME,
                        aio_namespace,
                        target_phases=("Succeeded",),
                        failure_phases=("Failed",),
                        timeout=self.POD_TIMEOUT_SECONDS,
                        interval=10,
                    )
                except (RuntimeError, TimeoutError) as e:
                    logs = get_pod_logs(self.POD_NAME, aio_namespace)
                    connector_diag = dump_opc_ua_connector_status(
                        OVEN_ASSET_NAME, OVEN_DATAFLOW_NAME, aio_namespace
                    )
                    pytest.fail(
                        f"Site '{name}': MQTT subscriber pod did not "
                        f"Succeed. {e}\nSubscriber pod logs:\n{logs}\n\n"
                        f"OPC UA connector diagnostic:\n{connector_diag}"
                    )
                logs = get_pod_logs(self.POD_NAME, aio_namespace).strip()
                assert logs, (
                    f"Site '{name}': MQTT subscriber pod produced no output. "
                    f"mosquitto_sub exited Succeeded but logged nothing."
                )
                # mosquitto_sub -C 1 emits exactly one payload line. Some
                # AIO connector versions inject trailing whitespace.
                first_line = next(
                    (line for line in logs.splitlines() if line.strip()), ""
                )
                try:
                    payload = json.loads(first_line)
                except json.JSONDecodeError as e:
                    pytest.fail(
                        f"Site '{name}': MQTT payload is not valid JSON: {e}. "
                        f"First line: {first_line!r}"
                    )
                observed_keys = set(payload.keys()) if isinstance(payload, dict) else set()
                shared = observed_keys & EXPECTED_OVEN_DATA_KEYS
                assert shared, (
                    f"Site '{name}': MQTT payload carries none of the "
                    f"expected oven data points. Expected any of "
                    f"{sorted(EXPECTED_OVEN_DATA_KEYS)}, observed keys: "
                    f"{sorted(observed_keys)}."
                )
            finally:
                delete_resource("pod", self.POD_NAME, aio_namespace)
                delete_resource("serviceaccount", self.SA_NAME, aio_namespace)


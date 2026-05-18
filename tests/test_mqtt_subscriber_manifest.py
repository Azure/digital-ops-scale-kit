"""Unit tests for `tests.integration.helpers.mqtt.mqtt_subscriber_pod_manifest`.

The renderer produces a Pod manifest that authenticates to the AIO MQTT
broker via a projected ServiceAccount token. A regression in the
rendered shape (wrong SAT audience, dropped CA mount, malformed
mosquitto_sub command line) would only surface as a failed live-Azure
E2E run, which is expensive feedback. These tests validate the
structural invariants without needing a cluster.
"""

from __future__ import annotations

import yaml

from tests.integration.helpers.mqtt import mqtt_subscriber_pod_manifest


DEFAULT_KWARGS = {
    "sa_name": "mqtt-test-sa",
    "pod_name": "mqtt-test-pod",
    "namespace": "azure-iot-operations",
    "topic": "azure-iot-operations/data/oven",
    "wait_seconds": 180,
}


def _render(**overrides):
    return mqtt_subscriber_pod_manifest(**{**DEFAULT_KWARGS, **overrides})


def _parse(yaml_text):
    return list(yaml.safe_load_all(yaml_text))


def test_renders_two_documents_with_expected_kinds():
    docs = _parse(_render())
    assert len(docs) == 2
    assert [d["kind"] for d in docs] == ["ServiceAccount", "Pod"]


def test_service_account_and_pod_share_namespace_and_sa_link():
    sa, pod = _parse(_render())
    assert sa["metadata"]["name"] == DEFAULT_KWARGS["sa_name"]
    assert sa["metadata"]["namespace"] == DEFAULT_KWARGS["namespace"]
    assert pod["metadata"]["name"] == DEFAULT_KWARGS["pod_name"]
    assert pod["metadata"]["namespace"] == DEFAULT_KWARGS["namespace"]
    assert pod["spec"]["serviceAccountName"] == DEFAULT_KWARGS["sa_name"]


def test_pod_is_one_shot():
    """`restartPolicy: Never` plus mosquitto_sub `-C 1 -W <wait>` makes the
    pod a one-shot witness: Succeeded on first message, Failed on timeout."""
    _, pod = _parse(_render())
    assert pod["spec"]["restartPolicy"] == "Never"


def test_sat_projection_uses_aio_internal_audience():
    """The audience MUST match what AIO's BrokerAuthentication CR accepts
    on install. A mismatch silently 401s every connect attempt."""
    _, pod = _parse(_render())
    broker_sat = next(v for v in pod["spec"]["volumes"] if v["name"] == "broker-sat")
    sources = broker_sat["projected"]["sources"]
    assert len(sources) == 1
    token_source = sources[0]["serviceAccountToken"]
    assert token_source["audience"] == "aio-internal"
    assert token_source["path"] == "broker-sat"
    assert token_source["expirationSeconds"] > 0


def test_trust_bundle_mounts_default_aio_ca_configmap():
    """The AIO CA bundle ConfigMap name is the AIO install's contract.
    A drift here would cause mosquitto_sub to fail TLS verification."""
    _, pod = _parse(_render())
    trust_bundle = next(v for v in pod["spec"]["volumes"] if v["name"] == "trust-bundle")
    assert trust_bundle["configMap"]["name"] == "azure-iot-operations-aio-ca-trust-bundle"


def test_volume_mounts_align_with_volumes():
    _, pod = _parse(_render())
    mounts = pod["spec"]["containers"][0]["volumeMounts"]
    mount_by_name = {m["name"]: m for m in mounts}
    assert set(mount_by_name) == {"broker-sat", "trust-bundle"}
    assert mount_by_name["broker-sat"]["mountPath"] == "/var/run/secrets/tokens"
    assert mount_by_name["trust-bundle"]["mountPath"] == "/var/run/certs"


def test_mosquitto_command_carries_required_flags():
    """A regression on the mosquitto_sub command line (e.g., dropping
    --cafile, dropping the K8S-SAT auth method, dropping the topic
    substitution) is the most common shape this renderer regresses into.
    Assert every load-bearing flag is present in the container args, and
    that the K8S-SAT property pair is ordered correctly."""
    _, pod = _parse(_render())
    args = pod["spec"]["containers"][0]["args"]
    assert len(args) == 1
    command = args[0]
    # Subscription
    assert "mosquitto_sub" in command
    assert f"--topic '{DEFAULT_KWARGS['topic']}'" in command
    # TLS
    assert "--cafile /var/run/certs/ca.crt" in command
    # SAT auth via MQTTv5 -D CONNECT. Ordering matters because mosquitto
    # pairs successive -D CONNECT properties into one property group, and
    # 'authentication-data' is only meaningful when 'authentication-method'
    # precedes it. A refactor that reorders the f-string fragments would
    # be a runtime regression that pure substring matches cannot catch.
    method_idx = command.index("authentication-method 'K8S-SAT'")
    data_idx = command.index("authentication-data $(cat /var/run/secrets/tokens/broker-sat)")
    assert method_idx < data_idx, (
        "authentication-method must precede authentication-data on the "
        "mosquitto_sub command line for the MQTTv5 K8S-SAT property pair "
        "to bind correctly."
    )
    # One-shot behavior
    assert "-C 1" in command
    assert f"-W {DEFAULT_KWARGS['wait_seconds']}" in command


def test_default_broker_endpoint_and_qos():
    """The default broker host, port, and QoS are baked into the AIO
    install contract (BrokerListener defaults). Drift here would only
    surface as a live-Azure E2E failure, so assert them explicitly."""
    _, pod = _parse(_render())
    command = pod["spec"]["containers"][0]["args"][0]
    assert "--host aio-broker" in command
    assert "--port 18883" in command
    assert "--qos 1" in command


def test_topic_and_wait_substitute_into_command():
    """Parameter pass-through. Catches a regression where a refactor
    accidentally hard-codes the topic or the wait."""
    command = _parse(_render(topic="my/custom/topic", wait_seconds=42))[1][
        "spec"
    ]["containers"][0]["args"][0]
    assert "--topic 'my/custom/topic'" in command
    assert "-W 42" in command


def test_image_is_pinned_by_default():
    """Floating `latest` tags would silently break the test the day
    `alpine:latest` ships a `mosquitto-clients` regression."""
    _, pod = _parse(_render())
    assert pod["spec"]["containers"][0]["image"] == "alpine:3.20"


def test_resources_have_both_requests_and_limits():
    """The pod runs in `azure-iot-operations` alongside AIO workloads.
    Missing requests would let kubelet over-schedule. Missing limits
    would let an apk runaway eat the node."""
    _, pod = _parse(_render())
    resources = pod["spec"]["containers"][0]["resources"]
    assert "requests" in resources
    assert "limits" in resources
    assert resources["requests"]["cpu"] and resources["requests"]["memory"]
    assert resources["limits"]["cpu"] and resources["limits"]["memory"]


def test_overrides_threaded_through():
    """All optional kwargs render through to where they belong."""
    docs = _parse(
        _render(
            sat_audience="custom-audience",
            trust_bundle_configmap="custom-ca",
            broker_host="custom-broker",
            broker_port=8883,
            image="alpine:3.21",
            qos=0,
        )
    )
    _, pod = docs
    broker_sat = next(v for v in pod["spec"]["volumes"] if v["name"] == "broker-sat")
    token_source = broker_sat["projected"]["sources"][0]["serviceAccountToken"]
    assert token_source["audience"] == "custom-audience"
    trust_bundle = next(
        v for v in pod["spec"]["volumes"] if v["name"] == "trust-bundle"
    )
    assert trust_bundle["configMap"]["name"] == "custom-ca"
    command = pod["spec"]["containers"][0]["args"][0]
    assert "--host custom-broker --port 8883" in command
    assert "--qos 0" in command
    assert pod["spec"]["containers"][0]["image"] == "alpine:3.21"

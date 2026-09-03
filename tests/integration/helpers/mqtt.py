"""Renderers and helpers for MQTT subscriber test pods.

Lives outside `tests/integration/helpers/kube.py` so that the pure-Python
rendering can be exercised by unit tests in the top-level `tests/`
directory without pulling in any kubectl machinery.
"""

import shlex
import textwrap


def _mqtt_client_pod_manifest(
    *,
    sa_name: str,
    pod_name: str,
    namespace: str,
    container_name: str,
    command: str,
    image: str,
    sat_audience: str,
    trust_bundle_configmap: str,
) -> str:
    """Render the shared ServiceAccount, token, trust, and resource shape."""
    return textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: ServiceAccount
        metadata:
          name: {sa_name}
          namespace: {namespace}
        ---
        apiVersion: v1
        kind: Pod
        metadata:
          name: {pod_name}
          namespace: {namespace}
          labels:
            app.kubernetes.io/component: scalekit-mqtt-test-client
        spec:
          serviceAccountName: {sa_name}
          restartPolicy: Never
          containers:
            - name: {container_name}
              image: {image}
              command: ["sh", "-c"]
              args:
                - >-
                  {command}
              resources:
                limits:
                  cpu: 250m
                  memory: 128Mi
                requests:
                  cpu: 50m
                  memory: 32Mi
              volumeMounts:
                - name: broker-sat
                  mountPath: /var/run/secrets/tokens
                - name: trust-bundle
                  mountPath: /var/run/certs
          volumes:
            - name: broker-sat
              projected:
                sources:
                  - serviceAccountToken:
                      path: broker-sat
                      audience: {sat_audience}
                      expirationSeconds: 3600
            - name: trust-bundle
              configMap:
                name: {trust_bundle_configmap}
        """
    )


def mqtt_subscriber_pod_manifest(
    *,
    sa_name: str,
    pod_name: str,
    namespace: str,
    topic: str,
    wait_seconds: int,
    qos: int = 1,
    message_count: int = 1,
    include_topic: bool = False,
    image: str = "alpine:3.20",
    sat_audience: str = "aio-internal",
    trust_bundle_configmap: str = "azure-iot-operations-aio-ca-trust-bundle",
    broker_host: str = "aio-broker",
    broker_port: int = 18883,
) -> str:
    """Render a ServiceAccount + Pod manifest that subscribes to one MQTT message.

    Follows the reference pattern in
    `Azure-Samples/explore-iot-operations/samples/quickstarts/mqtt-client.yaml`:
    a SAT with `aio-internal` audience is projected into the pod, the
    default AIO CA trust bundle ConfigMap is mounted as the TLS trust
    anchor, and mosquitto_sub authenticates via the MQTTv5 K8S-SAT
    extension.

    The container runs `mosquitto_sub -C <count> -W <wait>` so it exits
    Succeeded after receiving the requested messages and Failed on timeout.

    Args:
        sa_name: ServiceAccount name. Created in the same manifest. The
            SAT projection uses it.
        pod_name: Pod name.
        namespace: Kubernetes namespace. Must match the BrokerListener
            namespace (or callers must supply a fully-qualified
            `broker_host`).
        topic: MQTT topic to subscribe to.
        wait_seconds: max time mosquitto_sub waits for a message.
        qos: MQTT QoS level for the subscription.
        message_count: number of messages to receive before exiting.
        include_topic: include the topic before each payload in pod logs.
        image: container image. Defaults to `alpine:3.20` with
            mosquitto-clients installed at runtime via apk.
        sat_audience: SAT audience that must match the AIO
            BrokerAuthentication CR. The default is the value AIO
            stamps into its BrokerAuthentication on install.
        trust_bundle_configmap: ConfigMap holding the AIO CA bundle.
        broker_host: MQTT broker hostname (defaults to the in-namespace
            Service name).
        broker_port: MQTT broker port. Defaults to the internal TLS
            listener AIO ships by default.

    Returns:
        A multi-document YAML string (ServiceAccount + Pod) ready to be
        piped to `kubectl apply -f -`.

    Safe-input contract:
        The string arguments (`sa_name`, `pod_name`, `namespace`,
        `topic`, `image`, `sat_audience`, `trust_bundle_configmap`,
        `broker_host`) are interpolated into a YAML document with no
        escaping, and `topic` is additionally interpolated inside single
        quotes on a shell command line. Callers must therefore supply
        values that are: shell-single-quote-safe (no `'`), YAML-scalar-safe
        (no `:`, leading `-`/`&`/`*`/`!`/`#`, embedded `"`), and DNS-label-safe
        for identifier-typed fields. This module is intended for internal
        test callers with hard-coded values. Values from user input would
        need additional escaping.
    """
    verbose = " -v" if include_topic else ""
    args = (
        f"set -e && "
        f"apk add --no-cache --quiet mosquitto-clients >/dev/null && "
        f"mosquitto_sub --host {broker_host} --port {broker_port} "
        f"--topic {shlex.quote(topic)} --qos {qos} "
        f"-C {message_count} -W {wait_seconds}{verbose} "
        f"--cafile /var/run/certs/ca.crt "
        f"-D CONNECT authentication-method 'K8S-SAT' "
        f"-D CONNECT authentication-data $(cat /var/run/secrets/tokens/broker-sat)"
    )
    return _mqtt_client_pod_manifest(
        sa_name=sa_name,
        pod_name=pod_name,
        namespace=namespace,
        container_name="mqtt-sub",
        command=args,
        image=image,
        sat_audience=sat_audience,
        trust_bundle_configmap=trust_bundle_configmap,
    )


def mqtt_roundtrip_pod_manifest(
    *,
    sa_name: str,
    pod_name: str,
    namespace: str,
    source_topic: str,
    destination_topic: str,
    payload: str,
    wait_seconds: int = 180,
    subscriber_ready_delay_seconds: int = 10,
    qos: int = 1,
    image: str = "alpine:3.20",
    sat_audience: str = "aio-internal",
    trust_bundle_configmap: str = "azure-iot-operations-aio-ca-trust-bundle",
    broker_host: str = "aio-broker",
    broker_port: int = 18883,
) -> str:
    """Render one pod that subscribes before publishing a probe message."""
    connection = (
        f"--host {shlex.quote(broker_host)} --port {broker_port} "
        f"--qos {qos} --cafile /var/run/certs/ca.crt "
        f"-D CONNECT authentication-method 'K8S-SAT' "
        f"-D CONNECT authentication-data "
        f"$(cat /var/run/secrets/tokens/broker-sat)"
    )
    args = (
        f"set -e; "
        f"apk add --no-cache --quiet mosquitto-clients >/dev/null; "
        f"mosquitto_sub {connection} "
        f"--topic {shlex.quote(destination_topic)} "
        f"-C 1 -W {wait_seconds} > /tmp/received & "
        f"subscriber_pid=$!; "
        f"sleep {subscriber_ready_delay_seconds}; "
        f"mosquitto_pub {connection} "
        f"--topic {shlex.quote(source_topic)} "
        f"--message {shlex.quote(payload)}; "
        f"wait $subscriber_pid; "
        f"cat /tmp/received"
    )
    return _mqtt_client_pod_manifest(
        sa_name=sa_name,
        pod_name=pod_name,
        namespace=namespace,
        container_name="mqtt-roundtrip",
        command=args,
        image=image,
        sat_audience=sat_audience,
        trust_bundle_configmap=trust_bundle_configmap,
    )

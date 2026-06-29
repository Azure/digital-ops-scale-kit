"""Renderers and helpers for MQTT subscriber test pods.

Lives outside `tests/integration/helpers/kube.py` so that the pure-Python
rendering can be exercised by unit tests in the top-level `tests/`
directory without pulling in any kubectl machinery.
"""

import textwrap


def mqtt_subscriber_pod_manifest(
    *,
    sa_name: str,
    pod_name: str,
    namespace: str,
    topic: str,
    wait_seconds: int,
    qos: int = 1,
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

    The container runs `mosquitto_sub -C 1 -W <wait>` so it exits
    Succeeded on the first received message and Failed on the timeout,
    making the pod a one-shot witness suitable for asserting via
    `kubectl logs` + `.status.phase`.

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
    args = (
        f"set -e && "
        f"apk add --no-cache --quiet mosquitto-clients >/dev/null && "
        f"mosquitto_sub --host {broker_host} --port {broker_port} "
        f"--topic '{topic}' --qos {qos} "
        f"-C 1 -W {wait_seconds} "
        f"--cafile /var/run/certs/ca.crt "
        f"-D CONNECT authentication-method 'K8S-SAT' "
        f"-D CONNECT authentication-data $(cat /var/run/secrets/tokens/broker-sat)"
    )
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
            - name: mqtt-sub
              image: {image}
              command: ["sh", "-c"]
              args:
                - "{args}"
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

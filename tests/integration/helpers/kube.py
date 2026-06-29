"""Kubernetes assertion helpers for integration tests.

These helpers shell out to `kubectl` so the test process can read CRs and
Secrets that were written by cluster-side controllers (SecretSync, AIO
operators).

## Routing assumption

The helpers do NOT start `az connectedk8s proxy`. They route via the same
kubeconfig kubectl would normally pick up, with one workflow-time
override: if `SITEOPS_TEST_KUBECONFIG` is set in the environment, every
kubectl invocation is given an explicit `--kubeconfig=<path>` flag
pointed at that file. This isolates direct-kubectl reads from
`~/.kube/config`, which the siteops orchestrator's `arc:` kubectl steps
mutate via `az connectedk8s proxy` (adding a proxy-context entry that
points at a local port and switching current-context to it). When the
proxy process exits, the context entry is left dangling. Subsequent
direct kubectl reads against `~/.kube/config` would hit a dead URL and
fail with `connection refused`. The override file is read-only for the
runner user (e.g. the k3s admin file at `/etc/rancher/k3s/k3s.yaml`,
mode 0644 from create-k3s-cluster), so siteops cannot mutate it.

For a remote Arc-onboarded cluster you would need to start an Arc proxy
before running these tests (or layer Arc-proxy management into the
helpers in a later change). Customer integration tests against a
production Arc cluster are out of scope today.

The `kubectl_available` fixture in `conftest.py` guards on actual
`kubectl version` exit. In CI it hard-fails rather than skipping so a
misconfigured workflow does not silently lose the headline coverage.
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable

# Hard timeout per kubectl call so a flaky API server cannot hang a test.
KUBECTL_REQUEST_TIMEOUT = "10s"


def _kubectl_base() -> list[str]:
    """Return the base kubectl argv with an optional --kubeconfig override.

    Read at every call (not cached at import) so a test that temporarily
    unsets the env var via monkeypatch can fall back to default
    kubeconfig discovery.
    """
    base = ["kubectl"]
    override = os.environ.get("SITEOPS_TEST_KUBECONFIG")
    if override:
        base.extend(["--kubeconfig", override])
    return base


class KubectlError(RuntimeError):
    """Raised when a kubectl invocation fails for any reason other than NotFound."""

    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


def kubectl_json(args: list[str], *, timeout: int = 30) -> Any:
    """Run `kubectl <args> -o=json` and return the parsed JSON document.

    Args:
        args: kubectl arguments (e.g., `["get", "secret", "foo", "-n", "ns"]`).
            Do not include `kubectl` itself or `-o=json` (added automatically).
        timeout: subprocess timeout in seconds.

    Returns:
        Parsed JSON document. A single `get` returns a dict. A list `get`
        returns a dict with `items`.

    Raises:
        KubectlError: if kubectl returns non-zero or the output is not valid JSON.
    """
    full_args = [
        *_kubectl_base(),
        *args,
        "-o=json",
        f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}",
    ]
    proc = subprocess.run(full_args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise KubectlError(
            f"kubectl {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}",
            proc.returncode,
            proc.stderr,
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise KubectlError(
            f"kubectl {' '.join(args)} returned non-JSON output: {e}",
            0,
            proc.stderr,
        ) from e


def kubectl_text(args: list[str], *, timeout: int = 30) -> str:
    """Run `kubectl <args>` and return raw stdout text.

    Use for `-o jsonpath=...` and `--no-headers` outputs that are not JSON.

    Do not use to extract Secret payloads. The `data` and `stringData`
    fields of a Kubernetes Secret carry the secret values verbatim and
    would land in CI logs if returned from this helper into a
    `pytest.fail` message. Use `get_secret_value` instead, which keeps
    the value local to the test and never returns it on the failure path.
    """
    full_args = [
        *_kubectl_base(),
        *args,
        f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}",
    ]
    proc = subprocess.run(full_args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise KubectlError(
            f"kubectl {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}",
            proc.returncode,
            proc.stderr,
        )
    return proc.stdout


def _is_not_found(error: KubectlError) -> bool:
    """True when the error is the standard kubectl NotFound diagnostic."""
    text = error.stderr.lower()
    return "notfound" in text or "not found" in text


def get_secret(name: str, namespace: str) -> dict[str, Any] | None:
    """Return the parsed Secret resource, or None if it does not exist.

    Raises:
        KubectlError: on errors other than NotFound.
    """
    try:
        return kubectl_json(["get", "secret", name, "-n", namespace])
    except KubectlError as e:
        if _is_not_found(e):
            return None
        raise


def get_secret_value(name: str, namespace: str, key: str) -> str | None:
    """Return the base64-decoded value of `data[key]` on the named Secret.

    Returns None if the Secret does not exist or the key is not present.
    """
    secret = get_secret(name, namespace)
    if secret is None:
        return None
    encoded = secret.get("data", {}).get(key)
    if encoded is None:
        return None
    return base64.b64decode(encoded).decode("utf-8")


def wait_for_secret(
    name: str,
    namespace: str,
    *,
    expected_key: str | None = None,
    timeout: int = 600,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll for a Secret to materialize and return it once present.

    When `expected_key` is supplied, also requires `data[expected_key]` to be set.

    Args:
        name: Secret name.
        namespace: Secret namespace.
        expected_key: optional data key that must be present (e.g., the
            target_key from a SecretSync mapping).
        timeout: total wall-clock budget in seconds. Default 600 covers
            cold-start federated identity credential propagation, Key
            Vault RBAC propagation, and the first reconcile cycle. Tests
            that hit a known longer worst case should override upward.
        interval: poll interval in seconds.

    Returns:
        Parsed Secret resource.

    Raises:
        TimeoutError: if the Secret does not appear (or the expected key
            is missing) within `timeout`.
    """
    deadline = time.monotonic() + timeout
    last_error: str = "Secret never observed"
    while time.monotonic() < deadline:
        try:
            secret = get_secret(name, namespace)
            if secret is not None:
                if expected_key is None or expected_key in secret.get("data", {}):
                    return secret
                last_error = (
                    f"Secret found but `data[{expected_key}]` is missing. "
                    f"Available keys: {sorted(secret.get('data', {}).keys())}"
                )
            else:
                last_error = "Secret not yet present"
        except KubectlError as e:
            last_error = f"kubectl error during poll: {e}"
        time.sleep(interval)
    raise TimeoutError(
        f"Timed out after {timeout}s waiting for Secret `{name}` in namespace "
        f"`{namespace}`. Last observation: {last_error}"
    )


def get_custom_resource(
    api_version: str, kind: str, name: str, namespace: str
) -> dict[str, Any] | None:
    """Return a custom resource by API version + kind + name, or None if not found.

    Args:
        api_version: e.g., `secretsync.x-k8s.io/v1alpha1`.
        kind: e.g., `SecretSync`.
        name: resource name.
        namespace: resource namespace.

    Returns:
        Parsed resource dict, or None if NotFound.

    Raises:
        KubectlError: on errors other than NotFound.
    """
    # kubectl accepts <kind>.<group> as the resource shorthand.
    group = api_version.split("/", 1)[0] if "/" in api_version else ""
    type_arg = f"{kind.lower()}.{group}" if group else kind.lower()
    try:
        return kubectl_json(["get", type_arg, name, "-n", namespace])
    except KubectlError as e:
        if _is_not_found(e):
            return None
        raise


def wait_for_cr_status(
    api_version: str,
    kind: str,
    name: str,
    namespace: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: int = 300,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll a CR until `predicate(cr)` returns True or `timeout` elapses.

    Args:
        api_version, kind, name, namespace: as for `get_custom_resource`.
        predicate: callable taking the parsed CR dict, returning True when
            the desired condition holds.
        timeout: total wall-clock budget in seconds.
        interval: poll interval in seconds.

    Returns:
        The CR dict for which the predicate first returned True.

    Raises:
        TimeoutError: if the predicate never holds within `timeout`. The
            error message includes the last observed CR status for
            debugging.
    """
    deadline = time.monotonic() + timeout
    last_observed: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        cr = get_custom_resource(api_version, kind, name, namespace)
        if cr is not None:
            last_observed = cr
            if predicate(cr):
                return cr
        time.sleep(interval)
    msg = (
        f"Timed out after {timeout}s waiting for {kind} `{name}` in "
        f"namespace `{namespace}` to satisfy predicate."
    )
    if last_observed is not None:
        status = last_observed.get("status", {})
        msg += f" Last observed status: {json.dumps(status, indent=2)}"
    else:
        msg += " Resource was never observed."
    raise TimeoutError(msg)


def list_pods(namespace: str, label_selector: str | None = None) -> list[dict[str, Any]]:
    """Return a list of pods in the namespace, optionally filtered by labels.

    Args:
        namespace: Pod namespace.
        label_selector: Optional `key=value,key2=value2` selector forwarded
            to `kubectl --selector`.

    Returns:
        List of pod dicts (the `items` array of the kubectl response).
    """
    args = ["get", "pods", "-n", namespace]
    if label_selector:
        args.extend(["-l", label_selector])
    result = kubectl_json(args)
    if isinstance(result, dict):
        return result.get("items", [])
    return []


def is_pod_ready(pod: dict[str, Any]) -> bool:
    """True when the pod has a `Ready=True` status condition."""
    for condition in pod.get("status", {}).get("conditions", []):
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return True
    return False


def is_available() -> bool:
    """Return True if `kubectl` is on PATH and can reach a cluster.

    Used by the `kubectl_available` conftest fixture. When False, tests
    that depend on cluster reads should skip locally and hard-fail in CI.
    """
    if shutil.which("kubectl") is None:
        return False
    try:
        proc = subprocess.run(
            [*_kubectl_base(), "version", f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def apply_manifest(manifest_yaml: str, *, timeout: int = 30) -> None:
    """Apply a YAML manifest piped to `kubectl apply -f -`.

    Args:
        manifest_yaml: full YAML document(s) to apply.
        timeout: subprocess timeout in seconds.

    Raises:
        KubectlError: if kubectl returns non-zero.
    """
    proc = subprocess.run(
        [*_kubectl_base(), "apply", "-f", "-", f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}"],
        input=manifest_yaml,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise KubectlError(
            f"kubectl apply failed (exit {proc.returncode}): {proc.stderr.strip()}",
            proc.returncode,
            proc.stderr,
        )


def delete_resource(
    kind: str,
    name: str,
    namespace: str,
    *,
    ignore_not_found: bool = True,
    timeout: int = 30,
) -> None:
    """Delete a Kubernetes resource. Best-effort, intended for test teardown."""
    args = [
        *_kubectl_base(),
        "delete",
        kind,
        name,
        "-n",
        namespace,
        f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}",
    ]
    if ignore_not_found:
        args.append("--ignore-not-found=true")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 and not (ignore_not_found and _is_not_found_text(proc.stderr)):
        raise KubectlError(
            f"kubectl delete {kind}/{name} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}",
            proc.returncode,
            proc.stderr,
        )


def _is_not_found_text(text: str) -> bool:
    """True when the error is the standard kubectl NotFound diagnostic.

    Anchored on the canonical kubectl message so admission-webhook errors
    that happen to mention 'not found' in the reason string are not
    swallowed by ignore-not-found delete flows.
    """
    return "Error from server (NotFound)" in text or "NotFound" == text.strip()


def wait_for_deployment_ready(
    name: str,
    namespace: str,
    *,
    min_ready_replicas: int = 1,
    timeout: int = 300,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll a Deployment until `.status.readyReplicas` >= `min_ready_replicas`.

    Args:
        name: Deployment name.
        namespace: Deployment namespace.
        min_ready_replicas: minimum number of Ready pods required.
        timeout: total wall-clock budget in seconds.
        interval: poll interval in seconds.

    Returns:
        Parsed Deployment resource at the moment the predicate became true.

    Raises:
        TimeoutError: if the Deployment never reaches the ready replica count.
    """
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] = {}
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            deploy = kubectl_json(["get", "deployment", name, "-n", namespace])
            last_status = deploy.get("status", {})
            last_error = None
            ready = last_status.get("readyReplicas", 0) or 0
            if ready >= min_ready_replicas:
                return deploy
        except KubectlError as e:
            last_error = f"kubectl error during poll: {e}"
        time.sleep(interval)
    if last_error and last_status:
        observation = (
            f"Last kubectl error: {last_error}. Last successful status: "
            f"{json.dumps(last_status)}."
        )
    elif last_error:
        observation = last_error
    else:
        observation = f"Last status: {json.dumps(last_status)}"
    raise TimeoutError(
        f"Deployment `{name}` in `{namespace}` did not reach "
        f"readyReplicas>={min_ready_replicas} within {timeout}s. {observation}"
    )


def get_endpoint_addresses(
    service_name: str, namespace: str
) -> list[str]:
    """Return the list of ready endpoint IPs backing a Service.

    Reads EndpointSlices (the modern replacement for Endpoints) filtered
    by the Kubernetes-managed label that ties slices to their parent
    Service. Returns only addresses with `conditions.ready=true`.

    Per the Kubernetes API reference, `endpoints[*].conditions.ready` is
    tri-valued: True, False, or None (publisher cannot determine). This
    helper treats None as not-ready so callers asserting "Service has at
    least one Ready endpoint" do not pass on indeterminate state.
    """
    result = kubectl_json(
        [
            "get",
            "endpointslices",
            "-n",
            namespace,
            "-l",
            f"kubernetes.io/service-name={service_name}",
        ]
    )
    addresses: list[str] = []
    for slice_ in result.get("items", []) if isinstance(result, dict) else []:
        for endpoint in slice_.get("endpoints", []):
            if endpoint.get("conditions", {}).get("ready") is True:
                addresses.extend(endpoint.get("addresses", []))
    return addresses


def wait_for_service_endpoints(
    service_name: str,
    namespace: str,
    *,
    min_addresses: int = 1,
    timeout: int = 60,
    interval: int = 2,
) -> list[str]:
    """Poll until a Service has at least `min_addresses` Ready endpoints.

    Absorbs the propagation lag between a pod transitioning Ready and
    the EndpointSlice controller publishing its address to the slice.
    For freshly-deployed pods, callers should typically `wait_for_deployment_ready`
    first to bound the wall-clock, then use this helper with a short
    budget to ride out the controller round-trip.

    Args:
        service_name: Service name.
        namespace: Service namespace.
        min_addresses: minimum number of Ready endpoint addresses required.
        timeout: total wall-clock budget in seconds.
        interval: poll interval in seconds.

    Returns:
        The list of Ready endpoint addresses at the moment the predicate
        became true.

    Raises:
        TimeoutError: if the Service does not reach the required address
            count within `timeout`.
    """
    deadline = time.monotonic() + timeout
    last_observed: list[str] = []
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            last_observed = get_endpoint_addresses(service_name, namespace)
            last_error = None
            if len(last_observed) >= min_addresses:
                return last_observed
        except KubectlError as e:
            last_error = f"kubectl error during poll: {e}"
        time.sleep(interval)
    if last_error and last_observed:
        observation = (
            f"Last kubectl error: {last_error}. Last successful observation: "
            f"{last_observed}."
        )
    elif last_error:
        observation = last_error
    else:
        observation = f"Last observed: {last_observed}."
    raise TimeoutError(
        f"Service `{service_name}` in `{namespace}` did not reach "
        f">={min_addresses} Ready endpoints within {timeout}s. {observation}"
    )


def wait_for_pod_phase(
    name: str,
    namespace: str,
    *,
    target_phases: tuple[str, ...] = ("Succeeded",),
    failure_phases: tuple[str, ...] = ("Failed",),
    timeout: int = 300,
    interval: int = 5,
) -> dict[str, Any]:
    """Poll a Pod until its `.status.phase` is in `target_phases`.

    Args:
        name: Pod name.
        namespace: Pod namespace.
        target_phases: phases that satisfy the wait (return).
        failure_phases: phases that cause early failure (raise).
        timeout: total wall-clock budget in seconds.
        interval: poll interval in seconds.

    Returns:
        Parsed Pod resource at the moment the predicate became true.

    Raises:
        RuntimeError: if the Pod enters a failure phase before timeout.
        TimeoutError: if neither target nor failure phase is reached.
    """
    deadline = time.monotonic() + timeout
    last_phase = "<unknown>"
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            pod = kubectl_json(["get", "pod", name, "-n", namespace])
            last_phase = pod.get("status", {}).get("phase", "<unknown>")
            last_error = None
            if last_phase in target_phases:
                return pod
            if last_phase in failure_phases:
                raise RuntimeError(
                    f"Pod `{name}` in `{namespace}` entered failure phase "
                    f"`{last_phase}` before reaching {target_phases}."
                )
        except KubectlError as e:
            last_error = f"kubectl error during poll: {e}"
        time.sleep(interval)
    if last_error and last_phase != "<unknown>":
        observation = (
            f"Last kubectl error: {last_error}. Last observed phase: "
            f"{last_phase}."
        )
    elif last_error:
        observation = last_error
    else:
        observation = f"Last observed phase: {last_phase}."
    raise TimeoutError(
        f"Pod `{name}` in `{namespace}` did not reach {target_phases} "
        f"within {timeout}s. {observation}"
    )


def get_pod_logs(
    name: str, namespace: str, *, container: str | None = None, timeout: int = 30
) -> str:
    """Return raw `kubectl logs` output for a pod."""
    args = ["logs", name, "-n", namespace]
    if container:
        args.extend(["-c", container])
    return kubectl_text(args, timeout=timeout)


def assert_secret_value_equals(actual: str, expected: str, *, context: str) -> None:
    """Assert two secret values are byte-equal without echoing either to logs.

    Use this instead of `assert actual == expected` when comparing values
    pulled from a Kubernetes Secret. Plain `assert ==` and f-string
    failure messages would interpolate the value into pytest output, the
    GitHub Actions step log, and any artifact upload of the run log. On
    mismatch this helper reports lengths and short non-reversible
    fingerprints only.

    Args:
        actual: value read from the cluster.
        expected: value the deploy was supposed to write.
        context: short identifier (e.g., "Site=foo Secret=bar Key=baz")
            included in the failure message so a multi-secret loop can be
            triaged.

    Raises:
        AssertionError: if the values differ. The message carries lengths
            and 8-char SHA-256 fingerprints, never the values themselves.
    """
    if hmac.compare_digest(actual, expected):
        return
    actual_fp = hashlib.sha256(actual.encode("utf-8")).hexdigest()[:8]
    expected_fp = hashlib.sha256(expected.encode("utf-8")).hexdigest()[:8]
    raise AssertionError(
        f"Materialized Kubernetes Secret value did not match. {context}. "
        f"Expected len={len(expected)} fp={expected_fp}, "
        f"actual len={len(actual)} fp={actual_fp}."
    )

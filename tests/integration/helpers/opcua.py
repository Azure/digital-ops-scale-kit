"""Diagnostic helpers specific to the OPC UA data path.

These helpers live in their own module (rather than `kube.py`) because
they hardcode Azure IoT Operations and Azure Device Registry CRD names,
namespace defaults, and asset/dataflow status schemas. Generic kubectl
primitives stay in `kube.py`.

The OPC UA sample provisions namespace-scoped Device Registry resources
(`Microsoft.DeviceRegistry/namespaces/devices` and `.../assets`), not the
legacy root-asset + asset-endpoint-profile pair. The cluster CRs project
into the group `namespaces.deviceregistry.microsoft.com` (kinds `Asset`
and `Device`). The diagnostic prefers `-A` across all namespaces so the
helper is robust to projection-namespace changes between AIO releases, and
falls back to the AIO namespace for a caller that holds only a namespace-scoped
binding. The fallback reports the original error when it finds nothing, since a
bare empty result would read as "the resource does not exist" when the truth is
"the query was refused, and the fallback looked in one namespace".
"""

from tests.integration.helpers.kube import KubectlError, kubectl_text


def dump_opc_ua_connector_status(
    asset_name: str,
    dataflow_name: str,
    namespace: str,
    device_name: str,
) -> str:
    """Return the .status of the OPC UA device, asset, and dataflow plus AIO pod phases.

    Args:
        asset_name: name of the ADR namespace asset that drives the OPC UA
            connector.
        dataflow_name: name of the dataflow CR that routes asset data to
            its destination.
        namespace: AIO namespace where the dataflow and AIO operator pods
            live, and the fallback scope for the cluster-wide queries.
        device_name: name of the device carrying the inbound endpoint the
            asset refers to. Its `spec.enabled` and endpoint names are
            reported, since a device that is not enabled presents no
            endpoint and the supervisor then skips every asset on it.

    Returns the diagnostic text. Status fields and broad listings only,
    so the output is safe to interpolate into a `pytest.fail` message.
    """
    assets = "assets.namespaces.deviceregistry.microsoft.com"
    devices = "devices.namespaces.deviceregistry.microsoft.com"
    asset_status = (
        'jsonpath={range .items[?(@.metadata.name=="' + asset_name + '")]}'
        "{.metadata.namespace}/{.metadata.name}:\n{.status}\n{end}"
    )
    device_state = (
        'jsonpath={range .items[?(@.metadata.name=="' + device_name + '")]}'
        "{.metadata.namespace}/{.metadata.name} enabled={.spec.enabled} "
        "inbound={.spec.endpoints.inbound}\nstatus={.status}\n{end}"
    )

    # Each entry is a label, the preferred query, and a namespaced fallback
    # for when cluster-wide read is refused.
    queries: list[tuple[str, list[str], list[str] | None]] = [
        (
            "Namespace assets",
            ["get", assets, "-A"],
            ["get", assets, "-n", namespace],
        ),
        (
            "Namespace devices",
            ["get", devices, "-A"],
            ["get", devices, "-n", namespace],
        ),
        (
            f"Asset `{asset_name}` .status",
            ["get", assets, "-A", "-o", asset_status],
            ["get", assets, "-n", namespace, "-o", asset_status],
        ),
        (
            f"Device `{device_name}` enabled, endpoints, and .status",
            ["get", devices, "-A", "-o", device_state],
            ["get", devices, "-n", namespace, "-o", device_state],
        ),
        (
            f"Dataflow `{dataflow_name}` .status",
            ["get", "dataflows.connectivity.iotoperations.azure.com",
             dataflow_name, "-n", namespace, "-o", "jsonpath={.status}"],
            None,
        ),
        (
            f"Pods in `{namespace}`",
            ["get", "pods", "-n", namespace, "--no-headers"],
            None,
        ),
    ]

    def _text(args: list[str], fallback: list[str] | None) -> str:
        try:
            return kubectl_text(args).strip()
        except KubectlError as e:
            if fallback is None:
                return f"(diagnostic query failed: {e})"
            first = e
        try:
            out = kubectl_text(fallback).strip()
        except KubectlError as e:
            return f"(cluster-wide query failed: {first}. Fallback also failed: {e})"
        # An empty fallback result is ambiguous on its own, so it carries the
        # reason the broader query was not available.
        return out or (
            f"(cluster-wide query failed: {first}. The fallback searched only "
            f"`{namespace}` and found nothing)"
        )

    parts: list[str] = []
    for label, args, fallback in queries:
        parts.append(f"[{label}]")
        parts.append(_text(args, fallback) or "(empty)")
        parts.append("")
    return "\n".join(parts).rstrip()

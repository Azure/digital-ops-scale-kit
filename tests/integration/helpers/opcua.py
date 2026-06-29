"""Diagnostic helpers specific to the OPC UA data path.

These helpers live in their own module (rather than `kube.py`) because
they hardcode Azure IoT Operations and Azure Device Registry CRD names,
namespace defaults, and asset/dataflow status schemas. Generic kubectl
primitives stay in `kube.py`.

The OPC UA sample provisions namespace-scoped Device Registry resources
(`Microsoft.DeviceRegistry/namespaces/devices` and `.../assets`), not the
legacy root-asset + asset-endpoint-profile pair. The cluster CRs project
into the group `namespaces.deviceregistry.microsoft.com` (kinds `Asset`
and `Device`). The diagnostic queries `-A` across all namespaces so the
helper is robust to projection-namespace changes between AIO releases.
"""

from tests.integration.helpers.kube import KubectlError, kubectl_text


def dump_opc_ua_connector_status(
    asset_name: str,
    dataflow_name: str,
    namespace: str,
) -> str:
    """Return the .status of the OPC UA device, asset, and dataflow plus AIO pod phases.

    Args:
        asset_name: name of the ADR namespace asset that drives the OPC UA
            connector.
        dataflow_name: name of the dataflow CR that routes asset data to
            its destination.
        namespace: AIO namespace where the dataflow and AIO operator pods
            live. Asset and device CRs are queried with `-A` since
            namespace-scoped ADR resources may land in a cluster
            namespace different from the AIO namespace depending on the
            AIO release.

    Returns the diagnostic text. Status fields and broad listings only,
    so the output is safe to interpolate into a `pytest.fail` message.
    """
    queries = [
        (
            "All namespace assets cluster-wide",
            ["get", "assets.namespaces.deviceregistry.microsoft.com", "-A"],
        ),
        (
            "All namespace devices cluster-wide",
            ["get", "devices.namespaces.deviceregistry.microsoft.com", "-A"],
        ),
        (
            f"Asset `{asset_name}` .status (any namespace)",
            ["get", "assets.namespaces.deviceregistry.microsoft.com", "-A",
             "-o", "jsonpath={range .items[?(@.metadata.name==\""
             + asset_name + "\")]}{.metadata.namespace}/{.metadata.name}:\n"
             + "{.status}\n{end}"],
        ),
        (
            f"Dataflow `{dataflow_name}` .status",
            ["get", "dataflows.connectivity.iotoperations.azure.com",
             dataflow_name, "-n", namespace, "-o", "jsonpath={.status}"],
        ),
        (
            f"Pods in `{namespace}`",
            ["get", "pods", "-n", namespace, "--no-headers"],
        ),
    ]
    parts: list[str] = []
    for label, args in queries:
        parts.append(f"[{label}]")
        try:
            out = kubectl_text(args).strip()
            parts.append(out or "(empty)")
        except KubectlError as e:
            parts.append(f"(diagnostic query failed: {e})")
        parts.append("")
    return "\n".join(parts).rstrip()

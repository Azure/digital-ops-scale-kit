"""Diagnostic helpers specific to the OPC UA data path.

These helpers live in their own module (rather than `kube.py`) because
they hardcode Azure IoT Operations and Azure Device Registry CRD names,
namespace defaults, and asset/dataflow status schemas. Generic kubectl
primitives stay in `kube.py`.
"""

from tests.integration.helpers.kube import KubectlError, kubectl_text


def dump_opc_ua_connector_status(
    asset_name: str,
    dataflow_name: str,
    namespace: str,
) -> str:
    """Return the .status of the OPC UA asset and dataflow plus AIO pod phases.

    Args:
        asset_name: name of the ADR asset that drives the OPC UA connector.
        dataflow_name: name of the dataflow CR that routes asset data to
            its destination.
        namespace: AIO namespace where the asset, dataflow, and AIO
            operator pods live.

    Returns the diagnostic text. Pure status fields and pod metadata only,
    so the output is safe to interpolate into a `pytest.fail` message.
    """
    queries = [
        (
            f"Asset `{asset_name}` .status",
            ["get", "assets.deviceregistry.microsoft.com", asset_name,
             "-n", namespace, "-o", "jsonpath={.status}"],
        ),
        (
            f"Dataflow `{dataflow_name}` .status",
            ["get", "dataflows.connectivity.iotoperations.azure.com",
             dataflow_name, "-n", namespace, "-o", "jsonpath={.status}"],
        ),
        (
            f"Pods in `{namespace}`",
            ["get", "pods", "-n", namespace, "--no-headers", "-o", "wide"],
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

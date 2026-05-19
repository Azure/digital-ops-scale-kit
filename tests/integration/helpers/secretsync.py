"""Diagnostic helpers specific to the SecretSync data path.

These helpers live in their own module (rather than `kube.py`) because
they hardcode Azure SecretStore extension conventions: CRD shortnames,
namespace defaults, and the schema of the SecretSync controller's
status fields. Generic kubectl primitives stay in `kube.py`.
"""

from tests.integration.helpers.kube import KubectlError, kubectl_text


def dump_secretsync_status(
    secretsync_name: str,
    spc_name: str,
    namespace: str,
    controller_namespace: str = "azure-secret-store",
) -> str:
    """Return the .status of the SecretSync CR and SPC plus controller pod phases.

    Args:
        secretsync_name: name of the SecretSync custom resource.
        spc_name: name of the SecretProviderClass custom resource.
        namespace: namespace where the SecretSync and SPC live.
        controller_namespace: namespace of the Azure SecretStore extension
            pods. Defaults to `azure-secret-store`, the Arc extension's
            conventional `releaseNamespace`. Pass an explicit value when
            the cluster's extension was installed with a non-default
            namespace.

    Returns the diagnostic text. Pure status fields and pod metadata only,
    so the output is safe to interpolate into a `pytest.fail` message.
    """
    queries = [
        (
            f"SecretSync `{secretsync_name}` .status",
            ["get", "secretsync", secretsync_name, "-n", namespace,
             "-o", "jsonpath={.status}"],
        ),
        (
            f"SecretProviderClass `{spc_name}` .status",
            ["get", "secretproviderclass", spc_name, "-n", namespace,
             "-o", "jsonpath={.status}"],
        ),
        (
            f"Pods in `{controller_namespace}`",
            ["get", "pods", "-n", controller_namespace, "--no-headers"],
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

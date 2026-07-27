"""Workspace contracts for optional OIDC outputs in the AIO upgrade flow."""

from pathlib import Path


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


class TestAioUpgradeOidcOptionality:
    """Cluster resolution tolerates workload identity being disabled."""

    def test_cluster_oidc_outputs_are_safe_when_profile_is_absent(self, workspace):
        text = _read(
            workspace, "templates/common/modules/resolve-cluster.bicep"
        )
        assert (
            "connectedCluster.properties.?oidcIssuerProfile.?issuerUrl ?? ''"
        ) in text
        assert (
            "connectedCluster.properties.?oidcIssuerProfile."
            "?selfHostedIssuerUrl ?? ''"
        ) in text

    def test_resolve_aio_keeps_the_oidc_output_contract(self, workspace):
        text = _read(workspace, "templates/aio/resolve-aio.bicep")
        assert "output oidcIssuerUrl string = useSelfHostedIssuer" in text
        assert "resolvedCluster.outputs.selfHostedIssuerUrl" in text
        assert "resolvedCluster.outputs.oidcIssuerUrl" in text

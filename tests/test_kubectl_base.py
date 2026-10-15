"""Unit tests for `tests.integration.helpers.kube._kubectl_base`.

Validates the kubeconfig-override mechanism that isolates direct-kubectl
test reads from siteops's writable `~/.kube/config`. A regression that
silently dropped the `--kubeconfig` flag would only surface as cluster
read failures during a live E2E (90+ min round-trip), so the contract
is unit-tested here.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.integration.helpers import kube
from tests.integration.helpers.kube import _kubectl_base


def test_no_override_returns_plain_kubectl(monkeypatch):
    monkeypatch.delenv("SITEOPS_TEST_KUBECONFIG", raising=False)
    assert _kubectl_base() == ["kubectl"]


def test_override_injects_kubeconfig_flag(monkeypatch):
    monkeypatch.setenv("SITEOPS_TEST_KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
    assert _kubectl_base() == [
        "kubectl",
        "--kubeconfig",
        "/etc/rancher/k3s/k3s.yaml",
    ]


def test_empty_override_treated_as_unset(monkeypatch):
    monkeypatch.setenv("SITEOPS_TEST_KUBECONFIG", "")
    assert _kubectl_base() == ["kubectl"]


def test_base_read_at_every_call_not_cached(monkeypatch):
    """A test that toggles the env var mid-suite must see the new value
    on the next call. Cached behavior would silently desync."""
    monkeypatch.setenv("SITEOPS_TEST_KUBECONFIG", "/path/one")
    first = _kubectl_base()
    monkeypatch.setenv("SITEOPS_TEST_KUBECONFIG", "/path/two")
    second = _kubectl_base()
    assert first == ["kubectl", "--kubeconfig", "/path/one"]
    assert second == ["kubectl", "--kubeconfig", "/path/two"]


@pytest.mark.parametrize("crd_present", [False, True])
def test_opcua_template_discovery_handles_a_disabled_feature(monkeypatch, crd_present):
    resource = "connectortemplates.akri.iotoperations.azure.com"
    read = Mock(side_effect=[
        {"items": [{"metadata": {"name": resource}}] if crd_present else []},
        {"items": [{"metadata": {"name": "azureiotoperationsconnectorforopcua-one"}},
                   {"metadata": {"name": "unrelated"}}]},
    ])
    monkeypatch.setattr(kube, "kubectl_json", read)
    assert kube.list_opcua_connector_templates("aio") == (
        ["azureiotoperationsconnectorforopcua-one"] if crd_present else []
    )
    assert read.call_count == (2 if crd_present else 1)


def test_opcua_template_discovery_does_not_hide_cluster_read_errors(monkeypatch):
    read = Mock(side_effect=kube.KubectlError("denied", 1, "Forbidden"))
    monkeypatch.setattr(kube, "kubectl_json", read)
    with pytest.raises(kube.KubectlError, match="denied"):
        kube.list_opcua_connector_templates("aio")

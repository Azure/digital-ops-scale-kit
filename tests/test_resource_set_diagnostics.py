"""Fast contracts for advanced resource-set runtime diagnostics."""

import pytest

from tests.integration import test_resource_set_samples_manifest as sample


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_subscriber_failure_includes_allowlisted_runtime_summary(
    monkeypatch,
    error_type,
):
    private_detail = (
        "pod private-pod in private-namespace failed for "
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        "resourceGroups/rg-private"
    )
    monkeypatch.setattr(
        sample,
        "wait_for_pod_phase",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            error_type(private_detail)
        ),
    )
    monkeypatch.setattr(
        sample,
        "dataflow_runtime_summary",
        lambda *_args: "Runtime summary: profilePods=0.",
    )

    with pytest.raises(error_type) as exc_info:
        sample._wait_for_advanced_subscriber("probe", "namespace")

    message = str(exc_info.value)
    assert "profilePods=0" in message
    assert private_detail not in message
    assert "private-pod" not in message
    assert "private-namespace" not in message
    assert "00000000-0000-0000-0000-000000000000" not in message
    assert "rg-private" not in message

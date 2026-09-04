"""Fast contracts for dataflow sample phase cleanup."""

from types import SimpleNamespace

import pytest

from tests.integration import conftest
from tests.integration.helpers import dataflow_sample as sample


def test_sample_arm_resources_delete_dependents_before_profiles_and_endpoints():
    resources = sample.sample_arm_resources(
        "subscription",
        "resource-group",
        "aio-instance",
    )

    assert [
        (resource_type, name)
        for resource_type, name, _ in resources
    ] == [
        (sample.DATAFLOW_TYPE, sample.DATAFLOW_NAME),
        (sample.DATAFLOW_TYPE, sample.ALERTS_DATAFLOW_NAME),
        (sample.PROFILE_TYPE, sample.PROFILE_NAME),
        (sample.PROFILE_TYPE, sample.ALERTS_PROFILE_NAME),
        (sample.ENDPOINT_TYPE, sample.ENDPOINT_NAME),
        (sample.ENDPOINT_TYPE, sample.ALERTS_ENDPOINT_NAME),
    ]


def test_sample_arm_resources_use_the_exact_parent_hierarchy():
    resources = sample.sample_arm_resources(
        "subscription",
        "resource-group",
        "aio-instance",
    )
    by_name = {
        name: resource_id
        for _, name, resource_id in resources
    }

    assert by_name[sample.DATAFLOW_NAME].endswith(
        "/instances/aio-instance/dataflowProfiles/"
        "dataflow-sample-profile/dataflows/dataflow-sample-passthrough"
    )
    assert by_name[sample.PROFILE_NAME].endswith(
        "/instances/aio-instance/dataflowProfiles/dataflow-sample-profile"
    )
    assert by_name[sample.ENDPOINT_NAME].endswith(
        "/instances/aio-instance/dataflowEndpoints/dataflow-sample-mqtt-out"
    )


def test_cleanup_deletes_and_waits_in_dependency_order(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sample,
        "delete_arm_resource",
        lambda resource_id, api_version, **kwargs: calls.append(
            ("delete", resource_id, api_version, kwargs["redact"])
        ),
    )
    monkeypatch.setattr(
        sample,
        "wait_for_cr_deleted",
        lambda resource_type, name, namespace: calls.append(
            ("wait", resource_type, name, namespace)
        ),
    )

    sample.cleanup_dataflow_sample_resources(
        "subscription",
        "resource-group",
        "aio-instance",
        "2026-07-01",
        "namespace",
        redact=("private",),
    )

    resources = sample.sample_arm_resources(
        "subscription",
        "resource-group",
        "aio-instance",
    )
    assert calls == [
        call
        for resource_type, name, resource_id in resources
        for call in (
            (
                "delete",
                resource_id,
                "2026-07-01",
                ("private",),
            ),
            ("wait", resource_type, name, "namespace"),
        )
    ]


def test_phase_isolation_requires_explicit_true(monkeypatch):
    monkeypatch.delenv(sample.ISOLATE_PHASE_ENV, raising=False)
    assert not sample.phase_isolation_enabled()

    monkeypatch.setenv(sample.ISOLATE_PHASE_ENV, "false")
    assert not sample.phase_isolation_enabled()

    monkeypatch.setenv(sample.ISOLATE_PHASE_ENV, "TRUE")
    assert sample.phase_isolation_enabled()


def test_fixture_cleans_partial_deployment_before_setup_failure_escapes(
    monkeypatch,
):
    site = SimpleNamespace(
        name="site",
        subscription="subscription",
        resource_group="resource-group",
    )
    orchestrator = SimpleNamespace(
        deploy=lambda **_kwargs: {"summary": {"failed": 1}},
    )
    cleanup_calls = []
    monkeypatch.setattr(
        conftest,
        "_resolve_or_fail",
        lambda *_args: (SimpleNamespace(), [site]),
    )
    monkeypatch.setattr(
        conftest,
        "_assert_deployed",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("deployment failed")
        ),
    )
    monkeypatch.setattr(
        "tests.integration.helpers.dataflow_sample.phase_isolation_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "tests.integration.helpers.dataflow_sample."
        "cleanup_dataflow_sample_resources",
        lambda *args, **kwargs: cleanup_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "tests.integration.helpers.assertions.find_step",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        "tests.integration.helpers.assertions.assert_output_exists",
        lambda *_args: {"name": "aio-instance"},
    )
    monkeypatch.setattr(
        "tests.integration.helpers.releases.load_aio_release",
        lambda *_args: ("2608", {"aioApiVersion": "2026-07-01"}),
    )

    fixture = conftest.dataflow_sample_result.__wrapped__(
        orchestrator,
        "name=site",
        {"sites": {"site": {}}},
        "namespace",
    )

    with pytest.raises(AssertionError, match="deployment failed"):
        next(fixture)

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][0] == (
        "subscription",
        "resource-group",
        "aio-instance",
        "2026-07-01",
        "namespace",
    )

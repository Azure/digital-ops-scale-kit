"""Isolation helpers for the profile-heavy dataflow sample phase."""

import os

from tests.integration.helpers.azure import delete_arm_resource
from tests.integration.helpers.kube import wait_for_cr_deleted

ISOLATE_PHASE_ENV = "SITEOPS_E2E_ISOLATE_DATAFLOW_SAMPLE"

ENDPOINT_TYPE = "dataflowendpoints.connectivity.iotoperations.azure.com"
PROFILE_TYPE = "dataflowprofiles.connectivity.iotoperations.azure.com"
DATAFLOW_TYPE = "dataflows.connectivity.iotoperations.azure.com"

ENDPOINT_NAME = "dataflow-sample-mqtt-out"
ALERTS_ENDPOINT_NAME = "dataflow-sample-alerts-out"
PROFILE_NAME = "dataflow-sample-profile"
ALERTS_PROFILE_NAME = "dataflow-sample-alerts-pool"
DATAFLOW_NAME = "dataflow-sample-passthrough"
ALERTS_DATAFLOW_NAME = "dataflow-sample-alerts"


def phase_isolation_enabled() -> bool:
    """Return whether this run selected both profile-heavy phases."""
    return os.environ.get(ISOLATE_PHASE_ENV, "").casefold() == "true"


def sample_arm_resources(
    subscription: str,
    resource_group: str,
    aio_instance_name: str,
) -> tuple[tuple[str, str, str], ...]:
    """Return sample resources in safe deletion order."""
    root = (
        f"/subscriptions/{subscription}/resourceGroups/{resource_group}/"
        "providers/Microsoft.IoTOperations/instances/"
        f"{aio_instance_name}"
    )
    return (
        (
            DATAFLOW_TYPE,
            DATAFLOW_NAME,
            f"{root}/dataflowProfiles/{PROFILE_NAME}/dataflows/"
            f"{DATAFLOW_NAME}",
        ),
        (
            DATAFLOW_TYPE,
            ALERTS_DATAFLOW_NAME,
            f"{root}/dataflowProfiles/{ALERTS_PROFILE_NAME}/dataflows/"
            f"{ALERTS_DATAFLOW_NAME}",
        ),
        (
            PROFILE_TYPE,
            PROFILE_NAME,
            f"{root}/dataflowProfiles/{PROFILE_NAME}",
        ),
        (
            PROFILE_TYPE,
            ALERTS_PROFILE_NAME,
            f"{root}/dataflowProfiles/{ALERTS_PROFILE_NAME}",
        ),
        (
            ENDPOINT_TYPE,
            ENDPOINT_NAME,
            f"{root}/dataflowEndpoints/{ENDPOINT_NAME}",
        ),
        (
            ENDPOINT_TYPE,
            ALERTS_ENDPOINT_NAME,
            f"{root}/dataflowEndpoints/{ALERTS_ENDPOINT_NAME}",
        ),
    )


def cleanup_dataflow_sample_resources(
    subscription: str,
    resource_group: str,
    aio_instance_name: str,
    api_version: str,
    namespace: str,
    *,
    redact: tuple[str, ...],
) -> None:
    """Delete sample resources and wait for projected removal."""
    for resource_type, name, resource_id in sample_arm_resources(
        subscription,
        resource_group,
        aio_instance_name,
    ):
        delete_arm_resource(
            resource_id,
            api_version,
            redact=redact,
        )
        wait_for_cr_deleted(
            resource_type,
            name,
            namespace,
        )

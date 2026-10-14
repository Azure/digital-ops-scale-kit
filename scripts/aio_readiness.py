# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Require every active AIO pod to be Running and Ready."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PodReadiness:
    namespace_pods: int
    active_pods: int
    ready_active_pods: int

    @property
    def ready(self) -> bool:
        return self.active_pods >= 1 and self.ready_active_pods == self.active_pods

    def counts(self) -> dict[str, int]:
        return {
            "namespacePods": self.namespace_pods,
            "activePods": self.active_pods,
            "readyActivePods": self.ready_active_pods,
        }


def active_pod_readiness(pods: object) -> PodReadiness:
    if not isinstance(pods, dict) or not isinstance(pods.get("items"), list):
        raise ValueError("Invalid pod observation.")
    active = ready = 0
    for pod in pods["items"]:
        if not isinstance(pod, dict) or not isinstance(pod.get("status"), dict):
            raise ValueError("Invalid pod status.")
        metadata = pod.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Invalid pod metadata.")
        status = pod["status"]
        if status.get("phase") == "Succeeded":
            continue
        active += 1
        conditions = status.get("conditions")
        if conditions is None:
            continue
        if not isinstance(conditions, list) or any(not isinstance(row, dict) for row in conditions):
            raise ValueError("Invalid pod conditions.")
        if (not metadata.get("deletionTimestamp")
                and status.get("phase") == "Running"
                and any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions)):
            ready += 1
    return PodReadiness(len(pods["items"]), active, ready)

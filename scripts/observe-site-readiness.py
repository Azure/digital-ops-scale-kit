# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Observe bounded AIO instance and active pod readiness for a Site."""

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aio_readiness import active_pod_readiness  # noqa: E402

TIMEOUT_MESSAGE = (
    "::error::AIO instance projection and active pod readiness did not converge within fifteen minutes."
)
POD_PHASES = {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
CONTAINER_WAIT_REASONS = {
    "BackOff", "ContainerCreating", "CrashLoopBackOff", "CreateContainerConfigError",
    "CreateContainerError", "ErrImagePull", "ImagePullBackOff", "PodInitializing", "RunContainerError",
}


def diagnostics(pods: object) -> dict:
    phases: Counter[str] = Counter()
    reasons: Counter[str] = Counter()

    def allowed(value, values):
        return value if isinstance(value, str) and value in values else "Other"

    items = pods.get("items", []) if isinstance(pods, dict) else []
    for pod in items if isinstance(items, list) else []:
        status = pod.get("status", {}) if isinstance(pod, dict) else {}
        if not isinstance(status, dict):
            continue
        phases[allowed(status.get("phase"), POD_PHASES)] += 1
        for field in ("initContainerStatuses", "containerStatuses"):
            containers = status.get(field, [])
            for container in containers if isinstance(containers, list) else []:
                state = container.get("state", {}) if isinstance(container, dict) else {}
                waiting = state.get("waiting") if isinstance(state, dict) else None
                if isinstance(waiting, dict):
                    reasons[allowed(waiting.get("reason"), CONTAINER_WAIT_REASONS)] += 1
    return {"podPhases": dict(phases), "containerWaitingReasons": dict(reasons)}


def query(kind: str, timeout: float) -> object:
    try:
        result = subprocess.run(
            ["kubectl", "get", kind, "--namespace", "azure-iot-operations", "--output", "json"],
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("AIO observation exceeds its size limit.")
    return json.loads(result.stdout)


def instance_count(instances: object) -> int:
    if not isinstance(instances, dict) or not isinstance(instances.get("items"), list):
        raise ValueError("Invalid instance observation.")
    for instance in instances["items"]:
        if (not isinstance(instance, dict) or not isinstance(instance.get("metadata"), dict)
                or not isinstance(instance["metadata"].get("name"), str) or not instance["metadata"]["name"]):
            raise ValueError("Invalid instance identity.")
    return len(instances["items"])


def observe(*, query=query, clock=time.monotonic, sleep=time.sleep) -> dict[str, int]:
    deadline = clock() + 900
    pods = None

    def remaining():
        budget = deadline - clock()
        if budget <= 0:
            print(TIMEOUT_MESSAGE, file=sys.stderr)
            print(json.dumps(diagnostics(pods), sort_keys=True), file=sys.stderr)
            raise TimeoutError()
        return budget

    while True:
        pods = query("pods", min(30, remaining()))
        instances = query("instance.iotoperations.azure.com", min(30, remaining()))
        observation = active_pod_readiness(pods) if pods is not None else None
        count = instance_count(instances) if instances is not None else 0
        budget = remaining()
        if observation is not None and observation.ready and count >= 1:
            return {**observation.counts(), "instanceCustomResources": count}
        sleep(min(10, budget))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("Select new readiness state.")
        counts = observe()
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(counts, sort_keys=True) + "\n")
    except TimeoutError:
        return 1
    except (OSError, ValueError, TypeError):
        print("::error::AIO readiness observation is invalid or unavailable.", file=sys.stderr)
        return 1
    print("AIO instance and active pod readiness passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

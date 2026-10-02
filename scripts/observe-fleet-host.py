# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Observe bounded Kubernetes readiness on one run-owned fleet host."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_process import run  # noqa: E402
from fleet_workflow import FleetBudget, FleetCandidate, scope_for  # noqa: E402

from siteops.artifacts import load_artifact_json  # noqa: E402


def ready(pods: dict, instances: dict) -> bool:
    if not isinstance(pods, dict) or not isinstance(pods.get("items"), list):
        raise ValueError("Invalid pod observation.")
    if not isinstance(instances, dict) or not isinstance(instances.get("items"), list):
        raise ValueError("Invalid instance observation.")
    if len(instances["items"]) != 1:
        return False
    instance = instances["items"][0]
    if (not isinstance(instance, dict) or not isinstance(instance.get("metadata"), dict)
            or not isinstance(instance["metadata"].get("name"), str) or not instance["metadata"]["name"]):
        raise ValueError("Invalid instance identity.")
    active = []
    for pod in pods["items"]:
        if not isinstance(pod, dict) or not isinstance(pod.get("status"), dict):
            raise ValueError("Invalid pod status.")
        status = pod["status"]
        if status.get("phase") == "Succeeded":
            continue
        active.append(pod)
        conditions = status.get("conditions")
        if conditions is None:
            return False
        if not isinstance(conditions, list) or any(not isinstance(row, dict) for row in conditions):
            raise ValueError("Invalid pod conditions.")
        if (pod.get("metadata", {}).get("deletionTimestamp")
                or status.get("phase") != "Running"
                or not any(row.get("type") == "Ready" and row.get("status") == "True"
                           for row in conditions)):
            return False
    return bool(active)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--slot", required=True, choices=("one", "two"))
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.logs.exists() or args.output.exists():
            raise ValueError("Select new readiness state.")
        selected = FleetCandidate.parse(
            os.environ["FLEET_CANDIDATE"].encode(), repository=os.environ["GITHUB_REPOSITORY"],
            commit=os.environ["GITHUB_SHA"], ref=os.environ["GITHUB_REF"],
        )
        scope = scope_for(selected, args.inputs, run=int(os.environ["GITHUB_RUN_ID"]),
                          attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]), subscription=os.environ["AZURE_SUBSCRIPTION_ID"])
        args.logs.mkdir(mode=0o700)
        deadline = time.monotonic() + FleetBudget.from_environment().remaining("observed", 1200)
        attempt = 0
        previous = ()
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("Host readiness exceeded its deadline.")
            attempt += 1
            observed = {}
            current = []
            for kind in ("pods", "instances.iotoperations.azure.com"):
                name = f"{attempt}-{'pods' if kind == 'pods' else 'instances'}"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Host readiness exceeded its deadline.")
                code = run(["kubectl", "get", kind, "-n", "azure-iot-operations", "-o", "json"],
                           cwd=args.logs, logs=args.logs, name=name, timeout=min(30, remaining))
                current.extend(args.logs / f"{name}.{suffix}" for suffix in ("out", "err"))
                if code:
                    break
                with (args.logs / f"{name}.out").open("rb") as stream:
                    observed[kind] = load_artifact_json(stream.read(2 * 1024 * 1024 + 1),
                                                       limit=2 * 1024 * 1024, label="Host readiness")
            for path in previous:
                path.unlink()
            previous = tuple(current)
            if time.monotonic() >= deadline:
                raise ValueError("Host readiness exceeded its deadline.")
            if len(observed) == 2 and ready(observed["pods"], observed["instances.iotoperations.azure.com"]):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Host readiness exceeded its deadline.")
            time.sleep(min(15, remaining))
        report = {"apiVersion": "siteops.release.fleet/v1", "kind": "FleetHostReadiness",
                  "context": scope.context(), "scopeKey": scope.key, "slot": args.slot,
                  "status": "ready", "checks": ["instance-present", "active-pods-ready"],
                  "workloadFunctionality": "not-checked"}
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print("Fleet host readiness failed. Inspect private host diagnostics.", file=sys.stderr)
        return 1
    print("Fleet host instance and pod readiness passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

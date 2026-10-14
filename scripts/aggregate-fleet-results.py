# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Require exact fleet deployment, two host observations and confirmed owned cleanup."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from fleet_workflow import FleetCandidate  # noqa: E402

from siteops.artifacts import load_artifact_json, open_regular_file  # noqa: E402


def read(path: Path) -> dict:
    with open_regular_file(path) as stream:
        value = load_artifact_json(stream.read(65537), limit=65536, label="Fleet outcome")
    if not isinstance(value, dict):
        raise ValueError("The fleet outcome is not a document.")
    return value


def aggregate(selected: FleetCandidate, root: Path, *, run: int, attempt: int) -> dict:
    context = {
        "repository": selected.source["repository"], "sourceCommit": selected.source["commit"],
        "admissionSha256": selected.artifacts["admission"]["sha256"],
        "inventorySha256": selected.artifacts["inventory"]["sha256"], "run": run, "attempt": attempt,
    }
    deployment = read(root / "deployment" / "deployment.json")
    cleanup = read(root / "cleanup" / "cleanup.json")
    names = {f"fleet-readiness-{run}-{attempt}-{slot}" for slot in ("one", "two")}
    if {path.name for path in (root / "readiness").iterdir()} != names:
        raise ValueError("Fleet readiness is missing or contains unexpected slots.")
    readiness = [read(root / "readiness" / f"fleet-readiness-{run}-{attempt}-{slot}" / "readiness.json")
                 for slot in ("one", "two")]
    scope_keys = set()
    for document in (deployment, cleanup, *readiness):
        actual_context = document.get("context")
        if (
            document.get("apiVersion") != "siteops.release.fleet/v1"
            or not isinstance(actual_context, dict) or set(actual_context) != set(context)
            or any(type(actual_context[key]) is not type(value) or actual_context[key] != value
                   for key, value in context.items())
            or not isinstance(document.get("scopeKey"), str)
            or re.fullmatch("[0-9a-f]{64}", document["scopeKey"]) is None
        ):
            raise ValueError("A fleet outcome describes another candidate or invocation.")
        scope_keys.add(document["scopeKey"])
    if len(scope_keys) != 1:
        raise ValueError("Fleet outcomes disagree about the private scope binding.")
    if (
        set(deployment) != {
            "apiVersion", "kind", "context", "scopeKey", "status", "engineVersion", "targets",
            "deployInvocations", "parallel", "sentinelExcluded", "resolvedParametersVerified",
            "extensionVersionsVerified", "slots", "podReadiness", "workloadFunctionality",
        }
        or
        deployment.get("kind") != "FleetDeployment" or deployment.get("status") != "succeeded"
        or type(deployment.get("targets")) is not int or deployment["targets"] != 2
        or type(deployment.get("deployInvocations")) is not int or deployment["deployInvocations"] != 1
        or type(deployment.get("parallel")) is not int or deployment["parallel"] != 2
        or any(deployment.get(key) is not True for key in (
            "sentinelExcluded", "resolvedParametersVerified", "extensionVersionsVerified",
        ))
        or deployment.get("workloadFunctionality") != "not-checked"
        or not isinstance(deployment.get("slots"), dict) or set(deployment["slots"]) != {"one", "two"}
        or any(not isinstance(row, dict) or set(row) != {"release", "version", "apiVersion"}
               or not isinstance(row["version"], str)
               or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", row["version"]) is None
               or not isinstance(row["apiVersion"], str)
               or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?", row["apiVersion"]) is None
               for row in deployment["slots"].values())
        or any(deployment["slots"][slot].get("release") != release
               for slot, release in (("one", "2607"), ("two", "2608")))
    ):
        raise ValueError("The installed controller did not establish the required fleet outcome.")
    for slot, document in zip(("one", "two"), readiness, strict=True):
        if (
            set(document) != {
                "apiVersion", "kind", "context", "scopeKey", "slot", "status", "checks", "workloadFunctionality",
            }
            or document.get("kind") != "FleetHostReadiness" or document.get("status") != "ready"
            or document.get("slot") != slot
            or document.get("checks") != ["instance-present", "active-pods-ready"]
            or document.get("workloadFunctionality") != "not-checked"
        ):
            raise ValueError("A fleet host did not establish the required readiness outcome.")
    if (
        set(cleanup) != {"apiVersion", "kind", "groups", "context", "scopeKey", "slots", "status", "operationExit"}
        or cleanup.get("groups") not in {"ephemeral", "persistent"}
        or cleanup.get("kind") != "FleetCleanup" or cleanup.get("status") != "complete"
        or type(cleanup.get("operationExit")) is not int or cleanup["operationExit"] != 0
        or not isinstance(cleanup.get("slots"), dict) or set(cleanup["slots"]) != {"one", "two"}
        or any(row != {"state": "absent", "reason": "confirmed-absent"} for row in cleanup["slots"].values())
    ):
        raise ValueError("Owned fleet cleanup was not confirmed for both slots.")
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetAcceptance",
        "context": context, "groups": cleanup["groups"], "status": "passed", "targetCount": 2,
        "deployInvocations": 1,
        "planParameters": "checked", "extensionVersions": "checked", "podReadiness": "checked",
        "cleanup": "confirmed-absent", "workloadFunctionality": "not-checked",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        selected = FleetCandidate.parse(
            os.environ["FLEET_CANDIDATE"].encode(), repository=os.environ["GITHUB_REPOSITORY"],
            commit=os.environ["GITHUB_SHA"], ref=os.environ["GITHUB_REF"],
        )
        report = aggregate(selected, args.root, run=int(os.environ["GITHUB_RUN_ID"]),
                           attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        print("Fleet acceptance evidence is missing, inconsistent or unsuccessful.", file=sys.stderr)
        return 1
    print("The exact fleet candidate passed deployment, readiness and owned cleanup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

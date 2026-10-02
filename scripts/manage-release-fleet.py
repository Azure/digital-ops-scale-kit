# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Preflight, create or reconcile only the two resource groups owned by a release test."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_fleet import (  # noqa: E402
    AzureGroups,
    FleetError,
    FleetScope,
    cleanup,
    create,
    preflight,
)

from siteops.artifacts import load_artifact_json, open_regular_file  # noqa: E402


def expected_document(path: Path, expected_sha: str) -> dict:
    with open_regular_file(path) as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise FleetError("receipt-digest-mismatch")
    document = load_artifact_json(raw, limit=65536, label="Fleet receipt")
    if not isinstance(document, dict):
        raise FleetError("invalid-receipt")
    return document


def check_admission(admission: dict) -> None:
    source = admission.get("source")
    artifacts = admission.get("artifacts")
    subjects = admission.get("subjects")
    if (
        set(admission) != {
            "apiVersion", "kind", "source", "run", "attempt", "caller", "preview", "artifacts",
            "planSha256", "inventorySha256", "subjects", "status", "installation", "deployment",
        }
        or admission["apiVersion"] != "siteops.release.acceptance/v1"
        or admission["kind"] != "CandidateInputAdmission" or admission["status"] != "admitted"
        or admission["installation"] != "not-run" or admission["deployment"] != "not-run"
        or not isinstance(source, dict) or set(source) != {"repository", "commit", "ref"}
        or type(admission["preview"]) is not bool
        or admission["caller"] != (
            ".github/workflows/ci.yaml" if admission["preview"] else ".github/workflows/release.yaml"
        )
        or any(type(admission[key]) is not int or admission[key] <= 0 for key in ("run", "attempt"))
        or not isinstance(artifacts, dict) or set(artifacts) != {"plan", "inventory", "payload"}
        or any(type(value) is not int or value <= 0 for value in artifacts.values())
        or not isinstance(subjects, dict) or set(subjects) != {"engine", "workspace"}
        or any(type(value) is not int or value < 0 for value in subjects.values())
        or any(not isinstance(admission[key], str)
               or not re.fullmatch("[0-9a-f]{64}", admission[key])
               for key in ("planSha256", "inventorySha256"))
    ):
        raise FleetError("candidate-not-admitted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "create", "cleanup"))
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--expected-admission-sha", required=True)
    parser.add_argument("--ownership", type=Path)
    parser.add_argument("--expected-ownership-sha")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-logs", type=Path, required=True)
    parser.add_argument("--location")
    parser.add_argument("--operation-exit", type=int, default=0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.operation != "preflight" and (
        not args.execute or args.ownership is None or args.expected_ownership_sha is None
    ):
        parser.error("Creation and cleanup require --execute and independently selected ownership.")
    if args.operation == "create" and not args.location:
        parser.error("Creation requires an approved --location.")
    try:
        if args.output.exists() or args.private_logs.exists():
            raise FleetError("select-new-output-and-log-paths")
        admission = expected_document(args.admission, args.expected_admission_sha)
        check_admission(admission)
        scope = FleetScope(
            os.environ["GITHUB_REPOSITORY"], admission["source"]["commit"],
            args.expected_admission_sha, admission["inventorySha256"],
            int(os.environ["FLEET_RUN_ID"]), int(os.environ["FLEET_RUN_ATTEMPT"]),
            os.environ["AZURE_SUBSCRIPTION_ID"],
        )
        ownership = (
            expected_document(args.ownership, args.expected_ownership_sha)
            if args.operation != "preflight" else None
        )
        groups = AzureGroups(scope, args.private_logs)
        code = 0
        if args.operation == "preflight":
            report = preflight(scope, groups)
        elif args.operation == "create":
            create(scope, ownership, groups, args.location)
            report = {
                "apiVersion": "siteops.release.fleet/v1", "kind": "FleetCreation",
                "context": scope.context(), "scopeKey": scope.key, "status": "created",
                "slots": ["one", "two"],
            }
        else:
            code, report = cleanup(scope, ownership, groups, operation_exit=args.operation_exit)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, FleetError) else "invalid-input-or-local-state"
        print(f"Fleet operation failed: {reason}. Inspect private diagnostics and the selected receipts.", file=sys.stderr)
        return args.operation_exit if args.operation == "cleanup" and 0 < args.operation_exit <= 255 else 1
    print(f"Fleet {args.operation}: {report.get('status', 'admitted')}.")
    return code


if __name__ == "__main__":
    sys.exit(main())

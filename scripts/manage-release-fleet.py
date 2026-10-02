# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Preflight, create or reconcile only the two resource groups owned by a release test."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_fleet import (  # noqa: E402
    AzureGroups,
    FleetError,
    FleetScope,
    check_admission,
    cleanup,
    create,
    preflight,
)
from release_fleet import expected_document as expected_document  # noqa: E402


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

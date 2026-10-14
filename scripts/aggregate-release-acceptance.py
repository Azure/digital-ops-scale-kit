# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Record one Site case outcome, or aggregate every required release acceptance scenario."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from fleet_workflow import (  # noqa: E402
    FleetCandidate,
    admission_for,
    jobs,
    read_selected,
    run_jobs,
    scope_for,
)
from release_acceptance import (  # noqa: E402
    STEP_ASSERTIONS,
    AcceptanceError,
    aggregate,
    select_evidence,
    site_outcome,
    summary,
)
from release_fleet import SITE_SLOTS  # noqa: E402

from siteops.artifacts import load_artifact_json, open_regular_file  # noqa: E402

EVIDENCE = {"disabled": "outcome.json", "enabled": "outcome.json", "existing": "outcome.json",
            "fleet": "fleet-acceptance.json"}


def read(path: Path, limit: int = 64 * 1024):
    with open_regular_file(path) as stream:
        return load_artifact_json(stream.read(limit + 1), limit=limit, label="Acceptance evidence")


def optional(path: Path | None):
    try:
        return read(path) if path is not None and path.is_file() else None
    except (ValueError, OSError):
        return None


def candidate() -> FleetCandidate:
    return FleetCandidate.parse(
        os.environ["FLEET_CANDIDATE"].encode("utf-8"), repository=os.environ["GITHUB_REPOSITORY"],
        commit=os.environ["GITHUB_SHA"], ref=os.environ["GITHUB_REF"],
    )


def write(path: Path, document: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, sort_keys=True) + "\n")


def record_site(args) -> int:
    selected = candidate()
    scope = scope_for(selected, args.root, run=int(os.environ["FLEET_RUN_ID"]),
                      attempt=int(os.environ["FLEET_RUN_ATTEMPT"]),
                      subscription=os.environ["AZURE_SUBSCRIPTION_ID"], slot=args.slot)
    outcomes = {step: os.environ.get("OUTCOME_" + step.upper().replace("-", "_"), "") for step in STEP_ASSERTIONS}
    document = site_outcome(
        scope.context(), scope.key, args.slot, outcomes, qualification=optional(args.qualification),
        readiness=optional(args.readiness), cleanup=optional(args.cleanup),
        preflight=os.environ.get("OUTCOME_PREFLIGHT", ""), groups=scope.mode,
    )
    write(args.output, document)
    print(f"Site case {args.slot}: {document['status']}, {document['groups']} groups, "
          f"cleanup {document['cleanup']}, vault purge {document['vaultPurge']}.")
    return 0


def evidence(root: Path, key: str):
    directory = root / "evidence" / key
    # A failed download can leave extracted bytes that did not match their digest.
    if os.environ.get(f"DOWNLOAD_{key.upper()}") != "success" or not directory.exists():
        return None
    try:
        if {path.name for path in directory.iterdir()} != {EVIDENCE[key]}:
            return {}
        return read(directory / EVIDENCE[key])
    except (ValueError, OSError):
        return {}


def pages(path: Path, key: str) -> list:
    value = read(path, 8 * 1024 * 1024)
    if (not isinstance(value, list) or not 1 <= len(value) <= 32
            or any(not isinstance(page, dict) or not isinstance(page.get(key), list) for page in value)):
        raise AcceptanceError("Workflow metadata pages are invalid.")
    return value


def aggregate_run(args) -> int:
    selected = candidate()
    run, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
    commit = selected.source["commit"]
    plan, bound = None, False
    if not (args.root / "admission" / "receipt.json").exists() or not (args.root / "plan" / "plan.json").exists():
        # The selection or its downloads did not complete, so the evidence is missing rather than mismatched.
        bound = None
        print("The admission or plan for the selected candidate is unavailable.", file=sys.stderr)
    else:
        try:
            admission_for(selected, args.root)
            plan = load_artifact_json(
                read_selected(args.root / "plan" / "plan.json", selected.artifacts["plan"]["sha256"]),
                limit=2 * 1024 * 1024, label="Release plan",
            )
            bound = True
        except (ValueError, OSError):
            print("The admission or plan does not bind to the selected candidate.", file=sys.stderr)
    try:
        producer = jobs(pages(args.root / "producer-jobs.json", "jobs"), run=selected.producer["run"],
                        attempt=selected.producer["attempt"], commit=commit)
    except (ValueError, OSError):
        producer = None
    try:
        executions = run_jobs(pages(args.root / "run-jobs.json", "jobs"), run=run, attempt=attempt, commit=commit)
        artifacts = [item for page in pages(args.root / "run-artifacts.json", "artifacts") for item in page["artifacts"]]
    except (ValueError, OSError):
        executions = artifacts = None
    try:
        workflow = yaml.safe_load(args.workflow.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        workflow = None
    try:
        needs = json.loads(os.environ.get("ACCEPTANCE_NEEDS", ""))
    except ValueError:
        needs = None
    document = aggregate(
        selected, plan=plan, bound=bound, producer_jobs=producer, run_jobs=executions, artifacts=artifacts,
        documents={key: evidence(args.root, key) for key in EVIDENCE}, workflow=workflow,
        run=run, attempt=attempt, environment=os.environ.get("ACCEPTANCE_ENVIRONMENT", ""), needs=needs,
    )
    write(args.output, document)
    report = summary(document)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(report)
    print(report)
    if executions is not None and artifacts is not None and select_evidence(
        executions, artifacts, run=run, commit=commit,
    )["fleet"]["partial"]:
        print("The fleet must pass within one attempt. Use Re-run all jobs for a new fleet attempt.", file=sys.stderr)
    return 0 if document["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    site = commands.add_parser("site-outcome")
    site.add_argument("--root", required=True, type=Path)
    site.add_argument("--slot", required=True, choices=SITE_SLOTS)
    site.add_argument("--qualification", type=Path)
    site.add_argument("--readiness", type=Path)
    site.add_argument("--cleanup", type=Path)
    site.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("aggregate")
    run.add_argument("--root", required=True, type=Path)
    run.add_argument("--workflow", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        return record_site(args) if args.operation == "site-outcome" else aggregate_run(args)
    except (ValueError, OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, AcceptanceError) else (
            "Release acceptance evidence could not be bound to the selected candidate.")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

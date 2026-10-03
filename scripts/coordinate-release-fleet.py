# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Coordinate exact fleet job state without publishing target identities."""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_process import FleetProcessError  # noqa: E402
from fleet_process import run as run_private  # noqa: E402
from fleet_workflow import (  # noqa: E402
    CoordinationError,
    FleetBudget,
    FleetCandidate,
    check_inputs,
    jobs,
    named_job,
    scope_for,
    wait_for_job_state,
)
from release_fleet import AzureGroups, expected_document, validate_ownership  # noqa: E402

from siteops.artifacts import load_artifact_json  # noqa: E402


class GitHubReads:
    def __init__(self, root: Path):
        if not os.environ.get("GH_TOKEN"):
            raise CoordinationError("A scoped workflow metadata token is required.")
        self.root = root
        self.root.mkdir(mode=0o700)
        self.number = 0
        self.previous = ()

    def read(self, endpoint: str, *, pages=False, timeout=60):
        self.number += 1
        command = ["gh", "api", *(["--paginate", "--slurp"] if pages else []), endpoint]
        name = str(self.number)
        code = run_private(command, cwd=self.root, logs=self.root, name=name, timeout=timeout)
        paths = tuple(self.root / f"{name}.{suffix}" for suffix in ("out", "err"))
        for path in self.previous:
            path.unlink()
        self.previous = paths
        if code:
            raise CoordinationError("Workflow metadata query failed; private diagnostics were retained.")
        with paths[0].open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        return load_artifact_json(raw, limit=8 * 1024 * 1024, label="Workflow metadata")


def candidate() -> FleetCandidate:
    return FleetCandidate.parse(
        os.environ["FLEET_CANDIDATE"].encode("utf-8"),
        repository=os.environ["GITHUB_REPOSITORY"], commit=os.environ["GITHUB_SHA"],
        ref=os.environ["GITHUB_REF"],
    )


def output(values: dict) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in values.items():
            text = str(value)
            if "\r" in text or "\n" in text:
                raise CoordinationError("A workflow output has an invalid shape.")
            stream.write(f"{key}={text}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(
        "select", "check-inputs", "scope", "check-ownership", "wait-participants", "wait-ready", "wait-deployed",
        "wait-observed", "wait-cleanup", "ownership",
    ))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--slot", choices=("one", "two"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--ownership", type=Path)
    parser.add_argument("--expected-ownership-sha")
    args = parser.parse_args()
    try:
        selected = candidate()
        prefix = f"repos/{selected.source['repository']}/actions"
        if args.operation == "select":
            reader = GitHubReads(args.root / "selection-metadata")
            producer = selected.producer
            run = reader.read(f"{prefix}/runs/{producer['run']}/attempts/{producer['attempt']}")
            if (
                run.get("head_sha") != selected.source["commit"]
                or run.get("head_branch") != selected.source["ref"].removeprefix("refs/heads/")
                or run.get("path") != producer["caller"] or run.get("run_attempt") != producer["attempt"]
                or run.get("id") != producer["run"]
                or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int
                or run.get("repository", {}).get("full_name") != selected.source["repository"]
                or run.get("event") not in {"push", "workflow_dispatch"}
            ):
                raise CoordinationError("The fleet producer run differs from the selected source.")
            values = jobs(
                reader.read(f"{prefix}/runs/{producer['run']}/attempts/{producer['attempt']}/jobs?per_page=100", pages=True),
                run=producer["run"], attempt=producer["attempt"], commit=selected.source["commit"],
            )
            for name in ("Assemble release", "Admit frozen inputs"):
                job = named_job(values, name)
                if not job or job.get("status") != "completed" or job.get("conclusion") != "success":
                    raise CoordinationError("The selected candidate producer has not completed admission.")
            for role, record in selected.artifacts.items():
                selected.verify_artifact(role, reader.read(f"{prefix}/artifacts/{record['id']}"))
            output({**{f"{role}-id": row["id"] for role, row in selected.artifacts.items()},
                    **{f"{role}-sha": row["sha256"] for role, row in selected.artifacts.items()},
                    "producer-run": producer["run"]})
        elif args.operation == "check-inputs":
            check_inputs(selected, args.root)
            print("Frozen candidate inputs match the selected qualification records.")
        elif args.operation == "scope":
            if args.slot is None:
                raise CoordinationError("Select one fixed fleet slot.")
            scope = scope_for(
                selected, args.root, run=int(os.environ["FLEET_RUN_ID"]),
                attempt=int(os.environ["FLEET_RUN_ATTEMPT"]), subscription=os.environ["AZURE_SUBSCRIPTION_ID"],
            )
            group, cluster = scope.group(args.slot), scope.cluster(args.slot)
            cluster_id = f"/subscriptions/{scope.subscription}/resourceGroups/{group}/providers/Microsoft.Kubernetes/connectedClusters/{cluster}"
            values = {"FLEET_RESOURCE_GROUP": group, "FLEET_CLUSTER_NAME": cluster, "FLEET_CLUSTER_ID": cluster_id}
            for value in values.values():
                print("::add-mask::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), flush=True)
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
                for key, value in values.items():
                    stream.write(f"{key}={value}\n")
        elif args.operation == "check-ownership":
            FleetBudget.from_environment().remaining("ready", 60)
            if args.slot is None or args.ownership is None or not args.expected_ownership_sha:
                raise CoordinationError("Select an owned slot and its independently hashed receipt.")
            scope = scope_for(
                selected, args.root, run=int(os.environ["FLEET_RUN_ID"]),
                attempt=int(os.environ["FLEET_RUN_ATTEMPT"]), subscription=os.environ["AZURE_SUBSCRIPTION_ID"],
            )
            ownership = expected_document(args.ownership, args.expected_ownership_sha)
            slots = validate_ownership(scope, ownership)
            groups = AzureGroups(scope, args.root / "owned-group-observation")
            if not slots[args.slot]["admittedAbsent"] or not scope.owns(
                args.slot, groups.show(args.slot), slots[args.slot]["ownerSha256"],
            ):
                raise CoordinationError("The host resource group is not owned by this acceptance run.")
            print("The selected fleet slot is owned by this run.")
        elif args.operation.startswith("wait-"):
            run, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
            reader = GitHubReads(args.root / args.operation)

            def current_jobs(timeout):
                return jobs(
                    reader.read(f"{prefix}/runs/{run}/attempts/{attempt}/jobs?per_page=100",
                                pages=True, timeout=timeout),
                    run=run, attempt=attempt, commit=selected.source["commit"],
                )

            mode = args.operation.removeprefix("wait-")
            remaining = FleetBudget.from_environment().remaining(mode, args.timeout)
            wait_for_job_state(current_jobs, mode=mode, timeout=remaining,
                               interval=30 if mode in {"deployed", "cleanup"} else 15)
            print("The required fleet job state was observed.")
        else:
            # Recover the durable pre-write receipt even when prepare failed after uploading it.
            scope = scope_for(
                selected, args.root, run=int(os.environ["FLEET_RUN_ID"]),
                attempt=int(os.environ["FLEET_RUN_ATTEMPT"]), subscription=os.environ["AZURE_SUBSCRIPTION_ID"],
            )
            reader = GitHubReads(args.root / "ownership-metadata")
            if scope.run != int(os.environ["GITHUB_RUN_ID"]):
                original = reader.read(f"{prefix}/runs/{scope.run}/attempts/{scope.attempt}")
                if (
                    original.get("id") != scope.run or original.get("run_attempt") != scope.attempt
                    or original.get("head_sha") != selected.source["commit"]
                    or original.get("repository", {}).get("full_name") != selected.source["repository"]
                    or original.get("status") != "completed"
                ):
                    raise CoordinationError("Standalone reconciliation requires the original fleet run to be stopped.")
            values = jobs(
                reader.read(f"{prefix}/runs/{scope.run}/attempts/{scope.attempt}/jobs?per_page=100", pages=True),
                run=scope.run, attempt=scope.attempt, commit=selected.source["commit"],
            )
            prepare = named_job(values, "Fleet prepare")
            steps = prepare.get("steps") if prepare else None
            if not isinstance(steps, list):
                raise CoordinationError("Fleet preparation metadata is unavailable.")
            for name in ("Preflight resource ownership", "Retain resource ownership"):
                matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
                if (len(matches) != 1 or matches[0].get("status") != "completed"
                        or matches[0].get("conclusion") != "success"):
                    raise CoordinationError("Fleet ownership was not durably established before creation.")
            pages = reader.read(f"{prefix}/runs/{scope.run}/artifacts?per_page=100", pages=True)
            if not isinstance(pages, list) or not 1 <= len(pages) <= 16:
                raise CoordinationError("Fleet ownership metadata is invalid.")
            items = [item for page in pages for item in page.get("artifacts", [])]
            matches = [item for item in items if isinstance(item, dict)
                       and item.get("name") == f"fleet-ownership-{scope.run}-{scope.attempt}"]
            if (len(matches) != 1 or matches[0].get("expired") is not False
                    or type(matches[0].get("id")) is not int
                    or type(matches[0].get("size_in_bytes")) is not int or matches[0]["size_in_bytes"] <= 0
                    or not isinstance(matches[0].get("digest"), str)
                    or re.fullmatch("sha256:[0-9a-f]{64}", matches[0]["digest"]) is None
                    or type(matches[0].get("workflow_run", {}).get("id")) is not int
                    or matches[0].get("workflow_run", {}).get("id") != scope.run
                    or matches[0].get("workflow_run", {}).get("head_sha") != selected.source["commit"]):
                raise CoordinationError("The original fleet ownership artifact is missing or ambiguous.")
            output({"ownership-id": matches[0]["id"]})
    except FleetProcessError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        message = str(error) if isinstance(error, CoordinationError) else "Fleet coordination inputs or metadata were invalid."
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

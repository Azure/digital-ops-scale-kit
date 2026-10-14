# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Coordinate exact fleet and Site case job state without publishing target identities."""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from fleet_process import FleetProcessError  # noqa: E402
from fleet_process import run as run_private  # noqa: E402
from fleet_workflow import (  # noqa: E402
    CoordinationError,
    FleetBudget,
    FleetCandidate,
    check_inputs,
    jobs,
    named_job,
    run_jobs,
    scope_for,
    wait_for_job_state,
)
from release_acceptance import FLEET_JOBS, SITE_JOB, select_evidence  # noqa: E402
from release_fleet import (  # noqa: E402
    LOCATION,
    SITE_SLOTS,
    AzureGroups,
    expected_document,
    validate_ownership,
)
from release_verification import ReleaseVerifier  # noqa: E402

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


def write_private(path: Path, value) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=(
        "select", "check-inputs", "scope", "check-ownership", "wait-participants", "wait-ready", "wait-deployed",
        "wait-observed", "wait-cleanup", "ownership", "policy", "evidence",
    ))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--kind", choices=("fleet", "site"), default="fleet")
    parser.add_argument("--slot", choices=("one", "two", *SITE_SLOTS))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--ownership", type=Path)
    parser.add_argument("--expected-ownership-sha")
    parser.add_argument("--state", type=Path, help="Installed candidate state that holds the admitted roots.")
    parser.add_argument("--jobs-output", type=Path, help="New private file for the validated producer jobs.")
    parser.add_argument("--expect-groups", choices=("ephemeral", "persistent"),
                        help="Group mode the scheduling job selected for this scenario.")
    args = parser.parse_args()
    try:
        selected = candidate()
        prefix = f"repos/{selected.source['repository']}/actions"
        site_slot = None
        if args.kind == "site":
            if args.slot not in SITE_SLOTS:
                raise CoordinationError("Select one single Site case.")
            site_slot = args.slot
        elif args.slot in SITE_SLOTS:
            raise CoordinationError("A Site case slot requires the site kind.")
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
            producer_pages = reader.read(
                f"{prefix}/runs/{producer['run']}/attempts/{producer['attempt']}/jobs?per_page=100", pages=True,
            )
            values = jobs(producer_pages, run=producer["run"], attempt=producer["attempt"],
                          commit=selected.source["commit"])
            for name in ("Assemble release", "Admit frozen inputs"):
                job = named_job(values, name)
                if not job or job.get("status") != "completed" or job.get("conclusion") != "success":
                    raise CoordinationError("The selected candidate producer has not completed admission.")
            for role, record in selected.artifacts.items():
                selected.verify_artifact(role, reader.read(f"{prefix}/artifacts/{record['id']}"))
            if args.jobs_output is not None:
                write_private(args.jobs_output, producer_pages)
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
                slot=site_slot,
            )
            if args.expect_groups and scope.mode != args.expect_groups:
                raise CoordinationError("The resource group mode differs from the scheduled mode.")
            group, cluster = scope.group(args.slot), scope.cluster(args.slot)
            cluster_id = f"/subscriptions/{scope.subscription}/resourceGroups/{group}/providers/Microsoft.Kubernetes/connectedClusters/{cluster}"
            values = {"FLEET_RESOURCE_GROUP": group, "FLEET_CLUSTER_NAME": cluster, "FLEET_CLUSTER_ID": cluster_id}
            if site_slot == "existing":
                values["FLEET_VAULT_NAME"] = scope.vault(site_slot)
            if any("\r" in value or "\n" in value for value in values.values()):
                raise CoordinationError("A private target name has an invalid shape.")
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
            observed = groups.show(args.slot)
            location = observed.get("location") if isinstance(observed, dict) else None
            if (
                (not scope.groups and not slots[args.slot]["admittedAbsent"])
                or not scope.owns(args.slot, observed, slots[args.slot]["ownerSha256"])
                or not isinstance(location, str) or LOCATION.fullmatch(location) is None
            ):
                raise CoordinationError("The host resource group is not the one selected for this acceptance run.")
            # Persistent groups set the region for the resources this run creates.
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
                stream.write(f"FLEET_LOCATION={location}\n")
            print(f"The selected fleet slot uses its {scope.mode} resource group.")
        elif args.operation == "policy":
            # Each preparation or deployment phase starts a fresh policy, valid for one hour, from admitted roots.
            if args.state is None:
                raise CoordinationError("Select the installed candidate state.")
            for number in range(1, 21):
                directory = args.root / f"policy-{number}"
                if not os.path.lexists(directory):
                    break
            else:
                raise CoordinationError("The candidate policy refresh limit was reached.")
            verifier = ReleaseVerifier(
                directory, args.state / "workspace-policy" / "root.json", selected.source,
                signer=".github/workflows/_workspace-distribution.yaml",
                builder=selected.producer["caller"], runner_environment="self-hosted",
            )
            print(verifier.policy_file.parent)
        elif args.operation == "evidence":
            run, attempt = int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"])
            reader = GitHubReads(args.root / "evidence-metadata")
            execution_pages = reader.read(f"{prefix}/runs/{run}/jobs?filter=all&per_page=100", pages=True)
            executions = run_jobs(execution_pages, run=run, attempt=attempt, commit=selected.source["commit"])
            artifact_pages = reader.read(f"{prefix}/runs/{run}/artifacts?per_page=100", pages=True)
            if (not isinstance(artifact_pages, list) or not 1 <= len(artifact_pages) <= 32
                    or any(not isinstance(page, dict) or not isinstance(page.get("artifacts"), list)
                           for page in artifact_pages)):
                raise CoordinationError("The acceptance artifact metadata is invalid.")
            chosen = select_evidence(executions, [item for page in artifact_pages for item in page["artifacts"]],
                                     run=run, commit=selected.source["commit"])
            write_private(args.root / "run-jobs.json", execution_pages)
            write_private(args.root / "run-artifacts.json", artifact_pages)
            output({f"{key}-id": value["artifact"]["id"] if value["artifact"] else "" for key, value in chosen.items()})
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
                slot=site_slot,
            )
            reader = GitHubReads(args.root / "ownership-metadata")
            original_completed = ""
            if scope.run != int(os.environ["GITHUB_RUN_ID"]):
                original = reader.read(f"{prefix}/runs/{scope.run}/attempts/{scope.attempt}")
                if (
                    not isinstance(original, dict)
                    or type(original.get("id")) is not int or original["id"] != scope.run
                    or original.get("run_attempt") != scope.attempt
                    or original.get("head_sha") != selected.source["commit"]
                    or not isinstance(original.get("repository"), dict)
                    or original["repository"].get("full_name") != selected.source["repository"]
                    or original.get("status") != "completed"
                ):
                    raise CoordinationError("Standalone reconciliation requires the original fleet run to be stopped.")
                run = reader.read(f"{prefix}/runs/{scope.run}")
                if (
                    not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != scope.run
                    or run.get("head_sha") != selected.source["commit"]
                    or not isinstance(run.get("repository"), dict)
                    or run["repository"].get("full_name") != selected.source["repository"]
                    or run.get("status") != "completed"
                ):
                    raise CoordinationError("Standalone reconciliation requires the original fleet run to be stopped.")
            values = jobs(
                reader.read(f"{prefix}/runs/{scope.run}/attempts/{scope.attempt}/jobs?per_page=100", pages=True),
                run=scope.run, attempt=scope.attempt, commit=selected.source["commit"],
            )
            if site_slot is None:
                prepare = named_job(values, "Fleet prepare")
                names, artifact = ("Preflight resource ownership", "Retain resource ownership"), "fleet-ownership"
            else:
                prepare = named_job(values, SITE_JOB[site_slot])
                names, artifact = ("Retain Site ownership",), "site-ownership"
                retained = [step for step in (prepare or {}).get("steps") or ()
                            if isinstance(step, dict) and step.get("name") == names[0]]
            if scope.run != int(os.environ["GITHUB_RUN_ID"]) and (site_slot is None or prepare is not None):
                owning_jobs = ([prepare] if site_slot is not None else [
                    job for name in FLEET_JOBS if (job := named_job(values, name)) is not None
                ])
                if not owning_jobs or any(job is None or job.get("status") != "completed" for job in owning_jobs):
                    raise CoordinationError("The original owning job has not completed.")
                completed_times = []
                for job in owning_jobs:
                    completed = job.get("completed_at")
                    if not isinstance(completed, str) or re.fullmatch(
                        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", completed
                    ) is None:
                        raise CoordinationError("The original job completion time is invalid.")
                    try:
                        datetime.strptime(completed, "%Y-%m-%dT%H:%M:%SZ")
                    except ValueError:
                        raise CoordinationError("The original job completion time is invalid.") from None
                    completed_times.append(completed)
                original_completed = max(completed_times)
            if site_slot is not None and (prepare is None or (
                len(retained) == 1 and retained[0].get("status") == "completed"
                and retained[0].get("conclusion") != "success"
            )):
                # Creation runs only after a successful ownership upload in the same job.
                print("No Site resource group was created for this case in the selected attempt.")
                output({"ownership-id": "", "original-completed": ""})
                return 0
            steps = prepare.get("steps") if prepare else None
            if not isinstance(steps, list):
                raise CoordinationError("Fleet preparation metadata is unavailable.")
            for name in names:
                matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
                if (len(matches) != 1 or matches[0].get("status") != "completed"
                        or matches[0].get("conclusion") != "success"):
                    raise CoordinationError("Fleet ownership was not durably established before creation.")
            pages = reader.read(f"{prefix}/runs/{scope.run}/artifacts?per_page=100", pages=True)
            if not isinstance(pages, list) or not 1 <= len(pages) <= 16:
                raise CoordinationError("Fleet ownership metadata is invalid.")
            items = [item for page in pages for item in page.get("artifacts", [])]
            suffix = f"-{site_slot}" if site_slot else ""
            matches = [item for item in items if isinstance(item, dict)
                       and item.get("name") == f"{artifact}-{scope.run}-{scope.attempt}{suffix}"]
            if (len(matches) != 1 or matches[0].get("expired") is not False
                    or type(matches[0].get("id")) is not int
                    or type(matches[0].get("size_in_bytes")) is not int or matches[0]["size_in_bytes"] <= 0
                    or not isinstance(matches[0].get("digest"), str)
                    or re.fullmatch("sha256:[0-9a-f]{64}", matches[0]["digest"]) is None
                    or type(matches[0].get("workflow_run", {}).get("id")) is not int
                    or matches[0].get("workflow_run", {}).get("id") != scope.run
                    or matches[0].get("workflow_run", {}).get("head_sha") != selected.source["commit"]):
                raise CoordinationError("The original fleet ownership artifact is missing or ambiguous.")
            output({"ownership-id": matches[0]["id"], "original-completed": original_completed})
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

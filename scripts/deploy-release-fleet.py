# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Exercise one installed CLI deployment against the two owned acceptance targets."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from fleet_process import FleetProcessError, run  # noqa: E402
from fleet_workflow import CoordinationError, FleetBudget, FleetCandidate, scope_for  # noqa: E402
from release_fleet import AzureGroups, expected_document, validate_ownership  # noqa: E402
from release_verification import ReleaseVerifier  # noqa: E402

from siteops.arm_resources_azure_cli import _run_az  # noqa: E402
from siteops.artifacts import load_artifact_json, open_regular_file  # noqa: E402

RELEASES = {"one": "2607", "two": "2608"}
SELECTOR = "name=fleet-one,name=fleet-two"
EXPECTED_OPERATIONS = {
    "global-edge-site": "skipped", "edge-site": "skipped", "schema-registry": "succeeded",
    "adr-ns": "succeeded", "aio-enablement": "succeeded", "aio-instance": "succeeded",
    "schema-registry-role": "succeeded", "resolve-aio": "skipped", "secretsync": "skipped",
}


class FleetRunError(ValueError):
    def __init__(self, message: str, code=1):
        self.code = code if type(code) is int and 1 <= code <= 255 else 1
        super().__init__(message)


def read_json(path: Path) -> dict:
    with open_regular_file(path) as stream:
        value = load_artifact_json(stream.read(2 * 1024 * 1024 + 1),
                                   limit=2 * 1024 * 1024, label="Fleet result")
    if not isinstance(value, dict):
        raise FleetRunError("Fleet command returned an unsupported result.")
    return value


def write_private(path: Path, value: dict) -> str:
    raw = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def check_run(document: dict, version: str) -> None:
    summary = document.get("summary", {})
    if not isinstance(summary, dict):
        raise FleetRunError("The deployment summary is invalid.")
    sites, operations = summary.get("sites", {}), summary.get("operations", {})
    if not all(isinstance(value, dict) for value in (
        sites, operations, sites.get("counts") if isinstance(sites, dict) else None,
        operations.get("counts") if isinstance(operations, dict) else None, document.get("engine"),
    )):
        raise FleetRunError("The deployment counters are invalid.")
    if (
        document.get("apiVersion") != "siteops/v1alpha1" or document.get("kind") != "DeploymentRun"
        or document.get("projection") != "local-private" or document.get("status") != "succeeded"
        or type(document.get("exitCode")) is not int or document["exitCode"] != 0
        or document.get("engine", {}).get("version") != version
        or type(sites.get("total")) is not int or sites["total"] != 2
        or sites.get("counts", {}).get("succeeded") != 2
        or type(operations.get("total")) is not int or operations["total"] != 18
        or any(type(value) is not int or value < 0 for value in (
            *sites["counts"].values(), *operations["counts"].values(),
        ))
        or sum(sites["counts"].values()) != 2 or sum(operations["counts"].values()) != 18
        or operations["counts"].get("succeeded") != 10 or operations["counts"].get("skipped") != 8
        or operations.get("counts", {}).get("failed", 0) != 0
        or summary.get("interrupted") is not False
    ):
        raise FleetRunError("The deployment did not report two successfully completed targets.")
    targets = document.get("sites")
    if (not isinstance(targets, list) or len(targets) != 2 or any(not isinstance(row, dict) for row in targets)
            or {row.get("target") for row in targets} != {"fleet-one", "fleet-two"}):
        raise FleetRunError("The executed target identities differ from the selected fleet.")
    for target in targets:
        values = target.get("operations")
        if (target.get("status") != "succeeded" or not isinstance(values, list)
                or len(values) != len(EXPECTED_OPERATIONS)
                or any(not isinstance(row, dict) or not isinstance(row.get("identity"), dict) for row in values)
                or any(row.get("identity", {}).get("target") != target["target"] for row in values)
                or {row.get("identity", {}).get("step"): row.get("status") for row in values} != EXPECTED_OPERATIONS):
            raise FleetRunError("The executed operation identities or outcomes differ from the prepared fleet.")


def observe_versions(scope, slots: dict, logs: Path, *, runner=_run_az, timeout=600, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    waiting = set(RELEASES)
    attempt = 0
    while waiting:
        attempt += 1
        for slot in tuple(waiting):
            if clock() >= deadline:
                raise FleetRunError("AIO extension readiness exceeded its deadline.")
            code, stdout, stderr = runner([
                "az", "k8s-extension", "list", "--subscription", scope.subscription,
                "--resource-group", scope.group(slot), "--cluster-name", scope.cluster(slot),
                "--cluster-type", "connectedClusters", "--only-show-errors", "-o", "json",
            ])
            (logs / f"extensions-{slot}-{attempt}.out").write_bytes(stdout)
            (logs / f"extensions-{slot}-{attempt}.err").write_bytes(stderr)
            if clock() >= deadline:
                raise FleetRunError("AIO extension readiness exceeded its deadline.")
            if code:
                raise FleetRunError("The deployed extension version could not be observed.")
            values = load_artifact_json(stdout, limit=2 * 1024 * 1024, label="Extension observation")
            if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
                raise FleetRunError("The extension observation is invalid.")
            matches = [row for row in values if isinstance(row, dict)
                       and str(row.get("extensionType", "")).casefold() == "microsoft.iotoperations"]
            if len(matches) > 1:
                raise FleetRunError("The owned target has ambiguous AIO extensions.")
            if matches and matches[0].get("provisioningState") == "Succeeded":
                if matches[0].get("version") != slots[slot]["version"]:
                    raise FleetRunError("The deployed extension differs from the selected AIO release.")
                waiting.remove(slot)
        if not waiting:
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise FleetRunError("AIO extension readiness exceeded its deadline.")
        sleep(min(15, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--ownership", required=True, type=Path)
    parser.add_argument("--expected-ownership-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    scope = None
    code = 1
    timings = {}
    try:
        if args.output.exists() or os.name != "posix":
            raise FleetRunError("Fleet execution requires Linux and a new receipt path.")
        budget = FleetBudget.from_environment()
        budget.remaining("deployed", 60)
        selected = FleetCandidate.parse(
            os.environ["FLEET_CANDIDATE"].encode(), repository=os.environ["GITHUB_REPOSITORY"],
            commit=os.environ["GITHUB_SHA"], ref=os.environ["GITHUB_REF"],
        )
        scope = scope_for(
            selected, args.inputs, run=int(os.environ["GITHUB_RUN_ID"]),
            attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]), subscription=os.environ["AZURE_SUBSCRIPTION_ID"],
        )
        ownership = expected_document(args.ownership, args.expected_ownership_sha)
        owned_slots = validate_ownership(scope, ownership)
        qualification = read_json(args.qualification)
        project = args.state / "probe-state" / "operator"
        cache = args.state / "probe-state" / "cache"
        pin = project / "siteops.pin"
        if (
            qualification.get("kind") != "WorkspaceEngineQualification"
            or qualification.get("engineSelectionSha256") != selected.artifacts["engine"]["sha256"]
            or qualification.get("workspaceInventorySha256") != selected.artifacts["workspaces"]["sha256"]
            or qualification.get("planSha256") != selected.artifacts["plan"]["sha256"]
            or qualification.get("project", {}).get("workspace") != "workspaces/iot-operations"
            or qualification.get("project", {}).get("sourceReleaseObservation") != "not-performed"
            or qualification.get("project", {}).get("pinSha256") != hashlib.sha256(pin.read_bytes()).hexdigest()
        ):
            raise FleetRunError("The controller project differs from installed qualification.")
        groups = AzureGroups(scope, args.state / "group-observations")
        if any(not scope.owns(slot, groups.show(slot), owned_slots[slot]["ownerSha256"]) for slot in RELEASES):
            raise FleetRunError("Fleet deployment requires the owned resource groups.")
        python = args.state / "application" / "bin" / "python"
        command = args.state / "application" / "bin" / "siteops"
        # Installation overlaps host startup. Start a fresh, unchanged one-hour
        # test policy from admitted source and roots, not an expired setup policy.
        verifier = ReleaseVerifier(
            args.state / "fleet-workspace-policy", args.state / "workspace-policy" / "root.json",
            selected.source, signer=".github/workflows/_workspace-distribution.yaml",
            builder=selected.producer["caller"], runner_environment="self-hosted",
        )
        policy, roots = verifier.policy_file, verifier.root
        logs = args.state / "fleet-private"
        logs.mkdir(mode=0o700)
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        environment.update(SITEOPS_CACHE_DIR=str(cache), SITEOPS_REDACT_OUTPUT="1", PYTHONSAFEPATH="1")
        common = [str(command), "--project", str(project), "--trust-policy", str(policy), "--trusted-root", str(roots)]

        def execute(name, arguments, *, timeout=300, private=False):
            command_environment = {**environment, "SITEOPS_REDACT_OUTPUT": "0"} if private else environment
            started = time.monotonic()
            value = run(arguments, cwd=project, logs=logs, name=name,
                        timeout=budget.remaining("deployed", timeout), environment=command_environment)
            timings[name] = round(time.monotonic() - started, 3)
            print(f"Fleet {name} completed in {timings[name]:.1f}s (exit {value}).", flush=True)
            if value:
                raise FleetRunError(f"The installed fleet phase {name} failed.", value)
            budget.remaining("deployed", 1)

        targets = []
        for slot, release in RELEASES.items():
            cluster_id = f"/subscriptions/{scope.subscription}/resourceGroups/{scope.group(slot)}/providers/Microsoft.Kubernetes/connectedClusters/{scope.cluster(slot)}"
            answers = logs / f"{slot}-answers.json"
            write_private(answers, {
                "apiVersion": "siteops.inputs/v1", "kind": "SiteInputValues",
                "values": {"cluster": cluster_id, "siteName": f"fleet-{slot}", "environment": "fleet",
                           "aioRelease": release, "enableSecretSync": False, "brokerMemoryProfile": "Low"},
            })
            execute(f"save-{slot}", [
                *common, "inputs", "aio-install", "--offline-content", "--input-file", str(answers),
                "--read-resources", "--save-site", str(project / "sites" / f"fleet-{slot}.yaml"),
            ])
            targets.append({"slot": slot, "name": f"fleet-{slot}", "release": release,
                            "subscription": scope.subscription, "resourceGroup": scope.group(slot),
                            "cluster": scope.cluster(slot)})
        write_private(project / "sites" / "fleet-unselected.yaml", {
            "apiVersion": "siteops/v1", "kind": "Site", "name": "fleet-unselected",
            "subscription": scope.subscription, "resourceGroup": "never-deploy-sentinel",
            "location": "eastus", "labels": {"environment": "dev"},
            "parameters": {"clusterName": "never-deploy-sentinel"},
        })
        specification = logs / "plan-spec.json"
        digest = write_private(specification, {
            "engineVersion": qualification["engineVersion"], "project": str(project),
            "policy": str(policy), "trustedRoot": str(roots), "targets": targets,
            "privateOutput": str(logs / "executable-plan.json"),
        })
        execute("inspect-plan", [
            str(python), "-I", str(Path(__file__).with_name("probe-fleet-plan.py")),
            "--spec", str(specification), "--expected-spec-sha", digest,
        ], timeout=1800, private=True)
        inspection = read_json(logs / "inspect-plan.out")
        if (inspection.get("kind") != "FleetPlanInspection"
                or inspection.get("operationIdentitiesVerified") is not True
                or inspection.get("parametersVerified") is not True):
            raise FleetRunError("Fleet executable-plan assertions were not completed.")
        execute("deploy", [
            *common, "deploy", "aio-install", "--offline-content", "-l", SELECTOR,
            "--parallel", "2", "--yes", "--output", "json", "--projection", "local-private",
        ], timeout=9000, private=True)
        check_run(read_json(logs / "deploy.out"), qualification["engineVersion"])
        observe_versions(scope, inspection["slots"], logs, timeout=budget.remaining("deployed", 600))
        code = 0
        report = {
            "apiVersion": "siteops.release.fleet/v1", "kind": "FleetDeployment",
            "context": scope.context(), "scopeKey": scope.key, "status": "succeeded",
            "engineVersion": qualification["engineVersion"], "targets": 2,
            "deployInvocations": 1, "parallel": 2, "sentinelExcluded": True,
            "resolvedParametersVerified": True, "extensionVersionsVerified": True,
            "slots": inspection["slots"], "podReadiness": "pending-host-observation",
            "workloadFunctionality": "not-checked",
        }
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        code = error.code if isinstance(error, (FleetRunError, FleetProcessError)) else 1
        detail = str(error) if isinstance(error, (FleetRunError, FleetProcessError, CoordinationError)) else "Controller inputs or observations were invalid."
        print(f"Fleet deployment acceptance failed: {detail}", file=sys.stderr)
        report = {"apiVersion": "siteops.release.fleet/v1", "kind": "FleetDeployment",
                  "status": "failed", "operationExit": code}
        if scope is not None:
            report.update(context=scope.context(), scopeKey=scope.key)
    try:
        if timings:
            write_private(args.state / "fleet-private" / "phase-timing.json", timings)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, sort_keys=True) + "\n")
    except OSError:
        print("The fleet deployment receipt could not be retained.", file=sys.stderr)
        return code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())

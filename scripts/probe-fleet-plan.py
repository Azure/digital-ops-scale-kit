# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Inspect the ordinary installed planner without exposing resolved operator values."""

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path

EXPECTED_STEPS = {
    "global-edge-site": "skip", "edge-site": "skip", "schema-registry": "execute",
    "adr-ns": "execute", "aio-enablement": "execute", "aio-instance": "execute",
    "schema-registry-role": "execute", "resolve-aio": "skip", "secretsync": "skip",
}
SELECTOR = "name=fleet-one,name=fleet-two"


def inspect(spec: dict) -> dict:
    import siteops
    from siteops.cli import main as siteops_main
    from siteops.orchestrator import Orchestrator
    from siteops.planning import LiteralValue, PlanStatus, resolve_plan_value
    from siteops.yamlio import load

    prefix = Path(sys.prefix).resolve()
    if (not sys.flags.isolated or sys.prefix == sys.base_prefix
            or not Path(siteops.__file__).resolve().is_relative_to(prefix)
            or importlib.metadata.version("siteops") != spec["engineVersion"]
            or siteops.__version__ != spec["engineVersion"]):
        raise ValueError("Fleet planning requires the selected isolated engine.")
    rows = spec["targets"]
    if (not isinstance(rows, list) or len(rows) != 2
            or any(not isinstance(row, dict) or set(row) != {
                "slot", "name", "release", "subscription", "resourceGroup", "cluster",
            } for row in rows)
            or {row["slot"] for row in rows} != {"one", "two"}
            or any(row["name"] != f"fleet-{row['slot']}"
                   or row["release"] != {"one": "2607", "two": "2608"}[row["slot"]] for row in rows)):
        raise ValueError("The fleet target specification is incomplete.")
    wanted = {row["name"]: row for row in rows}
    observed = []
    original = Orchestrator.build_plan

    def checked_plan(orchestrator, *args, **kwargs):
        result = original(orchestrator, *args, **kwargs)
        plan = result.plan
        if (
            observed or result.status is not PlanStatus.PLANNED or not result.executable or plan is None
            or plan.cli_selector != SELECTOR or plan.max_parallel_sites != 2
            or plan.compilation_binding.value != "package-artifact"
            or len(plan.targets) != 2 or {target.name for target in plan.targets} != set(wanted)
        ):
            raise ValueError("The installed planner did not select exactly the intended fleet.")
        versions = {}

        def parameters(operation, names):
            if operation.details.parameters is None:
                raise ValueError("The deployment parameters were not prepared.")
            return {
                entry.key.value: resolve_plan_value(entry.value, {})
                for entry in operation.details.parameters.entries
                if isinstance(entry.key, LiteralValue) and entry.key.value in names
            }

        for target in plan.targets:
            row = wanted[target.name]
            if target.subscription != row["subscription"] or target.resource_group != row["resourceGroup"]:
                raise ValueError("The prepared fleet target differs from the owned scope.")
            site = orchestrator.load_site(target.name)
            if (site.properties.get("aioRelease") != row["release"]
                    or site.properties.get("deployOptions", {}).get("enableSecretSync") is not False
                    or site.parameters.get("clusterName") != row["cluster"]):
                raise ValueError("The effective Site does not match the requested fleet inputs.")
            operations = {operation.identity.step: operation for operation in target.operations}
            if (len(operations) != len(target.operations)
                    or {key: value.disposition.value for key, value in operations.items()} != EXPECTED_STEPS
                    or any(operation.identity.target != target.name for operation in operations.values())):
                raise ValueError("The fleet operation identities or dispositions changed.")
            with (orchestrator.workspace / "parameters" / "aio-releases" / (row["release"] + ".yaml")).open() as stream:
                configuration = load(stream)
            if (not isinstance(configuration.get("aioVersion"), str)
                    or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", configuration["aioVersion"]) is None
                    or not isinstance(configuration.get("aioApiVersion"), str)
                    or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview)?", configuration["aioApiVersion"]) is None):
                raise ValueError("The selected AIO release identity is invalid.")
            values = parameters(operations["aio-instance"], {
                "aioVersion", "aioApiVersion", "aioReleaseConfiguration", "clusterName", "brokerConfig",
            })
            enabled = parameters(operations["aio-enablement"], {"certManagerVersion", "secretStoreVersion"})
            if (values.get("aioVersion") != configuration["aioVersion"]
                    or values.get("aioApiVersion") != configuration["aioApiVersion"]
                    or values.get("aioReleaseConfiguration") != configuration["aioReleaseConfiguration"]
                    or enabled.get("certManagerVersion") != configuration["certManagerVersion"]
                    or enabled.get("secretStoreVersion") != configuration["secretStoreVersion"]
                    or values.get("clusterName") != row["cluster"]
                    or not isinstance(values.get("brokerConfig"), dict)
                    or values["brokerConfig"].get("memoryProfile") != "Low"):
                raise ValueError("The resolved deployment parameters differ from the requested release.")
            versions[row["slot"]] = {
                "release": row["release"], "version": configuration["aioVersion"],
                "apiVersion": configuration["aioApiVersion"],
            }
        if len({value["version"] for value in versions.values()}) != 2:
            raise ValueError("The fleet did not select two distinct AIO versions.")
        observed.append(versions)
        return result

    Orchestrator.build_plan = checked_plan
    previous = sys.argv
    sys.argv = [
        "siteops", "--project", spec["project"], "--trust-policy", spec["policy"],
        "--trusted-root", spec["trustedRoot"], "plan", "aio-install", "--offline-content",
        "-l", SELECTOR, "--parallel", "2", "--output", "json", "--projection", "local-private",
    ]
    try:
        with (
            Path(spec["privateOutput"]).open("x") as stream,
            Path(spec["privateOutput"] + ".err").open("x") as errors,
            contextlib.redirect_stdout(stream),
            contextlib.redirect_stderr(errors),
        ):
            try:
                siteops_main()
            except SystemExit as result:
                if result.code != 0:
                    raise ValueError("The ordinary fleet planning command failed.") from None
    finally:
        sys.argv = previous
        Orchestrator.build_plan = original
    if len(observed) != 1:
        raise ValueError("Fleet planning did not produce one verified plan.")
    for name, module in tuple(sys.modules.items()):
        if name == "siteops" or name.startswith("siteops."):
            origin = getattr(module, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(prefix):
                raise ValueError("Fleet planning imported code outside the selected installation.")
    return {
        "kind": "FleetPlanInspection", "engineVersion": spec["engineVersion"],
        "targetCount": 2, "parallel": 2, "operationIdentitiesVerified": True,
        "parametersVerified": True, "slots": observed[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--expected-spec-sha", required=True)
    args = parser.parse_args()
    try:
        with args.spec.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.expected_spec_sha:
            raise ValueError("The fleet plan specification changed.")
        spec = json.loads(raw)
        report = inspect(spec)
    except (ValueError, OSError, KeyError, TypeError, AttributeError, ImportError):
        print("Fleet executable plan inspection failed. Inspect private controller diagnostics.", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

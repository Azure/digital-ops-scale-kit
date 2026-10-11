# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Closed release acceptance receipts and their aggregation for one exact candidate."""

from __future__ import annotations

import itertools
import re

from fleet_workflow import FleetCandidate
from release_fleet import GROUP_MODES, SITE_SLOTS, VAULT_PURGE, VERSION

API = "siteops.release.acceptance/v1"
SCENARIOS = ("installer", "site-aio", "site-existing-secretsync", "site-combined", "fleet")
SLOT_SCENARIOS = {"disabled": "site-aio", "enabled": "site-combined", "existing": "site-existing-secretsync"}
ROW_STATUS = ("passed", "failed", "missing", "skipped", "cancelled", "ambiguous", "wrong-candidate", "not-applicable")
CLEANUP = ("confirmed-absent", "residual", "unknown", "not-attempted", "not-applicable")
ASSERTIONS = {
    "installer": ("cells-complete", "bytes-in-inventory", "ubuntu-26.04", "windows-standard-user"),
    "site-aio": (
        "engine-installed-origin", "candidate-project-pin", "guided-inspection-no-read",
        "prerequisite-refused-no-write", "guided-plan-operations", "manual-inline-configured-routes",
        "deploy-succeeded", "aio-readiness", "secretsync-absent",
    ),
    "site-combined": (
        "engine-installed-origin", "candidate-project-pin", "guided-inspection-no-read",
        "guided-plan-operations", "deploy-succeeded", "aio-readiness", "secretsync-bound",
        "vault-created-by-enablement",
    ),
    "site-existing-secretsync": (
        "engine-installed-origin", "candidate-project-pin", "guided-inspection-no-read",
        "guided-plan-operations", "deploy-succeeded", "secretsync-absent", "existing-vault-created",
        "existing-plan-operations", "existing-enable-succeeded", "aio-readiness", "secretsync-bound",
        "existing-vault-used",
    ),
    "fleet": (
        "two-targets", "one-invocation-parallel-2", "sentinel-excluded", "plan-parameters",
        "extension-versions", "pod-readiness",
    ),
}
# Each named Site case step contributes these assertions only when it succeeded.
STEP_ASSERTIONS = {
    "engine": ("engine-installed-origin", "candidate-project-pin"),
    "guided": ("guided-inspection-no-read", "guided-plan-operations"),
    "routes": ("manual-inline-configured-routes",),
    "deploy": ("deploy-succeeded",),
    "existing-vault": ("secretsync-absent", "existing-vault-created"),
    "existing-enable": ("existing-plan-operations", "existing-enable-succeeded"),
    "readiness": ("aio-readiness",),
}
SITE_JOB = {slot: f"Site case ({scenario})" for slot, scenario in SLOT_SCENARIOS.items()}
FLEET_JOBS = (
    "Select fleet candidate", "Fleet prepare", "Fleet host (one)", "Fleet host (two)", "Fleet controller",
    "Fleet cleanup", "Require complete fleet acceptance",
)
INSTALLER_JOBS = ("Build bundle", "Attest bundle", "Assemble release")
INSTALLER_CELLS = (
    *(f"Qualify bundle ({system}, Python {python})"
      for system in ("ubuntu-24.04", "windows-2025") for python in ("3.10", "3.11", "3.12", "3.13", "3.14")),
    "Qualify bundle (ubuntu-26.04, Python 3.11)",
    "Qualify bundle (windows-2025, Python 3.11, standard user)",
)
SEVERITY = ("wrong-candidate", "ambiguous", "missing", "cancelled", "skipped", "failed", "passed")
ENGINE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]{0,96})")
TOKEN = re.compile(r"[A-Za-z0-9._-]{1,64}")


class AcceptanceError(ValueError):
    """Evidence that cannot be bound to the selected candidate or acceptance run."""


def worst(statuses) -> str:
    values = list(statuses)
    return min(values, key=SEVERITY.index) if values else "missing"


def job_status(job: dict) -> str:
    if job.get("status") != "completed":
        return "missing"
    return {"success": "passed", "skipped": "skipped", "cancelled": "cancelled"}.get(job.get("conclusion"), "failed")


def matching(values: list[dict], name: str) -> list[dict]:
    return [job for job in values if isinstance(job.get("name"), str) and job["name"].split(" / ")[-1] == name]


def latest_job(values: list[dict], name: str) -> tuple[int | None, str]:
    """Select the latest attempt that ran a job, which governs that job's evidence."""
    matches = matching(values, name)
    if not matches:
        return None, "missing"
    attempt = max(job["run_attempt"] for job in matches)
    latest = [job for job in matches if job["run_attempt"] == attempt]
    return attempt, job_status(latest[0]) if len(latest) == 1 else "ambiguous"


def matrix_cells(matrix: object) -> list[dict]:
    """Expand a literal matrix with GitHub's documented exclude and include rules."""
    if not isinstance(matrix, dict):
        raise AcceptanceError("The installer matrix is not a literal mapping.")
    base = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    entries = {key: matrix.get(key, []) for key in ("include", "exclude")}
    if (
        not base
        or any(not isinstance(values, list) or not values or any(type(item) is not str for item in values)
               for values in base.values())
        or any(not isinstance(rows, list) or any(
            not isinstance(row, dict) or not row
            or any(type(key) is not str or type(value) is not str for key, value in row.items())
            for row in rows) for rows in entries.values())
    ):
        raise AcceptanceError("The installer matrix is not a closed literal matrix.")
    originals = [dict(zip(base, values, strict=True)) for values in itertools.product(*base.values())]
    originals = [cell for cell in originals
                 if not any(all(cell.get(key) == value for key, value in row.items()) for row in entries["exclude"])]
    cells = [dict(cell) for cell in originals]
    added = []
    for row in entries["include"]:
        merged = False
        for original, cell in zip(originals, cells, strict=True):
            if all(original[key] == value for key, value in row.items() if key in original):
                cell.update(row)
                merged = True
        if not merged:
            added.append(dict(row))
    return cells + added


def installer_cells(workflow: object) -> list[str]:
    """Name each installer qualification job the candidate's distribution workflow declares."""
    try:
        matrix = workflow["jobs"]["qualify"]["strategy"]["matrix"]
    except (KeyError, TypeError):
        raise AcceptanceError("The installer qualification matrix is unavailable.") from None
    names = []
    for cell in matrix_cells(matrix):
        if (set(cell) - {"os", "python", "account"} or not {"os", "python"} <= set(cell)
                or cell.get("account", "runner") not in {"runner", "standard"}
                or any(TOKEN.fullmatch(value) is None for value in cell.values())):
            raise AcceptanceError("An installer matrix cell is outside the acceptance contract.")
        suffix = ", standard user" if cell.get("account") == "standard" else ""
        names.append(f"Qualify bundle ({cell['os']}, Python {cell['python']}{suffix})")
    if len(set(names)) != len(names):
        raise AcceptanceError("The installer matrix names a cell more than once.")
    return names


def row(scenario: str, status: str, assertions=(), cleanup="not-applicable", vault_purge="not-applicable",
        groups=None) -> dict:
    """One scenario row. Groups records ephemeral or persistent, unknown without a receipt."""
    expected = ASSERTIONS[scenario]
    return {
        "scenario": scenario, "status": status, "groups": groups or (
            "not-applicable" if scenario == "installer" else "unknown"),
        "assertions": [name for name in expected if name in set(assertions)],
        "cleanup": cleanup, "vaultPurge": vault_purge,
    }


def installer_row(producer_jobs: list[dict], workflow: object) -> dict:
    try:
        cells = installer_cells(workflow)
    except AcceptanceError:
        return row("installer", "missing")
    if not set(INSTALLER_CELLS) <= set(cells):
        return row("installer", "missing")
    statuses = {}
    for name in (*INSTALLER_JOBS, *cells):
        matches = matching(producer_jobs, name)
        statuses[name] = (job_status(matches[0]) if len(matches) == 1
                          else "missing" if not matches else "ambiguous")
    assertions = []
    if all(statuses[name] == "passed" for name in cells):
        assertions.append("cells-complete")
    if all(statuses[name] == "passed" for name in INSTALLER_JOBS):
        assertions.append("bytes-in-inventory")
    if statuses[INSTALLER_CELLS[-2]] == "passed":
        assertions.append("ubuntu-26.04")
    if statuses[INSTALLER_CELLS[-1]] == "passed":
        assertions.append("windows-standard-user")
    return row("installer", worst(statuses.values()), assertions)


def _artifact(artifacts: list[dict], name: str | None, *, run: int, commit: str) -> tuple[dict | None, bool]:
    if name is None:
        return None, True
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == name]
    if not matches:
        return None, True
    item = matches[0]
    source = item.get("workflow_run")
    valid = (
        len(matches) == 1 and type(item.get("id")) is int and item["id"] > 0 and item.get("expired") is False
        and type(item.get("size_in_bytes")) is int and item["size_in_bytes"] > 0
        and isinstance(item.get("digest"), str) and re.fullmatch("sha256:[0-9a-f]{64}", item["digest"]) is not None
        and isinstance(source, dict) and type(source.get("id")) is int and source["id"] == run
        and source.get("head_sha") == commit
    )
    return (item, True) if valid else (None, False)


def select_evidence(run_jobs: list[dict], artifacts: list[dict], *, run: int, commit: str) -> dict:
    """Choose the receipt of the latest attempt that ran each scenario.

    Site cases may be retried alone. The fleet must come from one attempt, so a
    partial rerun of its jobs is reported as ambiguous evidence.
    """
    evidence = {}
    for slot in SITE_SLOTS:
        attempt, status = latest_job(run_jobs, SITE_JOB[slot])
        item, unique = _artifact(artifacts, attempt and f"site-outcome-{run}-{attempt}-{slot}", run=run, commit=commit)
        evidence[slot] = {"attempt": attempt, "status": status if unique else "ambiguous", "artifact": item}
    latest = {name: latest_job(run_jobs, name) for name in FLEET_JOBS}
    attempts = {attempt for attempt, _ in latest.values() if attempt is not None}
    status = worst(value for _, value in latest.values())
    attempt = next(iter(attempts)) if len(attempts) == 1 else None
    if len(attempts) > 1:
        status = "ambiguous"
    item, unique = _artifact(artifacts, attempt and f"fleet-acceptance-{run}-{attempt}", run=run, commit=commit)
    evidence["fleet"] = {"attempt": attempt, "status": status if unique else "ambiguous", "artifact": item,
                         "partial": len(attempts) > 1}
    return evidence


def _context_status(document: dict, expected: dict) -> str | None:
    context = document.get("context")
    if not isinstance(context, dict) or set(context) != set(expected):
        return "ambiguous"
    for key, value in expected.items():
        if type(context[key]) is not type(value) or context[key] != value:
            return "ambiguous" if key in {"run", "attempt"} else "wrong-candidate"
    return None


def check_site_outcome(document: object, slot: str, expected: dict) -> dict:
    scenario = SLOT_SCENARIOS[slot]
    if not isinstance(document, dict) or set(document) != {
        "apiVersion", "kind", "context", "scopeKey", "slot", "scenario", "status", "groups", "transport",
        "engineVersion", "assertions", "cleanup", "vaultPurge", "workloadFunctionality", "secretMaterialization",
    }:
        return row(scenario, "ambiguous", cleanup="unknown", vault_purge="not-attempted")
    mismatch = _context_status(document, expected)
    assertions = document["assertions"]
    if (
        mismatch is None and (
            document["apiVersion"] != API or document["kind"] != "SiteScenarioOutcome"
            or document["slot"] != slot or document["scenario"] != scenario or document["groups"] not in GROUP_MODES
            or document["status"] not in {"passed", "failed"} or document["transport"] != "prepublication"
            or document["workloadFunctionality"] != "not-checked"
            or document["secretMaterialization"] != "not-checked"
            or not isinstance(document["scopeKey"], str) or re.fullmatch("[0-9a-f]{64}", document["scopeKey"]) is None
            or (document["engineVersion"] is not None and (
                not isinstance(document["engineVersion"], str)
                or ENGINE_VERSION.fullmatch(document["engineVersion"]) is None))
            or not isinstance(assertions, list) or any(type(item) is not str for item in assertions)
            or len(set(assertions)) != len(assertions) or not set(assertions) <= set(ASSERTIONS[scenario])
            or document["cleanup"] not in CLEANUP[:-1] or document["vaultPurge"] not in VAULT_PURGE
            or (document["status"] == "passed") != (
                set(assertions) == set(ASSERTIONS[scenario]) and document["cleanup"] == "confirmed-absent")
        )
    ):
        mismatch = "ambiguous"
    if mismatch:
        return row(scenario, mismatch, cleanup="unknown", vault_purge="not-attempted")
    return row(scenario, document["status"], assertions, document["cleanup"], document["vaultPurge"],
               document["groups"])


FLEET_ACCEPTANCE = {
    "apiVersion": API, "kind": "FleetAcceptance", "status": "passed", "targetCount": 2, "deployInvocations": 1,
    "planParameters": "checked", "extensionVersions": "checked", "podReadiness": "checked",
    "cleanup": "confirmed-absent", "workloadFunctionality": "not-checked",
}


def check_fleet(document: object, expected: dict) -> dict:
    if (not isinstance(document, dict) or set(document) != {*FLEET_ACCEPTANCE, "context", "groups"}
            or document["groups"] not in GROUP_MODES):
        return row("fleet", "ambiguous", cleanup="unknown")
    mismatch = _context_status(document, expected)
    if mismatch:
        return row("fleet", mismatch, cleanup="unknown", groups=document["groups"])
    if any(type(document[key]) is not type(value) or document[key] != value
           for key, value in FLEET_ACCEPTANCE.items()):
        return row("fleet", "failed", cleanup="unknown", groups=document["groups"])
    return row("fleet", "passed", ASSERTIONS["fleet"], "confirmed-absent", groups=document["groups"])


def evidence_row(scenario: str, selected: dict, document: object, expected: dict) -> dict:
    """Combine the governing job status with its receipt. Either one failing fails the row."""
    if selected["status"] in {"missing", "ambiguous"} or selected["attempt"] is None:
        return row(scenario, selected["status"] if selected["status"] in {"missing", "ambiguous"} else "missing",
                   cleanup="unknown", vault_purge="not-attempted" if scenario != "fleet" else "not-applicable")
    if document is None:
        status = selected["status"] if selected["status"] in {"skipped", "cancelled"} else "missing"
        return row(scenario, status, cleanup="unknown",
                   vault_purge="not-attempted" if scenario != "fleet" else "not-applicable")
    context = {**expected, "attempt": selected["attempt"]}
    checked = (check_fleet(document, context) if scenario == "fleet"
               else check_site_outcome(document, next(s for s, v in SLOT_SCENARIOS.items() if v == scenario), context))
    if checked["status"] == "passed" and selected["status"] != "passed":
        checked["status"] = selected["status"]
    return checked


def required_scenarios(plan: object, candidate: FleetCandidate) -> set[str]:
    if (
        not isinstance(plan, dict) or plan.get("kind") != "ReleaseCandidate" or plan.get("active") is not True
        or plan.get("source") != candidate.source or plan.get("dryRun") is not candidate.producer["preview"]
        or not isinstance(plan.get("siteops"), dict) or type(plan["siteops"].get("bundle")) is not bool
        or not isinstance(plan.get("workspaces"), list)
    ):
        raise AcceptanceError("The release plan does not describe the selected candidate.")
    required = set(SCENARIOS) - {"installer"}
    if plan["siteops"]["bundle"]:
        required.add("installer")
    return required


def workspace_declared(plan: dict) -> bool:
    return sum(isinstance(item, dict) and item.get("id") == "azure.iot-operations"
               and item.get("workspace") == "workspaces/iot-operations" for item in plan["workspaces"]) == 1


def receipt(candidate: FleetCandidate, *, run: int, attempt: int, environment: str, rows: list[dict],
            complete: bool) -> dict:
    artifacts = candidate.artifacts
    status = "passed" if complete and all(item["status"] in {"passed", "not-applicable"} for item in rows) and any(
        item["status"] == "passed" for item in rows) else "failed"
    return {
        "apiVersion": API, "kind": "ReleaseAcceptance",
        "candidate": {
            "repository": candidate.source["repository"], "sourceCommit": candidate.source["commit"],
            "sourceRef": candidate.source["ref"], "caller": candidate.producer["caller"],
            "producerRun": candidate.producer["run"], "producerAttempt": candidate.producer["attempt"],
            "preview": candidate.producer["preview"], "admissionSha256": artifacts["admission"]["sha256"],
            "planSha256": artifacts["plan"]["sha256"], "inventorySha256": artifacts["inventory"]["sha256"],
        },
        "acceptance": {"run": run, "attempt": attempt}, "environment": environment,
        "transport": "prepublication", "publicRelease": "not-observed", "status": status,
        "scenarios": rows, "workloadFunctionality": "not-checked", "secretMaterialization": "not-checked",
    }


def aggregate(candidate: FleetCandidate, *, plan: object, bound: bool | None, producer_jobs: list[dict] | None,
              run_jobs: list[dict] | None, artifacts: list[dict] | None, documents: dict, workflow: object,
              run: int, attempt: int, environment: str, needs: object) -> dict:
    """Bind every required scenario to this candidate. Missing evidence is never success.

    `bound` is None when the admission or plan is unavailable, which records every
    scenario as missing. False records the candidate as mismatched.
    """
    if TOKEN.fullmatch(environment or "") is None:
        raise AcceptanceError("The acceptance environment is not a bounded name.")
    complete = (
        isinstance(needs, dict) and set(needs) == {"fleet-request", "fleet", "prep", "site-groups", "e2e"}
        and all(isinstance(value, dict) and value.get("result") == "success" for value in needs.values())
    )
    if bound is None:
        rows = [row("installer", "missing")] + [
            row(name, "missing", cleanup="unknown", vault_purge="not-applicable" if name == "fleet" else "not-attempted")
            for name in SCENARIOS[1:]
        ]
        return receipt(candidate, run=run, attempt=attempt, environment=environment, rows=rows, complete=False)
    try:
        required = required_scenarios(plan, candidate) if bound else None
    except AcceptanceError:
        required = None
    if required is None:
        rows = [row(name, "wrong-candidate", cleanup="unknown" if name != "installer" else "not-applicable")
                for name in SCENARIOS]
        return receipt(candidate, run=run, attempt=attempt, environment=environment, rows=rows, complete=False)
    expected = {
        "repository": candidate.source["repository"], "sourceCommit": candidate.source["commit"],
        "admissionSha256": candidate.artifacts["admission"]["sha256"],
        "inventorySha256": candidate.artifacts["inventory"]["sha256"], "run": run, "attempt": attempt,
    }
    rows = {}
    if "installer" in required:
        rows["installer"] = (installer_row(producer_jobs, workflow) if producer_jobs is not None
                             else row("installer", "missing"))
    else:
        rows["installer"] = row("installer", "not-applicable")
    declared = workspace_declared(plan)
    selected = (select_evidence(run_jobs, artifacts, run=run, commit=candidate.source["commit"])
                if run_jobs is not None and artifacts is not None and declared else None)
    for key, scenario in (*SLOT_SCENARIOS.items(), ("fleet", "fleet")):
        if selected is None:
            rows[scenario] = row(scenario, "missing", cleanup="unknown",
                                 vault_purge="not-attempted" if key != "fleet" else "not-applicable")
        else:
            rows[scenario] = evidence_row(scenario, selected[key], documents.get(key), expected)
    return receipt(candidate, run=run, attempt=attempt, environment=environment,
                   rows=[rows[name] for name in SCENARIOS], complete=complete)


def site_outcome(context: dict, scope_key: str, slot: str, outcomes: dict, *, qualification: object,
                 readiness: object, cleanup: object, preflight: str, groups: str) -> dict:
    """Record one Site case from named step outcomes, its readiness receipt and owned cleanup."""
    scenario = SLOT_SCENARIOS[slot]
    assertions = set()
    engine_version = None
    project = qualification.get("project") if isinstance(qualification, dict) else None
    if (
        isinstance(qualification, dict) and qualification.get("kind") == "WorkspaceEngineQualification"
        and isinstance(qualification.get("engineVersion"), str)
        and ENGINE_VERSION.fullmatch(qualification["engineVersion"]) is not None
    ):
        engine_version = qualification["engineVersion"]
    for step, names in STEP_ASSERTIONS.items():
        if outcomes.get(step) == "success":
            assertions.update(names)
    if not (engine_version and isinstance(project, dict)
            and project.get("workspace") == "workspaces/iot-operations"
            and project.get("sourceReleaseObservation") == "not-performed"):
        assertions -= set(STEP_ASSERTIONS["engine"])
    if slot == "disabled" and outcomes.get("guided") == "success":
        assertions.add("prerequisite-refused-no-write")
    ready = (
        isinstance(readiness, dict) and readiness.get("kind") == "PublishedPackageReadiness"
        and all(type(readiness.get(key)) is int and readiness[key] >= 1 for key in (
            "aioInstances", "activePods", "instanceCustomResources"))
        and type(readiness.get("readyActivePods")) is int
        and readiness["readyActivePods"] == readiness["activePods"]
    )
    if not ready or outcomes.get("readiness") != "success":
        assertions.discard("aio-readiness")
    elif readiness.get("secretSyncEnabled") is False and slot == "disabled":
        assertions.add("secretsync-absent")
    elif readiness.get("secretSyncEnabled") is True and slot != "disabled" and readiness.get("spcBoundToInstance") is True:
        assertions.add("secretsync-bound")
        if readiness.get("vaultBinding") == ("existing" if slot == "existing" else "created"):
            assertions.add("existing-vault-used" if slot == "existing" else "vault-created-by-enablement")
    cleanup_state, purge = ("not-attempted", "not-attempted") if preflight != "success" else ("unknown", "not-attempted")
    if (
        isinstance(cleanup, dict) and cleanup.get("apiVersion") == VERSION and cleanup.get("kind") == "SiteCleanup"
        and cleanup.get("context") == context and cleanup.get("scopeKey") == scope_key
        and cleanup.get("groups") == groups
        and isinstance(cleanup.get("slots"), dict) and set(cleanup["slots"]) == {slot}
        and cleanup.get("vaultPurge") in VAULT_PURGE
    ):
        state = cleanup["slots"][slot].get("state") if isinstance(cleanup["slots"][slot], dict) else None
        cleanup_state = {"absent": "confirmed-absent", "residual": "residual",
                         "not-attempted": "not-attempted"}.get(state, "unknown")
        if cleanup_state == "confirmed-absent" and cleanup.get("status") != "complete":
            cleanup_state = "unknown"
        purge = cleanup["vaultPurge"]
    expected = set(ASSERTIONS[scenario])
    assertions &= expected
    return {
        "apiVersion": API, "kind": "SiteScenarioOutcome", "context": context, "scopeKey": scope_key,
        "slot": slot, "scenario": scenario, "groups": groups,
        "status": "passed" if assertions == expected and cleanup_state == "confirmed-absent" else "failed",
        "transport": "prepublication", "engineVersion": engine_version,
        "assertions": [name for name in ASSERTIONS[scenario] if name in assertions],
        "cleanup": cleanup_state, "vaultPurge": purge,
        "workloadFunctionality": "not-checked", "secretMaterialization": "not-checked",
    }


def summary(document: dict) -> str:
    lines = [
        "## Release acceptance", "",
        f"Status: **{document['status']}**. Transport: prepublication. Public release: not observed.", "",
        "| Scenario | Status | Groups | Cleanup | Vault purge |", "|---|---|---|---|---|",
    ]
    lines.extend(f"| {item['scenario']} | {item['status']} | {item['groups']} | {item['cleanup']} | "
                 f"{item['vaultPurge']} |" for item in document["scenarios"])
    return "\n".join(lines) + "\n"

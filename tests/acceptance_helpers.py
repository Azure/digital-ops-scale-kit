"""Closed fixtures for release acceptance receipts, job metadata and candidate inputs."""

import hashlib
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fleet_workflow import ROLES, FleetCandidate  # noqa: E402
from release_acceptance import (  # noqa: E402
    ASSERTIONS,
    FLEET_JOBS,
    INSTALLER_CELLS,
    INSTALLER_JOBS,
    SITE_JOB,
    SLOT_SCENARIOS,
)

SOURCE = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
RUN = 900
PRODUCER = {"run": 42, "attempt": 3, "caller": ".github/workflows/release.yaml", "preview": False}


def write_candidate(root: Path, *, preview=False, bundle=True, workspace=True) -> dict:
    """Write admitted plan and admission documents under root and return their selection."""
    producer = {**PRODUCER, "preview": preview,
                "caller": ".github/workflows/ci.yaml" if preview else ".github/workflows/release.yaml"}
    selection = {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate", "source": dict(SOURCE),
        "producer": producer,
        "artifacts": {role: {"id": index, "sha256": hashlib.sha256(role.encode()).hexdigest()}
                      for index, role in enumerate(ROLES, 11)},
    }
    plan = {
        "apiVersion": "siteops.release/v1", "kind": "ReleaseCandidate", "active": True, "dryRun": preview,
        "source": dict(SOURCE), "release": {"tag": "v1.0.0b7"}, "siteops": {"bundle": bundle},
        "workspaces": [{"id": "azure.iot-operations", "workspace": "workspaces/iot-operations"}] if workspace else [],
    }
    raw = json.dumps(plan).encode()
    (root / "plan").mkdir(parents=True)
    (root / "plan" / "plan.json").write_bytes(raw)
    selection["artifacts"]["plan"]["sha256"] = hashlib.sha256(raw).hexdigest()
    admission = {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission", "source": dict(SOURCE),
        **producer, "artifacts": {"plan": 12, "inventory": 13, "payload": 99},
        "planSha256": selection["artifacts"]["plan"]["sha256"],
        "inventorySha256": selection["artifacts"]["inventory"]["sha256"],
        "subjects": {"engine": 4, "workspace": 1}, "status": "admitted",
        "installation": "not-run", "deployment": "not-run",
    }
    raw = json.dumps(admission).encode()
    (root / "admission").mkdir()
    (root / "admission" / "receipt.json").write_bytes(raw)
    selection["artifacts"]["admission"]["sha256"] = hashlib.sha256(raw).hexdigest()
    return selection


def parse(selection: dict) -> FleetCandidate:
    return FleetCandidate.parse(json.dumps(selection).encode(), repository=SOURCE["repository"],
                                commit=SOURCE["commit"], ref=SOURCE["ref"])


def context(selection: dict, attempt: int, *, run: int = RUN) -> dict:
    return {
        "repository": SOURCE["repository"], "sourceCommit": SOURCE["commit"],
        "admissionSha256": selection["artifacts"]["admission"]["sha256"],
        "inventorySha256": selection["artifacts"]["inventory"]["sha256"], "run": run, "attempt": attempt,
    }


def site_receipt(selection: dict, slot: str, attempt: int, groups="ephemeral", **changes) -> dict:
    scenario = SLOT_SCENARIOS[slot]
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "SiteScenarioOutcome",
        "context": context(selection, attempt), "scopeKey": "d" * 64, "slot": slot, "scenario": scenario,
        "status": "passed", "groups": groups, "transport": "prepublication", "engineVersion": "1.0.0b7",
        "assertions": list(ASSERTIONS[scenario]), "cleanup": "confirmed-absent", "vaultPurge": "purged",
        "workloadFunctionality": "not-checked", "secretMaterialization": "not-checked", **changes,
    }


def fleet_receipt(selection: dict, attempt: int, groups="ephemeral", **changes) -> dict:
    return {
        "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetAcceptance",
        "context": context(selection, attempt), "groups": groups, "status": "passed", "targetCount": 2, "deployInvocations": 1,
        "planParameters": "checked", "extensionVersions": "checked", "podReadiness": "checked",
        "cleanup": "confirmed-absent", "workloadFunctionality": "not-checked", **changes,
    }


class Jobs:
    """Build job metadata with unique IDs, as the jobs API reports executions."""

    def __init__(self, *, run=RUN, commit=SOURCE["commit"]):
        self.run, self.commit, self.values = run, commit, []

    def add(self, name, attempt=1, conclusion="success", *, prefix="", status="completed"):
        self.values.append({
            "id": len(self.values) + 1, "run_id": self.run, "run_attempt": attempt, "head_sha": self.commit,
            "name": prefix + name, "status": status, "conclusion": conclusion,
        })
        return self

    def acceptance(self, attempts=None, fleet_attempt=1, **conclusions):
        attempts = attempts or {}
        for slot in ("disabled", "enabled", "existing"):
            self.add(SITE_JOB[slot], attempts.get(slot, 1), conclusions.get(slot, "success"))
        for name in FLEET_JOBS:
            self.add(name, fleet_attempt, conclusions.get(name, "success"), prefix="Fleet / ")
        return self

    def pages(self):
        return [{"total_count": len(self.values), "jobs": list(self.values)}]


def producer_jobs(**conclusions) -> list[dict]:
    values = Jobs(run=PRODUCER["run"])
    for name in (*INSTALLER_JOBS, *INSTALLER_CELLS, "Admit frozen inputs"):
        prefix = "Prepare release candidate / " + ("" if name == "Assemble release" else "Engine / ")
        values.add(name, PRODUCER["attempt"], conclusions.get(name, "success"), prefix=prefix)
    return values.values


def artifact(name: str, identity: int, *, run=RUN, commit=SOURCE["commit"], **changes) -> dict:
    return {"id": identity, "name": name, "expired": False, "size_in_bytes": 512,
            "digest": "sha256:" + "e" * 64, "workflow_run": {"id": run, "head_sha": commit}, **changes}


def evidence_artifacts(attempts=None, fleet_attempt=1) -> list[dict]:
    attempts = attempts or {}
    values = [artifact(f"site-outcome-{RUN}-{attempts.get(slot, 1)}-{slot}", 500 + index)
              for index, slot in enumerate(("disabled", "enabled", "existing"))]
    values.append(artifact(f"fleet-acceptance-{RUN}-{fleet_attempt}", 600))
    return values


def installer_workflow(*, account_key=True, cells=True) -> dict:
    matrix = {"os": ["ubuntu-24.04", "windows-2025"], "python": ["3.10", "3.11", "3.12", "3.13", "3.14"]}
    if account_key:
        matrix["account"] = ["runner"]
    if cells:
        matrix["include"] = [{"os": "ubuntu-26.04", "python": "3.11"},
                             {"os": "windows-2025", "python": "3.11", "account": "standard"}]
    return {"jobs": {"qualify": {"strategy": {"matrix": matrix}}}}


DOUBLE = r"""
import json
import os
import sys
from pathlib import Path

name, arguments = os.environ["DOUBLE_NAME"], sys.argv[1:]
with open(os.environ["DOUBLE_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps([name, arguments]) + "\n")
rules = json.loads(Path(os.environ["DOUBLE_RULES"]).read_text(encoding="utf-8")).get(name, [])
for rule in rules:
    if all(token in arguments for token in rule["match"]):
        if any(token in arguments for token in rule.get("forbid", [])):
            sys.stderr.write("Forbidden option\n")
            sys.exit(98)
        for option, content in rule.get("write", {}).items():
            Path(arguments[arguments.index(option) + 1]).write_text(content, encoding="utf-8")
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("code", 0))
sys.stderr.write("Unexpected command\n")
sys.exit(99)
"""


def install_doubles(tmp_path: Path, rules: dict) -> Path:
    """Install closed command doubles that reject any command without a matching rule."""
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    script = tmp_path / "double.py"
    script.write_text(DOUBLE, encoding="utf-8")
    (tmp_path / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    python = shlex.quote(Path(sys.executable).as_posix())
    for name in (*rules, "python3"):
        target = binary / name
        body = (f'exec {python} "$@"\n' if name == "python3"
                else f'DOUBLE_NAME={name} exec {python} {shlex.quote(script.as_posix())} "$@"\n')
        target.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        target.chmod(0o755)
    return tmp_path / "calls.jsonl"


def double_environment(tmp_path: Path) -> dict:
    return {"DOUBLE_LOG": (tmp_path / "calls.jsonl").as_posix(), "DOUBLE_RULES": (tmp_path / "rules.json").as_posix()}


def calls(tmp_path: Path) -> list:
    log = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

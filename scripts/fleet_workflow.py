# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bind fleet workflow inputs and coordinate live host jobs through fixed metadata."""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from release_fleet import GROUP_NAME, SLOTS, FleetScope, check_admission, expected_document
from siteops_release_assets import ENGINE_REFERENCE_FILES, FrozenReleaseAssets, publication_assets
from workspace_engine import EngineSelection

from siteops.artifacts import hash_file, load_artifact_json, open_regular_file

ROLES = {
    "admission": "candidate-admission", "plan": "release-plan", "inventory": "release-assets",
    "engine": "workspace-engine", "workspaces": "workspace-release",
}
# Environment secrets that select persistent groups. Empty selects ephemeral groups.
GROUP_SECRETS = {"site": "E2E_SITE_RESOURCE_GROUP", "fleet": "E2E_FLEET_RESOURCE_GROUPS"}


class CoordinationError(ValueError):
    """Fixed workflow diagnostics that do not publish provider or target values."""


class FleetBudget:
    """One workflow-owned clock; later phases never reset the cleanup reserve."""

    CUTOFF_MINUTES = {"participants": 15, "ready": 45, "deployed": 245, "observed": 265, "cleanup": 320}

    def __init__(self, started: str, *, wall=time.time, clock=time.monotonic):
        if not isinstance(started, str) or re.fullmatch(r"[1-9][0-9]{0,11}", started) is None:
            raise CoordinationError("The fleet execution clock is missing or invalid.")
        elapsed = wall() - int(started)
        if not math.isfinite(elapsed) or elapsed < -5:
            raise CoordinationError("The fleet execution clock is in the future.")
        self.clock = clock
        self.origin = clock() - max(0, elapsed)

    @classmethod
    def from_environment(cls) -> FleetBudget:
        return cls(os.environ.get("FLEET_STARTED_AT", ""))

    def remaining(self, phase: str, maximum: float) -> float:
        if phase not in self.CUTOFF_MINUTES or not math.isfinite(maximum) or maximum <= 0:
            raise CoordinationError("The fleet phase budget is invalid.")
        remaining = self.origin + self.CUTOFF_MINUTES[phase] * 60 - self.clock()
        if remaining <= 0:
            raise CoordinationError(f"The fleet {phase} budget expired; cleanup headroom is reserved.")
        return min(maximum, remaining)


@dataclass(frozen=True)
class FleetCandidate:
    source: dict
    producer: dict
    artifacts: dict

    @classmethod
    def parse(cls, raw: bytes, *, repository: str, commit: str, ref: str) -> FleetCandidate:
        value = load_artifact_json(raw, limit=16384, label="Fleet candidate")
        if (
            not isinstance(value, dict) or set(value) != {"apiVersion", "kind", "source", "producer", "artifacts"}
            or value["apiVersion"] != "siteops.release.acceptance/v1" or value["kind"] != "FleetCandidate"
            or not isinstance(value["source"], dict) or set(value["source"]) != {"repository", "commit", "ref"}
            or value["source"]["repository"] != repository or value["source"]["commit"] != commit
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"refs/(heads|tags)/[A-Za-z0-9._/-]+", ref) or ".." in ref
            or not isinstance(value["source"]["ref"], str)
            or not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", value["source"]["ref"])
            or ".." in value["source"]["ref"]
        ):
            raise CoordinationError("The fleet candidate does not match the selected controller source.")
        producer, artifacts = value["producer"], value["artifacts"]
        if (
            not isinstance(producer, dict) or set(producer) != {"run", "attempt", "caller", "preview"}
            or type(producer["preview"]) is not bool
            or producer["caller"] != (
                ".github/workflows/ci.yaml" if producer["preview"] else ".github/workflows/release.yaml"
            )
            or (not producer["preview"] and value["source"]["ref"] != "refs/heads/main")
            or any(type(producer[key]) is not int or not 0 < producer[key] < 10**20 for key in ("run", "attempt"))
            or not isinstance(artifacts, dict) or set(artifacts) != set(ROLES)
            or any(
                not isinstance(row, dict) or set(row) != {"id", "sha256"}
                or type(row["id"]) is not int or not 0 < row["id"] < 10**20
                or not isinstance(row["sha256"], str) or re.fullmatch("[0-9a-f]{64}", row["sha256"]) is None
                for row in artifacts.values()
            )
            or len({row["id"] for row in artifacts.values()}) != len(ROLES)
        ):
            raise CoordinationError("The fleet producer or artifact selection is invalid.")
        return cls(value["source"], producer, artifacts)

    def document(self) -> dict:
        return {"apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate",
                "source": self.source, "producer": self.producer, "artifacts": self.artifacts}

    def verify_artifact(self, role: str, metadata: dict) -> None:
        if role not in ROLES:
            raise CoordinationError("The fleet artifact role is unsupported.")
        expected = self.artifacts[role]
        run = metadata.get("workflow_run")
        if (
            type(metadata.get("id")) is not int or metadata["id"] != expected["id"]
            or metadata.get("name") != f"{ROLES[role]}-{self.producer['run']}-{self.producer['attempt']}"
            or metadata.get("expired") is not False
            or type(metadata.get("size_in_bytes")) is not int or metadata["size_in_bytes"] <= 0
            or not isinstance(metadata.get("digest"), str)
            or re.fullmatch("sha256:[0-9a-f]{64}", metadata["digest"]) is None
            or not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != self.producer["run"]
            or run.get("head_sha") != self.source["commit"]
        ):
            raise CoordinationError("A fleet artifact does not belong to the selected producer.")


def read_selected(path: Path, digest: str, limit: int = 2 * 1024 * 1024) -> bytes:
    with open_regular_file(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != digest:
        raise CoordinationError("A fleet input differs from its independently selected digest.")
    return raw


def admission_for(candidate: FleetCandidate, root: Path) -> dict:
    artifacts = candidate.artifacts
    admission = expected_document(root / "admission" / "receipt.json", artifacts["admission"]["sha256"])
    check_admission(admission)
    if (
        admission["source"] != candidate.source
        or any(admission[key] != candidate.producer[key] for key in ("run", "attempt", "caller", "preview"))
        or admission["planSha256"] != artifacts["plan"]["sha256"]
        or admission["inventorySha256"] != artifacts["inventory"]["sha256"]
        or admission["artifacts"]["plan"] != artifacts["plan"]["id"]
        or admission["artifacts"]["inventory"] != artifacts["inventory"]["id"]
    ):
        raise CoordinationError("The admission receipt describes another fleet candidate.")
    return admission


def supplied_groups(kind: str, environ=os.environ) -> tuple[str, ...] | None:
    """Read persistent group names from the environment secret for this kind.

    The site secret holds one name. The fleet secret holds two names for slots
    one and two, separated by one comma without spaces. If both secrets are
    present, their group names must differ. Errors never echo values.
    """
    raw = environ.get(GROUP_SECRETS[kind], "")
    if raw == "":
        return None
    values = tuple(raw.split(",")) if kind == "fleet" else (raw,)
    if (len(values) != (2 if kind == "fleet" else 1) or any(GROUP_NAME.fullmatch(value) is None for value in values)
            or len({value.casefold() for value in values}) != len(values)):
        raise CoordinationError(f"{GROUP_SECRETS[kind]} must name {'two distinct resource groups' if kind == 'fleet' else 'one resource group'}.")
    other = environ.get(GROUP_SECRETS["fleet" if kind == "site" else "site"], "")
    if other and {value.casefold() for value in values} & {
        value.casefold() for value in other.split(",")
    }:
        raise CoordinationError("The single Site group must differ from both fleet groups.")
    return values


def scope_for(candidate: FleetCandidate, root: Path, *, run: int, attempt: int, subscription: str,
              slot: str | None = None) -> FleetScope:
    """Select the fleet scope, or the single Site scope when a Site case slot is supplied.

    The group mode comes from the environment secret for that kind, so every job
    of one scenario derives the same private names and public binding.
    """
    admission = admission_for(candidate, root)
    kind = "site" if slot is not None else "fleet"
    return FleetScope(
        candidate.source["repository"], candidate.source["commit"],
        candidate.artifacts["admission"]["sha256"], admission["inventorySha256"],
        run, attempt, subscription, kind, (slot,) if slot is not None else SLOTS, supplied_groups(kind),
    )


def check_inputs(candidate: FleetCandidate, root: Path) -> dict:
    """Bind original qualification inputs to the final admitted publication inventory."""
    artifacts = candidate.artifacts
    admission = admission_for(candidate, root)
    plan = load_artifact_json(
        read_selected(root / "plan" / "plan.json", artifacts["plan"]["sha256"]),
        limit=2 * 1024 * 1024, label="Fleet release plan",
    )
    if (
        not isinstance(plan, dict) or plan.get("kind") != "ReleaseCandidate"
        or plan.get("active") is not True or plan.get("dryRun") is not candidate.producer["preview"]
        or plan.get("source") != candidate.source or not isinstance(plan.get("workspaces"), list)
        or sum(isinstance(row, dict) and row.get("id") == "azure.iot-operations"
               and row.get("workspace") == "workspaces/iot-operations" for row in plan["workspaces"]) != 1
    ):
        raise CoordinationError("Fleet qualification requires the declared IoT Operations workspace.")
    frozen = FrozenReleaseAssets.from_bytes(
        read_selected(root / "inventory" / "release-assets.json", artifacts["inventory"]["sha256"]),
    )
    selection = EngineSelection.read(root / "engine" / "workspace-engine.json", artifacts["engine"]["sha256"])
    workspaces = FrozenReleaseAssets.from_bytes(
        read_selected(root / "workspaces" / "release-assets.json", artifacts["workspaces"]["sha256"]),
    )
    native_assets, workspace_assets = publication_assets(plan, frozen)
    engine_assets = native_assets if plan["siteops"]["bundle"] else frozen.engine.assets
    def identities(values):
        return {item.name: item.document() for item in values}
    if (
        frozen.source != candidate.source or selection.candidate != candidate.source
        or selection.plan_sha256 != artifacts["plan"]["sha256"]
        or workspaces.source != candidate.source
        or identities(engine_assets) != identities(selection.native.assets)
        or identities(asset for asset in workspace_assets if asset.name not in ENGINE_REFERENCE_FILES) != identities(workspaces.assets)
        or (plan["siteops"]["bundle"] and (
            selection.native.source != candidate.source or selection.reference is not None
        ))
        or (not plan["siteops"]["bundle"] and (
            selection.reference is None or selection.reference.document() != frozen.engine.document()
        ))
    ):
        raise CoordinationError("The qualification inputs differ from the admitted frozen assets.")
    for directory, assets in (("engine", selection.native.assets), ("workspaces", workspaces.assets)):
        for asset in assets:
            if hash_file(root / directory / asset.name, limit=asset.size) != (asset.size, asset.sha256):
                raise CoordinationError("A qualification asset changed after candidate admission.")
    return admission


def jobs(pages: list, *, run: int, attempt: int, commit: str) -> list[dict]:
    if (not isinstance(pages, list) or not 1 <= len(pages) <= 16
            or any(not isinstance(page, dict) or not isinstance(page.get("jobs"), list) for page in pages)):
        raise CoordinationError("The workflow job inventory is invalid.")
    result = [job for page in pages for job in page["jobs"]]
    if (
        not result or len(result) > 1000
        or any(type(page.get("total_count")) is not int or page["total_count"] != len(result) for page in pages)
        or any(not isinstance(job, dict) or type(job.get("id")) is not int
               or type(job.get("run_id")) is not int or job["run_id"] != run
               or type(job.get("run_attempt")) is not int or job["run_attempt"] != attempt
               or job.get("head_sha") != commit for job in result)
        or len({job["id"] for job in result}) != len(result)
    ):
        raise CoordinationError("The workflow job inventory is incomplete or belongs to another invocation.")
    return result


def run_jobs(pages: list, *, run: int, attempt: int, commit: str) -> list[dict]:
    """Validate every recorded execution of this run's jobs up to the current attempt."""
    if (not isinstance(pages, list) or not 1 <= len(pages) <= 32
            or any(not isinstance(page, dict) or not isinstance(page.get("jobs"), list) for page in pages)):
        raise CoordinationError("The workflow job inventory is invalid.")
    result = [job for page in pages for job in page["jobs"]]
    if (
        not result or len(result) > 3000
        or any(type(page.get("total_count")) is not int or page["total_count"] != len(result) for page in pages)
        or any(not isinstance(job, dict) or type(job.get("id")) is not int
               or type(job.get("run_id")) is not int or job["run_id"] != run
               or type(job.get("run_attempt")) is not int or not 0 < job["run_attempt"] <= attempt
               or job.get("head_sha") != commit or not isinstance(job.get("name"), str)
               for job in result)
        or len({job["id"] for job in result}) != len(result)
    ):
        raise CoordinationError("The workflow job inventory is incomplete or belongs to another invocation.")
    return result


def named_job(values: list[dict], name: str) -> dict | None:
    selected = [job for job in values if isinstance(job.get("name"), str)
                and job["name"].split(" / ")[-1] == name]
    if len(selected) > 1:
        raise CoordinationError("The workflow contains ambiguous fleet jobs.")
    return selected[0] if selected else None


def hosts_ready(values: list[dict], marker_name="Host ready") -> bool:
    ready = []
    for slot in SLOTS:
        job = named_job(values, f"Fleet host ({slot})")
        if job is None or job.get("status") in {"queued", "waiting", "pending"}:
            ready.append(False)
            continue
        if job.get("status") != "in_progress" or job.get("conclusion") is not None:
            raise CoordinationError("A fleet host exited before controller acceptance.")
        steps = job.get("steps")
        if not isinstance(steps, list):
            raise CoordinationError("Fleet host readiness metadata is unavailable.")
        markers = [step for step in steps if isinstance(step, dict) and step.get("name") == marker_name]
        if len(markers) > 1:
            raise CoordinationError("Fleet host readiness is ambiguous.")
        marker = markers[0] if markers else None
        if marker and marker.get("status") == "completed" and marker.get("conclusion") != "success":
            raise CoordinationError("A fleet host did not become ready.")
        ready.append(bool(marker and marker.get("status") == "completed" and marker.get("conclusion") == "success"))
    return all(ready)


def participants_started(values: list[dict]) -> bool:
    """Do not provision clusters when the controller cannot acquire its runner."""
    started = []
    for name in ("Fleet host (one)", "Fleet host (two)", "Fleet controller"):
        job = named_job(values, name)
        if job is None or job.get("status") in {"queued", "waiting", "pending"}:
            started.append(False)
        elif job.get("status") == "in_progress" and job.get("conclusion") is None:
            started.append(True)
        else:
            raise CoordinationError("A fleet participant exited before startup completed.")
    return all(started)


def wait_for_job_state(read_jobs, *, mode: str, timeout: float, interval: float = 15,
                       clock=time.monotonic, sleep=time.sleep) -> None:
    if (mode not in {"participants", "ready", "deployed", "observed", "cleanup"}
            or not math.isfinite(timeout) or not 0 < interval or not 0 < timeout <= 21600):
        raise CoordinationError("The fleet coordination deadline is invalid.")
    deadline = clock() + timeout
    next_notice = clock()
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise CoordinationError(
                "Fleet startup needs capacity for two hosts and one controller."
                if mode == "participants" else "Fleet coordination exceeded its bounded deadline."
            )
        values = read_jobs(min(60, remaining))
        if clock() >= deadline:
            raise CoordinationError("Workflow metadata exceeded the fleet coordination deadline.")
        if mode == "participants":
            if participants_started(values):
                return
        elif mode in {"ready", "observed"}:
            if hosts_ready(values, "Host ready" if mode == "ready" else "Observe host readiness"):
                return
        elif mode == "deployed":
            controller = named_job(values, "Fleet controller")
            if controller:
                if controller.get("status") == "completed" and controller.get("conclusion") != "success":
                    raise CoordinationError("The fleet controller exited before successful deployment.")
                steps = controller.get("steps", [])
                matches = [step for step in steps if isinstance(step, dict) and step.get("name") == "Deploy fleet"]
                if len(matches) > 1:
                    raise CoordinationError("Fleet deployment metadata is ambiguous.")
                if matches and matches[0].get("status") == "completed":
                    if matches[0].get("conclusion") != "success":
                        raise CoordinationError("Fleet deployment did not succeed.")
                    return
        else:
            cleanup = named_job(values, "Fleet cleanup")
            if cleanup and cleanup.get("status") == "completed":
                if cleanup.get("conclusion") != "success":
                    raise CoordinationError("Fleet cleanup did not complete successfully.")
                return
        remaining = deadline - clock()
        if remaining <= 0:
            raise CoordinationError("Fleet coordination exceeded its bounded deadline.")
        if clock() >= next_notice:
            print(f"Waiting for fleet {mode}.", flush=True)
            next_notice = clock() + 60
        sleep(min(interval, remaining))

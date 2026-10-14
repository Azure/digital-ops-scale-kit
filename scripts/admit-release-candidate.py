# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Admit frozen producer outputs before release acceptance consumes their bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from release_verification import ReleaseVerifier  # noqa: E402
from siteops_release_assets import (  # noqa: E402
    ENGINE_REFERENCE_NAME,
    PROOF_SUFFIX,
    FrozenReleaseAssets,
    publication_assets,
)

from siteops.artifacts import (  # noqa: E402
    hash_file,
    load_artifact_json,
    open_regular_file,
    require_node,
)
from siteops.cache_filesystem import make_private_directory  # noqa: E402
from siteops.workspace_source import ArtifactIdentity  # noqa: E402

MAX_DOCUMENT = 2 * 1024 * 1024
ARTIFACT_ROLES = {"plan": "release-plan", "inventory": "release-assets", "payload": "release-payload"}


class AdmissionError(ValueError):
    """A fixed release-input diagnostic safe for workflow output."""


@dataclass(frozen=True)
class Expected:
    repository: str
    commit: str
    ref: str
    run: int
    attempt: int
    caller: str
    preview: bool
    artifacts: dict[str, int]
    plan_sha256: str
    inventory_sha256: str

    def __post_init__(self):
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository)
            or not re.fullmatch(r"[0-9a-f]{40}", self.commit)
            or not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", self.ref)
            or ".." in self.ref
            or type(self.preview) is not bool
            or self.caller != (
                ".github/workflows/ci.yaml" if self.preview else ".github/workflows/release.yaml"
            )
            or (not self.preview and self.ref != "refs/heads/main")
            or set(self.artifacts) != set(ARTIFACT_ROLES)
            or any(type(value) is not int or not 0 < value < 10**20
                   for value in (self.run, self.attempt, *self.artifacts.values()))
            or len(set(self.artifacts.values())) != len(ARTIFACT_ROLES)
            or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (self.plan_sha256, self.inventory_sha256))
        ):
            raise AdmissionError("The independent candidate selection is incomplete or unsupported.")

    @property
    def source(self) -> dict[str, str]:
        return {"repository": self.repository, "commit": self.commit, "ref": self.ref}

    @classmethod
    def from_environment(cls) -> Expected:
        mode = os.environ["CANDIDATE_PREVIEW"]
        if mode not in {"true", "false"}:
            raise AdmissionError("Select an explicit candidate preview or publication context.")
        return cls(
            os.environ["CANDIDATE_REPOSITORY"], os.environ["CANDIDATE_COMMIT"],
            os.environ["CANDIDATE_REF"], int(os.environ["CANDIDATE_RUN"]),
            int(os.environ["CANDIDATE_ATTEMPT"]), os.environ["CANDIDATE_CALLER"], mode == "true",
            {role: int(os.environ[f"CANDIDATE_{role.upper()}_ARTIFACT"]) for role in ARTIFACT_ROLES},
            os.environ["CANDIDATE_PLAN_SHA"], os.environ["CANDIDATE_INVENTORY_SHA"],
        )


def read(path: Path, limit: int = MAX_DOCUMENT) -> bytes:
    with open_regular_file(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise AdmissionError("A candidate input exceeds its byte limit.")
    return raw


def document(raw: bytes, limit: int = MAX_DOCUMENT):
    return load_artifact_json(raw, limit=limit, label="Candidate admission input")


def matches(row, fields: dict) -> bool:
    return isinstance(row, dict) and all(
        type(row.get(key)) is type(value) and row[key] == value for key, value in fields.items()
    )


def check_producer(expected: Expected, run: dict, job_pages: list, artifacts: dict) -> None:
    """Require the completed producer, not completion of its still-running caller."""
    if (
        not matches(run, {
            "id": expected.run, "run_attempt": expected.attempt, "head_sha": expected.commit,
            "head_branch": expected.ref.removeprefix("refs/heads/"), "path": expected.caller,
        })
        or not matches(run.get("repository"), {"full_name": expected.repository})
        or run.get("event") not in {"push", "workflow_dispatch"}
    ):
        raise AdmissionError("The producer run does not match the selected candidate.")
    if (not isinstance(job_pages, list) or not 1 <= len(job_pages) <= 16
            or any(not isinstance(page, dict) or not isinstance(page.get("jobs"), list)
                   or len(page["jobs"]) > 100 for page in job_pages)):
        raise AdmissionError("The producer job inventory is invalid.")
    jobs = [job for page in job_pages for job in page["jobs"]]
    if (
        not jobs or len(jobs) > 1000 or any(not isinstance(job, dict) for job in jobs)
        or any(type(page.get("total_count")) is not int or page["total_count"] != len(jobs)
               for page in job_pages)
        or any(type(job.get("id")) is not int for job in jobs)
        or len({job["id"] for job in jobs}) != len(jobs)
    ):
        raise AdmissionError("The producer job inventory is incomplete or ambiguous.")
    selected = [job for job in jobs if isinstance(job.get("name"), str)
                and job["name"].split(" / ")[-1] == "Assemble release"]
    if len(selected) != 1 or not matches(
        selected[0], {
            "run_id": expected.run, "run_attempt": expected.attempt, "head_sha": expected.commit,
            "status": "completed", "conclusion": "success",
        },
    ):
        raise AdmissionError("The selected producer has not completed successfully.")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_ROLES):
        raise AdmissionError("The selected producer artifact inventory is incomplete.")
    for role, prefix in ARTIFACT_ROLES.items():
        artifact = artifacts[role]
        if (
            not matches(artifact, {
                "id": expected.artifacts[role], "name": f"{prefix}-{expected.run}-{expected.attempt}",
                "expired": False,
            })
            or type(artifact.get("size_in_bytes")) is not int or artifact["size_in_bytes"] <= 0
            or not isinstance(artifact.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])
            or not matches(artifact.get("workflow_run"), {"id": expected.run, "head_sha": expected.commit})
        ):
            raise AdmissionError("An artifact does not belong to the selected producer attempt.")


def admit(expected: Expected, plan_raw: bytes, inventory_raw: bytes, payload: Path, verify) -> dict:
    """Check the complete inventory and every executable/content subject's provenance."""
    if (
        hashlib.sha256(plan_raw).hexdigest() != expected.plan_sha256
        or hashlib.sha256(inventory_raw).hexdigest() != expected.inventory_sha256
    ):
        raise AdmissionError("The release inputs differ from the independently selected digests.")
    plan = document(plan_raw)
    if (
        not isinstance(plan, dict) or plan.get("apiVersion") != "siteops.release/v1"
        or plan.get("kind") != "ReleaseCandidate" or plan.get("active") is not True
        or plan.get("dryRun") is not expected.preview or plan.get("source") != expected.source
    ):
        raise AdmissionError("The release plan does not match the selected candidate context.")
    inventory = FrozenReleaseAssets.from_bytes(inventory_raw)
    native, workspaces = publication_assets(plan, inventory)
    require_node(payload, directory=True)
    if not inventory.assets or {path.name for path in payload.iterdir()} != {
        asset.name for asset in inventory.assets
    }:
        raise AdmissionError("The candidate payload differs from the complete frozen inventory.")
    for asset in inventory.assets:
        if hash_file(payload / asset.name, limit=asset.size) != (asset.size, asset.sha256):
            raise AdmissionError("A candidate payload asset differs from its frozen bytes.")
    counts = {}
    for role, assets in (("engine", native), ("workspace", workspaces)):
        by_name = {asset.name: asset for asset in assets}
        subjects = [asset for asset in assets if not asset.name.endswith(PROOF_SUFFIX)
                    and asset.name != "siteops-workspaces.json"]
        for subject in subjects:
            proof = by_name.get(subject.name + PROOF_SUFFIX)
            if proof is None:
                raise AdmissionError("A candidate subject has no independently selected proof.")
            verify("engine-reference" if subject.name == ENGINE_REFERENCE_NAME else role,
                   payload / subject.name, payload / proof.name,
                   ArtifactIdentity(subject.name, subject.size, subject.sha256))
        counts[role] = len(subjects)
    return {
        "apiVersion": "siteops.release.acceptance/v1",
        "kind": "CandidateInputAdmission",
        "source": expected.source, "run": expected.run, "attempt": expected.attempt,
        "caller": expected.caller, "preview": expected.preview,
        "artifacts": expected.artifacts, "planSha256": expected.plan_sha256,
        "inventorySha256": expected.inventory_sha256, "subjects": counts,
        "status": "admitted", "installation": "not-run", "deployment": "not-run",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "inventory", "payload", "run", "jobs", "artifact-metadata",
                 "trusted-root", "state", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        expected = Expected.from_environment()
        if args.output.exists() or args.state.exists():
            raise AdmissionError("Candidate admission needs new state and receipt paths.")
        payload = args.payload.resolve()
        if (args.state.resolve().is_relative_to(payload)
                or payload.is_relative_to(args.state.resolve())
                or args.trusted_root.resolve().is_relative_to(payload)):
            raise AdmissionError("Keep admission state and independent roots separate from the payload.")
        run = document(read(args.run))
        jobs = document(read(args.jobs, 8 * MAX_DOCUMENT), 8 * MAX_DOCUMENT)
        artifacts = {role: document(read(args.artifact_metadata / f"{role}.json"))
                     for role in ARTIFACT_ROLES}
        check_producer(expected, run, jobs, artifacts)
        make_private_directory(args.state)
        verifiers = {}

        def verify(role, artifact, proof, identity):
            if role not in verifiers:
                verifiers[role] = ReleaseVerifier(
                    args.state / role, args.trusted_root, expected.source,
                    signer=(
                        ".github/workflows/_release-candidate.yaml" if role == "engine-reference"
                        else f".github/workflows/_{'siteops' if role == 'engine' else 'workspace'}-distribution.yaml"
                    ),
                    builder=expected.caller, runner_environment="self-hosted",
                )
            verifiers[role](artifact, proof, identity)

        receipt = admit(expected, read(args.plan), read(args.inventory), args.payload, verify)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, AdmissionError) else "Candidate bytes or provenance failed admission."
        print(f"admit-release-candidate: {message}", file=sys.stderr)
        return 1
    print("The selected producer inputs were admitted. Installation and deployment were not run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

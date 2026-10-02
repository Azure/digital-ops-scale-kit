# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Ownership and cleanup for the two-slot release qualification fleet."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from siteops.arm_resources import ArmResourceError
from siteops.arm_resources_azure_cli import _run_az
from siteops.artifacts import load_artifact_json
from siteops.cache_filesystem import check_cache_ancestors, make_private_directory

SLOTS = ("one", "two")
VERSION = "siteops.release.fleet/v1"
MAX_DOCUMENT = 64 * 1024


class FleetError(ValueError):
    """A fixed failure category without provider diagnostics or target identities."""

    def __init__(self, reason: str, *, retryable: bool = False):
        if reason not in {
            "invalid-scope", "invalid-slot", "invalid-location", "provider-process-failed", "provider-timeout",
            "provider-request-failed", "invalid-existence-response", "invalid-group-response",
            "preexisting-group", "ownership-context-mismatch", "ownership-not-established",
            "ownership-mismatch", "invalid-cleanup-bound", "receipt-digest-mismatch",
            "invalid-receipt", "select-new-output-and-log-paths", "candidate-not-admitted",
            "private-diagnostics-failed",
        }:
            raise ValueError("Unsupported fleet failure category.")
        self.retryable = retryable
        super().__init__(reason)


@dataclass(frozen=True)
class FleetScope:
    repository: str
    source_commit: str
    admission_sha256: str
    inventory_sha256: str
    run: int
    attempt: int
    subscription: str

    def __post_init__(self):
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository)
            or not re.fullmatch(r"[0-9a-f]{40}", self.source_commit)
            or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (self.admission_sha256, self.inventory_sha256))
            or any(type(value) is not int or not 0 < value < 10**20 for value in (self.run, self.attempt))
            or not re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", self.subscription,
            )
        ):
            raise FleetError("invalid-scope")

    def context(self) -> dict:
        return {
            "repository": self.repository, "sourceCommit": self.source_commit,
            "admissionSha256": self.admission_sha256, "inventorySha256": self.inventory_sha256,
            "run": self.run, "attempt": self.attempt,
        }

    def _key(self, purpose: str) -> str:
        raw = json.dumps([purpose, self.context(), self.subscription.casefold()],
                         sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    @property
    def key(self) -> str:
        # The public binding must not reveal the separately derived Azure names.
        return self._key("binding")

    def group(self, slot: str) -> str:
        if slot not in SLOTS:
            raise FleetError("invalid-slot")
        return f"rg-siteops-fleet-{self._key('resource-name')[:24]}-{slot}"

    def cluster(self, slot: str) -> str:
        if slot not in SLOTS:
            raise FleetError("invalid-slot")
        return f"arc-siteops-fleet-{self._key('resource-name')[:24]}-{slot}"

    def tags(self, slot: str) -> dict[str, str]:
        self.group(slot)
        return {
            "managedBy": "siteops-fleet-acceptance", "qualificationScope": self.key,
            "qualificationSlot": slot, "runId": str(self.run), "runAttempt": str(self.attempt),
        }

    def owns(self, slot: str, observed: object) -> bool:
        group = self.group(slot)
        expected_id = f"/subscriptions/{self.subscription}/resourceGroups/{group}".casefold()
        return (
            isinstance(observed, dict) and isinstance(observed.get("id"), str)
            and observed["id"].casefold() == expected_id
            and isinstance(observed.get("name"), str) and observed["name"].casefold() == group.casefold()
            and isinstance(observed.get("tags"), dict)
            and all(observed["tags"].get(key) == value for key, value in self.tags(slot).items())
        )


class AzureGroups:
    """Use only bounded group operations in the explicitly selected subscription."""

    def __init__(
        self, scope: FleetScope, private_logs: Path, *,
        runner: Callable[[list[str]], tuple[int, bytes, bytes]] = _run_az,
    ) -> None:
        self.scope = scope
        self.logs = private_logs
        check_cache_ancestors(self.logs)
        make_private_directory(self.logs)
        self.runner = runner
        self.counter = 0

    def _call(self, operation: str, slot: str, extra: list[str], *, output: str) -> bytes:
        self.counter += 1
        try:
            code, stdout, stderr = self.runner([
                "az", "group", operation, "--subscription", self.scope.subscription,
                "--name", self.scope.group(slot), "--only-show-errors", "-o", output, *extra,
            ])
        except ArmResourceError as error:
            if error.code == "TIMEOUT":
                raise FleetError("provider-timeout", retryable=True) from None
            raise FleetError("provider-process-failed") from None
        try:
            for stream, data in (("stdout", stdout), ("stderr", stderr)):
                path = self.logs / f"{self.counter}-{slot}-{operation}.{stream}"
                with path.open("xb") as target:
                    target.write(data)
                path.chmod(0o600)
        except OSError:
            raise FleetError("private-diagnostics-failed") from None
        if code:
            raise FleetError("provider-request-failed")
        return stdout

    def exists(self, slot: str) -> bool:
        raw = self._call("exists", slot, [], output="json")
        try:
            result = load_artifact_json(raw, limit=32, label="Group existence")
        except ValueError:
            raise FleetError("invalid-existence-response") from None
        if type(result) is not bool:
            raise FleetError("invalid-existence-response")
        return result

    def show(self, slot: str) -> dict:
        raw = self._call("show", slot, [], output="json")
        try:
            result = load_artifact_json(raw, limit=MAX_DOCUMENT, label="Group observation")
        except ValueError:
            raise FleetError("invalid-group-response") from None
        if not isinstance(result, dict):
            raise FleetError("invalid-group-response")
        return result

    def create(self, slot: str, location: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9]{1,63}", location):
            raise FleetError("invalid-location")
        self._call("create", slot, [
            "--location", location, "--tags",
            *(f"{key}={value}" for key, value in self.scope.tags(slot).items()),
        ], output="none")

    def delete(self, slot: str) -> None:
        self._call("delete", slot, ["--yes", "--no-wait"], output="none")


def preflight(scope: FleetScope, groups: AzureGroups) -> dict:
    """Record absence for both slots before any creation is authorized."""
    for slot in SLOTS:
        if groups.exists(slot):
            raise FleetError("preexisting-group")
    return {
        "apiVersion": VERSION, "kind": "FleetOwnership",
        "context": scope.context(), "scopeKey": scope.key,
        "slots": {slot: {"admittedAbsent": True} for slot in SLOTS},
    }


def validate_ownership(scope: FleetScope, lease: object) -> dict:
    """An ownership receipt is meaningful only with its independent expected digest."""
    expected = scope.context()
    context = lease.get("context") if isinstance(lease, dict) else None
    if (
        not isinstance(lease, dict)
        or set(lease) != {"apiVersion", "kind", "context", "scopeKey", "slots"}
        or lease["apiVersion"] != VERSION or lease["kind"] != "FleetOwnership"
        or not isinstance(context, dict) or set(context) != set(expected)
        or any(type(context[key]) is not type(value) or context[key] != value
               for key, value in expected.items())
        or lease["scopeKey"] != scope.key
        or not isinstance(lease["slots"], dict) or set(lease["slots"]) != set(SLOTS)
        or any(not isinstance(row, dict) or set(row) != {"admittedAbsent"}
               or type(row["admittedAbsent"]) is not bool for row in lease["slots"].values())
    ):
        raise FleetError("ownership-context-mismatch")
    return lease["slots"]


def create(scope: FleetScope, lease: dict, groups: AzureGroups, location: str) -> None:
    """Create only slots admitted absent, refusing an intervening existing group."""
    if not re.fullmatch(r"[a-z][a-z0-9]{1,63}", location):
        raise FleetError("invalid-location")
    slots = validate_ownership(scope, lease)
    if not all(slots[slot]["admittedAbsent"] for slot in SLOTS):
        raise FleetError("ownership-not-established")
    for slot in SLOTS:
        if groups.exists(slot):
            raise FleetError("preexisting-group")
        groups.create(slot, location)
        if not scope.owns(slot, groups.show(slot)):
            raise FleetError("ownership-mismatch")


def cleanup(
    scope: FleetScope, lease: dict, groups: AzureGroups, *, operation_exit: int = 0,
    timeout: float = 1200, interval: float = 15, clock=time.monotonic, sleep=time.sleep,
) -> tuple[int, dict]:
    """Request owned deletions once, poll both slots, and preserve the operation exit."""
    slots = validate_ownership(scope, lease)
    if (
        type(operation_exit) is not int or not 0 <= operation_exit <= 255
        or type(timeout) not in {int, float} or type(interval) not in {int, float}
        or not 0 < timeout <= 3600 or not 0 < interval <= timeout
    ):
        raise FleetError("invalid-cleanup-bound")
    pending = {}
    results = {}
    for slot in SLOTS:
        if slots[slot]["admittedAbsent"]:
            pending[slot] = "inspect"
        else:
            results[slot] = {"state": "not-attempted", "reason": "ownership-not-established"}
    deadline = clock() + timeout
    while pending:
        requested = False
        for slot in tuple(pending):
            try:
                if not groups.exists(slot):
                    results[slot] = {"state": "absent", "reason": "confirmed-absent"}
                    del pending[slot]
                    continue
                if pending[slot] == "inspect":
                    if not scope.owns(slot, groups.show(slot)):
                        results[slot] = {"state": "not-attempted", "reason": "ownership-mismatch"}
                        del pending[slot]
                        continue
                    # A failed response can follow an accepted delete. Never resubmit blindly.
                    pending[slot] = "deleting"
                    requested = True
                    groups.delete(slot)
                results[slot] = {"state": "residual", "reason": "deletion-pending"}
            except FleetError as error:
                results[slot] = {"state": "unknown", "reason": str(error)}
                if not error.retryable:
                    del pending[slot]
        if not pending:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        if not requested:
            sleep(min(interval, remaining))
    complete = all(result["state"] == "absent" for result in results.values())
    receipt = {
        "apiVersion": VERSION, "kind": "FleetCleanup",
        "context": scope.context(), "scopeKey": scope.key, "slots": results,
        "status": "complete" if complete else "incomplete", "operationExit": operation_exit,
    }
    return operation_exit or (0 if complete else 1), receipt

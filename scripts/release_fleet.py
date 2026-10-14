# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Ownership and cleanup for release qualification fleet and single Site groups.

Ephemeral groups are created, fenced by an immutable marker and deleted by the
run. Persistent groups are supplied by the environment. The run never creates
or deletes them and removes only the resources missing from the snapshot it
committed before any write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from siteops.arm_resources import ArmResourceError
from siteops.arm_resources_azure_cli import _run_az
from siteops.artifacts import load_artifact_json, open_regular_file
from siteops.cache_filesystem import check_cache_ancestors, make_private_directory

SLOTS = ("one", "two")
SITE_SLOTS = ("disabled", "enabled", "existing")
VAULT_PURGE = ("purged", "not-applicable", "not-attempted", "failed")
GROUP_MODES = ("ephemeral", "persistent")
VERSION = "siteops.release.fleet/v1"
MAX_DOCUMENT = 256 * 1024
MAX_SNAPSHOT = 1000
VAULT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]")
GROUP_NAME = re.compile(r"[A-Za-z0-9._()-]{0,89}[A-Za-z0-9_()-]")
LOCATION = re.compile(r"[a-z][a-z0-9]{1,63}")
CLUSTER_TYPE = "microsoft.kubernetes/connectedclusters"
INSTANCE_TYPE = "microsoft.iotoperations/instances"
VAULT_TYPE = "microsoft.keyvault/vaults"
CREATION_CLOCK_TOLERANCE = timedelta(minutes=2)
CREATION_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})"
)
DEPLOYMENT_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")


class FleetError(ValueError):
    """A fixed failure category without provider diagnostics or target identities."""

    def __init__(self, reason: str, *, retryable: bool = False):
        if reason not in {
            "invalid-scope", "invalid-slot", "invalid-location", "provider-process-failed", "provider-timeout",
            "provider-request-failed", "invalid-existence-response", "invalid-group-response",
            "preexisting-group", "ownership-context-mismatch", "ownership-not-established",
            "ownership-mismatch", "invalid-cleanup-bound", "receipt-digest-mismatch",
            "invalid-receipt", "select-new-output-and-log-paths", "candidate-not-admitted",
            "private-diagnostics-failed", "invalid-allocation", "allocation-context-mismatch",
            "persistent-group-missing", "persistent-group-occupied", "invalid-resource-response",
            "invalid-deployment-response",
        }:
            raise ValueError("Unsupported fleet failure category.")
        self.retryable = retryable
        super().__init__(reason)


def expected_document(path: Path, expected_sha: str) -> dict:
    with open_regular_file(path) as stream:
        raw = stream.read(MAX_DOCUMENT + 1)
    if len(raw) > MAX_DOCUMENT or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise FleetError("receipt-digest-mismatch")
    result = load_artifact_json(raw, limit=MAX_DOCUMENT, label="Fleet receipt")
    if not isinstance(result, dict):
        raise FleetError("invalid-receipt")
    return result


def owner_hash(owner: object) -> str:
    if not isinstance(owner, str) or re.fullmatch(r"siteops-fleet-[0-9a-f]{64}", owner) is None:
        raise FleetError("invalid-allocation")
    return hashlib.sha256(owner.encode("ascii")).hexdigest()


def allocation_hashes(owners: object, slots: tuple[str, ...] = SLOTS) -> dict[str, str]:
    if not isinstance(owners, dict) or set(owners) != set(slots):
        raise FleetError("invalid-allocation")
    hashes = {slot: owner_hash(value) for slot, value in owners.items()}
    if len(set(hashes.values())) != len(slots):
        raise FleetError("invalid-allocation")
    return hashes


def check_admission(admission: dict) -> None:
    source = admission.get("source")
    artifacts = admission.get("artifacts")
    subjects = admission.get("subjects")
    if (
        set(admission) != {
            "apiVersion", "kind", "source", "run", "attempt", "caller", "preview", "artifacts",
            "planSha256", "inventorySha256", "subjects", "status", "installation", "deployment",
        }
        or admission["apiVersion"] != "siteops.release.acceptance/v1"
        or admission["kind"] != "CandidateInputAdmission" or admission["status"] != "admitted"
        or admission["installation"] != "not-run" or admission["deployment"] != "not-run"
        or not isinstance(source, dict) or set(source) != {"repository", "commit", "ref"}
        or type(admission["preview"]) is not bool
        or admission["caller"] != (
            ".github/workflows/ci.yaml" if admission["preview"] else ".github/workflows/release.yaml"
        )
        or any(type(admission[key]) is not int or admission[key] <= 0 for key in ("run", "attempt"))
        or not isinstance(artifacts, dict) or set(artifacts) != {"plan", "inventory", "payload"}
        or any(type(value) is not int or value <= 0 for value in artifacts.values())
        or not isinstance(subjects, dict) or set(subjects) != {"engine", "workspace"}
        or any(type(value) is not int or value < 0 for value in subjects.values())
        or any(not isinstance(admission[key], str)
               or not re.fullmatch("[0-9a-f]{64}", admission[key])
               for key in ("planSha256", "inventorySha256"))
    ):
        raise FleetError("candidate-not-admitted")


@dataclass(frozen=True)
class FleetScope:
    """Private naming and public binding for one run attempt.

    The fleet kind uses both fixed slots. The site kind uses exactly one single
    Site slot, so a matrix cell can be retried and cleaned up alone. Supplied
    group names select persistent groups, one per slot. Cluster and vault names
    are always bound to the run attempt.
    """

    repository: str
    source_commit: str
    admission_sha256: str
    inventory_sha256: str
    run: int
    attempt: int
    subscription: str
    kind: str = "fleet"
    slots: tuple[str, ...] = SLOTS
    groups: tuple[str, ...] | None = None

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
            or not (
                (self.kind == "fleet" and self.slots == SLOTS)
                or (self.kind == "site" and type(self.slots) is tuple
                    and len(self.slots) == 1 and self.slots[0] in SITE_SLOTS)
            )
            or (self.groups is not None and (
                type(self.groups) is not tuple or len(self.groups) != len(self.slots)
                or any(type(name) is not str or GROUP_NAME.fullmatch(name) is None for name in self.groups)
                or len({name.casefold() for name in self.groups}) != len(self.groups)
            ))
        ):
            raise FleetError("invalid-scope")

    @property
    def mode(self) -> str:
        return "persistent" if self.groups else "ephemeral"

    def context(self) -> dict:
        return {
            "repository": self.repository, "sourceCommit": self.source_commit,
            "admissionSha256": self.admission_sha256, "inventorySha256": self.inventory_sha256,
            "run": self.run, "attempt": self.attempt,
        }

    def _key(self, purpose: str) -> str:
        values = [purpose, self.context(), self.subscription.casefold()]
        if self.groups:
            values.append([name.casefold() for name in self.groups])
        raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    @property
    def key(self) -> str:
        # The public binding must not reveal the separately derived or supplied Azure names.
        return self._key("binding")

    def _name(self, prefix: str, slot: str) -> str:
        if slot not in self.slots:
            raise FleetError("invalid-slot")
        return f"{prefix}-siteops-{self.kind}-{self._key('resource-name')[:24]}-{slot}"

    def group(self, slot: str) -> str:
        if self.groups and slot in self.slots:
            return self.groups[self.slots.index(slot)]
        return self._name("rg", slot)

    def cluster(self, slot: str) -> str:
        return self._name("arc", slot)

    def vault(self, slot: str) -> str:
        """A private vault name for the single Site case that supplies an existing vault."""
        if self.kind != "site" or slot != "existing" or slot not in self.slots:
            raise FleetError("invalid-slot")
        return "kv" + self._key("vault-name")[:22]

    def bound_names(self, slot: str) -> set[str]:
        names = {self.cluster(slot)}
        if self.kind == "site" and slot == "existing":
            names.add(self.vault(slot))
        return {name.casefold() for name in names}

    def tags(self, slot: str) -> dict[str, str]:
        self.group(slot)
        return {
            "managedBy": f"siteops-{self.kind}-acceptance", "qualificationScope": self.key,
            "qualificationSlot": slot, "runId": str(self.run), "runAttempt": str(self.attempt),
        }

    def group_id(self, slot: str) -> str:
        return f"/subscriptions/{self.subscription}/resourceGroups/{self.group(slot)}"

    def snapshot_digest(self, resource_id: str) -> str:
        """A keyed digest that commits to a resource without publishing its identity."""
        return hmac.new(bytes.fromhex(self._key("snapshot")), resource_id.rstrip("/").casefold().encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def snapshot_commitment(self, slot: str, snapshot: list[str]) -> str:
        raw = json.dumps([self.key, slot, snapshot], separators=(",", ":")).encode("ascii")
        return hashlib.sha256(raw).hexdigest()

    def owns(self, slot: str, observed: object, expected_owner_sha: str) -> bool:
        """Require the selected group: owned by this attempt when ephemeral, the supplied group when persistent."""
        group = self.group(slot)
        if not (
            isinstance(observed, dict) and isinstance(observed.get("id"), str)
            and observed["id"].casefold() == self.group_id(slot).casefold()
            and isinstance(observed.get("name"), str) and observed["name"].casefold() == group.casefold()
        ):
            return False
        if self.groups:
            return True
        try:
            observed_owner_sha = owner_hash(observed.get("managedBy"))
        except FleetError:
            return False
        return (
            observed_owner_sha == expected_owner_sha and isinstance(observed.get("tags"), dict)
            and all(observed["tags"].get(key) == value for key, value in self.tags(slot).items())
        )


class AzureGroups:
    """Use only bounded group and resource operations in the explicitly selected subscription."""

    def __init__(
        self, scope: FleetScope, private_logs: Path, *,
        runner: Callable[[list[str]], tuple[int, bytes, bytes]] = _run_az,
        owners: dict[str, str] | None = None,
    ) -> None:
        self.scope = scope
        self.logs = private_logs
        check_cache_ancestors(self.logs)
        make_private_directory(self.logs)
        self.runner = runner
        self.owners = owners
        self.counter = 0

    def _call(self, operation: str, slot: str, extra: list[str], *, output: str) -> bytes:
        return self._run(operation, slot, [
            "group", operation, "--subscription", self.scope.subscription,
            "--name", self.scope.group(slot), "--only-show-errors", "-o", output, *extra,
        ])

    def _run(self, label: str, slot: str, arguments: list[str]) -> bytes:
        self.counter += 1
        try:
            code, stdout, stderr = self.runner(["az", *arguments])
        except ArmResourceError as error:
            if error.code == "TIMEOUT":
                raise FleetError("provider-timeout", retryable=True) from None
            raise FleetError("provider-process-failed") from None
        try:
            for stream, data in (("stdout", stdout), ("stderr", stderr)):
                path = self.logs / f"{self.counter}-{slot}-{label}.{stream}"
                with path.open("xb") as target:
                    target.write(data)
                path.chmod(0o600)
        except OSError:
            raise FleetError("private-diagnostics-failed") from None
        if code:
            raise FleetError("provider-request-failed")
        return stdout

    def _json(self, label: str, slot: str, arguments: list[str], error: str = "invalid-group-response") -> object:
        raw = self._run(label, slot, [
            *arguments, "--subscription", self.scope.subscription, "--only-show-errors", "-o", "json",
        ])
        try:
            return load_artifact_json(raw, limit=8 * 1024 * 1024, label="Azure observation")
        except ValueError:
            raise FleetError(error) from None

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
        if not LOCATION.fullmatch(location):
            raise FleetError("invalid-location")
        if self.scope.groups:
            raise FleetError("invalid-scope")
        owners = self.owners
        if owners is None:
            raise FleetError("invalid-allocation")
        allocation_hashes(owners, self.scope.slots)
        self._call("create", slot, [
            "--location", location, "--managed-by", owners[slot], "--tags",
            *(f"{key}={value}" for key, value in self.scope.tags(slot).items()),
        ], output="none")

    def delete(self, slot: str) -> None:
        if self.scope.groups:
            raise FleetError("invalid-scope")
        self._call("delete", slot, ["--yes", "--no-wait"], output="none")

    def resources(self, slot: str) -> list[dict]:
        """Every resource in the selected group, limited to identities inside that group."""
        values = self._json("resource-list", slot, [
            "resource", "list", "--resource-group", self.scope.group(slot),
            "--query", "[].{id:id,type:type,name:name,createdTime:createdTime}",
        ], "invalid-resource-response")
        prefix = (self.scope.group_id(slot) + "/providers/").casefold()
        if not isinstance(values, list) or len(values) > MAX_SNAPSHOT or any(
            not isinstance(item, dict) or set(item) != {"id", "type", "name", "createdTime"}
            or any(not isinstance(item[key], str) or not item[key] for key in ("id", "type", "name"))
            or item["createdTime"] is not None and not isinstance(item["createdTime"], str)
            or not item["id"].casefold().startswith(prefix)
            or re.search(r"[\s?#]|/\.\.?(?:/|$)", item["id"]) is not None
            for item in values
        ):
            raise FleetError("invalid-resource-response")
        return values

    def delete_resource(self, slot: str, resource: dict) -> None:
        """Request deletion of one resource inside the selected group without waiting."""
        identity = resource["id"]
        if (not identity.casefold().startswith((self.scope.group_id(slot) + "/providers/").casefold())
                or re.search(r"[\s?#]|/\.\.?(?:/|$)", identity) is not None):
            raise FleetError("invalid-resource-response")
        if is_cluster(resource):
            # The Arc proxy is gone by cleanup time, so the CLI cluster delete cannot run.
            self._run("resource-delete", slot, [
                "rest", "--method", "DELETE", "--uri", identity + "?api-version=2024-01-01", "--only-show-errors",
            ])
        else:
            self._run("resource-delete", slot, [
                "resource", "delete", "--ids", identity, "--no-wait", "--only-show-errors", "-o", "none",
            ])

    def deployments(self, slot: str) -> list[dict]:
        """Read and validate deployment timing and state for the selected group."""
        values = self._json("deployment-list", slot, [
            "deployment", "group", "list", "--resource-group", self.scope.group(slot),
            "--query", "[].{name:name,timestamp:properties.timestamp,duration:properties.duration,"
            "state:properties.provisioningState}",
        ], "invalid-deployment-response")
        if not isinstance(values, list) or len(values) > MAX_SNAPSHOT:
            raise FleetError("invalid-deployment-response")
        for item in values:
            if (
                not isinstance(item, dict) or set(item) != {"name", "timestamp", "duration", "state"}
                or any(not isinstance(value, str) or not value for value in item.values())
            ):
                raise FleetError("invalid-deployment-response")
            timestamp = _creation_time(item["timestamp"])
            duration = _deployment_duration(item["duration"])
            if timestamp is None or duration is None:
                raise FleetError("invalid-deployment-response")
            try:
                timestamp - duration
            except OverflowError:
                raise FleetError("invalid-deployment-response") from None
        return values

    def vaults(self, slot: str) -> list[str]:
        names = self._json("vault-list", slot, [
            "keyvault", "list", "--resource-group", self.scope.group(slot), "--query", "[].name",
        ])
        if not isinstance(names, list) or any(
            not isinstance(name, str) or VAULT_NAME.fullmatch(name) is None for name in names
        ):
            raise FleetError("invalid-group-response")
        return names

    def deleted_vault(self, slot: str, name: str) -> dict | None:
        if VAULT_NAME.fullmatch(name) is None:
            raise FleetError("invalid-group-response")
        records = self._json("vault-deleted", slot, [
            "keyvault", "list-deleted", "--resource-type", "vault", "--query", f"[?name=='{name}']",
        ])
        if not isinstance(records, list) or len(records) > 1 or any(
            not isinstance(record, dict) for record in records
        ):
            raise FleetError("invalid-group-response")
        return records[0] if records else None

    def purge_vault(self, slot: str, name: str, location: str) -> None:
        if VAULT_NAME.fullmatch(name) is None or not LOCATION.fullmatch(location):
            raise FleetError("invalid-group-response")
        self._run("vault-purge", slot, [
            "keyvault", "purge", "--subscription", self.scope.subscription, "--name", name,
            "--location", location, "--no-wait", "--only-show-errors", "-o", "none",
        ])


def is_cluster(resource: dict) -> bool:
    return (resource["type"].casefold() == CLUSTER_TYPE
            and re.search(r"/providers/microsoft\.kubernetes/connectedclusters/[^/]+$", resource["id"].casefold())
            is not None)


def _creation_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = CREATION_TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    base, fraction, offset = match.groups()
    normalized = base + (f".{fraction[:6]}" if fraction else "") + ("+00:00" if offset == "Z" else offset)
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _deployment_duration(value: object) -> timedelta | None:
    if not isinstance(value, str):
        return None
    match = DEPLOYMENT_DURATION.fullmatch(value)
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    try:
        return timedelta(hours=int(hours or 0), minutes=int(minutes or 0), seconds=float(seconds or 0))
    except (ValueError, OverflowError):
        return None


def preflight(scope: FleetScope, groups: AzureGroups) -> dict:
    """Commit the state of every slot before any Azure write.

    Ephemeral slots commit private creation markers and require absence.
    Persistent slots require the supplied group, refuse an existing AIO instance
    or a name this run would create, and commit a keyed snapshot of its resources.
    """
    if not scope.groups:
        owners = allocation_hashes(groups.owners, scope.slots)
        for slot in scope.slots:
            if groups.exists(slot):
                raise FleetError("preexisting-group")
        rows = {slot: {"admittedAbsent": True, "ownerSha256": owners[slot]} for slot in scope.slots}
    else:
        rows = {}
        for slot in scope.slots:
            if not groups.exists(slot):
                raise FleetError("persistent-group-missing")
            if not scope.owns(slot, groups.show(slot), ""):
                raise FleetError("ownership-mismatch")
            resources = groups.resources(slot)
            if any(item["type"].casefold() == INSTANCE_TYPE or item["name"].casefold() in scope.bound_names(slot)
                   for item in resources):
                raise FleetError("persistent-group-occupied")
            snapshot = sorted({scope.snapshot_digest(item["id"]) for item in resources})
            rows[slot] = {"admittedAbsent": False, "ownerSha256": scope.snapshot_commitment(slot, snapshot),
                          "snapshot": snapshot}
    return {
        "apiVersion": VERSION, "kind": "FleetOwnership",
        "context": scope.context(), "scopeKey": scope.key, "slots": rows,
    }


def validate_ownership(scope: FleetScope, lease: object) -> dict:
    """An ownership receipt is meaningful only with its independent expected digest."""
    expected = scope.context()
    context = lease.get("context") if isinstance(lease, dict) else None
    rows = lease.get("slots") if isinstance(lease, dict) else None

    def persistent_row(slot, row):
        snapshot = row.get("snapshot")
        return (
            set(row) == {"admittedAbsent", "ownerSha256", "snapshot"} and row["admittedAbsent"] is False
            and isinstance(snapshot, list) and len(snapshot) <= MAX_SNAPSHOT
            and all(isinstance(item, str) and re.fullmatch("[0-9a-f]{64}", item) for item in snapshot)
            and snapshot == sorted(set(snapshot))
            and row["ownerSha256"] == scope.snapshot_commitment(slot, snapshot)
        )

    if (
        not isinstance(lease, dict)
        or set(lease) != {"apiVersion", "kind", "context", "scopeKey", "slots"}
        or lease["apiVersion"] != VERSION or lease["kind"] != "FleetOwnership"
        or not isinstance(context, dict) or set(context) != set(expected)
        or any(type(context[key]) is not type(value) or context[key] != value
               for key, value in expected.items())
        or lease["scopeKey"] != scope.key
        or not isinstance(rows, dict) or set(rows) != set(scope.slots)
        or any(not isinstance(row, dict) or not isinstance(row.get("ownerSha256"), str)
               or re.fullmatch("[0-9a-f]{64}", row["ownerSha256"]) is None
               for row in rows.values())
        or len({row["ownerSha256"] for row in rows.values()}) != len(scope.slots)
        or any(
            not persistent_row(slot, row) if scope.groups
            else set(row) != {"admittedAbsent", "ownerSha256"} or type(row["admittedAbsent"]) is not bool
            for slot, row in rows.items()
        )
    ):
        raise FleetError("ownership-context-mismatch")
    return rows


def create(scope: FleetScope, lease: dict, groups: AzureGroups, location: str) -> None:
    """Fence each ephemeral creation with its committed marker, or confirm each persistent group."""
    if not LOCATION.fullmatch(location):
        raise FleetError("invalid-location")
    slots = validate_ownership(scope, lease)
    if scope.groups:
        for slot in scope.slots:
            if not groups.exists(slot):
                raise FleetError("persistent-group-missing")
            if not scope.owns(slot, groups.show(slot), slots[slot]["ownerSha256"]):
                raise FleetError("ownership-mismatch")
        return
    if not all(slots[slot]["admittedAbsent"] for slot in scope.slots):
        raise FleetError("ownership-not-established")
    if allocation_hashes(groups.owners, scope.slots) != {slot: slots[slot]["ownerSha256"] for slot in scope.slots}:
        raise FleetError("allocation-context-mismatch")
    for slot in scope.slots:
        if groups.exists(slot):
            raise FleetError("preexisting-group")
        groups.create(slot, location)
        if not scope.owns(slot, groups.show(slot), slots[slot]["ownerSha256"]):
            raise FleetError("ownership-mismatch")


def _poll(read, done, *, timeout: float, interval: float, clock, sleep):
    deadline = clock() + timeout
    while True:
        value = read()
        if done(value):
            return value, True
        remaining = deadline - clock()
        if remaining <= 0:
            return value, False
        sleep(min(interval, remaining))


def purge_vaults(
    scope: FleetScope, groups: AzureGroups, observed: dict, *, complete: bool,
    interval: float, clock, sleep, created_before: datetime | None = None,
    record_timeout: float = 120, purge_timeout: float = 300,
) -> str:
    """Purge only soft deleted vaults this attempt created in a group it used.

    Candidates are the vaults read in an ephemeral group before its deletion,
    or found in a persistent group's created delta, plus the vault name bound
    to this attempt for the existing Site slot. An unobserved bound name is
    checked once without waiting. Each deleted record must name the selected group.
    A purge outcome never changes the cleanup exit, and provider details stay private.
    """
    if not complete:
        return "not-attempted"
    if any(names is None for names in observed.values()):
        return "failed"
    bound = {slot: scope.vault(slot) for slot in scope.slots
             if scope.kind == "site" and slot == "existing"}
    selected = {(slot, name): True for slot, names in observed.items() for name in names}
    for slot, name in bound.items():
        selected.setdefault((slot, name), False)
    if not selected:
        return "not-applicable"
    result, purged = "purged", 0
    for (slot, name), seen in selected.items():
        expected = f"{scope.group_id(slot)}/providers/Microsoft.KeyVault/vaults/{name}".casefold()
        try:
            if seen:
                record, found = _poll(lambda: groups.deleted_vault(slot, name), lambda value: value is not None,
                                      timeout=record_timeout, interval=interval, clock=clock, sleep=sleep)
            else:
                # A bound name that the run never observed is checked once without waiting.
                record = groups.deleted_vault(slot, name)
                found = record is not None
                if not found:
                    continue
            properties = record.get("properties") if found else None
            if (
                not isinstance(properties, dict) or record.get("name") != name
                or not isinstance(properties.get("vaultId"), str)
                or properties["vaultId"].casefold() != expected
                or properties.get("purgeProtectionEnabled") is True
                or not isinstance(properties.get("location"), str)
            ):
                result = "failed"
                continue
            groups.purge_vault(slot, name, properties["location"])
            _, done = _poll(lambda: groups.deleted_vault(slot, name), lambda value: value is None,
                            timeout=purge_timeout, interval=interval, clock=clock, sleep=sleep)
            if not done:
                result = "failed"
                continue
            purged += 1
        except FleetError:
            result = "failed"
    return result if result == "failed" or purged else "not-applicable"


def _delete_groups(scope, slots, groups, results, vaults, *, timeout, interval, clock, sleep):
    pending = {}
    for slot in scope.slots:
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
                    if not scope.owns(slot, groups.show(slot), slots[slot]["ownerSha256"]):
                        results[slot] = {"state": "not-attempted", "reason": "ownership-mismatch"}
                        del pending[slot]
                        continue
                    if scope.kind == "site":
                        try:
                            vaults[slot] = groups.vaults(slot)
                        except FleetError:
                            vaults[slot] = None
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


def _delete_created(scope, slots, groups, results, vaults, *, timeout, interval, clock, sleep,
                    created_before=None, retry_after=180):
    """Delete the created delta and confirm it is empty.

    A bounded reconciliation waits for deployments started within the original
    job window and extends the bound to their completion. Missing creation
    times keep the slot incomplete without deleting those resources.
    """
    pending = {slot: "inspect" for slot in scope.slots}
    requested = {}
    deadline = clock() + timeout
    while pending:
        for slot in tuple(pending):
            try:
                if not groups.exists(slot):
                    results[slot] = {"state": "unknown", "reason": "persistent-group-missing"}
                    del pending[slot]
                    continue
                effective_bound = created_before
                if created_before is not None:
                    deployments = [
                        item for item in groups.deployments(slot)
                        if _creation_time(item["timestamp"]) - _deployment_duration(item["duration"])
                        <= created_before + CREATION_CLOCK_TOLERANCE
                    ]
                    if any(item["state"].casefold() not in {"succeeded", "failed", "canceled"}
                           for item in deployments):
                        results[slot] = {"state": "residual", "reason": "deployment-running"}
                        continue
                    effective_bound = max([
                        created_before, *(_creation_time(item["timestamp"]) for item in deployments),
                    ])
                snapshot = set(slots[slot]["snapshot"])
                created = []
                unavailable = False
                for item in groups.resources(slot):
                    if scope.snapshot_digest(item["id"]) in snapshot:
                        continue
                    if created_before is not None:
                        created_at = _creation_time(item.get("createdTime"))
                        if created_at is None:
                            unavailable = True
                            continue
                        if created_at > effective_bound + CREATION_CLOCK_TOLERANCE:
                            continue
                    created.append(item)
                if pending[slot] == "inspect":
                    vaults[slot] = [item["name"] for item in created if item["type"].casefold() == VAULT_TYPE
                                    and VAULT_NAME.fullmatch(item["name"])]
                    pending[slot] = "deleting"
                if not created:
                    results[slot] = ({"state": "residual", "reason": "creation-time-unavailable"} if unavailable
                                     else {"state": "absent", "reason": "confirmed-absent"})
                    del pending[slot]
                    continue
                # The cluster goes first so its extensions are removed with it.
                for item in sorted(created, key=lambda value: not is_cluster(value)):
                    identity = item["id"].casefold()
                    if identity in requested and clock() - requested[identity] < retry_after:
                        continue
                    try:
                        groups.delete_resource(slot, item)
                        requested[identity] = clock()
                    except FleetError as error:
                        if error.args[0] in {"invalid-resource-response", "private-diagnostics-failed"}:
                            raise
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
        sleep(min(interval, remaining))


def cleanup(
    scope: FleetScope, lease: dict, groups: AzureGroups, *, operation_exit: int = 0,
    timeout: float = 1200, interval: float = 15, clock=time.monotonic, sleep=time.sleep,
    created_before: datetime | None = None,
) -> tuple[int, dict]:
    """Remove what this attempt created, confirm it is absent, and preserve the operation exit.

    Ephemeral groups are deleted once and polled until absent. Persistent groups
    are never deleted. Without a bound, their created delta is removed and
    polled until empty. A bounded reconciliation waits for deployments started
    within the original job window and extends its resource bound to their
    completion, allowing the clock tolerance. Running deployments and resources
    without a usable creation time leave cleanup incomplete. Site scopes then
    purge only the vaults eligible for deletion.
    """
    slots = validate_ownership(scope, lease)
    if (
        type(operation_exit) is not int or not 0 <= operation_exit <= 255
        or type(timeout) not in {int, float} or type(interval) not in {int, float}
        or not 0 < timeout <= 3600 or not 0 < interval <= timeout
        or created_before is not None and (
            not isinstance(created_before, datetime) or created_before.utcoffset() is None
        )
    ):
        raise FleetError("invalid-cleanup-bound")
    results, vaults = {}, {}
    remove = _delete_created if scope.groups else _delete_groups
    options = {"created_before": created_before} if scope.groups else {}
    remove(scope, slots, groups, results, vaults, timeout=timeout, interval=interval, clock=clock, sleep=sleep,
           **options)
    complete = all(result["state"] == "absent" for result in results.values())
    receipt = {
        "apiVersion": VERSION, "kind": "FleetCleanup", "groups": scope.mode,
        "context": scope.context(), "scopeKey": scope.key, "slots": results,
        "status": "complete" if complete else "incomplete", "operationExit": operation_exit,
    }
    if scope.kind == "site":
        receipt["kind"] = "SiteCleanup"
        receipt["vaultPurge"] = purge_vaults(
            scope, groups, vaults, complete=complete, interval=interval, clock=clock, sleep=sleep,
            created_before=created_before if scope.groups else None,
        )
    return operation_exit or (0 if complete else 1), receipt

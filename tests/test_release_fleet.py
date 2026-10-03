"""Exercise fleet ownership and confirmed cleanup without cloud calls or real delays."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_fleet import (  # noqa: E402
    SLOTS,
    AzureGroups,
    FleetError,
    FleetScope,
    cleanup,
    create,
    preflight,
    validate_ownership,
)

SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
OWNERS = {"one": "siteops-fleet-" + "1" * 64, "two": "siteops-fleet-" + "2" * 64}


@pytest.fixture(autouse=True)
def block_live_processes(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Live process escaped the fleet fixture."))


@pytest.fixture
def scope():
    return FleetScope("example/repository", "a" * 40, "b" * 64, "c" * 64, 42, 3, SUBSCRIPTION)


class Groups:
    def __init__(self, scope):
        self.scope = scope
        self.present = {slot: False for slot in SLOTS}
        self.observations = {}
        self.after_delete = {}
        self.delete_errors = {}
        self.read_errors = {}
        self.create_failure = None
        self.calls = []
        self.owners = dict(OWNERS)
        self.created_owners = {}

    def exists(self, slot):
        self.calls.append(("exists", slot))
        if slot in self.read_errors:
            raise self.read_errors[slot]
        sequence = self.after_delete.get(slot)
        if sequence and ("delete", slot) in self.calls:
            self.present[slot] = sequence.pop(0)
        return self.present[slot]

    def show(self, slot):
        self.calls.append(("show", slot))
        name = self.scope.group(slot)
        return self.observations.get(slot, {
            "name": name, "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{name}",
            "tags": self.scope.tags(slot), "managedBy": self.created_owners.get(slot),
        })

    def create(self, slot, location):
        self.calls.append(("create", slot))
        self.present[slot] = True
        self.created_owners[slot] = self.owners[slot]
        if self.create_failure == slot:
            raise FleetError("provider-request-failed")

    def delete(self, slot):
        self.calls.append(("delete", slot))
        if slot in self.delete_errors:
            raise self.delete_errors[slot]
        if slot not in self.after_delete:
            self.present[slot] = False


class Clock:
    def __init__(self):
        self.value = 0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.value += duration


def invoke(scope, lease, groups, *, operation_exit=0):
    clock = Clock()
    code, report = cleanup(
        scope, lease, groups, operation_exit=operation_exit, timeout=3, interval=1,
        clock=clock.now, sleep=clock.sleep,
    )
    return code, report, clock


def test_preflight_refuses_an_existing_group_before_any_create(scope):
    groups = Groups(scope)
    groups.present["two"] = True
    with pytest.raises(FleetError, match="preexisting-group"):
        preflight(scope, groups)
    assert groups.calls == [("exists", "one"), ("exists", "two")]


def test_failed_preflight_inventory_is_not_treated_as_absence(scope):
    groups = Groups(scope)
    groups.read_errors["one"] = FleetError("provider-request-failed")
    with pytest.raises(FleetError, match="provider-request-failed"):
        preflight(scope, groups)
    assert groups.calls == [("exists", "one")]


def test_creation_rechecks_absence_and_does_not_take_over_an_existing_group(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    groups.present["one"] = True
    with pytest.raises(FleetError, match="preexisting-group"):
        create(scope, lease, groups, "eastus")
    assert not any(operation == "create" for operation, _ in groups.calls)


@pytest.mark.parametrize("fault", ["identity", "immutable-owner"])
def test_creation_requires_the_expected_identity_and_tags_before_the_next_slot(scope, fault):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    groups.observations["one"] = (
        {"name": "other", "id": "other", "tags": {}}
        if fault == "identity" else {
            "name": scope.group("one"),
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{scope.group('one')}",
            "tags": scope.tags("one"), "managedBy": OWNERS["two"],
        }
    )
    with pytest.raises(FleetError, match="ownership-mismatch"):
        create(scope, lease, groups, "eastus")
    assert ("create", "one") in groups.calls and ("create", "two") not in groups.calls


@pytest.mark.parametrize("foreign_owner", [None, "", "another-manager"])
@pytest.mark.parametrize("matching_tags", [False, True])
def test_intervening_group_is_not_overwritten_or_deleted(
    scope, tmp_path, foreign_owner, matching_tags,
):
    foreign = {
        "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{scope.group('one')}",
        "name": scope.group("one"), "managedBy": foreign_owner,
        "tags": scope.tags("one") if matching_tags else {"owner": "another-actor"},
    }
    state = {slot: None for slot in SLOTS}
    writes = []

    def runner(arguments):
        assert arguments[:2] == ["az", "group"]
        slot = next(slot for slot in SLOTS if scope.group(slot) == arguments[arguments.index("--name") + 1])
        operation = arguments[2]
        if operation == "exists":
            return 0, json.dumps(state[slot] is not None).encode(), b""
        if operation == "show":
            return 0, json.dumps(state[slot]).encode(), b""
        if operation == "create":
            if slot == "one":
                state[slot] = copy.deepcopy(foreign)
            owner = arguments[arguments.index("--managed-by") + 1] if "--managed-by" in arguments else None
            if state[slot] is not None and state[slot].get("managedBy") != owner:
                return 1, b"", b"ResourceGroupManagedByMismatch"
            tags = dict(value.split("=", 1) for value in arguments[arguments.index("--tags") + 1:])
            state[slot] = {
                "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{scope.group(slot)}",
                "name": scope.group(slot), "managedBy": owner, "tags": tags,
            }
            writes.append(("create", slot))
            return 0, b"", b""
        if operation == "delete":
            writes.append(("delete", slot))
            state[slot] = None
            return 0, b"", b""
        pytest.fail("Unexpected provider operation.")

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    groups.owners = dict(OWNERS)
    lease = preflight(scope, groups)
    with pytest.raises(FleetError, match="provider-request-failed"):
        create(scope, lease, groups, "eastus")
    assert state["one"] == foreign
    code, report, _ = invoke(scope, lease, groups)
    assert code == 1 and report["slots"]["one"]["state"] == "not-attempted"
    assert report["slots"]["two"]["state"] == "absent"
    assert state["one"] == foreign
    assert writes == []


def test_complete_cleanup_observes_absence_and_has_no_private_identities(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus")
    code, report, clock = invoke(scope, lease, groups)
    assert code == 0 and report["status"] == "complete"
    assert all(row == {"state": "absent", "reason": "confirmed-absent"} for row in report["slots"].values())
    assert clock.sleeps == []
    assert [call for call in groups.calls if call[0] == "delete"] == [("delete", "one"), ("delete", "two")]
    encoded = json.dumps(report) + json.dumps(lease)
    assert scope.key[:24] not in scope.group("one")
    for private in (SUBSCRIPTION, *OWNERS.values(), *(scope.group(slot) for slot in SLOTS),
                    *(scope.cluster(slot) for slot in SLOTS)):
        assert private not in encoded


@pytest.mark.parametrize("field", ["scope", "slot", "run", "name", "subscription", "immutable-owner"])
def test_cleanup_refuses_foreign_ownership_but_still_cleans_other_slot(scope, field):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus")
    observed = copy.deepcopy(groups.show("one"))
    if field in {"scope", "slot", "run"}:
        key = {"scope": "qualificationScope", "slot": "qualificationSlot", "run": "runId"}[field]
        observed["tags"][key] = "other"
    elif field == "name":
        observed["name"] = "other"
    elif field == "immutable-owner":
        observed["managedBy"] = OWNERS["two"]
    else:
        observed["id"] = observed["id"].replace(SUBSCRIPTION, "00000000-0000-0000-0000-000000000002")
    groups.observations["one"] = observed
    code, report, _ = invoke(scope, lease, groups)
    assert ("delete", "one") not in groups.calls
    assert code == 1
    assert report["slots"]["one"] == {"state": "not-attempted", "reason": "ownership-mismatch"}
    assert report["slots"]["two"]["state"] == "absent"


def test_a_group_not_admitted_by_this_run_is_never_deleted_even_with_matching_tags(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    groups.present["one"] = True
    lease["slots"]["one"]["admittedAbsent"] = False
    code, report, _ = invoke(scope, lease, groups)
    assert code == 1 and report["slots"]["one"]["state"] == "not-attempted"
    assert ("delete", "one") not in groups.calls


@pytest.mark.parametrize("change", ["attempt", "candidate", "subscription"])
def test_scope_drift_fails_before_any_provider_call(scope, change):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    scope = replace(scope, **{
        "attempt": {"attempt": 4},
        "candidate": {"admission_sha256": "d" * 64},
        "subscription": {"subscription": "00000000-0000-0000-0000-000000000002"},
    }[change])
    groups.calls.clear()
    with pytest.raises(FleetError, match="ownership-context-mismatch"):
        invoke(scope, lease, groups)
    assert groups.calls == []


@pytest.mark.parametrize("original_exit", [1, 23, 130])
def test_cleanup_after_partial_creation_preserves_the_original_failure(scope, original_exit):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    groups.create_failure = "two"
    with pytest.raises(FleetError, match="provider-request-failed"):
        create(scope, lease, groups, "eastus")
    code, report, _ = invoke(scope, lease, groups, operation_exit=original_exit)
    assert code == original_exit and report["operationExit"] == original_exit
    assert report["status"] == "complete"
    assert not any(groups.present.values())


@pytest.mark.parametrize("eventual", [False, True])
def test_accepted_deletion_is_polled_until_absence_or_a_failing_deadline(scope, eventual):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus")
    groups.after_delete["one"] = [True, False] if eventual else [True] * 10
    code, report, clock = invoke(scope, lease, groups)
    assert code == (0 if eventual else 1)
    assert report["slots"]["one"]["state"] == ("absent" if eventual else "residual")
    assert report["slots"]["two"]["state"] == "absent"
    assert groups.calls.count(("delete", "one")) == 1
    assert clock.value == (1 if eventual else 3)


def test_unknown_inventory_and_delete_failure_remain_failing_outcomes(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus")
    groups.read_errors["one"] = FleetError("provider-request-failed")
    groups.delete_errors["two"] = FleetError("provider-request-failed")
    code, report, clock = invoke(scope, lease, groups)
    assert code == 1
    assert all(row["state"] == "unknown" for row in report["slots"].values())
    assert clock.sleeps == []
    assert ("delete", "one") not in groups.calls
    assert groups.calls.count(("delete", "two")) == 1


def test_timeout_after_delete_submission_is_observed_without_resubmitting(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus")
    groups.delete_errors["one"] = FleetError("provider-timeout", retryable=True)
    groups.after_delete["one"] = [True, False]
    code, report, _ = invoke(scope, lease, groups)
    assert code == 0 and report["slots"]["one"]["state"] == "absent"
    assert groups.calls.count(("delete", "one")) == 1


@pytest.mark.parametrize("raw", [b"", b'"false"', b"null", b"{}\n", b"false\nextra"])
def test_adapter_rejects_malformed_existence_without_assuming_absence(scope, tmp_path, raw):
    groups = AzureGroups(scope, tmp_path / "logs", runner=lambda args: (0, raw, b"private-stderr"))
    with pytest.raises(FleetError, match="invalid-existence-response"):
        groups.exists("one")


def test_adapter_preserves_private_diagnostics_and_scopes_every_azure_command(scope, tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        return 0, b"false", b"private-provider-marker"

    logs = tmp_path / "logs"
    groups = AzureGroups(scope, logs, runner=runner)
    assert groups.exists("one") is False
    assert calls == [[
        "az", "group", "exists", "--subscription", SUBSCRIPTION,
        "--name", scope.group("one"), "--only-show-errors", "-o", "json",
    ]]
    assert (logs / "1-one-exists.stderr").read_bytes() == b"private-provider-marker"


def test_unknown_failure_text_cannot_enter_the_public_receipt():
    with pytest.raises(ValueError, match="Unsupported fleet failure category"):
        FleetError("private-provider-message")


def test_ownership_receipt_digest_is_checked_before_parsing(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "manage_fleet", ROOT / "scripts" / "manage-release-fleet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "receipt.json"
    path.write_text('{"private":"value"}')
    with pytest.raises(FleetError, match="receipt-digest-mismatch"):
        module.expected_document(path, "0" * 64)


@pytest.mark.parametrize("operation_exit", [0, 23, 130])
def test_actual_management_entrypoint_uses_selected_receipts_and_preserves_exit(
    tmp_path, monkeypatch, capsys, operation_exit,
):
    spec = importlib.util.spec_from_file_location(
        "manage_fleet_entry", ROOT / "scripts" / "manage-release-fleet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission",
        "status": "admitted",
        "source": {"repository": "example/repository", "commit": "a" * 40, "ref": "refs/heads/main"},
        "run": 40, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False,
        "artifacts": {"plan": 11, "inventory": 12, "payload": 13},
        "planSha256": "d" * 64, "inventorySha256": "c" * 64,
        "subjects": {"engine": 4, "workspace": 1},
        "installation": "not-run", "deployment": "not-run",
    }))
    admission_sha = hashlib.sha256(admission.read_bytes()).hexdigest()
    for key, value in {
        "GITHUB_REPOSITORY": "example/repository", "FLEET_RUN_ID": "42",
        "FLEET_RUN_ATTEMPT": "3", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
    }.items():
        monkeypatch.setenv(key, value)
    selected = []

    def groups(scope, _logs, *, owners=None):
        if not selected:
            selected.append(Groups(scope))
        assert selected[0].scope == scope
        if owners is not None:
            selected[0].owners = owners
        return selected[0]

    monkeypatch.setattr(module, "AzureGroups", groups)
    ownership = tmp_path / "ownership.json"
    allocation = tmp_path / "allocation.json"
    for operation in ("preflight", "create", "cleanup"):
        output = ownership if operation == "preflight" else tmp_path / f"{operation}.json"
        args = [
            "manage-release-fleet.py", operation, "--admission", str(admission),
            "--expected-admission-sha", admission_sha, "--output", str(output),
            "--private-logs", str(tmp_path / f"{operation}-logs"),
        ]
        if operation != "preflight":
            args.extend([
                "--execute", "--ownership", str(ownership),
                "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest(),
            ])
        if operation == "create":
            args.extend(["--location", "eastus"])
        if operation in {"preflight", "create"}:
            args.extend(["--allocation-state", str(allocation)])
        if operation == "cleanup":
            args.extend(["--operation-exit", str(operation_exit)])
        monkeypatch.setattr(sys, "argv", args)
        assert module.main() == (operation_exit if operation == "cleanup" else 0)
        assert output.is_file()
    private = json.loads(allocation.read_text())
    assert len(set(private["owners"].values())) == 2
    assert all(len(value) == len("siteops-fleet-") + 64 for value in private["owners"].values())
    if os.name == "posix":
        assert allocation.stat().st_mode & 0o777 == 0o600
    report = json.loads((tmp_path / "cleanup.json").read_text())
    assert report["status"] == "complete" and report["operationExit"] == operation_exit
    captured = capsys.readouterr()
    assert SUBSCRIPTION not in captured.out + captured.err
    assert all(value not in captured.out + captured.err + ownership.read_text()
               for value in private["owners"].values())
    assert not any(selected[0].present.values())


@pytest.mark.parametrize("operation", ["create", "cleanup"])
def test_mutating_entrypoints_require_deliberate_execution_arguments(tmp_path, monkeypatch, operation):
    spec = importlib.util.spec_from_file_location(
        "manage_fleet_refusal", ROOT / "scripts" / "manage-release-fleet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AzureGroups",
                        lambda *args: pytest.fail("Unapproved mutation reached the provider."))
    monkeypatch.setattr(sys, "argv", [
        "manage-release-fleet.py", operation, "--admission", str(tmp_path / "missing"),
        "--expected-admission-sha", "0" * 64, "--output", str(tmp_path / "result"),
        "--private-logs", str(tmp_path / "logs"),
    ])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2
    assert not (tmp_path / "result").exists()


def test_preflight_commits_distinct_private_markers_before_creation(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    assert lease["slots"] == {
        slot: {"admittedAbsent": True, "ownerSha256": hashlib.sha256(owner.encode()).hexdigest()}
        for slot, owner in OWNERS.items()
    }
    assert not any(operation == "create" for operation, _ in groups.calls)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "malformed", "changed"])
def test_creation_rejects_missing_or_changed_private_allocation_before_azure(scope, fault):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    groups.calls.clear()
    if fault == "missing":
        groups.owners = None
    elif fault == "duplicate":
        groups.owners["two"] = groups.owners["one"]
    elif fault == "malformed":
        groups.owners["one"] = "private-malformed-marker"
    else:
        groups.owners["one"] = "siteops-fleet-" + "3" * 64
    with pytest.raises(FleetError) as caught:
        create(scope, lease, groups, "eastus")
    assert "private-" not in str(caught.value)
    assert not groups.calls


@pytest.mark.parametrize("fault", ["missing", "duplicate", "invalid"])
def test_ownership_requires_original_marker_commitments(scope, fault):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    if fault == "missing":
        del lease["slots"]["one"]["ownerSha256"]
    elif fault == "duplicate":
        lease["slots"]["two"]["ownerSha256"] = lease["slots"]["one"]["ownerSha256"]
    else:
        lease["slots"]["one"]["ownerSha256"] = "not-a-digest"
    with pytest.raises(FleetError, match="ownership-context-mismatch"):
        validate_ownership(scope, lease)


@pytest.mark.parametrize("operation", ["preflight", "create"])
def test_allocation_state_is_required_before_azure_reads(tmp_path, monkeypatch, operation):
    spec = importlib.util.spec_from_file_location(
        "manage_allocation_required", ROOT / "scripts" / "manage-release-fleet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AzureGroups",
                        lambda *args, **kwargs: pytest.fail("Missing allocation reached Azure."))
    args = [
        "manage-release-fleet.py", operation, "--admission", str(tmp_path / "missing"),
        "--expected-admission-sha", "0" * 64, "--output", str(tmp_path / "result"),
        "--private-logs", str(tmp_path / "logs"),
    ]
    if operation == "create":
        args.extend(["--execute", "--ownership", str(tmp_path / "ownership"),
                     "--expected-ownership-sha", "0" * 64, "--location", "eastus"])
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2

"""Exercise fleet ownership and confirmed cleanup without cloud calls or real delays."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_fleet import (  # noqa: E402
    SITE_SLOTS,
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
        self.present = {slot: False for slot in scope.slots}
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


def invoke(scope, lease, groups, *, operation_exit=0, created_before=None):
    clock = Clock()
    code, report = cleanup(
        scope, lease, groups, operation_exit=operation_exit, timeout=3, interval=1,
        clock=clock.now, sleep=clock.sleep, created_before=created_before,
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
    real_cleanup = module.cleanup

    def bounded_cleanup(*args, **kwargs):
        assert kwargs["created_before"] == datetime(2026, 10, 9, 12, 34, 56, tzinfo=timezone.utc)
        return real_cleanup(*args, **kwargs)

    monkeypatch.setattr(module, "cleanup", bounded_cleanup)
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
            args.extend(["--operation-exit", str(operation_exit),
                         "--created-before", "2026-10-09T12:34:56Z"])
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


@pytest.mark.parametrize("value", [
    "2026-10-09T12:34:56+00:00", "2026-10-09T12:34:56.1Z",
    "2026-10-09T12:34:56", "2026-13-09T12:34:56Z", "rg-private-time",
])
def test_management_rejects_non_utc_second_cleanup_bounds_without_echo(tmp_path, monkeypatch, capsys, value):
    spec = importlib.util.spec_from_file_location(
        "manage_bound_refusal", ROOT / "scripts" / "manage-release-fleet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [
        "manage-release-fleet.py", "cleanup", "--created-before", value,
        "--admission", str(tmp_path / "missing"), "--expected-admission-sha", "0" * 64,
        "--output", str(tmp_path / "result"), "--private-logs", str(tmp_path / "logs"),
    ])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2
    message = capsys.readouterr().err
    assert "Expected a UTC completion time in YYYY-MM-DDTHH:MM:SSZ format." in message
    assert value not in message


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


def site_scope(slot="existing", subscription=SUBSCRIPTION, groups=None):
    return FleetScope("example/repository", "a" * 40, "b" * 64, "c" * 64, 42, 3, subscription, "site", (slot,),
                      groups)


class SiteGroups(Groups):
    """Closed group and vault double. Unexpected vault names or calls fail the test."""

    def __init__(self, scope, vaults=("kvowned",)):
        super().__init__(scope)
        self.owners = {scope.slots[0]: OWNERS["one"]}
        self.vault_names = list(vaults)
        self.vault_error = None
        self.deleted = {}
        self.purge_errors = set()
        self.record_group = scope.group(scope.slots[0])

    def vaults(self, slot):
        self.calls.append(("vaults", slot))
        if self.vault_error:
            raise self.vault_error
        assert self.present[slot], "Vault inventory must precede group deletion."
        return list(self.vault_names)

    def delete(self, slot):
        super().delete(slot)
        for name in self.vault_names:
            self.deleted[name] = {
                "name": name, "properties": {
                    "vaultId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{self.record_group}"
                               f"/providers/Microsoft.KeyVault/vaults/{name}",
                    "location": "eastus2", "purgeProtectionEnabled": None,
                },
            }

    def deleted_vault(self, slot, name):
        self.calls.append(("deleted", name))
        assert name in self.vault_names or name == self.scope.vault(slot), (
            "Only vaults read inside the owned group or bound to this attempt may be inspected.")
        return self.deleted.get(name)

    def purge_vault(self, slot, name, location):
        self.calls.append(("purge", name))
        assert name in self.vault_names and location == "eastus2"
        if name in self.purge_errors:
            raise FleetError("provider-request-failed")
        del self.deleted[name]


def site_cleanup(scope, groups):
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus2")
    groups.calls.clear()
    return invoke(scope, lease, groups)


def test_site_scope_owns_one_case_with_names_separate_from_the_fleet(scope):
    site = site_scope()
    assert site.slots == ("existing",)
    assert site.group("existing").startswith("rg-siteops-site-")
    assert site.cluster("existing").startswith("arc-siteops-site-")
    assert site.group("existing") not in {scope.group(slot) for slot in SLOTS}
    assert site.tags("existing")["managedBy"] == "siteops-site-acceptance"
    assert site.key == scope.key
    assert len(site.vault("existing")) == 24 and site.vault("existing").startswith("kv")
    assert site.vault("existing") != site_scope(subscription="00000000-0000-0000-0000-000000000002").vault("existing")
    assert site.vault("existing") not in json.dumps(site.context()) + site.key
    for invalid in (lambda: site.group("one"), lambda: scope.group("existing"), lambda: scope.vault("one"),
                    lambda: site_scope("enabled").vault("enabled")):
        with pytest.raises(FleetError, match="invalid-slot"):
            invalid()
    for kind, slots in (("site", SITE_SLOTS), ("site", ("one",)), ("fleet", ("existing",)), ("site", ["existing"])):
        with pytest.raises(FleetError, match="invalid-scope"):
            FleetScope("example/repository", "a" * 40, "b" * 64, "c" * 64, 42, 3, SUBSCRIPTION, kind, slots)


def test_site_cleanup_purges_only_vaults_read_inside_its_deleted_group():
    scope = site_scope()
    groups = SiteGroups(scope, vaults=("kvowned", "kvsecond"))
    code, report, _ = site_cleanup(scope, groups)
    assert code == 0
    assert report["kind"] == "SiteCleanup" and report["status"] == "complete" and report["groups"] == "ephemeral"
    assert report["vaultPurge"] == "purged"
    assert report["slots"] == {"existing": {"state": "absent", "reason": "confirmed-absent"}}
    assert groups.calls.index(("vaults", "existing")) < groups.calls.index(("delete", "existing"))
    assert groups.calls.index(("delete", "existing")) < groups.calls.index(("purge", "kvowned"))
    assert "kvowned" not in json.dumps(report)


@pytest.mark.parametrize(("fault", "expected"), [
    ("other-group", "failed"), ("purge-protected", "failed"), ("purge-error", "failed"),
    ("inventory-error", "failed"), ("no-vaults", "not-applicable"), ("never-deleted", "failed"),
])
def test_vault_purge_failures_are_recorded_without_failing_cleanup(fault, expected):
    scope = site_scope()
    groups = SiteGroups(scope, vaults=() if fault == "no-vaults" else ("kvowned",))
    if fault == "other-group":
        groups.record_group = "rg-belongs-to-someone-else"
    elif fault == "purge-error":
        groups.purge_errors.add("kvowned")
    elif fault == "inventory-error":
        groups.vault_error = FleetError("provider-request-failed")
    original_delete = groups.delete

    def delete(slot):
        original_delete(slot)
        if fault == "purge-protected":
            groups.deleted["kvowned"]["properties"]["purgeProtectionEnabled"] = True
        elif fault == "never-deleted":
            groups.deleted.clear()

    groups.delete = delete
    code, report, _ = site_cleanup(scope, groups)
    assert code == 0 and report["status"] == "complete"
    assert report["vaultPurge"] == expected
    if fault in {"other-group", "purge-protected", "never-deleted", "inventory-error"}:
        assert not any(call[0] == "purge" for call in groups.calls)


def test_incomplete_site_cleanup_does_not_attempt_a_purge():
    scope = site_scope()
    groups = SiteGroups(scope)
    groups.after_delete["existing"] = [True, True, True, True, True]
    code, report, _ = site_cleanup(scope, groups)
    assert code == 1 and report["status"] == "incomplete"
    assert report["vaultPurge"] == "not-attempted"
    assert not any(call[0] in {"deleted", "purge"} for call in groups.calls)


def test_fleet_cleanup_receipt_keeps_its_shape_without_vault_reads(scope):
    groups = Groups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus2")
    _, report, _ = invoke(scope, lease, groups)
    assert report["kind"] == "FleetCleanup" and "vaultPurge" not in report


def test_vault_adapter_uses_exact_scoped_commands_and_private_logs(tmp_path):
    scope = site_scope()
    group = scope.group("existing")
    expected = {
        ("keyvault", "list"): [
            "az", "keyvault", "list", "--resource-group", group, "--query", "[].name",
            "--subscription", SUBSCRIPTION, "--only-show-errors", "-o", "json"],
        ("keyvault", "list-deleted"): [
            "az", "keyvault", "list-deleted", "--resource-type", "vault", "--query", "[?name=='kvowned']",
            "--subscription", SUBSCRIPTION, "--only-show-errors", "-o", "json"],
        ("keyvault", "purge"): [
            "az", "keyvault", "purge", "--subscription", SUBSCRIPTION, "--name", "kvowned",
            "--location", "eastus2", "--no-wait", "--only-show-errors", "-o", "none"],
    }
    calls = []

    def runner(args):
        calls.append(args)
        assert args == expected[tuple(args[1:3])], "Unexpected Azure command."
        payload = {"list": b'["kvowned"]', "list-deleted": b"[]", "purge": b""}[args[2]]
        return 0, payload, b"private-provider-marker%0A\r"

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    assert groups.vaults("existing") == ["kvowned"]
    assert groups.deleted_vault("existing", "kvowned") is None
    groups.purge_vault("existing", "kvowned", "eastus2")
    assert len(calls) == 3
    assert (tmp_path / "logs" / "1-existing-vault-list.stderr").read_bytes() == b"private-provider-marker%0A\r"
    for invalid in ("kv'] || [?", "k", "x" * 30):
        with pytest.raises(FleetError):
            groups.deleted_vault("existing", invalid)
    with pytest.raises(FleetError):
        AzureGroups(scope, tmp_path / "other", runner=lambda args: (0, b'["bad name"]', b"")).vaults("existing")


def test_site_management_entrypoint_owns_one_case_and_reports_the_purge(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("manage_site_entry", ROOT / "scripts" / "manage-release-fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission", "status": "admitted",
        "source": {"repository": "example/repository", "commit": "a" * 40, "ref": "refs/heads/main"},
        "run": 40, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False,
        "artifacts": {"plan": 11, "inventory": 12, "payload": 13}, "planSha256": "d" * 64, "inventorySha256": "c" * 64,
        "subjects": {"engine": 4, "workspace": 1}, "installation": "not-run", "deployment": "not-run",
    }))
    admission_sha = hashlib.sha256(admission.read_bytes()).hexdigest()
    for key, value in {"GITHUB_REPOSITORY": "example/repository", "FLEET_RUN_ID": "42", "FLEET_RUN_ATTEMPT": "3",
                       "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION}.items():
        monkeypatch.setenv(key, value)
    selected = []

    def groups(scope, _logs, *, owners=None):
        assert scope.kind == "site" and scope.slots == ("existing",)
        if not selected:
            selected.append(SiteGroups(scope))
        if owners is not None:
            selected[0].owners = owners
        return selected[0]

    monkeypatch.setattr(module, "AzureGroups", groups)
    monkeypatch.setattr(module, "cleanup", lambda *args, **kwargs: cleanup(
        *args, **kwargs, timeout=3, interval=1, clock=lambda: 0, sleep=lambda _: None))
    ownership, allocation = tmp_path / "ownership.json", tmp_path / "allocation.json"
    for operation in ("preflight", "create", "cleanup"):
        output = ownership if operation == "preflight" else tmp_path / f"{operation}.json"
        args = ["manage-release-fleet.py", operation, "--kind", "site", "--slot", "existing",
                "--admission", str(admission), "--expected-admission-sha", admission_sha,
                "--output", str(output), "--private-logs", str(tmp_path / f"{operation}-logs")]
        if operation != "preflight":
            args += ["--execute", "--ownership", str(ownership),
                     "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest()]
        if operation == "create":
            args += ["--location", "eastus2"]
        if operation != "cleanup":
            args += ["--allocation-state", str(allocation)]
        monkeypatch.setattr(sys, "argv", args)
        assert module.main() == 0
    assert set(json.loads(ownership.read_text())["slots"]) == {"existing"}
    assert json.loads((tmp_path / "create.json").read_text())["slots"] == ["existing"]
    report = json.loads((tmp_path / "cleanup.json").read_text())
    assert report["kind"] == "SiteCleanup" and report["vaultPurge"] == "purged"
    captured = capsys.readouterr()
    assert "kvowned" not in captured.out + captured.err + json.dumps(report)


@pytest.mark.parametrize("arguments", [["--kind", "site"], ["--slot", "existing"]])
def test_site_kind_and_slot_are_required_together(tmp_path, monkeypatch, arguments):
    spec = importlib.util.spec_from_file_location("manage_site_refusal", ROOT / "scripts" / "manage-release-fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AzureGroups", lambda *args, **kwargs: pytest.fail("Ambiguous scope reached Azure."))
    monkeypatch.setattr(sys, "argv", [
        "manage-release-fleet.py", "preflight", *arguments, "--admission", str(tmp_path / "missing"),
        "--expected-admission-sha", "0" * 64, "--output", str(tmp_path / "result"),
        "--private-logs", str(tmp_path / "logs"), "--allocation-state", str(tmp_path / "allocation"),
    ])
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 2


PERSISTENT = {"site": ("rg-PersistentSite.one",), "fleet": ("rg-fleet-one", "rg-fleet-two")}


def persistent_scope(kind="site", slot="existing", groups=None):
    if kind == "site":
        return site_scope(slot, groups=groups or PERSISTENT["site"])
    return FleetScope("example/repository", "a" * 40, "b" * 64, "c" * 64, 42, 3, SUBSCRIPTION, "fleet", SLOTS,
                      groups or PERSISTENT["fleet"])


class PersistentGroups:
    """Closed double for supplied groups. Group creation or deletion fails the test."""

    def __init__(self, scope, existing=()):
        self.scope = scope
        self.present = {slot: True for slot in scope.slots}
        self.items = {slot: [] for slot in scope.slots}
        self.sticky, self.failures, self.list_errors, self.purge_errors = set(), {}, {}, set()
        self.deleted_vaults, self.calls, self.owners = {}, [], None
        self.deployment_rows = {slot: [] for slot in scope.slots}
        for slot in scope.slots:
            for kind, name in existing:
                self.add(slot, kind, name)

    def add(self, slot, kind, name, created_time=None):
        self.items[slot].append({"id": f"{self.scope.group_id(slot)}/providers/{kind}/{name}", "type": kind,
                                 "name": name, "createdTime": created_time})

    def soft_delete(self, slot, name, group=None):
        self.deleted_vaults[name] = {"name": name, "properties": {
            "vaultId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group or self.scope.group(slot)}"
                       f"/providers/Microsoft.KeyVault/vaults/{name}",
            "location": "westus3", "purgeProtectionEnabled": None}}

    def exists(self, slot):
        self.calls.append(("exists", slot))
        return self.present[slot]

    def show(self, slot):
        self.calls.append(("show", slot))
        return {"id": self.scope.group_id(slot), "name": self.scope.group(slot), "location": "westus3"}

    def create(self, slot, location):
        pytest.fail("A persistent group must never be created.")

    def delete(self, slot):
        pytest.fail("A persistent group must never be deleted.")

    def resources(self, slot):
        self.calls.append(("resources", slot))
        if slot in self.list_errors:
            raise self.list_errors[slot]
        return [dict(item) for item in self.items[slot]]

    def deployments(self, slot):
        self.calls.append(("deployments", slot))
        return copy.deepcopy(self.deployment_rows[slot])

    def delete_resource(self, slot, item):
        self.calls.append(("delete-resource", item["name"]))
        if self.failures.get(item["name"]):
            self.failures[item["name"]] -= 1
            raise FleetError("provider-request-failed")
        if item["name"] in self.sticky:
            return
        self.items[slot] = [value for value in self.items[slot] if value["id"] != item["id"]]
        if item["type"] == "Microsoft.KeyVault/vaults":
            self.soft_delete(slot, item["name"])

    def deleted_vault(self, slot, name):
        self.calls.append(("deleted", name))
        return self.deleted_vaults.get(name)

    def purge_vault(self, slot, name, location):
        self.calls.append(("purge", name))
        if name in self.purge_errors:
            raise FleetError("provider-request-failed")
        del self.deleted_vaults[name]


EXISTING = (("Microsoft.Storage/storageAccounts", "operatorstorage"), ("Microsoft.KeyVault/vaults", "kvoperator"))
CREATED = (("Microsoft.IoTOperations/instances", "aio-run"), ("Microsoft.DeviceRegistry/schemaRegistries", "sr-run"),
           ("Microsoft.ManagedIdentity/userAssignedIdentities", "mi-run"))


def created_by_run(scope, groups):
    for slot in scope.slots:
        groups.add(slot, "Microsoft.Kubernetes/connectedClusters", scope.cluster(slot))
        for kind, name in CREATED:
            groups.add(slot, kind, name)
    if scope.kind == "site":
        groups.add("existing", "Microsoft.KeyVault/vaults", scope.vault("existing"))


def test_persistent_scope_uses_supplied_groups_and_run_bound_names():
    site, fleet = persistent_scope(), persistent_scope("fleet")
    assert site.group("existing") == "rg-PersistentSite.one" and site.mode == "persistent"
    assert [fleet.group(slot) for slot in SLOTS] == list(PERSISTENT["fleet"])
    assert site.cluster("existing") == persistent_scope().cluster("existing")
    assert site.cluster("existing") != site_scope().cluster("existing") and site.key != site_scope().key
    assert site.cluster("existing").startswith("arc-siteops-site-")
    assert site.key != persistent_scope(groups=("rg-other",)).key
    for groups in (("rg one",), ("rg-ends.",), ("",), ("rg-" + "x" * 90,), ("a", "b"), ["rg"]):
        with pytest.raises(FleetError, match="invalid-scope"):
            site_scope(groups=groups)
    for groups in (("rg-one",), ("rg-one", "RG-ONE")):
        with pytest.raises(FleetError, match="invalid-scope"):
            persistent_scope("fleet", groups=groups)


@pytest.mark.parametrize("kind", ["site", "fleet"])
@pytest.mark.parametrize("fault", [None, "missing", "instance", "cluster-name", "vault-name"])
def test_persistent_preflight_commits_a_private_snapshot_and_refuses_collisions(kind, fault):
    scope = persistent_scope(kind)
    groups = PersistentGroups(scope, EXISTING)
    slot = scope.slots[-1]
    if fault == "missing":
        groups.present[slot] = False
    elif fault == "instance":
        groups.add(slot, "Microsoft.IoTOperations/instances", "operator-instance")
    elif fault == "cluster-name":
        groups.add(slot, "Microsoft.Kubernetes/connectedClusters", scope.cluster(slot).upper())
    elif fault == "vault-name" and kind == "site":
        groups.add(slot, "Microsoft.KeyVault/vaults", scope.vault(slot))
    if fault in {"missing", "instance", "cluster-name"} or (fault == "vault-name" and kind == "site"):
        with pytest.raises(FleetError, match="persistent-group-missing" if fault == "missing"
                           else "persistent-group-occupied"):
            preflight(scope, groups)
        return
    lease = preflight(scope, groups)
    public = json.dumps(lease)
    for slot in scope.slots:
        row = lease["slots"][slot]
        assert row["admittedAbsent"] is False and len(row["snapshot"]) == len(EXISTING)
        assert row["ownerSha256"] == scope.snapshot_commitment(slot, row["snapshot"])
    for value in (*scope.groups, "operatorstorage", "kvoperator", SUBSCRIPTION, scope.cluster(scope.slots[0])):
        assert value not in public
    assert validate_ownership(scope, lease) == lease["slots"]
    assert {call[0] for call in groups.calls} == {"exists", "show", "resources"}


@pytest.mark.parametrize("change", ["snapshot", "ephemeral-scope", "other-groups", "ephemeral-row"])
def test_persistent_ownership_binds_its_mode_groups_and_snapshot(change):
    scope = persistent_scope("fleet")
    lease = preflight(scope, PersistentGroups(scope, EXISTING))
    selected = scope
    if change == "snapshot":
        lease["slots"]["one"]["snapshot"] = lease["slots"]["one"]["snapshot"][1:]
    elif change == "ephemeral-scope":
        selected = FleetScope(scope.repository, scope.source_commit, scope.admission_sha256,
                              scope.inventory_sha256, scope.run, scope.attempt, SUBSCRIPTION)
    elif change == "other-groups":
        selected = persistent_scope("fleet", groups=("rg-fleet-one", "rg-fleet-three"))
    else:
        lease["slots"]["two"] = {"admittedAbsent": True, "ownerSha256": "f" * 64}
    with pytest.raises(FleetError, match="ownership-context-mismatch"):
        validate_ownership(selected, lease)


@pytest.mark.parametrize("kind", ["site", "fleet"])
def test_persistent_preparation_confirms_the_groups_without_any_write(kind, tmp_path):
    scope = persistent_scope(kind)
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus2")
    assert {call[0] for call in groups.calls} <= {"exists", "show", "resources"}
    rows = validate_ownership(scope, lease)
    # The installed controller checks ownership through this same call.
    assert all(scope.owns(slot, groups.show(slot), rows[slot]["ownerSha256"]) for slot in scope.slots)
    assert not scope.owns(scope.slots[0], {**groups.show(scope.slots[0]), "name": "rg-other"}, "")
    groups.present[scope.slots[0]] = False
    with pytest.raises(FleetError, match="persistent-group-missing"):
        create(scope, lease, groups, "eastus2")
    adapter = AzureGroups(scope, tmp_path / "logs", runner=lambda args: pytest.fail("A group write reached Azure."))
    for write in (lambda: adapter.create(scope.slots[0], "eastus2"), lambda: adapter.delete(scope.slots[0])):
        with pytest.raises(FleetError, match="invalid-scope"):
            write()


@pytest.mark.parametrize("kind", ["site", "fleet"])
def test_persistent_cleanup_removes_only_the_created_delta_and_confirms_absence(kind):
    scope = persistent_scope(kind)
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    created_by_run(scope, groups)
    groups.soft_delete(scope.slots[0], "kvoperator")
    groups.calls.clear()
    code, report, _ = invoke(scope, lease, groups)
    assert code == 0
    assert report["groups"] == "persistent" and report["status"] == "complete"
    assert report["slots"] == {slot: {"state": "absent", "reason": "confirmed-absent"} for slot in scope.slots}
    assert not any(action == "deployments" for action, _ in groups.calls)
    deleted = [name for operation, name in groups.calls if operation == "delete-resource"]
    assert "operatorstorage" not in deleted and "kvoperator" not in deleted
    assert deleted[0] == scope.cluster(scope.slots[0])
    for slot in scope.slots:
        assert {item["name"] for item in groups.items[slot]} == {name for _, name in EXISTING}
    assert ("purge", "kvoperator") not in groups.calls
    if kind == "site":
        assert report["kind"] == "SiteCleanup" and report["vaultPurge"] == "purged"
        assert ("purge", scope.vault("existing")) in groups.calls
    else:
        assert report["kind"] == "FleetCleanup" and "vaultPurge" not in report
    public = json.dumps(report)
    assert all(value not in public for value in (*scope.groups, scope.cluster(scope.slots[0]), "operatorstorage"))


def test_bounded_reconciliation_preserves_later_vault():
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.add("enabled", "Microsoft.Storage/storageAccounts", "createdbyattempt",
               created_time="2026-10-09T12:34:56.1234567+00:00")
    groups.add("enabled", "Microsoft.KeyVault/vaults", "kvlater",
               created_time="2026-10-09T12:40:00Z")
    groups.soft_delete("enabled", "kvlater")
    groups.calls.clear()
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert report["slots"]["enabled"] == {"state": "absent", "reason": "confirmed-absent"}
    assert ("delete-resource", "createdbyattempt") in groups.calls
    assert not any(action == "delete-resource" and name == "kvlater" for action, name in groups.calls)
    assert not any(action in {"deleted", "purge"} and name == "kvlater" for action, name in groups.calls)
    assert {item["name"] for item in groups.items["enabled"]} == {"operatorstorage", "kvoperator", "kvlater"}
    assert report["vaultPurge"] == "not-applicable"


def test_bounded_reconciliation_keeps_missing_creation_time_incomplete():
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.add("enabled", "Microsoft.Storage/storageAccounts", "createdbyattempt",
               created_time="2026-10-09T12:34:56Z")
    groups.add("enabled", "Microsoft.KeyVault/vaults", "kvunknown")
    groups.calls.clear()
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 1 and report["status"] == "incomplete"
    assert report["slots"]["enabled"] == {"state": "residual", "reason": "creation-time-unavailable"}
    assert report["vaultPurge"] == "not-attempted"
    assert [name for action, name in groups.calls if action == "delete-resource"] == ["createdbyattempt"]
    assert not any(action in {"deleted", "purge"} for action, _ in groups.calls)
    assert any(item["name"] == "kvunknown" for item in groups.items["enabled"])


@pytest.mark.parametrize("matching_group", [True, False])
def test_bounded_reconciliation_purges_unobserved_bound_vault_only_for_its_group(matching_group):
    scope = persistent_scope("site")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    name = scope.vault("existing")
    groups.soft_delete("existing", name, group=None if matching_group else "rg-other")
    groups.calls.clear()
    code, report, clock = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert report["vaultPurge"] == ("purged" if matching_group else "failed")
    assert (("purge", name) in groups.calls) is matching_group
    assert groups.calls.count(("deleted", name)) == (2 if matching_group else 1)
    assert clock.sleeps == []


def test_bounded_reconciliation_checks_missing_bound_vault_once_without_waiting():
    scope = persistent_scope("site")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.calls.clear()
    code, report, clock = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert report["vaultPurge"] == "not-applicable"
    assert groups.calls.count(("deleted", scope.vault("existing"))) == 1
    assert not any(action == "purge" for action, _ in groups.calls)
    assert clock.sleeps == []


@pytest.mark.parametrize("kind", ["site", "fleet"])
@pytest.mark.parametrize("state", ["sUcCeEdEd", "FAILED", "Canceled"])
def test_bounded_reconciliation_waits_for_deployment_and_confirms_extended_delta_gone(kind, state):
    scope = persistent_scope(kind, "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    slot = scope.slots[0]
    groups.deployment_rows[slot] = [{
        "name": "deployment-private", "timestamp": "2026-10-09T12:45:00.0000000Z",
        "duration": "PT11M", "state": "Running",
    }]
    clock = Clock()
    requested = []

    def delete_resource(selected, item):
        assert selected == slot
        requested.append((clock.now(), item["name"]))

    def sleep(duration):
        clock.sleep(duration)
        if clock.now() == 1:
            assert requested == []
            assert ("resources", slot) not in groups.calls
            groups.deployment_rows[slot][0]["state"] = state
            groups.add(slot, "Microsoft.IoTOperations/instances", "aioafterjob",
                       created_time="2026-10-09T12:43:00Z")
        else:
            groups.items[slot] = [item for item in groups.items[slot] if item["name"] != "aioafterjob"]

    groups.delete_resource = delete_resource
    groups.calls.clear()
    code, report = cleanup(
        scope, lease, groups, timeout=3, interval=1, clock=clock.now, sleep=sleep,
        created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert report["slots"][slot] == {"state": "absent", "reason": "confirmed-absent"}
    assert requested == [(1, "aioafterjob")]
    assert clock.sleeps == [1, 1]
    assert groups.calls.count(("deployments", slot)) == 3
    assert all(item["name"] != "aioafterjob" for item in groups.items[slot])


@pytest.mark.parametrize("kind", ["site", "fleet"])
def test_bounded_reconciliation_leaves_running_deployment_residual_without_deleting(kind):
    scope = persistent_scope(kind, "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    slot = scope.slots[0]
    groups.deployment_rows[slot] = [{
        "name": "deployment-private", "timestamp": "2026-10-09T12:40:00Z",
        "duration": "PT6M", "state": "rUnNiNg",
    }]
    groups.add(slot, "Microsoft.IoTOperations/instances", "aioinflight", created_time="2026-10-09T12:34:00Z")
    if kind == "fleet":
        groups.add("two", "Microsoft.Storage/storageAccounts", "completedstorage",
                   created_time="2026-10-09T12:34:00Z")
    groups.calls.clear()
    code, report, clock = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 1 and report["status"] == "incomplete"
    assert report["slots"][slot] == {"state": "residual", "reason": "deployment-running"}
    assert ("delete-resource", "aioinflight") not in groups.calls
    assert ("resources", slot) not in groups.calls
    assert groups.calls.count(("deployments", slot)) == 4
    assert clock.sleeps == [1, 1, 1]
    if kind == "fleet":
        assert report["slots"]["two"]["state"] == "absent"
        assert ("delete-resource", "completedstorage") in groups.calls
    else:
        assert report["vaultPurge"] == "not-attempted"


@pytest.mark.parametrize("state", ["Succeeded", "Running"])
def test_bounded_reconciliation_ignores_deployment_started_after_original_window(state):
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.deployment_rows["enabled"] = [{
        "name": "deployment-private", "timestamp": "2026-10-09T12:55:00Z",
        "duration": "PT17M", "state": state,
    }]
    groups.add("enabled", "Microsoft.IoTOperations/instances", "aiolater", created_time="2026-10-09T12:43:00Z")
    groups.calls.clear()
    code, report, clock = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert report["slots"]["enabled"] == {"state": "absent", "reason": "confirmed-absent"}
    assert not any(action == "delete-resource" for action, _ in groups.calls)
    assert groups.calls.count(("deployments", "enabled")) == 1
    assert any(item["name"] == "aiolater" for item in groups.items["enabled"])
    assert clock.sleeps == []


@pytest.mark.parametrize(("timestamp", "duration"), [
    ("2026-10-09T12:45:00Z", "PT8M"),
    ("2026-10-09T12:45:00.1234567+00:00", "PT8M0.1234567S"),
    ("2026-10-09T14:45:00+02:00", "PT8M"),
])
def test_bounded_reconciliation_includes_deployment_at_clock_tolerance(timestamp, duration):
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.deployment_rows["enabled"] = [{
        "name": "deployment-private", "timestamp": timestamp, "duration": duration, "state": "Succeeded",
    }]
    groups.add("enabled", "Microsoft.IoTOperations/instances", "aiodelayed", created_time="2026-10-09T12:47:00Z")
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert ("delete-resource", "aiodelayed") in groups.calls


DEPLOYMENT = {
    "name": "deployment-private", "timestamp": "2026-10-09T12:35:00Z", "duration": "PT1M", "state": "Succeeded",
}


@pytest.mark.parametrize("invalid", [
    None, False, {}, "[]", [None], [DEPLOYMENT] * 1001,
    [{**DEPLOYMENT, "extra": "private"}],
    [{key: value for key, value in DEPLOYMENT.items() if key != "duration"}],
    *([{**DEPLOYMENT, key: value}] for key in DEPLOYMENT for value in (None, 42, "")),
    *([{**DEPLOYMENT, "timestamp": value}] for value in (
        "2026-10-09T12:35:00", "2026-02-30T12:35:00Z", "2026-10-09T12:35:00+24:00", "private",
    )),
    *([{**DEPLOYMENT, "duration": value}] for value in (
        "PT", "P1D", "PT-1M", "PT1.5H", "PT1S2M", "PT1M ", "PT1.S", "PT999999999999999999999999H",
    )),
    [{**DEPLOYMENT, "timestamp": "0001-01-01T00:00:00Z", "duration": "PT1S"}],
])
def test_bounded_reconciliation_rejects_invalid_deployment_response(tmp_path, invalid):
    scope = persistent_scope("site", "enabled")
    lease = preflight(scope, PersistentGroups(scope, EXISTING))
    calls = []

    def runner(args):
        calls.append(args[1:4])
        if args[1:3] == ["group", "exists"]:
            return 0, b"true", b""
        if args[1:4] == ["deployment", "group", "list"]:
            return 0, json.dumps(invalid).encode(), b"private"
        return 0, b"[]", b""

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 1 and report["status"] == "incomplete"
    assert report["slots"]["enabled"] == {"state": "unknown", "reason": "invalid-deployment-response"}
    assert report["vaultPurge"] == "not-attempted"
    assert calls == [["group", "exists", "--subscription"], ["deployment", "group", "list"]]
    assert all(value not in json.dumps(report) for value in (*scope.groups, DEPLOYMENT["name"]))


def test_bounded_reconciliation_rejects_malformed_deployment_json(tmp_path):
    scope = persistent_scope("site", "enabled")
    lease = preflight(scope, PersistentGroups(scope, EXISTING))

    def runner(args):
        return 0, b"true" if args[1:3] == ["group", "exists"] else b"{invalid", b"private"

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 1 and report["status"] == "incomplete"
    assert report["slots"]["enabled"] == {"state": "unknown", "reason": "invalid-deployment-response"}


@pytest.mark.parametrize("duration", ["PT1M23.4567S", "PT45S", "PT2H3M", "PT0S"])
def test_deployment_adapter_validates_timing_and_uses_private_group_query(tmp_path, duration):
    scope = persistent_scope("site", "enabled")
    listing = [{**DEPLOYMENT, "timestamp": "2026-10-09T14:35:00.1234567+02:00", "duration": duration}]
    calls = []

    def runner(args):
        calls.append(args)
        return 0, json.dumps(listing).encode(), b"private"

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    assert groups.deployments("enabled") == listing
    assert calls == [[
        "az", "deployment", "group", "list", "--resource-group", scope.group("enabled"),
        "--query", "[].{name:name,timestamp:properties.timestamp,duration:properties.duration,"
        "state:properties.provisioningState}",
        "--subscription", SUBSCRIPTION, "--only-show-errors", "-o", "json",
    ]]


@pytest.mark.parametrize(("created_time", "expected"), [
    ("2026-10-09T12:36:56.1234567+00:00", "absent"),
    ("2026-10-09T12:34:56Z", "absent"),
    ("2026-10-09T12:34:56.12Z", "absent"),
    ("2026-10-09T12:34:56", "residual"),
    ("garbage", "residual"),
])
def test_reconciliation_requires_aware_arm_creation_time(created_time, expected):
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.add("enabled", "Microsoft.Storage/storageAccounts", "runstorage", created_time=created_time)
    groups.calls.clear()
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert report["slots"]["enabled"]["state"] == expected
    assert code == (0 if expected == "absent" else 1)
    assert (("delete-resource", "runstorage") in groups.calls) == (expected == "absent")


def test_unbounded_persistent_cleanup_still_deletes_later_vault():
    scope = persistent_scope("site", "enabled")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    groups.add("enabled", "Microsoft.KeyVault/vaults", "kvlater",
               created_time="2026-10-09T12:40:00Z")
    groups.calls.clear()
    code, report, _ = invoke(scope, lease, groups)
    assert code == 0 and report["status"] == "complete"
    assert report["vaultPurge"] == "purged"
    assert ("delete-resource", "kvlater") in groups.calls
    assert ("purge", "kvlater") in groups.calls
    assert not any(action == "deployments" for action, _ in groups.calls)


def test_ephemeral_site_group_cleanup_ignores_creation_bound():
    scope = site_scope("enabled")
    groups = SiteGroups(scope)
    lease = preflight(scope, groups)
    create(scope, lease, groups, "eastus2")
    code, report, _ = invoke(
        scope, lease, groups, created_before=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
    )
    assert code == 0 and report["status"] == "complete"
    assert ("delete", "enabled") in groups.calls
    assert report["vaultPurge"] == "purged"


@pytest.mark.parametrize("invalid", ["2026-10-09T12:35:00Z", 1, datetime(2026, 10, 9, 12, 35)])
def test_cleanup_requires_an_aware_datetime_bound(invalid):
    scope = persistent_scope("site")
    groups = PersistentGroups(scope)
    lease = preflight(scope, groups)
    with pytest.raises(FleetError, match="invalid-cleanup-bound"):
        invoke(scope, lease, groups, created_before=invalid)


@pytest.mark.parametrize(("fault", "state", "code"), [
    ("sticky", "residual", 1), ("listing", "unknown", 1), ("group-removed", "unknown", 1),
    ("dependency", "absent", 0),
])
def test_persistent_cleanup_fails_on_residual_or_unknown_state(fault, state, code):
    scope = persistent_scope("site")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    created_by_run(scope, groups)
    if fault == "sticky":
        groups.sticky.add("aio-run")
    elif fault == "listing":
        groups.list_errors["existing"] = FleetError("provider-request-failed")
    elif fault == "group-removed":
        groups.present["existing"] = False
    else:
        groups.failures["sr-run"] = 1
    result, report, _ = invoke(scope, lease, groups, operation_exit=0)
    assert (result, report["slots"]["existing"]["state"]) == (code, state)
    assert report["vaultPurge"] == ("purged" if code == 0 else "not-attempted")
    assert report["status"] == ("complete" if code == 0 else "incomplete")


@pytest.mark.parametrize(("case", "expected"), [
    ("other-group", "failed"), ("never-created", "not-applicable"), ("bound-only-record", "purged"),
    ("purge-error", "failed"),
])
def test_persistent_vault_purge_requires_the_selected_group_and_this_attempt(case, expected):
    scope = persistent_scope("site", "enabled" if case == "never-created" else "existing")
    groups = PersistentGroups(scope, EXISTING)
    lease = preflight(scope, groups)
    slot = scope.slots[0]
    groups.add(slot, "Microsoft.Kubernetes/connectedClusters", scope.cluster(slot))
    if case == "other-group":
        groups.add(slot, "Microsoft.KeyVault/vaults", "kvcreated")
        original = groups.delete_resource

        def delete_resource(slot, item):
            original(slot, item)
            if item["name"] == "kvcreated":
                groups.soft_delete(slot, "kvcreated", group="rg-someone-else")

        groups.delete_resource = delete_resource
    elif case == "bound-only-record":
        groups.soft_delete(slot, scope.vault(slot))
    elif case == "purge-error":
        groups.add(slot, "Microsoft.KeyVault/vaults", "kvcreated")
        groups.purge_errors.add("kvcreated")
    groups.soft_delete(slot, "kvoperator")
    code, report, _ = invoke(scope, lease, groups)
    assert code == 0 and report["status"] == "complete"
    assert report["vaultPurge"] == expected
    purged = {name for operation, name in groups.calls if operation == "purge"}
    assert "kvoperator" not in purged and ("deleted", "kvoperator") not in groups.calls
    if case in {"other-group", "never-created"}:
        assert not purged


def test_resource_adapter_stays_inside_the_selected_group(tmp_path):
    scope = persistent_scope("site")
    group = scope.group_id("existing")
    calls = []
    listing = [{"id": f"{group}/providers/Microsoft.Kubernetes/connectedClusters/arc", "type":
                "Microsoft.Kubernetes/connectedClusters", "name": "arc", "createdTime": None}]

    def runner(args):
        calls.append(args)
        if args[1:3] == ["resource", "list"]:
            return 0, json.dumps(listing).encode(), b"private%0A\\r"
        return 0, b"", b""

    groups = AzureGroups(scope, tmp_path / "logs", runner=runner)
    assert groups.resources("existing") == listing
    groups.delete_resource("existing", listing[0])
    groups.delete_resource("existing", {"id": f"{group}/providers/Microsoft.Storage/storageAccounts/s",
                                        "type": "Microsoft.Storage/storageAccounts", "name": "s"})
    assert calls == [
        ["az", "resource", "list", "--resource-group", scope.group("existing"), "--query",
         "[].{id:id,type:type,name:name,createdTime:createdTime}", "--subscription", SUBSCRIPTION,
         "--only-show-errors", "-o", "json"],
        ["az", "rest", "--method", "DELETE", "--uri", listing[0]["id"] + "?api-version=2024-01-01",
         "--only-show-errors"],
        ["az", "resource", "delete", "--ids", f"{group}/providers/Microsoft.Storage/storageAccounts/s",
         "--no-wait", "--only-show-errors", "-o", "none"],
    ]
    for identity in (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-other/providers/X/y/z",
                     f"{group}/providers/X/y/z?api-version=1", f"{group}/providers/X/../../rg-other/y"):
        with pytest.raises(FleetError, match="invalid-resource-response"):
            groups.delete_resource("existing", {"id": identity, "type": "X/y", "name": "z"})
    assert len(calls) == 3
    outside = [{"id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-other/providers/X/y/z", "type": "X/y",
                "name": "z", "createdTime": None}]
    with pytest.raises(FleetError, match="invalid-resource-response"):
        AzureGroups(scope, tmp_path / "other", runner=lambda args: (0, json.dumps(outside).encode(), b"")).resources(
            "existing")


@pytest.mark.parametrize("invalid", [False, 42, [], {}])
def test_resource_adapter_rejects_non_string_creation_time(tmp_path, invalid):
    scope = persistent_scope("site")
    listing = [{"id": f"{scope.group_id('existing')}/providers/Microsoft.Storage/storageAccounts/s",
                "type": "Microsoft.Storage/storageAccounts", "name": "s", "createdTime": invalid}]
    adapter = AzureGroups(scope, tmp_path / "logs", runner=lambda _: (0, json.dumps(listing).encode(), b""))
    with pytest.raises(FleetError, match="invalid-resource-response"):
        adapter.resources("existing")


@pytest.mark.parametrize(("kind", "secret"), [("site", "rg-PersistentSite.one"), ("fleet", "rg-fleet-one,rg-fleet-two")])
def test_persistent_management_entrypoint_never_writes_groups_or_publishes_their_names(
    tmp_path, monkeypatch, capsys, kind, secret,
):
    spec = importlib.util.spec_from_file_location(f"manage_persistent_{kind}",
                                                  ROOT / "scripts" / "manage-release-fleet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({
        "apiVersion": "siteops.release.acceptance/v1", "kind": "CandidateInputAdmission", "status": "admitted",
        "source": {"repository": "example/repository", "commit": "a" * 40, "ref": "refs/heads/main"},
        "run": 40, "attempt": 1, "caller": ".github/workflows/release.yaml", "preview": False,
        "artifacts": {"plan": 11, "inventory": 12, "payload": 13}, "planSha256": "d" * 64, "inventorySha256": "c" * 64,
        "subjects": {"engine": 4, "workspace": 1}, "installation": "not-run", "deployment": "not-run",
    }))
    admission_sha = hashlib.sha256(admission.read_bytes()).hexdigest()
    for key, value in {"GITHUB_REPOSITORY": "example/repository", "FLEET_RUN_ID": "42", "FLEET_RUN_ATTEMPT": "3",
                       "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION, "E2E_SITE_RESOURCE_GROUP": "",
                       "E2E_FLEET_RESOURCE_GROUPS": "",
                       ("E2E_SITE_RESOURCE_GROUP" if kind == "site" else "E2E_FLEET_RESOURCE_GROUPS"): secret}.items():
        monkeypatch.setenv(key, value)
    selected = []

    def groups(scope, _logs, *, owners=None):
        assert scope.mode == "persistent" and owners is None
        if not selected:
            selected.append(PersistentGroups(scope, EXISTING))
        assert selected[0].scope == scope
        return selected[0]

    monkeypatch.setattr(module, "AzureGroups", groups)
    monkeypatch.setattr(module, "cleanup", lambda *args, **kwargs: cleanup(
        *args, **kwargs, timeout=3, interval=1, clock=lambda: 0, sleep=lambda _: None))
    ownership, allocation = tmp_path / "ownership.json", tmp_path / "allocation.json"
    scope_args = ["--kind", "site", "--slot", "existing"] if kind == "site" else []
    for operation in ("preflight", "create", "cleanup"):
        output = ownership if operation == "preflight" else tmp_path / f"{operation}.json"
        args = ["manage-release-fleet.py", operation, *scope_args, "--admission", str(admission),
                "--expected-admission-sha", admission_sha, "--output", str(output),
                "--private-logs", str(tmp_path / f"{operation}-logs")]
        if operation != "preflight":
            args += ["--execute", "--ownership", str(ownership),
                     "--expected-ownership-sha", hashlib.sha256(ownership.read_bytes()).hexdigest()]
        if operation == "create":
            args += ["--location", "eastus2"]
        if operation != "cleanup":
            args += ["--allocation-state", str(allocation)]
        else:
            created_by_run(selected[0].scope, selected[0])
        monkeypatch.setattr(sys, "argv", args)
        assert module.main() == 0, operation
    assert not any(name == "aio-run" for slot in selected[0].items for name in
                   (item["name"] for item in selected[0].items[slot]))
    assert json.loads(allocation.read_text())["owners"] is None
    assert json.loads((tmp_path / "create.json").read_text())["status"] == "confirmed"
    report = json.loads((tmp_path / "cleanup.json").read_text())
    assert report["groups"] == "persistent" and report["status"] == "complete"
    captured = capsys.readouterr()
    public = captured.out + captured.err + ownership.read_text() + json.dumps(report)
    assert all(name not in public for name in secret.split(","))
    monkeypatch.setenv("E2E_SITE_RESOURCE_GROUP" if kind == "site" else "E2E_FLEET_RESOURCE_GROUPS",
                       "rg-private-marker one")
    monkeypatch.setattr(sys, "argv", ["manage-release-fleet.py", "preflight", *scope_args, "--admission",
                                      str(admission), "--expected-admission-sha", admission_sha, "--output",
                                      str(tmp_path / "again.json"), "--private-logs", str(tmp_path / "again-logs"),
                                      "--allocation-state", str(tmp_path / "again-allocation.json")])
    assert module.main() == 1
    assert "private-marker" not in capsys.readouterr().err

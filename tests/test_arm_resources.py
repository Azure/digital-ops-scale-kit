"""Read-only ARM observation contract and controlled Azure CLI adapter tests."""

import ctypes
import io
import json
import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from siteops import arm_resources, arm_resources_azure_cli
from siteops.arm_resources import (
    ArmResourceError,
    ArmResourceObservation,
    ArmResourceReader,
    new_arm_reader,
    parse_arm_resource_id,
    validate_arm_observation,
)
from siteops.compilation import VersionProvenance
from siteops.planning import CapabilityProviderIdentity
from siteops.process_args import prepare_process_args

RESOURCE_ID = (
    "/subscriptions/12345678-1234-1234-1234-123456789abc/resourceGroups/group-1/"
    "providers/Microsoft.Kubernetes/connectedClusters/cluster-1"
)
TYPE = "Microsoft.Kubernetes/connectedClusters"
VERSION = "2024-07-15-preview"
ISSUER = "https://private-issuer.example.invalid/tenant/secret"


def reference(resource_id=RESOURCE_ID, resource_type=TYPE):
    return parse_arm_resource_id(resource_id, expected_type=resource_type, api_version=VERSION)


def response(ref=None, **changes):
    ref = ref or reference()
    document = {
        "id": ref.resource_id,
        "type": ref.resource_type,
        "name": ref.name,
        "location": "eastus",
        "properties": {},
    }
    document.update(changes)
    return json.dumps(document).encode("utf-8")


def assert_safe(error, *private_values):
    assert isinstance(error.value, ArmResourceError)
    for value in private_values:
        if value:
            assert value not in str(error.value)
            assert value not in repr(error.value)


def test_parse_resource_id_is_bounded_and_retains_independent_references():
    first = reference()
    other = reference(
        "/SUBSCRIPTIONS/abcdefab-1234-5678-aaaa-000000000001/"
        "resourcegroups/Second_group/providers/Microsoft.HybridCompute/machines/Other.vm",
        "microsoft.hybridcompute/MACHINES",
    )
    assert (first.subscription, first.resource_group, first.resource_type, first.name) == (
        "12345678-1234-1234-1234-123456789abc",
        "group-1",
        TYPE,
        "cluster-1",
    )
    assert other.subscription == "abcdefab-1234-5678-aaaa-000000000001"
    assert other.resource_group == "Second_group"
    assert other.resource_type == "microsoft.hybridcompute/MACHINES"
    assert other.name == "Other.vm"
    assert first.resource_id not in repr(first)
    assert other.resource_id not in repr(other)
    assert first != other


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/subscriptions/not-a-guid/resourceGroups/rg/providers/Microsoft.Kubernetes/"
        "connectedClusters/cluster",
        RESOURCE_ID + "/childType/child",
        RESOURCE_ID + "/providers/Other.Provider/extensions/ext",
        RESOURCE_ID + "/",
        RESOURCE_ID.replace("/resourceGroups/", "/resourceGroup/"),
        RESOURCE_ID.replace("/providers/", "/missing/"),
        RESOURCE_ID.replace("/cluster-1", "/cluster%2Fone"),
        RESOURCE_ID.replace("/cluster-1", '/cluster"one'),
        RESOURCE_ID + "?api-version=latest",
        RESOURCE_ID + "#anchor",
        RESOURCE_ID + "\n",
        RESOURCE_ID.replace("group-1", "group one"),
        RESOURCE_ID.replace("cluster-1", "clu\\ster"),
        RESOURCE_ID.replace("Microsoft.Kubernetes", "Microsoft"),
        RESOURCE_ID.replace("connectedClusters", "connected-Clusters"),
        RESOURCE_ID.replace("Kubernetes", "Kubernetes"),
        RESOURCE_ID + "a" * 513,
    ],
)
def test_invalid_id_fails_without_echoing_input(value):
    with pytest.raises(ArmResourceError) as error:
        reference(value)
    assert error.value.code == "INVALID_ID"
    assert_safe(error, value, "12345678-1234-1234-1234-123456789abc")


@pytest.mark.parametrize("expected", ["Microsoft.Kubernetes", "Microsoft./clusters", "x y/t"])
def test_invalid_expected_type_fails_before_side_effect(expected):
    with pytest.raises(ArmResourceError) as error:
        reference(RESOURCE_ID, expected)
    assert error.value.code in {"INVALID_TYPE", "TYPE_MISMATCH"}
    assert_safe(error, RESOURCE_ID)


@pytest.mark.parametrize(
    "api_version",
    [
        "",
        "latest",
        "2024-7-15",
        "2024-13-01",
        "2024-02-30",
        "2024-07-15-preview-extra",
        "2024-07-15-preview?scope=secret",
        "2024-07-15 ",
        "0000-01-01",
    ],
)
def test_invalid_api_version_fails_without_echoing_value(api_version):
    with pytest.raises(ArmResourceError) as error:
        parse_arm_resource_id(RESOURCE_ID, expected_type=TYPE, api_version=api_version)
    assert error.value.code == "INVALID_API_VERSION"
    assert_safe(error, RESOURCE_ID, api_version)


def test_real_calendar_leap_date_is_a_valid_pinned_api_version():
    ref = parse_arm_resource_id(RESOURCE_ID, expected_type=TYPE, api_version="2024-02-29")
    assert ref.api_version == "2024-02-29"


def test_type_mismatch_is_distinct_from_invalid_id():
    with pytest.raises(ArmResourceError) as error:
        reference(RESOURCE_ID, "Microsoft.HybridCompute/machines")
    assert error.value.code == "TYPE_MISMATCH"
    assert_safe(error, RESOURCE_ID)


def test_directly_constructed_reference_cannot_bypass_resource_id_validation():
    ref = reference()
    with pytest.raises(ArmResourceError) as error:
        replace(ref, subscription="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert error.value.code == "INVALID_ID"
    assert_safe(error, RESOURCE_ID, ref.subscription)


def test_observation_validates_identity_location_and_canonical_name():
    ref = reference()
    observation = ArmResourceObservation(
        RESOURCE_ID.upper().replace("RESOURCEGROUPS", "resourceGroups"),
        "microsoft.kubernetes/CONNECTEDCLUSTERS",
        "East US",
        "CLUSTER-1",
        {},
    )
    assert validate_arm_observation(ref, observation) is None
    assert RESOURCE_ID not in repr(observation)


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_id": RESOURCE_ID.replace("cluster-1", "cluster-2")},
        {"resource_type": "Microsoft.HybridCompute/machines"},
        {"location": ""},
        {"location": "East\nUS"},
        {"name": "cluster-2"},
    ],
)
def test_observation_rejects_mismatched_identity_or_missing_location(changes):
    ref = reference()
    observation = ArmResourceObservation(
        **dict(
            resource_id=ref.resource_id,
            resource_type=TYPE,
            location="eastus",
            name=ref.name,
            facts={},
        )
        | changes
    )
    with pytest.raises(ArmResourceError) as error:
        validate_arm_observation(ref, observation)
    assert error.value.code == "INVALID_OBSERVATION"
    assert_safe(error, RESOURCE_ID, ref.subscription)


def test_fact_mapping_is_closed_boolean_and_not_mutable():
    original = {"connectedClusters.oidcIssuerAvailable": True}
    observation = ArmResourceObservation(RESOURCE_ID, TYPE, "eastus", "cluster-1", original)
    original["connectedClusters.oidcIssuerAvailable"] = False
    assert observation.facts["connectedClusters.oidcIssuerAvailable"] is True
    with pytest.raises(TypeError):
        observation.facts["connectedClusters.oidcIssuerAvailable"] = False
    for facts in ({"privateIssuer": True}, {"connectedClusters.oidcIssuerAvailable": ISSUER}):
        with pytest.raises(ArmResourceError) as error:
            ArmResourceObservation(RESOURCE_ID, TYPE, "eastus", "cluster-1", facts)
        assert_safe(error, ISSUER, RESOURCE_ID)


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, running=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.running = running
        self.stopped = False
        self.pid = 12345

    def poll(self):
        return None if self.running and not self.stopped else self.returncode

    def wait(self, timeout=None):
        if self.running and not self.stopped:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True


@pytest.fixture
def controlled_process(monkeypatch, tmp_path):
    calls = []
    processes = []
    tool = tmp_path / ("az.cmd" if os.name == "nt" else "az")
    monkeypatch.setattr(
        arm_resources_azure_cli,
        "resolve_tool_from_path",
        lambda name: str(tool) if name == "az" else None,
    )

    def blocked(*_args, **_kwargs):
        raise AssertionError("unexpected real process")

    monkeypatch.setattr(arm_resources_azure_cli.subprocess, "Popen", blocked)

    def run(process):
        def launch(argv, **kwargs):
            calls.append((argv, kwargs))
            processes.append(process)
            return process

        monkeypatch.setattr(arm_resources_azure_cli.subprocess, "Popen", launch)
        return process

    class FakeGroup:
        active_processes = 0

        def assign_and_resume(self, _process):
            self.active_processes = 1 if _process.poll() is None else 0

        def is_done(self, process):
            return self.active_processes == 0

        def stop(self, process):
            process.kill()
            self.active_processes = 0

        def close(self):
            pass

    # Mock both platform supervisors: tests must never signal an actual PID.
    group = "_WindowsJob" if os.name == "nt" else "_PosixProcessGroup"
    monkeypatch.setattr(arm_resources_azure_cli, group, FakeGroup)

    return run, calls, processes, tool


def test_cli_reads_fixed_argv_from_absolute_path_without_account_mutation(controlled_process):
    run, calls, processes, tool = controlled_process
    ref = reference()
    run(FakeProcess(response(ref)))
    result = arm_resources_azure_cli.AzureCliArmReader().read(ref)

    expected = [
        str(tool),
        "resource",
        "show",
        "--ids",
        ref.resource_id,
        "--api-version",
        VERSION,
        "--subscription",
        ref.subscription,
        "--output",
        "json",
        "--only-show-errors",
    ]
    assert Path(str(tool)).is_absolute()
    assert calls == [(prepare_process_args(expected), calls[0][1])]
    options = calls[0][1]
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.PIPE
    assert options["shell"] is False
    if os.name == "nt":
        assert options["creationflags"] == arm_resources_azure_cli._CREATE_SUSPENDED
    else:
        assert options["start_new_session"] is True
    assert "login" not in str(calls) and "account" not in str(calls)
    assert result.location == "eastus" and result.name == ref.name
    assert result.facts == {}
    assert ref.resource_id not in repr(result)
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_each_cli_read_selects_its_own_subscription(controlled_process):
    run, calls, _, _ = controlled_process
    first = reference()
    second = reference(
        RESOURCE_ID.replace(first.subscription, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    )
    reader = arm_resources_azure_cli.AzureCliArmReader()
    for ref in (first, second):
        run(FakeProcess(stdout=response(ref)))
        validate_arm_observation(ref, reader.read(ref))
    assert len(calls) == 2
    for (argv, options), ref in zip(calls, (first, second), strict=True):
        assert options["stdin"] is subprocess.DEVNULL
        assert argv == prepare_process_args(
            [
                str(controlled_process[3]),
                "resource",
                "show",
                "--ids",
                ref.resource_id,
                "--api-version",
                VERSION,
                "--subscription",
                ref.subscription,
                "--output",
                "json",
                "--only-show-errors",
            ]
        )


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (b"(ResourceNotFound) The resource was not found", "NOT_FOUND"),
        (b"status code: 404", "NOT_FOUND"),
        (b"(AuthorizationFailed) status code: 403", "FORBIDDEN"),
        (b"Please run 'az login' to setup account.", "NOT_LOGGED_IN"),
        (b"(SubscriptionNotFound) Subscription not found", "SUBSCRIPTION_MISSING"),
        (b"An unexpected error occurred", "FAILED"),
    ],
)
def test_cli_errors_are_closed_without_stderr_or_input_values(controlled_process, stderr, code):
    run, _, _, _ = controlled_process
    private = f" {RESOURCE_ID} secret=credential tenant=very-private".encode()
    run(FakeProcess(stderr=stderr + private, returncode=1))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == code
    assert_safe(error, RESOURCE_ID, "credential", "very-private", "12345678-1234-1234-1234")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"", "INVALID_OBSERVATION"),
        (b"not json" + ISSUER.encode(), "INVALID_OBSERVATION"),
        (b"[]", "INVALID_OBSERVATION"),
        (response(location=None), "INVALID_OBSERVATION"),
        (response(id=RESOURCE_ID.replace("cluster-1", "cluster-other")), "INVALID_OBSERVATION"),
        (response(type="Microsoft.HybridCompute/machines"), "INVALID_OBSERVATION"),
    ],
)
def test_cli_rejects_malformed_or_mismatched_response(controlled_process, body, code):
    run, _, _, _ = controlled_process
    run(FakeProcess(stdout=body))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == code
    assert_safe(error, RESOURCE_ID, ISSUER)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            b'{"id":"'
            + RESOURCE_ID.encode()
            + b'","id":"'
            + RESOURCE_ID.encode()
            + b'","type":"'
            + TYPE.encode()
            + b'","name":"cluster-1","location":"eastus"}',
            id="duplicate-identity",
        ),
        pytest.param(
            b'{"nested":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}",
            id="nested-over-limit",
        ),
    ],
)
def test_ambiguous_or_excessively_nested_json_fails_closed(controlled_process, body):
    run, _, _, _ = controlled_process
    run(FakeProcess(stdout=body))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "INVALID_OBSERVATION"
    assert_safe(error, RESOURCE_ID)


def test_cli_bounds_output_and_stops_running_process(controlled_process, monkeypatch):
    run, _, _, _ = controlled_process
    monkeypatch.setattr(arm_resources_azure_cli, "_MAX_RESPONSE_BYTES", 32)
    process = run(FakeProcess(stdout=b"x" * 1024, running=True))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "RESPONSE_LIMIT"
    assert process.stopped
    assert_safe(error, RESOURCE_ID)


def test_cli_bounds_stderr_even_on_success(controlled_process, monkeypatch):
    run, _, _, _ = controlled_process
    monkeypatch.setattr(arm_resources_azure_cli, "_MAX_STDERR_BYTES", 16)
    process = run(FakeProcess(stdout=response(), stderr=b"z" * 1024, running=True))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "RESPONSE_LIMIT"
    assert process.stopped


def test_cli_deadline_stops_process_without_returning_live_child(controlled_process, monkeypatch):
    run, _, _, _ = controlled_process
    monkeypatch.setattr(arm_resources_azure_cli, "_CLI_TIMEOUT_SECONDS", 0)
    process = run(FakeProcess(running=True))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "TIMEOUT"
    assert process.stopped
    assert_safe(error, RESOURCE_ID)


@pytest.mark.parametrize(
    "running",
    [
        pytest.param(True, id="timeout-with-live-process"),
        pytest.param(False, id="valid-json-with-live-grandchild"),
    ],
)
def test_failed_tree_cleanup_overrides_success_or_timeout(controlled_process, monkeypatch, running):
    run, _, _, _ = controlled_process
    process = run(FakeProcess(stdout=response() if not running else b"", running=running))

    class UnstoppableGroup:
        attempted = False
        closed = False

        def assign_and_resume(self, _process):
            pass

        def is_done(self, _process):
            return False

        def stop(self, _process):
            self.attempted = True
            raise ArmResourceError("FAILED")

        def close(self):
            if self.closed:
                return
            assert not process.stdout.closed and not process.stderr.closed
            self.closed = True
            process.kill()

    group = UnstoppableGroup()
    monkeypatch.setattr(
        arm_resources_azure_cli,
        "_WindowsJob" if os.name == "nt" else "_PosixProcessGroup",
        lambda: group,
    )
    monkeypatch.setattr(arm_resources_azure_cli, "_CLI_TIMEOUT_SECONDS", 0 if running else 0.25)
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "FAILED"
    assert group.attempted and group.closed and process.stopped
    assert process.stdout.closed and process.stderr.closed
    assert_safe(error, RESOURCE_ID)


def test_exited_launcher_with_drained_pipes_stops_remaining_job_child(
    controlled_process, monkeypatch
):
    run, _, _, _ = controlled_process
    process = run(FakeProcess(stdout=response(), returncode=0))

    class ChildGroup:
        child_stopped = False
        stop_calls = 0

        def assign_and_resume(self, _process):
            pass

        def is_done(self, _process):
            return self.child_stopped

        def stop(self, _process):
            self.child_stopped = True
            self.stop_calls += 1

        def close(self):
            pass

    group = ChildGroup()
    monkeypatch.setattr(
        arm_resources_azure_cli,
        "_WindowsJob" if os.name == "nt" else "_PosixProcessGroup",
        lambda: group,
    )
    monkeypatch.setattr(arm_resources_azure_cli, "_CLI_TIMEOUT_SECONDS", 0.25)
    result = arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert result.name == "cluster-1"
    assert group.child_stopped and group.stop_calls == 1
    assert process.stdout.closed and process.stderr.closed


def test_exited_launcher_with_pipe_held_by_child_still_times_out(controlled_process, monkeypatch):
    run, _, _, _ = controlled_process

    class HeldPipe:
        def __init__(self):
            self.released = threading.Event()
            self.closed = False

        def read(self, _size):
            self.released.wait()
            return b""

        def close(self):
            self.released.set()
            self.closed = True

    process = FakeProcess(stdout=response(), returncode=0)
    held_pipe = HeldPipe()
    process.stdout = held_pipe
    run(process)

    class ChildGroup:
        child_stopped = False

        def assign_and_resume(self, _process):
            pass

        def is_done(self, _process):
            return self.child_stopped

        def stop(self, _process):
            self.child_stopped = True
            held_pipe.released.set()

        def close(self):
            pass

    group = ChildGroup()
    monkeypatch.setattr(
        arm_resources_azure_cli,
        "_WindowsJob" if os.name == "nt" else "_PosixProcessGroup",
        lambda: group,
    )
    monkeypatch.setattr(arm_resources_azure_cli, "_CLI_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "TIMEOUT"
    assert group.child_stopped and held_pipe.closed and process.stderr.closed


def test_interrupted_read_stops_child_tree_and_returns_safe_cancelled(
    controlled_process, monkeypatch
):
    run, _, _, _ = controlled_process

    class InterruptedProcess(FakeProcess):
        def poll(self):
            if not self.stopped:
                raise KeyboardInterrupt("synthetic private value")
            return self.returncode

    class FakeTree:
        child_stopped = False
        stopped = False
        closed = False

        def assign_and_resume(self, _process):
            pass

        def is_done(self, process):
            return process.poll() is not None

        def stop(self, process):
            process.kill()
            self.child_stopped = True
            self.stopped = True

        def close(self):
            self.closed = True

    tree = FakeTree()
    monkeypatch.setattr(
        arm_resources_azure_cli,
        "_WindowsJob" if os.name == "nt" else "_PosixProcessGroup",
        lambda: tree,
    )
    process = run(InterruptedProcess(stdout=response(), running=True))
    with pytest.raises((ArmResourceError, KeyboardInterrupt)) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert tree.stopped and tree.child_stopped and tree.closed
    assert process.stopped and process.stdout.closed and process.stderr.closed
    assert isinstance(error.value, ArmResourceError)
    assert error.value.code == "CANCELLED"
    assert_safe(error, RESOURCE_ID, ISSUER, "synthetic private value")


def test_windows_job_stops_suspended_launcher_if_assignment_fails():
    job = object.__new__(arm_resources_azure_cli._WindowsJob)

    class EmptyJob:
        def WaitForSingleObject(self, _handle, _timeout):
            return 0

    job._kernel = EmptyJob()
    job._handle = 1
    job._assigned = False
    process = FakeProcess(running=True)
    job.stop(process)
    assert process.stopped


def _set_fake_active_process_count(buffer, size, count):
    assert size == 48
    assert arm_resources_azure_cli._JobAccounting.active_processes.offset == 40
    ctypes.memset(buffer, 0, size)
    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32))[10] = count


def test_windows_job_accounting_waits_for_children_after_launcher_exits():
    job = object.__new__(arm_resources_azure_cli._WindowsJob)
    process = FakeProcess(returncode=0)

    class AccountingKernel:
        active = 1

        def QueryInformationJobObject(self, _handle, info_class, buffer, size, _returned):
            assert info_class == 1
            _set_fake_active_process_count(buffer, size, self.active)
            return True

        def WaitForSingleObject(self, _handle, _timeout):
            raise AssertionError("A job handle does not signal on normal exit.")

    job._kernel = AccountingKernel()
    job._handle = 1
    job._assigned = True
    assert not job.is_done(process)
    job._kernel.active = 0
    assert job.is_done(process)


def test_windows_job_accounting_failure_cannot_claim_completion():
    job = object.__new__(arm_resources_azure_cli._WindowsJob)

    class FailingKernel:
        def QueryInformationJobObject(self, _handle, _info_class, _buffer, _size, _returned):
            return False

    job._kernel = FailingKernel()
    job._handle = 1
    with pytest.raises(ArmResourceError) as error:
        job.is_done(FakeProcess())
    assert error.value.code == "FAILED"


def test_windows_job_terminates_assigned_tree_before_failure_returns():
    job = object.__new__(arm_resources_azure_cli._WindowsJob)
    process = FakeProcess(running=True)

    class RunningJob:
        def __init__(self):
            self.terminated = False

        def QueryInformationJobObject(self, _handle, info_class, buffer, size, _returned):
            assert info_class == 1
            _set_fake_active_process_count(buffer, size, 0 if self.terminated else 2)
            return True

        def WaitForSingleObject(self, _handle, _timeout):
            raise AssertionError("A job handle does not signal on normal exit.")

        def TerminateJobObject(self, _handle, _exit_code):
            self.terminated = True
            process.kill()
            return True

    job._kernel = RunningJob()
    job._handle = 1
    job._assigned = True
    job.stop(process)
    assert job._kernel.terminated and process.stopped


def test_windows_job_waits_even_if_termination_call_reports_failure():
    job = object.__new__(arm_resources_azure_cli._WindowsJob)
    process = FakeProcess(running=True)

    class UncertainJob:
        queries = 0

        def QueryInformationJobObject(self, _handle, info_class, buffer, size, _returned):
            assert info_class == 1
            self.queries += 1
            if self.queries >= 2:
                process.kill()
            _set_fake_active_process_count(buffer, size, 0 if process.stopped else 1)
            return True

        def WaitForSingleObject(self, _handle, _timeout):
            raise AssertionError("A job handle does not signal on normal exit.")

        def TerminateJobObject(self, _handle, _exit_code):
            return False

    job._kernel = UncertainJob()
    job._handle = 1
    job._assigned = True
    with pytest.raises(ArmResourceError) as error:
        job.stop(process)
    assert error.value.code == "FAILED"
    assert job._kernel.queries >= 2 and process.stopped


def test_windows_job_failed_termination_is_bounded_and_explicit(monkeypatch):
    monkeypatch.setattr(arm_resources_azure_cli, "_PROCESS_STOP_SECONDS", 0)
    job = object.__new__(arm_resources_azure_cli._WindowsJob)
    process = FakeProcess(running=True)

    class UnstoppableJob:
        queried = False
        termination_attempted = False

        def QueryInformationJobObject(self, _handle, info_class, buffer, size, _returned):
            assert info_class == 1
            self.queried = True
            _set_fake_active_process_count(buffer, size, 2)
            return True

        def TerminateJobObject(self, _handle, _exit_code):
            self.termination_attempted = True
            return False

        def WaitForSingleObject(self, _handle, _timeout):
            raise AssertionError("Cleanup must not wait indefinitely on a job handle.")

    job._kernel = UnstoppableJob()
    job._handle = 1
    job._assigned = True
    with pytest.raises(ArmResourceError) as error:
        job.stop(process)
    assert error.value.code == "FAILED"
    assert job._kernel.queried and job._kernel.termination_attempted


def test_posix_group_stops_only_the_selected_process_group(monkeypatch):
    process = FakeProcess(running=True)
    calls = []

    def fake_killpg(pid, signal_number):
        calls.append((pid, signal_number))
        process.kill()

    monkeypatch.setattr(arm_resources_azure_cli.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(arm_resources_azure_cli.os, "killpg", fake_killpg, raising=False)
    arm_resources_azure_cli._PosixProcessGroup().stop(process)
    assert calls == [(process.pid, arm_resources_azure_cli.signal.SIGKILL)]
    assert process.stopped


def test_missing_tool_and_start_failure_have_safe_codes(controlled_process, monkeypatch):
    run, calls, _, _ = controlled_process
    monkeypatch.setattr(arm_resources_azure_cli, "resolve_tool_from_path", lambda _name: None)
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "TOOL_MISSING"
    assert not calls
    assert_safe(error, RESOURCE_ID)

    monkeypatch.setattr(
        arm_resources_azure_cli,
        "resolve_tool_from_path",
        lambda _name: str(Path.cwd().resolve() / "az"),
    )
    monkeypatch.setattr(
        arm_resources_azure_cli.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(ISSUER)),
    )
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(reference())
    assert error.value.code == "TOOL_MISSING"
    assert_safe(error, ISSUER, RESOURCE_ID)


def test_fact_extraction_is_boolean_only_and_never_discloses_issuer(controlled_process):
    run, _, _, _ = controlled_process
    run(
        FakeProcess(
            stdout=response(
                properties={
                    "oidcIssuerProfile": {
                        "enabled": True,
                        "issuerUrl": ISSUER,
                        "selfHostedIssuerUrl": ISSUER,
                    },
                    "securityProfile": {"workloadIdentity": {"enabled": True}},
                }
            )
        )
    )
    result = arm_resources_azure_cli.AzureCliArmReader().read(
        reference(),
        facts=frozenset(
            {
                "connectedClusters.workloadIdentityEnabled",
                "connectedClusters.oidcIssuerAvailable",
            }
        ),
    )
    assert result.facts == {
        "connectedClusters.workloadIdentityEnabled": True,
        "connectedClusters.oidcIssuerAvailable": True,
    }
    assert ISSUER not in repr(result) and ISSUER not in str(result)


class FakeFutureArmReader:
    identity = CapabilityProviderIdentity(
        name="azure-sdk", version=None, version_provenance=VersionProvenance.UNKNOWN
    )

    def __init__(self, private_document):
        self._document = private_document

    def read(self, ref, *, facts=frozenset()):
        document = self._document
        observation = ArmResourceObservation(
            resource_id=document["id"],
            resource_type=document["type"],
            location=document["location"],
            name=document["name"],
            facts=arm_resources.normalize_arm_resource_facts(ref, document, facts=facts),
        )
        validate_arm_observation(ref, observation)
        return observation


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        pytest.param(
            {
                "securityProfile": {"workloadIdentity": {"enabled": False}},
                "oidcIssuerProfile": {"enabled": True, "issuerUrl": ISSUER},
            },
            (False, True),
            id="oidc-on-workload-off",
        ),
        pytest.param(
            {
                "securityProfile": {"workloadIdentity": {"enabled": True}},
                "oidcIssuerProfile": {"enabled": False, "issuerUrl": ISSUER},
            },
            (True, True),
            id="workload-on-oidc-flag-off",
        ),
        pytest.param(
            {"securityProfile": {"workloadIdentity": {"enabled": True}}},
            (True, False),
            id="no-oidc-profile",
        ),
        pytest.param(
            {"oidcIssuerProfile": {"issuerUrl": ISSUER}},
            (False, True),
            id="no-workload-profile",
        ),
        pytest.param({}, (False, False), id="no-optional-profiles"),
        pytest.param(
            {"workloadIdentityEnabled": True, "oidcIssuerUrl": ISSUER},
            (False, False),
            id="unrelated-paths-do-not-count",
        ),
    ],
)
def test_cli_and_future_provider_use_closed_identical_fact_normalization(
    controlled_process, properties, expected
):
    run, _, _, _ = controlled_process
    ref = reference()
    document = json.loads(response(properties=properties))
    original_document = json.dumps(document, sort_keys=True)
    requested = frozenset(
        {
            "connectedClusters.workloadIdentityEnabled",
            "connectedClusters.oidcIssuerAvailable",
        }
    )
    fake = FakeFutureArmReader(document)
    assert isinstance(fake, ArmResourceReader)
    run(FakeProcess(stdout=json.dumps(document).encode("utf-8")))
    cli_observation = arm_resources_azure_cli.AzureCliArmReader().read(ref, facts=requested)
    fake_observation = fake.read(ref, facts=requested)
    assert (
        cli_observation.facts
        == fake_observation.facts
        == {
            "connectedClusters.workloadIdentityEnabled": expected[0],
            "connectedClusters.oidcIssuerAvailable": expected[1],
        }
    )
    assert set(fake_observation.facts) == requested
    assert all(type(value) is bool for value in fake_observation.facts.values())
    assert json.dumps(document, sort_keys=True) == original_document
    for observation in (cli_observation, fake_observation):
        assert ISSUER not in str(observation) and ISSUER not in repr(observation)
        assert RESOURCE_ID not in repr(observation)


@pytest.mark.parametrize(
    "properties",
    [
        pytest.param({"securityProfile": ISSUER}, id="invalid-security-profile"),
        pytest.param({"securityProfile": {"workloadIdentity": ISSUER}}, id="invalid-workload"),
        pytest.param(
            {"securityProfile": {"workloadIdentity": {"enabled": "true"}}},
            id="invalid-workload-enabled",
        ),
        pytest.param({"securityProfile": {"workloadIdentity": {}}}, id="missing-enabled-field"),
        pytest.param({"oidcIssuerProfile": {"issuerUrl": [ISSUER]}}, id="invalid-issuer-url"),
        pytest.param([ISSUER], id="invalid-properties-shape"),
    ],
)
def test_cli_and_future_provider_reject_malformed_facts_without_value_leakage(
    controlled_process, properties
):
    run, _, _, _ = controlled_process
    ref = reference()
    document = json.loads(response(properties=properties))
    requested = frozenset(
        {
            "connectedClusters.workloadIdentityEnabled",
            "connectedClusters.oidcIssuerAvailable",
        }
    )
    run(FakeProcess(stdout=json.dumps(document).encode("utf-8")))
    for reader in (arm_resources_azure_cli.AzureCliArmReader(), FakeFutureArmReader(document)):
        with pytest.raises(ArmResourceError) as error:
            reader.read(ref, facts=requested)
        assert error.value.code == "INVALID_OBSERVATION"
        assert_safe(error, ISSUER, RESOURCE_ID, ref.subscription)


@pytest.mark.parametrize(
    ("ref", "requested"),
    [
        pytest.param(reference(), "connectedClusters.privateIssuer", id="unknown-fact"),
        pytest.param(
            reference(
                RESOURCE_ID.replace(TYPE, "Microsoft.HybridCompute/machines"),
                "Microsoft.HybridCompute/machines",
            ),
            "connectedClusters.oidcIssuerAvailable",
            id="wrong-resource-type",
        ),
    ],
)
def test_neutral_normalizer_rejects_unrecognized_or_wrong_type_facts(ref, requested):
    document = json.loads(response(ref, properties={"oidcIssuerProfile": {"issuerUrl": ISSUER}}))
    with pytest.raises(ArmResourceError) as error:
        arm_resources.normalize_arm_resource_facts(ref, document, facts=frozenset({requested}))
    assert error.value.code == "UNSUPPORTED_FACT"
    assert_safe(error, ISSUER, ref.resource_id, ref.subscription)


def test_cli_calls_neutral_fact_normalizer_at_read_boundary(controlled_process, monkeypatch):
    run, _, _, _ = controlled_process
    run(FakeProcess(stdout=response(properties={})))
    normalizer = arm_resources.normalize_arm_resource_facts
    calls = []

    def tracked(ref, document, *, facts):
        calls.append((ref, document, facts))
        return normalizer(ref, document, facts=facts)

    monkeypatch.setattr(arm_resources_azure_cli, "normalize_arm_resource_facts", tracked)
    requested = frozenset({"connectedClusters.oidcIssuerAvailable"})
    observation = arm_resources_azure_cli.AzureCliArmReader().read(reference(), facts=requested)
    assert observation.facts == {"connectedClusters.oidcIssuerAvailable": False}
    assert len(calls) == 1 and calls[0][2] == requested


@pytest.mark.parametrize(
    ("security_profile", "oidc_enabled", "workload_enabled"),
    [
        pytest.param({"workloadIdentity": {"enabled": False}}, True, False, id="oidc-on-wi-off"),
        pytest.param({"workloadIdentity": {"enabled": True}}, False, True, id="oidc-off-wi-on"),
        pytest.param(None, True, False, id="security-profile-absent"),
        pytest.param({}, True, False, id="workload-profile-absent"),
    ],
)
def test_workload_identity_uses_security_profile_not_oidc_profile(
    controlled_process, security_profile, oidc_enabled, workload_enabled
):
    run, _, _, _ = controlled_process
    properties = {"oidcIssuerProfile": {"enabled": oidc_enabled, "issuerUrl": ISSUER}}
    if security_profile is not None:
        properties["securityProfile"] = security_profile
    run(FakeProcess(stdout=response(properties=properties)))
    observation = arm_resources_azure_cli.AzureCliArmReader().read(
        reference(),
        facts=frozenset(
            {
                "connectedClusters.workloadIdentityEnabled",
                "connectedClusters.oidcIssuerAvailable",
            }
        ),
    )
    assert observation.facts == {
        "connectedClusters.workloadIdentityEnabled": workload_enabled,
        "connectedClusters.oidcIssuerAvailable": True,
    }
    assert ISSUER not in repr(observation) and ISSUER not in str(observation)


@pytest.mark.parametrize(
    "security_profile",
    [
        pytest.param(ISSUER, id="security-profile-string"),
        pytest.param({"workloadIdentity": ISSUER}, id="workload-profile-string"),
        pytest.param({"workloadIdentity": {"enabled": "true"}}, id="enabled-string"),
        pytest.param({"workloadIdentity": {"enabled": 1}}, id="enabled-number"),
        pytest.param({"workloadIdentity": {"enabled": None}}, id="enabled-null"),
        pytest.param({"workloadIdentity": {}}, id="enabled-missing"),
    ],
)
def test_malformed_workload_identity_is_invalid_observation_without_values(
    controlled_process, security_profile
):
    run, _, _, _ = controlled_process
    run(
        FakeProcess(
            stdout=response(
                properties={
                    "oidcIssuerProfile": {"enabled": True, "issuerUrl": ISSUER},
                    "securityProfile": security_profile,
                }
            )
        )
    )
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(
            reference(),
            facts=frozenset({"connectedClusters.workloadIdentityEnabled"}),
        )
    assert error.value.code == "INVALID_OBSERVATION"
    assert_safe(error, ISSUER, RESOURCE_ID)


def test_missing_optional_oidc_profile_means_both_facts_are_false(controlled_process):
    run, _, _, _ = controlled_process
    run(FakeProcess(stdout=response(properties={})))
    result = arm_resources_azure_cli.AzureCliArmReader().read(
        reference(),
        facts=frozenset(
            {
                "connectedClusters.workloadIdentityEnabled",
                "connectedClusters.oidcIssuerAvailable",
            }
        ),
    )
    assert result.facts == {
        "connectedClusters.workloadIdentityEnabled": False,
        "connectedClusters.oidcIssuerAvailable": False,
    }


def test_workload_identity_remains_true_without_oidc_profile(controlled_process):
    run, _, _, _ = controlled_process
    run(
        FakeProcess(
            stdout=response(properties={"securityProfile": {"workloadIdentity": {"enabled": True}}})
        )
    )
    result = arm_resources_azure_cli.AzureCliArmReader().read(
        reference(),
        facts=frozenset(
            {
                "connectedClusters.workloadIdentityEnabled",
                "connectedClusters.oidcIssuerAvailable",
            }
        ),
    )
    assert result.facts == {
        "connectedClusters.workloadIdentityEnabled": True,
        "connectedClusters.oidcIssuerAvailable": False,
    }


@pytest.mark.parametrize(
    "properties",
    [
        {"securityProfile": {"workloadIdentity": {"enabled": "true"}}},
        {"oidcIssuerProfile": {"enabled": True, "issuerUrl": [ISSUER]}},
        {
            "securityProfile": {"workloadIdentity": {"enabled": None}},
            "oidcIssuerProfile": {"issuerUrl": ISSUER},
        },
    ],
)
def test_invalid_fact_types_are_rejected_without_raw_properties(controlled_process, properties):
    run, _, _, _ = controlled_process
    run(FakeProcess(stdout=response(properties=properties)))
    with pytest.raises(ArmResourceError) as error:
        arm_resources_azure_cli.AzureCliArmReader().read(
            reference(),
            facts=frozenset(
                {
                    "connectedClusters.workloadIdentityEnabled",
                    "connectedClusters.oidcIssuerAvailable",
                }
            ),
        )
    assert error.value.code == "INVALID_OBSERVATION"
    assert_safe(error, ISSUER, RESOURCE_ID)


def test_non_cluster_or_unknown_facts_rejected_before_process(controlled_process):
    _, calls, _, _ = controlled_process
    for ref, fact in [
        (reference(RESOURCE_ID, TYPE), "connectedClusters.privateIssuer"),
        (
            reference(
                RESOURCE_ID.replace(TYPE, "Microsoft.HybridCompute/machines"),
                "Microsoft.HybridCompute/machines",
            ),
            "connectedClusters.oidcIssuerAvailable",
        ),
    ]:
        with pytest.raises(ArmResourceError) as error:
            arm_resources_azure_cli.AzureCliArmReader().read(ref, facts=frozenset({fact}))
        assert error.value.code == "UNSUPPORTED_FACT"
    assert not calls


def test_selected_provider_is_explicit_and_fake_reader_is_interchangeable(monkeypatch):
    selected = new_arm_reader()
    assert isinstance(selected, ArmResourceReader)
    assert selected.identity == CapabilityProviderIdentity(
        name="azure-cli", version=None, version_provenance=VersionProvenance.UNKNOWN
    )
    with pytest.raises(ArmResourceError) as error:
        new_arm_reader("azure-sdk")
    assert error.value.code == "UNSUPPORTED_PROVIDER"

    class FakeReader:
        identity = CapabilityProviderIdentity(
            name="fake", version=None, version_provenance=VersionProvenance.UNKNOWN
        )

        def read(self, ref, *, facts=frozenset()):
            return ArmResourceObservation(
                ref.resource_id,
                ref.resource_type,
                "westus",
                ref.name,
                {fact: False for fact in facts},
            )

    monkeypatch.setitem(arm_resources._READERS, "fake", FakeReader)
    fake = new_arm_reader("fake")
    assert isinstance(fake, ArmResourceReader)
    for ref in (
        reference(),
        reference(
            RESOURCE_ID.replace(
                "12345678-1234-1234-1234-123456789abc", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            )
        ),
    ):
        observed = fake.read(ref, facts=frozenset({"connectedClusters.oidcIssuerAvailable"}))
        validate_arm_observation(ref, observed)
        assert not observed.facts["connectedClusters.oidcIssuerAvailable"]

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Read an explicitly selected ARM resource with the existing Azure CLI session."""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from siteops.arm_resources import (
    ArmResourceError,
    ArmResourceObservation,
    ArmResourceRef,
    _validate_requested_facts,
    normalize_arm_resource_facts,
    parse_arm_resource_id,
    validate_arm_observation,
)
from siteops.compilation import VersionProvenance, resolve_tool_from_path
from siteops.planning import CapabilityProviderIdentity
from siteops.process_args import prepare_process_args
from siteops.process_capture import BoundedCapture

_CLI_TIMEOUT_SECONDS = 20.0
_PROCESS_STOP_SECONDS = 2.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STDERR_BYTES = 32 * 1024
_CREATE_SUSPENDED = 0x00000004
_ERROR_CODES = (
    (
        re.compile(
            rb"SubscriptionNotFound|InvalidSubscriptionId|subscription (?:was |is )?not found",
            re.IGNORECASE,
        ),
        "SUBSCRIPTION_MISSING",
    ),
    (
        re.compile(
            rb"az login|not logged in|not logged into|LoginRequired|"
            rb"Please run ['\"]?az login",
            re.IGNORECASE,
        ),
        "NOT_LOGGED_IN",
    ),
    (
        re.compile(
            rb"AuthorizationFailed|Forbidden|status code:\s*403|"
            rb"does not have authorization",
            re.IGNORECASE,
        ),
        "FORBIDDEN",
    ),
    (
        re.compile(
            rb"ResourceNotFound|ResourceGroupNotFound|status code:\s*404|"
            rb"\bresource (?:was |is )?not found\b",
            re.IGNORECASE,
        ),
        "NOT_FOUND",
    ),
)


class _PosixProcessGroup:
    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        pass

    def is_done(self, process: subprocess.Popen[bytes]) -> bool:
        return process.poll() is not None

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            raise ArmResourceError("FAILED") from None
        try:
            process.wait(timeout=_PROCESS_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise ArmResourceError("FAILED") from None

    def close(self) -> None:
        pass


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64),
        ("period_kernel_time", ctypes.c_int64),
        ("total_page_faults", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


class _WindowsJob:
    """Start the launcher suspended, then supervise it and its children as one job."""

    def __init__(self) -> None:
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time", ctypes.c_int64),
                ("per_job_user_time", ctypes.c_int64),
                ("limit_flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic_limits", BasicLimits),
                ("io_counters", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel = self._kernel
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ArmResourceError("FAILED")
        self._assigned = False
        limits = ExtendedLimits()
        limits.basic_limits.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ArmResourceError("FAILED")

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        if not self._kernel.AssignProcessToJobObject(self._handle, process._handle):
            raise ArmResourceError("FAILED")
        self._assigned = True

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("thread_id", wintypes.DWORD),
                ("owner_id", wintypes.DWORD),
                ("base_priority", wintypes.LONG),
                ("delta_priority", wintypes.LONG),
                ("flags", wintypes.DWORD),
            ]

        kernel = self._kernel
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise ArmResourceError("FAILED")
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            if not kernel.Thread32First(snapshot, ctypes.byref(entry)):
                raise ArmResourceError("FAILED")
            found = False
            while True:
                if entry.owner_id == process.pid:
                    found = True
                    thread = kernel.OpenThread(0x0002, False, entry.thread_id)
                    if not thread:
                        raise ArmResourceError("FAILED")
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ArmResourceError("FAILED")
                    finally:
                        kernel.CloseHandle(thread)
                if not kernel.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
            if not found:
                raise ArmResourceError("FAILED")
        finally:
            kernel.CloseHandle(snapshot)

    def is_done(self, process: subprocess.Popen[bytes]) -> bool:
        # A job handle does not signal when its processes exit normally.
        accounting = _JobAccounting()
        if not self._kernel.QueryInformationJobObject(
            self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
        ):
            raise ArmResourceError("FAILED")
        return accounting.active_processes == 0

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        if not self._assigned:
            # The launcher is still suspended, so no child can have been created.
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=_PROCESS_STOP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                raise ArmResourceError("FAILED") from None
            return
        query_failed = False
        try:
            done = self.is_done(process)
        except ArmResourceError:
            done = False
            query_failed = True
        termination_failed = not done and not self._kernel.TerminateJobObject(self._handle, 1)
        deadline = time.monotonic() + _PROCESS_STOP_SECONDS
        while True:
            try:
                done = self.is_done(process)
            except ArmResourceError:
                done = False
                query_failed = True
            if done and process.poll() is not None:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    raise ArmResourceError("FAILED") from None
                if query_failed or termination_failed:
                    raise ArmResourceError("FAILED")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArmResourceError("FAILED")
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _classify_cli_error(stderr: bytes) -> str:
    for pattern, code in _ERROR_CODES:
        if pattern.search(stderr):
            return code
    return "FAILED"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name, value in pairs:
        if name in document:
            raise ArmResourceError("INVALID_OBSERVATION")
        document[name] = value
    return document


def _run_az(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        prepared = prepare_process_args(argv)
    except (OSError, ValueError):
        raise ArmResourceError("FAILED") from None
    group = _WindowsJob() if os.name == "nt" else _PosixProcessGroup()
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "bufsize": 0,
    }
    if os.name == "nt":
        options["creationflags"] = _CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    try:
        try:
            process = subprocess.Popen(prepared, **options)
        except OSError:
            raise ArmResourceError("TOOL_MISSING") from None
        readers: list[threading.Thread] = []
        stdout = BoundedCapture.create(_MAX_RESPONSE_BYTES)
        stderr = BoundedCapture.create(_MAX_STDERR_BYTES)
        try:
            group.assign_and_resume(process)
            if process.stdout is None or process.stderr is None:
                raise ArmResourceError("FAILED")
            for capture, stream in ((stdout, process.stdout), (stderr, process.stderr)):
                reader = threading.Thread(target=capture.read, args=(stream,), daemon=True)
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + _CLI_TIMEOUT_SECONDS
            while True:
                if stdout.exceeded.is_set() or stderr.exceeded.is_set():
                    raise ArmResourceError("RESPONSE_LIMIT")
                if stdout.failed.is_set() or stderr.failed.is_set():
                    raise ArmResourceError("FAILED")
                if time.monotonic() >= deadline:
                    raise ArmResourceError("TIMEOUT")
                # A descendant may keep the job active without holding either output pipe.
                if all(not reader.is_alive() for reader in readers) and process.poll() is not None:
                    break
                time.sleep(0.01)
            if stdout.exceeded.is_set() or stderr.exceeded.is_set():
                raise ArmResourceError("RESPONSE_LIMIT")
            if stdout.failed.is_set() or stderr.failed.is_set():
                raise ArmResourceError("FAILED")
            return_code = process.poll()
            if return_code is None:
                raise ArmResourceError("FAILED")
            return return_code, bytes(stdout.content), bytes(stderr.content)
        finally:
            try:
                group.stop(process)
            finally:
                group.close()
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                for reader in readers:
                    reader.join(timeout=_PROCESS_STOP_SECONDS)
                if any(reader.is_alive() for reader in readers):
                    raise ArmResourceError("FAILED")
    finally:
        group.close()


class AzureCliArmReader:
    """One bounded, read-only CLI call using the selected local az session."""

    identity = CapabilityProviderIdentity(
        name="azure-cli", version=None, version_provenance=VersionProvenance.UNKNOWN
    )

    def read(
        self, ref: ArmResourceRef, *, facts: frozenset[str] = frozenset()
    ) -> ArmResourceObservation:
        try:
            return self._read(ref, facts=facts)
        except KeyboardInterrupt:
            raise ArmResourceError("CANCELLED") from None

    def _read(
        self, ref: ArmResourceRef, *, facts: frozenset[str] = frozenset()
    ) -> ArmResourceObservation:
        if not isinstance(ref, ArmResourceRef):
            raise ArmResourceError("INVALID_ID")
        _validate_requested_facts(ref, facts)
        parsed = parse_arm_resource_id(
            ref.resource_id, expected_type=ref.resource_type, api_version=ref.api_version
        )
        if parsed != ref:
            raise ArmResourceError("INVALID_ID")
        try:
            tool = resolve_tool_from_path("az")
        except OSError:
            raise ArmResourceError("TOOL_MISSING") from None
        if not isinstance(tool, str) or not Path(tool).is_absolute():
            raise ArmResourceError("TOOL_MISSING")
        argv = [
            tool,
            "resource",
            "show",
            "--ids",
            ref.resource_id,
            "--api-version",
            ref.api_version,
            "--subscription",
            ref.subscription,
            "--output",
            "json",
            "--only-show-errors",
        ]
        return_code, stdout, stderr = _run_az(argv)
        if return_code:
            raise ArmResourceError(_classify_cli_error(stderr))
        try:
            document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ArmResourceError("INVALID_OBSERVATION") from None
        if not isinstance(document, dict):
            raise ArmResourceError("INVALID_OBSERVATION")
        observation = ArmResourceObservation(
            resource_id=document.get("id"),
            resource_type=document.get("type"),
            location=document.get("location"),
            name=document.get("name"),
            facts=normalize_arm_resource_facts(ref, document, facts=facts),
        )
        validate_arm_observation(ref, observation)
        return observation

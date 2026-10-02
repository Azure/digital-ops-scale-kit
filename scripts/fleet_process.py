# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run a test-owned Linux process group with private output and a bounded lifetime."""

import math
import os
import signal
import subprocess
import time
from pathlib import Path

MAX_LOG_BYTES = 8 * 1024 * 1024


class FleetProcessError(ValueError):
    """Process shutdown was not confirmed within the bounded cleanup wait."""

    def __init__(self, operation_exit: int):
        self.code = operation_exit or 1
        super().__init__("The fleet process did not stop within its cleanup deadline.")


def run(arguments: list[str], *, cwd: Path, logs: Path, name: str, timeout: float, environment=None) -> int:
    if (os.name != "posix" or type(timeout) not in {int, float}
            or not math.isfinite(timeout) or not 0 < timeout <= 18000):
        raise ValueError("Fleet execution requires Linux and a bounded command lifetime.")
    with (logs / f"{name}.out").open("xb") as stdout, (logs / f"{name}.err").open("xb") as stderr:
        os.fchmod(stdout.fileno(), 0o600)
        os.fchmod(stderr.fileno(), 0o600)
        process = subprocess.Popen(
            arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=True,
        )
        operation_exit = 1
        try:
            deadline = time.monotonic() + timeout
            while True:
                if any(os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES for stream in (stdout, stderr)):
                    operation_exit = 125
                    break
                code = process.poll()
                if code is not None:
                    operation_exit = code if code >= 0 else 130
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    operation_exit = 124
                    break
                time.sleep(min(0.1, remaining))
            return operation_exit
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise FleetProcessError(operation_exit) from None

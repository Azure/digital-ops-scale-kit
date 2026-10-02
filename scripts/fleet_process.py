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
        try:
            deadline = time.monotonic() + timeout
            while True:
                if any(os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES for stream in (stdout, stderr)):
                    return 125
                code = process.poll()
                if code is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 124
                time.sleep(min(0.1, remaining))
            return code if code >= 0 else 130
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
            process.wait(timeout=5)

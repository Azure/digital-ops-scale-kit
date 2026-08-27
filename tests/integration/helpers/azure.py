"""Azure CLI helpers for live integration tests.

Standard output is returned unsanitized because callers parse it. Never include
it in an exception or assertion message. Pass secret material through a file or
standard input rather than the argument vector.
"""

import shutil
import subprocess

from siteops.sanitize import scrub


def _azure_cli_path() -> str:
    path = shutil.which("az")
    if not path:
        raise RuntimeError(
            "Azure CLI (`az`) is required for live integration tests."
        )
    return path


def run_az(
    args: list[str], *, timeout: int = 120, redact: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run Azure CLI without exposing arguments or unsanitized errors.

    `check=False` prevents `CalledProcessError` from carrying the full argument
    vector into a pytest traceback. Failures suppress exception chaining for the
    same reason. Stderr is always structurally scrubbed before it reaches an
    error message.

    Args:
        args: Full argument vector beginning with `az`.
        timeout: Maximum command duration in seconds.
        redact: Additional literal values to replace in stderr before structural
            scrubbing.

    Returns:
        The completed process. Its stdout is raw and must remain private to the
        parsing caller.

    Raises:
        ValueError: If the argument vector does not begin with `az`.
        RuntimeError: If Azure CLI is unavailable, times out, cannot start, or
            exits unsuccessfully.
    """
    if not args or args[0] != "az":
        raise ValueError("run_az expects an argument vector beginning with `az`.")

    command = [_azure_cli_path(), *args[1:]]
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Timed out waiting for the Azure CLI command to complete."
        ) from None
    except OSError:
        raise RuntimeError("Could not start the Azure CLI command.") from None

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        for value in redact:
            if value:
                stderr = stderr.replace(value, "***")
        safe_stderr = (scrub(stderr) or "").strip()
        operation = args[1] if len(args) > 1 else ""
        detail = f": {safe_stderr}" if safe_stderr else ""
        raise RuntimeError(
            f"az {operation} failed (exit {proc.returncode}){detail}"
        ) from None
    return proc

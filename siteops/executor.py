# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Azure CLI and kubectl executor for deployments.

This module handles the low-level execution of:
- Azure deployment commands (az deployment group/sub create)
- kubectl commands via Arc-connected cluster proxy
- Template parameter extraction for filtering

The module automatically configures Azure CLI User-Agent tracking
(AZURE_HTTP_USER_AGENT) to include "siteops/{version}" for usage
telemetry in Azure Activity Logs.
"""

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any, TextIO

from siteops import __version__
from siteops.sanitize import (
    scrub_command_for_output,
    scrub_for_output,
    scrub_site_for_output,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User-Agent Configuration
# ---------------------------------------------------------------------------
# Azure CLI reads AZURE_HTTP_USER_AGENT and appends it to outgoing requests.
# This allows tracking Site Ops usage in Azure telemetry and activity logs.
# Format follows Azure SDK conventions: "tool-name/version"
# ---------------------------------------------------------------------------


def _configure_user_agent() -> None:
    """Configure Azure CLI User-Agent to include Site Ops identifier.

    Sets the AZURE_HTTP_USER_AGENT environment variable, which Azure CLI
    appends to all ARM requests. This enables usage tracking in:
    - Azure Activity Logs
    - Azure Telemetry

    The User-Agent follows Azure SDK conventions: "siteops/{version}"

    If AZURE_HTTP_USER_AGENT is already set, the Site Ops identifier is
    appended (if not already present) to preserve existing values.
    """
    siteops_agent = f"siteops/{__version__}"
    existing_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")

    # Avoid duplicate entries if module is reloaded
    if siteops_agent in existing_agent:
        logger.debug("User-Agent already configured: %s", existing_agent)
        return

    if existing_agent:
        new_agent = f"{existing_agent} {siteops_agent}"
    else:
        new_agent = siteops_agent

    os.environ["AZURE_HTTP_USER_AGENT"] = new_agent
    logger.debug("Configured AZURE_HTTP_USER_AGENT: %s", new_agent)


# Configure User-Agent on module import
_configure_user_agent()

# ---------------------------------------------------------------------------
# Thread Safety Locks
# ---------------------------------------------------------------------------

# Lock for thread-safe tmp_dir initialization
_tmp_dir_lock = threading.Lock()

# Lock for allocating unique Arc proxy ports
_arc_port_lock = threading.Lock()

# Track allocated Arc proxy port slots to avoid conflicts
# Each slot represents a (api_server_port, internal_port) pair
_allocated_arc_port_slots: set[int] = set()

# URL pattern - only HTTPS allowed for security
HTTPS_URL_PATTERN = re.compile(r"^https://", re.IGNORECASE)

# Upper bound for `_probe_arc_proxy_ready`. Default 180s covers
# observed worst-case proxy startup of ~120s on constrained infra,
# with headroom. Fast environments return in 3-10s. Override via
# `SITEOPS_ARC_PROXY_WAIT`.
ARC_PROXY_STARTUP_WAIT = int(os.environ.get("SITEOPS_ARC_PROXY_WAIT", "180"))

# TCP bind happens microseconds after the proxy is usable, so poll
# fast. Kubectl readiness is gated on API server response time, so
# faster polling adds no value.
_ARC_PROXY_PROBE_TCP_INTERVAL_S = 0.2
_ARC_PROXY_PROBE_READINESS_INTERVAL_S = 0.5

# Reserved window for the kubectl readiness phase so a late TCP bind
# still gets time to confirm the tunnel. Capped at half the total
# budget so very short timeouts allocate to both phases.
_ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S = 10.0

# Retries when `az connectedk8s proxy` exits with port-in-use. Slots may
# collide with processes outside the in-process allocator.
ARC_PROXY_MAX_PORT_RETRIES = int(os.environ.get("SITEOPS_ARC_PROXY_MAX_PORT_RETRIES", "3"))

# Matches `az connectedk8s proxy` port-in-use stderr (e.g. "ERROR: Port 47020
# is already in use.").
_ARC_PROXY_PORT_IN_USE_PATTERN = re.compile(
    r"port\s+\d+\s+is\s+already\s+in\s+use", re.IGNORECASE
)

# Default timeout for Azure CLI deployments (60 minutes)
# Azure deployments can take significant time for complex resources
DEFAULT_AZ_TIMEOUT_SECONDS = 3600

# Compiling one template is local work. Bounded anyway, so a wedged `az` fails
# the step with a diagnostic rather than holding a site's thread for the run.
DEFAULT_BICEP_BUILD_TIMEOUT_SECONDS = 300

# Default timeout for kubectl operations (10 minutes)
DEFAULT_KUBECTL_TIMEOUT_SECONDS = 600

# Per-poll az timeout for wait steps. Short by design: a single poll that hangs
# should not block the whole interval. The 3600s deploy default is wrong here.
DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS = 60

# Circuit breaker for wait steps: abort after this many consecutive transient or
# unknown polling errors so a broken `az`/network does not burn the full timeout.
# A single successful observation resets the counter.
WAIT_MAX_CONSECUTIVE_ERRORS = int(os.environ.get("SITEOPS_WAIT_MAX_CONSECUTIVE_ERRORS", "10"))

# Async deployment submit + poll. A single blocking `az deployment ... create` is one
# long process that captures the OIDC federated client assertion in memory at start. If
# it crosses the access-token refresh boundary mid-call it re-uses the now-expired
# assertion and fails (AADSTS700024) even though ARM completed the deployment. Submitting
# with `--no-wait` and observing with short-lived `deployment ... show` calls keeps every
# `az` process well under the ~5-minute assertion lifetime and lets the CI credential
# refresh take effect. See plans/siteops-arm-sdk-migration.md for the long-term SDK move.
DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS = 300
DEFAULT_DEPLOYMENT_POLL_INTERVAL_SECONDS = 20
DEFAULT_DEPLOYMENT_SUBMIT_MAX_RETRIES = 3

# A returncode-0 `--no-wait` submit means ARM accepted and created the deployment
# resource, so `show` should find it within seconds. Bound the read-after-write window so
# a deployment that never registered (for example create and show targeting different
# scopes) fails quickly instead of polling to the overall deadline.
DEPLOYMENT_NOTFOUND_GRACE_SECONDS = 120

# Maximum continuous wall-clock time the poller tolerates being unable to OBSERVE the
# deployment (auth blip, throttling, 5xx, torn credential-cache read). Sized to span at
# least two CI credential-refresh cycles so a single late or failed refresh self-heals
# before we give up. An observation error never itself fails the deployment. Only a
# terminal provisioningState, this grace, or the overall deadline ends the poll.
DEPLOYMENT_OBSERVATION_GRACE_SECONDS = 600

# Terminal ARM deployment provisioning states. Everything else (Accepted, Running,
# Creating, Updating, ...) is intermediate and keeps polling. The poll fails closed: an
# unrecognized state polls to the deadline rather than being mistaken for success.
_DEPLOYMENT_TERMINAL_SUCCESS = frozenset({"Succeeded"})
_DEPLOYMENT_TERMINAL_FAILURE = frozenset({"Failed", "Canceled"})

# Classification patterns for `az` failures during a wait poll. Order matters.
# Codes are matched before prose, because "was not found" appears in messages of
# every class: an authorization failure reports a principal that could not be
# found, and a 5xx can carry it as incidental detail. A permanent error fails the
# wait immediately, a transient one keeps polling.
_WAIT_NOT_FOUND_CODE_PATTERN = re.compile(
    # `NotFound` is bounded so it does not match the tail of a more specific
    # code. `SubscriptionNotFound` is a permanent configuration error, not a
    # missing resource, and an unbounded match classified it as the latter.
    r"ResourceNotFound|status code:\s*404|\(?\bNotFound\b\)?",
    re.IGNORECASE,
)
_WAIT_NOT_FOUND_PHRASE_PATTERN = re.compile(
    r"was not found|could not be found",
    re.IGNORECASE,
)
_WAIT_PERMANENT_ERROR_PATTERN = re.compile(
    r"AuthorizationFailed|does not have authorization|\bForbidden\b|status code:\s*403|"
    r"az login|not logged in|AADSTS|SubscriptionNotFound|InvalidResourceId|"
    r"is not a valid resource id|InvalidAuthenticationToken|ExpiredAuthenticationToken|"
    r"InvalidTemplate",
    re.IGNORECASE,
)
_WAIT_TRANSIENT_ERROR_PATTERN = re.compile(
    # `timeout` is bounded as a whole word. Azure parameter names embed it
    # freely (`idleTimeoutInMinutes`, `sessionTimeoutSeconds`), and an unbounded
    # match would read a deterministic validation error naming one as transient
    # and retry a submit that can never succeed.
    r"timed out|\btimeouts?\b|TooManyRequests|status code:\s*429|status code:\s*5\d\d|"
    r"ServerTimeout|ServiceUnavailable|connection (?:reset|aborted|refused)|"
    r"temporarily unavailable|Gateway Time-?out",
    re.IGNORECASE,
)


class WaitState(Enum):
    """Outcome of a single wait-condition observation."""

    SATISFIED = "satisfied"
    FAILED = "failed"
    PENDING = "pending"


def _classify_az_error(stderr: str) -> str:
    """Classify an `az` failure stderr for wait-step polling.

    Returns one of `resource_not_found`, `permanent`, `transient`, or `unknown`.
    Codes are tested before the not-found prose, which appears in messages of
    every class.
    """
    text = stderr or ""
    if _WAIT_NOT_FOUND_CODE_PATTERN.search(text):
        return "resource_not_found"
    if _WAIT_PERMANENT_ERROR_PATTERN.search(text):
        return "permanent"
    if _WAIT_TRANSIENT_ERROR_PATTERN.search(text):
        return "transient"
    if _WAIT_NOT_FOUND_PHRASE_PATTERN.search(text):
        return "resource_not_found"
    return "unknown"


def _format_arm_error(error_node: Any) -> str:
    """Flatten an ARM error object (`code`/`message`/`details`) into one line.

    Used to surface deployment failure detail. Falls back to a JSON dump for shapes that
    do not match the standard ARM error envelope.
    """
    if not isinstance(error_node, dict):
        return str(error_node)
    # Coerced, since this runs while reporting a failure. A provider returning
    # an unexpected shape must not raise from the reporting path and mask the
    # failure being reported.
    code = str(error_node.get("code", "") or "")
    message = str(error_node.get("message", "") or "")
    parts = [part for part in (code, message) if part]
    text = ": ".join(parts) if parts else json.dumps(error_node, default=str)
    details = error_node.get("details")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        detail_code = str(details[0].get("code", "") or "")
        detail_message = str(details[0].get("message", "") or "")
        detail_text = ": ".join(part for part in (detail_code, detail_message) if part)
        if detail_text:
            text = f"{text} ({detail_text})"
    return text


# A Bicep module compiles to a nested deployment, so every module shares this resource
# type. Reporting the type alone would name the mechanism rather than the module.
_NESTED_DEPLOYMENT_TYPE = "microsoft.resources/deployments"


def _operation_target_label(target: Any) -> str:
    """Name the resource a failed deployment operation targeted.

    Falls back to the resource id, then to an empty string when the operation
    carries no target at all. Both fields are coerced, since this runs while
    reporting a failure and a tool that returned something unexpected must not
    raise from the reporting path itself.
    """
    if not isinstance(target, dict):
        return ""
    resource_type = str(target.get("resourceType") or "")
    resource_id = str(target.get("id") or "")

    if resource_type.lower() == _NESTED_DEPLOYMENT_TYPE:
        name = str(target.get("resourceName") or "") or resource_id.rsplit("/", 1)[-1]
        if name:
            return f"{resource_type}/{name}"

    return resource_type or resource_id


def _describe_condition(condition: Any) -> str:
    """Human-readable one-line description of a wait condition for logs."""
    if getattr(condition, "type", None) == "arm-tag":
        return f"tag '{condition.tag_key}'='{condition.expected_value}' on {condition.resource_id}"
    return f"condition type '{getattr(condition, 'type', 'unknown')}'"


def _wait_failure_message(
    condition: Any,
    *,
    reason: str,
    last_value: str | None,
    last_error: str | None,
    poll_count: int,
    elapsed_seconds: float,
) -> str:
    """Build a diagnostic message for a failed or timed-out wait.

    Carries both the last observed value and the last underlying error so the
    operator can diagnose in one read (the diagnostic-on-failure convention).
    """
    parts = [f"Wait failed ({reason}) for {_describe_condition(condition)}."]
    if last_value is not None:
        parts.append(f"Last observed value: {last_value!r}.")
    else:
        parts.append("Tag value never observed.")
    if last_error:
        parts.append(f"Last error: {last_error}")
    parts.append(f"Polls: {poll_count}, elapsed: {elapsed_seconds:.0f}s.")
    return " ".join(parts)

# Arc proxy port configuration
# Each proxy needs 2 ports: api_server_port (--port) and internal_port (api_server_port - 1)
# We allocate slots with spacing of 10 to avoid conflicts
# Start at 47021 (not 47011) so slot 0 also triggers the fallback logic in Azure CLI
# This ensures internal port is always (api_server_port - 1), not the hardcoded 47010
ARC_PROXY_PORT_BASE = 47021  # First slot uses 47021/47020, avoiding default 47010
ARC_PROXY_PORT_SPACING = 10  # Space between slots
ARC_PROXY_MAX_SLOTS = 10  # Maximum concurrent proxies

# The engine's own subprocess timeout, distinct from anything a tool prints. A
# caller that needs to know the engine gave up compares against this exactly,
# since searching stderr for "timeout" also matches an ARM error naming a
# parameter such as `idleTimeoutInMinutes`.
ENGINE_TIMEOUT_SENTINEL = "Command timed out after {timeout}s"


@lru_cache(maxsize=128)
def _load_template_arm_json(template_path: str) -> dict[str, Any]:
    """Compile (or read) a template and return its ARM JSON.

    Bicep is compiled to a temporary file rather than to the console, since
    `az` decodes console output in the locale encoding and raises on content
    that encoding cannot represent.

    Results are cached per template path, so a template is compiled once no
    matter how many callers inspect it.

    The cached value is shared, and sites deploy on a thread pool, so treat
    the return as read-only. A caller that needs to mutate takes its own copy.

    Args:
        template_path: Absolute path to a `.bicep` or `.json` template.

    Returns:
        The parsed ARM template.

    Raises:
        ValueError: If the template cannot be compiled or parsed.
        FileNotFoundError: If the template does not exist.
    """
    path = Path(template_path)

    if not path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    if path.suffix == ".bicep":
        az_path = shutil.which("az")
        if not az_path:
            raise ValueError(
                "Azure CLI (`az`) not found on PATH. Install Azure CLI and ensure "
                "`az` is available, then retry."
            )

        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "template.json"
            try:
                result = subprocess.run(
                    [az_path, "bicep", "build", "--file", str(path), "--outfile", str(out_file)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=DEFAULT_BICEP_BUILD_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as e:
                # Raised as ValueError so callers handling a bad template handle
                # this too. `TimeoutExpired` is a `SubprocessError`, so letting it
                # escape would bypass them and fail the whole site rather than the
                # step.
                raise ValueError(
                    f"Timed out compiling Bicep template {template_path} after "
                    f"{DEFAULT_BICEP_BUILD_TIMEOUT_SECONDS}s"
                ) from e
            if result.returncode != 0:
                raise ValueError(
                    f"Failed to compile Bicep template {template_path}: {result.stderr}"
                )
            try:
                return json.loads(out_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(
                    f"Failed to parse compiled Bicep template {template_path}: {e}"
                ) from e

    if path.suffix == ".json":
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse ARM template {template_path}: {e}") from e

    raise ValueError(f"Unsupported template format: {path.suffix}. Expected .bicep or .json")


def get_template_parameters(template_path: str) -> frozenset[str]:
    """Extract parameter names from a Bicep or ARM template.

    Args:
        template_path: Absolute path to the template file

    Returns:
        Frozenset of parameter names the template accepts

    Raises:
        ValueError: If template cannot be parsed
        FileNotFoundError: If template file doesn't exist
    """
    arm_json = _load_template_arm_json(template_path)
    param_names = frozenset(arm_json.get("parameters", {}).keys())
    logger.debug(f"Template {Path(template_path).name} accepts parameters: {sorted(param_names)}")
    return param_names


# The compile cache lives on the loader, so expose clearing through the public
# reader. A caller that rewrites a template in place relies on this.
get_template_parameters.cache_clear = _load_template_arm_json.cache_clear


def filter_parameters(
    parameters: dict[str, Any],
    template_path: str,
    step_name: str,
) -> dict[str, Any]:
    """Filter parameters to only those accepted by the template.

    Args:
        parameters: All parameters provided for the step
        template_path: Absolute path to the template file
        step_name: Name of the step (for logging)

    Returns:
        Filtered parameters dict containing only keys the template accepts
    """
    accepted_params = get_template_parameters(template_path)

    filtered = {}
    unused = []

    for key, value in parameters.items():
        if key in accepted_params:
            filtered[key] = value
        else:
            unused.append(key)

    if unused:
        logger.debug(
            scrub_for_output(
                f"Step '{step_name}': Filtered out parameters not in template: {unused}"
            )
        )

    return filtered


def _allocate_arc_port_slot() -> int:
    """Allocate a unique port slot for Arc proxy.

    Returns:
        The api_server_port to use (internal port will be this - 1)

    Raises:
        RuntimeError: If no slots are available.
    """
    with _arc_port_lock:
        for slot in range(ARC_PROXY_MAX_SLOTS):
            if slot not in _allocated_arc_port_slots:
                _allocated_arc_port_slots.add(slot)
                port = ARC_PROXY_PORT_BASE + (slot * ARC_PROXY_PORT_SPACING)
                logger.debug(f"Allocated Arc proxy slot {slot} (port {port})")
                return port
        raise RuntimeError(
            f"No Arc proxy slot is free. At most {ARC_PROXY_MAX_SLOTS} proxies run "
            f"at once, so a manifest with a kubectl or wait step cannot deploy to "
            f"more than {ARC_PROXY_MAX_SLOTS} sites concurrently. Lower `parallel:` "
            f"in the manifest, or pass `--parallel {ARC_PROXY_MAX_SLOTS}`."
        )


def _release_arc_port_slot(port: int) -> None:
    """Release an allocated Arc proxy port slot."""
    with _arc_port_lock:
        slot = (port - ARC_PROXY_PORT_BASE) // ARC_PROXY_PORT_SPACING
        _allocated_arc_port_slots.discard(slot)
        logger.debug(f"Released Arc proxy slot {slot} (port {port})")


def _compute_probe_phase_budget(total_budget: float) -> tuple[float, float]:
    """Split the total probe budget into per-phase deadlines (relative).

    Returns `(tcp_budget, total_budget)`, where the kubectl readiness phase
    runs until the total budget elapses. The TCP phase exits earlier to
    reserve `_ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S` for readiness, capped
    at half the total so a small user-supplied timeout still allocates
    time to both phases. The readiness phase therefore always has at least
    `min(total_budget / 2, _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S)` seconds.

    Pure function so the split math can be unit-tested without timing.
    """
    readiness_budget = min(total_budget / 2.0, _ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S)
    return total_budget - readiness_budget, total_budget


_ARC_PROXY_RETAINED_LINES = 200


class _ProxyOutputDrainer:
    """Continuously read a subprocess's pipes so it cannot block writing to them.

    The Arc proxy runs for the whole life of the caller's `with` body. A proxy
    that fills the operating system's pipe buffer blocks on its next write and
    stops serving, which presents as a deploy that hangs rather than one that
    fails.

    `DEVNULL` would also stop the blocking, but the port-in-use retry matches on
    stderr, so that output is retained rather than discarded.
    """

    def __init__(
        self, process: subprocess.Popen, max_lines: int = _ARC_PROXY_RETAINED_LINES
    ) -> None:
        self._buffers: dict[str, "deque[str]"] = {}
        self._threads: list[threading.Thread] = []
        # Set as stderr is read, not derived from the retained tail. The tail
        # is bounded, so a proxy that keeps talking after the port error
        # evicts the line the retry depends on, and a retryable collision
        # would become a hard failure.
        self._port_in_use = False

        for name in ("stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is None:
                continue
            # Both pipes are drained, since either one filling blocks the
            # proxy. Only stderr is retained, because the diagnostic is its
            # only reader, and an unbounded buffer would move the pipe's growth
            # into memory. A zero-length buffer still drains.
            buffer: "deque[str]" = deque(maxlen=max_lines if name == "stderr" else 0)
            self._buffers[name] = buffer
            thread = threading.Thread(
                target=self._drain,
                args=(stream, buffer, name == "stderr"),
                name=f"arc-proxy-{name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _drain(
        self, stream: TextIO, buffer: "deque[str]", watch_for_port: bool = False
    ) -> None:
        """Read until end of stream, keeping whatever the buffer retains."""
        try:
            for line in stream:
                buffer.append(line)
                if watch_for_port and _ARC_PROXY_PORT_IN_USE_PATTERN.search(line):
                    self._port_in_use = True
        except UnicodeDecodeError:
            # Ordered above `ValueError`, which it subclasses, so this handler
            # is reachable at all. The Arc proxy's streams are opened with
            # `errors="replace"`, so this is the guard for a stream that is
            # not, rather than a case the current caller produces. Reported
            # rather than ending the drain silently, since the pipe stops being
            # read from here and the proxy can block on its next write.
            logger.debug("Arc proxy output could not be decoded, stopping this reader")
        except (OSError, ValueError):
            # Raised if the stream is closed while this read is in flight.
            # Treated as the end of the stream either way.
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def join(self, timeout: float = 5.0) -> None:
        """Wait for the readers to reach end of stream, within one total budget.

        The budget is shared rather than one each, since per reader made a
        `join(2)` take four seconds.

        A reader can outlive the proxy, because `az connectedk8s proxy` runs a
        separate binary that can inherit the write end and hold it open, so end
        of stream is not guaranteed. Two ways of forcing this reader to stop
        were measured and rejected: closing the stream deadlocks against the
        lock the blocked read holds, and closing the descriptor hangs the
        interpreter. Terminating every process that holds the write end does
        work, which is what the caller's cleanup aims at. A reader that still
        survives is a daemon bounded by `max_lines`, so it cannot block exit or
        grow without limit.
        """
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    @property
    def port_in_use(self) -> bool:
        """Whether the proxy reported its port was taken.

        Two readings, because neither covers the other. The flag is set as each
        line is read, so it survives a chatty proxy evicting the line from the
        bounded tail. The tail is searched as well, since a message split
        across two writes never appears whole on any single line.
        """
        return self._port_in_use or bool(
            _ARC_PROXY_PORT_IN_USE_PATTERN.search(self.stderr)
        )

    @property
    def stderr(self) -> str:
        """The retained tail of stderr, as one string."""
        return "".join(self._buffers.get("stderr", ()))


def _probe_arc_proxy_ready(
    proxy_process: subprocess.Popen,
    port: int,
    timeout_s: int | None = None,
    kubectl_path: str | None = None,
    kubeconfig_path: str | None = None,
) -> bool:
    """Active readiness probe for the Arc proxy.

    Two phases. First, TCP bind detection: poll `127.0.0.1:port` until it
    accepts a connection. Second, kubectl readiness: poll
    `kubectl get --raw /version` until it succeeds. The kubectl phase
    proves connectivity through the tunnel and that the proxy-written
    kubeconfig context is usable. It does not exercise resource RBAC,
    so a positive signal means apply can reach the API server but a
    later 401 or 403 on a write call is still possible.

    The total budget is split so the kubectl phase always has at least
    `_ARC_PROXY_PROBE_READINESS_MIN_BUDGET_S` seconds (capped at half the
    total when the user-supplied timeout is small). A TCP bind that
    succeeds right before the overall deadline still gets a real chance
    to confirm the tunnel.

    Bails early if the proxy process dies (`poll()` returns non-None) so
    the caller can read stderr and retry on port-in-use.

    Args:
        proxy_process: Running `az connectedk8s proxy` subprocess.
        port: Local port the proxy is bound to (`--port` argument).
        timeout_s: Upper bound for the probe in seconds.
            Defaults to `ARC_PROXY_STARTUP_WAIT`.
        kubectl_path: Path to the kubectl binary. When None, resolved via
            `shutil.which("kubectl")`. Pass an explicit path from the
            caller to avoid a second PATH lookup.
        kubeconfig_path: Path to the kubeconfig file the proxy writes
            (`--file` argument to `az connectedk8s proxy`). Passed to
            kubectl as `--kubeconfig=<path>` so the probe targets this
            specific proxy rather than the ambient `current-context`.
            None falls back to the default kubeconfig discovery.

    Returns:
        True if the proxy became responsive within the deadline.
        False if the deadline elapsed, or the proxy died, or kubectl was
        not available.
    """
    total_budget = timeout_s if timeout_s is not None else ARC_PROXY_STARTUP_WAIT
    start = time.monotonic()
    tcp_budget, readiness_total = _compute_probe_phase_budget(total_budget)
    tcp_deadline = start + tcp_budget
    deadline = start + readiness_total

    # Phase 1: TCP bind detection.
    bound = False
    while time.monotonic() < tcp_deadline:
        if proxy_process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                bound = True
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(_ARC_PROXY_PROBE_TCP_INTERVAL_S)
    if not bound:
        logger.debug(f"Arc proxy TCP bind probe timed out on port {port}")
        return False

    # Phase 2: kubectl readiness. Mirrors the engine path the orchestrator
    # runs for `apply`, so a positive signal here means apply will reach
    # the API server through the tunnel.
    if kubectl_path is None:
        kubectl_path = shutil.which("kubectl")
    if kubectl_path is None:
        logger.error(
            "Arc proxy readiness probe cannot run: kubectl not found in "
            "PATH. Install kubectl from "
            "https://kubernetes.io/docs/tasks/tools/."
        )
        return False

    cmd = [kubectl_path]
    if kubeconfig_path is not None:
        cmd.append(f"--kubeconfig={kubeconfig_path}")
    cmd.extend(["get", "--raw=/version", "--request-timeout=5s"])

    last_observation = "no kubectl invocation yet"
    while time.monotonic() < deadline:
        if proxy_process.poll() is not None:
            return False
        # Clamp the subprocess timeout to the remaining budget so a single
        # hung kubectl call cannot overrun the readiness deadline by 10s.
        # Floor at 1s so the call always gets a real attempt.
        remaining = deadline - time.monotonic()
        run_timeout = max(1.0, min(10.0, remaining))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=run_timeout,
            )
        except subprocess.TimeoutExpired:
            last_observation = f"kubectl invocation timed out at {run_timeout:.1f}s"
            time.sleep(_ARC_PROXY_PROBE_READINESS_INTERVAL_S)
            continue
        if result.returncode == 0:
            return True
        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        detail = stderr_text or stdout_text or "(no output)"
        first_line = detail.splitlines()[0]
        last_observation = (
            f"argv={scrub_command_for_output(cmd)!r} exit={result.returncode} "
            f"detail={scrub_for_output(first_line)[:200]!r}"
        )
        time.sleep(_ARC_PROXY_PROBE_READINESS_INTERVAL_S)

    logger.error(
        f"Arc proxy kubectl readiness probe timed out on port {port}. "
        f"Last observation: {last_observation}"
    )
    return False


@dataclass
class DeploymentResult:
    """Result of a Bicep/ARM deployment operation.

    Attributes:
        success: Whether the deployment succeeded
        step_name: Name of the step that was executed
        site_name: Name of the site deployed to
        deployment_name: Azure deployment name
        outputs: Deployment outputs (from Bicep/ARM)
        error: Error message if deployment failed
    """

    success: bool
    step_name: str
    site_name: str
    deployment_name: str
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class KubectlResult:
    """Result of a kubectl operation.

    Attributes:
        success: Whether the operation succeeded
        step_name: Name of the step that was executed
        site_name: Name of the site
        error: Error message if operation failed
    """

    success: bool
    step_name: str
    site_name: str
    error: str | None = None


@dataclass
class WaitResult:
    """Result of a wait step.

    A wait step is a gate. It produces no outputs. `error` carries the full
    diagnostic (last observed value, last underlying error, poll count, elapsed)
    when the wait fails or times out.

    Attributes:
        success: Whether the wait condition was satisfied.
        step_name: Name of the step that was executed.
        site_name: Name of the site.
        error: Diagnostic message if the wait failed or timed out.
    """

    success: bool
    step_name: str
    site_name: str
    error: str | None = None


class AzCliExecutor:
    """Executes Azure CLI deployments and kubectl operations.

    Handles:
    - Resource group and subscription-scoped ARM/Bicep deployments
    - kubectl apply via Arc-connected cluster proxy

    Attributes:
        workspace: Path to the Site Ops workspace directory
        dry_run: If True, commands are logged but not executed
    """

    def __init__(self, workspace: Path, dry_run: bool = False):
        self.workspace = workspace
        self.dry_run = dry_run
        self._tmp_dir: Path | None = None
        self._az_path: str | None = None
        self._kubectl_path: str | None = None

    @property
    def az_path(self) -> str | None:
        """Find and cache the az CLI executable path."""
        if self._az_path is None:
            self._az_path = shutil.which("az")
        return self._az_path

    @property
    def kubectl_path(self) -> str | None:
        """Find and cache the kubectl executable path."""
        if self._kubectl_path is None:
            self._kubectl_path = shutil.which("kubectl")
        return self._kubectl_path

    @property
    def tmp_dir(self) -> Path:
        """Get or create the temp directory for parameter files.

        Uses double-checked locking for thread-safe initialization.
        """
        if self._tmp_dir is None:
            with _tmp_dir_lock:
                if self._tmp_dir is None:
                    self._tmp_dir = self.workspace / ".siteops" / "tmp"
                    self._tmp_dir.mkdir(parents=True, exist_ok=True)
        return self._tmp_dir

    def _run_az(
        self,
        args: list[str],
        timeout: int = DEFAULT_AZ_TIMEOUT_SECONDS,
        site_name: str = "",
    ) -> tuple[bool, str, str]:
        """Run an Azure CLI command.

        Args:
            args: Command arguments (without 'az' prefix)
            timeout: Command timeout in seconds (default: 60 minutes)

        Returns:
            Tuple of (success, stdout, stderr)
        """
        if not self.az_path:
            return False, "", "Azure CLI (az) not found in PATH. Install from https://aka.ms/installazurecli"

        cmd = [self.az_path] + args
        # Rendered from the vector rather than scrubbed after joining, so a
        # value containing a space is replaced whole. Display only. Both lines
        # below carry the subscription and resource group the command targets,
        # and a dry run is what an operator runs in CI to preview a change.
        cmd_display = scrub_command_for_output(cmd)
        cmd_display = scrub_site_for_output(cmd_display, site_name) or ""

        if self.dry_run:
            logger.info(f"[DRY-RUN] {cmd_display}")
            return True, "{}", ""

        logger.debug(f"Executing: {cmd_display}")

        try:
            # Decode as UTF-8 rather than the locale encoding. `az` emits UTF-8,
            # and a byte the locale cannot represent otherwise raises inside
            # subprocess's reader thread, which surfaces as `stdout=None` rather
            # than as an error.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            return result.returncode == 0, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            return False, "", ENGINE_TIMEOUT_SENTINEL.format(timeout=timeout)
        except Exception as e:
            return False, "", f"Failed to execute az command: {e}"

    def _run_kubectl(
        self,
        args: list[str],
        timeout: int = DEFAULT_KUBECTL_TIMEOUT_SECONDS,
        kubeconfig: str | None = None,
    ) -> tuple[bool, str, str]:
        """Run a kubectl command.

        Args:
            args: Command arguments (without 'kubectl' prefix)
            timeout: Command timeout in seconds (default: 10 minutes)
            kubeconfig: When set, pass `--kubeconfig=<value>` to kubectl so
                the call targets a specific kubeconfig file rather than
                the ambient `current-context`. Used by `_arc_proxy` to
                pin kubectl to the per-proxy kubeconfig.

        Returns:
            Tuple of (success, stdout, stderr)
        """
        if not self.kubectl_path:
            return False, "", "kubectl not found in PATH. Install from https://kubernetes.io/docs/tasks/tools/"

        cmd = [self.kubectl_path]
        if kubeconfig is not None:
            cmd.append(f"--kubeconfig={kubeconfig}")
        cmd.extend(args)
        cmd_display = scrub_command_for_output(cmd)

        if self.dry_run:
            logger.info(f"[DRY-RUN] {cmd_display}")
            return True, "", ""

        logger.debug(f"Executing: {cmd_display}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
            )
            return result.returncode == 0, result.stdout or "", result.stderr or ""
        except subprocess.TimeoutExpired:
            return False, "", ENGINE_TIMEOUT_SENTINEL.format(timeout=timeout)
        except Exception as e:
            return False, "", f"Failed to execute kubectl command: {e}"

    @contextmanager
    def _arc_proxy(
        self,
        cluster_name: str,
        resource_group: str,
        subscription: str,
    ) -> Generator[str | None, None, None]:
        """Context manager for Arc-connected cluster proxy.

        Starts `az connectedk8s proxy` in the background, waits for it to
        establish, and ensures cleanup on exit (even on exceptions).

        Allocates a per-proxy kubeconfig file (`--file` argument to az) and
        a unique local port so parallel deploys targeting different Arc
        clusters do not race the ambient `~/.kube/config` current-context.

        Args:
            cluster_name: Name of the Arc-connected cluster
            resource_group: Resource group containing the cluster
            subscription: Azure subscription ID

        Yields:
            Path to the per-proxy kubeconfig file when the proxy started
            successfully, or None when it failed. Pass the path to
            `_run_kubectl(..., kubeconfig=<path>)` so the call targets
            this proxy rather than the ambient context.

        Example:
            with self._arc_proxy("my-cluster", "my-rg", "sub-id") as kubeconfig:
                if kubeconfig is not None:
                    self._run_kubectl(["apply", "-f", "config.yaml"], kubeconfig=kubeconfig)
        """
        if self.dry_run:
            logger.info(
                scrub_for_output(
                    f"[DRY-RUN] az connectedk8s proxy -n {cluster_name} "
                    f"-g {resource_group} --subscription {subscription}"
                )
            )
            yield "dry-run-kubeconfig"
            return

        if not self.az_path:
            logger.error("Azure CLI not found - cannot start Arc proxy")
            yield None
            return

        proxy_process: subprocess.Popen | None = None
        drainer: _ProxyOutputDrainer | None = None
        allocated_port: int | None = None
        # A slot stays held until the attempt sequence ends, so a retry cannot
        # be handed back the port that just failed.
        held_ports: list[int] = []
        started = False
        # Per-proxy kubeconfig so parallel proxies do not race the
        # ambient current-context in `~/.kube/config`. Created with
        # `mkstemp` for an atomic, unique file. The fd is closed
        # immediately. az populates the file when the proxy starts.
        kubeconfig_fd, kubeconfig_path = tempfile.mkstemp(
            prefix="siteops-arc-proxy-", suffix=".kubeconfig"
        )
        os.close(kubeconfig_fd)

        try:
            for attempt in range(ARC_PROXY_MAX_PORT_RETRIES):
                # Allocate a unique port slot for this proxy instance
                allocated_port = _allocate_arc_port_slot()
                held_ports.append(allocated_port)

                cmd = [
                    self.az_path,
                    "connectedk8s",
                    "proxy",
                    "-n",
                    cluster_name,
                    "-g",
                    resource_group,
                    "--subscription",
                    subscription,
                    "--port",
                    str(allocated_port),
                    "--file",
                    kubeconfig_path,
                ]

                logger.debug(f"Starting Arc proxy: {scrub_command_for_output(cmd)}")

                # Start process with its own process group for clean termination
                if os.name == "nt":
                    # Windows: use CREATE_NEW_PROCESS_GROUP for signal handling
                    proxy_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                else:
                    # Unix: use setsid to create new process group
                    proxy_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        preexec_fn=os.setsid,
                    )

                # Drain from the moment the process exists, so it can never
                # block on a full pipe.
                drainer = _ProxyOutputDrainer(proxy_process)

                # Active readiness probe. Bails early if the proxy process dies.
                logger.debug(
                    f"Probing Arc proxy readiness on port {allocated_port} "
                    f"(deadline {ARC_PROXY_STARTUP_WAIT}s)..."
                )
                ready = _probe_arc_proxy_ready(
                    proxy_process,
                    allocated_port,
                    kubectl_path=self.kubectl_path,
                    kubeconfig_path=kubeconfig_path,
                )
                if ready:
                    started = True
                    break  # proxy responsive

                # Probe did not become ready. Determine cause.
                if proxy_process.poll() is None:
                    # Port bound but tunnel never responded within deadline.
                    # Not a port-in-use case (proxy is still running), so no
                    # retry. Terminate and surface a clear diagnostic.
                    logger.error(
                        f"Arc proxy on port {allocated_port} bound but did not "
                        f"become responsive within {ARC_PROXY_STARTUP_WAIT}s. "
                        f"Check upstream cluster reachability and az identity."
                    )
                    try:
                        if os.name == "nt":
                            proxy_process.send_signal(signal.CTRL_BREAK_EVENT)
                        else:
                            os.killpg(os.getpgid(proxy_process.pid), signal.SIGTERM)
                        proxy_process.wait(timeout=5)
                    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                        # Best-effort terminate. Finally block handles full cleanup.
                        pass
                    break

                # The process has already exited here, so the readers are at end
                # of stream or about to be. Read what the drainer collected
                # rather than calling `communicate()`, which would contend with
                # them. The retry decision comes from a flag set as the line was
                # read, since the retained tail is bounded and a chatty proxy
                # can push the port error out of it.
                drainer.join(timeout=5)
                stderr = drainer.stderr
                is_port_in_use = drainer.port_in_use
                is_last_attempt = attempt == ARC_PROXY_MAX_PORT_RETRIES - 1

                if is_port_in_use and not is_last_attempt:
                    logger.warning(
                        f"Arc proxy port {allocated_port} (internal {allocated_port - 1}) "
                        f"in use, retrying with next slot "
                        f"(attempt {attempt + 1}/{ARC_PROXY_MAX_PORT_RETRIES})"
                    )
                    allocated_port = None
                    proxy_process = None
                    drainer = None
                    continue

                # Not retryable: surface stderr and bail.
                logger.error(f"Arc proxy exited unexpectedly: {scrub_for_output(stderr)}")
                break

        except Exception as e:
            logger.error(f"Failed to start Arc proxy: {scrub_for_output(str(e))}")
            yield None

        else:
            # Every yield sits outside the handler above. Inside the `try` it
            # caught whatever the caller's `with` body raised, reported it as a
            # startup failure, and destroyed the original exception. `finally`
            # still runs, so cleanup is unchanged.
            if started:
                logger.debug("Arc proxy established successfully")
                yield kubeconfig_path
            else:
                yield None

        finally:
            if proxy_process is not None and proxy_process.poll() is None:
                logger.debug("Terminating Arc proxy...")
                try:
                    if os.name == "nt":
                        # Windows: send CTRL+BREAK to process group
                        proxy_process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        # Unix: send SIGTERM to process group
                        os.killpg(os.getpgid(proxy_process.pid), signal.SIGTERM)

                    proxy_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.debug("Proxy did not terminate gracefully, forcing...")
                    proxy_process.kill()
                    try:
                        proxy_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.debug("Proxy did not exit after kill; reap will defer.")
                except Exception as e:
                    logger.debug(f"Error during proxy cleanup: {e}")
                    try:
                        proxy_process.kill()
                        proxy_process.wait(timeout=5)
                    except Exception as e:
                        logger.debug(f"Failed to kill proxy process: {e}")

            # Join briefly to retire the readers. The proxy being down does not
            # guarantee end of stream, since a descendant it started can still
            # hold the write end, so this is best effort. They are daemons
            # bounded by their buffers, so one that survives cannot block exit
            # or grow without limit.
            if drainer is not None:
                drainer.join(timeout=2)

            # Release every slot this call took, including those a retry left
            # held.
            for port in held_ports:
                _release_arc_port_slot(port)

            # Best-effort remove the per-proxy kubeconfig. The file may
            # already be gone (test teardown, manual cleanup), so swallow
            # FileNotFoundError. Other errors are logged at debug because
            # the file is in the OS temp dir and a stale copy is harmless.
            try:
                os.unlink(kubeconfig_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.debug(f"Failed to remove per-proxy kubeconfig {kubeconfig_path}: {e}")

    def _write_params_file(self, parameters: dict[str, Any], step_name: str, site_name: str) -> Path:
        """Write parameters to a temp file in ARM parameter format.

        Args:
            parameters: Parameter key-value pairs
            step_name: Step name (for filename)
            site_name: Site name (for filename)

        Returns:
            Path to the created parameter file
        """
        arm_params = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {k: {"value": v} for k, v in parameters.items()},
        }

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        # Add a short uuid suffix to avoid collisions when the same step
        # writes multiple param files within a single second (parallel sites
        # or rapid successive deploys on the same site).
        unique = uuid.uuid4().hex[:8]
        filename = f"{site_name}-{step_name}-{timestamp}-{unique}.json"

        tmp_dir = self.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)

        params_path = tmp_dir / filename

        # Create 0o600: the file holds resolved parameters, which can include
        # secrets (e.g. an SP password). os.open applies the mode at creation so
        # there is no world-readable window. POSIX mode is advisory on Windows
        # (ACL-based) but harmless. Deleted in the deploy's finally.
        fd = os.open(params_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(arm_params, f, indent=2)

        return params_path

    def _deploy(
        self,
        create_args: list[str],
        show_args: list[str],
        ops_args: list[str],
        parameters: dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Submit an Azure deployment asynchronously and poll it to a terminal state.

        The deployment is submitted with `--no-wait` and then observed with short
        `az deployment ... show` calls. This keeps every `az` process well under the OIDC
        federated-assertion lifetime, so a long deployment cannot fail on a stale
        in-memory assertion (AADSTS700024) the way a single blocking `create` does.

        Args:
            create_args: Base `az deployment ... create` arguments (without `--no-wait`).
            show_args: Matching `az deployment ... show` arguments for polling.
            ops_args: Matching `az deployment operation ... list` arguments for failure detail.
            parameters: Parameters to pass to the deployment.
            deployment_name: Name for the Azure deployment.
            step_name: Site Ops step name.
            site_name: Site Ops site name.

        Returns:
            DeploymentResult with success status and outputs.
        """
        if not self.dry_run and not self.az_path:
            return DeploymentResult(
                success=False,
                step_name=step_name,
                site_name=site_name,
                deployment_name=deployment_name,
                error="Azure CLI (az) not found in PATH. Install from https://aka.ms/installazurecli",
            )

        if parameters:
            params_path = self._write_params_file(parameters, step_name, site_name)
            create_args = create_args + ["--parameters", f"@{params_path}"]

        try:
            # Submit without blocking. A single long blocking `create` would re-use a
            # stale in-memory OIDC assertion across the token-refresh boundary. Do NOT
            # replace the show poll below with `az deployment ... wait`: that is itself a
            # single long-lived process and reintroduces the same failure.
            submit_args = create_args + ["--no-wait"]

            if self.dry_run:
                # Log the intended submit. Never submit or poll in dry-run.
                self._run_az(submit_args, site_name=site_name)
                return DeploymentResult(
                    success=True,
                    step_name=step_name,
                    site_name=site_name,
                    deployment_name=deployment_name,
                )

            proceed, early_result = self._submit_deployment(
                submit_args, deployment_name, step_name, site_name
            )
            # ARM holds the parameters inline in the submit PUT now, and the
            # poll uses `show` (no params file), so delete it here rather than
            # holding it for the full poll deadline. The finally is a backstop.
            if parameters:
                try:
                    params_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.debug(f"Failed to remove params file {params_path}: {e}")
            if not proceed:
                return early_result

            return self._poll_deployment(
                show_args, ops_args, deployment_name, step_name, site_name
            )
        finally:
            # Clean up the per-deploy params file. ARM has the parameters inline in the
            # submit PUT by the time we poll, so `show` never needs the file. Best-effort:
            # don't mask the deploy result on cleanup errors.
            if parameters:
                try:
                    params_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.debug(f"Failed to remove params file {params_path}: {e}")

    def _submit_deployment(
        self,
        submit_args: list[str],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> tuple[bool, DeploymentResult | None]:
        """Submit the deployment with `--no-wait`, retrying transient submit failures.

        A `--no-wait` submit makes a synchronous ARM PUT and returns once the request is
        accepted. Bad template or parameter errors surface here and fail fast. A transient
        network error retries (the timestamped deployment name makes the PUT idempotent).
        A submit timeout is ambiguous because the request may have reached ARM, so it
        falls through to polling, where the not-found grace catches a submit that never
        registered.

        Args:
            submit_args: Full `az deployment ... create ... --no-wait` arguments.
            deployment_name: Name for the Azure deployment.
            step_name: Site Ops step name.
            site_name: Site Ops site name.

        Returns:
            A tuple `(proceed_to_poll, early_result)`. When `proceed_to_poll` is True,
            `early_result` is None and the caller polls. When False, `early_result` is a
            terminal DeploymentResult for a fail-fast submit error.
        """
        last_error = ""
        for attempt in range(1, DEFAULT_DEPLOYMENT_SUBMIT_MAX_RETRIES + 1):
            ok, _stdout, stderr = self._run_az(
                submit_args,
                timeout=DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS,
                site_name=site_name,
            )
            if ok:
                return True, None

            last_error = stderr
            if stderr == ENGINE_TIMEOUT_SENTINEL.format(
                timeout=DEFAULT_DEPLOYMENT_SUBMIT_TIMEOUT_SECONDS
            ):
                # The engine's own timeout, not an ARM error. The PUT may have
                # reached ARM, so poll and let the not-found grace decide.
                logger.warning(
                    scrub_site_for_output(
                        f"Submit of '{deployment_name}' timed out. Polling in "
                        "case ARM accepted the request.",
                        site_name,
                    )
                )
                return True, None

            category = _classify_az_error(stderr)
            if category == "transient":
                if attempt < DEFAULT_DEPLOYMENT_SUBMIT_MAX_RETRIES:
                    time.sleep(min(DEFAULT_DEPLOYMENT_POLL_INTERVAL_SECONDS, 5 * attempt))
                    continue
                # Exhausted retries on a transient submit error. The PUT may still have
                # reached ARM, so poll and let the not-found grace decide.
                return True, None

            # Permanent or unrecognized submit failure (bad template, bad parameters,
            # auth, or an unclassified deterministic rejection). Nothing was created, so
            # fail fast with the real error rather than polling for a phantom deployment.
            return False, DeploymentResult(
                success=False,
                step_name=step_name,
                site_name=site_name,
                deployment_name=deployment_name,
                error=last_error,
            )

        return True, None

    def _poll_deployment(
        self,
        show_args: list[str],
        ops_args: list[str],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Poll a submitted deployment to a terminal state with short `show` calls.

        The only authoritative outcomes are the deployment's own `provisioningState`
        (Succeeded, or Failed/Canceled) and the overall deadline. Any failure to OBSERVE
        the deployment (auth blip, throttling, 5xx, a torn credential-cache read, or a
        transient not-found right after submit) never fails the deployment. It only
        retries under a grace window, because the deployment is owned by ARM and its fate
        is independent of our ability to read it.

        Args:
            show_args: `az deployment ... show` arguments.
            ops_args: `az deployment operation ... list` arguments for failure detail.
            deployment_name: Name for the Azure deployment.
            step_name: Site Ops step name.
            site_name: Site Ops site name.

        Returns:
            DeploymentResult. On success, `outputs` carries `properties.outputs`.
        """
        start = time.monotonic()
        deadline = start + DEFAULT_AZ_TIMEOUT_SECONDS
        last_clean_obs = start
        ever_visible = False
        last_obs_error: str | None = None
        last_state: str | None = None
        poll_count = 0

        while True:
            poll_count += 1
            ok, stdout, stderr = self._run_az(
                show_args,
                timeout=DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS,
                site_name=site_name,
            )

            deployment_obj: dict[str, Any] | None = None
            observed_state: str | None = None
            if ok and stdout:
                try:
                    parsed = json.loads(stdout)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    deployment_obj = parsed
                    state = parsed.get("properties", {}).get("provisioningState")
                    observed_state = state if isinstance(state, str) and state else None

            if observed_state is not None:
                ever_visible = True
                last_clean_obs = time.monotonic()
                last_obs_error = None
                last_state = observed_state

                if observed_state in _DEPLOYMENT_TERMINAL_SUCCESS:
                    outputs = deployment_obj.get("properties", {}).get("outputs") or {}
                    logger.debug(
                        scrub_site_for_output(
                            f"Deployment '{deployment_name}' succeeded after "
                            f"{poll_count} poll(s)",
                            site_name,
                        )
                    )
                    return DeploymentResult(
                        success=True,
                        step_name=step_name,
                        site_name=site_name,
                        deployment_name=deployment_name,
                        outputs=outputs,
                    )

                if observed_state in _DEPLOYMENT_TERMINAL_FAILURE:
                    detail = self._format_deployment_failure(
                        deployment_obj,
                        ops_args,
                        site_name,
                    )
                    return DeploymentResult(
                        success=False,
                        step_name=step_name,
                        site_name=site_name,
                        deployment_name=deployment_name,
                        error=(
                            f"Deployment '{deployment_name}' reached terminal state "
                            f"'{observed_state}'. {detail}"
                        ),
                    )
                # Intermediate state. Keep polling.
            else:
                # Could not observe a provisioningState this poll. This never fails the
                # deployment, only bounds how long we keep trying to read it.
                last_obs_error = (
                    stderr or "deployment show returned no parseable provisioningState"
                )
                category = _classify_az_error(stderr) if stderr else "unknown"
                now = time.monotonic()

                if category == "resource_not_found" and not ever_visible:
                    if now - start > DEPLOYMENT_NOTFOUND_GRACE_SECONDS:
                        return DeploymentResult(
                            success=False,
                            step_name=step_name,
                            site_name=site_name,
                            deployment_name=deployment_name,
                            error=(
                                f"Deployment '{deployment_name}' never became visible within "
                                f"{DEPLOYMENT_NOTFOUND_GRACE_SECONDS}s of submit. Verify the "
                                f"create and show target the same subscription and resource "
                                f"group. Last error: {last_obs_error}"
                            ),
                        )
                    # Still within the registration window. Keep polling.
                elif now - last_clean_obs > DEPLOYMENT_OBSERVATION_GRACE_SECONDS:
                    return DeploymentResult(
                        success=False,
                        step_name=step_name,
                        site_name=site_name,
                        deployment_name=deployment_name,
                        error=(
                            f"Lost the ability to observe deployment '{deployment_name}' for "
                            f"over {DEPLOYMENT_OBSERVATION_GRACE_SECONDS}s (last observed state: "
                            f"{last_state or 'none'}). ARM was not canceled and may still be "
                            f"running. Last error: {last_obs_error}"
                        ),
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return DeploymentResult(
                    success=False,
                    step_name=step_name,
                    site_name=site_name,
                    deployment_name=deployment_name,
                    error=(
                        f"Deployment '{deployment_name}' did not reach a terminal state within "
                        f"{DEFAULT_AZ_TIMEOUT_SECONDS // 60}m (last observed state: "
                        f"{last_state or 'none'}). ARM was not canceled and may still be in "
                        f"progress."
                    ),
                )
            time.sleep(min(DEFAULT_DEPLOYMENT_POLL_INTERVAL_SECONDS, remaining))

    def _format_deployment_failure(
        self,
        deployment_obj: dict[str, Any],
        ops_args: list[str],
        site_name: str = "",
    ) -> str:
        """Build a diagnostic for a Failed or Canceled deployment.

        The deployment's own `properties.error` is usually the shallow generic node ("At
        least one resource deployment operation failed"). The real per-resource cause
        lives in the deployment operations, so fetch those first and fall back to the
        top-level error only when the operations cannot be read.
        """
        detail = self._fetch_failed_operations(ops_args, site_name)
        if detail:
            return detail
        error_node = deployment_obj.get("properties", {}).get("error")
        if error_node:
            return _format_arm_error(error_node)
        return "No error detail was reported by ARM."

    def _fetch_failed_operations(
        self,
        ops_args: list[str],
        site_name: str = "",
    ) -> str | None:
        """Fetch failed deployment operations and format their root-cause errors.

        Returns a joined diagnostic string, or None when the operations cannot be read so
        the caller falls back to the deployment's top-level error.
        """
        ok, stdout, _stderr = self._run_az(
            ops_args,
            timeout=DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS,
            site_name=site_name,
        )
        if not ok or not stdout:
            return None
        try:
            operations = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(operations, list):
            return None

        messages: list[str] = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            props = operation.get("properties", {})
            if props.get("provisioningState") != "Failed":
                continue
            status_message = props.get("statusMessage")
            error_node = None
            if isinstance(status_message, dict):
                error_node = status_message.get("error", status_message)
            target = props.get("targetResource") or {}
            target_label = _operation_target_label(target)
            formatted = (
                _format_arm_error(error_node)
                if error_node
                else json.dumps(status_message)
            )
            messages.append(
                f"{target_label}: {formatted}" if target_label else formatted
            )

        return "; ".join(messages) if messages else None

    def deploy_resource_group(
        self,
        subscription: str,
        resource_group: str,
        template_path: Path,
        parameters: dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Deploy a Bicep/ARM template to a resource group.

        Args:
            subscription: Azure subscription ID
            resource_group: Target resource group name
            template_path: Path to the template file
            parameters: Deployment parameters
            deployment_name: Name for the Azure deployment
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            DeploymentResult with success status and outputs
        """
        create_args = [
            "deployment",
            "group",
            "create",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--template-file",
            str(template_path),
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        show_args = [
            "deployment",
            "group",
            "show",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        ops_args = [
            "deployment",
            "operation",
            "group",
            "list",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        return self._deploy(
            create_args, show_args, ops_args, parameters, deployment_name, step_name, site_name
        )

    def deploy_subscription(
        self,
        subscription: str,
        location: str,
        template_path: Path,
        parameters: dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Deploy a Bicep/ARM template at subscription scope.

        Args:
            subscription: Azure subscription ID
            location: Azure region for deployment metadata
            template_path: Path to the template file
            parameters: Deployment parameters
            deployment_name: Name for the Azure deployment
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            DeploymentResult with success status and outputs
        """
        create_args = [
            "deployment",
            "sub",
            "create",
            "--subscription",
            subscription,
            "--location",
            location,
            "--template-file",
            str(template_path),
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        show_args = [
            "deployment",
            "sub",
            "show",
            "--subscription",
            subscription,
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        ops_args = [
            "deployment",
            "operation",
            "sub",
            "list",
            "--subscription",
            subscription,
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        return self._deploy(
            create_args, show_args, ops_args, parameters, deployment_name, step_name, site_name
        )

    def _validate_kubectl_file(self, file_path: str) -> tuple[bool, str | None]:
        """Validate a kubectl file path or URL for security.

        Security checks:
        - URLs must be HTTPS (HTTP not allowed)
        - Local paths cannot traverse outside workspace

        Args:
            file_path: Local file path or URL

        Returns:
            Tuple of (is_valid, error_message)
        """
        if HTTPS_URL_PATTERN.match(file_path):
            return True, None

        if file_path.lower().startswith("http://"):
            return False, f"HTTP URLs not allowed for security (use HTTPS): {file_path}"

        try:
            resolved = (self.workspace / file_path).resolve()
            resolved.relative_to(self.workspace)
        except ValueError:
            return False, f"Path traversal not allowed (must be within workspace): {file_path}"

        if not resolved.exists():
            return False, f"File not found: {file_path}"

        return True, None

    def kubectl_apply(
        self,
        cluster_name: str,
        resource_group: str,
        subscription: str,
        files: list[str],
        step_name: str,
        site_name: str,
    ) -> KubectlResult:
        """Apply Kubernetes manifests to an Arc-connected cluster.

        Manages the full lifecycle:
        1. Start `az connectedk8s proxy` in background
        2. Wait for proxy to establish (~25 seconds)
        3. Run `kubectl apply -f` for all files
        4. Terminate proxy

        Args:
            cluster_name: Name of the Arc-connected cluster
            resource_group: Resource group containing the cluster
            subscription: Azure subscription ID
            files: List of file paths (workspace-relative) or HTTPS URLs
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            KubectlResult with success status
        """
        # Validate all files first
        resolved_files: list[str] = []
        for file_path in files:
            is_valid, error = self._validate_kubectl_file(file_path)
            if not is_valid:
                return KubectlResult(
                    success=False,
                    step_name=step_name,
                    site_name=site_name,
                    error=error,
                )

            if HTTPS_URL_PATTERN.match(file_path):
                resolved_files.append(file_path)
            else:
                resolved_files.append(str((self.workspace / file_path).resolve()))

        if self.dry_run:
            files_display = ", ".join(files)
            logger.info(
                scrub_for_output(
                    f"[DRY-RUN] kubectl apply via Arc proxy ({cluster_name}): {files_display}"
                )
            )
            return KubectlResult(success=True, step_name=step_name, site_name=site_name)

        if not self.kubectl_path:
            return KubectlResult(
                success=False,
                step_name=step_name,
                site_name=site_name,
                error="kubectl not found in PATH",
            )

        with self._arc_proxy(cluster_name, resource_group, subscription) as arc_kubeconfig:
            if arc_kubeconfig is None:
                return KubectlResult(
                    success=False,
                    step_name=step_name,
                    site_name=site_name,
                    error="Failed to establish Arc proxy connection",
                )

            args = ["apply"]
            for f in resolved_files:
                args.extend(["-f", f])

            success, stdout, stderr = self._run_kubectl(args, kubeconfig=arc_kubeconfig)

            if success and stdout:
                logger.debug(f"kubectl output:\n{scrub_for_output(stdout)}")

            return KubectlResult(
                success=success,
                step_name=step_name,
                site_name=site_name,
                error=stderr if not success else None,
            )

    @contextmanager
    def _condition_session(self, condition: Any) -> Generator[None, None, None]:
        """Per-condition context wrapping the wait loop. No-op for `arm-tag`.

        Reserved for future condition types that need a long-lived resource
        (for example an Arc proxy) spanning all polls.
        """
        with nullcontext():
            yield

    def _evaluate_condition(self, condition: Any, subscription: str) -> tuple[WaitState, str | None, str | None]:
        """Take one observation of a wait condition.

        Returns `(state, observed_value, error)`. `observed_value` is the value
        seen this poll (or None). `error` is a non-fatal poll error worth
        surfacing in the timeout diagnostic. On a PENDING state it also drives
        the consecutive-error circuit breaker.

        Dispatches by condition type. Unknown types are rejected at parse and
        validate time, so this guard is defensive.
        """
        if condition.type == "arm-tag":
            return self._evaluate_arm_tag(condition, subscription)
        raise ValueError(f"Unsupported wait condition type: {condition.type}")

    def _evaluate_arm_tag(self, condition: Any, subscription: str) -> tuple[WaitState, str | None, str | None]:
        """Take one observation of an arm-tag condition via `az resource show`."""
        args = [
            "resource",
            "show",
            "--ids",
            condition.resource_id,
            "--query",
            "tags",
            "--output",
            "json",
        ]
        if subscription:
            # Inline --subscription per call. Never `az account set`, which
            # mutates global state and races across concurrent site threads.
            args.extend(["--subscription", subscription])

        success, stdout, stderr = self._run_az(args, timeout=DEFAULT_WAIT_POLL_AZ_TIMEOUT_SECONDS)

        if success:
            try:
                tags = json.loads(stdout) if stdout.strip() else {}
            except json.JSONDecodeError as e:
                # A malformed tags payload is unexpected. Treat as a transient
                # hiccup and keep polling, but surface it for the diagnostic.
                return WaitState.PENDING, None, f"could not parse tags JSON: {e}"
            if not isinstance(tags, dict):
                tags = {}

            observed = tags.get(condition.tag_key)
            if observed is not None:
                observed = str(observed)

            # Satisfied (exact) is checked before failed (glob), so a failure
            # glob can never override an exact success match.
            if observed == condition.expected_value:
                return WaitState.SATISFIED, observed, None
            if (
                condition.failure_pattern
                and observed is not None
                and fnmatchcase(observed, condition.failure_pattern)
            ):
                return WaitState.FAILED, observed, (
                    f"tag reached failure value '{observed}' matching failurePattern "
                    f"'{condition.failure_pattern}'"
                )
            # Tag absent or some intermediate value. Not ready yet.
            return WaitState.PENDING, observed, None

        # The `az` call failed. Classify so permanent errors fail fast instead
        # of polling for the full timeout.
        classification = _classify_az_error(stderr)
        message = stderr.strip()
        if classification == "resource_not_found":
            return WaitState.FAILED, None, (
                f"resource not found: {condition.resource_id}. For arm-tag the resource is "
                f"expected to exist before the wait. Check the resourceId (subscription, resource "
                f"group, name). az error: {message}"
            )
        if classification == "permanent":
            return WaitState.FAILED, None, f"permanent error polling tags: {message}"
        # transient or unknown: keep polling, surface for the diagnostic.
        return WaitState.PENDING, None, message or "transient error polling tags"

    def wait_for_condition(
        self,
        condition: Any,
        timeout_minutes: int,
        poll_interval_seconds: int,
        subscription: str,
        step_name: str,
        site_name: str,
    ) -> WaitResult:
        """Poll `condition` until satisfied, failed, or the timeout elapses.

        Evaluates before sleeping (an already-satisfied condition returns on the
        first poll), uses a monotonic deadline, and clamps the final sleep so the
        wait does not overshoot the timeout. A permanent error or a failure-glob
        match aborts fast. A run of consecutive polling errors trips a circuit
        breaker so a broken `az`/network does not burn the full timeout.

        Args:
            condition: The wait condition (already template-resolved).
            timeout_minutes: Maximum minutes to wait before failing.
            poll_interval_seconds: Seconds between observations.
            subscription: Subscription passed inline to each `az` call.
            step_name: Site Ops step name.
            site_name: Site Ops site name.

        Returns:
            WaitResult. On failure, `error` carries the full diagnostic.
        """
        description = _describe_condition(condition)

        if self.dry_run:
            logger.info(
                scrub_for_output(
                    f"[DRY-RUN] would wait for {description} "
                    f"(timeout {timeout_minutes}m, poll {poll_interval_seconds}s)"
                )
            )
            return WaitResult(success=True, step_name=step_name, site_name=site_name)

        start = time.monotonic()
        deadline = start + timeout_minutes * 60
        last_value: str | None = None
        last_error: str | None = None
        poll_count = 0
        consecutive_errors = 0

        with self._condition_session(condition):
            while True:
                poll_count += 1
                state, observed, error = self._evaluate_condition(condition, subscription)
                if observed is not None:
                    last_value = observed
                if error is not None:
                    last_error = error

                if state == WaitState.SATISFIED:
                    logger.debug(
                        scrub_for_output(f"{description} satisfied after {poll_count} poll(s)")
                    )
                    return WaitResult(success=True, step_name=step_name, site_name=site_name)

                if state == WaitState.FAILED:
                    return WaitResult(
                        success=False,
                        step_name=step_name,
                        site_name=site_name,
                        error=_wait_failure_message(
                            condition,
                            reason="condition reached a terminal failure",
                            last_value=last_value,
                            last_error=error or last_error,
                            poll_count=poll_count,
                            elapsed_seconds=time.monotonic() - start,
                        ),
                    )

                # PENDING. Track consecutive polling errors for the breaker.
                if error is not None:
                    consecutive_errors += 1
                    if consecutive_errors >= WAIT_MAX_CONSECUTIVE_ERRORS:
                        return WaitResult(
                            success=False,
                            step_name=step_name,
                            site_name=site_name,
                            error=_wait_failure_message(
                                condition,
                                reason=f"{consecutive_errors} consecutive polling errors",
                                last_value=last_value,
                                last_error=last_error,
                                poll_count=poll_count,
                                elapsed_seconds=time.monotonic() - start,
                            ),
                        )
                else:
                    consecutive_errors = 0

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return WaitResult(
                        success=False,
                        step_name=step_name,
                        site_name=site_name,
                        error=_wait_failure_message(
                            condition,
                            reason=f"timed out after {timeout_minutes}m",
                            last_value=last_value,
                            last_error=last_error,
                            poll_count=poll_count,
                            elapsed_seconds=time.monotonic() - start,
                        ),
                    )
                time.sleep(min(poll_interval_seconds, remaining))

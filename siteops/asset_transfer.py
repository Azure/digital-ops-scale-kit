# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Opaque HTTPS acquisition with protected staging and a supervised deadline."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from siteops import _http_asset_worker as worker
from siteops.artifacts import ArtifactError, hash_file, load_artifact_json, open_regular_file
from siteops.cache_filesystem import (
    check_cache_ancestors,
    check_private_node,
    make_private_directory,
)
from siteops.process_capture import BoundedCapture
from siteops.workspace_source import ArtifactIdentity

logger = logging.getLogger(__name__)
_STOP_SECONDS = 2
_WORKER = Path(worker.__file__).resolve()
_ENVIRONMENT = {
    "systemroot", "windir", "systemdrive", "temp", "tmp", "tmpdir",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "ssl_cert_file", "ssl_cert_dir",
}
_FAILURES = {
    "input": "The asset transfer request is invalid.",
    "url": "The asset URL must use an approved HTTPS origin.",
    "redirect": "The asset redirect is outside the approved transfer policy.",
    "response": "The asset response has unsupported HTTP framing.",
    "encoding": "The asset response must use identity content encoding.",
    "identity": "The downloaded asset differs from its expected size or SHA-256.",
    "io": "The asset transfer could not complete its file access.",
    "timeout": "The asset transfer exceeded its deadline. Retry the acquisition.",
    "network": "The asset source could not complete the transfer. Retry the acquisition.",
    "http": "The asset source rejected the transfer request.",
    "auth": "The selected source access does not permit this asset read.",
    "not-found": "The selected asset was not found or is not accessible.",
    "rate-limit": "The asset source rate limit was reached. Retry after the limit resets.",
    "worker": "The asset transfer worker did not return a complete result.",
}


class AssetTransferError(ArtifactError):
    """Safe failure text with optional numeric HTTP retry information."""

    def __init__(
        self, reason: str, *, http_status: int | None = None,
        retry_after: int | None = None, reset_at: int | None = None,
    ):
        super().__init__(_FAILURES[reason], code=f"source.{reason}")
        self.http_status = http_status
        self.retry_after = retry_after
        self.reset_at = reset_at


class _WorkerStillRunning(AssetTransferError):
    def __init__(self):
        super().__init__("worker")


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_STOP_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            raise _WorkerStillRunning() from None


def _run_worker(operation: Path, request: bytes, timeout: float) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    request_path = operation / "request.json"
    descriptor = os.open(
        request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(request)
    check_private_node(request_path, directory=False)
    with open_regular_file(request_path) as source:
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", str(_WORKER)],
                stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=operation, shell=False, bufsize=0,
                env={key: value for key, value in os.environ.items() if key.lower() in _ENVIRONMENT},
            )
        except OSError:
            raise AssetTransferError("worker") from None
        readers = []
        stdout = BoundedCapture.create(worker.MAX_STATUS_BYTES)
        stderr = BoundedCapture.create(worker.MAX_STATUS_BYTES)
        try:
            if process.stdout is None or process.stderr is None:
                raise AssetTransferError("worker")
            for capture, stream in ((stdout, process.stdout), (stderr, process.stderr)):
                reader = threading.Thread(target=capture.read, args=(stream,), daemon=True)
                reader.start()
                readers.append(reader)
            while process.poll() is None:
                if any(capture.exceeded.is_set() or capture.failed.is_set() for capture in (stdout, stderr)):
                    raise AssetTransferError("worker")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssetTransferError("timeout")
                time.sleep(min(0.01, remaining))
            if time.monotonic() >= deadline:
                raise AssetTransferError("timeout")
            for reader in readers:
                reader.join(timeout=_STOP_SECONDS)
            if any(reader.is_alive() for reader in readers) or any(
                capture.exceeded.is_set() or capture.failed.is_set() for capture in (stdout, stderr)
            ) or stderr.content:
                raise AssetTransferError("worker")
            return process.returncode, bytes(stdout.content)
        finally:
            _stop_worker(process)
            for reader in readers:
                reader.join(timeout=_STOP_SECONDS)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _check_result(returncode: int, raw: bytes, identity: ArtifactIdentity) -> None:
    try:
        result = load_artifact_json(raw, limit=worker.MAX_STATUS_BYTES, label="Asset transfer result")
    except ArtifactError:
        raise AssetTransferError("worker") from None
    if not isinstance(result, dict):
        raise AssetTransferError("worker")
    status = result.get("status")
    if status == "ok":
        if (
            returncode != 0 or result.keys() != {"status", "size", "sha256"}
            or type(result["size"]) is not int
            or (result["size"], result["sha256"]) != (identity.size, identity.sha256)
        ):
            raise AssetTransferError("worker")
        return
    if (
        returncode != 1 or not isinstance(status, str) or status not in _FAILURES
        or result.keys() - {"status", "httpStatus", "retryAfter", "resetAt"}
    ):
        raise AssetTransferError("worker")
    for name in ("httpStatus", "retryAfter", "resetAt"):
        if name in result and (type(result[name]) is not int or not 0 <= result[name] <= 2**63 - 1):
            raise AssetTransferError("worker")
    http_status = result.get("httpStatus")
    if http_status is not None and not 100 <= http_status <= 599:
        raise AssetTransferError("worker")
    if status == "http":
        status = {
            401: "auth", 403: "auth", 404: "not-found", 408: "timeout", 504: "timeout",
        }.get(http_status, "network" if http_status is not None and http_status >= 500 else "http")
    raise AssetTransferError(
        status, http_status=http_status,
        retry_after=result.get("retryAfter"), reset_at=result.get("resetAt"),
    )


@contextmanager
def download_https_asset(
    url: str, identity: ArtifactIdentity, *, origins: tuple[str, ...],
    staging_parent: Path, timeout: float = 120,
) -> Iterator[Path]:
    """Yield identified opaque bytes until context exit, without parsing or authorizing them.

    Trusted adapter code supplies the URL and permitted origins. The caller owns
    the existing private staging parent. A new child contains each transfer and
    is removed only after its worker has exited. TLS and configured proxies use
    the standard library. This anonymous path reads no source credentials.
    """
    if (
        not isinstance(identity, ArtifactIdentity) or not isinstance(origins, tuple)
        or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300
    ):
        raise AssetTransferError("input")
    value: dict[str, Any] = {
        "url": url, "origins": list(origins), "size": identity.size, "sha256": identity.sha256,
    }
    try:
        worker.validate_request(value)
    except worker.TransferFailure as error:
        raise AssetTransferError(str(error)) from None
    request = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(request) > worker.MAX_REQUEST_BYTES:
        raise AssetTransferError("input")
    operation = Path(staging_parent) / f"transfer-{uuid.uuid4().hex}"
    try:
        check_cache_ancestors(operation)
        check_private_node(operation.parent, directory=True)
        make_private_directory(operation)
    except OSError:
        raise AssetTransferError("io") from None
    removable = True
    try:
        try:
            check_private_node(operation, directory=True)
            returncode, raw = _run_worker(operation, request, timeout)
            _check_result(returncode, raw, identity)
            asset = operation / "asset.bin"
            check_private_node(asset, directory=False)
            if hash_file(asset, limit=identity.size) != (identity.size, identity.sha256):
                raise AssetTransferError("identity")
        except _WorkerStillRunning:
            removable = False
            raise
        except OSError:
            raise AssetTransferError("io") from None
        yield asset
    finally:
        if removable:
            try:
                check_private_node(operation, directory=True)
                shutil.rmtree(operation)
            except (OSError, ArtifactError):
                logger.warning("Asset transfer staging cleanup could not be completed.")
        else:
            logger.warning("Asset transfer staging was retained because worker exit could not be confirmed.")

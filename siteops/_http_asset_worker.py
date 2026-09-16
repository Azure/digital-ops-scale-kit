# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run isolated HTTPS transfers using only the Python standard library."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

MAX_REQUEST_BYTES = 32 * 1024
MAX_ASSET_BYTES = 128 * 1024 * 1024
MAX_REDIRECTS = 3
READ_BYTES = 64 * 1024
MAX_STATUS_BYTES = 4096
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class TransferFailure(Exception):
    def __init__(
        self, code: str, *, status: int | None = None,
        retry_after: int | None = None, reset_at: int | None = None,
    ):
        super().__init__(code)
        self.result: dict[str, str | int] = {"status": code}
        for key, value in (
            ("httpStatus", status), ("retryAfter", retry_after), ("resetAt", reset_at),
        ):
            if value is not None:
                self.result[key] = value


def _origin(value: str) -> tuple[str, int]:
    if (
        not isinstance(value, str) or not value or len(value) > 8192
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise TransferFailure("url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise TransferFailure("url") from None
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or "#" in value or "\\" in value or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise TransferFailure("url")
    hostname = parsed.hostname.lower()
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise TransferFailure("url") from None
    elif len(hostname) > 253 or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
        for label in hostname.split(".")
    ):
        raise TransferFailure("url")
    return hostname, 443 if port is None else port


def validate_request(value: Any) -> set[tuple[str, int]]:
    if not isinstance(value, dict) or value.keys() != {"url", "origins", "size", "sha256"}:
        raise TransferFailure("input")
    origins = value["origins"]
    if not isinstance(origins, list) or not 1 <= len(origins) <= 8:
        raise TransferFailure("input")
    approved = set()
    for origin in origins:
        identity = _origin(origin)
        parsed = urlsplit(origin)
        if parsed.path not in {"", "/"} or parsed.query:
            raise TransferFailure("input")
        approved.add(identity)
    if len(approved) != len(origins) or _origin(value["url"]) not in approved:
        raise TransferFailure("url")
    size = value["size"]
    digest = value["sha256"]
    if type(size) is not int or not 0 < size <= MAX_ASSET_BYTES:
        raise TransferFailure("input")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise TransferFailure("input")
    return approved


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "siteops-artifact-source",
        },
    )


def _header_integer(headers: Message, name: str) -> int | None:
    values = headers.get_all(name, [])
    if len(values) == 1 and len(values[0]) <= 20 and values[0].isascii() and values[0].isdigit():
        value = int(values[0])
        if value <= 2**63 - 1:
            return value
    return None


def _retry_after(headers: Message) -> int | None:
    seconds = _header_integer(headers, "Retry-After")
    if seconds is not None:
        return seconds
    values = headers.get_all("Retry-After", [])
    if len(values) != 1 or len(values[0]) > 128:
        return None
    try:
        moment = parsedate_to_datetime(values[0])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0, math.ceil(moment.timestamp() - time.time()))
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _response(url: str, origins: set[tuple[str, int]]) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _RejectRedirects())
    for hop in range(MAX_REDIRECTS + 1):
        try:
            response = opener.open(_request(url), timeout=15)
        except urllib.error.HTTPError as error:
            try:
                if error.code in _REDIRECT_CODES:
                    locations = error.headers.get_all("Location", [])
                    if hop == MAX_REDIRECTS or len(locations) != 1 or len(locations[0]) > 8192:
                        raise TransferFailure("redirect")
                    location = locations[0]
                    if (
                        not location or any(not 33 <= ord(character) <= 126 for character in location)
                        or "#" in location or "\\" in location
                    ):
                        raise TransferFailure("redirect")
                    url = urljoin(url, location)
                    if _origin(url) not in origins:
                        raise TransferFailure("redirect")
                    continue
                code = "http"
                if error.code == 429 or (
                    error.code == 403 and (
                        _header_integer(error.headers, "X-RateLimit-Remaining") == 0
                        or error.headers.get("Retry-After") is not None
                    )
                ):
                    code = "rate-limit"
                raise TransferFailure(
                    code, status=error.code,
                    retry_after=_retry_after(error.headers),
                    reset_at=_header_integer(error.headers, "X-RateLimit-Reset"),
                ) from None
            finally:
                # Close without reading an unbounded redirect or error body.
                error.close()
        else:
            if response.geturl() != url:
                response.close()
                raise TransferFailure("redirect")
            return response
    raise TransferFailure("redirect")


def _copy_response(value: dict[str, Any], response: Any, destination: str) -> dict[str, Any]:
    with response:
        if response.status != 200:
            raise TransferFailure("response")
        encodings = response.headers.get_all("Content-Encoding", [])
        if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != "identity"):
            raise TransferFailure("encoding")
        transfers = response.headers.get_all("Transfer-Encoding", [])
        lengths = response.headers.get_all("Content-Length", [])
        if transfers and (
            len(transfers) != 1 or transfers[0].strip().lower() != "chunked" or lengths
        ):
            raise TransferFailure("response")
        if lengths and (
            len(lengths) != 1 or _header_integer(response.headers, "Content-Length") != value["size"]
        ):
            raise TransferFailure("identity")
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    try:
                        chunk = response.read1(min(READ_BYTES, value["size"] - size + 1))
                    except TimeoutError:
                        raise TransferFailure("timeout") from None
                    except (OSError, http.client.HTTPException):
                        raise TransferFailure("network") from None
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > value["size"]:
                        raise TransferFailure("identity")
                    digest.update(chunk)
                    output.write(chunk)
                if size != value["size"] or digest.hexdigest() != value["sha256"]:
                    raise TransferFailure("identity")
                output.flush()
                os.fsync(output.fileno())
        except TimeoutError:
            raise TransferFailure("timeout") from None
        except OSError:
            raise TransferFailure("io") from None
    return {"status": "ok", "size": size, "sha256": digest.hexdigest()}


def transfer(value: Any, destination: str) -> dict[str, Any]:
    origins = validate_request(value)
    try:
        return _copy_response(value, _response(value["url"], origins), destination)
    except (TimeoutError, urllib.error.URLError) as error:
        code = "timeout" if isinstance(error, TimeoutError) or isinstance(
            getattr(error, "reason", None), TimeoutError,
        ) else "network"
        raise TransferFailure(code) from None
    except (OSError, http.client.HTTPException, ValueError):
        raise TransferFailure("network") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransferFailure("input")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise TransferFailure("input")


def main() -> int:
    # Limit parser state only in this isolated process.
    http.client._MAXLINE = 8192
    http.client._MAXHEADERS = 64
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise TransferFailure("input")
        try:
            request = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant,
            )
        except (ValueError, UnicodeError, RecursionError):
            raise TransferFailure("input") from None
        result = transfer(request, "asset.bin")
    except TransferFailure as error:
        result = error.result
    except OSError:
        result = {"status": "io"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

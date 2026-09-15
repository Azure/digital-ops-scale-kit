# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded, read-only access to repository metadata through the GitHub API."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable
from urllib.parse import quote, urlsplit

from siteops.browse import BrowseError
from siteops.compilation import resolve_tool_from_path
from siteops.process_capture import BoundedCapture as _BoundedCapture

GITHUB_API_VERSION = "2026-03-10"
_API_ROOT = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_USER_AGENT = "siteops-github-source"
_NETWORK_TIMEOUT_SECONDS = 15.0
_CLI_TIMEOUT_SECONDS = 20.0
_PROCESS_STOP_SECONDS = 1.0
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_TREE_ENTRIES = 20_000
_RELEASE_ASSET_PAGE_SIZE = 100
_MAX_RELEASE_ASSETS = 256
_MAX_TAG_DEPTH = 8
logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_MODE_TYPES = {
    "040000": "tree",
    "100644": "blob",
    "100755": "blob",
    "120000": "blob",
    "160000": "commit",
}
_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})

Transport = Callable[[str], Any]


def _error(code: str, summary: str) -> BrowseError:
    return BrowseError(code, summary)


def _validate_owner(owner: object) -> str:
    if (
        not isinstance(owner, str)
        or not _OWNER_RE.fullmatch(owner)
        or "--" in owner
    ):
        raise _error(
            "github.reference",
            "GitHub repository owner must be a valid account or organization name.",
        )
    return owner


def _validate_repository(repository: object) -> str:
    if (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.fullmatch(repository)
        or repository in {".", ".."}
    ):
        raise _error(
            "github.reference",
            "GitHub repository name contains unsupported characters.",
        )
    return repository


def _validate_ref(ref: object) -> str:
    if not isinstance(ref, str) or not ref or len(ref) > 1024:
        raise _error("github.reference", "GitHub reference is invalid.")
    if (
        ref != ref.strip()
        or ref == "@"
        or ref.startswith("/")
        or ref.endswith(("/", "."))
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
        or any(character in " ~^:?*[\\\x7f" for character in ref)
    ):
        raise _error("github.reference", "GitHub reference is invalid.")
    for component in ref.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            raise _error("github.reference", "GitHub reference is invalid.")
    return ref


def _validate_sha(value: object, *, summary: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise _error("github.invalid-data", summary)
    return value.lower()


def _validate_tree_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip("/"):
        raise _error("github.invalid-data", "GitHub returned an invalid tree path.")
    if (
        "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _error("github.invalid-data", "GitHub returned an invalid tree path.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error("github.invalid-data", "GitHub returned a non-canonical tree path.")
    if PurePosixPath(value).as_posix() != value:
        raise _error("github.invalid-data", "GitHub returned a non-canonical tree path.")
    return value


def _validate_size(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _error("github.invalid-data", "GitHub returned an invalid object size.")
    return value


@dataclass(frozen=True)
class GitHubReference:
    """A GitHub repository and optional branch, tag, or commit reference."""

    owner: str
    repository: str
    ref: str | None = None

    def __post_init__(self) -> None:
        _validate_owner(self.owner)
        _validate_repository(self.repository)
        if self.ref is not None:
            _validate_ref(self.ref)

    @classmethod
    def parse(cls, value: str, ref: str | None = None) -> GitHubReference:
        """Parse a canonical GitHub locator without contacting GitHub."""
        if not isinstance(value, str) or not value or value != value.strip():
            raise _error("github.reference", "GitHub repository locator is invalid.")

        embedded_ref: str | None = None
        if value.startswith("github:"):
            locator = value[len("github:") :]
            if "@" in locator:
                locator, embedded_ref = locator.rsplit("@", 1)
                if ref is not None:
                    raise _error(
                        "github.reference",
                        "GitHub reference was specified more than once.",
                    )
            components = locator.split("/")
            if len(components) != 2:
                raise _error(
                    "github.reference",
                    "GitHub locator must identify exactly one owner and repository.",
                )
            owner, repository = components
        else:
            parsed = urlsplit(value)
            try:
                port = parsed.port
            except ValueError:
                raise _error("github.reference", "GitHub repository URL is invalid.") from None
            if (
                parsed.scheme.lower() != "https"
                or parsed.hostname is None
                or parsed.hostname.lower() != "github.com"
                or parsed.username is not None
                or parsed.password is not None
                or port is not None
                or "?" in value
                or "#" in value
                or parsed.query
                or parsed.fragment
            ):
                raise _error(
                    "github.reference",
                    "Only root https://github.com/owner/repository URLs are supported.",
                )
            if parsed.path.endswith("//"):
                raise _error(
                    "github.reference",
                    "Use a root repository URL, with --ref for the revision and -w for the workspace.",
                )
            path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
            components = path.split("/")
            if len(components) != 3 or components[0]:
                raise _error(
                    "github.reference",
                    "Use a root repository URL, with --ref for the revision and -w for the workspace.",
                )
            owner, repository = components[1:]
            if repository.endswith(".git"):
                repository = repository[:-4]

        selected_ref = embedded_ref if embedded_ref is not None else ref
        return cls(owner, repository, selected_ref)

    @property
    def web_url(self) -> str:
        """Return the canonical repository web URL."""
        return f"https://github.com/{self.owner}/{self.repository}"


@dataclass(frozen=True)
class GitHubTreeEntry:
    """A validated entry from a Git tree, including links and submodules."""

    path: str
    sha: str
    type: str
    mode: str
    size: int | None

    def __post_init__(self) -> None:
        _validate_tree_path(self.path)
        object.__setattr__(
            self, "sha", _validate_sha(self.sha, summary="GitHub returned an invalid tree object ID.")
        )
        if not isinstance(self.mode, str) or not isinstance(self.type, str):
            raise _error("github.invalid-data", "GitHub returned an invalid tree entry type.")
        expected_type = _MODE_TYPES.get(self.mode)
        if expected_type is None or self.type != expected_type:
            raise _error(
                "github.invalid-data",
                "GitHub returned an unsupported tree entry mode or type.",
            )
        _validate_size(self.size, optional=True)
        if self.type != "blob" and self.size is not None:
            raise _error(
                "github.invalid-data",
                "GitHub returned an unexpected size for a non-blob tree entry.",
            )


def _release_identifier(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise _error("github.invalid-data", "GitHub returned an invalid release or asset ID.")
    return value


def _release_asset_name(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 255
        or value != value.strip() or "/" in value or "\\" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise _error("github.invalid-data", "GitHub returned an invalid release asset name.")
    return value


@dataclass(frozen=True)
class GitHubReleaseAsset:
    """Observed asset identity, not a trusted local path or publisher assertion."""

    identifier: int
    name: str
    size: int
    sha256: str | None

    @classmethod
    def from_metadata(cls, value: Any) -> GitHubReleaseAsset:
        if not isinstance(value, dict):
            raise _error("github.invalid-data", "GitHub returned invalid release asset metadata.")
        identifier = _release_identifier(value.get("id"))
        name = _release_asset_name(value.get("name"))
        size = value.get("size")
        if type(size) is not int or not 0 <= size <= 2**63 - 1:
            raise _error("github.invalid-data", "GitHub returned an invalid release asset size.")
        if value.get("state") != "uploaded":
            raise _error("github.release-incomplete", "Release assets are not ready for acquisition.")
        digest = value.get("digest")
        if digest is not None:
            if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise _error("github.invalid-data", "GitHub returned an unsupported release asset digest.")
            digest = digest.removeprefix("sha256:")
        return cls(identifier, name, size, digest)

    def require_digest(self) -> str:
        if self.sha256 is None:
            raise _error("github.asset-digest-missing", "The selected release asset has no SHA-256 digest.")
        return self.sha256


@dataclass(frozen=True)
class GitHubReleaseSnapshot:
    """A published release observed at an exact tag object and source commit.

    Asset and tag observations are source metadata. They do not authenticate
    package bytes or replace consumer provenance policy.
    """

    reference: GitHubReference
    repository_id: int
    release_id: int
    tag_object: str
    source_commit: str
    prerelease: bool
    published_at: datetime
    immutable: bool | None
    assets: tuple[GitHubReleaseAsset, ...]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: BinaryIO,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _validate_route(route: object) -> str:
    if not isinstance(route, str) or not route.startswith("/") or route.startswith("//"):
        raise _error("github.invalid-data", "GitHub API route is invalid.")
    if "\\" in route or "\r" in route or "\n" in route:
        raise _error("github.invalid-data", "GitHub API route is invalid.")
    parsed = urlsplit(route)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise _error("github.invalid-data", "GitHub API route is invalid.")
    return route


def _classify_status(status: int, headers: Any = None, detail: bytes = b"") -> BrowseError:
    lower_detail = detail.lower()
    normalized = {key.casefold(): value for key, value in headers.items()} if headers else {}
    remaining = normalized.get("x-ratelimit-remaining")
    retry_after = normalized.get("retry-after")
    if status in {408, 504}:
        return _error("github.timeout", "GitHub did not respond within the read timeout.")
    if status == 404:
        return _error(
            "github.not-found",
            "GitHub repository, reference, or object was not found or is not accessible.",
        )
    if status == 429 or (
        status == 403
        and (
            remaining == "0"
            or retry_after is not None
            or b"rate limit" in lower_detail
            or b"secondary rate" in lower_detail
        )
    ):
        return _error(
            "github.rate-limit",
            "GitHub API rate limit was reached. Retry after the limit resets.",
        )
    if status in {401, 403}:
        return _error(
            "github.auth",
            "GitHub authentication is required or does not permit this read.",
        )
    if 300 <= status < 400:
        return _error(
            "github.redirect",
            "GitHub redirected the request. Use the repository's current owner and name.",
        )
    if status >= 500:
        return _error(
            "github.network",
            "GitHub API is temporarily unavailable. Retry the read later.",
        )
    return _error("github.invalid-data", "GitHub rejected the requested repository object.")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value.")


def _decode_json(payload: bytes) -> Any:
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise _error(
            "github.response-limit",
            "GitHub response exceeds the remote browsing limit. Narrow the repository scope.",
        )
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise _error("github.invalid-data", "GitHub returned invalid JSON data.") from None


def _anonymous_request(route: str) -> Any:
    route = _validate_route(route)
    url = f"{_API_ROOT}{route}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": _ACCEPT,
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        _RejectRedirects(),
    )
    try:
        with opener.open(request, timeout=_NETWORK_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            final_url = response.geturl()
            if final_url != url:
                raise _error(
                    "github.redirect",
                    "GitHub API redirected the request. Redirects are not accepted.",
                )
            if not isinstance(status, int) or isinstance(status, bool):
                raise _error("github.invalid-data", "GitHub returned an invalid HTTP status.")
            if not 200 <= status < 300:
                raise _classify_status(status, getattr(response, "headers", None))
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except BrowseError:
        raise
    except urllib.error.HTTPError as error:
        mapped = _classify_status(error.code, error.headers)
        try:
            error.close()
        finally:
            raise mapped from None
    except (TimeoutError, urllib.error.URLError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
            raise _error(
                "github.timeout",
                "GitHub did not respond within the read timeout.",
            ) from None
        raise _error(
            "github.network",
            "GitHub could not be reached for a read-only metadata request.",
        ) from None
    except (OSError, http.client.HTTPException):
        raise _error(
            "github.network",
            "GitHub could not be reached for a read-only metadata request.",
        ) from None
    if not isinstance(payload, bytes):
        raise _error("github.invalid-data", "GitHub returned an invalid response body.")
    return _decode_json(payload)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_PROCESS_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=_PROCESS_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("GitHub CLI process cleanup could not be confirmed.")


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                logger.warning("A GitHub CLI output stream could not be closed.")


def _resolve_gh() -> str:
    executable = resolve_tool_from_path("gh.exe" if os.name == "nt" else "gh")
    if executable is None:
        raise _error(
            "github.tool-missing",
            "GitHub CLI is required for auth='cli'. Install gh and configure github.com.",
        )
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        raise _error(
            "github.tool-missing",
            "GitHub CLI executable could not be resolved.",
        ) from None
    if not resolved.is_absolute() or not resolved.is_file():
        raise _error(
            "github.tool-missing",
            "GitHub CLI executable could not be resolved.",
        )
    if os.name == "nt" and resolved.suffix.casefold() != ".exe":
        raise _error("github.tool-missing", "Use an installed gh.exe binary, not a shell wrapper.")
    return str(resolved)


def _run_gh(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
        )
    except (FileNotFoundError, PermissionError, OSError):
        raise _error(
            "github.tool-missing",
            "GitHub CLI could not be started for the read-only request.",
        ) from None

    if process.stdout is None or process.stderr is None:
        _stop_process(process)
        raise _error("github.network", "GitHub CLI output could not be captured safely.")

    stdout = _BoundedCapture.create(_MAX_RESPONSE_BYTES)
    stderr = _BoundedCapture.create(_MAX_STDERR_BYTES)
    readers = [
        threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
    ]
    try:
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + _CLI_TIMEOUT_SECONDS
        timed_out = False
        while process.poll() is None:
            if stdout.exceeded.is_set() or stderr.exceeded.is_set():
                _stop_process(process)
                break
            if stdout.failed.is_set() or stderr.failed.is_set():
                _stop_process(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _stop_process(process)
                break
            time.sleep(0.01)

        for reader in readers:
            reader.join(timeout=_PROCESS_STOP_SECONDS)
        if any(reader.is_alive() for reader in readers):
            raise _error("github.network", "GitHub CLI output could not be captured safely.")
        if timed_out:
            raise _error("github.timeout", "GitHub CLI read timed out.")
        if stdout.exceeded.is_set() or stderr.exceeded.is_set():
            raise _error(
                "github.response-limit",
                "GitHub CLI output exceeded the remote browsing limit.",
            )
        if stdout.failed.is_set() or stderr.failed.is_set():
            raise _error("github.network", "GitHub CLI output could not be captured safely.")
        return_code = process.poll()
        if return_code is None:
            raise _error("github.timeout", "GitHub CLI read timed out.")
        return return_code, bytes(stdout.content), bytes(stderr.content)
    finally:
        if process.poll() is None:
            _stop_process(process)
        _close_process_pipes(process)


def _split_cli_response(payload: bytes) -> tuple[list[int], dict[str, str], bytes]:
    statuses: list[int] = []
    final_headers: dict[str, str] = {}
    remaining = payload
    while remaining.startswith(b"HTTP/"):
        line_end = remaining.find(b"\n")
        if line_end < 0:
            raise _error("github.invalid-data", "GitHub CLI returned invalid response headers.")
        status_line = remaining[:line_end].rstrip(b"\r")
        match = re.fullmatch(rb"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", status_line)
        if match is None:
            raise _error("github.invalid-data", "GitHub CLI returned invalid response headers.")
        separator = re.search(rb"\r?\n\r?\n", remaining)
        if separator is None:
            raise _error("github.invalid-data", "GitHub CLI returned invalid response headers.")
        statuses.append(int(match.group(1)))
        final_headers = {}
        for line in remaining[line_end + 1 : separator.start()].splitlines():
            if not line:
                continue
            if b":" not in line:
                raise _error(
                    "github.invalid-data",
                    "GitHub CLI returned invalid response headers.",
                )
            name, value = line.split(b":", 1)
            try:
                final_headers[name.decode("ascii").strip()] = value.decode("ascii").strip()
            except UnicodeError:
                raise _error(
                    "github.invalid-data",
                    "GitHub CLI returned invalid response headers.",
                ) from None
        remaining = remaining[separator.end() :]
    if not statuses:
        raise _error("github.invalid-data", "GitHub CLI did not return HTTP response metadata.")
    return statuses, final_headers, remaining


def _status_from_cli_error(stderr: bytes) -> int | None:
    match = re.search(
        rb"(?:HTTP/\S+\s+|\(HTTP\s+|HTTP\s+)([0-9]{3})",
        stderr,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match is not None else None


def _cli_request(route: str) -> Any:
    route = _validate_route(route)
    executable = _resolve_gh()
    argv = [
        executable,
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "--include",
        "--header",
        f"Accept: {_ACCEPT}",
        "--header",
        f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
        route,
    ]
    return_code, stdout, stderr = _run_gh(argv)

    try:
        statuses, headers, payload = _split_cli_response(stdout)
    except BrowseError:
        if return_code == 0:
            raise
        status = _status_from_cli_error(stderr)
        if status is not None:
            raise _classify_status(status, detail=stderr)
        lower_error = stderr.lower()
        if b"timed out" in lower_error or b"timeout" in lower_error:
            raise _error("github.timeout", "GitHub CLI read timed out.") from None
        if any(
            marker in lower_error
            for marker in (b"auth login", b"authentication", b"not logged", b"oauth token")
        ):
            raise _error(
                "github.auth",
                "GitHub CLI is not authenticated for the requested repository read.",
            ) from None
        raise _error(
            "github.network",
            "GitHub CLI could not complete the read-only API request.",
        ) from None

    if any(300 <= status < 400 for status in statuses):
        raise _classify_status(next(status for status in statuses if 300 <= status < 400))
    status = statuses[-1]
    if not 200 <= status < 300:
        raise _classify_status(status, headers=headers, detail=stderr)
    if return_code != 0:
        lower_error = stderr.lower()
        if b"timed out" in lower_error or b"timeout" in lower_error:
            raise _error("github.timeout", "GitHub CLI read timed out.")
        if any(
            marker in lower_error
            for marker in (b"auth login", b"authentication", b"not logged", b"oauth token")
        ):
            raise _error(
                "github.auth",
                "GitHub CLI is not authenticated for the requested repository read.",
            )
        raise _error(
            "github.network",
            "GitHub CLI could not complete the read-only API request.",
        )
    return _decode_json(payload)


class GitHubClient:
    """Read bounded repository, release, tree and blob metadata from GitHub."""

    def __init__(
        self,
        reference: GitHubReference,
        *,
        auth: str = "anonymous",
        transport: Transport | None = None,
    ):
        if not isinstance(reference, GitHubReference):
            raise TypeError("reference must be a GitHubReference")
        if auth not in {"anonymous", "cli"}:
            raise ValueError("auth must be 'anonymous' or 'cli'")
        if transport is not None and not callable(transport):
            raise TypeError("transport must be callable")
        self.reference = reference
        self.auth = auth
        self._transport = transport

    def _request(self, route: str) -> Any:
        if self._transport is not None:
            return self._transport(_validate_route(route))
        if self.auth == "cli":
            return _cli_request(route)
        return _anonymous_request(route)

    def _repository_route(self) -> str:
        owner = quote(self.reference.owner, safe="")
        repository = quote(self.reference.repository, safe="")
        return f"/repos/{owner}/{repository}"

    def _release_tag(self, tag: str) -> tuple[str, str]:
        route = f"{self._repository_route()}/git/ref/tags/{quote(tag, safe='')}"
        reference = self._request(route)
        if not isinstance(reference, dict) or reference.get("ref") != f"refs/tags/{tag}":
            raise _error("github.invalid-data", "GitHub did not identify the exact release tag.")
        target = reference.get("object")
        tag_object: str | None = None
        seen: set[str] = set()
        for depth in range(_MAX_TAG_DEPTH + 1):
            if not isinstance(target, dict) or target.get("type") not in {"commit", "tag"}:
                raise _error("github.invalid-data", "The release tag does not identify a source commit.")
            identity = _validate_sha(target.get("sha"), summary="GitHub returned an invalid release tag object.")
            if tag_object is None:
                tag_object = identity
            if target["type"] == "commit":
                return tag_object, identity
            if identity in seen or depth == _MAX_TAG_DEPTH:
                raise _error("github.tag-limit", "The release tag chain is cyclic or exceeds its limit.")
            seen.add(identity)
            annotated = self._request(f"{self._repository_route()}/git/tags/{identity}")
            if not isinstance(annotated, dict) or _validate_sha(
                annotated.get("sha"), summary="GitHub returned an invalid annotated tag object.",
            ) != identity:
                raise _error("github.invalid-data", "GitHub returned a different annotated tag object.")
            target = annotated.get("object")
        raise AssertionError("The bounded tag walk did not terminate.")

    def _release_assets(self, identifier: int) -> tuple[GitHubReleaseAsset, ...]:
        assets: list[GitHubReleaseAsset] = []
        identifiers: set[int] = set()
        names: set[str] = set()
        for page in range(1, _MAX_RELEASE_ASSETS // _RELEASE_ASSET_PAGE_SIZE + 2):
            response = self._request(
                f"{self._repository_route()}/releases/{identifier}/assets"
                f"?per_page={_RELEASE_ASSET_PAGE_SIZE}&page={page}"
            )
            if not isinstance(response, list) or len(response) > _RELEASE_ASSET_PAGE_SIZE:
                raise _error("github.invalid-data", "GitHub returned invalid release asset pagination.")
            if len(assets) + len(response) > _MAX_RELEASE_ASSETS:
                raise _error("github.release-limit", "The release asset inventory exceeds its limit.")
            for value in response:
                asset = GitHubReleaseAsset.from_metadata(value)
                if asset.identifier in identifiers or asset.name in names:
                    raise _error("github.invalid-data", "The release asset inventory contains duplicates.")
                identifiers.add(asset.identifier)
                names.add(asset.name)
                assets.append(asset)
            if len(response) < _RELEASE_ASSET_PAGE_SIZE:
                return tuple(sorted(assets, key=lambda asset: asset.name))
        raise _error("github.release-limit", "The release asset inventory exceeds its limit.")

    def resolve_release(self) -> GitHubReleaseSnapshot:
        """Observe an explicit published release without downloading its assets.

        The exact tag namespace is used rather than a branch or target_commitish.
        Metadata is rechecked after enumeration. Existing pins must retain their
        own resolved identities rather than follow this mutable selection again.
        """
        tag = self.reference.ref
        if tag is None:
            raise _error("github.release-tag-required", "Select an explicit release tag for package acquisition.")
        expected_repository = f"{self.reference.owner}/{self.reference.repository}"

        def repository_identity(value: Any) -> int:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("full_name"), str)
                or value["full_name"].casefold() != expected_repository.casefold()
            ):
                raise _error("github.repository-moved", "GitHub did not identify the requested source repository.")
            return _release_identifier(value.get("id"))

        repository_id = repository_identity(self._request(self._repository_route()))
        route = f"{self._repository_route()}/releases/tags/{quote(tag, safe='')}"

        def release_identity(value: Any) -> tuple[int, bool, datetime, bool | None]:
            if not isinstance(value, dict) or value.get("tag_name") != tag:
                raise _error("github.invalid-data", "GitHub did not identify the selected release.")
            if value.get("draft") is not False or not isinstance(value.get("prerelease"), bool):
                raise _error("github.release-unpublished", "Select a published release for package acquisition.")
            published = value.get("published_at")
            if not isinstance(published, str) or len(published) > 64:
                raise _error("github.release-unpublished", "GitHub did not identify a published release time.")
            try:
                timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                raise _error("github.invalid-data", "GitHub returned an invalid release timestamp.") from None
            if timestamp.tzinfo is None:
                raise _error("github.invalid-data", "GitHub returned an invalid release timestamp.")
            immutable = value.get("immutable")
            if immutable is not None and not isinstance(immutable, bool):
                raise _error("github.invalid-data", "GitHub returned invalid release immutability metadata.")
            return (
                _release_identifier(value.get("id")), value["prerelease"],
                timestamp.astimezone(timezone.utc), immutable,
            )

        identity = release_identity(self._request(route))
        tag_identity = self._release_tag(tag)
        assets = self._release_assets(identity[0])
        if release_identity(self._request(route)) != identity or self._release_tag(tag) != tag_identity:
            raise _error("github.source-changed", "The selected release changed during resolution. Retry explicitly.")
        if self._release_assets(identity[0]) != assets:
            raise _error("github.source-changed", "The release assets changed during resolution. Retry explicitly.")
        if repository_identity(self._request(self._repository_route())) != repository_id:
            raise _error("github.source-changed", "The source repository changed during resolution. Retry explicitly.")
        return GitHubReleaseSnapshot(
            self.reference, repository_id, identity[0], tag_identity[0],
            tag_identity[1], identity[1], identity[2], identity[3], assets,
        )

    def resolve_commit(self) -> str:
        """Resolve the configured reference or repository default branch to a commit."""
        selected_ref = self.reference.ref
        if selected_ref is None or self.auth == "cli":
            repository = self._request(self._repository_route())
            if not isinstance(repository, dict):
                raise _error(
                    "github.invalid-data",
                    "GitHub returned invalid repository metadata.",
                )
            if self.auth == "cli":
                # gh can follow redirects before returning its final response.
                full_name = repository.get("full_name")
                if not isinstance(full_name, str):
                    raise _error("github.invalid-data", "GitHub did not identify the repository.")
                expected = f"{self.reference.owner}/{self.reference.repository}"
                if full_name.casefold() != expected.casefold():
                    raise _error(
                        "github.repository-moved",
                        "The repository has moved. Use its current owner and repository name.",
                    )
            if selected_ref is None:
                try:
                    selected_ref = _validate_ref(repository.get("default_branch"))
                except BrowseError:
                    raise _error(
                        "github.invalid-data",
                        "GitHub returned an invalid default branch.",
                    ) from None

        route = f"{self._repository_route()}/commits/{quote(selected_ref, safe='')}"
        commit = self._request(route)
        if not isinstance(commit, dict):
            raise _error("github.invalid-data", "GitHub returned invalid commit metadata.")
        return _validate_sha(
            commit.get("sha"),
            summary="GitHub returned an invalid commit object ID.",
        )

    def get_tree(self, commit: str) -> dict[str, GitHubTreeEntry]:
        """Return the recursively enumerated Git tree pinned to an exact commit."""
        commit = _validate_sha(commit, summary="An exact Git commit SHA is required.")
        route = f"{self._repository_route()}/git/trees/{commit}?recursive=1"
        response = self._request(route)
        if not isinstance(response, dict):
            raise _error("github.invalid-data", "GitHub returned invalid tree metadata.")
        _validate_sha(
            response.get("sha"),
            summary="GitHub returned an invalid tree object ID.",
        )
        if "url" in response and not isinstance(response["url"], str):
            raise _error("github.invalid-data", "GitHub returned invalid tree metadata.")
        truncated = response.get("truncated")
        tree = response.get("tree")
        if not isinstance(truncated, bool) or not isinstance(tree, list):
            raise _error("github.invalid-data", "GitHub returned invalid tree metadata.")
        if truncated:
            raise _error(
                "github.tree-limit",
                "GitHub truncated the repository tree. Use a smaller published content scope.",
            )
        if len(tree) > _MAX_TREE_ENTRIES:
            raise _error(
                "github.tree-limit",
                "Repository tree exceeds 20,000 entries. Use a smaller published content scope.",
            )
        try:
            encoded_size = len(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise _error("github.invalid-data", "GitHub returned invalid tree metadata.") from None
        if encoded_size > _MAX_RESPONSE_BYTES:
            raise _error(
                "github.tree-limit",
                "Repository tree exceeds the remote browsing response limit.",
            )

        entries: dict[str, GitHubTreeEntry] = {}
        for raw_entry in tree:
            if not isinstance(raw_entry, dict):
                raise _error("github.invalid-data", "GitHub returned an invalid tree entry.")
            if "url" in raw_entry and not isinstance(raw_entry["url"], str):
                raise _error("github.invalid-data", "GitHub returned an invalid tree entry.")
            entry = GitHubTreeEntry(
                path=raw_entry.get("path"),
                sha=raw_entry.get("sha"),
                type=raw_entry.get("type"),
                mode=raw_entry.get("mode"),
                size=_validate_size(raw_entry.get("size"), optional=True),
            )
            if entry.path in entries:
                raise _error("github.invalid-data", "GitHub returned duplicate tree paths.")
            entries[entry.path] = entry
        return entries

    def read_blob(
        self,
        entry: GitHubTreeEntry,
        *,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> bytes:
        """Read and verify one regular Git blob within an explicit byte limit."""
        if not isinstance(entry, GitHubTreeEntry):
            raise TypeError("entry must be a GitHubTreeEntry")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if entry.type != "blob" or entry.mode not in _REGULAR_BLOB_MODES:
            raise _error(
                "github.blob-mode",
                "Only regular Git blob entries can be read as content.",
            )
        if entry.size is not None and entry.size > max_bytes:
            raise _error(
                "github.blob-limit",
                "Git blob exceeds the configured metadata preview limit.",
            )

        route = f"{self._repository_route()}/git/blobs/{entry.sha.lower()}"
        response = self._request(route)
        if not isinstance(response, dict):
            raise _error("github.invalid-data", "GitHub returned invalid blob metadata.")
        returned_sha = _validate_sha(
            response.get("sha"),
            summary="GitHub returned an invalid blob object ID.",
        )
        if returned_sha != entry.sha.lower():
            raise _error("github.integrity", "GitHub blob object ID did not match the tree.")
        if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
            raise _error("github.invalid-data", "GitHub returned invalid blob content encoding.")
        declared_size = _validate_size(response.get("size"), optional=False)
        assert declared_size is not None
        if declared_size > max_bytes:
            raise _error(
                "github.blob-limit",
                "Git blob exceeds the configured metadata preview limit.",
            )
        if entry.size is not None and declared_size != entry.size:
            raise _error("github.integrity", "GitHub blob size did not match the tree.")

        encoded = response["content"]
        if any(character.isspace() and character not in "\r\n" for character in encoded):
            raise _error("github.invalid-data", "GitHub returned invalid base64 blob content.")
        compact = encoded.replace("\r", "").replace("\n", "")
        maximum_encoded = 4 * ((max_bytes + 2) // 3)
        if len(compact) > maximum_encoded:
            raise _error(
                "github.blob-limit",
                "Git blob exceeds the configured metadata preview limit.",
            )
        try:
            content = base64.b64decode(compact.encode("ascii"), validate=True)
        except (UnicodeError, ValueError, binascii.Error):
            raise _error("github.invalid-data", "GitHub returned invalid base64 blob content.") from None
        if len(content) > max_bytes:
            raise _error(
                "github.blob-limit",
                "Git blob exceeds the configured metadata preview limit.",
            )
        if len(content) != declared_size:
            raise _error("github.integrity", "GitHub blob content size did not match its metadata.")

        identity = hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content,
            usedforsecurity=False,
        ).hexdigest()
        if identity != entry.sha.lower():
            raise _error("github.integrity", "GitHub blob content failed Git object verification.")
        return content

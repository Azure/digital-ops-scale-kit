# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Keep approved source trust outside projects and acquired packages."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from siteops.artifacts import ArtifactError, load_artifact_json, open_regular_file
from siteops.cache_filesystem import (
    check_cache_ancestors,
    check_private_node,
    make_private_directory,
)
from siteops.cache_layout import write_new
from siteops.github_attestation import load_github_policy
from siteops.github_source import GitHubReference

_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROFILE = "source.json"
_POLICY = "policy.json"
_ROOT = "trusted-root.jsonl"
_MAX_PROFILE = 16 * 1024
_MAX_POLICY = 256 * 1024
_MAX_ROOT = 2 * 1024 * 1024
logger = logging.getLogger(__name__)


class SourceProfileError(ArtifactError):
    def __init__(self, message: str, *, code: str = "source.profile-invalid"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class ApprovedSource:
    name: str
    provider: str
    reference: str
    policy_sha256: str
    root_sha256: str
    directory: Path

    @property
    def policy(self) -> Path:
        return self.directory / _POLICY

    @property
    def trusted_root(self) -> Path:
        return self.directory / _ROOT

    def document(self) -> dict:
        return {
            "apiVersion": "siteops.source/v1", "kind": "ApprovedSource",
            "name": self.name, "provider": self.provider, "reference": self.reference,
            "policySha256": self.policy_sha256, "trustedRootSha256": self.root_sha256,
        }


def source_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise SourceProfileError("The Windows user configuration location is unavailable.")
        path = Path(base)
    else:
        configured = os.environ.get("XDG_CONFIG_HOME")
        path = Path(configured) if configured else Path.home() / ".config"
    if not path.is_absolute():
        raise SourceProfileError("Choose an absolute user configuration location.")
    return path / "siteops" / "sources"


def _name(value: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise SourceProfileError("Source names must be lowercase letters, digits or hyphens.")
    return value


def _bytes(path: Path, limit: int) -> bytes:
    try:
        with open_regular_file(path) as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise SourceProfileError("A source trust file could not be read.") from None
    if not data or len(data) > limit:
        raise SourceProfileError("A source trust file is empty or exceeds its byte limit.")
    return data


def _private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        check_private_node(path, directory=True)
        return
    if not path.parent.is_dir():
        _private_directory(path.parent)
    check_cache_ancestors(path)
    make_private_directory(path)


def _profile(name: str, value: dict, directory: Path) -> ApprovedSource:
    if (
        not isinstance(value, dict)
        or value.keys() != {
            "apiVersion", "kind", "name", "provider", "reference",
            "policySha256", "trustedRootSha256",
        }
        or value["apiVersion"] != "siteops.source/v1"
        or value["kind"] != "ApprovedSource" or value["name"] != name
        or value["provider"] != "github-release/v1"
        or not all(isinstance(value[field], str) and _DIGEST.fullmatch(value[field])
                   for field in ("policySha256", "trustedRootSha256"))
    ):
        raise SourceProfileError("The approved source record is invalid.")
    reference = GitHubReference.parse(value["reference"])
    if reference.ref is not None or value["reference"] != f"github:{reference.owner}/{reference.repository}":
        raise SourceProfileError("An approved source must identify one repository, not a release.")
    return ApprovedSource(
        name, value["provider"], value["reference"],
        value["policySha256"], value["trustedRootSha256"], directory,
    )


def read_source(name: str, *, require_valid: bool = True) -> ApprovedSource:
    name = _name(name)
    directory = source_root() / name
    if not directory.is_dir():
        raise SourceProfileError("Approved source not found.", code="source.profile-missing")
    check_cache_ancestors(directory)
    check_private_node(directory, directory=True)
    record = load_artifact_json(_bytes(directory / _PROFILE, _MAX_PROFILE),
                                limit=_MAX_PROFILE, label="Approved source")
    result = _profile(name, record, directory)
    policy_bytes = _bytes(result.policy, _MAX_POLICY)
    root_bytes = _bytes(result.trusted_root, _MAX_ROOT)
    if (
        hashlib.sha256(policy_bytes).hexdigest() != result.policy_sha256
        or hashlib.sha256(root_bytes).hexdigest() != result.root_sha256
    ):
        raise SourceProfileError("The approved source trust files changed.")
    policy = load_github_policy(result.policy)
    reference = GitHubReference.parse(result.reference)
    if (
        policy.repository.casefold() != f"{reference.owner}/{reference.repository}".casefold()
        or policy.trusted_root_sha256 != result.root_sha256
        or (require_valid and policy.valid_until <= datetime.now(timezone.utc))
    ):
        raise SourceProfileError("The approved source policy is invalid or expired.")
    return result


def enroll_source(name: str, source: str, policy_file: Path, root_file: Path) -> ApprovedSource:
    name = _name(name)
    reference = GitHubReference.parse(source)
    if reference.ref is not None:
        raise SourceProfileError("Enroll the source repository, not a release.")
    policy = load_github_policy(policy_file)
    policy_bytes = _bytes(policy_file, _MAX_POLICY)
    root_bytes = _bytes(root_file, _MAX_ROOT)
    root_sha = hashlib.sha256(root_bytes).hexdigest()
    if (
        policy.repository.casefold() != f"{reference.owner}/{reference.repository}".casefold()
        or policy.trusted_root_sha256 != root_sha
        or policy.valid_until <= datetime.now(timezone.utc)
    ):
        raise SourceProfileError("The source, policy and trusted root do not agree.")
    root = source_root()
    directory = root / name
    if directory.exists() or directory.is_symlink():
        existing = read_source(name)
        previous = load_github_policy(existing.policy)
        if (
            existing.reference.casefold() != f"github:{reference.owner}/{reference.repository}".casefold()
            or (previous.repository, previous.source_ref, previous.signer_workflow,
                previous.builder_workflow, previous.runner_environment)
            != (policy.repository, policy.source_ref, policy.signer_workflow,
                policy.builder_workflow, policy.runner_environment)
        ):
            raise SourceProfileError("The existing source approval differs. Inspect it before changing trust.")
        return existing
    _private_directory(root.parent)
    _private_directory(root)
    check_cache_ancestors(directory)
    try:
        make_private_directory(directory)
    except FileExistsError:
        raise SourceProfileError("The approved source already exists. Inspect it before changing trust.") from None
    result = ApprovedSource(
        name, "github-release/v1", f"github:{reference.owner}/{reference.repository}",
        hashlib.sha256(policy_bytes).hexdigest(), root_sha, directory,
    )
    created: list[Path] = []
    try:
        write_new(result.policy, policy_bytes)
        created.append(result.policy)
        write_new(result.trusted_root, root_bytes)
        created.append(result.trusted_root)
        write_new(directory / _PROFILE, (json.dumps(result.document(), sort_keys=True) + "\n").encode("utf-8"))
        created.append(directory / _PROFILE)
        return read_source(name)
    except (OSError, ArtifactError):
        cleanup_failed = False
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                cleanup_failed = True
        try:
            directory.rmdir()
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            logger.warning("Approved source enrollment cleanup could not be completed.")
        raise SourceProfileError("The approved source could not be recorded.") from None


def list_sources() -> tuple[str, ...]:
    root = source_root()
    if not root.exists():
        return ()
    check_private_node(root, directory=True)
    names = []
    for directory in root.iterdir():
        names.append(read_source(directory.name, require_valid=False).name)
    return tuple(sorted(names))


def remove_source(name: str) -> None:
    directory = source_root() / _name(name)
    if not directory.is_dir():
        raise SourceProfileError("Approved source not found.", code="source.profile-missing")
    check_cache_ancestors(directory)
    check_private_node(directory, directory=True)
    if {path.name for path in directory.iterdir()} != {_PROFILE, _POLICY, _ROOT}:
        raise SourceProfileError("The source directory contains other files. Inspect it before removal.")
    for path in (directory / _PROFILE, directory / _POLICY, directory / _ROOT):
        check_private_node(path, directory=False)
    for path in (directory / _PROFILE, directory / _POLICY, directory / _ROOT):
        path.unlink()
    directory.rmdir()

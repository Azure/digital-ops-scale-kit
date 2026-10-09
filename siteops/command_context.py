# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Select content and Site configuration for local or project commands."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ContextManager, Protocol

from siteops.artifacts import ArtifactError, hash_file
from siteops.cache_filesystem import CacheError
from siteops.github_workspace_acquisition import GitHubWorkspaceAcquirer
from siteops.project import (
    ProjectError,
    WorkspacePin,
    pin_exists,
    project_root,
    read_pin,
    require_separate_cache,
)
from siteops.results import ProgressCallback, ProgressEvent, ProgressEventKind, ProgressPhase
from siteops.runtime import (
    RuntimePaths,
    bounded_runtime_path,
    create_private_directory,
    describe_os_error,
)
from siteops.source_profiles import read_source, sources_for
from siteops.workspace_cache import CachedWorkspace, WorkspaceCache, default_cache_root
from siteops.workspace_source import ResolvedWorkspaceSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceRequest:
    reference: str
    release: str | None
    approved_source: str | None


def resolve_source_request(
    value: str | None, *, release: str | None = None, approved_source: str | None = None,
    for_inspection: bool = False,
) -> SourceRequest:
    """Resolve an explicit locator or consumer source name without source requests."""
    from siteops.github_source import GitHubReference

    if value is None:
        if approved_source is None:
            raise ProjectError("Select --source or an approved source.")
        value = read_source(approved_source, require_valid=not for_inspection).reference
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProjectError("Source selection must be a locator or approved source name.")
    if "@" in value:
        if release is not None:
            raise ProjectError("The source release was specified more than once.")
        value, _, release = value.rpartition("@")
        if not value or not release:
            raise ProjectError("Source selection must include a nonempty source and release.")
    if not value.startswith(("github:", "https://")):
        if approved_source is not None:
            raise ProjectError(
                "Choose one approved source: --source NAME@RELEASE or --approved-source NAME, not both."
            )
        approved_source = value
        value = read_source(value, require_valid=not for_inspection).reference
    reference = GitHubReference.parse(value, ref=release)
    return SourceRequest(
        f"github:{reference.owner}/{reference.repository}", reference.ref, approved_source,
    )


def acquire_release(
    request: SourceRequest, cache: WorkspaceCache, policy: Path, trusted_root: Path,
    *, workspace: str | None = None, progress: ProgressCallback | None = None,
) -> ResolvedWorkspaceSource:
    """Resolve one published source through the currently supported adapter."""
    from siteops.github_source import GitHubClient, GitHubReference
    from siteops.github_workspace_acquisition import GitHubWorkspaceAcquirer

    reference = GitHubReference.parse(request.reference, ref=request.release)
    return GitHubWorkspaceAcquirer(
        cache, policy_file=policy, trusted_root=trusted_root, progress=progress,
    ).acquire(GitHubClient(reference), workspace=workspace).resolved


class ProjectAcquirer(Protocol):
    def lease(self, source: ResolvedWorkspaceSource) -> ContextManager[CachedWorkspace]: ...
    def restore(self, source: ResolvedWorkspaceSource) -> None: ...


def _approval_guidance(reference: str, *, direct: bool, release: str | None) -> str:
    """Name the enrolled source for a publisher, or how to enroll one."""
    try:
        names = sources_for(reference)
    except (ArtifactError, OSError):
        names = ()

    def selection(name: str) -> str:
        return (
            f"use --source {name}@{release or 'RELEASE'}" if direct
            else f"put --approved-source {name} before the command"
        )

    if names:
        return f"Approved source '{names[0]}' is enrolled for {reference}: {selection(names[0])}."
    return (
        f"Run `siteops source enroll NAME --source {reference}`, then {selection('NAME')}. "
        "Alternatively, supply --trust-policy and --trusted-root."
    )


def require_trust_inputs(
    cache_root: Path, policy: Path | None, trusted_root: Path | None,
    *, approved_source: str | None = None, source_reference: str | None = None,
    direct: bool = False, release: str | None = None,
) -> tuple[Path, Path]:
    if approved_source is not None:
        if policy is not None or trusted_root is not None:
            raise ProjectError("Choose --approved-source or explicit trust files, not both.")
        profile = read_source(approved_source)
        if source_reference is not None and profile.reference.casefold() != source_reference.casefold():
            error = ProjectError("The approved source does not match the selected workspace source.")
            error.private_message = (
                f"Approved source '{approved_source}' does not match the selected workspace source: "
                f"it is enrolled for {profile.reference}, not {source_reference}. "
                + _approval_guidance(source_reference, direct=direct, release=release)
            )
            raise error
        policy, trusted_root = profile.policy, profile.trusted_root
    if policy is None or trusted_root is None:
        error = ProjectError(
            "Verified content requires an approved source or independent --trust-policy and --trusted-root.",
            code="project.trust-required",
        )
        if source_reference is not None:
            error.private_message = (
                f"Verified content from {source_reference} requires an approved source. "
                + _approval_guidance(source_reference, direct=direct, release=release)
            )
        raise error
    for path in (policy, trusted_root):
        if path.resolve().is_relative_to(cache_root.resolve()):
            raise ProjectError("Consumer policy and trusted roots must be outside the content cache.")
    return policy.absolute(), trusted_root.absolute()


def project_acquirer(
    source: ResolvedWorkspaceSource, cache: WorkspaceCache, policy: Path, trusted_root: Path,
    *, progress: ProgressCallback | None = None,
) -> ProjectAcquirer:
    if source.source.provider != "github-release/v1":
        raise ProjectError("The pinned source provider is not supported by this installation.")
    return GitHubWorkspaceAcquirer(
        cache, policy_file=policy, trusted_root=trusted_root, progress=progress,
    )


@dataclass(frozen=True)
class CommandContext:
    workspace: Path
    site_root: Path
    project: Path | None = None
    pin: WorkspacePin | None = None
    package: CachedWorkspace | None = None
    source: ResolvedWorkspaceSource | None = None
    revalidate: Callable[[], None] | None = field(default=None, repr=False, compare=False)


def _remove_temporary_configuration(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as error:
        logger.warning(
            "Temporary Site configuration %s could not be removed: %s",
            bounded_runtime_path(path), describe_os_error(error),
        )


@contextmanager
def open_command_context(
    *, workspace: Path | None, project: Path | None, command: str,
    policy: Path | None, trusted_root: Path | None, offline: bool,
    approved_source: str | None = None,
    discover: Callable[[Path], Path | None],
    source: str | None = None,
    progress: ProgressCallback | None = None,
) -> Iterator[CommandContext]:
    """Resolve paths from the invocation directory and hold any package lease through use."""
    current = Path.cwd()
    selected_project = project if project is not None else (
        current if source is None and pin_exists(current) else None
    )
    root = project_root(selected_project) if selected_project is not None else None
    if command == "sites" and root is not None:
        if policy is not None or trusted_root is not None or approved_source is not None:
            raise ProjectError("Trust options apply to package use, not Site inspection.")
        yield CommandContext(root, root, project=root)
        return
    if source is None and (workspace is not None or root is None):
        if policy is not None or trusted_root is not None or approved_source is not None:
            raise ValueError(
                "--approved-source, --trust-policy and --trusted-root apply only to a workspace pin "
                "or --source content, not to a local workspace."
            )
        if offline:
            raise ValueError("--offline-content applies only to a workspace pin, not to a local workspace.")
        selected = workspace if workspace is not None else (discover(current) or current)
        selected = Path(selected).resolve()
        if not selected.is_dir():
            raise ProjectError("Workspace directory not found.")
        yield CommandContext(selected, root if root is not None else selected, project=root)
        return
    if source is not None and offline:
        raise ProjectError("Direct release selection needs source access. Use a project pin for --offline-content.")
    request = resolve_source_request(source, approved_source=approved_source) if source is not None else None
    if request is not None and request.release is None:
        raise ProjectError("Direct source use requires an explicit published release: --source SOURCE@RELEASE.")
    if request is not None and workspace is not None and (
        workspace.is_absolute() or ".." in workspace.parts
    ):
        raise ProjectError("A source workspace must be a relative published workspace path.")
    selection = read_pin(root).pin if request is None else None
    cache_root = default_cache_root()
    approval = request.approved_source if request is not None else approved_source
    reference = request.reference if request is not None else selection.selection.source.reference
    policy_file, root_file = require_trust_inputs(
        cache_root, policy, trusted_root,
        approved_source=approval, source_reference=reference,
        direct=request is not None, release=request.release if request is not None else None,
    )
    with ExitStack() as stack:
        configuration = root
        if configuration is None:
            configuration = create_private_directory(
                RuntimePaths.resolve().temp_root, prefix="siteops-target-",
            )
            stack.callback(_remove_temporary_configuration, configuration)
        require_separate_cache(configuration, cache_root)
        cache = WorkspaceCache(cache_root)
        selected = (
            acquire_release(
                request, cache, policy_file, root_file,
                workspace=workspace.as_posix() if workspace is not None else None,
                **({"progress": progress} if progress is not None else {}),
            )
            if request is not None else selection.selection
        )
        acquirer = project_acquirer(
            selected, cache, policy_file, root_file,
            **({"progress": progress} if progress is not None else {}),
        )
        try:
            package = stack.enter_context(acquirer.lease(selected))
        except CacheError as error:
            if offline or error.code not in {"cache.missing", "cache.proof-missing"}:
                raise
            acquirer.restore(selected)
            package = stack.enter_context(acquirer.lease(selected))
        content = package.package_root
        if selected.entry.workspace != ".":
            content = content.joinpath(*selected.entry.workspace.split("/"))

        def revalidate() -> None:
            if progress is not None:
                progress(ProgressEvent(
                    kind=ProgressEventKind.PHASE_STARTED, phase=ProgressPhase.VERIFICATION,
                ))
            current_policy, current_root = require_trust_inputs(
                cache_root, policy, trusted_root,
                approved_source=approval, source_reference=selected.source.reference,
            )
            receipt = package.verification
            if datetime.now(timezone.utc) >= receipt.valid_until:
                raise ProjectError("Source verification expired during preparation. Renew the approved source and review again.")
            if (
                hash_file(current_policy, limit=8 * 1024 * 1024)[1] != receipt.policy_sha256
                or hash_file(current_root, limit=8 * 1024 * 1024)[1] != receipt.root_sha256
            ):
                raise ProjectError("Source approval changed during preparation. Review a new plan.")

        yield CommandContext(
            content, configuration, project=root, pin=selection, package=package,
            source=selected, revalidate=revalidate,
        )

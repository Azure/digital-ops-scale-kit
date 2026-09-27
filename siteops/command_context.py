# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Select content and Site configuration for local or project commands."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Protocol

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
from siteops.source_profiles import read_source
from siteops.workspace_cache import CachedWorkspace, WorkspaceCache, default_cache_root
from siteops.workspace_source import ResolvedWorkspaceSource


class ProjectAcquirer(Protocol):
    def lease(self, source: ResolvedWorkspaceSource) -> ContextManager[CachedWorkspace]: ...
    def restore(self, source: ResolvedWorkspaceSource) -> None: ...


def require_trust_inputs(
    cache_root: Path, policy: Path | None, trusted_root: Path | None,
    *, approved_source: str | None = None, source_reference: str | None = None,
) -> tuple[Path, Path]:
    if approved_source is not None:
        if policy is not None or trusted_root is not None:
            raise ProjectError("Choose --approved-source or explicit trust files, not both.")
        profile = read_source(approved_source)
        if source_reference is not None and profile.reference.casefold() != source_reference.casefold():
            raise ProjectError("The approved source does not match the selected workspace source.")
        policy, trusted_root = profile.policy, profile.trusted_root
    if policy is None or trusted_root is None:
        raise ProjectError(
            "Pinned package use requires --approved-source or independent --trust-policy and --trusted-root.",
            code="project.trust-required",
        )
    for path in (policy, trusted_root):
        if path.resolve().is_relative_to(cache_root.resolve()):
            raise ProjectError("Consumer policy and trusted roots must be outside the content cache.")
    return policy.absolute(), trusted_root.absolute()


def project_acquirer(
    source: ResolvedWorkspaceSource, cache: WorkspaceCache, policy: Path, trusted_root: Path,
) -> ProjectAcquirer:
    if source.source.provider != "github-release/v1":
        raise ProjectError("The pinned source provider is not supported by this installation.")
    return GitHubWorkspaceAcquirer(cache, policy_file=policy, trusted_root=trusted_root)


@dataclass(frozen=True)
class CommandContext:
    workspace: Path
    site_root: Path
    project: Path | None = None
    pin: WorkspacePin | None = None
    package: CachedWorkspace | None = None


@contextmanager
def open_command_context(
    *, workspace: Path | None, project: Path | None, command: str,
    policy: Path | None, trusted_root: Path | None, offline: bool,
    approved_source: str | None = None,
    discover: Callable[[Path], Path | None],
) -> Iterator[CommandContext]:
    """Resolve paths from the invocation directory and hold any package lease through use."""
    current = Path.cwd()
    selected_project = project if project is not None else (current if pin_exists(current) else None)
    root = project_root(selected_project) if selected_project is not None else None
    if command == "sites" and root is not None:
        if policy is not None or trusted_root is not None or approved_source is not None:
            raise ProjectError("Trust options apply to package use, not Site inspection.")
        yield CommandContext(root, root, project=root)
        return
    if workspace is not None or root is None:
        if policy is not None or trusted_root is not None or approved_source is not None or offline:
            raise ValueError("Trust and offline options apply only when using a workspace pin.")
        selected = workspace if workspace is not None else (discover(current) or current)
        selected = Path(selected).resolve()
        if not selected.is_dir():
            raise ProjectError("Workspace directory not found.")
        yield CommandContext(selected, root if root is not None else selected, project=root)
        return
    selection = read_pin(root).pin
    cache_root = default_cache_root()
    require_separate_cache(root, cache_root)
    policy, trusted_root = require_trust_inputs(
        cache_root, policy, trusted_root,
        approved_source=approved_source, source_reference=selection.selection.source.reference,
    )
    cache = WorkspaceCache(cache_root)
    acquirer = project_acquirer(selection.selection, cache, policy, trusted_root)
    with ExitStack() as stack:
        try:
            package = stack.enter_context(acquirer.lease(selection.selection))
        except CacheError as error:
            if offline or error.code not in {"cache.missing", "cache.proof-missing"}:
                raise
            acquirer.restore(selection.selection)
            package = stack.enter_context(acquirer.lease(selection.selection))
        content = package.package_root
        if selection.selection.entry.workspace != ".":
            content = content.joinpath(*selection.selection.entry.workspace.split("/"))
        yield CommandContext(content, root, project=root, pin=selection, package=package)

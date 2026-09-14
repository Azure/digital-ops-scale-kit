# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""GitHub adaptation of the provider-neutral content index."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from siteops.browse import (
    BrowseDiagnostic,
    BrowseError,
    BrowseResult,
    BrowseSource,
    check_path_components,
    select_entries,
    validate_browse_options,
)
from siteops.content_index import (
    BINDINGS_NAME,
    INDEX_NAME,
    MAX_INDEX_BYTES,
    canonical_index_path,
    load_content_index,
    load_source_bindings,
    validate_source_snapshot,
)
from siteops.manifest_selection import is_explicit_manifest_path

if TYPE_CHECKING:
    from siteops.github_source import GitHubClient, GitHubTreeEntry


def github_input_digests(content: bytes) -> dict[str, str]:
    """Add Git object identity for this adapter, not as a common index requirement."""
    def blob(value: bytes) -> str:
        framed = b"blob " + str(len(value)).encode("ascii") + b"\x00" + value
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()

    return {
        "git-blob-sha1": blob(content),
        "git-blob-sha1-crlf": blob(content.replace(b"\n", b"\r\n")),
    }


def _workspace_name(value: Path | str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/")
    if text in {"", "."}:
        return ""
    try:
        return canonical_index_path(text, reference=True)
    except ValueError:
        raise BrowseError(
            "source.workspace", "For a remote source, -w must be a source-relative workspace path."
        ) from None


def _index_candidates(tree: dict[str, GitHubTreeEntry]) -> list[str]:
    result = []
    for path in tree:
        if PurePosixPath(path).name != INDEX_NAME:
            continue
        try:
            canonical_index_path(path, reference=True)
        except ValueError:
            continue
        result.append(path)
    return sorted(result)


def inspect_github(
    source: str,
    selection: str | None = None,
    *,
    ref: str | None = None,
    workspace: Path | str | None = None,
    auth: str = "anonymous",
    search: str | None = None,
    tags: tuple[str, ...] = (),
    category: str | None = None,
    include_partials: bool = False,
    limit: int | None = None,
    client: GitHubClient | None = None,
) -> BrowseResult:
    """Browse one pinned index without acquiring or executing its workspace."""
    context = BrowseSource("remote", "requested GitHub source", provider="github")
    workspace_name = ""
    try:
        validate_browse_options(selection, search, tags, category, limit)
        requested_workspace = _workspace_name(workspace)
        if client is None:
            from siteops.github_source import GitHubClient, GitHubReference

            client = GitHubClient(GitHubReference.parse(source, ref=ref), auth=auth)
        context = replace(context, reference=client.reference.web_url)
        revision = client.resolve_commit()
        context = replace(context, revision=revision)
        tree = client.get_tree(revision)
        if requested_workspace is not None:
            index_path = str(PurePosixPath(requested_workspace) / INDEX_NAME)
            candidates = [index_path] if index_path in tree else []
        else:
            candidates = _index_candidates(tree)
        if not candidates:
            raise BrowseError(
                "index.missing",
                "No published Site Ops index was found. The source owner can run "
                "siteops index --public --for-source github in its workspace.",
            )
        if len(candidates) > 1:
            diagnostics = [BrowseDiagnostic(
                "source.workspace", "This source has several indexed workspaces. Choose one with -w."
            )]
            diagnostics.extend(BrowseDiagnostic(
                "source.workspace.choice", "Available source-relative workspace.",
                PurePosixPath(path).parent.as_posix(),
            ) for path in candidates[:32])
            return BrowseResult("", diagnostics=tuple(diagnostics), source=context)
        index_path = candidates[0]
        parent = PurePosixPath(index_path).parent
        workspace_name = "" if parent.as_posix() == "." else parent.as_posix()
        prefix = workspace_name + "/" if workspace_name else ""
        binding_path = prefix + BINDINGS_NAME
        if binding_path not in tree:
            raise BrowseError("index.bindings", "The published index is missing its source bindings.")
        index = client.read_blob(tree[index_path], max_bytes=MAX_INDEX_BYTES)
        raw_bindings = client.read_blob(tree[binding_path], max_bytes=MAX_INDEX_BYTES)
        entries = load_content_index(index)
        bindings = load_source_bindings(raw_bindings, index)
        scoped = {
            path.removeprefix(prefix): item for path, item in tree.items()
            if path.startswith(prefix) and path != prefix.rstrip("/")
        }
        for item in bindings.inputs:
            actual = scoped.get(item.path)
            if actual and (actual.type != "blob" or actual.mode not in {"100644", "100755"}):
                raise BrowseError("index.source", "An indexed input is not a regular source file.")
        for path, item in scoped.items():
            parts = PurePosixPath(path).parts
            if not parts or parts[0] not in {"manifests", "samples"}:
                continue
            try:
                check_path_components(PurePosixPath(path), reference=False)
            except BrowseError:
                continue
            if (
                item.mode in {"120000", "160000"}
                or (len(parts) == 1 and item.type != "tree")
            ):
                raise BrowseError(
                    "index.source", "The indexed workspace uses unsupported source links or submodules."
                )
        files = {path: item for path, item in scoped.items() if item.type != "tree"}
        validate_source_snapshot(
            bindings, set(files),
            {path: item.sha for path, item in files.items() if item.mode in {"100644", "100755"}},
            algorithm="git-blob-sha1",
            equivalent_algorithms=("git-blob-sha1-crlf",),
        )
        context = replace(context, index_status="current metadata snapshot")

        def guide_url(value: str) -> str:
            path, separator, fragment = value.partition("#")
            url = client.reference.web_url + "/blob/" + revision + "/" + quote(prefix + path, safe="/")
            return url + (separator + quote(fragment, safe="-_") if separator else "")

        entries = tuple(replace(
            entry, guidance=replace(
                entry.guidance,
                documentation=tuple(guide_url(value) for value in entry.guidance.documentation),
            ),
        ) for entry in entries)
        result = BrowseResult(
            workspace_name or ".", entries, discovered=len(entries),
            name_inventory_complete=True, source=context,
        )
        selection_is_path = selection is not None and is_explicit_manifest_path(selection)
        if selection_is_path:
            selection = canonical_index_path(selection.replace("\\", "/").removeprefix("./"))
        filename_match = next(
            (entry.path for entry in entries if entry.path == selection), None
        ) if not selection_is_path else None
        return select_entries(
            result, selection, search=search, tags=tags, category=category,
            include_partials=include_partials, limit=limit, selection_is_path=selection_is_path,
            filename_match=filename_match,
        )
    except BrowseError as error:
        return BrowseResult(
            workspace_name, diagnostics=(error.diagnostic,),
            source=replace(context, index_status="unavailable"),
        )
    except ValueError:
        return BrowseResult(
            workspace_name,
            diagnostics=(BrowseDiagnostic("source.selection", "Remote selection options are invalid."),),
            source=context,
        )

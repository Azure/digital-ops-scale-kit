# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""GitHub browsing observations over the provider neutral metadata cache."""

from __future__ import annotations

import json
from datetime import timedelta

from siteops.artifacts import load_artifact_json
from siteops.browse import BrowseError, SourceObservation
from siteops.github_source import (
    GitHubClient,
    GitHubTreeEntry,
    _validate_sha,
    parse_git_tree_entries,
    validate_git_blob,
)
from siteops.source_metadata_cache import MAX_METADATA_BYTES, MetadataKey, SourceMetadataCache

REFERENCE_MAX_AGE = 5 * 60


class CachedGitHubClient:
    """Reuse identified trees/blobs and bounded reference observations for private browsing."""

    def __init__(
        self, client: GitHubClient, cache: SourceMetadataCache, *,
        refresh: bool = False, offline: bool = False,
    ):
        if type(refresh) is not bool or type(offline) is not bool or (refresh and offline):
            raise BrowseError("source.cache-options", "Choose either --refresh or --offline.")
        self.client = client
        self.reference = client.reference
        self.cache = cache
        self.refresh = refresh
        self.offline = offline
        self.observation: SourceObservation | None = None
        self._scope = json.dumps({
            "repository": f"{self.reference.owner}/{self.reference.repository}".casefold(),
            "access": client.auth,
        }, sort_keys=True, separators=(",", ":"))

    def _key(self, kind: str, identity: str) -> MetadataKey:
        return MetadataKey("github-metadata/v1", self._scope, kind, identity)

    def resolve_commit(self) -> str:
        pinned = self.reference.pinned_commit
        key = self._key("commit", pinned) if pinned else self._key(
            "reference", json.dumps(self.reference.ref, ensure_ascii=True),
        )
        try:
            value = self.cache.get_or_fetch(
                key, lambda: self.client.resolve_commit().encode("ascii"),
                maximum_age=None if pinned else REFERENCE_MAX_AGE,
                refresh=self.refresh, offline=self.offline,
            )
        except BrowseError as error:
            if error.diagnostic.code in {"github.rate-limit", "github.network", "github.timeout"}:
                raise BrowseError(
                    error.diagnostic.code,
                    error.diagnostic.summary + " Use --offline to request a previously cached snapshot.",
                ) from None
            raise
        try:
            revision = _validate_sha(
                value.payload.decode("ascii"), summary="The cached commit identity is invalid.",
            )
        except UnicodeError:
            raise BrowseError("github.integrity", "The cached commit identity is invalid.") from None
        if pinned is not None and revision != pinned:
            raise BrowseError("github.integrity", "The cached source differs from the pinned commit.")
        if not pinned:
            self.cache.remember(self._key("commit", revision), value)
        self.observation = SourceObservation(
            "cache" if value.reused else "source", value.observed_at,
            None if pinned else value.observed_at + timedelta(seconds=REFERENCE_MAX_AGE),
            self.offline, value.stale,
        )
        return revision

    def get_tree(self, commit: str) -> dict[str, GitHubTreeEntry]:
        commit = _validate_sha(commit, summary="An exact Git commit SHA is required.")

        def fetch() -> bytes:
            tree = self.client.get_tree(commit)
            return json.dumps([
                {
                    "path": row.path, "sha": row.sha, "type": row.type, "mode": row.mode,
                    **({"size": row.size} if row.size is not None else {}),
                }
                for row in sorted(tree.values(), key=lambda entry: entry.path)
            ], ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        value = self.cache.get_or_fetch(self._key("tree", commit), fetch, offline=self.offline)
        return parse_git_tree_entries(load_artifact_json(
            value.payload, limit=MAX_METADATA_BYTES, label="Cached Git tree",
        ))

    def read_blob(self, entry: GitHubTreeEntry, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
        if not isinstance(entry, GitHubTreeEntry):
            raise TypeError("entry must be a GitHubTreeEntry")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if entry.type != "blob" or entry.mode not in {"100644", "100755"}:
            raise BrowseError("github.blob-mode", "Only regular Git blob entries can be read as content.")
        if entry.size is not None and entry.size > max_bytes:
            raise BrowseError("github.blob-limit", "Git blob exceeds the configured metadata preview limit.")
        value = self.cache.get_or_fetch(
            self._key("blob", entry.sha),
            lambda: self.client.read_blob(entry, max_bytes=max_bytes), offline=self.offline,
        )
        return validate_git_blob(entry, value.payload, max_bytes=max_bytes)

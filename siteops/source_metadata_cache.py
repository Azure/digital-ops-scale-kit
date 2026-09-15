# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Protected metadata observations with separate mutable and immutable key semantics."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from siteops.artifact_verification import utc_text
from siteops.artifacts import ArtifactError, load_artifact_json, open_regular_file
from siteops.cache_filesystem import CacheError, cache_lock, check_private_node
from siteops.cache_layout import CacheLayout, cache_io, write_new

logger = logging.getLogger(__name__)
MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_ENCODED_BYTES = 4 * ((MAX_METADATA_BYTES + 2) // 3)
_MAX_RECORD_BYTES = _MAX_ENCODED_BYTES + 64 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: str, limit: int) -> None:
    if (
        not isinstance(value, str) or not value or len(value) > limit
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise CacheError("Metadata keys require bounded printable fields.", code="cache.metadata-key")


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CacheError("Metadata observations require a timezone.", code="cache.metadata-time")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class MetadataKey:
    """Provider and access scope are explicit, opaque data rather than filesystem paths."""

    provider: str
    scope: str
    kind: str
    identity: str

    def __post_init__(self) -> None:
        for value, limit in ((self.provider, 128), (self.scope, 2048), (self.kind, 128), (self.identity, 4096)):
            _text(value, limit)

    def document(self) -> dict[str, str]:
        return {
            "provider": self.provider, "scope": self.scope,
            "kind": self.kind, "identity": self.identity,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.document(), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class MetadataObservation:
    payload: bytes
    observed_at: datetime
    reused: bool
    stale: bool = False


class SourceMetadataCache(CacheLayout):
    """Cache validated source bytes, never package trust or executable state.

    Fetch callbacks belong to the trusted adapter and validate their response
    before returning bytes. Cache hits still need the adapter's identity and
    schema checks. Offline use may select an expired mutable observation
    explicitly, with its original timestamp retained.
    """

    def __init__(
        self, root: Path | None = None, *, lock_timeout: float = 20,
        clock: Callable[[], datetime] = _now,
    ):
        super().__init__(root, lock_timeout=lock_timeout)
        self._clock = clock

    @contextmanager
    def _locked_record(self, key: MetadataKey) -> Iterator[Path]:
        if not isinstance(key, MetadataKey):
            raise CacheError("A source metadata operation requires an identified key.")
        stack = ExitStack()
        try:
            with cache_io():
                self._check_root()
                stack.enter_context(cache_lock(
                    self.root / "locks" / f"metadata-{key.digest}.lock",
                    exclusive=True, timeout=self.lock_timeout,
                ))
                self._check_root()
            yield self.root / "metadata" / "records" / f"{key.digest}.json"
        finally:
            with cache_io():
                stack.close()

    def _read(
        self, path: Path, key: MetadataKey, *, mutable: bool, now: datetime,
    ) -> MetadataObservation | None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        check_private_node(path, directory=False)
        with open_regular_file(path) as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        try:
            row = load_artifact_json(raw, limit=_MAX_RECORD_BYTES, label="Source metadata record")
            if not isinstance(row, dict) or row.keys() != {
                "apiVersion", "kind", "key", "mutable", "observedAt", "payload",
            }:
                raise ValueError
            if (
                row["apiVersion"] != "siteops/v1alpha1" or row["kind"] != "SourceMetadataRecord"
                or row["key"] != key.document() or type(row["mutable"]) is not bool
                or row["mutable"] != mutable or not isinstance(row["observedAt"], str)
                or len(row["observedAt"]) > 64
            ):
                raise ValueError
            observed = _timestamp(datetime.fromisoformat(row["observedAt"].replace("Z", "+00:00")))
            if utc_text(observed) != row["observedAt"]:
                raise ValueError
            payload = row["payload"]
            if not isinstance(payload, dict) or payload.keys() != {"size", "sha256", "base64"}:
                raise ValueError
            if (
                type(payload["size"]) is not int or not 0 <= payload["size"] <= MAX_METADATA_BYTES
                or not isinstance(payload["base64"], str) or len(payload["base64"]) > _MAX_ENCODED_BYTES
            ):
                raise ValueError
            content = base64.b64decode(payload["base64"].encode("ascii"), validate=True)
            if (
                len(content) != payload["size"]
                or hashlib.sha256(content).hexdigest() != payload["sha256"]
                or base64.b64encode(content).decode("ascii") != payload["base64"]
            ):
                raise ValueError
        except (ArtifactError, ValueError, UnicodeError, binascii.Error):
            raise CacheError(
                "Cached source metadata is incomplete or inconsistent.", code="cache.metadata-corrupt",
            ) from None
        if observed > now:
            raise CacheError(
                "The cached observation is later than the system clock.", code="cache.metadata-time",
            )
        return MetadataObservation(content, observed, True)

    def _write(
        self, path: Path, key: MetadataKey, payload: bytes, observed: datetime, *,
        mutable: bool, previous: MetadataObservation | None,
    ) -> None:
        if not isinstance(payload, bytes) or len(payload) > MAX_METADATA_BYTES:
            raise CacheError("Source metadata exceeds its byte limit.", code="cache.metadata-limit")
        if previous is not None and not mutable and previous.payload != payload:
            raise CacheError(
                "The source changed metadata bound to an immutable identity.", code="cache.metadata-identity",
            )
        self.prepare_namespace("metadata")
        raw = json.dumps({
            "apiVersion": "siteops/v1alpha1", "kind": "SourceMetadataRecord",
            "key": key.document(), "mutable": mutable, "observedAt": utc_text(observed),
            "payload": {
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "base64": base64.b64encode(payload).decode("ascii"),
            },
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(raw) > _MAX_RECORD_BYTES:
            raise CacheError("Source metadata exceeds its record limit.", code="cache.metadata-limit")
        temporary = path.parent / f".record-{uuid.uuid4().hex}.tmp"
        created = False
        try:
            write_new(temporary, raw)
            created = True
            check_private_node(temporary, directory=False)
            temporary.replace(path)
            created = False
        finally:
            if created:
                try:
                    temporary.unlink()
                except OSError:
                    logger.warning("Source metadata staging cleanup could not be completed.")

    def get_or_fetch(
        self, key: MetadataKey, fetch: Callable[[], bytes], *,
        maximum_age: float | None = None, refresh: bool = False, offline: bool = False,
    ) -> MetadataObservation:
        """Reuse an observation or publish one validated fetch under the key's lock.

        A missing maximum age denotes an immutable key. Refresh contacts the
        source explicitly. Fetch failures preserve the prior record and propagate,
        without silently selecting stale data or another source.
        """
        if (
            type(refresh) is not bool or type(offline) is not bool or (refresh and offline)
            or not callable(fetch) or (maximum_age is not None and (
                type(maximum_age) not in {int, float} or not math.isfinite(maximum_age)
                or not 0 <= maximum_age <= 86400
            ))
        ):
            raise CacheError("The source metadata read policy is invalid.", code="cache.metadata-policy")
        with self._locked_record(key) as path:
            now = _timestamp(self._clock())
            with cache_io():
                previous = self._read(path, key, mutable=maximum_age is not None, now=now)
            stale = previous is not None and maximum_age is not None and (
                now - previous.observed_at
            ).total_seconds() >= maximum_age
            if previous is not None and not refresh and (offline or not stale):
                return MetadataObservation(previous.payload, previous.observed_at, True, stale)
            if offline:
                raise CacheError(
                    "The requested source metadata is not cached for offline use.", code="cache.metadata-missing",
                )
            payload = fetch()
            observed = _timestamp(self._clock())
            with cache_io():
                self._write(path, key, payload, observed, mutable=maximum_age is not None, previous=previous)
            return MetadataObservation(payload, observed, False)

    def remember(self, key: MetadataKey, observation: MetadataObservation) -> None:
        """Bind an already validated observation to an immutable key, retaining its timestamp."""
        if not isinstance(observation, MetadataObservation):
            raise CacheError("An immutable metadata key requires an observation.")
        observed = _timestamp(observation.observed_at)
        with self._locked_record(key) as path, cache_io():
            now = _timestamp(self._clock())
            if observed > now:
                raise CacheError("The observation is later than the system clock.", code="cache.metadata-time")
            previous = self._read(path, key, mutable=False, now=now)
            if previous is not None and previous.payload == observation.payload:
                return
            self._write(path, key, observation.payload, observed, mutable=False, previous=previous)

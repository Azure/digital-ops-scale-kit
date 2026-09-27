# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deterministic public descriptions and separate source-private input bindings."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

from siteops.browse import (
    API_VERSION,
    MAX_ENTRIES,
    BrowseError,
    ContentEntry,
    ContentReader,
    check_path_components,
    conventional_candidate,
    guidance_path,
    parse_guidance,
)
from siteops.content_metadata import require_mapping as _mapping
from siteops.content_metadata import require_text as _string
from siteops.content_metadata import require_text_list as _strings
from siteops.content_metadata import validate_envelope as _envelope

INDEX_NAME = "siteops-index.json"
BINDINGS_NAME = "siteops-index.inputs.json"
MAX_INDEX_BYTES = 2 * 1024 * 1024
_PUBLIC_GUIDANCE = {
    "category", "tags", "documentation", "outcome", "inputs", "supplied",
    "prerequisites", "effects", "removal", "coverage",
}
_HEX = re.compile(r"^[0-9a-f]+$")
_ALGORITHM = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
logger = logging.getLogger(__name__)


def canonical_text(content: bytes) -> bytes:
    """Normalize uniform UTF-8 LF/CRLF source text for portable index identities."""
    try:
        normalized = content.decode("utf-8").replace("\r\n", "\n")
    except UnicodeError:
        raise BrowseError("index.encoding", "Index inputs must be UTF-8 text.") from None
    result = normalized.encode("utf-8")
    if "\r" in normalized or content not in {result, result.replace(b"\n", b"\r\n")}:
        raise BrowseError("index.encoding", "Use uniform LF or CRLF line endings before indexing.")
    return result


def canonical_index_path(value: Any, *, reference: bool = False) -> str:
    """Validate portable, workspace-relative index identities without file access."""
    value = _string(value)
    path = PurePosixPath(value)
    if (
        "\\" in value or "\x00" in value or path.is_absolute()
        or PureWindowsPath(value).drive or ".." in path.parts
        or value != path.as_posix() or value == "."
    ):
        raise ValueError("Expected a canonical relative path.")
    check_path_components(path, reference=reference)
    return value


def _documentation(value: str) -> str:
    path, separator, fragment = value.partition("#")
    return canonical_index_path(path, reference=True) + (separator + fragment if separator else "")


def _json_bytes(document: dict[str, Any]) -> bytes:
    content = (json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(content) > MAX_INDEX_BYTES:
        raise BrowseError("index.limit", "Generated index exceeds the size limit.")
    return content


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON is not supported.")


def _json_document(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_INDEX_BYTES:
        raise BrowseError("index.limit", "Index document exceeds the size limit.")
    try:
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        pending = [(document, 0)]
        nodes = 0
        while pending:
            value, depth = pending.pop()
            nodes += 1
            if nodes > 100000 or depth > 30:
                raise ValueError("JSON structure limit.")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        return _mapping(document)
    except (ValueError, UnicodeError, RecursionError):
        raise BrowseError("index.invalid", "Index JSON is invalid or exceeds structural limits.") from None


def _public_entry(entry: ContentEntry) -> dict[str, Any]:
    """Build an allowlist, never a redacted private inspection serialization."""
    guide = entry.guidance
    facts: dict[str, Any] = {
        "tags": list(guide.tags), "documentation": list(guide.documentation),
    }
    for key, value in (
        ("category", guide.category), ("outcome", guide.outcome),
        ("prerequisites", guide.prerequisites), ("effects", guide.effects),
        ("removal", guide.removal), ("coverage", guide.coverage),
    ):
        if value is not None:
            facts[key] = value
    if guide.inputs is not None:
        facts["inputs"] = []
        for item in guide.inputs:
            row = {
                "field": item.field, "type": item.type, "requirement": item.requirement,
                "description": item.description, "sensitivity": item.sensitivity,
            }
            if item.sensitivity == "non-sensitive" and item.default_behavior is not None:
                row["defaultBehavior"] = item.default_behavior
            facts["inputs"].append(row)
    if guide.supplied is not None:
        facts["supplied"] = [
            {"step": item.step, "input": item.input, "description": item.description}
            for item in guide.supplied
        ]
    return {"path": entry.path, "name": entry.name, "role": guide.role, "guidance": facts}


@dataclass(frozen=True)
class BuiltIndex:
    index: bytes
    bindings: bytes
    published: int
    unclassified: int


@dataclass(frozen=True)
class InputBinding:
    path: str
    digests: tuple[tuple[str, str], ...] | None

    def digest(self, algorithm: str) -> str | None:
        return dict(self.digests or ()).get(algorithm)


@dataclass(frozen=True)
class SourceBindings:
    index_digest: str
    candidates: tuple[str, ...]
    inputs: tuple[InputBinding, ...]


def build_content_index(
    workspace: Path,
    *,
    approve_public: bool = False,
    additional_digests: Callable[[bytes], Mapping[str, str]] | None = None,
) -> BuiltIndex:
    """Build approved descriptions from the same snapshots used for inspection."""
    if not approve_public:
        raise BrowseError(
            "index.approval",
            "Use --public to approve the selected authored descriptions for a public index.",
        )
    reader = ContentReader(workspace)
    entries = reader.inventory()
    if reader.diagnostics:
        raise BrowseError("index.incomplete", "Fix the workspace's browse diagnostics before indexing.")
    published = [
        _public_entry(entry) for entry in entries
        if entry.metadata_status == "declared" and entry.guidance.role != "unclassified"
    ]
    index = _json_bytes({
        "apiVersion": API_VERSION, "kind": "ContentIndex",
        "projection": "publishable", "entries": published,
    })
    inputs = []
    for path, content in sorted(reader.snapshots.items()):
        digests = None
        if content is not None:
            content = canonical_text(content)
            digests = {"sha256": hashlib.sha256(content).hexdigest()}
            if additional_digests:
                extra = dict(additional_digests(content))
                if "sha256" in extra:
                    raise ValueError("A source binding cannot replace the content digest.")
                digests.update(extra)
        inputs.append({"path": path, "digests": digests})
    bindings = _json_bytes({
        "apiVersion": API_VERSION, "kind": "ContentIndexBindings",
        "projection": "source-private", "indexDigest": hashlib.sha256(index).hexdigest(),
        "normalization": "utf8-lf",
        "candidates": list(reader.candidate_paths), "inputs": inputs,
    })
    load_content_index(index)
    load_source_bindings(bindings, index)
    return BuiltIndex(index, bindings, len(published), len(entries) - len(published))


def load_content_index(content: bytes) -> tuple[ContentEntry, ...]:
    """Read a publication projection without accepting private inspection fields."""
    try:
        data = _envelope(
            _json_document(content), "ContentIndex",
            {"apiVersion", "kind", "projection", "entries"},
        )
        if data.get("projection") != "publishable":
            raise ValueError("Wrong projection.")
        rows = data.get("entries")
        if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
            raise ValueError("Invalid entry collection.")
        entries = []
        paths = set()
        for value in rows:
            row = _mapping(value, {"path", "name", "role", "guidance"})
            path = canonical_index_path(row.get("path"))
            if path in paths:
                raise ValueError("Duplicate entry identity.")
            paths.add(path)
            role = _string(row.get("role"))
            if role not in {"standalone", "partial"}:
                raise ValueError("Only declared roles can be published.")
            guide = _mapping(row.get("guidance"), _PUBLIC_GUIDANCE)
            for collection, allowed in (
                ("inputs", {
                    "field", "type", "requirement", "description", "sensitivity", "defaultBehavior",
                }),
                ("supplied", {"step", "input", "description"}),
            ):
                if collection in guide:
                    if not isinstance(guide[collection], list):
                        raise ValueError("Invalid guidance collection.")
                    for item in guide[collection]:
                        _mapping(item, allowed)
            guidance = parse_guidance(
                {"apiVersion": API_VERSION, "kind": "DeploymentEntry", "role": role, **guide},
                documentation_reference=_documentation,
            )
            entries.append(ContentEntry(
                path=path, name=_string(row.get("name")), description=guidance.outcome or "",
                selector=None, sites=(), guidance=guidance,
                metadata_status="indexed", targeting_known=False,
            ))
        return tuple(sorted(entries, key=lambda entry: entry.path))
    except BrowseError:
        raise
    except ValueError:
        raise BrowseError("index.invalid", "Published content index has an invalid contract.") from None


def _digest(value: Any, *, length: int | None = None) -> str:
    value = _string(value)
    if not _HEX.fullmatch(value) or len(value) > 128 or (length and len(value) != length):
        raise ValueError("Invalid digest.")
    return value


def load_source_bindings(content: bytes, index: bytes) -> SourceBindings:
    """Read source-private freshness evidence separately from public descriptions."""
    try:
        data = _envelope(
            _json_document(content), "ContentIndexBindings",
            {"apiVersion", "kind", "projection", "indexDigest", "normalization", "candidates", "inputs"},
        )
        if data.get("projection") != "source-private":
            raise ValueError("Wrong binding projection.")
        if data.get("normalization") != "utf8-lf":
            raise ValueError("Unsupported source normalization.")
        digest = _digest(data.get("indexDigest"), length=64)
        if digest != hashlib.sha256(canonical_text(index)).hexdigest():
            raise BrowseError("index.stale", "Index and source bindings do not describe the same bytes.")
        candidates = tuple(canonical_index_path(path) for path in _strings(data.get("candidates")))
        if len(candidates) > MAX_ENTRIES:
            raise ValueError("Too many candidates.")
        raw_inputs = data.get("inputs")
        if not isinstance(raw_inputs, list) or len(raw_inputs) > MAX_ENTRIES * 2 + 1:
            raise ValueError("Invalid input collection.")
        inputs = []
        for item in raw_inputs:
            row = _mapping(item, {"path", "digests"})
            if "digests" not in row:
                raise ValueError("Input presence must be explicit.")
            path = canonical_index_path(row.get("path"))
            digests = None
            if row.get("digests") is not None:
                values = _mapping(row["digests"])
                _digest(values.get("sha256"), length=64)
                for algorithm, value in values.items():
                    if not _ALGORITHM.fullmatch(algorithm):
                        raise ValueError("Invalid digest algorithm.")
                    _digest(value)
                digests = tuple(sorted(values.items()))
            inputs.append(InputBinding(path, digests))
        expected = {"content.yaml", *candidates}
        expected.update(guidance_path(PurePosixPath(path)).as_posix() for path in candidates)
        paths = [item.path for item in inputs]
        if len(paths) != len(set(paths)) or set(paths) != expected:
            raise ValueError("Binding input coverage differs from the candidates.")
        if any(item.digests is None for item in inputs if item.path in candidates):
            raise ValueError("Manifest candidates must have content identities.")
        return SourceBindings(digest, candidates, tuple(inputs))
    except BrowseError:
        raise
    except ValueError:
        raise BrowseError("index.bindings", "Source bindings are invalid or incomplete.") from None


def validate_source_snapshot(
    bindings: SourceBindings,
    source_paths: set[str],
    source_digests: Mapping[str, str],
    *,
    algorithm: str,
    equivalent_algorithms: tuple[str, ...] = (),
) -> None:
    """Check freshness using a source adapter's immutable file identities."""
    available_paths = {path.casefold() for path in source_paths}
    current = {
        path for path in source_paths
        if conventional_candidate(PurePosixPath(path), available_paths=available_paths)
    }
    expected = {
        path for path in bindings.candidates
        if conventional_candidate(PurePosixPath(path), available_paths=available_paths)
    }
    if current != expected:
        raise BrowseError("index.stale", "Deployment entries changed. The source owner must rebuild its index.")
    for item in bindings.inputs:
        actual = source_digests.get(item.path)
        if item.digests is None:
            if item.path in source_paths:
                raise BrowseError("index.stale", "Entry guidance changed. Rebuild the source index.")
            continue
        expected_digest = item.digest(algorithm)
        if expected_digest is None:
            raise BrowseError(
                "index.binding_missing", "The index lacks freshness bindings for this source provider."
            )
        permitted = {
            value for name in (algorithm, *equivalent_algorithms)
            if (value := item.digest(name)) is not None
        }
        if actual not in permitted:
            raise BrowseError("index.stale", "Indexed source inputs changed. Rebuild the source index.")


def _binding_algorithms(document: dict[str, Any]) -> set[str]:
    inputs = document.get("inputs")
    if not isinstance(inputs, list):
        raise BrowseError("index.output", "Existing source binding settings are invalid.")
    algorithms: set[str] = set()
    for value in inputs:
        try:
            row = _mapping(value, {"path", "digests"})
            if row.get("digests") is not None:
                algorithms.update(_mapping(row["digests"]))
        except ValueError:
            raise BrowseError("index.output", "Existing source binding settings are invalid.") from None
    return algorithms


def write_content_index(workspace: Path, bundle: BuiltIndex, *, check: bool = False) -> None:
    """Replace generated outputs only, or compare them without writing."""
    root = Path(workspace).resolve()
    outputs = (
        (root / INDEX_NAME, bundle.index, "ContentIndex"),
        (root / BINDINGS_NAME, bundle.bindings, "ContentIndexBindings"),
    )
    modes: dict[Path, int] = {}
    new_algorithms = _binding_algorithms(_json_document(bundle.bindings))
    for path, expected, kind in outputs:
        if path.is_symlink():
            raise BrowseError("index.output", "Generated index outputs cannot be links.")
        if path.exists():
            try:
                info = path.stat()
                modes[path] = stat.S_IMODE(info.st_mode)
                if info.st_size > MAX_INDEX_BYTES:
                    raise BrowseError("index.output", "Existing index output exceeds the size limit.")
                existing = path.read_bytes()
                existing_document = _json_document(existing)
                if (
                    existing_document.get("kind") != kind
                    or existing_document.get("apiVersion") != API_VERSION
                ):
                    raise BrowseError("index.output", "Refusing to overwrite an unrelated output file.")
                if kind == "ContentIndexBindings":
                    removed = _binding_algorithms(existing_document) - new_algorithms
                    if removed:
                        raise BrowseError(
                            "index.binding_settings",
                            "Refreshing would remove source bindings. Keep the same --for-source setting.",
                        )
            except OSError:
                raise BrowseError("index.output", "Index output could not be read.") from None
            if check and canonical_text(existing) != expected:
                raise BrowseError("index.stale", "Generated index outputs need to be rebuilt.")
        elif check:
            raise BrowseError("index.missing", "Generated index outputs are missing.")
    if check:
        return
    staged: list[tuple[Path, Path]] = []
    directory = None
    try:
        directory = Path(tempfile.mkdtemp(dir=root, prefix=".siteops-index-"))
        for path, content, _ in outputs:
            temporary = directory / path.name
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            staged.append((temporary, path))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path in modes:
                temporary.chmod(modes[path] & 0o600)
        for temporary, path in staged:
            temporary.replace(path)
    except OSError:
        raise BrowseError("index.output", "Generated index outputs could not be written.") from None
    finally:
        cleanup_failed = False
        for temporary, _ in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        if directory is not None:
            try:
                directory.rmdir()
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            logger.warning("Generated-index temporary cleanup could not be completed.")

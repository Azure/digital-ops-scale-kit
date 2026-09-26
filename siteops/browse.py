# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Site-independent inspection of local deployment entries.

Only manifest headers and descriptive metadata are read. Preparation, source
verification and deployment remain separate boundaries.
"""

from __future__ import annotations

import os
import stat
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Callable

import yaml

from siteops import yamlio
from siteops.artifact_verification import utc_text
from siteops.artifacts import ArtifactError, check_portable_component
from siteops.artifacts import is_link as _is_link
from siteops.content_metadata import API_VERSION
from siteops.content_metadata import require_mapping as _mapping
from siteops.content_metadata import require_text as _string
from siteops.content_metadata import require_text_list as _strings
from siteops.content_metadata import validate_envelope as _envelope
from siteops.manifest_selection import (
    ManifestSelectionError,
    is_explicit_manifest_path,
    select_manifest_path,
)
from siteops.models import _parse_manifest_spec

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_DIRECTORY_ITEMS = 10000
MAX_DEPTH = 12
_PROTECTED = frozenset(
    {"sites", "sites.local", "parameters", "resource-sets", "answers", "runs"}
)
_GUIDANCE_KEYS = frozenset(
    {
        "apiVersion", "kind", "role", "category", "tags", "documentation",
        "outcome", "inputs", "supplied", "prerequisites", "effects", "removal", "coverage",
    }
)


@dataclass(frozen=True)
class BrowseDiagnostic:
    code: str
    summary: str
    path: str | None = None
    line: int | None = None

    def document(self) -> dict[str, Any]:
        """Return a value-safe diagnostic without parser exception text."""
        return {
            "code": self.code, "summary": self.summary,
            "path": self.path, "line": self.line,
        }


class BrowseError(ValueError):
    """An expected, safely reportable inspection failure."""

    def __init__(
        self, code: str, summary: str, path: str | None = None, line: int | None = None
    ):
        super().__init__(summary)
        self.diagnostic = BrowseDiagnostic(code, summary, path, line)


@dataclass(frozen=True)
class InputGuidance:
    field: str
    type: str
    requirement: str
    description: str
    sensitivity: str = "unknown"
    default_behavior: str | None = None
    source: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "field": self.field, "type": self.type, "requirement": self.requirement,
            "description": self.description, "sensitivity": self.sensitivity,
            "defaultBehavior": self.default_behavior, "source": self.source,
        }


@dataclass(frozen=True)
class SuppliedGuidance:
    step: str
    input: str
    description: str
    source: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "step": self.step, "input": self.input,
            "description": self.description, "source": self.source,
        }


@dataclass(frozen=True)
class EntryGuidance:
    role: str = "unclassified"
    category: str | None = None
    tags: tuple[str, ...] = ()
    documentation: tuple[str, ...] = ()
    outcome: str | None = None
    inputs: tuple[InputGuidance, ...] | None = None
    supplied: tuple[SuppliedGuidance, ...] | None = None
    prerequisites: tuple[str, ...] | None = None
    effects: tuple[str, ...] | None = None
    removal: tuple[str, ...] | None = None
    coverage: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "category": self.category, "tags": list(self.tags),
            "documentation": list(self.documentation), "outcome": self.outcome,
            "inputs": None if self.inputs is None else [item.document() for item in self.inputs],
            "supplied": None if self.supplied is None else [
                item.document() for item in self.supplied
            ],
            "prerequisites": self.prerequisites, "effects": self.effects,
            "removal": self.removal, "coverage": self.coverage,
        }


@dataclass(frozen=True)
class ContentEntry:
    path: str
    name: str
    description: str
    selector: str | None
    sites: tuple[str, ...]
    guidance: EntryGuidance = field(default_factory=EntryGuidance)
    metadata_status: str = "absent"
    name_ambiguous: bool | None = None
    targeting_known: bool = True

    def document(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "description": self.description,
            "role": self.guidance.role, "metadataStatus": self.metadata_status,
            "nameAmbiguous": self.name_ambiguous,
            "targeting": {
                "known": self.targeting_known, "selector": self.selector, "sites": list(self.sites),
            },
            "guidance": self.guidance.document(),
        }


@dataclass(frozen=True)
class SourceObservation:
    """Record when source metadata was observed, separately from index or package trust."""

    origin: str
    observed_at: datetime
    refresh_after: datetime | None
    offline: bool
    stale: bool

    def document(self) -> dict[str, Any]:
        return {
            "origin": self.origin, "observedAt": utc_text(self.observed_at),
            "refreshAfter": utc_text(self.refresh_after) if self.refresh_after is not None else None,
            "offline": self.offline, "stale": self.stale,
        }


@dataclass(frozen=True)
class BrowseSource:
    """Consumer-established source context, never a package's self-certification."""

    kind: str
    reference: str
    revision: str | None = None
    provider: str | None = None
    index_status: str | None = None
    observation: SourceObservation | None = None
    project: str | None = None
    verification: str = "not-performed"

    def document(self, workspace: str) -> dict[str, Any]:
        result = {
            "kind": self.kind, "reference": self.reference, "version": self.revision,
            "provider": self.provider, "workspace": workspace,
            "indexStatus": self.index_status, "verification": self.verification,
        }
        if self.observation is not None:
            result["observation"] = self.observation.document()
        if self.project is not None:
            result["project"] = self.project
        return result


@dataclass(frozen=True)
class BrowseResult:
    workspace: str
    entries: tuple[ContentEntry, ...] = ()
    diagnostics: tuple[BrowseDiagnostic, ...] = ()
    selected: bool = False
    discovered: int = 0
    matched: int = 0
    name_inventory_complete: bool | None = None
    source: BrowseSource | None = None

    @property
    def status(self) -> str:
        if not self.diagnostics:
            return "complete"
        return "partial" if self.entries else "invalid"

    def document(self) -> dict[str, Any]:
        """Project inspection without prepared values or resource-state observations.

        `local-private` retains the existing destination label. It does not
        mean the described source is a local filesystem.
        """
        return {
            "apiVersion": API_VERSION, "kind": "ContentInspection",
            "projection": "local-private", "status": self.status,
            "mode": "entry" if self.selected else "inventory",
            "source": self.source.document(self.workspace) if self.source else {
                "kind": "local", "workspace": self.workspace,
                "version": None, "verification": "not-performed",
            },
            "preparation": "not-performed", "observations": "not-performed",
            "discovered": self.discovered, "matched": self.matched,
            "shown": len(self.entries), "hasMore": self.matched > len(self.entries),
            "nameInventoryComplete": self.name_inventory_complete,
            "entries": [entry.document() for entry in self.entries],
            "diagnostics": [diagnostic.document() for diagnostic in self.diagnostics],
        }


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    return _string(data[key]) if key in data else None


def _optional_strings(data: dict[str, Any], key: str) -> tuple[str, ...] | None:
    return _strings(data[key]) if key in data else None


def _is_guidance_file(path: Path) -> bool:
    name = path.name.casefold()
    return name == "entry.yaml" or name.endswith(".entry.yaml")


def guidance_path(path: PurePosixPath) -> PurePosixPath:
    """Locate the sidecar without accessing a filesystem or source provider."""
    return (
        path.with_name("entry.yaml")
        if path.name.casefold() in {"manifest.yaml", "manifest.yml"}
        else path.with_suffix(".entry.yaml")
    )


def conventional_candidate(
    path: PurePosixPath, *, available_paths: Collection[str] = (),
) -> bool:
    """Apply the shared discovery convention to a workspace-relative source path."""
    if not path.parts or path.parts[0] not in {"manifests", "samples"}:
        return False
    if any(part.startswith(".") or part.casefold() in _PROTECTED for part in path.parts):
        return False
    if (
        _is_guidance_file(path)
        or path.name.casefold().endswith(".inputs.yaml")
        or (
            path.name.casefold() == "inputs.yaml"
            and path.parts[0] == "manifests"
            and len(path.parts) > 2
            and any(
                path.with_name(name).as_posix().casefold() in available_paths
                for name in ("manifest.yaml", "manifest.yml")
            )
        )
        or path.suffix.casefold() not in {".yaml", ".yml"}
    ):
        return False
    return (
        path.parts[0] == "manifests"
        or path.name.casefold() in {"manifest.yaml", "manifest.yml"}
        or path.name.startswith("_")
    )


def check_path_components(relative: PurePath, *, reference: bool = False) -> None:
    """Apply the shared portable-path policy without accessing a filesystem."""
    for part in relative.parts:
        try:
            check_portable_component(part)
        except ArtifactError as error:
            raise BrowseError(error.code, str(error)) from None
        if part.startswith(".") or (not reference and part.casefold() in _PROTECTED):
            raise BrowseError(
                "path.protected", "Configuration and working-state paths are excluded."
            )


def parse_guidance(
    document: Any,
    *,
    documentation_reference: Callable[[str], str] | None = None,
    source_reference: Callable[[str], str] | None = None,
) -> EntryGuidance:
    """Parse descriptive facts independently of local or remote source access."""
    data = _envelope(document, "DeploymentEntry", _GUIDANCE_KEYS)
    role = _string(data.get("role", "unclassified"))
    if role not in {"standalone", "partial", "unclassified"}:
        raise ValueError("Invalid role.")

    def source(row: dict[str, Any]) -> str | None:
        value = _optional_string(row, "source")
        return source_reference(value) if value is not None and source_reference else value

    inputs = None
    if "inputs" in data:
        if not isinstance(data["inputs"], list):
            raise ValueError("Invalid inputs.")
        parsed_inputs = []
        for raw in data["inputs"]:
            row = _mapping(raw, {
                "field", "type", "requirement", "description",
                "sensitivity", "defaultBehavior", "source",
            })
            requirement = _string(row.get("requirement"))
            sensitivity = _string(row.get("sensitivity", "unknown"))
            if requirement not in {"required", "optional", "conditional", "unknown"}:
                raise ValueError("Invalid requirement.")
            if sensitivity not in {"sensitive", "non-sensitive", "unknown"}:
                raise ValueError("Invalid sensitivity.")
            if sensitivity != "non-sensitive" and "defaultBehavior" in row:
                raise ValueError("Protected inputs cannot carry default text.")
            parsed_inputs.append(InputGuidance(
                field=_string(row.get("field")), type=_string(row.get("type")),
                requirement=requirement, description=_string(row.get("description")),
                sensitivity=sensitivity, default_behavior=_optional_string(row, "defaultBehavior"),
                source=source(row),
            ))
        if len({item.field for item in parsed_inputs}) != len(parsed_inputs):
            raise ValueError("Duplicate input.")
        inputs = tuple(parsed_inputs)
    supplied = None
    if "supplied" in data:
        if not isinstance(data["supplied"], list):
            raise ValueError("Invalid supplied inputs.")
        parsed_supplied = []
        for raw in data["supplied"]:
            row = _mapping(raw, {"step", "input", "description", "source"})
            parsed_supplied.append(SuppliedGuidance(
                step=_string(row.get("step")), input=_string(row.get("input")),
                description=_string(row.get("description")), source=source(row),
            ))
        if len({(item.step, item.input) for item in parsed_supplied}) != len(parsed_supplied):
            raise ValueError("Duplicate supplied input.")
        supplied = tuple(parsed_supplied)
    documentation = _strings(data.get("documentation", []))
    return EntryGuidance(
        role=role, category=_optional_string(data, "category"),
        tags=_strings(data.get("tags", [])),
        documentation=tuple(
            documentation_reference(item) if documentation_reference else item
            for item in documentation
        ),
        outcome=_optional_string(data, "outcome"), inputs=inputs, supplied=supplied,
        prerequisites=_optional_strings(data, "prerequisites"),
        effects=_optional_strings(data, "effects"),
        removal=_optional_strings(data, "removal"),
        coverage=_optional_string(data, "coverage"),
    )


class ContentReader:
    """One bounded read of a selected workspace, independent of Site state."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.bytes_read = 0
        self.directory_items = 0
        self.diagnostics: list[BrowseDiagnostic] = []
        self.names_complete = True
        self.snapshots: dict[str, bytes | None] = {}
        self.candidate_paths: tuple[str, ...] = ()
        if not self.workspace.is_dir():
            raise BrowseError("workspace.missing", "Workspace directory was not found.")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def filename_candidate(self, selection: str) -> str | None:
        """Return a regular workspace file candidate without reading metadata."""
        try:
            path = self._path(selection)
        except BrowseError as error:
            if error.diagnostic.code in {
                "path.invalid", "path.outside", "path.alias", "path.protected",
            }:
                return None
            raise
        try:
            return self._relative(path) if path.is_file() else None
        except OSError:
            raise BrowseError("path.unreadable", "The content path is unreadable.") from None

    def _incomplete(self, diagnostic: BrowseDiagnostic) -> None:
        self.names_complete = False
        self.diagnostics.append(diagnostic)

    def _path(self, value: str | Path, *, reference: bool = False) -> Path:
        raw = str(value).replace("\\", "/")
        if "\x00" in raw or (
            PureWindowsPath(raw).drive and not PureWindowsPath(raw).is_absolute()
        ):
            raise BrowseError("path.invalid", "Content path has an unsupported form.")
        if PureWindowsPath(raw).drive and os.name != "nt":
            raise BrowseError("path.outside", "Choose a path inside the workspace.")
        path = Path(os.path.abspath(self.workspace / raw))
        try:
            relative = path.relative_to(self.workspace)
        except ValueError:
            raise BrowseError("path.outside", "Choose a path inside the workspace.") from None
        check_path_components(relative, reference=reference)
        current = self.workspace
        for part in relative.parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError:
                raise BrowseError("path.unreadable", "The content path is unreadable.") from None
            if _is_link(info):
                raise BrowseError(
                    "path.link", "Links and reparse points are excluded from inspection.",
                    relative.as_posix(),
                )
        try:
            canonical = path.resolve()
        except (OSError, RuntimeError):
            raise BrowseError("path.unreadable", "Content path could not be resolved.") from None
        try:
            canonical_relative = canonical.relative_to(self.workspace)
        except ValueError:
            raise BrowseError("path.outside", "Choose a path inside the workspace.") from None
        check_path_components(canonical_relative, reference=reference)
        return canonical

    def _yaml(self, path: Path, *, optional: bool = False) -> Any:
        path = self._path(path)
        relative = self._relative(path)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise BrowseError("file.invalid", "Expected a regular content file.", relative)
            if info.st_nlink > 1:
                raise BrowseError("file.link", "Hardlinked content files are excluded.", relative)
            if info.st_size > MAX_FILE_BYTES:
                raise BrowseError("file.limit", "Content file exceeds the inspection limit.", relative)
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            if optional:
                if relative in self.snapshots and self.snapshots[relative] is not None:
                    raise BrowseError("source.changed", "Content changed during inspection.", relative)
                self.snapshots[relative] = None
                return None
            raise BrowseError("file.missing", "Content file was not found.", relative) from None
        except OSError:
            raise BrowseError("file.unreadable", "Content file could not be read.", relative) from None
        self.bytes_read += len(content)
        if len(content) > MAX_FILE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
            raise BrowseError("read.limit", "Content exceeds the inspection read budget.", relative)
        if relative in self.snapshots and self.snapshots[relative] != content:
            raise BrowseError("source.changed", "Content changed during inspection.", relative)
        self.snapshots[relative] = content
        try:
            return yamlio.load_bounded(content.decode("utf-8"))
        except yamlio.YamlStructureLimitError:
            raise BrowseError(
                "yaml.limit", "Content YAML exceeds inspection structural limits.", relative
            ) from None
        except (UnicodeError, yaml.YAMLError, RecursionError) as error:
            mark = getattr(error, "problem_mark", None)
            line = mark.line + 1 if mark is not None else None
            raise BrowseError(
                "yaml.invalid", "Content YAML is invalid or exceeds structural limits.", relative, line
            ) from None

    def _reference(self, text: str, parent: Path) -> str:
        path_text, separator, fragment = _string(text).partition("#")
        path = self._path(parent / path_text, reference=True)
        return self._relative(path) + (separator + fragment if separator else "")

    def _guidance(self, document: Any, path: Path) -> EntryGuidance:
        return parse_guidance(
            document,
            documentation_reference=lambda value: self._reference(value, path.parent),
            source_reference=lambda value: self._reference(value, self.workspace),
        )

    def entry(self, path: Path) -> ContentEntry:
        """Read only this header and its sidecar, including on incomplete inventories."""
        path = self._path(path)
        relative = self._relative(path)
        try:
            data = _mapping(self._yaml(path))
            spec, name, description = _parse_manifest_spec(data, path)
            if "selector" in spec and "siteSelector" in spec:
                raise ValueError("Ambiguous targeting.")
            selector = spec.get("selector", spec.get("siteSelector"))
            entry = ContentEntry(
                path=relative, name=_string(name), description=_string(description, empty=True),
                selector=None if selector is None else _string(selector),
                sites=_strings([] if spec.get("sites") is None else spec["sites"]),
            )
        except ValueError as error:
            if isinstance(error, BrowseError):
                raise
            raise BrowseError("manifest.invalid", "Manifest header is invalid.", relative) from None
        metadata = self.workspace / guidance_path(PurePosixPath(relative)).as_posix()
        try:
            document = self._yaml(metadata, optional=True)
            if document is None:
                if metadata.exists():
                    raise ValueError("Empty metadata.")
                return entry
            guidance = self._guidance(document, metadata)
            return replace(entry, guidance=guidance, metadata_status="declared")
        except BrowseError as error:
            self.diagnostics.append(error.diagnostic)
        except ValueError:
            self.diagnostics.append(BrowseDiagnostic(
                "metadata.invalid", "Entry guidance is invalid or unsupported.",
                self._relative(metadata),
            ))
        return replace(entry, metadata_status="unavailable")

    def _scan(self) -> set[Path]:
        candidates: set[Path] = set()
        stack = [(self.workspace / name, 0) for name in ("samples", "manifests")]
        while stack:
            directory, depth = stack.pop()
            try:
                directory = self._path(directory)
                if not directory.exists():
                    continue
                if depth > MAX_DEPTH:
                    raise BrowseError("scan.depth", "Content nesting exceeds the inspection limit.")
                children = []
                with os.scandir(directory) as iterator:
                    for child in iterator:
                        self.directory_items += 1
                        if self.directory_items > MAX_DIRECTORY_ITEMS:
                            raise BrowseError("scan.limit", "Directory inventory exceeds the limit.")
                        children.append(child)
                available_paths = {
                    self._relative(Path(child.path)).casefold() for child in children
                    if child.name.casefold() in {"manifest.yaml", "manifest.yml"}
                    and child.is_file(follow_symlinks=False)
                }
                for child in sorted(children, key=lambda item: item.name):
                    if child.name.startswith(".") or child.name.casefold() in _PROTECTED:
                        continue
                    path = Path(child.path)
                    if _is_guidance_file(path):
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                    except OSError:
                        self._incomplete(BrowseDiagnostic(
                            "scan.unreadable", "Content path could not be inspected.",
                            self._relative(path),
                        ))
                        continue
                    if _is_link(info):
                        self._incomplete(BrowseDiagnostic(
                            "path.link", "Links and reparse points are excluded from inspection.",
                            self._relative(path),
                        ))
                        continue
                    if child.is_dir(follow_symlinks=False):
                        stack.append((path, depth + 1))
                    elif conventional_candidate(
                        PurePosixPath(self._relative(path)), available_paths=available_paths,
                    ):
                        candidates.add(path)
                        if len(candidates) > MAX_ENTRIES:
                            raise BrowseError("scan.limit", "Entry inventory exceeds the limit.")
            except BrowseError as error:
                self._incomplete(error.diagnostic)
                if error.diagnostic.code == "scan.limit":
                    break
            except OSError:
                self._incomplete(BrowseDiagnostic(
                    "scan.unreadable", "Content directory could not be inspected.",
                    self._relative(directory),
                ))
        return candidates

    def inventory(self) -> tuple[ContentEntry, ...]:
        """Discover bounded conventional candidates and explicitly named additions."""
        candidates = self._scan()
        additions = self.workspace / "content.yaml"
        try:
            document = self._yaml(additions, optional=True)
            if document is not None:
                data = _envelope(document, "WorkspaceContent", {"apiVersion", "kind", "entries"})
                paths = [self._path(item) for item in _strings(data.get("entries", []))]
                normalized = [os.path.normcase(str(path)) for path in paths]
                if len(normalized) != len(set(normalized)):
                    raise ValueError("Duplicate normalized path.")
                candidates.update(paths)
            elif additions.exists():
                raise ValueError("Empty workspace metadata.")
        except BrowseError as error:
            self._incomplete(error.diagnostic)
        except ValueError:
            self._incomplete(BrowseDiagnostic(
                "metadata.invalid", "Workspace content metadata is invalid or unsupported.",
                "content.yaml",
            ))
        if len(candidates) > MAX_ENTRIES:
            self._incomplete(BrowseDiagnostic("scan.limit", "Entry inventory exceeds the limit."))
        self.candidate_paths = tuple(sorted(self._relative(path) for path in candidates))
        entries = []
        canonical: set[str] = set()
        for path in sorted(candidates, key=self._relative)[:MAX_ENTRIES]:
            key = os.path.normcase(str(path))
            if key in canonical:
                continue
            canonical.add(key)
            try:
                entries.append(self.entry(path))
            except BrowseError as error:
                self._incomplete(error.diagnostic)
            if self.bytes_read > MAX_TOTAL_BYTES:
                self.names_complete = False
                break
        return tuple(entries)


def validate_browse_options(
    selection: str | None,
    search: str | None,
    tags: tuple[str, ...],
    category: str | None,
    limit: int | None,
) -> None:
    """Reject incompatible presentation options before source access."""
    if limit is not None and limit < 1:
        raise ValueError("The result limit must be positive.")
    if selection is not None and (search is not None or tags or category is not None or limit):
        raise ValueError("A selected entry cannot be combined with inventory filters.")


def inspect_content(
    workspace: Path,
    selection: str | None = None,
    *,
    search: str | None = None,
    tags: tuple[str, ...] = (),
    category: str | None = None,
    include_partials: bool = False,
    limit: int | None = None,
) -> BrowseResult:
    """Inspect a path or exact name, or filter a complete local inventory."""
    validate_browse_options(selection, search, tags, category, limit)
    reader = ContentReader(workspace)
    if selection is not None and is_explicit_manifest_path(selection):
        try:
            entry = reader.entry(Path(selection.replace("\\", "/")))
        except BrowseError as error:
            return BrowseResult(
                str(reader.workspace), diagnostics=(error.diagnostic,), selected=True
            )
        return BrowseResult(
            str(reader.workspace), (entry,), tuple(reader.diagnostics), True, 1, 1
        )
    inventory = reader.inventory()
    filename_match = None
    if selection is not None:
        try:
            filename_match = reader.filename_candidate(selection)
            if filename_match is not None and not any(
                entry.path == filename_match for entry in inventory
            ):
                inventory = (*inventory, reader.entry(reader.workspace / filename_match))
        except BrowseError as error:
            return BrowseResult(
                str(reader.workspace), diagnostics=(*reader.diagnostics, error.diagnostic),
                selected=True,
            )
    return select_entries(
        BrowseResult(
            str(reader.workspace), inventory, tuple(reader.diagnostics),
            discovered=len(inventory), name_inventory_complete=reader.names_complete,
        ),
        selection, search=search, tags=tags, category=category,
        include_partials=include_partials, limit=limit,
        filename_match=filename_match,
    )


def select_entries(
    result: BrowseResult,
    selection: str | None = None,
    *,
    search: str | None = None,
    tags: tuple[str, ...] = (),
    category: str | None = None,
    include_partials: bool = False,
    limit: int | None = None,
    selection_is_path: bool = False,
    filename_match: str | None = None,
) -> BrowseResult:
    """Use one selection and filtering model for local and published inventories."""
    validate_browse_options(selection, search, tags, category, limit)
    diagnostics = list(result.diagnostics)
    if selection is not None and selection_is_path:
        matches = tuple(entry for entry in result.entries if entry.path == selection)
        if not matches:
            diagnostics.append(BrowseDiagnostic(
                "lookup.missing", "This path is not included in the published index."
            ))
        return replace(
            result, entries=matches, diagnostics=tuple(diagnostics),
            selected=len(matches) == 1, matched=len(matches),
        )
    visible = tuple(
        entry for entry in result.entries if include_partials or entry.guidance.role != "partial"
    )
    counts = Counter(entry.name for entry in visible)
    visible = tuple(replace(
        entry,
        name_ambiguous=counts[entry.name] > 1 if result.name_inventory_complete else None,
    ) for entry in visible)
    if selection is not None:
        candidates_by_path = {entry.path: entry for entry in result.entries}
        candidates_by_path.update((entry.path, entry) for entry in visible)
        try:
            path = select_manifest_path(
                selection, ((entry.name, entry.path) for entry in visible),
                names_complete=bool(result.name_inventory_complete),
                filename_match=filename_match,
            )
        except ManifestSelectionError as error:
            diagnostics.append(BrowseDiagnostic(error.code, str(error)))
            matches = tuple(
                candidates_by_path[path] for path in error.paths if path in candidates_by_path
            ) if error.code == "lookup.ambiguous" else ()
            selected = False
        else:
            matches = (candidates_by_path[path],) if path in candidates_by_path else ()
            selected = len(matches) == 1
        return replace(
            result, entries=matches, diagnostics=tuple(diagnostics),
            selected=selected, matched=len(matches),
        )
    terms = tuple((search or "").casefold().split())
    matches = tuple(
        entry for entry in visible
        if (category is None or entry.guidance.category == category)
        and set(tags) <= set(entry.guidance.tags)
        and (
            not terms
            or all(term in " ".join((
                entry.name, entry.description, entry.path, entry.guidance.category or "",
                *entry.guidance.tags,
            )).casefold() for term in terms)
        )
    )
    return replace(result, entries=matches[:limit], selected=False, matched=len(matches))

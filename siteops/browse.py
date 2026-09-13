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
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from siteops import yamlio
from siteops.models import _parse_manifest_spec

API_VERSION = "siteops/v1alpha1"
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

    def document(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "description": self.description,
            "role": self.guidance.role, "metadataStatus": self.metadata_status,
            "nameAmbiguous": self.name_ambiguous,
            "targeting": {"selector": self.selector, "sites": list(self.sites)},
            "guidance": self.guidance.document(),
        }


@dataclass(frozen=True)
class BrowseResult:
    workspace: str
    entries: tuple[ContentEntry, ...] = ()
    diagnostics: tuple[BrowseDiagnostic, ...] = ()
    selected: bool = False
    discovered: int = 0
    matched: int = 0
    name_inventory_complete: bool | None = None

    @property
    def status(self) -> str:
        if not self.diagnostics:
            return "complete"
        return "partial" if self.entries else "invalid"

    def document(self) -> dict[str, Any]:
        """Project private inspection data without executable or provider state."""
        return {
            "apiVersion": API_VERSION, "kind": "ContentInspection",
            "projection": "local-private", "status": self.status,
            "mode": "entry" if self.selected else "inventory",
            "source": {
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


def _mapping(value: Any, allowed: frozenset[str] | set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Expected a string-keyed mapping.")
    if allowed is not None and value.keys() - allowed:
        raise ValueError("Unknown metadata field.")
    return value


def _string(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError("Expected text.")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    return _string(data[key]) if key in data else None


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a list.")
    result = tuple(_string(item) for item in value)
    if len(result) != len(set(result)):
        raise ValueError("Duplicate list item.")
    return result


def _optional_strings(data: dict[str, Any], key: str) -> tuple[str, ...] | None:
    return _strings(data[key]) if key in data else None


def _envelope(data: Any, kind: str, allowed: frozenset[str] | set[str]) -> dict[str, Any]:
    data = _mapping(data, allowed)
    if data.get("apiVersion") != API_VERSION or data.get("kind") != kind:
        raise ValueError("Unsupported metadata version or kind.")
    return data


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _is_guidance_file(path: Path) -> bool:
    name = path.name.casefold()
    return name == "entry.yaml" or name.endswith(".entry.yaml")


class ContentReader:
    """One bounded read of a selected workspace, independent of Site state."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.bytes_read = 0
        self.directory_items = 0
        self.diagnostics: list[BrowseDiagnostic] = []
        self.names_complete = True
        if not self.workspace.is_dir():
            raise BrowseError("workspace.missing", "Workspace directory was not found.")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    @staticmethod
    def _check_components(relative: Path, *, reference: bool) -> None:
        for part in relative.parts:
            if part != part.rstrip(" .") or PureWindowsPath(part).is_reserved():
                raise BrowseError("path.alias", "Ambiguous Windows path components are excluded.")
            if ":" in part:
                raise BrowseError("path.invalid", "Content paths cannot address alternate streams.")
            if part.startswith(".") or (not reference and part.casefold() in _PROTECTED):
                raise BrowseError(
                    "path.protected", "Configuration and working-state paths are excluded."
                )

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
        self._check_components(relative, reference=reference)
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
        self._check_components(canonical_relative, reference=reference)
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
                return None
            raise BrowseError("file.missing", "Content file was not found.", relative) from None
        except OSError:
            raise BrowseError("file.unreadable", "Content file could not be read.", relative) from None
        self.bytes_read += len(content)
        if len(content) > MAX_FILE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
            raise BrowseError("read.limit", "Content exceeds the inspection read budget.", relative)
        try:
            return yamlio.load_bounded(content.decode("utf-8"))
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

    def _source(self, data: dict[str, Any]) -> str | None:
        value = _optional_string(data, "source")
        return self._reference(value, self.workspace) if value is not None else None

    def _guidance(self, document: Any, path: Path) -> EntryGuidance:
        data = _envelope(document, "DeploymentEntry", _GUIDANCE_KEYS)
        role = _string(data.get("role", "unclassified"))
        if role not in {"standalone", "partial", "unclassified"}:
            raise ValueError("Invalid role.")
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
                    source=self._source(row),
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
                    description=_string(row.get("description")),
                    source=self._source(row),
                ))
            if len({(item.step, item.input) for item in parsed_supplied}) != len(parsed_supplied):
                raise ValueError("Duplicate supplied input.")
            supplied = tuple(parsed_supplied)
        return EntryGuidance(
            role=role, category=_optional_string(data, "category"),
            tags=_strings(data.get("tags", [])),
            documentation=tuple(
                self._reference(item, path.parent)
                for item in _strings(data.get("documentation", []))
            ),
            outcome=_optional_string(data, "outcome"), inputs=inputs, supplied=supplied,
            prerequisites=_optional_strings(data, "prerequisites"),
            effects=_optional_strings(data, "effects"),
            removal=_optional_strings(data, "removal"),
            coverage=_optional_string(data, "coverage"),
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
        metadata = (
            path.with_name("entry.yaml")
            if path.name.casefold() in {"manifest.yaml", "manifest.yml"}
            else path.with_suffix(".entry.yaml")
        )
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
        stack = [(self.workspace / name, 0, name) for name in ("samples", "manifests")]
        while stack:
            directory, depth, kind = stack.pop()
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
                        stack.append((path, depth + 1, kind))
                    elif (
                        path.suffix.casefold() in {".yaml", ".yml"}
                        and (
                            kind == "manifests"
                            or path.name.casefold() in {"manifest.yaml", "manifest.yml"}
                            or path.name.startswith("_")
                        )
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
    if limit is not None and limit < 1:
        raise ValueError("The result limit must be positive.")
    if selection is not None and (search is not None or tags or category is not None or limit):
        raise ValueError("A selected entry cannot be combined with inventory filters.")
    reader = ContentReader(workspace)
    if selection is not None and (
        "/" in selection or "\\" in selection or PureWindowsPath(selection).drive
        or selection.casefold().endswith((".yaml", ".yml"))
    ):
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
    visible = tuple(
        entry for entry in inventory if include_partials or entry.guidance.role != "partial"
    )
    counts = Counter(entry.name for entry in visible)
    visible = tuple(replace(
        entry,
        name_ambiguous=counts[entry.name] > 1 if reader.names_complete else None,
    ) for entry in visible)
    if selection is not None:
        matches = tuple(entry for entry in visible if entry.name == selection)
        if not reader.names_complete:
            reader.diagnostics.append(BrowseDiagnostic(
                "lookup.incomplete", "Name lookup requires a complete inventory. Use an explicit path."
            ))
            matches = ()
        elif not matches:
            reader.diagnostics.append(BrowseDiagnostic(
                "lookup.missing", "Entry name was not found. Browse the inventory or use a path."
            ))
        elif len(matches) > 1:
            reader.diagnostics.append(BrowseDiagnostic(
                "lookup.ambiguous", "Entry name is ambiguous. Select one of the explicit paths."
            ))
        return BrowseResult(
            str(reader.workspace), matches, tuple(reader.diagnostics),
            len(matches) == 1, len(inventory), len(matches),
            name_inventory_complete=reader.names_complete,
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
    return BrowseResult(
        str(reader.workspace), matches[:limit], tuple(reader.diagnostics),
        False, len(inventory), len(matches),
        name_inventory_complete=reader.names_complete,
    )

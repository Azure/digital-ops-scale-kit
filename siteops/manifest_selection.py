# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Source-independent matching of manifest names and explicit paths."""

from collections.abc import Iterable
from pathlib import PurePath, PureWindowsPath


class ManifestSelectionError(ValueError):
    """An unresolved selection with a safe summary and private canonical path choices."""

    def __init__(self, code: str, summary: str, paths: tuple[str, ...] = ()):
        super().__init__(summary)
        self.code = code
        self.paths = paths


def is_explicit_manifest_path(value: str | PurePath) -> bool:
    """Return whether a value is a path object or uses explicit path syntax."""
    return isinstance(value, PurePath) or any(part in value for part in ("/", "\\")) or bool(
        PureWindowsPath(value).drive
    )


def explicit_manifest_reference(path: str) -> str:
    """Format a canonical CLI path, prefixing root filenames with `./`."""
    return path if is_explicit_manifest_path(path) else "./" + path


def select_manifest_path(
    selection: str,
    candidates: Iterable[tuple[str, str]],
    *,
    names_complete: bool,
    filename_match: str | None = None,
) -> str:
    """Select one canonical path from exact-name and root-filename matches.

    The source owns candidate visibility and canonical path validation.
    Bare tokens require a complete name inventory. Explicit paths bypass this
    lookup and remain subject to the source's path boundary.
    """
    paths = {path for name, path in candidates if name == selection}
    if filename_match is not None:
        paths.add(filename_match)
    choices = tuple(sorted(paths))
    if not names_complete:
        raise ManifestSelectionError(
            "lookup.incomplete",
            "Name lookup requires a complete inventory. Use an explicit path.",
            choices,
        )
    if not choices:
        raise ManifestSelectionError(
            "lookup.missing",
            "Manifest not found. Browse the inventory or use an explicit path.",
        )
    if len(choices) > 1:
        raise ManifestSelectionError(
            "lookup.ambiguous",
            "Manifest selection is ambiguous. Choose one of the explicit paths.",
            choices,
        )
    return choices[0]

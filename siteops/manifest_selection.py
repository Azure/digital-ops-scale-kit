# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Source-independent matching of manifest names and explicit paths."""

import difflib
from collections.abc import Iterable
from pathlib import PurePath, PureWindowsPath


class ManifestSelectionError(ValueError):
    """An unresolved selection with a safe summary and private canonical path choices.

    `private_message` adds the nearest manifest name for output that is not
    redacted.
    """

    private_message: str | None = None

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


def nearest_manifest(selection: str, candidates: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
    """Return the closest manifest name and its canonical path, if any is close."""
    paths: dict[str, str] = {}
    for name, path in candidates:
        paths.setdefault(name, path)
    nearest = difflib.get_close_matches(selection, sorted(paths), n=1)
    return (nearest[0], paths[nearest[0]]) if nearest else None


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
    candidates = tuple(candidates)
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
        error = ManifestSelectionError(
            "lookup.missing",
            "Manifest not found. Run `siteops browse` to list manifests, or use an explicit path.",
        )
        nearest = nearest_manifest(selection, candidates)
        if nearest:
            error.private_message = (
                f"Manifest not found. Did you mean '{nearest[0]}'? Run `siteops browse` to "
                "list manifests, or use an explicit path."
            )
        raise error
    if len(choices) > 1:
        raise ManifestSelectionError(
            "lookup.ambiguous",
            "Manifest selection is ambiguous. Choose one of the explicit paths.",
            choices,
        )
    return choices[0]

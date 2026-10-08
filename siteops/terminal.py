# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Plain terminal presentation shared by the human renderers.

Every plain renderer uses one ASCII marker set, escapes control and format
characters in authored or Site text, and wraps prose at one resolved width.
No color or cursor control is emitted. Honor `NO_COLOR` and `TERM=dumb`
before adding either.
"""

from __future__ import annotations

import shutil
import sys
import textwrap
import unicodedata
from typing import TextIO

SUCCEEDED = "+"
FAILED = "x"
NOT_RUN = "-"
UNKNOWN = "?"
WARNING = "!"
BULLET = "-"

REDIRECTED_WIDTH = 72
MAX_WIDTH = 100
_MIN_WIDTH = 40


def sanitize(value: str) -> str:
    """Show control, format and unassigned characters as visible `\\uXXXX` text.

    Applying it again leaves the result unchanged, so a renderer can sanitize
    a field and its finished line without double escaping.
    """
    return "".join(
        f"\\u{ord(character):04x}"
        if unicodedata.category(character).startswith("C")
        else character
        for character in value
    )


def sanitize_lines(value: str) -> list[str]:
    """Split multiline text at line feeds and sanitize each line."""
    return [sanitize(line.removesuffix("\r")) for line in value.split("\n")]


def line_width(stream: TextIO | None = None) -> int:
    """Return the wrap width for a stream.

    A terminal uses its own width, capped at `MAX_WIDTH`. Redirected output
    uses `REDIRECTED_WIDTH`, so a file or CI log renders the same each time.
    """
    stream = sys.stdout if stream is None else stream
    try:
        interactive = stream.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    if not interactive:
        return REDIRECTED_WIDTH
    columns = shutil.get_terminal_size((REDIRECTED_WIDTH, 24)).columns
    return max(_MIN_WIDTH, min(columns, MAX_WIDTH))


def wrap(
    text: str,
    *,
    indent: str = "  ",
    hanging: str | None = None,
    width: int | None = None,
) -> list[str]:
    """Wrap prose without splitting identifiers, paths or hyphenated names."""
    return textwrap.wrap(
        text,
        width=line_width() if width is None else width,
        initial_indent=indent,
        subsequent_indent=hanging if hanging is not None else indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [f"{indent}{text}"]


def heading(title: str, *, blank_after: bool = True) -> list[str]:
    """Return an underlined section title preceded by a blank line."""
    lines = ["", f"  {title}", f"  {'-' * len(title)}"]
    if blank_after:
        lines.append("")
    return lines

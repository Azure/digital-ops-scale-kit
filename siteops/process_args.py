# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Literal process arguments for native executables and Windows batch launchers."""

from __future__ import annotations

import ctypes
import os
import subprocess
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

_WINDOWS = os.name == "nt"
_BATCH_SUFFIXES = {".bat", ".cmd"}
_MAX_BATCH_COMMAND_UNITS = 8191


@lru_cache(maxsize=1)
def _system_command_interpreter() -> str:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_directory = kernel.GetSystemDirectoryW
    get_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_directory(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError("The Windows system command interpreter could not be located.")
    return str(Path(buffer.value) / "cmd.exe")


def _quote_batch_argument(argument: str) -> str:
    if any(
        character in '%"'
        or ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in argument
    ):
        raise OSError(
            "Windows batch launchers require arguments without percent signs, "
            "double quotes or control characters. Use a native executable or "
            "change the selected path or input."
        )
    rendered = subprocess.list2cmdline([argument])
    if rendered.startswith('"'):
        return rendered
    # cmd also needs quotes around metacharacters without whitespace.
    trailing_slashes = len(argument) - len(argument.rstrip("\\"))
    return '"' + rendered + "\\" * trailing_slashes + '"'


def prepare_process_args(argv: Sequence[str]) -> Sequence[str] | str:
    """Preserve native argument vectors and prepare literal Windows batch input.

    Batch invocation disables AutoRun and delayed expansion. Text that cannot
    be preserved safely fails before process creation, without echoing values.
    """
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("A process command requires a nonempty argument vector.")
    if not _WINDOWS or Path(argv[0].rstrip(" .")).suffix.casefold() not in _BATCH_SUFFIXES:
        return argv
    body = " ".join(_quote_batch_argument(argument) for argument in argv)
    interpreter = subprocess.list2cmdline([_system_command_interpreter()])
    command = f'{interpreter} /d /v:off /s /c "{body}"'
    if len(command.encode("utf-16-le")) // 2 > _MAX_BATCH_COMMAND_UNITS:
        raise OSError("The Windows batch command exceeds its supported length.")
    return command

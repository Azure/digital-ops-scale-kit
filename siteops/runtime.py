# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Private transient allocations outside deployment content.

`SITEOPS_TEMP_DIR` selects their parent, otherwise the system temporary
directory is used. Allocation is lazy and each consumer cleans only the
files it owns. This module does not define persistent project state.

Directories and files use modes 0700 and 0600 at creation on POSIX.
Windows access follows inherited ACLs, so a selected root must already
provide suitable access controls. Existing parent permissions are unchanged.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Explicit operator selection. Set to an absolute directory path.
TEMP_DIR_ENV = "SITEOPS_TEMP_DIR"

# Owner-only modes applied at creation time by the tempfile primitives used
# below (`mkdtemp` creates 0o700, `mkstemp` creates 0o600).
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# Whether the POSIX mode above actually restricts access. False on Windows,
# where access is governed by ACLs and the mode is advisory only.
POSIX_MODE_ENFORCED = os.name != "nt"


class RuntimePathError(ValueError):
    """A selected runtime path cannot be used as an engine-owned root."""


def bounded_runtime_path(path: Path | str) -> str:
    """Name a private path for a diagnostic without publishing its prefix.

    A temp path carries the account name on Windows
    (`C:\\Users\\<account>\\AppData\\Local\\Temp\\...`) and the home directory
    elsewhere. A cleanup diagnostic needs to identify which allocation failed,
    not where the machine keeps its temp directory, so only the leaf and its
    immediate parent are rendered.
    """
    candidate = Path(path)
    parent_name = candidate.parent.name
    if parent_name:
        return f"...{os.sep}{parent_name}{os.sep}{candidate.name}"
    return candidate.name


def describe_os_error(error: OSError) -> str:
    """Render an OSError without echoing the full path it carries.

    `str(OSError)` appends the filename when the error has one, which is
    exactly the private path a diagnostic is trying not to publish. A
    system-raised error always carries `strerror` ("Directory not empty"),
    which is the useful half. An error raised with only a message has no
    filename to leak, so its message is kept rather than reduced to a class
    name an operator cannot act on.
    """
    if error.strerror:
        return error.strerror
    if error.filename is None and error.filename2 is None:
        message = str(error).strip()
        if message:
            return message
    return error.__class__.__name__


def _selected_root(environment: Mapping[str, str], name: str) -> Path | None:
    """Read an explicitly selected root, or None when the variable is unset.

    Absence is normal and falls through to the platform default. A variable
    that is present but unusable fails, so a misconfigured redirect is not
    silently ignored and transient files do not land somewhere unintended.
    """
    raw = environment.get(name)
    if raw is None:
        return None

    value = raw.strip()
    if not value:
        raise RuntimePathError(
            f"{name} is set but empty. Unset it to use the default location, "
            f"or set it to an absolute directory path."
        )

    try:
        candidate = Path(value).expanduser()
    except RuntimeError as error:
        raise RuntimePathError(
            f"{name} could not be expanded: {error}"
        ) from error

    if not candidate.is_absolute():
        raise RuntimePathError(
            f"{name} must be an absolute directory path, got {value!r}."
        )
    return candidate


def default_temp_root() -> Path:
    """System temporary directory.

    `tempfile.gettempdir()` applies the platform's own search order
    (`TMPDIR`, `TEMP`, `TMP`, then a platform default) and caches its answer
    for the process, which is why a test that needs a specific parent should
    set `SITEOPS_TEMP_DIR` rather than a platform variable.
    """
    return Path(tempfile.gettempdir())


@dataclass(frozen=True)
class RuntimePaths:
    """Parent for temporary allocations, resolved without creating directories."""

    temp_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.temp_root, Path):
            raise TypeError("temp_root must be a Path.")
        if not self.temp_root.is_absolute():
            raise RuntimePathError("temp_root must be an absolute directory path.")

    @classmethod
    def resolve(cls, environment: Mapping[str, str] | None = None) -> RuntimePaths:
        """Select the temporary parent from the environment or system default.

        Creates nothing. Raises `RuntimePathError` when a Site Ops variable is
        set to an empty or relative path.
        """
        env = os.environ if environment is None else environment
        return cls(
            temp_root=_selected_root(env, TEMP_DIR_ENV) or default_temp_root(),
        )


def prepare_root(root: Path) -> Path:
    """Ensure an engine-owned root exists, leaving its permissions alone.

    An operator may point `SITEOPS_TEMP_DIR` at a directory that does not
    exist yet, so a missing root is created. An existing root is untouched:
    its mode is the operator's decision, and the engine only ever creates
    private children inside it.

    Raises:
        RuntimePathError: The root could not be created.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimePathError(
            f"Runtime directory {bounded_runtime_path(root)} could not be "
            f"prepared: {describe_os_error(error)}"
        ) from error
    return root


def create_private_directory(parent: Path, *, prefix: str) -> Path:
    """Allocate a uniquely named, owner-only directory under `parent`.

    `mkdtemp` supplies both properties that matter: the name is unique against
    concurrent allocators, and the directory exists with mode `0o700` from
    creation. The parent is created when missing but an existing parent's
    permissions are left exactly as the operator set them.

    Returns:
        The created directory. The caller owns it and is responsible for
        removing it.

    Raises:
        RuntimePathError: The parent could not be prepared or the directory
            could not be created.
    """
    prepare_root(parent)

    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    except OSError as error:
        raise RuntimePathError(
            f"Engine scratch could not be created under "
            f"{bounded_runtime_path(parent)}: {describe_os_error(error)}"
        ) from error


def create_private_file(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
) -> tuple[int, Path]:
    """Create a uniquely named, owner-only file in an engine-owned directory.

    `mkstemp` creates with `O_EXCL` and mode `0o600`, so the name cannot
    collide with a concurrent allocation and the content is never briefly
    world-readable. It also means the file name carries no authored site or
    step name, which keeps operator-controlled text out of a file path.

    Returns:
        The open file descriptor and its path. The caller owns both.
    """
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    return handle, Path(name)

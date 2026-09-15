# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Private cache directories and process-held leases on local filesystems."""

from __future__ import annotations

import ctypes
import errno
import math
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from siteops.artifacts import ArtifactError, check_portable_component, require_node


class CacheError(ArtifactError):
    def __init__(self, message: str, *, code: str = "cache.invalid"):
        super().__init__(message, code=code)


def _windows():
    """Load native declarations only on Windows."""
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    void = ctypes.c_void_p
    pointer = ctypes.POINTER
    declarations = (
        (advapi.OpenProcessToken, [w.HANDLE, w.DWORD, pointer(w.HANDLE)], w.BOOL),
        (advapi.GetTokenInformation, [w.HANDLE, ctypes.c_int, void, w.DWORD, pointer(w.DWORD)], w.BOOL),
        (advapi.ConvertSidToStringSidW, [void, pointer(w.LPWSTR)], w.BOOL),
        (advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW,
         [w.LPCWSTR, w.DWORD, pointer(void), pointer(w.DWORD)], w.BOOL),
        (advapi.GetNamedSecurityInfoW,
         [w.LPCWSTR, ctypes.c_int, w.DWORD, pointer(void), pointer(void),
          pointer(void), pointer(void), pointer(void)], w.DWORD),
        (advapi.GetAclInformation, [void, void, w.DWORD, ctypes.c_int], w.BOOL),
        (advapi.GetAce, [void, w.DWORD, pointer(void)], w.BOOL),
        (kernel.GetCurrentProcess, [], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.LocalFree, [void], void),
        (kernel.CreateDirectoryW, [w.LPCWSTR, void], w.BOOL),
        (kernel.LockFileEx, [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, void], w.BOOL),
        (kernel.UnlockFileEx, [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, void], w.BOOL),
    )
    for function, arguments, result in declarations:
        function.argtypes = arguments
        function.restype = result
    return advapi, kernel


def _sid_text(advapi, kernel, sid) -> str:
    from ctypes import wintypes as w

    value = w.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise CacheError("Cache access-control identities could not be read.", code="cache.permissions")
    try:
        return value.value
    finally:
        kernel.LocalFree(value)


def _current_windows_sid(advapi, kernel) -> str:
    from ctypes import wintypes as w

    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise CacheError("The cache owner identity could not be read.", code="cache.permissions")
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise CacheError("The cache owner identity is unsupported.", code="cache.permissions")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise CacheError("The cache owner identity could not be read.", code="cache.permissions")
        return _sid_text(advapi, kernel, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)


def _check_windows_access(path: Path, *, private: bool) -> None:
    from ctypes import wintypes as w

    class AclSize(ctypes.Structure):
        _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]

    class Ace(ctypes.Structure):
        _fields_ = [("kind", w.BYTE), ("flags", w.BYTE), ("size", w.WORD), ("mask", w.DWORD)]

    advapi, kernel = _windows()
    current = _current_windows_sid(advapi, kernel)
    trusted = {current, "S-1-5-18", "S-1-5-32-544"}
    # The system volume can be owned by Windows Modules Installer.
    trusted_owners = trusted | {
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(
        str(path), 1, 0x00000005, ctypes.byref(owner), None, ctypes.byref(acl),
        None, ctypes.byref(descriptor),
    )
    if result:
        raise CacheError("Cache access controls could not be read.", code="cache.permissions")
    try:
        owner_text = _sid_text(advapi, kernel, owner)
        if owner_text not in ({current} if private else trusted_owners) or not acl:
            raise CacheError("Cache paths need trusted ownership and access controls.", code="cache.permissions")
        sizes = AclSize()
        if not advapi.GetAclInformation(acl, ctypes.byref(sizes), ctypes.sizeof(sizes), 2):
            raise CacheError("Cache access controls could not be read.", code="cache.permissions")
        if sizes.count > 4096:
            raise CacheError("Cache access controls are unsupported.", code="cache.permissions")
        for index in range(sizes.count):
            address = ctypes.c_void_p()
            if not advapi.GetAce(acl, index, ctypes.byref(address)):
                raise CacheError("Cache access controls could not be read.", code="cache.permissions")
            ace = ctypes.cast(address, ctypes.POINTER(Ace)).contents
            if ace.kind == 1:  # Deny entries grant no access.
                continue
            if ace.kind != 0 or ace.size < 16:
                raise CacheError("Cache access controls are unsupported.", code="cache.permissions")
            if not private and ace.flags & 0x08:  # Inherit-only does not apply to this ancestor.
                continue
            sid = _sid_text(advapi, kernel, ctypes.c_void_p(address.value + 8))
            # Ancestors must prevent replacement or permission changes by other users.
            forbidden = 0xFFFFFFFF if private else 0x500D0140
            if ace.mask & forbidden and sid not in trusted:
                raise CacheError(
                    "Choose a private cache location whose access controls exclude other users.",
                    code="cache.permissions",
                )
    finally:
        kernel.LocalFree(descriptor)


def check_private_node(path: Path, *, directory: bool) -> None:
    info = require_node(path, directory=directory)
    if os.name == "nt":
        _check_windows_access(path, private=True)
    elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CacheError("Cache paths must be owned by the current user and private.", code="cache.permissions")


def check_cache_ancestors(path: Path) -> None:
    """Check existing ancestors without changing any operator-owned permissions."""
    if not path.is_absolute() or path == path.parent:
        raise CacheError("The cache must be an absolute, non-root directory.", code="cache.path")
    for part in path.parts[1:]:
        check_portable_component(part)
        if part in {".", ".."}:
            raise CacheError("Cache paths cannot contain traversal components.", code="cache.path")
    for parent in reversed(path.parents):
        info = require_node(parent, directory=True)
        if parent.resolve(strict=True) != parent:
            raise CacheError("Cache ancestors cannot use filesystem aliases.", code="cache.path")
        if os.name == "nt":
            _check_windows_access(parent, private=False)
        else:
            trusted_owner = info.st_uid in {0, os.getuid()}
            writable = stat.S_IMODE(info.st_mode) & 0o022
            sticky = info.st_mode & stat.S_ISVTX
            if not trusted_owner or (writable and not sticky):
                raise CacheError("Cache ancestors must prevent changes by other users.", code="cache.permissions")


def make_private_directory(path: Path) -> None:
    """Create one new private directory, including its initial Windows DACL."""
    if os.name != "nt":
        path.mkdir(mode=0o700)
        return
    from ctypes import wintypes as w

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", w.BOOL)]

    advapi, kernel = _windows()
    sid = _current_windows_sid(advapi, kernel)
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None,
    ):
        raise CacheError("Private cache access controls could not be created.", code="cache.permissions")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def cache_lock(path: Path, *, exclusive: bool, timeout: float) -> Iterator[None]:
    """Hold a shared use lease or exclusive maintenance lease until context exit."""
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 300:
        raise CacheError("Cache lock timeout must be between zero and 300 seconds.")
    check_private_node(path.parent, directory=True)
    descriptor = -1
    locked = False
    try:
        try:
            descriptor = os.open(
                path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            check_private_node(path, directory=False)
            descriptor = os.open(
                path, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        check_private_node(path, directory=False)
        opened = os.fstat(descriptor)
        named = require_node(path, directory=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise CacheError("The cache lock changed while it was opened.")
        if os.name == "nt":
            import msvcrt
            from ctypes import wintypes as w

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                    ("offset", w.DWORD), ("offset_high", w.DWORD), ("event", w.HANDLE),
                ]

            _, kernel = _windows()
            handle = msvcrt.get_osfhandle(descriptor)
            overlapped = Overlapped()

            def attempt() -> bool:
                if kernel.LockFileEx(
                    handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped),
                ):
                    return True
                error = ctypes.get_last_error()
                if error != 33:
                    raise ctypes.WinError(error)
                return False

            def release() -> None:
                if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            import fcntl

            def attempt() -> bool:
                try:
                    fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    return True
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    return False

            def release() -> None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while not attempt():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CacheError("The package is in use. Retry after the active operation.", code="cache.busy")
            time.sleep(min(0.05, remaining))
        locked = True
        yield
    finally:
        try:
            if locked:
                release()
        finally:
            if descriptor >= 0:
                os.close(descriptor)

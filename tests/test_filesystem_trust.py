# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared filesystem trust rules for caches and verifier executables."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from siteops import cache_filesystem, github_attestation, github_source
from siteops.browse import BrowseError
from siteops.cache_filesystem import CacheError, check_trusted_executable
from siteops.github_attestation import VerificationError

posix = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes.")
_PRIVATE_GROUP = cache_filesystem._private_group


@pytest.fixture(autouse=True)
def _fresh_group_lookup():
    _PRIVATE_GROUP.cache_clear()
    yield
    _PRIVATE_GROUP.cache_clear()


def _info(mode: int, *, uid: int, gid: int) -> os.stat_result:
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


def _accounts(monkeypatch, *, group_name: str, members: list[str], other_primary: bool = False):
    import grp
    import pwd

    uid, gid = os.getuid(), os.getegid()
    user = SimpleNamespace(pw_name="operator", pw_uid=uid, pw_gid=gid)
    accounts = [user] + ([SimpleNamespace(pw_name="other", pw_uid=uid + 1, pw_gid=gid)] if other_primary else [])
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: user)
    monkeypatch.setattr(pwd, "getpwall", lambda: accounts)
    monkeypatch.setattr(grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name=group_name, gr_gid=gid, gr_mem=members))
    return uid, gid


@posix
@pytest.mark.parametrize(
    ("group_name", "members", "other_primary", "private"),
    [
        ("operator", [], False, True),
        ("operator", ["operator"], False, True),
        ("staff", [], False, False),
        ("operator", ["operator", "other"], False, False),
        ("operator", [], True, False),
    ],
)
def test_private_group_requires_the_users_own_group_without_other_members(
    monkeypatch, group_name, members, other_primary, private,
):
    _, gid = _accounts(monkeypatch, group_name=group_name, members=members, other_primary=other_primary)
    assert cache_filesystem._private_group() == (gid if private else None)


@posix
def test_private_group_lookup_failure_refuses(monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: (_ for _ in ()).throw(KeyError(_uid)))
    assert cache_filesystem._private_group() is None


@posix
@pytest.mark.parametrize(
    ("mode", "owner", "group", "private", "trusted"),
    [
        (0o755, "user", "own", False, True),
        (0o775, "user", "own", True, True),
        (0o775, "user", "own", False, False),
        (0o775, "user", "other", True, False),
        (0o775, "root", "own", True, False),
        (0o757, "user", "own", True, False),
        (0o755, "stranger", "own", True, False),
    ],
)
def test_writers_trusted_accepts_group_write_only_from_the_private_group(
    monkeypatch, mode, owner, group, private, trusted,
):
    uid, gid = os.getuid(), os.getegid()
    monkeypatch.setattr(cache_filesystem, "_private_group", lambda: gid if private else None)
    owners = {"user": uid, "root": 0, "stranger": uid + 1}
    info = _info(mode, uid=owners[owner], gid=gid if group == "own" else gid + 1)
    assert cache_filesystem._writers_trusted(info) is trusted


@posix
def test_trusted_executable_rejects_files_other_users_can_change(tmp_path):
    tmp_path.chmod(0o700)
    tool = tmp_path / "gh"
    tool.write_bytes(b"tool")
    tool.chmod(0o755)
    check_trusted_executable(tool)
    tool.chmod(0o757)
    with pytest.raises(CacheError):
        check_trusted_executable(tool)


@posix
def test_trusted_executable_rejects_an_open_parent(tmp_path):
    tmp_path.chmod(0o700)
    folder = tmp_path / "bin"
    folder.mkdir()
    folder.chmod(0o777)
    tool = folder / "gh"
    tool.write_bytes(b"tool")
    tool.chmod(0o755)
    with pytest.raises(CacheError):
        check_trusted_executable(tool)


def _untrusted(_path: Path) -> None:
    raise CacheError("The executable must be protected from other users.", code="cache.permissions")


def test_runtime_verifier_refuses_an_untrusted_github_cli(monkeypatch, tmp_path):
    tool = tmp_path / ("gh.exe" if os.name == "nt" else "gh")
    tool.write_bytes(b"tool")
    monkeypatch.setattr(github_attestation, "resolve_tool_from_path", lambda _name: str(tool))
    monkeypatch.setattr(github_attestation, "check_trusted_executable", _untrusted)
    with pytest.raises(VerificationError, match="protected from other users"):
        github_attestation._resolve_verifier()


def test_cli_transport_refuses_an_untrusted_github_cli(monkeypatch, tmp_path):
    tool = tmp_path / ("gh.exe" if os.name == "nt" else "gh")
    tool.write_bytes(b"tool")
    monkeypatch.setattr(github_source, "resolve_tool_from_path", lambda _name: str(tool))
    monkeypatch.setattr(github_source, "check_trusted_executable", _untrusted)
    with pytest.raises(BrowseError) as error:
        github_source._resolve_gh()
    assert error.value.diagnostic.code == "github.tool-untrusted"

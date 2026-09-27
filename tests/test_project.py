"""Portable project pins, atomic updates and independent content/configuration selection."""

import hashlib
import json
import shutil
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from siteops import cli, command_context
from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import check_private_node
from siteops.project import (
    PIN_NAME,
    ProjectError,
    WorkspacePin,
    project_root,
    read_pin,
    require_separate_cache,
    write_pin,
)
from siteops.workspace_cache import WorkspaceCache
from tests.workspace_acquisition_helpers import make_source


@pytest.fixture
def pinned(tmp_path):
    fixture = make_source(tmp_path)
    root = project_root(tmp_path / "project", create=True)
    pin = WorkspacePin(fixture.source)
    snapshot = write_pin(root, pin, expected_previous=None)
    return root, pin, snapshot


def test_pin_is_portable_provider_neutral_and_contains_only_source_expectations(pinned):
    root, pin, snapshot = pinned
    assert read_pin(root) == snapshot
    raw = (root / PIN_NAME).read_bytes()
    assert WorkspacePin.from_bytes(raw) == pin
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert b"example-registry/v1" in raw
    assert b"opaque:revision-7" in raw
    assert str(root).encode() not in raw
    assert set(json.loads(raw)) == {"apiVersion", "kind", "source", "content"}
    assert b"policy" not in raw and b"trustedRoot" not in raw
    check_private_node(root / PIN_NAME, directory=False)
    assert (root / ".siteops" / ".gitignore").read_bytes() == b"*\n"


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(policyFile="from-package.json"),
    lambda row: row.update(trustedRoot="from-package.json"),
    lambda row: row.update(kind="Other"),
    lambda row: row["source"].update(url="https://unapproved"),
    lambda row: row["content"]["package"].update(sha256="bad"),
    lambda row: row["content"].update(workspace="../outside"),
    lambda row: row["content"]["proof"].update(name=row["content"]["package"]["name"]),
])
def test_pin_rejects_authority_fields_and_invalid_selection(pinned, mutate):
    _, pin, _ = pinned
    row = pin.document()
    mutate(row)
    with pytest.raises(ProjectError) as caught:
        WorkspacePin.from_bytes(json.dumps(row).encode())
    assert caught.value.code == "project.pin-invalid"


@pytest.mark.parametrize("raw", [
    b'{"kind":"WorkspacePin","kind":"private"}', b'{"x":NaN}', b"[" * 2000,
    b"x" * (64 * 1024 + 1),
], ids=["duplicate", "number", "depth", "size"])
def test_pin_json_is_bounded_and_strict(raw):
    with pytest.raises(ProjectError):
        WorkspacePin.from_bytes(raw)


def test_pin_replacement_preserves_sites_and_checks_the_previous_identity(pinned):
    root, pin, previous = pinned
    sites = root / "sites"
    sites.mkdir()
    site = sites / "one.yaml"
    site.write_bytes(b"operator configuration")
    next_pin = WorkspacePin(replace(
        pin.selection, entry=replace(pin.selection.entry, kit_version="8"),
    ))
    current = write_pin(root, next_pin, expected_previous=previous.sha256)
    assert read_pin(root) == current
    assert site.read_bytes() == b"operator configuration"
    with pytest.raises(ProjectError) as caught:
        write_pin(root, pin, expected_previous=previous.sha256)
    assert caught.value.code == "project.pin-changed"
    assert read_pin(root) == current


def test_initialization_does_not_overwrite_a_competing_pin(pinned):
    root, pin, previous = pinned
    with pytest.raises(ProjectError, match="changed during acquisition"):
        write_pin(root, pin, expected_previous=None)
    assert read_pin(root) == previous


def test_invalid_existing_file_is_never_overwritten(pinned):
    root, pin, _ = pinned
    path = root / PIN_NAME
    path.write_bytes(b"operator file")
    with pytest.raises(ProjectError):
        write_pin(root, pin, expected_previous=None)
    assert path.read_bytes() == b"operator file"


def test_failed_publication_leaves_the_previous_pin_and_no_temporary_file(pinned, monkeypatch):
    root, pin, previous = pinned
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("replacement failure")))
    with pytest.raises(ProjectError) as caught:
        write_pin(root, pin, expected_previous=previous.sha256)
    assert caught.value.code == "project.io"
    assert read_pin(root) == previous
    assert not list((root / ".siteops").glob(".pin-*.tmp"))


def test_pin_reader_rejects_filesystem_aliases(pinned, tmp_path):
    root, _, _ = pinned
    other = tmp_path / "other.pin"
    (root / PIN_NAME).rename(other)
    try:
        (root / PIN_NAME).symlink_to(other)
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    with pytest.raises(ArtifactError):
        read_pin(root)


@pytest.mark.parametrize("placement", ["equal", "project-inside-cache", "cache-inside-project"])
def test_project_and_cache_must_be_disjoint(tmp_path, placement):
    project = tmp_path / "project"
    cache = {
        "equal": project, "project-inside-cache": tmp_path, "cache-inside-project": project / "cache",
    }[placement]
    with pytest.raises(ProjectError, match="separate directory trees"):
        require_separate_cache(project, cache)
    require_separate_cache(project, tmp_path / "other-cache")


def context(**changes):
    return command_context.open_command_context(**{
        "workspace": None, "project": None, "command": "plan", "policy": None,
        "trusted_root": None, "offline": False, "discover": lambda _: None,
        **changes,
    })


def test_project_argument_is_a_path_and_does_not_create_missing_directories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectError) as caught:
        with context(project=Path("factory")):
            pytest.fail("Missing project was accepted.")
    assert caught.value.code == "project.missing"
    assert not (tmp_path / "factory").exists()


def test_missing_pin_has_no_source_or_local_workspace_fallback(tmp_path, monkeypatch):
    root = project_root(tmp_path / "project", create=True)
    discover = Mock(side_effect=AssertionError("Implicit local fallback."))
    monkeypatch.setattr(command_context, "WorkspaceCache", Mock(side_effect=AssertionError("Cache access.")))
    with pytest.raises(ProjectError) as caught:
        with context(project=root, discover=discover):
            pytest.fail("Missing pin was accepted.")
    assert caught.value.code == "project.pin-missing"
    discover.assert_not_called()
    command_context.WorkspaceCache.assert_not_called()


def test_local_override_uses_project_sites_without_reading_or_changing_the_pin(pinned, tmp_path, monkeypatch):
    root, _, _ = pinned
    local = tmp_path / "clone"
    local.mkdir()
    (root / PIN_NAME).write_bytes(b"deliberately invalid pin, unused for local override")
    before = (root / PIN_NAME).read_bytes()
    monkeypatch.chdir(tmp_path)
    with context(project=Path("project"), workspace=Path("clone")) as selected:
        assert selected.workspace == local
        assert selected.site_root == root
        assert selected.pin is None and selected.package is None
    assert (root / PIN_NAME).read_bytes() == before


def test_current_directory_pin_discovers_project_even_with_local_override(pinned, tmp_path, monkeypatch):
    root, _, _ = pinned
    local = tmp_path / "clone"
    local.mkdir()
    monkeypatch.chdir(root)
    with context(workspace=Path("..") / "clone") as selected:
        assert selected.project == root
        assert selected.workspace == local and selected.site_root == root


def test_ordinary_local_discovery_still_needs_no_pin(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.chdir(tmp_path)
    with context(discover=lambda _: local) as selected:
        assert selected.workspace == selected.site_root == local
        assert selected.project is None


def test_sites_need_no_pin_or_acquisition(tmp_path, monkeypatch):
    root = project_root(tmp_path / "project", create=True)
    monkeypatch.setattr(command_context, "WorkspaceCache", Mock(side_effect=AssertionError("Cache access.")))
    with context(project=root, command="sites") as selected:
        assert selected.site_root == root and selected.package is None
    command_context.WorkspaceCache.assert_not_called()


def test_missing_trust_options_fail_before_cache_initialization(pinned, monkeypatch):
    root, _, _ = pinned
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(root.parent / "cache"))
    monkeypatch.setattr(command_context, "WorkspaceCache", Mock(side_effect=AssertionError("Cache access.")))
    with pytest.raises(ProjectError, match="--trust-policy"):
        with context(project=root):
            pytest.fail("Pin supplied its own authority.")
    command_context.WorkspaceCache.assert_not_called()


def test_packaged_command_uses_selected_approved_source_not_the_pin_for_trust(
    pinned, tmp_path, monkeypatch,
):
    root, pin, _ = pinned
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(tmp_path / "separate-cache"))
    policy, trusted_root = tmp_path / "policy.json", tmp_path / "trusted-root.jsonl"
    selected = SimpleNamespace(
        reference=pin.selection.source.reference, policy=policy, trusted_root=trusted_root,
    )
    monkeypatch.setattr(command_context, "read_source", lambda name: selected if name == "approved" else None)
    monkeypatch.setattr(command_context, "WorkspaceCache", lambda _: object())
    used = []

    class Acquirer:
        def lease(self, source):
            used.append(source)
            return nullcontext(SimpleNamespace(package_root=tmp_path / "source"))

    def acquire(source, cache, policy_file, root_file):
        assert (policy_file, root_file) == (policy, trusted_root)
        return Acquirer()

    monkeypatch.setattr(command_context, "project_acquirer", acquire)
    with context(project=root, approved_source="approved") as result:
        assert result.workspace == tmp_path / "source" / "workspace"
        assert result.pin == pin and result.package is not None
    assert used == [pin.selection]
    selected.reference = "github:other/content"
    with pytest.raises(ProjectError, match="does not match"):
        with context(project=root, approved_source="approved"):
            pytest.fail("Pin selected its own verification authority.")
    assert used == [pin.selection]


@pytest.mark.parametrize("arguments,phrases", [
    (["--help"], ["--project DIRECTORY", "--workspace PATH", "Overrides project package content",
                  "--trust-policy FILE", "--trusted-root FILE", "project pin ./factory"]),
    (["project", "pin", "--help"], ["--release RELEASE", "--release-workspace PATH",
                                  "without changing Sites", "global --trust-policy FILE",
                                  "--approved-source NAME"]),
    (["project", "show", "--help"], ["without acquiring or verifying package content"]),
    (["browse", "--help"], ["cached index metadata with --source", "cached project package and proof"]),
    (["sites", "--help"], ["operator project or local workspace"]),
])
def test_help_explains_selection_and_release_terminology(monkeypatch, capsys, arguments, phrases):
    monkeypatch.setattr(sys, "argv", ["siteops", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert all(phrase in output for phrase in phrases)


@pytest.mark.parametrize("explicit", [False, True])
def test_index_project_rejection_is_a_diagnostic_not_a_traceback(tmp_path, monkeypatch, capsys, explicit):
    root = tmp_path / "project"
    root.mkdir()
    (root / PIN_NAME).write_bytes(b"presence is sufficient for the index guard")
    monkeypatch.chdir(root)
    options = ["--project", str(root)] if explicit else []
    monkeypatch.setattr(sys, "argv", ["siteops", *options, "index", "--public"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "index.project:" in capsys.readouterr().err


def test_index_explicit_local_workspace_and_public_approval_controls(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / PIN_NAME).write_bytes(b"unused pin")
    local = tmp_path / "local"
    shutil.copytree(Path(__file__).parent / "fixtures" / "browse-workspace", local)
    monkeypatch.chdir(project)
    for suffix, expected in (([], 1), (["--public"], 0)):
        monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(local), "index", *suffix])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        assert stopped.value.code == expected
        output = capsys.readouterr()
        if expected:
            assert "index.approval:" in output.err
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--approved-source", "official", "-w", str(local), "index", "--public",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "index.project:" in capsys.readouterr().err
    assert (local / "siteops-index.json").is_file()
    assert not (project / "siteops-index.json").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_pin_rejects_cache_overlap_before_directory_creation(tmp_path, monkeypatch, capsys, existing):
    cache = WorkspaceCache(tmp_path / "cache")
    target = cache.root / ("staging" if existing else "factory")
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache.root))
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(tmp_path / "policy.json"),
        "--trusted-root", str(tmp_path / "root.json"),
        "project", "pin", str(target), "--source", "github:example/content", "--release", "v1",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "separate directory trees" in capsys.readouterr().err
    assert target.exists() is existing
    assert WorkspaceCache(cache.root).root == cache.root


def test_pin_reports_progress_before_cold_acquisition(tmp_path, monkeypatch, capsys):
    target = tmp_path / "factory"
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(tmp_path / "policy.json"),
        "--trusted-root", str(tmp_path / "root.json"),
        "project", "pin", str(target),
        "--source", "github:example/content", "--release", "v1",
    ])

    def stop_acquisition(*args, **kwargs):
        progress = capsys.readouterr().err
        assert "Resolving and verifying" in progress
        assert "may take time" in progress
        assert str(target) not in progress
        raise ProjectError("Fixture acquisition stopped.")

    with (
        patch("siteops.workspace_cache.default_cache_root", return_value=tmp_path / "cache"),
        patch("siteops.workspace_cache.WorkspaceCache"),
        patch("siteops.github_workspace_acquisition.GitHubWorkspaceAcquirer") as acquirer,
    ):
        acquirer.return_value.acquire.side_effect = stop_acquisition
        with pytest.raises(SystemExit) as stopped:
            cli.main()
    assert stopped.value.code == 1

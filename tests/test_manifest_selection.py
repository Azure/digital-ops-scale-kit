"""Shared manifest matching and local execution selection boundaries."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from siteops import cli
from siteops.manifest_selection import (
    ManifestSelectionError,
    explicit_manifest_reference,
    is_explicit_manifest_path,
    select_manifest_path,
)


@pytest.mark.parametrize("value", ["install", "install.yaml", "install.yml"])
def test_bare_tokens_can_be_manifest_names(value):
    assert not is_explicit_manifest_path(value)
    assert select_manifest_path(
        value, [(value, "manifests/install/manifest.yaml")], names_complete=True,
    ) == "manifests/install/manifest.yaml"


@pytest.mark.parametrize("value", [
    "./install", ".\\install.yaml", "manifests/install.yaml",
    "manifests\\install.yaml", "/workspace/install.yaml",
    "C:\\workspace\\install.yaml", Path("install.yaml"),
])
def test_explicit_path_forms_bypass_name_matching(value):
    assert is_explicit_manifest_path(value)


@pytest.mark.parametrize(("path", "expected"), [
    ("install.yaml", "./install.yaml"),
    ("install", "./install"),
    ("manifests/install.yaml", "manifests/install.yaml"),
])
def test_path_suggestions_preserve_explicit_selection(path, expected):
    assert explicit_manifest_reference(path) == expected


def test_name_and_filename_cannot_select_different_files():
    with pytest.raises(ManifestSelectionError) as failed:
        select_manifest_path(
            "install.yaml", [("install.yaml", "manifests/install/manifest.yaml")],
            filename_match="install.yaml", names_complete=True,
        )
    assert failed.value.code == "lookup.ambiguous"
    assert failed.value.paths == ("install.yaml", "manifests/install/manifest.yaml")


def test_identical_name_and_path_matches_are_one_choice():
    assert select_manifest_path(
        "install.yaml", [("install.yaml", "install.yaml"), ("install.yaml", "install.yaml")],
        filename_match="install.yaml", names_complete=True,
    ) == "install.yaml"


def test_filename_alone_still_requires_complete_name_discovery():
    with pytest.raises(ManifestSelectionError) as failed:
        select_manifest_path(
            "install.yaml", [], filename_match="install.yaml", names_complete=False,
        )
    assert failed.value.code == "lookup.incomplete"
    assert failed.value.paths == ("install.yaml",)


@pytest.mark.parametrize("selection", ["Install", "inst", "missing"])
def test_names_are_exact_without_fuzzy_fallback(selection):
    with pytest.raises(ManifestSelectionError) as failed:
        select_manifest_path(
            selection, [("install", "manifests/install.yaml")], names_complete=True,
        )
    assert failed.value.code == "lookup.missing"


def test_local_name_lookup_reads_no_site_or_parameter_values(complete_workspace, monkeypatch):
    original = Path.open
    opened = []

    def content_only(path, *args, **kwargs):
        relative = path.relative_to(complete_workspace)
        assert relative.parts[0] not in {"sites", "sites.local", "parameters"}
        opened.append(relative.as_posix())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", content_only)
    assert cli.resolve_manifest_path("test-manifest", complete_workspace) == (
        complete_workspace / "manifests" / "test-manifest.yaml"
    )
    assert opened == ["manifests/test-manifest.yaml"]


def test_bad_guidance_does_not_become_an_execution_admission_rule(complete_workspace):
    sidecar = complete_workspace / "manifests" / "test-manifest.entry.yaml"
    sidecar.write_text("private_guidance: [\n", encoding="utf-8")
    assert cli.resolve_manifest_path("test-manifest", complete_workspace) == (
        complete_workspace / "manifests" / "test-manifest.yaml"
    )


@pytest.mark.parametrize("selection", ["./local", ".\\local", "manifests/test-manifest.yaml"])
def test_explicit_execution_paths_do_not_inventory_content(complete_workspace, monkeypatch, selection):
    monkeypatch.setattr(cli, "ContentReader", Mock(side_effect=AssertionError("Unexpected inventory")))
    assert cli.resolve_manifest_path(selection, complete_workspace) == (
        complete_workspace / selection.replace("\\", "/")
    )


def test_ambiguous_selection_does_not_prepare_or_expose_paths_when_redacted(
    complete_workspace, monkeypatch, capsys,
):
    from argparse import Namespace

    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(cli, "resolve_manifest_path", Mock(side_effect=ManifestSelectionError(
        "lookup.ambiguous", "Manifest selection is ambiguous.", ("PRIVATE_PATH/manifest.yaml",),
    )))
    orchestrator = Mock()
    args = Namespace(manifest="PRIVATE_NAME", workspace=complete_workspace)
    assert cli.cmd_plan(args, orchestrator) == 1
    orchestrator.build_plan.assert_not_called()
    output = capsys.readouterr()
    assert "ambiguous" in output.err
    assert "PRIVATE_" not in output.out + output.err


def test_selection_paths_escape_terminal_controls(complete_workspace, monkeypatch, capsys):
    from argparse import Namespace

    monkeypatch.setattr(cli, "resolve_manifest_path", Mock(side_effect=ManifestSelectionError(
        "lookup.ambiguous", "Manifest selection is ambiguous.", ("name\x1b[2J.yaml",),
    )))
    assert cli.cmd_plan(Namespace(manifest="name", workspace=complete_workspace), Mock()) == 1
    output = capsys.readouterr()
    assert "\x1b" not in output.err
    assert "./name\\u001b[2J.yaml" in output.err

"""Site inspection formats share resolved values and explicit privacy rules."""

import json
import math
import sys
from argparse import Namespace
from datetime import date
from unittest.mock import MagicMock

import pytest
import yaml

from siteops.cli import cmd_sites, main
from siteops.orchestrator import Orchestrator


@pytest.fixture
def workspace(tmp_workspace, monkeypatch):
    monkeypatch.setattr(
        "subprocess.Popen",
        MagicMock(side_effect=AssertionError("Site inspection must not start tools")),
    )
    (tmp_workspace / "sites" / "base.yaml").write_text(
        "apiVersion: siteops/v1\nkind: SiteTemplate\n"
        "subscription: synthetic-subscription\nlocation: eastus\n"
        "parameters:\n  inherited: base\n  password: hidden-from-display\n",
        encoding="utf-8",
    )
    (tmp_workspace / "sites" / "z-site.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: z-site\ninherits: base.yaml\n"
        "resourceGroup: synthetic-group\nlabels:\n  environment: dev\n"
        "parameters:\n  selected: authored\n"
        "properties:\n  enabled: false\n  count: 0\n  unset: null\n"
        "  nested:\n    - name: item\n      clientSecret: hidden-nested-value\n",
        encoding="utf-8",
    )
    (tmp_workspace / "sites" / "a-site.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: a-site\ninherits: base.yaml\n",
        encoding="utf-8",
    )
    (tmp_workspace / "sites.local").mkdir()
    (tmp_workspace / "sites.local" / "z-site.yaml").write_text(
        "parameters:\n  selected: overlay\n",
        encoding="utf-8",
    )
    return tmp_workspace


def _args(output="plain", **overrides):
    return Namespace(**{
        "name": None, "selector": None, "show_sources": False,
        "verbose": False, "output": output, **overrides,
    })


def _documents(text, output):
    return json.loads(text) if output == "json" else list(yaml.safe_load_all(text))


@pytest.mark.parametrize("output", ["yaml", "json"])
def test_formats_use_the_same_sorted_resolved_sites(workspace, capsys, output):
    orchestrator = Orchestrator(workspace)
    assert cmd_sites(_args(output), orchestrator) == 0

    captured = capsys.readouterr()
    documents = _documents(captured.out, output)
    assert captured.err == ""
    assert [site["name"] for site in documents] == ["a-site", "z-site"]
    assert all(site["apiVersion"] == "siteops/v1" for site in documents)
    assert all(site["kind"] == "Site" for site in documents)
    assert all("inherits" not in site for site in documents)
    assert "resourceGroup" not in documents[0]
    assert documents[1]["resourceGroup"] == "synthetic-group"
    assert documents[1]["parameters"] == {
        "inherited": "base", "password": "***", "selected": "overlay",
    }
    assert documents[1]["properties"] == {
        "enabled": False, "count": 0, "unset": None,
        "nested": [{"name": "item", "clientSecret": "***"}],
    }
    assert "hidden" not in captured.out
    assert "Available Sites" not in captured.out
    assert orchestrator.load_site("z-site").parameters["password"] == "hidden-from-display"


@pytest.mark.parametrize("output", ["yaml", "json"])
@pytest.mark.parametrize("target", [{"name": "z-site"}, {"selector": "environment=dev"}])
def test_structured_output_keeps_single_site_targeting(workspace, capsys, output, target):
    assert cmd_sites(_args(output, **target), Orchestrator(workspace)) == 0
    documents = _documents(capsys.readouterr().out, output)
    assert len(documents) == 1
    assert documents[0]["name"] == "z-site"


@pytest.mark.parametrize("output", ["yaml", "json"])
def test_empty_workspace_has_no_success_prose_in_structured_output(
    tmp_workspace, capsys, output,
):
    assert cmd_sites(_args(output), Orchestrator(tmp_workspace)) == 0
    captured = capsys.readouterr()
    assert _documents(captured.out, output) == []
    assert captured.err == ""
    if output == "json":
        assert captured.out == "[]\n"
    else:
        assert captured.out == ""


@pytest.mark.parametrize("output", ["yaml", "json"])
@pytest.mark.parametrize("failure", ["no-match", "rejected-site"])
def test_incomplete_selection_emits_no_partial_document(
    workspace, capsys, output, failure,
):
    args = _args(output)
    if failure == "no-match":
        args.selector = "environment=absent"
    else:
        (workspace / "sites" / "broken.yaml").write_text(
            "name: broken\ninherits: absent.yaml\n", encoding="utf-8",
        )
    assert cmd_sites(args, Orchestrator(workspace)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


@pytest.mark.parametrize("output", ["yaml", "json"])
def test_source_annotations_require_plain_output(workspace, capsys, monkeypatch, output):
    orchestrator = Orchestrator(workspace)
    load = MagicMock(side_effect=AssertionError("Reject flags before loading"))
    monkeypatch.setattr(orchestrator, "load_all_sites", load)

    assert cmd_sites(_args(output, show_sources=True), orchestrator) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--show-sources requires --output plain" in captured.err
    load.assert_not_called()


@pytest.mark.parametrize("output", ["plain", "yaml", "json"])
@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_site_details_are_unavailable_under_ci_redaction(
    workspace, capsys, monkeypatch, output, marker,
):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "")
    monkeypatch.setenv(marker, "true")
    assert cmd_sites(_args(output), Orchestrator(workspace)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Site inspection output is private" in captured.err
    assert "synthetic-subscription" not in captured.err
    assert "z-site" not in captured.err


def test_explicit_private_automation_keeps_json_output(workspace, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    assert cmd_sites(_args("json", name="z-site"), Orchestrator(workspace)) == 0
    assert _documents(capsys.readouterr().out, "json")[0]["name"] == "z-site"


@pytest.mark.parametrize("output", ["plain", "yaml", "json"])
def test_parser_routes_explicit_formats(workspace, monkeypatch, capsys, output):
    monkeypatch.setattr(sys, "argv", [
        "siteops", "-w", str(workspace), "sites", "z-site", "--output", output,
    ])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    if output == "plain":
        assert "Available Sites" in captured.out
    else:
        assert _documents(captured.out, output)[0]["name"] == "z-site"


def test_render_is_replaced_not_retained_as_an_alias(workspace, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "siteops", "-w", str(workspace), "sites", "--render",
    ])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --render" in captured.err


@pytest.mark.parametrize("value", [date(2026, 9, 10), float("nan"), {1: "numeric-key"}])
def test_json_rejects_values_it_cannot_preserve(workspace, capsys, value):
    path = workspace / "sites.local" / "z-site.yaml"
    path.write_text(yaml.safe_dump({"parameters": {"value": value}}), encoding="utf-8")
    assert cmd_sites(_args("json", name="z-site"), Orchestrator(workspace)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot be represented as JSON" in captured.err
    assert "--output yaml" in captured.err

    assert cmd_sites(_args("yaml", name="z-site"), Orchestrator(workspace)) == 0
    preserved = _documents(capsys.readouterr().out, "yaml")[0]["parameters"]["value"]
    if isinstance(value, float) and math.isnan(value):
        assert math.isnan(preserved)
    else:
        assert preserved == value

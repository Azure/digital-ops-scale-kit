"""Pinned remote metadata browsing without remote workspace execution."""

import base64
import json
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from siteops import cli, github_source
from siteops.browse_output import render_browse_plain
from siteops.content_index import BINDINGS_NAME, INDEX_NAME, build_content_index
from siteops.github_catalog import github_input_digests, inspect_github

REVISION = "a" * 40
PREFIX = "workspaces/example/"


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    sha: str
    type: str = "blob"
    mode: str = "100644"
    size: int | None = None


class _Client:
    def __init__(self):
        self.reference = SimpleNamespace(web_url="https://github.com/example/kit")
        self.tree = {}
        self.blobs = {}
        self.calls = []

    def put(self, path, content):
        sha = github_input_digests(content)["git-blob-sha1"]
        self.tree[path] = _TreeEntry(path, sha, size=len(content))
        self.blobs[sha] = content

    def resolve_commit(self):
        self.calls.append(("resolve",))
        return REVISION

    def get_tree(self, commit):
        assert commit == REVISION
        self.calls.append(("tree", commit))
        return self.tree

    def read_blob(self, entry, *, max_bytes):
        self.calls.append(("blob", entry.sha))
        value = self.blobs[entry.sha]
        assert len(value) <= max_bytes
        return value


def _publish(workspace, client, prefix=PREFIX, *, git_bindings=True):
    bundle = build_content_index(
        workspace, approve_public=True,
        additional_digests=github_input_digests if git_bindings else None,
    )
    for path in workspace.rglob("*"):
        if path.is_file():
            client.put(prefix + path.relative_to(workspace).as_posix(), path.read_bytes())
    client.put(prefix + INDEX_NAME, bundle.index)
    client.put(prefix + BINDINGS_NAME, bundle.bindings)
    return bundle


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parent / "fixtures" / "browse-workspace", root)
    return root


def test_remote_preview_uses_the_shared_card_and_pinned_guides(workspace):
    client = _Client()
    _publish(workspace, client)
    result = inspect_github("github:example/kit", "storage", client=client)
    assert result.status == "complete" and result.selected
    assert result.workspace == PREFIX.rstrip("/")
    assert result.source.revision == REVISION
    assert result.source.index_status == "current metadata snapshot"
    assert result.entries[0].guidance.documentation == (
        f"https://github.com/example/kit/blob/{REVISION}/{PREFIX}manifests/storage/README.md",
    )
    output = render_browse_plain(result)
    assert "Remote preview only" in output
    assert "No targets declared" not in output
    assert "siteops -w" not in output
    document = result.document()
    assert document["source"]["kind"] == "remote"
    assert document["source"]["verification"] == "not-performed"
    assert document["entries"][0]["targeting"]["known"] is False
    assert len(client.calls) == 4


def test_remote_inventory_keeps_source_context_in_next_steps(workspace):
    client = _Client()
    _publish(workspace, client)
    result = inspect_github("github:example/kit", client=client)
    output = render_browse_plain(result)
    assert "same --source" in output
    assert "Pin --ref" in output
    empty = render_browse_plain(inspect_github("github:example/kit", search="absent", client=client))
    assert "No published entries match" in empty
    assert "custom layout" not in empty


def test_100_remote_entries_need_no_per_entry_fetch(workspace):
    original = workspace / "manifests" / "storage"
    for index in range(99):
        directory = workspace / "samples" / f"example-{index:03}"
        shutil.copytree(original, directory)
        manifest = directory / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["name"] = directory.name
        manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
        metadata = directory / "entry.yaml"
        guide = yaml.safe_load(metadata.read_text(encoding="utf-8"))
        guide["category"] = "sample"
        metadata.write_text(yaml.safe_dump(guide), encoding="utf-8")
    client = _Client()
    _publish(workspace, client)
    result = inspect_github("github:example/kit", client=client)
    assert result.status == "complete"
    assert len(result.entries) == 100
    assert len(client.calls) == 4
    assert len(render_browse_plain(result).splitlines()) < 125
    client.calls.clear()
    selected = inspect_github(
        "github:example/kit", category="sample", search="example-098", client=client
    )
    assert selected.status == "complete" and selected.matched == 1
    assert len(client.calls) == 4


def test_workspace_choice_is_explicit_when_several_indexes_exist(workspace):
    client = _Client()
    _publish(workspace, client)
    _publish(workspace, client, "workspaces/second/")
    result = inspect_github("github:example/kit", client=client)
    assert result.status == "invalid"
    assert {item.path for item in result.diagnostics if item.path} == {
        "workspaces/example", "workspaces/second"
    }
    assert len(client.calls) == 2
    selected = inspect_github("github:example/kit", workspace="workspaces/second", client=client)
    assert selected.status == "complete"
    assert selected.workspace == "workspaces/second"


@pytest.mark.parametrize("change", ["manifest", "guidance", "new-entry", "new-root", "deleted"])
def test_changed_inputs_never_receive_a_fresh_preview(workspace, change):
    client = _Client()
    _publish(workspace, client)
    if change == "manifest":
        client.put(PREFIX + "manifests/storage/manifest.yaml", b"changed")
    elif change == "guidance":
        client.put(PREFIX + "manifests/storage/entry.yaml", b"changed")
    elif change == "new-entry":
        client.put(PREFIX + "samples/new/manifest.yaml", b"new")
    elif change == "new-root":
        client.put(PREFIX + "content.yaml", b"new")
    else:
        del client.tree[PREFIX + "manifests/storage/manifest.yaml"]
    result = inspect_github("github:example/kit", client=client)
    assert result.status == "invalid"
    assert not result.entries
    assert result.diagnostics[0].code == "index.stale"
    assert result.source.revision == REVISION


def test_missing_index_and_missing_bindings_are_actionable(workspace):
    client = _Client()
    result = inspect_github("github:example/kit", client=client)
    assert result.diagnostics[0].code == "index.missing"
    assert "--for-source github" in result.diagnostics[0].summary
    _publish(workspace, client)
    del client.tree[PREFIX + BINDINGS_NAME]
    assert inspect_github("github:example/kit", client=client).diagnostics[0].code == "index.bindings"


def test_missing_adapter_binding_does_not_weaken_the_common_index(workspace):
    client = _Client()
    _publish(workspace, client, git_bindings=False)
    result = inspect_github("github:example/kit", client=client)
    assert result.diagnostics[0].code == "index.binding_missing"


def test_scoped_links_are_rejected_but_unrelated_repo_links_are_not(workspace):
    client = _Client()
    _publish(workspace, client)
    client.tree["unrelated/link"] = _TreeEntry("unrelated/link", "b" * 40, mode="120000", size=1)
    assert inspect_github("github:example/kit", client=client).status == "complete"
    path = PREFIX + "manifests/storage/manifest.yaml"
    client.tree[path] = replace(client.tree[path], mode="120000")
    assert inspect_github("github:example/kit", client=client).diagnostics[0].code == "index.source"


def test_private_projection_or_swapped_bytes_are_not_accepted(workspace):
    client = _Client()
    _publish(workspace, client)
    client.put(PREFIX + INDEX_NAME, json.dumps({
        "apiVersion": "siteops/v1alpha1", "kind": "ContentInspection",
        "projection": "local-private", "entries": [],
    }).encode())
    result = inspect_github("github:example/kit", client=client)
    assert result.status == "invalid" and not result.entries


def test_invalid_options_do_not_contact_the_source():
    client = _Client()
    result = inspect_github("github:example/kit", "storage", search="x", client=client)
    assert result.status == "invalid" and not client.calls
    result = inspect_github("github:example/kit", workspace="C:\\private", client=client)
    assert result.status == "invalid" and not client.calls


def test_remote_path_selection_uses_index_identity_only(workspace):
    client = _Client()
    _publish(workspace, client)
    result = inspect_github(
        "github:example/kit", "manifests\\storage\\manifest.yaml", client=client
    )
    assert result.selected
    assert result.entries[0].name == "storage"
    invalid = inspect_github("github:example/kit", "../outside.yaml", client=client)
    assert invalid.status == "invalid"


def test_actual_cli_and_transport_contract_use_five_pinned_reads(workspace, monkeypatch, capsys):
    fixture = _Client()
    _publish(workspace, fixture)
    routes = []

    def transport(route):
        routes.append(route)
        if route == "/repos/example/kit":
            return {"default_branch": "main"}
        if route == "/repos/example/kit/commits/main":
            return {"sha": REVISION}
        if route == f"/repos/example/kit/git/trees/{REVISION}?recursive=1":
            return {
                "sha": "b" * 40, "truncated": False,
                "tree": [
                    {"path": row.path, "sha": row.sha, "type": row.type, "mode": row.mode, "size": row.size}
                    for row in fixture.tree.values()
                ],
            }
        prefix = "/repos/example/kit/git/blobs/"
        assert route.startswith(prefix)
        sha = route.removeprefix(prefix)
        content = fixture.blobs[sha]
        return {"sha": sha, "encoding": "base64", "size": len(content),
                "content": base64.b64encode(content).decode("ascii")}

    client = github_source.GitHubClient(
        github_source.GitHubReference.parse("github:example/kit"), transport=transport,
    )
    monkeypatch.setattr(github_source, "GitHubClient", lambda *a, **k: client)
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("Loaded Sites"))
    monkeypatch.setattr(cli, "_auto_discover_workspace", lambda *a: pytest.fail("Read local workspace"))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.setattr(sys, "argv", [
        "siteops", "browse", "storage", "--source", "github:example/kit", "--output", "json",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["source"]["version"] == REVISION
    assert document["entries"][0]["name"] == "storage"
    assert len(routes) == 5
    assert all(REVISION in route or "/git/blobs/" in route for route in routes[2:])


def test_remote_cli_redaction_refuses_before_access(monkeypatch, capsys):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(github_source, "GitHubClient", lambda *a, **k: pytest.fail("Contacted source"))
    monkeypatch.setattr(sys, "argv", [
        "siteops", "browse", "--source", "github:private/source", "--output", "json",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1 and not output.out
    assert "private/source" not in output.err

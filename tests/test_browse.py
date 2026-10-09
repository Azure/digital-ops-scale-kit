"""Bounded content inspection, privacy, and configured-Site handoff."""

import builtins
import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from siteops import browse, browse_output, cli, yamlio
from siteops.browse import API_VERSION, BrowseError, BrowseSource, inspect_content
from siteops.browse_output import render_browse_plain, serialize_browse_json
from siteops.compilation import TemplateCompilationSession
from siteops.executor import AzCliExecutor, DeploymentResult


@pytest.fixture(autouse=True)
def _private_output(monkeypatch):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")


def _entry(
    workspace: Path,
    name: str = "example",
    *,
    relative: str | None = None,
    metadata: bool = True,
    guidance: dict | None = None,
    **header,
) -> Path:
    path = workspace / (relative or f"manifests/{name}/manifest.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "apiVersion": "siteops/v1", "kind": "Manifest", "name": name,
        "description": f"Deploy {name}.", "steps": [], **header,
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    if metadata:
        data = {
            "apiVersion": API_VERSION, "kind": "DeploymentEntry",
            "role": "standalone", "category": "operation", "tags": ["example"],
            "documentation": ["README.md"], "coverage": "Illustrative route.",
            **(guidance or {}),
        }
        sidecar = (
            path.with_name("entry.yaml")
            if path.name in {"manifest.yaml", "manifest.yml"}
            else path.with_suffix(".entry.yaml")
        )
        sidecar.write_text(yaml.safe_dump(data), encoding="utf-8")
        path.with_name("README.md").write_text("# Example\n", encoding="utf-8")
    return path


def _cli(monkeypatch, capsys, workspace: Path, *args: str):
    monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(workspace), *args])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    return stopped.value.code, output.out, output.err


def test_absent_metadata_is_unclassified_not_empty_requirements(tmp_path):
    _entry(tmp_path, metadata=False)
    result = inspect_content(tmp_path, "example")
    entry = result.entries[0]
    assert result.status == "complete"
    assert entry.metadata_status == "absent"
    assert entry.guidance.role == "unclassified"
    document = result.document()
    assert document["entries"][0]["guidance"]["inputs"] is None
    assert document["entries"][0]["guidance"]["prerequisites"] is None
    assert document["preparation"] == "not-performed"
    assert document["source"]["verification"] == "not-performed"
    assert "does not mean no inputs are required" in render_browse_plain(result)


def test_explicit_empty_guidance_remains_an_author_statement(tmp_path):
    _entry(tmp_path, guidance={"inputs": [], "prerequisites": [], "effects": []})
    result = inspect_content(tmp_path, "example")
    assert result.document()["entries"][0]["guidance"]["inputs"] == []
    assert "not an environment assessment" in render_browse_plain(result)


def test_selected_card_distinguishes_advice_from_input_contract(tmp_path):
    _entry(tmp_path)
    local = inspect_content(tmp_path, "example")
    plain = render_browse_plain(local)
    assert "Authored Site input guidance (descriptive only)" in plain
    assert plain.count("descriptive") == 1
    assert "Typed inputs: none." in plain
    assert "if the entry declares a contract" not in plain
    assert "siteops plan example -l 'name=<site>'" in plain
    assert "siteops inputs" not in plain
    assert local.document()["entries"][0]["typedInputs"] == {
        "status": "none", "count": 0, "answers": [],
    }

    remote = replace(
        local,
        source=BrowseSource(kind="github", reference="github:example/repo"),
    )
    remote_plain = render_browse_plain(remote)
    assert "Pin a release from an approved source" in remote_plain
    assert "Typed inputs are checked when you run `siteops inputs`, `plan` or `deploy`." in remote_plain
    assert "Typed inputs:" not in remote_plain


_CONTRACT = """\
apiVersion: siteops.inputs/v1
kind: SiteInputContract
nameFromResource: cluster
inputs:
  - name: siteName
    type: string
    description: Site name.
    sitePath: name
  - name: subscription
    type: string
    description: Subscription.
    sitePath: subscription
  - name: cluster
    type: azureResourceId
    required: false
    description: Cluster ID.
    resource:
      type: Microsoft.Kubernetes/connectedClusters
      apiVersion: 2024-07-15-preview
    derive:
      subscription: subscription
"""


@pytest.mark.parametrize("shell", ["PowerShell", "POSIX shell"])
def test_declared_contract_suggests_the_typed_route_by_name(tmp_path, monkeypatch, shell):
    _entry(tmp_path).with_name("inputs.yaml").write_text(_CONTRACT, encoding="utf-8")
    monkeypatch.setattr(browse_output, "_command_shell", lambda: shell)
    result = inspect_content(tmp_path, "example")
    plain = render_browse_plain(result)
    assert "Typed inputs: declared (3). Run `siteops inputs example`." in plain
    assert "  siteops inputs example\n" in plain
    assert "  siteops plan example --input 'cluster=<Arc-cluster-resource-ID>'\n" in plain
    assert "  siteops deploy example --input 'cluster=<Arc-cluster-resource-ID>'\n" in plain
    assert "For a configured Site, use -l 'name=<site>' instead of --input." in plain
    assert "siteops -w" not in plain
    assert str(tmp_path) not in plain.split("\n", 1)[1]
    assert "manifests/example/manifest.yaml'" not in plain
    assert result.document()["entries"][0]["typedInputs"] == {
        "status": "declared", "count": 3, "answers": [{
            "name": "cluster", "type": "azureResourceId",
            "resourceType": "Microsoft.Kubernetes/connectedClusters",
        }],
    }


def test_typed_route_placeholders_follow_the_answer_type(tmp_path, monkeypatch):
    contract = """\
apiVersion: siteops.inputs/v1
kind: SiteInputContract
inputs:
  - name: vault
    type: azureResourceId
    description: Vault ID.
    resource:
      type: Microsoft.KeyVault/vaults
      apiVersion: "2023-07-01"
    derive:
      subscription: subscription
  - name: subscription
    type: string
    required: false
    description: Subscription.
    sitePath: subscription
  - name: region
    type: string
    description: Region.
    sitePath: location
  - name: enabled
    type: boolean
    description: Toggle.
    sitePath: properties.enabled
"""
    _entry(tmp_path).with_name("inputs.yaml").write_text(contract, encoding="utf-8")
    monkeypatch.setattr(browse_output, "_command_shell", lambda: "POSIX shell")
    plain = render_browse_plain(inspect_content(tmp_path, "example"))
    assert (
        "  siteops plan example --input 'vault=<vault-resource-ID>' --input 'region=<region>' "
        "--input 'enabled=<true-or-false>'\n"
    ) in plain


@pytest.mark.parametrize(("contents", "status"), [
    ("apiVersion: siteops.inputs/v1\nkind: SiteInputContract\ninputs: [PRIVATE_SENTINEL]\n", "unreadable"),
    ("customLocationName: '{{ steps.resolve.outputs.name }}'\n", "none"),
])
def test_contract_status_comes_from_the_contract_file(tmp_path, contents, status):
    path = _entry(tmp_path)
    path.with_name("inputs.yaml").write_text(contents, encoding="utf-8")
    for selection in ("example", str(path)):
        result = inspect_content(tmp_path, selection)
        assert result.status == "complete"
        assert result.entries[0].typed_inputs.status == status
        plain = render_browse_plain(result)
        assert "PRIVATE_SENTINEL" not in plain + serialize_browse_json(result)
    if status == "unreadable":
        assert "Typed inputs: the contract could not be read." in plain
    else:
        assert "Typed inputs: none." in plain


def test_inventory_rows_do_not_read_input_contracts(tmp_path, monkeypatch):
    _entry(tmp_path).with_name("inputs.yaml").write_text(_CONTRACT, encoding="utf-8")
    monkeypatch.setattr(
        browse.ContentReader, "typed_inputs", lambda *a: pytest.fail("Read a contract"),
    )
    result = inspect_content(tmp_path)
    assert result.document()["entries"][0]["typedInputs"] is None


def test_ambiguous_or_path_selected_entries_suggest_explicit_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(browse_output, "_command_shell", lambda: "POSIX shell")
    first = _entry(tmp_path, "same", relative="manifests/one/manifest.yaml")
    _entry(tmp_path, "same", relative="samples/two/manifest.yaml", guidance={"role": "partial"})
    by_name = render_browse_plain(inspect_content(tmp_path, "same"))
    assert "siteops plan same -l 'name=<site>'" in by_name
    by_path = render_browse_plain(inspect_content(tmp_path, str(first)))
    assert "siteops plan manifests/one/manifest.yaml -l 'name=<site>'" in by_path
    both = inspect_content(tmp_path, "same", include_partials=True)
    assert not both.selected


def test_option_shaped_names_suggest_the_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(browse_output, "_command_shell", lambda: "POSIX shell")
    _entry(tmp_path, "--yes", relative="manifests/odd/manifest.yaml")
    result = inspect_content(tmp_path, "--yes")
    assert result.entries[0].name_ambiguous is False
    plain = render_browse_plain(result)
    assert "siteops plan manifests/odd/manifest.yaml -l 'name=<site>'" in plain
    assert "siteops plan --yes" not in plain and "siteops plan '--yes'" not in plain


def test_input_rows_hide_unknown_sensitivity(tmp_path):
    rows = [
        {"field": "plain", "type": "string", "requirement": "optional", "description": "One."},
        {"field": "secret", "type": "string", "requirement": "required",
         "description": "Two.", "sensitivity": "sensitive"},
        {"field": "named", "type": "string", "requirement": "optional",
         "description": "Three.", "sensitivity": "non-sensitive"},
    ]
    plain = render_browse_plain(inspect_content(tmp_path, str(_entry(tmp_path, guidance={"inputs": rows}))))
    assert "  - plain (string, optional)\n" in plain
    assert "  - secret (string, required, sensitive)\n" in plain
    assert "  - named (string, optional, non-sensitive)\n" in plain
    assert "sensitivity:" not in plain


def test_partial_hint_matches_the_inventory_option(tmp_path):
    _entry(tmp_path)
    _entry(tmp_path, "fragment", relative="manifests/_parts/_fragment.yaml", guidance={"role": "partial"})
    default = render_browse_plain(inspect_content(tmp_path))
    assert "Use --include-partials to show partials." in default
    assert "fragments" not in default
    included = render_browse_plain(inspect_content(tmp_path, include_partials=True))
    assert "Use --include-partials" not in included
    assert "Omit --include-partials to hide them." in included


@pytest.mark.parametrize("width", [72, 100])
def test_inventory_rows_use_the_manifest_label_and_the_terminal_width(tmp_path, monkeypatch, width):
    _entry(tmp_path, description="Install the platform. " * 12)
    monkeypatch.setattr(browse_output, "line_width", lambda: width)
    plain = render_browse_plain(inspect_content(tmp_path))
    assert "Manifests: 1 shown, 1 matches, 1 headers read." in plain
    row = next(line for line in plain.splitlines() if line.startswith("  example ["))
    assert row.endswith("...")
    assert width - 12 <= len(row) <= width


def test_inventory_diagnostics_show_text_without_codes(tmp_path):
    _entry(tmp_path)
    plain = render_browse_plain(inspect_content(tmp_path, "exampel"))
    assert "Inspection is incomplete:\n  - Manifest not found. Did you mean 'example'?" in plain
    assert "lookup.missing" not in plain
    assert "lookup.missing" in serialize_browse_json(inspect_content(tmp_path, "exampel"))


def test_heading_states_the_inspection_boundary_without_outcome_claims(tmp_path):
    _entry(tmp_path)
    local = render_browse_plain(inspect_content(tmp_path, "example"))
    assert "Private inspection of local content.\n" in local
    assert " not " not in local.split("\n\n", 1)[0]
    assert "outcome" not in local.split("\n\n", 1)[0]
    assert "  No Sites or selector declared. Pass -l or typed inputs when planning.\n" in local
    _entry(tmp_path, "targeted", selector="name=one")
    targeted = " ".join(render_browse_plain(inspect_content(tmp_path, "targeted")).split())
    assert "`siteops plan` selects Sites from the manifest targeting. -l or typed inputs replace it." in targeted
    assert "Targets have not been resolved" not in local + targeted


def test_heading_names_verified_and_published_sources(tmp_path):
    _entry(tmp_path)
    result = inspect_content(tmp_path, "example")
    verified = replace(result, source=BrowseSource("package", "github:example/kit", verification="verified"))
    remote = replace(result, source=BrowseSource("remote", "github:example/kit"))
    assert "\nPrivate inspection of a verified package.\n" in render_browse_plain(verified)
    assert (
        "\nPrivate inspection of published descriptions. Deployment verifies the package first.\n"
    ) in render_browse_plain(remote)


def test_aio_guidance_distinguishes_typed_defaults_and_packaged_tools(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    workspace = root / "workspaces" / "iot-operations"
    monkeypatch.setattr(browse_output, "_command_shell", lambda: "PowerShell")
    card = render_browse_plain(inspect_content(workspace, "aio-install"))
    assert "environment and country" in card
    assert "typed AIO route defaults to 2608" in card
    assert "compiled ARM JSON" in card
    assert "Authored Site input guidance" in card
    assert card.count("descriptive") == 1
    assert "sensitivity" not in card
    assert "Typed inputs: declared (" in card
    assert "Run `siteops inputs aio-install`." in card
    assert "  siteops plan aio-install --input 'cluster=<Arc-cluster-resource-ID>'\n" in card
    assert "siteops -w" not in card


def test_composed_opc_ua_card_states_its_cluster_step_and_download():
    root = Path(__file__).resolve().parents[1]
    card = " ".join(render_browse_plain(
        inspect_content(root / "workspaces" / "iot-operations", "aio-with-opc-ua")
    ).split())
    assert "kubectl" in card and "connectedk8s" in card
    assert "cluster connect" in card and "Kubernetes RBAC" in card
    assert "raw.githubusercontent.com" in card
    assert "Typed inputs: none." in card
    assert "siteops plan aio-with-opc-ua -l" in card


def test_typed_input_companion_is_not_another_deployment_entry(tmp_path):
    manifest = _entry(tmp_path)
    manifest.with_name("inputs.yaml").write_text(
        "apiVersion: siteops.inputs/v1\nkind: SiteInputContract\ninputs: []\n",
        encoding="utf-8",
    )

    result = inspect_content(tmp_path, include_partials=True)

    assert [entry.name for entry in result.entries] == ["example"]
    assert result.status == "complete"


def test_root_manifest_named_inputs_remains_discoverable(tmp_path):
    _entry(tmp_path, "inputs", relative="manifests/inputs.yaml")
    result = inspect_content(tmp_path)
    assert [entry.name for entry in result.entries] == ["inputs"]
    assert result.status == "complete"


def test_nested_inputs_manifest_remains_discoverable_without_a_sibling_entry(tmp_path):
    manifest = _entry(tmp_path, "network", relative="manifests/network/inputs.yaml")
    companion = _entry(tmp_path, "storage", relative="manifests/storage/manifest.yaml")
    companion.with_name("inputs.yaml").write_text(
        "apiVersion: siteops.inputs/v1\nkind: SiteInputContract\ninputs: []\n",
        encoding="utf-8",
    )

    result = inspect_content(tmp_path)
    assert result.status == "complete"
    assert {entry.name for entry in result.entries} == {"network", "storage"}
    assert next(entry.path for entry in result.entries if entry.name == "network") == (
        "manifests/network/inputs.yaml"
    )
    assert cli.resolve_manifest_path("network", tmp_path) == manifest


def test_ambiguous_nested_inputs_manifest_can_be_explicitly_listed(tmp_path):
    _entry(tmp_path, "storage", relative="manifests/storage/manifest.yaml")
    manifest = _entry(tmp_path, "network", relative="manifests/storage/inputs.yaml")
    assert [entry.name for entry in inspect_content(tmp_path).entries] == ["storage"]

    (tmp_path / "content.yaml").write_text(yaml.safe_dump({
        "apiVersion": API_VERSION,
        "kind": "WorkspaceContent",
        "entries": ["manifests/storage/inputs.yaml"],
    }), encoding="utf-8")
    assert {entry.name for entry in inspect_content(tmp_path).entries} == {"network", "storage"}
    assert cli.resolve_manifest_path("network", tmp_path) == manifest


def test_nested_inputs_is_not_suppressed_by_a_sibling_directory(tmp_path):
    _entry(tmp_path, "network", relative="manifests/network/inputs.yaml")
    (tmp_path / "manifests" / "network" / "manifest.yaml").mkdir()

    result = inspect_content(tmp_path)
    assert result.status == "complete"
    assert [entry.name for entry in result.entries] == ["network"]


def test_flat_manifest_input_companion_preserves_name_lookup(tmp_path):
    manifest = _entry(tmp_path, "storage", relative="manifests/storage.yaml")
    manifest.with_name("storage.inputs.yaml").write_text(
        "apiVersion: siteops.inputs/v1\nkind: SiteInputContract\ninputs: []\n",
        encoding="utf-8",
    )
    result = inspect_content(tmp_path, "storage")
    assert result.status == "complete"
    assert result.selected
    assert [entry.name for entry in result.entries] == ["storage"]


def test_category_never_hides_unclassified_or_partial_role(tmp_path):
    _entry(tmp_path, "unknown", guidance={"category": "core", "role": "unclassified"})
    _entry(tmp_path, "fragment", guidance={"category": "core", "role": "partial"})
    output = render_browse_plain(inspect_content(tmp_path, include_partials=True))
    assert "unknown [core, unclassified]" in output
    assert "fragment [core, partial]" in output


def test_inventory_order_filters_limit_and_partial_visibility(tmp_path):
    _entry(tmp_path, "zeta", guidance={"tags": ["mqtt", "local"], "category": "sample"})
    _entry(tmp_path, "alpha", guidance={"tags": ["mqtt"]})
    _entry(tmp_path, "fragment", relative="manifests/_parts/_fragment.yaml",
           guidance={"role": "partial"})
    result = inspect_content(tmp_path)
    assert [entry.name for entry in result.entries] == ["alpha", "zeta"]
    assert result.discovered == 3
    filtered = inspect_content(tmp_path, search="ZETA", tags=("mqtt", "local"), category="sample")
    assert [entry.name for entry in filtered.entries] == ["zeta"]
    assert [entry.name for entry in inspect_content(tmp_path, search="MQTT ZETA").entries] == ["zeta"]
    limited = inspect_content(tmp_path, limit=1)
    assert limited.matched == 2
    assert limited.document()["hasMore"]
    assert len(inspect_content(tmp_path, include_partials=True).entries) == 3
    assert inspect_content(tmp_path, "fragment").status == "invalid"
    assert inspect_content(tmp_path, "fragment", include_partials=True).selected


def test_ambiguous_names_require_paths(tmp_path):
    first = _entry(tmp_path, "same", relative="manifests/one/manifest.yaml")
    _entry(tmp_path, "same", relative="samples/two/manifest.yaml")
    result = inspect_content(tmp_path, "same")
    assert result.diagnostics[0].code == "lookup.ambiguous"
    assert len(result.entries) == 2
    assert "manifests/one/manifest.yaml" in render_browse_plain(result)
    selected = inspect_content(tmp_path, first.relative_to(tmp_path).as_posix())
    assert selected.status == "complete"
    assert selected.selected


@pytest.mark.parametrize("command", [("browse",), ("validate",), ("plan", "--describe"), ("deploy", "--yes")])
@pytest.mark.parametrize("token", ["choice", "choice.yaml"])
def test_name_filename_ambiguity_has_the_same_paths_across_commands(
    tmp_path, monkeypatch, capsys, command, token,
):
    _entry(tmp_path, token, relative="manifests/named/manifest.yaml")
    _entry(tmp_path, "file-choice", relative=token, metadata=False)
    def blocked(*args, **kwargs):
        pytest.fail("Prepared an ambiguous selection")

    monkeypatch.setattr(cli, "Orchestrator", lambda **k: SimpleNamespace(
        build_plan=blocked, execute_plan=blocked, validate=blocked,
    ))
    code, out, err = _cli(monkeypatch, capsys, tmp_path, *command, token)
    assert code == 1
    assert "ambiguous" in (out + err).lower()
    assert "manifests/named/manifest.yaml" in out + err
    assert "./" + token in out + err
    result = inspect_content(tmp_path, "./" + token)
    assert result.selected and result.entries[0].name == "file-choice"


def test_name_with_yaml_suffix_is_not_forced_to_be_a_filename(tmp_path):
    _entry(tmp_path, "example.yaml")
    result = inspect_content(tmp_path, "example.yaml")
    assert result.selected
    assert result.entries[0].path == "manifests/example.yaml/manifest.yaml"
    assert result.entries[0].name_ambiguous is False


def test_root_manifest_suggestions_use_unambiguous_path_syntax(tmp_path):
    _entry(tmp_path, "example.yaml", relative="example.yaml")
    _entry(tmp_path, "example.yaml", relative="manifests/other/manifest.yaml")
    result = inspect_content(tmp_path, "./example.yaml")
    output = render_browse_plain(result)
    assert "plan './example.yaml'" in output or "plan ./example.yaml" in output
    assert "deploy './example.yaml'" in output or "deploy ./example.yaml" in output


def test_bare_filename_requires_complete_names_but_explicit_path_does_not(tmp_path):
    _entry(tmp_path, "local", relative="local.yaml", metadata=False)
    directory = tmp_path / "manifests"
    directory.mkdir()
    (directory / "broken.yaml").write_text("name: [", encoding="utf-8")
    result = inspect_content(tmp_path, "local.yaml")
    assert not result.selected
    assert result.diagnostics[-1].code == "lookup.incomplete"
    assert inspect_content(tmp_path, "./local.yaml").selected


def test_incomplete_inventory_cannot_prove_name_uniqueness(tmp_path):
    path = _entry(tmp_path)
    broken = tmp_path / "manifests" / "broken.yaml"
    broken.write_text("name: [\n", encoding="utf-8")
    result = inspect_content(tmp_path, "example")
    assert result.status == "invalid"
    assert not result.entries
    assert result.diagnostics[-1].code == "lookup.incomplete"
    assert inspect_content(tmp_path).entries
    assert inspect_content(tmp_path, str(path)).status == "complete"


@pytest.mark.parametrize("affected", ["example", "other"])
def test_bad_entry_guidance_does_not_hide_known_manifest_names(tmp_path, affected):
    _entry(tmp_path, "example")
    _entry(tmp_path, "other")
    (tmp_path / "manifests" / affected / "entry.yaml").write_text("invalid: [", encoding="utf-8")
    result = inspect_content(tmp_path, "example")
    assert result.selected
    assert result.entries[0].name == "example"
    assert result.status == "partial"
    assert not any(item.code == "lookup.incomplete" for item in result.diagnostics)
    assert result.document()["nameInventoryComplete"] is True


@pytest.mark.parametrize("selector", [
    {"tags": ("internal",), "include_partials": True},
    {"limit": 1, "include_partials": True},
])
def test_filtered_ambiguous_names_keep_actionable_paths(tmp_path, selector):
    _entry(tmp_path, "same", relative="manifests/main/manifest.yaml")
    _entry(tmp_path, "same", relative="samples/parts/_same.yaml",
           guidance={"role": "partial", "tags": ["internal"]})
    result = inspect_content(tmp_path, **selector)
    assert len(result.entries) == 1
    assert result.entries[0].path in render_browse_plain(result)


@pytest.mark.parametrize("contents", [
    "",
    "null\n",
    "apiVersion: unsupported\nkind: DeploymentEntry\nsecret: PRIVATE_SENTINEL\n",
    "private_key_SENTINEL: 1\nprivate_key_SENTINEL: 2\n",
    "apiVersion: siteops/v1alpha1\nkind: DeploymentEntry\nrole: []\n",
    "apiVersion: siteops/v1alpha1\nkind: DeploymentEntry\ninputs: [PRIVATE_SENTINEL]\n",
])
def test_bad_metadata_has_safe_diagnostics_and_keeps_explicit_header(tmp_path, contents):
    path = _entry(tmp_path)
    path.with_name("entry.yaml").write_text(contents, encoding="utf-8")
    result = inspect_content(tmp_path, str(path))
    assert result.status == "partial"
    assert result.entries[0].metadata_status == "unavailable"
    assert result.entries[0].guidance.role == "unclassified"
    for output in (render_browse_plain(result), serialize_browse_json(result)):
        assert "PRIVATE_SENTINEL" not in output
        assert "private_key_SENTINEL" not in output
    assert result.diagnostics


@pytest.mark.parametrize("sensitivity", ["unknown", "sensitive"])
def test_protected_default_text_is_rejected_without_echo(tmp_path, sensitivity):
    path = _entry(tmp_path, guidance={"inputs": [{
        "field": "parameters.token", "type": "string", "requirement": "optional",
        "description": "Protected input.", "sensitivity": sensitivity,
        "defaultBehavior": "PRIVATE_SENTINEL",
    }]})
    result = inspect_content(tmp_path, str(path))
    assert result.status == "partial"
    assert "PRIVATE_SENTINEL" not in serialize_browse_json(result)


def test_supported_inputs_and_step_supplies_do_not_resolve_values(tmp_path):
    path = _entry(tmp_path, guidance={
        "inputs": [{
            "field": "parameters.resourceName", "type": "string", "requirement": "optional",
            "description": "Operator setting.", "sensitivity": "non-sensitive",
            "defaultBehavior": "Derived from the Site name.",
            "source": "parameters/defaults.yaml",
        }],
        "supplied": [{
            "step": "consumer", "input": "resourceName", "description": "Prior operation output.",
        }],
    })
    result = inspect_content(tmp_path, str(path))
    assert result.status == "complete"
    guidance = result.entries[0].guidance
    assert guidance.inputs[0].default_behavior == "Derived from the Site name."
    assert guidance.supplied[0].step == "consumer"
    assert "value" not in guidance.inputs[0].document()


def test_extra_paths_are_optional_and_confined(tmp_path):
    path = _entry(tmp_path, relative="operations/one/action.yaml")
    assert not inspect_content(tmp_path).entries
    additions = tmp_path / "content.yaml"
    additions.write_text(yaml.safe_dump({
        "apiVersion": API_VERSION, "kind": "WorkspaceContent",
        "entries": ["operations/one/action.yaml"],
    }), encoding="utf-8")
    assert inspect_content(tmp_path).entries[0].path == path.relative_to(tmp_path).as_posix()
    additions.write_text(yaml.safe_dump({
        "apiVersion": API_VERSION, "kind": "WorkspaceContent",
        "entries": ["operations/one/action.yaml", "./operations/one/action.yaml"],
    }), encoding="utf-8")
    assert inspect_content(tmp_path).status == "invalid"
    assert inspect_content(tmp_path, str(path)).status == "complete"


@pytest.mark.parametrize("target", [
    "sites/private.yaml", "sites.local/private.yaml", "parameters/private.yaml",
    "answers/private.yaml", "runs/private.yaml", ".dev-local/private.yaml",
    "../outside.yaml", "manifests/example/manifest.yaml:private",
])
def test_protected_and_escaping_paths_are_rejected_before_read(tmp_path, monkeypatch, target):
    _entry(tmp_path)

    def no_read(*args, **kwargs):
        raise AssertionError("Rejected paths must not be read")

    monkeypatch.setattr(Path, "open", no_read)
    result = inspect_content(tmp_path, target)
    assert result.status == "invalid"
    assert result.diagnostics[0].code.startswith("path.")


@pytest.mark.parametrize("directory", ["sites.", "sites ", "sites.local.", "parameters."])
def test_windows_normalization_aliases_are_rejected_before_read(tmp_path, monkeypatch, directory):
    actual = tmp_path / directory.rstrip(" .")
    actual.mkdir()
    (actual / "private.yaml").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Protected alias was read"))
    result = inspect_content(tmp_path, f"{directory}/private.yaml")
    assert result.status == "invalid"
    assert result.diagnostics[0].code.startswith("path.")


def test_canonical_windows_alias_cannot_hide_a_protected_directory(tmp_path, monkeypatch):
    protected = tmp_path / "sites.local" / "private.yaml"
    protected.parent.mkdir()
    protected.write_text("PRIVATE_SENTINEL", encoding="utf-8")
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if "SITESL~1" in path.parts:
            return protected
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Protected alias was read"))
    result = inspect_content(tmp_path, "SITESL~1/private.yaml")
    assert result.diagnostics[0].code == "path.protected"


@pytest.mark.parametrize("path", ["manifests/CON.yaml", "manifests/LPT1.yaml"])
def test_reserved_device_names_do_not_reach_file_access(tmp_path, monkeypatch, path):
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Device path was opened"))
    assert inspect_content(tmp_path, path).diagnostics[0].code == "path.alias"


def test_yaml_extension_case_does_not_hide_an_entry(tmp_path):
    path = _entry(tmp_path, "upper", relative="manifests/upper/MANIFEST.YAML", metadata=False)
    path.with_name("entry.yaml").write_text(
        f"apiVersion: {API_VERSION}\nkind: DeploymentEntry\nrole: standalone\n",
        encoding="utf-8",
    )
    result = inspect_content(tmp_path, "upper")
    assert result.selected and result.entries[0].metadata_status == "declared"


@pytest.mark.skipif(os.name != "nt", reason="Windows short-name filesystem behavior")
def test_actual_windows_short_name_is_not_a_configuration_read_route(tmp_path, monkeypatch):
    protected = tmp_path / "parameters"
    protected.mkdir()
    (protected / "private.yaml").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(32768)
    get_short_path = ctypes.windll.kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_short_path.restype = ctypes.c_uint
    length = get_short_path(str(protected), buffer, len(buffer))
    if not length or length >= len(buffer):
        pytest.skip("Short-name lookup is unavailable on this filesystem")
    alias = Path(buffer.value).name
    if alias.casefold() == protected.name.casefold():
        pytest.skip("This filesystem does not create short names")
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Protected short path was read"))
    result = inspect_content(tmp_path, f"{alias}/private.yaml")
    assert result.diagnostics[0].code == "path.protected"


def test_headers_do_not_expand_includes_or_read_sites_and_parameters(tmp_path, monkeypatch):
    path = _entry(
        tmp_path, metadata=False,
        steps=[{"include": "../../sites/private.yaml"}],
        parameters=["parameters/private.yaml"],
    )
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites" / "private.yaml").write_text("PRIVATE_SENTINEL\n", encoding="utf-8")
    original = Path.open
    reads = []

    def tracked(path, *args, **kwargs):
        reads.append(path)
        assert "sites" not in path.relative_to(tmp_path).parts
        assert "parameters" not in path.relative_to(tmp_path).parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked)
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("Unexpected engine file read"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected process"))
    result = inspect_content(tmp_path, str(path))
    assert result.status == "complete"
    assert reads == [path]
    assert "PRIVATE_SENTINEL" not in serialize_browse_json(result)


def test_read_only_and_no_git_copy_share_the_entry_contract(tmp_path, monkeypatch):
    authored = tmp_path / "authored"
    cached = tmp_path / "materialized"
    _entry(authored, relative="manifests/example/manifest.yml")
    shutil.copytree(authored, cached)
    expected = inspect_content(authored).document()["entries"]

    def no_write(*args, **kwargs):
        raise AssertionError("Inspection must not mutate the workspace")

    monkeypatch.setattr(Path, "write_text", no_write)
    monkeypatch.setattr(Path, "write_bytes", no_write)
    monkeypatch.setattr(Path, "mkdir", no_write)
    result = inspect_content(cached)
    assert result.document()["entries"] == expected
    assert result.document()["source"]["verification"] == "not-performed"
    assert not (cached / ".git").exists()


def test_symlinks_and_reparse_points_are_rejected_before_open(tmp_path, monkeypatch):
    path = _entry(tmp_path)
    original = Path.lstat
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)

    def linked(candidate, *args, **kwargs):
        if candidate == path:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", linked)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Link target was read"))
    result = inspect_content(tmp_path, str(path))
    assert result.diagnostics[0].code == "path.link"


def test_hardlinked_files_are_rejected(tmp_path):
    path = _entry(tmp_path, metadata=False)
    duplicate = path.with_name("duplicate.yaml")
    os.link(path, duplicate)
    assert inspect_content(tmp_path, str(path)).diagnostics[0].code == "file.link"


def test_scan_keeps_regular_siblings_of_a_link(tmp_path, monkeypatch):
    good = _entry(tmp_path)
    other = tmp_path / "manifests" / "a-link.yaml"
    other.write_text("ignored: true\n", encoding="utf-8")
    inode = other.stat().st_ino
    original = browse._is_link
    monkeypatch.setattr(
        browse, "_is_link", lambda info: info.st_ino == inode or original(info)
    )
    result = inspect_content(tmp_path)
    assert result.status == "partial"
    assert result.entries[0].path == good.relative_to(tmp_path).as_posix()


def test_limits_fail_explicitly_without_silent_inventory_success(tmp_path, monkeypatch):
    for index in range(4):
        _entry(tmp_path, f"entry-{index}")
    monkeypatch.setattr(browse, "MAX_ENTRIES", 2)
    result = inspect_content(tmp_path)
    assert result.status == "partial"
    assert len(result.entries) == 2
    assert any(item.code == "scan.limit" for item in result.diagnostics)
    assert inspect_content(tmp_path, "entry-0").status == "invalid"


def test_file_and_total_read_budgets(tmp_path, monkeypatch):
    path = _entry(tmp_path, metadata=False)
    monkeypatch.setattr(browse, "MAX_FILE_BYTES", 1)
    assert inspect_content(tmp_path, str(path)).diagnostics[0].code == "file.limit"
    monkeypatch.setattr(browse, "MAX_FILE_BYTES", 256 * 1024)
    monkeypatch.setattr(browse, "MAX_TOTAL_BYTES", 1)
    assert inspect_content(tmp_path).diagnostics[0].code == "read.limit"


@pytest.mark.parametrize("text", [
    "recursive: &recursive [*recursive]\n",
    "[" * 100 + "]" * 100,
    "a: &a [1, 2, 3]\nb: &b [*a, *a, *a]\nc: [*b, *b, *b]\n",
])
def test_bounded_yaml_rejects_excessive_expansion(text):
    with pytest.raises(yaml.YAMLError):
        yamlio.load_bounded(text, max_nodes=20, max_depth=10)


def test_bounded_yaml_preserves_valid_aliases_and_duplicate_key_rules():
    text = "base: &base {name: value}\ncopy: *base\n"
    assert yamlio.load_bounded(text) == yamlio.load(text)
    with pytest.raises(yamlio.DuplicateKeyError):
        yamlio.load_bounded("key: first\nkey: second\n")


def test_terminal_controls_are_escaped_and_json_retains_data(tmp_path):
    path = _entry(tmp_path, name="example", description="Text \x1b[31mhidden\u202e")
    result = inspect_content(tmp_path, str(path))
    plain = render_browse_plain(result)
    assert "\x1b" not in plain and "\u202e" not in plain
    assert "\\u001b" in plain and "\\u202e" in plain
    assert json.loads(serialize_browse_json(result))["entries"][0]["description"].endswith("\u202e")


def test_windows_commands_are_labeled_and_unsafe_shell_paths_are_withheld(tmp_path, monkeypatch):
    ordinary = _entry(tmp_path, "ordinary")
    unsafe = _entry(tmp_path, "unsafe", relative="manifests/unsafe&example/manifest.yaml")
    monkeypatch.setattr(browse_output, "_command_shell", lambda: "PowerShell")
    assert "PowerShell" in render_browse_plain(inspect_content(tmp_path, str(ordinary)))
    output = render_browse_plain(inspect_content(tmp_path, str(unsafe)))
    assert "commands are withheld" in output
    assert "siteops -w" not in output


def test_cli_refuses_private_output_before_source_or_site_access(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    monkeypatch.setattr(cli, "inspect_content", lambda *a, **k: pytest.fail("Read private content"))
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("Loaded Sites"))
    code, out, err = _cli(monkeypatch, capsys, tmp_path / "PRIVATE_SENTINEL", "browse", "--output", "json")
    assert code == 1 and not out
    assert "Content inspection output is private" in err
    assert "PRIVATE_SENTINEL" not in err


def test_cli_handles_100_mixed_entries_in_one_pass(tmp_path, monkeypatch, capsys):
    for index in range(100):
        name = f"entry-{index:03}"
        area = "samples" if index % 2 else "manifests"
        _entry(tmp_path, name, relative=f"{area}/{name}/manifest.yaml", guidance={
            "tags": ["large"], "category": "sample" if index % 2 else "operation",
        })
    reads = []
    original = Path.open

    def tracked(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked)
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("Loaded Sites"))
    monkeypatch.setattr(cli, "_resolve_extra_sites_dirs", lambda *a: pytest.fail("Read site settings"))
    monkeypatch.setenv("SITEOPS_EXTRA_SITES_DIRS", "PRIVATE_SENTINEL")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected process"))
    code, out, err = _cli(monkeypatch, capsys, tmp_path, "browse", "--output", "json")
    assert code == 0 and not err
    document = json.loads(out)
    assert document["matched"] == document["shown"] == document["discovered"] == 100
    assert len(reads) == len(set(reads)) == 200
    assert "PRIVATE_SENTINEL" not in out
    code, plain, _ = _cli(monkeypatch, capsys, tmp_path, "browse")
    assert code == 0 and len(plain.splitlines()) < 120
    assert all(f"entry-{index:03}" in plain for index in range(100))
    code, filtered, _ = _cli(
        monkeypatch, capsys, tmp_path, "browse", "--tag", "large",
        "--category", "sample", "--search", "entry-099", "--output", "json",
    )
    assert code == 0 and json.loads(filtered)["matched"] == 1


def test_unexpected_failure_does_not_emit_a_success_document(tmp_path, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "inspect_content", broken)
    monkeypatch.setattr(sys, "argv", ["siteops", "-w", str(tmp_path), "browse", "--output", "json"])
    with pytest.raises(RuntimeError, match="unexpected"):
        cli.main()
    assert not capsys.readouterr().out


def test_empty_inventory_and_invalid_option_combinations(tmp_path):
    assert inspect_content(tmp_path).status == "complete"
    with pytest.raises(ValueError, match="positive"):
        inspect_content(tmp_path, limit=0)
    with pytest.raises(ValueError, match="filters"):
        inspect_content(tmp_path, "example", search="example")
    with pytest.raises(BrowseError, match="directory"):
        inspect_content(tmp_path / "missing")


@pytest.mark.parametrize("by_name", [False, True])
def test_non_aio_browse_plan_deploy_uses_configured_site(tmp_path, monkeypatch, capsys, by_name):
    source = Path(__file__).parent / "fixtures" / "browse-workspace"
    workspace = tmp_path / "materialized"
    shutil.copytree(source, workspace)
    overlay = workspace / "sites.local"
    overlay.mkdir()
    (overlay / "example.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "example",
        "resourceGroup": "configured-group",
        "parameters": {"storageAccountName": "configuredstorage"},
    }), encoding="utf-8")
    submissions = []

    def version_only(argv, timeout):
        assert argv[1:] == ("version", "--output", "json")
        return subprocess.CompletedProcess(argv, 0, '{"azure-cli":"2.87.0"}', "")

    monkeypatch.setattr(
        "siteops.orchestrator.TemplateCompilationSession",
        lambda: TemplateCompilationSession(
            command_runner=version_only,
            tool_resolver=lambda name: str(tmp_path / "tools" / "az.exe"),
        ),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected live process"))

    def submit(self, **kwargs):
        submissions.append(kwargs)
        return DeploymentResult(
            success=True, step_name=kwargs["step_name"], site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
        )

    monkeypatch.setattr(AzCliExecutor, "deploy_resource_group", submit)
    code, out, _ = _cli(monkeypatch, capsys, workspace, "browse", "storage", "--output", "json")
    assert code == 0
    inspection = json.loads(out)
    assert "configuredstorage" not in out
    path = "storage" if by_name else inspection["entries"][0]["path"]
    assert inspection["source"]["verification"] == "not-performed"
    inventories = []
    original_inventory = browse.ContentReader.inventory

    def inventory(reader):
        inventories.append(reader.workspace)
        return original_inventory(reader)

    monkeypatch.setattr(browse.ContentReader, "inventory", inventory)
    code, out, _ = _cli(monkeypatch, capsys, workspace, "validate", path)
    assert code == 0
    assert len(inventories) == int(by_name)
    inventories.clear()
    code, out, _ = _cli(
        monkeypatch, capsys, workspace, "plan", path, "-l", "name=example", "--output", "json"
    )
    assert code == 0 and json.loads(out)["status"] == "planned"
    assert "configuredstorage" not in out
    assert not submissions
    assert len(inventories) == int(by_name)
    inventories.clear()
    code, out, _ = _cli(
        monkeypatch, capsys, workspace, "plan", path, "--describe",
        "-l", "name=example", "--output", "json",
    )
    assert code == 0 and json.loads(out)["status"] == "planned"
    assert json.loads(out)["intent"] == "describe"
    assert not submissions
    assert len(inventories) == int(by_name)
    inventories.clear()
    code, out, _ = _cli(
        monkeypatch, capsys, workspace, "deploy", path, "-l", "name=example", "--yes", "--output", "json"
    )
    assert code == 0 and json.loads(out)["status"] == "succeeded"
    assert len(inventories) == int(by_name)
    assert len(submissions) == 1
    assert submissions[0]["resource_group"] == "configured-group"
    assert submissions[0]["parameters"]["storageAccountName"] == "configuredstorage"
    assert submissions[0]["parameters"]["location"] == "westus2"
    assert submissions[0]["template_path"] == workspace / "templates" / "storage.template.json"

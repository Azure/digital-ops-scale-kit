"""Exercise override generation at its published and private output boundary."""

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate-site-overrides.py"


@pytest.mark.parametrize("destination", ["private", "explicit", "github", "ado"])
@pytest.mark.parametrize("invalid", [False, True])
def test_override_generation_keeps_site_identity_out_of_ci_output(
    tmp_path, monkeypatch, capsys, destination, invalid,
):
    spec = importlib.util.spec_from_file_location("override_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("TF_BUILD", raising=False)
    monkeypatch.delenv("SITEOPS_REDACT_OUTPUT", raising=False)
    if destination in {"private", "explicit"}:
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0" if destination == "private" else "1")
    else:
        monkeypatch.setenv("GITHUB_ACTIONS" if destination == "github" else "TF_BUILD", "true")
    site = "private-site-marker" + ("%" if invalid else "")
    answers = {site: {"parameters.clusterName": "private-cluster-marker"}}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(answers)))
    if invalid:
        with pytest.raises(SystemExit) as caught:
            module.main()
        assert caught.value.code == 1
    else:
        module.main()
        # A repeated generation exercises the preserved existing-overlay diagnostic.
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(answers)))
        module.main()
        assert (tmp_path / "sites.local" / f"{site}.yaml").is_file()
    output = capsys.readouterr()
    combined = output.out + output.err
    assert ("private-site-marker" in combined) is (destination == "private")
    assert "private-cluster-marker" not in combined
    if destination != "private":
        assert str(tmp_path) not in combined
    if not invalid:
        assert "Generated 1 site override(s)" in output.out
        assert "Skipped 1 site(s)" in output.out


def test_override_generation_preserves_a_file_created_after_the_existence_check(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("override_race", SCRIPT)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = tmp_path / "site.yaml"
    original = Path.open
    intervened = False

    def open_file(self, mode="r", *args, **kwargs):
        nonlocal intervened
        if self == path and not intervened and mode in {"w", "x"}:
            intervened = True
            with original(self, "w", encoding="utf-8") as stream:
                stream.write('{"preserved":true}\n')
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    generated, skipped = helper.generate_overlays({"site": {"parameters.value": "new"}}, tmp_path)
    assert generated == [] and skipped == ["site"]
    assert json.loads(path.read_text()) == {"preserved": True}


def _load_override_script():
    spec = importlib.util.spec_from_file_location("override_script_paths", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("destination,redacted,prefix", [
    ("local", False, "Warning: "),
    ("private", False, "Warning: "),
    ("explicit", True, "Warning: "),
    ("github", True, "::warning::"),
    ("ado", True, "##vso[task.logissue type=warning]"),
    ("github-private", False, "::warning::"),
    ("ado-private", False, "##vso[task.logissue type=warning]"),
])
@pytest.mark.parametrize("base_files,keys,unmatched", [
    (["selected-site.yaml"], ["selected-site"], []),
    ([], ["foreign-site"], ["foreign-site"]),
    (["regions/nested-site.yaml"], ["nested-site"], ["nested-site"]),
    (["selected-site.yml"], ["selected-site"], []),
    (["selected-site.yaml"], ["selected-site", "foreign-site", "other-site"], ["foreign-site", "other-site"]),
    (["selected.yaml"], ["inventory-site"], ["inventory-site"]),
], ids=["matched", "unmatched", "nested-basename", "yml", "mixed", "internal-name"])
def test_override_warning_decision_table(
    tmp_path, monkeypatch, capsys, destination, redacted, prefix, base_files, keys, unmatched,
):
    for variable in ("GITHUB_ACTIONS", "TF_BUILD", "SITEOPS_REDACT_OUTPUT", "SITEOPS_EXTRA_SITES_DIRS"):
        monkeypatch.delenv(variable, raising=False)
    if destination.startswith("github"):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    elif destination.startswith("ado"):
        monkeypatch.setenv("TF_BUILD", "true")
    if destination == "explicit":
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    elif destination == "private" or destination.endswith("-private"):
        monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    for relative in base_files:
        path = tmp_path / "sites" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kind: Site\nname: inventory-site\n", encoding="utf-8")
    overrides = {key: {"parameters.clusterName": "private-cluster-marker"} for key in keys}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(overrides)))

    _load_override_script().main()

    output = capsys.readouterr()
    combined = output.out + output.err
    assert f"Generated {len(keys)} site override(s)" in output.out
    for key in keys:
        overlay = tmp_path / "sites.local" / f"{key}.yaml"
        assert json.loads(overlay.read_text(encoding="utf-8")) == {
            "parameters": {"clusterName": "private-cluster-marker"},
        }
        if redacted:
            assert key not in combined
    assert "private-cluster-marker" not in combined
    warnings = [line for line in combined.splitlines() if line.startswith(prefix)]
    if unmatched:
        assert len(warnings) == 1
        warning = warnings[0]
        assert f"{len(unmatched)} Site override key(s) match no Site file" in warning
        assert (
            "Each key must name a Site file at the top of sites/ or a "
            "SITEOPS_EXTRA_SITES_DIRS directory, without its extension."
        ) in warning
        assert "The overlays were still written." in warning
        for key in keys:
            assert (key in warning) is (not redacted and key in unmatched)
        if prefix == "Warning: ":
            assert warning in output.err
            assert warning not in output.out
        else:
            assert warning in output.out
            assert output.err == ""
    else:
        assert warnings == []
        assert output.err == ""


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("extension", [".yaml", ".yml"])
def test_override_warning_checks_extra_directories_like_the_cli(
    tmp_path, monkeypatch, capsys, relative, extension,
):
    monkeypatch.chdir(tmp_path)
    for variable in ("GITHUB_ACTIONS", "TF_BUILD"):
        monkeypatch.delenv(variable, raising=False)
    first = tmp_path / "first-extra"
    second = tmp_path / "second-extra"
    first.mkdir()
    second.mkdir()
    (first / "regions").mkdir()
    (first / "regions" / "nested-site.yaml").write_text("kind: Site\n", encoding="utf-8")
    (second / f"library-site{extension}").write_text("kind: Site\n", encoding="utf-8")
    directories = [first.name, second.name] if relative else [str(first), str(second)]
    monkeypatch.setenv("SITEOPS_EXTRA_SITES_DIRS", os.pathsep.join(["", *directories, ""]))
    workspace = tmp_path / "workspace"
    overrides = {"library-site": {"location": "eastus"}, "nested-site": {"location": "eastus"}}
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(workspace)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(overrides)))

    _load_override_script().main()

    output = capsys.readouterr()
    assert "Generated 2 site override(s)" in output.out
    assert "1 Site override key(s) match no Site file (nested-site)." in output.err
    assert "library-site" not in output.err
    for key in overrides:
        assert (workspace / "sites.local" / f"{key}.yaml").is_file()


def test_override_warning_includes_keys_with_preserved_overlays(tmp_path, monkeypatch, capsys):
    for variable in ("GITHUB_ACTIONS", "TF_BUILD", "SITEOPS_EXTRA_SITES_DIRS"):
        monkeypatch.delenv(variable, raising=False)
    overlay = tmp_path / "sites.local" / "unmatched-site.yaml"
    overlay.parent.mkdir()
    overlay.write_text('{"location": "westus"}\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"unmatched-site": {"location": "eastus"}}'))

    _load_override_script().main()

    output = capsys.readouterr()
    assert "Generated 0 site override(s)" in output.out
    assert "Skipped 1 site(s)" in output.out
    assert "1 Site override key(s) match no Site file" in output.err
    assert json.loads(overlay.read_text(encoding="utf-8")) == {"location": "westus"}


def test_nested_override_keys_keep_the_existing_validation(tmp_path):
    with pytest.raises(ValueError, match="Invalid site name"):
        _load_override_script().generate_overlays({"regions/nested-site": {}}, tmp_path)


def test_ci_guide_names_the_nested_key_rejection(tmp_path):
    guide = " ".join((SCRIPT.parents[1] / "docs" / "ci-cd-setup.md").read_text(encoding="utf-8").split())
    with pytest.raises(ValueError, match="^Invalid site name: 'regions/selected'"):
        _load_override_script().generate_overlays({"regions/selected": {}}, tmp_path)
    assert "A key such as `regions/selected` stops the pipeline with `Invalid site name`." in guide
    assert "write its overlay to `sites.local/regions/selected.yaml`" in guide


@pytest.mark.parametrize("flat", [
    {"parameters.value": "nested", "parameters": "scalar"},
    {"parameters": "scalar", "parameters.value": "nested"},
    {"parameters.broker.memoryProfile": "Low", "parameters.broker": {"memoryProfile": "Medium"}},
    {"parameters.broker": "x", "parameters.broker.memoryProfile": "Low"},
])
def test_overlapping_override_paths_are_rejected_in_any_order(flat):
    with pytest.raises(ValueError, match="conflict"):
        _load_override_script().expand_dot_notation(flat)


def test_sibling_override_paths_expand_together():
    assert _load_override_script().expand_dot_notation({
        "parameters.broker.memoryProfile": "Low", "parameters.clusterName": "c", "location": "eastus",
    }) == {"parameters": {"broker": {"memoryProfile": "Low"}, "clusterName": "c"}, "location": "eastus"}

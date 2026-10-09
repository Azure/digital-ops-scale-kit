"""Exercise override generation at its published and private output boundary."""

import importlib.util
import io
import json
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

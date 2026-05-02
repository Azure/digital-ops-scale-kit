"""Unit tests for scripts/render-e2e-site.py.

The renderer is the last checkpoint before Azure spin-up: missing a required
env var or leaving a placeholder un-substituted must fail loudly, not emit a
silently broken site file. These tests exercise the happy path plus every
fail-fast branch.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "render-e2e-site.py"


def _load_render_module():
    """Import the renderer by path (hyphens in filename block normal import)."""
    spec = importlib.util.spec_from_file_location("render_e2e_site", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


render_e2e_site = _load_render_module()


REQUIRED_ENV = {
    "E2E_RESOURCE_GROUP": "rg-test",
    "E2E_CLUSTER_NAME": "cl-test",
    "E2E_AIO_RELEASE": "2603",
}

FULL_ENV = {
    **REQUIRED_ENV,
    "E2E_SITE_NAME": "e2e-unit-test",
    "E2E_SUBSCRIPTION": "00000000-0000-0000-0000-000000000000",
    "E2E_LOCATION": "eastus2",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove all E2E_* vars so each test starts from a known state."""
    for name in render_e2e_site.ALL_VARS:
        monkeypatch.delenv(name, raising=False)


class TestCollectValues:
    def test_missing_required_var_exits(self, monkeypatch):
        for k, v in REQUIRED_ENV.items():
            if k != "E2E_AIO_RELEASE":
                monkeypatch.setenv(k, v)
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.collect_values()
        assert "E2E_AIO_RELEASE" in str(exc.value)

    def test_all_required_missing_lists_all(self, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.collect_values()
        msg = str(exc.value)
        for name in REQUIRED_ENV:
            assert name in msg

    def test_whitespace_only_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("E2E_RESOURCE_GROUP", "   ")
        monkeypatch.setenv("E2E_CLUSTER_NAME", "cl")
        monkeypatch.setenv("E2E_AIO_RELEASE", "2603")
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.collect_values()
        assert "E2E_RESOURCE_GROUP" in str(exc.value)

    def test_all_vars_set_skips_az_fallback(self, monkeypatch):
        for k, v in FULL_ENV.items():
            monkeypatch.setenv(k, v)
        # If _run_az is called, surface it as a failure.
        monkeypatch.setattr(
            render_e2e_site, "_run_az", lambda args: pytest.fail(f"_run_az called: {args}")
        )
        result = render_e2e_site.collect_values()
        assert result == FULL_ENV

    def test_az_fallback_failure_exits(self, monkeypatch):
        for k, v in REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)

        def _boom(_args):
            raise RuntimeError("az not found")

        monkeypatch.setattr(render_e2e_site, "_run_az", _boom)
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.collect_values()
        assert "az not found" in str(exc.value)


class TestComputeDefaults:
    def test_site_name_default_uses_unix_epoch(self, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1700000000)
        monkeypatch.setattr(
            render_e2e_site,
            "_run_az",
            lambda args: pytest.fail(f"_run_az should not be called: {args}"),
        )
        values = dict.fromkeys(render_e2e_site.ALL_VARS, "")
        # Pre-fill the other defaults so only the site-name branch executes.
        values["E2E_SUBSCRIPTION"] = "sub"
        values["E2E_LOCATION"] = "westus"
        out = render_e2e_site.compute_defaults(values)
        assert out["E2E_SITE_NAME"] == "e2e-local-1700000000"

    def test_subscription_default_uses_az(self, monkeypatch):
        calls = []

        def _fake_az(args):
            calls.append(tuple(args))
            if args[0] == "account":
                return "sub-id-from-az"
            if args[0] == "group":
                return "westus"
            pytest.fail(f"unexpected az call: {args}")

        monkeypatch.setattr(render_e2e_site, "_run_az", _fake_az)
        values = {name: "" for name in render_e2e_site.ALL_VARS}
        values["E2E_RESOURCE_GROUP"] = "rg-x"
        out = render_e2e_site.compute_defaults(values)
        assert out["E2E_SUBSCRIPTION"] == "sub-id-from-az"
        assert out["E2E_LOCATION"] == "westus"
        assert any(a[0] == "account" for a in calls)
        assert any(a[0] == "group" for a in calls)


class TestRender:
    def _make_template(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "site.yaml.tmpl"
        p.write_text(body, encoding="utf-8")
        return p

    def test_full_substitution(self, tmp_path):
        tmpl = self._make_template(
            tmp_path,
            "name: ${E2E_SITE_NAME}\nrg: ${E2E_RESOURCE_GROUP}\nsub: ${E2E_SUBSCRIPTION}\n",
        )
        out = render_e2e_site.render(tmpl, FULL_ENV)
        assert "${" not in out
        assert "name: e2e-unit-test" in out
        assert "rg: rg-test" in out

    def test_unknown_placeholder_exits(self, tmp_path):
        tmpl = self._make_template(tmp_path, "bad: ${NOT_AN_E2E_VAR}\n")
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.render(tmpl, FULL_ENV)
        assert "NOT_AN_E2E_VAR" in str(exc.value)

    def test_unresolved_leftover_detected(self, tmp_path, monkeypatch):
        """If `Template.safe_substitute` somehow leaves a placeholder behind
        (e.g. future substitution mode change), the explicit post-render scan
        must still catch it before the file is written."""
        tmpl = self._make_template(tmp_path, "good: ${E2E_SITE_NAME}\n")
        real_sub = render_e2e_site.string.Template.safe_substitute

        def _leaky_sub(self, *args, **kwargs):
            # Simulate a regression where a placeholder slips through.
            return real_sub(self, *args, **kwargs) + "\nbad: ${FORGOTTEN_VAR}\n"

        monkeypatch.setattr(render_e2e_site.string.Template, "safe_substitute", _leaky_sub)
        with pytest.raises(SystemExit) as exc:
            render_e2e_site.render(tmpl, FULL_ENV)
        assert "${FORGOTTEN_VAR}" in str(exc.value)

    def test_bare_dollar_passes_through(self, tmp_path):
        """A literal `$` not part of `${name}` must not raise. Templates may
        contain shell-style `$VAR` refs in comments, regex examples, or doc
        snippets. `safe_substitute` lets those through. The leftover-pattern
        only fires on `${...}` forms."""
        body = "comment: '# uses $RUNNER_TEMP at runtime'\nname: ${E2E_SITE_NAME}\n"
        tmpl = self._make_template(tmp_path, body)
        out = render_e2e_site.render(tmpl, FULL_ENV)
        assert "$RUNNER_TEMP" in out
        assert "name: e2e-unit-test" in out

    def test_template_placeholders_preserved_verbatim_outside_dollar_brace(self, tmp_path):
        """`{{ ... }}` and `$VAR` (no braces) must pass through untouched. Those
        forms are consumed by siteops' own templater at load time."""
        body = "name: ${E2E_SITE_NAME}\ncluster: {{ site.parameters.clusterName }}\n"
        tmpl = self._make_template(tmp_path, body)
        out = render_e2e_site.render(tmpl, FULL_ENV)
        assert "{{ site.parameters.clusterName }}" in out
        assert "name: e2e-unit-test" in out


class TestRealTemplate:
    """Render the real e2e-test.yaml.tmpl and confirm it produces valid YAML."""

    def test_real_template_renders(self):
        import yaml

        template = Path(__file__).parent.parent / "tests" / "e2e" / "sites" / "e2e-test.yaml.tmpl"
        out = render_e2e_site.render(template, FULL_ENV)
        doc = yaml.safe_load(out)
        assert doc["name"] == FULL_ENV["E2E_SITE_NAME"]
        assert doc["resourceGroup"] == FULL_ENV["E2E_RESOURCE_GROUP"]
        assert doc["subscription"] == FULL_ENV["E2E_SUBSCRIPTION"]
        assert doc["properties"]["aioRelease"] == FULL_ENV["E2E_AIO_RELEASE"]

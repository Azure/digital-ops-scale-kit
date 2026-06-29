# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Tests for secret handling in the display layer and the params file.

- `siteops sites` and `siteops sites --render` redact secret-keyed values so a
  secret supplied via sites.local / SITE_OVERRIDES does not print.
- The deploy params file is created with owner-only permissions.
"""

import json
import os
from argparse import Namespace

import pytest

from siteops.cli import _is_sensitive_key, _redact_sensitive, cmd_sites
from siteops.executor import AzCliExecutor
from siteops.orchestrator import Orchestrator

SECRET = "S3cr3t-Sp-Value-xyz"


def _write_site_with_secret(workspace):
    (workspace / "sites").mkdir(parents=True)
    (workspace / "sites" / "s1.yaml").write_text(
        f"""apiVersion: siteops/v1
kind: Site
name: s1
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg
location: eastus
parameters:
  clusterName: my-cluster
  aksee:
    machineName: vm-1
    spPassword: {SECRET}
""",
        encoding="utf-8",
    )


class TestIsSensitiveKey:
    @pytest.mark.parametrize(
        "key",
        [
            "spPassword",
            "password",
            "clientSecret",
            "SP_SECRET",
            "apiToken",
            "accountKey",
            "connectionString",
            "myCredential",
        ],
    )
    def test_sensitive(self, key):
        assert _is_sensitive_key(key) is True

    @pytest.mark.parametrize(
        "key",
        ["clusterName", "machineName", "location", "tagKey", "memoryProfile", "aioRelease"],
    )
    def test_not_sensitive(self, key):
        assert _is_sensitive_key(key) is False


class TestRedactSensitive:
    def test_scalar_under_sensitive_key(self):
        out = _redact_sensitive({"spPassword": SECRET, "clusterName": "c1"})
        assert out["spPassword"] == "***"
        assert out["clusterName"] == "c1"

    def test_nested(self):
        out = _redact_sensitive({"aksee": {"spPassword": SECRET, "machineName": "vm1"}})
        assert out["aksee"]["spPassword"] == "***"
        assert out["aksee"]["machineName"] == "vm1"

    def test_whole_subtree_redacted_under_sensitive_key(self):
        out = _redact_sensitive({"credentials": {"user": "u", "pass": "p"}})
        assert out["credentials"] == "***"

    def test_list_of_dicts(self):
        out = _redact_sensitive({"items": [{"secret": "s", "name": "n"}]})
        assert out["items"][0]["secret"] == "***"
        assert out["items"][0]["name"] == "n"

    def test_does_not_mutate_input(self):
        src = {"spPassword": SECRET}
        _redact_sensitive(src)
        assert src["spPassword"] == SECRET

    def test_boolean_toggle_not_redacted(self):
        # `enableSecretSync` matches the `secret` substring but is a toggle,
        # not a secret. A bool value must never be redacted.
        out = _redact_sensitive({"deployOptions": {"enableSecretSync": False}})
        assert out["deployOptions"]["enableSecretSync"] is False

    def test_string_secret_still_redacted_alongside_bool(self):
        out = _redact_sensitive({"enableSecretSync": True, "spPassword": SECRET})
        assert out["enableSecretSync"] is True
        assert out["spPassword"] == "***"


class TestSitesRedaction:
    def test_listing_redacts_secret(self, tmp_path, capsys):
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_site_with_secret(ws)
        cmd_sites(Namespace(selector=None, verbose=False), Orchestrator(ws))
        out = capsys.readouterr().out
        assert SECRET not in out
        assert "spPassword: ***" in out
        assert "my-cluster" in out  # non-secret still shown

    def test_verbose_listing_redacts_secret(self, tmp_path, capsys):
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_site_with_secret(ws)
        cmd_sites(Namespace(selector=None, verbose=True), Orchestrator(ws))
        out = capsys.readouterr().out
        assert SECRET not in out
        assert "spPassword: ***" in out

    def test_render_redacts_secret(self, tmp_path, capsys):
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_site_with_secret(ws)
        cmd_sites(Namespace(selector=None, verbose=False, render=True, name=None), Orchestrator(ws))
        out = capsys.readouterr().out
        assert SECRET not in out
        assert "***" in out
        assert "my-cluster" in out


class TestParamsFilePermissions:
    def test_created_with_content(self, tmp_workspace):
        ex = AzCliExecutor(workspace=tmp_workspace)
        path = ex._write_params_file({"spPassword": SECRET, "clusterName": "c"}, "step", "site")
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["parameters"]["spPassword"]["value"] == SECRET
        assert data["parameters"]["clusterName"]["value"] == "c"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file mode; Windows uses ACLs")
    def test_owner_only_permissions(self, tmp_workspace):
        ex = AzCliExecutor(workspace=tmp_workspace)
        path = ex._write_params_file({"k": "v"}, "step", "site")
        assert (path.stat().st_mode & 0o777) == 0o600

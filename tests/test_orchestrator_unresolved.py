"""Tests for the unresolved-template hard-fail safeguard in `resolve_parameters`.

This guard exists so that a malformed `{{ ... }}` reference (e.g. typo'd
step name, missing site path, unreachable output) is reported before the
literal token is sent to ARM. Three behaviors must hold:

1. Unresolved templates in template-accepted params raise `ValueError`
   in non-dry-run mode.
2. Unresolved templates in params the template does NOT accept are
   silently filtered out and do not raise (filter-then-check ordering).
3. Dry-run mode downgrades the failure to a warning so dry-run plans can
   render `{{ steps.X.outputs.Y }}` placeholders without real outputs.
4. When `filter_parameters` itself raises (e.g. Bicep build unavailable),
   the unresolved-check is skipped to avoid masking the real upstream
   failure with a misleading "unresolved templates" error.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
import yaml

from siteops.models import Manifest, Site
from siteops.orchestrator import Orchestrator


def _write_template(workspace, name: str, params: dict[str, str]) -> str:
    """Write a minimal ARM JSON template that declares the given params.

    ARM JSON is parsed in process by `get_template_parameters`, while Bicep
    shells out to the compiler. These tests assert token detection in resolved
    parameters, not compilation, so JSON keeps them fast. The Bicep path itself
    is covered by `test_unresolved_check_with_bicep_template`.
    """
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {n: {"type": t} for n, t in params.items()},
        "resources": [],
    }
    path = workspace / "templates" / f"{name}.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    return f"templates/{name}.json"


def _write_bicep_template(workspace, name: str, params: dict[str, str]) -> str:
    """Write a minimal Bicep template. Compiles via `az bicep build`."""
    body = "\n".join(f"param {n} {t}" for n, t in params.items())
    path = workspace / "templates" / f"{name}.bicep"
    path.write_text(body + "\n", encoding="utf-8")
    return f"templates/{name}.bicep"


def _make_manifest(workspace, step_name: str, template_rel: str) -> Manifest:
    manifest_data = {
        "apiVersion": "siteops/v1",
        "kind": "Manifest",
        "name": "unresolved-test",
        "sites": ["test-site"],
        "steps": [
            {
                "name": step_name,
                "template": template_rel,
                "scope": "resourceGroup",
            }
        ],
    }
    manifest_path = workspace / "manifests" / "unresolved-test.yaml"
    manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
    return Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)


def _make_site() -> Site:
    return Site(
        name="test-site",
        subscription="00000000-0000-0000-0000-000000000000",
        resource_group="rg-test",
        location="eastus",
        labels={},
    )


class TestUnresolvedTemplateGuard:
    """Hard-fail behavior for `{{ ... }}` tokens that survive resolution."""

    def test_unresolved_in_accepted_param_raises(self, tmp_workspace):
        """A surviving `{{ ... }}` in a template-accepted param must raise."""
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        # Inject a parameter file whose value references a non-existent step.
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match="Unresolved template"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

    def test_unresolved_in_filtered_out_param_does_not_raise(self, tmp_workspace):
        """A `{{ ... }}` left on a param the template does NOT accept is
        filtered out before the unresolved-check and must not raise.

        This verifies the filter-then-check ordering: common.yaml-injected
        defaults (e.g. `siteAddress.country`) targeting non-consuming steps
        must not break deployment.
        """
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {
            "name": "valid-value",
            "extraneous": "{{ steps.unrelated.outputs.id }}",
        }

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        params = orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

        assert params == {"name": "valid-value"}
        assert "extraneous" not in params

    def test_unresolved_warns_in_dry_run(self, tmp_workspace, caplog):
        """A dry run downgrades the check to a warning for a step output.

        The reference names an earlier step in the same manifest, which is the
        one case a dry run genuinely cannot resolve, since no step has run.
        """
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest_data = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "unresolved-test",
            "sites": ["test-site"],
            "steps": [
                {"name": "produce", "template": template_rel, "scope": "resourceGroup"},
                {"name": "deploy", "template": template_rel, "scope": "resourceGroup"},
            ],
        }
        manifest_path = tmp_workspace / "manifests" / "unresolved-test.yaml"
        manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        site = _make_site()
        site.parameters = {"name": "{{ steps.produce.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace, dry_run=True)
        step = manifest.steps[1]

        with caplog.at_level(logging.WARNING, logger="siteops.orchestrator"):
            params = orchestrator.resolve_parameters(
                step, site, manifest, step_outputs={}
            )

        assert any("Unresolved template" in r.message for r in caplog.records)
        # Dry-run preserves the literal token rather than raising.
        assert "{{ steps.produce.outputs.id }}" in str(params)

    def test_a_dry_run_fails_on_a_step_that_does_not_exist(self, tmp_workspace):
        """A reference to a step no manifest declares resolves in neither a dry
        run nor a real one, so the dry run has nothing to excuse."""
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {"name": "{{ steps.does-not-exist.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator.resolve_parameters(
                manifest.steps[0], site, manifest, step_outputs={}
            )

    def test_a_dry_run_fails_on_a_step_that_runs_later(self, tmp_workspace):
        """A forward reference cannot resolve either, since the producing step
        has not run by the time this one needs the value."""
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest_data = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "unresolved-test",
            "sites": ["test-site"],
            "steps": [
                {"name": "first", "template": template_rel, "scope": "resourceGroup"},
                {"name": "later", "template": template_rel, "scope": "resourceGroup"},
            ],
        }
        manifest_path = tmp_workspace / "manifests" / "unresolved-test.yaml"
        manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
        manifest = Manifest.from_file(manifest_path, workspace_root=manifest_path.parent)

        site = _make_site()
        site.parameters = {"name": "{{ steps.later.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator.resolve_parameters(
                manifest.steps[0], site, manifest, step_outputs={}
            )

    def test_filter_failure_skips_unresolved_check(self, tmp_workspace, caplog):
        """When `filter_parameters` raises, skip the unresolved-check so the
        filter failure surfaces instead of being masked by a misleading
        'unresolved templates' error.
        """
        template_rel = _write_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        # Param contains an unresolved token that *would* trip the check if
        # filtering had run successfully.
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with patch(
            "siteops.orchestrator.filter_parameters",
            side_effect=ValueError("simulated bicep build failure"),
        ):
            with caplog.at_level(logging.WARNING, logger="siteops.orchestrator"):
                # Must NOT raise ValueError("Unresolved template ..."); the
                # caller is expected to surface the underlying filter error
                # at the deployment step instead.
                params = orchestrator.resolve_parameters(
                    step, site, manifest, step_outputs={}
                )

        # Filter warning was emitted and explicitly mentions the precheck skip.
        assert any(
            "Could not filter parameters" in r.message
            and "skipping unresolved-template precheck" in r.message
            for r in caplog.records
        )
        # No "Unresolved template" error was raised or logged.
        assert not any("Unresolved template" in r.message for r in caplog.records)
        # Unfiltered token is preserved (caller's deploy will surface root cause).
        assert "{{ steps.missing.outputs.id }}" in str(params)

    def test_unresolved_in_nested_list_raises(self, tmp_workspace):
        """Unresolved tokens inside a list value are detected (recursive walk).

        Bicep array params (e.g. tags, allowlists) accept lists, and an
        unresolved template buried inside one would otherwise reach ARM as a
        literal string element.
        """
        template_rel = _write_template(tmp_workspace, "accepts-tags", {"tags": "array"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {"tags": ["ok", "{{ steps.missing.outputs.id }}", "also-ok"]}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match=r"Unresolved template.*tags\[1\]"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

    def test_unresolved_in_nested_dict_raises(self, tmp_workspace):
        """Unresolved tokens inside an object value are detected (recursive walk)."""
        template_rel = _write_template(tmp_workspace, "accepts-config", {"config": "object"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {
            "config": {
                "outer": "fine",
                "nested": {"inner": "{{ steps.missing.outputs.id }}"},
            }
        }

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match=r"Unresolved template.*config\.nested\.inner"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})

    def test_unresolved_check_with_bicep_template(self, tmp_workspace):
        """The guard behaves identically when the parameter surface comes from a
        compiled Bicep template rather than ARM JSON.

        The other tests in this class use ARM JSON so they do not pay a Bicep
        compile each. This one keeps real `az bicep build` extraction covered.
        `az_path` is asserted first so a missing CLI reports itself. Without it
        the compile fails, the guard is skipped rather than reached, and this
        reports as `DID NOT RAISE`, which points at the guard instead of the
        absent tool.
        """
        from tests.workspace.conftest import az_path

        az_path()
        template_rel = _write_bicep_template(tmp_workspace, "accepts-name", {"name": "string"})
        manifest = _make_manifest(tmp_workspace, "deploy", template_rel)
        site = _make_site()
        site.parameters = {"name": "{{ steps.missing.outputs.id }}"}

        orchestrator = Orchestrator(tmp_workspace)
        step = manifest.steps[0]

        with pytest.raises(ValueError, match="Unresolved template"):
            orchestrator.resolve_parameters(step, site, manifest, step_outputs={})


class TestTemplatedParameterKeys:
    """A template in a parameter KEY resolves, and is caught when it cannot.

    Resolution mapped `{k: resolve(v)}`, so a key kept its braces. The
    fail-closed guard walked values only, so it did not catch that either. The
    one check whose job is stopping an unresolved template from reaching ARM
    could not see this class at all.
    """

    def _site(self) -> Site:
        return Site(
            name="chicago",
            subscription="00000000-0000-0000-0000-000000000000",
            resource_group="rg-chicago",
            location="eastus",
            labels={"environment": "dev"},
            properties={"endpoints": [{"host": "10.0.0.1"}]},
            parameters={"clusterName": "aio-chicago"},
        )

    def test_a_templated_key_resolves(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace)

        resolved = orchestrator._resolve_template_strings(
            {"{{ site.name }}": "value"}, self._site()
        )

        assert resolved == {"chicago": "value"}

    def test_a_templated_key_resolves_at_depth(self, tmp_workspace):
        """Nested mappings go through the same path, so depth is not special."""
        orchestrator = Orchestrator(tmp_workspace)

        resolved = orchestrator._resolve_template_strings(
            {"outer": {"{{ site.labels.environment }}": "value"}}, self._site()
        )

        assert resolved == {"outer": {"dev": "value"}}

    def test_key_and_value_resolve_together(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace)

        resolved = orchestrator._resolve_template_strings(
            {"{{ site.name }}": "{{ site.location }}"}, self._site()
        )

        assert resolved == {"chicago": "eastus"}

    def test_a_key_collision_is_rejected(self, tmp_workspace):
        """Keeping the last one would drop a value the operator wrote."""
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match=r"both resolve to the same name"):
            orchestrator._resolve_template_strings(
                {"{{ site.name }}": "first", "chicago": "second"}, self._site()
            )

    def test_a_key_resolving_to_an_object_is_rejected(self, tmp_workspace):
        """A whole-object template is legal in a value and not in a key."""
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match=r"cannot be a name"):
            orchestrator._resolve_template_strings(
                {"{{ site.properties.endpoints }}": "value"}, self._site()
            )

    def test_the_guard_catches_a_key_it_could_not_resolve(self, tmp_workspace):
        """Defense in depth. A key naming something the site lacks survives
        resolution, and the guard is the last thing before ARM."""
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match=r"Unresolved template.*\(key\)"):
            orchestrator._check_unresolved_templates(
                {"{{ site.labels.missing }}": "value"}, "chicago", "step-1"
            )

    def test_an_untemplated_key_is_untouched(self, tmp_workspace):
        """The common case pays nothing, including for a non-string key."""
        orchestrator = Orchestrator(tmp_workspace)

        resolved = orchestrator._resolve_template_strings(
            {"plainKey": "value", 7: "int-key"}, self._site()
        )

        assert resolved == {"plainKey": "value", 7: "int-key"}

    def test_step_output_keys_resolve_too(self, tmp_workspace):
        """`_resolve_step_outputs` had the same shape and the same gap."""
        orchestrator = Orchestrator(tmp_workspace)

        resolved = orchestrator._resolve_step_outputs(
            {"{{ steps.first.outputs.clusterName }}": "value"},
            {"first": {"clusterName": "arc-chicago"}},
            None,
            None,
        )

        assert resolved == {"arc-chicago": "value"}


class TestTemplatedKeysThroughResolveParameters:
    """Key resolution through the real entry point, not the helpers.

    The helper tests call `_resolve_template_strings` directly. Leaving the
    helpers correct while `resolve_parameters` resolved only values left those
    tests green and a real parameter set empty, so these go through the path a
    deployment actually takes, including filtering.
    """

    def _site(self) -> Site:
        return Site(
            name="seattle-dev",
            subscription="00000000-0000-0000-0000-000000000000",
            resource_group="rg-seattle",
            location="westus",
            labels={"environment": "dev"},
            properties={},
            parameters={},
        )

    def _resolve(self, workspace, params_yaml: str, declared: list[str]):
        (workspace / "parameters").mkdir(exist_ok=True)
        (workspace / "parameters" / "p.yaml").write_text(params_yaml, encoding="utf-8")
        template_rel = _write_template(workspace, "t", {n: "object" for n in declared})
        manifest_data = {
            "apiVersion": "siteops/v1",
            "kind": "Manifest",
            "name": "keys",
            "sites": ["seattle-dev"],
            "steps": [
                {
                    "name": "step1",
                    "template": template_rel,
                    "parameters": ["parameters/p.yaml"],
                }
            ],
        }
        manifest_path = workspace / "manifests" / "keys.yaml"
        manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
        orchestrator = Orchestrator(workspace)
        return orchestrator.resolve_parameters(
            manifest.steps[0], self._site(), manifest, step_outputs={}
        )

    def test_a_nested_templated_key_survives_to_the_deployment(self, tmp_workspace):
        """The documented shape. The outer name is declared by the template,
        so filtering keeps it and the inner name carries the site value."""
        resolved = self._resolve(
            tmp_workspace,
            'siteRoles:\n  "{{ site.name }}":\n    role: primary\n',
            ["siteRoles"],
        )

        assert resolved == {"siteRoles": {"seattle-dev": {"role": "primary"}}}

    def test_a_top_level_name_that_cannot_resolve_fails_closed(self, tmp_workspace):
        """Filtering drops a name the template does not declare, and a name
        with braces in it matches none, so without a check before filtering
        this deployed defaults and reported success."""
        with pytest.raises(ValueError, match=r"Unresolved template.*parameter name"):
            self._resolve(
                tmp_workspace,
                '"{{ site.parameters.nope }}":\n  role: primary\n',
                ["clusterName"],
            )

    def test_a_nested_name_that_cannot_resolve_fails_closed(self, tmp_workspace):
        """Caught after filtering, with the full path in the message."""
        with pytest.raises(ValueError, match=r"Unresolved template.*\(key\)"):
            self._resolve(
                tmp_workspace,
                'siteRoles:\n  "{{ site.parameters.nope }}":\n    role: primary\n',
                ["siteRoles"],
            )

    def test_a_resolvable_top_level_name_is_not_reported(self, tmp_workspace):
        """The guard is about names that did not resolve. One that resolved is
        subject to ordinary filtering, like any other name."""
        resolved = self._resolve(
            tmp_workspace, '"{{ site.name }}":\n  role: primary\n', ["seattle-dev"]
        )

        assert resolved == {"seattle-dev": {"role": "primary"}}


class TestMalformedTemplateDelimiters:
    """A token with one brace mistyped is still an unresolved template.

    Both guards used to require `{{` and `}}` together, so a mistyped closing
    brace resolved to nothing, matched no declared parameter, was filtered out,
    and deployed defaults while reporting success. That is the silent-default
    class the guards exist to stop.
    """

    def _site(self) -> Site:
        return Site(
            name="seattle-dev",
            subscription="00000000-0000-0000-0000-000000000000",
            resource_group="rg-seattle",
            location="westus",
            labels={},
            properties={},
            parameters={},
        )

    @pytest.mark.parametrize(
        "text",
        [
            "{{ site.name }}",
            "{{ site.name }",
            "{ site.name }}",
            "{{ steps.a.outputs.b }",
            "{ steps.resolve-aio.outputs.aioInstanceName }}",
            "{ site.properties.endpoints[0].host }}",
            "{ site.labels.cost-center }}",
        ],
        ids=[
            "complete",
            "missing-closer",
            "missing-opener",
            "step-output-missing-closer",
            "hyphenated-step-name",
            "indexed-path",
            "hyphenated-label",
        ],
    )
    def test_a_malformed_token_is_reported(self, tmp_workspace, text):
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator._check_unresolved_templates({"p": text}, "seattle-dev", "step-1")

    @pytest.mark.parametrize(
        "text",
        [
            "seattle-dev",
            "{'a': {'b': 1}}",
            "[{'x': 1}]",
            "a } b { c",
            "rg-plant-01",
            "{'sites': {'seattle': 'dev'}}",
            "{'site': 'seattle'}",
        ],
        ids=[
            "plain",
            "nested-mapping",
            "nested-list",
            "loose-braces",
            "name",
            "nested-mapping-named-sites",
            "quoted-key-named-site",
        ],
    )
    def test_data_rendered_into_a_string_is_not_reported(self, tmp_workspace, text):
        """Nested data ends in `}}`, and reporting it would block a deployment
        the operator wrote correctly. That is worse than the drop being fixed."""
        orchestrator = Orchestrator(tmp_workspace)

        orchestrator._check_unresolved_templates({"p": text}, "seattle-dev", "step-1")

    def test_a_malformed_name_is_reported_before_filtering(self, tmp_workspace):
        """The name guard runs first, so it needs the same scanner."""
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(ValueError, match=r"parameter name"):
            orchestrator._check_unresolved_keys(
                {"{{ site.name }": "value"}, "seattle-dev", "step-1"
            )

    def test_a_committed_workspace_reports_nothing(self, tmp_workspace):
        """The scanner runs on every deployment, so a false positive blocks a
        real deploy. This pins the widened rule against realistic values."""
        orchestrator = Orchestrator(tmp_workspace)

        orchestrator._check_unresolved_templates(
            {
                "clusterName": "aio-seattle",
                "tags": {"env": "dev", "owner": "platform"},
                "endpoints": ["10.0.0.1:4840", "opc.tcp://plc:4840"],
                "config": "{'memoryProfile': 'Low'}",
            },
            "seattle-dev",
            "step-1",
        )


class TestUnresolvedGuardsInDryRun:
    """A dry run warns where a real run fails.

    A dry run cannot resolve `{{ steps.X.outputs.Y }}`, because no step has
    produced an output yet, so failing there would make dry run useless on any
    manifest that chains. A real run has no such excuse. Neither half was
    pinned: replacing the warn with a raise, and replacing it with a silent
    return, both left the suite green.

    The excuse is that one case and no other. A `{{ site.X }}` path that did
    not resolve, and a mistyped delimiter, fail identically on the real
    deployment, so a dry run that passed them reported success for a run that
    was going to fail.
    """

    def _params(self) -> dict:
        return {"{{ steps.first.outputs.name }}": "value"}

    def test_a_dry_run_warns_and_continues(self, tmp_workspace, caplog):
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with caplog.at_level(logging.WARNING):
            orchestrator._check_unresolved_keys(self._params(), "site-a", "step-1")

        assert any("parameter name" in r.message for r in caplog.records), (
            "a dry run must still report the name, it just must not fail"
        )

    def test_a_real_run_fails(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace, dry_run=False)

        with pytest.raises(ValueError, match=r"parameter name"):
            orchestrator._check_unresolved_keys(self._params(), "site-a", "step-1")

    def test_a_dry_run_warns_for_a_value_too(self, tmp_workspace, caplog):
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with caplog.at_level(logging.WARNING):
            orchestrator._check_unresolved_templates(
                {"p": "{{ steps.first.outputs.name }}"}, "site-a", "step-1"
            )

        assert any("Unresolved template" in r.message for r in caplog.records)

    def test_a_real_run_fails_for_a_value_too(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace, dry_run=False)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator._check_unresolved_templates(
                {"p": "{{ steps.first.outputs.name }}"}, "site-a", "step-1"
            )

    def test_a_clean_payload_is_silent_either_way(self, tmp_workspace, caplog):
        for dry_run in (True, False):
            orchestrator = Orchestrator(tmp_workspace, dry_run=dry_run)
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                orchestrator._check_unresolved_keys({"plain": "value"}, "site-a", "step-1")
                orchestrator._check_unresolved_templates({"plain": "value"}, "site-a", "step-1")

            assert not caplog.records

    @pytest.mark.parametrize(
        "value",
        [
            "{{ site.parameters.typo }}",
            "{{ site.name }",
            "{ site.name }}",
            "{ steps.resolve-aio.outputs.id }}",
            "prefix-{{ steps.first.outputs.name }}-{{ site.parameters.typo }}",
        ],
        ids=[
            "site-path",
            "site-path-missing-closer",
            "malformed-site",
            "malformed-step",
            "step-output-beside-a-site-typo",
        ],
    )
    def test_a_dry_run_fails_on_what_a_real_run_would_also_fail_on(
        self, tmp_workspace, value
    ):
        """A dry run is what a CI gate runs to predict the deployment. Warning
        here let the gate pass and the deployment fail on the same input."""
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator._check_unresolved_templates({"p": value}, "site-a", "step-1")

    def test_one_bad_parameter_beside_a_step_output_still_fails_a_dry_run(
        self, tmp_workspace
    ):
        """The excuse applies per token, so a payload has to be excusable as a
        whole. Split across two parameters, since a single value containing
        both cannot tell the two quantifiers apart."""
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with pytest.raises(ValueError, match=r"Unresolved template"):
            orchestrator._check_unresolved_templates(
                {
                    "chained": "{{ steps.first.outputs.name }}",
                    "typo": "{{ site.parameters.typo }}",
                },
                "site-a",
                "step-1",
            )

    @pytest.mark.parametrize(
        "name",
        ["{{ site.parameters.typo }}", "{ site.name }}"],
        ids=["site-path", "malformed"],
    )
    def test_a_dry_run_fails_on_a_parameter_name_a_real_run_would_reject(
        self, tmp_workspace, name
    ):
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with pytest.raises(ValueError, match=r"parameter name"):
            orchestrator._check_unresolved_keys({name: "value"}, "site-a", "step-1")

    def test_a_dry_run_still_excuses_several_step_outputs_together(
        self, tmp_workspace, caplog
    ):
        """The excuse is per token, so a payload of nothing but step outputs
        stays excused however many there are."""
        orchestrator = Orchestrator(tmp_workspace, dry_run=True)

        with caplog.at_level(logging.WARNING):
            orchestrator._check_unresolved_templates(
                {
                    "a": "{{ steps.first.outputs.name }}",
                    "b": "https://{{ steps.second.outputs.host }}/path",
                },
                "site-a",
                "step-1",
            )

        assert any("Unresolved template" in r.message for r in caplog.records)

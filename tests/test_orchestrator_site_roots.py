"""Operator Site roots remain independent of deployment content."""

import builtins
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from siteops.compilation import TemplateCompilationSession
from siteops.executor import AzCliExecutor, DeploymentResult
from siteops.models import Manifest
from siteops.orchestrator import Orchestrator
from siteops.planning import PlanIntent


def _write(root, path, data):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


def _site(name, marker):
    return {
        "apiVersion": "siteops/v1", "kind": "Site", "name": name,
        "subscription": "00000000-0000-0000-0000-000000000000",
        "resourceGroup": "source-group", "location": "westus2",
        "labels": {"environment": "test"}, "parameters": {"marker": marker},
    }


@pytest.fixture
def roots(tmp_path):
    content = tmp_path / "content"
    project = tmp_path / "project"
    _write(content, "sites/packaged.yaml", _site("packaged", "content-example"))
    _write(content, "sites/target.yaml", _site("target", "content-target"))
    _write(content, "sites.local/target.yaml", {"parameters": {"marker": "content-overlay"}})
    _write(project, "sites/target.yaml", _site("target", "project-target"))
    _write(project, "sites.local/target.yaml", {
        "resourceGroup": "project-group", "parameters": {"marker": "project-overlay"},
    })
    manifest = _write(content, "manifests/deploy.yaml", {
        "apiVersion": "siteops/v1", "kind": "Manifest", "name": "deploy",
        "selector": "environment=test",
        "steps": [{"name": "resource", "template": "templates/resource.json", "scope": "resourceGroup"}],
    })
    template = content / "templates" / "resource.json"
    template.parent.mkdir()
    template.write_text(json.dumps({
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {"marker": {"type": "string"}},
        "resources": [],
    }), encoding="utf-8")
    return content, project, manifest


def test_separate_site_root_excludes_content_examples_and_overlays(roots, monkeypatch):
    content, project, path = roots
    original = builtins.open

    def guarded(path, *args, **kwargs):
        if isinstance(path, (str, Path)):
            resolved = Path(path).resolve()
            assert not resolved.is_relative_to(content / "sites")
            assert not resolved.is_relative_to(content / "sites.local")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded)
    engine = Orchestrator(content, site_config_root=project)
    manifest = Manifest.from_file(path, workspace_root=content)
    sites = engine.resolve_sites(manifest)
    assert [site.name for site in sites] == ["target"]
    assert sites[0].parameters["marker"] == "project-overlay"
    assert sites[0].resource_group == "project-group"
    assert engine.workspace == content
    assert engine.site_config_root == project


def test_default_site_root_preserves_local_workspace_behavior(roots):
    content, _, path = roots
    engine = Orchestrator(content)
    manifest = Manifest.from_file(path, workspace_root=content)
    assert {site.name for site in engine.resolve_sites(manifest)} == {"packaged", "target"}
    assert engine.load_site("target").parameters["marker"] == "content-overlay"


def test_empty_operator_root_does_not_fall_back_to_packaged_sites(roots, tmp_path):
    content, _, path = roots
    empty = tmp_path / "empty-project"
    empty.mkdir()
    engine = Orchestrator(content, site_config_root=empty)
    manifest = Manifest.from_file(path, workspace_root=content)
    assert engine.resolve_sites(manifest) == []


def test_missing_operator_root_fails_explicitly(roots, tmp_path):
    content, _, _ = roots
    with pytest.raises(FileNotFoundError, match="configuration root"):
        Orchestrator(content, site_config_root=tmp_path / "absent")


def test_inheritance_fallback_uses_operator_templates_not_content_templates(roots, tmp_path):
    content, project, _ = roots
    base = {
        "apiVersion": "siteops/v1", "kind": "SiteTemplate",
        "parameters": {"marker": "operator-base"},
    }
    _write(project, "sites/base.yaml", base)
    _write(content, "sites/base.yaml", {**base, "parameters": {"marker": "content-base"}})
    extra = tmp_path / "extra"
    child = _site("extra", "override")
    child.pop("parameters")
    child["inherits"] = "base.yaml"
    _write(extra, "extra.yaml", child)
    engine = Orchestrator(content, site_config_root=project, extra_trusted_sites_dirs=[extra])
    assert engine.load_site("extra").parameters["marker"] == "operator-base"


@pytest.mark.parametrize("relative", ["sites", "sites.local"])
def test_extra_directory_collision_guards_follow_operator_root(roots, relative):
    content, project, _ = roots
    with pytest.raises(ValueError):
        Orchestrator(
            content, site_config_root=project, extra_trusted_sites_dirs=[project / relative],
        )


def test_site_provenance_labels_are_relative_to_operator_root(roots):
    content, project, _ = roots
    engine = Orchestrator(content, site_config_root=project)
    assert engine._origin_label(project / "sites" / "target.yaml") == "sites/target.yaml"
    assert engine._origin_label(project / "sites.local" / "target.yaml") == "sites.local/target.yaml"


def test_plan_and_deploy_use_operator_inputs_with_content_templates(roots, tmp_path, monkeypatch):
    content, project, manifest = roots
    calls = []

    def version_only(argv, timeout):
        assert argv[1:] == ("version", "--output", "json")
        return subprocess.CompletedProcess(argv, 0, '{"azure-cli":"test"}', "")

    monkeypatch.setattr(
        "siteops.orchestrator.TemplateCompilationSession",
        lambda: TemplateCompilationSession(
            command_runner=version_only,
            tool_resolver=lambda name: str(tmp_path / "tools" / "az.exe"),
        ),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected live process"))

    def submit(self, **kwargs):
        calls.append(kwargs)
        return DeploymentResult(
            success=True, step_name=kwargs["step_name"], site_name=kwargs["site_name"],
            deployment_name=kwargs["deployment_name"],
        )

    monkeypatch.setattr(AzCliExecutor, "deploy_resource_group", submit)
    engine = Orchestrator(content, site_config_root=project)
    plan = engine.build_plan(manifest, intent=PlanIntent.EXECUTABLE)
    assert plan.status.value == "planned"
    assert [target.name for target in plan.plan.targets] == ["target"]
    assert not calls
    result = engine.deploy(manifest)
    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["resource_group"] == "project-group"
    assert calls[0]["parameters"]["marker"] == "project-overlay"
    assert calls[0]["template_path"] == content / "templates" / "resource.json"

"""Inspect real installed planning parameters without deploying or using source imports."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from siteops import __version__
from tests.installed_runtime import build_engine_wheel, install_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def fleet_app(tmp_path_factory):
    root = tmp_path_factory.mktemp("fleet-plan")
    source = root / "fixture-workspace"
    for folder in ("manifests", "templates", "parameters/aio-releases"):
        source.joinpath(*folder.split("/")).mkdir(parents=True, exist_ok=True)
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "resources": [],
        "parameters": {name: {"type": kind} for name, kind in {
            "clusterName": "string", "aioVersion": "string", "aioApiVersion": "string", "brokerConfig": "object",
            "aioReleaseConfiguration": "object", "certManagerVersion": "string", "secretStoreVersion": "string",
        }.items()},
    }
    (source / "templates" / "main.json").write_text(json.dumps(template))
    (source / "parameters" / "common.yaml").write_text(
        'clusterName: "{{ site.parameters.clusterName }}"\nbrokerConfig: "{{ site.parameters.brokerConfig }}"\n',
    )
    for release, version in (("2607", "1.4.41"), ("2608", "1.4.73")):
        (source / "parameters" / "aio-releases" / f"{release}.yaml").write_text(
            yaml.safe_dump({"aioVersion": version, "aioApiVersion": "2026-07-01",
                            "aioReleaseConfiguration": {} if release == "2607" else {"extension": {"wasmGraphControllerMqttTrust": True}},
                            "certManagerVersion": "0.14.0" if release == "2607" else "1.0.0",
                            "secretStoreVersion": "1.5.1" if release == "2607" else "1.5.2"}),
        )
    steps = []
    for name in (
        "global-edge-site", "edge-site", "schema-registry", "adr-ns", "aio-enablement",
        "aio-instance", "schema-registry-role", "resolve-aio", "secretsync",
    ):
        row = {"name": name, "template": "templates/main.json",
               "scope": "subscription" if name == "global-edge-site" else "resourceGroup"}
        if name in {"global-edge-site", "edge-site", "resolve-aio", "secretsync"}:
            row["when"] = "{{ site.properties.deployOptions.enableSecretSync }}"
        steps.append(row)
    (source / "manifests" / "aio-install.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Manifest", "name": "aio-install",
        "selector": "environment=dev", "parallel": 3,
        "parameters": ["parameters/common.yaml", "parameters/aio-releases/{{ site.properties.aioRelease }}.yaml"],
        "steps": steps,
    }))
    wheel = build_engine_wheel(root)
    app = install_engine(root / "runtime", wheel)
    result = subprocess.run(
        [str(app.python), "-I", str(ROOT / "tests" / "fixtures" / "prepare_installed_project.py"),
         str(app.root), str(source)], cwd=app.root / "unrelated", env=app.environment,
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (app.root / "project" / "sites" / "one.yaml").unlink()
    targets = []
    for slot, release in (("one", "2607"), ("two", "2608")):
        target = {"slot": slot, "release": release, "name": f"fleet-{slot}",
                  "subscription": "00000000-0000-0000-0000-000000000001",
                  "resourceGroup": f"private-group-{slot}", "cluster": f"private-cluster-{slot}"}
        (app.root / "project" / "sites" / f"fleet-{slot}.yaml").write_text(yaml.safe_dump({
            "apiVersion": "siteops/v1", "kind": "Site", "name": target["name"],
            "subscription": target["subscription"], "resourceGroup": target["resourceGroup"],
            "location": "eastus", "labels": {"environment": "fleet"},
            "properties": {"aioRelease": release, "deployOptions": {"enableSecretSync": False}},
            "parameters": {"clusterName": target["cluster"], "brokerConfig": {"memoryProfile": "Low"}},
        }))
        targets.append(target)
    (app.root / "project" / "sites" / "sentinel.yaml").write_text(yaml.safe_dump({
        "apiVersion": "siteops/v1", "kind": "Site", "name": "fleet-unselected",
        "subscription": "sentinel-sub", "resourceGroup": "sentinel-group", "location": "eastus",
        "labels": {"environment": "dev"},
    }))
    return app, targets


@pytest.mark.parametrize("fault", [None, "engine", "target", "release", "cluster"])
def test_installed_probe_observes_the_real_planner_and_keeps_values_private(fleet_app, fault):
    app, original = fleet_app
    targets = json.loads(json.dumps(original))
    if fault == "target":
        targets[0]["resourceGroup"] = "other"
    elif fault == "release":
        targets[0]["release"] = "2608"
    elif fault == "cluster":
        targets[0]["cluster"] = "other"
    name = fault or "valid"
    spec = {
        "engineVersion": "99.0.0" if fault == "engine" else __version__,
        "project": str(app.root / "project"), "policy": str(app.root / "policy.json"),
        "trustedRoot": str(app.root / "trusted-root.json"), "targets": targets,
        "privateOutput": str(app.root / f"{name}-plan.json"),
    }
    path = app.root / f"{name}-spec.json"
    path.write_text(json.dumps(spec))
    result = subprocess.run(
        [str(app.python), "-I", str(ROOT / "scripts" / "probe-fleet-plan.py"), "--spec", str(path),
         "--expected-spec-sha", hashlib.sha256(path.read_bytes()).hexdigest()],
        cwd=app.root / "unrelated", env=app.environment, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == (1 if fault else 0), result.stdout + result.stderr
    assert "private-group" not in result.stdout + result.stderr
    assert "private-cluster" not in result.stdout + result.stderr
    if not fault:
        report = json.loads(result.stdout)
        assert report["targetCount"] == 2 and report["parallel"] == 2
        assert report["slots"]["one"]["version"] == "1.4.41"
        assert report["slots"]["two"]["version"] == "1.4.73"

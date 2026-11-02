"""Extension version and train overrides reach install and upgrade, and nothing else moves.

Sites and SiteTemplates can set the six keys under `parameters`. They outrank
the AIO release file, which stays the source of the API version and the
release configuration.
"""

import pytest
import yaml

from siteops.planning import resolve_plan_value
from tests.workspace.test_aio_extension_settings import _parameters, _plan, _release

OVERRIDES = {
    "aioVersion": "1.4.200",
    "aioTrain": "preview",
    "secretStoreVersion": "1.5.9",
    "secretStoreTrain": "preview",
    "certManagerVersion": "1.1.9",
    "certManagerTrain": "preview",
}
INSTALL_STEPS = {
    "aio-instance": ("aioVersion", "aioTrain"),
    "aio-enablement": ("secretStoreVersion", "secretStoreTrain", "certManagerVersion", "certManagerTrain"),
}
UPGRADE_STEPS = {
    "check-upgrade-path": ("aioVersion", "aioTrain"),
    "update-extensions": tuple(OVERRIDES),
}


def _project(tmp_path, holder: str):
    project = tmp_path / "project"
    (project / "sites").mkdir(parents=True)
    fleet = {
        "apiVersion": "siteops/v1", "kind": "SiteTemplate", "name": "fleet",
        "properties": {"aioRelease": "2609", "deployOptions": {
            "enableGlobalSite": False, "enableEdgeSite": False, "enableSecretSync": False,
            "enableCertManager": True,
        }},
        "parameters": dict(OVERRIDES) if holder == "template" else {},
    }
    site = {
        "apiVersion": "siteops/v1", "kind": "Site", "name": "plant", "inherits": "fleet.yaml",
        "subscription": "00000000-0000-0000-0000-000000000001", "resourceGroup": "rg-existing",
        "location": "eastus",
        "parameters": {"clusterName": "existing-arc", **(OVERRIDES if holder == "site" else {})},
    }
    (project / "sites" / "fleet.yaml").write_text(yaml.safe_dump(fleet), encoding="utf-8")
    (project / "sites" / "plant.yaml").write_text(yaml.safe_dump(site), encoding="utf-8")
    return project


@pytest.mark.parametrize("holder", ["site", "template"])
@pytest.mark.parametrize(("manifest", "steps"), [
    pytest.param("aio-install", INSTALL_STEPS, id="install"),
    pytest.param("aio-upgrade", UPGRADE_STEPS, id="upgrade"),
])
def test_each_override_reaches_its_template_and_the_release_keeps_its_api(
    workspace, tmp_path, monkeypatch, holder, manifest, steps,
):
    project = _project(tmp_path, holder)
    release = _release(workspace, "2609")
    plan = _plan(workspace, tmp_path, manifest, monkeypatch, selector="name=plant", site_config_root=project)
    for step, keys in steps.items():
        parameters = _parameters(plan, step)
        for key in keys:
            assert resolve_plan_value(parameters[key], {}) == OVERRIDES[key], (step, key)
        for key in ("aioApiVersion", "aioReleaseConfiguration"):
            if key in parameters:
                assert resolve_plan_value(parameters[key], {}) == release[key], (step, key)
    instance = _parameters(plan, "aio-instance" if manifest == "aio-install" else "update-extensions")
    assert resolve_plan_value(instance["aioApiVersion"], {}) == release["aioApiVersion"]
    assert resolve_plan_value(instance["aioReleaseConfiguration"], {}) == release["aioReleaseConfiguration"]


def test_removing_an_override_restores_the_release_values(workspace, tmp_path, monkeypatch):
    project = _project(tmp_path, "none")
    release = _release(workspace, "2609")
    plan = _plan(workspace, tmp_path, "aio-upgrade", monkeypatch, selector="name=plant", site_config_root=project)
    update = _parameters(plan, "update-extensions")
    for key in OVERRIDES:
        assert resolve_plan_value(update[key], {}) == release[key], key


def test_the_guide_documents_each_override(workspace):
    root = workspace.parents[1]
    guide = (root / "docs" / "aio-releases.md").read_text(encoding="utf-8")
    section = guide.split("## Pin an extension version or train", 1)[1].split("\n## ", 1)[0]
    for key in OVERRIDES:
        assert f"{key}:" in section, key
    for readme in ("aio-install", "aio-upgrade"):
        text = (workspace / "manifests" / readme / "README.md").read_text(encoding="utf-8")
        assert "docs/aio-releases.md#pin-an-extension-version-or-train" in text, readme

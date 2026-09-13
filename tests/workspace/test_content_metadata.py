"""Authored guidance covers the actual workspace without defining execution."""

import yaml

from siteops.browse import inspect_content
from tests.workspace.test_manifest_validation import _all_manifest_files


def test_inventory_covers_actual_manifests_and_classifies_their_roles(workspace):
    result = inspect_content(workspace, include_partials=True)
    assert not result.diagnostics
    expected = {
        path.relative_to(workspace).as_posix(): path
        for path in _all_manifest_files(workspace)
    }
    assert {entry.path for entry in result.entries} == expected.keys()
    for entry in result.entries:
        assert entry.metadata_status == "declared"
        assert entry.guidance.role == (
            "partial" if expected[entry.path].name.startswith("_") else "standalone"
        )
        assert entry.guidance.coverage
        for reference in entry.guidance.documentation:
            assert (workspace / reference.split("#", 1)[0]).is_file()
        for row in (*tuple(entry.guidance.inputs or ()), *tuple(entry.guidance.supplied or ())):
            if row.source:
                assert (workspace / row.source.split("#", 1)[0]).is_file()
    normal = inspect_content(workspace)
    assert {entry.path for entry in normal.entries} == {
        path for path, file in expected.items() if not file.name.startswith("_")
    }


def test_first_journey_inputs_match_its_actual_site_and_selection(workspace):
    site = yaml.safe_load((workspace / "sites" / "catalog-basic.yaml").read_text(encoding="utf-8"))
    assert site["properties"]["resourceSets"]["dataflows"] == ["basic-routing"]
    assert (workspace / "resource-sets" / "dataflows" / "basic-routing.yaml").is_file()
    for name in ("aio-install", "resource-set-basic"):
        result = inspect_content(workspace, name)
        assert not result.diagnostics
        entry = result.entries[0]
        fields = {row.field for row in entry.guidance.inputs}
        assert {"subscription", "resourceGroup", "location", "properties.aioRelease"} <= fields
        assert entry.guidance.prerequisites
        assert entry.guidance.effects
        assert entry.guidance.removal
    install = inspect_content(workspace, "aio-install").entries[0]
    assert "parameters.clusterName" in {row.field for row in install.guidance.inputs}
    basic = inspect_content(workspace, "resource-set-basic").entries[0]
    assert "properties.resourceSets.dataflows" in {row.field for row in basic.guidance.inputs}
    assert basic.selector == "environment=sample,sample=resource-set-basic"
    assert install.selector == "environment=dev"

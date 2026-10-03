"""Distinguish integration markers from ordinary test names and parameter IDs."""

import pytest

from tests.integration import conftest as integration

pytest_plugins = ("pytester",)


@pytest.fixture
def collection_suite(pytester, monkeypatch):
    for name in (
        "GITHUB_ACTIONS", "TF_BUILD", "SITE_OVERRIDES", "SITEOPS_EXTRA_SITES_DIRS",
        "SITEOPS_E2E_UPGRADE_PHASE", "INTEGRATION_SKIP_CLEANUP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(integration, "WORKSPACE_PATH", pytester.path / "workspace")
    monkeypatch.setattr(integration, "_generated_overlays", False)
    monkeypatch.setattr(integration, "_pre_existing_overlays", set())
    pytester.makeini("[pytest]\nmarkers = integration: requires provider configuration\n")
    pytester.makeconftest(
        "from tests.integration.conftest import pytest_collection_finish, pytest_sessionfinish\n"
    )
    pytester.makepyfile(
        test_cases="""
            import pytest

            @pytest.mark.parametrize("pipeline", [
                "unit", "integration", pytest.param("marked", marks=pytest.mark.integration),
            ])
            def test_pipeline(pipeline):
                pass

            @pytest.mark.integration
            def test_marked_function():
                pass

            @pytest.mark.integration
            class TestMarkedClass:
                def test_inherited_marker(self):
                    pass
        """,
        test_marked_module="""
            import pytest
            pytestmark = pytest.mark.integration

            def test_inherited_marker():
                pass
        """,
    )
    return pytester


@pytest.mark.parametrize("ci_variable", [None, "GITHUB_ACTIONS", "TF_BUILD"])
def test_unit_lane_ignores_integration_parameter_ids(collection_suite, monkeypatch, ci_variable):
    if ci_variable:
        monkeypatch.setenv(ci_variable, "true")
    result = collection_suite.runpytest_inprocess("-m", "not integration", "-q")
    result.assert_outcomes(passed=2, deselected=4)
    assert result.ret == 0
    assert not (collection_suite.path / "workspace").exists()


@pytest.mark.parametrize("ci_variable", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_ci_integration_lane_requires_configuration(collection_suite, monkeypatch, ci_variable):
    monkeypatch.setenv(ci_variable, "true")
    result = collection_suite.runpytest_inprocess("-m", "integration", "-q")
    assert result.ret == 1
    assert "Integration tests require sites.local/ overlays" in (
        result.stdout.str() + result.stderr.str()
    )


def test_local_integration_lane_without_configuration_is_skipped(collection_suite):
    result = collection_suite.runpytest_inprocess("-m", "integration", "-q")
    result.assert_outcomes(skipped=4, deselected=2)
    assert result.ret == 0


@pytest.mark.parametrize("ci_variable", ["GITHUB_ACTIONS", "TF_BUILD"])
def test_configured_integration_lane_retains_inherited_markers(
    collection_suite, monkeypatch, ci_variable,
):
    monkeypatch.setenv(ci_variable, "true")
    overlays = collection_suite.path / "workspace" / "sites.local"
    overlays.mkdir(parents=True)
    (overlays / "synthetic.yaml").write_text("{}\n", encoding="utf-8")
    result = collection_suite.runpytest_inprocess("-m", "integration", "-q")
    result.assert_outcomes(passed=4, deselected=2)
    assert result.ret == 0

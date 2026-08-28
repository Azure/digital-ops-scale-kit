"""Fast contracts for AIO release loading used by integration tests."""

from types import SimpleNamespace

import pytest

from tests.integration.helpers.releases import load_aio_release


class FakeOrchestrator:
    def __init__(self, properties: dict[str, object]):
        self.site = SimpleNamespace(properties=properties)

    def load_site(self, site_name: str) -> SimpleNamespace:
        return self.site


def test_loads_the_selected_release_key_and_mapping(tmp_path):
    release_dir = tmp_path / "parameters" / "aio-releases"
    release_dir.mkdir(parents=True)
    (release_dir / "2608.yaml").write_text(
        'aioVersion: "1.4.73"\naioReleaseConfiguration: {}\n'
    )

    key, release = load_aio_release(
        FakeOrchestrator({"aioRelease": "2608"}),
        "test-site",
        tmp_path,
    )

    assert key == "2608"
    assert release["aioVersion"] == "1.4.73"


def test_missing_site_release_is_reported(tmp_path):
    with pytest.raises(AssertionError, match="properties.aioRelease"):
        load_aio_release(FakeOrchestrator({}), "test-site", tmp_path)


def test_non_mapping_release_is_rejected(tmp_path):
    release_dir = tmp_path / "parameters" / "aio-releases"
    release_dir.mkdir(parents=True)
    (release_dir / "2608.yaml").write_text("null\n")

    with pytest.raises(AssertionError, match="is not a mapping"):
        load_aio_release(
            FakeOrchestrator({"aioRelease": "2608"}),
            "test-site",
            tmp_path,
        )

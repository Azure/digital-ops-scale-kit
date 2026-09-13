"""The published workspace catalog matches its authored discovery inputs."""

from siteops.content_index import build_content_index, write_content_index
from siteops.github_catalog import github_input_digests


def test_generated_catalog_matches_the_workspace(workspace):
    bundle = build_content_index(
        workspace, approve_public=True, additional_digests=github_input_digests,
    )
    assert bundle.published
    assert not bundle.unclassified
    write_content_index(workspace, bundle, check=True)

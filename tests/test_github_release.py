"""Bounded observation of explicit releases, tag commits and asset identities."""

import copy

import pytest

from siteops import github_source as github
from siteops.browse import BrowseError
from siteops.github_source import GitHubClient, GitHubReference

COMMIT = "1" * 40
TAG_OBJECT = "2" * 40
ROOT = "/repos/example/content"
TAG = "release/v1"
RELEASE_ROUTE = ROOT + "/releases/tags/release%2Fv1"
TAG_ROUTE = ROOT + "/git/ref/tags/release%2Fv1"


def asset(identifier=11, name="content.zip", **changes):
    return {
        "id": identifier, "name": name, "state": "uploaded",
        "size": 1024, "digest": "sha256:" + "a" * 64,
        **changes,
    }


class ReleaseTransport:
    def __init__(self, *, annotated=False, assets=None):
        self.calls = []
        self.repository = {"id": 41, "full_name": "example/content"}
        self.release = {
            "id": 7, "tag_name": TAG, "draft": False, "prerelease": True,
            "published_at": "2026-01-01T00:00:00Z",
            "target_commitish": "a-different-branch",
        }
        self.reference = {
            "ref": "refs/tags/" + TAG,
            "object": {
                "type": "tag" if annotated else "commit",
                "sha": TAG_OBJECT if annotated else COMMIT,
            },
        }
        self.annotated = {
            "sha": TAG_OBJECT,
            "object": {"type": "commit", "sha": COMMIT},
        }
        self.assets = [asset()] if assets is None else assets

    def __call__(self, route):
        self.calls.append(route)
        if route == ROOT:
            return copy.deepcopy(self.repository)
        if route == RELEASE_ROUTE:
            return copy.deepcopy(self.release)
        if route == TAG_ROUTE:
            return copy.deepcopy(self.reference)
        if route == ROOT + "/git/tags/" + TAG_OBJECT:
            return copy.deepcopy(self.annotated)
        if route.startswith(ROOT + "/releases/7/assets?per_page=100&page="):
            page = int(route.rsplit("=", 1)[1])
            return copy.deepcopy(self.assets[(page - 1) * 100:page * 100])
        raise AssertionError(f"Unexpected release request: {route}")


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("The release metadata fixture attempted an external call.")

    monkeypatch.setattr(github, "_anonymous_request", blocked)
    monkeypatch.setattr(github, "_cli_request", blocked)


def resolve(transport, *, auth="anonymous"):
    return GitHubClient(
        GitHubReference("example", "content", TAG),
        auth=auth, transport=transport,
    ).resolve_release()


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("auth", ["anonymous", "cli"])
def test_explicit_release_uses_exact_tag_namespace_and_complete_asset_identity(annotated, auth):
    transport = ReleaseTransport(annotated=annotated)
    release = resolve(transport, auth=auth)
    assert release.repository_id == 41
    assert release.release_id == 7
    assert release.source_commit == COMMIT
    assert release.tag_object == (TAG_OBJECT if annotated else COMMIT)
    assert release.prerelease is True
    assert release.assets[0].identifier == 11
    assert release.assets[0].require_digest() == "a" * 64
    assert transport.calls.count(TAG_ROUTE) == 2
    assert transport.calls.count(RELEASE_ROUTE) == 2
    assert not any("/commits/" in route or "/blobs/" in route for route in transport.calls)


def test_release_tag_is_required_without_default_branch_resolution():
    transport = ReleaseTransport()
    with pytest.raises(BrowseError) as failure:
        GitHubClient(
            GitHubReference("example", "content"), transport=transport,
        ).resolve_release()
    assert failure.value.diagnostic.code == "github.release-tag-required"
    assert transport.calls == []


@pytest.mark.parametrize("metadata", [
    {}, [], {"id": True, "full_name": "example/content"},
    {"id": 41, "full_name": "other/content"},
])
def test_repository_identity_must_match_before_release_resolution(metadata):
    transport = ReleaseTransport()
    transport.repository = metadata
    with pytest.raises(BrowseError):
        resolve(transport)
    assert transport.calls == [ROOT]


@pytest.mark.parametrize("changes", [
    {"draft": True}, {"draft": 0}, {"prerelease": None},
    {"id": True}, {"id": -1}, {"tag_name": "other"},
    {"published_at": None}, {"published_at": "invalid"}, {"published_at": "2026-01-01"},
    {"immutable": 0},
])
def test_unpublished_or_inconsistent_release_metadata_is_rejected(changes):
    transport = ReleaseTransport()
    transport.release.update(changes)
    with pytest.raises(BrowseError):
        resolve(transport)
    assert TAG_ROUTE not in transport.calls


@pytest.mark.parametrize("changes", [
    {"id": 0}, {"id": True}, {"size": -1}, {"size": True},
    {"name": ""}, {"name": "nested/content.zip"}, {"name": "private\nvalue"},
    {"state": "starter"}, {"digest": "sha1:" + "a" * 40},
    {"digest": "sha256:bad"},
])
def test_release_asset_shape_and_identity_are_checked(changes):
    transport = ReleaseTransport(assets=[asset(**changes)])
    with pytest.raises(BrowseError) as failure:
        resolve(transport)
    assert "private\nvalue" not in str(failure.value)


def test_missing_asset_digest_remains_unknown_and_cannot_be_used_for_acquisition():
    release = resolve(ReleaseTransport(assets=[asset(digest=None)]))
    assert release.assets[0].sha256 is None
    with pytest.raises(BrowseError) as failure:
        release.assets[0].require_digest()
    assert failure.value.diagnostic.code == "github.asset-digest-missing"


@pytest.mark.parametrize("target", [
    None, {"type": "tree", "sha": COMMIT}, {"type": "commit", "sha": "invalid"},
])
def test_tag_must_refer_to_a_commit_or_bounded_annotated_chain(target):
    transport = ReleaseTransport()
    transport.reference["object"] = target
    with pytest.raises(BrowseError):
        resolve(transport)


def test_annotated_tag_identity_and_cycles_are_rejected():
    transport = ReleaseTransport(annotated=True)
    transport.annotated["sha"] = "3" * 40
    with pytest.raises(BrowseError):
        resolve(transport)
    transport.annotated["sha"] = TAG_OBJECT
    transport.annotated["object"] = {"type": "tag", "sha": TAG_OBJECT}
    with pytest.raises(BrowseError) as failure:
        resolve(transport)
    assert failure.value.diagnostic.code == "github.tag-limit"


def test_nested_tag_limit_stops_before_another_request():
    transport = ReleaseTransport(annotated=True)
    count = 0

    def nested(route):
        nonlocal count
        if route.startswith(ROOT + "/git/tags/"):
            count += 1
            return {
                "sha": route.rsplit("/", 1)[1],
                "object": {"type": "tag", "sha": f"{count + 10:040x}"},
            }
        return transport(route)

    with pytest.raises(BrowseError) as failure:
        resolve(nested)
    assert failure.value.diagnostic.code == "github.tag-limit"
    assert count == github._MAX_TAG_DEPTH


@pytest.mark.parametrize("response", [{}, None, [asset()] * 101])
def test_invalid_asset_pages_are_rejected(response):
    transport = ReleaseTransport()

    def invalid(route):
        if "/assets?" in route:
            return response
        return transport(route)

    with pytest.raises(BrowseError) as failure:
        resolve(invalid)
    assert failure.value.diagnostic.code == "github.invalid-data"


def test_asset_pagination_is_complete_bounded_and_sorted():
    assets = [asset(index + 1, f"asset-{index:03d}") for index in range(101)]
    transport = ReleaseTransport(assets=list(reversed(assets)))
    release = resolve(transport)
    assert [item.name for item in release.assets] == [item["name"] for item in assets]
    assert transport.calls.count(ROOT + "/releases/7/assets?per_page=100&page=2") == 2


@pytest.mark.parametrize("duplicate", ["id", "name"])
def test_duplicate_assets_across_pages_are_rejected(duplicate):
    assets = [asset(index + 1, f"asset-{index:03d}") for index in range(101)]
    assets[-1][duplicate] = assets[0][duplicate]
    with pytest.raises(BrowseError, match="duplicates"):
        resolve(ReleaseTransport(assets=assets))


def test_asset_limit_prevents_unbounded_pagination():
    assets = [asset(index + 1, f"asset-{index:03d}") for index in range(257)]
    transport = ReleaseTransport(assets=assets)
    with pytest.raises(BrowseError) as failure:
        resolve(transport)
    assert failure.value.diagnostic.code == "github.release-limit"
    assert not any(route.endswith("page=4") for route in transport.calls)


@pytest.mark.parametrize("change", ["repository", "release", "tag", "assets", "immutability"])
def test_release_mutations_during_resolution_are_explicit(change):
    transport = ReleaseTransport()

    def changing(route):
        result = transport(route)
        if change == "repository" and route == ROOT and transport.calls.count(ROOT) == 2:
            result["id"] += 1
        if transport.calls.count(RELEASE_ROUTE) == 2:
            if change == "release" and route == RELEASE_ROUTE:
                result["id"] += 1
            elif change == "immutability" and route == RELEASE_ROUTE:
                result["immutable"] = True
            elif change == "tag" and route == TAG_ROUTE:
                result["object"]["sha"] = "3" * 40
            elif change == "assets" and "/assets?" in route:
                result[0]["digest"] = "sha256:" + "b" * 64
        return result

    with pytest.raises(BrowseError) as failure:
        resolve(changing)
    assert failure.value.diagnostic.code == "github.source-changed"

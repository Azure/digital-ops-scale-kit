# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import hashlib
import io
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest

from siteops import github_source
from siteops.browse import BrowseError
from siteops.github_source import GitHubClient, GitHubReference, GitHubTreeEntry

COMMIT = "1" * 40
TREE = "2" * 40


def _blob(content: bytes, mode: str = "100644") -> tuple[GitHubTreeEntry, dict[str, object]]:
    sha = hashlib.sha1(
        f"blob {len(content)}\0".encode("ascii") + content,
        usedforsecurity=False,
    ).hexdigest()
    entry = GitHubTreeEntry("content/file.yaml", sha, "blob", mode, len(content))
    response = {
        "sha": sha,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }
    return entry, response


def _assert_code(error: pytest.ExceptionInfo[BrowseError], code: str) -> None:
    assert error.value.diagnostic.code == code


@pytest.fixture(autouse=True)
def _no_unexpected_external_calls(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("unexpected external call")

    monkeypatch.setattr(github_source.urllib.request, "build_opener", blocked)
    monkeypatch.setattr(github_source.subprocess, "Popen", blocked)


@pytest.fixture(autouse=True)
def _no_host_token(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("value", "ref", "expected"),
    [
        ("github:Azure/digital-ops-scale-kit", None, ("Azure", "digital-ops-scale-kit", None)),
        ("github:owner/repo@release/v1", None, ("owner", "repo", "release/v1")),
        ("github:owner/.github", COMMIT, ("owner", ".github", COMMIT)),
        ("https://github.com/owner/repo", "main", ("owner", "repo", "main")),
        ("https://GitHub.com/Owner/Repo.git/", None, ("Owner", "Repo", None)),
    ],
)
def test_reference_parses_supported_locators(value, ref, expected):
    reference = GitHubReference.parse(value, ref)

    assert (reference.owner, reference.repository, reference.ref) == expected
    assert reference.web_url == f"https://github.com/{expected[0]}/{expected[1]}"


@pytest.mark.parametrize(
    ("value", "ref"),
    [
        ("", None),
        (" github:owner/repo", None),
        ("github:owner", None),
        ("github:owner/repo/extra", None),
        ("github:-owner/repo", None),
        ("github:owner--name/repo", None),
        ("github:owner/repo name", None),
        ("github:owner/repo@", None),
        ("github:owner/repo@main", "other"),
        ("https://http://github.com/owner/repo", None),
        ("http://github.com/owner/repo", None),
        ("https://example.com/owner/repo", None),
        ("https://user@github.com/owner/repo", None),
        ("https://github.com:443/owner/repo", None),
        ("https://github.com/owner/repo/issues", None),
        ("https://github.com/owner/repo//", None),
        ("https://github.com/owner/repo?tab=readme", None),
        ("https://github.com/owner/repo?", None),
        ("https://github.com/owner/repo#readme", None),
        ("https://github.com/owner/repo#", None),
    ],
)
def test_reference_rejects_ambiguous_or_unsafe_locators(value, ref):
    with pytest.raises(BrowseError) as error:
        GitHubReference.parse(value, ref)

    _assert_code(error, "github.reference")


@pytest.mark.parametrize("ref", ["feature..x", "refs//heads/main", ".hidden", "bad lock.lock"])
def test_reference_rejects_invalid_refs(ref):
    with pytest.raises(BrowseError) as error:
        GitHubReference("owner", "repo", ref)

    _assert_code(error, "github.reference")


def test_constructor_is_side_effect_free_and_keeps_reference():
    reference = GitHubReference.parse("github:owner/repo")

    client = GitHubClient(reference)

    assert client.reference is reference
    assert client.access == "anonymous"


def test_resolve_commit_uses_repository_default_branch_and_encoded_route():
    routes = []

    def transport(route):
        routes.append(route)
        if route == "/repos/owner/repo":
            return {"default_branch": "release/v1"}
        return {"sha": COMMIT}

    result = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=transport,
    ).resolve_commit()

    assert result == COMMIT
    assert routes == [
        "/repos/owner/repo",
        "/repos/owner/repo/commits/release%2Fv1",
    ]


@pytest.mark.parametrize("ref", ["v1.2.3", COMMIT])
def test_resolve_commit_uses_commits_endpoint_for_explicit_refs_and_tags(ref):
    routes = []

    client = GitHubClient(
        GitHubReference.parse("github:owner/repo", ref),
        transport=lambda route: routes.append(route) or {"sha": COMMIT.upper()},
    )

    assert client.resolve_commit() == COMMIT
    assert routes == [f"/repos/owner/repo/commits/{ref}"]


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"default_branch": ""},
        {"default_branch": "bad ref"},
    ],
)
def test_default_branch_metadata_must_be_valid(response):
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda _route: response,
    )

    with pytest.raises(BrowseError) as error:
        client.resolve_commit()

    _assert_code(error, "github.invalid-data")


def test_get_tree_returns_regular_links_trees_and_submodules_at_pinned_route():
    raw_entries = [
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "3" * 40, "size": 4},
        {"path": "script.sh", "mode": "100755", "type": "blob", "sha": "4" * 40, "size": 5},
        {"path": "latest", "mode": "120000", "type": "blob", "sha": "5" * 40, "size": 6},
        {"path": "content", "mode": "040000", "type": "tree", "sha": "6" * 40},
        {"path": "vendor/repo", "mode": "160000", "type": "commit", "sha": "7" * 40},
    ]
    routes = []
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda route: routes.append(route)
        or {"sha": TREE, "url": "https://api.github.com/tree", "truncated": False, "tree": raw_entries},
    )

    entries = client.get_tree(COMMIT)

    assert routes == [f"/repos/owner/repo/git/trees/{COMMIT}?recursive=1"]
    assert list(entries) == ["README.md", "script.sh", "latest", "content", "vendor/repo"]
    assert entries["latest"].mode == "120000"
    assert entries["vendor/repo"].type == "commit"
    assert entries["content"].size is None


@pytest.mark.parametrize(
    ("tree", "code"),
    [
        ({"sha": TREE, "truncated": True, "tree": []}, "github.tree-limit"),
        ({"sha": "bad", "truncated": False, "tree": []}, "github.invalid-data"),
        ({"sha": TREE, "truncated": "false", "tree": []}, "github.invalid-data"),
        ({"sha": TREE, "truncated": False, "tree": {}}, "github.invalid-data"),
        (
            {
                "sha": TREE,
                "truncated": False,
                "tree": [
                    {"path": "a", "mode": "100644", "type": "blob", "sha": "3" * 40},
                    {"path": "a", "mode": "100644", "type": "blob", "sha": "4" * 40},
                ],
            },
            "github.invalid-data",
        ),
    ],
)
def test_get_tree_rejects_truncated_or_malformed_results(tree, code):
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda _route: tree,
    )

    with pytest.raises(BrowseError) as error:
        client.get_tree(COMMIT)

    _assert_code(error, code)


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "/absolute", "mode": "100644", "type": "blob", "sha": "3" * 40},
        {"path": "a//b", "mode": "100644", "type": "blob", "sha": "3" * 40},
        {"path": "a/../b", "mode": "100644", "type": "blob", "sha": "3" * 40},
        {"path": "a\\b", "mode": "100644", "type": "blob", "sha": "3" * 40},
        {"path": "a", "mode": "120000", "type": "tree", "sha": "3" * 40},
        {"path": "a", "mode": "100664", "type": "blob", "sha": "3" * 40},
        {"path": "a", "mode": "100644", "type": "blob", "sha": "short"},
        {"path": "a", "mode": "100644", "type": "blob", "sha": "3" * 40, "size": True},
        {"path": "a", "mode": "040000", "type": "tree", "sha": "3" * 40, "size": 0},
        {
            "path": "a",
            "mode": "100644",
            "type": "blob",
            "sha": "3" * 40,
            "url": 1,
        },
    ],
)
def test_get_tree_rejects_invalid_entry_shapes(entry):
    response = {"sha": TREE, "truncated": False, "tree": [entry]}
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda _route: response,
    )

    with pytest.raises(BrowseError) as error:
        client.get_tree(COMMIT)

    _assert_code(error, "github.invalid-data")


def test_get_tree_applies_own_entry_and_json_limits(monkeypatch):
    monkeypatch.setattr(github_source, "_MAX_TREE_ENTRIES", 1)
    entries = [
        {"path": str(index), "mode": "100644", "type": "blob", "sha": "3" * 40}
        for index in range(2)
    ]
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda _route: {"sha": TREE, "truncated": False, "tree": entries},
    )
    with pytest.raises(BrowseError) as error:
        client.get_tree(COMMIT)
    _assert_code(error, "github.tree-limit")

    monkeypatch.setattr(github_source, "_MAX_TREE_ENTRIES", 20_000)
    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 10)
    with pytest.raises(BrowseError) as error:
        client.get_tree(COMMIT)
    _assert_code(error, "github.tree-limit")


def test_read_blob_uses_object_route_and_verifies_git_identity():
    content = b"kind: example\n"
    entry, response = _blob(content)
    response["content"] = "\n".join(
        response["content"][index : index + 8]
        for index in range(0, len(response["content"]), 8)
    )
    routes = []
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda route: routes.append(route) or response,
    )

    assert client.read_blob(entry) == content
    assert routes == [f"/repos/owner/repo/git/blobs/{entry.sha}"]


@pytest.mark.parametrize("mode,type_", [("120000", "blob"), ("040000", "tree"), ("160000", "commit")])
def test_read_blob_rejects_non_regular_entries_without_request(mode, type_):
    calls = []
    size = 1 if type_ == "blob" else None
    entry = GitHubTreeEntry("item", "3" * 40, type_, mode, size)
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda route: calls.append(route),
    )

    with pytest.raises(BrowseError) as error:
        client.read_blob(entry)

    _assert_code(error, "github.blob-mode")
    assert calls == []


def test_read_blob_rejects_declared_oversize_before_request():
    calls = []
    entry = GitHubTreeEntry("large", "3" * 40, "blob", "100644", 11)
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda route: calls.append(route),
    )

    with pytest.raises(BrowseError) as error:
        client.read_blob(entry, max_bytes=10)

    _assert_code(error, "github.blob-limit")
    assert calls == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda response: response.update(sha="f" * 40), "github.integrity"),
        (lambda response: response.update(size=response["size"] + 1), "github.integrity"),
        (lambda response: response.update(content="%%%"), "github.invalid-data"),
        (
            lambda response: response.update(
                content=base64.b64encode(b"other content\n").decode("ascii")
            ),
            "github.integrity",
        ),
        (lambda response: response.update(encoding="utf-8"), "github.invalid-data"),
    ],
)
def test_read_blob_fails_closed_on_metadata_or_content_mismatch(mutate, code):
    entry, response = _blob(b"expected\n")
    mutate(response)
    client = GitHubClient(
        GitHubReference.parse("github:owner/repo"),
        transport=lambda _route: response,
    )

    with pytest.raises(BrowseError) as error:
        client.read_blob(entry)

    _assert_code(error, code)


class _Response:
    def __init__(self, url, payload=b"{}", status=200, headers=None):
        self.url = url
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.request = None
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, amount):
        assert amount == github_source._MAX_RESPONSE_BYTES + 1
        return self.payload[:amount]


class _Opener:
    def __init__(self, result):
        self.result = result

    def open(self, request, timeout):
        if isinstance(self.result, Exception):
            raise self.result
        self.result.request = request
        self.result.timeout = timeout
        return self.result


def test_anonymous_transport_uses_fixed_headers_host_timeout_and_no_redirects(monkeypatch):
    url = "https://api.github.com/repos/owner/repo"
    response = _Response(url, json.dumps({"default_branch": "main"}).encode())
    observed_handlers = []

    def build_opener(*handlers):
        observed_handlers.extend(handlers)
        return _Opener(response)

    monkeypatch.setattr(github_source.urllib.request, "build_opener", build_opener)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))

    assert client._request("/repos/owner/repo") == {"default_branch": "main"}
    assert response.request.full_url == url
    assert response.request.get_method() == "GET"
    assert response.request.get_header("Accept") == "application/vnd.github+json"
    assert response.request.get_header("User-agent") == "siteops-github-source"
    assert response.request.get_header("X-github-api-version") == "2026-03-10"
    assert response.timeout == github_source._NETWORK_TIMEOUT_SECONDS
    assert any(isinstance(handler, urllib.request.ProxyHandler) for handler in observed_handlers)
    assert any(isinstance(handler, github_source._RejectRedirects) for handler in observed_handlers)
    assert not response.request.has_header("Authorization")
    assert client.access == "anonymous"


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (401, {}, "github.auth"),
        (403, {}, "github.auth"),
        (403, {"X-RateLimit-Remaining": "0"}, "github.rate-limit"),
        (404, {}, "github.not-found"),
        (429, {}, "github.rate-limit"),
        (503, {}, "github.network"),
    ],
)
def test_anonymous_transport_maps_http_failures_without_payload(monkeypatch, status, headers, code):
    url = "https://api.github.com/repos/owner/repo"
    error_response = urllib.error.HTTPError(url, status, "secret detail", headers, io.BytesIO())
    monkeypatch.setattr(
        github_source.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(error_response),
    )
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))

    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")

    _assert_code(error, code)
    assert "secret detail" not in str(error.value)


def test_anonymous_transport_rejects_redirect_and_invalid_json(monkeypatch):
    requested = "https://api.github.com/repos/owner/repo"
    response = _Response("https://example.com/redirect", b"{}")
    monkeypatch.setattr(
        github_source.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(response),
    )
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.redirect")

    response.url = requested
    response.payload = b"not json"
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.invalid-data")


def test_anonymous_transport_bounds_payload_and_maps_timeouts(monkeypatch):
    url = "https://api.github.com/repos/owner/repo"
    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 4)
    response = _Response(url, b"12345")
    monkeypatch.setattr(
        github_source.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(response),
    )
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.response-limit")

    monkeypatch.setattr(
        github_source.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(TimeoutError("secret timeout detail")),
    )
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.timeout")
    assert "secret" not in str(error.value)


class _Process:
    def __init__(self, stdout, stderr=b"", returncode=0, hangs=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if hangs else returncode
        self.final_returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = self.final_returncode

    def kill(self):
        self.killed = True
        self.returncode = self.final_returncode

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("gh", timeout)
        return self.returncode


def _install_process(monkeypatch, process):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(github_source.subprocess, "Popen", popen)
    return calls


def test_github_cli_runs_without_a_shell_and_returns_its_output(monkeypatch):
    calls = _install_process(monkeypatch, _Process(b"verified", stderr=b"note"))

    assert github_source._run_gh(["gh", "version"]) == (0, b"verified", b"note")
    argv, kwargs = calls[0]
    assert argv == ["gh", "version"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE


def test_github_cli_runs_without_telemetry_and_reports_start_failure(monkeypatch):
    started = {}

    def refuse(*_args, **kwargs):
        started.update(kwargs)
        raise FileNotFoundError()

    monkeypatch.setenv("GH_TELEMETRY", "enabled")
    monkeypatch.setattr(github_source.subprocess, "Popen", refuse)
    with pytest.raises(BrowseError) as error:
        github_source._run_gh(["gh", "version"])

    _assert_code(error, "github.tool-missing")
    assert started["env"]["GH_TELEMETRY"] == "false"


def test_github_cli_bounds_stdout_stderr_and_elapsed_time(monkeypatch):
    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 4)
    _install_process(monkeypatch, _Process(b"more than four"))
    with pytest.raises(BrowseError) as error:
        github_source._run_gh(["gh", "attestation"])
    _assert_code(error, "github.response-limit")

    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(github_source, "_MAX_STDERR_BYTES", 4)
    _install_process(monkeypatch, _Process(b"", stderr=b"secret stderr", returncode=1))
    with pytest.raises(BrowseError) as error:
        github_source._run_gh(["gh", "attestation"])
    _assert_code(error, "github.response-limit")
    assert "secret" not in str(error.value)

    monkeypatch.setattr(github_source, "_MAX_STDERR_BYTES", 64 * 1024)
    hanging = _Process(b"", hangs=True)
    _install_process(monkeypatch, hanging)
    with pytest.raises(BrowseError) as error:
        github_source._run_gh(["gh", "attestation"], timeout=0.01)
    _assert_code(error, "github.timeout")
    assert hanging.terminated


def test_github_cli_cancellation_terminates_the_owned_process(monkeypatch):
    process = _Process(b"", hangs=True)
    _install_process(monkeypatch, process)

    def cancel(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(github_source.time, "sleep", cancel)
    with pytest.raises(KeyboardInterrupt):
        github_source._run_gh(["gh", "attestation"])
    assert process.terminated or process.killed


def test_tree_mode_validation_handles_non_scalar_input():
    with pytest.raises(BrowseError):
        GitHubTreeEntry("manifest.yaml", "a" * 40, "blob", [], 1)


def test_api_json_rejects_duplicate_identity_fields(monkeypatch):
    requested = "https://api.github.com/repos/owner/repo"
    response = _Response(requested, b'{"sha":"first","sha":"second"}')
    monkeypatch.setattr(
        github_source.urllib.request, "build_opener", lambda *_handlers: _Opener(response)
    )
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.invalid-data")


def test_github_cli_output_limit_terminates_then_kills_when_needed(monkeypatch):
    class Uncooperative(_Process):
        def terminate(self):
            self.terminated = True
            raise OSError("fixture")

    process = Uncooperative(b"too much output", hangs=True)
    _install_process(monkeypatch, process)
    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 4)
    with pytest.raises(BrowseError) as failure:
        github_source._run_gh(["gh", "attestation"])
    assert failure.value.diagnostic.code == "github.response-limit"
    assert process.terminated and process.killed


TOKEN = "ghp_SyntheticSentinelValue0123456789"
NOW = 1_800_000_000
ANONYMOUS_LIMIT = (
    "GitHub API rate limit was reached. It resets at 08:23 UTC (in 23 minutes). Retry after that time, "
    "or set GH_TOKEN to a GitHub token with public read access, for example "
    "`export GH_TOKEN=$(gh auth token)`."
)


def _capture_opener(monkeypatch, result):
    opener = _Opener(result)
    monkeypatch.setattr(github_source.urllib.request, "build_opener", lambda *_handlers: opener)
    return opener


@pytest.mark.parametrize("variables", [
    {"GH_TOKEN": TOKEN},
    {"GH_TOKEN": f"  {TOKEN}\n"},
    {"GH_TOKEN": TOKEN, "GITHUB_TOKEN": "other-token"},
])
def test_environment_token_is_sent_only_as_an_unredirected_api_header(monkeypatch, variables):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    url = "https://api.github.com/repos/owner/repo"
    response = _Response(url, b"{}")
    _capture_opener(monkeypatch, response)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))

    assert client._request("/repos/owner/repo") == {}
    request = response.request
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.github.com"
    assert request.unredirected_hdrs == {"Authorization": f"Bearer {TOKEN}"}
    assert "Authorization" not in request.headers
    assert client.access == "token"
    assert TOKEN not in repr(client._token) and TOKEN not in repr(vars(client))


@pytest.mark.parametrize("value", [TOKEN, "not a valid token"])
def test_github_token_alone_is_not_read_or_sent(monkeypatch, value):
    monkeypatch.setenv("GITHUB_TOKEN", value)
    url = "https://api.github.com/repos/owner/repo"
    response = _Response(url, b"{}")
    _capture_opener(monkeypatch, response)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))

    assert client._request("/repos/owner/repo") == {}
    assert client.access == "anonymous" and client._token is None
    assert not response.request.has_header("Authorization")
    assert response.request.unredirected_hdrs == {}
    assert all(value not in item for _, item in response.request.header_items())


def test_environment_token_is_not_copied_to_a_redirected_request(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    url = "https://api.github.com/repos/owner/repo"
    response = _Response(url, b"{}")
    _capture_opener(monkeypatch, response)
    GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")

    followed = urllib.request.HTTPRedirectHandler().redirect_request(
        response.request, io.BytesIO(), 302, "Found", {}, "https://objects.githubusercontent.com/x",
    )
    assert followed is not None
    assert not followed.has_header("Authorization")
    assert github_source._RejectRedirects().redirect_request(
        response.request, io.BytesIO(), 302, "Found", {}, "https://example.com/x",
    ) is None


def test_redirect_with_environment_token_is_rejected_without_a_second_request(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    url = "https://api.github.com/repos/owner/repo"
    redirect = urllib.error.HTTPError(url, 302, "Found", {"Location": "https://example.com/x"}, io.BytesIO())
    opener = _capture_opener(monkeypatch, redirect)
    opened = []
    original = opener.open
    opener.open = lambda request, timeout: opened.append(request) or original(request, timeout)

    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")

    _assert_code(error, "github.redirect")
    assert len(opened) == 1


def test_environment_token_is_never_sent_to_another_host(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setattr(github_source, "_API_ROOT", "https://example.com")
    client = GitHubClient(GitHubReference.parse("github:owner/repo"))
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.invalid-data")
    assert TOKEN not in str(error.value)


def test_injected_transport_does_not_read_the_environment_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    routes = []
    client = GitHubClient(GitHubReference("owner", "repo"), transport=lambda route: routes.append(route) or {})
    assert client.access == "anonymous"
    client._request("/repos/owner/repo")
    assert routes == ["/repos/owner/repo"]


@pytest.mark.parametrize("value", [f"{TOKEN} extra", f"{TOKEN}\nX-Injected: 1", "t\u00f6ken", "x" * 4097])
def test_invalid_environment_token_names_the_variable_without_its_value(monkeypatch, value):
    monkeypatch.setenv("GH_TOKEN", value)
    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference("owner", "repo"))
    _assert_code(error, "github.auth")
    assert str(error.value) == "GH_TOKEN must contain one GitHub token without spaces or control characters."
    assert value not in str(error.value) and TOKEN not in str(error.value)


@pytest.mark.parametrize(("failure", "code"), [
    (urllib.error.HTTPError("https://api.github.com/x", 401, TOKEN, {}, io.BytesIO()), "github.auth"),
    (urllib.error.HTTPError("https://api.github.com/x", 403, TOKEN, {}, io.BytesIO()), "github.auth"),
    (urllib.error.HTTPError("https://api.github.com/x", 404, TOKEN, {}, io.BytesIO()), "github.not-found"),
    (urllib.error.HTTPError(
        "https://api.github.com/x", 403, TOKEN,
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 23 * 60)}, io.BytesIO(),
    ), "github.rate-limit"),
    (urllib.error.HTTPError("https://api.github.com/x", 503, TOKEN, {}, io.BytesIO()), "github.network"),
    (urllib.error.URLError(TOKEN), "github.network"),
    (TimeoutError(TOKEN), "github.timeout"),
])
def test_environment_token_never_appears_in_errors_or_logs(monkeypatch, caplog, failure, code):
    caplog.set_level("DEBUG")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_SyntheticAutomaticSentinel")
    monkeypatch.setattr(github_source.time, "time", lambda: NOW)
    _capture_opener(monkeypatch, failure)
    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")
    _assert_code(error, code)
    for text in (str(error.value), repr(error.value), json.dumps(error.value.diagnostic.document()), caplog.text):
        assert TOKEN not in text and "ghs_SyntheticAutomaticSentinel" not in text
    assert error.value.__cause__ is None and error.value.__suppress_context__


@pytest.mark.parametrize("variables", [{"GH_TOKEN": TOKEN}, {"GH_TOKEN": TOKEN, "GITHUB_TOKEN": "other-token"}])
def test_rejected_environment_token_names_gh_token_without_falling_back(monkeypatch, variables):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    opener = _capture_opener(monkeypatch, urllib.error.HTTPError(
        "https://api.github.com/x", 401, "Bad credentials", {}, io.BytesIO(),
    ))
    opened = []
    original = opener.open
    opener.open = lambda request, timeout: opened.append(request) or original(request, timeout)
    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")
    assert str(error.value) == (
        "GitHub did not accept the token in GH_TOKEN for this read. Use a token that can read the "
        "repository, or unset GH_TOKEN to read public sources anonymously."
    )
    assert len(opened) == 1


@pytest.mark.parametrize(("status", "headers"), [
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 23 * 60)}),
    (429, {"x-ratelimit-reset": str(NOW + 23 * 60)}),
])
def test_anonymous_rate_limit_reports_reset_time_and_token_option(monkeypatch, status, headers):
    monkeypatch.setattr(github_source.time, "time", lambda: NOW)
    _capture_opener(monkeypatch, urllib.error.HTTPError(
        "https://api.github.com/x", status, "rate limited", headers, io.BytesIO(),
    ))
    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")
    _assert_code(error, "github.rate-limit")
    assert str(error.value) == ANONYMOUS_LIMIT


def test_rate_limit_suggests_choosing_an_automatic_github_token_without_printing_it(monkeypatch):
    sentinel = "ghs_SyntheticAutomaticSentinel"
    monkeypatch.setenv("GITHUB_TOKEN", sentinel)
    monkeypatch.setattr(github_source.time, "time", lambda: NOW)
    opener = _capture_opener(monkeypatch, urllib.error.HTTPError(
        "https://api.github.com/x", 403, "rate limited",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 23 * 60)}, io.BytesIO(),
    ))
    opened = []
    original = opener.open
    opener.open = lambda request, timeout: opened.append(request) or original(request, timeout)
    with pytest.raises(BrowseError) as error:
        GitHubClient(GitHubReference.parse("github:owner/repo"))._request("/repos/owner/repo")
    assert str(error.value) == (
        "GitHub API rate limit was reached. It resets at 08:23 UTC (in 23 minutes). Retry after that time, "
        'or use the token in GITHUB_TOKEN for Site Ops with `export GH_TOKEN="$GITHUB_TOKEN"`.'
    )
    assert sentinel not in json.dumps(error.value.diagnostic.document())
    assert not opened[0].has_header("Authorization")


@pytest.mark.parametrize(("headers", "token", "expected"), [
    ({"retry-after": "90"}, False,
     "GitHub API rate limit was reached. It resets at 08:02 UTC (in 2 minutes). Retry after that time, "
     "or set GH_TOKEN to a GitHub token with public read access, for example "
     "`export GH_TOKEN=$(gh auth token)`."),
    ({}, False,
     "GitHub API rate limit was reached. Retry later, or set GH_TOKEN to a GitHub token with public "
     "read access, for example `export GH_TOKEN=$(gh auth token)`."),
    ({"x-ratelimit-reset": "not-a-time"}, True,
     "GitHub API rate limit was reached for the token in GH_TOKEN. Retry later."),
    ({"x-ratelimit-reset": str(NOW + 30)}, True,
     "GitHub API rate limit was reached for the token in GH_TOKEN. "
     "It resets at 08:01 UTC (in 1 minute). Retry after that time."),
    ({"x-ratelimit-reset": str(NOW - 10)}, True,
     "GitHub API rate limit was reached for the token in GH_TOKEN. "
     "It resets at 08:00 UTC. Retry after that time."),
    ({"x-ratelimit-reset": str(NOW + 3 * 24 * 60 * 60)}, True,
     "GitHub API rate limit was reached for the token in GH_TOKEN. Retry later."),
])
def test_rate_limit_summary_variants(monkeypatch, headers, token, expected):
    monkeypatch.setattr(github_source.time, "time", lambda: NOW)
    error = github_source._classify_status(429, headers, token=token)
    assert error.diagnostic.code == "github.rate-limit"
    assert str(error) == expected

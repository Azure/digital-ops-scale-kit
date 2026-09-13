# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import hashlib
import io
import json
import subprocess
import urllib.error

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

    client = GitHubClient(reference, auth="cli")

    assert client.reference is reference
    assert client.auth == "cli"


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


def _install_cli(monkeypatch, tmp_path, process):
    executable = tmp_path / "gh.exe"
    executable.write_bytes(b"test")
    calls = []

    monkeypatch.setattr(github_source.shutil, "which", lambda _name: str(executable))

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(github_source.subprocess, "Popen", popen)
    return calls


def _included(status, payload=b"{}", reason=b"OK"):
    return b"HTTP/2.0 " + str(status).encode() + b" " + reason + b"\r\nX-Test: 1\r\n\r\n" + payload


def test_cli_transport_uses_absolute_argv_and_parses_bounded_json(monkeypatch, tmp_path):
    process = _Process(_included(200, b'{"sha":"' + COMMIT.encode() + b'"}'))
    calls = _install_cli(monkeypatch, tmp_path, process)
    client = GitHubClient(GitHubReference.parse("github:owner/repo", "main"), auth="cli")

    assert client.resolve_commit() == COMMIT
    argv, kwargs = calls[0]
    assert argv[0] == str((tmp_path / "gh.exe").resolve())
    assert argv[1:6] == ["api", "--hostname", "github.com", "--method", "GET"]
    assert argv[-1] == "/repos/owner/repo/commits/main"
    assert "auth" not in argv
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE


@pytest.mark.parametrize(
    ("status", "stderr", "code"),
    [
        (401, b"secret token rejected", "github.auth"),
        (403, b"API rate limit exceeded for secret identity", "github.rate-limit"),
        (404, b"private repository name", "github.not-found"),
        (503, b"internal payload", "github.network"),
    ],
)
def test_cli_transport_maps_http_failures_without_exposing_stderr(
    monkeypatch, tmp_path, status, stderr, code
):
    headers = b"X-RateLimit-Remaining: 0\r\n" if code == "github.rate-limit" else b""
    output = (
        b"HTTP/2.0 "
        + str(status).encode()
        + b" Error\r\n"
        + headers
        + b"\r\n{}"
    )
    process = _Process(output, stderr=stderr, returncode=1)
    _install_cli(monkeypatch, tmp_path, process)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")

    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")

    _assert_code(error, code)
    assert stderr.decode() not in str(error.value)


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (b"please run gh auth login with secret", "github.auth"),
        (b"request timed out after secret", "github.timeout"),
        (b"connection failed with secret", "github.network"),
        (b"gh: Not Found (HTTP 404) secret", "github.not-found"),
    ],
)
def test_cli_transport_classifies_failures_without_included_headers(
    monkeypatch, tmp_path, stderr, code
):
    process = _Process(b"", stderr=stderr, returncode=1)
    _install_cli(monkeypatch, tmp_path, process)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")

    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")

    _assert_code(error, code)
    assert b"secret" not in str(error.value).encode()


def test_cli_transport_reports_missing_tool_without_fallback(monkeypatch):
    monkeypatch.setattr(github_source.shutil, "which", lambda _name: None)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")

    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")

    _assert_code(error, "github.tool-missing")


def test_cli_transport_reports_executable_start_failure(monkeypatch, tmp_path):
    executable = tmp_path / "gh.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(github_source.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(
        github_source.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")

    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")

    _assert_code(error, "github.tool-missing")


def test_cli_transport_rejects_redirects_and_invalid_json(monkeypatch, tmp_path):
    process = _Process(_included(302, b"secret redirect"))
    _install_cli(monkeypatch, tmp_path, process)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.redirect")

    process.stdout = io.BytesIO(_included(200, b"not json"))
    process.stderr = io.BytesIO()
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.invalid-data")


def test_cli_transport_bounds_stdout_stderr_and_elapsed_time(monkeypatch, tmp_path):
    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 4)
    process = _Process(b"more than four")
    _install_cli(monkeypatch, tmp_path, process)
    client = GitHubClient(GitHubReference.parse("github:owner/repo"), auth="cli")
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.response-limit")

    monkeypatch.setattr(github_source, "_MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(github_source, "_MAX_STDERR_BYTES", 4)
    process.stdout = io.BytesIO(b"")
    process.stderr = io.BytesIO(b"secret stderr")
    process.returncode = 1
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.response-limit")
    assert "secret" not in str(error.value)

    monkeypatch.setattr(github_source, "_MAX_STDERR_BYTES", 64 * 1024)
    monkeypatch.setattr(github_source, "_CLI_TIMEOUT_SECONDS", 0.01)
    hanging = _Process(b"", hangs=True)
    _install_cli(monkeypatch, tmp_path, hanging)
    with pytest.raises(BrowseError) as error:
        client._request("/repos/owner/repo")
    _assert_code(error, "github.timeout")
    assert hanging.terminated

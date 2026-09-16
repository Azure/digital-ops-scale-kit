"""Small published GitHub catalog used by discovery and metadata cache tests."""

from dataclasses import dataclass
from types import SimpleNamespace

from siteops.content_index import BINDINGS_NAME, INDEX_NAME, build_content_index
from siteops.github_catalog import github_input_digests

REVISION = "a" * 40
PREFIX = "workspaces/example/"


@dataclass(frozen=True)
class TreeEntry:
    path: str
    sha: str
    type: str = "blob"
    mode: str = "100644"
    size: int | None = None


class Client:
    def __init__(self):
        self.reference = SimpleNamespace(web_url="https://github.com/example/kit")
        self.tree = {}
        self.blobs = {}
        self.calls = []

    def put(self, path, content):
        sha = github_input_digests(content)["git-blob-sha1"]
        self.tree[path] = TreeEntry(path, sha, size=len(content))
        self.blobs[sha] = content

    def resolve_commit(self):
        self.calls.append(("resolve",))
        return REVISION

    def get_tree(self, commit):
        assert commit == REVISION
        self.calls.append(("tree", commit))
        return self.tree

    def read_blob(self, entry, *, max_bytes):
        self.calls.append(("blob", entry.sha))
        value = self.blobs[entry.sha]
        assert len(value) <= max_bytes
        return value


def publish(workspace, client, prefix=PREFIX, *, git_bindings=True):
    bundle = build_content_index(
        workspace, approve_public=True,
        additional_digests=github_input_digests if git_bindings else None,
    )
    for path in workspace.rglob("*"):
        if path.is_file():
            client.put(prefix + path.relative_to(workspace).as_posix(), path.read_bytes())
    client.put(prefix + INDEX_NAME, bundle.index)
    client.put(prefix + BINDINGS_NAME, bundle.bindings)
    return bundle

"""Exercise release intent parsing against immutable Git trees."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from siteops_release import (  # noqa: E402
    ReleaseIntentError,
    discover_release_intent,
    inactive_release_plan,
    load_release_intent,
)

CLI = SCRIPTS / "prepare-siteops-release.py"
ZERO_SHA = "0" * 40
REPOSITORY = "example/releases"
SOURCE_REF = "refs/heads/main"
HEADLINE = "Release highlights"


def _git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> str:
    input_arguments = (
        {"stdin": subprocess.DEVNULL}
        if input_bytes is None
        else {"input": input_bytes}
    )
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        timeout=30,
        check=False,
        **input_arguments,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(
        errors="replace"
    )
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Release Intent Test")
    _git(root, "config", "user.email", "release-intent@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _write_source_version(repository: Path, source: str) -> None:
    path = repository / "siteops" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="")


def _write_record(
    repository: Path,
    declaration: dict | bytes,
    *,
    name: str = "candidate",
    notes: bytes | None = b"## Changes\n\nReviewed release notes.\n",
) -> tuple[bytes, bytes | None]:
    directory = repository / "releases" / name
    directory.mkdir(parents=True, exist_ok=True)
    raw = (
        declaration
        if isinstance(declaration, bytes)
        else json.dumps({"headline": HEADLINE, **declaration}, separators=(",", ":")).encode("utf-8")
    )
    (directory / "release.json").write_bytes(raw)
    if notes is not None:
        (directory / "notes.md").write_bytes(notes)
    return raw, notes


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _load(repository: Path, source_sha: str, path: str = "releases/candidate/release.json"):
    return load_release_intent(
        repository,
        source_sha,
        path,
        REPOSITORY,
        SOURCE_REF,
    )


def _run_cli(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(CLI), *arguments],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_engine_release_matches_literal_source_without_importing(repository: Path):
    _write_source_version(
        repository,
        'raise RuntimeError("must not execute")\n__version__ = "1.2.3"\n',
    )
    declaration, notes = _write_record(
        repository,
        {"tag": "siteops/v1.2.3"},
        notes=b"# Site Ops 1.2.3\r\n\r\nReady.\r\n",
    )
    source_sha = _commit(repository, "engine release")

    intent = _load(repository, source_sha)

    assert intent.notes == notes.decode("utf-8")
    assert intent.to_dict() == {
        "apiVersion": "siteops.release/v1",
        "kind": "ReleaseCandidate",
        "active": True,
        "dryRun": False,
        "source": {
            "repository": REPOSITORY,
            "commit": source_sha,
            "ref": SOURCE_REF,
        },
        "intent": {
            "path": "releases/candidate/release.json",
            "sha256": hashlib.sha256(declaration).hexdigest(),
            "notesPath": "releases/candidate/notes.md",
            "notesSha256": hashlib.sha256(notes).hexdigest(),
        },
        "release": {
            "stream": "siteops",
            "tag": "siteops/v1.2.3",
            "version": "1.2.3",
            "title": "siteops/v1.2.3: " + HEADLINE,
            "prerelease": False,
            "latest": False,
        },
        "siteops": {
            "bundle": True,
            "versionMode": "source",
            "baseVersion": "1.2.3",
            "releaseTag": None,
        },
    }


def test_selected_commit_is_immutable_when_head_and_worktree_advance(repository: Path):
    _write_source_version(repository, '__version__ = "1.0.0b1"\n')
    _write_record(repository, {"tag": "siteops/v1.0.0b1"}, notes=b"Original notes.\n")
    selected = _commit(repository, "selected")

    _write_source_version(repository, '__version__ = "9.0.0"\n')
    _write_record(repository, {"tag": "siteops/v9.0.0"}, notes=b"New notes.\n")
    _commit(repository, "advance head")
    (repository / "releases" / "candidate" / "release.json").write_text(
        '{"tag":"siteops/v10.0.0"}',
        encoding="utf-8",
    )

    intent = _load(repository, selected)

    assert intent.tag == "siteops/v1.0.0b1"
    assert intent.base_version == "1.0.0b1"
    assert intent.notes == "Original notes.\n"


def test_engine_tag_must_exactly_match_the_selected_source_literal(repository: Path):
    _write_source_version(repository, '__version__ = "1.0.0b1"\n')
    _write_record(repository, {"tag": "siteops/v1.0.0b2"})
    source_sha = _commit(repository, "mismatched engine release")

    with pytest.raises(ReleaseIntentError, match="exactly match"):
        _load(repository, source_sha)


def test_committed_example_requires_dry_run_and_is_never_publishable(repository: Path, tmp_path):
    _write_source_version(repository, '__version__ = "1.0.0b1"\n')
    example = repository / ".github" / "release-examples" / "preview"
    example.mkdir(parents=True)
    (example / "release.json").write_text(
        json.dumps({"tag": "v0.0.0.dev0", "headline": HEADLINE, "siteops": {"build": True}}),
    )
    (example / "notes.md").write_text("Example rehearsal.\n")
    sha = _commit(repository, "example")
    path = ".github/release-examples/preview/release.json"
    with pytest.raises(ReleaseIntentError):
        _load(repository, sha, path)
    intent = load_release_intent(repository, sha, path, REPOSITORY, SOURCE_REF, dry_run=True)
    assert intent.to_dict()["dryRun"] is True
    output = tmp_path / "dry-result"
    result = _run_cli(
        repository, "--repository", REPOSITORY, "--source-sha", sha, "--source-ref", SOURCE_REF,
        "--intent", path, "--dry-run", "--output-dir", str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((output / "plan.json").read_text())["dryRun"] is True
    assert discover_release_intent(repository, ZERO_SHA, sha) is None


def test_referenced_engine_tag_has_a_bounded_length(repository: Path):
    _write_record(repository, {"tag": "v1.0.0", "siteops": {"release": "siteops/v1." + "2" * 200}})
    source_sha = _commit(repository, "oversized reference")
    with pytest.raises(ReleaseIntentError, match="too long"):
        _load(repository, source_sha)


@pytest.mark.parametrize(
    "source",
    [None, '__version__ = "9.9.9rc1"\n'],
)
def test_content_only_release_does_not_read_current_engine_version(
    repository: Path,
    source: str | None,
):
    if source is not None:
        _write_source_version(repository, source)
    _write_record(
        repository,
        {
            "tag": "v2.0.0",
            "siteops": {"release": "siteops/v1.5.0"},
            "latest": True,
        },
    )
    source_sha = _commit(repository, "content release")

    intent = _load(repository, source_sha)

    assert intent.stream == "scalekit"
    assert intent.bundle is False
    assert intent.version_mode is None
    assert intent.base_version is None
    assert intent.release_tag == "siteops/v1.5.0"
    assert intent.latest is True


def test_combined_preview_uses_selected_source_as_build_base(repository: Path):
    _write_source_version(repository, '__version__: str = "1.0.0b1"\n')
    _write_record(
        repository,
        {"tag": "v2.0.0.dev4", "siteops": {"build": True}},
    )
    source_sha = _commit(repository, "combined preview")

    intent = _load(repository, source_sha)

    assert intent.to_dict()["release"] == {
        "stream": "scalekit",
        "tag": "v2.0.0.dev4",
        "version": "2.0.0.dev4",
        "title": "v2.0.0.dev4: " + HEADLINE,
        "prerelease": True,
        "latest": False,
    }
    assert intent.to_dict()["siteops"] == {
        "bundle": True,
        "versionMode": "build",
        "baseVersion": "1.0.0b1",
        "releaseTag": None,
    }


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        (
            {"tag": "v2.0.0", "siteops": {"build": True}},
            "only for a prerelease",
        ),
        (
            {"tag": "v2.0.0", "siteops": {"release": "siteops/v1.0.0rc1"}},
            "must reference a stable",
        ),
        (
            {
                "tag": "v2.0.0rc1",
                "siteops": {"release": "siteops/v1.0.0"},
                "latest": True,
            },
            "Only a stable Scale Kit",
        ),
        (
            {"tag": "siteops/v1.0.0", "latest": True},
            "cannot be marked latest",
        ),
        (
            {"tag": "siteops/v1.0.0", "siteops": {"build": True}},
            "must not contain the siteops",
        ),
    ],
)
def test_maturity_latest_and_stream_rules_are_enforced(
    repository: Path,
    declaration: dict,
    message: str,
):
    _write_source_version(repository, '__version__ = "1.0.0"\n')
    _write_record(repository, declaration)
    source_sha = _commit(repository, "invalid consistency")

    with pytest.raises(ReleaseIntentError, match=message):
        _load(repository, source_sha)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'{"tag":"v1.0.0",', "valid UTF-8 JSON"),
        (
            b'{"tag":"v1.0.0","tag":"v1.0.1","siteops":{"release":"siteops/v1.0.0"}}',
            "duplicate JSON key",
        ),
        (
            b'{"tag":"v1.0.0","siteops":{"release":"siteops/v1.0.0"},"command":"run"}',
            "unknown fields",
        ),
        (b'["v1.0.0"]', "JSON object"),
        (
            b'{"tag":"v1.0.0","headline":"Example","siteops":{"release":"siteops/v1.0.0"},"latest":"yes"}',
            "must be a boolean",
        ),
        (b'{"tag":"v1.0.0","headline":"Example","siteops":[]}', "must be an object"),
        (
            b'{"tag":"v1.0.0+local","headline":"Example","siteops":{"release":"siteops/v1.0.0"}}',
            "must not contain a local",
        ),
        (
            b'{"tag":"v1.0.0-RC1","headline":"Example","siteops":{"release":"siteops/v1.0.0"}}',
            "canonical PEP 440",
        ),
        (
            b'{"tag":"v1.0.0rc1","headline":"Example","siteops":{"build":false}}',
            "must be exactly",
        ),
        (
            b'{"tag":"v1.0.0rc1","headline":"Example","siteops":{"release":"siteops/v1.0.0","extra":true}}',
            "must be exactly",
        ),
    ],
)
def test_malformed_duplicate_unknown_and_typed_declarations_are_rejected(
    repository: Path,
    raw: bytes,
    message: str,
):
    _write_record(repository, raw)
    source_sha = _commit(repository, "invalid declaration")

    with pytest.raises(ReleaseIntentError, match=message):
        _load(repository, source_sha)


@pytest.mark.parametrize("headline", [None, "", " leading", "trailing ", "line\nbreak", "tab\there", "\u202eTitle", "x" * 121, 42])
def test_release_headline_must_be_bounded_printable_text(repository, headline):
    _write_source_version(repository, '__version__ = "1.0.0"\n')
    _write_record(repository, {"tag": "siteops/v1.0.0", "headline": headline})
    source_sha = _commit(repository, "invalid headline")
    with pytest.raises(ReleaseIntentError, match="headline"):
        _load(repository, source_sha)


def test_release_headline_is_required_in_the_reviewed_declaration(repository):
    _write_source_version(repository, '__version__ = "1.0.0"\n')
    _write_record(repository, b'{"tag":"siteops/v1.0.0"}')
    with pytest.raises(ReleaseIntentError, match="headline"):
        _load(repository, _commit(repository, "missing headline"))


@pytest.mark.parametrize("headline", ["AIO resources and workspace validation", "x" * 120, "D\u00e9ploiement multi-site"])
def test_release_title_uses_only_the_selected_tag_and_headline(repository, headline):
    _write_source_version(repository, '__version__ = "1.0.0"\n')
    _write_record(repository, {"tag": "siteops/v1.0.0", "headline": headline})
    selected = _commit(repository, "original headline")
    _write_record(repository, {"tag": "siteops/v1.0.0", "headline": "New headline"})
    _commit(repository, "changed headline")
    intent = _load(repository, selected)
    assert intent.title == "siteops/v1.0.0: " + headline
    assert intent.version == "1.0.0"


@pytest.mark.parametrize(
    "intent_path",
    [
        "../release.json",
        "releases/name/child/release.json",
        r"releases\name\release.json",
        "releases/../release.json",
        "releases/CON/release.json",
        "/releases/name/release.json",
    ],
)
def test_intent_paths_are_exact_and_portable(repository: Path, intent_path: str):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
    )
    source_sha = _commit(repository, "valid record")

    with pytest.raises(ReleaseIntentError, match="release-file path|portable"):
        _load(repository, source_sha, intent_path)


@pytest.mark.parametrize(
    ("repository_name", "source_ref", "message"),
    [
        ("owner/repo/extra", SOURCE_REF, "OWNER/REPO"),
        ("../repo", SOURCE_REF, "OWNER/REPO"),
        (REPOSITORY, "main", "full safe"),
        (REPOSITORY, "refs/pull/1/head", "full safe"),
        (REPOSITORY, "refs/heads/bad..name", "full safe"),
    ],
)
def test_source_identity_strings_are_validated(
    repository: Path,
    repository_name: str,
    source_ref: str,
    message: str,
):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
    )
    source_sha = _commit(repository, "valid record")

    with pytest.raises(ReleaseIntentError, match=message):
        load_release_intent(
            repository,
            source_sha,
            "releases/candidate/release.json",
            repository_name,
            source_ref,
        )


def test_source_sha_must_be_a_full_commit_in_the_repository(repository: Path):
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    source_sha = _commit(repository, "source")
    blob_sha = _git(repository, "hash-object", "-w", "--stdin", input_bytes=b"not a commit")

    with pytest.raises(ReleaseIntentError, match="full lowercase"):
        inactive_release_plan(repository, "A" * 40, REPOSITORY, SOURCE_REF)
    with pytest.raises(ReleaseIntentError, match="must identify a commit"):
        inactive_release_plan(repository, blob_sha, REPOSITORY, SOURCE_REF)
    with pytest.raises(ReleaseIntentError, match="before SHA.*full lowercase"):
        discover_release_intent(repository, "abc", source_sha)


@pytest.mark.parametrize(
    ("selected_path", "mode"),
    [
        ("releases/candidate/notes.md", "120000"),
        ("releases/candidate/release.json", "160000"),
    ],
)
def test_nonregular_git_modes_are_rejected(
    repository: Path,
    selected_path: str,
    mode: str,
):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
    )
    first_commit = _commit(repository, "regular record")
    if mode == "120000":
        object_id = _git(repository, "hash-object", selected_path)
    else:
        object_id = first_commit
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{object_id},{selected_path}",
    )
    _git(repository, "commit", "--quiet", "-m", "nonregular record")
    source_sha = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(ReleaseIntentError, match="regular Git file"):
        _load(repository, source_sha)


@pytest.mark.parametrize(
    ("notes", "message"),
    [
        (b" \r\n\t", "must not be empty"),
        (b"unsafe\x00notes\n", "unsafe control"),
        (b"x" * (64 * 1024 + 1), "size limit"),
        (b"\xff", "valid UTF-8"),
    ],
    ids=["blank", "control", "oversized", "invalid-utf8"],
)
def test_notes_are_nonempty_bounded_utf8_without_unsafe_controls(
    repository: Path,
    notes: bytes,
    message: str,
):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
        notes=notes,
    )
    source_sha = _commit(repository, "invalid notes")

    with pytest.raises(ReleaseIntentError, match=message):
        _load(repository, source_sha)


def test_missing_notes_and_oversized_json_are_rejected(repository: Path):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
        notes=None,
    )
    missing_notes_sha = _commit(repository, "missing notes")
    with pytest.raises(ReleaseIntentError, match="release notes.*missing"):
        _load(repository, missing_notes_sha)

    raw = (
        b'{"tag":"v1.0.0","siteops":{"release":"siteops/v1.0.0"}}'
        + b" " * (16 * 1024)
    )
    _write_record(repository, raw)
    oversized_sha = _commit(repository, "oversized declaration")
    with pytest.raises(ReleaseIntentError, match="release file exceeds"):
        _load(repository, oversized_sha)


def test_discovery_tracks_metadata_and_notes_from_commit_trees(repository: Path):
    _write_record(
        repository,
        {"tag": "v1.0.0b1", "siteops": {"release": "siteops/v1.0.0"}},
    )
    initial = _commit(repository, "initial record")
    assert discover_release_intent(repository, ZERO_SHA, initial) == (
        "releases/candidate/release.json"
    )

    _write_record(
        repository,
        {"tag": "v1.0.0b2", "siteops": {"release": "siteops/v1.0.0"}},
    )
    metadata = _commit(repository, "metadata change")
    assert discover_release_intent(repository, initial, metadata) == (
        "releases/candidate/release.json"
    )

    (repository / "releases" / "candidate" / "notes.md").write_text(
        "Updated notes.\n",
        encoding="utf-8",
    )
    notes = _commit(repository, "notes change")
    assert discover_release_intent(repository, metadata, notes) == (
        "releases/candidate/release.json"
    )


def test_discovery_ignores_whole_deletion_but_load_rejects_partial_record(
    repository: Path,
):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
    )
    before = _commit(repository, "record")

    (repository / "releases" / "candidate" / "notes.md").unlink()
    partial = _commit(repository, "remove notes")
    path = discover_release_intent(repository, before, partial)
    assert path == "releases/candidate/release.json"
    with pytest.raises(ReleaseIntentError, match="release notes.*missing"):
        _load(repository, partial, path)

    (repository / "releases" / "candidate" / "release.json").unlink()
    deleted = _commit(repository, "remove declaration")
    assert discover_release_intent(repository, partial, deleted) is None


def test_discovery_rejects_multiple_surviving_candidates(repository: Path):
    for name in ("first", "second"):
        _write_record(
            repository,
            {"tag": "v1.0.0b1", "siteops": {"release": "siteops/v1.0.0"}},
            name=name,
        )
    source_sha = _commit(repository, "two records")

    with pytest.raises(ReleaseIntentError, match="More than one"):
        discover_release_intent(repository, ZERO_SHA, source_sha)


def test_inactive_plan_has_the_fixed_null_envelope(repository: Path):
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    source_sha = _commit(repository, "source")

    assert inactive_release_plan(
        repository,
        source_sha,
        REPOSITORY,
        "refs/tags/v1.0.0",
    ) == {
        "apiVersion": "siteops.release/v1",
        "kind": "ReleaseCandidate",
        "active": False,
        "dryRun": False,
        "source": {
            "repository": REPOSITORY,
            "commit": source_sha,
            "ref": "refs/tags/v1.0.0",
        },
        "intent": None,
        "release": None,
        "siteops": None,
    }


def test_cli_writes_bound_active_plan_from_selected_commit(repository: Path, tmp_path: Path):
    _write_source_version(repository, '__version__ = "1.0.0b1"\n')
    _, notes = _write_record(
        repository,
        {"tag": "v1.0.0b8", "siteops": {"build": True}},
        notes=b"## Candidate\r\n\r\nPinned notes.\r\n",
    )
    selected = _commit(repository, "candidate")
    _write_record(
        repository,
        {"tag": "v9.0.0", "siteops": {"release": "siteops/v9.0.0"}},
        notes=b"Wrong notes.\n",
    )
    _commit(repository, "advance")
    output = tmp_path / "active-output"

    result = _run_cli(
        repository,
        "--repository",
        REPOSITORY,
        "--source-sha",
        selected,
        "--source-ref",
        SOURCE_REF,
        "--output-dir",
        str(output),
        "--intent",
        "releases/candidate/release.json",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "Prepared release candidate.\n"
    assert result.stderr == ""
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["active"] is True
    assert plan["source"]["commit"] == selected
    assert plan["release"]["tag"] == "v1.0.0b8"
    assert (output / "release-notes.md").read_bytes() == notes


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_discovery_writes_inactive_plan_without_notes(repository: Path, tmp_path: Path, dry_run):
    (repository / "README.md").write_text("before\n", encoding="utf-8")
    before = _commit(repository, "before")
    (repository / "README.md").write_text("after\n", encoding="utf-8")
    source_sha = _commit(repository, "after")
    output = tmp_path / "inactive-output"

    result = _run_cli(
        repository,
        "--repository",
        REPOSITORY,
        "--source-sha",
        source_sha,
        "--source-ref",
        SOURCE_REF,
        "--output-dir",
        str(output),
        "--before-sha",
        before,
        *(["--dry-run"] if dry_run else []),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "No changed release intent.\n"
    assert result.stderr == ""
    assert json.loads((output / "plan.json").read_text(encoding="utf-8"))["active"] is False
    assert json.loads((output / "plan.json").read_text(encoding="utf-8"))["dryRun"] is dry_run
    assert not (output / "release-notes.md").exists()


@pytest.mark.parametrize("deleted", [False, True])
def test_discovery_rejects_surviving_misnested_records_but_allows_their_removal(repository, deleted):
    (repository / "README.md").write_text("before\n", encoding="utf-8")
    before = _commit(repository, "before")
    _write_record(
        repository, {"tag": "v1.0.0b8", "siteops": {"build": True}}, name="group/nested",
    )
    source_sha = _commit(repository, "misnested record")
    if deleted:
        before = source_sha
        for name in ("release.json", "notes.md"):
            (repository / "releases" / "group" / "nested" / name).unlink()
        source_sha = _commit(repository, "remove misnested record")
        assert discover_release_intent(repository, before, source_sha) is None
    else:
        with pytest.raises(ReleaseIntentError, match="releases/<name>/release.json"):
            discover_release_intent(repository, before, source_sha)


def test_cli_refuses_to_overwrite_an_existing_output_directory(
    repository: Path,
    tmp_path: Path,
):
    _write_record(
        repository,
        {"tag": "v1.0.0", "siteops": {"release": "siteops/v1.0.0"}},
    )
    source_sha = _commit(repository, "candidate")
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    result = _run_cli(
        repository,
        "--repository",
        REPOSITORY,
        "--source-sha",
        source_sha,
        "--source-ref",
        SOURCE_REF,
        "--output-dir",
        str(output),
        "--intent",
        "releases/candidate/release.json",
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("prepare-siteops-release: validation error:")
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert set(output.iterdir()) == {sentinel}

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Read a reviewed release intent from an immutable Git commit."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

_API_VERSION = "siteops.release/v1"
_KIND = "ReleaseCandidate"
_ZERO_SHA = "0" * 40
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,38})?/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
)
_INTENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REF_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REGULAR_GIT_MODES = {"100644", "100755"}
_MAX_DECLARATION_BYTES = 16 * 1024
_MAX_NOTES_BYTES = 64 * 1024
_MAX_SOURCE_BYTES = 256 * 1024
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ReleaseIntentError(ValueError):
    """The selected release declaration does not satisfy the release contract."""


@dataclass(frozen=True)
class ReleaseIntent:
    """A release plan bound to declaration and notes blobs in one Git commit."""

    repository: str
    source_sha: str
    source_ref: str
    intent_path: str
    intent_sha256: str
    notes_path: str
    notes_sha256: str
    stream: str
    tag: str
    version: str
    title: str
    prerelease: bool
    latest: bool
    bundle: bool
    version_mode: str | None
    base_version: str | None
    release_tag: str | None
    notes: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the stable release candidate plan."""
        return {
            "apiVersion": _API_VERSION,
            "kind": _KIND,
            "active": True,
            "dryRun": self.dry_run,
            "source": {
                "repository": self.repository,
                "commit": self.source_sha,
                "ref": self.source_ref,
            },
            "intent": {
                "path": self.intent_path,
                "sha256": self.intent_sha256,
                "notesPath": self.notes_path,
                "notesSha256": self.notes_sha256,
            },
            "release": {
                "stream": self.stream,
                "tag": self.tag,
                "version": self.version,
                "title": self.title,
                "prerelease": self.prerelease,
                "latest": self.latest,
            },
            "siteops": {
                "bundle": self.bundle,
                "versionMode": self.version_mode,
                "baseVersion": self.base_version,
                "releaseTag": self.release_tag,
            },
        }


def load_release_intent(
    root: Path,
    source_sha: str,
    intent_path: str,
    repository: str,
    source_ref: str,
    *,
    dry_run: bool = False,
) -> ReleaseIntent:
    """Load and validate one release declaration from the selected commit."""
    repository_root = _repository_root(root)
    source_sha = _require_commit(repository_root, source_sha, "source SHA")
    repository = _validate_repository(repository)
    source_ref = _validate_source_ref(source_ref)
    intent_path = _validate_intent_path(intent_path, allow_example=dry_run)
    notes_path = intent_path.removesuffix("release.json") + "notes.md"

    declaration_bytes = _read_tree_blob(
        repository_root,
        source_sha,
        intent_path,
        _MAX_DECLARATION_BYTES,
        "The release file",
    )
    notes_bytes = _read_tree_blob(
        repository_root,
        source_sha,
        notes_path,
        _MAX_NOTES_BYTES,
        "The release notes",
    )
    declaration = _parse_declaration(declaration_bytes)
    notes = _parse_notes(notes_bytes)

    tag = declaration["tag"]
    latest = declaration.get("latest", False)
    if tag.startswith("siteops/v"):
        if "siteops" in declaration:
            raise ReleaseIntentError(
                "A Site Ops release file must not contain the siteops field."
            )
        if latest:
            raise ReleaseIntentError("A Site Ops release cannot be marked latest.")
        version = _tag_version(tag, "siteops/v", "Site Ops release tag")
        source_version = _source_version(repository_root, source_sha)
        if str(version) != source_version:
            raise ReleaseIntentError(
                "The Site Ops release tag must exactly match the selected source version."
            )
        stream = "siteops"
        bundle = True
        version_mode = "source"
        base_version = source_version
        release_tag = None
    elif tag.startswith("v"):
        version = _tag_version(tag, "v", "Scale Kit release tag")
        siteops = declaration.get("siteops")
        if siteops is None:
            raise ReleaseIntentError(
                "A Scale Kit release file must contain the siteops field."
            )
        prerelease = _is_prerelease(version)
        stream = "scalekit"
        if set(siteops) == {"build"} and siteops["build"] is True:
            if not prerelease:
                raise ReleaseIntentError(
                    "A combined Site Ops build is allowed only for a prerelease Scale Kit version."
                )
            bundle = True
            version_mode = "build"
            base_version = _source_version(repository_root, source_sha)
            release_tag = None
        elif set(siteops) == {"release"} and type(siteops["release"]) is str:
            referenced_version = _tag_version(
                siteops["release"],
                "siteops/v",
                "Referenced Site Ops release tag",
            )
            if not prerelease and _is_prerelease(referenced_version):
                raise ReleaseIntentError(
                    "A stable Scale Kit release must reference a stable Site Ops release."
                )
            bundle = False
            version_mode = None
            base_version = None
            release_tag = siteops["release"]
        else:
            raise ReleaseIntentError(
                'The siteops field must be exactly {"build": true} or '
                '{"release": "siteops/v<version>"}.'
            )
        if latest and prerelease:
            raise ReleaseIntentError("Only a stable Scale Kit release can be marked latest.")
    else:
        raise ReleaseIntentError(
            "The release tag must start with v or siteops/v and contain a canonical version."
        )

    prerelease = _is_prerelease(version)
    return ReleaseIntent(
        repository=repository,
        source_sha=source_sha,
        source_ref=source_ref,
        intent_path=intent_path,
        intent_sha256=hashlib.sha256(declaration_bytes).hexdigest(),
        notes_path=notes_path,
        notes_sha256=hashlib.sha256(notes_bytes).hexdigest(),
        stream=stream,
        tag=tag,
        version=str(version),
        title=f"{tag}: {declaration['headline']}",
        prerelease=prerelease,
        latest=latest,
        bundle=bundle,
        version_mode=version_mode,
        base_version=base_version,
        release_tag=release_tag,
        notes=notes,
        dry_run=dry_run,
    )


def discover_release_intent(root: Path, before_sha: str, source_sha: str) -> str | None:
    """Find the one changed release declaration that survives in the selected commit."""
    repository_root = _repository_root(root)
    source_sha = _require_commit(repository_root, source_sha, "source SHA")
    before_sha = _validate_sha(before_sha, "before SHA", allow_zero=True)

    if before_sha == _ZERO_SHA:
        changed_paths = _list_tree_paths(repository_root, source_sha, "releases")
    else:
        _require_commit(repository_root, before_sha, "before SHA")
        changed_paths = _changed_tree_paths(repository_root, before_sha, source_sha)

    directories: set[str] = set()
    for path in changed_paths:
        parts = path.split("/")
        if parts[0] != "releases" or parts[-1] not in {"release.json", "notes.md"}:
            continue
        if len(parts) != 3:
            if _tree_entry(repository_root, source_sha, path) is not None:
                raise ReleaseIntentError(
                    "Release records must use releases/<name>/release.json and sibling notes.md."
                )
            continue
        candidate_path = _validate_intent_path(
            f"releases/{parts[1]}/release.json"
        )
        directories.add(candidate_path.removesuffix("/release.json"))

    surviving: list[str] = []
    for directory in sorted(directories):
        declaration_path = f"{directory}/release.json"
        notes_path = f"{directory}/notes.md"
        if (
            _tree_entry(repository_root, source_sha, declaration_path) is not None
            or _tree_entry(repository_root, source_sha, notes_path) is not None
        ):
            surviving.append(declaration_path)

    if len(surviving) > 1:
        raise ReleaseIntentError(
            "More than one changed release file survives in the selected commit. Select one file explicitly."
        )
    return surviving[0] if surviving else None


def inactive_release_plan(
    root: Path,
    source_sha: str,
    repository: str,
    source_ref: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Return the inactive plan after validating the selected source identity."""
    repository_root = _repository_root(root)
    source_sha = _require_commit(repository_root, source_sha, "source SHA")
    repository = _validate_repository(repository)
    source_ref = _validate_source_ref(source_ref)
    return {
        "apiVersion": _API_VERSION,
        "kind": _KIND,
        "active": False,
        "dryRun": dry_run,
        "source": {
            "repository": repository,
            "commit": source_sha,
            "ref": source_ref,
        },
        "intent": None,
        "release": None,
        "siteops": None,
    }


def _repository_root(root: Path) -> Path:
    try:
        repository_root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ReleaseIntentError("The repository root does not exist.") from error
    if not repository_root.is_dir():
        raise ReleaseIntentError("The repository root must be a directory.")
    result = _git(
        repository_root,
        "rev-parse",
        "--is-inside-work-tree",
        error_message="Git could not validate the repository root.",
    )
    if result.strip() != b"true":
        raise ReleaseIntentError("The repository root must be a Git worktree.")
    return repository_root


def _git(
    root: Path,
    *arguments: str,
    error_message: str,
) -> bytes:
    try:
        environment = {
            **os.environ,
            "GCM_INTERACTIVE": "Never",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            ["git", "-C", os.fspath(root), *arguments],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseIntentError(error_message) from error
    if result.returncode != 0:
        raise ReleaseIntentError(error_message)
    return result.stdout


def _validate_sha(value: str, label: str, *, allow_zero: bool = False) -> str:
    if type(value) is not str or _SOURCE_SHA.fullmatch(value) is None:
        raise ReleaseIntentError(f"The {label} must be a full lowercase Git SHA.")
    if value == _ZERO_SHA and not allow_zero:
        raise ReleaseIntentError(f"The {label} must identify a Git commit.")
    return value


def _require_commit(root: Path, value: str, label: str) -> str:
    value = _validate_sha(value, label)
    kind = _git(
        root,
        "cat-file",
        "-t",
        value,
        error_message=f"The {label} does not exist in this repository.",
    )
    if kind.strip() != b"commit":
        raise ReleaseIntentError(f"The {label} must identify a commit in this repository.")
    return value


def _validate_repository(value: str) -> str:
    if type(value) is not str or _REPOSITORY.fullmatch(value) is None:
        raise ReleaseIntentError("The repository must use the OWNER/REPO form.")
    owner, name = value.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."} or name.endswith("."):
        raise ReleaseIntentError("The repository must use the OWNER/REPO form.")
    return value


def _validate_source_ref(value: str) -> str:
    if type(value) is not str or len(value) > 255:
        raise ReleaseIntentError(
            "The source ref must be a full safe refs/heads/... or refs/tags/... ref."
        )
    prefix = next(
        (candidate for candidate in ("refs/heads/", "refs/tags/") if value.startswith(candidate)),
        None,
    )
    if prefix is None:
        raise ReleaseIntentError(
            "The source ref must be a full safe refs/heads/... or refs/tags/... ref."
        )
    suffix = value[len(prefix):]
    components = suffix.split("/")
    if (
        not suffix
        or "@{" in suffix
        or ".." in suffix
        or any(
            _REF_COMPONENT.fullmatch(component) is None
            or component.endswith(".")
            or component.lower().endswith(".lock")
            for component in components
        )
    ):
        raise ReleaseIntentError(
            "The source ref must be a full safe refs/heads/... or refs/tags/... ref."
        )
    return value


def _validate_intent_path(value: str, *, allow_example: bool = False) -> str:
    if type(value) is not str or "\\" in value or len(value) > 256:
        raise ReleaseIntentError(
            "The release-file path must be releases/<name>/release.json."
        )
    parts = value.split("/")
    if allow_example and len(parts) == 4 and parts[:2] == [".github", "release-examples"]:
        _validate_intent_path("/".join(["releases", *parts[2:]]))
        return value
    if len(parts) != 3 or parts[0] != "releases" or parts[2] != "release.json":
        raise ReleaseIntentError(
            "The release-file path must be releases/<name>/release.json."
        )
    name = parts[1]
    stem = name.split(".", 1)[0].upper()
    if (
        _INTENT_NAME.fullmatch(name) is None
        or name.endswith(".")
        or stem in _RESERVED_WINDOWS_NAMES
    ):
        raise ReleaseIntentError("The release intent name must be a portable path component.")
    return value


def _tree_entry(root: Path, commit: str, path: str) -> tuple[str, str, str] | None:
    output = _git(
        root,
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        path,
        error_message="Git could not inspect the selected source tree.",
    )
    if not output:
        return None
    records = [record for record in output.split(b"\0") if record]
    if len(records) != 1:
        raise ReleaseIntentError("The selected Git path is ambiguous.")
    try:
        metadata, encoded_path = records[0].split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        actual_path = encoded_path.decode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise ReleaseIntentError("The selected Git path is not portable.") from error
    if actual_path != path:
        raise ReleaseIntentError("The selected Git path is ambiguous.")
    return mode, kind, object_id


def _read_tree_blob(
    root: Path,
    commit: str,
    path: str,
    maximum: int,
    label: str,
) -> bytes:
    entry = _tree_entry(root, commit, path)
    if entry is None:
        raise ReleaseIntentError(f"{label} is missing from the selected commit.")
    mode, kind, object_id = entry
    if mode not in _REGULAR_GIT_MODES or kind != "blob":
        raise ReleaseIntentError(f"{label} must be a regular Git file.")
    encoded_size = _git(
        root,
        "cat-file",
        "-s",
        object_id,
        error_message=f"Git could not read {label.lower()}.",
    )
    try:
        size = int(encoded_size.strip())
    except ValueError as error:
        raise ReleaseIntentError(f"Git reported an invalid size for {label.lower()}.") from error
    if size > maximum:
        raise ReleaseIntentError(f"{label} exceeds its size limit.")
    content = _git(
        root,
        "cat-file",
        "blob",
        object_id,
        error_message=f"Git could not read {label.lower()}.",
    )
    if len(content) != size:
        raise ReleaseIntentError(f"Git returned incomplete content for {label.lower()}.")
    return content


def _list_tree_paths(root: Path, commit: str, path: str) -> tuple[str, ...]:
    output = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        "--full-tree",
        commit,
        "--",
        path,
        error_message="Git could not inspect the selected source tree.",
    )
    return _decode_paths(output)


def _changed_tree_paths(root: Path, before_sha: str, source_sha: str) -> tuple[str, ...]:
    output = _git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        "--no-renames",
        before_sha,
        source_sha,
        "--",
        "releases",
        error_message="Git could not compare the selected source trees.",
    )
    return _decode_paths(output)


def _decode_paths(output: bytes) -> tuple[str, ...]:
    try:
        return tuple(
            record.decode("utf-8") for record in output.split(b"\0") if record
        )
    except UnicodeError as error:
        raise ReleaseIntentError("Release paths in the selected Git tree must be UTF-8.") from error


def _parse_declaration(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseIntentError(
                    f"The release file contains a duplicate JSON key: {key}."
                )
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8")
        document = json.loads(text, object_pairs_hook=unique_object)
    except ReleaseIntentError:
        raise
    except (UnicodeError, ValueError) as error:
        raise ReleaseIntentError(
            "The release file must be valid UTF-8 JSON."
        ) from error
    if type(document) is not dict:
        raise ReleaseIntentError("The release file must be a JSON object.")
    unknown = set(document) - {"tag", "headline", "siteops", "latest"}
    if unknown:
        raise ReleaseIntentError(
            "The release file contains unknown fields: "
            + ", ".join(sorted(unknown))
            + "."
        )
    if "tag" not in document or type(document["tag"]) is not str:
        raise ReleaseIntentError("The release file tag must be a string.")
    if len(document["tag"]) > 160:
        raise ReleaseIntentError("The release file tag is too long.")
    headline = document.get("headline")
    if (
        type(headline) is not str or not 1 <= len(headline) <= 120
        or headline != headline.strip() or not headline.isprintable()
    ):
        raise ReleaseIntentError(
            "The release headline must be 1-120 printable characters with no surrounding whitespace."
        )
    if "latest" in document and type(document["latest"]) is not bool:
        raise ReleaseIntentError("The release file latest field must be a boolean.")
    if "siteops" in document and type(document["siteops"]) is not dict:
        raise ReleaseIntentError("The release file siteops field must be an object.")
    return document


def _tag_version(tag: str, prefix: str, label: str) -> Version:
    if len(tag) > 160:
        raise ReleaseIntentError(f"The {label.lower()} is too long.")
    if not tag.startswith(prefix):
        raise ReleaseIntentError(f"The {label.lower()} must start with {prefix}.")
    raw_version = tag[len(prefix):]
    try:
        version = Version(raw_version)
    except InvalidVersion as error:
        raise ReleaseIntentError(f"The {label.lower()} must contain a valid PEP 440 version.") from error
    if version.local is not None:
        raise ReleaseIntentError(f"The {label.lower()} must not contain a local version.")
    if raw_version != str(version):
        raise ReleaseIntentError(f"The {label.lower()} must use the canonical PEP 440 form.")
    return version


def _source_version(root: Path, source_sha: str) -> str:
    raw = _read_tree_blob(
        root,
        source_sha,
        "siteops/__init__.py",
        _MAX_SOURCE_BYTES,
        "The Site Ops version source",
    )
    try:
        tree = ast.parse(raw, filename="siteops/__init__.py")
    except (SyntaxError, ValueError) as error:
        raise ReleaseIntentError(
            "The selected Site Ops version source is not valid Python."
        ) from error

    values: list[Any] = []
    invalid_assignment = False
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets_version = any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in statement.targets
            )
            if targets_version:
                invalid_assignment = len(statement.targets) != 1
                values.append(statement.value)
        elif isinstance(statement, ast.AnnAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == "__version__":
                values.append(statement.value)
        elif isinstance(statement, ast.AugAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == "__version__":
                invalid_assignment = True

    if invalid_assignment or len(values) != 1:
        raise ReleaseIntentError(
            "The selected source must define one literal __version__ value."
        )
    value = values[0]
    if not isinstance(value, ast.Constant) or type(value.value) is not str:
        raise ReleaseIntentError(
            "The selected source must define __version__ as a string literal."
        )
    source_version = value.value
    try:
        parsed = Version(source_version)
    except InvalidVersion as error:
        raise ReleaseIntentError(
            "The selected source __version__ must be a valid PEP 440 version."
        ) from error
    if parsed.local is not None or source_version != str(parsed):
        raise ReleaseIntentError(
            "The selected source __version__ must use canonical PEP 440 without a local segment."
        )
    return source_version


def _parse_notes(raw: bytes) -> str:
    try:
        notes = raw.decode("utf-8")
    except UnicodeError as error:
        raise ReleaseIntentError("The release notes must be valid UTF-8 Markdown.") from error
    if not notes.strip():
        raise ReleaseIntentError("The release notes must not be empty.")
    for character in notes:
        if character not in {"\n", "\r", "\t"} and unicodedata.category(character) == "Cc":
            raise ReleaseIntentError("The release notes contain an unsafe control character.")
    return notes


def _is_prerelease(version: Version) -> bool:
    return version.is_prerelease or version.is_devrelease

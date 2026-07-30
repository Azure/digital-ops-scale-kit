"""Structural contract for API-version dispatchers.

An AIO release that introduces a new ARM API generation adds a module per
version and a routing branch in each dispatcher. The routing convention is that
the newest generation is the fallback arm of the selection expression and every
older generation is an explicit equality check.

That convention is otherwise stated only in template comments, and a
misrouted branch still compiles, so it is asserted here. Dispatchers are
discovered rather than listed, so a new one is covered on arrival.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ALLOWED_BLOCK = re.compile(
    r"@allowed\(\[(?P<body>[^\]]*)\]\)\s*param\s+(?P<param>\w*ApiVersion)\s+string",
    re.MULTILINE,
)
QUOTED = re.compile(r"'([^']+)'")


def _templates_root(workspace: Path) -> Path:
    return workspace / "templates"


def _discover_api_version_consumers(workspace: Path) -> list[tuple[Path, str, list[str]]]:
    """Find every template that constrains an API-version parameter.

    A consumer declares `@allowed` on a `*ApiVersion` parameter. Some route
    modules on it, others forward it to a router or key a variable off it. All of
    them must accept the same version set, because a release selects one value
    that every consumer on the path receives.
    """
    found: list[tuple[Path, str, list[str]]] = []
    for path in sorted(_templates_root(workspace).rglob("*.bicep")):
        source = path.read_text(encoding="utf-8")
        for match in ALLOWED_BLOCK.finditer(source):
            versions = QUOTED.findall(match.group("body"))
            if versions:
                found.append((path, match.group("param"), versions))
    return found


def _discover_dispatchers(workspace: Path) -> list[tuple[Path, str, list[str]]]:
    """Consumers that additionally condition a module on the parameter.

    Only these have a routing shape to assert.
    """
    return [
        (path, param, versions)
        for path, param, versions in _discover_api_version_consumers(workspace)
        if re.search(rf"=\s*if\s*\(\s*{param}\s*==", path.read_text(encoding="utf-8"))
    ]


def _module_condition_versions(source: str, param: str) -> list[str]:
    return re.findall(rf"=\s*if\s*\(\s*{param}\s*==\s*'([^']+)'\s*\)", source)


def _selection_equalities(source: str, param: str) -> list[str]:
    """Versions compared inside selection expressions rather than module conditions."""
    versions: list[str] = []
    for line in source.splitlines():
        if re.search(r"=\s*if\s*\(", line):
            continue  # module condition, not a selection arm
        versions.extend(re.findall(rf"{param}\s*==\s*'([^']+)'", line))
    return versions


def _dispatcher_ids(workspace: Path) -> list[str]:
    return [
        f"{p.relative_to(_templates_root(workspace))}:{param}"
        for p, param, _ in _discover_dispatchers(workspace)
    ]


class TestDispatchShape:
    """Every allowed API version must route, and route exactly once."""

    def test_dispatchers_are_discoverable(self, workspace):
        """Guards the discovery itself, so a rename cannot silently empty this suite."""
        consumers = _discover_api_version_consumers(workspace)
        dispatchers = _discover_dispatchers(workspace)

        assert consumers, "no API-version consumers found; discovery regex is likely stale"
        assert dispatchers, "no API-version dispatchers found; discovery regex is likely stale"

        consumer_paths = {p for p, _, _ in consumers}
        assert {p for p, _, _ in dispatchers} <= consumer_paths, (
            "every dispatcher must also be discovered as a consumer"
        )
        assert len(consumers) > len(dispatchers), (
            "expected consumers that forward the parameter without routing a module "
            "themselves; if that is no longer true, this assertion can go"
        )

    def test_every_allowed_version_has_exactly_one_module(self, workspace):
        for path, param, versions in _discover_dispatchers(workspace):
            source = path.read_text(encoding="utf-8")
            conditions = _module_condition_versions(source, param)

            for version in versions:
                assert conditions.count(version) == 1, (
                    f"{path.name}: '{version}' is allowed for {param} but has "
                    f"{conditions.count(version)} module conditions, expected 1"
                )

            unexpected = set(conditions) - set(versions)
            assert not unexpected, (
                f"{path.name}: modules route versions not in @allowed: {sorted(unexpected)}"
            )

    def test_newest_version_is_the_fallback_arm(self, workspace):
        """The newest generation must not be selected by an equality check.

        When a generation is added, the previously-newest arm has to become an
        explicit equality so the new one can take the fallback. Skipping that
        promotion leaves the older generation selecting the newer module.
        """
        for path, param, versions in _discover_dispatchers(workspace):
            source = path.read_text(encoding="utf-8")
            selection = _selection_equalities(source, param)
            if not selection:
                continue  # dispatches modules but selects nothing conditionally

            newest = max(versions)
            assert newest not in selection, (
                f"{path.name}: '{newest}' is the newest allowed {param} but is "
                "selected by an explicit equality. The newest generation is the "
                "fallback arm."
            )

            for version in versions:
                if version == newest:
                    continue
                assert version in selection, (
                    f"{path.name}: '{version}' is allowed for {param} but never "
                    "appears in a selection arm, so its module outputs are unreachable"
                )


class TestDispatcherAllowedSetsAgree:
    """Every consumer of the same parameter must allow the same versions.

    A release selects one API version and every consumer on the path receives it,
    so a consumer that forwards the value without routing a module still rejects
    the deployment when its allowed set lags.
    """

    def test_allowed_sets_match_across_consumers(self, workspace):
        by_param: dict[str, dict[str, list[str]]] = {}
        for path, param, versions in _discover_api_version_consumers(workspace):
            by_param.setdefault(param, {})[path.name] = sorted(versions)

        for param, per_file in by_param.items():
            distinct = {tuple(v) for v in per_file.values()}
            assert len(distinct) == 1, (
                f"consumers disagree on allowed {param} values: {per_file}"
            )


@pytest.mark.parametrize("param", ["aioApiVersion", "adrApiVersion"])
def test_release_api_versions_are_accepted_by_every_consumer(workspace, param):
    """Every API version a release selects must be accepted everywhere it flows."""
    import yaml

    releases_dir = workspace / "parameters" / "aio-releases"
    selected = set()
    for release_file in sorted(releases_dir.glob("*.yaml")):
        with open(release_file, "r", encoding="utf-8") as f:
            selected.add(yaml.safe_load(f)[param])

    consumers = [c for c in _discover_api_version_consumers(workspace) if c[1] == param]
    assert consumers, f"no consumer constrains {param}"

    for path, _, versions in consumers:
        missing = selected - set(versions)
        assert not missing, (
            f"{path.name}: releases select {param} values it rejects: {sorted(missing)}"
        )

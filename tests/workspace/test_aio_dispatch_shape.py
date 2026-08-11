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


MODULE_ARM = re.compile(
    r"^module\s+(?P<name>\w+)\s+'(?P<path>[^']+)'\s*=\s*if\s*\(\s*"
    r"(?P<param>\w*ApiVersion)\s*==\s*'(?P<version>[^']+)'\s*\)\s*\{"
    r"(?P<body>.*?)\n\}",
    re.MULTILINE | re.DOTALL,
)
PARAMS_BLOCK = re.compile(r"params:\s*\{(?P<body>.*?)\n  \}", re.DOTALL)
PARAM_PAIR = re.compile(r"^\s{4}(?P<key>\w+):\s*(?P<value>.+?)\s*$", re.MULTILINE)


def _module_arms(source: str, param: str) -> list[re.Match]:
    """Every conditional module block routed on `param`, in file order."""
    return [m for m in MODULE_ARM.finditer(source) if m.group("param") == param]


def _forwarded_params(arm: re.Match) -> tuple[tuple[str, str], ...]:
    """The key and value text each arm forwards, order-independent."""
    block = PARAMS_BLOCK.search(arm.group("body"))
    if not block:
        return ()
    return tuple(
        sorted(
            (m.group("key"), m.group("value"))
            for m in PARAM_PAIR.finditer(block.group("body"))
        )
    )


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
    """Versions compared inside selection expressions rather than module conditions.

    Comments are stripped first. A version named only in a comment would
    otherwise satisfy this, so removing the real equality while leaving the
    comment that describes it would go unnoticed.
    """
    versions: list[str] = []
    for line in source.splitlines():
        line = re.sub(r"//.*$", "", line)
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
        """Guards the discovery itself, so a rename cannot silently empty this suite.

        Checked per file as well as globally. A template that constrains an
        API version and conditions a module on it is a dispatcher, and if the
        discovery regex stops matching that one file, every routing assertion
        below goes quiet for it while the other dispatchers keep the global
        list non-empty.
        """
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

        discovered = {path for path, _, _ in dispatchers}
        failures: list[str] = []
        for path, param, versions in consumers:
            source = path.read_text(encoding="utf-8")
            # Matched loosely, tolerating any bracketing before the parameter,
            # so a condition the strict discovery regex misses is still seen
            # here. A file conditioning a module on something else, such as a
            # feature flag, does not match and is not a dispatcher.
            if not re.search(rf"=\s*if\s*\([^{{]*{param}\s*==", source):
                continue
            if path not in discovered:
                failures.append(
                    f"{path.name} conditions a module on {param} but was not "
                    f"discovered as a dispatcher, so every routing assertion "
                    f"skips it. The condition is probably written in a form the "
                    f"discovery regex does not match, such as extra parentheses."
                )
                continue
            arms = _module_arms(source, param)
            if len(arms) != len(versions):
                failures.append(
                    f"{path.name} allows {len(versions)} versions for {param} "
                    f"but only {len(arms)} module arm(s) were parsed. An arm "
                    f"written in an unmatched form is invisible to the checks "
                    f"below rather than reported by them."
                )
        assert failures == [], "\n".join(failures)

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


class TestDispatcherArmsAgree:
    """Each routing arm must reach the module for its own generation, whole.

    A dispatcher is a fan-out where every arm does the same work at a different
    API generation. A damaged arm compiles cleanly and only affects the releases
    routed to it, and a live run exercises one release, so the two defects below
    show up nowhere but here.
    """

    def test_each_module_path_names_the_version_it_routes(self, workspace):
        """A module file is named for its generation, so the path states the routing.

        Swapping two module paths leaves conditions, allowed sets, and every
        module's own contents correct, so the only evidence is the disagreement
        between an arm's condition and its path.
        """
        failures: list[str] = []
        for path, param, _ in _discover_dispatchers(workspace):
            source = path.read_text(encoding="utf-8")
            for arm in _module_arms(source, param):
                version = arm.group("version")
                module_path = arm.group("path")
                if version not in module_path:
                    failures.append(
                        f"{path.name}: the arm for '{version}' deploys "
                        f"'{module_path}', which does not name that generation. "
                        f"An arm must route to the module for its own version."
                    )
        assert failures == [], "\n".join(failures)

    def test_every_arm_forwards_the_same_parameters(self, workspace):
        """Every generation receives the same inputs, so none silently does less.

        An arm that drops a value, or substitutes a literal for it, deploys a
        fraction of the request on exactly the releases routed to it. For a
        catalog family that means a declared resource kind is never created, and
        the deploy still reports success.
        """
        failures: list[str] = []
        for path, param, _ in _discover_dispatchers(workspace):
            source = path.read_text(encoding="utf-8")
            arms = _module_arms(source, param)
            if len(arms) < 2:
                continue

            baseline = _forwarded_params(arms[0])
            assert baseline, (
                f"{path.name}: no parameters parsed from the arm for "
                f"'{arms[0].group('version')}'. The params block shape changed, "
                f"so update this test rather than deleting it."
            )
            for arm in arms[1:]:
                current = _forwarded_params(arm)
                if current == baseline:
                    continue
                missing = sorted(set(baseline) - set(current))
                extra = sorted(set(current) - set(baseline))
                failures.append(
                    f"{path.name}: the arm for '{arm.group('version')}' forwards "
                    f"different parameters than '{arms[0].group('version')}'. "
                    f"Only in the first: {missing}. Only in this one: {extra}."
                )
        assert failures == [], "\n".join(failures)


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

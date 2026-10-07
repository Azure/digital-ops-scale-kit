"""Linux bootstrap coverage through native uv, managed CPython and the shipped helper."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import native_bundle
from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_bundle import publish_assets
from tests.native_uv_consumers import _unavailable
from tests.shell_helpers import bash_path, required_bash

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.sh"
HARNESS = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
HELPER = ROOT / "scripts" / "siteops_distribution.py"
UV_ARCHIVE_SHA256 = "6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f"
PYTHON_ARCHIVE_SHA256 = "68c6739376b65258dee5058ccf6777232fe38d31a578965ae8bda327ec7da3a8"
linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Requires native Linux ownership and executables.",
)
# The fixture command records its arguments and visible Python settings.
RECORDING_CLI = """import json
import os
import sys

from siteops import __version__


def main():
    arguments = sys.argv[1:]
    record = os.environ.get("TEST_SITEOPS_LOG")
    if record:
        policy = None
        if arguments[:1] == ["--trust-policy"]:
            with open(arguments[1], encoding="utf-8") as stream:
                policy = json.load(stream)
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "arguments": arguments,
                "policy": policy,
                "python": sorted(name for name in os.environ if name.startswith("PYTHON")),
            }) + "\\n")
    if arguments == ["--version"]:
        print("siteops " + __version__)
"""


@pytest.fixture(scope="module")
def native_inputs() -> dict[str, Path]:
    """Return the pinned uv archive and the exact managed CPython archive."""
    selected = {}
    for name, digest in (
        ("SITEOPS_TEST_UV_ARCHIVE", UV_ARCHIVE_SHA256),
        ("SITEOPS_TEST_UV_PYTHON_ARCHIVE", PYTHON_ARCHIVE_SHA256),
    ):
        value = os.environ.get(name)
        path = Path(value) if value else None
        if path is None or not path.is_file():
            _unavailable(f"{name} must name the pinned Linux archive.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            _unavailable(f"{name} differs from the pinned Linux archive.")
        selected[name] = path
    for tool in ("openssl", "python3", "tar"):
        if shutil.which(tool) is None:
            _unavailable(f"The Linux bootstrap harness needs {tool}.")
    return selected


@pytest.fixture
def published(bundle_factory, tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    """Publish combined and independently versioned bundles with recording commands."""
    original = native_bundle.write_wheel

    def recording(path, **options):
        if options["name"] == "siteops":
            options["cli_source"] = RECORDING_CLI
        original(path, **options)

    monkeypatch.setattr(native_bundle, "write_wheel", recording)
    archives = []
    for number, source in ((1, "a"), (2, "b")):
        root, manifest = bundle_factory(number, source_sha=source * 40)
        archive, _ = publish_assets(root, manifest, tmp_path / f"release-{number}")
        archives.append(archive)
    root, manifest = bundle_factory(3, source_sha="d" * 40, version="1.2.3")
    archive, _ = publish_assets(root, manifest, tmp_path / "release-3")
    return archives[0], archives[1], archive


def _harness(tmp_path, scenario, script, published, native_inputs):
    result = subprocess.run(
        [str(required_bash()), bash_path(HARNESS), bash_path(script), bash_path(HELPER)],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "runner"),
            "TMPDIR": str(tmp_path),
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_SCENARIO": scenario,
            "TEST_BUNDLE_ARCHIVE": str(published[0]),
            "TEST_REPLACEMENT_ARCHIVE": str(published[1]),
            "TEST_REFERENCED_ARCHIVE": str(published[2]),
            "TEST_UV_ARCHIVE": str(native_inputs["SITEOPS_TEST_UV_ARCHIVE"]),
            "TEST_PYTHON_ARCHIVE": str(native_inputs["SITEOPS_TEST_UV_PYTHON_ARCHIVE"]),
        },
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-8000:]
    assert f"Scenario {scenario} passed" in result.stdout


@linux_only
@pytest.mark.parametrize(
    "scenario",
    ["journey", "root", "storage", "policy", "tools", "runtime", "prerequisites", "content"],
)
def test_linux_bootstrap_installs_through_native_uv_and_the_shipped_helper(
    tmp_path,
    published,
    native_inputs,
    scenario,
):
    _harness(tmp_path, scenario, SCRIPT, published, native_inputs)


def _guard(tmp_path: Path, body: str, *, private_group: bool) -> subprocess.CompletedProcess[str]:
    source = SCRIPT.read_text(encoding="utf-8")
    functions = []
    for name in ("writers_trusted", "trusted_parent", "admit_directory", "admit_file"):
        found = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}", source)
        assert found, f"The production {name} guard is missing."
        functions.append(found.group(0))
    script = tmp_path / "guard.sh"
    script.write_text(
        'set -euo pipefail\numask 077\nuid="$(id -u)"\n'
        + ('private_gid="$(id -g)"\n' if private_group else 'private_gid=""\n')
        + "\n".join(functions) + "\n" + body,
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


@linux_only
@pytest.mark.parametrize(
    ("setup", "check", "accepted", "private_group"),
    [
        (
            "mkdir -m 0750 home; mkdir -m 0755 home/.local home/.local/share",
            'admit_directory "$PWD/home/.local/share/siteops" private',
            True,
            False,
        ),
        ("mkdir -m 1777 sticky", 'admit_directory "$PWD/sticky/siteops" private', True, False),
        ("mkdir -m 0755 existing", 'admit_directory "$PWD/existing" shared', True, False),
        ("mkdir -m 0755 existing", 'admit_directory "$PWD/existing" private', False, False),
        ("mkdir -m 0777 open", 'admit_directory "$PWD/open/siteops" private', False, False),
        ("mkdir -m 0770 group", 'admit_directory "$PWD/group/siteops" shared', False, False),
        # The user's private group adds no other writer; other write never passes.
        ("mkdir -m 0775 group", 'admit_directory "$PWD/group/siteops" private', True, True),
        ("mkdir -m 0775 group", 'admit_directory "$PWD/group" shared', True, True),
        ("mkdir -m 0757 open", 'admit_directory "$PWD/open/siteops" private', False, True),
        ("printf x > tool; chmod 0775 tool", 'admit_file "$PWD/tool"', True, True),
        ("printf x > tool; chmod 0775 tool", 'admit_file "$PWD/tool"', False, False),
        ("mkdir real; ln -s real alias", 'admit_directory "$PWD/alias/siteops" private', False, False),
        ("mkdir a", 'admit_directory "$PWD/a/../b" shared && [[ "$admitted" == "$PWD/b" ]]', True, False),
        ("", "admit_directory relative/siteops shared", False, False),
        ("", 'admit_directory "$PWD/line"$\'\\n\'"break" shared', False, False),
        ("", 'admit_file "$(readlink -e /usr/bin/env)"', True, False),
        ("printf x > tool; chmod 0755 tool", 'admit_file "$PWD/tool"', True, False),
        ("printf x > tool; chmod 0777 tool", 'admit_file "$PWD/tool"', False, True),
        ("printf x > tool; ln -s tool linked", 'admit_file "$PWD/linked"', False, False),
    ],
)
def test_linux_storage_guard_distinguishes_trusted_neighbors(
    tmp_path, setup, check, accepted, private_group,
):
    # Fix modes so a login umask such as 0002 cannot change the expected result.
    tmp_path.chmod(0o700)
    work = tmp_path / "work"
    work.mkdir()
    work.chmod(0o755)
    result = _guard(work, f"cd {bash_path(work)}\n{setup}\n{check}\necho ADMITTED\n", private_group=private_group)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("ADMITTED" in result.stdout) is accepted
    if not accepted:
        assert not (work / "open" / "siteops").exists()
        assert not (work / "real" / "siteops").exists()


def test_linux_bootstrap_admits_uv_storage_and_runtime_before_execution():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "python list" not in source
    runtime = re.search(r"(?ms)^prepare_runtime\(\) \{.*?^\}", source).group()
    assert runtime.index('admit_directory "${!name}" shared') < runtime.index("\n  select_uv\n")
    assert runtime.index("\n  if $replace; then admit_bound_runtime; fi\n") < runtime.index(
        "\n  select_runtime\n",
    )
    assert source.index("\nprepare_runtime\n") < source.index(
        'installer_helper="$(extract_installer_helper)"',
    )
    selection = source.split("select_runtime() {", 1)[1].split("\n}\n", 1)[0]
    assert selection.index('admit_runtime_tree "$pydir/cpython-$selected') < selection.index(
        'identity="$("$python" -I -S -B',
    )

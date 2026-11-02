"""Linux bootstrap coverage through native uv, managed CPython and the shipped helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests import native_bundle
from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_bundle import publish_assets
from tests.native_uv_consumers import (
    LINUX_PREVIOUS_PYTHON_SHA256,
    LINUX_PYTHON_SHA256,
    LINUX_UV_ARCHIVE_SHA256,
    MANAGED_PYTHON,
    PREVIOUS_PYTHON,
    _unavailable,
)
from tests.shell_helpers import bash_path, required_bash

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.sh"
HARNESS = ROOT / "tests" / "fixtures" / "bootstrap-harness.sh"
HELPER = ROOT / "scripts" / "siteops_distribution.py"
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
        ("SITEOPS_TEST_UV_ARCHIVE", LINUX_UV_ARCHIVE_SHA256),
        ("SITEOPS_TEST_UV_PYTHON_ARCHIVE", LINUX_PYTHON_SHA256),
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
    # uv verifies a downloaded runtime against the digest built into the pinned uv.
    embedded = _embedded_python_digest(selected["SITEOPS_TEST_UV_ARCHIVE"])
    if embedded != LINUX_PYTHON_SHA256:
        pytest.fail(
            f"The pinned uv expects CPython {MANAGED_PYTHON} digest {embedded}, not the fixture "
            "digest. Update the runtime fixture pins with the uv pin.",
        )
    return selected


def _embedded_python_digest(uv_archive: Path) -> str | None:
    """Read the pinned runtime's entry from the download list built into uv."""
    with tarfile.open(uv_archive, "r:gz") as archive:
        executable = archive.extractfile("uv-x86_64-unknown-linux-gnu/uv").read()
    key = f'"cpython-{MANAGED_PYTHON}-linux-x86_64-gnu":'.encode("ascii")
    if executable.count(key) != 1:
        return None
    entry = re.compile(rb"\{(?:[^{}]|\{[^{}]*\})*\}").match(executable, executable.index(key) + len(key))
    return json.loads(entry.group()).get("sha256") if entry else None


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


def _harness(tmp_path, scenario, script, published, native_inputs, **extra):
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
            **extra,
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


@pytest.fixture(scope="module")
def previous_archive() -> Path:
    """Return the CPython archive that v1.0.0b7 bootstraps installed."""
    value = os.environ.get("SITEOPS_TEST_UV_PREVIOUS_PYTHON_ARCHIVE")
    path = Path(value) if value else None
    if (path is None or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != LINUX_PREVIOUS_PYTHON_SHA256):
        _unavailable("SITEOPS_TEST_UV_PREVIOUS_PYTHON_ARCHIVE must name the previous pinned Linux archive.")
    return path


@linux_only
def test_linux_existing_installation_keeps_its_runtime_beside_the_pinned_one(
    tmp_path,
    published,
    native_inputs,
    previous_archive,
):
    _harness(
        tmp_path, "previous", SCRIPT, published, native_inputs,
        TEST_PREVIOUS_PYTHON_ARCHIVE=str(previous_archive),
    )


@linux_only
def test_linux_bootstrap_names_why_a_mirrored_runtime_was_refused(
    tmp_path,
    published,
    native_inputs,
):
    _harness(tmp_path, "digest", SCRIPT, published, native_inputs)


def _fake_runtime(parent: Path, version: str, root: Path) -> None:
    """Write a concrete runtime whose interpreter records its use and reports its identity."""
    minor = version.rpartition(".")[0]
    concrete = parent / f"cpython-{version}-linux-x86_64-gnu"
    (concrete / "bin").mkdir(parents=True)
    for directory in (parent, concrete, concrete / "bin"):
        directory.chmod(0o755)
    executable = concrete / "bin" / f"python{minor}"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f': > "{root}/ran-{version}"\n'
        f"printf 'cpython {version.replace('.', ' ')} True %s\\n' \"$0\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _select_runtime(tmp_path: Path, run_uv: str) -> subprocess.CompletedProcess[str]:
    """Run the production runtime selection with a uv double and the fixture storage."""
    text = SCRIPT.read_text(encoding="utf-8")
    functions = []
    for name in ("writers_trusted", "trusted_parent", "admit_directory", "admit_file",
                 "admit_runtime_tree", "provision_runtime", "select_runtime"):
        found = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}", text)
        assert found, f"The production {name} function is missing."
        functions.append(found.group(0))
    script = tmp_path / "select.sh"
    script.write_text(
        'set -euo pipefail\numask 077\nuid="$(id -u)"\nprivate_gid=""\n'
        + "\n".join(functions)
        + r"""
fail() { printf 'FAIL: %s\n' "$1"; exit 1; }
stage() { printf 'STAGE: %s\n' "$1"; }
"""
        + run_uv
        + r"""
tools="$TEST_ROOT/tools"
pydir="$TEST_ROOT/python"
staging="$TEST_ROOT/staging"
mkdir -p "$pydir"
mkdir -m 0700 "$staging"
select_runtime
printf 'SELECTED %s\n' "$python"
""",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env={**os.environ, "TEST_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )


@linux_only
@pytest.mark.parametrize(("present", "bound", "expected"), [
    pytest.param(("3.10.20", PREVIOUS_PYTHON), None, MANAGED_PYTHON, id="new-beside-older"),
    pytest.param((PREVIOUS_PYTHON, MANAGED_PYTHON), None, MANAGED_PYTHON, id="new-beside-pinned"),
    pytest.param((PREVIOUS_PYTHON, MANAGED_PYTHON), PREVIOUS_PYTHON, PREVIOUS_PYTHON, id="bound-previous"),
    pytest.param(("3.10.20",), "3.10.20", "3.10.20", id="bound-end-of-life"),
])
def test_linux_new_installation_uses_only_the_pinned_runtime(tmp_path, present, bound, expected):
    tmp_path.chmod(0o700)
    pydir = tmp_path / "python"
    tools = tmp_path / "tools"
    source = tmp_path / "source"
    for version in present:
        _fake_runtime(pydir, version, tmp_path)
    _fake_runtime(source, MANAGED_PYTHON, tmp_path)
    tools.mkdir(mode=0o700)
    if bound:
        tool = tools / "siteops"
        tool.mkdir(mode=0o700)
        configuration = tool / "pyvenv.cfg"
        configuration.write_text(
            f"home = {pydir}/cpython-{bound}-linux-x86_64-gnu/bin\nversion_info = {bound}\n",
            encoding="utf-8",
        )
        configuration.chmod(0o600)
    # The download source holds only the pinned runtime.
    result = _select_runtime(tmp_path, r"""
run_uv() {
  printf '%s\n' "$*" >> "$TEST_ROOT/uv.log"
  cp -R -- "$TEST_ROOT/source/cpython-$3-linux-x86_64-gnu" "$pydir/"
}
""")
    assert result.returncode == 0, result.stdout + result.stderr
    minor = expected.rpartition(".")[0]
    assert f"SELECTED {pydir}/cpython-{expected}-linux-x86_64-gnu/bin/python{minor}" in result.stdout
    provisioned = MANAGED_PYTHON not in present and not bound
    log = tmp_path / "uv.log"
    assert log.exists() is provisioned
    if provisioned:
        assert log.read_text(encoding="utf-8") == f"python install {MANAGED_PYTHON} --no-bin --no-registry\n"
    assert {path.name for path in tmp_path.glob("ran-*")} == {f"ran-{expected}"}


@linux_only
@pytest.mark.parametrize(("report", "code"), [
    pytest.param(
        f"Downloading cpython-{MANAGED_PYTHON}-linux-x86_64-gnu (download) (24.1MiB)\n"
        f"error: Failed to install cpython-{MANAGED_PYTHON}-linux-x86_64-gnu\n"
        "  cause: Failed to extract archive: private-source-detail.tar.gz\n"
        "  cause: No space left on device (os error 28)",
        2,
        id="extraction",
    ),
    pytest.param("", 124, id="silent-timeout"),
    # Only a bounded part of uv's report is read.
    pytest.param(
        "x" * 70000 + "\n  cause: Failed to download https://private-source-detail.invalid/cpython.tar.gz\n",
        1,
        id="beyond-bound",
    ),
])
def test_linux_other_runtime_provisioning_failures_keep_the_runtime_remedies(tmp_path, report, code):
    tmp_path.chmod(0o700)
    (tmp_path / "tools").mkdir(mode=0o700)
    # An older managed runtime must not replace the refused download.
    _fake_runtime(tmp_path / "python", PREVIOUS_PYTHON, tmp_path)
    (tmp_path / "report").write_text(report, encoding="utf-8")
    result = _select_runtime(tmp_path, f'run_uv() {{ cat -- "$TEST_ROOT/report" >&2; return {code}; }}\n')
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == (
        f"FAIL: CPython {MANAGED_PYTHON} could not be provisioned through uv. Allow uv's runtime source, "
        "set an approved HTTPS UV_PYTHON_INSTALL_MIRROR, or prepare the runtime with "
        f"uv python install {MANAGED_PYTHON} --no-bin --no-registry, then retry."
    )
    assert "private-source-detail" not in result.stdout + result.stderr
    assert "SELECTED" not in result.stdout
    assert not list(tmp_path.glob("ran-*"))


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

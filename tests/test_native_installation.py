"""Exercise stock pipx lifecycle with local wheels and isolated application state."""

import builtins
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import native_bundle
from tests.native_bundle import (
    DEPENDENCY_NAME,
    DEPENDENCY_VERSION,
    native_only,
)
from tests.native_bundle import (
    backend_wheelhouse as backend_wheelhouse,
)
from tests.native_bundle import (
    bundle_factory as bundle_factory,
)
from tests.native_bundle import (
    pipx_state as pipx_state,
)
from tests.native_bundle import (
    shared_backend as shared_backend,
)

pytestmark = native_only


def test_fixture_lock_generation_does_not_require_the_build_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        native_bundle, "sys", SimpleNamespace(version_info=(3, 10), modules=sys.modules),
    )
    original_import = builtins.__import__

    def import_without_toml(name, *args, **kwargs):
        if name in {"tomllib", "tomli"}:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_toml)
    try:
        create = native_bundle.bundle_factory.__wrapped__(tmp_path, monkeypatch)
    except pytest.skip.Exception:
        pytest.fail("Native consumer fixtures must remain available on Python 3.10.")
    root, manifest = create(1)
    assert (root / "pylock.toml").is_file()
    assert any(target.python == "3.10" for target in manifest.targets)


def _tamper(root: Path, destination: Path, wheel: str) -> Path:
    """Copy a bundle and change one recorded wheel byte for byte."""
    shutil.copytree(root, destination)
    with (destination / wheel).open("ab") as stream:
        stream.write(b"changed after the producer recorded this wheel")
    return destination


def _wheel_requirement(wheel: Path) -> str:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return f"siteops @ {wheel.resolve().as_uri()}#sha256={digest}"


def _online_manifest(path: Path, wheel: Path) -> Path:
    """Write the lock-free pipx manifest the guide uses to leave the locked state."""
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "siteops-installation"',
                'version = "1"',
                "dependencies = []",
                "[dependency-groups]",
                "siteops = " + json.dumps([_wheel_requirement(wheel)]),
                "[tool.pipx]",
                'version = "1.0"',
                "[tool.pipx.tools.siteops]",
                'apps = ["siteops"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_verified_lock_installs_repeats_repairs_and_removes(pipx_state, bundle_factory):
    root, manifest = bundle_factory(1)

    pipx_state.install_locked(root, label="install")
    assert pipx_state.version() == f"siteops {manifest.version}"
    assert pipx_state.command.parent == pipx_state.root / "bin"

    pipx_state.install_locked(root, label="repeat")
    assert pipx_state.version() == f"siteops {manifest.version}"

    pipx_state.install_locked(root, "--force", label="repair")
    assert pipx_state.version() == f"siteops {manifest.version}"

    metadata = pipx_state.metadata()
    assert metadata["main_package"]["package_version"] == manifest.version
    assert metadata["main_package"]["package_or_url"] == "siteops"
    assert metadata["main_package"]["pip_args"] == [
        "--isolated", "--require-hashes", "--no-index", "--only-binary=:all:", "--no-cache-dir",
    ]

    pipx_state.run("uninstall", "siteops", label="uninstall")
    assert not pipx_state.command.exists()
    assert "siteops" not in json.loads(
        pipx_state.run("list", "--output", "json", label="list-after-removal").stdout
    )["venvs"]


def test_failed_replacement_preserves_the_previous_command(pipx_state, bundle_factory, tmp_path):
    root_a, manifest_a = bundle_factory(1)
    root_b, manifest_b = bundle_factory(2)
    tampered = _tamper(root_b, tmp_path / "tampered bundle", manifest_b.application_wheel)

    pipx_state.install_locked(root_a, label="install")
    assert pipx_state.version() == f"siteops {manifest_a.version}"

    pipx_state.install_locked(tampered, "--force", expect=1, label="tampered")
    assert pipx_state.version() == f"siteops {manifest_a.version}"

    # The recorded lock stays valid only while its retained bundle stays in place.
    moved = tmp_path / "relocated bundle"
    root_a.rename(moved)
    try:
        pipx_state.run(
            "reinstall", "siteops", "--backend", "pip", "--skip-maintenance",
            expect=1, label="repair-without-retained-files",
        )
        assert pipx_state.version() == f"siteops {manifest_a.version}"
    finally:
        moved.rename(root_a)


def test_selected_builds_replace_in_both_directions(pipx_state, bundle_factory):
    root_a, manifest_a = bundle_factory(1)
    root_b, manifest_b = bundle_factory(2)

    pipx_state.install_locked(root_a, label="install-a")
    assert pipx_state.version() == f"siteops {manifest_a.version}"

    pipx_state.install_locked(root_b, "--force", label="replace-b")
    assert pipx_state.version() == f"siteops {manifest_b.version}"
    assert Path(pipx_state.metadata()["main_package"]["lock_file"]["__Path__"]) == (
        root_b / "pylock.toml"
    )

    pipx_state.install_locked(root_a, "--force", label="downgrade-a")
    assert pipx_state.version() == f"siteops {manifest_a.version}"
    assert Path(pipx_state.metadata()["main_package"]["lock_file"]["__Path__"]) == (
        root_a / "pylock.toml"
    )


def test_lock_transitions_use_explicit_native_commands(pipx_state, bundle_factory, tmp_path):
    root_a, manifest_a = bundle_factory(1)
    root_b, manifest_b = bundle_factory(2)
    wheel_b = root_b / manifest_b.application_wheel

    pipx_state.install_locked(root_a, label="install-a")
    assert pipx_state.metadata()["main_package"]["lock_file"] is not None

    # An ordinary named request does not silently clear a recorded lock.
    pipx_state.run(
        "install", f"siteops=={manifest_b.version}", "--force",
        "--backend", "pip", "--fetch-python", "never", "--skip-maintenance", "--app", "siteops",
        "--pip-args",
        "--no-index --only-binary=:all: --no-cache-dir "
        f"--find-links={wheel_b.parent.as_uri()}",
        expect=1, label="ordinary-while-locked",
    )
    assert pipx_state.version() == f"siteops {manifest_a.version}"
    assert pipx_state.metadata()["main_package"]["lock_file"] is not None

    # The supported transition to an unlocked source is an explicit manifest sync.
    manifest_path = _online_manifest(tmp_path / "siteops-online.toml", wheel_b)
    pipx_state.run(
        "manifest", "sync", manifest_path, "--backend", "pip", "--skip-maintenance",
        label="manifest-sync",
        env_overrides={"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": wheel_b.parent.as_uri()},
    )
    assert pipx_state.version() == f"siteops {manifest_b.version}"
    assert pipx_state.metadata()["main_package"]["lock_file"] is None

    # Returning to the verified state needs no metadata edit or removal first.
    pipx_state.install_locked(root_a, "--force", label="back-to-verified")
    assert pipx_state.version() == f"siteops {manifest_a.version}"
    assert pipx_state.metadata()["main_package"]["lock_file"] is not None


def test_pinning_and_injection_are_operator_decisions(pipx_state, bundle_factory):
    root_a, manifest_a = bundle_factory(1)
    root_b, manifest_b = bundle_factory(2)
    dependency = next(
        path for path in (root_a / "wheels").iterdir() if path.name.startswith("siteops_fixture")
    )

    pipx_state.install_locked(root_a, label="install-a")
    pipx_state.run("pin", "siteops", label="pin")
    assert pipx_state.metadata()["main_package"]["pinned"] is True

    # pipx refuses to inject into a locked environment, so the verified payload
    # stays exactly what the producer recorded.
    refused = pipx_state.run(
        "inject", "siteops", f"{DEPENDENCY_NAME}=={DEPENDENCY_VERSION}",
        "--backend", "pip", "--skip-maintenance",
        "--pip-args",
        "--isolated --no-index --only-binary=:all: --no-cache-dir "
        f"--find-links={dependency.parent.as_uri()}",
        expect=1, label="inject-into-locked",
    )
    assert "locked environment" in (refused.stdout + refused.stderr)
    assert pipx_state.metadata()["injected_packages"] == {}

    # Explicit force is stronger than a pin, which is why the guide treats
    # replacement as a deliberate operator action rather than a repair detail.
    pipx_state.install_locked(root_b, "--force", label="forced-over-pin")
    assert manifest_a.version != manifest_b.version
    assert pipx_state.version() == f"siteops {manifest_b.version}"


def test_an_unrelated_exposed_command_is_never_replaced(pipx_state, bundle_factory):
    root, _ = bundle_factory(1)
    foreign = pipx_state.command
    foreign.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"an unrelated command that pipx must not overwrite\n"
    foreign.write_bytes(sentinel)

    pipx_state.install_locked(root, label="install-with-foreign-command")
    assert foreign.read_bytes() == sentinel

    pipx_state.run("uninstall", "siteops", label="uninstall")
    assert foreign.read_bytes() == sentinel


def test_the_online_path_installs_one_wheel_from_a_configured_feed(pipx_state, bundle_factory):
    root, manifest = bundle_factory(1)
    wheel = root / manifest.application_wheel

    pipx_state.install_wheel(wheel, label="install-wheel")
    assert pipx_state.version() == f"siteops {manifest.version}"
    assert pipx_state.metadata()["main_package"]["lock_file"] is None

    pipx_state.run("uninstall", "siteops", label="uninstall")
    assert not pipx_state.command.exists()


def test_verified_lock_enforces_dependency_hashes_despite_ambient_pip_options(
    pipx_state, bundle_factory, tmp_path,
):
    root, manifest = bundle_factory(1)
    dependency = next(path for path in manifest.targets[0].wheels if path != manifest.application_wheel)
    tampered = _tamper(root, tmp_path / "tampered dependency", dependency)
    prefix = tmp_path / "unexpected prefix"
    overrides = {
        "PIP_NO_REQUIRE_HASHES": "1", "PIP_PREFIX": str(prefix),
        "PIP_FIND_LINKS": "http://127.0.0.1:9/unwanted",
    }
    pipx_state.install_locked(root, env_overrides=overrides, label="isolated-install")
    assert pipx_state.version() == f"siteops {manifest.version}"
    pipx_state.install_locked(
        tampered, "--force", expect=1, env_overrides=overrides, label="isolated-tamper",
    )
    assert pipx_state.version() == f"siteops {manifest.version}"
    assert not prefix.exists()


def test_every_supported_target_selects_the_same_locked_wheels(bundle_factory):
    root, manifest = bundle_factory(1)
    lock = (root / "pylock.toml").read_text(encoding="utf-8")
    for target in manifest.targets:
        assert f"python_version == '{target.python}'" in lock
    assert lock.count("[[packages]]") == 2
    assert f'name = "{DEPENDENCY_NAME}"' in lock
    assert os.name != "nt" or "\\" not in lock

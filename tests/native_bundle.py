"""Build synthetic wheels and published bundle fixtures with producer metadata."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BUILD_REQUIREMENTS = SCRIPTS / "siteops-build-requirements.txt"
DEPENDENCY_NAME = "siteops-fixture-dependency"
DEPENDENCY_VERSION = "1.0"
BACKEND_WHEELHOUSE_VARIABLE = "SITEOPS_TEST_BACKEND_WHEELHOUSE"
SUPPORTED_PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
SUPPORTED_PLATFORMS = ("windows-x86_64", "linux-x86_64")
NETWORK_BLOCK = {
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "",
}

native_only = pytest.mark.skipif(
    sys.platform not in {"win32", "linux"},
    reason="Native installation targets Windows and Linux.",
)


def pinned_backend() -> tuple[str, str]:
    """Return the pip version and hash the repository pins for build tooling."""
    text = BUILD_REQUIREMENTS.read_text(encoding="utf-8")
    for block in text.replace("\\\n", " ").splitlines():
        name, _, remainder = block.partition("==")
        if name.strip() != "pip":
            continue
        version, _, hashes = remainder.partition(" ")
        digest = hashes.strip().removeprefix("--hash=sha256:")
        return version.strip(), digest
    raise AssertionError("The committed build requirements must pin the build-tool pip.")


def _record(contents: dict[str, bytes], prefix: str) -> bytes:
    rows = [
        (
            name,
            "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode(),
            len(body),
        )
        for name, body in contents.items()
    ]
    rows.append((prefix + "/RECORD", "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(rows)
    return stream.getvalue().encode()


def write_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    module: str,
    entry_point: str | None = None,
    requires: tuple[str, ...] = (),
    cli_source: str | None = None,
) -> None:
    """Write a minimal, valid pure Python wheel for lifecycle coverage."""
    prefix = f"{name.replace('-', '_')}-{version}.dist-info"
    contents = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{module}/cli.py": (cli_source if cli_source is not None else (
            f"from {module} import __version__\n"
            "def main():\n"
            f'    print("{module} " + __version__)\n'
        )).encode(),
        prefix + "/METADATA": (
            f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.10\n"
            + "".join(f"Requires-Dist: {value}\n" for value in requires)
        ).encode(),
        prefix + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if entry_point:
        contents[prefix + "/entry_points.txt"] = (
            f"[console_scripts]\n{entry_point} = {module}.cli:main\n"
        ).encode()
    contents[prefix + "/RECORD"] = _record(contents, prefix)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for member, body in contents.items():
            wheel.writestr(member, body)


@pytest.fixture
def bundle_factory(tmp_path, monkeypatch):
    """Create native bundles that carry the producer's public layout."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from siteops_distribution import BundleManifest, BundleTarget, PayloadFile

    spec = importlib.util.spec_from_file_location(
        "siteops_native_fixture_builder", SCRIPTS / "build-siteops-bundle.py",
    )
    builder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = builder
    spec.loader.exec_module(builder)

    def create(number: int = 1, *, app: bool = True, version: str | None = None, source_sha: str = "a" * 40):
        root = tmp_path / f"bundle {number}"
        (root / "wheels").mkdir(parents=True)
        base_version = version or "1.0.0b1"
        version = version or f"1.0.0b1+build.{number}.1.g{source_sha[:12]}"
        application = root / "wheels" / f"siteops-{version}-py3-none-any.whl"
        dependency = (
            root / "wheels"
            / f"{DEPENDENCY_NAME.replace('-', '_')}-{DEPENDENCY_VERSION}-py3-none-any.whl"
        )
        write_wheel(
            application,
            name="siteops",
            version=version,
            module="siteops",
            entry_point="siteops" if app else None,
            requires=(f"{DEPENDENCY_NAME}=={DEPENDENCY_VERSION}",),
        )
        write_wheel(
            dependency,
            name=DEPENDENCY_NAME,
            version=DEPENDENCY_VERSION,
            module="siteops_fixture_dependency",
        )
        wheels = (
            application.relative_to(root).as_posix(),
            dependency.relative_to(root).as_posix(),
        )
        targets = tuple(
            BundleTarget(python=python, platform=platform, wheels=wheels)
            for python in SUPPORTED_PYTHONS
            for platform in SUPPORTED_PLATFORMS
        )
        builder._write_pylock(root, targets)
        for notice in ("LICENSE", "ThirdPartyNotices.txt"):
            (root / notice).write_text("Synthetic fixture notice.\n", encoding="utf-8")
        (root / "siteops-install.py").write_bytes((SCRIPTS / "siteops_distribution.py").read_bytes())
        files = tuple(
            PayloadFile(
                path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size=path.stat().st_size,
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )
        manifest = BundleManifest(
            version=version,
            base_version=base_version,
            repository="example/publisher",
            source_sha=source_sha,
            source_ref="refs/heads/main",
            build_number=number,
            build_attempt=1,
            application_wheel=wheels[0],
            targets=targets,
            files=files,
        )
        (root / "bundle.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return root, manifest

    return create


def _backend_wheelhouse(destination: Path) -> Path:
    """Provide the pinned backend wheel without touching operator tooling."""
    version, digest = pinned_backend()
    destination.mkdir(parents=True, exist_ok=True)
    supplied = os.environ.get(BACKEND_WHEELHOUSE_VARIABLE)
    if supplied:
        for candidate in sorted(Path(supplied).glob(f"pip-{version}-*.whl")):
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
                target = destination / candidate.name
                target.write_bytes(candidate.read_bytes())
                return destination
        pytest.fail(
            f"{BACKEND_WHEELHOUSE_VARIABLE} does not hold the pinned pip {version} wheel.",
        )
    requirement = destination.parent / "shared-backend.txt"
    requirement.write_text(f"pip=={version} --hash=sha256:{digest}\n", encoding="utf-8")
    download = subprocess.run(
        [
            sys.executable, "-m", "pip", "download", "--no-cache-dir",
            "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
            "--no-deps", "--dest", str(destination), "--requirement", str(requirement),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if download.returncode or not list(destination.glob(f"pip-{version}-*.whl")):
        pytest.fail(
            f"The pinned pip {version} wheel is unavailable. Set "
            f"{BACKEND_WHEELHOUSE_VARIABLE} to a directory holding it for offline runs.",
        )
    return destination


def publish_assets(root: Path, manifest, destination: Path) -> tuple[Path, Path]:
    """Lay out the producer's two published files: the archive and its wheel.

    The producer owns real publication. This mirrors only the resulting file
    layout so the workflow's own validation shell can be executed against it.
    """
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "siteops-install.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(root).as_posix())
    wheel = destination / Path(manifest.application_wheel).name
    wheel.write_bytes((root / manifest.application_wheel).read_bytes())
    return archive, wheel

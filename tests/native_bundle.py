"""Build synthetic wheels with producer metadata and run pipx in isolated state."""

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
from dataclasses import dataclass
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
    """Return the pip version and hash the repository pins for the pipx backend."""
    text = BUILD_REQUIREMENTS.read_text(encoding="utf-8")
    for block in text.replace("\\\n", " ").splitlines():
        name, _, remainder = block.partition("==")
        if name.strip() != "pip":
            continue
        version, _, hashes = remainder.partition(" ")
        digest = hashes.strip().removeprefix("--hash=sha256:")
        return version.strip(), digest
    raise AssertionError("The committed build requirements must pin the pipx backend pip.")


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
) -> None:
    """Write a minimal, valid pure Python wheel for lifecycle coverage."""
    prefix = f"{name.replace('-', '_')}-{version}.dist-info"
    contents = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{module}/cli.py": (
            f"from {module} import __version__\n"
            "def main():\n"
            f'    print("{module} " + __version__)\n'
        ).encode(),
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

    def create(number: int = 1, *, app: bool = True):
        root = tmp_path / f"bundle {number}"
        (root / "wheels").mkdir(parents=True)
        version = f"1.0.0b1+build.{number}.1.gaaaaaaaaaaaa"
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
            base_version="1.0.0b1",
            repository="example/publisher",
            source_sha="a" * 40,
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


def pipx_program() -> Path:
    """Resolve the pipx installed beside the running interpreter."""
    program = Path(sys.executable).parent / ("pipx.exe" if os.name == "nt" else "pipx")
    if not program.is_file():
        pytest.skip("Native lifecycle coverage requires the pinned pipx development tool.")
    return program


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


@dataclass(frozen=True)
class PipxState:
    """One isolated pipx installation area owned by a single test."""

    root: Path
    program: Path
    environment: dict[str, str]
    logs: Path

    @property
    def command(self) -> Path:
        return self.root / "bin" / ("siteops.exe" if os.name == "nt" else "siteops")

    def run(self, *arguments, expect: int = 0, label: str | None = None, env_overrides=None):
        name = label or f"step-{len(list(self.logs.glob('*.log'))) + 1}"
        environment = dict(self.environment)
        environment.update(env_overrides or {})
        result = subprocess.run(
            [str(self.program), *(str(value) for value in arguments)],
            cwd=self.root / "unrelated",
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        (self.logs / f"{name}.log").write_text(
            result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8",
        )
        if expect is not None:
            excerpt = "\n".join((result.stdout + result.stderr).strip().splitlines()[-12:])
            assert result.returncode == expect, f"{name} exited {result.returncode}\n{excerpt}"
        return result

    def install_locked(
        self, bundle: Path, *arguments, expect: int = 0, label: str | None = None,
        env_overrides=None,
    ):
        """Install the verified lock with the qualified strict policy."""
        return self.run(
            "install", "siteops",
            "--lock", bundle / "pylock.toml",
            "--backend", "pip",
            "--fetch-python", "never",
            "--skip-maintenance",
            "--app", "siteops",
            "--pip-args", "--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir",
            *arguments,
            expect=expect,
            label=label,
            env_overrides=env_overrides,
        )

    def install_wheel(self, wheel: Path, *arguments, expect: int = 0, label: str | None = None):
        """Install one wheel the way the online path does, from a local feed."""
        return self.run(
            "install", wheel,
            "--backend", "pip",
            "--fetch-python", "never",
            "--skip-maintenance",
            "--app", "siteops",
            "--pip-args",
            "--no-cache-dir --no-index --only-binary=:all: --find-links="
            + wheel.parent.as_uri(),
            *arguments,
            expect=expect,
            label=label,
        )

    def version(self) -> str:
        observed = subprocess.run(
            [str(self.command), "--version"],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert observed.returncode == 0, observed.stdout + observed.stderr
        return observed.stdout.strip()

    def metadata(self) -> dict:
        listed = self.run("list", "--output", "json", label="list")
        return json.loads(listed.stdout)["venvs"]["siteops"]["metadata"]


def pipx_environment(root: Path, shared: Path) -> dict[str, str]:
    """Return an environment whose pipx, pip, and profile state stays in `root`."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIP_", "PIPX_"))
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment.update(NETWORK_BLOCK)
    environment.update(
        {
            "HOME": str(root / "home"),
            "PIPX_HOME": str(root / "pipx"),
            "PIPX_BIN_DIR": str(root / "bin"),
            "PIPX_MAN_DIR": str(root / "man"),
            "PIPX_COMPLETION_DIR": str(root / "completions"),
            "PIPX_SHARED_LIBS": str(shared),
            "PIPX_DEFAULT_PYTHON": sys._base_executable,
            "PIPX_DEFAULT_BACKEND": "pip",
            "PIPX_FETCH_PYTHON": "never",
            "PIPX_USE_EMOJI": "0",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1",
            "PIP_KEYRING_PROVIDER": "disabled",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CACHE_DIR": str(root / "pip-cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _owned_state(program: Path, root: Path, shared: Path) -> PipxState:
    """Create the directories one isolated pipx area needs and return its state."""
    logs = root / "logs"
    for directory in (logs, root / "unrelated", root / "home"):
        directory.mkdir(parents=True, exist_ok=True)
    return PipxState(
        root=root,
        program=program,
        environment=pipx_environment(root, shared),
        logs=logs,
    )


@pytest.fixture(scope="session")
def backend_wheelhouse(tmp_path_factory) -> Path:
    """Return a directory holding only the pinned, hash-checked backend wheel."""
    return _backend_wheelhouse(tmp_path_factory.mktemp("native-backend-wheels"))


def provision_shared_backend(program: Path, root: Path, shared: Path, wheelhouse: Path) -> PipxState:
    """Install the pinned backend into one pipx shared library location."""
    state = _owned_state(program, root, shared)
    state.run(
        "upgrade-shared",
        "--pip-args",
        "--no-index --only-binary=:all: --no-cache-dir --force-reinstall --find-links=" + wheelhouse.as_uri(),
        label="upgrade-shared",
    )
    return state


@pytest.fixture(scope="session")
def shared_backend(tmp_path_factory, backend_wheelhouse) -> Path:
    """Provision pipx's shared backend once from the pinned, hash-checked wheel."""
    root = tmp_path_factory.mktemp("native-backend")
    shared = root / "shared"
    provision_shared_backend(pipx_program(), root, shared, backend_wheelhouse)
    return shared


@pytest.fixture
def pipx_state(tmp_path, shared_backend) -> PipxState:
    """Return an owned pipx area that reuses the provisioned shared backend."""
    return _owned_state(pipx_program(), tmp_path / "installation area", shared_backend)


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

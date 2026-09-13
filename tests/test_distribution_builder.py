"""Exercise the Site Ops installation bundle producer."""

import base64
import csv
import email
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.utils import parse_wheel_filename

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="The bundle producer uses Python 3.11 TOML support.",
)


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location(
        "siteops_bundle_builder",
        SCRIPTS / "build-siteops-bundle.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_build_source(destination: Path) -> Path:
    destination.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "ThirdPartyNotices.txt"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copytree(
        ROOT / "siteops",
        destination / "siteops",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / "scripts").mkdir()
    return destination


@pytest.fixture(scope="module")
def built_application(builder, tmp_path_factory):
    root = tmp_path_factory.mktemp("bundle-application")
    source = _copy_build_source(root / "source")
    checkout_version = (ROOT / "siteops" / "__init__.py").read_bytes()
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")
    base_version, version, roots = builder._derive_staged_source(
        source,
        build_number=12345,
        build_attempt=1,
        source_sha="abcdef1234567890abcdef1234567890abcdef12",
        runtime_requirements=runtime,
    )
    assert roots == frozenset({"pyyaml"})
    wheel = builder._build_application_wheel(source, root / "wheels")
    builder._inspect_application_wheel(
        wheel,
        source=source,
        version=version,
        runtime_requirements=runtime,
    )
    assert (ROOT / "siteops" / "__init__.py").read_bytes() == checkout_version
    return source, wheel, runtime, base_version, version


def _runtime_filenames():
    for python, platform_name, python_tag, _ in (
        ("3.10", "windows-x86_64", "310", "win_amd64"),
        ("3.10", "linux-x86_64", "310", "manylinux2014_x86_64"),
        ("3.11", "windows-x86_64", "311", "win_amd64"),
        ("3.11", "linux-x86_64", "311", "manylinux2014_x86_64"),
        ("3.12", "windows-x86_64", "312", "win_amd64"),
        ("3.12", "linux-x86_64", "312", "manylinux2014_x86_64"),
        ("3.13", "windows-x86_64", "313", "win_amd64"),
        ("3.13", "linux-x86_64", "313", "manylinux2014_x86_64"),
        ("3.14", "windows-x86_64", "314", "win_amd64"),
        ("3.14", "linux-x86_64", "314", "manylinux2014_x86_64"),
    ):
        yield (
            (python, platform_name),
            f"pyyaml-6.0.3-cp{python_tag}-cp{python_tag}-{_}.whl",
        )


def _write_wheel_record(archive, prefix):
    rows = []
    for name in archive.namelist():
        content = archive.read(name)
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append((name, "sha256=" + digest, str(len(content))))
    rows.append((prefix + "/RECORD", "", ""))
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    archive.writestr(prefix + "/RECORD", output.getvalue())


def _synthetic_wheelhouse(
    builder, root: Path, *, extra: str | None = None,
    requires_python: str = ">=3.8", dependencies: tuple[str, ...] = (),
    include_record: bool = True,
):
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    hashes = []
    filenames = [filename for _, filename in _runtime_filenames()]
    if extra:
        filenames.append(extra)
    for filename in filenames:
        path = wheelhouse / filename
        tags = parse_wheel_filename(filename)[3]
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "pyyaml-6.0.3.dist-info/METADATA",
                "Metadata-Version: 2.3\nName: PyYAML\nVersion: 6.0.3\n"
                + f"Requires-Python: {requires_python}\n"
                + "".join(f"Requires-Dist: {value}\n" for value in dependencies),
            )
            archive.writestr(
                "pyyaml-6.0.3.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
                + "".join(f"Tag: {tag}\n" for tag in sorted(tags, key=str)),
            )
            archive.writestr("fixture.txt", "runtime fixture payload")
            if include_record:
                _write_wheel_record(archive, "pyyaml-6.0.3.dist-info")
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    lock = root / "runtime.txt"
    hash_lines = []
    for index, digest in enumerate(hashes):
        continuation = " \\" if index < len(hashes) - 1 else ""
        hash_lines.append(f"    --hash=sha256:{digest}{continuation}")
    lock.write_text(
        "PyYAML==6.0.3 \\\n"
        + "\n".join(hash_lines)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    requirements = builder._read_locked_requirements(lock)
    return wheelhouse, requirements


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_source(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Bundle Test")
    _git(repository, "config", "user.email", "bundle-test@example.invalid")
    (repository / ".gitignore").write_text("ignored-secret.txt\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "test fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_committed_locks_pin_real_runtime_and_build_hashes(builder):
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")
    build = builder._read_locked_requirements(SCRIPTS / "siteops-build-requirements.txt")

    assert [(item.name, item.version, len(item.hashes)) for item in runtime] == [
        ("pyyaml", "6.0.3", 10)
    ]
    assert {(item.name, item.version, len(item.hashes)) for item in build} == {
        ("pip", "26.2.1", 1),
        ("setuptools", "84.0.0", 1),
        ("packaging", "26.3", 1),
    }
    assert all(
        len(digest) == 64 and digest == digest.lower()
        for requirement in (*runtime, *build)
        for digest in requirement.hashes
    )


def test_genuine_application_wheel_has_derived_identity_and_notices(built_application):
    source, wheel, _, base_version, version = built_application

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        prefix = metadata_name.removesuffix("METADATA")
        packaged_init = archive.read("siteops/__init__.py").decode("utf-8")
        direct_requirements = [
            value for value in metadata.get_all("Requires-Dist") if "; extra ==" not in value
        ]

        assert metadata["Name"] == "siteops"
        assert metadata["Version"] == version
        assert direct_requirements == ["pyyaml==6.0.3"]
        assert f'__version__ = "{version}"' in packaged_init
        assert archive.read(prefix + "licenses/LICENSE") == (source / "LICENSE").read_bytes()
        assert archive.read(prefix + "licenses/ThirdPartyNotices.txt") == (
            source / "ThirdPartyNotices.txt"
        ).read_bytes()
    assert base_version == "1.0.0b1"
    assert version == "1.0.0b1+build.12345.1.gabcdef123456"


def test_staged_version_and_dependency_pins_do_not_mutate_checkout(builder, tmp_path):
    source = _copy_build_source(tmp_path / "source")
    checkout_init = (ROOT / "siteops" / "__init__.py").read_bytes()
    checkout_project = (ROOT / "pyproject.toml").read_bytes()
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")

    _, version, roots = builder._derive_staged_source(
        source,
        build_number=7,
        build_attempt=3,
        source_sha="1" * 40,
        runtime_requirements=runtime,
    )

    assert version == "1.0.0b1+build.7.3.g111111111111"
    assert roots == frozenset({"pyyaml"})
    assert b'__version__ = "1.0.0b1"' in checkout_init
    assert (ROOT / "siteops" / "__init__.py").read_bytes() == checkout_init
    assert (ROOT / "pyproject.toml").read_bytes() == checkout_project
    assert b'__version__ = "1.0.0b1+build.7.3.g111111111111"' in (
        source / "siteops" / "__init__.py"
    ).read_bytes()
    assert 'dependencies = ["pyyaml==6.0.3"]' in (
        source / "pyproject.toml"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("base", ["1.0.0", "1.1.0b2"])
def test_versioned_engine_release_preserves_the_source_version(builder, tmp_path, base):
    source = _copy_build_source(tmp_path / "source")
    (source / "siteops" / "__init__.py").write_text(
        f'__version__ = "{base}"\n', encoding="utf-8",
    )
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")
    assert builder._derive_staged_source(
        source, build_number=9, build_attempt=2, source_sha="a" * 40,
        runtime_requirements=runtime, version_mode="source",
    ) == (base, base, frozenset({"pyyaml"}))
    wheel = builder._build_application_wheel(source, tmp_path / "wheels")
    builder._inspect_application_wheel(
        wheel, source=source, version=base, runtime_requirements=runtime,
    )


def test_unknown_version_mode_does_not_change_source(builder, tmp_path):
    source = _copy_build_source(tmp_path / "source")
    before = (source / "siteops" / "__init__.py").read_bytes()
    with pytest.raises(builder.BuildError, match="version mode"):
        builder._derive_staged_source(
            source, build_number=1, build_attempt=1, source_sha="a" * 40,
            runtime_requirements=(), version_mode="latest",
        )
    assert (source / "siteops" / "__init__.py").read_bytes() == before


def test_wheelhouse_selects_every_declared_target(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path)

    selected = builder._collect_runtime_wheels(wheelhouse, requirements)

    assert set(selected) == {key for key, _ in _runtime_filenames()}
    assert all(
        wheel.sha256 in requirements[0].hashes
        for wheels in selected.values() for wheel in wheels
    )


def test_wheelhouse_rejects_missing_dependency_target(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path)
    (wheelhouse / "pyyaml-6.0.3-cp314-cp314-win_amd64.whl").unlink()

    with pytest.raises(builder.BuildError, match="Python 3.14 on windows-x86_64"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_wheelhouse_rejects_tampered_dependency(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path)
    target = wheelhouse / "pyyaml-6.0.3-cp311-cp311-win_amd64.whl"
    target.write_bytes(target.read_bytes() + b"changed")

    with pytest.raises(builder.BuildError, match="hash pins"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_wheelhouse_rejects_unsupported_pinned_wheel(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(
        builder,
        tmp_path,
        extra="pyyaml-6.0.3-cp311-cp311-macosx_10_9_x86_64.whl",
    )

    with pytest.raises(builder.BuildError, match="unsupported or duplicate"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_bundle_assembly_is_reproducible_and_has_no_workspace_leakage(
    builder,
    built_application,
    tmp_path,
):
    source, application, _, base_version, version = built_application
    (source / "private-workspace.yaml").write_text("not payload\n", encoding="utf-8")
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path / "dependencies")
    runtime = builder._collect_runtime_wheels(wheelhouse, requirements)
    bundles = []
    archives = []
    for number in (1, 2):
        bundle = tmp_path / f"bundle-{number}"
        manifest = builder._assemble_bundle(
            source=source,
            bundle_root=bundle,
            application_wheel=application,
            application_sha256=hashlib.sha256(application.read_bytes()).hexdigest(),
            runtime_wheels=runtime,
            repository="example/siteops",
            source_sha="a" * 40,
            source_ref="refs/heads/main",
            build_number=12345,
            build_attempt=1,
            base_version=base_version,
            version=version,
        )
        archive = tmp_path / f"bundle-{number}.zip"
        builder._write_deterministic_zip(bundle, archive)
        bundles.append((bundle, manifest))
        archives.append(archive)

    assert bundles[0][1] == bundles[1][1]
    assert archives[0].read_bytes() == archives[1].read_bytes()
    with zipfile.ZipFile(archives[0]) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "bundle.json" in names
        assert "pylock.toml" in names
        assert "install.py" not in names
        assert "siteops_distribution.py" not in names
        assert not any("workspace" in name for name in names)
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
    assert json.loads((bundles[0][0] / "bundle.json").read_text(encoding="utf-8"))[
        "package"
    ]["version"] == version


def test_git_export_uses_only_tracked_source_and_ignores_ignored_tools(
    builder,
    git_source,
    tmp_path,
):
    repository, source_sha = git_source
    (repository / "ignored-secret.txt").write_text("not exported\n", encoding="utf-8")

    builder._validate_repository(repository, source_sha)
    destination = tmp_path / "export" / "source"
    destination.parent.mkdir()
    builder._export_tracked_source(repository, destination, source_sha)

    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (destination / "ignored-secret.txt").exists()
    assert not (destination / ".git").exists()


def test_git_export_remains_bound_when_head_advances(builder, git_source, tmp_path):
    repository, source_sha = git_source
    builder._validate_repository(repository, source_sha)
    (repository / "tracked.txt").write_text("a later commit\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "later fixture")
    destination = tmp_path / "export" / "source"
    destination.parent.mkdir()

    builder._export_tracked_source(repository, destination, source_sha)

    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"


def test_production_reads_locks_only_from_the_exact_export(
    builder, git_source, tmp_path, monkeypatch,
):
    repository, source_sha = git_source
    reads = []
    exports = []
    export = builder._export_tracked_source

    def capture_export(root, destination, commit):
        exports.append(commit)
        export(root, destination, commit)

    def read(path):
        reads.append(path)
        return ()

    def stop(requirements):
        raise builder.BuildError("Stopped after observing lock input paths.")

    monkeypatch.setattr(builder, "_export_tracked_source", capture_export)
    monkeypatch.setattr(builder, "_read_locked_requirements", read)
    monkeypatch.setattr(builder, "_validate_build_environment", stop)
    with pytest.raises(builder.BuildError, match="Stopped after observing"):
        builder.produce_bundle(
            root=repository, repository="example/siteops", source_ref="refs/heads/main",
            expected_source_sha=source_sha, build_number=1, build_attempt=1,
            output=tmp_path / "siteops-install.zip", wheelhouse=tmp_path / "wheels",
            download_dependencies=False,
        )
    assert exports == [source_sha]
    assert len(reads) == 2
    assert all(not path.is_relative_to(repository) for path in reads)
    assert {path.name for path in reads} == {
        "siteops-runtime-requirements.txt", "siteops-build-requirements.txt",
    }


@pytest.mark.parametrize(
    "declarations",
    [["PyYAML>=6.0.4"], ["PyYAML>=6.0", "requests>=2.32"]],
)
def test_source_dependency_drift_is_rejected_before_pinning(builder, tmp_path, declarations):
    source = _copy_build_source(tmp_path / "source")
    project = source / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'dependencies = ["pyyaml>=6.0"]', "dependencies = " + json.dumps(declarations),
        ),
        encoding="utf-8",
    )
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")
    with pytest.raises(builder.BuildError, match="runtime lock"):
        builder._derive_staged_source(
            source, build_number=1, build_attempt=1, source_sha="a" * 40,
            runtime_requirements=runtime,
        )


def test_wheel_python_requirement_must_cover_every_declared_target(builder, tmp_path):
    source = _copy_build_source(tmp_path / "source")
    project = source / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'requires-python = ">=3.10"', 'requires-python = ">=3.11"',
        ),
        encoding="utf-8",
    )
    runtime = builder._read_locked_requirements(SCRIPTS / "siteops-runtime-requirements.txt")
    _, version, _ = builder._derive_staged_source(
        source, build_number=1, build_attempt=1, source_sha="a" * 40,
        runtime_requirements=runtime,
    )
    wheel = builder._build_application_wheel(source, tmp_path / "wheels")
    with pytest.raises(builder.BuildError, match="Python"):
        builder._inspect_application_wheel(
            wheel, source=source, version=version, runtime_requirements=runtime,
        )


def test_renamed_pinned_wheels_do_not_change_their_target(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path)
    first, second = list(wheelhouse.iterdir())[:2]
    first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_bytes)
    second.write_bytes(first_bytes)
    with pytest.raises(builder.BuildError, match="tags"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_runtime_wheel_changed_after_collection_is_not_bundled(
    builder, built_application, tmp_path,
):
    source, application, _, base, version = built_application
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path / "dependencies")
    runtime = builder._collect_runtime_wheels(wheelhouse, requirements)
    changed = next(iter(runtime.values()))[0].path
    changed.write_bytes(changed.read_bytes() + b"changed after collection")

    with pytest.raises(builder.BuildError, match="changed"):
        builder._assemble_bundle(
            source=source, bundle_root=tmp_path / "bundle", application_wheel=application,
            application_sha256=hashlib.sha256(application.read_bytes()).hexdigest(),
            runtime_wheels=runtime, repository="example/siteops", source_sha="a" * 40,
            source_ref="refs/heads/main", build_number=1, build_attempt=1,
            base_version=base, version=version,
        )


def test_production_call_rejects_wrong_sha_before_other_inputs(
    builder,
    git_source,
    tmp_path,
):
    repository, _ = git_source
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(builder.BuildError, match="does not match"):
        builder.produce_bundle(
            root=repository,
            repository="example/siteops",
            source_ref="refs/heads/main",
            expected_source_sha="0" * 40,
            build_number=1,
            build_attempt=1,
            output=tmp_path / "siteops-install.zip",
            wheelhouse=wheelhouse,
            download_dependencies=False,
        )


def test_production_call_rejects_dirty_source_before_build(
    builder,
    git_source,
    tmp_path,
):
    repository, source_sha = git_source
    (repository / "untracked-source.py").write_text("changed\n", encoding="utf-8")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    with pytest.raises(builder.BuildError, match="tracked or untracked changes"):
        builder.produce_bundle(
            root=repository,
            repository="example/siteops",
            source_ref="refs/heads/main",
            expected_source_sha=source_sha,
            build_number=1,
            build_attempt=1,
            output=tmp_path / "siteops-install.zip",
            wheelhouse=wheelhouse,
            download_dependencies=False,
        )


def test_download_mode_requests_hashes_binary_wheels_and_every_target(
    builder,
    monkeypatch,
    tmp_path,
):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(builder, "_run", run)
    destination = tmp_path / "wheels"
    builder._download_runtime_wheels(
        SCRIPTS / "siteops-runtime-requirements.txt",
        destination,
    )

    assert len(commands) == 10
    assert all("--require-hashes" in command for command in commands)
    assert all("--only-binary=:all:" in command for command in commands)
    assert all("--no-deps" in command and "--no-cache-dir" in command for command in commands)
    assert {command[command.index("--python-version") + 1] for command in commands} == {
        "310",
        "311",
        "312",
        "313",
        "314",
    }


def test_build_environment_must_match_exact_toolchain(builder, monkeypatch):
    requirements = builder._read_locked_requirements(SCRIPTS / "siteops-build-requirements.txt")
    versions = {requirement.name: requirement.version for requirement in requirements}
    monkeypatch.setattr(builder.importlib.metadata, "version", versions.__getitem__)

    builder._validate_build_environment(requirements)
    versions["pip"] = "0"

    with pytest.raises(builder.BuildError, match="does not match"):
        builder._validate_build_environment(requirements)


@pytest.mark.parametrize(
    "dependency",
    ["unlocked>=1", "PyYAML>=7", "PyYAML==6.0.3; python_version >= '3.12'",
     "PyYAML[extra]==6.0.3", "PyYAML @ https://example.invalid/unpinned.whl"],
)
def test_runtime_dependency_closure_is_checked_before_lock_generation(builder, tmp_path, dependency):
    wheelhouse, requirements = _synthetic_wheelhouse(
        builder, tmp_path, dependencies=(dependency,),
    )
    with pytest.raises(builder.BuildError, match="dependencies.*runtime lock"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_runtime_python_requirement_must_cover_the_selected_target(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(
        builder, tmp_path, requires_python=">=3.11",
    )
    with pytest.raises(builder.BuildError, match=r"Python 3\.10"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_runtime_closure_can_include_a_shared_pure_wheel(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(
        builder, tmp_path, dependencies=("support>=1",),
    )
    path = wheelhouse / "support-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "support-1.0.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: support\nVersion: 1.0\nRequires-Python: >=3.10\n",
        )
        archive.writestr(
            "support-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        _write_wheel_record(archive, "support-1.0.dist-info")
    support = builder.LockedRequirement(
        name="support", version="1.0",
        hashes=frozenset({hashlib.sha256(path.read_bytes()).hexdigest()}),
    )
    selected = builder._collect_runtime_wheels(wheelhouse, (*requirements, support))
    assert len(selected) == 10
    assert all({wheel.name for wheel in wheels} == {"pyyaml", "support"} for wheels in selected.values())
    assert {wheels[1].path for wheels in selected.values()} == {path}
    builder._validate_runtime_closure(frozenset({"pyyaml"}), selected)
    with pytest.raises(builder.BuildError, match="outside the source dependency closure"):
        builder._validate_runtime_closure(frozenset({"support"}), selected)


def test_runtime_wheel_requires_record_before_bundling(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path, include_record=False)
    with pytest.raises(builder.BuildError, match="matching RECORD"):
        builder._collect_runtime_wheels(wheelhouse, requirements)


def test_runtime_wheel_reads_nonmetadata_members_before_bundling(builder, tmp_path):
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path)
    path = next(wheelhouse.glob("*.whl"))
    changed = path.read_bytes().replace(b"runtime fixture payload", b"damaged fixture payload")
    assert changed != path.read_bytes()
    path.write_bytes(changed)
    requirement = requirements[0]
    locks = (builder.LockedRequirement(
        requirement.name, requirement.version,
        requirement.hashes | {hashlib.sha256(changed).hexdigest()},
    ),)
    with pytest.raises(builder.BuildError, match="corrupt archive"):
        builder._collect_runtime_wheels(wheelhouse, locks)


def _assemble_test_bundle(builder, built_application, tmp_path):
    source, application, _, base, version = built_application
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path / "dependencies")
    runtime = builder._collect_runtime_wheels(wheelhouse, requirements)
    root = tmp_path / "bundle with spaces"
    manifest = builder._assemble_bundle(
        source=source, bundle_root=root, application_wheel=application,
        application_sha256=hashlib.sha256(application.read_bytes()).hexdigest(),
        runtime_wheels=runtime, repository="example/siteops", source_sha="a" * 40,
        source_ref="refs/heads/main", build_number=12345, build_attempt=1,
        base_version=base, version=version,
    )
    return root, manifest


def test_generated_pylock_contains_exact_portable_wheel_inventory(builder, built_application, tmp_path):
    root, manifest = _assemble_test_bundle(builder, built_application, tmp_path)
    lock = tomllib.loads((root / "pylock.toml").read_text(encoding="utf-8"))
    assert lock["lock-version"] == "1.0"
    assert lock["created-by"] == "siteops"
    assert lock["requires-python"] == ">=3.10,<3.15"
    assert {(package["name"], package["version"]) for package in lock["packages"]} == {
        ("siteops", manifest.version), ("pyyaml", "6.0.3"),
    }
    expected = {entry.path: entry for entry in manifest.files if entry.path.endswith(".whl")}
    actual = {}
    for package in lock["packages"]:
        assert set(package) == {"name", "version", "wheels"}
        for wheel in package["wheels"]:
            assert set(wheel) == {"name", "path", "size", "hashes"}
            path = wheel["path"]
            assert path.startswith("wheels/") and "\\" not in path
            assert (root / path).resolve().is_relative_to(root)
            assert wheel["name"] == Path(path).name
            assert wheel["size"] == expected[path].size
            assert wheel["hashes"] == {"sha256": expected[path].sha256}
            assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected[path].sha256
            assert path not in actual
            actual[path] = wheel
    assert actual.keys() == expected.keys()
    assert set(actual) == {wheel for target in manifest.targets for wheel in target.wheels}


def test_lock_environments_allow_only_declared_targets(builder, built_application, tmp_path):
    from packaging.markers import Marker

    root, manifest = _assemble_test_bundle(builder, built_application, tmp_path)
    lock = tomllib.loads((root / "pylock.toml").read_text(encoding="utf-8"))
    markers = [Marker(value) for value in lock["environments"]]
    assert len(markers) == len(manifest.targets)
    for python in ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15"):
        for system, machine in (
            ("win32", "AMD64"), ("linux", "x86_64"), ("darwin", "x86_64"),
            ("win32", "ARM64"), ("linux", "aarch64"),
        ):
            for implementation in ("cpython", "pypy"):
                matches = sum(marker.evaluate({
                    "python_version": python, "sys_platform": system,
                    "platform_machine": machine, "implementation_name": implementation,
                }) for marker in markers)
                supported = (
                    python not in {"3.9", "3.15"} and implementation == "cpython"
                    and (system, machine) in {("win32", "AMD64"), ("linux", "x86_64")}
                )
                assert matches == int(supported)


def test_application_wheel_change_after_inspection_is_rejected(builder, built_application, tmp_path):
    source, application, runtime_requirements, base, version = built_application
    wheelhouse, requirements = _synthetic_wheelhouse(builder, tmp_path / "dependencies")
    runtime = builder._collect_runtime_wheels(wheelhouse, requirements)
    copy = tmp_path / application.name
    shutil.copyfile(application, copy)
    digest = builder._inspect_application_wheel(
        copy, source=source, version=version, runtime_requirements=runtime_requirements,
    )
    copy.write_bytes(copy.read_bytes() + b"changed after inspection")
    with pytest.raises(builder.BuildError, match="application wheel changed"):
        builder._assemble_bundle(
            source=source, bundle_root=tmp_path / "bundle", application_wheel=copy,
            application_sha256=digest, runtime_wheels=runtime, repository="example/siteops",
            source_sha="a" * 40, source_ref="refs/heads/main", build_number=1,
            build_attempt=1, base_version=base, version=version,
        )


def test_published_wheel_is_identical_to_the_bundled_wheel(builder, built_application, tmp_path):
    root, manifest = _assemble_test_bundle(builder, built_application, tmp_path)
    archive = tmp_path / "staged.zip"
    builder._write_deterministic_zip(root, archive)
    output = tmp_path / "output"
    output.mkdir()
    wheel = root / manifest.application_wheel
    digest = next(entry.sha256 for entry in manifest.files if entry.path == manifest.application_wheel)
    builder._publish_artifacts(archive, wheel, output / "siteops-install.zip", digest)
    assert {path.name for path in output.iterdir()} == {"siteops-install.zip", wheel.name}
    with zipfile.ZipFile(output / "siteops-install.zip") as released:
        assert (output / wheel.name).read_bytes() == released.read(manifest.application_wheel)


def test_publication_creates_both_artifacts_with_owner_only_permissions(builder, tmp_path, monkeypatch):
    archive = tmp_path / "input.zip"
    wheel = tmp_path / "siteops-1.0-py3-none-any.whl"
    archive.write_bytes(b"archive")
    wheel.write_bytes(b"wheel")
    output = tmp_path / "output"
    output.mkdir()
    opened = {}
    original_open = builder.os.open

    def record_open(path, flags, mode):
        opened[Path(path).name] = mode
        return original_open(path, flags, mode)

    monkeypatch.setattr(builder.os, "open", record_open)
    builder._publish_artifacts(
        archive, wheel, output / "siteops-install.zip",
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )
    assert opened == {"siteops-install.zip": 0o600, wheel.name: 0o600}
    for original, name in ((archive, "siteops-install.zip"), (wheel, wheel.name)):
        artifact = output / name
        assert artifact.read_bytes() == original.read_bytes()
        if sys.platform != "win32":
            assert artifact.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("existing", ["siteops-install.zip", "siteops-1.0-py3-none-any.whl"])
def test_publication_never_overwrites_either_artifact(builder, tmp_path, existing):
    archive = tmp_path / "input.zip"
    wheel = tmp_path / "siteops-1.0-py3-none-any.whl"
    archive.write_bytes(b"archive")
    wheel.write_bytes(b"wheel")
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / existing
    sentinel.write_bytes(b"operator file")
    with pytest.raises(builder.BuildError, match="already exists"):
        builder._publish_artifacts(
            archive, wheel, output / "siteops-install.zip",
            hashlib.sha256(wheel.read_bytes()).hexdigest(),
        )
    assert sentinel.read_bytes() == b"operator file"
    assert list(output.iterdir()) == [sentinel]


def test_artifact_copy_failure_removes_only_its_incomplete_outputs(builder, tmp_path):
    archive = tmp_path / "input.zip"
    wheel = tmp_path / "siteops-1.0-py3-none-any.whl"
    archive.write_bytes(b"archive")
    wheel.write_bytes(b"changed after validation")
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "operator.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(builder.BuildError, match="changed after verification"):
        builder._publish_artifacts(archive, wheel, output / "siteops-install.zip", "0" * 64)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("source_has_dependencies", [True, False])
def test_production_builds_once_and_emits_the_same_wheel(
    builder, tmp_path, monkeypatch, source_has_dependencies,
):
    source = _copy_build_source(tmp_path / "source")
    if not source_has_dependencies:
        project = source / "pyproject.toml"
        text = project.read_text(encoding="utf-8")
        assert 'dependencies = ["pyyaml>=6.0"]' in text
        project.write_text(text.replace('dependencies = ["pyyaml>=6.0"]', "dependencies = []"), encoding="utf-8")
    wheelhouse, runtime = _synthetic_wheelhouse(builder, tmp_path / "dependencies")
    shutil.copyfile(tmp_path / "dependencies" / "runtime.txt", source / "scripts" / builder._RUNTIME_LOCK)
    shutil.copyfile(SCRIPTS / builder._BUILD_LOCK, source / "scripts" / builder._BUILD_LOCK)
    _git(source, "init", "--quiet")
    _git(source, "config", "user.name", "Bundle Test")
    _git(source, "config", "user.email", "bundle-test@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "--quiet", "-m", "test fixture")
    sha = _git(source, "rev-parse", "HEAD")
    monkeypatch.setattr(builder, "_validate_build_environment", lambda requirements: None)
    builds = []
    build = builder._build_application_wheel

    def record_build(source, destination):
        wheel = build(source, destination)
        builds.append((wheel.name, wheel.read_bytes()))
        return wheel

    monkeypatch.setattr(builder, "_build_application_wheel", record_build)
    output = tmp_path / "output"
    output.mkdir()
    arguments = dict(
        root=source, repository="example/siteops", source_ref="refs/heads/main",
        expected_source_sha=sha, build_number=42, build_attempt=1,
        output=output / "siteops-install.zip", wheelhouse=wheelhouse,
        download_dependencies=False,
    )
    if not source_has_dependencies:
        with pytest.raises(builder.BuildError, match="outside the source dependency closure"):
            builder.produce_bundle(**arguments)
        assert not builds
        assert not list(output.iterdir())
        return
    builder.produce_bundle(**arguments)
    assert len(builds) == 1
    name, content = builds[0]
    assert {path.name for path in output.iterdir()} == {"siteops-install.zip", name}
    assert (output / name).read_bytes() == content
    with zipfile.ZipFile(output / "siteops-install.zip") as archive:
        assert archive.read("wheels/" + name) == content
        lock = tomllib.loads(archive.read("pylock.toml").decode())
        assert {p["name"] for p in lock["packages"]} == {"siteops", *[item.name for item in runtime]}
    assert _git(source, "status", "--porcelain") == ""

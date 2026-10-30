"""Reviewed workspace production without provider access or deployment commands."""

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys

import pytest

from siteops.artifacts import ArtifactError
from siteops.workspace_package import extract_package, inspect_package, inspect_produced_package
from tests.release_helpers import (
    REPOSITORY,
    ROOT,
    SCRIPTS,
    SOURCE_REF,
    _commit,
    _run_cli,
    _write_record,
    _write_source_version,
)
from tests.release_helpers import repository as repository
from tests.shell_helpers import run_script, write_executable
from tests.workspace_release_helpers import PRODUCER, _declaration, _load, _produce, _workspace
from tests.yaml_documents import cached_safe_load

sys.path.insert(0, str(SCRIPTS))

from siteops_release import ReleaseIntentError  # noqa: E402
from workspace_release import WorkspaceBuild, load_workspace_builds  # noqa: E402

CANDIDATE = cached_safe_load((ROOT / ".github" / "workflows" / "_release-candidate.yaml").read_text())
WORKSPACE_WORKFLOW = cached_safe_load((ROOT / ".github" / "workflows" / "_workspace-distribution.yaml").read_text())


def _build_step(name):
    return next(step for step in WORKSPACE_WORKFLOW["jobs"]["build"]["steps"] if step.get("name") == name)


@pytest.mark.parametrize("script,options", [
    ("build-workspace-package.py", ("--target-engine-version VERSION",)),
    ("build-workspace-release.py", ("--root DIRECTORY", "--release-workspace PATH", "--expected-plan-sha SHA256")),
    ("assemble-workspace-release.py", ("--staging DIRECTORY", "--trusted-root FILE", "--expected-plan-sha SHA256")),
    ("prepare-workspace-engine.py", (
        "--control DIRECTORY", "--built-assets DIRECTORY", "--archive-sha SHA256",
        "--expected-runner-environment {github-hosted,self-hosted}",
    )),
    ("qualify-workspace-engine.py", (
        "--engine DIRECTORY", "--expected-engine-selection-sha256 SHA256",
        "--expected-workspace-inventory-sha256 SHA256", "--state DIRECTORY", "--gh FILE",
        "--expected-runner-environment {github-hosted,self-hosted}",
    )),
    ("stage-release-payload.py", (
        "--engine-inventory FILE", "--engine-selection FILE", "--expected-engine-inventory-sha256 SHA256",
        "--expected-engine-selection-sha256 SHA256", "--expected-workspace-inventory-sha256 SHA256",
    )),
    ("probe-installed-workspaces.py", ("--spec FILE", "--expected-spec-sha SHA256")),
])
def test_release_tool_help_identifies_paths_and_digest_inputs(tmp_path, script, options):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), "--help"],
        cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    help_text = " ".join(result.stdout.split())
    for option in options:
        assert option in help_text


def test_workspace_declaration_is_source_bound_and_does_not_import_referenced_engine(repository):
    request = _workspace(repository)
    _write_source_version(repository, 'raise RuntimeError("never import candidate source")\n')
    raw, _ = _write_record(repository, _declaration([request]))
    sha = _commit(repository, "workspace intent")
    intent = _load(repository, sha)
    assert intent.to_dict()["workspaces"] == [request]
    assert intent.intent_sha256 == hashlib.sha256(raw).hexdigest()
    assert intent.engine_version() == "1.2.3"
    assert intent.version == "2.0.0" and intent.components == "content"
    request["id"] = "different"
    _write_record(repository, _declaration([request]))
    _commit(repository, "advance source")
    assert _load(repository, sha).workspaces[0].kit_id == "fixture.storage"


@pytest.mark.parametrize("combined", [False, True])
def test_real_release_producer_emits_exact_packages_without_proofs(repository, tmp_path, combined):
    requests = [_workspace(repository), _workspace(repository, "second")]
    _write_source_version(repository, '__version__ = "1.2.3"\n')
    _write_record(repository, _declaration(requests, combined=combined))
    sha = _commit(repository, "produce workspace candidate")
    output = tmp_path / "assets"
    extra = ("--build-number", "42", "--build-attempt", "3") if combined else ()
    result = _produce(repository, sha, output, *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    raw = (output / "workspace-builds.json").read_bytes()
    assert summary["sha256"] == hashlib.sha256(raw).hexdigest()
    record = json.loads(raw)
    assert record["source"] == {"repository": REPOSITORY, "commit": sha, "ref": SOURCE_REF}
    assert record["engineVersion"] == (
        "1.2.3+build.42.3.g" + sha[:12] if combined else "1.2.3"
    )
    assert record["provenance"] == summary["provenance"] == "not-established"
    assert {path.name for path in output.iterdir()} == {
        "workspace.zip", "second.zip", "workspace-builds.json",
    }
    for row in record["workspaces"]:
        archive = output / row["package"]["name"]
        inspected = inspect_produced_package(
            archive, row["package"]["sha256"], engine_version=record["engineVersion"],
        )
        assert archive.stat().st_size == row["package"]["size"]
        assert inspected.metadata.source_revision == sha
        assert inspected.metadata.kit_id == row["kit"]["id"] == "fixture.storage"
        assert inspected.metadata.version == row["kit"]["version"] == ("2.0.0b1" if combined else "2.0.0")
        assert inspected.metadata.workspace_root == row["workspace"]
        assert "compiled-templates/v1" in row["compatibility"]["requiredFeatures"]
        assert inspected.metadata.templates[0].producer_mode == "native-arm-json"
        with pytest.raises(ArtifactError, match="different Site Ops version"):
            inspect_package(archive, row["package"]["sha256"])
        destination = tmp_path / ("consumer-" + row["workspace"])
        with pytest.raises(ArtifactError, match="different Site Ops version"):
            extract_package(archive, row["package"]["sha256"], destination)
        assert not destination.exists()


@pytest.mark.parametrize("github", [False, True])
def test_release_index_check_preserves_existing_source_binding_settings(repository, tmp_path, github):
    request = _workspace(repository, index=True, github=github)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "indexed candidate")
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((output / "workspace-builds.json").read_text())
    assert record["workspaces"][0]["index"]["sha256"] == hashlib.sha256(
        (repository / "workspace" / "siteops-index.json").read_bytes(),
    ).hexdigest()


@pytest.mark.parametrize("fault", ["stale", "missing-bindings", "missing-index", "unknown-algorithm"])
def test_release_rejects_incomplete_or_stale_committed_indexes(repository, tmp_path, fault):
    request = _workspace(repository, index=True)
    root = repository / "workspace"
    if fault == "stale":
        manifest = root / "manifests" / "storage" / "manifest.yaml"
        raw = manifest.read_bytes()
        newline = b"\r\n" if b"\r\n" in raw else b"\n"
        manifest.write_bytes(raw + newline + b"# new reviewed input" + newline)
    elif fault == "missing-bindings":
        (root / "siteops-index.inputs.json").unlink()
    elif fault == "missing-index":
        (root / "siteops-index.json").unlink()
    else:
        path = root / "siteops-index.inputs.json"
        document = json.loads(path.read_text())
        next(row for row in document["inputs"] if row["digests"])["digests"]["other-sha256"] = "a" * 64
        path.write_text(json.dumps(document))
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "invalid index")
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode != 0
    if fault == "stale":
        assert "Generated index outputs need to be rebuilt." in result.stderr
    assert not result.stdout and not output.exists()


@pytest.mark.parametrize("fault", ["dirty", "wrong-sha", "export-ignore", "second-package"])
def test_release_producer_failure_preserves_output_absence(repository, tmp_path, fault):
    request = _workspace(repository)
    requests = [request]
    if fault == "export-ignore":
        (repository / ".gitattributes").write_text("workspace/manifests export-ignore\n")
    elif fault == "second-package":
        requests.append(_workspace(repository, "second"))
        template = repository / "second" / "templates" / "storage.template.json"
        template.write_text("not an ARM template")
    _write_record(repository, _declaration(requests))
    sha = _commit(repository, "candidate")
    if fault == "dirty":
        (repository / "LICENSE").write_text("uncommitted")
    elif fault == "wrong-sha":
        sha = "a" * 40
    output = tmp_path / "assets"
    result = _produce(repository, sha, output)
    assert result.returncode != 0 and not result.stdout
    assert not output.exists()


def test_release_producer_preserves_existing_directory_and_files(repository, tmp_path):
    request = _workspace(repository)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "candidate")
    output = tmp_path / "assets"
    output.mkdir()
    marker = output / "operator.txt"
    marker.write_text("preserve")
    result = _produce(repository, sha, output)
    assert result.returncode != 0
    assert marker.read_text() == "preserve"


@pytest.mark.parametrize("fault", [
    "engine-only", "empty", "too-many", "duplicate-root", "duplicate-name", "reserved-name",
    "path", "license", "directory-license", "unknown-field", "unbounded-range",
    "wrong-engine", "missing-feature", "duplicate-feature",
])
def test_invalid_workspace_declarations_fail_during_source_preparation(repository, fault):
    request = _workspace(repository)
    declaration = _declaration([request])
    if fault == "engine-only":
        declaration = {"tag": "siteops/v1.2.3", "workspaces": [request]}
        _write_source_version(repository, '__version__ = "1.2.3"\n')
    elif fault == "empty":
        declaration["workspaces"] = []
    elif fault == "too-many":
        declaration["workspaces"] = [request] * 65
    elif fault in {"duplicate-root", "duplicate-name"}:
        other = copy.deepcopy(request)
        other["package" if fault == "duplicate-root" else "workspace"] = "other.zip"
        declaration["workspaces"].append(other)
    elif fault == "reserved-name":
        request["package"] = "siteops-install.zip"
    elif fault == "path":
        request["workspace"] = "../workspace"
    elif fault == "license":
        request["licenses"] = ["absent"]
    elif fault == "directory-license":
        request["licenses"] = ["workspace"]
    elif fault == "unknown-field":
        request["command"] = "unreviewed hook"
    elif fault == "unbounded-range":
        request["compatibility"]["siteops"] = ">=1"
    elif fault == "wrong-engine":
        request["compatibility"]["siteops"] = ">=3,<4"
    elif fault == "missing-feature":
        request["compatibility"]["requiredFeatures"] = None
    else:
        request["compatibility"]["requiredFeatures"] = ["manifest/v1", "manifest/v1"]
    _write_record(repository, declaration)
    sha = _commit(repository, "invalid intent")
    with pytest.raises(ReleaseIntentError):
        _load(repository, sha)


def test_prepare_cli_retains_the_exact_workspace_request(repository, tmp_path):
    request = _workspace(repository)
    request["include"] = []
    request["compatibility"]["requiredFeatures"] = ["manifest/v1", "composition/v1"]
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "candidate")
    output = tmp_path / "plan"
    result = _run_cli(
        repository, "--repository", REPOSITORY, "--source-sha", sha, "--source-ref", SOURCE_REF,
        "--intent", "releases/candidate/release.json", "--output-dir", str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((output / "plan.json").read_text())["workspaces"] == [request]


def test_declaration_loading_needs_no_runtime_or_provider_modules(repository, tmp_path):
    request = _workspace(repository)
    _write_source_version(repository, 'raise RuntimeError("do not import the selected source")\n')
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "pure declaration")
    code = """
import builtins, sys
from pathlib import Path
source, scripts, repository, sha = sys.argv[1:]
sys.path[:0] = [scripts, source]
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in {"yaml", "siteops.compilation", "siteops.models", "siteops.github_source"}:
        raise AssertionError("Declaration loading imported a runtime or provider module: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from siteops_release import load_release_intent
intent = load_release_intent(
    Path(repository), sha, "releases/candidate/release.json", "example/releases", "refs/heads/main",
)
assert intent.workspaces[0].package_name == "workspace.zip"
assert intent.engine_version() == "1.2.3"
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(ROOT), str(SCRIPTS), str(repository), sha],
        cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("number,attempt", [(None, None), (0, 1), (1, 0), (-1, 1), (10**20, 1)])
def test_combined_workspace_build_requires_a_bounded_build_identity(repository, tmp_path, number, attempt):
    request = _workspace(repository)
    _write_source_version(repository, '__version__ = "1.2.3"\n')
    _write_record(repository, _declaration([request], combined=True))
    sha = _commit(repository, "combined candidate")
    output = tmp_path / "assets"
    extra = () if number is None else ("--build-number", str(number), "--build-attempt", str(attempt))
    result = _produce(repository, sha, output, *extra)
    assert result.returncode != 0
    assert "build number and attempt" in result.stderr
    assert not output.exists()


def test_workspace_request_round_trip_preserves_absent_and_empty_options(repository):
    request = _workspace(repository)
    assert WorkspaceBuild.from_document(request).document() == request
    request["include"] = []
    request["compatibility"]["requiredFeatures"] = ["composition/v1", "manifest/v1"]
    assert WorkspaceBuild.from_document(request).document() == request
    requests = [
        {**request, "workspace": f"workspace-{number}", "package": f"workspace-{number}.zip"}
        for number in range(64)
    ]
    assert len(load_workspace_builds(requests, engine_version="1.2.3")) == 64


def test_release_cleanup_reports_retained_outputs_without_swallowing_primary_error(
    repository, tmp_path, monkeypatch, capsys,
):
    requests = [_workspace(repository), _workspace(repository, "second")]
    (repository / "second" / "templates" / "storage.template.json").write_text("invalid template")
    _write_record(repository, _declaration(requests))
    sha = _commit(repository, "failure after first package")
    spec = importlib.util.spec_from_file_location("workspace_release_producer_test", PRODUCER)
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    output = tmp_path / "assets"
    monkeypatch.setattr(sys, "argv", [
        str(PRODUCER), "--root", str(repository), "--repository", REPOSITORY,
        "--expected-source-sha", sha, "--source-ref", SOURCE_REF,
        "--release-file", "releases/candidate/release.json", "--output-dir", str(output),
    ])
    original = type(output).unlink

    def deny_owned_output(path, *args, **kwargs):
        if path == output / "workspace.zip":
            raise PermissionError("Controlled cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(output), "unlink", deny_owned_output)
    assert producer.main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Template" in captured.err
    assert "could not be removed" in captured.err
    assert "directory was retained" in captured.err
    assert {path.name for path in output.iterdir()} == {"workspace.zip"}


@pytest.mark.parametrize("fault", [None, "digest", "request", "source", "boolean", "oversized", "half-pair"])
def test_producer_requires_the_exact_prepared_plan(repository, tmp_path, fault):
    request = _workspace(repository)
    _write_record(repository, _declaration([request]))
    sha = _commit(repository, "prepared workspace")
    plan = _load(repository, sha).to_dict()
    path = tmp_path / "prepared.json"
    if fault == "request":
        plan["workspaces"][0]["id"] = "unreviewed"
    elif fault == "source":
        plan["source"]["commit"] = "d" * 40
    elif fault == "boolean":
        plan["dryRun"] = 0
    raw = json.dumps(plan).encode()
    if fault == "oversized":
        raw += b" " * (1024 * 1024)
    path.write_bytes(raw)
    expected = "a" * 64 if fault == "digest" else hashlib.sha256(raw).hexdigest()
    extra = ["--prepared-plan", str(path)]
    if fault != "half-pair":
        extra.extend(("--expected-plan-sha", expected))
    output = tmp_path / "assets"
    result = _produce(repository, sha, output, *extra)
    if fault is None:
        assert result.returncode == 0, result.stdout + result.stderr
        record = json.loads((output / "workspace-builds.json").read_bytes())
        assert record["planSha256"] == expected
    else:
        assert result.returncode != 0
        assert not output.exists()
        if fault in {"source", "request", "boolean"}:
            assert "differs from the committed release intent" in result.stderr


def test_workspace_job_uses_the_prepared_candidate_with_no_signing_authority():
    job = WORKSPACE_WORKFLOW["jobs"]["build"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    caller = CANDIDATE["jobs"]["workspace-build"]
    assert caller["needs"] == "prepare"
    assert caller["if"] == "needs.prepare.outputs.active == 'true' && needs.prepare.outputs.workspaces == 'true'"
    assert caller["uses"] == "./.github/workflows/_workspace-distribution.yaml"
    assert caller["strategy"]["max-parallel"] == 4
    assert caller["with"]["workspace"] == "${{ matrix.workspace }}"
    assert "environment" not in job
    checkout = _build_step("Checkout the event commit")["with"]
    assert checkout["ref"] == "${{ github.sha }}"
    assert checkout["persist-credentials"] is False
    download = _build_step("Download the prepared release plan")["with"]
    assert download["artifact-ids"] == "${{ inputs.plan-artifact-id }}"
    assert download["run-id"] == "${{ github.run_id }}"
    assert download["digest-mismatch"] == "error"
    assert WORKSPACE_WORKFLOW["env"]["EXPECTED_PLAN_SHA"] == "${{ inputs.expected-plan-sha }}"
    assert WORKSPACE_WORKFLOW["env"]["INTENT_PATH"] == "${{ inputs.intent }}"
    upload = _build_step("Retain the unsigned workspace assets")["with"]
    assert upload["path"] == "${{ runner.temp }}/workspace-assets"
    assert upload["overwrite"] is False and upload["if-no-files-found"] == "error"
    assert "github.run_attempt" in upload["name"]
    assert not any("attest@" in step.get("uses", "") for step in job["steps"])
    review = CANDIDATE["jobs"]["review"]
    assert "workspace-assets" in review["needs"]
    assert "needs.workspace-assets.result == 'success'" in review["if"]
    assert "needs.workspace-assets.result == 'skipped'" in review["if"]
    production = _build_step("Produce the prepared workspaces")["run"]
    assert '--build-number "$GITHUB_RUN_ID" --build-attempt "$GITHUB_RUN_ATTEMPT"' in production
    assert '--prepared-plan "$RUNNER_TEMP/workspace-plan/plan.json" --expected-plan-sha "$EXPECTED_PLAN_SHA"' in production


@pytest.mark.parametrize("failure", [None, "python", "bicep", "checksum"])
def test_workspace_tooling_setup_uses_locked_feed_and_private_configuration(tmp_path, failure):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = tmp_path / "commands"
    write_executable(bin_dir / "python", """#!/usr/bin/env bash
case "$(basename "$0"):$1:$2" in
  python:-m:venv|python:-m:pip) ;;
  *) exit 99 ;;
esac
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$TOOL_CALLS"
if [[ "$(basename "$0")" == python && "$FAIL_TOOL" == python && "$2" == pip ]]; then exit 7; fi
""")
    write_executable(bin_dir / "curl", """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "$TOOL_CALLS"
[[ "$*" == *"https://github.com/Azure/bicep/releases/download/v0.45.15/bicep-linux-x64"* ]] || exit 99
if [[ "$FAIL_TOOL" == bicep ]]; then exit 8; fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --output ]]; then output="$2"; shift 2; else shift; fi
done
printf 'controlled compiler bytes' > "$output"
""")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    result = run_script(
        _build_step("Install workspace production tools")["run"], tmp_path,
        {
            "HOME": str(runtime / "home"), "AZURE_CONFIG_DIR": str(runtime / "azure"),
            "RUNNER_TEMP": runtime.as_posix(), "WORKSPACE_PYTHON": (bin_dir / "python").as_posix(),
            "BICEP_VERSION": WORKSPACE_WORKFLOW["jobs"]["build"]["env"]["BICEP_VERSION"],
            "BICEP_SHA256": "d" * 64 if failure == "checksum" else hashlib.sha256(b"controlled compiler bytes").hexdigest(),
            "TOOL_CALLS": commands.as_posix(), "FAIL_TOOL": failure or "",
        },
    )
    assert result.returncode == (0 if failure is None else 1), result.stdout + result.stderr
    calls = commands.read_text()
    assert "--require-hashes --only-binary=:all: --no-cache-dir" in calls
    assert "-r scripts/siteops-build-requirements.txt -r scripts/siteops-runtime-requirements.txt" in calls
    assert ("bicep-linux-x64" in calls) is (failure != "python")
    assert (runtime / "azure" / "bin" / "bicep").exists() is (failure is None)
    if failure == "checksum":
        assert "differs from its approved SHA-256" in result.stdout + result.stderr
    assert _build_step("Install workspace production tools")["env"]["PIP_INDEX_URL"] == (
        "https://packagefeedproxy.microsoft.io/pypi/simple/"
    )


def test_default_preview_declares_the_complete_workspace_path():
    ci = cached_safe_load((ROOT / ".github/workflows/ci.yaml").read_text())
    selected = ci[True]["workflow_dispatch"]["inputs"]["release-file"]["default"]
    declaration = json.loads((ROOT / selected).read_bytes())
    assert selected == ".github/release-examples/workspace-preview/release.json"
    assert declaration["siteops"] == {"build": True}
    assert len(declaration["workspaces"]) == 1
    request = WorkspaceBuild.from_document(declaration["workspaces"][0])
    assert request.workspace == "workspaces/iot-operations"
    assert (ROOT / request.workspace).is_dir()
    assert all((ROOT / path).is_file() for path in request.licenses)
    assert all((ROOT / path).exists() for path in request.includes)
    environment = WORKSPACE_WORKFLOW["jobs"]["build"]["env"]
    assert environment["BICEP_SHA256"] == "ff5b194b042c220df4a50d6768ed1d6c39a32894bfdc4ff83d62b115d966a7ce"
    script = _build_step("Install workspace production tools")["run"]
    assert script.index("sha256sum --check") < script.index('install -m 700')
    assert "az bicep install" not in script
    assert "--max-filesize 134217728" in script and "--tlsv1.2" in script


def _workspace_job(repository, tmp_path, *, combined=False, dry_run=False):
    request = _workspace(repository)
    _write_record(repository, _declaration([request], combined=combined))
    scripts = repository / "scripts"
    scripts.mkdir()
    for name in (
        "build-workspace-release.py", "workspace_producer.py", "workspace_release.py",
        "siteops_release.py", "siteops_release_assets.py", "source_snapshot.py",
    ):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    shutil.copytree(ROOT / "siteops", repository / "siteops", ignore=shutil.ignore_patterns("__pycache__"))
    _write_source_version(repository, '__version__ = "1.2.3"\n')
    (repository / ".gitignore").write_text("workflow-step.sh\nbin/\n")
    sha = _commit(repository, "workflow candidate")
    runtime = tmp_path / "runtime"
    (runtime / "workspace-plan").mkdir(parents=True)
    plan = _load(repository, sha).to_dict()
    plan["dryRun"] = dry_run
    raw = json.dumps(plan).encode()
    (runtime / "workspace-plan" / "plan.json").write_bytes(raw)
    output = tmp_path / "outputs"
    (repository / "bin").mkdir()
    write_executable(repository / "bin" / "az", "#!/usr/bin/env bash\nexit 99\n")
    environment = {
            "WORKSPACE_PYTHON": sys.executable.replace("\\", "/"), "GITHUB_WORKSPACE": repository.as_posix(),
            "GITHUB_REPOSITORY": REPOSITORY, "SOURCE_SHA": sha, "SOURCE_REF": SOURCE_REF,
            "EXPECTED_SOURCE_SHA": sha, "WORKSPACE_SELECTION": "workspace", "WORKSPACE_SLOT": "1",
            "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "3", "DRY_RUN": str(dry_run).lower(),
            "INTENT_PATH": "releases/candidate/release.json", "EXPECTED_PLAN_SHA": hashlib.sha256(raw).hexdigest(),
            "RUNNER_TEMP": runtime.as_posix(), "AZURE_CONFIG_DIR": (runtime / "azure").as_posix(),
            "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_OUTPUT": output.as_posix(),
    }
    return runtime, output, environment, sha


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_actual_workspace_build_step_runs_the_shared_producer(repository, tmp_path, combined, dry_run):
    runtime, output, environment, sha = _workspace_job(
        repository, tmp_path, combined=combined, dry_run=dry_run,
    )
    result = run_script(
        _build_step("Produce the prepared workspaces")["run"], repository, environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr + (
        (runtime / "workspace-build.err").read_text() if (runtime / "workspace-build.err").exists() else ""
    )
    record = runtime / "workspace-assets" / "workspace-builds.json"
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["record-sha"] == hashlib.sha256(record.read_bytes()).hexdigest()
    document = json.loads(record.read_bytes())
    assert document["source"]["commit"] == sha
    assert document["dryRun"] is dry_run
    assert values["package-name"] == "workspace.zip"
    assert values["package-sha"] == document["workspaces"][0]["package"]["sha256"]
    assert document["engineVersion"] == ("1.2.3+build.42.3.g" + sha[:12] if combined else "1.2.3")


@pytest.mark.parametrize("fault", ["extra", "digest", "size", "request", "record", "plan"])
def test_workspace_job_refuses_outputs_changed_after_production(repository, tmp_path, fault):
    runtime, output, environment, _ = _workspace_job(repository, tmp_path)
    wrapper = tmp_path / "python-wrapper.py"
    wrapper.write_text(
        """import hashlib, json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] != ["-B", "scripts/build-workspace-release.py"] and args[0] != "-c":
    raise SystemExit("Unexpected workspace Python invocation")
result = subprocess.run([sys.executable, *args], check=False)
if result.returncode or args[0] != "-B":
    raise SystemExit(result.returncode)
root = Path(os.environ["RUNNER_TEMP"])
record_path = root / "workspace-assets/workspace-builds.json"
record = json.loads(record_path.read_bytes())
package = root / "workspace-assets/workspace.zip"
fault = os.environ["OUTPUT_FAULT"]
if fault == "extra":
    (root / "workspace-assets/.extra").write_bytes(b"unlisted")
elif fault == "digest":
    package.write_bytes(b"x" * package.stat().st_size)
elif fault == "size":
    package.write_bytes(package.read_bytes() + b"x")
elif fault == "plan":
    (root / "workspace-plan/plan.json").write_bytes(b"{}")
elif fault in {"request", "record"}:
    record["workspaces"][0]["kit"]["id"] = "changed"
    raw = json.dumps(record).encode()
    record_path.write_bytes(raw)
    if fault == "request":
        path = root / "workspace-build-summary.json"
        summary = json.loads(path.read_bytes())
        summary["sha256"] = hashlib.sha256(raw).hexdigest()
        path.write_text(json.dumps(summary))
""",
        encoding="utf-8",
    )
    executable = repository / "bin" / "workspace-python"
    write_executable(executable, '#!/usr/bin/env bash\nexec "$REAL_PYTHON" "$JOB_WRAPPER" "$@"\n')
    environment.update(
        WORKSPACE_PYTHON=executable.as_posix(), REAL_PYTHON=sys.executable.replace("\\", "/"),
        JOB_WRAPPER=wrapper.as_posix(), OUTPUT_FAULT=fault,
    )
    result = run_script(_build_step("Produce the prepared workspaces")["run"], repository, environment)
    assert result.returncode != 0
    assert "::error::" in result.stdout + result.stderr
    assert not output.exists() or output.read_text() == ""


def test_release_workspace_selection_keeps_the_complete_prepared_intent(repository, tmp_path):
    requests = [_workspace(repository), _workspace(repository, "second")]
    _write_record(repository, _declaration(requests))
    sha = _commit(repository, "selected workspace")
    output = tmp_path / "selected"
    result = _produce(repository, sha, output, "--release-workspace", "second")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name for path in output.iterdir()} == {"second.zip", "workspace-builds.json"}
    record = json.loads((output / "workspace-builds.json").read_bytes())
    assert [row["workspace"] for row in record["workspaces"]] == ["second"]
    rejected = tmp_path / "unknown"
    result = _produce(repository, sha, rejected, "--release-workspace", "absent")
    assert result.returncode != 0 and not rejected.exists()

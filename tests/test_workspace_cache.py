"""Private package publication, verified reuse and process-held cache leases."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from siteops import package_builder
from siteops import workspace_cache as cache_module
from siteops.artifact_verification import ArtifactVerification
from siteops.artifacts import ArtifactError, hash_file
from siteops.cache_filesystem import (
    CacheError,
    cache_lock,
    check_private_node,
    make_private_directory,
)
from siteops.compilation import TemplateCompilationSession
from siteops.executor import DeploymentResult
from siteops.orchestrator import Orchestrator
from siteops.planning import CompilationBinding, PlanIntent, PlanStatus, SubmissionMode
from siteops.results import RunStatus
from siteops.workspace_cache import WorkspaceCache, default_cache_root

REVISION = "feed:release-7"


class Verifier:
    def __init__(self, **changes):
        self.calls = []
        self.changes = changes

    def __call__(self, artifact):
        self.calls.append(artifact)
        size, digest = hash_file(artifact, limit=128 * 1024 * 1024)
        now = datetime.now(timezone.utc)
        return replace(ArtifactVerification(
            sha256=digest, size=size,
            policy_id="test-publisher", policy_version=1, policy_sha256="1" * 64,
            root_sha256="2" * 64, proof_sha256="3" * 64,
            evaluated_at=now, valid_until=now + timedelta(days=1),
            verifier="test", verifier_version="1", evidence_json=b"{}",
        ), **self.changes)


@pytest.fixture
def package_archive(tmp_path):
    source = tmp_path / "source"
    workspace = source / "workspace"
    (workspace / "manifests").mkdir(parents=True)
    (workspace / "templates").mkdir()
    (workspace / "manifests" / "storage.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Manifest\nname: storage\nselector: name=one\nsteps:\n"
        "  - name: storage\n    template: templates/main.json\n    scope: resourceGroup\n",
        encoding="utf-8",
    )
    (workspace / "templates" / "main.json").write_text(json.dumps({
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0", "resources": [],
    }), encoding="utf-8")
    archive = tmp_path / "content.zip"
    inspection = package_builder.build_package(
        source, archive, workspace="workspace", kit_id="example.storage",
        version="7", source_revision=REVISION, siteops_range=">=1.0.0b1,<2",
    )
    return archive, inspection.sha256


@pytest.fixture
def populated(tmp_path, package_archive):
    cache = WorkspaceCache(tmp_path / "cache", lock_timeout=0)
    archive, digest = package_archive
    verifier = Verifier()
    inspection = cache.publish(archive, digest, source_revision=REVISION, verify=verifier)
    return cache, archive, digest, verifier, inspection


def test_default_cache_selection_is_lazy_and_has_one_override(tmp_path, monkeypatch):
    override = tmp_path / "override"
    assert default_cache_root({"SITEOPS_CACHE_DIR": str(override)}) == override
    assert not override.exists()
    if os.name == "nt":
        assert default_cache_root({"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "siteops" / "cache"
        with pytest.raises(CacheError):
            default_cache_root({})
    else:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert default_cache_root({}) == tmp_path / ".cache" / "siteops"
        assert default_cache_root({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "siteops"
        assert default_cache_root({"XDG_CACHE_HOME": ""}) == tmp_path / ".cache" / "siteops"
        assert default_cache_root({"XDG_CACHE_HOME": "relative"}) == tmp_path / ".cache" / "siteops"


@pytest.mark.parametrize("value", ["", " ", ".", "..", "relative", "..\\cache" if os.name == "nt" else "../cache"])
def test_invalid_cache_override_never_falls_back(value):
    with pytest.raises(CacheError):
        default_cache_root({"SITEOPS_CACHE_DIR": value})


def test_cache_initialization_preserves_unmarked_operator_directory(tmp_path):
    root = tmp_path / "operator"
    root.mkdir()
    sentinel = root / "site.yaml"
    sentinel.write_text("operator-owned", encoding="utf-8")
    with pytest.raises(ArtifactError):
        WorkspaceCache(root)
    assert list(root.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "operator-owned"


def test_new_nested_cache_is_private_and_reopens(tmp_path):
    root = tmp_path / "new-parent" / "cache"
    cache = WorkspaceCache(root)
    for path in (root.parent, root, root / "objects", root / "objects" / "sha256"):
        check_private_node(path, directory=True)
    check_private_node(root / "cache.json", directory=False)
    assert WorkspaceCache(root).root == cache.root


def test_publication_keeps_original_archive_and_exact_materialization(populated):
    cache, archive, digest, verifier, inspection = populated
    root = cache.root / "objects" / "sha256" / digest
    assert (root / "package.zip").read_bytes() == archive.read_bytes()
    assert inspection.validate_materialization(root / "content") == root / "content"
    assert len(verifier.calls) == 1
    assert verifier.calls[0].parent.parent == cache.root / "staging"
    assert not list((cache.root / "staging").iterdir())
    receipts = list((cache.root / "receipts" / digest).glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_bytes())["subject"]["digest"] == digest
    assert set(path.name for path in root.iterdir()) == {"package.zip", "content"}


def test_warm_lease_uses_no_source_and_never_trusts_stored_receipt(populated):
    cache, archive, digest, verifier, _ = populated
    archive.unlink()
    receipt = next((cache.root / "receipts" / digest).glob("*.json"))
    receipt.write_bytes(b'{"forged":"accepted"}')
    with patch("socket.socket", side_effect=AssertionError("Cache reuse must stay local")):
        with cache.lease(digest, source_revision=REVISION, verify=verifier) as content:
            binding = content.bind("storage")
            assert binding.manifest_path.name == "storage.yaml"
            assert content.verification.sha256 == digest
    assert len(verifier.calls) == 2
    assert json.loads(receipt.read_bytes())["kind"] == "ArtifactVerification"
    assert not list((cache.root / "staging").iterdir())


def test_repeat_publication_reuses_verified_object(populated):
    cache, archive, digest, verifier, inspection = populated
    archive.unlink()
    assert cache.publish(archive, digest, source_revision=REVISION, verify=verifier) == inspection
    assert len(verifier.calls) == 2


def test_miss_requires_exact_pinned_acquisition_without_verifier(tmp_path):
    cache = WorkspaceCache(tmp_path / "cache")
    verifier = Verifier()
    with pytest.raises(CacheError) as failure:
        with cache.lease("a" * 64, source_revision=REVISION, verify=verifier):
            pytest.fail("An absent package cannot be leased.")
    assert failure.value.code == "cache.missing"
    assert verifier.calls == []


@pytest.mark.parametrize("digest", ["A" * 64, "../elsewhere", "", "b" * 63, "c" * 65])
def test_digest_never_becomes_an_arbitrary_filesystem_path(tmp_path, digest):
    cache = WorkspaceCache(tmp_path / "cache")
    with pytest.raises(CacheError, match="lowercase SHA-256"):
        with cache.lease(digest, source_revision=REVISION, verify=Verifier()):
            pytest.fail("Invalid identity was accepted.")
    assert list((cache.root / "locks").iterdir()) == []


def test_provenance_failure_precedes_any_package_parser(tmp_path, package_archive, monkeypatch):
    archive, digest = package_archive
    cache = WorkspaceCache(tmp_path / "cache")

    def reject(_):
        raise ArtifactError("Publisher is not approved.")

    def unexpected(*args, **kwargs):
        pytest.fail("Unverified package bytes reached extraction.")

    monkeypatch.setattr(cache_module, "extract_package", unexpected)
    with pytest.raises(ArtifactError, match="not approved"):
        cache.publish(archive, digest, source_revision=REVISION, verify=reject)
    assert list((cache.root / "objects" / "sha256").iterdir()) == []
    assert list((cache.root / "staging").iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"sha256": "f" * 64},
    {"size": -1},
    {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
    {"valid_until": datetime(2100, 1, 1)},
    {"evaluated_at": datetime(2100, 1, 1, tzinfo=timezone.utc)},
    {"policy_sha256": "../../policy"},
])
def test_wrong_or_expired_verification_cannot_publish(tmp_path, package_archive, changes):
    archive, digest = package_archive
    cache = WorkspaceCache(tmp_path / "cache")
    with pytest.raises(CacheError):
        cache.publish(archive, digest, source_revision=REVISION, verify=Verifier(**changes))
    assert list((cache.root / "objects" / "sha256").iterdir()) == []
    assert list((cache.root / "staging").iterdir()) == []


def test_cached_policy_failure_stops_before_content_inspection(populated, monkeypatch):
    cache, _, digest, _, _ = populated
    monkeypatch.setattr(cache_module, "inspect_package", lambda *args: pytest.fail("Policy must pass first."))
    with pytest.raises(CacheError, match="not currently valid"):
        with cache.lease(
            digest, source_revision=REVISION,
            verify=Verifier(valid_until=datetime(2000, 1, 1, tzinfo=timezone.utc)),
        ):
            pytest.fail("Expired policy was accepted.")


def test_verifier_mutation_is_detected_before_extraction(tmp_path, package_archive):
    archive, digest = package_archive
    original = archive.read_bytes()
    cache = WorkspaceCache(tmp_path / "cache")

    def mutate(path):
        evidence = Verifier()(path)
        path.write_bytes(b"changed during verification")
        return evidence

    with pytest.raises(CacheError, match="changed during verification"):
        cache.publish(archive, digest, source_revision=REVISION, verify=mutate)
    assert archive.read_bytes() == original
    assert list((cache.root / "staging").iterdir()) == []


def test_policy_expiry_during_materialization_stops_publication(tmp_path, package_archive, monkeypatch):
    archive, digest = package_archive
    cache = WorkspaceCache(tmp_path / "cache")
    verification = Verifier()(archive)
    validate = cache._validate_content

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return verification.valid_until + timedelta(seconds=1)

    def finish_validation(*args):
        validate(*args)
        monkeypatch.setattr(cache_module, "datetime", Later)

    monkeypatch.setattr(cache, "_validate_content", finish_validation)
    with pytest.raises(CacheError, match="expired during cache validation"):
        cache.publish(archive, digest, source_revision=REVISION, verify=lambda _: verification)
    assert not list((cache.root / "objects" / "sha256").iterdir())
    assert not list((cache.root / "staging").iterdir())


def test_failed_publication_leaves_no_partial_object_or_source_changes(tmp_path, package_archive, monkeypatch):
    archive, digest = package_archive
    original = archive.read_bytes()
    cache = WorkspaceCache(tmp_path / "cache")
    rename = Path.rename

    def fail_publication(path, target):
        if path.parent == cache.root / "staging":
            raise OSError("publication unavailable")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publication)
    with pytest.raises(CacheError) as failure:
        cache.publish(archive, digest, source_revision=REVISION, verify=Verifier())
    assert failure.value.code == "cache.io"
    assert not list((cache.root / "objects" / "sha256").iterdir())
    assert not list((cache.root / "staging").iterdir())
    assert archive.read_bytes() == original


def test_caller_io_failure_is_not_reclassified_as_cache_failure(populated):
    cache, _, digest, verifier, _ = populated
    with pytest.raises(OSError, match="caller operation failed"):
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            raise OSError("caller operation failed")


@pytest.mark.parametrize("relative", [
    "package.zip",
    "content/siteops-package.json",
    "content/workspace/manifests/storage.yaml",
    "content/workspace/templates/main.json",
])
def test_tampered_cache_bytes_are_not_repaired_in_place(populated, relative):
    cache, archive, digest, verifier, _ = populated
    target = (cache.root / "objects" / "sha256" / digest).joinpath(*relative.split("/"))
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactError):
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            pytest.fail("Changed cache bytes were accepted.")
    with pytest.raises(ArtifactError):
        cache.publish(archive, digest, source_revision=REVISION, verify=verifier)
    assert target.read_bytes() == b"tampered"


@pytest.mark.parametrize("directory", [False, True])
def test_unexpected_materialized_node_invalidates_lease(populated, directory):
    cache, _, digest, verifier, _ = populated
    target = cache.root / "objects" / "sha256" / digest / "content" / "extra"
    if directory:
        target.mkdir()
    else:
        target.write_bytes(b"extra")
    with pytest.raises(ArtifactError, match="inventory"):
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            pytest.fail("Unexpected content was accepted.")


def test_source_revision_is_independently_bound(populated):
    cache, _, digest, verifier, _ = populated
    with pytest.raises(CacheError, match="resolved source"):
        with cache.lease(digest, source_revision="feed:other-release", verify=verifier):
            pytest.fail("A different resolved source was accepted.")


def test_shared_use_excludes_publication_and_releases_after_caller_failure(populated):
    cache, archive, digest, verifier, _ = populated
    with pytest.raises(RuntimeError, match="caller failure"):
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            with cache.lease(digest, source_revision=REVISION, verify=verifier):
                pass
            with pytest.raises(CacheError) as failure:
                cache.publish(archive, digest, source_revision=REVISION, verify=verifier)
            assert failure.value.code == "cache.busy"
            raise RuntimeError("caller failure")
    cache.publish(archive, digest, source_revision=REVISION, verify=verifier)


_CHILD_LOCK = """
import sys
from pathlib import Path
from siteops.cache_filesystem import CacheError, cache_lock
try:
    with cache_lock(Path(sys.argv[1]), exclusive=sys.argv[2] == "exclusive", timeout=0):
        print("held", flush=True)
        if len(sys.argv) > 3:
            sys.stdin.readline()
except CacheError as error:
    if error.code != "cache.busy":
        raise
    print("busy", flush=True)
"""


def test_native_leases_coordinate_across_processes(populated):
    cache, _, digest, verifier, _ = populated
    lock = cache.root / "locks" / f"{digest}.lock"
    with cache.lease(digest, source_revision=REVISION, verify=verifier):
        for mode, expected in (("shared", "held"), ("exclusive", "busy")):
            result = subprocess.run(
                [sys.executable, "-c", _CHILD_LOCK, str(lock), mode],
                check=True, text=True, capture_output=True, timeout=15,
            )
            assert result.stdout.strip() == expected
    with cache_lock(lock, exclusive=True, timeout=0):
        pass


def test_native_lock_releases_after_process_exit(tmp_path):
    root = tmp_path / "locks"
    make_private_directory(root)
    lock = root / "package.lock"
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_LOCK, str(lock), "exclusive", "wait"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(CacheError, match="in use"):
            with cache_lock(lock, exclusive=True, timeout=0):
                pytest.fail("Another process holds this lock.")
    finally:
        process.terminate()
        process.communicate(timeout=15)
    with cache_lock(lock, exclusive=True, timeout=0):
        pass


_CHILD_PUBLISH = """
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from siteops.artifacts import hash_file
from siteops.artifact_verification import ArtifactVerification
from siteops.workspace_cache import WorkspaceCache
def verify(path):
    size, digest = hash_file(path, limit=128 * 1024 * 1024)
    now = datetime.now(timezone.utc)
    return ArtifactVerification(
        digest, size, "test", 1, "1" * 64, "2" * 64, "3" * 64,
        now, now + timedelta(days=1), "test", "1", b"{}",
    )
sys.stdin.readline()
cache = WorkspaceCache(Path(sys.argv[1]))
cache.publish(Path(sys.argv[2]), sys.argv[3], source_revision="feed:release-7", verify=verify)
print("published", flush=True)
"""


def test_concurrent_native_initialization_and_publication_converge(tmp_path, package_archive):
    archive, digest = package_archive
    root = tmp_path / "cache"
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", _CHILD_PUBLISH, str(root), str(archive), digest],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in processes:
            output, error = process.communicate(timeout=30)
            assert process.returncode == 0, error
            assert output.strip() == "published"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
    assert len(list((root / "objects" / "sha256").iterdir())) == 1
    assert not list((root / "staging").iterdir())
    assert not list(tmp_path.glob(".siteops-cache-*"))
    with WorkspaceCache(root).lease(digest, source_revision=REVISION, verify=Verifier()):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows access-control contract")
@pytest.mark.parametrize("target", ["root", "archive", "payload"])
def test_windows_cache_rejects_shared_access_without_repair(populated, target):
    cache, _, digest, verifier, _ = populated
    root = cache.root / "objects" / "sha256" / digest
    path = {
        "root": cache.root, "archive": root / "package.zip",
        "payload": root / "content" / "workspace" / "templates" / "main.json",
    }[target]
    subprocess.run(
        [str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
         str(path), "/grant", "*S-1-1-0:R"],
        check=True, capture_output=True, timeout=15,
    )
    with pytest.raises(CacheError) as failure:
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            pytest.fail("Shared cache access was accepted.")
    assert failure.value.code == "cache.permissions"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions contract")
@pytest.mark.parametrize("relative", ["", "objects", "locks"])
def test_posix_cache_rejects_shared_mode_without_repair(populated, relative):
    cache, _, digest, verifier, _ = populated
    path = cache.root / relative
    path.chmod(0o755)
    with pytest.raises(CacheError) as failure:
        with cache.lease(digest, source_revision=REVISION, verify=verifier):
            pytest.fail("Shared cache access was accepted.")
    assert failure.value.code == "cache.permissions"
    assert path.stat().st_mode & 0o777 == 0o755


def test_symlink_cache_root_preserves_its_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "alias"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable for this account.")
    with pytest.raises(ArtifactError):
        WorkspaceCache(root)
    assert list(target.iterdir()) == []


def test_cached_non_aio_package_uses_existing_planner_and_executor(populated, tmp_path):
    cache, archive, digest, verifier, _ = populated
    archive.unlink()
    project = tmp_path / "operator"
    (project / "sites").mkdir(parents=True)
    (project / "sites" / "one.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: one\nsubscription: sub\n"
        "resourceGroup: group\nlocation: eastus\n", encoding="utf-8",
    )
    before = (project / "sites" / "one.yaml").read_bytes()

    def command_runner(argv, timeout):
        assert argv[1:] == ("version", "--output", "json")
        return subprocess.CompletedProcess(argv, 0, stdout='{"azure-cli":"test"}', stderr="")

    session = TemplateCompilationSession(
        command_runner=command_runner,
        tool_resolver=lambda name: str(tmp_path / "tools" / f"{name}.exe"),
    )
    provider = Mock()
    provider.deploy_resource_group.side_effect = lambda **arguments: DeploymentResult(
        success=True, step_name=arguments["step_name"], site_name=arguments["site_name"],
        deployment_name=arguments["deployment_name"],
    )
    with (
        cache.lease(digest, source_revision=REVISION, verify=verifier) as content,
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
        patch("siteops.executor.subprocess.Popen", side_effect=AssertionError("No live operations.")),
    ):
        binding = content.bind("storage")
        orchestrator = Orchestrator(
            binding.workspace,
            site_config_root=project, materialized_package=binding,
            executor=provider,
        )
        result = orchestrator.build_plan(binding.manifest_path, intent=PlanIntent.EXECUTABLE)
        assert result.status == PlanStatus.PLANNED
        assert result.plan.submission_mode == SubmissionMode.ARM_JSON
        assert result.plan.compilation_binding == CompilationBinding.PACKAGE_ARTIFACT
        execution = orchestrator.deploy(binding.manifest_path, plan_result=result)
        assert execution.status == RunStatus.SUCCEEDED
        assert provider.deploy_resource_group.call_args.kwargs["template_path"] == (
            binding.workspace / "templates" / "main.json"
        )
    assert (project / "sites" / "one.yaml").read_bytes() == before
    assert hashlib.sha256((cache.root / "objects" / "sha256" / digest / "package.zip").read_bytes()).hexdigest() == digest

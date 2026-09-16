"""Proof storage, source admission and offline use through the existing engine."""

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from siteops import workspace_cache as storage
from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import CacheError, check_private_node, make_private_directory
from siteops.compilation import TemplateCompilationSession
from siteops.executor import DeploymentResult
from siteops.orchestrator import Orchestrator
from siteops.planning import CompilationBinding, PlanIntent, PlanStatus, SubmissionMode
from siteops.results import RunStatus
from siteops.workspace_acquisition import WorkspaceAcquisition
from siteops.workspace_cache import WorkspaceCache
from siteops.workspace_source import SourceResolutionError
from tests.workspace_acquisition_helpers import make_source


@pytest.fixture
def fixture(tmp_path):
    return make_source(tmp_path)


@pytest.fixture
def acquired(fixture):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    flow = WorkspaceAcquisition(fixture.cache, verify=fixture.verifier)
    flow.publish(fixture.source, fixture.archive)
    return fixture, flow


def proof_root(fixture):
    return fixture.cache.root / "proofs" / "sha256" / fixture.source.entry.proof.sha256


def package_root(fixture):
    return fixture.cache.root / "objects" / "sha256" / fixture.source.entry.package.sha256


def test_proof_namespace_is_optional_private_and_preserves_existing_packages(fixture):
    cache = fixture.cache
    marker = (cache.root / "cache.json").read_bytes()
    cache.publish(
        fixture.archive, fixture.source.entry.package.sha256,
        source_revision=fixture.source.source.revision,
        verify=lambda archive: fixture.verifier(archive, fixture.proof, fixture.source),
    )
    package_before = (package_root(fixture) / "package.zip").stat()
    assert not (cache.root / "proofs").exists()
    reopened = WorkspaceCache(cache.root)
    reopened.retain_proof(fixture.proof, fixture.source.entry.proof)
    assert (cache.root / "cache.json").read_bytes() == marker
    assert (package_root(fixture) / "package.zip").stat() == package_before
    for path in (cache.root / "proofs", cache.root / "proofs" / "sha256", proof_root(fixture)):
        check_private_node(path, directory=True)
    with WorkspaceCache(cache.root).lease_proof(fixture.source.entry.proof) as path:
        assert path.name == "proof.bin"
        assert path.read_bytes() == fixture.proof.read_bytes()
        check_private_node(path, directory=False)
    assert not list((cache.root / "staging").iterdir())


def test_invalid_existing_proof_namespace_is_preserved(fixture):
    existing = fixture.cache.root / "proofs"
    make_private_directory(existing)
    sentinel = existing / "operator.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(CacheError):
        fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    assert list(existing.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"


def test_missing_proof_is_explicit_and_does_not_create_a_namespace(fixture):
    with pytest.raises(CacheError) as caught:
        with fixture.cache.lease_proof(fixture.source.entry.proof):
            pytest.fail("Missing proof was yielded.")
    assert caught.value.code == "cache.proof-missing"
    assert not (fixture.cache.root / "proofs").exists()


@pytest.mark.parametrize("body", [b"x", b"x" * 26, b"x" * 1024])
def test_changed_proof_stops_before_retention(fixture, body):
    fixture.proof.write_bytes(body)
    with pytest.raises(ArtifactError):
        fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    assert not proof_root(fixture).exists()
    assert not list((fixture.cache.root / "staging").iterdir())


def test_retained_proof_reuse_does_not_read_the_original(fixture):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    fixture.proof.unlink()
    before = (proof_root(fixture) / "proof.bin").stat()
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    assert (proof_root(fixture) / "proof.bin").stat() == before


def test_proof_shared_use_blocks_exclusive_retention(fixture):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    with fixture.cache.lease_proof(fixture.source.entry.proof):
        with fixture.cache.lease_proof(fixture.source.entry.proof):
            with pytest.raises(CacheError) as caught:
                fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
            assert caught.value.code == "cache.busy"
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)


def test_proof_changes_during_use_are_detected(fixture):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    with pytest.raises(ArtifactError):
        with fixture.cache.lease_proof(fixture.source.entry.proof) as path:
            path.write_bytes(b"changed")


def test_proof_lease_preserves_caller_failure(fixture):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    with pytest.raises(OSError, match="caller"):
        with fixture.cache.lease_proof(fixture.source.entry.proof):
            raise OSError("caller")


@pytest.mark.parametrize("relative", ["proof.bin", "extra"])
def test_corrupt_proof_is_never_repaired_in_place(acquired, relative):
    fixture, flow = acquired
    changed = proof_root(fixture) / relative
    changed.write_bytes(b"corrupt")
    with pytest.raises(ArtifactError):
        fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    with pytest.raises(ArtifactError):
        with flow.lease(fixture.source):
            pytest.fail("Corrupt evidence was used.")
    assert changed.read_bytes() == b"corrupt"
    assert len(fixture.verifier.calls) == 1


@pytest.mark.parametrize("relative", ["proofs", "proofs/sha256", "object", "file"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode checks")
def test_shared_proof_permissions_are_not_repaired(acquired, relative):
    fixture, flow = acquired
    path = {
        "object": proof_root(fixture), "file": proof_root(fixture) / "proof.bin",
    }.get(relative, fixture.cache.root.joinpath(*relative.split("/")))
    path.chmod(0o755 if path.is_dir() else 0o644)
    with pytest.raises(CacheError) as caught:
        with flow.lease(fixture.source):
            pytest.fail("Shared proof storage was accepted.")
    assert caught.value.code == "cache.permissions"
    assert path.stat().st_mode & 0o077


def test_failed_proof_rename_keeps_staging_and_published_inventory_complete(fixture, monkeypatch):
    fixture.cache._prepare_proofs()
    rename = Path.rename

    def fail(path, target):
        if target == proof_root(fixture):
            raise OSError("rename")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(CacheError) as caught:
        fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    assert caught.value.code == "cache.io"
    assert not proof_root(fixture).exists()
    assert not list((fixture.cache.root / "staging").iterdir())
    assert fixture.proof.exists()


def test_verification_failure_precedes_package_parsing(fixture, monkeypatch):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    verifier = Mock(side_effect=ArtifactError("Publisher rejected."))
    flow = WorkspaceAcquisition(fixture.cache, verify=verifier)
    monkeypatch.setattr(storage, "extract_package", Mock(side_effect=AssertionError("Unverified parse.")))
    with pytest.raises(ArtifactError, match="Publisher"):
        flow.publish(fixture.source, fixture.archive)
    storage.extract_package.assert_not_called()
    assert not package_root(fixture).exists()
    assert not list((fixture.cache.root / "staging").iterdir())


def test_verification_must_identify_the_selected_proof(fixture, monkeypatch):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    fixture.verifier.changes = {"proof_sha256": "0" * 64}
    flow = WorkspaceAcquisition(fixture.cache, verify=fixture.verifier)
    monkeypatch.setattr(storage, "extract_package", Mock(side_effect=AssertionError("Wrong proof parsed.")))
    with pytest.raises(SourceResolutionError) as caught:
        flow.publish(fixture.source, fixture.archive)
    assert caught.value.code == "source.identity"
    storage.extract_package.assert_not_called()
    assert not package_root(fixture).exists()


@pytest.mark.parametrize("changes", [
    {"kit_id": "other"}, {"kit_version": "8"}, {"workspace": "other"},
])
def test_source_selection_is_checked_before_publication(fixture, changes):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)
    flow = WorkspaceAcquisition(fixture.cache, verify=fixture.verifier)
    wrong = replace(fixture.source, entry=replace(fixture.source.entry, **changes))
    with pytest.raises(SourceResolutionError, match="metadata differs"):
        flow.publish(wrong, fixture.archive)
    assert not package_root(fixture).exists()
    assert not list((fixture.cache.root / "staging").iterdir())
    assert not list((fixture.cache.root / "receipts").iterdir())


def test_source_selection_is_checked_on_cache_reuse(acquired):
    fixture, flow = acquired
    wrong = replace(fixture.source, entry=replace(fixture.source.entry, kit_version="other"))
    with pytest.raises(SourceResolutionError):
        with flow.lease(wrong):
            pytest.fail("A different package selection was leased.")
    with pytest.raises(SourceResolutionError):
        flow.publish(wrong, fixture.archive)
    assert package_root(fixture).exists()


def test_pinned_reuse_is_local_and_revalidates_current_policy(acquired):
    fixture, _ = acquired
    fixture.archive.unlink()
    fixture.proof.unlink()
    fixture.verifier.version = 2
    receipt = next((fixture.cache.root / "receipts").rglob("*.json"))
    receipt.write_bytes(b'{"forged":"accepted"}')
    flow = WorkspaceAcquisition(WorkspaceCache(fixture.cache.root), verify=fixture.verifier)
    with patch("socket.socket", side_effect=AssertionError("Pinned use must remain local.")):
        with flow.lease(fixture.source) as content:
            assert content.verification.policy_version == 2
            assert content.verification.proof_sha256 == fixture.source.entry.proof.sha256
            assert content.bind("storage").manifest_path.name == "storage.yaml"
    assert len(fixture.verifier.calls) == 2
    assert len(list((fixture.cache.root / "receipts").rglob("*.json"))) == 2


def test_expired_policy_blocks_reuse_before_package_inspection(acquired, monkeypatch):
    fixture, flow = acquired
    fixture.verifier.changes = {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    monkeypatch.setattr(storage, "inspect_package", Mock(side_effect=AssertionError("Expired evidence parsed.")))
    with pytest.raises(CacheError, match="not currently valid"):
        with flow.lease(fixture.source):
            pytest.fail("Expired policy was reused.")
    storage.inspect_package.assert_not_called()


def test_proof_mutation_by_verifier_stops_before_package_parser(fixture, monkeypatch):
    fixture.cache.retain_proof(fixture.proof, fixture.source.entry.proof)

    def mutate(archive, proof, selected):
        result = fixture.verifier(archive, proof, selected)
        proof.write_bytes(b"changed")
        return result

    flow = WorkspaceAcquisition(fixture.cache, verify=mutate)
    monkeypatch.setattr(storage, "extract_package", Mock(side_effect=AssertionError("Mutated proof parsed.")))
    with pytest.raises(ArtifactError):
        flow.publish(fixture.source, fixture.archive)
    storage.extract_package.assert_not_called()
    assert not package_root(fixture).exists()


def test_missing_retained_proof_stops_before_verifier(fixture):
    flow = WorkspaceAcquisition(fixture.cache, verify=fixture.verifier)
    with pytest.raises(CacheError) as caught:
        flow.publish(fixture.source, fixture.archive)
    assert caught.value.code == "cache.proof-missing"
    assert fixture.verifier.calls == []
    assert not package_root(fixture).exists()


def test_non_github_non_aio_pinned_package_uses_the_existing_executor(acquired, tmp_path):
    fixture, flow = acquired
    fixture.archive.unlink()
    fixture.proof.unlink()
    project = tmp_path / "operator"
    (project / "sites").mkdir(parents=True)
    site = project / "sites" / "one.yaml"
    site.write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: one\nsubscription: sub\n"
        "resourceGroup: group\nlocation: eastus\n", encoding="utf-8",
    )
    before = site.read_bytes()

    def runner(argv, timeout):
        assert argv[1:] == ("version", "--output", "json")
        return subprocess.CompletedProcess(argv, 0, stdout='{"azure-cli":"test"}', stderr="")

    session = TemplateCompilationSession(
        command_runner=runner, tool_resolver=lambda name: str(tmp_path / f"{name}.exe"),
    )
    executor = Mock()
    executor.deploy_resource_group.side_effect = lambda **args: DeploymentResult(
        success=True, step_name=args["step_name"], site_name=args["site_name"],
        deployment_name=args["deployment_name"],
    )
    with (
        patch("socket.socket", side_effect=AssertionError("No source network.")),
        patch("siteops.orchestrator.TemplateCompilationSession", return_value=session),
        patch("siteops.executor.subprocess.Popen", side_effect=AssertionError("No live deployment.")),
        flow.lease(fixture.source) as content,
    ):
        binding = content.bind("storage")
        engine = Orchestrator(
            binding.workspace, site_config_root=project, materialized_package=binding,
            executor=executor,
        )
        plan = engine.build_plan(binding.manifest_path, intent=PlanIntent.EXECUTABLE)
        assert plan.status == PlanStatus.PLANNED
        assert plan.plan.submission_mode == SubmissionMode.ARM_JSON
        assert plan.plan.compilation_binding == CompilationBinding.PACKAGE_ARTIFACT
        assert engine.deploy(binding.manifest_path, plan_result=plan).status == RunStatus.SUCCEEDED
        assert executor.deploy_resource_group.call_args.kwargs["template_path"] == (
            binding.workspace / "templates" / "main.json"
        )
    assert site.read_bytes() == before
    assert fixture.source.source.provider == "example-registry/v1"
    assert json.loads(next((fixture.cache.root / "receipts").rglob("*.json")).read_bytes())["verifier"] == {
        "name": "test-verifier", "version": "1",
    }

"""Bind public engine references to qualified selection without supplying consumer trust."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from siteops_release_assets import (  # noqa: E402
    ENGINE_REFERENCE_NAME,
    EngineReference,
    FrozenReleaseAssets,
    ReferencedEngine,
    ReleaseAsset,
    ReleaseAssetsError,
    engine_reference,
)
from workspace_engine import EngineSelection  # noqa: E402

WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/_release-candidate.yaml").read_text())


@pytest.fixture
def selected():
    source = {"repository": "example/content", "commit": "a" * 40, "ref": "refs/heads/main"}
    assets = tuple(ReleaseAsset(name, 10, str(index) * 64) for index, name in enumerate((
        "siteops-install.zip", "siteops-install.zip.attestation.jsonl",
        "siteops-1.0.0b1+build.42.1.gaaaaaaaaaaaa-py3-none-any.whl",
        "siteops-1.0.0b1+build.42.1.gaaaaaaaaaaaa-py3-none-any.whl.attestation.jsonl",
    ), 1))
    plan = {
        "source": source, "release": {"tag": "v1.0.0b7"}, "dryRun": False,
        "siteops": {"bundle": True, "releaseTag": None},
    }
    selection = EngineSelection(
        source, "f" * 64, FrozenReleaseAssets(**{
            "repository": source["repository"], "commit": source["commit"],
            "source_ref": source["ref"], "assets": assets,
        }), "1.0.0b1+build.42.1.gaaaaaaaaaaaa", "e" * 64, (("3.11", "linux-x86_64"),),
    )
    return plan, selection


@pytest.mark.parametrize("built", [False, True])
def test_reference_projects_full_build_and_resolved_source_identities(selected, built):
    plan, selection = selected
    if not built:
        reference = ReferencedEngine("71", "siteops/v1.0.0b1", "b" * 40, selection.native.assets)
        selection = replace(selection, version="1.0.0b1", reference=reference,
                            native=replace(selection.native, commit="c" * 40))
        plan["siteops"] = {"bundle": False, "releaseTag": reference.tag}
    record = engine_reference(plan, selection.document())
    assert record.revision == "a" * 40
    assert record.engine_revision == ("a" * 40 if built else "c" * 40)
    assert record.version == selection.version
    assert record.engine_release == ("v1.0.0b7" if built else "siteops/v1.0.0b1")
    assert record.bundle == selection.native.assets[0]
    assert record.proof == selection.native.assets[1]
    assert EngineReference.from_bytes(record.serialized()) == record
    text = record.serialized().decode()
    assert all(value not in text for value in ("example/content", "releaseId", "sourceRef", "signer", "targets", "url"))


def test_common_record_accepts_opaque_provider_revisions(selected):
    plan, selection = selected
    record = replace(engine_reference(plan, selection.document()),
                     revision="revision-42", engine_revision="revision-17",
                     release="content-release", engine_release="engine-release")
    assert EngineReference.from_bytes(record.serialized()) == record


@pytest.mark.parametrize("fault", [
    "extra-policy", "extra-source", "empty-revision", "preview-type", "bundle-name",
    "proof-name", "size", "digest", "duplicate", "oversize", "missing", "invalid-json",
])
def test_reference_rejects_unsupported_or_ambiguous_fields(selected, fault):
    plan, selection = selected
    document = engine_reference(plan, selection.document()).document()
    if fault == "extra-policy":
        document["policy"] = {"allow": True}
    elif fault == "extra-source":
        document["engine"]["url"] = "https://example.invalid/unapproved.zip"
    elif fault == "empty-revision":
        document["engine"]["revision"] = ""
    elif fault == "preview-type":
        document["preview"] = 1
    elif fault in {"bundle-name", "proof-name"}:
        document["engine"]["bundle" if fault == "bundle-name" else "proof"]["name"] = "../other.zip"
    elif fault == "size":
        document["engine"]["bundle"]["size"] = True
    elif fault == "digest":
        document["engine"]["bundle"]["sha256"] = "not-a-digest"
    elif fault == "missing":
        del document["engine"]["version"]
    raw = json.dumps(document).encode()
    if fault == "duplicate":
        raw = raw.replace(b'"preview": false', b'"preview": false, "preview": false')
    elif fault == "oversize":
        raw += b" " * 16385
    elif fault == "invalid-json":
        raw = b"not json"
    with pytest.raises(ReleaseAssetsError):
        EngineReference.from_bytes(raw)


def test_generation_refuses_a_different_publisher(selected):
    plan, selection = selected
    other = replace(selection, native=replace(selection.native, repository="other/publisher"))
    with pytest.raises(ReleaseAssetsError, match="source"):
        engine_reference(plan, other.document())


def test_preparation_entrypoint_emits_the_record_from_its_bound_selection(selected, tmp_path, monkeypatch, capsys):
    plan, selection = selected
    spec = importlib.util.spec_from_file_location("prepare_engine_reference", ROOT / "scripts/prepare-workspace-engine.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "load_release_intent", lambda *args, **kwargs: SimpleNamespace(to_dict=lambda: plan))
    monkeypatch.setattr(helper, "bind_prepared_plan", lambda *args: selection.plan_sha256)
    monkeypatch.setattr(helper, "prepare_engine", lambda *args, **kwargs: selection)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Live tools escaped preparation."))
    record = tmp_path / "reference" / ENGINE_REFERENCE_NAME
    monkeypatch.setattr(sys, "argv", [
        "prepare-workspace-engine.py", "--root", str(tmp_path), "--repository", "example/content",
        "--source-sha", "a" * 40, "--source-ref", "refs/heads/main",
        "--release-file", "releases/test/release.json", "--prepared-plan", str(tmp_path / "plan.json"),
        "--expected-plan-sha", selection.plan_sha256, "--builder-workflow", ".github/workflows/release.yaml",
        "--output", str(tmp_path / "output"), "--control", str(tmp_path / "control"),
        "--trusted-root", str(tmp_path / "roots.json"), "--build-number", "42", "--build-attempt", "1",
        "--expected-runner-environment", "self-hosted", "--engine-reference-output", str(record),
    ])
    assert helper.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["engineReferenceSha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
    assert result["engineRevision"] == selection.native.commit
    assert EngineReference.from_bytes(record.read_bytes()) == engine_reference(plan, selection.document())


def test_signing_is_isolated_and_required_before_assembly():
    jobs = WORKFLOW["jobs"]
    signing = jobs["engine-reference"]
    assert signing["needs"] == ["prepare", "engine-input"]
    assert signing["runs-on"] == ["${{ inputs.release-pool }}"]
    assert signing["permissions"] == {
        "contents": "read", "actions": "read", "id-token": "write", "attestations": "write",
    }
    assert "environment" not in signing
    assert not any(step.get("uses", "").startswith("actions/checkout") for step in signing["steps"])
    assert "engine-reference" in jobs["review"]["needs"]
    assert "needs.engine-reference.result == 'success'" in jobs["review"]["if"]
    download = next(step for step in signing["steps"] if step.get("name") == "Download the exact engine reference")
    assert download["with"]["artifact-ids"] == "${{ needs.engine-input.outputs.reference-artifact-id }}"
    assert download["with"]["digest-mismatch"] == "error"
    attest = next(step for step in signing["steps"] if step.get("id") == "attest")
    assert attest["with"]["subject-path"].endswith("/engine-reference-subject/siteops-engine.json")


@pytest.mark.parametrize(("job", "prerequisite"), [("engine-reference", "engine-input"), ("admit", "review")])
def test_content_only_signing_handles_the_skipped_build_without_accepting_failed_selection(job, prerequisite):
    jobs = WORKFLOW["jobs"]
    assert "needs.distribution.result == 'skipped'" in jobs["engine-input"]["if"]
    signing = jobs[job]
    assert signing["needs"] == ["prepare", prerequisite]
    assert "always()" in signing["if"]
    assert "!cancelled()" in signing["if"]
    assert "needs.prepare.result == 'success'" in signing["if"]
    assert f"needs.{prerequisite}.result == 'success'" in signing["if"]


@pytest.mark.parametrize("fault", [None, "bytes", "revision", "release", "preview", "engine", "extra-file"])
def test_signing_admits_only_the_generated_subject(selected, tmp_path, monkeypatch, fault):
    plan, selection = selected
    document = engine_reference(plan, selection.document()).document()
    if fault in {"revision", "release"}:
        document[fault] = "other"
    elif fault == "preview":
        document["preview"] = True
    elif fault == "engine":
        document["engine"]["revision"] = "b" * 40
    raw = json.dumps(document).encode()
    root = tmp_path / "engine-reference-subject"
    root.mkdir()
    (root / ENGINE_REFERENCE_NAME).write_bytes(raw)
    if fault == "extra-file":
        (root / "extra.txt").write_text("extra")
    for name, value in {
        "RUNNER_TEMP": str(tmp_path), "EXPECTED_REFERENCE_SHA": "0" * 64 if fault == "bytes" else hashlib.sha256(raw).hexdigest(),
        "RELEASE_TAG": plan["release"]["tag"], "SOURCE_SHA": "a" * 40, "DRY_RUN": "false",
        "EXPECTED_ENGINE_REVISION": selection.native.commit, "EXPECTED_ENGINE_VERSION": selection.version,
    }.items():
        monkeypatch.setenv(name, value)
    body = next(step["run"] for step in WORKFLOW["jobs"]["engine-reference"]["steps"]
                if step.get("name") == "Admit the generated engine reference")
    if fault:
        with pytest.raises(SystemExit):
            exec(compile(body, "<engine-reference-signing>", "exec"), {})
    else:
        exec(compile(body, "<engine-reference-signing>", "exec"), {})


@pytest.mark.parametrize("changed", [False, True])
def test_signing_rechecks_bytes_when_retaining_proof(selected, tmp_path, monkeypatch, changed):
    plan, selection = selected
    raw = engine_reference(plan, selection.document()).serialized()
    subject = tmp_path / "engine-reference-subject"
    subject.mkdir()
    (subject / ENGINE_REFERENCE_NAME).write_bytes(raw + (b" " if changed else b""))
    proof = tmp_path / "proof.jsonl"
    proof.write_bytes(b"synthetic proof")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("EXPECTED_REFERENCE_SHA", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("PROOF_PATH", str(proof))
    body = next(step["run"] for step in WORKFLOW["jobs"]["engine-reference"]["steps"]
                if step.get("name") == "Retain the engine reference and proof")
    if changed:
        with pytest.raises(SystemExit, match="changed"):
            exec(compile(body, "<retain-engine-reference>", "exec"), {})
        assert not any((tmp_path / "engine-reference-attested").iterdir())
    else:
        exec(compile(body, "<retain-engine-reference>", "exec"), {})
        output = tmp_path / "engine-reference-attested"
        assert {path.name for path in output.iterdir()} == {
            ENGINE_REFERENCE_NAME, ENGINE_REFERENCE_NAME + ".attestation.jsonl",
        }
        assert (output / ENGINE_REFERENCE_NAME).read_bytes() == raw
        assert (output / (ENGINE_REFERENCE_NAME + ".attestation.jsonl")).read_bytes() == proof.read_bytes()

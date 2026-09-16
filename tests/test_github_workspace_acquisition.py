"""Release acquisition, current policy and pinned reuse through actual verifier parsing."""

import hashlib
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from siteops import cli, github_source
from siteops import github_attestation as attestation
from siteops import github_workspace_acquisition as acquisition
from siteops import github_workspace_source as source
from siteops.artifacts import ArtifactError
from siteops.cache_filesystem import CacheError, make_private_directory
from siteops.compilation import TemplateCompilationSession
from siteops.executor import DeploymentResult
from siteops.github_source import (
    GitHubClient,
    GitHubReference,
    GitHubReleaseAsset,
    GitHubReleaseSnapshot,
)
from siteops.workspace_source import WORKSPACE_RELEASE_NAME
from tests.workspace_acquisition_helpers import make_source


@pytest.fixture
def github(tmp_path, monkeypatch):
    fixture = make_source(
        tmp_path, provider="github-release/v1", reference="github:example/content", revision="a" * 40,
    )
    inputs = {
        WORKSPACE_RELEASE_NAME: fixture.descriptor,
        fixture.source.entry.package.name: fixture.archive.read_bytes(),
        fixture.source.entry.proof.name: fixture.proof.read_bytes(),
    }
    identities = (fixture.source.source.descriptor, fixture.source.entry.package, fixture.source.entry.proof)
    assets = tuple(
        GitHubReleaseAsset(number, value.name, value.size, value.sha256)
        for number, value in enumerate(identities, start=100)
    )
    reference = GitHubReference("example", "content", "release-7")
    release = GitHubReleaseSnapshot(
        reference, 1, 2, "a" * 40, "a" * 40, True,
        datetime(2026, 1, 1, tzinfo=timezone.utc), False, assets,
    )
    client = Mock(spec=GitHubClient)
    client.auth = "anonymous"
    client.reference = reference
    client.resolve_release.return_value = release
    roots = tmp_path / "roots.jsonl"
    roots.write_bytes(b'{"fixture":"local trusted roots"}\n')
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
        "id": "approved-content", "version": 1, "validUntil": "2100-01-01T00:00:00Z",
        "trustedRootSha256": hashlib.sha256(roots.read_bytes()).hexdigest(),
        "provider": {
            "kind": "github-attestation/v1", "repository": "example/content",
            "sourceRef": "refs/heads/main",
            "signerWorkflow": ".github/workflows/sign.yml",
            "builderWorkflow": ".github/workflows/release.yml",
        },
    }), encoding="utf-8")
    executable = tmp_path / "gh.exe"
    executable.write_bytes(b"test executable placeholder")
    state = SimpleNamespace(
        fixture=fixture, client=client, roots=roots, policy=policy, inputs=inputs,
        calls=[], downloads=[], repository="example/content", code=0,
    )

    def run(argv):
        state.calls.append(argv)
        if argv[1:] == ["--version"]:
            return 0, b"gh version 2.95.0 (fixture)\n", b""
        assert argv[1:3] == ["attestation", "verify"]
        assert Path(argv[argv.index("--bundle") + 1]).read_bytes() == inputs[fixture.proof.name]
        assert Path(argv[argv.index("--custom-trusted-root") + 1]).read_bytes() == roots.read_bytes()
        repository_uri = f"https://github.com/{state.repository}"
        result = {
            "verificationResult": {
                "mediaType": attestation._RESULT_MEDIA_TYPE,
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": attestation._PREDICATE,
                    "subject": [{"name": "producer name", "digest": {
                        "sha256": fixture.source.entry.package.sha256,
                    }}],
                    "predicate": {},
                },
                "signature": {"certificate": {
                    "subjectAlternativeName": repository_uri + "/.github/workflows/sign.yml@refs/heads/main",
                    "issuer": attestation._ISSUER,
                    "sourceRepositoryURI": repository_uri,
                    "sourceRepositoryDigest": "a" * 40,
                    "sourceRepositoryRef": "refs/heads/main",
                    "buildSignerDigest": "a" * 40,
                    "runnerEnvironment": "github-hosted",
                    "buildConfigURI": repository_uri + "/.github/workflows/release.yml@refs/heads/main",
                    "buildConfigDigest": "a" * 40,
                }},
                "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2020-01-01T00:00:00Z"}],
            },
        }
        return state.code, json.dumps([result]).encode(), b"private native diagnostic"

    @contextmanager
    def download(url, expected, *, origins, staging_parent):
        assert url.endswith(f"/releases/assets/{next(a.identifier for a in assets if a.name == expected.name)}")
        assert staging_parent == fixture.cache.root / "staging"
        assert origins == source._ASSET_ORIGINS
        state.downloads.append(expected.name)
        directory = staging_parent / f"download-{len(state.downloads)}"
        make_private_directory(directory)
        path = directory / "asset.bin"
        path.write_bytes(inputs[expected.name])
        path.chmod(0o600)
        try:
            yield path
        finally:
            path.unlink()
            directory.rmdir()

    monkeypatch.setattr(attestation, "resolve_tool_from_path", lambda _: str(executable))
    monkeypatch.setattr(attestation, "_run_gh", run)
    monkeypatch.setattr(source, "download_https_asset", download)
    state.flow = acquisition.GitHubWorkspaceAcquirer(
        fixture.cache, policy_file=policy, trusted_root=roots,
    )
    return state


def test_acquisition_binds_real_verifier_inputs_and_publishes_complete_cache(github):
    selected = github.flow.acquire(github.client)
    assert selected.resolved == github.fixture.source
    assert github.downloads == [WORKSPACE_RELEASE_NAME, "proof.jsonl", "workspace.zip"]
    assert len(github.calls) == 2
    assert github.calls[-1][github.calls[-1].index("--source-digest") + 1] == "a" * 40
    assert github.calls[-1][github.calls[-1].index("--source-ref") + 1] == "refs/heads/main"
    package = github.fixture.cache.root / "objects" / "sha256" / selected.resolved.entry.package.sha256
    assert (package / "package.zip").read_bytes() == github.inputs["workspace.zip"]
    assert (package / "content" / "workspace" / "manifests" / "storage.yaml").is_file()
    receipt = json.loads(next((github.fixture.cache.root / "receipts").rglob("*.json")).read_bytes())
    assert receipt["proofSha256"] == selected.resolved.entry.proof.sha256
    assert receipt["policy"]["sha256"] == hashlib.sha256(github.policy.read_bytes()).hexdigest()
    assert receipt["trustedRootSha256"] == hashlib.sha256(github.roots.read_bytes()).hexdigest()
    assert not list((github.fixture.cache.root / "staging").iterdir())


def test_pinned_lease_never_resolves_source_or_downloads(github, monkeypatch):
    selected = github.flow.acquire(github.client)
    github.fixture.archive.unlink()
    github.fixture.proof.unlink()
    calls_before = len(github.calls)
    github.client.resolve_release.side_effect = AssertionError("Pinned use resolved its source.")
    monkeypatch.setattr(source, "download_https_asset", Mock(side_effect=AssertionError("Pinned download.")))
    with patch("socket.socket", side_effect=AssertionError("Pinned network.")):
        with github.flow.lease(selected.resolved) as content:
            assert content.bind("storage").manifest_path.name == "storage.yaml"
            assert content.verification.proof_sha256 == selected.resolved.entry.proof.sha256
    assert len(github.calls) == calls_before + 2
    assert len(github.downloads) == 3


def test_explicit_resolution_reuses_valid_package_and_proof(github):
    first = github.flow.acquire(github.client)
    second = github.flow.acquire(github.client)
    assert first == second
    assert github.client.resolve_release.call_count == 2
    assert github.downloads == [WORKSPACE_RELEASE_NAME, "proof.jsonl", "workspace.zip", WORKSPACE_RELEASE_NAME]
    assert len(github.calls) == 4


def test_existing_proof_avoids_redundant_transfer(github):
    github.fixture.cache.retain_proof(github.fixture.proof, github.fixture.source.entry.proof)
    github.flow.acquire(github.client)
    assert github.downloads == [WORKSPACE_RELEASE_NAME, "workspace.zip"]


def test_missing_proof_can_be_reacquired_without_downloading_cached_package(github):
    selected = github.flow.acquire(github.client)
    root = github.fixture.cache.root / "proofs" / "sha256" / selected.resolved.entry.proof.sha256
    (root / "proof.bin").unlink()
    root.rmdir()
    with pytest.raises(CacheError) as caught:
        with github.flow.lease(selected.resolved):
            pytest.fail("Missing proof was silently acquired during pinned use.")
    assert caught.value.code == "cache.proof-missing"
    assert len(github.downloads) == 3
    github.flow.acquire(github.client)
    assert github.downloads[3:] == [WORKSPACE_RELEASE_NAME, "proof.jsonl"]


@pytest.mark.parametrize("kind", ["package", "proof"])
def test_corrupt_cached_inputs_fail_without_repair_or_redownload(github, kind):
    selected = github.flow.acquire(github.client)
    if kind == "package":
        path = github.fixture.cache.root / "objects" / "sha256" / selected.resolved.entry.package.sha256 / "package.zip"
    else:
        path = github.fixture.cache.root / "proofs" / "sha256" / selected.resolved.entry.proof.sha256 / "proof.bin"
    path.write_bytes(b"corrupt")
    with pytest.raises(ArtifactError):
        github.flow.acquire(github.client)
    assert github.downloads[3:] == [WORKSPACE_RELEASE_NAME]
    assert path.read_bytes() == b"corrupt"


@pytest.mark.parametrize("change", ["repository", "expired", "root"])
def test_policy_preflight_stops_before_source_resolution(github, change):
    document = json.loads(github.policy.read_bytes())
    if change == "repository":
        document["provider"]["repository"] = "other/content"
    elif change == "expired":
        document["validUntil"] = "2000-01-01T00:00:00Z"
    else:
        document["trustedRootSha256"] = "0" * 64
    github.policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(attestation.VerificationError):
        github.flow.acquire(github.client)
    github.client.resolve_release.assert_not_called()
    assert github.downloads == []
    assert github.calls == []


def test_policy_updates_are_used_locally_without_source_refresh(github):
    selected = github.flow.acquire(github.client)
    document = json.loads(github.policy.read_bytes())
    document["version"] = 2
    github.policy.write_text(json.dumps(document), encoding="utf-8")
    with github.flow.lease(selected.resolved) as content:
        assert content.verification.policy_version == 2
        assert content.verification.policy_sha256 == hashlib.sha256(github.policy.read_bytes()).hexdigest()
    assert len(github.downloads) == 3
    assert github.client.resolve_release.call_count == 1


def test_expired_policy_rejects_pinned_use_without_network(github):
    selected = github.flow.acquire(github.client)
    document = json.loads(github.policy.read_bytes())
    document["validUntil"] = "2000-01-01T00:00:00Z"
    github.policy.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(attestation.VerificationError, match="expired"):
        with github.flow.lease(selected.resolved):
            pytest.fail("Expired policy authorized reuse.")
    assert len(github.calls) == 2
    assert len(github.downloads) == 3


def test_native_verification_failure_stops_publication(github, monkeypatch):
    github.code = 1
    monkeypatch.setattr("siteops.workspace_cache.extract_package", Mock(side_effect=AssertionError("Unverified parse.")))
    with pytest.raises(attestation.VerificationError, match="did not pass"):
        github.flow.acquire(github.client)
    assert not list((github.fixture.cache.root / "objects" / "sha256").iterdir())
    assert not list((github.fixture.cache.root / "staging").iterdir())


def test_repository_policy_switch_cannot_change_source_identity_mid_verification(github, monkeypatch):
    verify = acquisition.verify_github_artifact

    def switch(*args, **kwargs):
        document = json.loads(github.policy.read_bytes())
        document["provider"]["repository"] = "other/content"
        github.policy.write_text(json.dumps(document), encoding="utf-8")
        github.repository = "other/content"
        return verify(*args, **kwargs)

    monkeypatch.setattr(acquisition, "verify_github_artifact", switch)
    with pytest.raises(attestation.VerificationError, match="changed during source verification"):
        github.flow.acquire(github.client)
    assert not list((github.fixture.cache.root / "objects" / "sha256").iterdir())


@pytest.mark.parametrize("changes", [
    {"provider": "other-registry/v1"}, {"reference": "github:other/content"},
    {"revision": "b" * 40},
])
def test_pinned_source_facts_remain_bound_to_policy_and_provenance(github, changes):
    selected = github.flow.acquire(github.client)
    wrong = replace(selected.resolved, source=replace(selected.resolved.source, **changes))
    with pytest.raises(attestation.VerificationError):
        with github.flow.lease(wrong):
            pytest.fail("Changed source facts were authorized.")
    assert len(github.downloads) == 3


def test_cli_auth_is_not_substituted_during_acquisition(github):
    github.client.auth = "cli"
    with pytest.raises(ArtifactError) as caught:
        github.flow.acquire(github.client)
    assert caught.value.code == "source.auth-unsupported"
    assert github.downloads == []
    assert github.calls == []


@pytest.fixture
def project_cli(github, tmp_path, monkeypatch):
    root = tmp_path / "operator"
    (root / "sites").mkdir(parents=True)
    (root / "sites" / "one.yaml").write_text(
        "apiVersion: siteops/v1\nkind: Site\nname: one\nsubscription: operator-sub\n"
        "resourceGroup: operator-group\nlocation: eastus\n", encoding="utf-8",
    )
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(github.fixture.cache.root))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")

    def make_client(reference):
        assert reference == github.client.reference
        return github.client

    monkeypatch.setattr(github_source, "GitHubClient", make_client)
    monkeypatch.setattr(acquisition, "GitHubClient", make_client)

    def runner(argv, timeout):
        assert argv[1:] == ("version", "--output", "json")
        return subprocess.CompletedProcess(argv, 0, stdout='{"azure-cli":"test"}', stderr="")

    compilation = TemplateCompilationSession(
        command_runner=runner, tool_resolver=lambda name: str(tmp_path / f"{name}.exe"),
    )
    monkeypatch.setattr("siteops.orchestrator.TemplateCompilationSession", lambda: compilation)
    monkeypatch.setattr("siteops.executor.subprocess.Popen", Mock(side_effect=AssertionError("Live operation.")))
    original = cli.Orchestrator
    engines = []
    executor = Mock()

    def submit(**args):
        with pytest.raises(CacheError) as caught:
            github.fixture.cache.publish(
                github.fixture.archive, github.fixture.source.entry.package.sha256,
                source_revision=github.fixture.source.source.revision, verify=Mock(),
            )
        assert caught.value.code == "cache.busy"
        return DeploymentResult(
            success=True, step_name=args["step_name"], site_name=args["site_name"],
            deployment_name=args["deployment_name"],
        )

    executor.deploy_resource_group.side_effect = submit

    def engine(**kwargs):
        engines.append(kwargs)
        return original(**kwargs, executor=executor)

    monkeypatch.setattr(cli, "Orchestrator", engine)
    return SimpleNamespace(
        root=root, github=github, engines=engines, executor=executor,
        trust=["--trust-policy", str(github.policy), "--trusted-root", str(github.roots)],
    )


def invoke(monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["siteops", *args])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    return stopped.value.code, capsys.readouterr()


def pin_project(project_cli, monkeypatch, capsys):
    state = project_cli
    code, output = invoke(monkeypatch, capsys, [
        *state.trust, "project", "pin", str(state.root),
        "--source", "github:example/content", "--release", "release-7", "--output", "json",
    ])
    assert code == 0, output
    assert json.loads(output.out)["kind"] == "WorkspacePin"


def test_public_project_pin_and_plan_use_existing_site_configuration(project_cli, monkeypatch, capsys):
    state = project_cli
    site_before = (state.root / "sites" / "one.yaml").read_bytes()
    pin_project(state, monkeypatch, capsys)
    pin_before = (state.root / "siteops.pin").read_bytes()
    downloads = list(state.github.downloads)
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage", "--output", "json",
    ])
    assert code == 0, output
    document = json.loads(output.out)
    assert document["status"] == "planned"
    assert "operator-sub" in output.out
    assert "Package SHA-256:" in output.err
    assert state.engines[-1]["site_config_root"] == state.root
    assert state.engines[-1]["materialized_package"].manifest_relative_path == "manifests/storage.yaml"
    assert state.github.downloads == downloads
    assert (state.root / "siteops.pin").read_bytes() == pin_before
    assert (state.root / "sites" / "one.yaml").read_bytes() == site_before


@pytest.mark.parametrize("command", ["validate", "describe", "dry-run", "deploy"])
def test_public_project_commands_share_binding_and_keep_the_lease(project_cli, monkeypatch, capsys, command):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    forms = {
        "validate": ["validate", "storage"],
        "describe": ["plan", "storage", "--describe", "--output", "json"],
        "dry-run": ["deploy", "storage", "--dry-run", "--output", "json"],
        "deploy": ["deploy", "storage", "--output", "json"],
    }
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, *forms[command],
    ])
    assert code == 0, output
    assert state.engines[-1]["materialized_package"] is not None
    if command == "deploy":
        assert state.executor.deploy_resource_group.call_count == 1
        assert state.executor.deploy_resource_group.call_args.kwargs["subscription"] == "operator-sub"
    else:
        state.executor.deploy_resource_group.assert_not_called()


def test_public_project_browse_does_not_suggest_an_unverified_cache_path(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "browse", "storage",
    ])
    assert code == 0, output
    assert "Package verified" in output.out
    assert "same project" in output.out
    assert str(state.github.fixture.cache.root) not in output.out
    assert "Remote preview only" not in output.out
    assert state.engines == []


def test_project_local_override_preserves_pin_and_operator_sites(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    before = (state.root / "siteops.pin").read_bytes()
    calls = len(state.github.calls)
    local = state.github.fixture.archive.parent / "source" / "workspace"
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), "-w", str(local), "plan", "storage", "--output", "json",
    ])
    assert code == 0, output
    assert "operator-sub" in output.out
    assert "local workspace override" in output.err
    assert "materialized_package" not in state.engines[-1]
    assert state.engines[-1]["site_config_root"] == state.root
    assert (state.root / "siteops.pin").read_bytes() == before
    assert len(state.github.calls) == calls


def test_current_directory_pin_selects_project_without_global_project_flag(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    monkeypatch.chdir(state.root)
    code, output = invoke(monkeypatch, capsys, [*state.trust, "plan", "storage", "--describe"])
    assert code == 0, output
    assert state.engines[-1]["site_config_root"] == state.root


def test_project_sites_and_show_do_not_require_verification(project_cli, monkeypatch, capsys):
    state = project_cli
    code, output = invoke(monkeypatch, capsys, ["--project", str(state.root), "sites", "--output", "json"])
    assert code == 0, output
    assert json.loads(output.out)[0]["subscription"] == "operator-sub"
    assert state.github.calls == [] and state.github.downloads == []
    pin_project(state, monkeypatch, capsys)
    calls = len(state.github.calls)
    code, output = invoke(monkeypatch, capsys, ["project", "show", str(state.root), "--output", "json"])
    assert code == 0
    assert json.loads(output.out)["kind"] == "WorkspacePin"
    assert len(state.github.calls) == calls


def remove_cached_package(state):
    path = (
        state.github.fixture.cache.root / "objects" / "sha256"
        / state.github.fixture.source.entry.package.sha256
    )
    shutil.rmtree(path)


def test_missing_package_restores_exact_pin_but_offline_never_fetches(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    before = (state.root / "siteops.pin").read_bytes()
    remove_cached_package(state)
    downloads = len(state.github.downloads)
    code, _ = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage", "--offline",
    ])
    assert code == 1 and len(state.github.downloads) == downloads
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage", "--describe",
    ])
    assert code == 0, output
    assert state.github.downloads[downloads:] == [WORKSPACE_RELEASE_NAME, "workspace.zip"]
    assert (state.root / "siteops.pin").read_bytes() == before


def test_changed_release_does_not_restore_or_rewrite_the_pin(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    before = (state.root / "siteops.pin").read_bytes()
    remove_cached_package(state)
    row = json.loads(state.github.inputs[WORKSPACE_RELEASE_NAME])
    row["source"]["revision"] = "b" * 40
    descriptor = json.dumps(row).encode()
    state.github.inputs[WORKSPACE_RELEASE_NAME] = descriptor
    release = state.github.client.resolve_release.return_value
    state.github.client.resolve_release.return_value = replace(
        release, source_commit="b" * 40,
        assets=(replace(release.assets[0], size=len(descriptor), sha256=hashlib.sha256(descriptor).hexdigest()),
                *release.assets[1:]),
    )
    downloads = len(state.github.downloads)
    calls = len(state.github.calls)
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage",
    ])
    assert code == 1
    assert "differs from the workspace pin" in output.err
    assert state.github.downloads[downloads:] == [WORKSPACE_RELEASE_NAME]
    assert len(state.github.calls) == calls
    assert (state.root / "siteops.pin").read_bytes() == before
    assert state.engines == []


def test_project_manifest_escape_is_rejected_before_engine_construction(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    code, _ = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "validate", "../outside.yaml",
    ])
    assert code == 1 and state.engines == []


def test_project_validate_guards_inputs_before_the_manifest_parser(project_cli, monkeypatch, capsys):
    from siteops.models import Manifest

    state = project_cli
    pin_project(state, monkeypatch, capsys)
    original = Manifest.from_file
    guarded = []

    def load(path, **kwargs):
        guard = kwargs.get("input_path_guard")
        assert guard is not None
        with pytest.raises(ArtifactError):
            guard(state.root / "sites" / "one.yaml")
        guarded.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(Manifest, "from_file", load)
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "validate", "storage",
    ])
    assert code == 0, output
    assert len(guarded) == 1


def test_expired_project_policy_stops_before_site_or_engine_construction(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    policy = json.loads(state.github.policy.read_bytes())
    policy["validUntil"] = "2000-01-01T00:00:00Z"
    state.github.policy.write_text(json.dumps(policy))
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage",
    ])
    assert code == 1 and "expired" in output.err
    assert state.engines == []


def test_project_publishable_plan_keeps_private_source_context_out_of_output(project_cli, monkeypatch, capsys):
    state = project_cli
    pin_project(state, monkeypatch, capsys)
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    code, output = invoke(monkeypatch, capsys, [
        "--project", str(state.root), *state.trust, "plan", "storage",
        "--output", "json", "--projection", "publishable",
    ])
    assert code == 0, output
    assert json.loads(output.out)["status"] == "planned"
    assert "example/content" not in output.out + output.err
    assert str(state.root) not in output.out + output.err
    assert "operator-sub" not in output.out + output.err


def test_project_pin_output_redaction_precedes_source_access(project_cli, monkeypatch, capsys):
    state = project_cli
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    code, output = invoke(monkeypatch, capsys, [
        *state.trust, "project", "pin", str(state.root),
        "--source", "github:example/content", "--release", "release-7",
    ])
    assert code == 1 and not output.out
    assert state.github.calls == [] and state.github.downloads == []
    assert not (state.root / "siteops.pin").exists()


def test_project_pin_release_workspace_option_selects_the_declared_workspace(project_cli, monkeypatch, capsys):
    state = project_cli
    code, output = invoke(monkeypatch, capsys, [
        *state.trust, "project", "pin", str(state.root),
        "--source", "github:example/content", "--release", "release-7",
        "--release-workspace", "workspace", "--output", "json",
    ])
    assert code == 0, output
    assert json.loads(output.out)["content"]["workspace"] == "workspace"


def test_project_pin_creates_a_new_project_outside_the_cache(project_cli, tmp_path, monkeypatch, capsys):
    state = project_cli
    state.root = tmp_path / "new-project"
    assert not state.root.exists()
    pin_project(state, monkeypatch, capsys)
    assert (state.root / "siteops.pin").is_file()

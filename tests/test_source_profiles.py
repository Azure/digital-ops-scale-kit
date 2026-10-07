"""Consumer trust enrollment is independent of a project pin or workspace package."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from siteops import cli
from siteops.cache_filesystem import CacheError
from siteops.command_context import require_trust_inputs
from siteops.project import ProjectError
from siteops.source_profiles import (
    SourceProfileError,
    enroll_source,
    list_sources,
    read_source,
    remove_source,
)
from tests.workspace_acquisition_helpers import make_source

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    import siteops.source_profiles as profiles

    storage = tmp_path / "private" / "sources"
    monkeypatch.setattr(profiles, "source_root", lambda: storage)
    root = b'{"trusted":true}\n'
    root_file = tmp_path / "independent-root.jsonl"
    root_file.write_bytes(root)
    policy = {
        "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
        "id": "approved-release", "version": 1,
        "validUntil": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "trustedRootSha256": hashlib.sha256(root).hexdigest(),
        "provider": {
            "kind": "github-attestation/v1",
            "repository": "example/content", "sourceRef": "refs/heads/main",
            "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
            "builderWorkflow": ".github/workflows/release.yaml",
            "runnerEnvironment": "self-hosted",
        },
    }
    policy_file = tmp_path / "independent-policy.json"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    return storage, policy_file, root_file, policy


def test_enrollment_requires_explicit_files_and_never_changes_existing_source(inputs, tmp_path):
    storage, policy_file, root_file, _ = inputs
    enrolled = enroll_source("approved", "github:example/content", policy_file, root_file)
    assert enrolled.reference == "github:example/content"
    assert list_sources() == ("approved",)
    assert read_source("approved") == enrolled
    assert enrolled.policy.read_bytes() == policy_file.read_bytes()
    assert enrolled.trusted_root.read_bytes() == root_file.read_bytes()
    assert enroll_source("approved", "github:example/content", policy_file, root_file) == enrolled
    assert read_source("approved") == enrolled
    cache = tmp_path / "different-cache"
    assert require_trust_inputs(
        cache, None, None, approved_source="approved", source_reference="github:example/content",
    ) == (enrolled.policy, enrolled.trusted_root)
    with pytest.raises(ProjectError, match="does not match"):
        require_trust_inputs(
            cache, None, None, approved_source="approved", source_reference="github:other/content",
        )
    with pytest.raises(ProjectError, match="Choose"):
        require_trust_inputs(
            cache, policy_file, root_file, approved_source="approved",
            source_reference="github:example/content",
        )
    remove_source("approved")
    assert list_sources() == ()
    assert not (storage / "approved").exists()


@pytest.mark.parametrize("parent_mode", [0o755, 0o750])
def test_enrollment_preserves_safe_existing_configuration_ancestors(
    inputs, tmp_path, monkeypatch, parent_mode,
):
    import siteops.source_profiles as profiles

    _, policy_file, root_file, _ = inputs
    configuration = tmp_path / "configuration"
    configuration.mkdir(mode=parent_mode)
    if os.name != "nt":
        configuration.chmod(parent_mode)
    monkeypatch.setattr(
        profiles, "source_root", lambda: configuration / "siteops" / "sources",
    )
    check_private = profiles.check_private_node

    def check_new_private_node(path, *, directory):
        assert path != configuration, "An existing safe parent is not a private Site Ops node."
        check_private(path, directory=directory)

    monkeypatch.setattr(profiles, "check_private_node", check_new_private_node)
    enrolled = enroll_source("approved", "github:example/content", policy_file, root_file)
    assert read_source("approved") == enrolled
    assert configuration.is_dir()
    if os.name != "nt":
        assert configuration.stat().st_mode & 0o777 == parent_mode
    assert enrolled.directory.parent.stat().st_mode & 0o777 == 0o700 or os.name == "nt"


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent mode is checked on Linux.")
def test_enrollment_rejects_writable_existing_configuration_parent(inputs, tmp_path, monkeypatch):
    import siteops.source_profiles as profiles

    _, policy_file, root_file, _ = inputs
    configuration = tmp_path / "configuration"
    configuration.mkdir()
    configuration.chmod(0o777)
    monkeypatch.setattr(
        profiles, "source_root", lambda: configuration / "siteops" / "sources",
    )
    with pytest.raises(CacheError, match="prevent changes by other users"):
        enroll_source("approved", "github:example/content", policy_file, root_file)
    assert not (configuration / "siteops").exists()


@pytest.mark.parametrize("change", ["trusted-root", "policy-id", "policy-version"])
def test_reenrollment_refuses_changed_stable_trust(
    inputs, change,
):
    _, policy_file, root_file, policy = inputs
    existing = enroll_source("approved", "github:example/content", policy_file, root_file)
    original_policy = existing.policy.read_bytes()
    original_root = existing.trusted_root.read_bytes()
    if change == "trusted-root":
        root_file.write_bytes(b'{"trusted":"replacement"}\n')
        policy["trustedRootSha256"] = hashlib.sha256(root_file.read_bytes()).hexdigest()
    elif change == "policy-id":
        policy["id"] = "replacement"
    else:
        policy["version"] = 2
    policy_file.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(SourceProfileError, match="differs.*Remove"):
        enroll_source("approved", "github:example/content", policy_file, root_file)
    assert existing.policy.read_bytes() == original_policy
    assert existing.trusted_root.read_bytes() == original_root
    assert read_source("approved") == existing


def test_reenrollment_keeps_existing_expiry_when_bootstrap_regenerates_policy(inputs):
    _, policy_file, root_file, policy = inputs
    existing = enroll_source("approved", "github:example/content", policy_file, root_file)
    original_policy = existing.policy.read_bytes()
    policy["validUntil"] = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    assert enroll_source("approved", "github:example/content", policy_file, root_file) == existing
    assert existing.policy.read_bytes() == original_policy


def test_reenrollment_cannot_silently_keep_a_longer_expiry(inputs):
    _, policy_file, root_file, policy = inputs
    existing = enroll_source("approved", "github:example/content", policy_file, root_file)
    original_policy = existing.policy.read_bytes()
    policy["validUntil"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(SourceProfileError, match="differs.*Remove"):
        enroll_source("approved", "github:example/content", policy_file, root_file)
    assert existing.policy.read_bytes() == original_policy


@pytest.mark.parametrize("fault", ["wrong-source", "wrong-root", "expired", "named-release"])
def test_untrusted_enrollment_fails_before_a_profile_is_created(inputs, fault):
    storage, policy_file, root_file, policy = inputs
    source = "github:example/content"
    if fault == "wrong-source":
        source = "github:other/content"
    elif fault == "wrong-root":
        root_file.write_bytes(b"changed root")
    elif fault == "expired":
        policy["validUntil"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        policy_file.write_text(json.dumps(policy), encoding="utf-8")
    else:
        source += "@v1"
    with pytest.raises(SourceProfileError):
        enroll_source("approved", source, policy_file, root_file)
    assert not storage.exists()


def test_expired_enrollment_can_be_inspected_and_removed(inputs):
    _, policy_file, root_file, policy = inputs
    enroll_source("approved", "github:example/content", policy_file, root_file)
    policy["validUntil"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    updated = json.dumps(policy).encode("utf-8")
    selected = read_source("approved")
    selected.policy.write_bytes(updated)
    record = selected.document()
    record["policySha256"] = hashlib.sha256(updated).hexdigest()
    (selected.directory / "source.json").write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(SourceProfileError, match="expired") as error:
        read_source("approved")
    assert error.value.code == "source.profile-expired"
    assert "source show NAME" in str(error.value)
    assert "remove" in str(error.value) and "enroll" in str(error.value)
    assert selected.policy.read_bytes() == updated
    assert read_source("approved", require_valid=False).name == "approved"
    assert list_sources() == ("approved",)
    remove_source("approved")


def test_unexpected_files_block_removal_without_deleting_operator_data(inputs):
    _, policy_file, root_file, _ = inputs
    selected = enroll_source("approved", "github:example/content", policy_file, root_file)
    extra = selected.directory / "operator.txt"
    extra.write_text("keep", encoding="utf-8")
    with pytest.raises(SourceProfileError, match="other files"):
        remove_source("approved")
    assert extra.read_text(encoding="utf-8") == "keep"
    assert selected.policy.exists()


def test_corrupted_profile_can_be_removed_without_touching_unexpected_files(inputs):
    _, policy_file, root_file, _ = inputs
    selected = enroll_source("approved", "github:example/content", policy_file, root_file)
    selected.policy.write_bytes(b"corrupt policy")
    with pytest.raises(SourceProfileError):
        read_source("approved")
    remove_source("approved")
    assert not selected.directory.exists()


def test_interrupted_enrollment_cleans_only_files_created_by_that_attempt(inputs, monkeypatch):
    import siteops.source_profiles as profiles

    storage, policy_file, root_file, _ = inputs
    original = profiles.write_new
    calls = 0

    def fail_after_first_file(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("controlled write failure")
        original(path, data)

    monkeypatch.setattr(profiles, "write_new", fail_after_first_file)
    with pytest.raises(SourceProfileError, match="could not be recorded"):
        enroll_source("approved", "github:example/content", policy_file, root_file)
    assert not (storage / "approved").exists()
    assert policy_file.exists() and root_file.exists()


def test_enrolling_a_different_publisher_policy_cannot_replace_an_existing_source(inputs):
    _, policy_file, root_file, policy = inputs
    selected = enroll_source("approved", "github:example/content", policy_file, root_file)
    previous = selected.policy.read_bytes()
    policy["provider"]["sourceRef"] = "refs/heads/other"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(SourceProfileError, match="differs"):
        enroll_source("approved", "github:example/content", policy_file, root_file)
    assert selected.policy.read_bytes() == previous


def test_cli_help_keeps_approved_source_explicit():
    env = {**os.environ, "SITEOPS_REDACT_OUTPUT": "0"}
    result = subprocess.run(
        [sys.executable, "-m", "siteops", "--help"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--approved-source" in result.stdout
    assert "source" in result.stdout


def test_cli_enrolls_inspects_and_refuses_a_different_project_source(tmp_path):
    root = b'{"trusted":true}\n'
    policy = {
        "apiVersion": "siteops/v1alpha1", "kind": "ArtifactVerificationPolicy",
        "id": "approved-content", "version": 1,
        "validUntil": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "trustedRootSha256": hashlib.sha256(root).hexdigest(),
        "provider": {
            "kind": "github-attestation/v1", "repository": "example/content",
            "sourceRef": "refs/heads/main",
            "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
            "builderWorkflow": ".github/workflows/release.yaml",
            "runnerEnvironment": "self-hosted",
        },
    }
    policy_file, root_file = tmp_path / "policy.json", tmp_path / "root.jsonl"
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    root_file.write_bytes(root)
    env = {
        **os.environ, "SITEOPS_REDACT_OUTPUT": "0",
        "SITEOPS_CACHE_DIR": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "LOCALAPPDATA": str(tmp_path / "config"),
    }

    def invoke(*arguments):
        return subprocess.run(
            [sys.executable, "-m", "siteops", *map(str, arguments)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20,
        )

    enrolled = invoke(
        "--trust-policy", policy_file, "--trusted-root", root_file,
        "source", "enroll", "approved", "--source", "github:example/content",
    )
    assert enrolled.returncode == 0, enrolled.stderr
    shown = invoke("source", "show", "approved").stdout
    assert "github:example/content" in shown
    assert "Signing workflow: .github/workflows/_workspace-distribution.yaml" in shown
    assert "Runner: self-hosted" in shown and "Valid until:" in shown
    assert invoke("source", "list").stdout.strip() == "approved"
    mismatch = invoke(
        "--approved-source", "approved", "project", "pin", tmp_path / "factory",
        "--source", "github:other/content", "--release", "v1",
    )
    assert mismatch.returncode != 0
    assert "does not match" in mismatch.stderr
    assert not (tmp_path / "factory").exists()
    assert invoke("source", "remove", "approved").returncode == 0
    assert invoke("source", "list").stdout.strip() == ""


def test_project_pin_uses_the_explicit_approved_source_without_network(
    inputs, tmp_path, monkeypatch, capsys,
):
    _, policy_file, root_file, _ = inputs
    enroll_source("approved", "github:example/content", policy_file, root_file)
    selected = make_source(
        tmp_path, provider="github-release/v1", reference="github:example/content",
    ).source
    import siteops.github_workspace_acquisition as acquisition

    fake = SimpleNamespace(acquire=lambda *_, **__: SimpleNamespace(resolved=selected))
    monkeypatch.setattr(acquisition, "GitHubWorkspaceAcquirer", lambda *_, **__: fake)
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(tmp_path / "other-cache"))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    project = tmp_path / "factory"
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--approved-source", "approved", "project", "pin",
        str(project), "--release", "release-7",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0, capsys.readouterr().err
    from siteops.project import read_pin

    assert read_pin(project).pin.selection == selected
    assert "Source: github:example/content" in capsys.readouterr().out


def test_source_enroll_does_not_copy_approval_from_the_package_cache(
    inputs, tmp_path, monkeypatch, capsys,
):
    storage, policy_file, root_file, _ = inputs
    cache = tmp_path / "package-cache"
    cache.mkdir()
    inside = cache / "policy.json"
    inside.write_bytes(policy_file.read_bytes())
    monkeypatch.setenv("SITEOPS_CACHE_DIR", str(cache))
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "0")
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(inside), "--trusted-root", str(root_file),
        "source", "enroll", "approved", "--source", "github:example/content",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "outside the content cache" in capsys.readouterr().err
    assert not storage.exists()


@pytest.mark.parametrize("redacted", [False, True])
def test_source_enroll_and_remove_have_private_safe_success(
    inputs, monkeypatch, capsys, redacted,
):
    storage, policy_file, root_file, _ = inputs
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1" if redacted else "0")
    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(policy_file), "--trusted-root", str(root_file),
        "source", "enroll", "private-name", "--source", "github:example/content",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 0, output.err
    assert read_source("private-name").reference == "github:example/content"
    if redacted:
        assert output.out.strip() == "Approved source enrolled."
        assert "private-name" not in output.out + output.err
        assert "example/content" not in output.out + output.err
    else:
        assert "private-name" in output.out and "github:example/content" in output.out

    monkeypatch.setattr(sys, "argv", ["siteops", "source", "remove", "private-name"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 0, output.err
    assert not (storage / "private-name").exists()
    assert "private-name" not in output.out if redacted else "private-name" in output.out


def test_redacted_source_inspection_and_invalid_enrollment_are_safe(
    inputs, monkeypatch, capsys,
):
    storage, policy_file, root_file, _ = inputs
    monkeypatch.setenv("SITEOPS_REDACT_OUTPUT", "1")
    for command in (("list",), ("show", "private-name")):
        monkeypatch.setattr(sys, "argv", ["siteops", "source", *command])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        output = capsys.readouterr()
        assert stopped.value.code == 1
        assert "private-name" not in output.out + output.err

    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(policy_file), "--trusted-root", str(root_file),
        "source", "enroll", "private-name", "--source", "github:private-rejected/content",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1
    assert "private-name" not in output.out + output.err
    assert "private-rejected" not in output.out + output.err
    assert not storage.exists()

def _standard_root(monkeypatch, *roots: bytes) -> None:
    import siteops.source_profiles as profiles

    supplied = iter(roots)
    monkeypatch.setattr(profiles, "fetch_trusted_root", lambda: next(supplied))


def test_standard_enrollment_approves_main_branch_releases_and_renews(inputs, monkeypatch):
    from siteops.source_profiles import OFFICIAL_SOURCE, enroll_standard_source

    _standard_root(monkeypatch, b'{"root":1}\n', b'{"root":2}\n')
    first = enroll_standard_source("core")
    assert first.reference == OFFICIAL_SOURCE == "github:Azure/digital-ops-scale-kit"
    policy = json.loads(first.policy.read_text(encoding="utf-8"))
    assert policy["provider"] == {
        "kind": "github-attestation/v1", "repository": "Azure/digital-ops-scale-kit",
        "sourceRef": "refs/heads/main",
        "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
        "builderWorkflow": ".github/workflows/release.yaml", "runnerEnvironment": "self-hosted",
    }
    remaining = datetime.fromisoformat(policy["validUntil"]) - datetime.now(timezone.utc)
    assert timedelta(days=29) < remaining <= timedelta(days=30)
    assert first.trusted_root.read_bytes() == b'{"root":1}\n'

    renewed = enroll_standard_source("core")
    assert renewed.trusted_root.read_bytes() == b'{"root":2}\n'
    assert renewed.root_sha256 != first.root_sha256
    assert list_sources() == ("core",)


def test_standard_enrollment_never_replaces_different_trust(inputs, monkeypatch):
    from siteops.source_profiles import enroll_standard_source

    _, policy_file, root_file, _ = inputs
    enroll_source("approved", "github:example/content", policy_file, root_file)
    _standard_root(monkeypatch, b'{"root":1}\n', b'{"root":1}\n')
    for source in ("github:example/content", "github:other/content"):
        with pytest.raises(SourceProfileError, match="differs"):
            enroll_standard_source("approved", source)
    assert read_source("approved").policy.read_bytes() == policy_file.read_bytes()


def test_source_enroll_defaults_to_the_official_publisher_under_any_name(inputs, monkeypatch, capsys):
    storage, policy_file, _, _ = inputs
    _standard_root(monkeypatch, b'{"root":1}\n')
    monkeypatch.setattr(sys, "argv", ["siteops", "source", "enroll", "core"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    output = capsys.readouterr()
    assert stopped.value.code == 0, output.err
    assert "Approved source core: github:Azure/digital-ops-scale-kit." in output.out
    assert "Valid until:" in output.out
    assert (storage / "core").is_dir()

    monkeypatch.setattr(sys, "argv", [
        "siteops", "--trust-policy", str(policy_file), "source", "enroll", "other",
    ])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert "Supply both --trust-policy and --trusted-root" in capsys.readouterr().err
    assert not (storage / "other").exists()

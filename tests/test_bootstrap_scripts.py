"""Check the two platform entry scripts without contacting package or cloud services."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.bootstrap_discovery_helpers import (
    COMMIT,
    ENGINE_COMMIT,
    expected_arguments,
    observation,
    reference,
    run_bootstrap,
)
from tests.shell_helpers import bash_path, required_bash
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "bootstrap"
SOURCE_SHA = "c" * 40


@pytest.fixture(params=["bash", "powershell"])
def discovery_shell(request):
    if request.param == "powershell" and sys.platform != "win32":
        pytest.skip("The Windows lane runs native PowerShell 5.1.")
    return request.param


@pytest.mark.parametrize("content", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_optional_commit_discovers_then_pins_the_production_flow(tmp_path, discovery_shell, content, local):
    replies = [{"observations": [observation(content=content)], "exit": 0}] * 2
    if content:
        replies += [{"observations": [observation(engine=True)], "exit": 0}]
    result, calls, _ = run_bootstrap(tmp_path, discovery_shell, replies, content=content, local=local)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PINNED_FLOW_REACHED" in result.stdout
    assert f"Discovered source commit: {COMMIT}." in result.stdout
    assert len(calls) == (3 if content else 2)
    assert calls[0] == expected_arguments(calls[0], discovery_shell, pinned=False, content=content)
    assert calls[1] == expected_arguments(calls[1], discovery_shell, pinned=True, content=content)
    if content:
        assert calls[2] == expected_arguments(calls[2], discovery_shell, pinned=True, engine=True)
        assert f"({ENGINE_COMMIT})" in result.stdout


@pytest.mark.parametrize("content", [False, True])
def test_explicit_commit_skips_discovery_and_preserves_pinned_arguments(tmp_path, discovery_shell, content):
    replies = [{"observations": [observation(content=content)], "exit": 0}]
    if content:
        replies += [{"observations": [observation(engine=True)], "exit": 0}]
    result, calls, _ = run_bootstrap(tmp_path, discovery_shell, replies, explicit=True, content=content, local=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PINNED_FLOW_REACHED" in result.stdout
    assert "Discovered source commit:" not in result.stdout
    assert len(calls) == (2 if content else 1)
    assert calls[0] == expected_arguments(calls[0], discovery_shell, pinned=True, content=content)
    if content:
        assert calls[1] == expected_arguments(calls[1], discovery_shell, pinned=True, engine=True)


@pytest.mark.parametrize("fault", [
    "sourceRepositoryRef", "buildConfigURI", "subjectAlternativeName", "sourceRepositoryURI",
    "issuer", "runnerEnvironment", "buildSignerDigest", "buildConfigDigest",
    "missing-digest", "null-digest", "number-digest", "short-digest", "newline-digest",
    "extra-result", "empty-results", "too-many-results", "wrong-media", "gh-failure", "malformed-output", "object-output",
])
def test_discovery_refuses_untrusted_or_ambiguous_verified_evidence(tmp_path, discovery_shell, fault):
    item = observation()
    certificate = item["verificationResult"]["signature"]["certificate"]
    replies = [{"observations": [item], "exit": 0}]
    if fault in certificate:
        certificate[fault] = "PRIVATE_WRONG"
    elif fault == "missing-digest":
        del certificate["sourceRepositoryDigest"]
    elif fault == "null-digest":
        certificate["sourceRepositoryDigest"] = None
    elif fault == "number-digest":
        certificate["sourceRepositoryDigest"] = 123
    elif fault == "short-digest":
        for key in ("sourceRepositoryDigest", "buildSignerDigest", "buildConfigDigest"):
            certificate[key] = "c" * 39
    elif fault == "newline-digest":
        certificate["sourceRepositoryDigest"] = COMMIT + "\nPRIVATE_WRONG"
    elif fault == "extra-result":
        other = observation(engine=True)
        replies[0]["observations"].append(other)
    elif fault == "empty-results":
        replies[0]["observations"] = []
    elif fault == "too-many-results":
        replies[0]["observations"] *= 129
    elif fault == "wrong-media":
        item["verificationResult"]["mediaType"] = "PRIVATE_WRONG"
    elif fault == "gh-failure":
        replies[0]["exit"] = 1
    elif fault == "malformed-output":
        replies[0]["raw"] = '{"PRIVATE_WRONG":broken'
    elif fault == "object-output":
        # An object must not supply its own result count.
        item["Count"] = 1
        replies[0]["raw"] = json.dumps(item)
    else:
        pytest.fail("The discovery fault is not implemented.")
    result, calls, _ = run_bootstrap(tmp_path, discovery_shell, replies, dry_run=True)
    assert result.returncode != 0
    assert len(calls) == 1
    message = "The source discovery provenance could not be verified." if fault == "gh-failure" else (
        "The source commit could not be discovered from verified evidence."
    )
    assert message in " ".join((result.stdout + result.stderr).split())
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr
    assert "Discovered source commit:" not in result.stdout
    assert "PINNED_FLOW_REACHED" not in result.stdout


@pytest.mark.parametrize("content", [False, True])
def test_discovered_commit_must_survive_the_second_pinned_verification(tmp_path, discovery_shell, content):
    item = observation(content=content)
    disagreement = observation(content=content)
    disagreement["verificationResult"]["signature"]["certificate"]["sourceRepositoryDigest"] = "d" * 40
    result, calls, _ = run_bootstrap(
        tmp_path, discovery_shell,
        [{"observations": [item], "exit": 0}, {"observations": [disagreement], "exit": 0}],
        content=content,
    )
    assert result.returncode != 0
    assert len(calls) == 2
    assert calls[1] == expected_arguments(calls[1], discovery_shell, pinned=True, content=content)
    assert "PINNED_FLOW_REACHED" not in result.stdout


@pytest.mark.parametrize("content", [False, True])
def test_normal_discovery_ignores_staged_evidence_and_downloads_the_selected_release(tmp_path, discovery_shell, content):
    replies = [{"observations": [observation(content=content)], "exit": 0}] * 2
    if content:
        replies += [{"observations": [observation(engine=True)], "exit": 0}]
    result, calls, writes = run_bootstrap(
        tmp_path, discovery_shell, replies, content=content,
        real_admission=discovery_shell == "powershell" or sys.platform == "linux",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PINNED_FLOW_REACHED" in result.stdout
    subject = "siteops-engine.json" if content else "siteops-install.zip"
    local_directory = tmp_path / "source" if discovery_shell == "powershell" or sys.platform == "linux" else tmp_path
    local_subject = str(local_directory / subject) if discovery_shell == "powershell" else bash_path(local_directory / subject)
    assert all(call[2] != local_subject for call in calls)
    assert {
        (local_directory / subject).relative_to(tmp_path),
        (local_directory / (subject + ".attestation.jsonl")).relative_to(tmp_path),
    }.isdisjoint(writes)
    assert len(calls) == (3 if content else 2)
    base = "https://github.com/example/publisher/releases/download/v1.0.0b7/"
    downloads = (tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()
    expected = [base + subject, base + subject + ".attestation.jsonl"]
    if content:
        engine_base = "https://github.com/example/publisher/releases/download/siteops%2Fv1.0.0b1/"
        expected += [engine_base + "siteops-install.zip", engine_base + "siteops-install.zip.attestation.jsonl"]
    assert downloads == expected


def test_normal_content_discovery_ignores_a_superseded_signed_reference(tmp_path, discovery_shell):
    stale = reference()
    stale["engine"].update(release="siteops/v0.9.0", revision="b" * 40, version="0.9.0")
    result, calls, _ = run_bootstrap(
        tmp_path, discovery_shell, [{"observations": [observation(content=True)], "exit": 0}] * 2 +
        [{"observations": [observation(engine=True)], "exit": 0}], content=True, local_record=stale,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Selected engine: 1.0.0b1 from siteops/v1.0.0b1" in result.stdout
    assert "0.9.0" not in result.stdout
    assert len(calls) == 3
    assert calls[2][calls[2].index("--source-digest") + 1] == ENGINE_COMMIT
    downloads = (tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()
    assert downloads[0].endswith("/v1.0.0b7/siteops-engine.json")
    assert downloads[2].endswith("/siteops%2Fv1.0.0b1/siteops-install.zip")
    assert json.loads((tmp_path / "siteops-engine.json").read_bytes()) == stale


@pytest.mark.parametrize("explicit", [False, True])
def test_signed_content_release_binding_must_match_the_requested_tag(tmp_path, discovery_shell, explicit):
    record = reference()
    record["release"] = "v9.9.9"
    replies = [{"observations": [observation(content=True)], "exit": 0}] * (1 if explicit else 2)
    result, calls, _ = run_bootstrap(
        tmp_path, discovery_shell, replies, content=True, explicit=explicit, record=record,
    )
    assert result.returncode != 0
    assert len(calls) == (1 if explicit else 2)
    assert "PINNED_FLOW_REACHED" not in result.stdout
    assert "Selected engine:" not in result.stdout


@pytest.mark.parametrize("content", [False, True])
def test_discovery_preview_reports_pinned_commit_without_writes(tmp_path, discovery_shell, content):
    replies = [{"observations": [observation(content=content)], "exit": 0}] * 2
    result, calls, writes = run_bootstrap(
        tmp_path, discovery_shell, replies, dry_run=True, content=content,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Discovered source commit: {COMMIT}." in result.stdout
    assert "Preview only." in result.stdout
    assert len(calls) == 2
    assert not writes


def test_discovery_preview_refuses_missing_local_evidence_without_writes(tmp_path, discovery_shell):
    result, calls, writes = run_bootstrap(tmp_path, discovery_shell, [], dry_run=True, local=False)
    assert result.returncode != 0
    assert not calls and not writes
    assert "Source discovery in preview requires the release asset and proof beside the bootstrap script." in (
        " ".join((result.stdout + result.stderr).split())
    )


def test_bash_discovery_template_reads_only_verified_certificate_fields():
    source = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    template = re.search(r"^  template='(.*)'$", source, re.MULTILINE).group(1)
    assert "{{range .}}" in template
    assert "{{with .verificationResult.signature.certificate}}" in template
    assert re.findall(r" \.([A-Za-z]+)", template) == [
        "verificationResult", "verificationResult", "subjectAlternativeName", "issuer",
        "sourceRepositoryURI", "sourceRepositoryDigest", "sourceRepositoryRef", "buildSignerDigest",
        "buildConfigURI", "buildConfigDigest", "runnerEnvironment",
    ]
    assert '"%q"' in template and '"%q\\t%q' in template


@pytest.mark.skipif(sys.platform != "win32", reason="Ordinal comparisons use native Windows PowerShell 5.1.")
@pytest.mark.parametrize("mode", ["discovery", "pinned"])
@pytest.mark.parametrize("suffix", ["", "\u0000", "\u00ad", "\u200b"], ids=["exact", "nul", "soft-hyphen", "zero-width"])
@pytest.mark.parametrize("field", [
    "subjectAlternativeName", "issuer", "sourceRepositoryURI", "sourceRepositoryDigest",
    "sourceRepositoryRef", "buildSignerDigest", "buildConfigURI", "buildConfigDigest", "runnerEnvironment", "mediaType",
])
def test_windows_verified_fields_require_ordinal_equality(tmp_path, mode, suffix, field):
    item = observation()
    target = item["verificationResult"] if field == "mediaType" else item["verificationResult"]["signature"]["certificate"]
    target[field] += suffix
    # Preserve JSON escapes until the production ConvertFrom-Json decodes them.
    reply = {"raw": json.dumps([item]), "exit": 0}
    replies = [reply, {"observations": [observation()], "exit": 0}] if mode == "discovery" else [reply]
    result, calls, _ = run_bootstrap(
        tmp_path, "powershell", replies, explicit=mode == "pinned", dry_run=mode == "discovery",
    )
    assert (result.returncode == 0) is (suffix == ""), result.stdout + result.stderr
    assert len(calls) == (2 if mode == "discovery" and suffix == "" else 1)
    if suffix:
        message = "The source commit could not be discovered from verified evidence." if mode == "discovery" else (
            "The verified observation format is unsupported." if field == "mediaType" else
            "The verified certificate does not match the selected release."
        )
        assert message in " ".join((result.stdout + result.stderr).split())
        assert "Discovered source commit:" not in result.stdout
        assert "PINNED_FLOW_REACHED" not in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Engine reference comparisons use native Windows PowerShell 5.1.")
@pytest.mark.parametrize("suffix", ["", "\u0000", "\u00ad", "\u200b"], ids=["exact", "nul", "soft-hyphen", "zero-width"])
@pytest.mark.parametrize("field", ["apiVersion", "kind", "release", "revision", "bundle-name"])
def test_windows_engine_reference_fields_require_ordinal_equality(tmp_path, suffix, field):
    record = reference()
    if field == "bundle-name":
        record["engine"]["bundle"]["name"] += suffix
    else:
        record[field] += suffix
    replies = [{"observations": [observation(content=True)], "exit": 0},
               {"observations": [observation(engine=True)], "exit": 0}]
    result, _, _ = run_bootstrap(tmp_path, "powershell", replies, content=True, explicit=True, record=record)
    assert (result.returncode == 0) is (suffix == ""), result.stdout + result.stderr
    assert ("PINNED_FLOW_REACHED" in result.stdout) is (suffix == "")


def test_windows_provenance_and_reference_checks_do_not_use_cultural_equality():
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for name in ("Verify-ReleaseAsset", "Find-SourceCommit", "Read-EngineReference"):
        body = re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source).group()
        assert not re.search(r"\s-c(?:eq|ne)\s", body)
        assert "[StringComparison]::Ordinal" in body


@pytest.mark.parametrize("edition", ["windows-51", "core-7"])
@pytest.mark.parametrize("count", [1, 2])
def test_native_powershell_discovery_accepts_one_and_two_verified_results(tmp_path, edition, count):
    if sys.platform != "win32":
        pytest.skip("Native Windows PowerShell compatibility requires Windows.")
    program = shutil.which("powershell.exe" if edition == "windows-51" else "pwsh.exe")
    if program is None:
        pytest.skip("The requested native PowerShell edition is unavailable.")
    raw = json.dumps([observation()] * count)
    replies = [{"raw": raw, "exit": 0}] * 2
    result, calls, writes = run_bootstrap(
        tmp_path, "powershell", replies, dry_run=True, real_admission=True, powershell=program,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Discovered source commit: {COMMIT}." in result.stdout
    assert len(calls) == 2 and not writes
    assert calls[0] == expected_arguments(calls[0], "powershell", pinned=False)
    assert calls[1] == expected_arguments(calls[1], "powershell", pinned=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Real POSIX ownership and modes require the Linux lane.")
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(("case", "staged"), [
    (case, staged) for case in ("private", "group-writable", "other-writable", "shared-sticky", "piped")
    for staged in (False, True)
] + [(case, True) for case in ("foreign-owner", "symlink", "dangling-asset", "dangling-proof", "subject-only", "proof-only")])
def test_linux_local_discovery_uses_real_file_and_directory_admission(tmp_path, case, staged, dry_run):
    owned_asset = None
    original_owner = None

    def setup(directory, script, asset):
        nonlocal owned_asset, original_owner
        if case == "group-writable":
            # Choose a group other than the admitted user private group.
            groups = [group for group in os.getgroups() if group != os.getgid()]
            group = groups[0] if groups else os.getgid() + 1
            try:
                os.chown(directory, -1, group)
            except PermissionError:
                pytest.skip("This account cannot assign a distinct fixture group.")
            directory.chmod(0o770)
        elif case == "other-writable":
            directory.chmod(0o707)
        elif case == "shared-sticky":
            # A shared sticky directory within the fixture models the system temporary root.
            directory.chmod(0o1777)
        elif case == "foreign-owner":
            original_owner = asset.stat().st_uid, asset.stat().st_gid
            try:
                os.chown(asset, os.getuid() + 1, -1)
            except PermissionError:
                pytest.skip("This account cannot assign another fixture owner.")
            owned_asset = asset
        elif case == "symlink":
            target = directory / "foreign.zip"
            asset.rename(target)
            asset.symlink_to(target)
        elif case in {"dangling-asset", "dangling-proof"}:
            asset.unlink()
            proof = directory / (asset.name + ".attestation.jsonl")
            proof.unlink()
            (asset if case == "dangling-asset" else proof).symlink_to(directory / "absent")
        elif case == "subject-only":
            (directory / (asset.name + ".attestation.jsonl")).unlink()
        elif case == "proof-only":
            asset.unlink()

    try:
        result, calls, writes = run_bootstrap(
            tmp_path, "bash", [{"observations": [observation()], "exit": 0}] * 2,
            dry_run=dry_run, local=staged, real_admission=True, local_setup=setup, piped=case == "piped",
        )
    finally:
        if owned_asset:
            os.chown(owned_asset, *original_owner)
    no_local = not staged or case == "piped"
    message = "The source discovery asset or proof is unavailable or oversized." if case in (
        "foreign-owner", "symlink", "dangling-asset", "dangling-proof", "subject-only", "proof-only",
    ) else "The bootstrap script must use a trusted file location for source discovery."
    _assert_real_discovery_result(tmp_path, result, calls, writes, dry_run=dry_run,
                                  no_local=no_local, safe=case == "private", message=message)


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows ACL and reparse admission requires Windows.")
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(("case", "staged"), [
    (case, staged) for case in ("private", "directory-users", "directory-everyone", "script-users", "junction", "piped")
    for staged in (False, True)
] + [(case, True) for case in ("asset-users", "asset-everyone", "symlink", "foreign-owner",
                             "dangling-asset", "dangling-proof", "subject-only", "proof-only")])
def test_windows_local_discovery_uses_real_acl_and_reparse_admission(tmp_path, case, staged, dry_run):
    granted = None
    principal = None
    linked = None
    owned_asset = None
    original_owner = None

    def setup(directory, script, asset):
        nonlocal granted, principal, linked, owned_asset, original_owner
        if case == "foreign-owner":
            sid = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                capture_output=True, text=True, check=True, timeout=20,
            ).stdout.strip()
            changed = subprocess.run(
                ["icacls.exe", str(asset), "/setowner", "*S-1-5-32-545"],
                capture_output=True, text=True, timeout=20,
            )
            if changed.returncode:
                pytest.skip("This account cannot assign an untrusted fixture owner.")
            owned_asset, original_owner = asset, sid
        elif case in {"dangling-asset", "dangling-proof"}:
            asset.unlink()
            proof = directory / (asset.name + ".attestation.jsonl")
            proof.unlink()
            path = asset if case == "dangling-asset" else proof
            created = subprocess.run(
                [os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(path), str(directory / "absent")],
                capture_output=True, text=True, timeout=20,
            )
            if created.returncode:
                pytest.skip("This account cannot create the dangling fixture junction.")
            linked = path, True
        elif case == "subject-only":
            (directory / (asset.name + ".attestation.jsonl")).unlink()
        elif case == "proof-only":
            asset.unlink()
        elif "-" in case:
            target, identity = case.split("-")
            principal = "*S-1-5-32-545" if identity == "users" else "*S-1-1-0"
            granted = directory if target == "directory" else script if target == "script" else asset
            rights = "(OI)(CI)M" if target == "directory" else "M"
            changed = subprocess.run(
                ["icacls.exe", str(granted), "/grant", principal + ":" + rights],
                capture_output=True, text=True, timeout=20,
            )
            if changed.returncode:
                granted = None
                pytest.skip("This account cannot grant access to the fixture.")
        elif case == "symlink":
            target = directory / "foreign.zip"
            asset.rename(target)
            _windows_file_link(asset, target)
            linked = asset, False
        elif case == "junction":
            target = directory.with_name("redirected-source")
            directory.rename(target)
            created = subprocess.run(
                [os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(directory), str(target)],
                capture_output=True, text=True, timeout=20,
            )
            if created.returncode:
                pytest.skip("This account cannot create the fixture junction.")
            linked = directory, True

    try:
        result, calls, writes = run_bootstrap(
            tmp_path, "powershell", [{"observations": [observation()], "exit": 0}] * 2,
            dry_run=dry_run, local=staged, real_admission=True, local_setup=setup, piped=case == "piped",
        )
    finally:
        if owned_asset:
            subprocess.run(
                ["icacls.exe", str(owned_asset), "/setowner", "*" + original_owner],
                capture_output=True, text=True, check=True, timeout=20,
            )
        if granted:
            subprocess.run(
                ["icacls.exe", str(granted), "/remove:g", principal],
                capture_output=True, text=True, check=True, timeout=20,
            )
        if linked:
            path, directory = linked
            if directory:
                os.rmdir(path)
            else:
                path.unlink()
    message = "The source discovery asset or proof is unavailable or oversized." if case.startswith("asset-") or (
        case in {"symlink", "foreign-owner", "dangling-asset", "dangling-proof", "subject-only", "proof-only"}
    ) else "The bootstrap script must use a trusted file location for source discovery."
    _assert_real_discovery_result(tmp_path, result, calls, writes, dry_run=dry_run,
                                  no_local=not staged or case == "piped", safe=case == "private", message=message)


@pytest.mark.parametrize("dry_run", [False, True])
def test_bash_unknown_script_location_uses_the_download_path(tmp_path, dry_run):
    result, calls, writes = run_bootstrap(
        tmp_path, "bash", [{"observations": [observation()], "exit": 0}] * 2,
        dry_run=dry_run, piped=True, real_admission=sys.platform == "linux",
    )
    _assert_real_discovery_result(tmp_path, result, calls, writes, dry_run=dry_run,
                                  no_local=True, safe=False, message="")


def _assert_real_discovery_result(tmp_path, result, calls, writes, *, dry_run, no_local, safe, message):
    accepted = not dry_run or (not no_local and safe)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    log = tmp_path / "curl.log"
    downloads = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    if accepted:
        assert len(calls) == 2 and f"Discovered source commit: {COMMIT}." in result.stdout
        if dry_run:
            assert not writes and not downloads
        else:
            base = "https://github.com/example/publisher/releases/download/v1.0.0b7/"
            assert downloads == [base + "siteops-install.zip", base + "siteops-install.zip.attestation.jsonl"]
            assert "PINNED_FLOW_REACHED" in result.stdout
    else:
        assert not calls and not downloads
        if dry_run:
            assert not writes
        if no_local:
            message = "Source discovery in preview requires the release asset and proof beside the bootstrap script."
        assert message in " ".join((result.stdout + result.stderr).split())
        assert "Discovered source commit:" not in result.stdout


def test_local_evidence_admission_is_called_only_at_the_preview_discovery_boundary():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    windows = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = bash.split("if ! $commit_supplied && $dry_run; then", 1)[1].split('commit="$(discover_source_commit', 1)[0]
    assert "\n  select_local_discovery_assets\n" in block
    assert bash.count("\n  select_local_discovery_assets\n") == 1
    block = windows.split("if (-not $commitSupplied -and $DryRun) {", 1)[1].split("$SourceCommit = Find-SourceCommit", 1)[0]
    assert "$discoveryAssets = Select-LocalDiscoveryEvidence $discoverySubject" in block
    assert windows.count("$discoveryAssets = Select-LocalDiscoveryEvidence $discoverySubject") == 1


def test_local_script_admission_follows_evidence_presence_in_both_platforms():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    body = re.search(r"(?ms)^select_local_discovery_assets\(\) \{.*?^\}", bash).group()
    assert body.index('[[ -n "$self" ]] || return 0') < body.index('admit_file "$self"')
    assert body.index('-L "$directory/$discovery_subject.attestation.jsonl"') < body.index('admit_file "$self"')
    windows = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    body = re.search(r"(?ms)^function Select-LocalDiscoveryEvidence\([^\n]*\) \{.*?^\}", windows).group()
    assert body.index("if (-not $PSCommandPath) { return $null }") < body.index(
        "Require-PrivateExecutablePath $PSCommandPath",
    )
    assert body.index("if ($present.Count -eq 0) { return $null }") < body.index(
        "Require-PrivateExecutablePath $PSCommandPath",
    )


def test_ubuntu_fixture_uses_a_pinned_acr_base_and_nonroot_python():
    source = (ROOT / "tests" / "fixtures" / "Dockerfile.bootstrap-ubuntu").read_text(encoding="utf-8")
    assert re.fullmatch(
        r"FROM ubuntu\.azurecr\.io/ubuntu:noble@sha256:[0-9a-f]{64}",
        source.splitlines()[0],
    )
    assert "apt-get install -y --no-install-recommends python3 python3-venv" in source
    assert "USER 65534:65534" in source
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in source


def test_bash_bootstrap_gates_on_capability_without_elevation():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert "getconf GNU_LIBC_VERSION" in bash
    for removed in ("/etc/os-release", "VERSION_ID", "sudo", "apt-get", "--with-azure-cli"):
        assert removed not in bash


def test_bootstraps_run_github_cli_without_telemetry():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    assert bash.index("\nexport GH_TELEMETRY=false\n") < bash.index('gh_version="$(timeout')
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert powershell.index("\n$env:GH_TELEMETRY = 'false'\n") < powershell.index("\n$gh = Select-GitHubCli\n")


def test_bash_bootstrap_admits_private_data_root_before_retained_tool_use():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    for invocation in ("\n  prepare_runtime\n", "\nprepare_runtime\n"):
        assert bash.index('admit_directory "$data" private') < bash.index(invocation)
    runtime = re.search(r"(?ms)^prepare_runtime\(\) \{.*?^\}", bash).group()
    assert "\n  select_uv\n" in runtime


def test_windows_bootstrap_admits_private_data_root_before_retained_tool_use():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "function Require-PrivateDataRoot(" in powershell
    assert powershell.index("Require-PrivateDataRoot $data") < powershell.index(
        "$uv = Select-Uv $data $download $curl",
    )


def test_windows_bootstrap_owns_new_data_root_before_private_acl_and_admission():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    create = powershell.split("function Require-PrivateDataRoot(", 1)[1].split(
        "function Require-PrivateExecutablePath(", 1,
    )[0].split("if (-not $exists)", 1)[1]
    assert '& icacls.exe $Path /setowner "*$sid"' in create
    assert create.index('& icacls.exe $Path /setowner "*$sid"') < create.index(
        '& icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F"',
    )
    assert "if (-not $owned) {" in create
    assert "Reject 'ROOT_DATA_OWNER'" in create


def test_bootstrap_failures_are_distinguishable_from_progress():
    bash = {line.strip() for line in (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8").splitlines()}
    assert "fail() { printf 'Site Ops installation failed: %s\\n' \"$1\" >&2; exit 1; }" in bash
    assert "stage() { printf 'Site Ops installation: %s\\n' \"$1\"; }" in bash
    powershell = {
        line.strip() for line in (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8").splitlines()
    }
    assert 'function Fail([string]$Message) { throw "Site Ops installation failed: $Message" }' in powershell
    assert 'function Stage([string]$Message) { Write-Host "Site Ops installation: $Message" }' in powershell


def test_windows_bootstrap_creates_retained_bundle_as_user_before_helper_extraction():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    lines = [line.strip() for line in powershell.splitlines()]
    declared = lines.index("$bundle = Join-Path $root $bundleId")
    helper = next(index for index, line in enumerate(lines) if line.startswith("$installed = Check-Payload @("))
    # The helper would otherwise create the bundle with the elevated token's default owner.
    assert "Require-PrivateDataRoot $bundle" in lines[declared + 1:helper]
    assert not any("Test-Path -LiteralPath $bundle" in line for line in lines)

def test_windows_bootstrap_checks_managed_executables_before_running_them():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert powershell.index("Require-PrivateExecutablePath $available (Split-Path -Parent $available) -Optional") < (
        powershell.index("Stage 'Keep: the other uv installation unchanged.")
    )
    runtime = powershell.split("function Select-ManagedPython(", 1)[1].split("\n}\n", 1)[0]
    assert runtime.index("Require-PrivateRuntimeTree (Split-Path -Parent $python) $Directory $python") < (
        runtime.index("& $python -I -S -B")
    )
    assert "python', 'list'" not in runtime
    assert powershell.index("Require-PrivateRuntimeTree $toolHome $toolDirectory") < powershell.index(
        "$installed = Check-Payload @($mode",
    )
    assert powershell.index("Require-PrivateExecutablePath $expectedCommand $commandDirectory") < (
        powershell.index("$siteops = Native 'siteops.exe'")
    )
    assert "Join-Path $toolHome 'Scripts\\siteops.exe'" in powershell
    assert "$validatedTarget" not in powershell
    assert powershell.index("Require-PrivateExecutablePath $expectedCommand $commandDirectory") < (
        powershell.index("& $siteops --version")
    )


def _windows_functions(*names: str) -> str:
    source = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    bodies = []
    for name in ("Read-NodeAcl", *names):
        body = re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source)
        assert body, f"The Windows {name} helper is missing."
        bodies.append(body.group(0))
    return "\n".join(bodies) + "\n"


def _windows_private_root_wrapper(tmp_path: Path, setup: str = "") -> Path:
    wrapper = tmp_path / "check-data-root.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + setup
        + _windows_functions("Require-PrivateDataRoot")
        + "$ErrorActionPreference = 'Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "'PRIVATE_ROOT_ACCEPTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_tool_probe(
    wrapper: Path, root: Path, local_appdata: Path,
    executable: Path | None = None, private_root: Path | None = None,
):
    env = {
        **os.environ,
        "TEST_DATA_ROOT": str(root),
        "TEST_LOCALAPPDATA": str(local_appdata),
    }
    if executable is not None:
        env["TEST_TOOL"] = str(executable)
        env["TEST_PRIVATE_ROOT"] = str(private_root)
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=wrapper.parent,
        env=env,
        capture_output=True, text=True, timeout=20,
    )


def _windows_tool_path_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "check-tool.ps1"
    wrapper.write_text(
        'function Fail([string]$message) { throw "Site Ops installation: $message" }\n'
        + _windows_functions("Require-PrivateDataRoot", "Require-PrivateExecutablePath")
        + "$ErrorActionPreference='Stop'\n"
        "Require-PrivateDataRoot $env:TEST_DATA_ROOT\n"
        "Require-PrivateExecutablePath $env:TEST_TOOL $env:TEST_PRIVATE_ROOT\n"
        "'TOOL_ADMITTED'\n",
        encoding="utf-8",
    )
    return wrapper


def _windows_file_link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except OSError as error:
        if error.winerror == 1314:
            if os.environ.get("SITEOPS_REQUIRE_WINDOWS_SYMLINK_REJECTION") == "1":
                pytest.fail("Required Windows file symlink capability is unavailable.")
            pytest.skip("This local host cannot create a Windows file symlink.")
        raise


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["uv", "runtime"])
def test_windows_bootstrap_rejects_untrusted_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    if kind == "uv":
        shared = root / "tools"
        executable = shared / "uv" / "0.12.20" / "uv.exe"
        executable.parent.mkdir(parents=True)
        rights = "(OI)(CI)M"
    else:
        shared = local_appdata
        executable = shared / "uv" / "python" / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
        rights = "(OI)(CI)(WD,AD)"
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", f"*S-1-5-32-545:{rights}"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic fixture ACL.")
    try:
        executable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shutil.which("where.exe"), executable)
        selected = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata,
            executable, root if kind == "uv" else shared / "uv" / "python",
        )
        assert selected.returncode != 0
        assert "TOOL_ACL" in selected.stderr
        assert "TOOL_ADMITTED" not in selected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
@pytest.mark.parametrize("kind", ["uv", "runtime"])
def test_windows_bootstrap_accepts_private_selected_executable_path(tmp_path, kind):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    if kind == "uv":
        executable = root / "tools" / "uv" / "0.12.20" / "uv.exe"
        private_root = root
    else:
        private_root = local_appdata / "uv" / "python"
        executable = private_root / "cpython-3.11.16-windows-x86_64-none" / "python.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    selected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, executable, private_root,
    )
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert "TOOL_ADMITTED" in selected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_accepts_private_copied_siteops_launcher(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    shutil.copy2(shutil.which("where.exe"), launcher)

    accepted = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "TOOL_ADMITTED" in accepted.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_file_link_to_foreign_command(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    unrelated = root / "tools" / "other" / "Scripts" / "siteops.exe"
    unrelated.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), unrelated)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, unrelated)

    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_copied_launcher_with_writable_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    shutil.copy2(shutil.which("where.exe"), launcher)
    grant = subprocess.run(
        ["icacls.exe", str(bin_dir), "/grant", "*S-1-5-32-545:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic target ACL.")
    try:
        rejected = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
        )
        assert rejected.returncode != 0
        assert "TOOL_ACL" in rejected.stderr
        assert "TOOL_ADMITTED" not in rejected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(bin_dir), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_file_link_even_to_private_tool(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target = root / "tools" / "siteops" / "Scripts" / "siteops.exe"
    target.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "siteops.exe"
    _windows_file_link(launcher, target)
    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, bin_dir,
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher admission needs Windows.")
def test_windows_bootstrap_rejects_reparse_command_directory_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target_directory = root / "tools" / "other" / "Scripts"
    target_directory.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), target_directory / "siteops.exe")
    alias = root / "tools" / "siteops"
    try:
        alias.symlink_to(target_directory.parent, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(target_directory.parent)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    launcher = alias / "Scripts" / "siteops.exe"
    rejected = _windows_tool_probe(
        _windows_tool_path_wrapper(tmp_path), root, local_appdata, launcher, root / "tools",
    )
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
def test_windows_bootstrap_rejects_untrusted_executable_file_even_under_private_parent(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    wrapper = _windows_tool_path_wrapper(tmp_path)
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    executable = root / "tools" / "other" / "Scripts" / "siteops.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    grant = subprocess.run(
        ["icacls.exe", str(executable), "/grant", "*S-1-5-32-545:M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic executable ACL.")
    try:
        rejected = _windows_tool_probe(wrapper, root, local_appdata, executable, root)
        assert rejected.returncode != 0
        assert "TOOL_ACL" in rejected.stderr
        assert "TOOL_ADMITTED" not in rejected.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(executable), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows junction rules need Windows.")
def test_windows_bootstrap_rejects_linked_executable_ancestor(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    wrapper = _windows_tool_path_wrapper(tmp_path)
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    external = tmp_path / "elsewhere"
    (external / "other" / "Scripts").mkdir(parents=True)
    alias = root / "tools"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(external)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    executable = alias / "other" / "Scripts" / "siteops.exe"
    shutil.copy2(shutil.which("where.exe"), executable)
    rejected = _windows_tool_probe(wrapper, root, local_appdata, executable, root)
    assert rejected.returncode != 0
    assert "TOOL_TYPE" in rejected.stderr
    assert "TOOL_ADMITTED" not in rejected.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL admission needs Windows.")
def test_windows_bootstrap_accepts_create_only_ancestor_above_private_tool_root(tmp_path):
    local_appdata = tmp_path / "LocalAppData"
    local_appdata.mkdir()
    root = local_appdata / "siteops"
    initial = _windows_tool_probe(_windows_private_root_wrapper(tmp_path), root, local_appdata)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    executable = root / "tools" / "other" / "Scripts" / "siteops.exe"
    executable.parent.mkdir(parents=True)
    shutil.copy2(shutil.which("where.exe"), executable)
    grant = subprocess.run(
        ["icacls.exe", str(tmp_path), "/grant", "*S-1-5-32-545:(WD,AD)"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the synthetic ancestor ACL.")
    try:
        accepted = _windows_tool_probe(
            _windows_tool_path_wrapper(tmp_path), root, local_appdata, executable, root,
        )
        assert accepted.returncode == 0, accepted.stdout + accepted.stderr
        assert "TOOL_ADMITTED" in accepted.stdout
    finally:
        subprocess.run(
            ["icacls.exe", str(tmp_path), "/remove:g", "*S-1-5-32-545"],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL and junction rules need Windows.")
def test_windows_bootstrap_rejects_shared_root_without_changing_it(tmp_path):
    wrapper = _windows_private_root_wrapper(tmp_path)
    safe = tmp_path / "safe"
    safe.mkdir()
    valid = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(safe / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert "PRIVATE_ROOT_ACCEPTED" in valid.stdout
    assert (safe / "siteops").is_dir()
    repeated = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(safe / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr

    shared = tmp_path / "shared"
    shared.mkdir()
    users = "*S-1-5-32-545"
    grant = subprocess.run(
        ["icacls.exe", str(shared), "/grant", f"{users}:(OI)(CI)M"],
        capture_output=True, text=True, timeout=20,
    )
    if grant.returncode:
        pytest.skip("The local test user cannot change the fixture directory ACL.")
    try:
        rejected = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
            cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(shared / "siteops")},
            capture_output=True, text=True, timeout=20,
        )
        assert rejected.returncode != 0
        assert "private Site Ops data root" in rejected.stderr
        assert "ROOT_ANCESTOR_ACL" in rejected.stderr
        assert "PRIVATE_ROOT_ACCEPTED" not in rejected.stdout
        assert not (shared / "siteops").exists()
    finally:
        subprocess.run(
            ["icacls.exe", str(shared), "/remove:g", users],
            capture_output=True, text=True, check=True, timeout=20,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse rules need Windows.")
def test_windows_bootstrap_rejects_symlinked_data_ancestor(tmp_path):
    wrapper = _windows_private_root_wrapper(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(private, target_is_directory=True)
    except OSError:
        junction = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:TEST_ALIAS -Target $env:TEST_TARGET | Out-Null",
            ],
            env={**os.environ, "TEST_ALIAS": str(alias), "TEST_TARGET": str(private)},
            capture_output=True, text=True, timeout=20,
        )
        if junction.returncode:
            pytest.skip("Creating a test reparse point is unavailable on this host.")
    rejected = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(alias / "siteops")},
        capture_output=True, text=True, timeout=20,
    )
    assert rejected.returncode != 0
    assert "private Site Ops data root" in rejected.stderr
    assert "ROOT_ANCESTOR_TYPE" in rejected.stderr
    assert not (private / "siteops").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows filesystem errors need PowerShell 5.1.")
@pytest.mark.parametrize(
    ("case", "category"),
    [
        ("invalid-path", "ROOT_PATH"),
        ("missing-ancestor", "ROOT_ANCESTOR_TYPE"),
        ("ancestor-error", "ROOT_ANCESTOR_TYPE"),
        ("root-probe-error", "ROOT_DATA_TYPE"),
        ("creation-error", "ROOT_DATA_CREATE"),
        ("owner-error", "ROOT_DATA_OWNER"),
        ("protection-error", "ROOT_DATA_ACL"),
    ],
)
def test_windows_bootstrap_root_filesystem_errors_are_bounded(tmp_path, case, category):
    marker = "PRIVATE_DATA_PATH_MARKER"
    target = tmp_path / "siteops"
    setup = ""
    if case == "invalid-path":
        target = Path(f"C:\\{marker}<\\siteops")
    elif case == "missing-ancestor":
        target = tmp_path / f"missing-{marker}" / "siteops"
    elif case == "ancestor-error":
        setup = f"function Get-Item {{ throw '{marker}' }}\n"
    elif case == "root-probe-error":
        setup = f"function Test-Path {{ throw '{marker}' }}\n"
    elif case == "creation-error":
        setup = f"function New-Item {{ throw '{marker}' }}\n"
    elif case == "owner-error":
        setup = (
            f"function icacls.exe {{ Write-Error '{marker}'; "
            "$global:LASTEXITCODE = 5 }\n"
        )
    else:
        setup = (
            "function icacls.exe { if ($args -contains '/setowner') { "
            "$global:LASTEXITCODE = 0; return }; "
            f"Write-Error '{marker}'; $global:LASTEXITCODE = 5 }}\n"
        )
    wrapper = _windows_private_root_wrapper(tmp_path, setup)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, env={**os.environ, "TEST_DATA_ROOT": str(target)},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert category in result.stderr
    assert marker not in result.stdout + result.stderr
    assert "PRIVATE_ROOT_ACCEPTED" not in result.stdout


def test_existing_siteops_build_is_rejected_before_any_shared_backend_change():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "mode=replace" in bash and 'check_payload "$mode" "$archive" "$bundle"' in bash
    assert '2) fail "Another Site Ops selection is installed. Use --replace after review." ;;' in bash
    assert "$mode = if ($Replace) { 'replace' } else { 'install' }" in powershell
    assert "Check-Payload @($mode, $archive, $bundle" in powershell
    assert '"$siteops" --version' in bash and "siteops.exe" in powershell


def test_windows_native_verification_parses_json_without_powershell_51_jq_quoting():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert "--jq" not in powershell
    assert "ConvertFrom-Json" in powershell
    guide = (ROOT / "docs" / "install-siteops.md").read_text(encoding="utf-8")
    verified = guide.split("### Verify the bootstrap script", 1)[1].split("## Before you start", 1)[0]
    windows = verified.split("Windows PowerShell:", 1)[1]
    assert "--jq" not in windows
    assert "ConvertFrom-Json" in windows


def test_windows_prerequisites_are_checked_not_installed_and_enrollment_is_delegated():
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for removed in ("WithAzureCli", "WinGet", "winget", "GetEnvironmentVariable('PATH'",
                    "Ensure-UvStorage $env:TEMP", "Join-Path $env:TEMP", "attestation trusted-root",
                    "ArtifactVerificationPolicy"):
        assert removed not in powershell
    lines = [line.strip() for line in powershell.splitlines()]
    resolved = lines.index("$gh = Select-GitHubCli")
    assert lines.count("$gh = Select-GitHubCli") == 1
    assert resolved < lines.index("if ($DryRun) {") < lines.index("if (-not $Yes) {")
    selection = powershell.split("function Select-GitHubCli() {", 1)[1].split("\n}\n", 1)[0]
    assert selection.index("Require-PrivateExecutablePath $path $path -Optional") < selection.index(
        "& $path version",
    )
    assert "& $gh attestation verify" in powershell
    assert '& $siteops source enroll $EnrollSource --source "github:$Repository"' in lines
    assert "Stage 'Azure CLI was not found. Install it before deploying: https://aka.ms/installazurecli'" in lines


@pytest.mark.skipif(sys.platform != "win32", reason="This checks native PowerShell 5.1 argument handling.")
@pytest.mark.parametrize("accepted", [False, True])
def test_windows_bootstrap_checks_native_verifier_certificate_before_bundle_use(
    tmp_path, accepted,
):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^function Verify-ReleaseAsset\([^\n]*\) \{.*?^\}", script).group()
    assert "Verify-ReleaseAsset $archive $engineCommit $engineRef $engineCaller '_siteops-distribution.yaml'" in script
    observation = verified_observation(
        "Azure/digital-ops-scale-kit", SOURCE_SHA, "refs/heads/main",
        ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
    )
    if not accepted:
        observation["verificationResult"]["signature"]["certificate"]["buildConfigURI"] = "PRIVATE_WRONG"
    evidence = tmp_path / "observation.json"
    evidence.write_text(json.dumps([observation]), encoding="utf-8")
    wrapper = tmp_path / "verify.ps1"
    wrapper.write_text(
        """function gh.exe { Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw; $global:LASTEXITCODE = 0 }
function Stage([string]$message) { }
function Fail([string]$message) { throw $message }
$ErrorActionPreference = 'Stop'
$gh = 'gh.exe'
$archive = 'unopened.zip'
$Repository = 'Azure/digital-ops-scale-kit'
$SourceRef = 'refs/heads/main'
$Caller = 'release.yaml'
$SourceCommit = '""" + SOURCE_SHA + "'\n" + block
        + "\nVerify-ReleaseAsset $archive $SourceCommit $SourceRef $Caller '_siteops-distribution.yaml'\n'CERTIFICATE_ACCEPTED'\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        env={**os.environ, "TEST_EVIDENCE": str(evidence)},
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert ("CERTIFICATE_ACCEPTED" in result.stdout) is accepted
    assert "PRIVATE_WRONG" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 5.1 is available on Windows.")
def test_windows_retained_release_key_binds_exact_selection(tmp_path):
    script = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^function Get-SelectionKey\([^\n]*\) \{.*?^\}", script).group()
    statement = (
        "$Repository='Azure/digital-ops-scale-kit';$Release='siteops/v1.0.0b1';"
        "$SourceCommit='" + SOURCE_SHA + "';$SourceRef='refs/heads/main';"
        "$Caller='release.yaml';$data='unused';"
        + block + "\nGet-SelectionKey $Release $SourceCommit $SourceRef $Caller\n"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", statement],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    selected = (
        "Azure/digital-ops-scale-kit", "siteops/v1.0.0b1",
        SOURCE_SHA, "refs/heads/main", "release.yaml",
    )
    expected = hashlib.sha256(("\0".join(selected) + "\0").encode()).hexdigest()
    assert result.stdout.strip() == expected


def test_bash_is_portable_lf_and_parses():
    script = SCRIPTS / "siteops-bootstrap.sh"
    content = script.read_bytes()
    assert content.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in content
    result = subprocess.run(
        [str(required_bash()), "-n", bash_path(script)],
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_both_scripts_keep_the_azure_and_source_boundaries_explicit():
    bash = (SCRIPTS / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    for script in (bash, powershell):
        assert "gh auth login" not in script and "az login" not in script
        assert "siteops deploy" not in script
        assert "attestation verify" in script
        assert "siteops-install.zip.attestation.jsonl" in script
        assert "--cert-identity" in script and "--source-digest" in script
        assert "--tlsv1.2" in script
        assert "buildConfigURI" in script and "runnerEnvironment" in script
        if script is bash:
            assert script.index("attestation verify") < script.index('check_payload "$mode"')
        else:
            assert script.index("attestation verify") < script.index("Check-Payload @($mode")


def test_bootstrap_preview_keeps_source_names_private_in_redacted_output():
    for path in (SCRIPTS / "siteops-bootstrap.sh", SCRIPTS / "siteops-bootstrap.ps1"):
        script = path.read_text(encoding="utf-8")
        assert "SITEOPS_REDACT_OUTPUT" in script
        assert "An explicitly selected source will be enrolled after installation." in script
        assert "with an approved source. Authenticate to Azure separately." in script
    if sys.platform != "win32":
        return
    script = SCRIPTS / "siteops-bootstrap.ps1"
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
            "-EnrollSource", "private-name", "-DryRun",
        ],
        env={**os.environ, "SITEOPS_REDACT_OUTPUT": "1"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private-name" not in result.stdout + result.stderr


def test_bootstrap_messages_use_enrollment_and_product_terms():
    expected = (
        "will enroll {repository} with an expiring policy after installation.",
        "Enroll this publisher as an approved source? [y/N]",
        "Installed Site Ops $version with approved source {name}. Authenticate to Azure separately.",
        "Installed Site Ops $version. Next, authenticate to Azure and enroll a content source, "
        "for example siteops source enroll official.",
    )
    for path, repository, name in (
        (SCRIPTS / "siteops-bootstrap.sh", "$repository", "$enroll_name"),
        (SCRIPTS / "siteops-bootstrap.ps1", "$Repository", "$EnrollSource"),
    ):
        script = path.read_text(encoding="utf-8")
        for line in expected:
            assert line.format(repository=repository, name=name) in script, (path.name, line)
        for retired in ("will approve", "approved consumer source", "approve a workspace source",
                        "Installed siteops"):
            assert retired not in script, (path.name, retired)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_names_the_enrollment(tmp_path):
    if shutil.which("gh") is None:
        pytest.skip("GitHub CLI is unavailable.")
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(SCRIPTS / "siteops-bootstrap.ps1"),
            "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
            "-EnrollSource", "demo", "-DryRun",
        ],
        env={**os.environ, "SITEOPS_REDACT_OUTPUT": "0", "LOCALAPPDATA": str(tmp_path / "state")},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Source demo will enroll Azure/digital-ops-scale-kit with an expiring policy" in result.stdout
    assert "approve" not in result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="The Linux preview runs on the Linux runner.")
def test_bash_preview_and_unattended_refusal_do_not_acquire_tools(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text("#!/usr/bin/env bash\necho 'gh version 2.95.0 (fixture)'\n", encoding="utf-8")
    for path, mode in ((tmp_path, 0o700), (tools, 0o755), (gh, 0o755)):
        path.chmod(mode)
    script = SCRIPTS / "siteops-bootstrap.sh"
    arguments = ["bash", str(script), "--release", "siteops/v1.0.0b1", "--source-commit", SOURCE_SHA]
    env = {
        **os.environ, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "state"),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }
    preview = subprocess.run(
        [*arguments, "--dry-run"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        arguments, input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "pass --yes" in denied.stderr
    assert not (tmp_path / "state").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_and_invalid_identity_do_not_acquire_tools(tmp_path):
    script = SCRIPTS / "siteops-bootstrap.ps1"
    path = shutil.which("pwsh") or shutil.which("powershell")
    if path is None:
        pytest.skip("PowerShell is unavailable.")
    arguments = [
        path, "-NoProfile", "-File", str(script),
        "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA,
    ]
    env = {**os.environ, "LOCALAPPDATA": str(tmp_path / "state")}
    preview = subprocess.run(
        [*arguments, "-DryRun"], input="", text=True, capture_output=True, env=env, timeout=20,
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "Uses the installed GitHub CLI" in preview.stdout
    assert "No tools or content were downloaded" in preview.stdout
    denied = subprocess.run(
        [*arguments[:-1], "bad", "-Yes"], input="", text=True,
        capture_output=True, env=env, timeout=20,
    )
    assert denied.returncode != 0
    assert "Select an exact release" in denied.stderr
    # The admitted GitHub CLI may keep its own state; the preview creates no Site Ops state.
    assert not (tmp_path / "state" / "siteops").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell preview runs on Windows.")
def test_powershell_preview_requires_github_cli_without_installing_it(tmp_path):
    system = Path(os.environ["SystemRoot"]) / "System32"
    result = subprocess.run(
        [
            str(system / "WindowsPowerShell" / "v1.0" / "powershell.exe"), "-NoProfile",
            "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "siteops-bootstrap.ps1"),
            "-Release", "siteops/v1.0.0b1", "-SourceCommit", SOURCE_SHA, "-DryRun",
        ],
        env={**os.environ, "PATH": str(system), "LOCALAPPDATA": str(tmp_path / "state")},
        input="", text=True, capture_output=True, timeout=20,
    )
    assert result.returncode != 0
    # Windows PowerShell wraps long errors at its console width.
    message = "GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry."
    assert "".join(message.split()) in "".join(result.stderr.split())
    assert "Preview only" not in result.stdout
    assert not (tmp_path / "state").exists()

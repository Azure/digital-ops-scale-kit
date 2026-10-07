"""Exercise content-to-engine selection without source, signing or Azure operations."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.installed_runtime import isolated_environment
from tests.native_bundle import bundle_factory as bundle_factory
from tests.native_bundle import publish_assets
from tests.native_uv_consumers import _unavailable
from tests.shell_helpers import bash_path, run_script
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap"
CONTENT_SHA = "c" * 40
ENGINE_SHA = "a" * 40


def reference(*, combined=False, preview=False):
    return {
        "apiVersion": "siteops.release.engine/v1", "kind": "EngineReference",
        "release": "v1.0.0b7", "revision": CONTENT_SHA, "preview": preview,
        "engine": {
            "release": "v1.0.0b7" if combined else "siteops/v1.0.0b1",
            "revision": CONTENT_SHA if combined else ENGINE_SHA,
            "version": "1.0.0b1+build.42.1.gcccccccccccc" if combined else "1.0.0b1",
            "bundle": {"name": "siteops-install.zip", "size": 100, "sha256": "1" * 64},
            "proof": {"name": "siteops-install.zip.attestation.jsonl", "size": 20, "sha256": "2" * 64},
        },
    }


def serialized(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def powershell_functions(*names):
    source = (BOOTSTRAP / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    return "\n".join(
        re.search(rf"(?ms)^function {name}\([^\n]*\) \{{.*?^\}}", source).group()
        for name in names
    )


def parse_reference(tmp_path, shell, raw, *, preview=False):
    path = tmp_path / "reference.json"
    path.write_bytes(raw)
    if shell == "bash":
        source = (BOOTSTRAP / "siteops-bootstrap.sh").read_text(encoding="utf-8")
        body = re.search(r"(?ms)^read_engine_reference\(\) \{.*?^\}", source).group()
        return run_script(
            body + '\npython="$TEST_PYTHON"\nrelease=v1.0.0b7\ncommit="$TEST_COMMIT"\n'
            'source_ref="$TEST_REF"\ncaller="$TEST_CALLER"\nread_engine_reference "$TEST_FILE"\n',
            tmp_path, {
                "TEST_PYTHON": bash_path(Path(sys.executable)), "TEST_FILE": bash_path(path),
                "TEST_COMMIT": CONTENT_SHA, "TEST_REF": "refs/heads/preview" if preview else "refs/heads/main",
                "TEST_CALLER": "ci.yaml" if preview else "release.yaml",
            },
        )
    program = shutil.which("powershell.exe")
    if program is None:
        pytest.skip("Native Windows PowerShell is covered by the Windows lane.")
    wrapper = tmp_path / "reference.ps1"
    wrapper.write_text(
        "$ErrorActionPreference='Stop'\nfunction Fail([string]$message) { throw $message }\n"
        + powershell_functions("Read-EngineReference")
        + "\n$r=Read-EngineReference $env:TEST_FILE 'v1.0.0b7' $env:TEST_COMMIT $env:TEST_REF $env:TEST_CALLER\n"
        + "@($r.Release,$r.Commit,$r.SourceRef,$r.Caller,$r.Version,$r.BundleSize,$r.BundleSha,$r.ProofSize,$r.ProofSha)\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [program, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)], cwd=tmp_path,
        env={**isolated_environment(tmp_path / "state"), "TEST_FILE": str(path), "TEST_COMMIT": CONTENT_SHA,
             "TEST_REF": "refs/heads/preview" if preview else "refs/heads/main",
             "TEST_CALLER": "ci.yaml" if preview else "release.yaml"},
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("preview", [False, True])
def test_platforms_resolve_exact_engine_without_replacing_content_policy(tmp_path, shell, combined, preview):
    document = reference(combined=combined, preview=preview)
    result = parse_reference(tmp_path, shell, serialized(document), preview=preview)
    assert result.returncode == 0, result.stdout + result.stderr
    engine = document["engine"]
    assert result.stdout.splitlines() == [
        engine["release"], engine["revision"],
        "refs/heads/preview" if combined and preview else "refs/heads/main",
        "ci.yaml" if combined and preview else "release.yaml", engine["version"],
        "100", "1" * 64, "20", "2" * 64,
    ]


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("fault", [
    "source", "release", "preview", "preview-number", "combined-revision", "engine-tag", "version",
    "bundle", "proof", "size-bool", "size-float", "too-large", "extra-policy", "duplicate", "bad-json",
])
def test_platforms_refuse_ambiguous_or_unbound_engine_references(tmp_path, shell, fault):
    document = reference()
    if fault == "source":
        document["revision"] = "b" * 40
    elif fault == "release":
        document["release"] = "v9.9.9"
    elif fault.startswith("preview"):
        document["preview"] = 0 if fault == "preview-number" else True
    elif fault == "combined-revision":
        document["engine"]["release"] = document["release"]
    elif fault == "engine-tag":
        document["engine"]["release"] = "v8.0.0"
    elif fault == "version":
        document["engine"]["version"] = "private-marker\ncommands"
    elif fault in {"bundle", "proof"}:
        document["engine"][fault]["sha256"] = "private-marker"
    elif fault.startswith("size"):
        document["engine"]["bundle"]["size"] = True if fault == "size-bool" else 100.0
    elif fault == "too-large":
        document["engine"]["proof"]["size"] = 2097153
    elif fault == "extra-policy":
        document["policy"] = {"signer": "private-marker"}
    raw = serialized(document)
    if fault == "duplicate":
        raw = raw.replace(b'"preview":false', b'"preview":false,"preview":false')
    elif fault == "bad-json":
        raw = b'{"private-marker":broken'
    result = parse_reference(tmp_path, shell, raw)
    assert result.returncode != 0
    assert "private-marker" not in result.stdout + result.stderr
    assert "siteops/v1.0.0b1" not in result.stdout


def test_both_bootstraps_require_two_distinct_proofs_before_bundle_use():
    bash = (BOOTSTRAP / "siteops-bootstrap.sh").read_text(encoding="utf-8")
    powershell = (BOOTSTRAP / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert bash.index('verify_release_asset "$reference_assets/siteops-engine.json"') < bash.index(
        'read_engine_reference "$reference_assets/siteops-engine.json"',
    )
    assert bash.index('verify_release_asset "$assets/siteops-install.zip"') < bash.index(
        'installer_helper="$(extract_installer_helper)"',
    )
    assert powershell.index("Verify-ReleaseAsset $referencePath") < powershell.index(
        "$selectedEngine = Read-EngineReference",
    )
    assert powershell.index("Verify-ReleaseAsset $archive") < powershell.index(
        "$installerHelper = Get-InstallerHelper $archive",
    )
    for script in (bash, powershell):
        assert "_release-candidate.yaml" in script and "_siteops-distribution.yaml" in script
        assert "engine-references" in script and "install-downloads" in script
    assert '"$repository" "$engine_commit" "$engine_ref"' in bash
    assert "$Repository, $engineCommit," in powershell


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_direct_and_content_release_inputs_cannot_be_combined(tmp_path, shell):
    if shell == "bash":
        result = run_script(
            'bash "$TEST_SCRIPT" --release siteops/v1.0.0b1 --content-release v1.0.0b7 '
            '--source-commit "$TEST_COMMIT" --dry-run\n',
            tmp_path, {"TEST_SCRIPT": bash_path(BOOTSTRAP / "siteops-bootstrap.sh"), "TEST_COMMIT": CONTENT_SHA},
        )
    else:
        program = shutil.which("powershell.exe")
        if program is None:
            pytest.skip("Native Windows PowerShell is covered by the Windows lane.")
        result = subprocess.run([
            program, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BOOTSTRAP / "siteops-bootstrap.ps1"),
            "-Release", "siteops/v1.0.0b1", "-ContentRelease", "v1.0.0b7", "-SourceCommit", CONTENT_SHA, "-DryRun",
        ], cwd=tmp_path, env=isolated_environment(tmp_path / "state"), capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Choose either" in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows bootstrap journey.")
@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("fault", [None, "digest", "proof-digest", "version"])
def test_windows_content_install_uses_native_uv_and_rechecks_both_retained_proofs(
    bundle_factory, tmp_path, combined, fault,
):
    uv = os.environ.get("SITEOPS_TEST_UV")
    runtime = os.environ.get("SITEOPS_TEST_UV_PYTHON_DIR")
    if not uv or not runtime or not Path(uv).is_file() or not Path(runtime).is_dir():
        _unavailable("The Windows content journey requires the native uv and managed runtime fixtures.")
    document = reference(combined=combined)
    bundle, manifest = bundle_factory(
        version=document["engine"]["version"], source_sha=document["engine"]["revision"],
    )
    archive, _ = publish_assets(bundle, manifest, tmp_path / "published")
    proof = tmp_path / "bundle-proof.jsonl"
    proof.write_bytes(b"synthetic bundle proof")
    document["engine"]["bundle"].update(size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    document["engine"]["proof"].update(size=proof.stat().st_size, sha256=hashlib.sha256(proof.read_bytes()).hexdigest())
    if fault == "digest":
        document["engine"]["bundle"]["sha256"] = "0" * 64
    elif fault == "proof-digest":
        document["engine"]["proof"]["sha256"] = "0" * 64
    elif fault == "version":
        document["engine"]["version"] = "1.9.9"
        if not combined:
            document["engine"]["release"] = "siteops/v1.9.9"
    record = tmp_path / "siteops-engine.json"
    record.write_bytes(serialized(document))
    record_proof = tmp_path / "reference-proof.jsonl"
    record_proof.write_bytes(b"synthetic reference proof")
    base = "https://github.com/example/publisher/releases/download/"
    engine_tag = document["engine"]["release"].replace("/", "%2F")
    downloads = {
        base + "v1.0.0b7/siteops-engine.json": str(record),
        base + "v1.0.0b7/siteops-engine.json.attestation.jsonl": str(record_proof),
        base + engine_tag + "/siteops-install.zip": str(archive),
        base + engine_tag + "/siteops-install.zip.attestation.jsonl": str(proof),
    }
    evidence = {
        "siteops-engine.json": [verified_observation(
            "example/publisher", CONTENT_SHA, "refs/heads/main",
            ".github/workflows/_release-candidate.yaml", ".github/workflows/release.yaml",
        )],
        "siteops-install.zip": [verified_observation(
            "example/publisher", document["engine"]["revision"], "refs/heads/main",
            ".github/workflows/_siteops-distribution.yaml", ".github/workflows/release.yaml",
        )],
    }
    downloads_path, evidence_path = tmp_path / "downloads.json", tmp_path / "evidence.json"
    downloads_path.write_text(json.dumps(downloads), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    log = tmp_path / "operations.log"
    overrides = r"""
function Native([string]$Name) {
    switch ($Name) {
        'curl.exe' { return 'Test-Download' }
        'uv.exe' { return $env:TEST_UV }
        'siteops.exe' { return (Join-Path $env:UV_TOOL_BIN_DIR 'siteops.exe') }
        default { throw 'Unexpected native tool selection.' }
    }
}
function Select-GitHubCli { 'Test-Verify' }
function Test-Download {
    $mapping = Get-Content -LiteralPath $env:TEST_DOWNLOADS -Raw | ConvertFrom-Json
    $source = $mapping.PSObject.Properties[$args[-1]].Value
    $index = [array]::IndexOf($args, '--output')
    if (-not $source -or $index -lt 0) { throw 'Unapproved fixture download.' }
    [IO.File]::AppendAllText($env:TEST_LOG, "download`n")
    [IO.File]::Copy($source, $args[$index + 1], $false)
    $global:LASTEXITCODE = 0
}
function Test-Verify {
    if ($args[0] -ne 'attestation' -or $args[1] -ne 'verify') { throw 'Unapproved verifier operation.' }
    $name = [IO.Path]::GetFileName($args[2])
    [IO.File]::AppendAllText($env:TEST_LOG, "verify-$name`n")
    if ($env:TEST_REJECT -eq $name) { $global:LASTEXITCODE = 1; return }
    $mapping = Get-Content -LiteralPath $env:TEST_EVIDENCE -Raw | ConvertFrom-Json
    $result = $mapping.PSObject.Properties[$name].Value
    if (-not $result) { throw 'Unapproved verification subject.' }
    ConvertTo-Json -InputObject $result -Depth 20 -Compress
    $global:LASTEXITCODE = 0
}
"""
    source = (BOOTSTRAP / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    assert source.count("$curl = Native 'curl.exe'\n") == 1
    wrapper = tmp_path / "bootstrap.ps1"
    wrapper.write_text(source.replace("$curl = Native 'curl.exe'\n", overrides + "\n$curl = Native 'curl.exe'\n"),
                       encoding="utf-8")
    environment = {
        **isolated_environment(tmp_path / "state"), "TEST_UV": uv, "TEST_DOWNLOADS": str(downloads_path),
        "TEST_EVIDENCE": str(evidence_path), "TEST_LOG": str(log), "TEST_REJECT": "",
        "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
        "UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_PYTHON_INSTALL_DIR": runtime,
    }
    arguments = [
        shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
        "-ContentRelease", "v1.0.0b7", "-SourceCommit", CONTENT_SHA, "-Repository", "example/publisher", "-Yes",
    ]

    def run(reject=""):
        return subprocess.run(arguments, cwd=tmp_path, env={**environment, "TEST_REJECT": reject},
                              capture_output=True, text=True, timeout=120)

    first = run()
    if fault:
        assert first.returncode != 0
        assert "differs from the signed content selection" in first.stderr
        assert not (tmp_path / "tools" / "siteops").exists()
        return
    assert first.returncode == 0, first.stdout + first.stderr
    assert f"Installed siteops {manifest.version}" in first.stdout
    receipt = tmp_path / "tools" / "siteops" / "uv-receipt.toml"
    identity = receipt.stat().st_mtime_ns, receipt.read_bytes()
    assert log.read_text().splitlines().count("download") == 4
    repeated = run()
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert (receipt.stat().st_mtime_ns, receipt.read_bytes()) == identity
    assert log.read_text().splitlines().count("download") == 4
    for subject in ("siteops-engine.json", "siteops-install.zip"):
        rejected = run(subject)
        assert rejected.returncode != 0
        assert "provenance could not be verified" in rejected.stderr
    assert log.read_text().splitlines().count("download") == 4
    assert (receipt.stat().st_mtime_ns, receipt.read_bytes()) == identity


@pytest.mark.skipif(sys.platform != "win32", reason="Native PowerShell source enrollment.")
def test_windows_content_enrollment_does_not_adopt_the_engine_caller(tmp_path):
    source = (BOOTSTRAP / "siteops-bootstrap.ps1").read_text(encoding="utf-8")
    start = "    if ($EnrollSource) {\n        $lines ="
    block = start + source.split(start, 1)[1].split("    if ($assets -eq $download)", 1)[0]
    wrapper = tmp_path / "enroll.ps1"
    wrapper.write_text(
        r"""
$ErrorActionPreference = 'Stop'
function Fail([string]$message) { throw $message }
function Test-Roots {
    if (($args -join ' ') -cne 'attestation trusted-root') { throw 'Unexpected root request.' }
    '{"fixture":"independent-roots"}'
    $global:LASTEXITCODE = 0
}
function Test-Enroll {
    if ($args[0] -cne '--trust-policy' -or $args[4] -cne 'source' -or $args[5] -cne 'enroll') {
        throw 'Unexpected enrollment operation.'
    }
    Get-Content -LiteralPath $args[1] -Raw
    $global:LASTEXITCODE = 0
}
$download = $env:TEST_STATE
$gh = 'Test-Roots'
$siteops = 'Test-Enroll'
$EnrollSource = 'demo'
$Repository = 'example/publisher'
$SourceRef = 'refs/heads/content-preview'
$Caller = 'ci.yaml'
$engineRef = 'refs/heads/main'
$engineCaller = 'release.yaml'
"""
        + block, encoding="utf-8",
    )
    result = subprocess.run([
        shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
    ], cwd=tmp_path, env={**isolated_environment(tmp_path / "state"), "TEST_STATE": str(tmp_path)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    provider = json.loads(result.stdout)["provider"]
    assert provider["sourceRef"] == "refs/heads/content-preview"
    assert provider["builderWorkflow"] == ".github/workflows/ci.yaml"
    assert provider["signerWorkflow"] == ".github/workflows/_workspace-distribution.yaml"

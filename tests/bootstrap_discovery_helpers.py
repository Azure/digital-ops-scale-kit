"""Run the production discovery flow with closed download and verifier doubles."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from tests.shell_helpers import bash_path, required_bash, write_executable
from tests.verification_helpers import verified_observation

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "c" * 40
ENGINE_COMMIT = "a" * 40
REPOSITORY = "example/publisher"
SOURCE_REF = "refs/heads/main"
CALLER = "release.yaml"


def observation(*, content=False, engine=False):
    return verified_observation(
        REPOSITORY, ENGINE_COMMIT if engine else COMMIT, SOURCE_REF,
        ".github/workflows/_release-candidate.yaml" if content else ".github/workflows/_siteops-distribution.yaml",
        ".github/workflows/" + CALLER,
    )


def reference():
    archive = b"synthetic engine archive"
    proof = b"synthetic proof"
    return {
        "apiVersion": "siteops.release.engine/v1", "kind": "EngineReference",
        "release": "v1.0.0b7", "revision": COMMIT, "preview": False,
        "engine": {
            "release": "siteops/v1.0.0b1", "revision": ENGINE_COMMIT, "version": "1.0.0b1",
            "bundle": {"name": "siteops-install.zip", "size": len(archive),
                       "sha256": hashlib.sha256(archive).hexdigest()},
            "proof": {"name": "siteops-install.zip.attestation.jsonl", "size": len(proof),
                      "sha256": hashlib.sha256(proof).hexdigest()},
        },
    }


def _bash_overrides(*, real_admission=False):
    overrides = r'''
uname() { case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) return 99 ;; esac; }
getconf() { [[ "$*" == GNU_LIBC_VERSION ]] || return 99; echo 'glibc 2.39'; }
getent() { return 1; }
admit_file() { [[ -f "$1" && ! -L "$1" ]] || return 1; admitted="$1"; }
admit_directory() {
  [[ "${3:-create}" == existing ]] || mkdir -p -m 0700 -- "$1"
  [[ -d "$1" && ! -L "$1" ]] || return 1
  admitted="$1"
}
runtime_calls=0
prepare_runtime() {
  ((runtime_calls += 1))
  if [[ -n "$content_release" && $runtime_calls == 1 ]]; then python="$TEST_READER_PYTHON"; return; fi
  echo PINNED_FLOW_REACHED
  exit 0
}
'''
    if real_admission:
        return "runtime_calls=0\n" + re.search(r"(?ms)^prepare_runtime\(\) \{.*?^\}", overrides).group()
    return overrides


def _powershell_overrides():
    return r'''
function Native([string]$Name) {
    if ($Name -ceq 'curl.exe') { return 'Test-Download' }
    throw 'Unexpected tool selection.'
}
function Select-GitHubCli { return 'Test-Verify' }
function Get-InstallerHelper { Write-Host 'PINNED_FLOW_REACHED'; exit 0 }
function Test-Download {
    if ($args[-1] -notmatch '^https://github.com/example/publisher/releases/download/[^/]+/(siteops-install.zip|siteops-engine.json)(.attestation.jsonl)?$') {
        throw 'Unexpected fixture download.'
    }
    if ($env:TEST_FORBID_DOWNLOAD -eq '1') { throw 'Unexpected fixture download.' }
    [IO.File]::AppendAllText($env:TEST_DOWNLOAD_LOG, ($args[-1] + "`n"))
    $index = [array]::IndexOf($args, '--output')
    if ($index -lt 0) { throw 'Unexpected fixture download arguments.' }
    $name = [IO.Path]::GetFileName($args[-1])
    [IO.File]::Copy((Join-Path $env:TEST_PUBLISHED $name), $args[$index + 1], $false)
    $global:LASTEXITCODE = 0
}
function Test-Verify {
    if ($args[0] -cne 'attestation' -or $args[1] -cne 'verify' -or $env:GH_TELEMETRY -cne 'false') {
        throw 'Unexpected fixture verification.'
    }
    $calls = if (Test-Path -LiteralPath $env:TEST_GH_LOG) { @(Get-Content -LiteralPath $env:TEST_GH_LOG) } else { @() }
    $responses = @((Get-Content -LiteralPath $env:TEST_RESPONSES -Raw | ConvertFrom-Json))
    if ($calls.Count -ge $responses.Count) { throw 'Unexpected fixture verification count.' }
    $reply = $responses[$calls.Count]
    [IO.File]::AppendAllText($env:TEST_GH_LOG, ((ConvertTo-Json -InputObject @($args) -Compress) + "`n"))
    $global:LASTEXITCODE = [int]$reply.exit
    if ($reply.PSObject.Properties['raw']) { return $reply.raw }
    ConvertTo-Json -InputObject @($reply.observations) -Depth 20 -Compress
}
'''


def run_bootstrap(
    tmp_path, shell, responses, *, explicit=False, dry_run=False, content=False,
    local=True, record=None, script_text=None, real_admission=False, local_setup=None, piped=False,
    local_record=None, powershell="powershell.exe",
):
    published = tmp_path / "published"
    published.mkdir()
    (tmp_path / "profile").mkdir()
    (published / "siteops-install.zip").write_bytes(b"synthetic engine archive")
    (published / "siteops-install.zip.attestation.jsonl").write_bytes(b"synthetic proof")
    record = reference() if record is None else record
    (published / "siteops-engine.json").write_bytes(
        (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    (published / "siteops-engine.json.attestation.jsonl").write_bytes(b"synthetic proof")
    reply_path, log = tmp_path / "responses.json", tmp_path / "gh.jsonl"
    download_log = tmp_path / "curl.log"
    reply_path.write_text(json.dumps(responses), encoding="utf-8")
    scripts = ROOT / "scripts" / "bootstrap"
    extension = "sh" if shell == "bash" else "ps1"
    source = script_text or (scripts / f"siteops-bootstrap.{extension}").read_text(encoding="utf-8")
    source_directory = tmp_path / "source" if real_admission else tmp_path
    if real_admission:
        source_directory.mkdir(mode=0o700)
    wrapper = source_directory / f"siteops-bootstrap.{extension}"
    subject = "siteops-engine.json" if content else "siteops-install.zip"
    if local:
        for name in (subject, subject + ".attestation.jsonl"):
            shutil.copyfile(published / name, source_directory / name)
        if local_record is not None:
            (source_directory / subject).write_bytes(
                (json.dumps(local_record, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            )
    env = {
        **os.environ, "TEST_RESPONSES": str(reply_path), "TEST_GH_LOG": str(log),
        "TEST_DOWNLOAD_LOG": str(download_log),
        "TEST_PUBLISHED": str(published), "TEST_FORBID_DOWNLOAD": "1" if dry_run else "0",
        "TEST_PYTHON": str(Path(sys.executable)), "LOCALAPPDATA": str(tmp_path / "profile"),
        "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "state"),
        "GH_TELEMETRY": "true", "SITEOPS_REDACT_OUTPUT": "0",
    }
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                 "PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    if shell == "powershell":
        anchor = "$curl = Native 'curl.exe'\n"
        assert source.count(anchor) == 1
        source = source.replace(anchor, _powershell_overrides() + "\n" + anchor)
        wrapper.write_text(source, encoding="utf-8")
        arguments = [
            powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
            "-ContentRelease" if content else "-Release", "v1.0.0b7",
            "-Repository", REPOSITORY,
        ]
        if explicit:
            arguments += ["-SourceCommit", COMMIT]
        arguments += ["-DryRun"] if dry_run else ["-Yes"]
        if piped:
            arguments = [
                powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "& ([scriptblock]::Create([Console]::In.ReadToEnd()))", *arguments[6:],
            ]
    else:
        # Git Bash exercises the Linux control flow with storage and capability adapters.
        anchor = '[[ "$(uname -s)"'
        assert source.count(anchor) == 1
        source = source.replace(anchor, _bash_overrides(real_admission=real_admission) + "\n" + anchor)
        if not real_admission:
            anchor = 'admit_directory "$data/install-staging" private'
            source = source.replace(anchor, 'keeps_private() { return 0; }\n' + anchor)
        wrapper.write_text(source, encoding="utf-8", newline="\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gh_double = tmp_path / "gh-double.py"
        gh_double.write_text(
            (ROOT / "tests" / "fixtures" / "bootstrap-gh-double.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        write_executable(
            bin_dir / "gh",
            '#!/usr/bin/env bash\nexec "$TEST_PYTHON" -I -S -B "$TEST_GH_DOUBLE" "$@"\n',
        )
        reader = bin_dir / "fixture-python"
        write_executable(reader, '#!/usr/bin/env bash\nset -o pipefail\n"$TEST_PYTHON" "$@" | tr -d \'\\r\'\n')
        write_executable(bin_dir / "curl", r'''#!/usr/bin/env bash
[[ "$TEST_FORBID_DOWNLOAD" != 1 ]] || exit 99
output=""
while (($#)); do
  if [[ "$1" == -o ]]; then output="$2"; shift 2
  elif (($# == 1)); then url="$1"; shift
  else shift; fi
done
[[ -n "$output" && "$url" == https://github.com/example/publisher/releases/download/* ]] || exit 99
printf '%s\n' "$url" >> "$TEST_DOWNLOAD_LOG"
cp -- "$TEST_PUBLISHED/${url##*/}" "$output"
''')
        env.update({
            "TEST_PYTHON": bash_path(Path(sys.executable)), "TEST_GH_DOUBLE": bash_path(gh_double),
            "TEST_READER_PYTHON": bash_path(reader),
            "TEST_PUBLISHED": bash_path(published), "TEST_RESPONSES": bash_path(reply_path),
            "TEST_GH_LOG": bash_path(log), "HOME": bash_path(tmp_path),
            "TEST_DOWNLOAD_LOG": bash_path(download_log),
            "XDG_DATA_HOME": bash_path(tmp_path / "state"),
        })
        # Set PATH inside Bash so Windows path separators cannot select an ambient downloader.
        preamble = f'export PATH="{bash_path(bin_dir)}:$PATH"\n'
        launcher = tmp_path / "launch.sh"
        invocation = 'exec bash -s -- "$@"\n' if piped else f'exec bash "{bash_path(wrapper)}" "$@"\n'
        launcher.write_text(preamble + invocation, encoding="utf-8", newline="\n")
        arguments = [
            str(required_bash()), bash_path(launcher), "--content-release" if content else "--release",
            "v1.0.0b7", "--repository", REPOSITORY,
        ]
        if explicit:
            arguments += ["--source-commit", COMMIT]
        arguments += ["--dry-run"] if dry_run else ["--yes"]
    if local_setup:
        local_setup(source_directory, wrapper, source_directory / subject)

    def snapshot():
        records = {}
        pending = [tmp_path]
        while pending:
            for path in pending.pop().iterdir():
                info = path.lstat()
                key = path.relative_to(tmp_path)
                redirected = stat.S_ISLNK(info.st_mode) or (
                    sys.platform == "win32" and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                )
                if redirected:
                    records[key] = ("link", os.readlink(path))
                elif stat.S_ISDIR(info.st_mode):
                    records[key] = None
                    pending.append(path)
                else:
                    records[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return records

    before = snapshot()
    result = subprocess.run(
        arguments, cwd=tmp_path, env=env, input=source if piped else "", text=True, capture_output=True, timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    after = snapshot()
    writes = {path for path in after.keys() | before.keys() if after.get(path) != before.get(path)
              or (path in after) != (path in before)}
    return result, calls, writes - {log.relative_to(tmp_path)}


def expected_arguments(call, shell, *, pinned, content=False, engine=False):
    subject = call[2]
    commit = ENGINE_COMMIT if engine else COMMIT
    signer = "_release-candidate.yaml" if content else "_siteops-distribution.yaml"
    result = [
        "attestation", "verify", subject, "--bundle", subject + ".attestation.jsonl",
        "--repo", REPOSITORY, "--cert-identity",
        f"https://github.com/{REPOSITORY}/.github/workflows/{signer}@{SOURCE_REF}",
        "--source-ref", SOURCE_REF,
    ]
    if pinned:
        result += ["--source-digest", commit, "--signer-digest", commit]
    result += [
        "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
        "--predicate-type", "https://slsa.dev/provenance/v1", "--hostname", "github.com",
        "--digest-alg", "sha256", "--format", "json",
    ]
    if shell == "bash":
        option = "--jq" if pinned else "--template"
        source = (ROOT / "scripts" / "bootstrap" / "siteops-bootstrap.sh").read_text(encoding="utf-8")
        if pinned:
            query = re.search(r'^  query="(.*)"$', source, re.MULTILINE).group(1)
            for name, value in {
                "signer": result[8], "builder": f"https://github.com/{REPOSITORY}/.github/workflows/{CALLER}@{SOURCE_REF}",
                "repository": REPOSITORY, "expected_commit": commit, "expected_ref": SOURCE_REF,
            }.items():
                query = query.replace("$" + name, value)
            query = query.replace('\\"', '"')
        else:
            query = re.search(r"^  template='(.*)'$", source, re.MULTILINE).group(1)
        result += [option, query]
    return result

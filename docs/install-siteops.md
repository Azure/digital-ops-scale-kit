# Install Site Ops from a release

Install an identified Site Ops build without cloning this repository. The
native installation assets carry the same application bytes:

| Asset | Contents | Use it for |
|---|---|---|
| `siteops-<version>-py3-none-any.whl` | The Site Ops engine wheel | Ordinary installation from a release you already trust |
| `siteops-install.zip` | That identical wheel, pinned runtime dependency wheels, `pylock.toml`, the bundle inventory, and license notices | Verified, index-free installation with a recorded dependency set |

Each asset has its own detached proof, `<asset>.attestation.jsonl`, and the
publishing pipeline signs and cross-checks both. As an operator you use one
asset: install the wheel directly, or download the archive with its proof and
install from the authenticated bundle. Both paths install with stock
[pipx](https://pipx.pypa.io/). Releases that publish
`siteops-bootstrap.sh` and `siteops-bootstrap.ps1` also provide a detached
proof for each script. Both script entry routes run the same platform script
and install from the authenticated archive. Site Ops has no private package
store.

A Site Ops release installs the engine only. Workspace content has its own
source and version. Acquire a compatible workspace from an approved content
release through a [workspace pin](projects.md#run-project-pin), or use a local
checkout. Installing the engine does not acquire or authorize that content.

A release without these assets uses the
[linked Site Ops release](releasing.md#release-content-against-an-existing-engine)
or the [contributor source installation](../CONTRIBUTING.md#development-setup)
path.

## Choose an installation route

The bootstrap scripts support Ubuntu 24.04 x64, managed Azure Linux 3 x64,
and Windows x64. Select an
exact approved release tag and its full source commit. The public release
notes identify both. Do not use a floating branch or `latest` as installation
authority. Both scripts disclose required tool changes and ask for consent.
Use `--yes` on Ubuntu or `-Yes` on Windows only for an explicitly approved
unattended installation. The bootstrap scripts and guided AIO workspace
have not yet been published in official releases. Check each selected
release's asset inventory before using these commands. Azure login and
deployment are separate.

| Route | First script trust | Requirements |
|---|---|---|
| [HTTPS bootstrap](#bootstrap-from-https) | Official HTTPS delivery. The script has not been independently authenticated before it starts. | Supported shell and HTTPS downloader. Missing tools may require an approved package channel and administrator consent. |
| [Verify the bootstrap script](#verify-the-bootstrap-script) | Detached proof, exact publisher, source commit, signing workflow, caller and runner checked before execution. | GitHub CLI 2.95 or newer in version 2 from an approved channel. No GitHub login. |
| [Release wheel](#install-the-release-wheel) or [manual verified bundle](#install-the-verified-bundle) | Separate native installation paths with different provenance and dependency guarantees. | Provision the tools in [Before you start](#before-you-start). |

The HTTPS path is suitable when your policy accepts the official release
endpoint as authority for the initial script. Later verification of the
archive does not retroactively authenticate that script. For publisher
provenance before any installer code runs, select the verified path. A
checksum obtained alongside a script from the same location does not add
independent publisher authentication. The script's approved source setup
is always opt-in.

### Bootstrap from HTTPS

Replace the tag and commit with the pair in your approved release record.
The following downloads the complete script to a new private location
before execution. The release must contain the script asset.

Ubuntu 24.04 or managed Azure Linux 3:

```bash
(
  set -euo pipefail
  tag="<approved-release-tag>"; sha="<full-source-commit>"
  download="$(mktemp -d)"; chmod 700 "$download"
  script="$download/siteops-bootstrap.sh"
  url="https://github.com/Azure/digital-ops-scale-kit/releases/download/${tag//\//%2F}/siteops-bootstrap.sh"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-redirs 3 --max-time 120 --output "$script" "$url" &&
    bash "$script" --release "$tag" --source-commit "$sha" \
      --with-azure-cli --enroll-source official
)
```

If Ubuntu has `wget` but not `curl`, use
`wget --https-only --max-redirect=3 --timeout=120 -O "$script" "$url"`
in place of the `curl` download above, then run the same `bash` command
only when the download succeeds. The script will disclose any required
tool changes, including obtaining `curl` from the approved Ubuntu channel.

Windows PowerShell:

```powershell
& {
  $ErrorActionPreference = "Stop"
  $tag = "<approved-release-tag>"; $sha = "<full-source-commit>"
  $download = Join-Path $env:TEMP ("siteops-bootstrap-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Path $download | Out-Null
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  icacls $download /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "The download directory could not be protected." }
  $script = Join-Path $download "siteops-bootstrap.ps1"
  $url = "https://github.com/Azure/digital-ops-scale-kit/releases/download/$([uri]::EscapeDataString($tag))/siteops-bootstrap.ps1"
  & curl.exe --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
    --tlsv1.2 --max-redirs 3 --max-time 120 --output $script $url
  if ($LASTEXITCODE -ne 0) { throw "The bootstrap script could not be downloaded." }
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script `
    -Release $tag -SourceCommit $sha -WithAzureCli -EnrollSource official
}
```

The PowerShell execution policy setting is scoped to this process. An
organization policy may still prohibit unsigned scripts. Use your approved
managed installation path in that case. Keep the downloaded file to inspect
it, or remove only the private directory you created when finished. The
scripts do not run `gh auth login` or `az login`. They download public assets
anonymously, verify the engine archive and offer source enrollment only
because the command above selects `official`.

Repeating the same selected installation checks the retained bundle before
skipping pipx changes. The script retains the authenticated ZIP and proof
in private user storage for that exact release selection. It rechecks
their proof on repeat without downloading the same assets again. This
uses additional disk space beside the extracted bundle. A different
build or an explicit repair requires
`--replace` on Ubuntu or `-Replace` on Windows. This opts into pipx
`--force`, which can override a pipx pin. Review the selected version,
source commit and existing installation before using it. An interrupted
extraction or changed retained bundle fails for inspection rather than
overwriting the existing directory. The bootstrap does not claim a
transactional rollback.

### Verify the bootstrap script

Install GitHub CLI 2.95 or newer in version 2 through an approved channel
before this route. Ubuntu 24.04's distribution package is older than the
qualified verifier. Download the versioned script and its proof without
executing either one. Neither public asset requires GitHub authentication.
The verification below uses your approved source commit, not an identity
read from the script or proof.

Ubuntu 24.04 or managed Azure Linux 3:

```bash
(
  set -euo pipefail
  tag="<approved-release-tag>"; sha="<full-source-commit>"
  download="$(mktemp -d)"; chmod 700 "$download"
  script="$download/siteops-bootstrap.sh"
  url="https://github.com/Azure/digital-ops-scale-kit/releases/download/${tag//\//%2F}/"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-redirs 3 --max-time 120 --output "$script" "${url}siteops-bootstrap.sh"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-redirs 3 --max-time 120 --output "$script.attestation.jsonl" \
    "${url}siteops-bootstrap.sh.attestation.jsonl"
  repository="Azure/digital-ops-scale-kit"
  source_ref="refs/heads/main"
  signer="https://github.com/$repository/.github/workflows/_siteops-distribution.yaml@$source_ref"
  builder="https://github.com/$repository/.github/workflows/release.yaml@$source_ref"
  query="length > 0 and all(.[]; .verificationResult.mediaType == \"application/vnd.dev.sigstore.verificationresult+json;version=0.1\" and (.verificationResult.signature.certificate | .buildConfigURI == \"$builder\" and .buildConfigDigest == \"$sha\" and .runnerEnvironment == \"self-hosted\"))"
  verified="$(gh attestation verify "$script" --bundle "$script.attestation.jsonl" \
    --repo "$repository" --cert-identity "$signer" --source-ref "$source_ref" \
    --source-digest "$sha" --signer-digest "$sha" \
    --cert-oidc-issuer https://token.actions.githubusercontent.com \
    --predicate-type https://slsa.dev/provenance/v1 --hostname github.com \
    --digest-alg sha256 --format json --jq "$query")"
  [[ "$verified" == true ]] || { echo "Script verification failed." >&2; exit 1; }
  bash "$script" --release "$tag" --source-commit "$sha" \
    --with-azure-cli --enroll-source official
)
```

Windows PowerShell:

```powershell
& {
  $ErrorActionPreference = "Stop"
$tag = "<approved-release-tag>"; $sha = "<full-source-commit>"
$download = Join-Path $env:TEMP ("siteops-bootstrap-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $download | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $download /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The download directory could not be protected." }
$script = Join-Path $download "siteops-bootstrap.ps1"
$url = "https://github.com/Azure/digital-ops-scale-kit/releases/download/$([uri]::EscapeDataString($tag))/"
foreach ($name in @("siteops-bootstrap.ps1", "siteops-bootstrap.ps1.attestation.jsonl")) {
  & curl.exe --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
    --tlsv1.2 --max-redirs 3 --max-time 120 --output (Join-Path $download $name) ($url + $name)
  if ($LASTEXITCODE -ne 0) { throw "A bootstrap asset could not be downloaded." }
}
$repository = "Azure/digital-ops-scale-kit"; $sourceRef = "refs/heads/main"
$signer = "https://github.com/$repository/.github/workflows/_siteops-distribution.yaml@$sourceRef"
$builder = "https://github.com/$repository/.github/workflows/release.yaml@$sourceRef"
$lines = [Collections.Generic.List[string]]::new(); $bytes = 0
$preference = $ErrorActionPreference
try {
  $ErrorActionPreference = "Continue"
  & gh.exe attestation verify $script --bundle "$script.attestation.jsonl" `
    --repo $repository --cert-identity $signer --source-ref $sourceRef `
    --source-digest $sha --signer-digest $sha `
    --cert-oidc-issuer https://token.actions.githubusercontent.com `
    --predicate-type https://slsa.dev/provenance/v1 --hostname github.com `
    --digest-alg sha256 --format json 2>$null | ForEach-Object {
      $bytes += [Text.Encoding]::UTF8.GetByteCount($_) + 1
      if ($bytes -gt 8388608) { throw "Verification evidence is too large." }
      $lines.Add($_)
    }
  $status = $LASTEXITCODE
} finally { $ErrorActionPreference = $preference }
if ($status -ne 0 -or $lines.Count -eq 0) { throw "Script verification failed." }
$observations = @((($lines -join "`n") | ConvertFrom-Json))
if ($observations.Count -lt 1 -or $observations.Count -gt 128) {
  throw "Script verification returned an unsupported result count."
}
$expected = @{
  subjectAlternativeName = $signer
  issuer = "https://token.actions.githubusercontent.com"
  sourceRepositoryURI = "https://github.com/$repository"
  sourceRepositoryDigest = $sha
  sourceRepositoryRef = $sourceRef
  buildSignerDigest = $sha
  buildConfigURI = $builder
  buildConfigDigest = $sha
  runnerEnvironment = "self-hosted"
}
foreach ($item in $observations) {
  $verified = $item.verificationResult
  $certificate = $verified.signature.certificate
  if ($verified -isnot [pscustomobject] -or $certificate -isnot [pscustomobject] -or
      $verified.mediaType -isnot [string] -or
      $verified.mediaType -cne "application/vnd.dev.sigstore.verificationresult+json;version=0.1") {
    throw "Unsupported verified script observation."
  }
  foreach ($key in $expected.Keys) {
    $value = $certificate.PSObject.Properties[$key].Value
    if ($value -isnot [string] -or $value -cne $expected[$key]) {
      throw "The verified script certificate differs from the selected release."
    }
  }
}
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script `
  -Release $tag -SourceCommit $sha -WithAzureCli -EnrollSource official
}
```

Do not change these publisher, workflow or runner values to accommodate a
failed check. The release ZIP has its own detached proof and is verified
again by the authenticated script. Use the manual path below when a
managed environment cannot run this bootstrap. Preinstalled tools that
already meet the supported versions are retained.

### Azure Cloud Shell and Codespaces

Azure Cloud Shell runs managed Azure Linux 3 without `sudo`. Its supported
quick route uses existing `curl`, Python, GitHub CLI and Azure CLI. When
pipx is absent, the script installs it in private user storage. It tries
a real pip-equipped `venv` first, then the installed `virtualenv` module
when needed. The installer makes no OS package changes in this mode and
fails with a remedy if a required tool is missing.

Configure an approved HTTPS Python index in pip settings or `PIP_INDEX_URL`
before any required pipx or shared backend download. The script checks this
configuration without printing the index URL or credentials and rejects
extra indexes, find-links and trusted hosts. The check uses pip's effective
index for each tool installation or backend download, including any
command-specific configuration. It does not silently select
the public default index. Keep package configuration and diagnostic output
private. The Windows bootstrap applies the same check when it provisions
pipx or its shared backend. The configured HTTPS index is an operator
approval, not a publisher identity inferred by the script. Check whether
your Cloud Shell storage persists `$HOME`. An idle
or interrupted session may end a long deployment. Confirm the current Azure
identity and subscription privately before resource reads or deployment.

The Bash bootstrap keeps retained files under
`${XDG_DATA_HOME:-$HOME/.local/share}/siteops`. The directory must be private
to the current user, and its ancestors must not be untrusted or symlinked.
If your XDG data path is shared, select a private user-owned location with
trusted ancestors before installing. The script rejects an unsafe root
before choosing a retained pipx executable or reading cached assets.
The Windows bootstrap checks the same boundary for its
`LOCALAPPDATA\siteops` directory, including ancestor write access and
reparse points, before using retained tools or downloads. Its fixed
`ROOT_PATH`, `ROOT_ANCESTOR_*` and `ROOT_DATA_*` error categories
distinguish a path, ancestor or data directory rejection without
printing the directory or account identity. Inspect the affected
directory and its ACL locally. Select private user storage beneath
trusted ancestors rather than relaxing permissions on shared storage.

The `Azure-Samples/explore-iot-operations` Codespace may use Ubuntu 24.04,
but its base image can change. Check `/etc/os-release` and tool versions in
the actual session. If its `venv` cannot seed pip, an installed `virtualenv`
module is tried before any Ubuntu package change. When pipx uses an
image-managed home, command directory,
or shared backend outside your home, the Bash bootstrap isolates Site Ops
under private user data rather than altering the image's pipx installation.
Sign in to Azure explicitly when needed. Its local k3d cluster is not an
Arc-connected target until you connect it separately with authorization.
For both hosted journeys, installing the CLI is only the first step: obtain
an approved workspace and review a plan before deploying to an existing
Arc-connected cluster.

The script verifies `siteops` inside its child shell and prints its
`Command directory:` on success. If `siteops` is not on the parent shell's
current PATH, open a new shell or add the printed directory for this session:

```bash
export PATH="<printed command directory>:$PATH"
```

```powershell
$env:PATH = "<printed command directory>;" + $env:PATH
```

The printed directory can differ from pipx's default when a managed pipx
home was isolated. Do not assume a fixed `$HOME/.local/bin` location.

## Before you start

Run as your ordinary user rather than as an administrator or with `sudo`, and
keep downloaded files in a private directory.

| Prerequisite | Requirement | Needed for |
|---|---|---|
| Platform | Windows x64, or Linux x64 using glibc 2.17 or newer | Both paths |
| Python | Standard 64-bit CPython 3.10 through 3.14 with a pip-equipped `venv`, or installed `virtualenv` on managed Azure Linux | Both paths |
| pipx | Version 1.17.2, available as `pipx` | Both paths |
| Package feed | An approved index that serves required wheels. The bootstrap also needs it when provisioning pipx or its shared backend. | Release wheel and bootstrap tool downloads |
| pipx backend pip | Version 26.2.1 | Verified bundle path |
| GitHub CLI | Version 2.95.0 or newer in the 2.x release line | Downloading and verifying assets |

Obtain these tools through your organization's managed software channel or their
official instructions:
[Python](https://www.python.org/downloads/),
[pipx](https://pipx.pypa.io/stable/installation/), and
[GitHub CLI](https://cli.github.com/).
They are maintained separately from Site Ops, and some Linux distributions
package Python's `venv` support separately. Prepare them first for the native
installation commands below. The bootstrap scripts inspect and propose tool
changes instead.
Use a maintained patch release of your selected Python minor version. The
installed wheels also enforce their own Python-version requirements.

Installing the CLI does not authenticate to Azure or deploy resources. Obtain
and review workspace content separately, then pass its path with `-w`. Azure
CLI, Bicep, and kubectl requirements depend on the operations you later
select.

## Install the release wheel

This is the ordinary path. It trusts the release channel you download from and
the package feed your environment is configured to use.

```powershell
pipx install "https://github.com/Azure/digital-ops-scale-kit/releases/download/<tag>/siteops-<version>-py3-none-any.whl" `
  --backend pip --fetch-python never --skip-maintenance --app siteops `
  --pip-args "--only-binary=:all: --no-cache-dir"
```

```bash
pipx install "https://github.com/Azure/digital-ops-scale-kit/releases/download/<tag>/siteops-<version>-py3-none-any.whl" \
  --backend pip --fetch-python never --skip-maintenance --app siteops \
  --pip-args "--only-binary=:all: --no-cache-dir"
```

Take the tag and wheel file name from the release page, or use the command the
release notes already print for that build. `--backend pip` and
`--fetch-python never` keep pipx from selecting another backend or downloading
an interpreter, `--skip-maintenance` leaves its shared backend alone, and
`--app siteops` declares the command the installation must provide, so a later
failed replacement keeps the working one. `--only-binary=:all:` keeps
installation to built wheels and never builds a downloaded source distribution.

Runtime dependencies come from the package index your environment is already
configured to use. To name that index in the command instead, add
`--index-url <your approved index>` inside `--pip-args`. Adding `--isolated`
ignores pip environment variables and user configuration, but not global
configuration. An index that your network cannot reach fails the installation,
so do not copy an index URL from another organization's instructions.

pipx records the URL it installed from and takes runtime dependencies from your
configured index. **pipx does not check the publisher's provenance
attestation**, and a checksum published beside a download does not authenticate
its publisher. Use the verified bundle path below when you require that
authentication together with the recorded runtime dependencies. The standalone
wheel's proof is available for independent provenance inspection. The verified
bundle path does not use it.

Replacing an online installation uses the same command with `--force`. Review
any pipx pin first, and confirm the result with `siteops --version`. If the
installation already records a verified lock, `--force` does not clear it.
Use the explicit [installation transition](#select-another-build-repair-or-remove)
instead of an ordinary online replacement request.

## Install the verified bundle

Use this path when the installation must record its complete dependency set, run
without a package index, and start from authenticated publisher provenance.

It needs exactly two files: `siteops-install.zip` and
`siteops-install.zip.attestation.jsonl`. Verifying the archive authenticates
every file inside it, including the engine wheel, so this path never downloads
or verifies the standalone wheel.

### Download the archive and its proof

`gh` works from any directory, so every command below uses an explicit path.

```powershell
& {
    $ErrorActionPreference = "Stop"
    $tag = "<tag>"
    $download = Join-Path $env:TEMP ("siteops-download-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $download | Out-Null
    gh release download $tag --repo Azure/digital-ops-scale-kit `
      --pattern "siteops-install.zip" `
      --pattern "siteops-install.zip.attestation.jsonl" `
      --dir $download
    if ($LASTEXITCODE -ne 0) { throw "The release assets could not be downloaded." }
    $download
}
```

```bash
(
  set -euo pipefail
  umask 077
  tag="<tag>"
  download="$(mktemp -d)"
  gh release download "$tag" --repo Azure/digital-ops-scale-kit \
    --pattern "siteops-install.zip" \
    --pattern "siteops-install.zip.attestation.jsonl" \
    --dir "$download"
  printf '%s\n' "$download"
)
```

The parentheses and the `& { ... }` block keep `set -euo pipefail` and
`$ErrorActionPreference` scoped to the download, so your interactive shell keeps
its own settings.

### Authenticate and retain the bundle

Confirm the release tag and its full source commit in the official repository
first, and keep that commit as the expected identity. The repository, signing
workflow, calling workflow, runner class and expected commit are trust decisions: take them from the official
repository and this guidance, never from a downloaded manifest or a command
supplied inside the archive.

Run the block for your shell to verify the archive and then extract it.
Verification failure stops the block before extraction. The destination is
a new private directory named for the archive digest. Keep that directory:
pipx records the lock and wheel paths for later repair and replacement.

```powershell
& {
    $ErrorActionPreference = "Stop"
    $download = "<download directory>"
    $sourceSha = "<full source commit from the selected official release>"
    $repository = "Azure/digital-ops-scale-kit"
    $sourceRef = "refs/heads/main"
    $signer = "https://github.com/$repository/.github/workflows/_siteops-distribution.yaml@$sourceRef"
    $builder = "https://github.com/$repository/.github/workflows/release.yaml@$sourceRef"
    $archive = Join-Path $download "siteops-install.zip"
    $lines = [Collections.Generic.List[string]]::new()
    $bytes = 0
    gh attestation verify $archive `
      --bundle "$archive.attestation.jsonl" `
      --repo $repository --cert-identity $signer --source-ref $sourceRef `
      --source-digest $sourceSha `
      --signer-digest $sourceSha `
      --cert-oidc-issuer "https://token.actions.githubusercontent.com" `
      --predicate-type "https://slsa.dev/provenance/v1" `
      --hostname github.com --digest-alg sha256 --format json | ForEach-Object {
        $bytes += [Text.Encoding]::UTF8.GetByteCount($_) + 1
        if ($bytes -gt 8388608) { throw "Verification evidence exceeds its byte limit." }
        $lines.Add($_)
      }
    if ($LASTEXITCODE -ne 0) { throw "Verification failed. Do not extract this archive." }
    $raw = $lines -join "`n"
    if (-not $raw.TrimStart().StartsWith("[")) { throw "Expected an array of verified observations." }
    $results = @($raw | ConvertFrom-Json)
    if ($results.Count -lt 1 -or $results.Count -gt 128) { throw "Verification evidence is empty or oversized." }
    $expected = @{
        subjectAlternativeName = $signer
        issuer = "https://token.actions.githubusercontent.com"
        sourceRepositoryURI = "https://github.com/$repository"
        sourceRepositoryDigest = $sourceSha
        sourceRepositoryRef = $sourceRef
        buildSignerDigest = $sourceSha
        buildConfigURI = $builder
        buildConfigDigest = $sourceSha
        runnerEnvironment = "self-hosted"
    }
    foreach ($result in $results) {
        $verified = $result.verificationResult
        $certificate = $verified.signature.certificate
        if ($verified -isnot [pscustomobject] -or $certificate -isnot [pscustomobject] -or
            $verified.mediaType -isnot [string] -or
            $verified.mediaType -cne "application/vnd.dev.sigstore.verificationresult+json;version=0.1") {
            throw "Unsupported verified observation."
        }
        foreach ($key in $expected.Keys) {
            $value = $certificate.PSObject.Properties[$key].Value
            if ($value -isnot [string] -or $value -cne $expected[$key]) {
                throw "The verified certificate does not match the selected release policy."
            }
        }
    }
    $bundleId = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $bundle = Join-Path $env:LOCALAPPDATA "siteops\bundles\$bundleId"
    if (Test-Path -LiteralPath $bundle) { throw "That bundle directory already exists. Use the retained bundle or choose a new private location." }
    New-Item -ItemType Directory -Path (Split-Path $bundle) -Force | Out-Null
    New-Item -ItemType Directory -Path $bundle | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls $bundle /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The bundle directory could not be protected." }
    Expand-Archive -LiteralPath $archive -DestinationPath $bundle
    $bundle
}
```

```bash
(
  set -euo pipefail
  umask 077
  download="<download directory>"
  source_sha="<full source commit from the selected official release>"
  repository="Azure/digital-ops-scale-kit"
  source_ref="refs/heads/main"
  signer="https://github.com/$repository/.github/workflows/_siteops-distribution.yaml@$source_ref"
  builder="https://github.com/$repository/.github/workflows/release.yaml@$source_ref"
  archive="$download/siteops-install.zip"
  verification="$(mktemp)"
  trap 'rm -f "$verification"' EXIT
  timeout --kill-after=5 120 gh attestation verify "$archive" \
    --bundle "$archive.attestation.jsonl" \
    --repo "$repository" --cert-identity "$signer" --source-ref "$source_ref" \
    --source-digest "$source_sha" \
    --signer-digest "$source_sha" \
    --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
    --predicate-type "https://slsa.dev/provenance/v1" \
    --hostname github.com --digest-alg sha256 --format json \
    | head -c 8388609 > "$verification"
  python3 -B -c '
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_bytes()
if len(raw) > 8388608:
    raise SystemExit("Verification evidence exceeds its byte limit.")
expected = {
    "subjectAlternativeName": sys.argv[5],
    "issuer": "https://token.actions.githubusercontent.com",
    "sourceRepositoryURI": "https://github.com/" + sys.argv[2],
    "sourceRepositoryDigest": sys.argv[3], "sourceRepositoryRef": sys.argv[4],
    "buildSignerDigest": sys.argv[3], "buildConfigURI": sys.argv[6],
    "buildConfigDigest": sys.argv[3], "runnerEnvironment": "self-hosted",
}
try:
    results = json.loads(raw.decode("utf-8"))
    if not isinstance(results, list) or not 1 <= len(results) <= 128:
        raise ValueError()
    for result in results:
        verified = result["verificationResult"]
        certificate = verified["signature"]["certificate"]
        if (
            verified["mediaType"] != "application/vnd.dev.sigstore.verificationresult+json;version=0.1"
            or any(certificate.get(key) != value for key, value in expected.items())
        ):
            raise ValueError()
except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
    raise SystemExit("The verified certificate does not match the selected release policy.") from None
' "$verification" "$repository" "$source_sha" "$source_ref" "$signer" "$builder"
  bundle_id="$(sha256sum "$archive" | cut -d ' ' -f 1)"
  bundle="${XDG_DATA_HOME:-$HOME/.local/share}/siteops/bundles/$bundle_id"
  mkdir -p "$(dirname "$bundle")"
  mkdir "$bundle"
  python3 -B -m zipfile -e "$archive" "$bundle"
  printf '%s\n' "$bundle"
)
```

These commands authenticate the ZIP and all of its contents. You do not need
the standalone wheel or its proof for this path. The commands enforce official
builds from `main`, so a build from a fork does not satisfy that policy. For an
explicitly selected preview, use its repository, source ref, source commit
and calling workflow consistently. CI previews use `ci.yaml` as the caller,
while published releases use `release.yaml`. Use the engine release's source commit
when a content release links to a separate Site Ops release.

GitHub CLI validates the signing chain, the expected workflow and repository
identity, the source and signer commits, and the file digest. The commands
also compare each verified certificate with the exact caller and `self-hosted`
runner class required by this publisher. That class does not identify a pool
or establish its image and isolation controls. `--bundle` reads
the downloaded proof instead of the attestation API, although the default
trusted-root refresh can still use the network. Verification establishes origin
and integrity. It does not promise that the selected build is free of defects,
and fully offline verification requires independently provisioned trusted
signing roots.

`icacls` and `umask` keep the extracted files private without any Site Ops cache
manager. Windows inherits ACLs, so choose a location protected for your account.
These commands do not rewrite permissions on directories that already exist. The
downloaded archive and its proof remain yours: keep them for later
re-verification, or remove them once the bundle directory is in place.

### Provision the pipx backend that reads the lock

pipx installs packages with a shared backend of its own, not with your shell's
`python -m pip`. The verified path needs a backend that reads `pylock.toml`,
which pip gained in 26.1 and **still labels experimental** in the qualified
26.2.1 release.

This is a separate tooling operation, and it changes the shared pip backend
used by your other pipx applications too. Obtain organizational approval when
that tooling is centrally managed. The download uses your approved package
feed. It happens before index-free Site Ops installation.

```powershell
& {
    $ErrorActionPreference = "Stop"
    $tools = Join-Path $env:TEMP ("siteops-tools-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tools | Out-Null
    python -m pip download "pip==26.2.1" --no-deps --only-binary=:all: --dest $tools
    if ($LASTEXITCODE -ne 0) { throw "The pip backend wheel could not be downloaded." }
    $wheelhouse = ([UriBuilder]::new("file", "", -1, $tools)).Uri.AbsoluteUri
    pipx upgrade-shared `
      --pip-args "--no-index --only-binary=:all: --no-cache-dir --force-reinstall --find-links=$wheelhouse"
    if ($LASTEXITCODE -ne 0) { throw "The pipx backend could not be provisioned." }
}
```

```bash
(
  set -euo pipefail
  umask 077
  tools="$(mktemp -d)"
  python3 -m pip download "pip==26.2.1" --no-deps --only-binary=:all: --dest "$tools"
  wheelhouse="$(python3 -B -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$tools")"
  pipx upgrade-shared \
    --pip-args "--no-index --only-binary=:all: --no-cache-dir --force-reinstall --find-links=$wheelhouse"
)
```

`--force-reinstall` selects the downloaded backend even if another version is
already installed. Download the wheel with `--require-hashes` and a hash-pinned
requirements file when your policy demands hash-pinned tooling. `pipx upgrade-shared` is the
supported way to change that backend. Never edit pipx's shared libraries or its
metadata by hand.

### Install from the verified lock

```powershell
$bundle = "<retained bundle directory printed by the extraction step>"
pipx install siteops --lock "$bundle\pylock.toml" `
  --backend pip --fetch-python never --skip-maintenance --app siteops `
  --pip-args "--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir"
```

```bash
bundle="<retained bundle directory printed by the extraction step>"
pipx install siteops --lock "$bundle/pylock.toml" \
  --backend pip --fetch-python never --skip-maintenance --app siteops \
  --pip-args "--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir"
```

`--lock` selects the producer's recorded dependency
set, `--backend pip` and `--fetch-python never` keep pipx from selecting another
backend or downloading an interpreter, `--skip-maintenance` leaves the backend
you provisioned in place, and `--app siteops` states the expected command. The
pip policy ignores pip environment variables and user configuration, requires
a recorded hash for every file, forbids any index, accepts only built wheels,
and writes no cache.

Once installed, `pipx runpip siteops --version` reports the pip backend used
by that application. This is distinct from the pip in your calling shell.

## Use the installed CLI

```text
siteops --version
siteops --help
```

The package and command report the exact artifact version. Versioned releases
use their declared source package version, and identified builds add a suffix,
for example `1.0.0b1+build.12345.1.gabcdef123456`. The engine version and Scale
Kit content version remain separate identities.

If the command is not found, add pipx's command directory to `PATH`, for example
with `pipx ensurepath`, and open a new terminal. A successful installation
message is not proof that `PATH` resolves to that command: check
`siteops --version` after any installation change.

Use `siteops -w <workspace>` with your local content. See
[site configuration](site-configuration.md) and
[manifest reference](manifest-reference.md) for the deployment model.

## Select another build, repair, or remove

`<bundle>` is the stable directory of the authenticated build you selected.
Windows uses `\` in that path.

| Task | Command |
|---|---|
| Confirm or install the selected build | `pipx install siteops --lock <bundle>/pylock.toml --backend pip --fetch-python never --skip-maintenance --app siteops --pip-args "--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir"` |
| Upgrade, downgrade, or repair | The same command with `--force` |
| Remove Site Ops | `pipx uninstall siteops` |
| Move an online installation to a verified one | The verified command with `--force` |
| Move a verified installation to an online one | `pipx manifest sync <manifest>.toml --backend pip --skip-maintenance` |
| Prevent unattended upgrades | `pipx pin siteops` |

Repeating the install request for the build that is already present succeeds and
does not replace the existing environment. To select a different bundle,
including another build, use `--force` deliberately. Repair also uses `--force`
with the bundle you intend to end up with. Site Ops never uninstalls first and
never edits pipx metadata to change installation state.

A recorded lock persists until you deliberately replace it. Select another
authenticated lock with `--force` to change the locked build. Leaving the locked
state is an explicit `pipx manifest sync` with a lock-free manifest that names
the release wheel URL you want. That command takes no `--pip-args`, so
configure its approved index and `PIP_ONLY_BINARY=:all:` through your
environment. Keep `PIPX_FETCH_PYTHON=never` when automatic interpreter
acquisition is not permitted. This manifest is a pipx tool declaration, not a
dependency lock: only the producer generates `pylock.toml`, and you never write
or edit one.

```toml
[project]
name = "siteops-installation"
version = "1"
dependencies = []

[dependency-groups]
siteops = ["siteops @ https://github.com/Azure/digital-ops-scale-kit/releases/download/<tag>/siteops-<version>-py3-none-any.whl"]

[tool.pipx]
version = "1.0"

[tool.pipx.tools.siteops]
apps = ["siteops"]
```

Installation behavior:

- pipx preserves the previous environment when an installation command reports
  a failure. Confirm the exposed command with `siteops --version` before
  continuing.
- A changed or truncated wheel fails the hash policy and cannot become the
  selected installation.
- Explicit `--force` overrides `pipx pin siteops`. A pin stops routine upgrades,
  not a deliberate replacement.
- pipx refuses `pipx inject` into a locked environment, so a verified
  installation keeps exactly the payload the producer recorded.
- An unrelated command already at pipx's command path is never overwritten, and
  pipx still reports the package installation as successful. Confirm
  `siteops --version` rather than the installation message.
- Replacement uses the existing environment's interpreter. Changing the Python
  interpreter is a separate pipx operation.
- Interruption is not a transaction. Forced process termination, power loss, or
  storage failure is not a guaranteed rollback.

## Retained files and private diagnostics

| Platform | Suggested bundle root |
|---|---|
| Windows | `%LOCALAPPDATA%\siteops\bundles\<bundle-id>` |
| Linux | `$XDG_DATA_HOME/siteops/bundles/<bundle-id>`, or `~/.local/share/siteops/bundles/<bundle-id>` |

Keep a bundle directory while an installation refers to it. A repair reads
the recorded lock and its wheels again. Missing, moved, or modified files
prevent that repair. Restore the directory, or download, verify, and extract
the release again into a new bundle directory.

pipx owns its environments, command directory, and logs. Existing `PIPX_HOME`,
`PIPX_BIN_DIR`, `PIPX_MAN_DIR`, `PIPX_COMPLETION_DIR`, and `PIPX_SHARED_LIBS`
selections are honored. Its logs can contain local paths and environment detail,
so keep them private and review them before attaching them to a report.

## Common problems

| Symptom | Action |
|---|---|
| Attestation verification fails | Stop before extracting or installing. Confirm the selected release, source commit, both files, and a trusted GitHub CLI installation. |
| `pylock.toml` is rejected or reported as unsupported | The pipx backend is older than pip 26.1. Provision the qualified backend as described above, then retry. |
| No matching distribution during a release wheel installation | The configured feed does not serve a required runtime wheel for this interpreter. Use the verified bundle, which carries them. |
| `pipx` is not found | Install supported pipx through a trusted channel and make its command available in `PATH`. |
| The installed build is not the one you selected | Repeat the verified command for the intended bundle with `--force`. |
| `siteops` runs an unexpected program | Another command with that name is earlier in `PATH`. Resolve the ownership of that command before retrying. |
| Windows reports `WinError 206` or cannot launch the installed command from a deeply nested path | Use a shorter pipx state location for a fresh installation. Preserve existing environments rather than moving them, because their launchers record interpreter paths. |
| A locked installation refuses a named request | That is the expected transition policy. Use `--force` with the verified lock, or an explicit manifest sync to leave the locked state. |

## Supported targets

Each Site Ops release qualifies both installation paths on Windows and Linux
across CPython 3.10 through 3.14 before publication. PyPy, free-threaded Python,
musl-based Linux, macOS, and ARM are outside the supported matrix.

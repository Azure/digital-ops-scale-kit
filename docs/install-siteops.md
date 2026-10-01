# Install Site Ops from a release

Install an identified Site Ops build without cloning this repository. The
native installation assets carry the same application bytes:

| Asset | Contents | Use it for |
|---|---|---|
| `siteops-<version>-py3-none-any.whl` | The Site Ops engine wheel | Ordinary installation from a release you already trust |
| `siteops-install.zip` | That identical wheel, pinned runtime dependency wheels, `pylock.toml`, the shared `siteops-install.py` helper, the bundle inventory, and license notices | Verified, index-free installation with a recorded dependency set |

Each asset has its own detached proof, `<asset>.attestation.jsonl`, and the
publishing pipeline signs and cross-checks both. As an operator you use one
asset: install the wheel directly, or download the archive with its proof and
install from the authenticated bundle. Both routes install with
stock [uv](https://docs.astral.sh/uv/), using its normal tool and Python
locations and native removal command.
Releases that publish
`siteops-bootstrap.sh` and `siteops-bootstrap.ps1` also provide a detached
proof for each script. Both script entry routes run the same platform script
and install from the authenticated archive. Site Ops has no private package
store.

Use bootstrap scripts and the engine ZIP from the same release. Current
scripts require the helper in that ZIP. They verify the archive's provenance
before extracting the helper to fresh protected storage and running it.
The helper does not select the publisher or authorize its own archive.

A Site Ops release installs the engine only. Workspace content has its own
source and version. Select a compatible approved release directly with
`--source SOURCE@RELEASE` in the [guided deployment](guided-inputs.md),
use a [workspace pin](projects.md#run-project-pin) for repeatable fleets,
or use a local checkout. Installing the engine does not acquire or authorize
that content.

A release without these assets uses the
[linked Site Ops release](releasing.md#release-content-against-an-existing-engine)
or the [contributor source installation](../CONTRIBUTING.md#development-setup)
path.

## Choose an installation route

The bootstrap scripts support Ubuntu 24.04 x64, managed Azure Linux 3 x64,
and Windows x64. Select an
exact approved release. Its release notes provide complete commands with the
tag, source commit, publisher and script digest already filled in.
Do not use a floating branch or `latest` as installation
authority. Both scripts disclose required tool changes and ask for consent.
Use `--yes` on Ubuntu or `-Yes` on Windows only for an explicitly approved
unattended installation. Use these routes with releases that contain the
bootstrap and compatible workspace assets. Check the selected release's
asset inventory before using these commands. Azure login and
deployment are separate.

| Route | First script trust | Requirements |
|---|---|---|
| [HTTPS bootstrap](#bootstrap-from-https) | Official HTTPS delivery. The script has not been independently authenticated before it starts. | Supported shell and HTTPS downloader. Missing tools may require an approved package channel and administrator consent. |
| [Verify the bootstrap script](#verify-the-bootstrap-script) | Detached proof, exact publisher, source commit, signing workflow, caller and runner checked before execution. | GitHub CLI 2.95 or newer in version 2 from an approved channel. No GitHub login. |
| [Release wheel](#install-the-release-wheel) | Approved release channel and dependency feed. Native uv does not verify the detached proof. | uv from an approved channel and a configured package feed. |

Managed environments can [provision approved tools first](#before-you-start),
then use the same verified bootstrap. This keeps one bundle verification
and installation path.

The HTTPS path is suitable when your policy accepts the official release
endpoint as authority for the initial script. Later verification of the
archive does not retroactively authenticate that script. For publisher
provenance before any installer code runs, select the verified path. A
checksum obtained alongside a script from the same location does not add
independent publisher authentication. The script's approved source setup
is always opt-in.

### Bootstrap from HTTPS

Copy the complete Bash or PowerShell command from your approved release's
`Install Site Ops` section. It downloads the script fully, checks the exact
size and SHA-256 from the reviewed release instructions, then runs it from
a fresh private directory. A failed download or mismatch stops execution.
The command removes its temporary script when it finishes.

Run it from a trusted user shell with the normal protected temporary
directory. The generated command installs the engine only. It asks before
tool changes and leaves Azure authentication and source enrollment separate.

For an approved selection assembled manually, the following templates also
request Azure CLI and explicitly enroll the official content source. Replace
the tag and commit with the pair from that release. These templates rely on
HTTPS delivery without the additional digest check in the generated commands.

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
skipping native tool changes. The script retains the authenticated ZIP and proof
in private user storage for that exact release selection. It rechecks
their proof on repeat without downloading the same assets again. This
uses additional disk space beside the extracted bundle. A different
build or an explicit repair requires
`--replace` on Ubuntu or `-Replace` on Windows. This opts into native
uv replacement or repair in ordinary shared uv tool storage. Review the
selected version, source commit and existing installation before using it. An interrupted
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
again by the authenticated script. In a managed environment,
[provision approved tools first](#before-you-start). Preinstalled tools that
already meet the supported versions are retained.

### Azure Cloud Shell and Codespaces

Azure Cloud Shell runs managed Azure Linux 3 without `sudo`. Its route uses
existing OS tools such as `curl`, GitHub CLI and Azure CLI. The bootstrap
acquires pinned native uv when needed and provisions uv-managed Python,
without requiring a system Python, pipx or virtualenv installation.
It makes no OS package changes in this mode and fails with a remedy if a
required OS tool is missing.

The authenticated application uses only bundled wheels and no package index.
Runtime downloads use uv's verified catalog and system certificates, or an
explicitly configured HTTPS `UV_PYTHON_INSTALL_MIRROR`. Keep configuration
and diagnostics private. uv does not read pip configuration. The ordinary
online wheel route separately uses your approved uv package index.
Check whether your Cloud Shell storage persists `$HOME`. An idle
or interrupted session may end a long deployment. Confirm the current Azure
identity and subscription privately before resource reads or deployment.

The Bash bootstrap keeps retained files under
`${XDG_DATA_HOME:-$HOME/.local/share}/siteops`. The directory must be private
to the current user, and its ancestors must not be untrusted or symlinked.
If your XDG data path is shared, select a private user-owned location with
trusted ancestors before installing. The script rejects an unsafe root
before choosing a retained uv executable or reading cached assets.
The Windows bootstrap checks the same boundary for its
`LOCALAPPDATA\siteops` directory, including ancestor write access and
reparse points, before using retained tools or downloads. It also
checks the complete path and ACL of selected uv tools, ordinary uv storage
and concrete uv-managed Python before running them. Existing qualified uv
0.12.20 is reused when its executable bytes and path pass admission.
Otherwise the script acquires the checksum-pinned native archive into a
protected Site Ops tooling cache without changing another uv installation.
When uv is absent it exposes a pinned `uv.exe` in your ordinary command
directory for later `uv tool uninstall siteops`. It does not install Python
with WinGet, create Python aliases or register it globally. Explicit
`UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` and `UV_PYTHON_INSTALL_DIR` are honored
after path admission. The runtime download uses uv's trusted system
certificates or an explicitly configured HTTPS
`UV_PYTHON_INSTALL_MIRROR`. The authenticated application installation
uses only bundled wheels and no package index.
An otherwise private data root does not make an existing writable
child directory or executable safe. Its fixed
`ROOT_PATH`, `ROOT_ANCESTOR_*` and `ROOT_DATA_*` error categories
distinguish a path, ancestor or data directory rejection. `TOOL_*`
identifies a selected executable or its parent. Neither prints the
directory or account identity. Inspect the affected directory and
its ACL locally. Select private user storage beneath trusted
ancestors rather than relaxing permissions on shared storage.
The Windows bootstrap requires a regular copied `siteops.exe` whose bytes
match the selected environment's executable. It rejects file symlinks,
redirected directories, unrelated commands and changed launcher bytes
before running the command.

The `Azure-Samples/explore-iot-operations` Codespace may use Ubuntu 24.04,
but its base image can change. Check `/etc/os-release` and tool versions in
the actual session. The bootstrap preserves another uv installation and
uses a pinned executable when its selected version is not qualified.
Application and Python directories remain ordinary uv storage, including
explicit directory selections that pass admission. Directories writable
by another user or group are refused rather than having their permissions
changed automatically. Select protected storage after reviewing the access
needed by other applications.
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

The printed directory can differ when a native manager location was
explicitly selected. Do not assume a fixed command directory.

## Before you start

Run as your ordinary user rather than as an administrator or with `sudo`, and
keep downloaded files in a private directory. The bootstrap can provision
missing prerequisites after consent. Use your organization's approved channels
to provision them first when software installation is centrally managed.

| Prerequisite | Requirement | Needed for |
|---|---|---|
| Platform | Windows x64, Ubuntu 24.04 x64 or managed Azure Linux 3 x64 | Bootstrap |
| Native manager | uv 0.12.20 from an approved channel | Both routes |
| Python | Managed CPython 3.11.16 for a fresh bootstrap. uv can provision it without a system Python installation. | Both routes |
| Package feed | An approved index that serves the required runtime wheels. Configure it in uv. | Online release wheel |
| GitHub CLI | Version 2.95.0 or newer in the 2.x release line | Detached proof verification |

Obtain these tools through your organization's managed software channel or their
official instructions:
[uv](https://docs.astral.sh/uv/getting-started/installation/),
[managed Python](https://docs.astral.sh/uv/guides/install-python/), and
[GitHub CLI](https://cli.github.com/).
The bootstrap preserves another uv installation and uses an admitted qualified
executable or a pinned tooling copy. The application and runtime remain in
normal uv storage. It does not borrow Azure CLI's interpreter or change global
Python aliases.

Installing the CLI does not authenticate to Azure or deploy resources. Review
workspace content separately, then select it with `--source SOURCE@RELEASE`,
an operator project or `-w`. Azure CLI, Bicep and kubectl requirements depend
on the operations you select.

## Install the release wheel

This is the ordinary path. It trusts the release channel you download from and
the package feed your environment is configured to use.

With uv available, use the exact release wheel on either platform. uv owns the
managed Python runtime and ordinary tool locations. Set an approved
`UV_DEFAULT_INDEX` for dependency downloads if your organization requires a
private feed. uv does not read pip's index configuration.

```powershell
uv tool install "https://github.com/Azure/digital-ops-scale-kit/releases/download/<tag>/siteops-<version>-py3-none-any.whl" `
  --python 3.11.16 --managed-python --no-build --system-certs
```

```bash
uv tool install "https://github.com/Azure/digital-ops-scale-kit/releases/download/<tag>/siteops-<version>-py3-none-any.whl" \
  --python 3.11.16 --managed-python --no-build --system-certs
```

Use the exact command printed in the release notes to avoid assembling the
tag and wheel filename yourself. `--no-build` requires built wheels rather
than executing downloaded source builds.

Runtime dependencies come from the package index your environment is already
configured to use. To name that index in the command instead, add
`--default-index <your approved index>`. An unreachable index fails the
installation. Use your own approved configuration rather than another
organization's index URL.

Native uv does not verify the publisher's detached attestation. Use the
verified bundle path when you require independent publisher authentication
and the producer's complete recorded dependency set. The standalone wheel's
proof remains available for independent inspection. The verified bootstrap
instead authenticates the ZIP containing that same wheel.

Replacing or repairing an online installation uses the same exact wheel
command with `--reinstall`. Confirm the result with `siteops --version`.
Review the [installation transitions](#select-another-build-repair-or-remove)
before switching between online and verified routes.

## Install the verified bundle

Use either bootstrap entry above. Both authenticate `siteops-install.zip`
against its detached proof before extracting the installer helper. For
provenance before the first script runs, choose
[Verify the bootstrap script](#verify-the-bootstrap-script).
An environment with centrally provisioned tools uses that same entry.

The ZIP contains the engine wheel, all recorded runtime dependency wheels,
`pylock.toml` and the shared installer helper. The helper checks the complete
payload before calling native uv with offline, no-index and no-build options,
then checks the installed application bytes and runtime binding. A raw
`uv tool install --with-requirements pylock.toml` command is not a substitute
for those checks. Keep the producer's lock unchanged.

The bootstrap retains protected release files for repeat or repair requests.
It runs a fresh helper from the authenticated archive, not an unchecked
retained copy. You do not separately download the standalone wheel or its
proof for this route.

### Publisher and managed-environment policy

The expected repository, source ref, source commit, signing workflow, caller
and runner class come from your approved release selection. Downloaded
metadata cannot choose them. The official scripts require the exact
`_siteops-distribution.yaml` signer and `release.yaml` caller at the selected
commit, with `self-hosted` provenance. This class does not identify a
particular runner pool. An explicitly selected CI preview uses its own
repository, source ref, commit and `ci.yaml` caller. It does not qualify as
an official release.

GitHub CLI verifies the signing chain and file digest. The bootstrap also
checks the source, signer, caller and runner fields before extraction.
`--bundle` reads the detached proof without a GitHub login. Trusted-root
refresh can still use the network. Verification establishes origin and
integrity, not the absence of defects.

Application installation uses only admitted bundle wheels, but the bootstrap
is not a fully offline installer. Runtime or tool acquisition, release
downloads and trusted-root refresh can require network access. A managed
environment must approve those channels and storage locations before use.
If its policy cannot permit them, stop and use an independently approved
managed distribution rather than bypassing verification.

## Use the installed CLI

```text
siteops --version
siteops --help
```

The package and command report the exact artifact version. Versioned releases
use their declared source package version, and identified builds add a suffix,
for example `1.0.0b1+build.12345.1.gabcdef123456`. The engine version and Scale
Kit content version remain separate identities.

If the command is not found, use `uv tool update-shell` to add uv's command
directory to `PATH`, then open a new terminal. A successful installation
message is not proof that `PATH` resolves to that command: check
`siteops --version` after any installation change.

Continue with the [direct guided deployment](guided-inputs.md), or use
`siteops -w <workspace>` with local content. The same
[Site configuration](site-configuration.md) and
[manifest model](manifest-reference.md) support retained projects and fleets.

## Select another build, repair, or remove

Select the intended release and its complete command before changing the
installation. Neither route follows `latest` automatically.

| Task | Command |
|---|---|
| Confirm a verified installation | Rerun the same release's bootstrap command. |
| Upgrade, downgrade or repair a verified installation | Add `--replace` to the final Bash script invocation or `-Replace` to the PowerShell invocation. |
| Upgrade, downgrade or repair an online installation | Rerun the exact wheel command with `--reinstall`. |
| Move an online uv installation to a verified one | Use the selected bootstrap with `--replace` or `-Replace`. |
| Move a verified installation to an online one | Review the different dependency and provenance guarantees, then use the exact wheel command with `--reinstall`. |
| Remove Site Ops | `uv tool uninstall siteops` |

The bootstrap leaves a matching, validated installation unchanged.
Replacement uses native uv and the selected admitted runtime. It does not
edit uv's environment or metadata by hand. Keep tool maintenance and Python
runtime maintenance separate from application replacement.
A missing or inconsistent runtime binding requires inspection and native
`uv tool uninstall siteops` before a fresh installation. `--replace` and
`-Replace` repair the selected application, not an invalid runtime binding.

Installation behavior:

- A changed or truncated bundle payload is refused before native installation.
- An unrelated exposed command is preserved. Remove it with its owning manager
  only after inspection, then rerun the selected installation.
- If an older pipx installation owns `siteops`, use `pipx uninstall siteops`
  after inspection, then install the approved release with uv. Do not remove
  unrelated pipx tools or shared uv storage.
- Unknown Python startup files stop a verified replacement. Inspect the tool
  before using `uv tool uninstall siteops`, then reinstall from the approved
  release. Installation does not silently remove those files.
- Native `uv tool upgrade` and an online reinstall do not perform the
  bootstrap's publisher and payload checks. Use the verified entry for
  verified maintenance.
- Confirm `siteops --version` and command ownership after a reported failure.
- Interruption is not a transaction. Forced process termination, power loss, or
  storage failure is not a guaranteed rollback.

## Retained files and private diagnostics

| Platform | Bootstrap data root |
|---|---|
| Windows | `%LOCALAPPDATA%\siteops` |
| Linux | `$XDG_DATA_HOME/siteops`, or `~/.local/share/siteops` |

Keep retained bundles and their recorded wheel/lock paths intact. The bootstrap
rechecks retained state. Inspect a mismatch rather than overwriting the
directory. Rerun the approved bootstrap selection to acquire missing assets.

uv owns application environments, Python installations and command exposure.
The bootstrap honors admitted `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` and
`UV_PYTHON_INSTALL_DIR` locations. These may be shared with other uv tools.
Uninstalling Site Ops does not authorize deleting shared uv storage, runtimes,
tooling or source approvals. Diagnostics can contain local paths and
environment detail. Keep them private and review them before sharing.

## Common problems

| Symptom | Action |
|---|---|
| Attestation verification fails | Stop before extracting or installing. Confirm the selected release, source commit, both files, and a trusted GitHub CLI installation. |
| No matching distribution during a release wheel installation | The configured feed does not serve a required runtime wheel for this interpreter. Use the verified bundle, which carries them. |
| `uv` is not found | Use the bootstrap, or provision the qualified uv through an approved channel. |
| The installed build is not the one you selected | Use the intended release's bootstrap with `--replace` or `-Replace`, then check command ownership and version. |
| `siteops` runs an unexpected program | Another command with that name is earlier in `PATH`. Resolve the ownership of that command before retrying. |
| The Windows bootstrap refuses a retained uv executable or Python directory | Inspect the selected path, ACL and pinned bytes before retrying. It preserves another uv installation and will not overwrite an unrelated command. |
| The tool has unrecognized Python startup files | Inspect the environment before using native uninstall. Rerun the approved bootstrap after removing the tool with its manager. |
| A retained bundle fails validation | Preserve it for private inspection. Acquire the selected release again in an approved private location rather than editing the lock or wheels. |

## Supported targets

Each Site Ops release qualifies both installation paths on Windows and Linux
across CPython 3.10 through 3.14 before publication. PyPy, free-threaded Python,
musl-based Linux, macOS, and ARM are outside the supported matrix.

# Install Site Ops from a release

Install an identified Site Ops build without cloning this repository. A release
publishes two assets that carry the same application bytes:

| Asset | Contents | Use it for |
|---|---|---|
| `siteops-<version>-py3-none-any.whl` | The Site Ops engine wheel | Ordinary installation from a release you already trust |
| `siteops-install.zip` | That identical wheel, pinned runtime dependency wheels, `pylock.toml`, the bundle inventory, and license notices | Verified, index-free installation with a recorded dependency set |

Each asset has its own detached proof, `<asset>.attestation.jsonl`, and the
publishing pipeline signs and cross-checks both. As an operator you use one
asset: install the wheel directly, or download the archive with its proof and
install from the authenticated bundle. Both paths install with stock
[pipx](https://pipx.pypa.io/). Site Ops ships no installer program, no bootstrap
script, and no private package store.

A Site Ops release installs the engine only. Workspace content has its own
source and version. The included IoT Operations workspace is currently
obtained from a matching Scale Kit repository checkout. This release does not
provide verified remote workspace packages or guided workspace discovery.

A release without these assets uses the
[linked Site Ops release](releasing.md#release-content-against-an-existing-engine)
or the [contributor source installation](../CONTRIBUTING.md#development-setup)
path.

## Before you start

Run as your ordinary user rather than as an administrator or with `sudo`, and
keep downloaded files in a private directory.

| Prerequisite | Requirement | Needed for |
|---|---|---|
| Platform | Windows x64, or Linux x64 using glibc 2.17 or newer | Both paths |
| Python | Standard 64-bit CPython 3.10 through 3.14, with working `venv` support | Both paths |
| pipx | Version 1.17.2, available as `pipx` | Both paths |
| Package feed | An approved index that serves the runtime dependencies as wheels | Release wheel path |
| pipx backend pip | Version 26.2.1 | Verified bundle path |
| GitHub CLI | Version 2.95.0 or newer, providing `gh attestation verify` with the flags used below | Downloading and verifying assets |

Obtain these tools through your organization's managed software channel or their
official instructions:
[Python](https://www.python.org/downloads/),
[pipx](https://pipx.pypa.io/stable/installation/), and
[GitHub CLI](https://cli.github.com/).
They are maintained separately from Site Ops, and some Linux distributions
package Python's `venv` support separately. Prepare them first: the Site Ops
installation commands below add no tooling of their own.
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
workflow, and expected commit are trust decisions: take them from the official
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
    $archive = Join-Path $download "siteops-install.zip"

    gh attestation verify $archive `
      --bundle "$archive.attestation.jsonl" `
      --repo Azure/digital-ops-scale-kit `
      --cert-identity "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main" `
      --source-ref refs/heads/main `
      --source-digest $sourceSha `
      --signer-digest $sourceSha `
      --cert-oidc-issuer "https://token.actions.githubusercontent.com" `
      --predicate-type "https://slsa.dev/provenance/v1" `
      --deny-self-hosted-runners
    if ($LASTEXITCODE -ne 0) { throw "Verification failed. Do not extract this archive." }
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
  archive="$download/siteops-install.zip"

  gh attestation verify "$archive" \
    --bundle "$archive.attestation.jsonl" \
    --repo Azure/digital-ops-scale-kit \
    --cert-identity "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main" \
    --source-ref refs/heads/main \
    --source-digest "$source_sha" \
    --signer-digest "$source_sha" \
    --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
    --predicate-type "https://slsa.dev/provenance/v1" \
    --deny-self-hosted-runners
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
explicitly selected preview, use its repository, source ref, and source commit
consistently in the verifier policy. Use the engine release's source commit
when a content release links to a separate Site Ops release.

GitHub CLI validates the signing chain, the expected workflow and repository
identity, the source and signer commits, and the file digest. `--bundle` reads
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

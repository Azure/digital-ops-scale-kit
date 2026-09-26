#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() { printf 'Site Ops installation: %s\n' "$1" >&2; exit 1; }
stage() { printf 'Site Ops installation: %s\n' "$1"; }
private_dir() {
  local owner mode
  [[ -d "$1" && ! -L "$1" ]] ||
    fail "A retained installation directory must be private."
  owner="$(stat -c %u -- "$1")"
  mode="$(stat -c %a -- "$1")"
  [[ "$owner" == "$(id -u)" ]] && (( (8#$mode & 077) == 0 )) ||
    fail "A retained installation directory must be owned by the current user and private."
}
require_private_data_root() {
  if ! python3 - "$1" >/dev/null 2>&1 <<'PY'
import os
import stat
import sys

path = sys.argv[1]
parts = path.split("/")[1:]
if not os.path.isabs(path) or not parts or any(part in {".", ".."} for part in parts):
    raise SystemExit(1)
uid = os.getuid()
directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
try:
    for part in (part for part in parts if part):
        parent = os.fstat(directory)
        trusted_owner = parent.st_uid in {0, uid}
        writable = stat.S_IMODE(parent.st_mode) & 0o022
        # A sticky shared parent cannot replace another user's private child.
        if not trusted_owner or (writable and not parent.st_mode & stat.S_ISVTX):
            raise SystemExit(1)
        try:
            os.mkdir(part, mode=0o700, dir_fd=directory)
        except FileExistsError:
            pass
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    root = os.fstat(directory)
    if root.st_uid != uid or stat.S_IMODE(root.st_mode) & 0o077:
        raise SystemExit(1)
finally:
    os.close(directory)
PY
  then
    fail "Configure a private Site Ops data root under trusted, non-symlinked directories."
  fi
}
command -v bash >/dev/null || fail "Bash is required."

release=""
commit=""
repository="Azure/digital-ops-scale-kit"
source_ref="refs/heads/main"
caller="release.yaml"
enroll_name=""
approve=false
dry_run=false
with_azure_cli=false
replace=false
while (($#)); do
  case "$1" in
    --release|--source-commit|--repository|--source-ref|--caller|--enroll-source)
      (($# > 1)) || fail "$1 requires a value."
      case "$1" in
        --release) release="$2" ;;
        --source-commit) commit="$2" ;;
        --repository) repository="$2" ;;
        --source-ref) source_ref="$2" ;;
        --caller) caller="$2" ;;
        --enroll-source) enroll_name="$2" ;;
      esac
      shift 2 ;;
    --yes) approve=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --with-azure-cli) with_azure_cli=true; shift ;;
    --replace) replace=true; shift ;;
    *) fail "Unknown installation option." ;;
  esac
done
[[ "$release" =~ ^(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}$ ]] || fail "Select an exact release tag."
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "Supply the approved full source commit."
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Supply an OWNER/REPO publisher."
[[ "$source_ref" =~ ^refs/heads/[A-Za-z0-9._/-]+$ && "$source_ref" != *..* ]] || fail "Supply an exact source branch."
[[ "$caller" == release.yaml || "$caller" == ci.yaml ]] || fail "Select a supported calling workflow."
[[ -z "$enroll_name" || "$enroll_name" =~ ^[a-z][a-z0-9-]{0,39}$ ]] ||
  fail "Choose a lowercase approved source name."
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail "Use Linux on x86_64."
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  ubuntu:24.04) platform=ubuntu ;;
  azurelinux:3.0) platform=azurelinux ;;
  *) fail "This bootstrap supports Ubuntu 24.04 and managed Azure Linux 3." ;;
esac

data="${XDG_DATA_HOME:-$HOME/.local/share}/siteops"
[[ "$data" == /* && "$data" != /siteops ]] || fail "Select an absolute private data location."
stage "Release: $release ($commit) from $repository."
if [[ "$platform" == azurelinux ]]; then
  stage "Managed Azure Linux uses existing OS tools without sudo or package manager changes."
else
  stage "Tool changes use approved apt channels and your configured Python package feed."
fi
stage "GitHub CLI, Python with a pip-equipped venv or virtualenv, pipx 1.17.2, and pip 26.2.1 are needed."
stage "Changes to a pipx shared backend affect its selected home. Managed pipx homes are isolated."
stage "This script does not sign in or deploy."
stage "Python downloads, when needed, require a configured approved HTTPS package index."
stage "pipx may add its application directory to your user PATH."
if command -v curl >/dev/null; then
  stage "Keep: installed HTTPS downloader."
elif [[ "$platform" == azurelinux ]]; then
  stage "Required: HTTPS downloader supplied by the managed environment."
else
  stage "Add: curl and certificate authorities from Ubuntu."
fi
if command -v python3 >/dev/null &&
    python3 -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32))' 2>/dev/null; then
  stage "Keep: supported 64-bit Python. Check pip-equipped venv support before installation."
elif [[ "$platform" == azurelinux ]]; then
  stage "Required: supported 64-bit Python supplied by the managed environment."
else
  stage "Add: supported 64-bit Python and venv from Ubuntu."
fi
if command -v gh >/dev/null; then
  stage "Check: installed GitHub CLI version before changing tools."
elif [[ "$platform" == azurelinux ]]; then
  stage "Required: GitHub CLI 2.95 or newer supplied by the managed environment."
else
  stage "Add: GitHub CLI from its signed Ubuntu package channel."
fi
if command -v pipx >/dev/null || [[ -x "$data/tools/pipx/bin/pipx" ]]; then
  stage "Check: installed pipx and its shared backend before changing tools."
else
  stage "Add: pipx 1.17.2 through the configured Python feed."
fi
if $replace; then
  stage "The selected build will explicitly replace or repair an existing Site Ops installation."
fi
if $with_azure_cli; then
  if command -v az >/dev/null; then
    stage "Keep: available Azure CLI."
  elif [[ "$platform" == azurelinux ]]; then
    stage "Required: Azure CLI supplied by the managed environment."
  else
    stage "Add: Azure CLI from the Microsoft Ubuntu package channel."
  fi
fi
if [[ -n "$enroll_name" ]]; then
  if [[ "${SITEOPS_REDACT_OUTPUT:-0}" == 1 ]]; then
    stage "An explicitly selected source will be enrolled after installation."
  else
    stage "Source $enroll_name will approve $repository with a time-limited policy after installation."
  fi
fi
if $dry_run; then
  stage "Preview only. No tools or content were downloaded."
  exit 0
fi
if ! $approve; then
  [[ -t 0 ]] || fail "In automation, pass --yes after reviewing the changes."
  read -r -p "Install missing tools and the selected Site Ops build? [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || fail "Installation was not approved."
fi
if [[ -n "$enroll_name" ]] && ! $approve; then
  read -r -p "Enroll this publisher as an approved consumer source? [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || fail "Source enrollment was not approved."
fi

require_sudo() {
  [[ "$platform" == ubuntu ]] ||
    fail "Managed Azure Linux requires compatible OS tools. Use a provisioned session with curl, Python, GitHub CLI, and Azure CLI when selected."
  command -v sudo >/dev/null || fail "An approved administrator is needed to install missing OS tools."
  sudo -n true 2>/dev/null || {
    [[ -t 0 ]] || fail "Missing OS tools require administrator authorization."
    sudo -v || fail "Administrator authorization was declined."
  }
}
if ! command -v curl >/dev/null; then
  require_sudo
  sudo apt-get update -qq
  sudo apt-get install -y curl ca-certificates
fi
if ! command -v python3 >/dev/null ||
    ! python3 -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32))' 2>/dev/null; then
  require_sudo
  sudo apt-get update -qq
  sudo apt-get install -y python3 python3-venv
fi
python3 -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32))' 2>/dev/null ||
  fail "The available Python must be a supported 64-bit interpreter."
require_private_data_root "$data"
venv_check="$(mktemp -d)"
trap 'rm -rf -- "$venv_check"' EXIT
venv_tool=(python3 -m venv)
if ! "${venv_tool[@]}" "$venv_check/check" >/dev/null 2>&1 ||
   ! "$venv_check/check/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf -- "$venv_check/check"
  if python3 -m virtualenv --no-periodic-update "$venv_check/check" >/dev/null 2>&1 &&
     "$venv_check/check/bin/python" -m pip --version >/dev/null 2>&1; then
    venv_tool=(python3 -m virtualenv --no-periodic-update)
  else
    rm -rf -- "$venv_check/check"
    [[ "$platform" == ubuntu ]] ||
      fail "Managed Azure Linux needs a pip-equipped venv or an installed Python virtualenv."
    require_sudo
    sudo apt-get update -qq
    sudo apt-get install -y python3-venv
    "${venv_tool[@]}" "$venv_check/check" ||
      fail "Python venv is unavailable."
  fi
  "$venv_check/check/bin/python" -m pip --version >/dev/null 2>&1 ||
    fail "The Python environment must include pip."
fi
rm -rf -- "$venv_check"
trap - EXIT

require_approved_python_index() {
  [[ "$2" == install || "$2" == download ]] ||
    fail "The Python package command has an unsupported index policy."
  local pip_configuration
  pip_configuration="$("$1" -m pip config list 2>/dev/null)" ||
    fail "The selected Python environment's package settings could not be inspected."
  printf '%s\n' "$pip_configuration" | python3 -c '
import ast
import sys
from urllib.parse import urlsplit

settings = {}
for line in sys.stdin:
    key, separator, raw = line.partition("=")
    if not separator:
        continue
    try:
        value = ast.literal_eval(raw.strip())
    except (SyntaxError, ValueError):
        raise SystemExit(1)
    if not isinstance(value, str):
        raise SystemExit(1)
    settings[key.strip()] = value
if any(value for key, value in settings.items()
       if key.endswith((".extra-index-url", ".find-links", ".trusted-host"))):
    raise SystemExit(1)
index = next((settings[key] for key in
              (":env:.index-url", f"{sys.argv[1]}.index-url", "global.index-url")
              if settings.get(key)), "")
try:
    parsed = urlsplit(index)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit(1)
except ValueError:
    raise SystemExit(1)
' "$2" || fail "Configure one approved HTTPS Python index in pip settings or PIP_INDEX_URL, without extra indexes, find-links, or trusted hosts."
}

gh_ready=false
if command -v gh >/dev/null; then
  gh_version="$(gh version | head -n 1)"
  if [[ "$gh_version" =~ ^gh\ version\ 2\.([0-9]+)\.([0-9]+) ]] && ((10#${BASH_REMATCH[1]} > 95 || (10#${BASH_REMATCH[1]} == 95 && 10#${BASH_REMATCH[2]} >= 0))); then
    gh_ready=true
  fi
fi
if ! $gh_ready; then
  require_sudo
  keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
  source_file=/etc/apt/sources.list.d/github-cli.list
  key="$(mktemp)"
  trap 'rm -f -- "$key"' EXIT
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-time 60 --max-filesize 65536 \
    -o "$key" https://cli.github.com/packages/githubcli-archive-keyring.gpg
  printf '%s  %s\n' '6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b' "$key" |
    sha256sum --check --status || fail "The approved GitHub CLI package key changed."
  sudo install -d -m 0755 /etc/apt/keyrings /etc/apt/sources.list.d
  if [[ -e "$keyring" ]]; then
    cmp -s "$key" "$keyring" || fail "An existing GitHub CLI package key differs."
  else
    sudo install -m 0644 "$key" "$keyring"
  fi
  source_line="deb [arch=$(dpkg --print-architecture) signed-by=$keyring] https://cli.github.com/packages stable main"
  if [[ -e "$source_file" ]]; then
    [[ "$(cat "$source_file")" == "$source_line" ]] || fail "An existing GitHub CLI source differs."
  else
    printf '%s\n' "$source_line" | sudo tee "$source_file" >/dev/null
  fi
  sudo apt-get update -qq
  sudo apt-get install -y gh
  rm -f -- "$key"
  trap - EXIT
fi
command -v gh >/dev/null || fail "GitHub CLI was not installed."
[[ "$(gh version | head -n 1)" =~ ^gh\ version\ 2\.([0-9]+)\.([0-9]+) ]] &&
  ((10#${BASH_REMATCH[1]} >= 95)) || fail "GitHub CLI 2.95 or newer in the 2.x line is required."

if $with_azure_cli && ! command -v az >/dev/null; then
  require_sudo
  keyring=/etc/apt/keyrings/microsoft.gpg
  source_file=/etc/apt/sources.list.d/azure-cli.sources
  key="$(mktemp)"
  trap 'rm -f -- "$key"' EXIT
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --tlsv1.2 --max-time 60 --max-filesize 65536 \
    -o "$key" https://packages.microsoft.com/keys/microsoft.asc
  command -v gpg >/dev/null || fail "GPG is required to admit the Microsoft package key."
  dearmored="$(mktemp)"
  gpg --batch --yes --dearmor -o "$dearmored" "$key" ||
    fail "The Microsoft package key is invalid."
  sudo install -d -m 0755 /etc/apt/keyrings /etc/apt/sources.list.d
  if [[ -e "$keyring" ]]; then
    cmp -s "$dearmored" "$keyring" || fail "An existing Microsoft package key differs."
  else
    sudo install -m 0644 "$dearmored" "$keyring"
  fi
  source_text="$(printf 'Types: deb\nURIs: https://packages.microsoft.com/repos/azure-cli/\nSuites: noble\nComponents: main\nArchitectures: amd64\nSigned-by: %s\n' "$keyring")"
  if [[ -e "$source_file" ]]; then
    [[ "$(cat "$source_file")" == "$source_text" ]] || fail "An existing Azure CLI source differs."
  else
    printf '%s\n' "$source_text" | sudo tee "$source_file" >/dev/null
  fi
  sudo apt-get update -qq
  sudo apt-get install -y azure-cli
  rm -f -- "$key" "$dearmored"
  trap - EXIT
  command -v az >/dev/null || fail "Azure CLI was not installed."
fi

pipx_bin="$(command -v pipx || true)"
if [[ -x "$data/tools/pipx/bin/pipx" ]]; then
  pipx_bin="$data/tools/pipx/bin/pipx"
fi
if [[ -z "$pipx_bin" || "$("$pipx_bin" --version 2>/dev/null)" != 1.17.2 ]]; then
  tools="$data/tools"
  mkdir -p "$tools"
  if [[ -e "$tools/pipx" ]]; then
    [[ -d "$tools/pipx" && ! -L "$tools/pipx" &&
       -f "$tools/pipx/pyvenv.cfg" && -x "$tools/pipx/bin/python" ]] ||
      fail "Existing Site Ops pipx tooling differs. Inspect it before repair."
  else
    "${venv_tool[@]}" "$tools/pipx" || fail "The user pipx environment could not be created."
  fi
  require_approved_python_index "$tools/pipx/bin/python" install
  pip_install_log="$(mktemp)"
  trap 'rm -f -- "$pip_install_log"' EXIT
  "$tools/pipx/bin/python" -m pip install --only-binary=:all: --no-cache-dir 'pipx==1.17.2' \
    > "$pip_install_log" 2>&1 ||
    fail "pipx could not be installed from the configured Python index."
  rm -f -- "$pip_install_log"
  trap - EXIT
  pipx_bin="$tools/pipx/bin/pipx"
fi
[[ "$("$pipx_bin" --version)" == 1.17.2 ]] || fail "pipx 1.17.2 is required."

allowed_home="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$HOME")"
allowed_data="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$data")"
isolate_pipx=false
for setting in PIPX_HOME PIPX_BIN_DIR PIPX_SHARED_LIBS; do
  location="$("$pipx_bin" environment --value "$setting")" ||
    fail "The pipx installation locations could not be inspected."
  [[ "$location" == /* ]] || fail "The pipx installation locations must be absolute."
  resolved="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$location")" ||
    fail "The pipx installation locations could not be resolved."
  case "$resolved" in
    "$allowed_home"|"$allowed_home"/*|"$allowed_data"|"$allowed_data"/*) ;;
    *) isolate_pipx=true ;;
  esac
done
if $isolate_pipx; then
  mkdir -p "$data/pipx" "$data/bin" "$data/man" "$data/completions" ||
    fail "Private pipx storage could not be created."
  private_dir "$data/pipx"
  private_dir "$data/bin"
  private_dir "$data/man"
  private_dir "$data/completions"
  PIPX_HOME="$data/pipx"
  PIPX_BIN_DIR="$data/bin"
  PIPX_SHARED_LIBS="$data/pipx/shared"
  PIPX_MAN_DIR="$data/man"
  PIPX_COMPLETION_DIR="$data/completions"
  export PIPX_HOME PIPX_BIN_DIR PIPX_SHARED_LIBS PIPX_MAN_DIR PIPX_COMPLETION_DIR
  stage "Using private Site Ops pipx storage instead of the managed pipx home."
fi

staging="$(mktemp -d)"
trap 'rm -rf -- "$staging"' EXIT
selection_id="$(printf '%s\0' "$repository" "$release" "$commit" "$source_ref" "$caller" |
  sha256sum | cut -d ' ' -f 1)"
cache="$data/install-downloads/$selection_id"
encoded_release="${release//\//%2F}"
url="https://github.com/$repository/releases/download/$encoded_release/"
assets="$staging"
if [[ -e "$cache" || -L "$cache" ]]; then
  private_dir "$cache"
  entries=("$cache"/*)
  [[ ${#entries[@]} -eq 2 ]] ||
    fail "Retained release bytes are incomplete. Inspect them before retrying."
  assets="$cache"
  stage "Rechecking the retained release without downloading its assets."
else
  for asset in siteops-install.zip siteops-install.zip.attestation.jsonl; do
    limit=536870912
    if [[ "$asset" == *.attestation.jsonl ]]; then limit=2097152; fi
    stage "Downloading $asset anonymously."
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize "$limit" \
      -o "$staging/$asset" "$url$asset"
    [[ -s "$staging/$asset" && $(wc -c < "$staging/$asset") -le $limit ]] ||
      fail "A required release asset is empty or exceeds its byte limit."
  done
fi
[[ -f "$assets/siteops-install.zip" && ! -L "$assets/siteops-install.zip" &&
   -f "$assets/siteops-install.zip.attestation.jsonl" &&
   ! -L "$assets/siteops-install.zip.attestation.jsonl" &&
   $(wc -c < "$assets/siteops-install.zip") -le 536870912 &&
   $(wc -c < "$assets/siteops-install.zip.attestation.jsonl") -le 2097152 ]] ||
  fail "Retained release bytes are invalid or oversized."
signer="https://github.com/$repository/.github/workflows/_siteops-distribution.yaml@$source_ref"
builder="https://github.com/$repository/.github/workflows/$caller@$source_ref"
query="length > 0 and all(.[]; .verificationResult.mediaType == \"application/vnd.dev.sigstore.verificationresult+json;version=0.1\" and (.verificationResult.signature.certificate | .subjectAlternativeName == \"$signer\" and .issuer == \"https://token.actions.githubusercontent.com\" and .sourceRepositoryURI == \"https://github.com/$repository\" and .sourceRepositoryDigest == \"$commit\" and .sourceRepositoryRef == \"$source_ref\" and .buildSignerDigest == \"$commit\" and .buildConfigURI == \"$builder\" and .buildConfigDigest == \"$commit\" and .runnerEnvironment == \"self-hosted\"))"
stage "Checking the bundle's source, signer, caller, and runner."
verified="$(timeout --kill-after=5 120 gh attestation verify "$assets/siteops-install.zip" \
  --bundle "$assets/siteops-install.zip.attestation.jsonl" --repo "$repository" \
  --cert-identity "$signer" --source-ref "$source_ref" --source-digest "$commit" \
  --signer-digest "$commit" --cert-oidc-issuer https://token.actions.githubusercontent.com \
  --predicate-type https://slsa.dev/provenance/v1 --hostname github.com \
  --digest-alg sha256 --format json --jq "$query" 2>/dev/null)" ||
  fail "The bundle's provenance could not be verified."
[[ "$verified" == true ]] || fail "The bundle certificate does not match the selected release."

archive="$assets/siteops-install.zip"
bundle_id="$(sha256sum "$archive" | cut -d ' ' -f 1)"
bundle="$data/bundles/$bundle_id"
mkdir -p "$data/bundles"
recorded="$("$pipx_bin" list --output json | python3 -c '
import json, sys
apps = json.load(sys.stdin)["venvs"]
app = apps.get("siteops")
if app:
    lock = app["metadata"]["main_package"].get("lock_file")
    print(lock["__Path__"] if lock else "unlocked")
' 2>/dev/null)" || fail "The current pipx installation cannot be inspected."
if [[ -n "$recorded" && "$recorded" != "$bundle/pylock.toml" ]] && ! $replace; then
  fail "Another Site Ops build is installed. Select --replace after reviewing the native pipx transition."
fi
repeat=false
if [[ -e "$bundle" ]]; then
  private_dir "$bundle"
  [[ -f "$bundle/bundle.json" && -f "$bundle/pylock.toml" ]] ||
    fail "The retained bundle is incomplete. Inspect it before repair."
  if ! python3 - "$archive" "$bundle/bundle.json" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    if archive.namelist().count("bundle.json") != 1:
        raise SystemExit(1)
    member = archive.getinfo("bundle.json")
    if member.file_size > 1048576:
        raise SystemExit(1)
    with archive.open(member) as stream:
        expected = stream.read(1048577)
with open(sys.argv[2], "rb") as stream:
    actual = stream.read(1048577)
if expected != actual:
    raise SystemExit(1)
PY
  then
    fail "The retained bundle manifest differs from the authenticated archive."
  fi
  if ! python3 - "$bundle" <<'PY'
import hashlib
import json
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "bundle.json").read_bytes())
entries = manifest.get("files")
if not isinstance(entries, list) or not 1 <= len(entries) <= 1024:
    raise SystemExit(1)
expected = {"bundle.json"}
for entry in entries:
    name, size, digest = entry["path"], entry["size"], entry["sha256"]
    parts = pathlib.PurePosixPath(name).parts
    if (not parts or any(part in {".", ".."} for part in parts)
            or name.startswith("/") or "\\" in name or name in expected
            or not isinstance(size, int) or size < 0 or size > 1073741824):
        raise SystemExit(1)
    path = root.joinpath(*parts)
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != size:
        raise SystemExit(1)
    observed = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            observed.update(block)
    if observed.hexdigest() != digest:
        raise SystemExit(1)
    expected.add(name)
directories = set()
for name in expected:
    parent = pathlib.PurePosixPath(name).parent
    while parent.as_posix() != ".":
        directories.add(parent.as_posix())
        parent = parent.parent
actual = set()
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit(1)
    relative = path.relative_to(root).as_posix()
    if path.is_file():
        actual.add(relative)
    elif not path.is_dir() or relative not in directories:
        raise SystemExit(1)
if actual != expected:
    raise SystemExit(1)
PY
  then
    fail "The retained bundle contents differ from the authenticated archive."
  fi
  if [[ "$recorded" == "$bundle/pylock.toml" ]] && ! $replace; then
    backend_version="$("$pipx_bin" runpip siteops --version)" ||
      fail "The installed pipx backend could not be inspected."
    [[ "$backend_version" == "pip 26.2.1 "* ]] ||
      fail "The installed pipx backend differs from the verified lock reader."
    repeat=true
  fi
else
  mkdir "$bundle"
  python3 -m zipfile -e "$archive" "$bundle" || fail "The verified bundle could not be extracted."
fi
version="$(python3 -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    document = json.load(source)
if document.get("source") != {
    "repository": sys.argv[2], "commit": sys.argv[3], "ref": sys.argv[4]
} or document.get("apiVersion") != "siteops.install/v1":
    raise SystemExit("The verified bundle describes another source.")
print(document["package"]["version"])
' "$bundle/bundle.json" "$repository" "$commit" "$source_ref")" ||
  fail "The verified bundle manifest is invalid."
[[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$ ]] ||
  fail "The verified bundle has an unsupported version."
if ! $repeat; then
shared_home="$("$pipx_bin" environment --value PIPX_SHARED_LIBS)" ||
  fail "The pipx shared backend location could not be inspected."
if [[ -x "$shared_home/bin/python" &&
      "$("$shared_home/bin/python" -m pip --version 2>/dev/null)" == "pip 26.2.1 "* ]]; then
  stage "Keeping the compatible pipx shared backend."
else
  wheelhouse="$(mktemp -d)"
  trap 'rm -rf -- "$staging" "$wheelhouse"' EXIT
  "${venv_tool[@]}" "$staging/backend-tools" || fail "Python venv is unavailable."
  require_approved_python_index "$staging/backend-tools/bin/python" download
  "$staging/backend-tools/bin/python" -m pip download 'pip==26.2.1' \
    --no-deps --only-binary=:all: --dest "$wheelhouse" > "$staging/pip-download.log" 2>&1 ||
    fail "The shared backend could not be downloaded from the configured Python index."
  pip_wheels=("$wheelhouse"/pip-26.2.1-*.whl)
  [[ ${#pip_wheels[@]} -eq 1 && -f "${pip_wheels[0]}" ]] || fail "The approved backend wheel is unavailable."
  printf '%s  %s\n' '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e' "${pip_wheels[0]}" |
    sha256sum --check --status || fail "The selected backend wheel differs from its reviewed hash."
  wheelhouse_uri="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$wheelhouse")"
  export PIPX_DEFAULT_PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
  "$pipx_bin" upgrade-shared --pip-args "--no-index --only-binary=:all: --no-cache-dir --force-reinstall --find-links=$wheelhouse_uri" ||
    fail "The pipx shared backend could not be provisioned."
fi
export PIPX_DEFAULT_PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
force_args=()
if $replace && [[ -n "$recorded" ]]; then force_args=(--force); fi
"$pipx_bin" install siteops --lock "$bundle/pylock.toml" \
  --backend pip --fetch-python never --skip-maintenance --app siteops \
  --pip-args "--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir" \
  "${force_args[@]}" ||
  fail "Site Ops could not be installed from the verified lock."
backend_version="$("$pipx_bin" runpip siteops --version)" ||
  fail "The installed pipx backend could not be inspected."
[[ "$backend_version" == "pip 26.2.1 "* ]] ||
  fail "The installed pipx backend differs from the verified lock reader."
"$pipx_bin" ensurepath >/dev/null ||
  fail "pipx could not make its command directory available to future shells."
fi
bin_dir="$("$pipx_bin" environment --value PIPX_BIN_DIR)" ||
  fail "The pipx command directory could not be resolved."
[[ "$bin_dir" == /* && -x "$bin_dir/siteops" ]] ||
  fail "pipx did not expose the selected siteops command."
export PATH="$bin_dir:$PATH"
[[ "$(command -v siteops)" == "$bin_dir/siteops" && "$(siteops --version)" == "siteops $version" ]] ||
  fail "The exposed siteops command does not match the selected build."
stage "Command directory: $bin_dir. Add it to your current PATH or open a new shell."
if [[ -n "$enroll_name" ]]; then
  trusted_root="$staging/trusted-root.jsonl"
  timeout --kill-after=5 120 gh attestation trusted-root | head -c 2097153 > "$trusted_root" ||
    fail "The GitHub trusted-root snapshot could not be obtained."
  [[ -s "$trusted_root" && $(wc -c < "$trusted_root") -le 2097152 ]] ||
    fail "The trusted-root snapshot is empty or oversized."
  root_digest="$(sha256sum "$trusted_root" | cut -d ' ' -f 1)"
  policy_file="$staging/source-policy.json"
  python3 - "$policy_file" "$root_digest" "$repository" "$source_ref" "$caller" <<'PY'
import datetime
import json
import pathlib
import sys

destination, digest, repository, source_ref, caller = sys.argv[1:]
policy = {
    "apiVersion": "siteops/v1alpha1",
    "kind": "ArtifactVerificationPolicy",
    "id": "approved-source",
    "version": 1,
    "validUntil": (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=30)).isoformat(),
    "trustedRootSha256": digest,
    "provider": {
        "kind": "github-attestation/v1",
        "repository": repository,
        "sourceRef": source_ref,
        "signerWorkflow": ".github/workflows/_workspace-distribution.yaml",
        "builderWorkflow": ".github/workflows/" + caller,
        "runnerEnvironment": "self-hosted",
    },
}
pathlib.Path(destination).write_text(json.dumps(policy), encoding="utf-8")
PY
  siteops --trust-policy "$policy_file" --trusted-root "$trusted_root" \
    source enroll "$enroll_name" --source "github:$repository" ||
    fail "The approved source could not be enrolled."
fi
if [[ "$assets" == "$staging" ]]; then
  mkdir -p "$data/install-downloads"
  mkdir "$cache" || fail "The authenticated release cache could not be reserved."
  for asset in siteops-install.zip siteops-install.zip.attestation.jsonl; do
    cp -- "$staging/$asset" "$cache/$asset" ||
      fail "Authenticated release bytes could not be retained after installation."
    [[ "$(sha256sum "$staging/$asset" | cut -d ' ' -f 1)" == "$(sha256sum "$cache/$asset" | cut -d ' ' -f 1)" ]] ||
      fail "The retained release bytes differ from the authenticated download."
  done
fi
if [[ -n "$enroll_name" ]]; then
  if [[ "${SITEOPS_REDACT_OUTPUT:-0}" == 1 ]]; then
    stage "Installed siteops $version with an approved source. Authenticate to Azure separately."
  else
    stage "Installed siteops $version with approved source $enroll_name. Authenticate to Azure separately."
  fi
else
  stage "Installed siteops $version. Authenticate to Azure and approve a workspace source separately."
fi

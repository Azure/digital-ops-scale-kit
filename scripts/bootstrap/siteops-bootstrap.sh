#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() { printf 'Site Ops installation failed: %s\n' "$1" >&2; exit 1; }
stage() { printf 'Site Ops installation: %s\n' "$1"; }
command -v bash >/dev/null || fail "Bash is required."

extract_installer_helper() {
  "$python" -I -S -B - "$archive" "$staging" "${engine_version:-}" <<'PY'
import json
import pathlib
import stat
import sys
import zipfile

try:
    with zipfile.ZipFile(sys.argv[1]) as archive:
        if sys.argv[3]:
            metadata = [item for item in archive.infolist() if item.filename.casefold() == "bundle.json"]
            if (len(metadata) != 1 or metadata[0].filename != "bundle.json"
                    or not 0 < metadata[0].file_size <= 1048576):
                raise ValueError()
            with archive.open(metadata[0]) as stream:
                manifest = json.loads(stream.read(1048577))
            if manifest.get("package", {}).get("version") != sys.argv[3]:
                raise ValueError()
        entries = [item for item in archive.infolist()
                   if item.filename.casefold() == "siteops-install.py"]
        if (len(entries) != 1 or entries[0].filename != "siteops-install.py"
                or not 0 < entries[0].file_size <= 1048576
                or stat.S_IFMT(entries[0].external_attr >> 16) not in {0, stat.S_IFREG}):
            raise ValueError()
        with archive.open(entries[0]) as stream:
            content = stream.read(1048577)
        if len(content) != entries[0].file_size:
            raise ValueError()
    helper = pathlib.Path(sys.argv[2]) / "siteops-install.py"
    with helper.open("xb") as output:
        output.write(content)
    print(helper)
except (OSError, ValueError, TypeError, AttributeError, zipfile.BadZipFile):
    sys.exit("The authenticated release has no valid installer helper.")
PY
}
check_payload() {
  "$python" -I -S -B "$installer_helper" "$@"
}

# A user private group (pam_umask with USERGROUPS_ENAB) holds only this user,
# so its write access adds no other writer. Debian's OpenSSH and zsh's
# compaudit accept it under the same conditions. Any lookup failure refuses it.
detect_private_group() {
  local gid user group name members accounts account primary
  private_gid=""
  gid="$(id -g)" && user="$(getent passwd "$uid")" && group="$(getent group "$gid")" &&
    accounts="$(getent passwd)" || return 0
  IFS=: read -r name _ _ members <<< "$group"
  [[ "$name" == "${user%%:*}" && ( -z "$members" || "$members" == "$name" ) ]] || return 0
  while IFS=: read -r account _ _ primary _; do
    [[ "$primary" != "$gid" || "$account" == "$name" ]] || return 0
  done <<< "$accounts"
  private_gid="$gid"
}
# Only root or this user may own a trusted entry. Other write is never
# accepted, and group write only from this user's private group.
writers_trusted() {
  local mode=$((8#$3))
  [[ "$1" == "$uid" || "$1" == 0 ]] && (( (mode & 8#002) == 0 )) && {
    (( (mode & 8#020) == 0 )) || [[ "$1" == "$uid" && -n "$private_gid" && "$2" == "$private_gid" ]]
  }
}
# An existing directory on a trusted path has trusted writers, or a sticky
# bit protects this user's child from them.
trusted_parent() {
  local owner group mode
  [[ -d "$1" && ! -L "$1" ]] || return 1
  read -r owner group mode < <(stat -c '%u %g %a' -- "$1") || return 1
  writers_trusted "$owner" "$group" "$mode" ||
    { [[ "$owner" == "$uid" || "$owner" == 0 ]] && (( 8#$mode & 8#1000 )); }
}
# Walk an absolute path from the root without following links. Missing
# directories are created privately unless the caller requires an existing
# path. Private Site Ops data must be owned by this user and closed to others.
# Native uv storage only excludes other writers. Sets admitted to the
# lexically normalized path.
admit_directory() {
  local path="$1" kind="$2" create="${3:-create}" current="" part owner group mode
  local -a parts
  admitted=""
  [[ "$path" == /?* && "$path" != *//* && ! "$path" =~ [[:cntrl:]] ]] || return 1
  IFS=/ read -r -a parts <<< "${path#/}"
  trusted_parent / || return 1
  for part in "${parts[@]}"; do
    case "$part" in
      .) return 1 ;;
      ..) [[ -n "$current" ]] || return 1; current="${current%/*}"; continue ;;
    esac
    current="$current/$part"
    if [[ ! -e "$current" && ! -L "$current" ]]; then
      [[ "$create" == create ]] || return 1
      mkdir -m 0700 -- "$current" 2>/dev/null || true
    fi
    trusted_parent "$current" || return 1
  done
  [[ -n "$current" ]] || return 1
  read -r owner group mode < <(stat -c '%u %g %a' -- "$current") || return 1
  if [[ "$kind" == private ]]; then
    [[ "$owner" == "$uid" ]] && (( (8#$mode & 8#077) == 0 )) || return 1
  else
    writers_trusted "$owner" "$group" "$mode" || return 1
  fi
  admitted="$current"
}
# A selected file is a regular file with trusted writers inside admitted
# existing directories.
admit_file() {
  local name="${1##*/}" file owner group mode
  [[ "$1" == /*/* || "$1" == /?* ]] && [[ -n "$name" && "$name" != . && "$name" != .. ]] ||
    return 1
  admit_directory "${1%/*}" "${2:-shared}" existing || return 1
  file="$admitted/$name"
  admitted=""
  [[ -f "$file" && ! -L "$file" ]] || return 1
  read -r owner group mode < <(stat -c '%u %g %a' -- "$file") || return 1
  writers_trusted "$owner" "$group" "$mode" || return 1
  admitted="$file"
}
pinned_uv() {
  [[ "$(stat -c %s -- "$1")" == 50437584 &&
     "$(sha256sum < "$1" | cut -d ' ' -f 1)" == b8299463da6fa7da3b94464444d252d0afca8ac6c96cb229f1baf4012f365246 ]]
}
run_uv() {
  env "${uv_unset[@]}" UV_CACHE_DIR="$staging/uv-cache" \
    timeout --kill-after=5 600 "$uv" "$@" --no-config --no-progress --system-certs
}
# Admit one of uv's standard locations. These uv commands only compute paths.
uv_directory() {
  local value
  value="$(run_uv "$@" 2>/dev/null | head -c 4097)" || return 1
  (( ${#value} <= 4096 )) && admit_directory "$value" shared
}
select_uv() {
  local target tooling cached
  path_uv="$(command -v uv || true)"
  if [[ -n "$path_uv" ]]; then
    # Run the admitted target, never the link that selected it.
    if [[ "$path_uv" == /* ]] && target="$(readlink -e -- "$path_uv")" &&
       admit_file "$target" && pinned_uv "$admitted"; then
      uv="$admitted"
      stage "Keep: the selected qualified uv installation."
      return
    fi
    stage "Keep: the other uv installation unchanged. Use a pinned Site Ops tooling copy."
  fi
  admit_directory "$data/tools/uv/0.12.20" private ||
    fail "The native uv tooling cache must be private. Inspect it before repair."
  tooling="$admitted"
  cached="$tooling/uv"
  if [[ -e "$cached" || -L "$cached" ]]; then
    [[ "$(find "$tooling" -mindepth 1 -maxdepth 1 -printf x | wc -c)" == 1 ]] &&
      admit_file "$cached" private && pinned_uv "$cached" ||
      fail "The retained native uv copy differs from the selected release. Inspect it before repair."
  else
    stage "Downloading pinned native uv 0.12.20."
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize 19827214 -o "$staging/uv.tar.gz" \
      https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-unknown-linux-gnu.tar.gz ||
      fail "The native uv archive could not be downloaded."
    [[ "$(stat -c %s -- "$staging/uv.tar.gz")" == 19827214 &&
       "$(sha256sum < "$staging/uv.tar.gz" | cut -d ' ' -f 1)" == 6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f ]] ||
      fail "The native uv archive differs from the selected release."
    tar -xzOf "$staging/uv.tar.gz" uv-x86_64-unknown-linux-gnu/uv > "$staging/uv" 2>/dev/null &&
      pinned_uv "$staging/uv" || fail "The native uv executable differs from the selected release."
    (set -C; cat -- "$staging/uv" > "$cached") && chmod 0700 -- "$cached" &&
      admit_file "$cached" private && pinned_uv "$cached" ||
      fail "The pinned uv tooling copy could not be retained."
  fi
  uv="$cached"
}
# Make the pinned copy available for ordinary maintenance when no uv exists.
expose_uv() {
  local maintenance="$bin/uv"
  [[ -z "$path_uv" ]] || return 0
  if [[ -e "$maintenance" || -L "$maintenance" ]]; then
    admit_file "$maintenance" && pinned_uv "$maintenance" ||
      fail "An unrelated uv command occupies the native maintenance location. Inspect it before installation."
    return 0
  fi
  (set -C; cat -- "$uv" > "$maintenance") && chmod 0755 -- "$maintenance" &&
    admit_file "$maintenance" && pinned_uv "$maintenance" ||
    fail "The uv maintenance command could not be exposed."
  stage "uv maintenance directory: $bin. Add it to your PATH if needed."
}
# Admit a whole concrete runtime tree before any of its files execute.
admit_runtime_tree() {
  local concrete="$1" minor="$2" unsafe link target group_write=(-perm /020)
  admit_directory "$concrete" shared existing ||
    fail "The uv-managed Python must use trusted, non-symlinked directories."
  [[ -f "$concrete/bin/python$minor" && ! -L "$concrete/bin/python$minor" ]] ||
    fail "The uv-managed Python installation is incomplete. Inspect it before repair."
  [[ -z "$private_gid" ]] || group_write+=(\( ! -uid "$uid" -o ! -gid "$private_gid" \))
  unsafe="$(find "$concrete" \( ! -uid "$uid" ! -uid 0 -o ! -type l \
    \( -perm /002 -o "${group_write[@]}" \) \) -print -quit 2>/dev/null)" && [[ -z "$unsafe" ]] ||
    fail "The uv-managed Python has files other users can change. Inspect it before repair."
  while IFS= read -r -d '' link; do
    target="$(readlink -e -- "$link")" && [[ "$target" == "$concrete"/* ]] ||
      fail "The uv-managed Python links outside its concrete installation."
  done < <(find "$concrete" -type l -print0)
}
# Enumerate concrete keys natively. uv discovery runs every listed interpreter.
select_runtime() {
  local selected="" existing="" home="" line candidate minor patch identity
  local best_minor=-1 best_patch=-1
  local key='^cpython-(3\.(1[0-4])\.(0|[1-9][0-9]{0,2}))-linux-x86_64-gnu$'
  if [[ -e "$tools/siteops" || -L "$tools/siteops" ]]; then
    admit_file "$tools/siteops/pyvenv.cfg" && (( $(stat -c %s -- "$admitted") <= 65536 )) ||
      fail "The current Site Ops tool needs inspection. Use uv tool uninstall siteops after review."
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        "version_info = "*) existing="${line#version_info = }" ;;
        "home = "*) home="${line#home = }" ;;
      esac
    done < "$tools/siteops/pyvenv.cfg"
    if [[ "cpython-$existing-linux-x86_64-gnu" =~ $key &&
          "$home" == "$pydir/cpython-$existing-linux-x86_64-gnu/bin" &&
          -e "$pydir/cpython-$existing-linux-x86_64-gnu" ]]; then
      selected="$existing"
    else
      fail "The existing Site Ops runtime is not an available uv-managed Python. Inspect it or run uv tool uninstall siteops after review, then retry."
    fi
  fi
  if [[ -z "$selected" ]]; then
    for candidate in "$pydir"/cpython-3.*-linux-x86_64-gnu; do
      [[ "${candidate##*/}" =~ $key ]] || continue
      if [[ "${BASH_REMATCH[1]}" == 3.11.16 ]]; then
        selected=3.11.16
        break
      fi
      minor="${BASH_REMATCH[2]}"
      patch="${BASH_REMATCH[3]}"
      if (( minor > best_minor || (minor == best_minor && patch > best_patch) )); then
        best_minor="$minor"
        best_patch="$patch"
        selected="${BASH_REMATCH[1]}"
      fi
    done
  fi
  if [[ -z "$selected" ]]; then
    stage "Provisioning uv-managed CPython 3.11.16 without command aliases."
    run_uv python install 3.11.16 --no-bin --no-registry > /dev/null 2>&1 ||
      fail "The uv-managed Python could not be provisioned. Check the approved runtime source."
    selected=3.11.16
  fi
  minor="${selected%.*}"
  admit_runtime_tree "$pydir/cpython-$selected-linux-x86_64-gnu" "$minor"
  python="$pydir/cpython-$selected-linux-x86_64-gnu/bin/python$minor"
  identity="$("$python" -I -S -B -c 'import sys; print(sys.implementation.name, *sys.version_info[:3], sys.maxsize > 2**32, sys._base_executable)' 2>/dev/null)" ||
    identity=""
  [[ "$identity" == "cpython ${selected//./ } True $python" ]] ||
    fail "The selected concrete uv-managed Python differs from the installed runtime."
}
# Native replacement queries the current tool interpreter before reinstalling.
admit_bound_runtime() {
  local bound="" layout='^(cpython-3\.(1[0-4])\.(0|[1-9][0-9]{0,2})-linux-x86_64-gnu)/bin/python3\.(1[0-4])$'
  [[ -e "$tools/siteops" || -L "$tools/siteops" ]] || return 0
  bound="$(readlink -e -- "$tools/siteops/bin/python")" || bound=""
  if [[ -n "$bound" && "$bound" == "$pydir"/* && "${bound#"$pydir"/}" =~ $layout &&
        "${BASH_REMATCH[2]}" == "${BASH_REMATCH[4]}" ]]; then
    admit_runtime_tree "$pydir/${BASH_REMATCH[1]}" "3.${BASH_REMATCH[2]}"
  elif [[ -z "$bound" ]] || ! admit_file "$bound"; then
    fail "The current Site Ops tool uses an unsafe runtime. Inspect it before using uv tool uninstall siteops."
  fi
}

verify_release_asset() {
  local subject="$1" expected_commit="$2" expected_ref="$3" expected_caller="$4" workflow="$5"
  local signer builder query verified
  signer="https://github.com/$repository/.github/workflows/$workflow@$expected_ref"
  builder="https://github.com/$repository/.github/workflows/$expected_caller@$expected_ref"
  query="length > 0 and all(.[]; .verificationResult.mediaType == \"application/vnd.dev.sigstore.verificationresult+json;version=0.1\" and (.verificationResult.signature.certificate | .subjectAlternativeName == \"$signer\" and .issuer == \"https://token.actions.githubusercontent.com\" and .sourceRepositoryURI == \"https://github.com/$repository\" and .sourceRepositoryDigest == \"$expected_commit\" and .sourceRepositoryRef == \"$expected_ref\" and .buildSignerDigest == \"$expected_commit\" and .buildConfigURI == \"$builder\" and .buildConfigDigest == \"$expected_commit\" and .runnerEnvironment == \"self-hosted\"))"
  verified="$(timeout --kill-after=5 120 "$gh" attestation verify "$subject" \
    --bundle "$subject.attestation.jsonl" --repo "$repository" \
    --cert-identity "$signer" --source-ref "$expected_ref" --source-digest "$expected_commit" \
    --signer-digest "$expected_commit" --cert-oidc-issuer https://token.actions.githubusercontent.com \
    --predicate-type https://slsa.dev/provenance/v1 --hostname github.com \
    --digest-alg sha256 --format json --jq "$query" 2>/dev/null)" ||
    fail "The selected asset's provenance could not be verified."
  [[ "$verified" == true ]] || fail "The asset certificate does not match the selected release."
}
prepare_runtime() {
  [[ "${runtime_prepared:-false}" == false ]] || return 0
  local disabled='^(0|false)?$'
  local mirror='^https://([^/?#@[:space:]]+@)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?([/?#][^[:space:]]*)?$'
  [[ -z "${UV_INSECURE_HOST:-}" && -z "${UV_PYTHON_DOWNLOADS_JSON_URL:-}" &&
     "${UV_INSECURE_NO_ZIP_VALIDATION:-}" =~ $disabled ]] ||
    fail "Remove insecure uv settings and custom runtime catalogs before verified installation."
  [[ -z "${UV_PYTHON_INSTALL_MIRROR:-}" || "$UV_PYTHON_INSTALL_MIRROR" =~ $mirror ]] ||
    fail "Select an approved HTTPS Python runtime mirror."
  uv_unset=()
  python_unset=()
  while IFS= read -r name; do
    case "$name" in
      PYTHON*|VIRTUAL_ENV|CONDA_PREFIX) python_unset+=(-u "$name"); uv_unset+=(-u "$name") ;;
      UV_TOOL_DIR|UV_TOOL_BIN_DIR|UV_PYTHON_INSTALL_DIR|UV_PYTHON_INSTALL_MIRROR)
        [[ -n "${!name}" ]] || uv_unset+=(-u "$name") ;;
      UV_*) uv_unset+=(-u "$name") ;;
    esac
  done < <(compgen -e)
  for name in UV_TOOL_DIR UV_TOOL_BIN_DIR UV_PYTHON_INSTALL_DIR; do
    if [[ -n "${!name:-}" ]]; then
      admit_directory "${!name}" shared ||
        fail "Select uv tool, command, and Python directories under trusted, non-symlinked directories."
    fi
  done
  select_uv
  uv_directory tool dir ||
    fail "Select uv tool, command, and Python directories under trusted, non-symlinked directories."
  tools="$admitted"
  uv_directory tool dir --bin ||
    fail "Select uv tool, command, and Python directories under trusted, non-symlinked directories."
  bin="$admitted"
  uv_directory python dir ||
    fail "Select uv tool, command, and Python directories under trusted, non-symlinked directories."
  pydir="$admitted"
  expose_uv
  if [[ ( -e "$bin/siteops" || -L "$bin/siteops" ) && ! -e "$tools/siteops" && ! -L "$tools/siteops" ]]; then
    fail "The exposed command belongs to another installation. Remove it with its original manager."
  fi
  if $replace; then admit_bound_runtime; fi
  select_runtime
  runtime_prepared=true
}
read_engine_reference() {
  "$python" -I -S -B - "$1" "$release" "$commit" "$source_ref" "$caller" <<'PY'
import json
import pathlib
import re
import sys

try:
    path, release, revision, source_ref, caller = sys.argv[1:]
    with pathlib.Path(path).open("rb") as stream:
        raw = stream.read(16385)
    if not 0 < len(raw) <= 16384:
        raise ValueError()
    record = json.loads(raw)
    if (type(record) is not dict
            or set(record) != {"apiVersion", "kind", "release", "revision", "preview", "engine"}
            or record["apiVersion"] != "siteops.release.engine/v1" or record["kind"] != "EngineReference"
            or record["release"] != release or record["revision"] != revision
            or type(record["preview"]) is not bool or record["preview"] != (caller == "ci.yaml")
            or raw != (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()):
        raise ValueError()
    engine = record["engine"]
    if (type(engine) is not dict or set(engine) != {"release", "revision", "version", "bundle", "proof"}
            or not isinstance(engine["release"], str)
            or re.fullmatch(r"(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}", engine["release"]) is None
            or not isinstance(engine["revision"], str) or re.fullmatch("[0-9a-f]{40}", engine["revision"]) is None
            or not isinstance(engine["version"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", engine["version"]) is None):
        raise ValueError()
    if engine["release"] == release:
        if engine["revision"] != revision:
            raise ValueError()
        engine_ref, engine_caller = source_ref, caller
    else:
        if engine["release"] != "siteops/v" + engine["version"]:
            raise ValueError()
        engine_ref, engine_caller = "refs/heads/main", "release.yaml"
    for key, name, maximum in (
        ("bundle", "siteops-install.zip", 134217728),
        ("proof", "siteops-install.zip.attestation.jsonl", 2097152),
    ):
        asset = engine[key]
        if (type(asset) is not dict or set(asset) != {"name", "size", "sha256"} or asset["name"] != name
                or type(asset["size"]) is not int or not 0 < asset["size"] <= maximum
                or not isinstance(asset["sha256"], str) or re.fullmatch("[0-9a-f]{64}", asset["sha256"]) is None):
            raise ValueError()
    for value in (
        engine["release"], engine["revision"], engine_ref, engine_caller, engine["version"],
        engine["bundle"]["size"], engine["bundle"]["sha256"], engine["proof"]["size"], engine["proof"]["sha256"],
    ):
        print(value)
except (OSError, ValueError, TypeError, KeyError, RecursionError):
    sys.exit("The signed engine reference is invalid or differs from the selected content release.")
PY
}

release=""
content_release=""
runtime_prepared=false
commit=""
repository="Azure/digital-ops-scale-kit"
source_ref="refs/heads/main"
caller="release.yaml"
enroll_name=""
approve=false
dry_run=false
replace=false
private_gid=""
while (($#)); do
  case "$1" in
    --release|--content-release|--source-commit|--repository|--source-ref|--caller|--enroll-source)
      (($# > 1)) || fail "$1 requires a value."
      case "$1" in
        --release) release="$2" ;;
        --content-release) content_release="$2" ;;
        --source-commit) commit="$2" ;;
        --repository) repository="$2" ;;
        --source-ref) source_ref="$2" ;;
        --caller) caller="$2" ;;
        --enroll-source) enroll_name="$2" ;;
      esac
      shift 2 ;;
    --yes) approve=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --replace) replace=true; shift ;;
    *) fail "Unknown installation option." ;;
  esac
done
if [[ -n "$content_release" ]]; then
  [[ -z "$release" && "$content_release" =~ ^v[0-9][0-9A-Za-z._-]{0,100}$ ]] ||
    fail "Choose either --release for an engine or --content-release for its signed engine selection."
  release="$content_release"
fi
[[ "$release" =~ ^(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}$ ]] || fail "Select an exact release tag."
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "Supply the approved full source commit."
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "Supply an OWNER/REPO publisher."
[[ "$source_ref" =~ ^refs/heads/[A-Za-z0-9._/-]+$ && "$source_ref" != *..* ]] || fail "Supply an exact source branch."
[[ "$caller" == release.yaml || "$caller" == ci.yaml ]] || fail "Select a supported calling workflow."
[[ -z "$enroll_name" || "$enroll_name" =~ ^[a-z][a-z0-9-]{0,39}$ ]] ||
  fail "Choose a lowercase approved source name."
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail "Use Linux on x86_64."
command -v getconf >/dev/null && getconf GNU_LIBC_VERSION >/dev/null 2>&1 ||
  fail "Use a Linux distribution based on glibc."
for tool in curl cut env find getent head id mktemp mv readlink sha256sum stat tar timeout wc; do
  command -v "$tool" >/dev/null || fail "The base system tool $tool is required."
done
uid="$(id -u)"
detect_private_group
# The verifier runs only after its file and every parent pass admission.
gh="$(command -v gh || true)"
gh_version=""
if [[ "$gh" == /* ]] && gh="$(readlink -e -- "$gh")"; then
  admit_file "$gh" ||
    fail "The GitHub CLI executable must be owned by an administrator or the current user and protected from other users."
  gh="$admitted"
  gh_version="$(timeout --kill-after=5 30 "$gh" version 2>/dev/null | head -n 1)" || gh_version=""
fi
[[ "$gh_version" =~ ^gh\ version\ 2\.([0-9]+)\.[0-9]+ ]] && ((10#${BASH_REMATCH[1]} >= 95)) ||
  fail "GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry."

data="${XDG_DATA_HOME:-$HOME/.local/share}/siteops"
[[ "$data" == /* && "$data" != /siteops ]] || fail "Select an absolute private data location."
stage "Release: $release ($commit) from $repository."
stage "Uses the installed GitHub CLI, checksum-pinned uv 0.12.20, and uv-managed Python."
stage "No administrator rights or OS packages are used. No account is signed in."
stage "Existing uv and Python installations are not upgraded."
stage "Managed Python downloads use uv's HTTPS runtime source or an approved HTTPS mirror."
stage "Shell profiles are not edited. Add the reported command directory to PATH if needed."
if command -v uv >/dev/null; then
  stage "Check: the existing uv executable. Preserve it if another version is installed."
else
  stage "Add: pinned native uv in protected tooling storage and expose it for maintenance."
fi
stage "Check: ordinary uv tool, command, and managed Python storage."
if $replace; then
  stage "The selected build will explicitly replace or repair an existing Site Ops installation."
fi
command -v az >/dev/null ||
  stage "Azure CLI was not found. Install it before deploying: https://aka.ms/installazurecli"
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

admit_directory "$data" private ||
  fail "Configure a private Site Ops data root under trusted, non-symlinked directories. Its parent directories must not be writable by other users or shared groups."
data="$admitted"

# Staged files rely on umask 077. Inherited access rules can override it, so
# check what new entries in private storage actually get.
keeps_private() {
  local entry owner mode
  : > "$1/probe-file" && mkdir -- "$1/probe-directory" || return 1
  for entry in "$1/probe-file" "$1/probe-directory"; do
    read -r owner mode < <(stat -c '%u %a' -- "$entry") || return 1
    [[ "$owner" == "$uid" ]] && (( (8#$mode & 8#077) == 0 )) || return 1
  done
  rm -rf -- "$1/probe-file" "$1/probe-directory"
}
admit_directory "$data/install-staging" private ||
  fail "Site Ops staging storage must be private. Inspect it before retrying."
staging_root="$admitted"
# Remove staging left by interrupted runs. A day exceeds any live installation.
find "$staging_root" -mindepth 1 -maxdepth 1 -mmin +1440 -exec rm -rf -- {} + 2>/dev/null || true
staging=""
trap '[[ -z "$staging" ]] || rm -rf -- "$staging"' EXIT
staging="$(mktemp -d "$staging_root/run.XXXXXXXXXX")" && admit_directory "$staging" private existing &&
  keeps_private "$admitted" ||
  fail "New files in the Site Ops data root do not stay private. Remove inherited access rules from it, then retry."
staging="$admitted"
mkdir -m 0700 -- "$staging/tmp"
export TMPDIR="$staging/tmp"
engine_release="$release"
engine_commit="$commit"
engine_ref="$source_ref"
engine_caller="$caller"
engine_version=""
reference_download=""
if [[ -n "$content_release" ]]; then
  reference_id="$(printf '%s\0' "$repository" "$release" "$commit" "$source_ref" "$caller" |
    sha256sum | cut -d ' ' -f 1)"
  admit_directory "$data/engine-references" private ||
    fail "The engine reference cache must use private storage."
  reference_cache="$admitted/$reference_id"
  reference_assets="$reference_cache"
  if [[ -e "$reference_cache" || -L "$reference_cache" ]]; then
    admit_directory "$reference_cache" private existing &&
      [[ "$(find "$reference_cache" -mindepth 1 -maxdepth 1 -printf x | wc -c)" == 2 ]] ||
      fail "Retained engine reference bytes are incomplete. Inspect them before retrying."
    stage "Rechecking the retained content release's engine selection."
  else
    reference_download="$staging/reference"
    mkdir -m 0700 "$reference_download"
    reference_assets="$reference_download"
    for asset in siteops-engine.json siteops-engine.json.attestation.jsonl; do
      limit=16384
      if [[ "$asset" == *.attestation.jsonl ]]; then limit=2097152; fi
      curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize "$limit" \
        -o "$reference_assets/$asset" "https://github.com/$repository/releases/download/$release/$asset" ||
        fail "The content release's signed engine reference is unavailable. Use that release's explicit engine installation instructions."
    done
  fi
  for asset in siteops-engine.json siteops-engine.json.attestation.jsonl; do
    limit=16384
    if [[ "$asset" == *.attestation.jsonl ]]; then limit=2097152; fi
    admit_file "$reference_assets/$asset" private && [[ -s "$reference_assets/$asset" ]] &&
      (( $(wc -c < "$reference_assets/$asset") <= limit )) ||
      fail "The engine reference or its proof is invalid or oversized."
  done
  stage "Checking the content release's engine reference."
  verify_release_asset "$reference_assets/siteops-engine.json" "$commit" "$source_ref" "$caller" _release-candidate.yaml
  prepare_runtime
  read_engine_reference "$reference_assets/siteops-engine.json" > "$staging/engine-selection" ||
    fail "The signed engine reference could not select an engine."
  mapfile -t selected_engine < "$staging/engine-selection"
  [[ ${#selected_engine[@]} == 9 ]] || fail "The engine selection is incomplete."
  engine_release="${selected_engine[0]}"
  engine_commit="${selected_engine[1]}"
  engine_ref="${selected_engine[2]}"
  engine_caller="${selected_engine[3]}"
  engine_version="${selected_engine[4]}"
  stage "Selected engine: $engine_version from $engine_release ($engine_commit)."
fi
selection_id="$(printf '%s\0' "$repository" "$engine_release" "$engine_commit" "$engine_ref" "$engine_caller" |
  sha256sum | cut -d ' ' -f 1)"
admit_directory "$data/install-downloads" private ||
  fail "The authenticated release cache must be private. Inspect it before retrying."
cache="$admitted/$selection_id"
encoded_release="${engine_release//\//%2F}"
url="https://github.com/$repository/releases/download/$encoded_release/"
assets="$staging"
if [[ -e "$cache" || -L "$cache" ]]; then
  admit_directory "$cache" private existing &&
    [[ "$(find "$cache" -mindepth 1 -maxdepth 1 -printf x | wc -c)" == 2 ]] ||
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
      -o "$staging/$asset" "$url$asset" || fail "A required release asset could not be downloaded."
    [[ -s "$staging/$asset" && $(wc -c < "$staging/$asset") -le $limit ]] ||
      fail "A required release asset is empty or exceeds its byte limit."
  done
fi
for asset in siteops-install.zip siteops-install.zip.attestation.jsonl; do
  limit=536870912
  if [[ "$asset" == *.attestation.jsonl ]]; then limit=2097152; fi
  admit_file "$assets/$asset" private && [[ -s "$assets/$asset" ]] &&
    (( $(wc -c < "$assets/$asset") <= limit )) ||
    fail "Retained release bytes are invalid or oversized."
done
if [[ -n "$content_release" ]]; then
  [[ "$(stat -c %s -- "$assets/siteops-install.zip")" == "${selected_engine[5]}" &&
     "$(sha256sum < "$assets/siteops-install.zip" | cut -d ' ' -f 1)" == "${selected_engine[6]}" &&
     "$(stat -c %s -- "$assets/siteops-install.zip.attestation.jsonl")" == "${selected_engine[7]}" &&
     "$(sha256sum < "$assets/siteops-install.zip.attestation.jsonl" | cut -d ' ' -f 1)" == "${selected_engine[8]}" ]] ||
    fail "The engine bundle or proof differs from the signed content selection."
fi
stage "Checking the bundle's source, signer, caller, and runner."
verify_release_asset "$assets/siteops-install.zip" "$engine_commit" "$engine_ref" "$engine_caller" _siteops-distribution.yaml

archive="$assets/siteops-install.zip"
bundle_id="$(sha256sum < "$archive" | cut -d ' ' -f 1)"
admit_directory "$data/bundles" private ||
  fail "The retained bundle store must be private. Inspect it before repair."
bundle="$admitted/$bundle_id"
if [[ -e "$bundle" || -L "$bundle" ]]; then
  admit_directory "$bundle" private existing ||
    fail "A retained installation directory must be owned by the current user and private."
fi

prepare_runtime
installer_helper="$(extract_installer_helper)" ||
  fail "The authenticated installer helper could not be prepared."
admit_file "$installer_helper" private ||
  fail "The authenticated installer helper could not be prepared."
mode=install
if $replace; then mode=replace; fi
helper_status=0
check_payload "$mode" "$archive" "$bundle" "$repository" "$engine_commit" "$engine_ref" \
  "$uv" "$tools" "$bin" > "$staging/installed.json" 2>/dev/null || helper_status=$?
case "$helper_status" in
  0) ;;
  2) fail "Another Site Ops selection is installed. Use --replace after review." ;;
  3) fail "The exposed command belongs to another installation. Remove it with its original manager." ;;
  4) fail "The tool has unrecognized Python startup files. Inspect it before using uv tool uninstall siteops." ;;
  *) fail "The bundle or installed payload failed validation." ;;
esac
(( $(wc -c < "$staging/installed.json") <= 4096 )) ||
  fail "The installer helper returned oversized results."
installed="$(< "$staging/installed.json")"
result='^\{"version": "([A-Za-z0-9][A-Za-z0-9.!+_-]{0,127})", "wheel": "wheels/[^/"]+\.whl"\}$'
[[ "$installed" =~ $result ]] || fail "The installer helper returned unsupported results."
version="${BASH_REMATCH[1]}"
[[ -z "$engine_version" || "$version" == "$engine_version" ]] ||
  fail "The installed version differs from the signed content selection."
siteops="$tools/siteops/bin/siteops"
[[ -L "$bin/siteops" && "$(readlink -- "$bin/siteops")" == "$siteops" ]] && admit_file "$siteops" ||
  fail "The exposed siteops command does not match the selected build."
export PATH="$bin:$PATH"
hash -r
[[ "$(command -v siteops)" == "$bin/siteops" &&
   "$(env "${python_unset[@]}" "$siteops" --version 2>/dev/null)" == "siteops $version" ]] ||
  fail "The exposed siteops command does not match the selected build."
stage "Command directory: $bin. Add it to your current PATH or open a new shell."
stage "For native removal, run uv tool uninstall siteops."
if [[ -n "$enroll_name" ]]; then
  trusted_root="$staging/trusted-root.jsonl"
  timeout --kill-after=5 120 "$gh" attestation trusted-root | head -c 2097153 > "$trusted_root" ||
    fail "The GitHub trusted-root snapshot could not be obtained."
  [[ -s "$trusted_root" && $(wc -c < "$trusted_root") -le 2097152 ]] ||
    fail "The trusted-root snapshot is empty or oversized."
  root_digest="$(sha256sum < "$trusted_root" | cut -d ' ' -f 1)"
  policy_file="$staging/source-policy.json"
  "$python" -I -S -B - "$policy_file" "$root_digest" "$repository" "$source_ref" "$caller" <<'PY'
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
  env "${python_unset[@]}" "$siteops" --trust-policy "$policy_file" --trusted-root "$trusted_root" \
    source enroll "$enroll_name" --source "github:$repository" ||
    fail "The approved source could not be enrolled."
fi
if [[ "$assets" == "$staging" ]]; then
  mkdir -m 0700 -- "$cache" || fail "The authenticated release cache could not be reserved."
  mv -- "$staging/siteops-install.zip" "$staging/siteops-install.zip.attestation.jsonl" "$cache/" ||
    fail "Authenticated release bytes could not be retained after installation."
fi
if [[ -n "$reference_download" ]]; then
  mkdir -m 0700 -- "$reference_cache" || fail "The engine reference cache could not be reserved."
  mv -- "$reference_download/siteops-engine.json" "$reference_download/siteops-engine.json.attestation.jsonl" \
    "$reference_cache/" || fail "The verified engine reference could not be retained."
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

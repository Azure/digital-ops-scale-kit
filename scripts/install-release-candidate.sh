#!/usr/bin/env bash
# Install a release candidate's engine from its downloaded payload, apart from
# any existing installation. The payload's bootstrap must match the bootstrap at
# the candidate commit. A private copy of that commit's bootstrap then verifies
# the bundle's source, signer, caller and runner, as it does for a published
# release.
set -euo pipefail
umask 077

fail() { printf 'Candidate installation failed: %s\n' "$1" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: bash install-release-candidate.sh --payload DIRECTORY --release TAG --source-commit COMMIT
         [--repository OWNER/REPOSITORY] [--source-ref REF] [--caller WORKFLOW] [--replace]

Checks that the payload's bootstrap matches scripts/bootstrap/siteops-bootstrap.sh
at the source commit in the clone that holds this script. Then places the
candidate's engine ZIP and proof in the bootstrap's retained release cache under
$HOME/siteops-candidate, and runs a private copy of the commit's bootstrap there
with the same values. uv, managed Python and Site Ops are installed there too.
--replace passes --replace to the bootstrap, to replace another candidate in the
same trial.
Defaults: --repository Azure/digital-ops-scale-kit --source-ref refs/heads/main
--caller release.yaml.
EOF
}

payload='' release='' commit='' replace=''
repository=Azure/digital-ops-scale-kit ref=refs/heads/main caller=release.yaml
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --replace) replace=--replace; shift ;;
    --payload|--release|--source-commit|--repository|--source-ref|--caller)
      (($# >= 2)) || fail "$1 requires a value."
      case "$1" in
        --payload) payload="$2" ;;
        --release) release="$2" ;;
        --source-commit) commit="$2" ;;
        --repository) repository="$2" ;;
        --source-ref) ref="$2" ;;
        --caller) caller="$2" ;;
      esac
      shift 2 ;;
    *) usage >&2; fail "Unknown option: $1" ;;
  esac
done
[[ -n "$payload" && -n "$release" && -n "$commit" ]] ||
  { usage >&2; fail "Choose --payload, --release and --source-commit."; }
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "--source-commit must be a full lowercase commit SHA."
for value in "$release" "$repository" "$ref" "$caller"; do
  [[ -n "$value" && "$value" != *[[:space:][:cntrl:]]* ]] ||
    fail "Release, repository, source ref and caller must be text without spaces."
done
[[ "${HOME:-}" == /* ]] || fail "HOME must be an absolute path."
for name in siteops-install.zip siteops-install.zip.attestation.jsonl siteops-bootstrap.sh; do
  [[ -f "$payload/$name" && ! -L "$payload/$name" ]] || fail "The payload has no regular file $name."
done
[[ "$(uname -s)" == Linux ]] || fail "Run the candidate installation on Linux, like the bootstrap it starts."

# The payload's bootstrap must be the bootstrap committed at the candidate
# commit in the clone that holds this script, so a changed payload is refused.
# Git reads only that clone, without replacement objects and without inherited
# variables that select another repository, object store or ref namespace.
source_dir="$(dirname -- "${BASH_SOURCE[0]}")"
source_git() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_INDEX_FILE -u GIT_NAMESPACE -u GIT_CEILING_DIRECTORIES \
    git --no-replace-objects -C "$source_dir" "$@"
}
bootstrap_blob="$commit:scripts/bootstrap/siteops-bootstrap.sh"
source_git cat-file -e "$commit^{commit}" 2>/dev/null ||
  fail "The clone that holds this script has no commit $commit. Clone the repository at the candidate commit first."
source_git cat-file blob "$bootstrap_blob" 2>/dev/null |
  cmp -s - "$payload/siteops-bootstrap.sh" ||
  fail "The payload's siteops-bootstrap.sh differs from scripts/bootstrap/siteops-bootstrap.sh at $commit."

# Storage admission copied from scripts/bootstrap/siteops-bootstrap.sh, so the
# trial is checked before anything is written. A test keeps the copies identical.
# If the bootstrap accepts local release files, it can admit this storage itself
# and these copies can go.
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
writers_trusted() {
  local mode=$((8#$3))
  [[ "$1" == "$uid" || "$1" == 0 ]] && (( (mode & 8#002) == 0 )) && {
    (( (mode & 8#020) == 0 )) || [[ "$1" == "$uid" && -n "$private_gid" && "$2" == "$private_gid" ]]
  }
}
trusted_parent() {
  local owner group mode
  [[ -d "$1" && ! -L "$1" ]] || return 1
  read -r owner group mode < <(stat -c '%u %g %a' -- "$1") || return 1
  writers_trusted "$owner" "$group" "$mode" ||
    { [[ "$owner" == "$uid" || "$owner" == 0 ]] && (( 8#$mode & 8#1000 )); }
}
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
      if [[ "$create" == check ]]; then
        [[ -w "${current%/*}/" ]]
        return
      fi
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

# The cache is kept between runs, so the probe is a directory this run creates under a new name.
# Its file is created only after the directory proves private, and the probe is always removed.
cache_keeps_private() {
  local probe entry owner mode status=0
  probe="$(mktemp -u -- "$1/.private-probe.XXXXXXXXXX")" && mkdir -- "$probe" || return 1
  for entry in "$probe" "$probe/file"; do
    [[ "$entry" == "$probe" ]] || : > "$entry" || { status=1; break; }
    read -r owner mode < <(stat -c '%u %a' -- "$entry") &&
      [[ "$owner" == "$uid" ]] && (( (8#$mode & 8#077) == 0 )) || { status=1; break; }
  done
  rm -rf -- "$probe"
  return "$status"
}

refuse_storage() {
  fail "Choose a HOME whose directories are not symbolic links and are not writable by other users or shared groups. $1"
}
uid="$(id -u)"
detect_private_group

# The bootstrap's selection key and data directories for this candidate.
key="$(printf '%s\0' "$repository" "$release" "$commit" "$ref" "$caller" | sha256sum | cut -d ' ' -f 1)"
admit_directory "$HOME/siteops-candidate" private ||
  refuse_storage "The trial directory must be private: $HOME/siteops-candidate."
trial="$admitted"
admit_directory "$trial/share/siteops/install-downloads/$key" private ||
  refuse_storage "The release cache must be private: $trial/share/siteops/install-downloads/$key."
cache="$admitted"
cache_keeps_private "$cache" ||
  fail "New files in $cache do not stay private. Remove inherited access rules from it, then retry."
export XDG_DATA_HOME="$trial/share"
export UV_TOOL_DIR="$trial/uv/tools"
export UV_TOOL_BIN_DIR="$trial/bin"
export UV_PYTHON_INSTALL_DIR="$trial/uv/python"
for name in siteops-install.zip siteops-install.zip.attestation.jsonl; do
  if [[ -e "$cache/$name" || -L "$cache/$name" ]]; then
    admit_file "$cache/$name" private && cmp -s "$payload/$name" "$cache/$name" ||
      fail "$cache already holds a different $name. Remove $trial before installing another candidate."
  else
    # Copy beside the cache and rename, so an interrupted copy leaves no partial file in it.
    partial="$(mktemp "$trial/.partial-XXXXXX")"
    trap 'rm -f -- "$partial"' EXIT
    cp -- "$payload/$name" "$partial"
    mv -T -- "$partial" "$cache/$name"
    trap - EXIT
    admit_file "$cache/$name" private || fail "The copied $name in $cache is not private."
  fi
done
printf 'Candidate files are in %s.\n' "$cache"
arguments=(--release "$release" --source-commit "$commit" --repository "$repository" --source-ref "$ref"
  --caller "$caller")
[[ -z "$replace" ]] || arguments+=("$replace")
# Run a private copy of the commit's bootstrap, so a later change to the payload never runs.
bootstrap="$(mktemp "$trial/.bootstrap-XXXXXX")"
trap 'rm -f -- "$bootstrap"' EXIT
source_git cat-file blob "$bootstrap_blob" > "$bootstrap" &&
  source_git cat-file blob "$bootstrap_blob" | cmp -s - "$bootstrap" &&
  admit_file "$bootstrap" private ||
  fail "The bootstrap at $commit could not be copied to $trial."
status=0
bash "$bootstrap" "${arguments[@]}" || status=$?
exit "$status"

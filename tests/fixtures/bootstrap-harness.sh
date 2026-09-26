#!/usr/bin/env bash
set -euo pipefail
umask 077

bootstrap="$1"
root="$(mktemp -d)"
trap 'rm -rf -- "$root"' EXIT
export TEST_ROOT="$root" HOME="$root/home" XDG_DATA_HOME="$root/state"
export REAL_PYTHON="$(command -v python3)"
mkdir -p "$root/home" "$root/bin"
export PATH="$root/bin:/usr/bin:/bin"

"$REAL_PYTHON" - "$root/archive.zip" <<'PY'
import hashlib
import json
import sys
import zipfile

lock = b'lock-version = "1.0"\n'
manifest = {
    "apiVersion": "siteops.install/v1",
    "source": {
        "repository": "Azure/digital-ops-scale-kit",
        "commit": "c" * 40,
        "ref": "refs/heads/main",
    },
    "package": {"version": "1.0.0b1"},
    "files": [{
        "path": "pylock.toml", "size": len(lock),
        "sha256": hashlib.sha256(lock).hexdigest(),
    }],
}
with zipfile.ZipFile(sys.argv[1], "w") as archive:
    archive.writestr("bundle.json", json.dumps(manifest))
    archive.writestr("pylock.toml", lock)
PY
printf 'opaque proof\n' > "$root/proof.jsonl"
export TEST_ARCHIVE="$root/archive.zip" TEST_PROOF="$root/proof.jsonl"
export TEST_LOG="$root/pipx-calls" TEST_BIN_DIR="$root/bin"
export TEST_DOWNLOAD_LOG="$root/downloads"
export TEST_VENV_LOG="$root/venv-calls"
: > "$TEST_LOG"
: > "$TEST_DOWNLOAD_LOG"
: > "$TEST_VENV_LOG"
if [[ "${TEST_HARNESS_MANAGED:-0}" == 1 ]]; then
  export PIPX_HOME=/usr/local/py-utils/pipx
  export PIPX_BIN_DIR=/usr/local/py-utils/bin
  export PIPX_SHARED_LIBS=/usr/local/py-utils/pipx/shared
  export PIPX_MAN_DIR=/usr/local/py-utils/man
  export PIPX_COMPLETION_DIR=/usr/local/py-utils/completions
  export TEST_FAIL_VENV=1
  if [[ "${TEST_NO_PIP_IN_VENV:-0}" == 1 ]]; then export TEST_FAIL_VENV=0; fi
fi

cat > "$root/bin/curl" <<'SH'
#!/usr/bin/env bash
output=""
for ((index=1; index<=$#; index++)); do
  if [[ "${!index}" == -o ]]; then next=$((index+1)); output="${!next}"; fi
done
[[ "$*" == *"https://github.com/Azure/digital-ops-scale-kit/releases/download/"* ]] || exit 99
printf '%s\n' "$*" >> "$TEST_DOWNLOAD_LOG"
if [[ "$*" == *"siteops-install.zip.attestation.jsonl" ]]; then
  cp "$TEST_PROOF" "$output"
elif [[ "$*" == *"siteops-install.zip" ]]; then
  cp "$TEST_ARCHIVE" "$output"
else
  exit 99
fi
SH
cat > "$root/bin/gh" <<'SH'
#!/usr/bin/env bash
case "$1 $2" in
  "version ")
    if [[ "${TEST_OLD_GH:-0}" == 1 ]]; then
      echo "gh version 2.94.0"
    else
      echo "gh version 2.95.0"
    fi ;;
  "attestation verify") printf '%s\n' "$TEST_VERIFY" ;;
  *) exit 99 ;;
esac
SH
cat > "$root/bin/pip-config" <<'SH'
#!/usr/bin/env bash
interpreter="$1"
if [[ "${TEST_NO_INDEX:-0}" != 1 ]]; then
  if [[ "${TEST_INSECURE_INDEX:-0}" == 1 ]]; then
    echo "global.index-url='http://packages.example.invalid/private-fixture/'"
  else
    echo "global.index-url='https://packages.example.invalid/private-fixture/'"
  fi
  if [[ "${TEST_EXTRA_INDEX:-0}" == 1 ]]; then
    echo "global.extra-index-url='https://other.example.invalid/simple/'"
  fi
  if [[ "${TEST_DOWNLOAD_INDEX:-0}" == unsafe ]]; then
    echo "download.index-url='http://other.example.invalid/simple/'"
  elif [[ "${TEST_DOWNLOAD_INDEX:-0}" == secure ]]; then
    echo "download.index-url='https://packages.example.invalid/approved-download/'"
  fi
  if [[ "$interpreter" == */backend-tools/bin/python ]]; then
    if [[ "${TEST_BACKEND_INDEX:-0}" == unsafe ]]; then
      echo "download.index-url='http://other.example.invalid/backend-only/'"
    elif [[ "${TEST_BACKEND_INDEX:-0}" == secure ]]; then
      echo "download.index-url='https://packages.example.invalid/backend-only/'"
    fi
  fi
fi
SH
cat > "$root/bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == -m && ( "$2" == venv || "$2" == virtualenv ) ]]; then
  target="${@: -1}"
  printf '%s %s\n' "$2" "$target" >> "$TEST_VENV_LOG"
  if [[ "$2" == venv && "${TEST_FAIL_VENV:-0}" == 1 ]]; then exit 7; fi
  if [[ "$2" == virtualenv && "${TEST_FAIL_VIRTUALENV:-0}" == 1 ]]; then exit 7; fi
  mkdir -p "$target/bin"
  if [[ "$2" == venv && "${TEST_NO_PIP_IN_VENV:-0}" == 1 ]]; then
    printf '#!/usr/bin/env bash\nexit 23\n' > "$target/bin/python"
    chmod +x "$target/bin/python"
    exit 0
  fi
  if [[ "$target" != */backend-tools ]]; then
    cat > "$target/bin/python" <<'PYTOOL'
#!/usr/bin/env bash
if [[ "$1 $2 $3" == "-m pip --version" ]]; then
  echo 'pip 26.2.1 from controlled fixture'
elif [[ "$1 $2 $3 $4" == "-m pip config list" ]]; then
  "$TEST_BIN_DIR/pip-config" "$0"
elif [[ "$1 $2 $3" == "-m pip install" ]]; then
  [[ -n "${TEST_PIPX_TEMPLATE:-}" ]] || exit 99
  cp "$TEST_PIPX_TEMPLATE" "$(dirname "$0")/pipx"
  chmod +x "$(dirname "$0")/pipx"
else
  exit 99
fi
PYTOOL
    chmod +x "$target/bin/python"
    exit 0
  fi
  cat > "$target/bin/python" <<'PYTOOL'
#!/usr/bin/env bash
  if [[ "$1 $2 $3 $4" == "-m pip config list" ]]; then
    exec "$TEST_BIN_DIR/pip-config" "$0"
  fi
  [[ "$1 $2" == "-m pip" && "$3" == download ]] || exit 99
[[ "${TEST_FAIL_PIP:-0}" != 1 ]] || exit 7
while (($#)); do
  if [[ "$1" == --dest ]]; then dest="$2"; shift 2; else shift; fi
done
printf 'controlled wheel\n' > "$dest/pip-26.2.1-py3-none-any.whl"
PYTOOL
  chmod +x "$target/bin/python"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
SH
cat > "$root/bin/sha256sum" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == --check ]]; then
  read -r digest file
  if [[ "$file" == *pip-26.2.1-*.whl ]]; then
    [[ "$digest" == 71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e ]]
    exit
  fi
  printf '%s  %s\n' "$digest" "$file" | /usr/bin/sha256sum --check --status
  exit
fi
exec /usr/bin/sha256sum "$@"
SH
cat > "$root/bin/pipx" <<'SH'
#!/usr/bin/env bash
case "$1" in
  --version) echo 1.17.2 ;;
  list)
    if [[ -n "${TEST_LOCK_PATH:-}" ]]; then
      printf '{"venvs":{"siteops":{"metadata":{"main_package":{"lock_file":{"__Path__":"%s"}}}}}}\n' "$TEST_LOCK_PATH"
    else
      printf '{"venvs":{}}\n'
    fi ;;
  upgrade-shared|install|ensurepath)
    printf '%s HOME=%s BIN=%s SHARED=%s MAN=%s COMPLETIONS=%s\n' "$*" \
      "${PIPX_HOME:-}" "${PIPX_BIN_DIR:-}" "${PIPX_SHARED_LIBS:-}" \
      "${PIPX_MAN_DIR:-}" "${PIPX_COMPLETION_DIR:-}" >> "$TEST_LOG"
    if [[ "$1" == install && -n "${PIPX_BIN_DIR:-}" ]]; then
      mkdir -p "$PIPX_BIN_DIR"
      cp "$TEST_BIN_DIR/siteops" "$PIPX_BIN_DIR/siteops"
    fi ;;
  runpip) echo 'pip 26.2.1 from controlled fixture' ;;
  environment)
    [[ "$2" == --value ]] || exit 99
    case "$3" in
      PIPX_HOME) echo "${PIPX_HOME:-$HOME/.local/share/pipx}" ;;
      PIPX_BIN_DIR) echo "${PIPX_BIN_DIR:-$TEST_BIN_DIR}" ;;
      PIPX_SHARED_LIBS) echo "${PIPX_SHARED_LIBS:-$HOME/.local/share/pipx/shared}" ;;
      *) exit 99 ;;
    esac ;;
  *) exit 99 ;;
esac
SH
cat > "$root/bin/siteops" <<'SH'
#!/usr/bin/env bash
[[ "$1" == --version ]] || exit 99
echo 'siteops 1.0.0b1'
SH
cat > "$root/bin/sudo" <<'SH'
#!/usr/bin/env bash
echo "Unexpected OS installation in isolated test." >&2
exit 99
SH
chmod +x "$root/bin/"*
if [[ "${TEST_HARNESS_NO_PIPX:-0}" == 1 ]]; then
  export TEST_PIPX_TEMPLATE="$root/pipx-template"
  mv "$root/bin/pipx" "$TEST_PIPX_TEMPLATE"
fi

arguments=(--release siteops/v1.0.0b1 --source-commit "$(printf 'c%.0s' {1..40})" --yes)
export TEST_UNTRUSTED_LOG="$root/untrusted-pipx-calls"
cat > "$root/untrusted-pipx" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_UNTRUSTED_LOG"
echo 1.17.2
SH
chmod +x "$root/untrusted-pipx"
shared="$root/shared-data"
mkdir -p "$shared/siteops/tools/pipx/bin"
cp "$root/untrusted-pipx" "$shared/siteops/tools/pipx/bin/pipx"
chmod 0777 "$shared"
if XDG_DATA_HOME="$shared" bash "$bootstrap" "${arguments[@]}" > "$root/shared-root.log" 2>&1; then
  echo "A shared data root was accepted." >&2
  exit 1
fi
[[ ! -s "$TEST_UNTRUSTED_LOG" ]] ||
  { echo "A shared data root executed an untrusted pipx." >&2; exit 1; }
grep -q 'private Site Ops data root' "$root/shared-root.log"
private="$root/private-data"
mkdir -p "$private/siteops/tools/pipx/bin"
cp "$root/untrusted-pipx" "$private/siteops/tools/pipx/bin/pipx"
ln -s "$private" "$root/linked-data"
if XDG_DATA_HOME="$root/linked-data" bash "$bootstrap" "${arguments[@]}" > "$root/linked-root.log" 2>&1; then
  echo "A symlinked data root was accepted." >&2
  exit 1
fi
[[ ! -s "$TEST_UNTRUSTED_LOG" ]] ||
  { echo "A symlinked data root executed an untrusted pipx." >&2; exit 1; }
grep -q 'private Site Ops data root' "$root/linked-root.log"
if [[ "${TEST_HARNESS_MANAGED:-0}" == 1 ]]; then
  export TEST_OLD_GH=1
  if bash "$bootstrap" "${arguments[@]}" > "$root/old-gh.log" 2>&1; then
    echo "Managed Azure Linux accepted an unsupported verifier." >&2
    exit 1
  fi
  grep -q 'Managed Azure Linux requires compatible OS tools' "$root/old-gh.log"
  [[ ! -s "$TEST_DOWNLOAD_LOG" && ! -s "$TEST_LOG" ]] ||
    { echo "Missing OS tools changed installation state." >&2; exit 1; }
  export TEST_OLD_GH=0 TEST_FAIL_VIRTUALENV=1
  if bash "$bootstrap" "${arguments[@]}" > "$root/no-venv.log" 2>&1; then
    echo "Managed Azure Linux accepted a missing Python environment." >&2
    exit 1
  fi
  grep -q 'needs a pip-equipped venv' "$root/no-venv.log"
  [[ ! -s "$TEST_DOWNLOAD_LOG" && ! -s "$TEST_LOG" ]] ||
    { echo "Missing venv changed installation state." >&2; exit 1; }
  export TEST_FAIL_VIRTUALENV=0
fi
if [[ "${TEST_HARNESS_SHARED_OK:-0}" == 1 ]]; then
  export TEST_BIN_DIR="$HOME/.local/bin"
  mkdir -p "$TEST_BIN_DIR"
  cp "$root/bin/siteops" "$TEST_BIN_DIR/siteops"
  shared="$HOME/.local/share/pipx/shared/bin"
  mkdir -p "$shared"
  cat > "$shared/python" <<'PYTOOL'
#!/usr/bin/env bash
echo 'pip 26.2.1 from controlled fixture'
PYTOOL
  chmod +x "$shared/python"
  export TEST_VERIFY=true
  bash "$bootstrap" "${arguments[@]}" > "$root/existing-shared.log" 2>&1 ||
    { cat "$root/existing-shared.log" >&2; exit 1; }
  grep -q 'install siteops --lock' "$TEST_LOG"
  if grep -q 'upgrade-shared' "$TEST_LOG"; then
    echo "A compatible pipx shared backend was changed." >&2
    exit 1
  fi
  echo 'Existing shared pipx backend was preserved with isolated tools and no network.'
  exit 0
fi
export TEST_VERIFY=false
if bash "$bootstrap" "${arguments[@]}" > "$root/rejected.log" 2>&1; then
  echo "Invalid proof executed the installer." >&2
  exit 1
fi
[[ ! -d "$root/state/siteops/bundles" && ! -s "$TEST_LOG" ]] ||
  { echo "Invalid proof changed installation state." >&2; exit 1; }

export TEST_VERIFY=true TEST_FAIL_PIP=1
export TEST_NO_INDEX=1
if bash "$bootstrap" "${arguments[@]}" > "$root/no-index.log" 2>&1; then
  echo "An unconfigured Python index was accepted." >&2
  exit 1
fi
grep -q 'Configure one approved HTTPS Python index' "$root/no-index.log"
[[ ! -s "$TEST_LOG" ]] ||
  { echo "Missing index changed pipx tooling." >&2; exit 1; }
export TEST_NO_INDEX=0
for setting in TEST_INSECURE_INDEX TEST_EXTRA_INDEX; do
  export "$setting"=1
  if bash "$bootstrap" "${arguments[@]}" > "$root/invalid-index.log" 2>&1; then
    echo "An unapproved Python index was accepted." >&2
    exit 1
  fi
  grep -q 'Configure one approved HTTPS Python index' "$root/invalid-index.log"
  [[ ! -s "$TEST_LOG" ]] ||
    { echo "An unapproved index changed pipx tooling." >&2; exit 1; }
  export "$setting"=0
done
export TEST_DOWNLOAD_INDEX=unsafe
if bash "$bootstrap" "${arguments[@]}" > "$root/invalid-download-index.log" 2>&1; then
  echo "An unapproved download index was accepted." >&2
  exit 1
fi
grep -q 'Configure one approved HTTPS Python index' "$root/invalid-download-index.log" ||
  { echo "An unapproved download index reached the backend path." >&2; exit 1; }
[[ ! -s "$TEST_LOG" ]] ||
  { echo "An unapproved download index changed pipx tooling." >&2; exit 1; }
export TEST_DOWNLOAD_INDEX=secure
if bash "$bootstrap" "${arguments[@]}" > "$root/approved-download-index.log" 2>&1; then
  echo "The controlled backend download unexpectedly succeeded." >&2
  exit 1
fi
grep -q 'shared backend could not be downloaded' "$root/approved-download-index.log"
export TEST_DOWNLOAD_INDEX=
export TEST_BACKEND_INDEX=unsafe
if bash "$bootstrap" "${arguments[@]}" > "$root/invalid-backend-index.log" 2>&1; then
  echo "A backend-specific unapproved index was accepted." >&2
  exit 1
fi
grep -q 'Configure one approved HTTPS Python index' "$root/invalid-backend-index.log" ||
  { echo "An unapproved backend interpreter index reached the backend path." >&2; exit 1; }
[[ ! -s "$TEST_LOG" ]] ||
  { echo "An unapproved backend interpreter index changed pipx tooling." >&2; exit 1; }
export TEST_BACKEND_INDEX=secure
if bash "$bootstrap" "${arguments[@]}" > "$root/approved-backend-index.log" 2>&1; then
  echo "The controlled backend download unexpectedly succeeded." >&2
  exit 1
fi
grep -q 'shared backend could not be downloaded' "$root/approved-backend-index.log"
export TEST_BACKEND_INDEX=
if bash "$bootstrap" "${arguments[@]}" > "$root/failed-feed.log" 2>&1; then
  echo "A missing backend wheel was accepted." >&2
  exit 1
fi
bundle="$root/state/siteops/bundles/$(/usr/bin/sha256sum "$TEST_ARCHIVE" | cut -d ' ' -f 1)"
[[ -f "$bundle/pylock.toml" ]] ||
  { cat "$root/failed-feed.log" >&2;
    echo "Controlled partial bundle was not retained." >&2; exit 1; }
if grep -q 'packages.example.invalid' "$root/failed-feed.log"; then
  echo "Private feed details were printed to the console." >&2
  exit 1
fi
export TEST_FAIL_PIP=0
bash "$bootstrap" "${arguments[@]}" > "$root/recovered.log" 2>&1 ||
  { cat "$root/recovered.log" >&2; exit 1; }
grep -q 'install siteops --lock' "$TEST_LOG"

export TEST_LOCK_PATH="$bundle/pylock.toml"
before="$(wc -l < "$TEST_LOG")"
downloads_before="$(wc -l < "$TEST_DOWNLOAD_LOG")"
bash "$bootstrap" "${arguments[@]}" > "$root/repeated.log" 2>&1 ||
  { cat "$root/repeated.log" >&2; exit 1; }
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "Repeated install changed pipx tooling." >&2; exit 1; }
[[ "$(wc -l < "$TEST_DOWNLOAD_LOG")" == "$downloads_before" ]] ||
  { echo "Repeated install downloaded the same authenticated release." >&2; exit 1; }

export TEST_LOCK_PATH="/other/pylock.toml"
if bash "$bootstrap" "${arguments[@]}" > "$root/foreign.log" 2>&1; then
  echo "Another installed build was replaced implicitly." >&2
  exit 1
fi
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "Rejected replacement changed pipx tooling." >&2; exit 1; }
bash "$bootstrap" "${arguments[@]}" --replace > "$root/replaced.log" 2>&1 ||
  { cat "$root/replaced.log" >&2; exit 1; }
grep -q 'install siteops --lock .* --force' "$TEST_LOG"
cp "$bundle/bundle.json" "$root/bundle-manifest"
printf '\n' >> "$bundle/bundle.json"
before="$(wc -l < "$TEST_LOG")"
if bash "$bootstrap" "${arguments[@]}" --replace > "$root/tampered-manifest.log" 2>&1; then
  echo "A changed retained bundle manifest was accepted." >&2
  exit 1
fi
grep -q 'retained bundle manifest differs' "$root/tampered-manifest.log"
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "A changed retained manifest changed pipx tooling." >&2; exit 1; }
cp "$root/bundle-manifest" "$bundle/bundle.json"
printf 'altered bytes\n' >> "$bundle/pylock.toml"
if bash "$bootstrap" "${arguments[@]}" --replace > "$root/tampered-wheel.log" 2>&1; then
  echo "A changed retained bundle file was accepted." >&2
  exit 1
fi
grep -q 'retained bundle contents differ' "$root/tampered-wheel.log"
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "A changed retained file changed pipx tooling." >&2; exit 1; }
if [[ "${TEST_HARNESS_MANAGED:-0}" == 1 ]]; then
  grep -q 'virtualenv .*/backend-tools' "$TEST_VENV_LOG"
  grep -q "install siteops --lock .* HOME=$root/state/siteops/pipx BIN=$root/state/siteops/bin SHARED=$root/state/siteops/pipx/shared MAN=$root/state/siteops/man COMPLETIONS=$root/state/siteops/completions" "$TEST_LOG"
  [[ -x "$root/state/siteops/bin/siteops" ]] ||
    { echo "Site Ops was not installed in private pipx storage." >&2; exit 1; }
  if grep -q 'BIN=/usr/local/py-utils' "$TEST_LOG"; then
    echo "The managed pipx installation was changed." >&2
    exit 1
  fi
  if [[ "${TEST_HARNESS_NO_PIPX:-0}" == 1 ]]; then
    [[ -x "$root/state/siteops/tools/pipx/bin/pipx" ]] ||
      { echo "Missing pipx was not installed in user storage." >&2; exit 1; }
  fi
fi
if [[ "${TEST_NO_PIP_IN_VENV:-0}" == 1 ]]; then
  grep -q 'virtualenv .*/backend-tools' "$TEST_VENV_LOG" ||
    { echo "A pip-free venv did not use available virtualenv." >&2; exit 1; }
fi
echo 'Bootstrap control flow passed with isolated tools and no network.'

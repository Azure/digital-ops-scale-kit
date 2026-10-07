#!/usr/bin/env bash
# Exercise the Linux bootstrap with native uv, a real uv-managed runtime and
# the shipped installer helper. Downloads, provenance and OS package tools are
# controlled doubles that reject unexpected calls. The pinned uv archive and
# CPython archive are supplied locally, and the runtime is served through a
# local HTTPS mirror signed by a disposable test authority.
set -euo pipefail
umask 077

bootstrap="$1"
installer="$2"
scenario="${TEST_SCENARIO:-journey}"
for input in TEST_BUNDLE_ARCHIVE TEST_REPLACEMENT_ARCHIVE TEST_UV_ARCHIVE TEST_PYTHON_ARCHIVE; do
  [[ -f "${!input:-}" ]] || { echo "Bootstrap harness input $input is missing." >&2; exit 2; }
done
root="$(mktemp -d)"
server=""
cleanup() {
  if [[ -n "$server" ]]; then kill "$server" 2>/dev/null || true; fi
  chmod -R u+rwX -- "$root" 2>/dev/null || true
  rm -rf -- "$root"
}
trap cleanup EXIT
die() { printf 'Bootstrap harness: %s\n' "$1" >&2; exit 1; }
real_python="$(command -v python3)"
uv_url=https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-unknown-linux-gnu.tar.gz
uv_sha=b8299463da6fa7da3b94464444d252d0afca8ac6c96cb229f1baf4012f365246
first=(--release siteops/v1.0.0b1 --source-commit "$(printf 'a%.0s' {1..40})" --yes)
second=(--release siteops/v1.0.0b2 --source-commit "$(printf 'b%.0s' {1..40})" --yes)
first_version=1.0.0b1+build.1.1.gaaaaaaaaaaaa
second_version=1.0.0b1+build.2.1.gbbbbbbbbbbbb

"$real_python" -I - "$TEST_BUNDLE_ARCHIVE" "$TEST_REPLACEMENT_ARCHIVE" "$installer" <<'PY' ||
import sys
import zipfile

with open(sys.argv[3], "rb") as source:
    helper = source.read()
for path in sys.argv[1:3]:
    with zipfile.ZipFile(path) as archive:
        if archive.read("siteops-install.py") != helper:
            raise SystemExit(1)
PY
  die "Each fixture bundle must carry the shared installer helper."

logs="$root/logs"
doubles="$root/doubles"
system="$root/system"
mkdir -p "$logs" "$doubles" "$system" "$root/tmp" "$root/homes" "$root/inject/siteops"
: > "$logs/curl"
: > "$logs/gh"
: > "$logs/mirror"
: > "$logs/rejected"
: > "$logs/siteops"
for tool in bash cat chmod cmp cp cut env find getconf gzip head id mkdir mktemp mv readlink rm \
    sha256sum stat tar tee timeout uname wc; do
  ln -s "$(command -v "$tool")" "$system/$tool"
done
# Account lookups pass through unless a scenario simulates a private or shared group.
real_getent="$(command -v getent)"
cat > "$doubles/getent" <<SH
#!/usr/bin/env bash
user="\$("$real_getent" passwd "\$(id -u)")" || exit 2
case "\${TEST_GETENT:-}:\$1" in
  private:group) printf '%s:x:%s:\n' "\${user%%:*}" "\$2" ;;
  shared:group) printf '%s:x:%s:%s,intruder\n' "\${user%%:*}" "\$2" "\${user%%:*}" ;;
  private:passwd|shared:passwd) [[ \$# == 1 ]] && printf '%s\n' "\$user" || "$real_getent" "\$@" ;;
  *) exec "$real_getent" "\$@" ;;
esac
SH
for tool in apt apt-get dpkg gpg pip pip3 pipx python python3 sudo tdnf; do
  printf '#!/usr/bin/env bash\nprintf "%%s %%s\\n" %s "$*" >> "$TEST_LOGS/rejected"\nexit 99\n' \
    "$tool" > "$doubles/$tool"
done
cat > "$doubles/curl" <<'SH'
#!/usr/bin/env bash
output=""
for ((index = 1; index <= $#; index++)); do
  if [[ "${!index}" == -o ]]; then next=$((index + 1)); output="${!next}"; fi
done
target="${!#}"
printf '%s\n' "$target" >> "$TEST_LOGS/curl"
[[ " $* " == *" --proto =https "* && " $* " == *" --tlsv1.2 "* &&
   " $* " == *" --max-filesize "* && -n "$output" ]] ||
  { echo "curl unbounded $target" >> "$TEST_LOGS/rejected"; exit 98; }
release=https://github.com/example/publisher/releases/download
case "$target" in
  "$release/siteops%2Fv1.0.0b1/siteops-install.zip") cp -- "$TEST_BUNDLE_ARCHIVE" "$output" ;;
  "$release/siteops%2Fv1.0.0b2/siteops-install.zip") cp -- "$TEST_REPLACEMENT_ARCHIVE" "$output" ;;
  "$release/siteops%2Fv1.2.3/siteops-install.zip") cp -- "$TEST_REFERENCED_ARCHIVE" "$output" ;;
  "$release/v1.0.0b7/siteops-install.zip") cp -- "$TEST_BUNDLE_ARCHIVE" "$output" ;;
  "$release/v1.0.0b"[78]/siteops-engine.json)
    [[ "${TEST_NO_REFERENCE:-0}" == 0 ]] || exit 22
    cp -- "$TEST_REFERENCE_ROOT/${target##*/download/}" "$output" ;;
  "$release/v1.0.0b"[78]/siteops-engine.json.attestation.jsonl)
    printf 'opaque reference proof\n' > "$output" ;;
  "$release/siteops%2Fv1.2.3/siteops-install.zip.attestation.jsonl"|"$release/v1.0.0b7/siteops-install.zip.attestation.jsonl")
    printf 'opaque proof\n' > "$output" ;;
  "$release/siteops%2Fv1.0.0b"[12]/siteops-install.zip.attestation.jsonl)
    printf 'opaque proof\n' > "$output" ;;
  https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-unknown-linux-gnu.tar.gz)
    cp -- "$TEST_UV_ARCHIVE" "$output"
    if [[ "${TEST_TAMPER_UV:-0}" == 1 ]]; then printf 'x' >> "$output"; fi ;;
  *) echo "curl $target" >> "$TEST_LOGS/rejected"; exit 99 ;;
esac
SH
cat > "$doubles/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_LOGS/gh"
case "${1:-} ${2:-}" in
  "version ")
    if [[ "${TEST_OLD_GH:-0}" == 1 ]]; then echo "gh version 2.94.0 (fixture)"
    else echo "gh version 2.95.0 (fixture)"; fi ;;
  "attestation verify")
    [[ " $* " == *" --repo example/publisher "* && -f "$3" ]] || exit 97
    if [[ "$3" == */siteops-engine.json ]]; then
      [[ " $* " == *"/_release-candidate.yaml@"* ]] || exit 97
      printf '%s\n' "${TEST_REFERENCE_VERIFY:-true}"
      exit 0
    fi
    [[ "${TEST_VERIFY:-true}" != error ]] || exit 1
    printf '%s\n' "${TEST_VERIFY:-true}" ;;
  "attestation trusted-root")
    printf '{"mediaType":"application/vnd.dev.sigstore.trustedroot+json;version=0.1"}\n' ;;
  *) echo "gh $*" >> "$TEST_LOGS/rejected"; exit 99 ;;
esac
SH
chmod 0755 "$doubles"/*
# Inherited Python settings must never reach the installed command.
printf 'import pathlib\npathlib.Path(%s).touch()\n' "'$root/inject-ran'" > "$root/inject/sitecustomize.py"
cp "$root/inject/sitecustomize.py" "$root/inject/siteops/__init__.py"

tls="$root/tls"
mkdir -p "$tls" "$root/mirror/20260924"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=siteops-harness-authority \
  -keyout "$tls/ca.key" -out "$tls/ca.pem" -addext basicConstraints=critical,CA:TRUE \
  -addext keyUsage=critical,keyCertSign > /dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj /CN=127.0.0.1 -keyout "$tls/server.key" \
  -out "$tls/server.csr" > /dev/null 2>&1
printf 'subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' \
  > "$tls/extensions"
openssl x509 -req -in "$tls/server.csr" -CA "$tls/ca.pem" -CAkey "$tls/ca.key" \
  -CAcreateserial -days 1 -extfile "$tls/extensions" -out "$tls/server.pem" > /dev/null 2>&1
cp -- "$TEST_PYTHON_ARCHIVE" \
  "$root/mirror/20260924/cpython-3.11.16+20260924-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
"$real_python" -I - "$tls" "$root/mirror" "$logs/mirror" <<'PY' &
import functools
import http.server
import pathlib
import ssl
import sys

tls, content, log = map(pathlib.Path, sys.argv[1:])
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(tls / "server.pem", tls / "server.key")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(self.path + "\n")


server = http.server.ThreadingHTTPServer(
    ("127.0.0.1", 0), functools.partial(Handler, directory=str(content)),
)
server.socket = context.wrap_socket(server.socket, server_side=True)
(tls / "port.partial").write_text(str(server.server_address[1]), encoding="ascii")
(tls / "port.partial").rename(tls / "port")
server.serve_forever()
PY
server=$!
for _ in $(seq 100); do [[ -f "$tls/port" ]] && break; sleep 0.1; done
[[ -f "$tls/port" ]] || die "The local runtime mirror did not start."
mirror_url="https://127.0.0.1:$(< "$tls/port")"
mkdir -p "$root/seed"
tar -xzOf "$TEST_UV_ARCHIVE" uv-x86_64-unknown-linux-gnu/uv > "$root/seed/uv"
chmod 0700 "$root/seed/uv"

count() { local lines; lines="$(wc -l < "$1")"; printf '%s' "$((lines))"; }
fresh_home() {
  HOME="$root/homes/$1"
  mkdir -p "$HOME/.local/share"
  # Ubuntu 24.04 creates 0750 homes. Ordinary XDG parents are often 0755.
  chmod 0750 "$HOME"
  chmod 0755 "$HOME/.local" "$HOME/.local/share"
  data="$HOME/.local/share/siteops"
  tools="$HOME/.local/share/uv/tools"
  bin="$HOME/.local/bin"
  pydir="$HOME/.local/share/uv/python"
  extra=()
  bootstrap_path="$doubles:$system"
}
reference_root="$root/references"
if [[ "$scenario" == content ]]; then
  [[ -f "${TEST_REFERENCED_ARCHIVE:-}" ]] || die "The referenced engine fixture is required."
  "$real_python" -I - "$reference_root" "$TEST_BUNDLE_ARCHIVE" "$TEST_REFERENCED_ARCHIVE" <<'PY'
import hashlib
import json
import pathlib
import sys
import zipfile

root = pathlib.Path(sys.argv[1])
for tag, revision, engine_tag, engine_revision, path, preview in (
    ("v1.0.0b7", "a" * 40, "v1.0.0b7", "a" * 40, pathlib.Path(sys.argv[2]), False),
    ("v1.0.0b8", "c" * 40, "siteops/v1.2.3", "d" * 40, pathlib.Path(sys.argv[3]), True),
):
    raw = path.read_bytes()
    proof = b"opaque proof\n"
    with zipfile.ZipFile(path) as archive:
        version = json.loads(archive.read("bundle.json"))["package"]["version"]
    record = {
        "apiVersion": "siteops.release.engine/v1", "kind": "EngineReference",
        "release": tag, "revision": revision, "preview": preview,
        "engine": {
            "release": engine_tag, "revision": engine_revision, "version": version,
            "bundle": {"name": "siteops-install.zip", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "proof": {"name": "siteops-install.zip.attestation.jsonl", "size": len(proof), "sha256": hashlib.sha256(proof).hexdigest()},
        },
    }
    directory = root / tag
    directory.mkdir(parents=True)
    (directory / "siteops-engine.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
    )
PY
fi
run_bootstrap() {
  local label="$1"
  shift
  env -i HOME="$HOME" PATH="$bootstrap_path" TMPDIR="$root/tmp" LC_ALL=C \
    SSL_CERT_FILE="$tls/ca.pem" UV_PYTHON_INSTALL_MIRROR="$mirror_url" \
    PYTHONPATH="$root/inject" TEST_LOGS="$logs" TEST_SITEOPS_LOG="$logs/siteops" \
    TEST_BUNDLE_ARCHIVE="$TEST_BUNDLE_ARCHIVE" TEST_REPLACEMENT_ARCHIVE="$TEST_REPLACEMENT_ARCHIVE" \
    TEST_REFERENCED_ARCHIVE="${TEST_REFERENCED_ARCHIVE:-}" TEST_REFERENCE_ROOT="$reference_root" \
    TEST_REFERENCE_VERIFY="${reference_verify:-true}" TEST_NO_REFERENCE="${no_reference:-0}" \
    TEST_UV_ARCHIVE="$TEST_UV_ARCHIVE" TEST_VERIFY="${verify:-true}" \
    TEST_OLD_GH="${old_gh:-0}" TEST_TAMPER_UV="${tamper_uv:-0}" TEST_GETENT="${getent_mode:-}" "${extra[@]}" \
    bash "$bootstrap" --repository example/publisher "$@" > "$logs/$label.out" 2>&1
}
succeeds() {
  local label="$1"
  shift
  run_bootstrap "$label" "$@" || { cat "$logs/$label.out" >&2; die "$label failed."; }
}
refuses() {
  local label="$1" message="$2"
  shift 2
  if run_bootstrap "$label" "$@"; then
    cat "$logs/$label.out" >&2
    die "$label was accepted."
  fi
  grep -qF -- "$message" "$logs/$label.out" ||
    { cat "$logs/$label.out" >&2; die "$label did not report: $message"; }
}
no_uv_download() { ! grep -qxF "$uv_url" "$logs/curl" || die "$1 acquired uv."; }
installed_version() {
  env -i PATH="$system" "$1/siteops" --version
}
seed_runtime() {
  env -i HOME="$HOME" PATH="$system" SSL_CERT_FILE="$tls/ca.pem" \
    UV_PYTHON_INSTALL_MIRROR="$mirror_url" UV_PYTHON_INSTALL_DIR="$1" \
    UV_CACHE_DIR="$root/seed-cache" "$root/seed/uv" python install 3.11.16 --no-bin \
    --no-registry --no-config --no-progress > "$logs/seed.out" 2>&1 ||
    { cat "$logs/seed.out" >&2; die "The existing runtime fixture could not be prepared."; }
}
planted() {
  mkdir -p "${1%/*}"
  printf '#!/usr/bin/env bash\n: > %q\nexit 1\n' "$2" > "$1"
  chmod 0755 "$1"
}
# Give new entries owner rwx, group r-x and other rwx regardless of umask.
inherit_open_access() {
  "$real_python" -I - "$1" <<'PY' || die "The fixture filesystem must support POSIX default ACLs."
import os
import struct
import sys

entries = ((0x01, 7), (0x04, 5), (0x20, 7))
value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, permissions, 0xFFFFFFFF)
                                        for tag, permissions in entries)
os.setxattr(sys.argv[1], "system.posix_acl_default", value)
PY
  ( umask 077; : > "$1/check" )
  [[ "$(stat -c %a "$1/check")" == 646 ]] || die "The inherited access fixture did not override the umask."
  rm -- "$1/check"
}
scenario_journey() {
  local receipt identity second_bundle downloads
  fresh_home journey
  succeeds install "${first[@]}" --enroll-source demo
  [[ "$(count "$logs/curl")" == 3 ]] && grep -qxF "$uv_url" "$logs/curl" ||
    die "Fresh installation made unexpected downloads."
  [[ "$(count "$logs/mirror")" == 1 ]] ||
    die "The managed runtime was not provisioned once through the approved mirror."
  [[ -L "$bin/siteops" && "$(readlink "$bin/siteops")" == "$tools/siteops/bin/siteops" ]] ||
    die "Native uv did not expose siteops."
  [[ "$(sha256sum < "$bin/uv" | cut -d ' ' -f 1)" == "$uv_sha" ]] ||
    die "The pinned uv was not exposed for maintenance."
  [[ ! -e "$bin/python3.11" && ! -e "$bin/python3" && ! -e "$bin/python" ]] ||
    die "Managed Python command aliases were exposed."
  grep -qxF "home = $pydir/cpython-3.11.16-linux-x86_64-gnu/bin" "$tools/siteops/pyvenv.cfg" ||
    die "Site Ops is not bound to the concrete managed runtime."
  [[ "$(installed_version "$bin")" == "siteops $first_version" ]] ||
    die "The installed command reports another build."
  grep -qF "Installed siteops $first_version with approved source demo." "$logs/install.out" ||
    die "The installation did not report the enrolled build."
  grep -qF "Azure CLI was not found. Install it before deploying:" "$logs/install.out" ||
    die "A missing Azure CLI was not reported."
  [[ -d "$data/install-staging" && -z "$(find "$data/install-staging" -mindepth 1 -print -quit)" ]] ||
    die "Installation staging was not removed."
  ! grep -q '127\.0\.0\.1' "$logs/install.out" || die "Runtime source details were printed."
  [[ "$(find "$data/install-downloads" -type f | wc -l)" == 2 ]] ||
    die "The authenticated release was not retained."
  "$real_python" -I - "$logs/siteops" <<'PY' || die "Source enrollment used unexpected inputs."
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
enrollments = [item for item in records if item["arguments"][:1] == ["--trust-policy"]]
assert len(enrollments) == 1
arguments = enrollments[0]["arguments"]
assert arguments[2] == "--trusted-root"
assert arguments[4:] == ["source", "enroll", "demo", "--source", "github:example/publisher"]
provider = enrollments[0]["policy"]["provider"]
assert provider["repository"] == "example/publisher"
assert provider["sourceRef"] == "refs/heads/main"
assert provider["builderWorkflow"] == ".github/workflows/release.yaml"
assert all(item["python"] == [] for item in records)
PY
  [[ ! -e "$root/inject-ran" ]] || die "Inherited Python paths reached the installed command."

  receipt="$(sha256sum < "$tools/siteops/uv-receipt.toml")"
  identity="$(stat -c %i "$tools/siteops/pyvenv.cfg")"
  succeeds repeat "${first[@]}"
  grep -qF "Rechecking the retained release without downloading its assets." "$logs/repeat.out" ||
    die "Repeat installation did not reuse the retained release."
  [[ "$(count "$logs/curl")" == 3 && "$(count "$logs/mirror")" == 1 ]] ||
    die "Repeat installation downloaded content again."
  [[ "$(sha256sum < "$tools/siteops/uv-receipt.toml")" == "$receipt" &&
     "$(stat -c %i "$tools/siteops/pyvenv.cfg")" == "$identity" ]] ||
    die "Repeat installation changed the native tool."

  refuses implicit "Another Site Ops selection is installed. Use --replace after review." "${second[@]}"
  [[ "$(sha256sum < "$tools/siteops/uv-receipt.toml")" == "$receipt" &&
     "$(stat -c %i "$tools/siteops/pyvenv.cfg")" == "$identity" ]] ||
    die "An implicit replacement changed the native tool."
  succeeds replace "${second[@]}" --replace
  [[ "$(installed_version "$bin")" == "siteops $second_version" ]] ||
    die "Explicit replacement did not install the selected build."
  [[ "$(count "$logs/mirror")" == 1 ]] || die "Replacement provisioned another runtime."

  second_bundle="$data/bundles/$(sha256sum < "$TEST_REPLACEMENT_ARCHIVE" | cut -d ' ' -f 1)"
  receipt="$(sha256sum < "$tools/siteops/uv-receipt.toml")"
  cp -- "$second_bundle/LICENSE" "$root/license"
  printf 'altered\n' >> "$second_bundle/LICENSE"
  refuses altered "The bundle or installed payload failed validation." "${second[@]}"
  [[ "$(sha256sum < "$tools/siteops/uv-receipt.toml")" == "$receipt" ]] ||
    die "An altered retained payload changed the native tool."
  cp -- "$root/license" "$second_bundle/LICENSE"
  printf 'import os\n' > "$tools/siteops/lib/python3.11/site-packages/extra.pth"
  refuses startup "unrecognized Python startup files" "${second[@]}" --replace
  rm -- "$tools/siteops/lib/python3.11/site-packages/extra.pth"
  succeeds repaired "${second[@]}"

  env -i HOME="$HOME" PATH="$system" "$bin/uv" tool uninstall siteops --no-config \
    > "$logs/uninstall.out" 2>&1 || die "Stock uv could not remove Site Ops."
  [[ ! -e "$tools/siteops" && ! -e "$bin/siteops" && ! -L "$bin/siteops" ]] ||
    die "Stock uv removal left Site Ops exposed."
  planted "$bin/siteops" "$root/foreign-ran"
  refuses foreign "The exposed command belongs to another installation." "${second[@]}"
  [[ ! -e "$root/foreign-ran" && ! -e "$tools/siteops" ]] ||
    die "A foreign siteops command was executed or replaced."
  rm -- "$bin/siteops"
  downloads="$(count "$logs/curl")"
  succeeds reinstall "${second[@]}"
  [[ "$(count "$logs/curl")" == "$downloads" && "$(count "$logs/mirror")" == 1 ]] ||
    die "Reinstallation did not reuse the retained release and runtime."
  [[ "$(installed_version "$bin")" == "siteops $second_version" ]] ||
    die "Reinstallation exposed another build."
}
scenario_root() {
  local private
  fresh_home root
  mkdir -p "$root/shared/data"
  planted "$root/shared/data/siteops/tools/uv/0.12.20/uv" "$root/planted-ran"
  chmod 0777 "$root/shared"
  extra=(XDG_DATA_HOME="$root/shared/data")
  refuses shared-root "private Site Ops data root" "${first[@]}"
  private="$root/private-data"
  planted "$private/siteops/tools/uv/0.12.20/uv" "$root/planted-ran"
  ln -s "$private" "$root/linked-data"
  extra=(XDG_DATA_HOME="$root/linked-data")
  refuses linked-root "private Site Ops data root" "${first[@]}"
  mkdir -p "$root/group/data"
  chmod 0770 "$root/group"
  extra=(XDG_DATA_HOME="$root/group/data")
  getent_mode=shared refuses group-root "Site Ops installation failed: Configure a private Site Ops data root" "${first[@]}"
  grep -qF -- "must not be writable by other users or shared groups" "$logs/group-root.out" ||
    die "group-root did not report how to repair a shared parent."
  [[ ! -e "$root/planted-ran" && ! -s "$logs/curl" ]] ||
    die "An untrusted data root executed a tool or downloaded content."

  # The user's private group adds no other writer, as with a 0002 login umask.
  mkdir -p "$root/user-group/data"
  chmod 0775 "$root/user-group" "$root/user-group/data"
  extra=(XDG_DATA_HOME="$root/user-group/data")
  getent_mode=private verify=false refuses user-group-root "The asset certificate does not match" "${first[@]}"
  [[ "$(stat -c %a "$root/user-group/data/siteops")" == 700 ]] ||
    die "A private-group parent did not receive a private data root."

  # A sticky shared parent cannot replace this user's private child.
  mkdir -p "$root/sticky"
  chmod 1777 "$root/sticky"
  extra=(XDG_DATA_HOME="$root/sticky/data")
  verify=false refuses sticky-root "The asset certificate does not match" "${first[@]}"
  [[ "$(stat -c %a "$root/sticky/data/siteops")" == 700 ]] ||
    die "A sticky parent did not receive a private data root."
  extra=()
  verify=false refuses rejected-proof "The asset certificate does not match" "${first[@]}"
  verify=error refuses failed-proof "provenance could not be verified" "${first[@]}"
  [[ ! -e "$data/bundles" && ! -e "$data/tools" && ! -e "$HOME/.local/share/uv" && ! -e "$bin" ]] ||
    die "A rejected proof changed installation state."
  no_uv_download "A rejected proof"
  [[ ! -s "$logs/mirror" ]] || die "A rejected proof provisioned a runtime."
}
scenario_storage() {
  local base downloads
  fresh_home storage
  mkdir -p "$root/open" "$root/real-python"
  chmod 0777 "$root/open"
  ln -s "$root/real-python" "$root/python-link"
  extra=(UV_TOOL_DIR="$root/open/tools")
  refuses open-tools "Select uv tool, command, and Python directories" "${first[@]}"
  extra=(UV_PYTHON_INSTALL_DIR="$root/python-link")
  refuses linked-python "Select uv tool, command, and Python directories" "${first[@]}"
  extra=(UV_TOOL_BIN_DIR=relative/bin)
  refuses relative-bin "Select uv tool, command, and Python directories" "${first[@]}"
  no_uv_download "Rejected uv storage"
  [[ ! -e "$data/tools" && ! -s "$logs/mirror" && ! -e "$root/real-python/cpython-3.11.16-linux-x86_64-gnu" ]] ||
    die "Rejected uv storage ran or provisioned tools."

  base="$root/uv-state"
  mkdir -p "$base"
  chmod 0755 "$base"
  extra=(UV_TOOL_DIR="$base/tools" UV_TOOL_BIN_DIR="$base/unused/../bin"
         UV_PYTHON_INSTALL_DIR="$base/python")
  succeeds explicit "${first[@]}"
  [[ -L "$base/bin/siteops" && "$(readlink "$base/bin/siteops")" == "$base/tools/siteops/bin/siteops" ]] ||
    die "Explicit uv tool storage was not used."
  grep -qxF "home = $base/python/cpython-3.11.16-linux-x86_64-gnu/bin" "$base/tools/siteops/pyvenv.cfg" ||
    die "The explicit managed runtime directory was not used."
  [[ ! -e "$tools" && ! -e "$pydir" && -x "$base/bin/uv" ]] ||
    die "Explicit uv storage leaked into default locations."
  env -i HOME="$HOME" PATH="$system" UV_TOOL_DIR="$base/tools" UV_TOOL_BIN_DIR="$base/bin" \
    "$base/bin/uv" tool uninstall siteops --no-config > "$logs/explicit-uninstall.out" 2>&1 ||
    die "Stock uv could not remove the explicit installation."

  # Staging lives in private Site Ops storage, so a temporary directory that
  # widens new files or lets other users change them is never used.
  fresh_home storage-staging
  mkdir "$root/inherited"
  inherit_open_access "$root/inherited"
  mkdir -p "$data/install-staging/run.stale" "$data/install-staging/run.live"
  chmod 0700 "$data" "$data/install-staging" "$data/install-staging/run.stale" "$data/install-staging/run.live"
  touch -d '2 days ago' "$data/install-staging/run.stale"
  extra=(TMPDIR="$root/inherited")
  succeeds inherited-temp "${first[@]}" --enroll-source demo
  [[ "$(installed_version "$bin")" == "siteops $first_version" ]] ||
    die "Private staging did not install the selected build."
  [[ -z "$(find "$root/inherited" -mindepth 1 -print -quit)" ]] ||
    die "The temporary directory was used for staging."
  [[ "$(find "$data/install-staging" -mindepth 1)" == "$data/install-staging/run.live" ]] ||
    die "Staging remained, stale staging was kept, or live staging was removed."
  [[ "$(find "$data/install-downloads" -type f -perm 600 | wc -l)" == 2 ]] ||
    die "The retained release is not owner-only."
  extra=(TMPDIR="$root/open")
  succeeds open-temp "${first[@]}"
  mkdir "$root/inherited-data"
  inherit_open_access "$root/inherited-data"
  extra=(XDG_DATA_HOME="$root/inherited-data")
  downloads="$(count "$logs/curl")"
  refuses inherited-data "New files in the Site Ops data root do not stay private." "${first[@]}"
  [[ "$(count "$logs/curl")" == "$downloads" ]] || die "Storage that widens new files downloaded content."
}
scenario_policy() {
  local setting
  fresh_home policy
  extra=(UV_PYTHON_INSTALL_MIRROR=http://127.0.0.1:9/python)
  refuses http-mirror "Select an approved HTTPS Python runtime mirror." "${first[@]}"
  for setting in UV_PYTHON_DOWNLOADS_JSON_URL=https://127.0.0.1:9/catalog.json \
      UV_INSECURE_HOST=127.0.0.1 UV_INSECURE_NO_ZIP_VALIDATION=1; do
    extra=("$setting")
    refuses "insecure-${setting%%=*}" "Remove insecure uv settings" "${first[@]}"
  done
  no_uv_download "Rejected uv policy"
  extra=()
  tamper_uv=1 refuses tampered-uv "The native uv archive differs from the selected release." "${first[@]}"
  [[ ! -e "$data/tools/uv/0.12.20/uv" && ! -e "$bin/uv" && ! -e "$tools" && ! -s "$logs/mirror" ]] ||
    die "A tampered uv archive was retained or executed."

  # Ambient uv and Python settings are removed rather than trusted.
  printf 'python-install-mirror = "http://127.0.0.1:9/python"\n' > "$root/hostile.toml"
  extra=(UV_OFFLINE=1 UV_CONFIG_FILE="$root/hostile.toml" UV_PYTHON_DOWNLOADS=never
         UV_INDEX=https://127.0.0.1:9/simple UV_PYTHON_PREFERENCE=only-system
         UV_INSECURE_NO_ZIP_VALIDATION=false PYTHONHOME="$root/nowhere")
  succeeds hostile-environment "${first[@]}"
  [[ "$(count "$logs/mirror")" == 1 && "$(installed_version "$bin")" == "siteops $first_version" ]] ||
    die "Stripped uv settings changed the verified installation."
}
scenario_tools() {
  fresh_home tools-qualified
  mkdir -p "$root/existing/bin" "$root/linked/bin"
  chmod 0755 "$root/existing" "$root/existing/bin"
  cp -- "$root/seed/uv" "$root/existing/bin/uv"
  chmod 0755 "$root/existing/bin/uv"
  ln -s "$root/existing/bin/uv" "$root/linked/bin/uv"
  bootstrap_path="$doubles:$root/existing/bin:$system"
  succeeds qualified "${first[@]}"
  grep -qF "Keep: the selected qualified uv installation." "$logs/qualified.out" ||
    die "A qualified uv was not reused."
  no_uv_download "Qualified uv reuse"
  [[ ! -e "$data/tools" && ! -e "$bin/uv" ]] || die "Qualified uv reuse created another uv copy."
  bootstrap_path="$doubles:$root/linked/bin:$system"
  succeeds linked-qualified "${first[@]}"
  grep -qF "Keep: the selected qualified uv installation." "$logs/linked-qualified.out" ||
    die "A linked qualified uv was not resolved to its admitted target."

  fresh_home tools-other
  planted "$root/other/bin/uv" "$root/other-ran"
  chmod 0755 "$root/other" "$root/other/bin"
  bootstrap_path="$doubles:$root/other/bin:$system"
  succeeds other "${first[@]}"
  grep -qF "Keep: the other uv installation unchanged." "$logs/other.out" ||
    die "Another uv was not preserved."
  [[ ! -e "$root/other-ran" && ! -e "$bin/uv" && -x "$data/tools/uv/0.12.20/uv" ]] ||
    die "Another uv was executed or shadowed."

  fresh_home tools-open
  mkdir -p "$root/open-bin"
  cp -- "$root/seed/uv" "$root/open-bin/uv"
  chmod 0755 "$root/open-bin/uv"
  chmod 0777 "$root/open-bin"
  bootstrap_path="$doubles:$root/open-bin:$system"
  succeeds open-qualified "${first[@]}"
  grep -qF "Keep: the other uv installation unchanged." "$logs/open-qualified.out" ||
    die "A qualified uv in a writable directory was selected."

  fresh_home tools-occupied
  planted "$bin/uv" "$root/occupied-ran"
  refuses occupied "An unrelated uv command occupies the native maintenance location." "${first[@]}"
  [[ ! -e "$root/occupied-ran" && ! -e "$tools/siteops" ]] ||
    die "An unrelated maintenance uv was executed or replaced."
}
scenario_runtime() {
  local concrete mirrored
  fresh_home runtime-existing
  seed_runtime "$pydir"
  mirrored="$(count "$logs/mirror")"
  succeeds existing-runtime "${first[@]}"
  [[ "$(count "$logs/mirror")" == "$mirrored" ]] || die "An existing managed runtime was not reused."
  cp "$tools/siteops/pyvenv.cfg" "$logs/original-pyvenv.cfg"
  sed -i "s#^home = .*#home = $root/missing-runtime#" "$tools/siteops/pyvenv.cfg"
  refuses inconsistent-binding "Inspect it or run uv tool uninstall siteops after review" "${first[@]}" --replace
  grep -Fq "home = $root/missing-runtime" "$tools/siteops/pyvenv.cfg" ||
    die "A rejected runtime changed the tool configuration."
  cp "$logs/original-pyvenv.cfg" "$tools/siteops/pyvenv.cfg"
  mkdir -p "$root/open-runtime"
  planted "$root/open-runtime/bin/python3.11" "$root/bound-ran"
  chmod 0777 "$root/open-runtime"
  ln -sfn "$root/open-runtime/bin/python3.11" "$tools/siteops/bin/python"
  sed -i "s#^home = .*#home = $root/open-runtime/bin#" "$tools/siteops/pyvenv.cfg"
  refuses bound-replace "The current Site Ops tool uses an unsafe runtime." "${first[@]}" --replace
  refuses bound-install "Inspect it or run uv tool uninstall siteops after review" "${first[@]}"
  [[ ! -e "$root/bound-ran" ]] || die "An unadmitted tool runtime was executed."

  fresh_home runtime-unsafe
  concrete="$pydir/cpython-3.11.16-linux-x86_64-gnu"
  planted "$concrete/bin/python3.11" "$root/runtime-ran"
  mkdir -p "$concrete/lib"
  chmod 0777 "$concrete/lib"
  refuses unsafe-runtime "The uv-managed Python has files other users can change." "${first[@]}"
  rm -rf -- "$concrete"
  planted "$concrete/bin/python3.11" "$root/runtime-ran"
  mkdir -m 0777 "$root/open-libraries"
  ln -s "$root/open-libraries" "$concrete/lib"
  refuses linked-library "The uv-managed Python links outside its concrete installation." "${first[@]}"
  [[ ! -e "$root/runtime-ran" ]] || die "A runtime with unadmitted library links was executed."
  rm -rf -- "$concrete"
  planted "$root/elsewhere/bin/python3.11" "$root/runtime-ran"
  ln -s "$root/elsewhere" "$concrete"
  refuses linked-runtime "The uv-managed Python must use trusted, non-symlinked directories." "${first[@]}"
  rm -- "$concrete"
  mkdir -p "$concrete"
  refuses incomplete-runtime "The uv-managed Python installation is incomplete." "${first[@]}"
  rm -rf -- "$concrete"
  [[ ! -e "$root/runtime-ran" ]] || die "An unadmitted managed runtime was executed."
  mirrored="$(count "$logs/mirror")"
  seed_runtime "$root/mislabeled"
  mv -- "$root/mislabeled/cpython-3.11.16-linux-x86_64-gnu" "$pydir/cpython-3.11.15-linux-x86_64-gnu"
  refuses mislabeled-runtime "differs from the installed runtime." "${first[@]}"
  [[ ! -e "$tools/siteops" ]] || die "A mislabeled runtime installed Site Ops."
}
scenario_prerequisites() {
  local runs
  fresh_home prerequisites
  old_gh=1 refuses old-gh "GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com" "${first[@]}"
  mkdir -m 0755 "$root/no-gh" "$root/open-gh"
  ln -s "$doubles/curl" "$root/no-gh/curl"
  ln -s "$doubles/getent" "$root/no-gh/getent"
  bootstrap_path="$root/no-gh:$system"
  refuses missing-gh "GitHub CLI 2.95 or newer is required." "${first[@]}"
  cp -- "$doubles/gh" "$root/open-gh/gh"
  chmod 0777 "$root/open-gh"
  runs="$(count "$logs/gh")"
  bootstrap_path="$root/open-gh:$doubles:$system"
  refuses open-gh "The GitHub CLI executable must be owned by an administrator or the current user" "${first[@]}"
  [[ "$(count "$logs/gh")" == "$runs" ]] || die "An unadmitted GitHub CLI was executed."
  [[ ! -s "$logs/curl" && ! -e "$data" ]] || die "A rejected GitHub CLI changed installation state."
}
scenario_content() {
  local downloads combined referenced
  combined=(--content-release v1.0.0b7 --source-commit "$(printf 'a%.0s' {1..40})" --yes)
  referenced=(--content-release v1.0.0b8 --source-commit "$(printf 'c%.0s' {1..40})" \
    --source-ref refs/heads/content-preview --caller ci.yaml --yes)
  fresh_home content-missing
  no_reference=1 refuses missing-reference "signed engine reference is unavailable" "${combined[@]}"
  no_uv_download missing-reference
  [[ ! -e "$tools/siteops" ]] || die "Missing metadata installed an engine."
  fresh_home content-proof
  reference_verify=false refuses reference-proof "asset certificate does not match" "${combined[@]}"
  no_uv_download reference-proof
  cp "$reference_root/v1.0.0b7/siteops-engine.json" "$logs/original-reference"
  for fault in digest version; do
    fresh_home "content-$fault"
    "$real_python" -I - "$logs/original-reference" "$reference_root/v1.0.0b7/siteops-engine.json" "$fault" <<'PY'
import json
import pathlib
import sys

record = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
if sys.argv[3] == "digest":
    record["engine"]["bundle"]["sha256"] = "0" * 64
else:
    record["engine"]["version"] = "1.9.9"
pathlib.Path(sys.argv[2]).write_text(
    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
)
PY
    if [[ "$fault" == digest ]]; then
      refuses wrong-bundle-digest "differs from the signed content selection" "${combined[@]}"
    else
      refuses wrong-bundle-version "installer helper could not be prepared" "${combined[@]}"
    fi
    [[ ! -e "$tools/siteops" ]] || die "A rejected engine selection installed the tool."
  done
  cp "$logs/original-reference" "$reference_root/v1.0.0b7/siteops-engine.json"
  fresh_home content-combined
  planted "$root/untrusted-parser" "$root/untrusted-parser-ran"
  extra=(runtime_prepared=true python="$root/untrusted-parser")
  succeeds content-combined "${combined[@]}"
  [[ ! -e "$root/untrusted-parser-ran" ]] || die "Ambient runtime state bypassed tool admission."
  extra=()
  [[ "$(installed_version "$bin")" == "siteops $first_version" ]] || die "Combined content selected another engine."
  downloads="$(count "$logs/curl")"
  succeeds content-repeat "${combined[@]}"
  [[ "$(count "$logs/curl")" == "$downloads" ]] || die "Repeated content resolution redownloaded assets."
  verify=false refuses content-bundle-proof "asset certificate does not match" "${combined[@]}"
  fresh_home content-reference
  succeeds content-reference "${referenced[@]}" --enroll-source demo
  [[ "$(installed_version "$bin")" == "siteops 1.2.3" ]] || die "Content-only selection installed another engine."
  "$real_python" -I - "$logs/siteops" "$logs/gh" <<'PY' || die "Content and engine policy identities were mixed."
import json
import sys

records = [json.loads(line) for line in open(sys.argv[1])]
policy = [item["policy"] for item in records if item["policy"]][-1]["provider"]
assert policy["sourceRef"] == "refs/heads/content-preview"
assert policy["builderWorkflow"] == ".github/workflows/ci.yaml"
commands = open(sys.argv[2]).read().splitlines()
bundle = [line for line in commands if "attestation verify" in line and "/siteops-install.zip " in line][-1]
assert "--source-digest " + "d" * 40 in bundle
assert "/_siteops-distribution.yaml@refs/heads/main" in bundle
assert "/release.yaml@refs/heads/main" in bundle
reference = [line for line in commands if "attestation verify" in line and "/siteops-engine.json " in line][-1]
assert "--source-digest " + "c" * 40 in reference
assert "/_release-candidate.yaml@refs/heads/content-preview" in reference
PY
  downloads="$(count "$logs/curl")"
  succeeds referenced-repeat "${referenced[@]}"
  [[ "$(count "$logs/curl")" == "$downloads" ]] || die "Referenced engine was not retained."
}

case "$scenario" in
  journey|root|storage|policy|tools|runtime|prerequisites|content) "scenario_$scenario" ;;
  *) die "Unknown scenario $scenario." ;;
esac
[[ ! -s "$logs/rejected" ]] || { cat "$logs/rejected" >&2; die "An unexpected tool call was made."; }
echo "Scenario $scenario passed with native uv, managed CPython and no network."

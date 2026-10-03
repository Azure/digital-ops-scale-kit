#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "Select an absolute, fresh native fixture directory." >&2
  exit 2
fi
fixtures="$1"
command -v openssl >/dev/null
mkdir -m 700 "$fixtures"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize 19827214 \
  --output "$fixtures/uv.tar.gz" \
  https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-unknown-linux-gnu.tar.gz
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize 30778566 \
  --output "$fixtures/cpython.tar.gz" \
  https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.11.16%2B20260924-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
printf '%s  %s\n' \
  6590717592ace991ff83a63fef799e3ad9d33ecc8f96c5d6bdd732496e79337f "$fixtures/uv.tar.gz" \
  68c6739376b65258dee5058ccf6777232fe38d31a578965ae8bda327ec7da3a8 "$fixtures/cpython.tar.gz" |
  sha256sum --check --status
mkdir -m 700 "$fixtures/native"
tar -xzf "$fixtures/uv.tar.gz" -C "$fixtures/native" --strip-components=1 \
  uv-x86_64-unknown-linux-gnu/uv

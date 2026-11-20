#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

command -v npm >/dev/null 2>&1 || {
  echo "error: npm is required to build the administration frontend" >&2
  exit 1
}
command -v uv >/dev/null 2>&1 || {
  echo "error: uv is required to build the Python distributions" >&2
  exit 1
}

echo "==> Installing locked frontend dependencies"
npm --prefix web ci

echo "==> Building the administration frontend"
npm --prefix web run build

echo "==> Building wheel and source distribution"
uv build --clear

echo "==> Build artifacts"
find dist -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' \) -print | sort

#!/usr/bin/env bash
# Build the release artifact: a DETERMINISTIC tarball of the runtime files.
#
# Deterministic (sorted entries, fixed mtime/owner, gzip without timestamp)
# so the sha256 that `axp release` writes into the manifest matches the asset
# uploaded to GitHub byte for byte, and a rebuild from the same tree yields
# the same digest. The manifest itself is NOT inside the artifact: its
# digest is part of the signed manifest, which would be circular.
#
# Usage: scripts/build-artifact.sh <version> [out-dir]   -> dist/mintbot-mcp-<version>.tar.gz
set -euo pipefail

VERSION="${1:?usage: build-artifact.sh <version> [out-dir]}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${2:-${ROOT}/dist}"
OUT="${OUT_DIR}/mintbot-mcp-${VERSION}.tar.gz"

RUNTIME_FILES=(
  mintbot_mcp/__init__.py mintbot_mcp/__main__.py mintbot_mcp/app.py mintbot_mcp/auth.py
  mintbot_mcp/backends.py mintbot_mcp/chat.py mintbot_mcp/cli.py mintbot_mcp/config.py
  mintbot_mcp/server.py
  pyproject.toml requirements.txt
  install.sh upgrade.sh uninstall.sh healthcheck.sh
  README.md LICENSE after-install.md
)

for f in "${RUNTIME_FILES[@]}"; do
  [ -f "${ROOT}/${f}" ] || { echo "build-artifact: missing ${f}" >&2; exit 1; }
done

mkdir -p "${OUT_DIR}"
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
  -C "${ROOT}" -cf - "${RUNTIME_FILES[@]}" | gzip -n -9 > "${OUT}"
printf '%s  %s\n' "$(sha256sum "${OUT}" | cut -d" " -f1)" "${OUT}"

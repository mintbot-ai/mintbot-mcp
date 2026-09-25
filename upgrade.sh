#!/usr/bin/env bash
# AXP upgrade hook. install.sh is idempotent and converges the virtualenv, the
# package files and the launcher, so an upgrade is an install with the previous
# version announced. An AXP host restarts the daemon it owns afterwards; the
# standalone install.sh restarts its own unit.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '[mintbot-mcp] upgrading from %s to %s\n' "${AXP_FROM_VERSION:-unknown}" "${AXP_EXT_VERSION:-this version}"
exec "${SOURCE_DIR}/install.sh"

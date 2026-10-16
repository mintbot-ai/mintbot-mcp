#!/usr/bin/env bash
# AXP upgrade hook. install.sh stages and validates a complete private runtime
# before switching the release pointer. The AXP host owns daemon restart policy.
set -euo pipefail
SOURCE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
printf '[mintbot-mcp] upgrading from %s to %s\n' "${AXP_FROM_VERSION:-unknown}" "${AXP_EXT_VERSION:-this version}"
exec "${SOURCE_DIR}/install.sh"

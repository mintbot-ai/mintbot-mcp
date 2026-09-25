#!/usr/bin/env bash
# AXP uninstall hook (also standalone-runnable). The bearer token is kept
# unless AXP_PURGE=1 (SPEC section 6.2), so uninstall + reinstall never forces
# every MCP client to be reconfigured.
set -euo pipefail

log() { printf '[mintbot-mcp] %s\n' "$*"; }

# ---- host <-> script contract (AXP SPEC section 6) ---------------------------
# Every AXP_* variable is optional: an AXP host sets them, a human running the
# script by hand gets the spec defaults below (standalone rule, SPEC section 10).
PUBLISHER="mintbot.ai"
NAME="mintbot-mcp"
if [ -n "${AXP_PREFIX:-}" ]; then
  PREFIX="${AXP_PREFIX}"
elif [ "$(id -u)" -eq 0 ]; then
  PREFIX="/opt/${NAME}"
else
  PREFIX="${HOME}/.local/opt/${NAME}"
fi
if [ -n "${AXP_STATE_DIR:-}" ]; then
  STATE_DIR="${AXP_STATE_DIR}"
elif [ "$(id -u)" -eq 0 ]; then
  STATE_DIR="/var/lib/axp/${PUBLISHER}/${NAME}"
else
  STATE_DIR="${HOME}/.local/state/axp/${PUBLISHER}/${NAME}"
fi
export AXP_STATE_DIR="${STATE_DIR}"          # the daemon and the CLI read the same variable
LAUNCHER="${PREFIX}/bin/mintbot-mcp"
# The standalone unit (no AXP host). An AXP host binds the daemon through the
# manifest's `command` and writes a unit of its own (SPEC section 5.3).
UNIT_NAME="mintbot-mcp.service"
UNIT_FILE="${MINTBOT_MCP_UNIT_DIR:-/etc/systemd/system}/${UNIT_NAME}"

# Only the unit the standalone install.sh wrote is ours to remove; an AXP
# host tears its own unit down.
if [ -f "${UNIT_FILE}" ] && grep -q 'standalone mintbot-mcp install' "${UNIT_FILE}"; then
  systemctl disable --now --quiet "${UNIT_NAME}" 2>/dev/null || true
  rm -f "${UNIT_FILE}"
  systemctl daemon-reload 2>/dev/null || true
  log "removed ${UNIT_NAME}"
fi

rm -rf "${PREFIX}"
if [ "${AXP_PURGE:-0}" = "1" ]; then
  rm -rf "${STATE_DIR}"
  log "removed the server and its bearer token"
else
  log "removed the server; preserved the bearer token at ${STATE_DIR}/token"
  log "set AXP_PURGE=1 when uninstalling to delete the token too"
fi

#!/usr/bin/env bash
# AXP health hook (SPEC section 6.4): exit 0 healthy, 1 unhealthy, 2 unknown.
# First stdout line is the one-line status a host may display.
set -euo pipefail

unhealthy() { printf 'unhealthy: %s\n' "$*"; exit 1; }
unknown()   { printf 'unknown: %s\n' "$*"; exit 2; }

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

# The unit a mintbot AXP host writes for the `mcp` service (axp_runtime naming).
HOST_UNIT="mintbot-ext-${PUBLISHER}-${NAME}-svc-mcp.service"
PORT="${MINTBOT_MCP_PORT:-8650}"

[ -x "${LAUNCHER}" ] || unhealthy "launcher is not installed at ${LAUNCHER}"
[ -x "${PREFIX}/venv/bin/python" ] || unhealthy "virtualenv is missing under ${PREFIX}/venv"
PYTHONPATH="${PREFIX}/lib" "${PREFIX}/venv/bin/python" -c 'import mintbot_mcp.server' 2>/dev/null \
  || unhealthy "the package or its dependencies do not import"
[ -s "${STATE_DIR}/token" ] || unhealthy "bearer token is missing at ${STATE_DIR}/token"

# The daemon answers /health without a token. A sandboxed hook may not be
# allowed to reach loopback at all, which says nothing about the daemon; only
# a refused connection does.
probe="$("${PREFIX}/venv/bin/python" - "${PORT}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{sys.argv[1]}/health"
try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.load(resp)
    print("ok" if body.get("ok") else "bad")
except urllib.error.URLError as exc:
    reason = getattr(exc, "reason", exc)
    print("refused" if isinstance(reason, ConnectionRefusedError) else f"blocked: {reason}")
except Exception as exc:  # noqa: BLE001 - any other failure is reported, not raised
    print(f"blocked: {exc}")
PY
)"
case "${probe}" in
  ok)      printf 'ok: daemon answers on 127.0.0.1:%s\n' "${PORT}"; exit 0 ;;
  bad)     unhealthy "daemon answers on 127.0.0.1:${PORT} but reports not ok" ;;
  refused) unhealthy "nothing listens on 127.0.0.1:${PORT}" ;;
esac

# Loopback is not reachable from here: fall back to the unit state.
if command -v systemctl >/dev/null 2>&1; then
  seen=""
  for unit in "${HOST_UNIT}" "${UNIT_NAME}"; do
    state="$(systemctl is-active "${unit}" 2>/dev/null || true)"
    if [ "${state}" = "active" ]; then
      printf 'ok: %s is active (loopback probe not permitted from this sandbox)\n' "${unit}"
      exit 0
    fi
    [ -n "${state}" ] && seen="${seen}${unit}=${state} "
  done
  [ -n "${seen}" ] && unhealthy "no mintbot-mcp unit is active (${seen% })"
fi
unknown "cannot reach 127.0.0.1:${PORT} (${probe}) and systemd cannot be asked"

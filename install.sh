#!/usr/bin/env bash
# AXP install hook for mintbot-mcp (also standalone-runnable, SPEC section 10).
# Transactional: build and validate a new runtime before switching current.
# Re-running preserves state and never pip-modifies the active virtualenv.
#
# Under an AXP host the daemon is the host's: the manifest binds the service
# with a `command`, so the host writes, contains and supervises the systemd
# unit itself and this script MUST NOT start the daemon (SPEC section 5.3).
# Run by hand (no AXP_HOST) as root on a systemd machine, the script installs
# a plain `mintbot-mcp.service` instead.
set -euo pipefail

log() { printf '[mintbot-mcp] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
PYTHON="${MINTBOT_MCP_PYTHON:-python3}"
command -v "${PYTHON}" >/dev/null 2>&1 || die "python3 is required (Debian: apt install python3 python3-venv)"
SOURCE_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

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

# pip writes its cache under ~/.cache by default, which a sandboxing host keeps
# read-only; the cache dir the host hands over is exactly for this.
if [ -n "${AXP_CACHE_DIR:-}" ]; then
  mkdir -p "${AXP_CACHE_DIR}/pip"
  export PIP_CACHE_DIR="${AXP_CACHE_DIR}/pip"
fi

log "installing into ${PREFIX}"
install -d -m 0755 "${PREFIX}" "${PREFIX}/bin" "${PREFIX}/releases"
install -d -m 0700 "${STATE_DIR}"
exec 9>"${PREFIX}/.install.lock"
flock -n 9 || die "another install is running"
[ ! -e "${PREFIX}/current" ] || [ -L "${PREFIX}/current" ] || die "current is not a release symlink"
TRANSACTION="$(mktemp -d "${PREFIX}/.transaction.XXXXXXXX")"
RELEASE=""
COMMITTED=0
SERVICE_TOUCHED=0
NEW_UNIT=0
WAS_ACTIVE=0
CHANGED=()
# Back up only entrypoints, never move a virtualenv or touch user data.
remember() {
  local path="$1" index="${#CHANGED[@]}"
  if [ -e "${path}" ] || [ -L "${path}" ]; then
    cp -a -- "${path}" "${TRANSACTION}/${index}"
  fi
  CHANGED+=("${path}")
}
finish() {
  local status=$? failed=0 index path
  trap - EXIT HUP INT TERM
  if [ "${COMMITTED}" != 1 ]; then
    for ((index=${#CHANGED[@]}-1; index>=0; index--)); do
      path="${CHANGED[index]}"
      if [ -e "${TRANSACTION}/${index}" ] || [ -L "${TRANSACTION}/${index}" ]; then
        mv -Tf -- "${TRANSACTION}/${index}" "${path}" || failed=1
      else
        rm -f -- "${path}" || failed=1
      fi
    done
    if [ "${SERVICE_TOUCHED}" = 1 ]; then
      if [ "${NEW_UNIT}" = 1 ]; then
        systemctl disable --now --quiet "${UNIT_NAME}" || failed=1
      fi
      systemctl daemon-reload || failed=1
      if [ "${WAS_ACTIVE}" = 1 ]; then
        systemctl restart "${UNIT_NAME}" || failed=1
      fi
      rm -f -- "${UNIT_FILE}.tmp" || failed=1
    fi
    if [ "${failed}" = 0 ]; then
      [ -z "${RELEASE}" ] || rm -rf -- "${RELEASE}"
    else
      log "ERROR: rollback incomplete; retained ${TRANSACTION} and ${RELEASE} for recovery" >&2
      exit 1
    fi
  fi
  rm -f -- "${PREFIX}/.current.next" "${PREFIX}/.link.next"
  rm -rf -- "${TRANSACTION}"
  exit "${status}"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# This is the FINAL path: virtualenv console scripts embed absolute shebangs.
RELEASE="$(mktemp -d "${PREFIX}/releases/release.XXXXXXXX")"
install -d -m 0755 "${RELEASE}/bin" "${RELEASE}/lib/mintbot_mcp"
log "creating the staged private virtualenv"
"${PYTHON}" -m venv "${RELEASE}/venv"
log "installing pinned Python dependencies"
"${RELEASE}/venv/bin/pip" install --disable-pip-version-check --quiet --requirement "${SOURCE_DIR}/requirements.txt"
PACKAGE="${SOURCE_DIR}/mintbot_mcp"
[ -d "${PACKAGE}" ] || PACKAGE="${SOURCE_DIR}/lib/mintbot_mcp"
install -m 0644 "${PACKAGE}"/*.py "${RELEASE}/lib/mintbot_mcp/"
install -m 0644 "${SOURCE_DIR}/README.md" "${SOURCE_DIR}/LICENSE" "${SOURCE_DIR}/after-install.md" \
  "${SOURCE_DIR}/requirements.txt" "${RELEASE}/"
for script in install.sh upgrade.sh uninstall.sh healthcheck.sh; do
  install -m 0755 "${SOURCE_DIR}/${script}" "${RELEASE}/${script}"
done
# Pin BOTH code and runtime, even if current changes mid-start.
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  ": \"\${AXP_STATE_DIR:=${STATE_DIR}}\"" \
  'export AXP_STATE_DIR' \
  "cd \"${RELEASE}\"" \
  "export PYTHONPATH=\"${RELEASE}/lib\"" \
  "exec \"${RELEASE}/venv/bin/python\" -m mintbot_mcp \"\$@\"" \
  > "${RELEASE}/bin/mintbot-mcp"
chmod 0755 "${RELEASE}/bin/mintbot-mcp"
AXP_PREFIX="${RELEASE}" "${RELEASE}/healthcheck.sh" --offline
"${RELEASE}/bin/mintbot-mcp" token >/dev/null
remember "${PREFIX}/current"
ln -s "${RELEASE}" "${PREFIX}/.current.next"
mv -Tf "${PREFIX}/.current.next" "${PREFIX}/current"
for name in README.md LICENSE after-install.md requirements.txt install.sh upgrade.sh uninstall.sh healthcheck.sh bin/mintbot-mcp; do
  remember "${PREFIX}/${name}"
  ln -s "${PREFIX}/current/${name}" "${PREFIX}/.link.next"
  mv -Tf "${PREFIX}/.link.next" "${PREFIX}/${name}"
done
# Legacy directories stay at their original paths for old shebangs/processes.
for name in venv lib; do
  if [ ! -e "${PREFIX}/${name}" ]; then
    remember "${PREFIX}/${name}"
    ln -s "current/${name}" "${PREFIX}/${name}"
  fi
done

if [ -n "${AXP_HOST:-}" ]; then
  log "AXP host ${AXP_HOST} owns the service unit; the daemon is not started here"
elif [ "$(id -u)" -eq 0 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  log "installing ${UNIT_NAME}"
  install -d -m 0755 "$(dirname "${UNIT_FILE}")"
  if [ -e "${UNIT_FILE}" ]; then
    systemctl is-active --quiet "${UNIT_NAME}" && WAS_ACTIVE=1
  else
    NEW_UNIT=1
  fi
  remember "${UNIT_FILE}"
  SERVICE_TOUCHED=1
  printf '%s\n' \
    "# Written by ${PREFIX}/install.sh (standalone mintbot-mcp install); removed by uninstall.sh." \
    '[Unit]' \
    'Description=mintbot-mcp: MCP server for this mintbot agent' \
    'After=network-online.target' \
    'Wants=network-online.target' \
    '' \
    '[Service]' \
    'Type=simple' \
    "ExecStart=${LAUNCHER} serve" \
    "Environment=AXP_STATE_DIR=${STATE_DIR}" \
    "WorkingDirectory=${PREFIX}" \
    'Restart=always' \
    'RestartSec=2' \
    'NoNewPrivileges=yes' \
    'PrivateTmp=yes' \
    'ProtectSystem=strict' \
    'ProtectHome=read-only' \
    "ReadWritePaths=${STATE_DIR} ${PREFIX}" \
    '' \
    '[Install]' \
    'WantedBy=multi-user.target' \
    > "${UNIT_FILE}.tmp"
  mv -f "${UNIT_FILE}.tmp" "${UNIT_FILE}"
  systemctl daemon-reload
  if [ "${NEW_UNIT}" = 1 ]; then
    systemctl enable --quiet "${UNIT_NAME}"
    systemctl restart "${UNIT_NAME}"
  elif [ "${WAS_ACTIVE}" = 1 ]; then
    systemctl restart "${UNIT_NAME}"
  fi
  log "${UNIT_NAME} installed (existing enablement and stopped state preserved)"
else
  log "no systemd available here; start the server with: ${LAUNCHER} serve"
fi
COMMITTED=1
log "installation complete; run '${LAUNCHER} client-config' for a ready-to-paste MCP client block"

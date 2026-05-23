#!/bin/sh
# Install the launchd agent that runs potion-perps-bot 24/7 (Phase 3.2).
#
# Idempotent: re-running this script tears down any previous instance and
# installs the current one. Safe to run after a `git pull`.
#
# What it does:
#   1. Resolve PROJECT_DIR from this script's location (no hard-coding).
#   2. Resolve PYTHON via `command -v python3`, refuse if < 3.10.
#   3. Render the plist template into ~/Library/LaunchAgents/.
#   4. `launchctl bootout` any previous agent (ignore "not loaded" errors).
#   5. `launchctl bootstrap` the new plist into the user's GUI session.
#   6. Print the status/log/uninstall commands.

set -eu

LABEL="local.potion-perps-bot"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist.template"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET="${TARGET_DIR}/${LABEL}.plist"

# --- 1. macOS check ------------------------------------------------
case "$(uname -s)" in
    Darwin) ;;
    *)
        echo "error: install.sh is macOS-only (uname=$(uname -s))" >&2
        exit 1
        ;;
esac

# --- 2. Resolve & version-check python3 ----------------------------
PYTHON="$(command -v python3 || true)"
if [ -z "${PYTHON}" ]; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi
# Resolve symlinks so launchd has a stable absolute path.
PYTHON="$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${PYTHON}")"

PYVER="$("${PYTHON}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYMAJOR="${PYVER%%.*}"
PYMINOR="${PYVER#*.}"
if [ "${PYMAJOR}" -lt 3 ] || { [ "${PYMAJOR}" -eq 3 ] && [ "${PYMINOR}" -lt 10 ]; }; then
    echo "error: python3 ${PYVER} is too old (need >= 3.10)" >&2
    exit 1
fi

# --- 3. Sanity-check repo layout -----------------------------------
if [ ! -f "${PROJECT_DIR}/main.py" ]; then
    echo "error: main.py not found at ${PROJECT_DIR}/main.py" >&2
    exit 1
fi
if [ ! -f "${TEMPLATE}" ]; then
    echo "error: plist template missing: ${TEMPLATE}" >&2
    exit 1
fi
if [ ! -x "${SCRIPT_DIR}/run.sh" ]; then
    echo "error: deploy/launchd/run.sh is missing or not executable" >&2
    exit 1
fi
mkdir -p "${PROJECT_DIR}/logs"
mkdir -p "${TARGET_DIR}"

# --- 4. Render the plist -------------------------------------------
# sed delimiter is '|' to avoid escaping '/' in paths.
sed \
    -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
    -e "s|{{PYTHON}}|${PYTHON}|g" \
    "${TEMPLATE}" > "${TARGET}.tmp"
mv "${TARGET}.tmp" "${TARGET}"

# --- 5. (Re)load via launchctl -------------------------------------
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
SERVICE="${DOMAIN}/${LABEL}"

# Best-effort teardown of any previous instance. Both `bootout` and the
# legacy `unload` are tried; failures are expected and ignored.
launchctl bootout "${SERVICE}" 2>/dev/null || true
launchctl unload "${TARGET}" 2>/dev/null || true

launchctl bootstrap "${DOMAIN}" "${TARGET}"

cat <<EOF
Installed ${LABEL} at ${TARGET}

Useful commands:
    Status:    launchctl print ${SERVICE} | head -20
    Tail logs: tail -f ${PROJECT_DIR}/logs/bot.log
    Restart:   launchctl kickstart -k ${SERVICE}
    Uninstall: ${SCRIPT_DIR}/uninstall.sh

Sleep note: caffeinate -i prevents idle sleep, but lid-closed-on-battery
can still sleep the machine. See deploy/launchd/README.md.
EOF

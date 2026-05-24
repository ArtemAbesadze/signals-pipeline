#!/bin/sh
# Install launchd agent(s) for potion-perps-bot (Phase 3.2 + 4.1).
#
# Usage:
#   install.sh              # install main bot (default — same as Phase 3.2)
#   install.sh bot          # install main bot
#   install.sh forwarder    # install Telethon forwarder (Phase 4.1)
#   install.sh all          # install both
#
# Idempotent: re-running tears down any previous instance and reinstalls.
# Safe to run after a `git pull`.
#
# Two agents:
#   - local.potion-perps-bot       — main trading bot (main.py)
#   - local.potion-perps-forwarder — Telethon @PotionScannerBot DM relay
#                                    (scripts/telethon_forwarder.py)

set -eu

MODE="${1:-bot}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TARGET_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

# --- macOS check ---
case "$(uname -s)" in
    Darwin) ;;
    *)
        echo "error: install.sh is macOS-only (uname=$(uname -s))" >&2
        exit 1
        ;;
esac

# --- Resolve & version-check python3 ---
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

mkdir -p "${PROJECT_DIR}/logs"
mkdir -p "${TARGET_DIR}"

_install_agent() {
    label="$1"
    template_name="$2"
    wrapper="$3"
    entrypoint="$4"

    template="${SCRIPT_DIR}/${template_name}"
    target="${TARGET_DIR}/${label}.plist"

    if [ ! -f "${template}" ]; then
        echo "error: plist template missing: ${template}" >&2
        return 1
    fi
    if [ ! -f "${PROJECT_DIR}/${entrypoint}" ]; then
        echo "error: entrypoint missing: ${PROJECT_DIR}/${entrypoint}" >&2
        return 1
    fi
    if [ ! -x "${SCRIPT_DIR}/${wrapper}" ]; then
        echo "error: ${wrapper} is missing or not executable" >&2
        return 1
    fi

    # Render the plist
    sed \
        -e "s|{{PROJECT_DIR}}|${PROJECT_DIR}|g" \
        -e "s|{{PYTHON}}|${PYTHON}|g" \
        "${template}" > "${target}.tmp"
    mv "${target}.tmp" "${target}"

    # Reload — bootout any prior instance, ignore "not loaded" errors
    service="${DOMAIN}/${label}"
    launchctl bootout "${service}" 2>/dev/null || true
    launchctl unload "${target}" 2>/dev/null || true

    launchctl bootstrap "${DOMAIN}" "${target}"
    echo "Installed ${label} at ${target}"
}

_check_session_file() {
    # The forwarder needs an interactive first-run sign-in to create the
    # Telethon session. Without it the agent will crash and respawn forever
    # at ThrottleInterval=30. Warn but don't refuse — the user might be
    # about to do the sign-in immediately.
    session_file="${PROJECT_DIR}/data/.telethon_session"
    if [ ! -f "${session_file}" ] && [ ! -f "${session_file}.session" ]; then
        cat >&2 <<EOF
warning: no Telethon session file at ${session_file}
   The forwarder needs an interactive first-run sign-in:
       cd ${PROJECT_DIR} && python3 scripts/telethon_forwarder.py
   Telegram sends an SMS code; entering it saves the session. Once the
   session exists, the launchd agent runs non-interactively.
EOF
    fi
}

case "${MODE}" in
    bot)
        _install_agent \
            "local.potion-perps-bot" \
            "local.potion-perps-bot.plist.template" \
            "run.sh" "main.py"
        ;;
    forwarder)
        _check_session_file
        _install_agent \
            "local.potion-perps-forwarder" \
            "local.potion-perps-forwarder.plist.template" \
            "forwarder_run.sh" "scripts/telethon_forwarder.py"
        ;;
    all)
        _install_agent \
            "local.potion-perps-bot" \
            "local.potion-perps-bot.plist.template" \
            "run.sh" "main.py"
        _check_session_file
        _install_agent \
            "local.potion-perps-forwarder" \
            "local.potion-perps-forwarder.plist.template" \
            "forwarder_run.sh" "scripts/telethon_forwarder.py"
        ;;
    *)
        echo "Usage: $0 [bot|forwarder|all]" >&2
        exit 1
        ;;
esac

cat <<EOF

Useful commands:
    Status (bot):       launchctl print ${DOMAIN}/local.potion-perps-bot | head -20
    Status (forwarder): launchctl print ${DOMAIN}/local.potion-perps-forwarder | head -20
    Tail bot log:       tail -f ${PROJECT_DIR}/logs/bot.log
    Tail forwarder:     tail -f ${PROJECT_DIR}/logs/forwarder.err
    Restart bot:        launchctl kickstart -k ${DOMAIN}/local.potion-perps-bot
    Restart forwarder:  launchctl kickstart -k ${DOMAIN}/local.potion-perps-forwarder
    Uninstall:          ${SCRIPT_DIR}/uninstall.sh all

Sleep note: caffeinate -i prevents idle sleep, but lid-closed-on-battery
can still sleep the machine. See deploy/launchd/README.md.
EOF

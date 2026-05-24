#!/bin/sh
# Uninstall launchd agent(s).
#
# Usage:
#   uninstall.sh             # uninstall main bot (default)
#   uninstall.sh bot         # uninstall main bot
#   uninstall.sh forwarder   # uninstall Telethon forwarder
#   uninstall.sh all         # uninstall both
#
# Idempotent — safe to run when an agent is not installed.

set -eu

MODE="${1:-bot}"
TARGET_DIR="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

case "$(uname -s)" in
    Darwin) ;;
    *)
        echo "error: uninstall.sh is macOS-only (uname=$(uname -s))" >&2
        exit 1
        ;;
esac

_uninstall_agent() {
    label="$1"
    target="${TARGET_DIR}/${label}.plist"
    service="${DOMAIN}/${label}"

    launchctl bootout "${service}" 2>/dev/null || true
    launchctl unload "${target}" 2>/dev/null || true

    if [ -f "${target}" ]; then
        rm -f "${target}"
        echo "Removed ${target}"
    else
        echo "No plist at ${target}, nothing to remove"
    fi
    echo "Uninstalled ${label}"
}

case "${MODE}" in
    bot)
        _uninstall_agent "local.potion-perps-bot"
        ;;
    forwarder)
        _uninstall_agent "local.potion-perps-forwarder"
        ;;
    all)
        _uninstall_agent "local.potion-perps-bot"
        _uninstall_agent "local.potion-perps-forwarder"
        ;;
    *)
        echo "Usage: $0 [bot|forwarder|all]" >&2
        exit 1
        ;;
esac

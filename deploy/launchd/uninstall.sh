#!/bin/sh
# Tear down the potion-perps-bot launchd agent (Phase 3.2).
#
# Idempotent: safe to run when the agent is not installed.
#
# What it does:
#   1. `launchctl bootout` the running service (ignore "not loaded").
#   2. Remove ~/Library/LaunchAgents/local.potion-perps-bot.plist.

set -eu

LABEL="local.potion-perps-bot"
TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
SERVICE="gui/${UID_NUM}/${LABEL}"

case "$(uname -s)" in
    Darwin) ;;
    *)
        echo "error: uninstall.sh is macOS-only (uname=$(uname -s))" >&2
        exit 1
        ;;
esac

launchctl bootout "${SERVICE}" 2>/dev/null || true
launchctl unload "${TARGET}" 2>/dev/null || true

if [ -f "${TARGET}" ]; then
    rm -f "${TARGET}"
    echo "Removed ${TARGET}"
else
    echo "No plist at ${TARGET}, nothing to remove"
fi
echo "Uninstalled ${LABEL}"

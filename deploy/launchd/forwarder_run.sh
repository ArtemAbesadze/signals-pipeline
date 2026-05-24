#!/bin/sh
# Wrapper invoked by launchd via local.potion-perps-forwarder.plist.
#
# Same pattern as run.sh for the main bot:
#   - cd to project root so .env, data/, logs/ resolve as expected
#   - `caffeinate -i` blocks idle sleep while the forwarder is alive
#   - `exec` so SIGTERM from launchctl bootout reaches Python directly
#   - rotates forwarder.{out,err} on each restart (cp + truncate, not mv,
#     because launchd's open fd would orphan onto the renamed inode — see
#     run.sh comments for the full explanation)
#
# PYTHONBIN is injected by the plist's EnvironmentVariables (set at install
# time by install.sh). Fallback to `python3` is for ad-hoc shell use, not
# production.
set -e
cd "$(dirname "$0")/../.."

_rotate_launchd_log() {
    f="$1"
    if [ -s "$f" ]; then
        cp "$f" "$f.1"
    fi
    : > "$f"
}
mkdir -p logs
_rotate_launchd_log logs/forwarder.out
_rotate_launchd_log logs/forwarder.err

exec /usr/bin/caffeinate -i "${PYTHONBIN:-python3}" scripts/telethon_forwarder.py

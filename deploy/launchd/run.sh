#!/bin/sh
# Wrapper invoked by launchd via deploy/launchd/local.potion-perps-bot.plist.
#
# - `cd` to project root so relative paths (config/, data/, logs/) resolve
# - `caffeinate -i` prevents idle sleep while the bot is running. Lid-closed
#   on battery can still sleep (see deploy/launchd/README.md for the gap)
# - `exec` so SIGTERM from `launchctl bootout` / `launchctl kickstart -k`
#   reaches Python directly; main.py's handlers shut down cleanly
#
# PYTHONBIN is injected by the plist's EnvironmentVariables (set at install
# time by install.sh). Fallback to `python3` is for ad-hoc shell invocation
# outside launchd, not for production.
set -e
cd "$(dirname "$0")/../.."
exec /usr/bin/caffeinate -i "${PYTHONBIN:-python3}" main.py

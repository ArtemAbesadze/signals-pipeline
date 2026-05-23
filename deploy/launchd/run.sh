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

# Bound the launchd-managed stdout/stderr files (Phase 3.3): launchd writes
# them directly via StandardOutPath / StandardErrorPath with no rotation, so
# any sustained spam would grow them unboundedly. 2-deep ring — previous
# boot's diagnostics survive one restart, older boots are dropped. Combined
# with the plist's ThrottleInterval=30, per-cycle growth is bounded.
#
# NOTE: launchd opens StandardOut/ErrorPath with O_APPEND *before* exec'ing
# this script. We can't `mv` the file — the open fd would keep writing to
# the renamed inode (so the new process's output would land in .1, not the
# fresh file). Instead, `cp` the content to .1, then truncate the original
# in place. Launchd's fd still points at the same inode, which now has
# zero bytes, and O_APPEND writes resume from offset 0.
_rotate_launchd_log() {
    f="$1"
    if [ -s "$f" ]; then
        cp "$f" "$f.1"
    fi
    : > "$f"
}
mkdir -p logs
_rotate_launchd_log logs/launchd.out
_rotate_launchd_log logs/launchd.err

exec /usr/bin/caffeinate -i "${PYTHONBIN:-python3}" main.py

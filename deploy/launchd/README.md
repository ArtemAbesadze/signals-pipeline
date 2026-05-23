# launchd deployment (Phase 3.2)

Runs `potion-perps-bot` 24/7 on macOS as a user-level launchd agent. Survives
crashes (auto-respawn) and reboots (auto-start on login).

> The project's main `README.md` is frozen during the rework and gets a full
> rewrite in Phase 3.4. The operational notes below will migrate there at that
> point; for now this file is the source of truth.

## Install

```sh
deploy/launchd/install.sh
```

The script:
- Resolves the absolute path to `python3` (refuses if < 3.10).
- Renders `local.potion-perps-bot.plist.template` into
  `~/Library/LaunchAgents/local.potion-perps-bot.plist`.
- Loads the agent into your GUI session via `launchctl bootstrap`.

It's idempotent — re-run it after a `git pull` or a Python upgrade to
re-render and re-bootstrap.

## Uninstall

```sh
deploy/launchd/uninstall.sh
```

Tears down the agent and removes the installed plist. Idempotent.

## Status, logs, restart

```sh
# Is it running? (PID, last exit code, restart count)
launchctl print gui/$(id -u)/local.potion-perps-bot | head -20

# Live application logs (structlog rotating files)
tail -f logs/bot.log

# Early-startup crashes (before structlog initialises — config / import errors)
tail -f logs/launchd.err

# Manual restart without uninstalling
launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot
```

## Log files & rotation

The bot writes two streams of logs:

| File | Written by | Rotation |
|---|---|---|
| `logs/bot.log` | The bot (structlog, JSON) | Size-based: 10 MB per file, 5 backups (`logs/bot.log.1` … `.5`) |
| `logs/launchd.out`, `logs/launchd.err` | launchd directly (stdout/stderr) | 2-deep ring; rotated on every bot startup by `run.sh` |

The launchd files exist for **pre-structlog crash diagnostics only** — import
errors, config-load errors, anything that crashes before logging is wired up.
On every restart, `run.sh` copies the current `launchd.{out,err}` to `.1`
(overwriting the previous `.1`) and truncates the original in place. Because
`ThrottleInterval=30` caps respawn frequency, per-cycle growth is bounded.

We `cp` and truncate-in-place rather than `mv` because launchd has already
opened the file by the time `run.sh` runs — a rename would leave the open
file descriptor pointing at the renamed inode and the new process's output
would land in `.1`, not the fresh file.

## What this gives you

- **Auto-respawn on crash.** `KeepAlive.SuccessfulExit = false` means launchd
  restarts the bot only if it exits non-zero. Clean `SIGTERM` / `launchctl
  bootout` stops are respected.
- **Auto-start on login.** `RunAtLoad = true`.
- **Throttle.** `ThrottleInterval = 30` keeps a crash loop from hammering
  Discord/Telegram with reconnects.
- **Idle-sleep prevention.** `run.sh` wraps the bot in `caffeinate -i`, which
  holds an idle-sleep assertion for the bot's lifetime.

## What this does **not** give you — known gaps

### Lid-closed-on-battery sleep

`caffeinate -i` prevents *idle* sleep, but macOS will still sleep the machine
when the lid closes on battery power (clamshell power management). The bot
process is paused, and any CryptoPrinter signal fired during that window is
**lost** — Discord delivers `on_message` events live only; there is no
backfill, no edit-event listener, no queue.

**Mitigations** (in order of operational simplicity):

1. Leave the laptop plugged in.
2. Don't close the lid, or use a clamshell-mode setup (external display +
   power).
3. Migrate to a VPS (Phase 5.2). This is the only true fix.

We intentionally do **not** script `pmset` changes from the installer —
mutating global power settings without an obvious undo is the wrong kind of
surprise.

### Network failures

The plist does not restart on network state changes. The bot's Discord and
Telegram clients reconnect on their own; that's the right layer.

### Same-disk backups

Daily SQLite backups (Phase 3.1) land in `backups/` on the same disk as the
source DB. Real DR — an offsite copy step — is parked for Phase 5.

## Files in this directory

| File | Purpose |
|---|---|
| `local.potion-perps-bot.plist.template` | Plist with `{{PROJECT_DIR}}` / `{{PYTHON}}` placeholders |
| `run.sh` | Wrapper invoked by launchd — execs `caffeinate -i $PYTHONBIN main.py` |
| `install.sh` | Renders the plist, bootstraps the service |
| `uninstall.sh` | Bootouts the service, removes the installed plist |
| `README.md` | This file |

## What lives outside the repo after install

`~/Library/LaunchAgents/local.potion-perps-bot.plist` — the rendered, machine-
specific plist. Removed by `uninstall.sh`. Not tracked in git.

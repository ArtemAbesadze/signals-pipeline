# launchd deployment (Phase 3.2 + Phase 4.1)

Runs `potion-perps-bot` 24/7 on macOS as user-level launchd agents. Survives
crashes (auto-respawn) and reboots (auto-start on login). Two agents:

| Label | Purpose |
|---|---|
| `local.potion-perps-bot` | Main trading bot — `python3 main.py` |
| `local.potion-perps-forwarder` | Telethon @PotionScannerBot DM relay — `python3 scripts/telethon_forwarder.py` (Phase 4.1) |

The forwarder exists because Telegram's Bot API can't observe another bot's
outbound DMs. To capture @PotionScannerBot's signals we have to BE a
recipient — a personal Telegram account (Telethon) subscribes to the bot and
relays each DM into a private channel the trading bot reads.

## Install

```sh
deploy/launchd/install.sh             # bot only (default — Phase 3 behavior)
deploy/launchd/install.sh forwarder   # forwarder only
deploy/launchd/install.sh all         # both
```

The script:
- Resolves the absolute path to `python3` (refuses if < 3.10).
- Renders the appropriate plist template(s) into
  `~/Library/LaunchAgents/`.
- Loads the agent(s) via `launchctl bootstrap`.

Idempotent — re-run after a `git pull` or Python upgrade.

### One-time Telethon setup (before installing the forwarder)

The forwarder needs an interactive sign-in to produce a session file
(`data/.telethon_session`). Without it the launchd agent will crash-respawn
forever. From the repo root:

```sh
python3 scripts/telethon_forwarder.py
```

Telegram sends an SMS / app code; enter it at the prompt. The session file is
written with `0600` permissions and `.gitignore`'d. Subsequent runs (and the
launchd agent) are non-interactive.

`install.sh forwarder` warns and continues if the session file is missing —
the agent will keep crashing until you run the interactive sign-in, but
nothing else breaks.

## Uninstall

```sh
deploy/launchd/uninstall.sh             # bot only
deploy/launchd/uninstall.sh forwarder   # forwarder only
deploy/launchd/uninstall.sh all         # both
```

Tears down the agent(s) and removes the installed plist(s). Idempotent.

## Status, logs, restart

```sh
# Is the bot running?
launchctl print gui/$(id -u)/local.potion-perps-bot | head -20
# Is the forwarder running?
launchctl print gui/$(id -u)/local.potion-perps-forwarder | head -20

# Live application logs (structlog rotating files)
tail -f logs/bot.log

# Forwarder logs — Telethon writes via the default stdlib logger to launchd's
# stdout/err, so the wrapper-rotated files are where to look
tail -f logs/forwarder.err

# Pre-structlog crashes (config / import errors) for the bot
tail -f logs/launchd.err

# Manual restart without uninstalling
launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot
launchctl kickstart -k gui/$(id -u)/local.potion-perps-forwarder
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
| `local.potion-perps-bot.plist.template` | Bot agent plist (token-substituted at install) |
| `local.potion-perps-forwarder.plist.template` | Forwarder agent plist |
| `run.sh` | Bot wrapper — `exec caffeinate -i $PYTHONBIN main.py` |
| `forwarder_run.sh` | Forwarder wrapper — `exec caffeinate -i $PYTHONBIN scripts/telethon_forwarder.py` |
| `install.sh` | Renders plist(s), bootstraps service(s). Mode: `bot` / `forwarder` / `all` |
| `uninstall.sh` | Bootouts service(s), removes installed plist(s). Same modes |
| `README.md` | This file |

## What lives outside the repo after install

`~/Library/LaunchAgents/local.potion-perps-bot.plist` — the rendered, machine-
specific plist. Removed by `uninstall.sh`. Not tracked in git.

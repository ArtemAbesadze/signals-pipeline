# Potion Perps Bot

A private automation tool for **three known users** (Artem + two friends). It
listens to CryptoPrinter's trade calls on Discord, parses them, executes the
corresponding perp trades on each user's Hyperliquid account, and exposes
monitoring & control through a per-user Telegram bot.

This is **not** a marketable SaaS. There is no signup, no broadcast, no paid
tier. The invite-code / subscription layer that existed in earlier versions
was removed during the rework. See [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md)
for the full reframe.

The design spine: **CryptoPrinter's calls should be the only possible source
of fallacy.** Every decision the bot makes is recorded so that "did CP get it
wrong, or did the bot do something CP didn't say?" can be answered from the
database in under 30 seconds.

---

## Quick start

```bash
git clone git@github.com:ArtemAbesadze/signals-pipeline.git potion-perps-bot
cd potion-perps-bot
pip install -r requirements.txt

cp .env.example .env                      # add HL creds + bot tokens
cp config/config.example.yaml config/config.yaml

python3 -m pytest tests/                  # 578 should pass
python3 main.py                           # foreground; Ctrl-C to stop
```

To run 24/7 on macOS via launchd:

```bash
deploy/launchd/install.sh                 # see deploy/launchd/README.md
```

---

## What's installed locally

After `git clone` + `deploy/launchd/install.sh`, this is the full inventory of
what physically exists on the machine:

| Artifact | Path | Created by | Purpose |
|---|---|---|---|
| Code | `~/ClaudeProjects/potion-perps-bot/` | `git clone` | The repo |
| launchd agent | `~/Library/LaunchAgents/local.potion-perps-bot.plist` | `deploy/launchd/install.sh` | Auto-starts on login, respawns on crash |
| Bot process | one `python3 main.py` under `caffeinate -i` | launchd → `run.sh` | The actual bot |
| Database | `data/trades.db` (+ `.db-wal`, `.db-shm`) | First run | All persistent state |
| Encryption key | `data/.encryption_key` | First run (auto) | Fernet master key for credential encryption |
| App logs | `logs/bot.log` (+ `.1`…`.5`) | structlog | Structured JSON, rotated at 10 MB × 5 |
| launchd logs | `logs/launchd.{out,err}` (+ `.1`) | launchd / `run.sh` | Pre-structlog crash diagnostics, 2-deep ring |
| Daily backups | `backups/trades-YYYYMMDD-HHMMSS.db` | Background task in the bot | 30-day retention, mtime-based prune |
| Ports | `:8080` (health), `:8081` (admin API) | The bot | Both bind `0.0.0.0` — harmless behind laptop NAT, see [§ remote server](#moving-to-a-remote-server) |

**Not on disk (must be set up):**

- `.env` — `HL_ACCOUNT_ADDRESS`, `HL_API_WALLET`, `HL_API_SECRET`, optionally `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `ADMIN_API_KEY`. Template: `.env.example`.
- `config/config.yaml` — non-secret runtime settings. Template: `config/config.example.yaml`.

Both are gitignored. They are the only files you bring with you across machines.

---

## The database & auditability

One SQLite file at `data/trades.db`, WAL mode. Single-writer (the bot), so no
contention. Four tables:

| Table | What it holds |
|---|---|
| `users` | Per-user state — Fernet-encrypted HL credentials, active preset, port settings |
| `trades` | One row per CP signal-derived position; includes `raw_signal_text` + `decision_snapshot` (JSON) |
| `orders` | Every order ever submitted to Hyperliquid (entry, TPs, SLs, replacements) |
| `trade_events` | Append-only audit log — every lifecycle event (signal received, parse error, TP hit, SL moved, manual close, etc.) with `raw_text` + `action_taken` |

**Per-user isolation:** composite PK `(user_id, trade_id)` on `trades` and
`orders`. All queries filter by `user_id`. One user's bug cannot touch another
user's data.

**The audit spine:**
- `trades.decision_snapshot` — JSON captured at trade open. Includes the
  active preset, the resolved sizing, the risk gate's verdict. Read this to
  answer "why did the bot size it that way?"
- `trade_events` — append-only. Read this to answer "what did the bot see,
  and what did it do about it?" Parse errors land here as `event_type='error'`
  rows, not as crashes (D6).

**Daily backups:** taken via the SQLite online backup API at 06:00 UTC, so
writers are not blocked during the snapshot. Files land in `backups/`,
30-day retention. **Caveat:** same-disk DR — see [§ remote server](#moving-to-a-remote-server).

### Inspecting a single trade end-to-end

The whole point of the audit trail is being able to reconstruct a trade
from the DB. The flow:

```bash
# 1. Find the trade you care about (most recent for a user)
sqlite3 -header -column data/trades.db \
  "SELECT trade_id, coin, side, status, created_at, close_reason
   FROM trades WHERE user_id = 'artem' ORDER BY trade_id DESC LIMIT 5;"

# 2. The raw signal text the bot received
sqlite3 data/trades.db \
  "SELECT raw_signal_text FROM trades WHERE user_id = 'artem' AND trade_id = 42;"

# 3. The decision snapshot — preset, sizing, risk gate
sqlite3 data/trades.db \
  "SELECT decision_snapshot FROM trades WHERE user_id = 'artem' AND trade_id = 42;" \
  | python3 -m json.tool

# 4. Every lifecycle event for that trade, in order
sqlite3 -header -column data/trades.db \
  "SELECT occurred_at, event_type, action_taken, substr(raw_text, 1, 60) AS raw
   FROM trade_events
   WHERE user_id = 'artem' AND trade_id = 42
   ORDER BY occurred_at;"

# 5. The orders submitted to Hyperliquid for that trade
sqlite3 -header -column data/trades.db \
  "SELECT order_type, side, size, price, status, fill_price
   FROM orders WHERE user_id = 'artem' AND trade_id = 42;"
```

This is the answer to "did CP get it wrong, or did our bot do something the
signal didn't say?"

---

## Daily operations

```bash
# Is the bot running?
launchctl print gui/$(id -u)/local.potion-perps-bot | head -20

# Live application logs
tail -f logs/bot.log

# Early-startup crashes (config error, import error)
tail -f logs/launchd.err

# Manual restart
launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot

# Stop (won't auto-restart)
launchctl bootout gui/$(id -u)/local.potion-perps-bot
```

### Where to look when X breaks

| Symptom | Start here |
|---|---|
| Bot didn't act on a CP message | `src/parser/classifier.py` → `pipeline.process_message` |
| Wrong size on a trade | `trades.decision_snapshot` → `src/strategy/position_sizer.py` |
| Order rejected by Hyperliquid | `src/exchange/order_builder.py` (sizing/rounding) → `position_manager.submit_trade` |
| Telegram command misbehaved | `src/telegram/handlers/` (one file per command group) |
| User can't register | `src/telegram/handlers/registration.py` + `src/state/user_db.py` |
| State out of sync after restart | `src/exchange/position_manager.py::sync_positions` |

---

## Configuration

Two files, both gitignored, both templated:

- **`.env`** — secrets. Hyperliquid keys, Telegram & Discord tokens, optional
  admin API key. Template: `.env.example`.
- **`config/config.yaml`** — everything else. Template: `config/config.example.yaml`.

The example YAML is the source of truth for what's available; this section is
just the orientation map.

| Block | What it controls |
|---|---|
| `input.adapter` | `simulation` / `cli` / `file` / `discord` — where the bot pulls signals from |
| `exchange.network` | `testnet` or `mainnet`. Each user picks at registration (D7); this is the global default. |
| `strategy.active_preset` | Which preset all users default to. Override per-user in their Telegram config. |
| `strategy_presets.*` | Custom presets (override built-ins by re-using the name) |
| `strategy.size_by_risk` | Override preset `size_pct` per CP risk level (`LOW` / `MEDIUM` / `HIGH`) |
| `risk.*` | Pre-trade circuit breakers: max open positions, daily loss, position cap, total exposure cap |
| `backups.*` | Daily backup schedule + retention |
| `logging.*` | Level + file path + per-library overrides (see `config/config.example.yaml`) |

### Strategy presets

The seven built-in presets (D2) mirror CryptoPrinter's weekly report rows so
your per-user PnL lines up directly with CP's columns:

| Preset | TP split (1/2/3) | SL → breakeven? | Default size |
|---|---|---|---|
| `tp1_only` | 100% / 0 / 0 | no | 2% |
| `tp2_only` | 0 / 100% / 0 | no | 2% |
| `tp3_only` | 0 / 0 / 100% | no | 2% |
| `tp2_be` | 0 / 100% / 0 | after TP1 | 2% |
| `tp3_be` | 0 / 0 / 100% | after TP1 | 2% |
| `hybrid` | 10% / 70% / 20% | after TP1 | 2% |
| `even_split` *(default)* | 33% / 33% / 34% | after TP1 | 2% |

**No bot-imposed exit logic.** The active preset is the only authority on
when and how positions are exited (D2). No "smart" overrides, no surprise
closures.

---

## Cleaning up local disk

Everything that grows is already bounded:

| Source | Cap |
|---|---|
| `logs/bot.log*` | 50 MB hard (10 MB × 5 backups, rotated by `RotatingFileHandler`) |
| `logs/launchd.{out,err}*` | ~`ThrottleInterval × stderr-spam-rate`, 2-deep ring (rotated by `run.sh` on every restart) |
| `backups/trades-*.db` | 30 days, mtime-based prune by the bot |
| `data/trades.db` | Grows with trade events; ~MB-class after a year of active trading |

That said, manual recipes:

```bash
# Drop log backups (keeps current bot.log, drops .1 through .5 and launchd .1s)
rm -f logs/bot.log.[1-9] logs/launchd.{out,err}.1

# Drop old DB backups beyond retention (the bot does this itself; manual = belt + braces)
find backups -name 'trades-*.db' -mtime +30 -delete

# Shrink the DB after large deletes / closed-trade pruning
# IMPORTANT: stop the bot first — VACUUM acquires an exclusive lock
launchctl bootout gui/$(id -u)/local.potion-perps-bot
sqlite3 data/trades.db 'VACUUM;'
deploy/launchd/install.sh                  # re-bootstrap

# Delete shadow-mode captures (when Phase 4.1 lands)
find signals/captures -name '*.txt' -delete
```

**Nuclear reset** (lose everything, re-register all users):

```bash
launchctl bootout gui/$(id -u)/local.potion-perps-bot
rm -rf data/ logs/ backups/
deploy/launchd/install.sh
# Then /register from each user's Telegram
```

Do this only if (a) the DB is corrupt past the point of recovery from
backups, or (b) you're moving to a clean environment and want a fresh start.
Not for routine cleanup.

---

## Re-registering a user

There are three scenarios that look similar but have different blast radii.

### A. User wants to change their HL API wallet (rotation, not loss)

1. From Telegram, run `/cancel` to abort any in-flight registration, then `/register`.
2. The conversation flow walks them through entering the new credentials.
3. Old credentials are overwritten in the `users` row. Trade history is preserved.

### B. `data/.encryption_key` is lost or rotated

This is the destructive case — losing the master key means every Fernet-encrypted
credential in the DB is now undecryptable. **Trade history is still intact**,
but every user has to re-enter their HL credentials.

1. Stop the bot: `launchctl bootout gui/$(id -u)/local.potion-perps-bot`.
2. Delete the dead key: `rm data/.encryption_key`.
3. Re-start: `deploy/launchd/install.sh`. A fresh key is generated automatically on first run.
4. Each user runs `/register` again from Telegram. The flow detects that their
   stored creds are now garbage and prompts for re-entry.
5. Trades opened before the rotation are still in the DB and visible — only
   the credentials needed to operate on the exchange were lost.

### C. Replace a user wholesale

For example, one of the friends drops out and a different one takes their slot.

1. From Telegram, the admin runs `/admin` to list users.
2. Use the admin REST API to deactivate the old user — `curl -X POST -H "X-API-Key: $K" http://127.0.0.1:8081/api/users/<uid>/deactivate`. This stops dispatching CP signals to them; their trade history is preserved.
3. The new user `/register`s from their own Telegram account.

Never delete a `users` row directly. Deactivation is reversible, deletion
cascades into orphaned `trades` / `orders` / `trade_events` rows that
confuse the audit trail.

---

## Moving to a remote server

The laptop deployment is *one* deployment. It is not the canonical one.

### Why you'd move

Laptop sleep is the silent killer. `caffeinate -i` prevents idle sleep, but
macOS will still sleep the machine when the lid closes on battery (clamshell
power management). The bot process is paused, and any CryptoPrinter signal
fired during that window is **lost** — Discord delivers `on_message` events
live only, with no backfill. A VPS doesn't sleep.

### Three structural things to fix before exposing to the internet

1. **`:8080` health + `:8081` admin currently bind `0.0.0.0`.** On a public
   VPS, the `X-API-Key` is the entire perimeter. Either bind to `127.0.0.1`
   and put a reverse proxy in front, or add an IP allowlist + TLS.
   (`src/health.py:65`, `src/api/admin.py:83`.)
2. **`backups/` is same-disk.** For real DR you need an offsite copy step.
   Add a cron / systemd timer that `rsync`s `backups/` to another host or
   object store after the bot's daily backup completes.
3. **Replace launchd with systemd.** The plist is the only macOS-specific
   thing in this repo. Everything else is portable Python + stdlib + YAML
   config + env vars.

### Migration checklist

1. **Provision** a Linux VPS. Python 3.10+. Any small instance handles the
   load — this is one process doing IO-bound work.
2. **Clone and install dependencies**:
   ```bash
   git clone git@github.com:ArtemAbesadze/signals-pipeline.git
   cd signals-pipeline
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Copy secrets and config out-of-band** (not via git):
   - `.env`
   - `config/config.yaml`
4. **Preserve credentials and history** (optional but usually wanted):
   - `data/.encryption_key`
   - `data/trades.db`
   The key has to come over with the DB — without it, the encrypted
   credentials in the DB are unreadable and every user has to re-register
   (see § re-registering, scenario B).
5. **Write a systemd unit.** Sketch (`/etc/systemd/system/potion-perps-bot.service`):
   ```ini
   [Unit]
   Description=Potion Perps Bot
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=potion
   WorkingDirectory=/home/potion/signals-pipeline
   ExecStart=/home/potion/signals-pipeline/.venv/bin/python3 main.py
   Restart=on-failure
   RestartSec=30s
   StandardOutput=append:/home/potion/signals-pipeline/logs/systemd.out
   StandardError=append:/home/potion/signals-pipeline/logs/systemd.err

   [Install]
   WantedBy=multi-user.target
   ```
   `systemctl daemon-reload && systemctl enable --now potion-perps-bot`.
6. **Lock down the ports.** In `config/config.yaml` set `health.port` and
   the admin API to bind on `127.0.0.1` (requires a small change to
   `src/api/admin.py` and `src/health.py` to honour a bind-address from
   config — currently hard-coded to `0.0.0.0`).
7. **Add an offsite backup step.** Example cron entry:
   ```cron
   30 6 * * *  rsync -az --delete /home/potion/signals-pipeline/backups/ remote-host:potion-backups/
   ```
   06:30 UTC = 30 minutes after the bot's daily snapshot.
8. **Smoke-test on testnet first** (D7). Switch to mainnet per user only
   after a clean testnet run.

---

## Telegram bot

DM-only. Group messages are rejected by the `dm_only_filter` middleware.
Credential-collection messages are deleted on receipt.

### User commands

| Command | What it does |
|---|---|
| `/start` | First-touch greeting + link to register |
| `/register` | Walk through HL creds + testnet/mainnet choice (multi-step conversation) |
| `/menu` | Dashboard — open trades, recent activity, port status |
| `/help` | Command list |
| `/cancel` | Cancel an in-progress conversation (e.g., abort `/register` mid-flow) |
| `/balance` | Hyperliquid balance |
| `/positions` | Open positions |
| `/port` | Port/wallet status: configured `port_usd`, mode (withdraw / compound / watermark), live wallet, guardrail status |
| `/trades` | Open trades (paginated) |
| `/history` | Closed trades (paginated) |
| `/stats` | Win-rate, PnL, average R, by-preset breakdown |
| `/config` | View / edit per-user config (preset, port settings, auto-execute) |
| `/preset` | Change active preset (shortcut to `/config`) |
| `/auto` | Toggle auto-execute on/off |
| `/activate`, `/deactivate` | Resume or pause CP signal dispatch to this user |

### Admin-only commands

Only users in the admin list (set via env / admin API) can run these.

| Command | What it does |
|---|---|
| `/admin` | List admin commands |
| `/kill` | Halt all trading across all users (sets a global flag; existing positions are not touched) |
| `/resume` | Lift the kill switch |
| `/add_admin`, `/remove_admin`, `/list_admins` | Manage the admin list |
| `/inject` | Manually inject a signal text (useful for testing — uses the full pipeline) |

### Admin REST API (`:8081`)

`X-API-Key` header required (set `ADMIN_API_KEY` in `.env`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/users` | Create a user |
| `GET` | `/api/users` | List users |
| `GET` | `/api/users/{user_id}` | Get one user |
| `PUT` | `/api/users/{user_id}` | Update one user |
| `POST` | `/api/users/{user_id}/activate` | Start dispatching signals to this user |
| `POST` | `/api/users/{user_id}/deactivate` | Stop dispatching signals to this user |
| `POST` | `/api/kill` | Kill switch across all users |
| `POST` | `/api/resume` | Lift the kill switch |

---

## Testnet → mainnet promotion

This is a serious moment. The promotion checklist:

1. **Mandatory testnet soak.** Run on testnet for at least one full CP signal
   day (multiple opens, at least one TP hit, at least one SL hit). Audit:
   ```bash
   sqlite3 data/trades.db \
     "SELECT trade_id, coin, side, status, close_reason, pnl_pct
      FROM trades WHERE user_id = '<uid>' ORDER BY trade_id DESC LIMIT 20;"
   ```
   Look for: any `parse_error` rows in `trade_events`, any orders rejected
   by Hyperliquid, any decision_snapshots whose sizing doesn't match what
   the preset says.
2. **Port size dry-run.** With the user's intended mainnet `port_usd`, run
   `/port` on testnet and confirm:
   - `port_usd` ≤ wallet × 0.95 (guardrail warning threshold).
   - Daily-loss circuit breaker (`risk.max_daily_loss_pct`) matches your
     real tolerance.
   - Per-position cap (`risk.max_position_size_usd`) matches your real cap.
3. **Mainnet promotion gate** (Phase 3.5 — pending). When shipped, this will
   require an explicit Telegram confirmation dialog for the first mainnet
   trade above a threshold.
4. **Flip network.** From `/register` on Telegram, or by editing the user's
   row via the admin API. Each user flips independently — there is no
   global mainnet switch.
5. **Watch the first trade.** Stay at the terminal, tail `logs/bot.log`,
   keep `/positions` open in Telegram. The first mainnet trade is the one
   most likely to surface a network-specific bug (different symbol
   alias, different min-order rounding, different precision).
6. **Backup the DB before promotion.** Manual snapshot in addition to the
   daily:
   ```bash
   cp data/trades.db "backups/pre-mainnet-$(date -u +%Y%m%d-%H%M%S).db"
   ```

---

## Development

```bash
python3 -m pytest tests/                  # full suite, ~6s
python3 -m pytest tests/test_logger.py -v # one file

git checkout -b feature/<name>            # branch from rework/scope-v1 during the rework
```

Project structure: see [`CLAUDE.md`](CLAUDE.md) (kept current, indexed for
future sessions). The rework spine: [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md).

Conventions enforced across the codebase:

- **Audit everything.** Every trade open writes a `decision_snapshot`. Every lifecycle event writes a `trade_events` row.
- **Never crash on bad input.** Parse errors become `trade_events.event_type='error'` rows; the pipeline keeps running.
- **No bot-imposed exit logic.** The active preset is the only authority on exits.
- **Per-user isolation.** Composite PK `(user_id, trade_id)`. All queries filter by user.
- **Tests close behind code.** ~580 tests across `tests/`. New features land with tests, not after.

---

## Phase status

| Phase | What | Status |
|---|---|---|
| 1 | Audit, parsing, sizing, strategy presets | ✅ shipped |
| 2 | Telegram menu / dashboard / port screen / audit trail | ✅ shipped |
| 3.1 | Daily SQLite backups (D9) | ✅ shipped |
| 3.2 | launchd agent for 24/7 local deployment | ✅ shipped |
| 3.3 | Log rotation polish | ✅ shipped |
| 3.4 | README rewrite (this file) | ✅ shipped |
| 3.5 | Mainnet promotion gate (confirmation dialog) | ⏳ pending |
| 4   | Go live — wire CP's real Discord, onboard on testnet, then mainnet | ⏳ pending |
| 5   | Parking lot — weekly perf report, VPS, CI/CD, backtest tooling | ⏳ later |

Full phase breakdown: [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md).

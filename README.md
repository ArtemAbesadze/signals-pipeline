# Potion Perps Bot

A private automation tool for **three known users** (Artem + two friends). It
ingests CryptoPrinter's trade calls from a Telegram channel, parses them,
executes the corresponding perp trades on each user's Hyperliquid account, and
exposes monitoring & control through a per-user Telegram bot.

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
git checkout rework/scope-v1                # active branch — main is pre-rework
pip install -r requirements.txt

cp .env.example .env                        # add HL creds + Telegram tokens
cp config/config.example.yaml config/config.yaml

python3 -m pytest tests/                    # 684 should pass
python3 main.py                             # foreground; Ctrl-C to stop
```

To run 24/7 on macOS, install **both** launchd agents (the bot itself + the
Telethon forwarder that pulls CP signals from your personal Telegram account):

```bash
deploy/launchd/install.sh all
```

See [§ Architecture](#architecture) for why two processes.

---

## Architecture

The most distinctive thing about this codebase is **how CP signals reach the
bot**. They do **not** come from Discord directly — they come via a chain
that exists because Railway already runs the public CP Discord listener and we
can't add a second one. The whole story:

```
CryptoPrinter Discord channel
    ↓ (Railway's listener — out of our control)
@PotionScannerBot DMs each subscriber on Telegram
    ↓ (one subscriber = Artem's personal Telegram account)
scripts/telethon_forwarder.py
  (Telethon, running as Artem's userbot)
    ↓ posts each DM verbatim
"Potion Signals Mirror" private Telegram channel
    ↓ (channel_post updates)
Trading bot's TelegramChannelAdapter → pipeline
    ↓ classify → parse → size → submit
Hyperliquid (per-user via encrypted API keys)
```

Two processes, two launchd agents:

| Agent | What | Process |
|---|---|---|
| `local.potion-perps-bot` | Main trading bot — Telegram bot + pipeline + Hyperliquid client + admin API | `python3 main.py` |
| `local.potion-perps-forwarder` | Telethon userbot — listens to @PotionScannerBot DMs and reposts to the private mirror channel | `python3 scripts/telethon_forwarder.py` |

Both managed by `deploy/launchd/install.sh all`. Both wrapped in
`caffeinate -i` so macOS won't idle-sleep them.

**Why not direct Discord?** Discord allows one gateway connection per bot
token. Railway already runs `@PotionScannerBot` for external paying users;
sharing the token would knock their service offline. We can't add a second
bot to CP's server (no admin access). Discord has no event-mirroring feature;
sharding distributes events, doesn't duplicate them. This was researched
and ruled out — don't re-propose without new info.

**Why Telethon (user account) and not a second Telegram bot?**
@PotionScannerBot DMs subscribers, and `bot` accounts can't read another
bot's DMs. Telethon runs as a real user account, which can. Telethon's userbot
pattern is tolerated by Telegram for personal-account automation (no spam, no
mass DMs).

**The Discord adapter is still in the codebase.** It's tested and works. It
just isn't the live source. If the Railway / Telethon chain ever breaks, it's
the fallback.

---

## Current configuration

State as of the most recent commit on `rework/scope-v1`:

| Field | Value | Where |
|---|---|---|
| Active branch | `rework/scope-v1` | Default branch on GitHub |
| Input adapter | `telegram_channel` | `config/config.yaml` |
| Default network | `testnet` | `config/config.yaml`; per-user override at registration |
| Default preset | `even_split` (33/33/34 + SL→BE after TP1) | `config/config.yaml` |
| Default auto-execute | `false` | `config/config.yaml`; per-user override |
| Mainnet confirm threshold | $100 (size above this requires Telegram approval) | `config/config.example.yaml::risk.mainnet_confirm_above_usd` |
| Mainnet confirm timeout | 5 minutes | `config/config.example.yaml::risk.mainnet_confirm_timeout_min` |
| Tests | 684 passing | `tests/` |
| Users registered | 2 of 3 slots — both on testnet | `users` + `user_config` tables |

**Per-user state right now:**

```
user_id      display_name  network   active_preset  auto_execute  port_usd  port_mode
-----------  ------------  --------  -------------  ------------  --------  ---------
7441245554   Artem A       testnet   even_split     ON            $500      withdraw
7375268438   swaag         testnet   even_split     OFF           (unset)   withdraw
```

Note: swaag has no `port_usd` configured, so every CP signal records a
"skipped: port not configured" event for her until she sets one. The bot keeps
running normally; just her dispatch is a no-op.

To see this for yourself any time:

```bash
sqlite3 -header -column data/trades.db \
  "SELECT u.user_id, u.display_name, c.network, uc.active_preset,
          uc.auto_execute, uc.port_usd, uc.port_mode
   FROM users u
   JOIN user_credentials c USING(user_id)
   JOIN user_config uc USING(user_id);"
```

---

## Limitations

Honest list of what this tool does *not* do.

**Scope**
- **Three users, max.** There's no UX or operations story for more. Adding a
  fourth would work technically but isn't a use case we've designed for.
- **Hyperliquid only.** No other exchange. The position sizer, order builder,
  and asset metadata are all HL-specific.
- **CryptoPrinter only.** No other signal source. The parser is tuned to CP's
  message format.

**Operational**
- **Single point of dispatch.** One Telethon forwarder, on one machine, with
  one personal Telegram account. If Artem's Telegram session expires or his
  laptop is offline, the bot stops receiving signals.
- **No backfill on missed signals.** If signals fire while the laptop is
  asleep or the Telethon session is broken, those signals are gone. Neither
  Telegram nor CP nor Railway re-sends.
- **Laptop sleep risk.** `caffeinate -i` blocks idle sleep. **Lid-closed on
  battery still sleeps the machine** (clamshell power management). Stay
  plugged in + lid open during signal hours, or move to a VPS (Phase 5.2 —
  see [§ remote server](#moving-to-a-remote-server)).
- **Railway / Telethon chain dependency.** The CP signal path now depends on:
  Railway's @PotionScannerBot being up, Artem's Telegram account being
  reachable, the Telethon session staying valid. Any link breaking = no
  signals. The bot will run fine but with nothing to process.
- **Same-disk DB backups.** Daily SQLite backups land in `backups/` on the
  same machine. Real DR needs an offsite step. Documented in
  [§ remote server](#moving-to-a-remote-server).

**Trade execution**
- **No bot-imposed exit logic.** The active preset is the only authority on
  exits. The bot does not "smart-close" on volatility, news, drawdown, etc.
  (Design decision D2.)
- **No partial-fill recovery beyond what Hyperliquid does natively.** If HL
  partially fills an IOC and rejects the rest, the bot accepts that state.
- **Approximate fill prices on lifecycle events.** When CP says "TP1 hit",
  the bot marks the local order FILLED at the *target* price, not the
  exchange's actual fill price (which CP doesn't expose). Real PnL is what
  HL says it is, but the order audit row shows approximate prices.
- **Testnet has its own failure modes.** Thin orderbooks and oracle drift
  cause "Price too far from oracle" rejections on IOC closes (a real
  outstanding NEAR position on testnet is documented in CLAUDE.md). Mainnet
  liquidity makes this a non-issue but it can mislead testnet soak.

**Security**
- **Encryption key + DB on the same disk.** Lose the laptop, lose both.
  Documented mitigation: re-register (scenario B below).
- **Admin API binds to `0.0.0.0`.** Harmless behind laptop NAT, must be
  locked down before a VPS move. Three structural fixes called out in
  [§ remote server](#moving-to-a-remote-server).
- **Telethon session file is account-level credentials.** Treat
  `data/.telethon_session.session` like a password. If it leaks, attackers
  can read all of Artem's Telegram DMs. Gitignored, 0600 perms, but it's on
  disk and decryptable.

---

## What's installed locally

After `git clone` + `deploy/launchd/install.sh all`, this is the full
inventory of what physically exists on the machine:

| Artifact | Path | Created by | Purpose |
|---|---|---|---|
| Code | `~/ClaudeProjects/potion-perps-bot/` | `git clone` | The repo |
| Bot agent | `~/Library/LaunchAgents/local.potion-perps-bot.plist` | `install.sh bot` | Auto-starts main.py on login, respawns on crash |
| Forwarder agent | `~/Library/LaunchAgents/local.potion-perps-forwarder.plist` | `install.sh forwarder` | Auto-starts Telethon forwarder on login |
| Bot process | `python3 main.py` under `caffeinate -i` | launchd → `run.sh` | The trading bot |
| Forwarder process | `python3 scripts/telethon_forwarder.py` under `caffeinate -i` | launchd → `forwarder_run.sh` | Listens to @PotionScannerBot DMs, mirrors to private channel |
| Database | `data/trades.db` (+ `.db-wal`, `.db-shm`) | First bot run | All persistent state — users, trades, orders, audit log |
| Encryption key | `data/.encryption_key` | First bot run (auto) | Fernet master key for credential encryption |
| Telethon session | `data/.telethon_session.session` | First forwarder run (interactive login) | Telethon account-level credentials (0600, gitignored) |
| App logs | `logs/bot.log` (+ `.1`…`.5`) | structlog | Structured JSON, rotated at 10 MB × 5 |
| Bot launchd logs | `logs/launchd.{out,err}` (+ `.1`) | launchd / `run.sh` | Pre-structlog crash diagnostics, 2-deep ring |
| Forwarder logs | `logs/forwarder.{out,err}` (+ `.1`) | launchd / `forwarder_run.sh` | 2-deep ring |
| Daily backups | `backups/trades-YYYYMMDD-HHMMSS.db` | Background task in the bot | 30-day retention, mtime-based prune |
| Ports | `:8080` (health), `:8081` (admin API) | The bot | Both bind `0.0.0.0` — harmless behind laptop NAT, see [§ remote server](#moving-to-a-remote-server) |

**Not on disk (must be set up):**

- `.env` — `HL_ACCOUNT_ADDRESS`, `HL_API_WALLET`, `HL_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TG_SOURCE_BOT`, `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_PHONE`, optionally `ADMIN_API_KEY`. Template: `.env.example`.
- `config/config.yaml` — non-secret runtime settings. Template: `config/config.example.yaml`.

Both are gitignored. They are the only files you bring with you across machines
(along with `data/.encryption_key` if you want to keep credentials decryptable,
and `data/.telethon_session.session` if you don't want to re-login Telethon).

---

## The database & auditability

One SQLite file at `data/trades.db`, WAL mode. Single-writer (the bot), so no
contention. Six tables:

| Table | What it holds |
|---|---|
| `users` | One row per Telegram user — display name, status |
| `user_credentials` | Fernet-encrypted HL credentials + per-user network choice (testnet/mainnet) |
| `user_config` | Per-user preset, auto-execute, port settings, risk limits, custom presets |
| `telegram_admins` | Who can run `/admin` commands |
| `trades` | One row per CP signal-derived position; includes `raw_signal_text` + `decision_snapshot` (JSON) |
| `orders` | Every order ever submitted to Hyperliquid (entry, TPs, SLs, replacements) |
| `trade_events` | Append-only audit log — every lifecycle event (signal received, parse error, TP hit, SL moved, manual close, dedup suppressed, etc.) |

**Per-user isolation:** composite PK `(user_id, trade_id)` on `trades` and
`orders`. All queries filter by `user_id`. One user's bug cannot touch another
user's data.

**The audit spine:**
- `trades.decision_snapshot` — JSON captured at trade open. Includes the
  active preset, the resolved sizing, the risk gate's verdict, the
  port-at-open. Read this to answer "why did the bot size it that way?"
- `trade_events` — append-only. Read this to answer "what did the bot see,
  and what did it do about it?" Parse errors land here as `event_type='error'`
  rows, not as crashes (D6). Duplicate CP messages caught by the pipeline
  dedup (Bug #14) leave a row with `action_taken='deduplicated: ...'`.

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
   FROM trades WHERE user_id = '7441245554' ORDER BY trade_id DESC LIMIT 5;"

# 2. The raw signal text the bot received
sqlite3 data/trades.db \
  "SELECT raw_signal_text FROM trades WHERE user_id = '7441245554' AND trade_id = 2177;"

# 3. The decision snapshot — preset, sizing, risk gate
sqlite3 data/trades.db \
  "SELECT decision_snapshot FROM trades WHERE user_id = '7441245554' AND trade_id = 2177;" \
  | python3 -m json.tool

# 4. Every lifecycle event for that trade, in order
sqlite3 -header -column data/trades.db \
  "SELECT occurred_at, event_type, action_taken, substr(raw_text, 1, 60) AS raw
   FROM trade_events
   WHERE user_id = '7441245554' AND trade_id = 2177
   ORDER BY occurred_at;"

# 5. The orders submitted to Hyperliquid for that trade
sqlite3 -header -column data/trades.db \
  "SELECT order_type, side, size, price, status, fill_price
   FROM orders WHERE user_id = '7441245554' AND trade_id = 2177;"
```

This is the answer to "did CP get it wrong, or did our bot do something the
signal didn't say?"

**D10 nuance:** Hyperliquid is the source of truth for *position* state. CP
lifecycle messages reconcile our `orders` table and drive the audit log, but
they can be delayed, missed, or wrong. Before acting on a position (cancel,
close, modify SL), the relevant handlers query HL directly. The audit log
in the DB is still the right place to debug *what the bot did*; HL is the
right place to check *what's actually on the books right now*.

---

## Daily operations

```bash
# Is the bot running?
launchctl print gui/$(id -u)/local.potion-perps-bot | head -20

# Is the forwarder running?
launchctl print gui/$(id -u)/local.potion-perps-forwarder | head -20

# Live application logs (signals, classification, exchange calls)
tail -f logs/bot.log

# Live forwarder logs (Telethon receive + repost)
tail -f logs/forwarder.err logs/forwarder.out

# Early-startup crashes (config error, import error)
tail -f logs/launchd.err

# Manual restart
launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot
launchctl kickstart -k gui/$(id -u)/local.potion-perps-forwarder

# Stop both (won't auto-restart until next install / login)
launchctl bootout gui/$(id -u)/local.potion-perps-bot
launchctl bootout gui/$(id -u)/local.potion-perps-forwarder
```

### Where to look when X breaks

| Symptom | Start here |
|---|---|
| CP signal didn't reach the bot at all | `scripts/telethon_forwarder.py` + `logs/forwarder.err`; check Telethon session validity |
| Bot saw the signal but didn't act on it | `src/parser/classifier.py` → `pipeline.process_message`; check `trade_events` for the message and what was decided |
| `channel_post` updates never arrive in the bot | `src/telegram/bot.py` — `start_polling(allowed_updates=Update.ALL_TYPES)` is load-bearing; Telegram excludes channel_post by default |
| Per-user setting change in Telegram didn't take effect | `Pipeline.refresh_config` + `Orchestrator.refresh_user_config` — should fire on `/config` / `/preset` / `/auto` / `/port` |
| Wrong size on a trade | `trades.decision_snapshot` → `src/strategy/position_sizer.py` |
| Order rejected by Hyperliquid | `src/exchange/order_builder.py` (sizing/rounding) → `position_manager.submit_trade` |
| "Position Closed" notification but HL says open | `src/telegram/handlers/approval.py::confirm_close_pos_callback` + `position_manager.close_position` — HL response is now checked; see Bug #13 fix |
| Cancel handler left a position open | D10 case — `_handle_canceled` in `pipeline.py` now queries HL first; see Bug #11 fix |
| Telegram command misbehaved | `src/telegram/handlers/` (one file per command group) |
| User can't register | `src/telegram/handlers/registration.py` + `src/state/user_db.py` |
| State out of sync after restart | `src/exchange/position_manager.py::sync_positions` |
| Duplicate CP message processed twice | Shouldn't happen — `pipeline.py` dedups on `(trade_id, msg_type)` within 60s; see Bug #14 fix |

---

## Configuration

Two files, both gitignored, both templated:

- **`.env`** — secrets. Hyperliquid keys, Telegram bot token, Telethon API
  credentials, optional admin API key. Template: `.env.example`.
- **`config/config.yaml`** — everything else. Template: `config/config.example.yaml`.

The example YAML is the source of truth for what's available; this section is
the orientation map.

| Block | What it controls |
|---|---|
| `input.adapter` | `simulation` / `cli` / `file` / `discord` / `telegram_channel` (live). Production uses `telegram_channel`. |
| `telegram.signals_channel_id` | The private mirror channel ID (`-1003954991193` in our config) |
| `telegram.bot_token` | Telegram bot token (alternatively `TELEGRAM_BOT_TOKEN` env var) |
| `exchange.network` | `testnet` or `mainnet`. Per-user at registration (D7); this is the default for new users. |
| `strategy.active_preset` | Which preset all users default to. Per-user override in their Telegram config. |
| `strategy_presets.*` | Custom presets (override built-ins by re-using the name) |
| `strategy.size_by_risk` | Override preset `size_pct` per CP risk level (`LOW` / `MEDIUM` / `HIGH`) |
| `strategy.auto_execute` | Default auto-execute for new users (false by default; per-user override) |
| `risk.*` | Pre-trade circuit breakers: max open positions, daily loss, position cap, total exposure cap |
| `risk.mainnet_confirm_above_usd` | Mainnet-only — trades above this size require Telegram approval (Phase 3.5) |
| `risk.mainnet_confirm_timeout_min` | How long the approval prompt is valid before auto-decline |
| `risk.testnet_position_floor_usd` | Testnet-only — bump sub-min sizes up to this value instead of skipping (Phase 4.1) |
| `port.*` | D1 port/wallet architecture: default `port_usd`, mode (`withdraw` / `compound` / `watermark`), watermark |
| `backups.*` | Daily backup schedule + retention |
| `logging.*` | Level + file path + per-library overrides (see `config/config.example.yaml`) |

**Hot-reload:** Per-user config changes via Telegram (`/config`, `/preset`,
`/auto`, `/port`) hot-reload into the running pipeline via
`Orchestrator.refresh_user_config(user_id)`. No restart needed. Credential
changes (network flip, key rotation) still need full
`deactivate_user + activate_user` — that's what `/promote_to_mainnet` does
behind the scenes.

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
| `logs/forwarder.{out,err}*` | Same shape as launchd logs, 2-deep ring (rotated by `forwarder_run.sh`) |
| `backups/trades-*.db` | 30 days, mtime-based prune by the bot |
| `data/trades.db` | Grows with trade events; ~MB-class after a year of active trading |

Manual recipes:

```bash
# Drop all rotated log backups (keeps current files)
rm -f logs/bot.log.[1-9] logs/launchd.{out,err}.1 logs/forwarder.{out,err}.1

# Drop old DB backups beyond retention (the bot does this itself; manual = belt + braces)
find backups -name 'trades-*.db' -mtime +30 -delete

# Shrink the DB after large deletes / closed-trade pruning
# IMPORTANT: stop the bot first — VACUUM acquires an exclusive lock
launchctl bootout gui/$(id -u)/local.potion-perps-bot
sqlite3 data/trades.db 'VACUUM;'
deploy/launchd/install.sh bot                # re-bootstrap

# Delete shadow-mode captures from Phase 1.1 (if you no longer need them)
find signals/captures -name '*.txt' -delete 2>/dev/null || true
```

**Nuclear reset** (lose everything, re-register all users):

```bash
deploy/launchd/uninstall.sh all
rm -rf data/ logs/ backups/
deploy/launchd/install.sh all
# Then /register from each user's Telegram + Telethon re-login on forwarder first run
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
3. Old credentials are overwritten in the `user_credentials` row. Trade history is preserved.

Alternative if `ADMIN_API_KEY` isn't set (no admin REST API access): direct DB
update is fine for testnet. Kickstart the bot afterwards to rebuild the
HyperliquidClient with new creds.

```bash
python3 -c "from src.state.user_db import UserDatabase; db = UserDatabase(); db.update_user_credentials('USER_ID', api_wallet='0x...', api_secret='0x...'); db.close()"
launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot
```

### B. `data/.encryption_key` is lost or rotated

This is the destructive case — losing the master key means every Fernet-encrypted
credential in the DB is now undecryptable. **Trade history is still intact**,
but every user has to re-enter their HL credentials.

1. Stop the bot: `launchctl bootout gui/$(id -u)/local.potion-perps-bot`.
2. Delete the dead key: `rm data/.encryption_key`.
3. Re-start: `deploy/launchd/install.sh bot`. A fresh key is generated automatically on first run.
4. Each user runs `/register` again from Telegram. The flow detects that their
   stored creds are now garbage and prompts for re-entry.
5. Trades opened before the rotation are still in the DB and visible — only
   the credentials needed to operate on the exchange were lost.

### C. Replace a user wholesale

For example, one of the friends drops out and a different one takes their slot.

1. Use the admin REST API to deactivate the old user — `curl -X POST -H "X-API-Key: $K" http://127.0.0.1:8081/api/users/<uid>/deactivate`. This stops dispatching CP signals to them; their trade history is preserved.
2. The new user `/register`s from their own Telegram account.

Never delete a `users` row directly. Deactivation is reversible; deletion
cascades into orphaned `trades` / `orders` / `trade_events` rows that
confuse the audit trail.

---

## Telegram bot

DM-only. Group messages are rejected by the `dm_only_filter` middleware.
Channel posts pass through (the input adapter uses them). Credential-collection
messages are deleted on receipt.

### User commands

| Command | What it does |
|---|---|
| `/start` | First-touch greeting + link to register |
| `/register` | Walk through HL creds + testnet/mainnet choice (multi-step conversation). Mainnet requires typing `MAINNET` to confirm. |
| `/promote_to_mainnet` | Switch an existing testnet user to mainnet (Phase 3.5). Re-validates creds against mainnet before flipping; failure leaves the user on testnet, no DB rollback drama. |
| `/menu` | Dashboard — open trades, recent activity, port status |
| `/help` | Command list |
| `/cancel` | Cancel an in-progress conversation (e.g., abort `/register` mid-flow) |
| `/balance` | Hyperliquid balance (shows both spot USDC and perp account value) |
| `/positions` | Open positions (live from HL, not local cache — D10) |
| `/port` | Port/wallet status: configured `port_usd`, mode (withdraw / compound / watermark), live wallet, guardrail status |
| `/trades` | Open trades (paginated) |
| `/history` | Closed trades (paginated) |
| `/stats` | Win-rate, PnL, average R, by-preset breakdown |
| `/config` | View / edit per-user config (preset, risk limits, max leverage). Hot-reloads. |
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
| `/inject` | Manually inject a signal text (useful for testing — uses the full pipeline; trade IDs 80000-89999 are reserved for inject) |

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
   Look for: any `event_type='error'` rows in `trade_events`, any orders rejected
   by Hyperliquid, any decision_snapshots whose sizing doesn't match what
   the preset says.

2. **Port size dry-run.** With the user's intended mainnet `port_usd`, run
   `/port` on testnet and confirm:
   - `port_usd` ≤ wallet × 0.95 (guardrail warning threshold).
   - Daily-loss circuit breaker (`risk.max_daily_loss_pct`) matches your
     real tolerance.
   - Per-position cap (`risk.max_position_size_usd`) matches your real cap.

3. **Mainnet promotion gate** (Phase 3.5 — **shipped**). On mainnet,
   trades larger than `risk.mainnet_confirm_above_usd` ($100 default) require
   an explicit Telegram approval before submission. The prompt has a
   `mainnet_confirm_timeout_min` minute window (5 default); if you don't
   respond, the trade is auto-declined and recorded as
   `confirmation_timeout`. A background `ConfirmationSweeper` task runs
   every 30s and survives bot restarts (the marker is in the DB, not in
   asyncio state).

4. **Flip network.** From Telegram, run `/promote_to_mainnet`. It
   re-validates the user's API credentials against mainnet before changing
   their network field. If validation fails, the user stays on testnet with
   a clear error. Each user flips independently — there is no global mainnet
   switch.

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

## Moving to a remote server

The laptop deployment is *one* deployment. It is not the canonical one.

### Why you'd move

**Laptop sleep is the silent killer.** `caffeinate -i` prevents idle sleep,
but macOS will still sleep the machine when the lid closes on battery
(clamshell power management). The bot and forwarder processes are paused, and
any CryptoPrinter signal fired during that window is **lost** — there's no
backfill from Telegram, Telethon, or Railway. A VPS doesn't sleep.

**Also worth considering**: even on a VPS, the dispatch path still depends on
Railway being up and Artem's Telegram account being reachable. A VPS removes
*one* failure mode, not all of them.

### Three structural things to fix before exposing to the internet

1. **`:8080` health + `:8081` admin currently bind `0.0.0.0`.** On a public
   VPS, the `X-API-Key` is the entire perimeter. Either bind to `127.0.0.1`
   and put a reverse proxy in front, or add an IP allowlist + TLS.
   (`src/health.py:65`, `src/api/admin.py:83`.)
2. **`backups/` is same-disk.** For real DR you need an offsite copy step.
   Add a cron / systemd timer that `rsync`s `backups/` to another host or
   object store after the bot's daily backup completes.
3. **Replace launchd with systemd.** Two services needed — one for the bot,
   one for the forwarder. The plists are the only macOS-specific things in
   this repo. Everything else is portable Python + stdlib + YAML config +
   env vars.

### Migration checklist

1. **Provision** a Linux VPS. Python 3.10+. Any small instance handles the
   load — both processes do IO-bound work.

2. **Clone and install dependencies**:
   ```bash
   git clone git@github.com:ArtemAbesadze/signals-pipeline.git
   cd signals-pipeline
   git checkout rework/scope-v1
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Copy secrets and config out-of-band** (not via git):
   - `.env`
   - `config/config.yaml`

4. **Preserve credentials and history** (optional but usually wanted):
   - `data/.encryption_key`
   - `data/trades.db`
   - `data/.telethon_session.session` (otherwise the forwarder will prompt for SMS code on first run)

   The encryption key has to come over with the DB — without it, the
   encrypted HL credentials in the DB are unreadable and every user has to
   re-register (see § re-registering, scenario B).

5. **Write two systemd units.** Sketch for the bot
   (`/etc/systemd/system/potion-perps-bot.service`):
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
   And a near-identical one for the forwarder
   (`potion-perps-forwarder.service`), pointing at
   `scripts/telethon_forwarder.py`.

   `systemctl daemon-reload && systemctl enable --now potion-perps-bot potion-perps-forwarder`.

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

## Locked design decisions

Full text in [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md). Short version:

| ID | Decision |
|----|----------|
| **D1** | Port/wallet separation. Sizing uses user-configured `port_usd` with 3 modes (`withdraw` / `compound` / `watermark`). Guardrail: halt if port > wallet, warn if port > wallet × 0.95. Never auto-close positions. |
| **D2** | 7 strategy presets mirror CP's report rows. `even_split` (33/33/34 + BE) is the default. |
| **D3** | Audit trail: `trades.raw_signal_text` + `trades.decision_snapshot` (JSON) + `trade_events` table for every lifecycle event. |
| **D4** | No SaaS layer in Telegram — no invite codes, no subscription expiry, no broadcast. Just registration, encrypted creds, trade views, config, DM-only, per-user kill/resume. |
| **D5** | No Discord message-edit handling. CP sends updates as new messages, never edits. |
| **D6** | Defensive parsing — never crash; route errors to `trade_events` as `event_type='error'`. |
| **D7** | User picks testnet or mainnet at registration. |
| **D8** | Local laptop deployment for now. Server migration deferred. Keep design portable. |
| **D9** | Daily SQLite backups, date-stamped, 30-day prune. |
| **D10** | Hyperliquid is the source of truth for position state. Before decisions that act on a trade (cancel, close, modify SL), query HL directly rather than trusting local `trade.status`. CP lifecycle messages drive the audit trail and reconcile the orders table but they can be missed/mangled/delayed. Forced by the 2026-05-25 NEAR/#2126 silent-fill case. |

---

## Testing — synthetic CP signal driver

A third process (`scripts/test_driver.py`) replaces the real Telegram
forwarder during test runs. It generates synthetic CP-format signals
and drives each through its full lifecycle, posting to the same mirror
channel that the real forwarder uses. Everything downstream — parser,
pipeline, position manager, Hyperliquid — runs unmodified.

**Test trade IDs live in `[7_000_000, 7_999_999]`.** Real CP trade IDs
are 4 digits (max ~3000), so any 7-digit `trade_id` in the DB is
synthetic. This is the primary "obvious test trade" marker; everything
else (cleanup, inspection, filtering) keys off this range.

### Run

```bash
# Copy the template and tweak trade_count / duration / scenarios
cp config/test_driver.example.yaml config/test_driver.yaml

# Dry run — print the plan, post nothing
python3 scripts/test_driver.py --dry-run

# Live run — stops the real forwarder, posts to mirror channel,
# runs all scheduled trades concurrently, exits when done.
python3 scripts/test_driver.py

# Bulk delete every test trade across runs
python3 scripts/test_driver.py --cleanup
```

The driver does NOT restart the real forwarder on exit — restart it
manually with `launchctl kickstart -k gui/$(id -u)/local.potion-perps-forwarder`
when you're done testing.

### Scenarios

Five lifecycle paths, fired uniformly across the configured trade count:

| Scenario | Sequence |
|---|---|
| `all_tp_hit` | signal → trade_live → TP1 → BE → TP2 → ALL_TP |
| `stop_hit` | signal → trade_live → stop |
| `tp1_then_stop` | signal → trade_live → TP1 → BE → stop |
| `cancel_pending` | signal → cancel (entry never filled) |
| `cancel_after_fill` | signal → trade_live → cancel (D10 forced market close) |

### Inspecting test trades while a run is in progress

```bash
# Lifecycle event count per test trade — green means it's progressing
sqlite3 -header -column data/trades.db \
  "SELECT t.trade_id, t.coin, t.side, t.status, t.close_reason,
          COUNT(e.id) AS event_count
   FROM trades t LEFT JOIN trade_events e
     ON t.trade_id = e.trade_id AND t.user_id = e.user_id
   WHERE t.trade_id >= 7000000
   GROUP BY t.trade_id ORDER BY t.trade_id;"

# Most recent events across all test trades
sqlite3 -header -column data/trades.db \
  "SELECT trade_id, event_type, occurred_at, action_taken
   FROM trade_events
   WHERE trade_id >= 7000000
   ORDER BY occurred_at DESC LIMIT 20;"

# Any errors hit during the run
sqlite3 data/trades.db \
  "SELECT trade_id, occurred_at, action_taken FROM trade_events
   WHERE trade_id >= 7000000 AND event_type = 'error';"
```

### What the driver does NOT test

- **Real market behavior** — orders filling at synthetic price levels;
  testnet orderbooks are thin and most TP/SL orders will rest unfilled.
- **Real exchange rejections beyond submit time** — oracle distance on
  closes, slippage, etc. Closes happen because we *send* a cancel /
  TP-hit message, not because the market hit the price.
- **The Telethon forwarder itself** — it's stopped for the test.

These need real CP signals + real markets. The driver tests the bot's
*signal processing*, not the exchange interaction beyond order submit.

---

## Development

```bash
python3 -m pytest tests/                  # full suite, ~5s
python3 -m pytest tests/test_e2e_pipeline.py -v  # one file
python3 -m pytest tests/test_e2e_pipeline.py::TestPipelineDedup -v  # one class
python3 -m pytest tests/test_test_driver.py -v   # test driver tests only

git checkout -b feature/<name>            # branch from rework/scope-v1
```

Project structure: see [`CLAUDE.md`](CLAUDE.md) (kept current, indexed for
future sessions). The rework spine: [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md).

Conventions enforced across the codebase:

- **Audit everything.** Every trade open writes a `decision_snapshot`. Every lifecycle event writes a `trade_events` row.
- **Never crash on bad input.** Parse errors become `trade_events.event_type='error'` rows; the pipeline keeps running.
- **No bot-imposed exit logic.** The active preset is the only authority on exits.
- **Per-user isolation.** Composite PK `(user_id, trade_id)`. All queries filter by user.
- **HL is source of truth for state** (D10). Local DB drives the audit story; HL drives action decisions.
- **Tests close behind code.** 718 tests across `tests/`. New features land with tests, not after.

---

## Phase status

| Phase | What | Status |
|---|---|---|
| 1 | Audit, parsing, sizing, strategy presets, defensive parsing | ✅ shipped |
| 2 | Telegram menu / dashboard / port screen / audit trail | ✅ shipped |
| 3.1 | Daily SQLite backups (D9) | ✅ shipped |
| 3.2 | launchd agent for 24/7 local deployment | ✅ shipped |
| 3.3 | Log rotation polish | ✅ shipped |
| 3.4 | README rewrite | ✅ shipped |
| 3.5 | Mainnet promotion gate (confirmation dialog) | ✅ shipped |
| 4.1 | Telegram channel adapter + Telethon forwarder + D10 + bugs #1, #3, #8, #9, #4, #11, #12, #13, #14, #15 | ✅ shipped |
| 4.2 | Real CP signal soak — observe & fix | ⏳ in progress |
| 4.3 | Synthetic test driver (`scripts/test_driver.py`) — full-lifecycle integration tests via the mirror channel | ✅ shipped |
| 5 | Parking lot — weekly perf report, VPS, CI/CD, backtest tooling | ⏳ later |

Full phase breakdown: [`docs/REWORK_BRIEF.md`](docs/REWORK_BRIEF.md).

# CLAUDE.md

Project orientation for future Claude sessions on `potion-perps-bot` (GitHub: `ArtemAbesadze/signals-pipeline`).

Read this file first. The full rework brief lives at `docs/REWORK_BRIEF.md`.

---

## What this project is (current reframing)

A **private automation tool** for Artem and 2 known friends — three users total. It ingests CryptoPrinter (CP) trade calls from a Discord channel, parses them, and executes the corresponding perpetual futures trades on each user's Hyperliquid account, with monitoring and control via Telegram.

It is **no longer** a marketable SaaS. The previous customer-facing layer (invite codes, subscription expiry, broadcast, paid-tier admin sprawl) is being ripped out. Multi-user support stays because each user has their own Hyperliquid account.

### Design spine

**CryptoPrinter's calls should be the only possible source of fallacy.** The bot itself must be reliable, deterministic, and auditable. When a trade goes wrong, the answer to "did CP get it wrong, or did our bot do something the signal didn't say?" must be retrievable from the database in under 30 seconds.

Every concrete decision in this codebase ties back to that goal: per-trade decision snapshots, lifecycle event logs, defensive parsing that never crashes, no bot-imposed exit logic beyond what the active preset encodes.

---

## Where things live

```
potion-perps-bot/
├── main.py                              # Entry — wires everything, runs the loop
├── CLAUDE.md                            # ← this file
├── README.md                            # Operator's manual (Phase 3.4 rewrite)
├── config/
│   ├── config.example.yaml
│   └── config.yaml                      # active, gitignored
├── deploy/launchd/                      # Two launchd agents (bot + forwarder, Phase 3.2 / 4.1)
├── scripts/
│   └── telethon_forwarder.py            # Phase 4.1 — user-account DM forwarder
├── docs/
│   ├── REWORK_BRIEF.md                  # full rework spec (D1–D9 + phases)
│   ├── telegram-bot-plan.md             # legacy SaaS design — reversed in D4
│   └── telegram-implementation-steps.md # legacy SaaS plan — reversed in D4
├── src/
│   ├── orchestrator.py                  # Multi-user fan-out
│   ├── pipeline.py                      # Per-user signal processor; mainnet gate lives here
│   ├── crypto.py                        # Fernet credential encryption
│   ├── health.py                        # :8080 health endpoint
│   ├── api/admin.py                     # :8081 admin REST API
│   ├── config/settings.py               # YAML + .env loader, typed dataclasses
│   ├── exchange/                        # Hyperliquid SDK wrapper, order builder, position manager
│   ├── input/                           # Adapters: discord, simulation, cli, telegram_channel (Phase 4.1)
│   ├── parser/                          # classifier + signal/update parsers
│   ├── state/                           # SQLite — trades, orders, users, encrypted creds
│   ├── strategy/position_sizer.py       # sizing + pre-trade risk gate
│   ├── telegram/                        # bot + handlers + notifications + monitors + confirmation_sweeper
│   └── utils/                           # structlog setup, symbol mapper
├── tests/                               # 638 tests across 33 files
└── signals/
    ├── samples/                         # Real CP samples for parser tests (Discord format — still valid via forwarder)
    └── test/                            # E2E test fixtures
```

Key files when something breaks:

| Symptom | Start here |
|---------|-----------|
| Bot didn't act on a CP message | `src/parser/classifier.py` → `pipeline.process_message` |
| CP signal didn't reach the bot at all | `scripts/telethon_forwarder.py` (forwarder side) + `src/input/telegram_channel_adapter.py` (bot side); `logs/forwarder.err` + `logs/bot.log` |
| Wrong size on a trade | `src/strategy/position_sizer.py` → `_handle_signal` in `pipeline.py` |
| Order rejected by Hyperliquid | `src/exchange/order_builder.py` (sizing/rounding) → `position_manager.submit_trade` |
| Telegram command misbehaved | `src/telegram/handlers/` (one file per command group) |
| User can't register | `src/telegram/handlers/registration.py` + `src/state/user_db.py` |
| State out of sync after restart | `src/exchange/position_manager.py::sync_positions` |
| `channel_post` events never arrive | `src/telegram/bot.py` `start_polling(allowed_updates=Update.ALL_TYPES)` — default subscription omits channel posts; verified bug-fixed in `381bbc2` |

---

## Conventions

1. **Audit everything.** Every trade open writes a `decision_snapshot` (JSON). Every lifecycle event writes a `trade_events` row with `raw_text` + `action_taken`. If a column would make debugging easier, add it.
2. **Never crash on bad input.** Parse errors are logged + recorded as a `trade_events.event_type='error'` row + skipped. The pipeline keeps running. The defensive boundary is the message handler in `pipeline.py`.
3. **No bot-imposed exit logic.** The active preset is the only authority on when/how positions are exited. No "smart" overrides, no surprise closures.
4. **Encrypted credentials stay encrypted.** Fernet symmetric encryption via `src/crypto.py`. Never downgrade. Master key from `ENCRYPTION_KEY` env or `data/.encryption_key`.
5. **Per-user isolation.** Composite PK `(user_id, trade_id)` on `trades` and `orders`. All queries filter by `user_id`. One user's bug cannot touch another user's data.
6. **DM-only for Telegram.** `dm_only_filter` middleware rejects group messages. Credential-collection messages are deleted on receipt.
7. **No Discord edit handling.** CP sends all updates as new messages, never edits existing ones. `on_message` only — do not add `on_message_edit`.
8. **Tests close behind code.** 638 tests today across `tests/`. New features land with tests, not after.
9. **Branch first during rework.** Active branch: `rework/scope-v1`. No commits to `main` until the rework is feature-complete.
10. **README is current.** Phase 3.4 rewrote it for the private-tool scope. Keep it accurate as the codebase evolves — no longer frozen.

---

## Locked design decisions (summary — full text in `docs/REWORK_BRIEF.md`)

| ID | Decision |
|----|----------|
| **D1** | Port/wallet separation. Sizing uses user-configured `port_usd` with 3 modes (`withdraw` / `compound` / `watermark`). Guardrail: halt if port > wallet, warn if port > wallet × 0.95. Never auto-close positions. |
| **D2** | Rebuild 7 strategy presets to mirror CP's report rows: `tp1_only`, `tp2_only`, `tp3_only`, `tp2_be`, `tp3_be`, `hybrid` (10/70/20+BE), `even_split` (33/33/34+BE, **default**). |
| **D3** | Audit trail: `trades.raw_signal_text` + `trades.decision_snapshot` (JSON) + new `trade_events` table for every lifecycle event. |
| **D4** | Rip the SaaS layer from Telegram (invite codes, expiry, broadcast, admin sprawl). Keep registration, encrypted creds, trade views, config, DM-only, per-user kill/resume. Menu redesign is **interactive** — do not predesign. |
| **D5** | No Discord message-edit handling. |
| **D6** | Defensive parsing — never crash; route errors to `trade_events`. |
| **D7** | User picks testnet or mainnet at registration (current flow stays). |
| **D8** | Local laptop deployment (24/7). Server migration deferred. Keep design portable (env vars, file config). |
| **D9** | Daily SQLite backups to `backups/`, date-stamped, prune > 30 days. |

---

## Current phase

**Phase 4.1 is wired and partially validated.** Branch `rework/scope-v1`,
HEAD is the CLAUDE.md commit immediately following `381bbc2`. The architecture
pivot from direct Discord to a Telegram channel + Telethon forwarder is
complete; end-to-end on a real CP signal is the last validation step before
moving to Phase 4.2.

Shipped (Phase 1–3.5):

| # | Commit | What |
|---|---|---|
| 1.1 | `df5fb90` | Shadow-mode capture (no live trades) |
| 1.2 | `8717f8b` + `e96219d` | Sample corpus expansion, CP format adoption, ORDER_PENDING / TRADE_LIVE |
| 1.3 | `b210903` | Port/wallet architecture (D1) |
| 1.4 | `055029c` | 7 new strategy presets (D2), even_split default |
| 1.5 | `1158a4c` | Audit-log plumbing (D3) — raw_signal_text + decision_snapshot + trade_events |
| 1.6 | `7bbc3a4` | Defensive parsing (D6) — typed errors, never crashes |
| 2.1 | `c366c55` | SaaS layer ripped (D4) |
| 2.2 | `80320b3` + `d772548` | Condensed dashboard, Port screen, Audit Trail |
| 3.1 | `a3770a8` | Daily SQLite backup task (D9) — stdlib only, server-portable, 06:00 UTC default, mtime-based prune, no catch-up on miss |
| 3.2 | `59ccc25` | launchd agent for 24/7 local deployment — `caffeinate -i` blocks idle sleep, `KeepAlive.SuccessfulExit=false` respects clean exits, 2-deep launchd-log ring via `cp + truncate` |
| 3.3 | `b897d8d` | Log rotation polish — launchd files bounded, per-library level overrides for httpx/discord.gateway/etc., sustained-load test for `RotatingFileHandler` |
| 3.4 | `747eaeb` | README rewrite — operator's manual for the private-tool scope; full inventory of what's on disk, DB inspection recipes, cleanup commands, VPS migration playbook |
| 3.5 | `0ee481f` | Mainnet promotion gate — typed `MAINNET` confirmation in `/register` + `/promote_to_mainnet`, big-trade Telegram confirmation dialog ($100 / 5-min defaults), ConfirmationSweeper background task |

Phase 4.1 (this session, 2026-05-24):

| # | Commit | What |
|---|---|---|
| 4.1a | `82be37f` | Markdown specials escape in preset/display names; orchestrator startup failures now log full tracebacks |
| 4.1b | `1d1a196` | Pin `hyperliquid-python-sdk>=0.23.0` (0.22.0's `Info.__init__` blows up on testnet) |
| 4.1c | `60deedc` | `TelegramChannelAdapter` — listens for `channel_post` via the existing bot's Application; dm_only_filter passthrough for channel posts |
| 4.1d | `e68122a` | Telethon forwarder + second launchd agent; install.sh / uninstall.sh accept `bot \| forwarder \| all` modes |
| 4.1e | `381bbc2` | `start_polling(allowed_updates=Update.ALL_TYPES)` — Telegram doesn't push `channel_post` by default; bug discovered during end-to-end testing |

### Phase 4.1 architecture pivot — why we're not on Discord

CP signals reach the laptop via **Telegram, not Discord directly**. Discord
allows one gateway connection per bot token (per shard), and Railway already
runs a Discord listener (`@PotionScannerBot`) for external paying users.
Sharing their token would knock their service offline. We can't add a second
bot to CP's server either — no admin access there. Verified against
Discord's docs: there is no event-mirroring feature; sharding *distributes*
events, doesn't duplicate them. See conversation 2026-05-24 if anyone tries
to re-propose Discord-direct.

The chain we use instead:

```
CP Discord channel
    ↓ (Railway's listener — out of our control)
@PotionScannerBot sends DM to each subscriber
    ↓ (one subscriber = Artem's personal Telegram account)
scripts/telethon_forwarder.py (Telethon as Artem's user account)
    ↓ posts each DM verbatim
"Potion Signals Mirror" private channel (id -1003954991193)
    ↓ (channel_post updates via getUpdates)
Trading bot's TelegramChannelAdapter → pipeline
```

Two processes, two launchd agents:
- `local.potion-perps-bot` → `python3 main.py`
- `local.potion-perps-forwarder` → `python3 scripts/telethon_forwarder.py`

Both managed by `deploy/launchd/install.sh all`. The forwarder's session
file (`data/.telethon_session.session`, 0600, gitignored) is account-level
credentials — treat like a password. Telethon's userbot pattern is tolerated
by Telegram for personal-account automation (no spam, no mass DMs).

Parser side: existing 46 samples + classifier handle the Railway-forwarded
format unchanged. The wrapper noise (`Trade Update:` header, `Source:`,
`Trade Now:`, UTC footer) is benign — we keyword/regex extract, we don't
strict-parse. Verified via `python3 -c "from src.parser.classifier import
classify; ..."` on a real forwarded breakeven message in 2026-05-24
session — `classify` returns `breakeven`, `parse_breakeven` returns the
expected `Breakeven(pair='ETH/USDT', trade_id=2096, tp_secured=2)`.

### Phase 4.1 — where we left off (end of 2026-05-24 session)

**Status: wiring is live; channel-side validated; forwarder→channel waiting on a real CP signal.**

Validated this session:
- ✅ Telethon forwarder signed in as Artem (id `7441245554`), listening for DMs from `@PotionScannerBot` (id `8735069918`), forwarding to channel `-1003954991193`.
- ✅ Main bot running with `adapter=telegram_channel`, 1 active pipeline (swaag). `TelegramChannelAdapter attached: channel_id=-1003954991193` appears in startup logs.
- ✅ Channel → bot link verified: manual test post in "Potion Signals Mirror" produced `Classified message as: noise` in `logs/bot.log`.

Pending verification (Step 2 below):
- ⏳ Real CP signal flowing all the way through `forwarder → channel → bot → pipeline → trade_events`.
- ⏳ Launchd installation of both agents (currently running foreground in two terminals).

### Picking up next session — concrete steps

#### Step 0 — Sanity check

```bash
git branch --show-current                  # rework/scope-v1
git log --oneline -6                       # HEAD = this CLAUDE.md commit, then 381bbc2 / e68122a / 60deedc / 1d1a196 / 82be37f
git status                                 # clean
python3 -m pytest tests/ 2>&1 | tail -2    # 638 passed
```

If any of these are off, stop and investigate before doing anything else.

#### Step 1 — Are the two processes still alive?

```bash
ps aux | grep -E 'main\.py|telethon_forwarder' | grep -v grep
```

The laptop sleep gap is still real and a closed lid likely killed both processes overnight. Expected: zero or two processes. If zero, relaunch:

```bash
# Terminal 1
python3 main.py
# Terminal 2 (in repo root)
python3 scripts/telethon_forwarder.py
```

The forwarder should NOT prompt for a code — the session at `data/.telethon_session.session` is valid from last session. If it does prompt, the session was lost; re-do the interactive sign-in.

#### Step 2 — Check for any CP signals that fired since last session

```bash
sqlite3 -header -column data/trades.db \
  "SELECT occurred_at, event_type, substr(action_taken, 1, 70) AS action
   FROM trade_events ORDER BY id DESC LIMIT 10;"
```

Any rows with `event_type` of `signal_alert`, `tp_hit`, `breakeven`, `stop_hit`, `cancel`, `trade_closed` etc. = Phase 4.1 is fully validated end-to-end. If only the manual `noise` event from last session is there, keep both processes running and wait — or trigger another channel→bot test by posting in the channel manually.

When a real signal arrives, expected log chain:
- `logs/forwarder.err` (or terminal): `Forwarded N-char message to channel -1003954991193`
- `logs/bot.log`: `--- Incoming message (N chars) ---` → `Classified message as: <type>`
- For `signal_alert`: a `trade_events` row with `action_taken='skipped: port not configured'` (because swaag has no port set — see Step 4 notes).

#### Step 3 — Move both processes to launchd

Once Step 2 confirms a real CP signal flowed end-to-end:

```bash
# Stop foreground processes first (Ctrl+C in both terminals).
deploy/launchd/install.sh all
# Verify
launchctl print gui/$(id -u)/local.potion-perps-bot | head -10
launchctl print gui/$(id -u)/local.potion-perps-forwarder | head -10
tail -f logs/bot.log logs/forwarder.err
```

Wait for one more CP signal under launchd-managed processes. If it flows the same way, **Phase 4.1 is shipped**. Update this CLAUDE.md and move on.

#### Step 4 — Begin Phase 4.2 (Artem onboards on testnet)

There's an inactive `Artem A` (`user_id=7441245554`) row in the DB from earlier testing. Clean it up first before re-registering — the user_id collides with Artem's Telegram chat_id and `/register` will refuse to create a new row over an existing one.

```bash
# Confirm there's nothing to lose first
sqlite3 data/trades.db \
  "SELECT (SELECT COUNT(*) FROM trades WHERE user_id='7441245554') AS trades,
          (SELECT COUNT(*) FROM orders WHERE user_id='7441245554') AS orders,
          (SELECT COUNT(*) FROM trade_events WHERE user_id='7441245554') AS events;"
# Should be 0/0/0. If non-zero, stop and re-evaluate before deleting.

# Stop the bot, delete the row, restart
launchctl bootout gui/$(id -u)/local.potion-perps-bot
sqlite3 data/trades.db \
  "DELETE FROM user_credentials WHERE user_id='7441245554';
   DELETE FROM user_config WHERE user_id='7441245554';
   DELETE FROM users WHERE user_id='7441245554';"
deploy/launchd/install.sh bot
```

Then from Artem's Telegram:
1. `/register` → testnet → enter HL testnet credentials (must work on testnet; the SDK bump validates them at registration).
2. `/port` → Set Amount → e.g. `$200` testnet → mode `withdraw`.
3. `/config` → confirm `auto_execute: OFF`, `max_leverage: 20`, `max_position_size_usd: 500`. **Don't change auto_execute** for first signal day.
4. `/menu` → 📡 Calls → stay parked here so Approve/Reject buttons appear inline when signals arrive.
5. Wait for next CP signal. Approve consciously. Watch the orders go out to HL testnet in `logs/bot.log`.
6. Audit the trade end-to-end per the README's recipe (`raw_signal_text`, `decision_snapshot`, `trade_events`, `orders`).

Soak for at least one full CP signal day: multiple opens, at least one TP hit, at least one full close.

#### Operational notes — things that bit us during 4.1

- **swaag (`7375268438`) is a real user (one of the two friends), active in DB, no port set.** Every CP signal flowing through the pipeline produces a `skipped: port not configured` trade_events row AND a Telegram DM to her. Acceptable for testing but noisy. To silence during testing: `curl -X POST -H "X-API-Key: $ADMIN_API_KEY" http://127.0.0.1:8081/api/users/7375268438/deactivate` (reversible — `/activate` puts her back).
- **`config/config.yaml` ended this session with `auto_execute: false`.** Phase 3 had it `true`. Keep it `false` until Phase 4.2 has soaked through a full signal day cleanly.
- **`Update.ALL_TYPES` in `start_polling` is load-bearing.** Telegram's default subscription excludes `channel_post`. Without the explicit opt-in, the channel adapter silently never receives anything. Test: `test_telegram_channel_adapter.py` covers the adapter; no test currently covers the `start_polling` allowed_updates choice (could add one).
- **Don't suggest Discord-direct again.** See "Phase 4.1 architecture pivot" above. The constraint is documented; the workaround is built. If Railway ever loses CP access, we revisit then.
- **Discord adapter is still in the codebase** and tested; we just don't use it as the live source. Keep it — it costs nothing and is the fallback if the Railway/Telethon chain breaks.
- **Mainnet gate defaults: $100 USD threshold, 5-minute timeout.** Both in `config.example.yaml` under `risk.mainnet_confirm_above_usd` / `mainnet_confirm_timeout_min`. The 5-minute timeout is deliberate — perp signals go stale fast, see `feedback_mainnet_confirm_timeout` memory.
- **Confirmation prompts bypass the calls-view gate** (they always push). When friends onboard, make sure their Telegram notifications are on for the bot or they'll miss approval prompts on big trades.
- **`/promote_to_mainnet` re-validates credentials against mainnet before flipping anything.** If the API key is testnet-only, validation fails and the user stays on testnet. No DB rollback drama. (`src/telegram/handlers/promotion.py`.)
- **The ConfirmationSweeper runs every 30 seconds** (`src/telegram/confirmation_sweeper.py`). Pending confirmations older than `mainnet_confirm_timeout_min` minutes get auto-declined with a `confirmation_timeout` close reason + `CONFIRMATION_TIMEOUT` event row. State survives bot restarts because the marker is in the DB (`trades.requires_confirmation`), not in an asyncio task.
- **Backup-loop "skip on miss" was a deliberate 3.1 choice** (over a catch-up-on-wake policy). Don't re-litigate without a reason. If catch-up is added later, it needs a marker file for "last successful backup."
- **`cp + truncate` not `mv` in `deploy/launchd/run.sh` and `forwarder_run.sh`.** launchd opens the stdout/stderr files before exec'ing the wrapper — renaming would orphan the open fd onto the renamed inode and the new process's output would land in `.1`, not the fresh file. Two dedicated lint tests in `test_launchd_deploy.py` enforce this for both wrappers.
- **Laptop-sleep gap is still real.** `caffeinate -i` blocks idle sleep, but lid-closed-on-battery still sleeps the machine. Signals fired during sleep are *lost* on both ends — no backfill from Telethon, no backfill from the bot. Mitigation: stay plugged in + lid open, or move to a VPS (Phase 5.2). This is now also Railway-bot-dependent, so Railway sleep/outage = us not getting signals either.

#### Structural assumptions still on the parking lot for Phase 5 / VPS

- `:8080` health + `:8081` admin bind to `0.0.0.0` (`src/health.py:65`, `src/api/admin.py:83`). Harmless on a laptop behind NAT; on a public VPS the `X-API-Key` is the entire perimeter — bind `127.0.0.1` + reverse proxy or add IP allowlist + TLS first.
- `backups/` lands on the same disk as the DB (`src/state/backup.py`). For real DR, add an offsite `rsync`/`scp` step. Documented in `config/config.example.yaml` and the README's VPS migration section.
- The `launchd` plist is the only macOS-specific artefact. Replacement systemd unit sketch lives in `README.md` § "Moving to a remote server."

Phase 5 = parking lot (weekly performance report, VPS, CI/CD, backtest tooling). Full breakdown: `docs/REWORK_BRIEF.md`.

## Tests

**638/638 passing** as of `381bbc2` (up from 594 at end of Phase 3.5; +44 tests in Phase 4.1 across the channel adapter, the Telethon forwarder, the second launchd agent, and the channel-post passthrough in dm_only_filter).

---

## Quick reference

| Need | Command |
|------|---------|
| Install both launchd agents | `deploy/launchd/install.sh all` |
| Install just one | `deploy/launchd/install.sh bot` / `... forwarder` |
| Bot status | `launchctl print gui/$(id -u)/local.potion-perps-bot \| head -20` |
| Forwarder status | `launchctl print gui/$(id -u)/local.potion-perps-forwarder \| head -20` |
| Restart bot | `launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot` |
| Restart forwarder | `launchctl kickstart -k gui/$(id -u)/local.potion-perps-forwarder` |
| Stop bot | `launchctl bootout gui/$(id -u)/local.potion-perps-bot` |
| Foreground run (testing) | `python3 main.py` + `python3 scripts/telethon_forwarder.py` |
| Tests | `python3 -m pytest tests/ -v` |
| New branch | `git checkout -b <name>` from `rework/scope-v1` |
| Env vars | `.env` (gitignored); template in `.env.example` |
| Active config | `config/config.yaml` (gitignored); template in `config/config.example.yaml` |
| App logs | `logs/bot.log` (structlog JSON, rotating 10 MB × 5) |
| Forwarder logs | `logs/forwarder.err` (launchd-rotated, 2-deep ring) |
| Pre-structlog crashes | `logs/launchd.err` (2-deep ring, rotated on each restart) |
| DB | `data/trades.db` (SQLite, WAL mode) |
| Encryption key | `data/.encryption_key` (auto-generated if missing) |
| Telethon session | `data/.telethon_session.session` (0600, .gitignored; full account credentials) |
| Backups | `backups/trades-YYYYMMDD-HHMMSS.db` (30-day retention, daily 06:00 UTC) |
| Signals channel id | `-1003954991193` (in `config/config.yaml` as `telegram.signals_channel_id`) |
| Source bot | `@PotionScannerBot` (id `8735069918`, in `.env` as `TG_SOURCE_BOT`) |

---

## Working style (for future Claude sessions)

- **Branch first, commit small, commit often.** Active rework branch is `rework/scope-v1`. No commits to `main` during rework.
- **Propose plans before writing code.** Files touched, design choices, tests added, LOC estimate. Wait for explicit approval.
- **Be opinionated; push back.** If something violates auditability, introduces surprise behavior, or risks crashes, say so directly.
- **Ask when a tradeoff is genuinely ambiguous.** One question now beats untangling a wrong assumption later.
- **Flag issues seen during reading.** Even out-of-scope contradictions to the design goals get surfaced — document, don't fix unilaterally.
- **Phase 4 is operational, not architectural.** Less code, more careful flipping of real-world switches. When proposing work, prefer "smallest change that lets us observe and learn" over "comprehensive instrumentation up front."

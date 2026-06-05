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
│   ├── telethon_forwarder.py            # Phase 4.1 — user-account DM forwarder
│   └── test_driver.py                   # Phase 4.3 — synthetic CP signal driver (test trade IDs 7_000_000+)
├── docs/
│   ├── REWORK_BRIEF.md                  # master scope + phases + handoff (D1-D11)
│   ├── HYPERLIQUID_INTEGRATION.md       # what we do on HL today (15 sections + bugs)
│   ├── BLOFIN_INTEGRATION.md            # what we'll do on Blofin (Phase 6 spec + demo)
│   └── archive/                         # pre-rework SaaS docs (reversed in D4)
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
├── tests/                               # 916 tests across 38 files
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
8. **Tests close behind code.** 916 tests today across `tests/`. New features land with tests, not after.
9. **Branch first during rework.** Active branch: `rework/scope-v1`. No commits to `main` until the rework is feature-complete.
10. **README is current.** Phase 3.4 rewrote it for the private-tool scope. Keep it accurate as the codebase evolves — no longer frozen.
11. **Test trade IDs live in `[7_000_000, 7_999_999]`.** Real CP IDs are 4 digits (max ~3000), so any 7-digit `trade_id` in `trades` / `orders` / `trade_events` is synthetic from `scripts/test_driver.py`. Bulk-delete with `python3 scripts/test_driver.py --cleanup`. Don't intermix this range with real CP trade IDs anywhere.

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
| **D10** | Hyperliquid is the source of truth for position state. Before decisions that act on a trade (cancel, close, modify SL), query HL directly rather than trusting cached local `trade.status`. CP lifecycle messages drive the audit trail and reconcile the orders table (Bug #3 fix), but they can be missed/mangled/delayed — HL is the authoritative state for what's actually on the books. Forced by 2026-05-25 NEAR/#2126 — silent entry fill, no `TRADE_LIVE` from CP, cancel handler left an open position because it trusted local `status=PENDING`. |
| **D11** | Exchange is the source of truth for recorded **and displayed** data — extends D10 from decisions to data. Persisted values (fill prices, size, realized PnL, order status) and Telegram-shown values (balance, positions, PnL) are read back from the exchange, not inferred from CP text. CP is trigger + audit only. **Binding for Blofin** (and any future adapter): on each CP-triggered event, re-read the trade's real state from Blofin (`trade-history`, `positions`) and persist real values; fall back to CP only on query failure and mark the row approximate — never silently mix. The HL path only partially holds this (fills/PnL are CP-derived approximations — HL lacks the endpoints); not retrofitted. Full text + rationale in `docs/REWORK_BRIEF.md`. |

---

## Current phase

**Phase 6 (Blofin migration) is well underway. Every part that can be built
offline / credential-free has shipped: the full Blofin client + order builder
+ position manager, the exchange-adapter dispatch wiring (a Blofin user now
trades end-to-end through their own stack), the `/register` exchange choice,
and the exchange-aware test driver. What remains (6.11 soak, 6.8b D11 fill
read-back, 6.12 per-user migration) is operational — it needs the live Blofin
demo and the agents running. Phase 4.2 (real CP soak) is paused; both agents
are stopped.**

Where we are today (2026-06-05):

- **Branch**: `rework/scope-v1`. HEAD on GitHub matches local (`6c0a145`).
- **Tests**: 916/916 across 38 files.
- **Live state**: both launchd agents **STOPPED**. Nothing is consuming CP
  signals right now. Restart with `deploy/launchd/install.sh all` when ready.
- **DB migrated for multi-exchange** (Phase 6.7): `orders.oid` INTEGER→TEXT,
  `user_credentials` +`exchange`/`passphrase_enc`. Both existing users read as
  `exchange=hyperliquid`. Pre-migration snapshot at
  `data/trades.db.pre-blofin-migration-20260604-212526`.
- **Demo validated 2026-06-04** — `scripts/blofin_demo_check.py` against the
  Blofin demo host confirmed signing (base64-of-hex, no 60009) + every
  endpoint; corrected several wrong endpoint guesses. Authoritative endpoint
  map is `docs/BLOFIN_INTEGRATION.md` § 0. Demo needs its OWN key (production
  keys → `152401`); demo is pre-funded ~500k USDT, cross + net_mode.

Phase 6 commits (this migration, HL untouched — both exchanges coexist):

| # | Commit | What |
|---|---|---|
| 6.7 | `7cd95c0` | Schema + config: `orders.oid`→TEXT, `user_credentials` +exchange/passphrase, `ExchangeConfig` carries them (D11-aligned). |
| 6.6 | `9bdd6fa` | `potion_to_blofin` symbol mapper (BASE-QUOTE instId; MATIC→POL, RNDR→RENDER, FTM→S; no kilo prefix). |
| 6.3 | `1affd05` (+ later) | `src/exchange/blofin.py` — hand-rolled `BlofinClient`: signing, reads (balance/positions/orders/mids/asset_meta/fills/tpsl), writes (place/cancel/set_leverage/close/tpsl). |
| 6.4 | — | `src/exchange/blofin_order_builder.py` — contract-value sizing, lotSize/tickSize rounding, native TP/SL (Bug #18 class obviated). |
| 6.5 | `ba7236f` | `src/exchange/blofin_position_manager.py` — mirrors PositionManager's contract; 3-source sync; cancel routes order vs tpsl (102068 benign). |
| 6.8 | `5deed0d` | **Dispatch wiring** — `src/exchange/adapter.py` (ExchangeAdapter); Pipeline builds its adapter from `config.exchange.exchange`; `build_exchange_client` Blofin branch live; orchestrator sync/kill exchange-aware. HL byte-identical. |
| 6.9 | `bd9643f` | `/register` exchange choice — HL vs Blofin credential sub-flows, Demo/Live network labels, per-exchange validation, persists exchange+passphrase. |
| 6.10 | `6c0a145` | Test-driver exchange dispatch — Blofin coin pool/prices from public market endpoints; per-user orphan-cancel routing; `--top-up-demo` (experimental). |

- **Bug #20 fixed** (`73b20e4`): resting-entry trades now promote PENDING→OPEN.
- **Two source-of-truth docs** for the migration:
  - [`docs/HYPERLIQUID_INTEGRATION.md`](docs/HYPERLIQUID_INTEGRATION.md) —
    full audit of every HL touchpoint (15 capability sections + every bug).
  - [`docs/BLOFIN_INTEGRATION.md`](docs/BLOFIN_INTEGRATION.md) — mirror
    structure for Blofin; § 0 is the demo-confirmed endpoint map.

**What's left in Phase 6 (all operational — needs the live demo + agents):**

- **6.11 — demo soak.** Register a Blofin **demo** user via the new
  `/register`, set `config/test_driver.yaml` `exchange: blofin`, run the
  driver's 5 scenarios, audit per the D11 checklist. This soak is also where
  two open shapes get confirmed empirically: the `demo-apply-money` body
  (6.10's `--top-up-demo`) and the triggered-TP/SL fill shape (needed for
  6.8b).
- **6.8b — D11 fill read-back.** Replace the CP target-price approximation in
  `Pipeline._mark_order_filled` with real `fills-history` reads for Blofin
  (entry matches by orderId; a triggered TP/SL conditional executes under a
  NEW execution orderId ≠ stored tpslId — design that matching against a real
  captured demo fill, don't guess). HL keeps the approximation (no fills
  endpoint). The adapter is the seam: add `read_fill_price` to it.
- **6.12 — per-user migration.** One user at a time; keep HL as fallback.

Shipped (Phase 1–4.3):

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
| 4.3a | `05ea9c1` | Synthetic test driver — replaces real forwarder, generates CP-format signals for all 5 lifecycle scenarios; test trade IDs in [7_000_000, 7_999_999] |
| 4.3b | `37a0454` | Test driver posts via Telethon (bots don't see their own channel posts) |
| 4.3c | `e351b6f` | **Bug #18 fix** — `_round_price` ≤6 decimals AND ≤5 sig figs (kBONK/kSHIB/kPEPE class) |
| 4.3d | `07d3ae1` | Realistic test driver percentages (price × leverage, not random) |
| 4.3e | `0d49f8e` | `--cleanup` cancels orphan HL orders alongside DB delete |
| 4.3f | `7807123` | **Bug #19 fix** — /positions slash command keyboard parity with menu path |

Other Bug fixes across Phase 4.x:

| Bug | Commit | What |
|---|---|---|
| #13 | `ca072ee` + `3160bf6` | Verify HL fill before claiming close succeeded; surface HL error verbatim |
| #14 | `9db341c` | Pipeline-level dedup of @PotionScannerBot's two-variant delivery (60s window on (trade_id, msg_type)) |
| #15 | `7ea9aa3` | DM text handlers no-op on channel_post (filters.ChatType.PRIVATE + defense-in-depth guards) |
| #16 | `53d7210` | Escape Markdown specials in `action_taken` rendering — broke /menu when dedup audit rows contained `(trade_closed)` |
| #17 | `37a0454` | Test driver Telethon posting (see 4.3b above) |
| #18 | `e351b6f` | Decimal cap in _round_price (see 4.3c above) |
| #19 | `7807123` | /positions slash command close buttons (see 4.3f above) |
| #20 | `73b20e4` | **Resting-entry trades stuck PENDING.** No live handler promoted a trade PENDING→OPEN — only submit-time immediate fill or startup `sync_positions` did. A resting limit entry that filled later (TRADE_LIVE from CP, not an immediate fill) stayed PENDING, so every handler gated on `status==OPEN` silently no-opped — notably the breakeven-after-TP1 SL move. Real case: ADA #2184 (2026-06-01 soak) hit TP1 but its SL was never moved to entry on HL. Fix: `_promote_to_open_if_filled` (D10 — confirms position on HL, falls back to trusting CP's explicit fill on query failure) called from `_handle_trade_live` and `_handle_tp_hit`. |

Phase 4.1 — wiring (sessions 2026-05-24 → 2026-05-25):

| # | Commit | What |
|---|---|---|
| 4.1a | `82be37f` | Markdown specials escape in preset/display names; orchestrator startup failures now log full tracebacks |
| 4.1b | `1d1a196` | Pin `hyperliquid-python-sdk>=0.23.0` (0.22.0's `Info.__init__` blows up on testnet) |
| 4.1c | `60deedc` | `TelegramChannelAdapter` — listens for `channel_post` via the existing bot's Application; dm_only_filter passthrough for channel posts |
| 4.1d | `e68122a` | Telethon forwarder + second launchd agent; install.sh / uninstall.sh accept `bot \| forwarder \| all` modes |
| 4.1e | `381bbc2` | `start_polling(allowed_updates=Update.ALL_TYPES)` — Telegram doesn't push `channel_post` by default; bug discovered during end-to-end testing |
| 4.1f | `18d475e` | Hot-reload pipeline Config after Telegram setting edits — without this, `update_user_config(...)` updated the DB but the running pipeline kept its stale cached Config (`Pipeline.refresh_config` + `Orchestrator.refresh_user_config` + handler hooks) |
| 4.1g | `d4d427e` + `016d57e` | Testnet position floor — on testnet, sub-min calculated sizes bump UP to `RiskConfig.testnet_position_floor_usd` (default $15) instead of skipping. Lets you exercise the full pipeline on testnet without funding the wallet to the level size-by-risk math demands. Broadened in `016d57e` to cover the gap where calc clears HL min but loses notional to `szDecimals` flooring downstream (MEDIUM at $500 port → $10 → $9.25 BTC notional → rejected). Mainnet path unchanged — still raises. |

Phase 4.1 — first real CP signal post-mortem & fixes (session 2026-05-29):

The first night of real signals (NEAR #2126 + IMX #2127 on 2026-05-25)
exposed six bugs. All fixed; tests added for each.

| # | Bug | Commit | Fix summary |
|---|---|---|---|
| 1 | Forwarder didn't preserve message order — Telethon dispatches each `events.NewMessage` handler as its own asyncio task, concurrent `send_message` calls raced. IMX/#2127 showed BREAKEVEN posted before TP1_HIT in our channel even though CP sent in order. | `b45c068` | `asyncio.Lock` around the send call serialises forwarding in receive order. Extracted to testable `_forward_one`; new test specifically asserts no overlapping sends + correct ordering on concurrent submissions. |
| 3 + 8 | Local `orders` table stayed at `status='submitted'` after exchange-side fills. IMX TP1 + TP2 hit per CP, but our DB still said both resting. Downstream: `/trades` vs `/positions` disagreed, PnL math wrong, "pending entry" UI for filled trades. | `12354c1` | Pipeline lifecycle handlers reconcile the orders table: `_handle_trade_live`→entry FILLED, `_handle_tp_hit`→TP{n} FILLED, `_handle_all_tp_hit`→all TPs FILLED + SL CANCELED, `_handle_stop_hit`→SL FILLED + TPs CANCELED. Uses target price as approx fill_price (CP doesn't expose exact fills); idempotent against the @PotionScannerBot duplicate-message pattern. |
| 9 | Trading menu showed perp `account_value` ($1.49) as "Balance" while ~$649 sat in spot USDC under HL portfolio margin. | `6c0e661` | Show both: `💵 USDC: $649.00` + `📊 Perp account value: $1.49`. |
| 4 | `parse_canceled` slurped @PotionScannerBot wrapper noise into the reason — "Source: Potion #Perp Bot Calls TRADE CANCELED TRAD..." instead of the actual reason text. | `66b9f84` | `_extract_cancel_reason` anchors on PAIR/`#NNN` line, walks forward dropping known wrapper-header/footer patterns. Parenthesized reason still wins when CP uses that format. |
| 11 | **D10 case**: cancel handler used local `trade.status` to decide cancel-orders vs market-close. NEAR's entry filled silently on HL (no `TRADE_LIVE` from CP); status stayed PENDING; cancel handler only canceled the resting orders; position stayed open uncovered. Discovered by checking HL directly two days later. | `3ca1402` | **D10 codified** in CLAUDE.md and REWORK_BRIEF.md: HL is source of truth for position state. `_handle_canceled` now queries `get_open_positions()` and routes to `close_position` when HL has a position, falls back to local status if HL query itself fails. |
| 12 | HL rejected our 10% IOC close-spread limit prices with "Price too far from oracle". Even HL's own UI hit the same when the user tried to close NEAR — testnet oracle drift + thin orderbook. | `bc80fbd` | `CLOSE_LIMIT_SPREAD_PCT = 3.0` constant used in both `PositionManager.close_position` and the `confirm_close_pos_callback` raw-position close path. Sentinel test prevents quiet re-broadening. |

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

### Picking up — first 10 minutes for a new session

```bash
cd ~/ClaudeProjects/potion-perps-bot

# 1. Right branch
git branch --show-current             # rework/scope-v1
git log --oneline -5

# 2. Tree clean
git status

# 3. Tests pass
python3 -m pytest tests/ -q | tail -2  # expect 916 passed

# 4. Both launchd agents up
launchctl print gui/$(id -u)/local.potion-perps-bot 2>&1 | grep state
launchctl print gui/$(id -u)/local.potion-perps-forwarder 2>&1 | grep state

# 5. Any real CP trades fire while you were away?
sqlite3 -header -column data/trades.db \
  "SELECT trade_id, coin, side, status, close_reason, pnl_pct, created_at
   FROM trades WHERE user_id='7441245554' AND trade_id < 80000
   ORDER BY trade_id DESC LIMIT 5;"

# 6. Any test trades in the DB?
sqlite3 data/trades.db \
  "SELECT COUNT(*) FROM trades WHERE trade_id BETWEEN 7000000 AND 7999999;"
# 0 = clean. Non-zero = `python3 scripts/test_driver.py --cleanup`.
```

If any of these are off, stop and investigate.

If forwarder is in a respawn loop, it probably can't open
`data/.telethon_session.session` — re-run interactively
(`python3 scripts/telethon_forwarder.py`) to refresh the session, then
`deploy/launchd/install.sh forwarder`.

### Where to go next — two parallel tracks

#### Track A: Phase 4.2 soak (passive, observation)

Watch real CP signals flow through and audit closed trades with the
README's "Inspecting a single trade end-to-end" recipe. We have one
confirmed real CP signal through the new pipeline (#2177 ETH breakeven
on 2026-05-29). Phase 4.2 closes when we've seen a full day's worth
of real signals process cleanly.

#### Track B: Phase 6 — Blofin migration (active, big effort)

**This is the main upcoming work.** Why we're migrating:

- HL doesn't offer the leverage CP signals routinely call for.
- CP's signal calibration assumes Blofin as the trading terminal.
- Several HL gotchas (Bug #9 portfolio margin, Bug #12 oracle distance,
  Bug #18 decimal cap) are solved by Blofin's API design (published
  per-asset metadata, dedicated close-positions and tpsl-order endpoints).

Two source-of-truth docs ready:

- [`docs/HYPERLIQUID_INTEGRATION.md`](docs/HYPERLIQUID_INTEGRATION.md)
  — every HL touchpoint, 15 capability sections + every bug.
- [`docs/BLOFIN_INTEGRATION.md`](docs/BLOFIN_INTEGRATION.md) — mirror
  structure for Blofin; includes a dedicated Demo Trading section we
  use instead of HL testnet.

Steps 1–10 of the original implementation order (apply for key → demo
validate → client → order builder → position manager → symbol mapper →
schema → /register → test driver) are **all done** — see the Phase 6
commit table under "Current phase". What remains is operational:

- **6.11 — demo soak** (next). Register a Blofin demo user, point the test
  driver at `exchange: blofin`, run the 5 scenarios, audit per D11. Confirms
  the `demo-apply-money` body shape + the triggered-fill shape for 6.8b.
- **6.8b — D11 fill read-back** for Blofin (real `fills-history`, not CP
  target prices). Needs a real captured triggered-conditional fill from the
  soak — don't guess the TP/SL matching.
- **6.12 — per-user migration**, one at a time, HL stays as fallback.

See `REWORK_BRIEF.md` § Phase 6 for the full row-by-row table.

**HL code stays in the repo permanently** as the fallback adapter.

### Operational notes — what we learned

- **swaag (`7375268438`) is a real user on a SEPARATE testnet account** (master `0x8fd9888fB9ad93A968aB9C2e4eA12036C286BC98`). Not just a test fixture. She's active, has credentials, no port set. Every CP signal flowing through the pipeline currently produces a `skipped: port not configured` event + Telegram DM to her. Either ask her to set a port, or temporarily deactivate via admin API if she'd be annoyed.
- **Artem's `auto_execute=ON` is a deliberate choice for 4.2 testnet testing.** CLAUDE.md historically recommended OFF for first signal day; user overrode. On testnet this is fine (no real-money risk); on mainnet the gate at $100 would catch big trades anyway. **Don't quietly toggle back to OFF without asking.**
- **`config/config.yaml` has `adapter: telegram_channel` and `auto_execute: false`.** The YAML `auto_execute: false` is the default for new users; Artem's per-user override (auto_execute=true in DB) wins for his pipeline.
- **Per-user Config changes via Telegram hot-reload now** (commit `18d475e`). The DB is the source of truth; `Orchestrator.refresh_user_config(user_id)` reloads from DB after any `/config` / `/preset` / `/auto` / `/port` edit. Credential changes still need full `deactivate + activate` — that's what `/promote_to_mainnet` does.
- **Updating credentials when you don't have ADMIN_API_KEY set:** direct DB call works fine (see 2026-05-25 session, where the API wallet rotated). `python3 -c "from src.state.user_db import UserDatabase; db = UserDatabase(); db.update_user_credentials('USER_ID', api_wallet='0x...', api_secret='0x...'); db.close()"`. After update, `launchctl kickstart -k gui/$(id -u)/local.potion-perps-bot` to rebuild the HyperliquidClient with new creds.
- **The testnet position floor (default $15) is in `RiskConfig` as a dataclass field, not in the `user_config` SQL table.** All users get the dataclass default. To change per-user, would need a schema migration. For now: edit the default in `src/config/settings.py` if you ever need to.
- **HL testnet API wallet authorization is separate from creation.** Generating an API key locally is one operation; getting the master account to authorize it on HL testnet is another, done via https://app.hyperliquid-testnet.xyz/. The `/register` flow only validates the master account exists (`get_account_state`, a read op); the API wallet's authorization gets discovered at first-trade time. See polish item above.
- **`Update.ALL_TYPES` in `start_polling` is load-bearing.** Telegram's default subscription excludes `channel_post`. Without the explicit opt-in, the channel adapter silently never receives anything (no error, no log line). If channel adapter ever stops working: this is the first thing to check.
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

**916/916 passing** across 38 files. Phase 6 added `test_blofin_client.py`,
`test_blofin_order_builder.py`, `test_blofin_position_manager.py`,
`test_exchange_adapter.py`, and `test_registration_exchange.py`; the test
driver + e2e suites grew exchange-dispatch coverage. Recent additions worth
knowing about:

- `tests/test_e2e_pipeline.py::TestPendingToOpenPromotion` (5 tests, Bug #20) — TRADE_LIVE/TP_HIT promote a resting-entry trade PENDING→OPEN (D10: HL-confirmed, falls back to CP fill on query failure); the core regression asserts TP1 on a still-PENDING trade fires the breakeven SL move.
- `tests/test_test_driver.py` (41 tests) — scenarios, template fidelity (each event round-trips through classify+parse), scheduling determinism, cleanup, realistic-percentage math, orphan-order helper.
- `tests/test_positions_keyboard.py` (9 tests, Bug #19) — locks the slash-command ↔ menu-path keyboard parity contract via static import check + behavioural assertions.
- `tests/test_text_handler_guards.py` (4 tests, Bug #15) — DM text handlers no-op cleanly on channel_post.
- `TestPricePrecisionForTightTickCoins` in `test_e2e_pipeline.py` (23 tests, Bug #18) — both `_round_price` copies agree, sig-figs + decimal cap enforced for every low-priced coin in the pool.
- `TestPipelineDedup` (5 tests, Bug #14) — same `(trade_id, msg_type)` within 60s gets suppressed; different keys pass; window expiry; cache pruning.
- `TestClosePositionResponseHandling` (4 tests, Bug #13) — close honestly reports filled / error / no-fill IOC.
- Markdown-escape regression test in `test_formatters.py` (Bug #16).

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
| Synthetic test driver | `python3 scripts/test_driver.py [--dry-run \| --cleanup \| --top-up-demo]` (replaces forwarder; exchange-aware since 6.10) |
| Test trade ID range | `[7_000_000, 7_999_999]` — bulk inspect: `WHERE trade_id >= 7000000` |
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

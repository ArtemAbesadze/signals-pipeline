# Rework Brief

The master spec for the rework branch `rework/scope-v1` (cut from `main` at
commit `170abbd` on 2026-05-19). This document is the **source of truth** for
project scope, design decisions, phase status, and handoff between sessions
or collaborators.

**Read this first.** Then [`../CLAUDE.md`](../CLAUDE.md) for codebase
orientation, [`HYPERLIQUID_INTEGRATION.md`](HYPERLIQUID_INTEGRATION.md) for the
exchange we use today, and [`BLOFIN_INTEGRATION.md`](BLOFIN_INTEGRATION.md)
for the exchange we're migrating to in Phase 6.

---

## Project reframing

**What this project was**: a marketable SaaS for automating CryptoPrinter (CP)
trade calls on Hyperliquid perps. Built with invite codes, subscription
expiry, admin broadcast, billing-shaped UX — designed for selling access.

**What it now is**: a private automation tool for Artem and 2 friends. Three
users total, all known to each other, no paying customers, no subscriptions.
Multi-user support stays because each user has their own exchange account,
but the customer-facing SaaS layer is gone (ripped in commit `c366c55` —
Phase 2.1).

**Core purpose**: faithfully automate CryptoPrinter's Discord bot calls
(entry + full lifecycle: TP hits, breakeven moves, stop hits, cancels,
manual SL/TP amendments) onto each user's exchange account, with monitoring
and control via Telegram.

**Underlying design goal — the spine of every decision**:

> CryptoPrinter calls should be the only possible source of fallacy.
> The bot itself must be reliable, deterministic, and auditable. If a trade
> goes wrong, I should be able to answer in 30 seconds:
>
> - What was the original signal text?
> - What did our parser extract?
> - What strategy + port + risk-level decided the size?
> - What orders did we place?
> - What lifecycle events happened?
> - Was the loss caused by CP being wrong, or by our bot doing something
>   the signal didn't say?

---

## Current state of the codebase (as of HEAD on `rework/scope-v1`)

| Field | Value |
|---|---|
| Branch | `rework/scope-v1` (default branch on GitHub) |
| Test count | **808/808 passing** across 35 test files |
| LOC | ~19,000 across `src/` + `scripts/` + `tests/` |
| Users | Artem (Telegram `7441245554`, testnet, $500 port, auto_execute=ON) + swaag (Telegram `7375268438`, testnet, no port set) — third slot empty |
| Exchange | Hyperliquid testnet (everyone). Mainnet promotion is gated by Phase 3.5 (typed `MAINNET` confirm + per-trade approval dialog). |
| Signal source | CP Discord channel → Railway's `@PotionScannerBot` DMs → Artem's TG account → Telethon forwarder → private "Potion Signals Mirror" channel → bot's `TelegramChannelAdapter` |
| Deployment | macOS `launchd` on Artem's laptop, 24/7. Two agents (bot + forwarder), both wrapped in `caffeinate -i`. |
| Database | SQLite at `data/trades.db`, WAL mode. 6 tables: `users`, `user_credentials`, `user_config`, `telegram_admins`, `trades`, `orders`, `trade_events`. |
| Encryption | Fernet symmetric, master key at `data/.encryption_key`. All HL credentials encrypted at rest. |

**Live system state right now**: both launchd agents running, HL clean (0
positions / 0 orders), no test data polluting the DB (last `--cleanup` ran
2026-06-04), bot has received and processed real CP signals during the
ongoing Phase 4.2 soak.

---

## Locked design decisions (D1–D11)

Treat these as constraints, not suggestions. Order is historical; relevance
is unchanged.

### D1. Port/wallet separation

Position sizing uses a user-configured "port" — a subset of the exchange
wallet — not the full wallet balance.

- New columns: `user_config.port_usd` (REAL), `user_config.port_mode` (TEXT)
- Three port modes (user picks in Telegram):
  - **`withdraw`** — profits stay in wallet; port stays at the configured value.
  - **`compound`** — port = port + cumulative P&L (both directions).
  - **`watermark`** — profits push the port up and the new value becomes
    the new floor; losses can pull port down but never below the highest
    floor ever reached.
- Guardrails:
  - If `port > wallet`: halt new trades immediately, notify the user.
  - If `port > wallet × 0.95`: warn but continue.
  - **Open positions are never auto-closed.** The bot does not impose its
    own exit logic — the active preset is the only authority.

### D2. Strategy presets mirror CP's report rows

Seven presets, one per row of CP's weekly performance report (so our weekly
report lines up directly with CP's):

| Name | tp_split | SL → BE | CP report row |
|---|---|---|---|
| `tp1_only` | 100/0/0 | never | TP1 Only (Safe Way) |
| `tp2_only` | 0/100/0 | never | TP2 Only |
| `tp3_only` | 0/0/100 | never | TP3 Only |
| `tp2_be` | 0/100/0 | after TP1 | TP2 + BE |
| `tp3_be` | 0/0/100 | after TP1 | TP3 + BE |
| `hybrid` | 10/70/20 | after TP1 | Hybrid 10/70/20 |
| **`even_split`** | 33/33/34 | after TP1 | (default for new users) |

### D3. Audit trail (everything reconstructable)

- `trades.raw_signal_text` — verbatim message that opened the trade.
- `trades.decision_snapshot` — JSON captured at open with preset, sizing,
  port state, risk gate verdict, leverage caps applied.
- `trade_events` table — append-only log of every lifecycle event with
  `raw_text` + `action_taken`. Dedup-suppressed messages also land here so
  post-mortems can find what was caught.

### D4. No SaaS layer in Telegram

Customer-facing SaaS infrastructure is gone. **Kept**: registration,
encrypted creds, trade notifications, trade views, config, DM-only,
per-user `/kill` + `/resume`. **Removed**: invite codes, subscription
expiry, broadcast, `/extend`, `/revoke`, `ExpiryChecker`, most of admin
sprawl. Menu structure was redesigned interactively in Phase 2.2.

### D5. No Discord message-edit handling

CP sends all updates as new messages — never edits. Do not add
`on_message_edit` handling.

### D6. Defensive parsing

The bot must never crash on malformed CP messages. Parse errors log to
`trade_events.event_type='error'` and skip safely. The pipeline keeps
running. The defensive boundary is `pipeline.process_message`.

### D7. Testnet / mainnet onboarding

User picks network at registration. No forced testnet period. Mainnet
requires typed `MAINNET` confirmation (Phase 3.5).

### D8. Local laptop deployment

Designed to run on Artem's laptop, 24/7. Server migration deferred to
Phase 5.2. Design stays portable (env vars, file config) but doesn't
optimize for cloud infra.

### D9. Daily backups

`sqlite3.Connection.backup()` (stdlib, server-portable) to `backups/`
at 06:00 UTC, mtime-based 30-day prune.

### D10. Exchange is the source of truth for position state

**Forced by**: 2026-05-25 NEAR/#2126 — entry filled silently on HL,
no `TRADE_LIVE` from CP, cancel handler trusted local `status=PENDING`
and left the position uncovered.

Before decisions that act on a trade (cancel, close, modify SL), query
the exchange directly rather than the cached `trade.status`. CP lifecycle
messages drive the audit trail and reconcile the orders table, but they
can be missed/mangled/delayed. **HL is authoritative for what's actually
on the books.** Applied today in `_handle_canceled`; other call sites
get migrated as we find them.

Worth noting for Phase 6 (Blofin migration): D10 applies to any exchange.
The Blofin equivalent will use Blofin's `GET /api/v1/trade/positions`.

### D11. Exchange is the source of truth for recorded *and displayed* data

**Extends D10 from decisions to data.** D10 says "query the exchange before
*acting*." D11 says: also query the exchange before *recording* and before
*showing*. The values we persist (fill prices, position size, realized PnL,
order status) and the values we surface in Telegram (balance, positions,
PnL) come from the **exchange**, not from inferring them off CP's message
text.

CP's role is **trigger + audit only**: a CP lifecycle message tells us
*that* something happened on trade #X and is recorded verbatim in
`trade_events` — but the **actual numbers** are read back from the exchange.
The spine holds: "CryptoPrinter's calls should be the only possible source
of fallacy" means our bot must not *add* fallacy by storing assumed values.

**Why now**: the HL implementation only partially holds this. Live balance
and positions display already read from HL, and decision gates query HL
(D10), but persisted **fill prices and PnL are CP-derived approximations**
— `_mark_order_filled` writes the order's *target price* as the fill (HL
exposes no convenient fill-price API), and `pnl_pct` is CP's reported
number. That's exactly the assumption-based path D11 forbids.

**Why Blofin makes it real**: Blofin exposes what HL didn't —
`GET /api/v1/trade/trade-history` (real fills), `GET /api/v1/trade/positions`
+ `positions-history` (real size/PnL/closed state),
`GET /api/v1/trade/account-balance` (real balance), and WS `orders`/
`positions` channels (push fills).

**The rule (Blofin position manager + display)**: on each CP-triggered
lifecycle event, re-read that trade's state from Blofin and write the real
values; display reads Blofin directly. **Prefer exchange truth; fall back
to the CP value only when the exchange query fails, and when you do, mark
that row as approximate in the audit trail** — never silently mix the two,
so a post-mortem can always tell a real value from a CP-derived fallback.

Closing the HL gap retroactively is out of scope (HL lacks the endpoints);
D11 is binding for all Blofin code and any future exchange adapter.

---

## Phase status

Status legend: ✅ shipped · 🟡 in progress · ◻ later

### Phase 1 — Development & validation (no live trades)

| # | Status | Commit | Task |
|---|---|---|---|
| 1.1 | ✅ | `df5fb90` | Shadow-mode capture (no live trades) |
| 1.2 | ✅ | `8717f8b` + `e96219d` | Sample corpus expansion, CP format adoption, ORDER_PENDING + TRADE_LIVE |
| 1.3 | ✅ | `b210903` | Port/wallet architecture (D1) |
| 1.4 | ✅ | `055029c` | 7 strategy presets (D2), even_split default |
| 1.5 | ✅ | `1158a4c` | Audit-log plumbing (D3) |
| 1.6 | ✅ | `7bbc3a4` | Defensive parsing (D6) |

### Phase 2 — Telegram rework

| # | Status | Commit | Task |
|---|---|---|---|
| 2.1 | ✅ | `c366c55` | SaaS layer ripped (D4) — −1737 LOC |
| 2.2 | ✅ | `80320b3` + `d772548` | Condensed dashboard, Port screen, per-trade Audit Trail |

### Phase 3 — Pre-launch

| # | Status | Commit | Task |
|---|---|---|---|
| 3.1 | ✅ | `a3770a8` | Daily SQLite backups (D9) |
| 3.2 | ✅ | `59ccc25` | launchd agent for 24/7 local deployment |
| 3.3 | ✅ | `b897d8d` | Log rotation polish |
| 3.4 | ✅ | `747eaeb` | README rewrite |
| 3.5 | ✅ | `0ee481f` | Mainnet promotion gate — typed `MAINNET` confirm + big-trade approval dialog |

### Phase 4 — GO LIVE

#### Phase 4.1 — Real signal wiring (✅ shipped)

| # | Commit | What |
|---|---|---|
| 4.1a | `82be37f` | Markdown escape in preset/display names; orchestrator startup tracebacks |
| 4.1b | `1d1a196` | Pin `hyperliquid-python-sdk >= 0.23.0` |
| 4.1c | `60deedc` | `TelegramChannelAdapter` — listens for `channel_post`, dm_only_filter passthrough |
| 4.1d | `e68122a` | Telethon forwarder + second launchd agent |
| 4.1e | `381bbc2` | `start_polling(allowed_updates=Update.ALL_TYPES)` — channel posts excluded by default |
| 4.1f | `18d475e` | Hot-reload pipeline Config after Telegram setting edits |
| 4.1g | `d4d427e` + `016d57e` | Testnet position floor — bump sub-min sizes to $15 instead of skipping |

Then the **first-signal-day post-mortem** (session 2026-05-29) which
surfaced and fixed six bugs:

| Bug | Commit | Summary |
|---|---|---|
| #1 forwarder race | `b45c068` | `asyncio.Lock` around send; preserves receive order |
| #3 + #8 orders table stale | `12354c1` | Pipeline lifecycle handlers reconcile orders table |
| #9 portfolio margin balance | `6c0e661` | Show spot USDC + perp account value separately |
| #4 cancel-parser noise | `66b9f84` | Strip @PotionScannerBot wrapper before extracting reason |
| #11 D10 case | `3ca1402` | Cancel handler queries HL position before deciding close vs cancel-orders |
| #12 close-spread oracle | `bc80fbd` | 10% → 3% IOC close spread |

#### Phase 4.2 — Real CP signal soak (🟡 ongoing)

**Goal**: observe real CP signals flowing end-to-end. Mechanics identical
to what `/inject` produced; the goal is *observation*, not building.

Want to see, across one full CP signal day:
- Multiple `signal_alert` opens that fire as auto-execute
- At least one `tp_hit` event with the auto-BE-after-TP1 move
- At least one closure (`all_tp_hit`, `stop_hit`, or `cancel`)

All audit-trail integrity per the README's "Inspecting a single trade
end-to-end" recipe.

First confirmed real CP trade through the new pipeline: **#2177 ETH
breakeven on 2026-05-29** (the dedup gate caught the @PotionScannerBot
duplicate cleanly). Phase 4.1 considered closed at that moment.

#### Phase 4.3 — Synthetic test driver (✅ shipped)

Built so we can validate the full pipeline without waiting on real CP.

| # | Commit | What |
|---|---|---|
| 4.3a | `05ea9c1` | `scripts/test_driver.py` — replaces real forwarder, posts CP-format synthetic signals to mirror channel, runs all 5 lifecycle scenarios concurrently |
| 4.3b | `37a0454` | Test driver posts via Telethon (Artem's user account), not the bot token — bots don't see their own channel posts |
| 4.3c | `e351b6f` | **Bug #18 fix** — `_round_price` enforces ≤6 decimals AND ≤5 sig figs. Surfaced on kBONK test trade, confirmed in production. |
| 4.3d | `07d3ae1` | Realistic test driver percentages — TP/SL %s derived from signal prices × leverage (not random) |
| 4.3e | `0d49f8e` | `--cleanup` also cancels orphan HL orders so test runs don't accumulate residue |
| 4.3f | `7807123` | **Bug #19 fix** — `/positions` slash command was missing per-coin close buttons; shared helper with the Trading-menu path |

Test trade IDs occupy `[7_000_000, 7_999_999]` — 7-digit IDs vs real CP's
4-digit IDs make them trivially distinguishable. `--cleanup` deletes the
range from DB AND cancels matching open orders on HL.

Other Phase 4.x bugs fixed in the same window (chronological):

| Bug | Commit | Summary |
|---|---|---|
| #13 honest close-result | `ca072ee` + `3160bf6` | Close handlers verify HL response (filled vs error vs no-fill IOC) before claiming success. Surface HL's error text verbatim. |
| #14 pipeline dedup | `9db341c` | @PotionScannerBot delivers each signal as two variants. Pipeline now dedups on `(trade_id, msg_type)` within a 60s window. |
| #15 channel-post text-handler crash | `7ea9aa3` | DM text handlers (`config_text_handler`, etc.) crashed on channel_post updates because `context.user_data` is None there. Added `filters.ChatType.PRIVATE` to the registrations + defense-in-depth guards. |
| #16 Markdown escape in `action_taken` | `53d7210` | Dedup audit rows contained text like `(trade_closed)`; the underscore opened a Markdown italic entity, broke /menu rendering. Escape at the interpolation site. |
| #17 test driver posting | `37a0454` | (See Phase 4.3b above.) |
| #18 price precision | `e351b6f` | (See Phase 4.3c above.) |
| #19 positions slash command | `7807123` | (See Phase 4.3f above.) |
| #20 resting-entry stuck PENDING | `73b20e4` | No live handler promoted a trade PENDING→OPEN — only submit-time immediate fill or startup `sync_positions`. A resting limit entry that filled *later* (CP's TRADE_LIVE, not an immediate fill) stayed PENDING, so every `status==OPEN`-gated handler silently no-opped — notably the BE-after-TP1 SL move. ADA #2184 (2026-06-01 soak) hit TP1 but its SL was never moved to entry on HL. Fix: `_promote_to_open_if_filled` (D10 — HL-confirmed, falls back to trusting CP's explicit fill on query failure) from `_handle_trade_live` + `_handle_tp_hit`. |

### Phase 5 — Post-launch (parking lot)

| # | Status | Task |
|---|---|---|
| 5.1 | ◻ | Weekly performance report (Sheets or PDF). Row labels already match CP's report (per D2). |
| 5.2 | ◻ | VPS migration. Three structural fixes called out in README § "Moving to a remote server": bind `127.0.0.1` on admin ports, offsite backup step, systemd unit. |
| 5.3 | ◻ | CI/CD (lint + tests on push). |
| 5.4 | ◻ | Backtest tooling for strategy A/B once enough live data exists. |
| 5.5 | ◻ | Long-running polish: consolidate `_round_price` (two copies), add HL-side wallet-authorization check at `/register` time, sync `.env` defaults with in-DB Artem creds. |

### Phase 6 — Blofin migration (🟡 next major effort)

**Why migrating off HL**:
- HL doesn't offer the leverage CP signals routinely call for (25–50x on
  small-caps is normal in CP; HL caps at 20–50x with low caps on the
  smaller coins).
- CP's signal calibration assumes Blofin as the trading terminal —
  matching the platform CP is tuned for is straightforward upside.
- HL had several gotchas (Bug #9 portfolio margin, Bug #12 oracle
  distance, Bug #18 decimal cap) that Blofin's API design avoids by
  having dedicated endpoints (close-positions, tpsl-order) and
  published per-asset metadata (tickSize, lotSize).

**Two source-of-truth docs** capture the spec, written 2026-06-03 and
2026-06-04 respectively:

- [`HYPERLIQUID_INTEGRATION.md`](HYPERLIQUID_INTEGRATION.md) — every HL
  touchpoint in the bot today, 15 capability sections + bug history.
- [`BLOFIN_INTEGRATION.md`](BLOFIN_INTEGRATION.md) — Blofin's answer to
  each of those sections + deltas + new capabilities + migration notes.
  Includes a dedicated **Demo Trading** section that documents how the
  demo environment replaces HL testnet.

The two docs mirror each other section-by-section so they diff cleanly.

**Implementation order** (estimate: 3–4 focused sessions):

| # | Task | What |
|---|---|---|
| 6.1 | Apply for Blofin "API Transaction" permission | Real-world dependency. Required before any code runs. |
| 6.2 | Validate the demo environment | Six-step plan in `BLOFIN_INTEGRATION.md` § Demo Trading. Sign+send smoke test, top-up demo balance, place + cancel one tiny order. **Do not start client code until this validates.** |
| 6.3 | `src/exchange/blofin.py` — hand-rolled HTTP client | No SDK from Blofin. ~200 LOC. Surface mirrors `HyperliquidClient`. |
| 6.4 | `src/exchange/blofin_order_builder.py` | Contract-value math, tickSize-based rounding, native TP/SL via `/api/v1/trade/tpsl-order` |
| 6.5 | `src/exchange/blofin_position_manager.py` | submit_trade / cancel / close / sync. Use `/api/v1/trade/close-positions` (native market close — Bug #12 obviated). **D11**: on each CP-triggered event, re-read the trade's real state from Blofin (`trade-history` fills, `positions`) and persist the real values — never CP-target approximations; fall back to CP only on query failure and mark the row approximate. |
| 6.6 | `src/utils/symbol_mapper.py` — `potion_to_blofin` | Parallel to `potion_to_hyperliquid`. Most coins map identically; build override table during demo testing. |
| 6.7 | Schema migration ✅ (Commit 1, `7cd95c0`) | Added `exchange` + `passphrase_enc` to `user_credentials` **and** migrated `orders.oid` INTEGER→TEXT (Blofin string IDs). `ExchangeConfig` carries `exchange` + `passphrase`. Existing HL users untouched. |
| 6.8 | `/register` flow — exchange choice | New users pick Blofin or HL at registration. **D11**: Telegram menus read balance/positions/PnL from Blofin directly. |
| 6.9 | Test driver wiring | Adapter-pattern dispatch so the existing driver works against either exchange. Add `--top-up-demo` helper. |
| 6.10 | Soak on Blofin demo | Run all 5 test driver scenarios on Blofin demo; audit cleanly before any user moves to production Blofin. **D11 check**: persisted fills/PnL match Blofin's own `trade-history`/`positions-history`, not CP's numbers. |
| 6.11 | Per-user migration | One user at a time. Keep HL pipeline alive as fallback. |

**HL code stays in the repo permanently** as the fallback adapter — the
work is to add Blofin alongside, not to replace.

---

## Handoff — picking up from this point

If you're a new model / collaborator picking up this branch, read these
documents in order:

1. **This file** — project scope, design decisions, phase status.
2. **[`../CLAUDE.md`](../CLAUDE.md)** — codebase orientation, conventions,
   operational notes, "where things live".
3. **[`HYPERLIQUID_INTEGRATION.md`](HYPERLIQUID_INTEGRATION.md)** — the
   exchange we use today, all 15 capability sections.
4. **[`BLOFIN_INTEGRATION.md`](BLOFIN_INTEGRATION.md)** — the exchange
   we're migrating to. Phase 6 specs.
5. **[`../README.md`](../README.md)** — operator's manual (laptop deploy,
   DB inspection, cleanup recipes, VPS playbook).

### First 10 minutes — sanity checks

```bash
cd ~/ClaudeProjects/potion-perps-bot

# 1. On the right branch
git branch --show-current             # rework/scope-v1
git log --oneline -5

# 2. Tree clean
git status

# 3. Tests pass
python3 -m pytest tests/ -q | tail -2  # expect 808 passed

# 4. Both launchd agents up
launchctl print gui/$(id -u)/local.potion-perps-bot 2>&1 | grep state
launchctl print gui/$(id -u)/local.potion-perps-forwarder 2>&1 | grep state

# 5. Any real CP trades fire while you were away?
sqlite3 -header -column data/trades.db \
  "SELECT trade_id, coin, side, status, close_reason, pnl_pct, created_at
   FROM trades WHERE user_id='7441245554' AND trade_id < 80000
   ORDER BY trade_id DESC LIMIT 5;"

# 6. Any test trades left lying around in the DB?
sqlite3 data/trades.db \
  "SELECT COUNT(*) FROM trades WHERE trade_id BETWEEN 7000000 AND 7999999;"
# 0 = clean. Non-zero = `python3 scripts/test_driver.py --cleanup`.
```

### What you'd do next, by branch

#### If continuing Phase 4.2 soak (low-effort, observation mode):

- Watch `logs/bot.log` for `Classified message as:` lines indicating real CP signals.
- Audit any closed trade with the README's "Inspecting a single trade end-to-end" recipe.
- Phase 4.2 closes when we have a full day's worth of real signals
  through the pipeline cleanly. Phase 6 work can happen in parallel.

#### If starting Phase 6 (Blofin migration, big effort):

- Read [`BLOFIN_INTEGRATION.md`](BLOFIN_INTEGRATION.md) end-to-end.
- The first blocker is **applying for Blofin "API Transaction"
  permission** — Artem has to do this on the Blofin website. Without
  that permission the API key can't be created.
- After approval, generate the API key with: **Permissions = Read + Trade only**
  (NO Withdraw, NO Transfer). Save the passphrase securely.
- Run the **6-step demo validation** before writing the BlofinClient
  (see `BLOFIN_INTEGRATION.md` § "Demo Trading — validation plan when we start").

### Operational notes for new sessions

These came up during the rework and are easy to forget:

- **Don't suggest Discord-direct.** The constraint is documented in
  `CLAUDE.md`. Railway already runs `@PotionScannerBot`; sharing the
  token kills their service for paying users.
- **Don't quietly toggle Artem's `auto_execute` to OFF.** It's ON
  deliberately on testnet. Mainnet has its own gate.
- **Don't commit to `main` during rework.** Active branch is
  `rework/scope-v1`. The merge happens later when the full transition
  is feature-complete and we're ready.
- **swaag is a real user**, not a fixture. She's active with no port
  configured, so every CP signal posts a "skipped: port not configured"
  audit row + Telegram DM to her. Either ask her to set a port or
  temporarily deactivate via the admin API. Don't delete.
- **Test trade IDs ≥ 7_000_000.** Real CP IDs are < 80,000. If you see
  a 7-digit trade_id, it's synthetic from `scripts/test_driver.py`.
  Bulk-clean with `--cleanup` (which also cancels matching open orders
  on HL).
- **`Update.ALL_TYPES` in `start_polling` is load-bearing.** If channel
  adapter ever stops working, check this first.

### When to ask vs decide

- **Ask** for: anything that touches real money (mainnet promotion, big
  trade sizes, changes to credential storage, withdrawing funds).
- **Ask** for: scope changes — anything that affects the design
  decisions D1–D10 above.
- **Decide** (and document): refactors that don't change external
  behavior, test additions, comment/doc edits, internal naming.
- **Decide** (and surface): bugs and weak spots you see while reading.
  Document, don't fix unilaterally; let Artem prioritize.

### Files that grow stale fastest

- `CLAUDE.md` § "Current phase" — needs an update after every major
  shipped phase or significant bug. The phase status table in
  `REWORK_BRIEF.md` (this file) is the master; CLAUDE.md is the
  operational mirror.
- `README.md` § "Current configuration" + "Phase status" — needs an
  update when users / port / network state changes meaningfully.
- Bug history tables in both files — append, don't rewrite.

---

## Tests

**808/808 passing** as of HEAD on `rework/scope-v1`. 35 test files.
Recent additions (last ~10 commits):

- `tests/test_e2e_pipeline.py::TestPendingToOpenPromotion` — 5 tests on
  Bug #20: TRADE_LIVE/TP_HIT promote a resting-entry trade PENDING→OPEN
  (D10 HL-confirmed, CP-fallback on query failure); regression asserts
  TP1 on a still-PENDING trade fires the breakeven SL move.
- `tests/test_test_driver.py` — 41 tests covering scenarios, template
  fidelity, scheduling, cleanup, realistic-percentage math, orphan-order
  helper.
- `tests/test_positions_keyboard.py` — 9 tests locking the slash-command
  ↔ menu-path keyboard parity contract (Bug #19).
- `tests/test_text_handler_guards.py` — 4 tests on channel-post text
  handler defense (Bug #15).
- `tests/test_e2e_pipeline.py::TestPricePrecisionForTightTickCoins` —
  23 tests on the Bug #18 decimal-cap fix.
- `tests/test_e2e_pipeline.py::TestPipelineDedup` — 5 tests on Bug #14
  pipeline-level dedup.
- `tests/test_e2e_pipeline.py::TestClosePositionResponseHandling` — 4
  tests on Bug #13 honest close result.
- `tests/test_formatters.py::TestFormatMainMenu::test_recent_event_action_taken_underscores_are_escaped`
  — Bug #16 regression.

---

## Constraints / guardrails

- **Auditability is the spine.** Every trade open writes a
  `decision_snapshot`. Every lifecycle event writes a `trade_events`
  row. If a column would make debugging easier, default to adding it.
- **The bot never imposes its own exit logic** beyond what the active
  preset encodes. No surprise position closures, no "smart" overrides.
- **Never crash on bad input.** Parse errors logged + skipped.
- **Encrypted credential storage stays Fernet.** Don't downgrade.
- **Per-user isolation stays.** Composite PK `(user_id, trade_id)`.
- **HL is source of truth for position state (D10).** Same will apply
  to Blofin in Phase 6.
- **Exchange is source of truth for recorded + displayed data (D11).**
  On Blofin, persist real fills/PnL/state read back from the exchange,
  not values inferred from CP text; fall back to CP only on query failure
  and mark the row approximate. Binding for all Blofin code.
- **Test trade IDs in `[7_000_000, 7_999_999]`** — never reuse this
  range for anything else.

---

## Working style

- **Branch first, commit small, commit often.** Active rework branch is
  `rework/scope-v1`. No commits to `main` during rework.
- **Propose plans before writing code.** Files touched, design choices,
  tests added, LOC estimate. Wait for explicit approval on anything
  that touches mainnet, credentials, schema, or design decisions.
- **Be opinionated; push back.** If something violates auditability,
  introduces surprise behavior, or risks crashes, say so directly.
- **Ask when a tradeoff is genuinely ambiguous.** One question now
  beats untangling a wrong assumption later.
- **Flag issues seen during reading.** Even out-of-scope contradictions
  to the design goals get surfaced — document, don't fix unilaterally.
- **Phase 4 was operational, not architectural.** Less code, more
  careful flipping of real-world switches. Phase 6 is the opposite —
  a real architectural shift to Blofin. Plan accordingly.

---

## Documentation map

```
docs/
├── REWORK_BRIEF.md                  ← you are here. Master scope + phases + handoff.
├── HYPERLIQUID_INTEGRATION.md       ← what we do on HL today (15 capability sections).
├── BLOFIN_INTEGRATION.md            ← what we'll do on Blofin (mirror structure + demo).
└── archive/
    ├── README.md                     ← explains the archive
    ├── telegram-bot-plan.md          ← pre-rework SaaS design — REVERSED in D4
    └── telegram-implementation-steps.md  ← same; superseded.

CLAUDE.md                            ← project orientation for future AI sessions.
README.md                            ← operator's manual (laptop deploy, DB recipes, etc.)
```

Anything not on this list is either application code, tests, or
generated artifacts. Code citations to specific files / line numbers
are scattered throughout the docs above; trust the docs more than
half-remembered codebase state.

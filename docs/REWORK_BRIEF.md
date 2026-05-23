# Rework Brief

The verbatim spec for the rework session that started 2026-05-19, captured here so future sessions don't depend on transient prompt context.

Branch: `rework/scope-v1` (off `main` at commit `170abbd`).

---

## Project reframing

**What this project was:** a marketable SaaS for automating CryptoPrinter (CP) Discord trade calls onto Hyperliquid perps. Built with invite codes, subscription expiry, admin broadcast, billing-shaped UX — all designed for selling access to strangers.

**What it now is:** a private automation tool for Artem and 2 friends. Three users total, all known to each other, no paying customers, no subscriptions. Multi-user support stays because we each have our own Hyperliquid account, but the customer-facing SaaS layer is being ripped out.

**Core purpose:** faithfully automate CryptoPrinter's Discord-bot calls (entry + full lifecycle: TP hits, breakeven moves, stop hits, cancels, manual SL/TP amendments) onto each user's Hyperliquid account, with monitoring and control via Telegram.

**Underlying design goal — the spine of every decision:**

> CryptoPrinter calls should be the only possible source of fallacy. The bot itself must be reliable, deterministic, and auditable. If a trade goes wrong, I should be able to answer in 30 seconds:
>
> - What was the original signal text?
> - What did our parser extract?
> - What strategy + port + risk-level decided the size?
> - What orders did we place?
> - What lifecycle events happened?
> - Was the loss caused by CP being wrong, or by our bot doing something the signal didn't say?

---

## Current state of the codebase (as of 2026-05-19, commit `170abbd`)

- ~15,100 LOC, ~370 tests across 24 test files.
- Multi-user orchestrator (`src/orchestrator.py`) — N user pipelines, isolated, hot-add/remove, kill switch.
- Encrypted credential storage (`src/crypto.py`) — Fernet symmetric, key from `ENCRYPTION_KEY` env or `data/.encryption_key` file.
- Admin REST API (`src/api/admin.py`) — aiohttp, X-API-Key, full user CRUD + kill/resume.
- Real Discord adapter (`src/input/discord_adapter.py`) — `discord.py`, filters by channel + source-bot display name.
- Full SaaS-flavored Telegram bot (`src/telegram/`, ~3000 LOC) — invite codes, registration, per-user config, trade approval flow, ~20 user commands + ~10 admin commands, PnL monitor, expiry checker.
- 6 strategy presets, sizing per signal risk level (`size_by_risk`), full lifecycle parsing for 10 message types.
- Health server on :8080, structured JSON logging, graceful SIGTERM/SIGINT, Docker + docker-compose.

---

## Locked design decisions

Treat these as constraints, not suggestions.

### D1. Port/wallet separation

Position sizing is currently % of total Hyperliquid wallet balance. Change to % of "port" — a user-configured subset of the wallet.

- New columns:
  - `user_config.port_usd` (REAL)
  - `user_config.port_mode` (TEXT, one of `'withdraw' | 'compound' | 'watermark'`)
- Position sizing uses `port_usd`, not wallet.
- Three modes (user-selectable in Telegram):
  - **`withdraw`** — profits stay in wallet; port stays at the configured value.
  - **`compound`** — port = port + cumulative P&L (both directions).
  - **`watermark`** — profits push the port up; the new value becomes the new floor. Losses can pull port down but never below the highest floor ever reached. Floor = best historical port value.
- Guardrails:
  - If `port > wallet`: halt new trades immediately and notify the user.
  - If `port > wallet × 0.95`: warn but continue.
  - Open positions are **never auto-closed** by this rule. The bot does not impose its own exit logic.

### D2. Strategy presets rebuilt to mirror CryptoPrinter's report

Drop the current 6 presets (`runner`, `conservative`, `tp2_exit`, `tp3_hold`, `breakeven_filter`, `small_runner`). Replace with:

| Name          | tp_split   | SL→BE       | CP report label         |
|---------------|------------|-------------|-------------------------|
| `tp1_only`    | 100/0/0    | never       | TP1 Only (Safe Way)     |
| `tp2_only`    | 0/100/0    | never       | TP2 Only                |
| `tp3_only`    | 0/0/100    | never       | TP3 Only                |
| `tp2_be`      | 0/100/0    | after TP1   | TP2 + BE                |
| `tp3_be`      | 0/0/100    | after TP1   | TP3 + BE                |
| `hybrid`      | 10/70/20   | after TP1   | Hybrid 10/70/20         |
| `even_split`  | 33/33/34   | after TP1   | (our addition — default)|

**Default preset for new users: `even_split`.**

Our weekly per-user performance report should have rows that line up with CP's own report rows for direct comparison.

### D3. Audit trail (minimal-but-sophisticated)

Add to `trades`:
- `raw_signal_text TEXT` — the verbatim Discord message that opened the trade.
- `decision_snapshot TEXT` — JSON, captured at trade open:
  ```json
  {
    "preset": "even_split",
    "size_pct_applied": 2.0,
    "port_usd_at_open": 1000.0,
    "risk_level": "MEDIUM",
    "exposure_used_pct": 14.5,
    "leverage_applied": 14,
    "why": "size_by_risk[MEDIUM]=2.0; clamped to max_position_size=500"
  }
  ```

New table `trade_events`:
```sql
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER,
    user_id TEXT,
    occurred_at TEXT,
    event_type TEXT,    -- signal_alert | tp_hit | breakeven | stop_hit
                        -- | sl_move | trade_closed | cancel | error
    raw_text TEXT,      -- verbatim message that triggered this
    action_taken TEXT,  -- "moved SL to 1985" / "closed 33% at TP1" / etc.
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);
```

Use for weekly reports + post-mortem debugging.

### D4. No more SaaS layer in Telegram

Telegram bot is being redesigned. Most customer-facing infrastructure goes.

**Remove or simplify drastically:**
- Subscription expiry / `access_expires_at` logic.
- Expiry warnings (3d, 1d notifications).
- `ExpiryChecker` background task.
- `/generate_code`, `/generate_codes`, `/list_codes`, `/revoke_code`.
- `/extend`, `/revoke` (subscription lifecycle).
- `/broadcast` (3 users — pointless).
- Most of `handlers/admin.py`.
- Probably the entire `invite_codes` table (or simplify to one shared access code, no expiry).

**Keep:**
- Registration with creds-in-DM (self-deleting messages).
- Encrypted credential storage (Fernet) — never downgrade.
- Trade notifications.
- Trade view, positions view, history view.
- Config view + strategy selection.
- DM-only enforcement.
- `/kill` and `/resume` per user.
- Per-user isolation.

**Add (after menu redesign):**
- Port management UI (set port amount, select mode, view current state, view port vs wallet).
- Audit/explain view for a given trade.
- Weekly performance report trigger or auto-send.

The menu structure will be redesigned **interactively** during Phase 2. Do not propose final menu shapes ahead of that.

### D5. No Discord message-edit handling

CryptoPrinter sends all updates as new messages — never edits. Do not add `on_message_edit` handling. Current `on_message`-only behavior is correct.

### D6. Defensive parsing

The bot must never crash on malformed CP messages. Every parse error logs to `trade_events` and skips the message safely. The pipeline keeps running.

### D7. Testnet / mainnet onboarding

Current flow stays — user picks network at registration. No forced testnet period.

### D8. Deployment target

Designed to run locally on Artem's laptop, 24/7. Server migration comes later. Keep the design portable (env vars, file-based config) but don't optimize for k8s/cloud infra.

### D9. Backups — in scope

Lightweight daily SQLite backup (`sqlite3 .backup`, dump to a local `backups/` directory with date-stamped filenames, prune older than 30 days). Schedule via a background asyncio task in `main.py` or cron.

---

## Phase plan

Status legend: ✅ shipped on `rework/scope-v1` · ⏳ next · ◻ pending

### Phase 1 — Development & validation (no live trades; sample-driven)

| # | Status | Commit | Task |
|---|---|---|---|
| 1.1 | ✅ | `df5fb90` | **Shadow mode** — capture-only Discord adapter mode that listens to a channel and logs every message verbatim, without executing. The form factor; live wiring is Phase 4.1. |
| 1.2 | ✅ | `8717f8b` + `e96219d` | **Sample corpus expansion** — adopted new CP format (mentions, prev URLs, footer, signed profits, POSITION type), added ORDER_PENDING + TRADE_LIVE message types, +50 signal samples, classifier + parser hardened. |
| 1.3 | ✅ | `b210903` | **Port/wallet architecture (D1)** — schema, sizing, three modes (withdraw/compound/watermark), guardrails. |
| 1.4 | ✅ | `055029c` | **Strategy preset overhaul (D2)** — 7 new presets matching CP's report rows, `even_split` default, idempotent migration from old names. |
| 1.5 | ✅ | `1158a4c` | **Audit-log plumbing (D3)** — `trades.raw_signal_text` + `trades.decision_snapshot` + new `trade_events` table; every handler writes events. |
| 1.6 | ✅ | `7bbc3a4` | **Defensive parsing (D6)** — typed parser errors, classifier never crashes, action_taken truncation, submission-failure event. |

### Phase 2 — Telegram rework (interactive design)

| # | Status | Commit | Task |
|---|---|---|---|
| 2.1 | ✅ | `c366c55` | Rip SaaS layer (D4) — invite codes / expiry / broadcast / users / extend / revoke / renew flow removed. −1737 LOC. |
| 2.2 | ✅ | `80320b3` + `d772548` | Interactive menu redesign — condensed main dashboard, 4 drill-downs (Calls / Trading / Port / Config), Pause toggle on main. Port screen (state + history). Per-trade Audit Trail. |
| 2.3 | (folded into 2.2) | | Port management UI. |
| 2.4 | (folded into 2.2) | | Audit/explain views. |
| 2.5 | ◻ | | Update notifications to surface port-vs-wallet status and mode-relevant info. (Deferred — may roll into Phase 4 polish.) |

### Phase 3 — Pre-launch

| # | Status | Task |
|---|---|---|
| 3.1 | ✅ | `a3770a8` | **Backups (D9)** — daily SQLite backup via `sqlite3.Connection.backup()` (stdlib, server-portable) to `backups/`, mtime-based 30-day prune, 06:00 UTC default, no catch-up on miss, asyncio task wired into `main.py` with shared `shutdown_event`. |
| 3.2 | ⏳ | | **Local deployment setup** — `launchd` plist for macOS so the bot runs 24/7. SIGTERM/SIGINT already wired. Must address laptop-sleep (CP signals during sleep are lost — `caffeinate -i` in `ExecStart` or "prevent sleep when plugged in"). |
| 3.3 | ◻ | **Log rotation polish** — confirm the existing rotating-file handler (10 MB × 5) caps correctly under sustained load. Tune if needed. |
| 3.4 | ◻ | **README rewrite** — the README is frozen during the rework. This is the slot for the full rewrite. |
| 3.5 | ◻ | **Mainnet promotion gate** — conservative defaults, big-trade confirmation dialog in Telegram, etc. |

### Phase 4 — GO LIVE

| # | Status | Task |
|---|---|---|
| 4.1 | ◻ | Wire the real Discord adapter to CP's channel (channel ID + bot or selfbot auth). |
| 4.2 | ◻ | Artem onboards as the first user on testnet, runs ~5 live signals end-to-end, validates audit log + Telegram UI. |
| 4.3 | ◻ | Add the 2 friends as users. |
| 4.4 | ◻ | Move to mainnet (per-user choice). |

### Phase 5 — Post-launch (parking lot)

| # | Task |
|---|------|
| 5.1 | Weekly performance report (Sheets or PDF). Row labels already match CP's report (per D2). |
| 5.2 | VPS migration if needed. |
| 5.3 | CI/CD (lint + tests on push). |
| 5.4 | Backtest tooling for strategy A/B once enough live data exists. |

---

## Tests

**560/560 passing** as of `a3770a8` (the baseline at `170abbd` was 414).

---

## Constraints / guardrails

- **Auditability is the spine.** Every trade open writes a `decision_snapshot`. Every lifecycle event writes a `trade_events` row. If a column would make debugging easier, default to adding it.
- The bot **never imposes its own exit logic** beyond what the active preset encodes. No surprise position closures, no "smart" overrides. Source bot says X, we do X.
- The bot **never crashes on bad input.** Parse errors are logged and skipped.
- Encrypted credential storage stays Fernet. Don't downgrade.
- Per-user isolation stays. Composite PK `(user_id, trade_id)` everywhere. One user's bug can't touch another user's data.
- **Don't update the README** during the rework — it's stale, it'll be rewritten in Phase 3.4 as a final polish step.

---

## Working style

- Create a git branch immediately (`rework/scope-v1` — done 2026-05-19). Do not commit to `main` during rework. Commit small, commit often.
- Propose plans before writing code. Be opinionated. Push back on anything that violates the design goals (auditability, no surprise behavior, never crashes).
- When unsure about a design tradeoff, ask. Better to answer one question now than untangle a wrong assumption later.
- If something contradicts the design goals during reading (a place where the bot crashes, surprises the user, drops audit information), flag it even if it's not in scope for the current phase.

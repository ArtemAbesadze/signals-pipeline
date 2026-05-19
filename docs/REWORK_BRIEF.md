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

### Phase 1 — Development & validation (no live trades; sample-driven)

| # | Task |
|---|------|
| 1.1 | **Shadow mode for sample collection** — capture-only Discord adapter mode that connects to a channel and logs every message verbatim to a file, without executing. Used to build a sample corpus from CP's channel while we keep developing. Artem will run this manually and provide samples via screenshot/copy until the final Discord wiring step. |
| 1.2 | **Sample corpus expansion** — collect 30+ more CP messages of varying lifecycle events; integrate into `signals/samples/`; add classifier/parser test coverage. |
| 1.3 | **Port/wallet architecture (D1)** — schema, sizing, three modes, guardrail logic. |
| 1.4 | **Strategy preset overhaul (D2)** — rebuild the 7-preset list, set `even_split` as default, update `size_by_risk` handling, migrate any existing `user_config` rows. |
| 1.5 | **Audit-log plumbing (D3)** — `trades` columns + `trade_events` table + pipeline + lifecycle handlers all writing events. |
| 1.6 | **Defensive parsing (D6)** — never crash on bad input; route errors to `trade_events`. |

### Phase 2 — Telegram rework (interactive design)

| # | Task |
|---|------|
| 2.1 | Rip SaaS layer (D4) — invite/expiry/broadcast/admin sprawl removed. |
| 2.2 | Redesign menu structure interactively (don't predesign). |
| 2.3 | Add port-management UI. |
| 2.4 | Add audit/explain views. |
| 2.5 | Update notifications to surface port-vs-wallet status and mode-relevant info. |

### Phase 3 — Pre-launch

| # | Task |
|---|------|
| 3.1 | Backups (D9). |
| 3.2 | Local deployment setup — systemd / launchd / tmux per Artem's OS preference. |
| 3.3 | Log rotation — the structured JSON logs need to not grow infinite. |
| 3.4 | **README rewrite.** |
| 3.5 | Mainnet promotion gate — conservative defaults, big-trade confirmation dialog in Telegram, etc. |

### Phase 4 — GO LIVE

| # | Task |
|---|------|
| 4.1 | Wire the real Discord adapter to CP's channel (channel ID + bot or selfbot auth). |
| 4.2 | Artem onboards as the first user on testnet, runs ~5 live signals end-to-end, validates audit log + Telegram UI. |
| 4.3 | Add the 2 friends as users. |
| 4.4 | Move to mainnet (per-user choice). |

### Phase 5 — Post-launch (parking lot)

| # | Task |
|---|------|
| 5.1 | Weekly performance report (Sheets or PDF). |
| 5.2 | VPS migration if needed. |
| 5.3 | CI/CD (lint + tests on push). |
| 5.4 | Backtest tooling for strategy A/B once enough live data exists. |

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

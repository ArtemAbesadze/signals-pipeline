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
├── README.md                            # Stale during rework (rewritten in Phase 3.4)
├── config/
│   ├── config.example.yaml
│   └── config.yaml                      # active, gitignored
├── docs/
│   ├── REWORK_BRIEF.md                  # full rework spec (D1–D9 + phases)
│   ├── telegram-bot-plan.md             # legacy SaaS design — being reversed
│   └── telegram-implementation-steps.md # legacy SaaS plan — being reversed
├── src/
│   ├── orchestrator.py                  # Multi-user fan-out
│   ├── pipeline.py                      # Per-user signal processor
│   ├── crypto.py                        # Fernet credential encryption
│   ├── health.py                        # :8080 health endpoint
│   ├── api/admin.py                     # :8081 admin REST API
│   ├── config/settings.py               # YAML + .env loader, typed dataclasses
│   ├── exchange/                        # Hyperliquid SDK wrapper, order builder, position manager
│   ├── input/                           # Discord adapter (live) + sim/cli/file adapters
│   ├── parser/                          # classifier + signal/update parsers
│   ├── state/                           # SQLite — trades, orders, users, encrypted creds
│   ├── strategy/position_sizer.py       # sizing + pre-trade risk gate
│   ├── telegram/                        # bot + handlers + notifications + monitors
│   └── utils/                           # structlog setup, symbol mapper
├── tests/                               # ~370 tests across 24 files
└── signals/
    ├── samples/                         # Real Discord samples for parser tests
    └── test/                            # E2E test fixtures
```

Key files when something breaks:

| Symptom | Start here |
|---------|-----------|
| Bot didn't act on a CP message | `src/parser/classifier.py` → `pipeline.process_message` |
| Wrong size on a trade | `src/strategy/position_sizer.py` → `_handle_signal` in `pipeline.py` |
| Order rejected by Hyperliquid | `src/exchange/order_builder.py` (sizing/rounding) → `position_manager.submit_trade` |
| Telegram command misbehaved | `src/telegram/handlers/` (one file per command group) |
| User can't register | `src/telegram/handlers/registration.py` + `src/state/user_db.py` |
| State out of sync after restart | `src/exchange/position_manager.py::sync_positions` |

---

## Conventions

1. **Audit everything.** Every trade open writes a `decision_snapshot` (JSON). Every lifecycle event writes a `trade_events` row with `raw_text` + `action_taken`. If a column would make debugging easier, add it.
2. **Never crash on bad input.** Parse errors are logged + recorded as a `trade_events.event_type='error'` row + skipped. The pipeline keeps running. The defensive boundary is the message handler in `pipeline.py`.
3. **No bot-imposed exit logic.** The active preset is the only authority on when/how positions are exited. No "smart" overrides, no surprise closures.
4. **Encrypted credentials stay encrypted.** Fernet symmetric encryption via `src/crypto.py`. Never downgrade. Master key from `ENCRYPTION_KEY` env or `data/.encryption_key`.
5. **Per-user isolation.** Composite PK `(user_id, trade_id)` on `trades` and `orders`. All queries filter by `user_id`. One user's bug cannot touch another user's data.
6. **DM-only for Telegram.** `dm_only_filter` middleware rejects group messages. Credential-collection messages are deleted on receipt.
7. **No Discord edit handling.** CP sends all updates as new messages, never edits existing ones. `on_message` only — do not add `on_message_edit`.
8. **Tests close behind code.** ~370 tests today across `tests/`. New features land with tests, not after.
9. **Branch first during rework.** Active branch: `rework/scope-v1`. No commits to `main` until the rework is feature-complete.
10. **README is frozen during rework.** Gets a full rewrite in Phase 3.4. Don't update it incrementally.

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

**Phase 3 — Pre-launch.** Phases 1, 2 and Phase 3.1 are shipped. Branch
`rework/scope-v1` is at `a3770a8` on GitHub.

Done:

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

Up next (Phase 3 — pre-launch polish):

1. **3.2 Local deployment** — `launchd` plist for macOS so the bot runs 24/7 on Artem's laptop. SIGTERM/SIGINT already wired. **Must address laptop sleep** (see "Picking up" below — it's the biggest operational gap and was flagged during 3.1).
2. **3.3 Log rotation polish** — confirm rotating logs cap correctly under sustained load; tune if needed.
3. **3.4 README rewrite** — the README is frozen during rework; this is where it gets the full rewrite for the new scope. Surface the same-disk-backup DR caveat and the `0.0.0.0` admin-port caveat (both noted below) here.
4. **3.5 Mainnet promotion gate** — conservative defaults + big-trade confirmation dialog in Telegram before flipping to mainnet.

### Picking up where we left off (session paused mid-Phase-3)

Sanity checks before doing anything else:

```bash
git branch --show-current        # should print: rework/scope-v1
git log --oneline -3             # HEAD should be a3770a8 (Phase 3.1)
git status                       # should be clean
python3 -m pytest tests/ 2>&1 | tail -2   # 560 passed
```

If all four are green, you're at the right checkpoint. **Phase 3.2 (`launchd` plist) is next** — propose a plan before writing code. Key notes that won't be obvious from `git log`:

- **Laptop sleep is the silent killer of this deployment.** macOS sleep stops the bot, and CP signals that fire during sleep are **gone** — `on_message` only fires while the process is up, and there's no queue. `launchd` respawns on crash, not on sleep-wake. Mitigation for 3.2: wrap `ExecStart` in `caffeinate -i`, or instruct the user to set System Settings → "Prevent sleep when plugged in." This is the strongest argument for the eventual VPS move (Phase 5.2) and should be called out clearly in 3.2's plist + README copy.
- **The backup loop's "skip on miss" policy was a deliberate choice** (over a catch-up-on-wake policy) made during 3.1. Don't re-litigate it without a reason. If catch-up is added later, it needs a marker file for "last successful backup."
- **Two structural assumptions worth knowing for the eventual server move (Phase 5.2):**
  - `:8080` health + `:8081` admin bind to `0.0.0.0` (`src/health.py:65`, `src/api/admin.py:83`). Behind NAT on a laptop = harmless. On a public VPS = the `X-API-Key` is the entire perimeter — bind to `127.0.0.1` + reverse proxy, or add IP allowlist + TLS, before exposing.
  - `backups/` lands on the same disk as the DB (`src/state/backup.py`). For real DR, add an offsite `rsync`/`scp` step. Documented inline in `config/config.example.yaml`; deferred to Phase 5.
- **D8 ("designed local, portable") has held up.** The only laptop-bound item in Phase 3 is 3.2's `launchd` plist itself — by design. The structural code is deployment-agnostic.

Phase 4 = go live (wire CP's real Discord, Artem onboards on testnet, then friends, then mainnet per-user).
Phase 5 = parking lot (weekly performance report, VPS, CI/CD, backtest tooling).

Full phase breakdown: `docs/REWORK_BRIEF.md`.

## Tests

**560/560 passing** as of `a3770a8` (up from 414 at the start of the rework — +146 tests across 12 commits).

---

## Quick reference

| Need | Command |
|------|---------|
| Run bot | `python3 main.py` (Ctrl+C to stop) |
| Tests | `python3 -m pytest tests/ -v` |
| New branch | `git checkout -b <name>` from `rework/scope-v1` |
| Env vars | `.env` (gitignored); template in `.env.example` |
| Active config | `config/config.yaml` (gitignored); template in `config/config.example.yaml` |
| Logs | `logs/bot.log` (rotating, 10 MB × 5) |
| DB | `data/trades.db` (SQLite, WAL mode) |
| Encryption key | `data/.encryption_key` (auto-generated if missing) |

---

## Working style (for future Claude sessions)

- **Branch first, commit small, commit often.** Active rework branch is `rework/scope-v1`. No commits to `main` during rework.
- **Propose plans before writing code.** Files touched, design choices, tests added, LOC estimate. Wait for explicit approval.
- **Be opinionated; push back.** If something violates auditability, introduces surprise behavior, or risks crashes, say so directly.
- **Ask when a tradeoff is genuinely ambiguous.** One question now beats untangling a wrong assumption later.
- **Flag issues seen during reading.** Even out-of-scope contradictions to the design goals get surfaced — document, don't fix unilaterally.
- **Don't update the README during rework** (Phase 3.4 owns that).

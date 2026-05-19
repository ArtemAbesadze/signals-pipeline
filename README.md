# Potion Perps Bot

Multi-user automated trading service that ingests Potion Perps signals from Discord, parses them in real time, and executes perpetual futures trades on Hyperliquid on behalf of each registered user — with per-user strategy presets, encrypted credentials, risk guardrails, and a Telegram bot for onboarding, configuration, approval, and monitoring.

**Status:** Phase 4 complete — multi-user pipeline, Discord signal ingestion, encrypted per-user credentials, full Telegram bot UI, admin REST API, Docker deploy, ~370 tests. **Testnet only** (mainnet registration is intentionally blocked).

---

## Architecture

```
Discord (Potion Perps)
        │
        ▼
┌─────────────────┐
│ Discord Adapter │  (one process, listens to one channel + source bot)
└────────┬────────┘
         │  asyncio.Queue
         ▼
┌──────────────────────────────────────────────────────────────┐
│                       Orchestrator                           │
│  one signal in → fan out to every active user pipeline       │
│                                                              │
│  ├── User Pipeline (alice) ─→ Hyperliquid (alice's creds)    │
│  ├── User Pipeline (bob)   ─→ Hyperliquid (bob's creds)      │
│  └── User Pipeline (…)     ─→ Hyperliquid (…)                │
└────────────────────┬─────────────────────────────────────────┘
                     │
        ┌────────────┼────────────┬────────────┬────────────┐
        ▼            ▼            ▼            ▼            ▼
   SQLite        Telegram     Admin REST    Health      PnL +
   (state +     Bot (users)   API (8081)    (8080)      Expiry
   encrypted                                             monitors
   creds)
```

A single Discord adapter feeds one signal into the orchestrator, which dispatches it to each active user's pipeline. Each pipeline runs the same classify → parse → size → build → submit flow, but uses that user's credentials, config, and database scope. The Telegram bot is the user-facing layer for registration, approval, and monitoring; the admin REST API is the operator-facing layer for user management and emergency control.

---

## Components

| Component | Responsibility |
|-----------|---------------|
| `main.py` | Loads config, wires the orchestrator, starts adapter / admin API / health / Telegram bot / background monitors, runs the main message loop, handles graceful shutdown. |
| `src/orchestrator.py` | Multi-user fan-out. Manages per-user `Pipeline` contexts, activate/deactivate, pause/resume, kill switch. |
| `src/pipeline.py` | Per-user signal processor. Classifies messages, dispatches to handlers, runs the size + risk + order-build + submit flow. |
| `src/input/` | Signal source adapters: `discord` (live), `simulation` (file replay), `cli` (paste), `file` (stub). |
| `src/parser/` | `classifier.py` (10 message types) and parsers that turn raw text into typed dataclasses (`ParsedSignal`, `TpHit`, `StopHit`, etc.). |
| `src/strategy/position_sizer.py` | USD allocation based on balance, risk level, preset; pre-trade risk gate. |
| `src/exchange/` | `HyperliquidClient` (SDK wrapper + retries + rate-limit handling), `order_builder` (signal → orders), `position_manager` (submit / cancel / close / move SL / startup sync). |
| `src/state/` | SQLite. `TradeDatabase` (trades + orders, user-scoped) and `UserDatabase` (users, encrypted creds, per-user config, invite codes, Telegram admins). |
| `src/crypto.py` | Fernet symmetric encryption for credentials at rest. |
| `src/telegram/` | Telegram bot: registration flow, menu UI, approval callbacks, trade notifications, admin commands, PnL + expiry background monitors. |
| `src/api/admin.py` | aiohttp REST API for user management and kill switch (port 8081, `X-API-Key` auth). |
| `src/health.py` | Zero-dependency async HTTP health endpoint (port 8080). |
| `src/utils/` | Structured logging (structlog → JSON), symbol mapping (Potion pair → Hyperliquid coin). |

---

## Quick Start

```bash
# Install
pip install -r requirements.txt
cp .env.example .env
cp config/config.example.yaml config/config.yaml

# Edit .env with your secrets (see below)
# Edit config/config.yaml with your input adapter and risk preferences

# Run
python main.py

# Run tests
python -m pytest tests/ -v
```

### `.env` secrets

```bash
# Hyperliquid (used by single-user fallback mode and admin operations)
HL_ACCOUNT_ADDRESS=0x_your_master_account_address
HL_API_WALLET=0x_your_api_wallet_address
HL_API_SECRET=0x_your_api_wallet_private_key

# Discord signal source
DISCORD_BOT_TOKEN=your_discord_bot_token

# Telegram bot (omit to run without Telegram)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_ADMIN_IDS=12345678,87654321

# Admin REST API
ADMIN_API_KEY=long_random_string
ADMIN_API_PORT=8081

# Credential encryption (auto-generated to data/.encryption_key if unset)
ENCRYPTION_KEY=base64_fernet_key
```

Hyperliquid uses a **master account + API wallet** model:
- **Master account** — owns the funds, used for all queries.
- **API wallet** — only signs transactions on behalf of the master account.

Per-user credentials submitted via Telegram registration are validated against Hyperliquid before being stored Fernet-encrypted in SQLite. The master `.env` credentials are only used in single-user fallback mode (when the user DB is empty).

---

## Telegram Bot

The Telegram bot is the user-facing interface. Users only interact with the bot — they never touch the admin API or config files.

### User onboarding

1. Admin runs `/generate_code 30` and shares the resulting `PPB-XXXX-XXXX` code with a paying member.
2. User sends `/register` to the bot in a DM.
3. Multi-step `ConversationHandler` collects: invite code → master account address → API wallet address → API private key → network. Credential messages are deleted on receipt.
4. Bot validates credentials against Hyperliquid, encrypts them, creates the user row, marks the code redeemed, and activates a pipeline for them.

### Main menu

`/menu` opens an inline-keyboard menu with screens:

| Screen | Purpose |
|--------|---------|
| **Account** | Masked wallet info, network, subscription status, expiry, code used, "Renew Access" button. |
| **Calls View** | Live feed of the last 10 signals (≤3 days old) as full signal cards. In manual-approve mode, each pending signal gets Approve / Reject buttons. The view auto-refreshes when new signals arrive. |
| **Trading** | Balance, open positions (with close buttons), active trades (paginated), trade history (last 15). |
| **Statistics** | Win rate, total / average / best / worst PnL %, wins / losses / breakeven counts. |
| **Dashboard** | Pipeline status, preset, leverage cap, risk limits, expiry. |
| **Configuration** | Change preset, toggle auto-execute, set max leverage, set risk limits, activate/deactivate the pipeline. |

### Approval flow

When `auto_execute` is **OFF**, new signals are recorded as `PENDING` and only surfaced as Approve/Reject buttons if the user is currently in Calls View (so push notifications never spam users who aren't actively monitoring). Approving rebuilds the order set from the stored trade and submits it. Rejecting marks the trade `CANCELED`.

When `auto_execute` is **ON**, signals are submitted immediately and users receive informational push notifications (no buttons).

### Push notifications

Per-user notifications fire on: new signal, trade opened, trade failed, TP hit, all TPs hit, stop hit, trade canceled, trade closed, SL moved to breakeven, SL adjusted, PnL alert (±5% / -3%), signal skipped, risk warning, access expiry warnings (3d / 1d), expired.

### Admin commands

| Command | Description |
|---------|-------------|
| `/generate_code [days]` / `/generate_codes <count> [days]` | Generate single or batch invite codes. |
| `/list_codes` / `/revoke_code <code>` | Inspect / revoke invite codes. |
| `/users` | List all registered users with status, preset, expiry. |
| `/extend <user_id> <days>` / `/revoke <user_id>` | Adjust user access. |
| `/add_admin <id>` / `/remove_admin <id>` / `/list_admins` | Manage who can run admin commands. |
| `/kill` / `/resume` | Emergency switch — cancels all orders, market-closes all positions across all users, blocks new signals until resumed. |
| `/broadcast <message>` | Send a message to all active users. |
| `/inject` | ⚠️ Testing only — inject synthetic signals into the pipeline using live testnet prices. |

DM-only and per-user rate limiting (30 commands / 60s) are enforced via pre-processing middleware.

---

## Admin REST API

`aiohttp` server on `ADMIN_API_PORT` (default 8081), authenticated via `X-API-Key`.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/users` | Create user (credentials + optional config). |
| `GET` | `/api/users` | List users (filter by `?status=`). |
| `GET` | `/api/users/{user_id}` | User detail (no secrets). |
| `PUT` | `/api/users/{user_id}` | Update display name / config / credentials. |
| `POST` | `/api/users/{user_id}/activate` | Mark active + spin up pipeline. |
| `POST` | `/api/users/{user_id}/deactivate` / `DELETE /api/users/{user_id}` | Mark inactive + tear down pipeline. |
| `POST` | `/api/kill` | Kill switch — close everything, block new signals. |
| `POST` | `/api/resume` | Resume signal processing after kill. |

---

## Configuration

Two sources of truth:
- `.env` — secrets only (HL credentials, Discord/Telegram tokens, admin API key, encryption key).
- `config/config.yaml` — non-secret defaults (input adapter, risk limits, logging, network). Per-user values override these from the `user_config` table.

### Strategy presets

A "strategy" is just three settings: `tp_split`, `move_sl_to_breakeven_after`, `size_pct`. Built-in presets:

| Preset | TP Split | SL to BE | Size % | Description |
|--------|----------|----------|--------|-------------|
| `runner` | 33 / 33 / 34 | After TP1 | 2% | Default — let winners run. |
| `conservative` | 100 / 0 / 0 | Never | 2% | Close everything at TP1. |
| `tp2_exit` | 50 / 50 / 0 | After TP1 | 2% | Exit fully at TP2. |
| `tp3_hold` | 0 / 0 / 100 | After TP1 | 2% | Hold everything for TP3. |
| `breakeven_filter` | 33 / 33 / 34 | After TP1 | 1.5% | Smaller size, same split. |
| `small_runner` | 33 / 33 / 34 | After TP1 | 0.5% | Minimal risk runner. |

Custom presets are defined in `config.yaml` and override built-ins with the same name. Each user can also override `size_pct` per risk level via `size_by_risk` (e.g. `LOW: 4.0`, `MEDIUM: 2.0`, `HIGH: 1.0`).

### Risk controls (enforced before every new trade)

| Guard | Config key | Default | What it does |
|-------|-----------|---------|--------------|
| Max open positions | `risk.max_open_positions` | 10 | Reject new trades when reached. |
| Daily loss circuit breaker | `risk.max_daily_loss_pct` | 10% | Stop trading when cumulative daily losses exceed threshold. |
| Total exposure cap | `risk.max_total_exposure_usd` | $2,000 | Cap combined USD across all open positions. |
| Max position size | `risk.max_position_size_usd` | $500 | Per-trade USD cap. |
| Min order value | `risk.min_order_usd` | $10 | Hyperliquid minimum notional. |
| Leverage cap | `strategy.max_leverage` | 20× | `min(signal_leverage, config_max, exchange_max)`. |

Per-user values override these defaults via the Telegram Configuration menu.

---

## Signal Handling

### Supported message types

| Type | Action |
|------|--------|
| `SIGNAL_ALERT` | Parse → size → build orders → submit (or record as pending if auto-execute is off). |
| `TP_HIT` | Notify + optionally move SL to breakeven. |
| `ALL_TP_HIT` | Mark trade closed with profit. |
| `BREAKEVEN` | Move SL to entry price. |
| `STOP_HIT` | Mark trade closed with loss. |
| `CANCELED` | Cancel orders, market-close any position. |
| `TRADE_CLOSED` | Market-close remaining position. |
| `PREPARATION` | Log only — do NOT execute (heads-up message). |
| `MANUAL_UPDATE` | Detect SL moves (`Move SL to 1985`, `SL → 0.025`, etc.), otherwise log. |
| `NOISE` | Ignore. |

### Symbol mapping

110+ pairs mapped from Potion Perps format to Hyperliquid coin names:

| Pattern | Example | Handling |
|---------|---------|----------|
| Direct 1:1 | `ETH/USDT` → `ETH` | Strip `/USDT`. |
| Kilo-prefix | `1000BONK/USDT` → `kBONK` | Convert `1000X` to `kX`. |
| Bare meme coins | `BONK/USDT` → `kBONK` | Explicit override. |
| Rebrands | `MATIC/USDT` → `POL`, `FTM/USDT` → `S` | Explicit override. |

Validated against live exchange metadata at runtime. Assets not listed on Hyperliquid are caught and rejected with a clear error.

### Startup position sync

On startup, every active user's pipeline reconciles its local DB state with the actual exchange:

| Scenario | Action |
|----------|--------|
| `OPEN` in DB, position exists on exchange | Verified — no change. |
| `OPEN` in DB, no position | Marked `CLOSED` (SL/TP filled while offline). |
| `PENDING` in DB, entry still resting | Verified — no change. |
| `PENDING` in DB, position exists | Promoted to `OPEN` (filled while offline). |
| `PENDING` in DB, no order / no position | Marked `CANCELED` (expired while offline). |
| Position on exchange, no DB record | Logged as orphan (never auto-managed). |

Conservative — only updates DB state, never auto-opens or auto-closes positions during sync.

---

## Multi-User Model

Every runtime component is instance-scoped — no globals.

| Component | Scope |
|-----------|-------|
| `HyperliquidClient` | Per-user credentials and SDK clients. |
| `Pipeline` | Per-user config, client, database. |
| `TradeDatabase` | All queries filtered by `user_id`; composite PK `(user_id, trade_id)`. |
| `PositionManager` | Per-user (carries client + DB). |
| `TelegramNotifier` | Per-user (carries chat ID + calls-view checker). |
| Parsers + order builder | Stateless pure functions. |
| Input adapter | Single shared instance — one signal source for everyone. |

Users share one SQLite file safely (WAL mode + composite keys). The orchestrator dispatches each incoming signal to every active user's pipeline in a try/except so one user's error never affects another.

---

## Database Schema

One SQLite file (`data/trades.db` by default). Tables:

**`trades`** — one row per signal
```
(user_id, trade_id) PRIMARY KEY
pair, coin, side, risk_level, trade_type, size_hint
entry_price, stop_loss, tp1, tp2, tp3
leverage, signal_leverage, position_size_usd, position_size_coin
status (preparing | pending | open | closed | canceled)
created_at, updated_at, closed_at, close_reason, pnl_pct, notes
```

**`orders`** — one row per exchange order
```
id PRIMARY KEY AUTOINCREMENT
trade_id, user_id  → FK to trades
order_type (entry | stop_loss | tp1 | tp2 | tp3)
coin, side, size, price, oid (Hyperliquid order ID)
status (pending | submitted | filled | canceled | rejected)
fill_price, created_at, updated_at
```

**`users`** — registered users
```
user_id PRIMARY KEY, display_name, status (active | inactive)
created_at, updated_at
```

**`user_credentials`** — Fernet-encrypted secrets
```
user_id PRIMARY KEY → FK users
account_address_enc, api_wallet_enc, api_secret_enc
network, created_at, updated_at
```

**`user_config`** — per-user overrides
```
user_id PRIMARY KEY → FK users
active_preset, auto_execute, max_leverage
size_by_risk_json, custom_presets_json
max_open_positions, max_daily_loss_pct,
max_position_size_usd, max_total_exposure_usd, min_order_usd
telegram_chat_id, invite_code, access_expires_at
created_at, updated_at
```

**`invite_codes`**
```
code PRIMARY KEY, created_by, created_at
duration_days, redeemed_by, redeemed_at, expires_at
status (active | redeemed | revoked | expired)
```

**`telegram_admins`** — dynamically added admins
```
telegram_id PRIMARY KEY, added_by, created_at
```

---

## Project Structure

```
potion-perps-bot/
├── main.py                              # Entry point — wires everything, runs the loop
├── Dockerfile, docker-compose.yml       # Container deployment
├── requirements.txt
├── config/
│   ├── config.example.yaml              # Template with comments
│   └── config.yaml                      # Active config (gitignored)
├── src/
│   ├── orchestrator.py                  # Multi-user fan-out
│   ├── pipeline.py                      # Per-user signal processor
│   ├── crypto.py                        # Fernet encryption
│   ├── health.py                        # Health endpoint
│   ├── api/
│   │   └── admin.py                     # Admin REST API
│   ├── config/
│   │   └── settings.py                  # YAML + .env loader, typed dataclasses, validation
│   ├── exchange/
│   │   ├── hyperliquid.py               # SDK wrapper + retries + rate-limit handling
│   │   ├── order_builder.py             # ParsedSignal → Hyperliquid orders
│   │   └── position_manager.py          # Submit / cancel / close / move SL / startup sync
│   ├── input/
│   │   ├── base_adapter.py              # Abstract interface
│   │   ├── discord_adapter.py           # Live Discord listener
│   │   ├── simulation_adapter.py        # Replay .txt files
│   │   ├── cli_adapter.py               # Paste from terminal
│   │   └── file_adapter.py              # (stub)
│   ├── parser/
│   │   ├── classifier.py                # 10 MessageType enum + classify()
│   │   ├── signal_parser.py             # TRADING SIGNAL ALERT → ParsedSignal
│   │   └── update_parser.py             # All lifecycle events → typed dataclasses
│   ├── state/
│   │   ├── models.py                    # TradeRecord, OrderRecord, enums
│   │   ├── database.py                  # SQLite trades + orders, user-scoped
│   │   └── user_db.py                   # Users, encrypted creds, config, invite codes
│   ├── strategy/
│   │   └── position_sizer.py            # Sizing + pre-trade risk gate
│   ├── telegram/
│   │   ├── bot.py                       # Application setup, handler registration
│   │   ├── keyboards.py                 # Inline keyboard builders
│   │   ├── formatters.py                # Message formatting helpers
│   │   ├── middleware.py                # Auth, admin check, DM-only, rate limit, error handler
│   │   ├── notifications.py             # TelegramNotifier — push trade events
│   │   ├── expiry_checker.py            # Hourly background task — expiry + warnings
│   │   ├── pnl_monitor.py               # 60s background task — PnL threshold alerts
│   │   ├── invite_codes.py              # Code generation
│   │   └── handlers/
│   │       ├── help.py                  # /start, /help, /cancel, unknown
│   │       ├── registration.py          # /register ConversationHandler
│   │       ├── menu.py                  # Main menu + screen routing
│   │       ├── account.py               # /balance, /positions, /status, /activate, /deactivate
│   │       ├── trades.py                # /trades, /history, /stats, notes, sub-views
│   │       ├── config.py                # /config, /preset, /auto, inline edits
│   │       ├── approval.py              # Approve / Reject / Close Position callbacks
│   │       └── admin.py                 # Invite codes, user management, kill, broadcast, /inject
│   └── utils/
│       ├── logger.py                    # structlog → JSON file + console
│       └── symbol_mapper.py             # Potion pair → Hyperliquid coin (110+ mappings)
├── tests/                               # ~370 tests across 24 files
└── signals/
    ├── samples/                         # Real Discord signal samples (all 10 types)
    └── test/                            # End-to-end test fixtures
```

---

## Testing

```bash
python -m pytest tests/ -v
```

~370 tests, all passing. Coverage by area:

| Area | Files |
|------|-------|
| Parsing & symbol mapping | `test_classifier.py`, `test_signal_parser.py`, `test_update_parser.py`, `test_symbol_mapper.py` |
| Strategy & risk | `test_risk_controls.py` |
| State & encryption | `test_user_db.py`, `test_crypto.py`, `test_invite_codes.py` |
| Exchange & retries | `test_retry.py` |
| Orchestrator & pipeline | `test_orchestrator.py`, `test_e2e_pipeline.py` (40 cases — full pipeline with mocked exchange) |
| Telegram | `test_notifications.py`, `test_approval.py`, `test_admin_commands.py`, `test_pnl_monitor.py`, `test_trade_notes.py`, `test_expiry_enforcement.py` |
| API & infra | `test_admin_api.py`, `test_health.py`, `test_logging.py`, `test_discord_adapter.py` |
| Hardening | `test_step12_hardening.py` |

---

## Docker Deployment

```bash
docker compose up -d --build
```

- Built on `python:3.12-slim` with a stdlib-based `HEALTHCHECK`.
- Mounts `config/`, `data/`, `logs/`, and `signals/incoming/` from the host.
- Exposes the health endpoint (`HEALTH_PORT`, default 8080) and admin REST API (`ADMIN_API_PORT`, default 8081).
- `restart: unless-stopped`, `stop_grace_period: 10s`.
- Logs rotate at 10 MB × 5 backups (configured in `src/utils/logger.py`).

The bot handles `SIGTERM` / `SIGINT` and shuts down components in order: PnL monitor → expiry checker → Telegram bot → admin API → health server → orchestrator (per-user cleanup) → user DB.

---

## Hyperliquid Integration Notes

Lessons baked into the code:

- **szDecimals**: Each asset has a fixed number of size decimal places (e.g. ETH=4, ADA=0, ZK=0). Sizes are **floored** (not rounded) to the per-asset precision from `get_asset_meta()`.
- **Price precision**: 5 significant figures, enforced via `_round_price()`.
- **Minimum notional**: $10 per order (size × mid price, not limit price).
- **Trigger orders**: `triggerPx` must be a float. SL/TP use `{"trigger": {"triggerPx": float, "isMarket": True, "tpsl": "sl" | "tp"}}`.
- **Portfolio margin**: USDC lives in the spot clearinghouse but is available for perps; `get_balance()` queries both.
- **maxLeverage**: Per-asset cap from metadata. Effective leverage is `min(signal_leverage, config_max_leverage, exchange_max_leverage)`.
- **Retries**: Transient errors and rate-limit responses (`429`, `"rate limit"`, `"too many requests"`) are retried with exponential backoff + jitter.

---

## Changelog

**2026-02-26 — Phase 4 complete**
- Full Telegram bot UI: menu, registration with invite codes, calls view with approve/reject, trading sub-views, statistics, dashboard, configuration, trade notes, user-initiated renewal.
- Per-user trade notifications, PnL threshold alerts (+5% / -3%), access expiry warnings (3d / 1d).
- Admin Telegram commands: invite codes, users, extend/revoke, kill switch, broadcast, dynamic admin management, test signal injection.
- Discord adapter wired to live channel. Multi-user orchestrator dispatches each signal to every active pipeline.
- Encrypted credential storage (Fernet) and admin REST API for user CRUD + kill switch.
- Hardening: DM-only filter, per-user rate limit, global error handler, retry on transient errors, structured JSON logging, log rotation.
- Mainnet registration intentionally blocked — testnet only for now.
- ~370 tests passing.

**2026-02-11 — Phase 2 complete, E2E verified**
- Strategy presets (6 built-in + user-defined), position sizing with risk-level overrides.
- Risk controls: daily loss circuit breaker, total exposure cap, consolidated risk gate.
- Position sync on startup reconciles DB state with exchange after restart.
- Symbol mapper: 110+ pairs, rebrands (MATIC→POL, FTM→S), kilo-prefix, validation.
- Dynamic SL adjustment from manual update messages.
- Full pipeline orchestrator with 10-type message handling and `auto_execute` support.
- Market close price rounding fixed for low-price assets.
- End-to-end testnet run verified: signal → orders → lifecycle → cleanup.

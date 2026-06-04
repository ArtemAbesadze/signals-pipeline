# Hyperliquid Integration — what we do, why, and what we've learned

A complete inventory of every Hyperliquid (HL) touchpoint in the bot,
organized by capability. Written as the source-of-truth reference for
exchange-related questions and as the spec we'll map onto Blofin during
Phase 6 migration.

**Audience**: future-Claude / future-Artem / Wuke / any collaborator
investigating how the bot interacts with HL.

**Structure**: each section answers four questions:

- **What we do** — actual function calls, code paths, file:line citations.
- **Why** — the design constraint that drove it.
- **Edge cases / gotchas** — every bug we've fixed in this area, with commit SHA.
- **Migration note** — what a comparable exchange (Blofin) must provide.

---

## 1. Authentication & credential model

### What we do

HL uses an unusual two-key model: a **master account address** owns the
funds, and a separate **API wallet** signs trades. The master never
exposes its private key to the bot.

| Field | Purpose | Where it's stored |
|---|---|---|
| `account_address` | Master 0x-address, used for queries (positions, balances). Read-only on the API wallet side. | `users.account_address_enc` (Fernet-encrypted) |
| `api_wallet` | API wallet's 0x-address — Hyperliquid uses this to identify the signer. | `users.api_wallet_enc` (Fernet-encrypted) |
| `api_secret` | API wallet's private key (0x-prefixed). Signs every order via EIP-712 (Ethereum-style typed-data signing). | `users.api_secret_enc` (Fernet-encrypted) |

Signing is **EIP-712 typed data**, performed by the HL SDK (via
`eth_account.Account.from_key(private_key)`). The bot never hand-rolls
the signature.

Credentials enter the system at `/register` (see
`src/telegram/handlers/registration.py`), pass through
`src.crypto.encrypt()` (Fernet symmetric, master key in
`data/.encryption_key`), and persist to `users.<field>_enc` columns.

The HL client is built at orchestrator activation
(`src/orchestrator.py:98`):

```python
client = HyperliquidClient(
    account_address=user_config.exchange.account_address,
    private_key=user_config.exchange.api_secret,
    network=user_config.exchange.network,
)
```

### Why

HL's API-wallet separation is a security feature: the master account
holds funds; the API wallet only has Trade permission delegated to it on
the HL side. Compromising the API key (= leaking the API wallet's
private key) lets an attacker trade with the master's funds **but not
withdraw them** — the master never signs withdrawals via the API.

### Edge cases / gotchas

- **API wallet authorization is separate from creation**. Generating a
  key locally (via `eth_account.Account.create()`) doesn't authorize it
  on HL; the master must explicitly authorize the API wallet's address
  through HL's UI. The bot's `/register` flow validates the master
  exists (via `get_account_state` — a read op) but **does not** validate
  that the API wallet is authorized to trade. That gets discovered at
  first-trade time. See CLAUDE.md polish backlog.
- **EIP-712 chainId binding**. The signed nonce binds to HL's chainId; a
  testnet API wallet cannot sign mainnet orders and vice versa. The
  network parameter is critical and per-user
  (`user_credentials.network`).
- **Encryption key loss = re-registration**. If `data/.encryption_key`
  is lost or rotated, every stored API wallet credential becomes
  unreadable. Trade history survives. Documented in README §
  Re-registering, scenario B.

### Migration note (Blofin)

Blofin uses **HMAC-SHA256 with passphrase**, not EIP-712. The credential
schema needs to grow a `passphrase` column (or store the secret + passphrase
as a JSON blob). API keys have a **90-day expiry if no IP is linked** —
no equivalent constraint on HL.

---

## 2. Connection lifecycle

### What we do

`HyperliquidClient` (`src/exchange/hyperliquid.py:64`) wraps the
official `hyperliquid-python-sdk` library, building two underlying
clients:

| SDK client | Purpose | Network |
|---|---|---|
| `hyperliquid.info.Info` | Read endpoints (positions, balances, mids, meta). No auth required for queries against an arbitrary address. | Same base URL as Exchange |
| `hyperliquid.exchange.Exchange` | Trade endpoints (order, cancel, leverage). Signs every call with the API wallet. | Same base URL as Info |

Base URLs are pulled from the SDK constants:

```python
NETWORK_URLS = {
    "testnet": TESTNET_API_URL,    # https://api.hyperliquid-testnet.xyz
    "mainnet": MAINNET_API_URL,    # https://api.hyperliquid.xyz
}
```

The client is built once per user pipeline at activation
(`src/orchestrator.py`). It survives Telegram-side config changes (preset,
risk limits, port) via the hot-reload path (`refresh_config`); only
credential or network changes require full
`deactivate_user + activate_user` (e.g. `/promote_to_mainnet`).

### Why

The SDK requires `skip_ws=True` when used outside an asyncio context;
the bot polls via `Info` rather than maintaining WebSocket subscriptions.
Polling is fine for our cadence (D10 reconciliation on cancel/close,
periodic balance checks).

### Edge cases / gotchas

- **SDK version pin**: `hyperliquid-python-sdk >= 0.23.0`. Earlier
  versions crash on `Info.__init__` for testnet. Pinned in commit
  `1d1a196`.
- **No WebSocket**. We rely entirely on polling for state updates. A
  signal that fires during a sleep window (laptop closed lid on
  battery) is lost — Telegram doesn't backfill `getUpdates` past its
  consume offset. See CLAUDE.md "laptop sleep" notes.

### Migration note (Blofin)

Blofin offers **both REST and WebSocket private channels**
(`positions`, `orders`, `account`). Stage 1 = REST parity. Stage 2 =
optional WS for position events; might let us drop some polling.

---

## 3. Asset metadata

### What we do

`HyperliquidClient.get_asset_meta()` (`src/exchange/hyperliquid.py:215`)
fetches HL's universe metadata once per process and caches it on the
instance:

```python
{
    "ETH":   {"szDecimals": 4, "maxLeverage": 25, ...},
    "BTC":   {"szDecimals": 5, "maxLeverage": 50, ...},
    "kBONK": {"szDecimals": 0, "maxLeverage": 20, ...},
    ...
}
```

Pulled from `self._info.meta()` — returns 200+ assets on testnet/mainnet.

The metadata is consumed at three points:

1. **Pipeline startup** (`src/pipeline.py:107`) — cached as
   `self._asset_meta` per user.
2. **Order construction** (`order_builder.build_orders`, line 134) —
   `szDecimals` floor for sizing, `maxLeverage` cap.
3. **Symbol mapping validation** (`utils/symbol_mapper.py:239`) — `if
   coin not in available_coins: raise ValueError`.

### Why

HL's size precision is per-asset (BTC = 5 decimals, ETH = 4, kBONK = 0
which means whole-coin sizing). Wire-format conversion errors if you
send a size with more decimals than the asset allows.

### Edge cases / gotchas

- **Cache is per-process** — restart drops it. The first request after
  restart pays an extra RTT.
- **`maxLeverage` is per-asset**. Most cap at 20–50x; only BTC/ETH go to
  100x. `build_orders` (line 137) caps signal leverage at
  `min(signal.leverage, user_config.max_leverage, asset_meta.maxLeverage)`.
- **`marginTableId`** field is also present in the response but we
  don't use it. Will matter if we ever do per-asset margin tier
  reasoning.
- **Tick size is NOT in `meta()`**. HL enforces "5 sig figs AND ≤ 6
  decimals" on prices but the cap isn't published per-asset; we
  hard-code it in `_round_price`. See § 14 → Bug #18.

### Migration note (Blofin)

`GET /api/v1/market/instruments` returns instrument metadata, likely
includes `tickSize` / `lotSize` directly per-asset. Worth using over a
hard-coded cap if reliably populated.

---

## 4. Order placement

### What we do

A "trade" on HL is composed of **5 individual orders** that the bot
submits sequentially via `PositionManager.submit_trade`
(`src/exchange/position_manager.py:245`):

| Order | Type | Details |
|---|---|---|
| Entry | Limit GTC | `{"limit": {"tif": "Gtc"}}` — rests until price hits limit |
| Stop-loss | Trigger market, reduce-only | `{"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}}` |
| TP1 | Trigger market, reduce-only | `tpsl: "tp"`, sized as `tp_split[0]` of entry |
| TP2 | Trigger market, reduce-only | `tpsl: "tp"`, sized as `tp_split[1]` |
| TP3 | Trigger market, reduce-only | Remainder of entry size |

Each `OrderParams` (defined `order_builder.py:60`) carries `coin`,
`is_buy`, `sz`, `limit_px`, `order_type`, `reduce_only`. The SDK call
takes positional args:

```python
self._client.exchange.order(
    coin, is_buy, size, limit_px,
    {"limit": {"tif": "Ioc"}},
    reduce_only=True,
)
```

Submission order matters:

1. Set leverage first (`update_leverage(coin, lev, is_cross=True)`).
2. Submit entry. If rejected, raise — no SL/TPs.
3. Submit SL.
4. Submit each TP whose size > 0.

Each order is recorded in the local `orders` table BEFORE the exchange
call. The oid (HL's order ID) is patched in after the response arrives.

### Why

The single combined "trade with attached SL/TP" form (`positionTpsl`)
exists in HL's SDK but only accepts 1 SL + 1 TP, not the
multi-target setup CP signals need. We emulate by submitting 5
individual orders.

Sizes are **floored**, not rounded, to avoid `float_to_wire` precision
errors on the wire. `_floor_to(size, szDecimals)` at line 32.

### Edge cases / gotchas

- **Minimum order value**: $10 (constant `MIN_ORDER_VALUE_USD` at line
  29). Smaller orders are rejected by HL with "Order has zero size" or
  similar. On testnet we bump up to `RiskConfig.testnet_position_floor_usd`
  (default $15) to clear this — see Phase 4.1, commit `d4d427e`.
- **TP split allocation**: last TP gets the remainder
  (`order_builder.py:200`) to ensure the three TP sizes sum exactly to
  the entry size. Without this, floor-rounding loses dust and leaves a
  residual position after all-TPs-hit.
- **TPs with `sz=0`** are skipped at submission (line 305). Happens for
  presets like `tp1_only` where TP2 and TP3 have `tp_split` of 0.
- **First-time submission per coin** may need leverage set even if it's
  the user's default. HL's per-coin leverage is sticky across orders
  but the first order on a new coin needs an explicit
  `update_leverage`.
- **Bug #18** (commit `e351b6f`): prices passed to `exchange.order`
  must round to ≤6 decimals AND ≤5 sig figs simultaneously, or HL
  rejects with "Order has invalid price". Especially relevant for low-
  priced coins (kBONK, kSHIB, kPEPE). See § 14.
- **Rejection ≠ error**: the SDK doesn't raise on rejection. It returns
  a response dict whose `statuses[0]` contains `"error"`. The bot
  parses this and raises `OrderSubmissionError`
  (`position_manager.py:280`). Easy to miss in new code paths.

### Migration note (Blofin)

Blofin has a **first-class TP/SL endpoint** (`POST /api/v1/trade/tpsl-order`)
and **batch order placement** (`POST /api/v1/trade/multiple-orders`).
Could submit all 5 orders in one call vs HL's sequential 5 RTTs.
Margin and position mode (`cross`/`isolated`, `one-way`/`hedge`) are
explicit on Blofin — we'll need to set them per-account.

---

## 5. Order cancellation

### What we do

Single-order cancel via the SDK:

```python
self._client.exchange.cancel(coin, oid)
```

Used in three paths:

- **`cancel_trade(trade_id)`** (`position_manager.py:310`) — walks all
  `SUBMITTED` orders for the trade and cancels each.
- **`move_stop_loss(trade_id, coin, new_price)`** (line 403) — cancels
  old SL, places new one at the new price.
- **`close_position`** (line 342) — cancels remaining orders before
  market-closing.

### Why

Trigger orders persist on HL until canceled, filled, or trigger price
is reached. After a trade closes (manually or via stop), the
unattached siblings must be canceled or they'll fire on the next
matching tick.

### Edge cases / gotchas

- **HL is idempotent on cancel** — already-canceled orders return OK,
  not error. We rely on this in the orphan-cleanup path
  (`scripts/test_driver.py::cancel_orphan_orders_on_hl`).
- **No batch cancel by trade_id** in the SDK we use. We loop. With ≤5
  orders per trade this is fine.
- **HL auto-cancels reduce-only siblings** when the position closes
  via SL/TP fill, but we don't get a per-order notification. Our
  pipeline reconciles by inferring from CP messages (commit `12354c1`,
  Bug #3+#8). See § 13.

### Migration note (Blofin)

`DELETE /api/v1/trade/order` for single, `DELETE /api/v1/trade/multiple-orders`
for batch. The batch-cancel-by-trade pattern is cleaner there.

---

## 6. Position queries

### What we do

`HyperliquidClient.get_open_positions()`
(`src/exchange/hyperliquid.py:186`) filters non-zero positions from
`get_account_state()`:

```python
[
    {
        "coin": "ETH",
        "size": 0.5,           # negative for SHORT
        "entry_price": "3000",
        "unrealized_pnl": "12.5",
        "leverage": {"type": "cross", "value": 10},
        "liquidation_price": "2800",
    },
    ...
]
```

**Size is signed**: positive = LONG, negative = SHORT. The bot relies
on this convention everywhere it interacts with positions (D10
checks, sync_positions, close_position).

Three call sites:

- `sync_positions` (line 122) — startup reconciliation.
- `_handle_canceled` (`src/pipeline.py:644`) — D10 source-of-truth
  check before deciding to market-close vs just cancel orders.
- `close_position` (line 345) — verify position exists before
  attempting to close.

### Why D10

CP lifecycle messages (TRADE_LIVE, STOP_HIT, etc.) drive the audit log
and reconcile the orders table, but they can be missed, delayed, or
wrong. NEAR/#2126 on 2026-05-25 had a silent entry fill with no
TRADE_LIVE from CP; the bot's local trade.status stayed PENDING; the
cancel handler trusted that and left the actual position uncovered on
HL. Codified as D10 in `docs/REWORK_BRIEF.md` and CLAUDE.md: HL is
authoritative for position state. (Commit `3ca1402`, Bug #11.)

### Edge cases / gotchas

- **`liquidationPx` may be `null`** for cross positions with low risk.
  Defensive parsing in any UI that surfaces it.
- **`leverage` field shape** differs from the simple integer we set —
  it's `{"type": "cross"|"isolated", "value": N}`. Don't compare to
  a plain int.

### Migration note (Blofin)

`GET /api/v1/trade/positions` — confirm sign convention; Blofin may
use a `side` field instead of signed size.

---

## 7. Order queries

### What we do

`HyperliquidClient.get_open_orders()`
(`src/exchange/hyperliquid.py:205`) returns all currently resting
orders for the master address:

```python
[
    {"oid": 123456, "coin": "ETH", "side": "B", "sz": "0.5", ...},
    ...
]
```

Used at startup by `sync_positions` (line 146) to build the
`resting_oids` set; orders in our DB that have an oid NOT in this set
are inferred to have filled or expired while the bot was offline.

### Why

Source of truth for "what orders does HL think we have right now."
Combined with `get_open_positions`, drives the full sync logic at
startup.

### Edge cases / gotchas

- **`side`** is a single character (`"B"` for buy, `"A"` for ask/sell).
  Different convention from our `OrderParams.is_buy`.
- **`oid`** is returned as a string in some response shapes, integer
  in others. We coerce with `int(o["oid"])` in `sync_positions:157`.

### Migration note (Blofin)

`GET /api/v1/trade/active-orders`. Verify `side` convention and oid
type.

---

## 8. Balance / wallet

### What we do — and why this is tricky on HL

HL operates under **portfolio margin**: a unified balance backs both
spot and perps. USDC sits in the **spot clearinghouse**; the perps
account just reports a `marginSummary.accountValue` that's often near
zero even when the user has plenty of buying power.

We discovered this on 2026-05-25 (Bug #9): the Trading menu showed
`Balance: $1.49` (perp account value) while $649 sat in spot USDC. Fix
in commit `6c0e661`: surface both numbers.

`HyperliquidClient.get_balance()` (`src/exchange/hyperliquid.py:159`)
returns a unified shape:

```python
{
    "usdc_balance":         "649.00",   # from spot_user_state (real buying power)
    "account_value":        "1.49",     # from marginSummary (perp side)
    "total_margin_used":    "0.50",
    "total_position_value": "20.00",
    "withdrawable":         "648.50",   # full account
}
```

The bot uses `usdc_balance` for the port-vs-wallet guardrail
(`pipeline.py:215`) and displays both `usdc_balance` and `account_value`
in the Trading menu (`format_trading_hub`).

### Edge cases / gotchas

- **`get_spot_balances`** (line 144) returns ALL non-zero spot tokens,
  not just USDC. We filter to USDC for the balance check.
- **`withdrawable`** is the headline number for "what you could
  withdraw RIGHT NOW" but isn't useful for our sizing math — we want
  the spot USDC.
- **Two RTTs per balance check**: perp `user_state` + spot
  `spot_user_state`. Cached implicitly only within a single function
  call; no cross-call caching.

### Migration note (Blofin)

`GET /api/v1/trade/account-balance` for futures balance,
`GET /api/v1/asset/balances` for full multi-currency view. Blofin's
margin model is explicit (cross vs isolated per-coin) — no
portfolio-margin quirk like HL.

---

## 9. Leverage management

### What we do

Per-coin leverage set before each entry via
`exchange.update_leverage(leverage, coin, is_cross=True)`
(called in `position_manager.py:263`).

Effective leverage is the min of three values
(`order_builder.py:138`):

```python
leverage = signal.leverage                              # from CP
leverage = min(leverage, user_config.max_leverage)      # user cap
leverage = min(leverage, asset_meta[coin].maxLeverage)  # exchange cap
```

### Why

- CP signals routinely call for 20–50x.
- Users default to `max_leverage=20` (configurable in Telegram).
- HL has per-coin maxes (BTC/ETH = 100x, most others 20–50x).

The three-way min ensures we never exceed any of them.

### Edge cases / gotchas

- **`is_cross=True` hard-coded**. We always use cross margin. Isolated
  is not exposed. Carried over implicit from initial implementation;
  no business case for isolated yet.
- **Leverage is sticky per-coin**. If we set 10x on ETH and later set
  20x on BTC, ETH stays at 10x. Each new coin in a session needs an
  explicit `update_leverage` call.
- **Leverage capping on the SIGNAL leverage in the test driver** —
  the driver's `_tp_profit_pct` uses `plan.leverage` (the signal's
  value, not the effective one). For CP, that's how CP itself reports
  percentages. Real position math uses effective leverage. See
  CLAUDE.md notes on Bug #18-adjacent percentage math.

### Migration note (Blofin)

`POST /api/v1/trade/leverage` for per-coin set,
`GET /api/v1/trade/multiple-leverage` to query several at once
(useful for /balance UI). Blofin also has a position mode setting
(one-way vs hedge) we don't have on HL.

---

## 10. Market data

### What we do

Only `get_all_mids()` is used in production
(`src/exchange/hyperliquid.py:210`):

```python
{"BTC": "50000.0", "ETH": "3000.0", "kBONK": "0.0054", ...}
```

Called by `close_position` (line 359) to compute the IOC limit
price (mid ± 3% per Bug #12).

### Why

Mid is the cheapest price reference for "close at market" — we wrap it
in a 3% spread to handle slippage and oracle drift without crossing
HL's oracle-distance check.

### Edge cases / gotchas

- **All-mids is the whole universe** — ~200 coins. Bandwidth is not a
  concern (~5KB JSON).
- **Mid ≠ tradable price** — bid-ask spread can be wide on testnet.
  Bug #12 (commit `bc80fbd`) tightened our close spread from 10% to
  3% after the 2026-05-25 NEAR position couldn't close at 10% under
  testnet oracle drift.
- We do NOT use mark price, index price, or funding rate endpoints.
  All available in the SDK but currently unused.

### Migration note (Blofin)

`GET /api/v1/market/tickers` for the bulk equivalent. Blofin also
exposes `mark-price` and `funding-rate` separately, useful if we want
to surface funding cost in the audit log.

---

## 11. Symbol mapping (Potion → HL)

### What we do

`src/utils/symbol_mapper.py:potion_to_hyperliquid()` (line 203). Three
resolution layers, in order:

1. **`_OVERRIDES`** (line ~50): explicit rebrands and naming
   differences:
   ```python
   "MATIC": "POL",       # Polygon rebrand
   "FTM":   "S",         # Fantom → Sonic
   "RNDR":  "RENDER",
   "SHIB":  "kSHIB",     # bare → kilo
   "PEPE":  "kPEPE",
   "BONK":  "kBONK",
   "LUNC":  "kLUNC",
   "NEIRO": "kNEIRO",
   "DOGS":  "kDOGS",
   "JELLYJELLY": "JELLY",
   ```
2. **`COMMON_PAIRS`** dict — known pair-to-coin lookups for the top
   ~100 perp coins. Includes coins NOT on HL with their HL equivalent
   left blank-or-missing so they fail validation cleanly.
3. **Kilo prefix rule**: `1000BONK/USDT` → `kBONK`. The `1000` prefix
   is CP's convention; HL uses `k`. (Line 232.)
4. **Fallback**: strip `/USDT`, use the base. `ZK/USDT` → `ZK`.

If `available_coins` is passed (from `get_asset_meta()`), validation
happens at the end: unknown coin → `ValueError`.

### Why

CP signals reference Binance-style symbols (`1000BONK/USDT`); HL uses
its own naming (`kBONK`). The map is built from real CP samples —
extended in commit `466d3d6` to 110+ entries with 62 tests.

### Edge cases / gotchas

- **Coins on CP but not on HL** (XRP for a while, others sporadically)
  fail at `available_coins` validation. We catch this in the test
  driver (`config.exclude_coins`) and document via the audit log
  (`event_type='error'`).
- **HL's listed-coin universe changes**. The hardcoded
  `HYPERLIQUID_COINS` set is a snapshot from 2025-05 (line ~75); rely
  on `available_coins` from the live `meta()` call rather than the
  hardcoded set when validating.

### Migration note (Blofin)

Symbol format on Blofin is likely `BTC-USDT` or `BTC-USDT-SWAP` (need
to verify). The mapping table needs a parallel `potion_to_blofin`
function. Many CP-known coins should map identically.

---

## 12. Response-parsing contracts

### What we do

HL responses are deeply nested dicts that follow this shape on success:

```python
{
    "status": "ok",
    "response": {
        "type": "order",
        "data": {
            "statuses": [
                {"resting": {"oid": 12345}},
                # OR {"filled": {"oid": 12345, "avgPx": "100.0", "totalSz": "0.5"}}
                # OR {"error": "<message>"}
            ]
        }
    }
}
```

Three helper functions in `position_manager.py`:

| Helper | Line | What it does |
|---|---|---|
| `_get_statuses(result)` | 55 | Safely extracts `result.response.data.statuses`, defaulting to `[]` if any nesting level is the wrong type. |
| `_extract_oid(result)` | 71 | Returns `oid` from either `resting.oid` or `filled.oid`, or `None`. |
| `_extract_fill(result)` | 84 | Returns the `filled` sub-dict (`{oid, avgPx, totalSz}`) or `None`. |
| `_get_error(result)` | 95 | Returns `statuses[0].error` string, or the raw response string if statuses are missing. |

Used to:
- Tell rest from fill on order submission.
- Detect errors (rejected orders).
- Verify HL actually filled a close before claiming success (Bug #13,
  commit `ca072ee`).

### Why

The SDK does NOT raise on exchange-side rejections. It returns the
response dict. Without these defensive helpers, a rejected order
looks indistinguishable from a successful one until something
downstream tries to query a fictional oid.

### Edge cases / gotchas

- **The exchange may return strings at any nesting level**. For
  example, a top-level auth failure returns `{"status": "err",
  "response": "Auth error"}` — `response` is a string. `_get_statuses`
  handles this by checking `isinstance(response, dict)`.
- **`statuses` is a list because batched orders return one entry per
  order**. We submit single orders so we always index `[0]`. If we
  ever batch, this needs revisiting.
- **`avgPx` and `totalSz` are strings, not floats** in the response.
  Cast with `float()`.
- **Idempotency of close**: per Bug #13, `close_position` now
  inspects the response with `_get_error` + `_extract_fill` and:
  - **Error present** → raise, don't mark CLOSED
  - **Fill present** → mark CLOSED with `avgPx`
  - **No error and no fill** (IOC didn't match) → raise, don't mark
    CLOSED. The position is still open and the caller must surface
    that.

### Migration note (Blofin)

Blofin uses standard REST `{code, msg, data}` shape (HTTP 200 with
`code` field). Different parsing helpers needed. Error path is more
conventional — `code != "0"` is the failure signal.

---

## 13. Lifecycle reconciliation

### What we do

Three reconciliation paths:

**Path 1: Startup sync** (`PositionManager.sync_positions`,
`position_manager.py:122`). Reconciles local DB ↔ HL state when the
bot starts up.

```
For each local OPEN trade:
    if HL has position for that coin   → mark verified
    else                                → mark CLOSED (sync_no_position)

For each local PENDING trade:
    if our entry-order oid in HL's resting_oids   → still pending
    elif HL has position for that coin            → entry filled while
                                                    offline → mark OPEN
    else                                          → entry expired → CANCELED

For each HL position with no local trade  → log as ORPHAN
```

**Path 2: CP-driven order reconciliation** (`pipeline.py`, commit
`12354c1`). When CP sends a lifecycle event, the relevant handler
updates the local `orders` table even though we don't get per-order
fill notifications from HL:

| Event | Handler effect on orders table |
|---|---|
| `TRADE_LIVE` | entry → FILLED; trade PENDING→OPEN if HL confirms the position (Bug #20) |
| `TP_HIT n` | TP{n} → FILLED |
| `BREAKEVEN` | move SL (cancel old, place new at entry price) |
| `ALL_TP_HIT` | all 3 TPs → FILLED, SL → CANCELED |
| `STOP_HIT` | SL → FILLED, all TPs → CANCELED |
| `CANCELED` | query HL position, if exists → close_position; else cancel_trade |

The fill price is approximated as the order's own target price (CP
doesn't expose exact fills). Idempotent against @PotionScannerBot's
duplicate-message pattern.

**Path 3: D10 source-of-truth check** (`pipeline.py:644`,
`_handle_canceled`, commit `3ca1402`). Before acting on a `CANCELED`
event, the handler queries HL directly:

```python
positions = self._client.get_open_positions()
has_position = any(
    p.get("coin") == trade.coin and float(p.get("size", 0)) != 0
    for p in positions
)
if has_position:
    self._pm.close_position(...)   # actual market close
else:
    self._pm.cancel_trade(...)     # just cancel resting orders
```

### Why D10

The 2026-05-25 NEAR/#2126 silent fill case: entry filled silently on
HL (no TRADE_LIVE from CP), local `trade.status` stayed PENDING,
cancel handler trusted that and only canceled resting orders — the
actual position stayed open on the exchange uncovered. Discovered two
days later by checking HL manually. Codified D10: HL is the
authoritative state for what's on the books; CP messages drive the
audit story but can be wrong.

### Edge cases / gotchas

- **Test driver creates "ghost positions"** by design. Synthetic
  `stop_hit`/`all_tp_hit` events mark orders FILLED locally even
  though no actual HL fills happen. The position remains on HL until
  manually closed or the test driver's `--cleanup` cancels the
  orphan orders. Documented in README § Testing.
- **Sync handles three cases**, not four. If a local PENDING has no
  oid (entry submission failed before we got a response), it stays
  PENDING forever until manually intervened. Rare but possible.
- **No fill-price API on HL for already-closed positions**. We
  approximate from CP's TP/SL target price. For real PnL audit, query
  HL's user_fills endpoint (not currently wired up).

### Migration note (Blofin)

Same three paths port over. Blofin's WebSocket private channels
(`positions`, `orders`) could replace the polling-based sync with
event-driven updates. Stage 2 work.

---

## 14. Known quirks / production bugs

These are HL-specific behaviors we've learned the hard way. Each is a
candidate "thing that might also be true on Blofin" or "thing Blofin
solves natively" — worth checking during migration.

### Bug #9: Portfolio margin vs perp account value (commit `6c0e661`)

`marginSummary.accountValue` near-zero while real buying power lives
in spot USDC. The Trading menu now surfaces both. § 8 above.

### Bug #11: D10 — HL is source of truth (commit `3ca1402`)

Cancel handler trusted local `trade.status=PENDING` and left a real
position uncovered. Now queries `get_open_positions` first. § 13 above.

### Bug #12: 3% close-spread cap (commit `bc80fbd`)

`CLOSE_LIMIT_SPREAD_PCT = 3.0` (`position_manager.py:32`). The
original 10% spread tripped HL's oracle-distance rejection on testnet
during the 2026-05-25 NEAR close. Sentinel test in
`tests/test_e2e_pipeline.py::TestClosePositionSpread` prevents quiet
re-broadening.

### Bug #13: Verify HL fill before claiming closed (commit `ca072ee`)

`close_position` previously called `update_trade_status(CLOSED)`
regardless of HL's response. Now inspects `_get_error` + `_extract_fill`.
Three branches: error → raise, fill → CLOSED, no-fill IOC → raise.
§ 12 above.

### Bug #18: Price precision cap (commit `e351b6f`)

HL perps enforce **both** 5 sig figs **and** ≤6 decimals
simultaneously. For coins under ~$0.01 (kBONK, kSHIB, kPEPE), 5 sig
figs naturally produces 7 decimals → "Order has invalid price".
`_round_price` in both `order_builder.py` and `position_manager.py`
applies sig-figs round THEN `round(..., 6)`. The two copies must
agree — sentinel test in `TestPricePrecisionForTightTickCoins`.

### Bug #20: Resting-entry trades stuck PENDING (commit `73b20e4`)

`TradeStatus.OPEN` was only ever written in two places, both in
`position_manager.py`: at submit time when the entry **fills
immediately** (line 292), and by startup `sync_positions` when HL shows
a position for a PENDING trade (line 209). No *live* lifecycle handler
promoted PENDING→OPEN. A resting limit entry that filled later (the
common case — CP sends `TRADE_LIVE`, which is not an immediate fill)
therefore stayed PENDING in the running pipeline. Every handler gated on
`status == OPEN` then silently no-opped: the breakeven-after-TP1 SL move
(`pipeline.py:571`), the standalone breakeven handler (`:650`), the D10
cancel/close routing (`:758`), and manual SL updates (`:910`).

`_handle_trade_live` had a comment claiming "we already know via the
exchange order-fill event" — but there is **no fill-polling mechanism**,
so the assumption was structurally false for resting entries.

Real case: ADA #2184 on the 2026-06-01 soak. Entry rested then filled;
`orders.entry=FILLED` (reconciled by `_mark_order_filled`) but
`trades.status` stuck at PENDING. TP1 hit → BE move skipped ("trade not
open") → the ADA short kept its original SL on HL instead of being moved
to entry. A faithfulness violation: the preset said move-SL-to-BE, the
bot silently didn't.

Fix: `_promote_to_open_if_filled` — D10-style, queries
`get_open_positions()` and promotes only if HL confirms the position;
on query failure it trusts CP's explicit fill confirmation and promotes
anyway (staying PENDING is the harmful outcome). Called from both
`_handle_trade_live` and `_handle_tp_hit` (a TP can't hit on an unfilled
entry). Sibling local-vs-exchange drift (#2200 OP, #2188 RENDER — local
`open` but gone from HL) is the *milder inverse*: those self-heal via
`sync_positions` on the next restart (`sync_no_position` → CLOSED).
Migration note: the same gap would exist on Blofin — `_handle_trade_live`
on the Blofin path must promote via `GET /api/v1/trade/positions`.

### Channel post visibility (test driver, commit `37a0454`)

Not strictly HL but adjacent: bots don't receive `channel_post`
updates for messages they themselves sent to a channel. The test
driver had to switch from posting via `TELEGRAM_BOT_TOKEN` to posting
via Telethon (Artem's user account). HL has no equivalent quirk but
the lesson generalizes: read the exchange's actual delivery semantics
before assuming.

### Testnet position floor (commit `d4d427e`)

On testnet, sub-min position sizes are bumped up to
`RiskConfig.testnet_position_floor_usd` (default $15) instead of
skipping. Lets us exercise the pipeline on accounts that don't have
the wallet size HL's `$10` real minimum requires for some coins
(after szDecimals flooring loses notional).

### API wallet authorization

`/register` validates the master account exists via
`get_account_state` (a read op). The API wallet's authorization-to-
trade gets validated at first-trade time, not at registration. Polish
backlog: do a no-op write check at registration (e.g.
`cancel_all_orders` on a coin with no orders) to surface
"API Wallet does not exist" earlier.

### Migration note (Blofin)

Bug #12 (oracle distance) is likely irrelevant on Blofin since the
dedicated `close-positions` endpoint handles the close, not an IOC
limit. Bug #18 (decimal cap) needs to be verified against Blofin's
`pxDecimals` from `meta`. Bug #9 (portfolio margin) doesn't apply —
Blofin's futures balance is direct.

---

## 15. Rate limits & error handling

### What we do

`retry_on_transient(max_retries=3, base_delay=1.0)` decorator
(`src/exchange/hyperliquid.py:20`) wraps every public method on
`HyperliquidClient`. It catches:

- `(ConnectionError, TimeoutError, OSError)` — network transients.
- Any exception whose `str(e).lower()` contains "rate limit", "too
  many requests", or "429" — HL's rate-limit responses.

Retries with exponential backoff + jitter: `base_delay * 2^attempt +
random.uniform(0, 0.5)`. Other exceptions re-raise immediately —
e.g. bad creds, invalid signature, exchange-side rejection.

### Why

HL's REST endpoints are robust but the SDK occasionally throws on
unstable connections. Position queries and balance checks are
read-only and idempotent; retry-on-transient is safe. The SDK is
NOT safe for retry on the exchange-mutating side
(`exchange.order` could double-submit), so the retry decorator is
applied only to the Info-side methods — submission goes through a
separate `_submit_order` (`position_manager.py:452`) with
`retry_on_transient(max_retries=2, base_delay=0.5)` (tighter caps).

### Edge cases / gotchas

- **No documented per-endpoint rate limits** on HL — we've never hit
  them in practice with our cadence (<1 RPS).
- **Retry on order submission is a calculated risk**. With
  `max_retries=2` and idempotent order builder, double-submit risk is
  low but non-zero. Watch for duplicate orders in the orders table if
  this ever causes pain.
- **Catching the `last_exc`**: if all retries fail, the last exception
  is re-raised at `wrapper:54`. Marked `# pragma: no cover` — never
  actually fires in tests.

### Migration note (Blofin)

Blofin publishes rate limits: **500 req/min by IP** for general
endpoints, **30 req/10s for trading endpoints**. With 3 users and our
polling pattern, fine. But the per-trading-endpoint cap (30/10s) is
tight enough to be worth thinking about during a fast CP signal day —
3 users × 5 orders = 15 submissions inside 10s if multiple signals
fire simultaneously. Worth instrumenting.

---

## Quick reference — every HL surface, in one table

| What | Function | File:Line | Surface |
|---|---|---|---|
| Account state | `get_account_state` | `hyperliquid.py:139` | `info.user_state(addr)` |
| Spot balances | `get_spot_balances` | `hyperliquid.py:144` | `info.spot_user_state(addr)` |
| Unified balance | `get_balance` | `hyperliquid.py:159` | Composes the two above |
| Positions | `get_open_positions` | `hyperliquid.py:186` | Filters non-zero from `get_account_state` |
| Orders | `get_open_orders` | `hyperliquid.py:205` | `info.open_orders(addr)` |
| Mids | `get_all_mids` | `hyperliquid.py:210` | `info.all_mids()` |
| Asset metadata | `get_asset_meta` | `hyperliquid.py:215` | `info.meta()` (cached) |
| Place order | `submit_trade` → `_submit_order` | `position_manager.py:245, 452` | `exchange.order(...)` |
| Cancel order | `cancel_trade` / `move_stop_loss` | `position_manager.py:316, 419` | `exchange.cancel(coin, oid)` |
| Set leverage | `submit_trade` | `position_manager.py:263` | `exchange.update_leverage(...)` |
| Market close | `close_position` | `position_manager.py:324` | IOC limit at mid ± 3% |
| Sync state | `sync_positions` | `position_manager.py:122` | Reconciles DB ↔ HL on startup |

---

## What this doc enables for the Blofin migration

`BLOFIN_INTEGRATION.md` (to be written) mirrors these 15 sections,
section-by-section answering:

1. **What Blofin offers** — endpoint and request/response shape.
2. **Delta from HL** — what changes (everything in § 1 auth, several
   patterns in § 12 response parsing).
3. **New capability** — anything Blofin offers that HL doesn't (e.g.
   native TP/SL order type, batch order placement, market-close
   endpoint, WebSocket private channels).
4. **Migration note** — exactly what code changes for that section.

The two docs together become the spec for the `BlofinClient` rewrite
(Phase 6).

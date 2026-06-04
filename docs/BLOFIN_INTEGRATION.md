# Blofin Integration — spec for the Phase 6 migration

A mirror of [`HYPERLIQUID_INTEGRATION.md`](HYPERLIQUID_INTEGRATION.md):
same 15 capability sections, but answered from Blofin's perspective.
This is the spec the `BlofinClient` rewrite will be built against.

**Audience**: future-Claude / future-Artem / Wuke implementing the
Blofin transport, plus anyone debugging Blofin-related behavior.

**Sources**: Blofin's public API docs at <https://docs.blofin.com>,
fetched 2026-06-04. Cross-referenced against the HL doc to call out
deltas, new capabilities, and gotchas we'll need to handle in code.

**Structure**: each section answers four questions:

- **What Blofin offers** — endpoint, request/response shape, exact field names.
- **Delta from HL** — what's different from how we do it today.
- **New capability** — anything Blofin offers that HL doesn't.
- **Migration note** — exactly what code changes for that section.

A separate [§ Demo Trading](#demo-trading-our-replacement-for-hl-testnet)
section at the end documents how the demo environment works — we will
use it instead of HL testnet for all dev / testing.

---

## 1. Authentication & credential model

### What Blofin offers

HMAC-SHA256 with five headers on every authenticated request:

| Header | Value |
|---|---|
| `ACCESS-KEY` | API key string |
| `ACCESS-SIGN` | Base64(HMAC-SHA256(secret, prehash).hexdigest().encode()) |
| `ACCESS-TIMESTAMP` | Unix epoch in **milliseconds** (e.g. `1597026383085`) |
| `ACCESS-NONCE` | Unique-per-request UUID/snowflake (server-deduplicated) |
| `ACCESS-PASSPHRASE` | The passphrase chosen at API key creation |

**Prehash string format** (exact order, no separators):
```
{requestPath}{method}{timestamp}{nonce}{body}
```

Where:
- `requestPath` includes query string for GET (e.g. `/api/v1/asset/balances?accountType=futures`)
- `method` is UPPERCASE (`GET`, `POST`, `DELETE`)
- `body` is the JSON-stringified request body for POST, `""` (empty string) for GET
- JSON body **must not contain extra spaces** — `json.dumps()` without `indent` or extra separators

**Signing quirk**: after the HMAC, the docs explicitly say "convert to
hexadecimal first, THEN convert the hex STRING to bytes (not hex2bytes)
before base64-encoding." This is unusual — most exchanges base64 the
raw HMAC bytes, not the hex digest. **Easy to get wrong.**

```python
# From the docs, verbatim
def create_signature_blofin(secret_key, nonce, method, timestamp, path, body=None):
    if body:
        prehash_string = f"{path}{method}{timestamp}{nonce}{json.dumps(body)}"
    else:
        prehash_string = f"{path}{method}{timestamp}{nonce}"

    signature = hmac.new(secret_key.encode(), prehash_string.encode(), hashlib.sha256)
    hexdigest = signature.hexdigest()
    hexdigest_to_bytes = hexdigest.encode()   # ← the "string2bytes" step
    base64_encoded = base64.b64encode(hexdigest_to_bytes).decode()
    return base64_encoded
```

**Request expiry**: a signed request expires 1 minute after its
timestamp. We need NTP-synced clocks on whatever box runs the bot.

**API key permissions** (chosen at creation):

| Permission | What it grants | Our bot needs |
|---|---|---|
| Read | Account info, bills, order history | ✅ (mandatory) |
| Trade | Place + cancel orders, set leverage, set modes | ✅ |
| Transfer | Move funds between sub-accounts | ❌ (security boundary) |
| Withdraw | Pull funds off the exchange | ❌ (security boundary) |

**API Key Usage** (radio button on key creation, see screenshot):

| Usage | Purpose | Our bot needs |
|---|---|---|
| Connect to Third-Party Applications | Read-only data sharing (Coinstats etc.) | ❌ |
| **API Transaction** | **Programmatic trading via REST/WS** | ✅ **— this is what we apply for** |
| Tax Report API | Read-only historical for tax tools | ❌ |
| BloFin MCP | AI-assistant integration via MCP protocol | ❌ |

**IP whitelist + expiry**: optional, up to 20 IPs per key. Keys **without
an IP linked expire after 90 days**. With an IP linked, no expiry.

**Error code on bad signature**: `60009` ("Login failed").

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Signing | EIP-712 typed-data (Ethereum) | HMAC-SHA256 + base64-of-hex |
| Identity | Master address + delegated API wallet | Single API key + passphrase |
| Credentials count | 3 (account, api_wallet, api_secret) | 3 (api_key, api_secret, passphrase) — same column count |
| Network binding | Implicit (chainId in signature) | Explicit base URL |
| Key expiry | None | 90 days unless IP-linked |
| Multi-IP | N/A | Up to 20 |
| Clock skew tolerance | None documented | 1 minute |

### New capability

- **IP whitelisting** is a real defense-in-depth layer we don't have on
  HL. On a VPS with a static IP we should use it.

### Migration note

- **Schema change**: `user_credentials` table needs a `passphrase_enc`
  column. The existing `account_address_enc` / `api_wallet_enc` /
  `api_secret_enc` columns map naturally to `api_key_enc` /
  `passphrase_enc` / `api_secret_enc` — but the meaning changes. Better
  to migrate to an exchange-agnostic schema:
  ```sql
  ALTER TABLE user_credentials ADD COLUMN exchange TEXT NOT NULL DEFAULT 'hyperliquid';
  ALTER TABLE user_credentials ADD COLUMN passphrase_enc TEXT;
  -- account_address_enc → repurpose as "primary_id_enc" (HL master | Blofin api_key)
  -- api_wallet_enc      → repurpose as "secondary_id_enc" (HL api_wallet | Blofin api_key duplicate)
  -- api_secret_enc      → stays (HL private key | Blofin api_secret)
  ```
- **NTP**: ensure the laptop / VPS time is NTP-synced. macOS does this
  by default; Linux VPS will need `systemd-timesyncd` or `chrony`.
- **The base64-of-hex quirk**: implement carefully and **test against
  Blofin's own example signature** (the docs include one) before going
  live. Off-by-one signature bugs are silent — they look like 60009
  Login failed and could be misdiagnosed as bad credentials.

---

## 2. Connection lifecycle

### What Blofin offers

Five base URLs, by environment + protocol:

| Environment | REST | Public WS | Private WS |
|---|---|---|---|
| **Production** | `https://openapi.blofin.com` | `wss://openapi.blofin.com/ws/public` | `wss://openapi.blofin.com/ws/private` |
| **Demo Trading** | `https://demo-trading-openapi.blofin.com` | `wss://demo-trading-openapi.blofin.com/ws/public` | `wss://demo-trading-openapi.blofin.com/ws/private` |

There is also a Copy Trading private WS endpoint
(`wss://openapi.blofin.com/ws/copytrading/private`) — not in scope for us.

**No official Python SDK ships from Blofin** (unlike HL's
`hyperliquid-python-sdk`). The docs include Python code samples but
the bot will need to hand-roll the HTTP client. `requests` for REST,
`websockets` library for WS if we go that route.

**Connection patterns**:
- REST: standard request/response. We instantiate a small `BlofinClient`
  class and call methods. Same shape as `HyperliquidClient`.
- WebSocket: persistent connection per private channel set. Login
  message after connect, then `subscribe` ops. Auto-disconnect after
  30s of no data — must implement ping/pong heartbeat.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Official SDK | Yes (`hyperliquid-python-sdk`) | **No** — hand-roll the client |
| Real-time channel | We only use polling | Available, optional |
| Base URL count | 2 (testnet/mainnet) | 2 REST + 4 WS (env × public/private) |
| Heartbeat | N/A | **Required** — 30s auto-disconnect |

### New capability

- **WebSocket private channels** (`positions`, `orders`, `account`,
  `algo-orders`) — could replace our polling-based position checks
  with push-based events. Stage 2 work; Stage 1 = REST parity.

### Migration note

- **No SDK** = `BlofinClient` is a hand-rolled HTTP wrapper. ~150–200
  LOC vs HL's ~230 LOC wrapping the SDK. Not significantly more code.
- **Add `aiohttp`** (already a transitive dep via PTB) or stay
  synchronous with `requests`. Synchronous is fine for our cadence;
  WebSocket needs async.
- **Heartbeat**: if we go WS, implement a 20s ping timer. Reconnect on
  any failure with exponential backoff (existing `retry_on_transient`
  pattern carries over).

---

## 3. Asset metadata

### What Blofin offers

`GET /api/v1/market/instruments` — single source for everything we
need at order-construction time:

```json
{
  "code": "0",
  "msg": "success",
  "data": [{
    "instId":         "BTC-USDT",
    "baseCurrency":   "BTC",
    "quoteCurrency":  "USDT",
    "contractValue":  "0.001",        // 0.001 BTC per contract
    "listTime":       "1597026383085",
    "expireTime":     "",
    "maxLeverage":    "100",
    "minSize":        "0.1",          // minimum order in contracts
    "lotSize":        "0.1",          // size increment
    "tickSize":       "0.5",          // price increment
    "instType":       "SWAP",
    "contractType":   "linear",
    "maxLimitSize":   "...",
    "maxMarketSize":  "...",
    "state":          "live",
    "settleCurrency": "USDT"
  }, ...]
}
```

**Key fields**:
- `tickSize` — exact price increment. **Replaces HL's empirical
  "5 sig figs + 6 decimals" rule.**
- `lotSize` + `minSize` — exact size increment + minimum. Replaces
  HL's per-coin `szDecimals` + global `$10 minimum`.
- `contractValue` — for some coins, 1 contract ≠ 1 unit of the
  underlying. BTC-USDT has `0.001` — 1 contract = 0.001 BTC.
- `maxLeverage` — per-asset cap, same concept as HL.
- `state` = `"live"` or `"suspend"` — filter at startup.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Precision spec | Empirical (we coded our own) | `tickSize` published per-asset |
| Size precision | `szDecimals` (decimal places) | `lotSize` (increment value) |
| Min order | Global $10 | Per-asset `minSize` in contracts |
| Contract value | 1:1 (1 ETH = 1 ETH worth of position) | **Variable** — BTC contract = 0.001 BTC |
| Cache strategy | We cache on instance (HL never invalidates) | Same — fetch once at startup |

### New capability

- **Published `tickSize`** — Bug #18 (price precision) goes away as a
  hard-coded rule. We round to the nearest `tickSize` multiple per-coin.
- **`maxLimitSize` / `maxMarketSize`** — published per-asset.
  Lets us pre-validate huge orders before submitting.
- **Funding-rate visibility** via `funding-rate` endpoint (see § 10).

### Migration note

- **`order_builder` needs a small refactor**:
  - Drop the `_round_price(price, sig_figs=5, max_decimals=6)` helper.
  - Replace with `_round_to_tick(price, tickSize)` per-asset.
  - Replace `_floor_to(size, szDecimals)` with
    `_floor_to_lot(size, lotSize)` (semantically same, different
    units — work in contracts, not coin amounts).
  - **Contract-value math**: `size_in_contracts = position_size_usd /
    (entry_price * contract_value)`. The extra `contract_value`
    factor is a new failure mode if forgotten — write a test that
    pins BTC: `$50 at $50000 entry = 1 contract` (= 0.001 BTC).
- **Bug #18 sentinel test** (the 5-sig-figs + 6-decimals sentinel)
  becomes irrelevant for Blofin. Don't delete it — keep it for the HL
  fallback path. Add a parallel `TestBlofinTickRounding` for the
  Blofin path.

---

## 4. Order placement

### What Blofin offers

Three relevant endpoints:

#### `POST /api/v1/trade/order` — single order

```json
{
  "instId":        "BTC-USDT",
  "marginMode":    "cross",          // "cross" | "isolated"
  "side":          "buy",            // "buy" | "sell"
  "orderType":     "limit",          // "limit" | "market" | "post_only" | "fok" | "ioc"
  "price":         "50000.0",        // required for limit-type
  "size":          "0.1",            // in contracts
  "clientOrderId": "potion_42_entry", // optional, our own ID
  "reduceOnly":    false,
  "positionSide":  "net"             // "net" (one-way) | "long" | "short" (hedge)
}
```

Response:
```json
{
  "code": "0",
  "msg":  "success",
  "data": { "orderId": "...", "clientOrderId": "potion_42_entry" }
}
```

#### `POST /api/v1/trade/multiple-orders` — batch

Array of order objects matching the single-order schema. Max batch
size **not documented in the public docs** — verify empirically (most
exchanges cap at 10–20).

#### `POST /api/v1/trade/tpsl-order` — dedicated TP/SL

First-class TP/SL order type. Schema details not fully exposed in
public docs — will need to read the live docs page or test against
demo. The key point: **TP and SL are attached to a position natively;
we don't have to emulate via reduce-only limit orders**.

Other endpoints we won't use initially: `POST /api/v1/trade/algo-order`
(conditional execution beyond TP/SL).

**clientOrderId**: optional but **strongly recommended** — lets us tie
Blofin's response back to our `orders` table without depending on the
returned `orderId`. We currently use HL's oid as the link; switching
to a clientOrderId scheme on Blofin is cleaner because we can stamp
the trade_id + order_type into the ID.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Orders per trade | 5 sequential (entry + SL + 3 TPs as separate calls) | Could be 1 entry + 1 tpsl-order, or 5 batched in one call |
| TP/SL representation | Reduce-only trigger-market emulation | Native TP/SL endpoint |
| Wire format | EIP-712 signed dict | JSON + HMAC headers |
| Client-side ID | None — we depend on HL's oid | `clientOrderId` field — round-trippable |
| Order type vocabulary | `{"limit": {"tif": "Gtc"}}` nested | Flat `"orderType": "limit"`, `"tif"` only optional for some |
| `reduceOnly` | Per-order field | Per-order field (same) |

### New capability

- **Batch order placement** — submit entry + SL + 3 TPs in one HTTP
  call. Atomic from the user's perspective (though Blofin may process
  them sequentially server-side). Halves the worst-case submission
  time and reduces partial-failure modes.
- **Native TP/SL** — `tpsl-order` is the "right" way to attach exit
  orders; current HL code constructs reduce-only limit orders with
  trigger conditions, which is the workaround for HL's lack of a
  combined TP/SL primitive.
- **`clientOrderId`** — pure win. Lets us decouple local-DB tracking
  from exchange-assigned IDs.

### Migration note

- **`build_orders` returns a different shape** for Blofin. Either:
  - Keep `TradeOrderSet` as a neutral container and let
    `BlofinClient.submit_trade` know how to dispatch (single batch
    call vs sequential). OR
  - Add a per-exchange order-set type. Cleaner long-term but more
    refactoring.
- **TP/SL design choice**: native `tpsl-order` vs reduce-only emulation.
  Native is cleaner but changes how reconciliation works (the
  orders table needs to distinguish "real" orders from "tpsl plans").
  **Recommended: use native** — Blofin reports TP/SL fills the same
  way HL does (in CP messages); the audit trail stays identical.
- **`clientOrderId` scheme**: `potion_{trade_id}_{order_type}`, e.g.
  `potion_2127_entry`, `potion_2127_sl`, `potion_2127_tp1`. Length
  limit: verify Blofin's max — probably 32 chars.
- **Order rejection signal**: check `response.code != "0"`, not
  `statuses[0].error` like HL.

---

## 5. Order cancellation

### What Blofin offers

- `DELETE /api/v1/trade/order` — single cancel by `orderId` or
  `clientOrderId`
- `DELETE /api/v1/trade/multiple-orders` — batch
- `DELETE /api/v1/trade/tpsl-order` — for TP/SL orders specifically
  (since they're tracked separately)
- `DELETE /api/v1/trade/algo-order` — algo orders

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Single cancel | `exchange.cancel(coin, oid)` | DELETE with `{orderId, instId}` |
| Batch cancel | Not exposed in our wrapping | First-class endpoint |
| Cancel by client ID | N/A | Yes — pass `clientOrderId` instead of `orderId` |
| Idempotency | Already-canceled returns OK | TBD — verify on demo |

### New capability

- **Cancel-by-clientOrderId** simplifies our orphan-cleanup logic.
  Current `--cleanup` queries the local DB for orphan oids and groups
  by user; with `clientOrderId` we can predict-and-cancel
  `potion_7000005_sl` directly.
- **Batch cancel by `instId`** — could cancel all open ETH orders in
  one call. Useful if we ever do trade-wide stop-out.

### Migration note

- **`cancel_trade` simplifies**: send a batch-cancel request listing
  all SUBMITTED orders for the trade in one call. Or use the
  cancel-by-instId batch endpoint if we keep the bot strictly
  one-trade-per-coin at a time (CP convention).
- **Already-canceled handling**: assume idempotent until proven
  otherwise. If it returns an error, retry the cleanup logic to
  tolerate it.

---

## 6. Position queries

### What Blofin offers

`GET /api/v1/trade/positions` — currently-open positions:

```json
{
  "code": "0", "msg": "success",
  "data": [{
    "instId":           "BTC-USDT",
    "side":             "long",            // "long" | "short" — UNSIGNED size
    "size":             "0.5",             // contracts
    "entryPrice":       "50000",
    "markPrice":        "50100",
    "liquidationPrice": "45000",
    "unrealizedPnL":    "5.0",
    "leverage":         "10",
    "marginMode":       "cross",
    "positionMode":     "one-way"
  }]
}
```

**Critical difference from HL**: Blofin uses `side` (string) +
`size` (unsigned) instead of HL's signed `size`. The bot's D10
check, which currently looks at `float(p.get("size", 0)) != 0`,
needs to also branch on the `side` field if we care about direction.

Filters available: `instId`, `marginMode`, `positionMode`.

Also: `GET /api/v1/trade/positions-history` for historical/closed
positions (paginated, default 100).

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Direction | Signed size (`-150` = short 150) | `side: "long"\|"short"` + unsigned `size` |
| Pagination | N/A (live positions only) | History endpoint paginates |
| Field names | `szi`, `entryPx`, `unrealizedPnl` | `size`, `entryPrice`, `unrealizedPnL` |

### New capability

- **`positions-history`** is a real audit improvement over HL.
  Currently we depend on our own `trades` table for closed-trade
  history; Blofin gives us the exchange's view independently. Useful
  for D10 audits.

### Migration note

- **`get_open_positions` returns the same canonical shape we have
  today.** Internally normalize:
  ```python
  signed_size = float(p["size"]) * (1 if p["side"] == "long" else -1)
  ```
- **D10 source-of-truth check** stays — the helper function just gets
  a different shape input.

---

## 7. Order queries

### What Blofin offers

- `GET /api/v1/trade/active-orders` — resting limit orders
- `GET /api/v1/trade/active-tpsl-orders` — resting TP/SL plans
- `GET /api/v1/trade/active-algo-orders` — algo orders
- `GET /api/v1/trade/order` — single order details
- `GET /api/v1/trade/order-history` — historical orders (paginated)
- `GET /api/v1/trade/tpsl-order-history` — TP/SL history
- `GET /api/v1/trade/trade-history` — actual fills (not orders)

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Active orders | One endpoint (`get_open_orders`) | **Three** — orders / tpsl / algo separately |
| Fill history | Not currently wired | Available via `trade-history` |
| Pagination | N/A | Standard before/after/limit |

### New capability

- **`trade-history`** gives us actual fill prices (vs our current
  CP-target-price approximation). Real PnL calculations get cleaner.
- **Separate TP/SL listing** — easier to query "is this trade's SL
  still active?" than digging through a mixed orders list.

### Migration note

- **Sync logic must query all three**: regular orders + TP/SL +
  algo. Combine into a single set of resting IDs for the sync compare.
- **Consider wiring `trade-history`** as a Phase 6+ enhancement: when
  CP says "TP1 hit", look up the actual fill from `trade-history` and
  store it as `orders.fill_price` instead of the approximation.

---

## 8. Balance / wallet

### What Blofin offers

Two relevant endpoints:

#### `GET /api/v1/asset/balances?accountType=futures`

Full account balance, multi-currency. `accountType` values:
`funding`, `futures`, `copy_trading`, `earn`, `spot`, `inverse_contract`.

Response per currency:
```json
{
  "currency":  "USDT",
  "balance":   "10000.0",     // total holdings
  "available": "9800.0",      // available for trading or withdrawal
  "frozen":    "200.0",       // locked in open orders / positions
  "bonus":     "0"            // promotional credits
}
```

#### `GET /api/v1/trade/account-balance` — futures-specific

Returns margin requirements, available equity, and unrealized P&L.
Exact field list not fully exposed in the public docs — verify on demo.

Plus `GET /api/v1/account/config` for the account-level config
(unified vs segregated, position mode default).

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Wallet model | Portfolio margin (spot USDC backs perps) | Segregated by default (`accountType=futures`) |
| Bug #9 (spot vs perp confusion) | Real | **Doesn't apply** — futures balance is direct |
| Multi-currency | Single USDC under PM | Native per-currency view |
| Available vs frozen | Combined `withdrawable` | Explicit `available` + `frozen` split |

### New capability

- **Clearer balance semantics** — `available` is the number we use for
  the port-vs-wallet guardrail; `frozen` shows how much is locked in
  open positions + orders. No more spot/perp dance.
- **Multi-currency** — could matter if a user holds margin in
  something other than USDT (BTC, ETH). For our scope: USDT-only.

### Migration note

- **`get_balance`** dramatically simplifies:
  ```python
  def get_balance(self) -> dict[str, str]:
      raw = self._get("/api/v1/asset/balances", {"accountType": "futures", "currency": "USDT"})
      bal = raw["data"][0]
      return {
          "usdt_balance":  bal["balance"],     # rename from "usdc"
          "available":     bal["available"],
          "frozen":        bal["frozen"],
          "bonus":         bal["bonus"],
      }
  ```
- **Bug #9 fix** (Trading menu showing perp value as "Balance") becomes
  irrelevant. The dual-display can be simplified to just `available`
  but **keep the dual surface** for symmetry across exchanges —
  `format_trading_hub` is shared code.
- **The /port command's guardrail math** uses `available` instead of
  spot USDC. Same semantics.

---

## 9. Leverage management

### What Blofin offers

- `POST /api/v1/trade/leverage` — set per-instrument:
  ```json
  {"instId": "BTC-USDT", "leverage": "10", "marginMode": "cross", "positionSide": "net"}
  ```
- `GET /api/v1/trade/leverage` — current setting for one instrument
- `GET /api/v1/trade/multiple-leverage` — bulk fetch with `instIds`
  array

Plus two new account-level toggles:

- `POST /api/v1/trade/margin-mode` — set `cross` | `isolated`
- `POST /api/v1/trade/position-mode` — set `one-way` | `hedge`

**`marginMode`** is per-instrument on Blofin (you can have BTC on
cross and ETH on isolated). HL uses portfolio margin globally, so the
concept doesn't translate 1:1.

**`positionMode`** is an **account-wide** setting that affects all
new positions:
- `one-way` (= `positionSide: "net"`): one position per coin per
  account. Long-then-Short on the same coin closes the existing
  position first. **This is what we want.**
- `hedge` (= `positionSide: "long"` or `"short"`): can hold long and
  short simultaneously. CP signals are always directional → not useful
  for our flow.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Leverage scope | Per-coin (sticky) | Per-coin (sticky) — same |
| Margin mode | Portfolio margin only | Cross OR isolated, per-coin |
| Position mode | One-way implicit | Explicit `one-way` / `hedge` — must set |
| Account config visibility | Implicit | `GET /api/v1/account/config` exposes |

### New capability

- **`multiple-leverage`** — bulk query for /balance UI. Currently we
  don't surface per-coin leverage in /menu; could be useful.
- **Margin mode per-coin** — gives users freedom to isolate
  high-volatility positions while keeping safer ones cross-margined.
  For first-pass migration, we hard-code `cross` (matches current HL
  behavior).

### Migration note

- **Account-config bootstrap on first activation**: set the right
  margin mode + position mode the first time a user activates Blofin.
  Subsequent activations no-op (settings are persistent).
  ```python
  def ensure_account_config(client):
      # one-way is required for our directional CP signals
      try:
          client.set_position_mode("one-way")
      except AlreadySetError:  # custom — translate the exchange's "no change" response
          pass
  ```
- **`build_orders.update_leverage`**: call the new endpoint with
  `instId` + `marginMode` + `positionSide="net"`.
- **The signal-leverage → effective-leverage cap** stays:
  `min(signal.leverage, user.max_leverage, asset_meta.maxLeverage)`.
  Blofin's `maxLeverage` is per-asset like HL — same code path.

---

## 10. Market data

### What Blofin offers

| Endpoint | Returns |
|---|---|
| `GET /api/v1/market/tickers` | Last price, bid/ask, 24h OHLC + volume per instrument |
| `GET /api/v1/market/mark-price` | Mark + index prices |
| `GET /api/v1/market/funding-rate` | Current funding rate |
| `GET /api/v1/market/funding-rate-history` | Historical funding (paginated, 100 max) |
| `GET /api/v1/market/books` | L2 orderbook, up to depth 100 |
| `GET /api/v1/market/candles` | OHLCV candlesticks |
| `GET /api/v1/market/trades` | Recent trades |

`tickers` response (per instrument):
```json
{
  "instId":         "BTC-USDT",
  "last":           "50000",
  "lastSize":       "0.5",
  "askPrice":       "50001",
  "askSize":        "10",
  "bidPrice":       "49999",
  "bidSize":        "10",
  "high24h":        "51000",
  "open24h":        "49500",
  "low24h":         "49000",
  "volCurrency24h": "10000",          // in base currency
  "vol24h":         "1000000",        // in contracts
  "ts":             "1597026383085"
}
```

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Mid price | `get_all_mids()` returns ALL coins | `tickers` can be all or one |
| Mark price | Not exposed by SDK wrapper | Dedicated endpoint |
| Funding | Not exposed by SDK wrapper | Real-time + historical |

### New capability

- **Funding rate**: could surface in /audit (per-trade) as
  "estimated funding cost over hold duration." Phase 6+ polish.
- **Mark vs last vs index** — three distinct price points. For
  `close_position` we want the mid (best execution); for liquidation
  audits, mark is the right reference.

### Migration note

- **`get_all_mids`** becomes a call to `/market/tickers` (no
  `instId` returns all):
  ```python
  def get_all_mids(self) -> dict[str, float]:
      raw = self._get("/api/v1/market/tickers")
      return {t["instId"]: (float(t["bidPrice"]) + float(t["askPrice"])) / 2 for t in raw["data"]}
  ```
  Or use `last` directly if mid isn't needed (it isn't for our IOC
  close — the spread handles the gap).
- **`close_position`**: we may not even need this if we use Blofin's
  dedicated `POST /api/v1/trade/close-positions` market-close endpoint
  (see § 6). That endpoint **does not take a limit price** — it's a
  true market order. Bug #12's 3%-spread workaround becomes
  unnecessary.

---

## 11. Symbol mapping (Potion → Blofin)

### What Blofin offers

Instrument format: `{BASE}-{QUOTE}` for spot, often `{BASE}-{QUOTE}`
for perpetuals too (no `-SWAP` suffix in the docs samples). `BTC-USDT`,
`ETH-USDT`, etc. `instType: "SWAP"` distinguishes the perp variants.

### Delta from HL

| Potion | HL | Blofin (expected) |
|---|---|---|
| `BTC/USDT` | `BTC` | `BTC-USDT` |
| `ETH/USDT` | `ETH` | `ETH-USDT` |
| `1000BONK/USDT` | `kBONK` | `1000BONK-USDT` (no kilo prefix on Blofin?) |
| `XRP/USDT` | `XRP` (not on HL testnet) | `XRP-USDT` (likely listed on Blofin) |
| `SHIB/USDT` | `kSHIB` | `SHIB-USDT` or `1000SHIB-USDT` — **verify on demo** |
| `MATIC/USDT` | `POL` (HL rebrand) | `POL-USDT` (Blofin likely rebranded too — verify) |

### New capability

- **No kilo-prefix gymnastics** — Blofin likely uses the original
  Binance-style symbols. Less custom mapping than HL.
- **Potentially more coins** — Blofin lists more perps than HL,
  especially the smaller-cap ones CP signals on.

### Migration note

- **New `potion_to_blofin(pair) -> str`** helper next to the existing
  `potion_to_hyperliquid`. Simpler:
  ```python
  def potion_to_blofin(pair: str, available_instruments: dict | None = None) -> str:
      base, quote = pair.split("/")
      inst_id = f"{base}-{quote}"
      if available_instruments is not None and inst_id not in available_instruments:
          # try the rebrand overrides (MATIC→POL etc.) — keep a small table
          inst_id = _BLOFIN_OVERRIDES.get(base, base) + "-" + quote
      return inst_id
  ```
- **Build `_BLOFIN_OVERRIDES`** during demo testing by trying each
  Potion sample pair against `/market/instruments`; any failures get
  an override entry.
- **`HYPERLIQUID_COINS` set in symbol_mapper.py** can stay — it's
  only consulted on the HL path. Add a parallel `BLOFIN_INSTRUMENTS`
  built dynamically at startup from the instruments endpoint.

---

## 12. Response-parsing contracts

### What Blofin offers

Universal response shape:
```json
{
  "code": "0",                       // "0" = success, anything else = error
  "msg":  "success",                 // human-readable
  "data": { ... } | [ ... ] | null   // payload, shape depends on endpoint
}
```

For order endpoints, `data.orderId` and `data.clientOrderId` are the
two IDs you get back.

For batch endpoints, `data` is an array; **individual items may have
their own success/failure** — partial-failure is real.

HTTP status codes generally **200** even on logical errors —
**check `code` not HTTP status**. Rate limits are an exception (429).

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Success signal | Nested `statuses[0].resting` or `.filled` | Top-level `code == "0"` |
| Error signal | `statuses[0].error` | `code != "0"`, `msg` carries text |
| Order ID location | `statuses[0].resting.oid` or `.filled.oid` | `data.orderId` (or `data.clientOrderId`) |
| Fill info | `statuses[0].filled.{avgPx, totalSz, oid}` | `data` from `trade-history` separately |
| HTTP semantics | All 200 | Mostly 200, **429 for rate limit** |
| Defensive parsing risk | High (nested + nullable) | Moderate (one level + types matter) |

### New capability

- **`clientOrderId` round-trip** — we can name orders ourselves and
  not depend on the exchange to assign IDs we later track.

### Migration note

- **Replace `_get_statuses`, `_extract_oid`, `_extract_fill`,
  `_get_error`** with simpler Blofin equivalents:
  ```python
  def _blofin_ok(result: dict) -> bool:
      return result.get("code") == "0"
  def _blofin_error(result: dict) -> str | None:
      return None if _blofin_ok(result) else result.get("msg") or "unknown error"
  def _blofin_order_id(result: dict) -> str | None:
      d = result.get("data") or {}
      return d.get("orderId") or d.get("clientOrderId")
  ```
- **No "filled in same response" path on Blofin order placement** —
  we have to query `trade-history` or use the orders WS to detect
  fills. Initially: assume `resting` and reconcile via CP events as
  before. Equivalent to HL's resting branch.
- **Partial-failure on batch** — `multiple-orders` and
  `multiple-cancels` need per-item status checking. New helper.

---

## 13. Lifecycle reconciliation

### What Blofin offers

Same three reconciliation paths as HL, mapped to Blofin endpoints:

1. **Startup sync**: combine `GET /api/v1/trade/positions` +
   `GET /api/v1/trade/active-orders` + `GET /api/v1/trade/active-tpsl-orders`.
   Three calls vs HL's two.
2. **CP-driven order reconciliation**: identical logic — CP events
   drive `orders` table state in our DB.
3. **D10 source-of-truth check**: same — query `get_open_positions`
   before market-closing a "canceled" trade.

**Optional Stage 2**: subscribe to WS private channels (`positions`,
`orders`) for event-driven reconciliation. Reduces polling burden;
gives near-real-time fill awareness.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Sync sources | 2 endpoints (positions + orders) | 3 endpoints (+ tpsl-orders) |
| Real-time updates | Polling only | Polling OR WS push |
| Order ID format | int (HL oid) | string (Blofin orderId / our clientOrderId) |

### Migration note

- **Sync gets one extra call**, otherwise identical structure.
- **Order ID column** in `orders` table should be TEXT, not INTEGER,
  to accommodate Blofin's string-style IDs. Schema migration:
  ```sql
  -- existing: orders.oid INTEGER
  -- migrate to: orders.oid TEXT
  -- (SQLite is forgiving on type affinity, but be explicit)
  ```
- **D10 check** — same logic, just call Blofin's positions endpoint
  instead of HL's. Adapter pattern keeps the pipeline code unchanged.

---

## 14. Known quirks / production bugs (anticipated)

We haven't run a single trade on Blofin yet. This section gets
populated as we hit real issues — same shape as HL doc § 14. **Things
I'd predict will surface based on the API shape**:

### The base64-of-hex signature trick

The docs explicitly warn against the "natural" base64-of-raw-bytes
approach. Easy to write wrong, hard to debug (looks like 60009 bad
creds). **Verify against Blofin's example signature in the docs
before going live.**

### Resting-entry PENDING→OPEN promotion (carry over Bug #20)

HL Bug #20 (see HL doc § 14): no live handler promoted a resting-entry
trade from PENDING→OPEN, so the breakeven-after-TP1 move silently
no-opped. The fix (`_promote_to_open_if_filled`) lives in the shared
pipeline, but it queries the *exchange* — the Blofin
`PositionManager`/client must implement `get_open_positions()` against
`GET /api/v1/trade/positions` (with the unsigned-`size`+`side`
normalization from § 6) so the promotion path works identically on
Blofin. Without it, Blofin resting entries hit the same bug. Bonus: if
we wire the orders WebSocket (`positions`/`orders` channels), entry
fills become push-events and promotion can happen on the fill rather
than waiting for CP's `TRADE_LIVE`.

### Position mode bootstrap

If a Blofin account defaults to `hedge` mode and we send orders
without setting `positionSide=net`, the orders may be rejected or
behave unexpectedly. First user activation on Blofin must explicitly
set `one-way` mode.

### Margin mode lock during open positions

Most exchanges block margin-mode changes while a position is open. If
we ever build a "switch a user from cross to isolated" feature, it'll
need to close positions first.

### Tickets close at market — no spread to tune

Bug #12 (3% close spread) goes away on Blofin because `close-positions`
is a true market order. But: there's no documented oracle-distance
check we can fall foul of either. Verify on demo what happens when
liquidity is thin.

### `clientOrderId` length / character constraints

Not documented in our fetch. Most exchanges allow 32 chars,
alphanumeric + underscores. `potion_{trade_id}_{order_type}` (e.g.
`potion_7000123_stop_loss`) is 24 chars — fine. But verify.

### Partial batch failure

`multiple-orders` may succeed for 3 of 5 orders and fail for 2. Our
current code's `submit_trade` raises on entry rejection; the batch
path needs different error handling (e.g. roll back successful
submits when SL fails).

### Demo data persistence

Demo accounts may reset balances periodically. **Not documented in
our fetch.** Watch for any "demo reset" notices and don't store
long-running data on demo without backups.

---

## 15. Rate limits & error handling

### What Blofin offers

**REST limits**:
- 500 req/min per IP — exceed → 5-minute suspension
- 1500 req/5min per IP — exceed → 1-hour suspension
- **Trading endpoints: 30 req per 10s by UserId** (not IP)

**WebSocket limits**:
- 1 new connection per second per IP
- Auto-disconnect if no data received for 30s

**Rate limit response**: HTTP **429** with body
`"Rate limit reached. Please refer to API documentation..."`. No
`Retry-After` header documented.

**Error code structure**: `code` + `msg` in response body.
Documented codes:
- `60009` — Login failed / signature verification failed
- `60012` — Invalid request format
- HTTP 403 — Network firewall (excessive requests, 5-min ban)
- HTTP 429 — Rate limit

**More codes** exist (insufficient balance, invalid price, position
not found, etc.) but aren't enumerated in the public docs we fetched.
We'll learn them by hitting them on demo.

### Delta from HL

| | HL | Blofin |
|---|---|---|
| Documented limits | None | Yes — explicit numbers |
| Trading-endpoint cap | Soft | Hard: 30/10s per user |
| Rate-limit signal | Error message contains "rate limit" | HTTP 429 |
| Retry header | N/A | None documented |

### New capability

- **Rate-limit is now a real concern**. 30 trades/10s with 3 users
  and 5 orders per trade = 75 orders per 10s in a flash signal day.
  We need to either batch (use `multiple-orders` — 1 call for 5
  orders) or stagger.

### Migration note

- **`retry_on_transient` decorator** extends to handle HTTP 429
  explicitly:
  ```python
  except HTTPError as e:
      if e.response.status_code == 429:
          # Blofin rate limit — back off harder
          delay = base_delay * (4 ** attempt) + random.uniform(0, 1)
          time.sleep(delay)
          continue
  ```
- **Trading endpoint budget**: 30 calls / 10s per user. With our
  current "5 orders per trade sequential" pattern, 3 simultaneous
  trades = 15 calls in <1s. Add WS subscription overhead, plus
  position queries, and we're flirting with the limit during a
  CP-signal storm. **Use `multiple-orders` from day 1** to compress
  5 calls into 1.
- **Surface 429s to users** — if the bot's bumping limits during a
  CP storm, the bot should kill-switch rather than miss signals.
  Add a counter; if 429s ≥ 3 in 60s, send a Telegram alert.

---

## Demo Trading — our replacement for HL testnet

The user explicitly asked for this as a separate section. Demo
trading is **how we'll run all dev / testing / soak work** during and
after the Blofin migration, in lieu of an HL-testnet equivalent.

### What demo offers (and what's not yet clear)

**Base URLs** (separate from production — clean isolation):
```
REST:        https://demo-trading-openapi.blofin.com
Public WS:   wss://demo-trading-openapi.blofin.com/ws/public
Private WS:  wss://demo-trading-openapi.blofin.com/ws/private
```

**Authentication**: same HMAC-SHA256 method as production. The docs
state explicitly: *"The same authentication signature method"*
applies, using the fixed path `/users/self/verify` for WS auth.

**Funding demo accounts**: `POST /api/v1/asset/demo-apply-money` —
the request includes:
```json
{
  "adjustType": "...",
  "demoApplyMoney": [
    { "currency": "USDT", "amountStr": "10000" }
  ]
}
```
Full parameter list was truncated in the public docs we fetched —
**confirm exact shape against the live docs page or by trying it on
the demo environment**.

**What's NOT explicitly documented** (will verify by experimentation):

| Question | What we'd need to find out |
|---|---|
| Do demo accounts use real market data with simulated balances, or a fully simulated orderbook? | Compare `/market/tickers` on demo vs production. If they match → real data. |
| Do production API keys work on demo, or do we need a separate demo key? | Try a production key against demo; expect 401/403 if separate. |
| Are there demo-only endpoints unavailable on production (and vice versa)? | Probably only `/demo-apply-money` is demo-only; trading endpoints work on both. |
| Does demo balance reset periodically (daily, weekly)? | Watch over a few days; if we see resets, plan around them. |
| Does WebSocket auth method differ? | Likely same — uses the same `/users/self/verify` fixed path. |
| Rate limits — same as production or relaxed? | Probably same (the limits are per-IP for the most part). |

### Delta from HL testnet

| | HL testnet | Blofin demo |
|---|---|---|
| Base URL | `api.hyperliquid-testnet.xyz` | `demo-trading-openapi.blofin.com` |
| Funding | Manual transfer via UI / faucet | API endpoint (`demo-apply-money`) |
| Liquidity | Real orderbook, often thin (oracle drift issues) | Likely similar — must verify |
| Symbol parity | Different coins available than mainnet | Likely same as production |
| Auth | Same as mainnet (with testnet chainId in sig) | Same as production (separate URL is the only difference) |

### New capability

- **Demo funding via API** — we can top up programmatically when test
  drivers drain balance. HL testnet required manual UI faucet visits.
- **Cleaner environment switch** — different base URL, no hidden
  chainId or shared state. Less risk of accidentally signing a
  production order with a "test" mindset.

### Migration note — how the test driver evolves

The current test driver (Phase 4.3) targets HL testnet. For Blofin:

- **Config**: add `exchange.network` mapping where `testnet` /
  `mainnet` becomes `demo` / `production` for Blofin users. Or rename
  the field to be exchange-agnostic (e.g. `environment: live | test`).
- **Driver does NOT change** — it's exchange-agnostic. It posts CP
  messages to the mirror channel; the bot's pipeline + `BlofinClient`
  handle the rest.
- **Pre-test funding**: add a small helper to the test driver:
  ```bash
  python3 scripts/test_driver.py --top-up-demo 1000
  ```
  Calls `/asset/demo-apply-money` for each registered demo user.
  Useful when balances drift after many test runs.
- **Cleanup logic** carries over — `--cleanup` will cancel orphan
  orders on demo just as it does on HL testnet.

### Validation plan when we start

Before writing the `BlofinClient`:

1. **Apply for API Transaction permission** on a real Blofin account.
2. **Create one API key** with Read + Trade permissions (NOT
   Withdraw or Transfer). Add a strong passphrase.
3. **Hit the demo REST API** with a tiny script that signs a
   `/api/v1/asset/balances` query. Confirm signature works.
4. **Top up a demo account** via `/asset/demo-apply-money`. Verify
   balance reflects.
5. **Submit a single test order** through demo (e.g. BTC-USDT, 0.001
   contract limit at 50% below mid — sits resting). Verify response
   shape matches the docs.
6. **Cancel the test order**. Verify idempotency.

Only THEN start writing the production `BlofinClient`.

---

## Quick reference — every Blofin surface we need, in one table

Mirrors the HL doc's quick-reference table. File:line citations will
be filled in as the `BlofinClient` lands.

| What we need | Blofin endpoint | Method | Notes |
|---|---|---|---|
| Account config | `/api/v1/account/config` | GET | Detect position mode |
| Set position mode | `/api/v1/trade/position-mode` | POST | Force `one-way` |
| Set margin mode | `/api/v1/trade/margin-mode` | POST | Default `cross` per-coin |
| Set leverage | `/api/v1/trade/leverage` | POST | Per-coin before order |
| Balance | `/api/v1/asset/balances?accountType=futures` | GET | `available` is the buying power |
| Positions | `/api/v1/trade/positions` | GET | Unsigned size + `side` |
| Active orders | `/api/v1/trade/active-orders` | GET | Resting limit orders |
| Active TP/SL | `/api/v1/trade/active-tpsl-orders` | GET | Resting TP/SL plans |
| Place order | `/api/v1/trade/order` | POST | Single — for entry |
| Place batch | `/api/v1/trade/multiple-orders` | POST | All 5 orders at once if practical |
| Place TP/SL | `/api/v1/trade/tpsl-order` | POST | Native — better than HL's emulation |
| Cancel order | `/api/v1/trade/order` | DELETE | By orderId or clientOrderId |
| Cancel batch | `/api/v1/trade/multiple-orders` | DELETE | Whole-trade cleanup |
| Close position | `/api/v1/trade/close-positions` | POST | True market — no spread tuning needed |
| Instruments | `/api/v1/market/instruments` | GET | tickSize, lotSize, maxLeverage |
| Tickers | `/api/v1/market/tickers` | GET | Mid/bid/ask for IOC math |
| Demo funding | `/api/v1/asset/demo-apply-money` | POST | Top up demo balance |
| WS — positions | `/ws/private` → subscribe `positions` | WS | Stage 2 optional |
| WS — orders | `/ws/private` → subscribe `orders` | WS | Stage 2 optional |

---

## What this doc enables

Both `HYPERLIQUID_INTEGRATION.md` and this doc together become the
spec sheet for the Phase 6 `BlofinClient` rewrite. Implementation
order:

1. **`src/exchange/blofin.py`** — the BlofinClient class, mirroring
   the surface of HyperliquidClient. Hand-rolled HTTP (no SDK).
   Sign-and-send helper, plus thin wrappers for each endpoint.
2. **`src/exchange/blofin_order_builder.py`** — Blofin-specific order
   construction (contracts, tickSize, lotSize, native TP/SL).
3. **`src/exchange/blofin_position_manager.py`** — wraps
   submit_trade / cancel / close / sync. Reuses pipeline-level
   contract.
4. **`src/utils/symbol_mapper.py`** gets a parallel
   `potion_to_blofin` function.
5. **`src/state/user_db.py`** — add `exchange` + `passphrase_enc`
   columns; existing HL users continue to work.
6. **Telegram /register flow** — exchange-choice question for new
   users.
7. **Test driver** — `--top-up-demo` helper.

Estimated effort: ~3–4 focused sessions to land the BlofinClient with
demo coverage. Then a real-money soak before any user is migrated to
production Blofin.

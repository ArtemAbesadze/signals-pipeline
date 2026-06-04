#!/usr/bin/env python3
"""Blofin demo-environment validation smoke test (Phase 6.2).

Run this BEFORE writing any BlofinClient code. It verifies, against the
Blofin **demo** REST API, that:

  1. Our HMAC-SHA256 signing works (the base64-of-hex quirk — easy to get
     wrong; a wrong signature looks like "60009 Login failed").
  2. We can read account balance.
  3. We can read instrument metadata (tickSize / lotSize / minSize /
     contractValue) — the real values we'll build order math against (D11).
  4. We can top up the demo balance.
  5. We can place a tiny resting order and read back the real response shape.
  6. We can cancel it (and cancel is idempotent).

Credentials come from the environment (NEVER hard-code or paste them):

    BLOFIN_API_KEY, BLOFIN_API_SECRET, BLOFIN_PASSPHRASE

Put them in `.env` (gitignored) and run:

    set -a; source .env; set +a
    python3 scripts/blofin_demo_check.py

Nothing here touches production — base URL is the demo host, and the only
order placed is a tiny limit far from mid that we immediately cancel. This
script is a throwaway harness; the verified sign function gets lifted into
`src/exchange/blofin.py` once it's confirmed working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import requests

DEMO_BASE_URL = "https://demo-trading-openapi.blofin.com"


# ---------------------------------------------------------------------------
# Signing — per docs.blofin.com (the base64-of-HEX-STRING quirk)
# ---------------------------------------------------------------------------
def sign(secret: str, prehash: str) -> str:
    """HMAC-SHA256 → hexdigest → base64 of the hex STRING (not raw bytes).

    Most exchanges base64 the raw HMAC bytes; Blofin base64s the hex digest
    string. Getting this wrong returns 60009 "Login failed".
    """
    mac = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256)
    hexdigest = mac.hexdigest()
    return base64.b64encode(hexdigest.encode()).decode()


def _creds() -> tuple[str, str, str]:
    key = os.getenv("BLOFIN_API_KEY", "")
    secret = os.getenv("BLOFIN_API_SECRET", "")
    passphrase = os.getenv("BLOFIN_PASSPHRASE", "")
    missing = [
        n for n, v in (
            ("BLOFIN_API_KEY", key),
            ("BLOFIN_API_SECRET", secret),
            ("BLOFIN_PASSPHRASE", passphrase),
        ) if not v
    ]
    if missing:
        sys.exit(
            f"Missing env var(s): {', '.join(missing)}.\n"
            "Add them to .env (gitignored) and `set -a; source .env; set +a`."
        )
    return key, secret, passphrase


def request(method: str, path: str, body: dict | None = None) -> dict:
    """Sign + send one request. ``path`` must include the query string for
    GET. Prints a compact trace and returns the parsed JSON (or a synthetic
    error dict on transport failure)."""
    key, secret, passphrase = _creds()
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    prehash = f"{path}{method}{ts}{nonce}{body_str}"
    headers = {
        "ACCESS-KEY": key,
        "ACCESS-SIGN": sign(secret, prehash),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    url = DEMO_BASE_URL + path
    try:
        resp = requests.request(
            method, url, headers=headers,
            data=body_str if body_str else None, timeout=15,
        )
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {"_non_json": resp.text}
        print(f"  {method} {path} -> HTTP {resp.status_code}")
        print("  " + json.dumps(parsed, indent=2)[:1400])
        return parsed
    except requests.RequestException as e:
        print(f"  {method} {path} -> TRANSPORT ERROR: {e}")
        return {"code": "transport_error", "msg": str(e)}


def ok(result: dict) -> bool:
    return str(result.get("code")) == "0"


def first(data) -> dict:
    """Blofin returns ``data`` as a LIST for order endpoints (one item per
    order, each with its own code/msg). Normalize to the first item."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


# ---------------------------------------------------------------------------
# The 6 validation steps
# ---------------------------------------------------------------------------
def step1_signature_and_balance() -> bool:
    print("\n[1/6] Signature smoke test — GET futures balance")
    r = request("GET", "/api/v1/asset/balances?accountType=futures")
    if str(r.get("code")) == "60009":
        print("  ✗ 60009 Login failed — signature is WRONG. Stopping.")
        return False
    if not ok(r):
        print(f"  ! non-zero code {r.get('code')}: {r.get('msg')} "
              "(signature likely OK if not 60009 — endpoint/permission issue)")
    else:
        print("  ✓ signature accepted")
    return str(r.get("code")) != "60009"


def step2_positions() -> None:
    # CONFIRMED on demo: account/position data lives under /api/v1/account/,
    # NOT /api/v1/trade/ (the latter returns 152404 "not supported").
    print("\n[2/6] Open positions — GET /api/v1/account/positions (D10/D11 + sync)")
    request("GET", "/api/v1/account/positions")
    print("  (balance: GET /api/v1/account/balance · modes: account/margin-mode,"
          " account/position-mode — all confirmed working)")


def step3_instruments() -> dict | None:
    print("\n[3/6] Instruments — BTC-USDT metadata (tickSize/lotSize/minSize)")
    r = request("GET", "/api/v1/market/instruments?instId=BTC-USDT")
    data = r.get("data") or []
    inst = data[0] if data else None
    if inst:
        print("  ✓ BTC-USDT:", {
            k: inst.get(k) for k in
            ("tickSize", "lotSize", "minSize", "contractValue", "maxLeverage", "state")
        })
    return inst


def step4_topup_demo() -> None:
    print("\n[4/6] Top up demo balance (exact body shape is doc-truncated — trying)")
    # adjustType is not fully documented; print whatever each attempt returns
    # so we learn the real contract. Best-effort — does not gate the run.
    for adjust in ("1", "add"):
        body = {
            "adjustType": adjust,
            "demoApplyMoney": [{"currency": "USDT", "amountStr": "1000"}],
        }
        r = request("POST", "/api/v1/asset/demo-apply-money", body)
        if ok(r):
            print(f"  ✓ top-up accepted with adjustType={adjust!r}")
            return
    print("  ! top-up not confirmed — note the responses above; we'll adjust the body")


def step5_place_test_order(inst: dict | None) -> str | None:
    print("\n[5/6] Place a tiny resting order (limit far below mid)")
    if not inst:
        print("  - skipped (no instrument metadata)")
        return None
    tick = float(inst.get("tickSize") or "0.1")
    min_size = inst.get("minSize") or inst.get("lotSize") or "0.1"
    # mid from tickers
    t = request("GET", "/api/v1/market/tickers?instId=BTC-USDT")
    tdata = (t.get("data") or [{}])[0]
    bid = float(tdata.get("bidPrice") or tdata.get("last") or "0")
    if bid <= 0:
        print("  - skipped (no price)")
        return None
    raw_px = bid * 0.5  # 50% below — will rest, won't fill
    px = round(round(raw_px / tick) * tick, 10)
    coid = "potiondemo" + uuid.uuid4().hex[:8]
    body = {
        "instId": "BTC-USDT", "marginMode": "cross", "side": "buy",
        "orderType": "limit", "price": str(px), "size": str(min_size),
        "clientOrderId": coid, "positionSide": "net",
    }
    r = request("POST", "/api/v1/trade/order", body)
    item = first(r.get("data"))
    if ok(r) and str(item.get("code", "0")) == "0":
        oid = item.get("orderId")
        print(f"  ✓ order placed: orderId={oid} clientOrderId={item.get('clientOrderId')}")
        return oid
    print("  ! order not placed — see response (mode/permission/param issue to learn from)")
    return None


def step6_cancel(order_id: str | None) -> None:
    print("\n[6/6] Cancel the test order (+ idempotency)")
    if not order_id:
        print("  - skipped (no order placed)")
        return
    body = {"instId": "BTC-USDT", "orderId": order_id}
    r1 = request("POST", "/api/v1/trade/cancel-order", body)
    if ok(r1):
        print("  ✓ canceled")
    print("  - second cancel (idempotency check):")
    request("POST", "/api/v1/trade/cancel-order", body)


def step7_active_orders_sweep() -> None:
    """List resting orders (captures the active-orders shape for sync) and
    cancel any leftover BTC-USDT demo orders so the harness self-cleans."""
    print("\n[7] Pending orders — GET /api/v1/trade/orders-pending + sweep leftovers")
    r = request("GET", "/api/v1/trade/orders-pending?instId=BTC-USDT")
    orders = r.get("data") or []
    if not orders:
        print("  ✓ no resting orders")
        return
    for o in orders:
        oid = o.get("orderId")
        print(f"  - canceling leftover orderId={oid}")
        request("POST", "/api/v1/trade/cancel-order", {"instId": "BTC-USDT", "orderId": oid})
    after = request("GET", "/api/v1/trade/orders-pending?instId=BTC-USDT")
    print("  ✓ clean" if not (after.get("data") or []) else "  ! still resting — check above")


def main() -> None:
    print("Blofin DEMO validation —", DEMO_BASE_URL)
    if not step1_signature_and_balance():
        sys.exit("Signature failed — fix signing before continuing.")
    step2_positions()
    inst = step3_instruments()
    step4_topup_demo()
    oid = step5_place_test_order(inst)
    step6_cancel(oid)
    step7_active_orders_sweep()
    print("\nDone. Review the responses above with Claude before writing BlofinClient.")


if __name__ == "__main__":
    main()

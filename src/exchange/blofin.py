"""Blofin REST client — hand-rolled HTTP (Blofin ships no Python SDK).

Mirrors the read surface of ``HyperliquidClient`` so it drops into
``orchestrator.build_exchange_client``; writes are Blofin-native. Every
endpoint and response shape here is demo-CONFIRMED — see
``docs/BLOFIN_INTEGRATION.md`` § 0 (validated 2026-06-04 via
``scripts/blofin_demo_check.py``).

D11: reads are the source of truth for recorded + displayed data. The
position manager (Phase 6.5) re-reads real state from these after each
CP event rather than inferring it from the message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import requests

from src.exchange.hyperliquid import retry_on_transient

logger = logging.getLogger(__name__)

NETWORK_URLS = {
    "demo": "https://demo-trading-openapi.blofin.com",
    "production": "https://openapi.blofin.com",
}

# Cancel of an order that's already filled/canceled/gone. Unlike HL (which is
# idempotent on cancel), Blofin returns this code — callers treat it as benign.
CANCEL_GONE_CODE = "102068"


class BlofinError(Exception):
    """Blofin API error (non-zero ``code``) or a transport/protocol failure."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def sign(secret: str, prehash: str) -> str:
    """HMAC-SHA256 → hexdigest → base64 of the hex STRING (Blofin's quirk).

    Demo-confirmed 2026-06-04. base64-ing the RAW digest bytes (the "natural"
    approach most exchanges use) returns ``60009 Login failed`` on Blofin.
    """
    mac = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.hexdigest().encode()).decode()


# --- response helpers (Blofin wraps order/cancel data in a LIST) ------------
def response_ok(result: dict) -> bool:
    return str(result.get("code")) == "0"


def response_error(result: dict) -> str | None:
    if response_ok(result):
        return None
    return result.get("msg") or f"code {result.get('code')}"


def first_item(data: Any) -> dict:
    """Order/cancel responses put ``data`` in a list (one item per order,
    each with its own code/msg). Normalize to the first item."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def order_id_of(result: dict) -> str | None:
    return first_item(result.get("data")).get("orderId")


def tpsl_id_of(result: dict) -> str | None:
    """order-tpsl returns ``data`` as a dict; cancel-tpsl as a list. Handle both."""
    return first_item(result.get("data")).get("tpslId")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class BlofinClient:
    """Unified Blofin REST wrapper. Scoped to a single user's credentials."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        network: str = "demo",
    ):
        if network not in NETWORK_URLS:
            raise ValueError(
                f"Unknown Blofin network '{network}'. Use 'demo' or 'production'."
            )
        self._key = api_key
        self._secret = api_secret
        self._passphrase = passphrase
        self._network = network
        self._base_url = NETWORK_URLS[network]
        self._session = requests.Session()
        self._instruments_cache: dict[str, dict] | None = None
        logger.info(
            "BlofinClient initialized: network=%s key=%s***",
            network, (api_key or "")[:6],
        )

    @property
    def network(self) -> str:
        return self._network

    # ------------------------------------------------------------------
    # Signing + transport
    # ------------------------------------------------------------------
    def _headers(self, method: str, path: str, body_str: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        prehash = f"{path}{method}{ts}{nonce}{body_str}"
        return {
            "ACCESS-KEY": self._key,
            "ACCESS-SIGN": sign(self._secret, prehash),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Sign + send. Returns parsed JSON. ``path`` must include the query
        string for GET (it's part of the signature). Network errors
        (ConnectionError/Timeout — OSError subclasses) propagate so the
        ``retry_on_transient`` decorator on read methods can retry them; a
        429 is re-raised as a rate-limit ``BlofinError`` (also retried)."""
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(method, path, body_str)
        resp = self._session.request(
            method, self._base_url + path,
            headers=headers, data=body_str or None, timeout=15,
        )
        if resp.status_code == 429:
            raise BlofinError(f"rate limit (429) on {method} {path}")
        try:
            return resp.json()
        except ValueError as e:
            raise BlofinError(
                f"non-JSON response ({resp.status_code}) on {method} {path}: "
                f"{resp.text[:200]}"
            ) from e

    def _checked(self, method: str, path: str, body: dict | None = None) -> dict:
        """``_request`` + raise ``BlofinError`` on a non-zero top-level code.
        Used by reads; writes return the raw dict for per-item inspection."""
        result = self._request(method, path, body)
        if not response_ok(result):
            raise BlofinError(
                f"{method} {path} failed: {response_error(result)}",
                code=str(result.get("code")),
            )
        return result

    # ------------------------------------------------------------------
    # Reads (transient-retried) — D11 source of truth
    # ------------------------------------------------------------------
    @retry_on_transient()
    def get_balance(self) -> dict[str, str]:
        """Futures account balance. ``available`` is the buying power used by
        the port/wallet guardrail."""
        data = self._checked("GET", "/api/v1/account/balance").get("data") or {}
        details = data.get("details") or []
        usdt = next((d for d in details if d.get("currency") == "USDT"), {})
        return {
            "total_equity": data.get("totalEquity", "0"),
            "currency": "USDT",
            "equity": usdt.get("equity", "0"),
            "available": usdt.get("available", "0"),
            "frozen": usdt.get("frozen", "0"),
            "bonus": usdt.get("bonus", "0"),
        }

    @retry_on_transient()
    def get_open_positions(self) -> list[dict[str, Any]]:
        """Open positions, normalized to a signed-size shape (matching the HL
        client). In ``net_mode`` Blofin signs the ``positions`` field
        (negative = short), so direction comes from the sign, like HL's szi."""
        out = []
        for p in self._checked("GET", "/api/v1/account/positions").get("data") or []:
            size = _f(p.get("positions"))
            if size == 0:
                continue
            inst = p.get("instId", "")
            out.append({
                "coin": inst.split("-")[0] if inst else "",
                "inst_id": inst,
                "size": size,
                "side": "LONG" if size > 0 else "SHORT",
                "entry_price": p.get("averagePrice"),
                "mark_price": p.get("markPrice"),
                "unrealized_pnl": p.get("unrealizedPnl"),
                "leverage": p.get("leverage"),
                "liquidation_price": p.get("liquidationPrice") or None,
                "margin_mode": p.get("marginMode"),
            })
        return out

    @retry_on_transient()
    def get_open_orders(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Resting (pending) orders, optionally filtered by instrument."""
        path = "/api/v1/trade/orders-pending"
        if inst_id:
            path += f"?instId={inst_id}"
        return self._checked("GET", path).get("data") or []

    @retry_on_transient()
    def get_all_mids(self) -> dict[str, float]:
        """Mid price per instrument (mean of bid/ask, falling back to last)."""
        mids: dict[str, float] = {}
        for t in self._checked("GET", "/api/v1/market/tickers").get("data") or []:
            inst = t.get("instId")
            if not inst:
                continue
            bid, ask = _f(t.get("bidPrice")), _f(t.get("askPrice"))
            mids[inst] = (bid + ask) / 2 if bid > 0 and ask > 0 else _f(t.get("last"))
        return mids

    @retry_on_transient()
    def get_asset_meta(self) -> dict[str, dict]:
        """Per-instrument metadata (tickSize/lotSize/minSize/contractValue/
        maxLeverage), keyed by instId. Cached per process like the HL client."""
        if self._instruments_cache is None:
            raw = self._checked("GET", "/api/v1/market/instruments")
            self._instruments_cache = {
                i["instId"]: i for i in (raw.get("data") or [])
            }
            logger.info("Cached %d Blofin instruments", len(self._instruments_cache))
        return self._instruments_cache

    @retry_on_transient()
    def get_open_tpsl_orders(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        """Resting TP/SL conditionals (algo orders), optionally by instrument.
        Tracked separately from regular orders — needed for startup sync."""
        path = "/api/v1/trade/orders-tpsl-pending"
        if inst_id:
            path += f"?instId={inst_id}"
        return self._checked("GET", path).get("data") or []

    @retry_on_transient()
    def get_fills(self, inst_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Executed fills (real fill prices) — the D11 source for actual
        fill_price/PnL, replacing CP's target-price approximation."""
        path = f"/api/v1/trade/fills-history?limit={int(limit)}"
        if inst_id:
            path += f"&instId={inst_id}"
        return self._checked("GET", path).get("data") or []

    # ------------------------------------------------------------------
    # Writes — NOT auto-retried (avoid double-submit). Return the raw dict so
    # the position manager can inspect per-item codes (e.g. 102068 on cancel).
    # ------------------------------------------------------------------
    def set_leverage(
        self, inst_id: str, leverage: int, margin_mode: str = "cross",
        position_side: str = "net",
    ) -> dict:
        return self._checked("POST", "/api/v1/account/set-leverage", {
            "instId": inst_id, "leverage": str(leverage),
            "marginMode": margin_mode, "positionSide": position_side,
        })

    def place_order(
        self, *, inst_id: str, side: str, order_type: str, size: str | float,
        price: str | float | None = None, margin_mode: str = "cross",
        position_side: str = "net", reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "instId": inst_id, "marginMode": margin_mode, "side": side,
            "orderType": order_type, "size": str(size),
            "positionSide": position_side, "reduceOnly": reduce_only,
        }
        if price is not None:
            body["price"] = str(price)
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/trade/order", body)

    def cancel_order(
        self, inst_id: str, order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"instId": inst_id}
        if order_id is not None:
            body["orderId"] = str(order_id)
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/trade/cancel-order", body)

    def place_tpsl(
        self, *, inst_id: str, side: str, size: str | float,
        tp_trigger_price: str | float | None = None,
        sl_trigger_price: str | float | None = None,
        order_price: str = "-1", margin_mode: str = "cross",
        position_side: str = "net", reduce_only: bool = True,
        client_order_id: str | None = None,
    ) -> dict:
        """Place a TP/SL conditional (``order-tpsl``). ``order_price='-1'`` =
        market execution on trigger. tp-only and sl-only are both accepted.
        Returns ``data`` as a dict with ``tpslId``."""
        body: dict[str, Any] = {
            "instId": inst_id, "marginMode": margin_mode,
            "positionSide": position_side, "side": side, "size": str(size),
            "reduceOnly": reduce_only,
        }
        if tp_trigger_price is not None:
            body["tpTriggerPrice"] = str(tp_trigger_price)
            body["tpOrderPrice"] = order_price
        if sl_trigger_price is not None:
            body["slTriggerPrice"] = str(sl_trigger_price)
            body["slOrderPrice"] = order_price
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/trade/order-tpsl", body)

    def cancel_tpsl(self, inst_id: str, tpsl_id: str) -> dict:
        """Cancel a TP/SL conditional. The endpoint takes a LIST body."""
        return self._request(
            "POST", "/api/v1/trade/cancel-tpsl",
            [{"instId": inst_id, "tpslId": str(tpsl_id)}],
        )

    def close_position(
        self, inst_id: str, margin_mode: str = "cross", position_side: str = "net",
    ) -> dict:
        """True market close of the whole position (no limit price → no
        oracle-distance spread tuning needed, unlike HL Bug #12)."""
        return self._checked("POST", "/api/v1/trade/close-position", {
            "instId": inst_id, "marginMode": margin_mode,
            "positionSide": position_side,
        })

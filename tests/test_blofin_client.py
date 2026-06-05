"""Tests for BlofinClient (Phase 6.3).

The HTTP layer is mocked; fixtures use the REAL response shapes captured from
the demo environment on 2026-06-04 (see docs/BLOFIN_INTEGRATION.md § 0), so
the tests reflect actual Blofin behavior (D11), not assumptions.
"""

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest

from src.exchange.blofin import (
    BlofinClient,
    BlofinError,
    CANCEL_GONE_CODE,
    first_item,
    order_id_of,
    response_error,
    response_ok,
    sign,
    tpsl_id_of,
)


def _resp(payload: dict, status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=payload)
    r.text = json.dumps(payload)
    return r


@pytest.fixture
def client():
    c = BlofinClient("APIKEY", "SECRET", "PASS", network="demo")
    c._session = MagicMock()
    return c


# Real demo shapes ----------------------------------------------------------
BALANCE = {"code": "0", "msg": "success", "data": {
    "ts": "1", "totalEquity": "499486.0", "isolatedEquity": "0",
    "details": [{
        "currency": "USDT", "equity": "500000", "balance": "500000",
        "available": "499997.86", "availableEquity": "499997.86",
        "frozen": "2.13", "orderFrozen": "0", "equityUsd": "499486.0",
        "isolatedUnrealizedPnl": "0", "bonus": "0",
    }],
}}

POSITIONS_LONG = {"code": "0", "msg": "success", "data": [{
    "positionId": "1", "instId": "BTC-USDT", "instType": "SWAP",
    "marginMode": "cross", "positionSide": "net", "positions": "0.1",
    "availablePositions": "0.1", "averagePrice": "63970.0",
    "markPrice": "63967.5", "liquidationPrice": "", "unrealizedPnl": "-0.0002",
    "leverage": "5",
}]}

POSITIONS_SHORT = {"code": "0", "msg": "success", "data": [{
    "instId": "ETH-USDT", "marginMode": "cross", "positionSide": "net",
    "positions": "-2.0", "averagePrice": "3000.0", "markPrice": "3010.0",
    "liquidationPrice": "3500.0", "unrealizedPnl": "-20.0", "leverage": "10",
}]}

ORDERS_PENDING = {"code": "0", "msg": "success", "data": [{
    "orderId": "1000128754044", "clientOrderId": "potion_42_entry",
    "instId": "BTC-USDT", "side": "buy", "orderType": "limit",
    "price": "31961.0", "size": "0.1", "state": "live",
}]}

TICKERS = {"code": "0", "msg": "success", "data": [
    {"instId": "BTC-USDT", "last": "64000", "bidPrice": "63999", "askPrice": "64001"},
    {"instId": "ETH-USDT", "last": "3000", "bidPrice": "0", "askPrice": "0"},
]}

INSTRUMENTS = {"code": "0", "msg": "success", "data": [
    {"instId": "BTC-USDT", "tickSize": "0.1", "lotSize": "0.1", "minSize": "0.1",
     "contractValue": "0.001", "maxLeverage": "150", "state": "live"},
    {"instId": "ETH-USDT", "tickSize": "0.01", "lotSize": "0.01", "minSize": "0.01",
     "contractValue": "0.01", "maxLeverage": "100", "state": "live"},
]}

FILLS = {"code": "0", "msg": "success", "data": [{
    "instId": "BTC-USDT", "tradeId": "1", "orderId": "1000128754758",
    "fillPrice": "63970", "fillSize": "0.1", "fillPnl": "0", "side": "buy",
    "positionSide": "net", "fee": "0.0038", "ts": "1",
}]}

ORDER_OK = {"code": "0", "msg": "", "data": [
    {"orderId": "1000128754245", "clientOrderId": "potion_42_entry",
     "code": "0", "msg": "Order placed"}]}

CANCEL_OK = {"code": "0", "msg": "", "data": [
    {"orderId": "1000128754245", "code": "0", "msg": "Order canceled"}]}

CANCEL_GONE = {"code": CANCEL_GONE_CODE,
               "msg": "Cancel failed as the order has been filled, triggered, "
                      "canceled or does not exist."}

ERR_NOT_SUPPORTED = {"code": "152404", "msg": "This operation is not supported"}


# Signing -------------------------------------------------------------------
class TestSigning:
    def test_base64_of_hex_not_raw_bytes(self):
        secret, prehash = "SECRET", "/api/v1/x" + "GET" + "123" + "nonce"
        expected = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest().encode()
        ).decode()
        wrong_raw = base64.b64encode(
            hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert sign(secret, prehash) == expected
        assert sign(secret, prehash) != wrong_raw  # the 60009 trap

    def test_headers_have_required_fields(self, client):
        h = client._headers("GET", "/api/v1/account/balance", "")
        assert set(h) >= {
            "ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP",
            "ACCESS-NONCE", "ACCESS-PASSPHRASE",
        }
        assert h["ACCESS-KEY"] == "APIKEY"
        assert h["ACCESS-PASSPHRASE"] == "PASS"
        assert h["ACCESS-TIMESTAMP"].isdigit()
        assert len(h["ACCESS-NONCE"]) > 0

    def test_prehash_includes_body_for_post(self, client):
        # signature differs when a body is present vs empty
        empty = client._headers("POST", "/p", "")["ACCESS-SIGN"]
        with_body = client._headers("POST", "/p", '{"a":1}')["ACCESS-SIGN"]
        assert empty != with_body


# Transport -----------------------------------------------------------------
class TestTransport:
    def test_unknown_network_raises(self):
        with pytest.raises(ValueError, match="Unknown Blofin network"):
            BlofinClient("k", "s", "p", network="mainnet")

    def test_request_signs_and_targets_demo_host(self, client):
        client._session.request.return_value = _resp(BALANCE)
        client.get_balance()
        args, kwargs = client._session.request.call_args
        assert args[0] == "GET"
        assert args[1] == "https://demo-trading-openapi.blofin.com/api/v1/account/balance"
        assert "ACCESS-SIGN" in kwargs["headers"]

    def test_checked_raises_on_nonzero_code(self, client):
        client._session.request.return_value = _resp(ERR_NOT_SUPPORTED)
        with pytest.raises(BlofinError) as e:
            client.get_balance()
        assert e.value.code == "152404"

    def test_429_raises_rate_limit_error(self, client):
        # Tested at the transport level: routing through a retry-decorated read
        # would sleep the real backoff. _request raising a "rate limit" error
        # is what lets retry_on_transient recognize + back off on 429.
        client._session.request.return_value = _resp({}, status=429)
        with pytest.raises(BlofinError, match="rate limit"):
            client._request("GET", "/api/v1/account/balance")

    def test_non_json_raises(self, client):
        bad = MagicMock(status_code=200, text="<html>")
        bad.json = MagicMock(side_effect=ValueError("no json"))
        client._session.request.return_value = bad
        with pytest.raises(BlofinError, match="non-JSON"):
            client.get_balance()


# Reads ---------------------------------------------------------------------
class TestReads:
    def test_get_balance_normalizes_usdt(self, client):
        client._session.request.return_value = _resp(BALANCE)
        b = client.get_balance()
        assert b["available"] == "499997.86"
        assert b["frozen"] == "2.13"
        assert b["total_equity"] == "499486.0"

    def test_positions_long_signed_positive(self, client):
        client._session.request.return_value = _resp(POSITIONS_LONG)
        p = client.get_open_positions()[0]
        assert p["coin"] == "BTC"
        assert p["inst_id"] == "BTC-USDT"
        assert p["size"] == 0.1
        assert p["side"] == "LONG"
        assert p["entry_price"] == "63970.0"
        assert p["liquidation_price"] is None  # "" normalized to None

    def test_positions_short_signed_negative(self, client):
        client._session.request.return_value = _resp(POSITIONS_SHORT)
        p = client.get_open_positions()[0]
        assert p["coin"] == "ETH"
        assert p["size"] == -2.0
        assert p["side"] == "SHORT"
        assert p["liquidation_price"] == "3500.0"

    def test_positions_skips_zero(self, client):
        payload = {"code": "0", "data": [{"instId": "BTC-USDT", "positions": "0"}]}
        client._session.request.return_value = _resp(payload)
        assert client.get_open_positions() == []

    def test_open_orders_passes_instid(self, client):
        client._session.request.return_value = _resp(ORDERS_PENDING)
        orders = client.get_open_orders("BTC-USDT")
        assert orders[0]["orderId"] == "1000128754044"
        assert "instId=BTC-USDT" in client._session.request.call_args[0][1]

    def test_get_all_mids_uses_bid_ask_then_last(self, client):
        client._session.request.return_value = _resp(TICKERS)
        mids = client.get_all_mids()
        assert mids["BTC-USDT"] == 64000.0       # (63999+64001)/2
        assert mids["ETH-USDT"] == 3000.0        # bid/ask 0 → falls back to last

    def test_asset_meta_cached(self, client):
        client._session.request.return_value = _resp(INSTRUMENTS)
        m1 = client.get_asset_meta()
        m2 = client.get_asset_meta()
        assert m1 is m2
        assert m1["BTC-USDT"]["contractValue"] == "0.001"
        assert client._session.request.call_count == 1  # cached

    def test_get_fills_real_fill_price(self, client):
        client._session.request.return_value = _resp(FILLS)
        f = client.get_fills("BTC-USDT")[0]
        assert f["fillPrice"] == "63970"
        assert "fills-history" in client._session.request.call_args[0][1]


# Writes --------------------------------------------------------------------
class TestWrites:
    def test_place_limit_order_body(self, client):
        client._session.request.return_value = _resp(ORDER_OK)
        r = client.place_order(
            inst_id="BTC-USDT", side="buy", order_type="limit", size="0.1",
            price="50000", client_order_id="potion_42_entry",
        )
        assert order_id_of(r) == "1000128754245"
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body["instId"] == "BTC-USDT"
        assert body["price"] == "50000"
        assert body["reduceOnly"] is False
        assert body["clientOrderId"] == "potion_42_entry"

    def test_place_market_order_omits_price(self, client):
        client._session.request.return_value = _resp(ORDER_OK)
        client.place_order(inst_id="BTC-USDT", side="buy", order_type="market", size="0.1")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert "price" not in body

    def test_demo_apply_money_puts_to_account_inside_item(self, client):
        # 2026-06-05 probe: toAccount at the top level is rejected, so it goes
        # inside each demoApplyMoney item. Returns raw (not _checked).
        client._session.request.return_value = _resp({"code": "0", "data": {}})
        client.demo_apply_money(amount="1000", currency="USDT",
                                adjust_type="1", to_account="futures")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert "demo-apply-money" in client._session.request.call_args[0][1]
        assert body["adjustType"] == "1"
        item = body["demoApplyMoney"][0]
        assert item == {"currency": "USDT", "amountStr": "1000", "toAccount": "futures"}
        assert "toAccount" not in body  # not at the top level

    def test_place_reduce_only_tp(self, client):
        client._session.request.return_value = _resp(ORDER_OK)
        client.place_order(
            inst_id="BTC-USDT", side="sell", order_type="limit", size="0.05",
            price="70000", reduce_only=True,
        )
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body["reduceOnly"] is True

    def test_cancel_order_body_and_path(self, client):
        client._session.request.return_value = _resp(CANCEL_OK)
        r = client.cancel_order("BTC-USDT", order_id="1000128754245")
        assert response_ok(r)
        assert client._session.request.call_args[0][1].endswith("/api/v1/trade/cancel-order")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body == {"instId": "BTC-USDT", "orderId": "1000128754245"}

    def test_cancel_gone_is_distinguishable(self, client):
        client._session.request.return_value = _resp(CANCEL_GONE)
        r = client.cancel_order("BTC-USDT", order_id="999")
        assert not response_ok(r)
        assert str(r["code"]) == CANCEL_GONE_CODE  # caller treats as benign

    def test_set_leverage_body(self, client):
        client._session.request.return_value = _resp(
            {"code": "0", "data": {"leverage": "5"}})
        client.set_leverage("BTC-USDT", 5)
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body == {"instId": "BTC-USDT", "leverage": "5",
                        "marginMode": "cross", "positionSide": "net"}

    def test_close_position_body(self, client):
        client._session.request.return_value = _resp(
            {"code": "0", "msg": "success", "data": {"instId": "BTC-USDT"}})
        client.close_position("BTC-USDT")
        assert client._session.request.call_args[0][1].endswith("/api/v1/trade/close-position")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body == {"instId": "BTC-USDT", "marginMode": "cross",
                        "positionSide": "net"}


# TP/SL ---------------------------------------------------------------------
TPSL_OK = {"code": "0", "msg": "Order placed",
           "data": {"tpslId": "10001386047", "clientOrderId": None, "code": "0", "msg": None}}
CANCEL_TPSL_OK = {"code": "0", "msg": "Batch orders canceled",
                  "data": [{"tpslId": "10001386047", "code": "0", "msg": None}]}
TPSL_PENDING = {"code": "0", "msg": "success", "data": [{
    "tpslId": "10001386127", "instId": "BTC-USDT", "side": "sell",
    "slTriggerPrice": "40000", "slOrderPrice": "-1", "size": "0.1", "state": "live"}]}


class TestTpsl:
    def test_place_tpsl_sl_only_body(self, client):
        client._session.request.return_value = _resp(TPSL_OK)
        r = client.place_tpsl(inst_id="BTC-USDT", side="sell", size="0.1",
                              sl_trigger_price="40000")
        assert tpsl_id_of(r) == "10001386047"
        assert client._session.request.call_args[0][1].endswith("/api/v1/trade/order-tpsl")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body["slTriggerPrice"] == "40000"
        assert body["slOrderPrice"] == "-1"
        assert "tpTriggerPrice" not in body
        assert body["reduceOnly"] is True

    def test_place_tpsl_tp_only_body(self, client):
        client._session.request.return_value = _resp(TPSL_OK)
        client.place_tpsl(inst_id="BTC-USDT", side="sell", size="0.05",
                          tp_trigger_price="70000")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body["tpTriggerPrice"] == "70000"
        assert body["tpOrderPrice"] == "-1"
        assert "slTriggerPrice" not in body

    def test_cancel_tpsl_list_body(self, client):
        client._session.request.return_value = _resp(CANCEL_TPSL_OK)
        client.cancel_tpsl("BTC-USDT", "10001386047")
        assert client._session.request.call_args[0][1].endswith("/api/v1/trade/cancel-tpsl")
        body = json.loads(client._session.request.call_args[1]["data"])
        assert body == [{"instId": "BTC-USDT", "tpslId": "10001386047"}]

    def test_get_open_tpsl_orders(self, client):
        client._session.request.return_value = _resp(TPSL_PENDING)
        orders = client.get_open_tpsl_orders("BTC-USDT")
        assert orders[0]["tpslId"] == "10001386127"
        assert "orders-tpsl-pending" in client._session.request.call_args[0][1]

    def test_tpsl_id_of_dict_and_list(self):
        assert tpsl_id_of(TPSL_OK) == "10001386047"        # data is a dict
        assert tpsl_id_of(CANCEL_TPSL_OK) == "10001386047"  # data is a list


# Helpers -------------------------------------------------------------------
class TestHelpers:
    def test_first_item_list_and_dict(self):
        assert first_item([{"a": 1}]) == {"a": 1}
        assert first_item([]) == {}
        assert first_item({"a": 1}) == {"a": 1}
        assert first_item(None) == {}

    def test_response_ok_and_error(self):
        assert response_ok({"code": "0"})
        assert not response_ok({"code": "152404"})
        assert response_error({"code": "0"}) is None
        assert "not supported" in response_error(ERR_NOT_SUPPORTED)

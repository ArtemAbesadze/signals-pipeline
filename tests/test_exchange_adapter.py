"""Tests for the exchange adapter (Phase 6.8) — the seam that keeps the
pipeline exchange-agnostic.

Each adapter wraps a mocked client + real TradeDatabase. We assert it selects
the right position manager, order builder, balance field, symbol map, and
minimum-size handling — and that the factory dispatches correctly."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.exchange.adapter import (
    BlofinAdapter,
    HyperliquidAdapter,
    build_adapter,
    build_position_manager,
)
from src.exchange.blofin_position_manager import BlofinPositionManager
from src.exchange.position_manager import PositionManager
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side
from src.state.database import TradeDatabase

HL_META = {
    "BTC": {"szDecimals": 5, "maxLeverage": 50, "tickSize": 0.1},
}
BLOFIN_META = {
    "BTC-USDT": {"contractValue": "0.001", "lotSize": "0.1", "minSize": "0.1",
                 "tickSize": "0.1", "maxLeverage": "150"},
}


def _signal():
    return ParsedSignal(
        pair="BTC/USDT", trade_id=7000001, risk_level=RiskLevel.MEDIUM,
        trade_type="SWING", size="1-4%", side=Side.LONG, entry=50000.0,
        stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=55000.0, leverage=10,
    )


class _Preset:
    tp_split = [0.33, 0.33, 0.34]


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as d:
        database = TradeDatabase(user_id="u", db_path=Path(d) / "t.db")
        yield database
        database.close()


@pytest.fixture
def hl_client():
    c = MagicMock()
    c.get_asset_meta.return_value = HL_META
    c.get_balance.return_value = {"usdc_balance": "649.00", "available": "1.49"}
    return c


@pytest.fixture
def blofin_client():
    c = MagicMock()
    c.get_asset_meta.return_value = BLOFIN_META
    # Blofin balance carries both; adapter must read ``available``, not equity.
    c.get_balance.return_value = {"available": "500000", "total_equity": "510000"}
    return c


class TestFactory:
    def test_default_is_hyperliquid(self, hl_client, db):
        assert isinstance(build_adapter(None, hl_client, db), HyperliquidAdapter)
        assert isinstance(build_adapter("hyperliquid", hl_client, db), HyperliquidAdapter)

    def test_blofin_selected(self, blofin_client, db):
        assert isinstance(build_adapter("blofin", blofin_client, db), BlofinAdapter)

    def test_case_insensitive(self, blofin_client, db):
        assert isinstance(build_adapter("BLOFIN", blofin_client, db), BlofinAdapter)

    def test_unknown_raises(self, hl_client, db):
        with pytest.raises(ValueError, match="Unknown exchange"):
            build_adapter("ftx", hl_client, db)

    def test_build_position_manager_dispatch(self, hl_client, blofin_client, db):
        assert isinstance(build_position_manager("hyperliquid", hl_client, db), PositionManager)
        assert isinstance(build_position_manager("blofin", blofin_client, db), BlofinPositionManager)
        with pytest.raises(ValueError, match="Unknown exchange"):
            build_position_manager("ftx", hl_client, db)

    def test_build_position_manager_does_not_fetch_meta(self, blofin_client, db):
        # The orchestrator's sync/kill path must not pay an extra get_asset_meta
        # round-trip that the full adapter would.
        build_position_manager("blofin", blofin_client, db)
        blofin_client.get_asset_meta.assert_not_called()


class TestHyperliquidAdapter:
    def test_selects_hl_position_manager(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        assert isinstance(a.position_manager, PositionManager)
        assert a.name == "hyperliquid"

    def test_caches_asset_meta(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        assert a.asset_meta == HL_META
        hl_client.get_asset_meta.assert_called_once()

    def test_wallet_balance_reads_usdc(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        assert a.wallet_balance_usd() == 649.00

    def test_map_symbol_hl(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        assert a.map_symbol("BTC/USDT") == "BTC"

    def test_build_trade_set_applies_min_floor(self, hl_client, db):
        # A sub-$10 size is bumped to the $10 HL minimum notional before
        # building: $2 and $10 both produce the size that $10/entry yields.
        a = HyperliquidAdapter(hl_client, db)
        small = a.build_trade_set(_signal(), 2.0, _Preset(), max_leverage=20)
        floored = a.build_trade_set(_signal(), 10.0, _Preset(), max_leverage=20)
        assert small.coin == "BTC"
        assert a.entry_size(small) == a.entry_size(floored)  # both bumped to $10
        assert a.entry_size(small) == small.entry.sz

    def test_entry_size_uses_sz(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        ts = MagicMock()
        ts.entry.sz = 0.123
        assert a.entry_size(ts) == 0.123


class TestBlofinAdapter:
    def test_selects_blofin_position_manager(self, blofin_client, db):
        a = BlofinAdapter(blofin_client, db)
        assert isinstance(a.position_manager, BlofinPositionManager)
        assert a.name == "blofin"

    def test_wallet_balance_reads_available_not_equity(self, blofin_client, db):
        a = BlofinAdapter(blofin_client, db)
        assert a.wallet_balance_usd() == 500000.0

    def test_map_symbol_blofin(self, blofin_client, db):
        a = BlofinAdapter(blofin_client, db)
        assert a.map_symbol("BTC/USDT") == "BTC-USDT"

    def test_build_trade_set_no_floor(self, blofin_client, db):
        # Blofin enforces instrument minSize itself; no $10 bump. A real size
        # builds a contract-denominated set with inst_id.
        a = BlofinAdapter(blofin_client, db)
        ts = a.build_trade_set(_signal(), 500.0, _Preset(), max_leverage=50)
        assert ts.inst_id == "BTC-USDT"
        assert ts.coin == "BTC"
        assert a.entry_size(ts) == ts.entry.size

    def test_entry_size_uses_size(self, blofin_client, db):
        a = BlofinAdapter(blofin_client, db)
        ts = MagicMock()
        ts.entry.size = 4.5
        assert a.entry_size(ts) == 4.5


_ORDERS_HISTORY = [
    {"clientOrderId": "potion_7000001_entry", "algoClientOrderId": "",
     "averagePrice": "4.912", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_tp1",
     "averagePrice": "4.887", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_stop_loss",
     "averagePrice": "4.913", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_tp2",
     "averagePrice": "4.863", "state": "live"},  # not filled → ignored
]


class _OT:
    """Minimal order_type stand-in exposing .value."""
    def __init__(self, value): self.value = value


class TestRealFillPrice:
    def test_hyperliquid_returns_none(self, hl_client, db):
        # HL has no order-history endpoint → always None (caller keeps CP est).
        a = HyperliquidAdapter(hl_client, db)
        assert a.real_fill_price("BTC", 7000001, _OT("entry")) is None

    def test_blofin_entry_matches_client_order_id(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _ORDERS_HISTORY
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("entry")) == 4.912
        blofin_client.get_orders_history.assert_called_once_with("INJ-USDT")

    def test_blofin_tp_matches_algo_client_order_id(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _ORDERS_HISTORY
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("tp1")) == 4.887
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("stop_loss")) == 4.913

    def test_blofin_unfilled_state_ignored(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _ORDERS_HISTORY
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("tp2")) is None  # state=live

    def test_blofin_moved_sl_suffix_matches(self, blofin_client, db):
        # A breakeven-moved SL is placed as potion_{id}_stop_loss_{n} (the
        # unique-clientOrderId fix, 2026-06-26). Its real fill price must still
        # resolve for order_type stop_loss via the suffix-aware match.
        blofin_client.get_orders_history.return_value = [
            {"clientOrderId": "", "algoClientOrderId": "potion_7000001_stop_loss_1",
             "averagePrice": "4.901", "state": "filled"},
        ]
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("stop_loss")) == 4.901

    def test_blofin_no_match_returns_none(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _ORDERS_HISTORY
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 9999999, _OT("entry")) is None

    def test_blofin_query_failure_returns_none(self, blofin_client, db):
        blofin_client.get_orders_history.side_effect = RuntimeError("boom")
        a = BlofinAdapter(blofin_client, db)
        assert a.real_fill_price("INJ-USDT", 7000001, _OT("entry")) is None


_PNL_HISTORY = [
    {"clientOrderId": "potion_7000001_entry", "algoClientOrderId": "",
     "pnl": "0", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_tp1",
     "pnl": "0.335", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_stop_loss",
     "pnl": "-0.0273", "state": "filled"},
    {"clientOrderId": "", "algoClientOrderId": "potion_7000099_tp1",
     "pnl": "99.0", "state": "filled"},          # different trade — excluded
    {"clientOrderId": "", "algoClientOrderId": "potion_7000001_tp2",
     "pnl": "5.0", "state": "live"},             # not filled — excluded
]


class TestRealizedPnl:
    def test_hyperliquid_returns_none(self, hl_client, db):
        a = HyperliquidAdapter(hl_client, db)
        assert a.realized_pnl("BTC", 7000001) is None

    def test_blofin_sums_pnl_for_trade(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _PNL_HISTORY
        a = BlofinAdapter(blofin_client, db)
        # 0 + 0.335 + (-0.0273), excluding the other trade + the unfilled row
        assert a.realized_pnl("INJ-USDT", 7000001) == pytest.approx(0.3077)

    def test_blofin_no_match_returns_none(self, blofin_client, db):
        blofin_client.get_orders_history.return_value = _PNL_HISTORY
        a = BlofinAdapter(blofin_client, db)
        assert a.realized_pnl("INJ-USDT", 8888888) is None

    def test_blofin_query_failure_returns_none(self, blofin_client, db):
        blofin_client.get_orders_history.side_effect = RuntimeError("boom")
        a = BlofinAdapter(blofin_client, db)
        assert a.realized_pnl("INJ-USDT", 7000001) is None

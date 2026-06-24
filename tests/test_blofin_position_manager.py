"""Tests for BlofinPositionManager (Phase 6.5) — submit/cancel/close/move/sync.

BlofinClient is mocked; a real TradeDatabase is used. Mirrors the HL
position-manager coverage."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.exchange.blofin_order_builder import build_blofin_orders
from src.exchange.blofin_position_manager import BlofinPositionManager
from src.exchange.position_manager import OrderSubmissionError
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side
from src.state.database import TradeDatabase
from src.state.models import OrderStatus, OrderType, TradeRecord, TradeStatus

META = {
    "BTC-USDT": {"contractValue": "0.001", "lotSize": "0.1", "minSize": "0.1",
                 "tickSize": "0.1", "maxLeverage": "150"},
}


def _signal(trade_id=7000001, side=Side.LONG):
    return ParsedSignal(
        pair="BTC/USDT", trade_id=trade_id, risk_level=RiskLevel.MEDIUM,
        trade_type="SWING", size="1-4%", side=side, entry=50000.0,
        stop_loss=49000.0, tp1=51000.0, tp2=52000.0, tp3=55000.0, leverage=10,
    )


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as d:
        database = TradeDatabase(user_id="u", db_path=Path(d) / "t.db")
        yield database
        database.close()


@pytest.fixture
def client():
    c = MagicMock()
    c.set_leverage.return_value = {"code": "0", "data": {}}
    c.place_order.return_value = {"code": "0", "data": [{"orderId": "O1", "code": "0"}]}
    _tids = iter(["T_SL", "T_TP1", "T_TP2", "T_TP3"])
    c.place_tpsl.side_effect = lambda **k: {
        "code": "0", "data": {"tpslId": next(_tids), "code": "0"}}
    c.cancel_order.return_value = {"code": "0", "data": [{"orderId": "O1", "code": "0"}]}
    c.cancel_tpsl.return_value = {"code": "0", "data": [{"tpslId": "T", "code": "0"}]}
    c.close_position.return_value = {"code": "0", "data": {"instId": "BTC-USDT"}}
    c.get_open_positions.return_value = []
    c.get_open_orders.return_value = []
    c.get_open_tpsl_orders.return_value = []
    # Above the $50k entry → a long's breakeven SL (at entry) is below market = valid.
    c.get_all_mids.return_value = {"BTC-USDT": 51000.0}
    return c


@pytest.fixture
def pm(client, db):
    return BlofinPositionManager(client, db)


def _seed_trade(db, trade_id=7000001, side=Side.LONG, status=None):
    s = _signal(trade_id, side)
    ts = build_blofin_orders(s, 500.0, META)
    db.create_trade(TradeRecord(
        trade_id=trade_id, user_id="u", pair=s.pair, coin=ts.coin,
        side=side.value, risk_level="MEDIUM", trade_type="SWING", size_hint="1-4%",
        entry_price=s.entry, stop_loss=s.stop_loss, tp1=s.tp1, tp2=s.tp2, tp3=s.tp3,
        leverage=ts.leverage, signal_leverage=s.leverage,
        position_size_usd=500.0, position_size_coin=ts.entry.size,
    ))
    if status:
        db.update_trade_status(trade_id, status)
    return ts


def _pos(inst="BTC-USDT", size=10.0):
    return {"inst_id": inst, "coin": inst.split("-")[0], "size": size,
            "margin_mode": "cross", "side": "LONG" if size > 0 else "SHORT"}


class TestSubmit:
    def test_records_five_orders_with_ids(self, pm, db, client):
        ts = _seed_trade(db)
        assert pm.submit_trade(ts) is True

        orders = {o.order_type: o for o in db.get_orders_for_trade(7000001)}
        assert set(orders) == {OrderType.ENTRY, OrderType.STOP_LOSS,
                               OrderType.TP1, OrderType.TP2, OrderType.TP3}
        assert orders[OrderType.ENTRY].oid == "O1"
        assert orders[OrderType.STOP_LOSS].oid == "T_SL"
        assert all(o.status == OrderStatus.SUBMITTED for o in orders.values())
        client.set_leverage.assert_called_once()
        assert client.place_order.call_count == 1
        assert client.place_tpsl.call_count == 4  # SL + 3 TPs

    def test_entry_uses_regular_order_tps_use_tpsl(self, pm, db, client):
        ts = _seed_trade(db)
        pm.submit_trade(ts)
        # entry side buy; SL/TP side sell (closing a long)
        assert client.place_order.call_args[1]["side"] == "buy"
        sl_call = client.place_tpsl.call_args_list[0][1]
        assert sl_call["side"] == "sell"
        assert sl_call["sl_trigger_price"] is not None
        assert sl_call["tp_trigger_price"] is None

    def test_leverage_failure_does_not_abort_trade(self, pm, db, client):
        # Blofin rejects a leverage change while the instrument has open
        # orders/position; the trade must still place (at current leverage),
        # not be dropped. Surfaced by the 2026-06-05 soak (POL/WIF collisions).
        ts = _seed_trade(db)
        client.set_leverage.side_effect = Exception(
            "You have pending cross orders. Please cancel them before adjusting "
            "your leverage."
        )
        assert pm.submit_trade(ts) is True
        client.place_order.assert_called_once()        # entry still placed
        assert client.place_tpsl.call_count == 4       # SL + 3 TPs still placed
        orders = {o.order_type: o for o in db.get_orders_for_trade(7000001)}
        assert orders[OrderType.ENTRY].oid == "O1"

    def test_entry_rejected_raises(self, pm, db, client):
        ts = _seed_trade(db)
        client.place_order.return_value = {"code": "0", "data": [
            {"orderId": None, "code": "152002", "msg": "insufficient balance"}]}
        with pytest.raises(OrderSubmissionError, match="Entry order rejected"):
            pm.submit_trade(ts)

    def test_tpsl_rejection_logged_not_aborting(self, pm, db, client):
        ts = _seed_trade(db)
        # SL rejected, TPs ok
        results = iter([
            {"code": "0", "data": {"code": "9999", "msg": "sl bad"}},
            {"code": "0", "data": {"tpslId": "T_TP1", "code": "0"}},
            {"code": "0", "data": {"tpslId": "T_TP2", "code": "0"}},
            {"code": "0", "data": {"tpslId": "T_TP3", "code": "0"}},
        ])
        client.place_tpsl.side_effect = lambda **k: next(results)
        assert pm.submit_trade(ts) is True
        orders = {o.order_type: o for o in db.get_orders_for_trade(7000001)}
        # SL recorded but no oid (rejected); TPs got ids
        assert orders[OrderType.STOP_LOSS].oid is None
        assert orders[OrderType.TP1].oid == "T_TP1"


class TestCancel:
    def test_routes_by_order_type(self, pm, db, client):
        ts = _seed_trade(db)
        pm.submit_trade(ts)
        pm.cancel_trade(7000001)
        # entry → cancel_order with the instId; SL/TPs → cancel_tpsl
        client.cancel_order.assert_called_once_with("BTC-USDT", order_id="O1")
        assert client.cancel_tpsl.call_count == 4
        assert db.get_trade(7000001).status == TradeStatus.CANCELED
        assert all(o.status == OrderStatus.CANCELED
                   for o in db.get_orders_for_trade(7000001))

    def test_benign_102068_still_marks_canceled(self, pm, db, client):
        ts = _seed_trade(db)
        pm.submit_trade(ts)
        client.cancel_tpsl.return_value = {
            "code": "0", "data": [{"tpslId": "T", "code": "102068"}]}
        client.cancel_order.return_value = {"code": "102068", "msg": "gone"}
        pm.cancel_trade(7000001)
        orders = {o.order_type: o for o in db.get_orders_for_trade(7000001)}
        assert orders[OrderType.ENTRY].status == OrderStatus.CANCELED
        assert orders[OrderType.STOP_LOSS].status == OrderStatus.CANCELED


class TestClose:
    def test_no_position_marks_closed_without_close_call(self, pm, db, client):
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.get_open_positions.return_value = []
        pm.close_position(7000001, "BTC", reason="manual_close")
        assert db.get_trade(7000001).status == TradeStatus.CLOSED
        client.close_position.assert_not_called()

    def test_with_position_closes_and_marks_closed(self, pm, db, client):
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.get_open_positions.return_value = [_pos()]
        pm.close_position(7000001, "BTC", reason="stop")
        client.close_position.assert_called_once()
        assert client.close_position.call_args[0][0] == "BTC-USDT"
        assert db.get_trade(7000001).status == TradeStatus.CLOSED

    def test_close_rejected_raises_and_not_closed(self, pm, db, client):
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.get_open_positions.return_value = [_pos()]
        client.close_position.return_value = {"code": "152404", "msg": "nope"}
        with pytest.raises(OrderSubmissionError, match="Close rejected"):
            pm.close_position(7000001, "BTC")
        assert db.get_trade(7000001).status != TradeStatus.CLOSED


class TestMoveSl:
    def test_breakeven_cancels_old_places_new(self, pm, db, client):
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.place_tpsl.side_effect = lambda **k: {
            "code": "0", "data": {"tpslId": "T_NEW_SL", "code": "0"}}
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is True
        client.cancel_tpsl.assert_called()  # old SL canceled
        sls = [o for o in db.get_orders_for_trade(7000001)
               if o.order_type == OrderType.STOP_LOSS]
        # old SL canceled, new SL submitted at entry
        assert any(o.status == OrderStatus.CANCELED for o in sls)
        assert any(o.status == OrderStatus.SUBMITTED and o.oid == "T_NEW_SL" for o in sls)

    def test_no_active_sl_returns_false(self, pm, db):
        _seed_trade(db, status=TradeStatus.OPEN)  # no orders submitted
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is False

    def test_be_skipped_when_price_crossed_back_keeps_old_sl(self, pm, db, client):
        # Long position, but price dropped below entry → breakeven SL (at entry)
        # would be ABOVE market = rejected by Blofin. Must keep the old SL, not
        # cancel it (no unprotected window). 2026-06-05 soak case.
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.cancel_tpsl.reset_mock()
        client.get_all_mids.return_value = {"BTC-USDT": 49500.0}  # below entry 50000
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is False
        client.cancel_tpsl.assert_not_called()        # old SL untouched
        sls = [o for o in db.get_orders_for_trade(7000001)
               if o.order_type == OrderType.STOP_LOSS]
        assert all(o.status == OrderStatus.SUBMITTED for o in sls)  # still protected

    def test_new_sl_rejected_keeps_old_sl(self, pm, db, client):
        # Price passes the fast pre-check but the venue still rejects the new SL.
        # Place-then-cancel means the old SL is never touched — position stays
        # protected (ADA #2262 fix; replaces the old cancel-then-reinstate path).
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.cancel_tpsl.reset_mock()
        client.place_tpsl.side_effect = lambda **k: {
            "code": "0", "data": {"code": "9999", "msg": "rejected"}}  # new SL rejected
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is False
        client.cancel_tpsl.assert_not_called()          # old SL never canceled
        sls = [o for o in db.get_orders_for_trade(7000001)
               if o.order_type == OrderType.STOP_LOSS]
        # original SL still live; the rejected attempt is marked REJECTED (no orphan)
        assert any(o.status == OrderStatus.SUBMITTED and o.oid == "T_SL" for o in sls)
        assert any(o.status == OrderStatus.REJECTED for o in sls)

    def test_duplicate_breakeven_is_noop(self, pm, db, client):
        # The exact ADA #2262 scenario: a 2nd breakeven message must NOT re-move
        # an already-at-BE SL (the old code canceled it, then failed to replace
        # it, leaving the position unprotected). Idempotent → no API calls.
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        client.place_tpsl.side_effect = lambda **k: {
            "code": "0", "data": {"tpslId": "T_BE", "code": "0"}}
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is True   # 1st move
        client.place_tpsl.reset_mock()
        client.cancel_tpsl.reset_mock()
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is True   # 2nd (dup)
        client.place_tpsl.assert_not_called()           # no destructive re-move
        client.cancel_tpsl.assert_not_called()

    def test_new_sl_placed_before_old_canceled(self, pm, db, client):
        # Ordering guarantee: the new SL is placed BEFORE the old is canceled,
        # so there is never a window without a stop on the book.
        ts = _seed_trade(db, status=TradeStatus.OPEN)
        pm.submit_trade(ts)
        calls = []
        client.place_tpsl.side_effect = lambda **k: (
            calls.append("place"),
            {"code": "0", "data": {"tpslId": "T_NEW_SL", "code": "0"}})[1]
        client.cancel_tpsl.side_effect = lambda *a, **k: (
            calls.append("cancel"),
            {"code": "0", "data": [{"tpslId": "T", "code": "0"}]})[1]
        assert pm.move_sl_to_breakeven(7000001, "BTC", 50000.0) is True
        assert calls == ["place", "cancel"]   # place first, then cancel


class TestSync:
    def test_open_no_position_closed(self, pm, db, client):
        _seed_trade(db, status=TradeStatus.OPEN)
        client.get_open_positions.return_value = []
        summary = pm.sync_positions()
        assert 7000001 in summary["closed"]
        assert db.get_trade(7000001).status == TradeStatus.CLOSED

    def test_pending_entry_resting_verified(self, pm, db, client):
        ts = _seed_trade(db)  # PENDING
        pm.submit_trade(ts)   # entry oid = O1
        client.get_open_orders.return_value = [{"orderId": "O1", "instId": "BTC-USDT"}]
        summary = pm.sync_positions()
        assert 7000001 in summary["verified"]
        assert db.get_trade(7000001).status == TradeStatus.PENDING

    def test_pending_position_promoted_to_open(self, pm, db, client):
        ts = _seed_trade(db)
        pm.submit_trade(ts)
        client.get_open_orders.return_value = []        # entry no longer resting
        client.get_open_positions.return_value = [_pos()]
        summary = pm.sync_positions()
        assert 7000001 in summary["verified"]
        assert db.get_trade(7000001).status == TradeStatus.OPEN

    def test_pending_nothing_canceled(self, pm, db, client):
        ts = _seed_trade(db)
        pm.submit_trade(ts)
        client.get_open_orders.return_value = []
        client.get_open_positions.return_value = []
        summary = pm.sync_positions()
        assert 7000001 in summary["canceled"]
        assert db.get_trade(7000001).status == TradeStatus.CANCELED

    def test_orphan_position(self, pm, db, client):
        client.get_open_positions.return_value = [_pos(inst="ETH-USDT")]
        summary = pm.sync_positions()
        assert "ETH-USDT" in summary["orphans"]

"""Tests for TradeDatabase — focuses on the D3 audit-log surface (events
table, raw_signal_text, decision_snapshot). The trade lifecycle queries
themselves are covered indirectly via the e2e tests."""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.state.database import TradeDatabase
from src.state.models import EventType, TradeRecord, TradeStatus


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "trades.db"
        d = TradeDatabase(user_id="alice", db_path=path)
        yield d
        d.close()


def _trade(trade_id: int = 1, **overrides) -> TradeRecord:
    """Build a TradeRecord with sensible defaults for testing."""
    base = dict(
        trade_id=trade_id,
        user_id="alice",
        pair="BTC/USDT",
        coin="BTC",
        side="LONG",
        risk_level="MEDIUM",
        trade_type="SWING",
        size_hint="1-4%",
        entry_price=50000.0,
        stop_loss=49000.0,
        tp1=51000.0,
        tp2=52000.0,
        tp3=55000.0,
        leverage=10,
        signal_leverage=10,
        position_size_usd=100.0,
        position_size_coin=0.002,
    )
    base.update(overrides)
    return TradeRecord(**base)


# ====================================================================
# Schema + migration
# ====================================================================


class TestSchema:
    def test_trade_events_table_exists(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_events'"
        )
        assert cursor.fetchone() is not None

    def test_trade_events_columns(self, db):
        cursor = db._conn.execute("PRAGMA table_info(trade_events)")
        cols = {row[1] for row in cursor.fetchall()}
        assert {"id", "trade_id", "user_id", "occurred_at",
                "event_type", "raw_text", "action_taken"} <= cols

    def test_trades_has_audit_columns(self, db):
        cursor = db._conn.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "raw_signal_text" in cols
        assert "decision_snapshot" in cols

    def test_three_event_indexes_exist(self, db):
        cursor = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='trade_events'"
        )
        names = {row[0] for row in cursor.fetchall()}
        assert "idx_events_trade" in names
        assert "idx_events_type" in names
        assert "idx_events_time" in names


class TestLegacyMigration:
    def test_migration_adds_audit_columns(self, tmp_path):
        """Simulate a pre-D3 trades schema and verify migration patches it."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        # Pre-D3 trades schema (no raw_signal_text / decision_snapshot)
        conn.execute(
            "CREATE TABLE trades ("
            "trade_id INTEGER, user_id TEXT, pair TEXT, coin TEXT, "
            "side TEXT, risk_level TEXT, trade_type TEXT, size_hint TEXT, "
            "entry_price REAL, stop_loss REAL, tp1 REAL, tp2 REAL, tp3 REAL, "
            "leverage INTEGER, signal_leverage INTEGER, "
            "position_size_usd REAL, position_size_coin REAL, "
            "status TEXT, created_at TEXT, updated_at TEXT, "
            "closed_at TEXT, close_reason TEXT, pnl_pct REAL)"
        )
        conn.commit()
        conn.close()

        # Opening via TradeDatabase should add the columns
        db = TradeDatabase(user_id="alice", db_path=db_path)
        cursor = db._conn.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "raw_signal_text" in cols
        assert "decision_snapshot" in cols
        db.close()


# ====================================================================
# Trade audit fields round-trip
# ====================================================================


class TestTradeAuditFields:
    def test_raw_signal_text_round_trips(self, db):
        msg = "TRADING SIGNAL ALERT\nverbatim CP text"
        db.create_trade(_trade(raw_signal_text=msg))
        trade = db.get_trade(1)
        assert trade.raw_signal_text == msg

    def test_decision_snapshot_round_trips(self, db):
        snap = {
            "preset": "even_split",
            "size_pct_applied": 2.0,
            "port_usd_at_open": 1000.0,
            "why": "test",
        }
        db.create_trade(_trade(decision_snapshot=snap))
        trade = db.get_trade(1)
        assert trade.decision_snapshot == snap

    def test_decision_snapshot_stored_as_json(self, db):
        snap = {"preset": "tp1_only", "exposure_used_pct": 14.5}
        db.create_trade(_trade(decision_snapshot=snap))
        # Inspect the raw column to confirm it's JSON, not a Python repr
        row = db._conn.execute(
            "SELECT decision_snapshot FROM trades WHERE trade_id = 1"
        ).fetchone()
        assert json.loads(row["decision_snapshot"]) == snap

    def test_null_audit_fields_round_trip(self, db):
        db.create_trade(_trade())
        trade = db.get_trade(1)
        assert trade.raw_signal_text is None
        assert trade.decision_snapshot is None


# ====================================================================
# trade_events CRUD
# ====================================================================


class TestRecordEvent:
    def test_record_returns_autoincrement_id(self, db):
        a = db.record_event(1, EventType.SIGNAL_ALERT, "raw", "opened")
        b = db.record_event(1, EventType.TP_HIT, "raw2", "TP1")
        assert b > a

    def test_record_with_null_trade_id(self, db):
        """Orphan error events have no trade_id."""
        rid = db.record_event(None, EventType.ERROR, "garbage", "parse error")
        events = db.get_events_for_user()
        assert len(events) == 1
        assert events[0].id == rid
        assert events[0].trade_id is None

    def test_get_events_for_trade_chronological(self, db):
        db.record_event(42, EventType.SIGNAL_ALERT, "r1", "open")
        db.record_event(42, EventType.TP_HIT, "r2", "tp1")
        db.record_event(42, EventType.TRADE_CLOSED, "r3", "close")
        events = db.get_events_for_trade(42)
        assert [e.event_type for e in events] == [
            EventType.SIGNAL_ALERT,
            EventType.TP_HIT,
            EventType.TRADE_CLOSED,
        ]

    def test_get_events_for_user_filtered_by_type(self, db):
        db.record_event(1, EventType.SIGNAL_ALERT, "r1", "open")
        db.record_event(2, EventType.ERROR, "r2", "bad msg")
        db.record_event(3, EventType.TP_HIT, "r3", "tp")
        db.record_event(None, EventType.ERROR, "r4", "orphan err")
        errs = db.get_events_for_user(event_type=EventType.ERROR)
        assert len(errs) == 2
        assert all(e.event_type == EventType.ERROR for e in errs)

    def test_get_events_for_user_limit(self, db):
        for i in range(20):
            db.record_event(1, EventType.SIGNAL_ALERT, f"r{i}", f"a{i}")
        recent = db.get_events_for_user(limit=5)
        assert len(recent) == 5

    def test_event_isolated_by_user(self, tmp_path):
        """Two users sharing one DB file see only their own events."""
        path = tmp_path / "shared.db"
        a = TradeDatabase(user_id="alice", db_path=path)
        b = TradeDatabase(user_id="bob", db_path=path)
        a.record_event(1, EventType.SIGNAL_ALERT, "alice's", "open")
        b.record_event(2, EventType.SIGNAL_ALERT, "bob's", "open")
        assert len(a.get_events_for_user()) == 1
        assert len(b.get_events_for_user()) == 1
        assert a.get_events_for_user()[0].raw_text == "alice's"
        a.close()
        b.close()


class TestEventTypeEnum:
    def test_ten_event_types(self):
        """D3 spec + Phase 1.2 additions = 10."""
        expected = {
            "signal_alert", "order_pending", "trade_live",
            "tp_hit", "breakeven", "stop_hit", "sl_move",
            "trade_closed", "cancel", "error",
        }
        assert {e.value for e in EventType} == expected


# ====================================================================
# Port-change history (Phase 2.2 Commit B)
# ====================================================================


class TestGetPortChanges:
    """Per-closed-trade port-delta candidates surfaced for the Port screen."""

    def _close_with_pnl(self, db, trade_id: int, pnl_pct: float, coin: str = "BTC"):
        trade = _trade(trade_id=trade_id, coin=coin, position_size_usd=100.0)
        db.create_trade(trade)
        db.update_trade_status(
            trade_id, TradeStatus.CLOSED, close_reason="all_tp_hit", pnl_pct=pnl_pct,
        )

    def test_returns_recent_closed_trades_with_realized_pnl(self, db):
        self._close_with_pnl(db, trade_id=1, pnl_pct=10.0)   # +$10 delta
        self._close_with_pnl(db, trade_id=2, pnl_pct=-25.0)  # -$25 delta
        changes = db.get_port_changes(limit=5)
        assert len(changes) == 2
        # most-recent first; #2 came in second so it's first in DESC
        deltas = {c["trade_id"]: c["delta_usd"] for c in changes}
        assert deltas[1] == pytest.approx(10.0)
        assert deltas[2] == pytest.approx(-25.0)

    def test_skips_open_trades(self, db):
        db.create_trade(_trade(trade_id=1))  # still PENDING
        changes = db.get_port_changes()
        assert changes == []

    def test_skips_canceled_trades_without_pnl(self, db):
        db.create_trade(_trade(trade_id=1))
        db.update_trade_status(1, TradeStatus.CANCELED, close_reason="canceled")
        changes = db.get_port_changes()
        assert changes == []

    def test_includes_canceled_with_pnl(self, db):
        """Closed trades with pnl_pct set are included regardless of close_reason."""
        db.create_trade(_trade(trade_id=1, position_size_usd=200.0))
        db.update_trade_status(
            1, TradeStatus.CLOSED, close_reason="stop_hit", pnl_pct=-30.0,
        )
        changes = db.get_port_changes()
        assert len(changes) == 1
        assert changes[0]["delta_usd"] == pytest.approx(-60.0)
        assert changes[0]["close_reason"] == "stop_hit"

    def test_limit_respected(self, db):
        for i in range(1, 11):
            self._close_with_pnl(db, trade_id=i, pnl_pct=5.0)
        assert len(db.get_port_changes(limit=3)) == 3
        assert len(db.get_port_changes(limit=100)) == 10

    def test_user_isolation(self, tmp_path):
        from src.state.database import TradeDatabase
        path = tmp_path / "shared.db"
        a = TradeDatabase(user_id="alice", db_path=path)
        b = TradeDatabase(user_id="bob", db_path=path)
        # alice's closed trade
        a.create_trade(_trade(trade_id=1))
        a.update_trade_status(1, TradeStatus.CLOSED, close_reason="all_tp_hit", pnl_pct=10.0)
        assert len(a.get_port_changes()) == 1
        assert b.get_port_changes() == []
        a.close()
        b.close()

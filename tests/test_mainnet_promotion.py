"""Phase 3.5 — Mainnet promotion gate tests.

Covers:
  - UserDatabase.apply_mainnet_defaults — applies the tighter position cap
  - Pipeline gate sets requires_confirmation on big mainnet auto-execute trades
  - Gate does NOT trigger for: testnet, small trades, manual mode
  - Database persists / round-trips requires_confirmation
  - Pipeline emits CONFIRMATION_REQUESTED audit event when gate triggers
  - ConfirmationSweeper expires stale trades + records CONFIRMATION_TIMEOUT
  - Approval handler records CONFIRMATION_APPROVED / DECLINED events
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from src.crypto import reset_fernet
from src.state.database import TradeDatabase
from src.state.models import EventType, TradeRecord, TradeStatus
from src.state.user_db import UserDatabase
from src.telegram.confirmation_sweeper import ConfirmationSweeper
from src.telegram.handlers.approval import signal_approval_callback


@pytest.fixture(autouse=True)
def _encryption_key():
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def user_db(tmpdir):
    udb = UserDatabase(db_path=tmpdir / "test.db")
    yield udb
    udb.close()


@pytest.fixture
def trade_db(tmpdir):
    return TradeDatabase(user_id="alice", db_path=tmpdir / "test.db")


SAMPLE_CREDS = {
    "account_address": "0xABC",
    "api_wallet": "0xWALLET",
    "api_secret": "0xSECRET",
    "network": "testnet",
}


# ====================================================================
# UserDatabase.apply_mainnet_defaults
# ====================================================================

class TestApplyMainnetDefaults:
    """Tighter position cap is applied on mainnet promotion."""

    def test_lowers_position_cap_from_default(self, user_db):
        user_db.create_user("alice", "Alice", SAMPLE_CREDS)
        cfg_before = user_db.get_user_config("alice")
        assert cfg_before["max_position_size_usd"] == 500.0

        user_db.apply_mainnet_defaults("alice")

        cfg_after = user_db.get_user_config("alice")
        assert cfg_after["max_position_size_usd"] == 100.0

    def test_does_not_touch_auto_execute(self, user_db):
        """If the user already turned auto_execute ON, we don't silently
        flip it OFF — that would be a surprise. The big-trade gate handles
        the auto-execute-on case via per-trade confirmation."""
        user_db.create_user(
            "alice", "Alice", SAMPLE_CREDS,
            config={"auto_execute": True},
        )
        user_db.apply_mainnet_defaults("alice")
        cfg = user_db.get_user_config("alice")
        assert cfg["auto_execute"] is True

    def test_other_config_fields_untouched(self, user_db):
        user_db.create_user(
            "alice", "Alice", SAMPLE_CREDS,
            config={"max_leverage": 7, "active_preset": "tp1_only"},
        )
        user_db.apply_mainnet_defaults("alice")
        cfg = user_db.get_user_config("alice")
        assert cfg["max_leverage"] == 7
        assert cfg["active_preset"] == "tp1_only"


# ====================================================================
# Database round-trip — requires_confirmation column
# ====================================================================

class TestRequiresConfirmationColumn:
    """The new column persists and round-trips via create_trade / get_trade."""

    def _make_trade(self, **overrides) -> TradeRecord:
        defaults = dict(
            trade_id=42, user_id="alice", pair="BTC/USDT", coin="BTC",
            side="LONG", risk_level="LOW", trade_type="SWING",
            size_hint="1-4%", entry_price=50000.0, stop_loss=48000.0,
            tp1=51000.0, tp2=52000.0, tp3=53000.0,
            leverage=10, signal_leverage=20,
            position_size_usd=250.0, position_size_coin=0.005,
        )
        defaults.update(overrides)
        return TradeRecord(**defaults)

    def test_default_is_false(self, trade_db):
        trade_db.create_trade(self._make_trade())
        row = trade_db.get_trade(42)
        assert row.requires_confirmation is False

    def test_true_round_trips(self, trade_db):
        trade_db.create_trade(self._make_trade(trade_id=43, requires_confirmation=True))
        row = trade_db.get_trade(43)
        assert row.requires_confirmation is True

    def test_get_expired_confirmations_filters(self, trade_db):
        # One old confirmation, one fresh — only the old should be returned.
        old_trade = self._make_trade(trade_id=100, requires_confirmation=True)
        new_trade = self._make_trade(trade_id=101, requires_confirmation=True)
        no_confirm = self._make_trade(trade_id=102, requires_confirmation=False)
        trade_db.create_trade(old_trade)
        trade_db.create_trade(new_trade)
        trade_db.create_trade(no_confirm)

        # Manually backdate trade 100 by setting created_at to 1 hour ago
        old_dt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with trade_db._conn:
            trade_db._conn.execute(
                "UPDATE trades SET created_at = ? WHERE trade_id = 100",
                (old_dt,),
            )

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        expired = trade_db.get_expired_confirmations(cutoff)
        assert expired == [100]


# ====================================================================
# Pipeline gate — requires_confirmation logic
# ====================================================================

class TestPipelineGate:
    """End-to-end: process a real signal under different network/size
    configurations and verify the gate's behaviour.

    Reuses the e2e_pipeline test rig — same fixtures, same mocked client.
    """

    @pytest.fixture
    def rig(self, tmpdir):
        """Build pipeline + db + mocked client, paramaterizable by config."""
        from tests.test_e2e_pipeline import _make_client, _make_config, ASSET_META
        from src.pipeline import Pipeline

        def _build(network="mainnet", auto_execute=True, port_usd=10000.0,
                   confirm_above=100.0):
            config = _make_config(tmpdir, auto_execute=auto_execute, port_usd=port_usd)
            # Override the test-config defaults with what we care about
            config.exchange.network = network
            config.risk.mainnet_confirm_above_usd = confirm_above
            client = _make_client()
            db = TradeDatabase(user_id="alice", db_path=tmpdir / "test.db")
            pipeline = Pipeline(config=config, client=client, db=db)
            return pipeline, db, client

        return _build

    def _load_sample(self):
        from tests.test_e2e_pipeline import _load
        return _load("signal_alert_06.txt")  # ADA/USDT #1259 LOW risk, ~$400 size

    def test_mainnet_big_auto_triggers_gate(self, rig):
        """Mainnet + auto_execute=ON + size > threshold → requires_confirmation=True,
        trade stays PENDING, no submit_trade call."""
        pipeline, db, client = rig(
            network="mainnet", auto_execute=True, confirm_above=100.0,
        )
        pipeline.process_message(self._load_sample())

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.requires_confirmation is True
        assert trade.status == TradeStatus.PENDING
        # No exchange call to submit the trade
        assert client.exchange.order.call_count == 0

    def test_testnet_never_triggers_gate(self, rig):
        """Same size, same auto_execute=ON, but on testnet → no gate.
        Trade fires normally."""
        pipeline, db, client = rig(
            network="testnet", auto_execute=True, confirm_above=100.0,
        )
        pipeline.process_message(self._load_sample())

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.requires_confirmation is False
        # Exchange was called (orders submitted)
        assert client.exchange.order.call_count >= 1

    def test_small_mainnet_trade_no_gate(self, rig):
        """Mainnet + small trade (under threshold) → no gate, trade fires."""
        pipeline, db, client = rig(
            network="mainnet", auto_execute=True,
            # Set a very high threshold so this trade is "small"
            confirm_above=10_000.0,
        )
        pipeline.process_message(self._load_sample())

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.requires_confirmation is False
        assert client.exchange.order.call_count >= 1

    def test_manual_mode_no_gate_set_but_still_pending(self, rig):
        """auto_execute=OFF → trade is PENDING (existing manual flow) but
        requires_confirmation stays False — the gate is for the auto-on
        path. Buttons come from the existing approve/reject UI."""
        pipeline, db, client = rig(
            network="mainnet", auto_execute=False, confirm_above=100.0,
        )
        pipeline.process_message(self._load_sample())

        trade = db.get_trade(1259)
        assert trade is not None
        assert trade.status == TradeStatus.PENDING
        assert trade.requires_confirmation is False
        assert client.exchange.order.call_count == 0

    def test_gate_emits_confirmation_requested_event(self, rig):
        """Audit trail: CONFIRMATION_REQUESTED must be in trade_events so a
        post-mortem can find 'all big-trade prompts'."""
        pipeline, db, client = rig(
            network="mainnet", auto_execute=True, confirm_above=100.0,
        )
        pipeline.process_message(self._load_sample())

        events = db.get_events_for_trade(1259)
        event_types = [e.event_type for e in events]
        assert EventType.CONFIRMATION_REQUESTED in event_types


# ====================================================================
# ConfirmationSweeper
# ====================================================================

class TestConfirmationSweeper:
    """Background task that auto-declines expired confirmations."""

    def _orchestrator_with_pipeline(self, trade_db, timeout_min=5):
        """Build a mock orchestrator whose single pipeline is fakeable."""
        ctx = MagicMock()
        ctx.paused = False
        ctx.db = trade_db
        ctx.config = MagicMock()
        ctx.config.risk.mainnet_confirm_timeout_min = timeout_min
        # Pipeline's _notifier — None means "no Telegram, just DB ops"
        ctx.pipeline._notifier = None

        orchestrator = MagicMock()
        orchestrator.pipelines = {"alice": ctx}
        return orchestrator

    def _make_pending_trade(self, trade_db, trade_id=200, age_minutes=10):
        """Insert a pending trade with requires_confirmation=1, backdated."""
        trade = TradeRecord(
            trade_id=trade_id, user_id="alice", pair="BTC/USDT", coin="BTC",
            side="LONG", risk_level="LOW", trade_type="SWING",
            size_hint="1-4%", entry_price=50000.0, stop_loss=48000.0,
            tp1=51000.0, tp2=52000.0, tp3=53000.0,
            leverage=10, signal_leverage=20,
            position_size_usd=250.0, position_size_coin=0.005,
            requires_confirmation=True,
        )
        trade_db.create_trade(trade)
        backdate = (
            datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        ).isoformat()
        with trade_db._conn:
            trade_db._conn.execute(
                "UPDATE trades SET created_at = ? WHERE trade_id = ?",
                (backdate, trade_id),
            )

    @pytest.mark.asyncio
    async def test_sweep_cancels_expired(self, trade_db):
        """An older-than-timeout pending confirmation is canceled with
        close_reason='confirmation_timeout' and a CONFIRMATION_TIMEOUT event."""
        self._make_pending_trade(trade_db, trade_id=200, age_minutes=10)
        orchestrator = self._orchestrator_with_pipeline(trade_db, timeout_min=5)
        sweeper = ConfirmationSweeper(orchestrator=orchestrator)

        expired = await sweeper.sweep_once()
        assert expired == [("alice", 200)]

        trade = trade_db.get_trade(200)
        assert trade.status == TradeStatus.CANCELED
        assert trade.close_reason == "confirmation_timeout"

        events = trade_db.get_events_for_trade(200)
        event_types = [e.event_type for e in events]
        assert EventType.CONFIRMATION_TIMEOUT in event_types

    @pytest.mark.asyncio
    async def test_sweep_leaves_fresh_alone(self, trade_db):
        """A fresh pending confirmation (within the timeout) is not touched."""
        self._make_pending_trade(trade_db, trade_id=201, age_minutes=1)
        orchestrator = self._orchestrator_with_pipeline(trade_db, timeout_min=5)
        sweeper = ConfirmationSweeper(orchestrator=orchestrator)

        expired = await sweeper.sweep_once()
        assert expired == []

        trade = trade_db.get_trade(201)
        assert trade.status == TradeStatus.PENDING

    @pytest.mark.asyncio
    async def test_sweep_ignores_non_confirmation_pending(self, trade_db):
        """An ordinary PENDING trade without requires_confirmation=1 (e.g.
        auto_execute=OFF) must NOT be auto-declined by the sweeper."""
        trade = TradeRecord(
            trade_id=202, user_id="alice", pair="BTC/USDT", coin="BTC",
            side="LONG", risk_level="LOW", trade_type="SWING",
            size_hint="1-4%", entry_price=50000.0, stop_loss=48000.0,
            tp1=51000.0, tp2=52000.0, tp3=53000.0,
            leverage=10, signal_leverage=20,
            position_size_usd=250.0, position_size_coin=0.005,
            requires_confirmation=False,
        )
        trade_db.create_trade(trade)
        backdate = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with trade_db._conn:
            trade_db._conn.execute(
                "UPDATE trades SET created_at = ? WHERE trade_id = ?",
                (backdate, 202),
            )

        orchestrator = self._orchestrator_with_pipeline(trade_db, timeout_min=5)
        sweeper = ConfirmationSweeper(orchestrator=orchestrator)

        expired = await sweeper.sweep_once()
        assert expired == []
        assert trade_db.get_trade(202).status == TradeStatus.PENDING


# ====================================================================
# Approval handler — confirmation_approved / confirmation_declined events
# ====================================================================

class TestApprovalHandlerConfirmationEvents:
    """Reuse the existing approval handler patterns from test_approval.py."""

    def _make_trade(self, *, requires_confirmation: bool):
        return TradeRecord(
            trade_id=300, user_id="user-1", pair="BTC/USDT", coin="BTC",
            side="LONG", risk_level="LOW", trade_type="SWING",
            size_hint="1-4%", entry_price=50000.0, stop_loss=48000.0,
            tp1=51000.0, tp2=52000.0, tp3=53000.0,
            leverage=10, signal_leverage=20,
            position_size_usd=250.0, position_size_coin=0.005,
            status=TradeStatus.PENDING,
            created_at=datetime(2026, 5, 1),
            updated_at=datetime(2026, 5, 1),
            requires_confirmation=requires_confirmation,
        )

    @pytest.mark.asyncio
    async def test_reject_records_confirmation_declined(self):
        """When requires_confirmation=1 and user clicks Reject, the audit
        event is CONFIRMATION_DECLINED, not just a generic 'rejected'."""
        from tests.test_approval import _make_update_and_context
        trade = self._make_trade(requires_confirmation=True)
        update, context, ctx = _make_update_and_context("signal:reject:300", trade=trade)

        await signal_approval_callback(update, context)

        # The new event was recorded
        record_event_calls = ctx.db.record_event.call_args_list
        event_types = [c.kwargs.get("event_type") for c in record_event_calls]
        assert EventType.CONFIRMATION_DECLINED in event_types
        # The status update was called with the confirmation-specific reason
        ctx.db.update_trade_status.assert_called_once()
        update_call = ctx.db.update_trade_status.call_args
        assert update_call.kwargs.get("close_reason") == "confirmation_declined"

    @pytest.mark.asyncio
    async def test_reject_ordinary_pending_no_confirmation_event(self):
        """If requires_confirmation=0 (ordinary manual-mode reject), the
        old behaviour stands — no CONFIRMATION_DECLINED event."""
        from tests.test_approval import _make_update_and_context
        trade = self._make_trade(requires_confirmation=False)
        update, context, ctx = _make_update_and_context("signal:reject:300", trade=trade)

        await signal_approval_callback(update, context)

        record_event_calls = ctx.db.record_event.call_args_list
        event_types = [c.kwargs.get("event_type") for c in record_event_calls]
        assert EventType.CONFIRMATION_DECLINED not in event_types

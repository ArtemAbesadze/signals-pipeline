"""Tests for the dashboard / Port / Audit Trail formatters added in Phase 2.2."""

from datetime import datetime, timezone
from types import SimpleNamespace

from src.state.models import EventType
from src.telegram.formatters import (
    format_audit_trail,
    format_main_menu,
    format_port,
    format_trading_hub,
)


def _ev(event_type: EventType, action: str, hour: int = 14):
    """Minimal stand-in for a TradeEvent row."""
    return SimpleNamespace(
        event_type=event_type,
        action_taken=action,
        raw_text="",
        occurred_at=datetime(2026, 5, 19, hour, 30, 0),
    )


class TestFormatMainMenu:
    def test_no_port_shows_not_set(self):
        text = format_main_menu(
            display_name="Artem",
            port_state={"port_usd": None, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0,
            pipeline_active=True,
            auto_execute=False,
            preset_name="even_split",
            open_trades=[],
            today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0,
            recent_events=[],
        )
        assert "Hey Artem" in text
        assert "_not set_" in text
        assert "even_split" in text

    def test_port_exceeds_wallet_marker(self):
        text = format_main_menu(
            display_name="Artem",
            port_state={"port_usd": 2000.0, "port_mode": "compound", "port_watermark": None},
            wallet_usd=1000.0,
            pipeline_active=True, auto_execute=True, preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "🛑" in text  # port > wallet halt indicator

    def test_port_warning_marker(self):
        text = format_main_menu(
            display_name="Artem",
            port_state={"port_usd": 960.0, "port_mode": "compound", "port_watermark": None},
            wallet_usd=1000.0,
            pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "⚠️" in text  # port > 95% of wallet

    def test_port_within_bounds_marker(self):
        text = format_main_menu(
            display_name="Artem",
            port_state={"port_usd": 500.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0,
            pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "✅" in text

    def test_paused_pipeline(self):
        text = format_main_menu(
            display_name="A", port_state={"port_usd": 100.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=500.0,
            pipeline_active=False, auto_execute=False, preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "⏸ Paused" in text

    def test_open_trades_section(self):
        trade = SimpleNamespace(
            coin="XRP", side="SHORT", trade_id=2102,
            entry_price=1.357,
        )
        position = {
            "coin": "XRP", "entry_price": 1.357, "size": -100.0,
            "unrealized_pnl": 5.0,
        }
        text = format_main_menu(
            display_name="A", port_state={"port_usd": 100.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=500.0, pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[(trade, position)],
            today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "Open trades (1)" in text
        assert "XRP SHORT #2102" in text
        # PnL line should include a % sign with the realized direction
        assert "%" in text

    def test_open_trades_pending_entry(self):
        """If a trade has no matching exchange position, render as pending entry."""
        trade = SimpleNamespace(coin="DOGE", side="SHORT", trade_id=2104, entry_price=0.1)
        text = format_main_menu(
            display_name="A", port_state={"port_usd": 100.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=500.0, pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[(trade, None)],
            today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "pending entry" in text

    def test_today_summary_with_closes(self):
        text = format_main_menu(
            display_name="A", port_state={"port_usd": 100.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=500.0, pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[],
            today_count_closed=3, today_total_pnl_pct=45.0,
            today_wins=2, today_losses=1, recent_events=[],
        )
        assert "Today" in text
        assert "+45.00%" in text
        assert "3 closed" in text

    def test_preset_with_underscore_safe_markdown(self):
        """Built-in preset names like `even_split` contain underscores. They
        must render as a backtick code span — otherwise the `_` opens an
        italic entity that never closes and Telegram rejects the whole
        message with `Can't parse entities`."""
        text = format_main_menu(
            display_name="Artem",
            port_state={"port_usd": None, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0, pipeline_active=True, auto_execute=False,
            preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "`even_split`" in text

    def test_display_name_with_underscore_escaped(self):
        """A `_` in the display name would open an italic entity — must be
        backslash-escaped to render literally."""
        text = format_main_menu(
            display_name="Test_User",
            port_state={"port_usd": None, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0, pipeline_active=True, auto_execute=False,
            preset_name="hybrid",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=[],
        )
        assert "Test\\_User" in text

    def test_recent_cp_events(self):
        events = [
            _ev(EventType.TP_HIT, "TP1 +13.93%; SL moved to entry", hour=16),
            _ev(EventType.BREAKEVEN, "BE after TP1", hour=15),
        ]
        text = format_main_menu(
            display_name="A", port_state={"port_usd": 100.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=500.0, pipeline_active=True, auto_execute=False, preset_name="even_split",
            open_trades=[], today_count_closed=0, today_total_pnl_pct=0.0,
            today_wins=0, today_losses=0, recent_events=events,
        )
        assert "Recent CP" in text
        assert "TP1 +13.93%" in text
        assert "🎯" in text   # TP_HIT icon
        assert "⚖️" in text   # BREAKEVEN icon


class TestFormatTradingHub:
    """The Trading screen has to surface spot USDC, not just perp margin.
    On HL under portfolio margin, ``account_value`` (perp side) reads
    near-zero while the user's actual USDC sits in spot — bug #9 from the
    2026-05-25 CP soak."""

    def test_shows_both_usdc_and_perp_value(self):
        balance = {
            "usdc_balance": "649.00",
            "account_value": "1.49",  # the misleading-on-its-own value
            "total_margin_used": "0.50",
            "total_position_value": "20.00",
            "withdrawable": "648.50",
        }
        text = format_trading_hub(balance, positions=[], open_trades=0)
        # User has to be able to find both numbers on screen
        assert "$649.00" in text
        assert "$1.49" in text
        # And know which is which
        assert "USDC" in text
        assert "Perp" in text

    def test_unavailable_balance_renders(self):
        text = format_trading_hub(balance=None, positions=None)
        assert "Unavailable" in text

    def test_unrealized_pnl_included_when_positions(self):
        balance = {"usdc_balance": "1000", "account_value": "50"}
        positions = [
            {"unrealized_pnl": "5.50"},
            {"unrealized_pnl": "-2.30"},
        ]
        text = format_trading_hub(balance, positions, open_trades=2)
        assert "$3.20" in text  # net unrealised


class TestFormatPort:
    def test_not_configured(self):
        text = format_port(
            port_state={"port_usd": None, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0,
            history=[],
        )
        assert "not configured" in text

    def test_withdraw_mode_description(self):
        text = format_port(
            port_state={"port_usd": 500.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1000.0,
            history=[],
        )
        assert "$500.00" in text
        assert "withdraw" in text
        assert "profits stay in wallet" in text

    def test_compound_mode_with_history(self):
        history = [
            {"trade_id": 2102, "coin": "XRP", "close_reason": "all_tp_hit",
             "pnl_pct": 10.0, "delta_usd": 10.0,
             "closed_at": datetime(2026, 5, 19, 14, 32)},
            {"trade_id": 2101, "coin": "ARB", "close_reason": "stop_hit",
             "pnl_pct": -5.0, "delta_usd": -5.0,
             "closed_at": datetime(2026, 5, 19, 13, 1)},
        ]
        text = format_port(
            port_state={"port_usd": 1005.0, "port_mode": "compound", "port_watermark": None},
            wallet_usd=2000.0,
            history=history,
        )
        assert "$1,005.00" in text
        assert "compound" in text
        assert "+$10.00" in text
        assert "-$5.00" in text
        assert "Recent port changes" in text

    def test_withdraw_mode_history_marks_as_not_applied(self):
        history = [
            {"trade_id": 1, "coin": "BTC", "close_reason": "all_tp_hit",
             "pnl_pct": 10.0, "delta_usd": 10.0,
             "closed_at": datetime(2026, 5, 19, 14, 32)},
        ]
        text = format_port(
            port_state={"port_usd": 1000.0, "port_mode": "withdraw", "port_watermark": None},
            wallet_usd=1100.0,
            history=history,
        )
        assert "not applied" in text or "withdraw mode" in text
        assert "Recent closed trades" in text

    def test_watermark_floor_displayed(self):
        text = format_port(
            port_state={"port_usd": 1100.0, "port_mode": "watermark", "port_watermark": 1100.0},
            wallet_usd=2000.0,
            history=[],
        )
        assert "Floor" in text
        assert "$1,100.00" in text

    def test_port_exceeds_wallet_halt_message(self):
        text = format_port(
            port_state={"port_usd": 2000.0, "port_mode": "compound", "port_watermark": None},
            wallet_usd=500.0,
            history=[],
        )
        assert "exceeds wallet" in text or "🛑" in text

    def test_within_bounds_headroom(self):
        text = format_port(
            port_state={"port_usd": 500.0, "port_mode": "compound", "port_watermark": None},
            wallet_usd=1000.0,
            history=[],
        )
        assert "headroom" in text


class TestFormatAuditTrail:
    def _make_trade(self, snapshot=None):
        return SimpleNamespace(
            trade_id=2102,
            pair="XRP/USDT",
            side="SHORT",
            decision_snapshot=snapshot,
            created_at=datetime(2026, 5, 19, 10, 32),
        )

    def test_with_snapshot_and_events(self):
        snap = {
            "preset": "even_split", "size_pct_applied": 4.0,
            "port_usd_at_open": 1000.0, "port_mode": "compound",
            "risk_level": "LOW", "leverage_applied": 14, "leverage_signal": 14,
            "position_size_usd": 40.0, "exposure_used_pct": 4.0,
            "wallet_usd_at_open": 1100.0,
            "why": "size_by_risk[LOW]=4.0% of port $1000 = $40",
        }
        trade = self._make_trade(snapshot=snap)
        events = [
            _ev(EventType.SIGNAL_ALERT, "opened size=$40 lev=14x", hour=10),
            _ev(EventType.TP_HIT, "TP1 +13.93%", hour=16),
            _ev(EventType.TRADE_CLOSED, "all TPs hit at +125.61%", hour=17),
        ]
        text = format_audit_trail(trade, events)

        assert "Trade #2102" in text
        assert "XRP/USDT" in text
        assert "Decision at open" in text
        assert "even_split" in text
        assert "$1,000.00" in text
        assert "Events" in text
        assert "opened size=$40 lev=14x" in text
        assert "TP1 +13.93%" in text

    def test_preset_with_underscore_safe_markdown(self):
        """Audit trail renders preset name from decision_snapshot; same
        underscore-vs-italic hazard as the main menu — wrap in backticks."""
        snap = {
            "preset": "even_split", "size_pct_applied": 4.0,
            "port_usd_at_open": 1000.0, "port_mode": "compound",
            "risk_level": "LOW", "leverage_applied": 14, "leverage_signal": 14,
            "position_size_usd": 40.0, "exposure_used_pct": 4.0,
            "wallet_usd_at_open": 1100.0,
            "why": "test",
        }
        trade = self._make_trade(snapshot=snap)
        text = format_audit_trail(trade, events=[])
        assert "`even_split`" in text

    def test_no_snapshot_falls_back(self):
        trade = self._make_trade(snapshot=None)
        text = format_audit_trail(trade, events=[])
        assert "No decision snapshot recorded" in text

    def test_no_events(self):
        trade = self._make_trade(snapshot=None)
        text = format_audit_trail(trade, events=[])
        assert "Events" in text
        assert "none" in text

    def test_long_action_truncated(self):
        trade = self._make_trade(snapshot=None)
        long_action = "x" * 200
        events = [_ev(EventType.ERROR, long_action)]
        text = format_audit_trail(trade, events)
        # truncated to 77 + "..." = 80 chars max
        assert "..." in text
        assert long_action not in text

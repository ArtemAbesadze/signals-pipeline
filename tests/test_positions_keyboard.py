"""Bug #19 — both ``/positions`` and the Trading menu's positions screen
must render per-coin close buttons.

Discovered 2026-06-01: the slash command path used
``trading_sub_keyboard()`` (just back/refresh/close, no per-coin close
buttons), while the Trading-menu path built a custom keyboard with the
close buttons. Same screen, two different UXs — close was unreachable
from the slash command.

Both paths now go through ``positions_keyboard()`` so the contract is
single-sourced. These tests lock the contract so a future refactor
doesn't silently re-diverge them.
"""

from __future__ import annotations

from telegram import InlineKeyboardMarkup

from src.telegram.keyboards import positions_keyboard


def _button_texts(keyboard: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in keyboard.inline_keyboard for b in row]


def _button_callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in keyboard.inline_keyboard for b in row]


class TestPositionsKeyboard:
    def test_empty_positions_just_shows_footer(self):
        kb = positions_keyboard([])
        # Just the back/refresh/close footer rows — no close buttons
        callbacks = _button_callbacks(kb)
        assert not any(cb.startswith("close_pos:") or cb.startswith("close_trade:") for cb in callbacks)
        # Footer has the standard 3 nav buttons
        assert any("menu:trading" in cb for cb in callbacks)

    def test_position_with_matching_trade_uses_close_trade_callback(self):
        """When the position matches a known open trade, route to the
        lifecycle-aware close (close_trade:{tid}) — that goes through
        the bot's reconciliation."""
        positions = [{"coin": "ETH", "size": "0.5"}]
        trade_id_by_coin = {"ETH": 12345}
        kb = positions_keyboard(positions, trade_id_by_coin)
        callbacks = _button_callbacks(kb)
        assert "close_trade:12345" in callbacks
        assert "close_pos:ETH" not in callbacks

    def test_position_without_matching_trade_uses_close_pos_callback(self):
        """Orphan / ghost positions (no local trade) route to the raw
        exchange-close path (close_pos:{coin})."""
        positions = [{"coin": "kBONK", "size": "-3708"}]
        kb = positions_keyboard(positions, trade_id_by_coin={})
        callbacks = _button_callbacks(kb)
        assert "close_pos:kBONK" in callbacks
        assert not any(cb.startswith("close_trade:") for cb in callbacks)

    def test_close_button_per_position(self):
        positions = [
            {"coin": "BTC", "size": "0.01"},
            {"coin": "ETH", "size": "0.5"},
            {"coin": "DOGE", "size": "-150"},
        ]
        kb = positions_keyboard(positions, trade_id_by_coin={"ETH": 42})
        texts = _button_texts(kb)
        # One close button per position
        close_buttons = [t for t in texts if t.startswith("🔴 Close")]
        assert close_buttons == ["🔴 Close BTC", "🔴 Close ETH", "🔴 Close DOGE"]

    def test_skips_positions_without_coin_field(self):
        positions = [{"coin": "ETH", "size": "0.5"}, {"size": "0.1"}]
        kb = positions_keyboard(positions)
        texts = _button_texts(kb)
        close_buttons = [t for t in texts if t.startswith("🔴 Close")]
        assert close_buttons == ["🔴 Close ETH"]

    def test_none_trade_id_by_coin_falls_back_to_raw_close(self):
        """Passing trade_id_by_coin=None must not crash; all positions
        get routed to close_pos."""
        positions = [{"coin": "ETH", "size": "0.5"}]
        kb = positions_keyboard(positions, trade_id_by_coin=None)
        callbacks = _button_callbacks(kb)
        assert "close_pos:ETH" in callbacks

    def test_back_label_customizable(self):
        positions = [{"coin": "ETH", "size": "0.5"}]
        kb = positions_keyboard(
            positions,
            back_target="menu:main",
            back_label="⬅️ Menu",
        )
        texts = _button_texts(kb)
        assert "⬅️ Menu" in texts


class TestSlashAndMenuPathsCallTheSameHelper:
    """The whole point of Bug #19's fix is keyboard parity between
    ``/positions`` and the Trading-menu positions screen. Static check
    that both handlers import the shared helper."""

    def test_slash_command_imports_positions_keyboard(self):
        from src.telegram.handlers import account
        import inspect
        src = inspect.getsource(account)
        assert "positions_keyboard" in src, (
            "/positions slash command must import positions_keyboard "
            "from src.telegram.keyboards"
        )

    def test_trading_menu_imports_positions_keyboard(self):
        from src.telegram.handlers import trades
        import inspect
        src = inspect.getsource(trades)
        assert "positions_keyboard" in src, (
            "Trading-menu positions screen must import positions_keyboard "
            "from src.telegram.keyboards"
        )

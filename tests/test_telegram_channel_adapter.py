"""Unit tests for TelegramChannelAdapter.

Tests:
- Inherits from BaseAdapter, has a queue
- attach() registers a MessageHandler with the right filter
- attach() is idempotent
- _on_channel_post puts text on the queue
- _on_channel_post ignores empty text
- _on_channel_post captures caption when post.text is None
- start() / stop() are no-ops (don't crash, don't drive polling)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from telegram.ext import MessageHandler

from src.input.base_adapter import BaseAdapter
from src.input.telegram_channel_adapter import TelegramChannelAdapter


CHANNEL_ID = -1001234567890


@pytest.fixture
def adapter() -> TelegramChannelAdapter:
    return TelegramChannelAdapter(channel_id=CHANNEL_ID)


@pytest.fixture
def mock_app() -> MagicMock:
    app = MagicMock()
    app.add_handler = MagicMock()
    return app


class TestConstruction:
    def test_is_base_adapter(self, adapter):
        assert isinstance(adapter, BaseAdapter)

    def test_has_queue(self, adapter):
        assert isinstance(adapter.queue, asyncio.Queue)

    def test_channel_id_exposed(self, adapter):
        assert adapter.channel_id == CHANNEL_ID

    def test_accepts_provided_queue(self):
        q: asyncio.Queue = asyncio.Queue()
        a = TelegramChannelAdapter(channel_id=CHANNEL_ID, queue=q)
        assert a.queue is q


class TestAttach:
    def test_attach_registers_one_handler(self, adapter, mock_app):
        adapter.attach(mock_app)
        mock_app.add_handler.assert_called_once()
        (handler,), _ = mock_app.add_handler.call_args
        assert isinstance(handler, MessageHandler)

    def test_attach_idempotent(self, adapter, mock_app):
        adapter.attach(mock_app)
        adapter.attach(mock_app)
        assert mock_app.add_handler.call_count == 1


class TestOnChannelPost:
    @pytest.mark.asyncio
    async def test_text_lands_on_queue(self, adapter):
        update = MagicMock()
        update.channel_post.text = "TRADING SIGNAL ALERT\nPAIR: BTC/USDT #100"
        update.channel_post.caption = None

        await adapter._on_channel_post(update, MagicMock())

        assert adapter.queue.qsize() == 1
        assert adapter.queue.get_nowait() == (
            "TRADING SIGNAL ALERT\nPAIR: BTC/USDT #100"
        )

    @pytest.mark.asyncio
    async def test_strips_surrounding_whitespace(self, adapter):
        update = MagicMock()
        update.channel_post.text = "   \n\n  signal text  \n  "
        update.channel_post.caption = None

        await adapter._on_channel_post(update, MagicMock())

        assert adapter.queue.get_nowait() == "signal text"

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, adapter):
        update = MagicMock()
        update.channel_post.text = "   "
        update.channel_post.caption = None

        await adapter._on_channel_post(update, MagicMock())

        assert adapter.queue.empty()

    @pytest.mark.asyncio
    async def test_no_post_ignored(self, adapter):
        update = MagicMock()
        update.channel_post = None

        await adapter._on_channel_post(update, MagicMock())

        assert adapter.queue.empty()

    @pytest.mark.asyncio
    async def test_falls_back_to_caption(self, adapter):
        """Posts with an attached image carry text as ``caption``, not ``text``."""
        update = MagicMock()
        update.channel_post.text = None
        update.channel_post.caption = "TP HIT — signal text"

        await adapter._on_channel_post(update, MagicMock())

        assert adapter.queue.get_nowait() == "TP HIT — signal text"


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_is_noop(self, adapter):
        """start() returns immediately — host Application owns polling."""
        await adapter.start()  # must not raise / block

    @pytest.mark.asyncio
    async def test_stop_is_noop(self, adapter):
        await adapter.stop()  # must not raise


class TestRealisticPotionScannerFormat:
    """End-to-end: forwarded @PotionScannerBot DM landing in our queue."""

    @pytest.mark.asyncio
    async def test_breakeven_signal_format(self, adapter):
        """Mirrors the verbatim post the Telethon forwarder relays."""
        raw = (
            "Trade Update: Stop Loss to Breakeven\n"
            "Source: Potion #Perp Bot Calls\n\n"
            "Trading Signal Alert\n"
            "BREAK EVEN HIT AFTER TP 2\n\n"
            "📝PAIR: ETH/USDT #2096 (prev: View on Discord)\n\n"
            "Price has returned to entry after TP 2 was secured. Capital protected.\n\n"
            "Called by\n\n"
            "Trade Now: here\n\n"
            "2026-05-23 20:37 UTC"
        )
        update = MagicMock()
        update.channel_post.text = raw
        update.channel_post.caption = None

        await adapter._on_channel_post(update, MagicMock())

        queued = adapter.queue.get_nowait()
        # Adapter passes through verbatim; the parser handles the wrapper noise.
        # See test_classifier.py + test_update_parser.py for parse coverage.
        assert queued == raw

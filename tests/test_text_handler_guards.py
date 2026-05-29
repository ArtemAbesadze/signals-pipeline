"""Bug #15 — the three DM text handlers (config / port / trade-note)
must no-op when called with ``context.user_data is None``.

Background: ``channel_post`` updates from the Phase 4.1 input adapter
(``TelegramChannelAdapter``) carry no per-user context — ``user_data``
is ``None`` for channel and group updates. Before the fix, every CP
signal hit by the channel adapter caused PTB to dispatch the three
DM text MessageHandlers (registered with ``filters.TEXT &
~filters.COMMAND``), each crashing on ``user_data.get(...)``.

Two layers of defense:

1. Registration filter (in ``src/telegram/bot.py``) adds
   ``filters.ChatType.PRIVATE`` so the handlers never fire on
   channel posts. Verified by the bot-startup audit at
   :func:`test_text_handlers_registered_with_private_filter`.

2. Each handler guards ``user_data is None`` and returns silently,
   so direct invocation with a non-DM update doesn't crash.
   Verified per-handler below.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram.handlers.config import config_text_handler
from src.telegram.handlers.port import port_amount_text_handler
from src.telegram.handlers.trades import trade_note_text_handler


def _make_context_with_no_user_data():
    """Mimic the PTB context for a ``channel_post`` update — user_data
    is None because there is no per-user channel context."""
    context = MagicMock()
    context.user_data = None
    # bot_data would still exist (it's app-wide), but we don't expect
    # the handler to reach it; not setting it means an attribute access
    # would fail loudly if the guard didn't fire.
    return context


def _make_update_with_channel_text(text: str = "any text"):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "channel"
    return update


@pytest.mark.asyncio
async def test_config_text_handler_noops_on_channel_post():
    update = _make_update_with_channel_text("12")
    context = _make_context_with_no_user_data()

    # Must not raise (previously: AttributeError on user_data.get)
    await config_text_handler(update, context)

    # And must not have tried to reply
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_port_amount_text_handler_noops_on_channel_post():
    update = _make_update_with_channel_text("$500")
    context = _make_context_with_no_user_data()

    await port_amount_text_handler(update, context)

    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_trade_note_text_handler_noops_on_channel_post():
    update = _make_update_with_channel_text("a note")
    context = _make_context_with_no_user_data()

    await trade_note_text_handler(update, context)

    update.message.reply_text.assert_not_called()


def test_text_handlers_registered_with_private_filter():
    """Primary defense: bot.py registers the three text MessageHandlers
    with ``ChatType.PRIVATE`` so they never dispatch on channel posts.

    This is the contract — the secondary in-handler guard is belt and
    suspenders. If someone removes the ChatType filter and forgets to
    keep the guard, this test will catch the regression."""
    from pathlib import Path

    bot_src = (Path(__file__).parent.parent / "src" / "telegram" / "bot.py").read_text()

    # Each of the three text MessageHandler registrations must include
    # ``filters.ChatType.PRIVATE`` in the same filter expression.
    for handler_name in (
        "config_text_handler",
        "port_amount_text_handler",
        "trade_note_text_handler",
    ):
        # Walk all occurrences (import + registration) and find the one
        # preceded by a MessageHandler(...) opener within a reasonable window.
        pos = 0
        found = False
        while True:
            idx = bot_src.find(handler_name, pos)
            if idx < 0:
                break
            window_start = max(0, idx - 500)
            opener = bot_src.rfind("MessageHandler(", window_start, idx)
            if opener >= 0:
                registration = bot_src[opener:idx]
                assert "ChatType.PRIVATE" in registration, (
                    f"{handler_name} is registered without filters.ChatType.PRIVATE "
                    "— channel_post updates will crash the handler. See Bug #15."
                )
                found = True
                break
            pos = idx + 1
        assert found, (
            f"No MessageHandler(...) registration found for {handler_name} "
            "— either the registration was removed or moved out of bot.py."
        )

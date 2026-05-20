"""Unit tests for admin Telegram command handlers.

Tests /kill, /resume, and the kill confirmation callback. Mocks user_db,
orchestrator, and Telegram Update/Context objects.

The SaaS-era admin commands (/users, /extend, /revoke, /broadcast,
/generate_code, /list_codes, etc.) were removed in Phase 2.1.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.user_db import UserRecord
from src.telegram.handlers.admin import (
    admin_callback,
    kill_command,
    resume_command,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

ADMIN_ID = 99999


def _make_user(user_id="user-1", display_name="Alice", status="active"):
    return UserRecord(
        user_id=user_id,
        display_name=display_name,
        status=status,
        created_at="2025-01-01T00:00:00+00:00",
        updated_at="2025-01-01T00:00:00+00:00",
    )


def _make_context(args=None, admin=True, user_db=None, orchestrator=None):
    """Build a mock Context with bot_data containing user_db and orchestrator."""
    if user_db is None:
        user_db = MagicMock()
    if orchestrator is None:
        orchestrator = MagicMock()

    context = MagicMock()
    context.args = args or []
    context.bot_data = {
        "user_db": user_db,
        "orchestrator": orchestrator,
        "admin_ids": [ADMIN_ID] if admin else [],
    }
    context.bot = AsyncMock()
    return context


def _make_update(user_id=ADMIN_ID):
    """Build a mock Update for a command message."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(callback_data, user_id=ADMIN_ID):
    """Build a mock Update for an inline callback query."""
    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = user_id
    return update


# ------------------------------------------------------------------
# /kill
# ------------------------------------------------------------------

class TestKillCommand:
    @pytest.mark.asyncio
    async def test_shows_confirmation(self):
        context = _make_context()
        update = _make_update()

        await kill_command(update, context)

        call_kwargs = update.message.reply_text.call_args[1]
        assert "reply_markup" in call_kwargs
        markup = call_kwargs["reply_markup"]
        button_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "admin:kill_confirm" in button_data
        assert "admin:kill_cancel" in button_data


# ------------------------------------------------------------------
# Kill confirm/cancel callbacks
# ------------------------------------------------------------------

class TestAdminCallback:
    @pytest.mark.asyncio
    async def test_kill_confirm(self):
        orchestrator = MagicMock()
        orchestrator.kill_all.return_value = {
            "user-1": {"closed": 2, "errors": []},
        }
        context = _make_context(orchestrator=orchestrator)
        update = _make_callback_update("admin:kill_confirm")

        await admin_callback(update, context)

        orchestrator.kill_all.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "Kill Switch Activated" in text
        assert "user-1" in text
        assert "2 closed" in text

    @pytest.mark.asyncio
    async def test_kill_cancel(self):
        context = _make_context()
        update = _make_callback_update("admin:kill_cancel")

        await admin_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "canceled" in text.lower()


# ------------------------------------------------------------------
# /resume
# ------------------------------------------------------------------

class TestResumeCommand:
    @pytest.mark.asyncio
    async def test_resume(self):
        orchestrator = MagicMock()
        context = _make_context(orchestrator=orchestrator)
        update = _make_update()

        await resume_command(update, context)

        orchestrator.resume.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "resumed" in text.lower()


# ------------------------------------------------------------------
# Non-admin rejection
# ------------------------------------------------------------------

class TestNonAdminRejected:
    @pytest.mark.asyncio
    async def test_non_admin_kill_command_rejected(self):
        context = _make_context(admin=False)
        update = _make_update(user_id=12345)  # Not in admin_ids

        await kill_command(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "administrator" in text.lower()

    @pytest.mark.asyncio
    async def test_non_admin_callback_rejected(self):
        context = _make_context(admin=False)
        update = _make_callback_update("admin:kill_confirm", user_id=12345)

        await admin_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "administrator" in text.lower()

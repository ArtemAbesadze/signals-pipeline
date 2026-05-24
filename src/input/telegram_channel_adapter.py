"""Telegram channel listener adapter.

Hooks into the running ``TelegramBot.Application`` as a handler that filters
on ``channel_post`` updates from a single configured channel. Pushes the raw
post text onto the shared ``asyncio.Queue`` that the main loop drains, same
shape as ``DiscordAdapter``.

Why not a standalone polling loop:
- A second ``getUpdates`` poller on the same bot token would race with
  ``TelegramBot``'s polling — Telegram delivers each update exactly once,
  so half our channel posts would land in the wrong process. Sharing the
  Application is the only correct way.

Lifecycle note: ``start()`` / ``stop()`` are no-ops. The host Application
(``TelegramBot``) owns polling. ``attach(app)`` is the hookup point.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from src.input.base_adapter import BaseAdapter

logger = logging.getLogger(__name__)


class TelegramChannelAdapter(BaseAdapter):
    """Listens for ``channel_post`` updates from a single channel.

    Args:
        channel_id: Numeric Telegram chat ID for the channel (e.g.
            ``-1001234567890`` for a private channel/supergroup).
        queue: Optional shared asyncio.Queue; one is created if not provided.
    """

    def __init__(
        self,
        channel_id: int,
        queue: asyncio.Queue | None = None,
    ):
        super().__init__(queue)
        self._channel_id = channel_id
        self._attached = False

    @property
    def channel_id(self) -> int:
        return self._channel_id

    def attach(self, app: Application) -> None:
        """Register the channel-post handler on the given Application.

        Idempotent — calling twice is a no-op. PTB lets us add handlers at
        any point in the Application lifecycle, so call order vs.
        ``Application.start()`` doesn't matter.
        """
        if self._attached:
            return
        app.add_handler(
            MessageHandler(
                filters.UpdateType.CHANNEL_POST
                & filters.Chat(chat_id=self._channel_id),
                self._on_channel_post,
            ),
        )
        self._attached = True
        logger.info(
            "TelegramChannelAdapter attached: channel_id=%d", self._channel_id,
        )

    async def _on_channel_post(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        post = update.channel_post
        if post is None:
            return
        text = (post.text or post.caption or "").strip()
        if not text:
            logger.debug(
                "TelegramChannelAdapter: empty post in channel %d, ignored",
                self._channel_id,
            )
            return
        logger.debug(
            "TelegramChannelAdapter: %d-char post received from channel %d",
            len(text), self._channel_id,
        )
        await self._queue.put(text)

    async def start(self) -> None:
        """No-op. Polling is driven by the host Application's updater."""
        logger.info(
            "TelegramChannelAdapter ready (channel_id=%d)", self._channel_id,
        )

    async def stop(self) -> None:
        """No-op. The host Application owns the polling lifecycle."""
        return

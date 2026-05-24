#!/usr/bin/env python3
"""Telethon user-account forwarder for @PotionScannerBot DMs.

Subscribes (as your personal Telegram account) to ``@PotionScannerBot``,
listens for incoming DMs from it, and re-posts each one verbatim into a
private channel where the trading bot is admin. The trading bot picks
them up via the ``telegram_channel`` adapter (see
``src/input/telegram_channel_adapter.py``).

Why this exists:
    Bot tokens cannot observe their own outbound messages —
    ``@PotionScannerBot``'s DMs to subscribers are not events visible via
    ``getUpdates``. The only way to see them is to BE one of the
    recipients. This script is the recipient.

One-time setup:
    1. Get API credentials at https://my.telegram.org/auth (free)
    2. Add to .env (see .env.example for the full block):
           TG_API_ID=<int>
           TG_API_HASH=<hex string>
           TG_PHONE=<your_phone_number_with_country_code>
           TG_SOURCE_BOT=@PotionScannerBot
           TG_DEST_CHANNEL_ID=-1001234567890   # numeric channel id
           TG_SESSION_FILE=data/.telethon_session
    3. Make sure your personal Telegram account has subscribed to
       ``@PotionScannerBot`` (the user must have started a conversation
       with the bot at least once, or Telethon can't resolve it).
    4. Run interactively the first time:
           python3 scripts/telethon_forwarder.py
       Telegram sends an SMS / app code. Enter it at the prompt. The
       session is saved at TG_SESSION_FILE; subsequent runs are
       non-interactive.
    5. After the session exists, install the launchd agent via
       ``deploy/launchd/install.sh forwarder`` and the script runs 24/7
       under launchd alongside the main bot.

Operational notes:
    - Session file IS your account. ``chmod 600`` enforced at startup,
      .gitignore'd, never commit, never share.
    - Telegram tolerates personal-account automation (forwarding your own
      DMs to your own channel). Bans typically follow spam / mass DMing —
      not this pattern. If your account ever gets restricted, signals
      stop and you'll know within minutes from a missed signal.
    - Laptop sleep kills this process the same way it kills main.py.
      Both processes share the same operational gap; both want a VPS
      eventually.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

logger = logging.getLogger("forwarder")

_REQUIRED_ENV = (
    "TG_API_ID",
    "TG_API_HASH",
    "TG_PHONE",
    "TG_SOURCE_BOT",
    "TG_DEST_CHANNEL_ID",
)


def read_config() -> dict:
    """Load + validate forwarder config from .env.

    Raises SystemExit if any required var is missing or unparseable.
    Exposed (not underscore-prefixed) so the test suite can exercise the
    validation path without invoking the Telethon client.
    """
    load_dotenv()
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"telethon_forwarder: missing env vars: {', '.join(missing)}. "
            "See the docstring at the top of scripts/telethon_forwarder.py."
        )
    try:
        api_id = int(os.environ["TG_API_ID"])
    except ValueError as e:
        raise SystemExit(f"telethon_forwarder: TG_API_ID must be an integer: {e}")
    try:
        dest_channel_id = int(os.environ["TG_DEST_CHANNEL_ID"])
    except ValueError as e:
        raise SystemExit(
            f"telethon_forwarder: TG_DEST_CHANNEL_ID must be a numeric chat id "
            f"(e.g. -1001234567890), got: {os.environ['TG_DEST_CHANNEL_ID']!r}"
        )
    return {
        "api_id": api_id,
        "api_hash": os.environ["TG_API_HASH"],
        "phone": os.environ["TG_PHONE"],
        "source_bot": os.environ["TG_SOURCE_BOT"],
        "dest_channel_id": dest_channel_id,
        "session_file": os.environ.get(
            "TG_SESSION_FILE", "data/.telethon_session",
        ),
    }


def _secure_session(path: Path) -> None:
    """Best-effort: enforce 0600 on the session file if it exists.

    Telethon may create it with the process umask (commonly 0644). The
    file is account-level credentials — same blast radius as the bot
    encryption key — so it needs the same locked-down permissions.
    """
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError as e:
        logger.warning("Could not chmod 600 on session file %s: %s", path, e)


async def run(cfg: dict) -> None:
    """Main forwarder loop. Returns when the client disconnects.

    Split out so a future test can inject a fake client. The default
    invocation builds a real TelegramClient against ``cfg``.
    """
    session_path = Path(cfg["session_file"])
    session_path.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_path), cfg["api_id"], cfg["api_hash"])
    await client.start(phone=cfg["phone"])
    _secure_session(session_path)

    me = await client.get_me()
    logger.info(
        "Forwarder signed in as %s (id=%d)",
        getattr(me, "first_name", None) or getattr(me, "username", "?"),
        me.id,
    )

    source_entity = await client.get_entity(cfg["source_bot"])
    logger.info(
        "Listening for DMs from %s (id=%d) -> channel %d",
        cfg["source_bot"], source_entity.id, cfg["dest_channel_id"],
    )

    @client.on(events.NewMessage(from_users=source_entity, incoming=True))
    async def on_dm(event):
        text = (event.message.text or "").strip()
        if not text:
            logger.debug("Skipping empty DM from source bot")
            return
        try:
            await client.send_message(cfg["dest_channel_id"], text)
            logger.info(
                "Forwarded %d-char message to channel %d",
                len(text), cfg["dest_channel_id"],
            )
        except Exception:
            logger.exception(
                "Failed to forward to channel %d", cfg["dest_channel_id"],
            )

    await client.run_until_disconnected()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = read_config()
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

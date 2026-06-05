"""Account-related slash commands — /balance, /positions, /activate, /deactivate.

The /status command was removed in Phase 2.2 (its info is now on the
main dashboard). The account_nav_callback (legacy inline-button
navigation between balance/positions/status views) is also gone — the
Trading hub is the central nav for account data.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.telegram.formatters import format_balance, format_positions
from src.telegram.keyboards import positions_keyboard, trading_sub_keyboard
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)


def _get_client(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    """Get the HyperliquidClient for a registered user from the orchestrator."""
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    if not ctx:
        return None
    return ctx.client


def _get_exchange(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> str:
    """Active exchange for the user's live pipeline (6.12)."""
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    ctx = orchestrator.pipelines.get(user_id) if orchestrator else None
    if ctx is not None:
        return ctx.config.exchange.exchange
    return "hyperliquid"


@registered_only
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance — show account balance."""
    user_id = context.user_data["user_id"]
    client = _get_client(context, user_id)

    if not client:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    try:
        balance = client.get_balance()
    except Exception as e:
        logger.error("Failed to fetch balance for user %s: %s", user_id, e)
        await update.message.reply_text("⚠️ Failed to fetch balance. Try again later.")
        return

    text = format_balance(balance, _get_exchange(context, user_id))
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=trading_sub_keyboard(),
    )


@registered_only
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions — show open positions with per-coin close buttons.

    Uses the shared ``positions_keyboard`` helper so the close buttons
    are reachable from both the ``/positions`` slash command and the
    Trading menu's positions screen (they previously diverged).
    """
    user_id = context.user_data["user_id"]
    client = _get_client(context, user_id)

    if not client:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Use /activate or contact admin."
        )
        return

    try:
        positions = client.get_open_positions()
    except Exception as e:
        logger.error("Failed to fetch positions for user %s: %s", user_id, e)
        await update.message.reply_text("⚠️ Failed to fetch positions. Try again later.")
        return

    # Match the Trading-menu positions path: build per-coin close buttons,
    # routing to ``close_trade:{tid}`` for HL positions that have a local
    # open trade (so the close goes through the lifecycle reconciliation)
    # vs ``close_pos:{coin}`` for orphan / ghost positions.
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")
    trade_id_by_coin: dict[str, int] = {}
    if orchestrator is not None:
        ctx = orchestrator.pipelines.get(user_id)
        if ctx is not None and ctx.db is not None:
            try:
                for t in ctx.db.get_open_trades():
                    trade_id_by_coin[t.coin] = t.trade_id
            except Exception:
                logger.exception("Failed to load open trades for /positions buttons")

    await update.message.reply_text(
        format_positions(positions),
        parse_mode="Markdown",
        reply_markup=positions_keyboard(positions, trade_id_by_coin),
    )


@registered_only
async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /activate — resume receiving trade signals."""
    user_id = context.user_data["user_id"]
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")

    if not orchestrator:
        await update.message.reply_text("⚠️ Trading system is not available.")
        return

    paused = orchestrator.is_user_paused(user_id)
    if paused is None:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Contact admin."
        )
        return

    if not paused:
        await update.message.reply_text("✅ Trading is already active.")
        return

    orchestrator.resume_user(user_id)
    await update.message.reply_text(
        "▶️ Trading activated! You will now receive trade signals."
    )


@registered_only
async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /deactivate — pause receiving trade signals."""
    user_id = context.user_data["user_id"]
    orchestrator: Orchestrator | None = context.bot_data.get("orchestrator")

    if not orchestrator:
        await update.message.reply_text("⚠️ Trading system is not available.")
        return

    paused = orchestrator.is_user_paused(user_id)
    if paused is None:
        await update.message.reply_text(
            "⚠️ Your trading pipeline is not active. Contact admin."
        )
        return

    if paused:
        await update.message.reply_text("⏸ Trading is already paused.")
        return

    orchestrator.pause_user(user_id)
    await update.message.reply_text(
        "⏸ Trading deactivated. You will not receive new trade signals.\n"
        "Use /activate to resume."
    )

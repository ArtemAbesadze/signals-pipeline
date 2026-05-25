"""Port management handlers — /port command + port:* callbacks.

The Port screen shows the user's currently configured port (amount,
mode, watermark) alongside the wallet balance with a bounds indicator,
plus a history of port-delta candidates from recently closed trades.

Modes (D1):
  - withdraw: port immutable; profits accumulate in wallet
  - compound: port += pnl_usd on every close (both signs)
  - watermark: profits raise floor; losses clamp at the floor
"""

import logging
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.user_db import VALID_PORT_MODES, UserDatabase
from src.telegram.formatters import format_port
from src.telegram.keyboards import port_keyboard
from src.telegram.middleware import registered_only

logger = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("America/New_York")
_PORT_HISTORY_LIMIT = 5
_PORT_FULL_HISTORY_LIMIT = 20


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE) -> Orchestrator | None:
    return context.bot_data.get("orchestrator")


def _refresh_pipeline(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> None:
    """Force the running pipeline to reload its Config from DB.

    Called after every ``set_port`` / ``update_user_config`` here so port
    edits don't get stranded in the DB while the pipeline runs on a stale
    Config. See same helper in ``handlers/config.py``.
    """
    orchestrator = _get_orchestrator(context)
    if orchestrator is None:
        return
    try:
        orchestrator.refresh_user_config(user_id)
    except Exception:
        logger.exception(
            "Failed to refresh pipeline config for user %s", user_id,
        )


def _get_client(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    orchestrator = _get_orchestrator(context)
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.client if ctx else None


def _get_trade_db(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    orchestrator = _get_orchestrator(context)
    if not orchestrator:
        return None
    ctx = orchestrator.pipelines.get(user_id)
    return ctx.db if ctx else None


def build_port_text(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: str,
    *,
    history_limit: int = _PORT_HISTORY_LIMIT,
) -> str:
    """Build the Port screen text. Exposed so menu.py's port stub can reuse it."""
    user_db = _get_user_db(context)
    port_state = user_db.get_port_state(user_id) or {
        "port_usd": None, "port_mode": "withdraw", "port_watermark": None,
    }

    wallet_usd: float | None = None
    client = _get_client(context, user_id)
    if client is not None:
        try:
            bal = client.get_balance()
            wallet_usd = float(bal.get("usdc_balance", 0))
        except Exception:
            logger.exception("Failed to fetch wallet for user %s", user_id)

    history: list[dict] = []
    trade_db = _get_trade_db(context, user_id)
    if trade_db is not None:
        try:
            history = trade_db.get_port_changes(limit=history_limit)
        except Exception:
            logger.exception("Failed to fetch port history for user %s", user_id)

    return format_port(port_state, wallet_usd, history, tz=_LOCAL_TZ)


# ------------------------------------------------------------------
# Slash command
# ------------------------------------------------------------------


@registered_only
async def port_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /port — show the Port management screen."""
    user_id = context.user_data["user_id"]
    text = build_port_text(context, user_id)
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=port_keyboard(),
    )


# ------------------------------------------------------------------
# Callbacks
# ------------------------------------------------------------------


def _mode_selector_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("withdraw", callback_data="port:mode:withdraw"),
            InlineKeyboardButton("compound", callback_data="port:mode:compound"),
            InlineKeyboardButton("watermark", callback_data="port:mode:watermark"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="port:back")],
    ])


async def port_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle port:* callbacks (Set Amount / Change Mode / Full History)."""
    query = update.callback_query
    await query.answer()

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await query.edit_message_text("❌ You're not registered. Use /register.")
        return

    data = query.data

    if data == "port:set_amount":
        context.user_data["awaiting_port"] = "amount"
        await query.edit_message_text(
            "✏️ *Set Port Amount*\n\n"
            "Send the new port amount in USD (positive number).\n"
            "Use /cancel to abort.",
            parse_mode="Markdown",
        )
        return

    if data == "port:change_mode":
        await query.edit_message_text(
            "🔄 *Choose Port Mode*\n\n"
            "• *withdraw* — profits stay in wallet, port unchanged\n"
            "• *compound* — port += pnl on every close (both directions)\n"
            "• *watermark* — profits raise floor; losses clamped at floor",
            parse_mode="Markdown",
            reply_markup=_mode_selector_keyboard(),
        )
        return

    if data.startswith("port:mode:"):
        mode = data.split(":", 2)[2]
        if mode not in VALID_PORT_MODES:
            await query.edit_message_text(f"❌ Unknown mode: {mode}")
            return
        state = user_db.get_port_state(user_id)
        try:
            if state and state.get("port_usd") is not None:
                # Re-set with the existing amount (initializes watermark if needed)
                user_db.set_port(user_id, port_usd=state["port_usd"], port_mode=mode)
            else:
                # No amount yet — just update the mode column
                user_db.update_user_config(user_id, port_mode=mode)
        except ValueError as e:
            await query.edit_message_text(f"⚠️ {e}")
            return
        _refresh_pipeline(context, user_id)
        text = build_port_text(context, user_id) + f"\n\n_🔄 Mode set to {mode}._"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=port_keyboard(),
        )
        return

    if data == "port:full_history":
        text = build_port_text(context, user_id, history_limit=_PORT_FULL_HISTORY_LIMIT)
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=port_keyboard(),
        )
        return

    if data == "port:back":
        text = build_port_text(context, user_id)
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=port_keyboard(),
        )
        return


async def port_amount_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input when the user is setting the port amount."""
    if context.user_data.get("awaiting_port") != "amount":
        return

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        context.user_data.pop("awaiting_port", None)
        return

    raw = update.message.text.strip().lstrip("$").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Couldn't parse that as a number. Try again or /cancel."
        )
        return

    state = user_db.get_port_state(user_id) or {"port_mode": "withdraw"}
    try:
        user_db.set_port(
            user_id, port_usd=amount, port_mode=state.get("port_mode", "withdraw"),
        )
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nTry again or /cancel.")
        return

    _refresh_pipeline(context, user_id)
    context.user_data.pop("awaiting_port", None)
    text = build_port_text(context, user_id) + f"\n\n_✏️ Port set to ${amount:,.2f}._"
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=port_keyboard(),
    )

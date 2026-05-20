"""Admin command handlers — kill switch, admin-role management, and the
testing-only signal-injection helper.

The SaaS-era surface (invite codes, expiry, broadcast, user listing,
extend/revoke access) was removed in Phase 2.1 / D4. This file is now
focused on operational controls for the 3-user private deployment.
"""

import logging
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.orchestrator import Orchestrator
from src.state.user_db import UserDatabase
from src.telegram.middleware import admin_only

logger = logging.getLogger(__name__)


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE) -> Orchestrator:
    return context.bot_data["orchestrator"]


ADMIN_HELP_MESSAGE = (
    "*Admin Commands*\n\n"
    "*Emergency*\n"
    "/kill — Kill switch (close all positions for all users)\n"
    "/resume — Resume signal processing after kill\n\n"
    "*Testing*\n"
    "/inject — ⚠️ TESTING ONLY — Inject a synthetic signal\n\n"
    "*Admin Access*\n"
    "/add\\_admin <telegram\\_id> — Grant admin access\n"
    "/remove\\_admin <telegram\\_id> — Revoke admin access\n"
    "/list\\_admins — List all admins"
)


@admin_only
async def admin_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admin — show all admin commands."""
    await update.message.reply_text(ADMIN_HELP_MESSAGE, parse_mode="Markdown")


# ------------------------------------------------------------------
# Kill switch
# ------------------------------------------------------------------


@admin_only
async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kill — show confirmation before activating kill switch."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm Kill", callback_data="admin:kill_confirm"),
            InlineKeyboardButton("Cancel", callback_data="admin:kill_cancel"),
        ]
    ])
    await update.message.reply_text(
        "Are you sure you want to activate the kill switch?\n"
        "This will cancel all orders and close all positions for ALL users.",
        reply_markup=keyboard,
    )


@admin_only
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume — resume signal processing after kill switch."""
    orchestrator = _get_orchestrator(context)
    orchestrator.resume()
    await update.message.reply_text("Kill switch deactivated. Signal processing resumed.")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin inline button callbacks (kill confirm/cancel)."""
    query = update.callback_query
    await query.answer()

    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await query.edit_message_text("This action is only available to administrators.")
        return

    action = query.data

    if action == "admin:kill_confirm":
        orchestrator = _get_orchestrator(context)
        results = orchestrator.kill_all()

        lines = ["*Kill Switch Activated*\n"]
        for user_id, result in results.items():
            lines.append(
                f"User `{user_id}`: {result['closed']} closed, "
                f"{len(result.get('errors', []))} errors"
            )
        if not results:
            lines.append("No active pipelines.")

        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif action == "admin:kill_cancel":
        await query.edit_message_text("Kill switch canceled.")


# ------------------------------------------------------------------
# Admin role management
# ------------------------------------------------------------------


@admin_only
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add_admin <telegram_id> — grant admin access."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /add\\_admin <telegram\\_id>\nExample: `/add_admin 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID must be a number.")
        return

    added_by = update.effective_user.id
    if user_db.add_telegram_admin(new_admin_id, added_by):
        # Add to runtime list so it takes effect immediately
        admin_ids: list[int] = context.bot_data.get("admin_ids", [])
        if new_admin_id not in admin_ids:
            admin_ids.append(new_admin_id)
        await update.message.reply_text(
            f"Admin access granted to `{new_admin_id}`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"`{new_admin_id}` is already an admin.",
            parse_mode="Markdown",
        )


@admin_only
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remove_admin <telegram_id> — revoke admin access."""
    user_db = _get_user_db(context)

    if not context.args:
        await update.message.reply_text(
            "Usage: /remove\\_admin <telegram\\_id>\nExample: `/remove_admin 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID must be a number.")
        return

    if target_id == update.effective_user.id:
        await update.message.reply_text("You cannot remove yourself as admin.")
        return

    if user_db.remove_telegram_admin(target_id):
        admin_ids: list[int] = context.bot_data.get("admin_ids", [])
        if target_id in admin_ids:
            admin_ids.remove(target_id)
        await update.message.reply_text(
            f"Admin access revoked for `{target_id}`.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"`{target_id}` is not a dynamically added admin.\n"
            "Admins set via TELEGRAM\\_ADMIN\\_IDS env var cannot be removed here.",
            parse_mode="Markdown",
        )


@admin_only
async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list_admins — show all admin IDs."""
    admin_ids: list[int] = context.bot_data.get("admin_ids", [])

    if not admin_ids:
        await update.message.reply_text("No admins configured.")
        return

    lines = ["*Admin Users*\n"]
    for aid in admin_ids:
        lines.append(f"`{aid}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ------------------------------------------------------------------
# ⚠️  TESTING ONLY — Signal injection for development/QA
# ------------------------------------------------------------------

# Track the last injected trade ID so TP/close signals target the right trade
_last_inject_trade_id: int | None = None
_last_inject_coin: str | None = None

_INJECT_COINS = ["ETH", "BTC", "SOL", "XRP"]
_INJECT_SIDES = ["LONG", "SHORT"]
_INJECT_RISKS = ["LOW", "MEDIUM", "HIGH"]
_INJECT_TYPES = ["SWING", "SCALP"]


def _get_any_client(context: ContextTypes.DEFAULT_TYPE):
    """Get a HyperliquidClient from any active pipeline (for price fetching)."""
    orchestrator = _get_orchestrator(context)
    for ctx in orchestrator.pipelines.values():
        return ctx.client
    return None


def _build_signal(price: float, trade_id: int, coin: str, side: str, leverage: int, risk: str, trade_type: str) -> str:
    """Build a signal around the current price with randomized parameters."""
    if side == "LONG":
        entry = round(price * 1.005, 2)
        sl = round(price * 0.97, 2)
        tp1 = round(price * 1.01, 2)
        tp2 = round(price * 1.02, 2)
        tp3 = round(price * 1.03, 2)
    else:
        entry = round(price * 0.995, 2)
        sl = round(price * 1.03, 2)
        tp1 = round(price * 0.99, 2)
        tp2 = round(price * 0.98, 2)
        tp3 = round(price * 0.97, 2)

    return (
        f"TRADING SIGNAL ALERT\n\n"
        f"PAIR: {coin}/USDT #{trade_id}\n"
        f"({risk} RISK)\n\n"
        f"TYPE: {trade_type}\n"
        f"SIZE: 1-4%\n"
        f"SIDE: {side}\n\n"
        f"ENTRY: {entry}\n"
        f"SL: {sl}          (-3.00%)\n\n"
        f"TAKE PROFIT TARGETS:\n\n"
        f"TP1: {tp1}      (1.00%)\n"
        f"TP2: {tp2}      (2.00%)\n"
        f"TP3: {tp3}      (3.00%)\n\n"
        f"LEVERAGE: {leverage}x\n\n"
        f"PROTECT YOUR CAPITAL, MANAGE RISK, LETS PRINT!"
    )


def _build_tp_signal(tp_num: str, profit: str, trade_id: int, coin: str) -> str:
    """Build a TP hit signal for the given trade."""
    if tp_num == "all":
        return (
            f"**\U0001f525ALL TAKE-PROFIT TARGETS HIT**\n\n"
            f"**\U0001f4ddPAIR:** {coin}/USDT #{trade_id}\n\n"
            f"**\U0001f4b0PROFIT:** {profit} \U0001f4c8"
        )
    return (
        f"**✅ TP TARGET {tp_num} HIT**\n\n"
        f"**\U0001f4ddPAIR:** {coin}/USDT #{trade_id}\n\n"
        f"**\U0001f4b0PROFIT:** {profit} \U0001f4c8"
    )


@admin_only
async def inject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /inject — show test signal buttons for quick injection."""
    client = _get_any_client(context)
    price_lines = []
    if client:
        try:
            mids = client.get_all_mids()
            for coin in _INJECT_COINS:
                p = float(mids.get(coin, "0"))
                if p:
                    price_lines.append(f"{coin}: ${p:,.2f}")
        except Exception:
            pass

    price_info = "\n" + " | ".join(price_lines) + "\n" if price_lines else ""

    trade_id_info = ""
    if _last_inject_trade_id:
        trade_id_info = f"\nLast injected trade: #{_last_inject_trade_id}\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("New Random Signal", callback_data="inject:signal")],
        [
            InlineKeyboardButton("TP1 Hit", callback_data="inject:tp1"),
            InlineKeyboardButton("TP2 Hit", callback_data="inject:tp2"),
        ],
        [InlineKeyboardButton("All TP Hit", callback_data="inject:all_tp")],
    ])
    await update.message.reply_text(
        f"*Test Signal Injection*\n{price_info}{trade_id_info}\n"
        f"Coins: {', '.join(_INJECT_COINS)} | Leverage: 3-15x | LONG/SHORT\n"
        f"Select a signal to inject into the pipeline for all active users:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def inject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inject:{signal_type} callbacks — dispatch test signal to all users."""
    global _last_inject_trade_id, _last_inject_coin

    query = update.callback_query
    await query.answer()

    admin_ids: list[int] = context.bot_data.get("admin_ids", [])
    if update.effective_user.id not in admin_ids:
        await query.edit_message_text("This action is only available to administrators.")
        return

    signal_type = query.data.replace("inject:", "")
    orchestrator = _get_orchestrator(context)

    if signal_type == "signal":
        client = _get_any_client(context)
        if not client:
            await query.edit_message_text("No active pipelines — cannot fetch price.")
            return

        coin = random.choice(_INJECT_COINS)
        side = random.choice(_INJECT_SIDES)
        leverage = random.randint(3, 15)
        risk = random.choice(_INJECT_RISKS)
        trade_type = random.choice(_INJECT_TYPES)

        try:
            mids = client.get_all_mids()
            price = float(mids.get(coin, "0"))
        except Exception as e:
            await query.edit_message_text(f"Failed to fetch {coin} price: {e}")
            return

        if not price:
            await query.edit_message_text(f"Could not get {coin} price.")
            return

        trade_id = random.randint(80000, 89999)
        _last_inject_trade_id = trade_id
        _last_inject_coin = coin

        raw_signal = _build_signal(price, trade_id, coin, side, leverage, risk, trade_type)
        orchestrator.dispatch(raw_signal)

        pipelines = len(orchestrator.pipelines)
        paused = sum(1 for ctx in orchestrator.pipelines.values() if ctx.paused)
        await query.edit_message_text(
            f"*New Signal #{trade_id}* injected ({coin} {side} {leverage}x @ ${price:,.2f}).\n"
            f"Dispatched to {pipelines - paused} active pipeline(s).",
            parse_mode="Markdown",
        )

    elif signal_type in ("tp1", "tp2", "all_tp"):
        if not _last_inject_trade_id:
            await query.edit_message_text("No test trade active. Inject a New Signal first.")
            return

        tp_map = {
            "tp1": ("1", "1.00%"),
            "tp2": ("2", "2.00%"),
            "all_tp": ("all", "3.00%"),
        }
        tp_num, profit = tp_map[signal_type]
        coin = _last_inject_coin or "ETH"
        raw_signal = _build_tp_signal(tp_num, profit, _last_inject_trade_id, coin)
        orchestrator.dispatch(raw_signal)

        label = {"tp1": "TP1 Hit", "tp2": "TP2 Hit", "all_tp": "All TP Hit"}
        pipelines = len(orchestrator.pipelines)
        paused = sum(1 for ctx in orchestrator.pipelines.values() if ctx.paused)
        await query.edit_message_text(
            f"*{label[signal_type]}* for trade #{_last_inject_trade_id} injected.\n"
            f"Dispatched to {pipelines - paused} active pipeline(s).",
            parse_mode="Markdown",
        )

    else:
        await query.edit_message_text(f"Unknown signal type: {signal_type}")

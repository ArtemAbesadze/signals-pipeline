"""Exchange / network switch flow (Phase 6.12).

Lets an already-registered user move between exchanges and networks from inside
the bot (Config → 🔁 Switch exchange). Credentials persist per
(exchange, network): switching to a combo you've used before reuses the saved
key (after a re-validate); a new combo prompts for credentials once.

Safety:
  - HARD BLOCK if the user has open/pending trades on the current exchange —
    switching would orphan those positions (the pipeline repoints).
  - Atomic: the active pointer only moves AFTER the target creds validate.
    /cancel anytime keeps you on the current exchange.
  - Mainnet/Live target requires the typed MAINNET token (real money).
  - On success the pipeline is deactivated+reactivated so the new client is
    built from the new combo.

Separate ConversationHandler from /register (which is load-bearing) — it reuses
registration's credential VALIDATION (validate_exchange_credentials) and format
checks, not its state machine (the step order differs: switch picks
exchange+network first, then reuses-or-asks for creds).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.state.database import TradeDatabase
from src.state.user_db import UserDatabase
from src.telegram.formatters import format_exchange_badge
from src.telegram.handlers.registration import (
    MAINNET_CONFIRM_TOKEN,
    _HEX_PATTERN,
    _is_plausible_blofin_secret,
    cancel_command,
    validate_exchange_credentials,
)

logger = logging.getLogger(__name__)

# Conversation states
(
    SW_EXCHANGE,        # choosing target exchange
    SW_NETWORK,         # choosing target network
    SW_CONFIRM_SAVED,   # confirm reuse of saved creds
    SW_MAINNET,         # typed MAINNET confirm for a live target
    SW_HL_ADDR, SW_HL_WALLET, SW_HL_SECRET,   # HL credential entry
    SW_BF_KEY, SW_BF_SECRET, SW_BF_PASS,      # Blofin credential entry
) = range(10)


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("orchestrator")


def _open_trade_count(context: ContextTypes.DEFAULT_TYPE, user_id: str) -> int:
    """Open/pending trade count on the user's CURRENT exchange — for the hard
    block. Reads the live pipeline DB if active, else opens a short-lived one."""
    orch = _get_orchestrator(context)
    ctx = orch.pipelines.get(user_id) if orch else None
    if ctx is not None:
        try:
            return len(ctx.db.get_open_trades())
        except Exception:
            logger.exception("open-trade check failed for %s", user_id)
            return 0
    config = context.bot_data.get("config")
    db_path = config.database.path if config else "data/trades.db"
    tdb = TradeDatabase(user_id=user_id, db_path=db_path)
    try:
        return len(tdb.get_open_trades())
    finally:
        tdb.close()


# ------------------------------------------------------------------
# Entry — Config → 🔁 Switch exchange
# ------------------------------------------------------------------
async def switch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is not None:
        await query.answer()
    chat_id = update.effective_chat.id
    user_db = _get_user_db(context)
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await update.effective_chat.send_message("You're not registered. Send /register first.")
        return ConversationHandler.END

    # Hard block: never switch with open positions/orders on the current exchange.
    open_n = _open_trade_count(context, user_id)
    if open_n > 0:
        cur_ex, cur_net = user_db.get_active_exchange_network(user_id)
        await update.effective_chat.send_message(
            f"🛑 You have *{open_n}* open/pending trade(s) on "
            f"{format_exchange_badge(cur_ex, cur_net)}.\n\n"
            "Settle them before switching exchanges — switching now would leave "
            "those positions unmanaged. Close them, then try again.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    cur_ex, cur_net = user_db.get_active_exchange_network(user_id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🟩 Hyperliquid", callback_data="swex:hyperliquid"),
        InlineKeyboardButton("🟦 Blofin", callback_data="swex:blofin"),
    ]])
    await update.effective_chat.send_message(
        f"🔁 *Switch exchange*\n\nCurrently: {format_exchange_badge(cur_ex, cur_net)}\n\n"
        "Pick the exchange to switch to:",
        parse_mode="Markdown", reply_markup=keyboard,
    )
    return SW_EXCHANGE


async def sw_choose_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    exchange = query.data.replace("swex:", "")
    if exchange not in ("hyperliquid", "blofin"):
        await query.edit_message_text("❌ Invalid exchange.")
        return SW_EXCHANGE
    context.user_data["sw_exchange"] = exchange

    if exchange == "blofin":
        net_buttons = [
            InlineKeyboardButton("🧪 Demo", callback_data="swnet:testnet"),
            InlineKeyboardButton("🌐 Live", callback_data="swnet:mainnet"),
        ]
    else:
        net_buttons = [
            InlineKeyboardButton("🧪 Testnet", callback_data="swnet:testnet"),
            InlineKeyboardButton("🌐 Mainnet", callback_data="swnet:mainnet"),
        ]
    await query.edit_message_text(
        f"Switching to *{exchange}*. Pick the network:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([net_buttons]),
    )
    return SW_NETWORK


async def sw_choose_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    network = query.data.replace("swnet:", "")
    if network not in ("testnet", "mainnet"):
        await query.edit_message_text("❌ Invalid network.")
        return SW_NETWORK
    context.user_data["sw_network"] = network

    user_db = _get_user_db(context)
    user_id = user_db.get_user_by_telegram_chat_id(update.effective_chat.id)
    target_ex = context.user_data["sw_exchange"]
    cur_ex, cur_net = user_db.get_active_exchange_network(user_id)
    if (target_ex, network) == (cur_ex, cur_net):
        await query.edit_message_text(
            f"✅ You're already on {format_exchange_badge(target_ex, network)}. "
            "No change.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if network == "mainnet":
        env_word = "Live" if target_ex == "blofin" else "Mainnet"
        await query.edit_message_text(
            f"⚠️ *{env_word} ({target_ex}) — real-money trading.*\n\n"
            f"Type *{MAINNET_CONFIRM_TOKEN}* to confirm, or /cancel to abort.",
            parse_mode="Markdown",
        )
        return SW_MAINNET

    return await _proceed_after_target(update, context)


async def sw_mainnet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text.upper() != MAINNET_CONFIRM_TOKEN:
        await update.effective_chat.send_message(
            f"❌ Token didn't match. Type *{MAINNET_CONFIRM_TOKEN}* or /cancel.",
            parse_mode="Markdown",
        )
        return SW_MAINNET
    return await _proceed_after_target(update, context)


async def _proceed_after_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Target (exchange, network) chosen + (if mainnet) confirmed. Reuse saved
    creds if present, else collect them."""
    user_db = _get_user_db(context)
    user_id = user_db.get_user_by_telegram_chat_id(update.effective_chat.id)
    target_ex = context.user_data["sw_exchange"]
    target_net = context.user_data["sw_network"]

    if user_db.has_credentials(user_id, target_ex, target_net):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Use saved & switch", callback_data="swconf:yes"),
            InlineKeyboardButton("✖ Cancel", callback_data="swconf:no"),
        ]])
        await update.effective_chat.send_message(
            f"You already have saved credentials for "
            f"{format_exchange_badge(target_ex, target_net)}. Use them?",
            parse_mode="Markdown", reply_markup=keyboard,
        )
        return SW_CONFIRM_SAVED

    # No saved creds — collect them.
    if target_ex == "blofin":
        await update.effective_chat.send_message(
            "🟦 Send your Blofin *API Key* (Read+Trade, no Withdraw):",
            parse_mode="Markdown",
        )
        return SW_BF_KEY
    await update.effective_chat.send_message(
        "🟩 Send your Hyperliquid *Account Address* (0x...):", parse_mode="Markdown",
    )
    return SW_HL_ADDR


async def sw_confirm_saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "swconf:no":
        await query.edit_message_text("Switch cancelled — staying on your current exchange.")
        _clear(context)
        return ConversationHandler.END

    user_db = _get_user_db(context)
    user_id = user_db.get_user_by_telegram_chat_id(update.effective_chat.id)
    target_ex = context.user_data["sw_exchange"]
    target_net = context.user_data["sw_network"]
    creds = user_db.get_user_credentials_decrypted(user_id, target_ex, target_net) or {}
    await query.edit_message_text("🔄 Re-validating saved credentials...")
    err = validate_exchange_credentials(
        target_ex, target_net, creds.get("account_address", ""),
        creds.get("api_secret", ""), creds.get("passphrase", ""),
    )
    if err is not None:
        await update.effective_chat.send_message(
            f"❌ Saved credentials no longer validate:\n{err}\n\n"
            "Let's re-enter them.",
        )
        # fall through to credential collection
        if target_ex == "blofin":
            await update.effective_chat.send_message("🟦 Send your Blofin *API Key*:", parse_mode="Markdown")
            return SW_BF_KEY
        await update.effective_chat.send_message("🟩 Send your *Account Address* (0x...):", parse_mode="Markdown")
        return SW_HL_ADDR
    return await _commit_switch(update, context, save=False)


# --- HL credential entry -------------------------------------------
async def sw_hl_addr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message("❌ Invalid address. Send *Account Address* (0x...):", parse_mode="Markdown")
        return SW_HL_ADDR
    context.user_data["account_address"] = text
    await update.effective_chat.send_message("🔑 Now send your *API Wallet Address* (0x...):", parse_mode="Markdown")
    return SW_HL_WALLET


async def sw_hl_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message("❌ Invalid address. Send *API Wallet Address* (0x...):", parse_mode="Markdown")
        return SW_HL_WALLET
    context.user_data["api_wallet"] = text
    await update.effective_chat.send_message("🔒 Now send your *API Private Key* (0x...):", parse_mode="Markdown")
    return SW_HL_SECRET


async def sw_hl_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not _HEX_PATTERN.match(text) or len(text) != 66:
        await update.effective_chat.send_message("❌ Invalid key. Send *API Private Key* (0x...):", parse_mode="Markdown")
        return SW_HL_SECRET
    context.user_data["api_secret"] = text
    return await _commit_switch(update, context, save=True)


# --- Blofin credential entry ---------------------------------------
async def sw_bf_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not _is_plausible_blofin_secret(text):
        await update.effective_chat.send_message("❌ Doesn't look like an API key. Send your *API Key*:", parse_mode="Markdown")
        return SW_BF_KEY
    context.user_data["account_address"] = text
    await update.effective_chat.send_message("🔒 Now send your *API Secret*:", parse_mode="Markdown")
    return SW_BF_SECRET


async def sw_bf_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not _is_plausible_blofin_secret(text):
        await update.effective_chat.send_message("❌ Doesn't look like an API secret. Send your *API Secret*:", parse_mode="Markdown")
        return SW_BF_SECRET
    context.user_data["api_secret"] = text
    await update.effective_chat.send_message("🗝 Now send your *Passphrase*:", parse_mode="Markdown")
    return SW_BF_PASS


async def sw_bf_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not text:
        await update.effective_chat.send_message("❌ Passphrase can't be empty. Send your *Passphrase*:", parse_mode="Markdown")
        return SW_BF_PASS
    context.user_data["passphrase"] = text
    return await _commit_switch(update, context, save=True)


# ------------------------------------------------------------------
async def _commit_switch(update: Update, context: ContextTypes.DEFAULT_TYPE, *, save: bool) -> int:
    """Validate (when new creds), persist + switch active pointer, reactivate.
    Atomic: the pointer only moves after validation succeeds."""
    user_db = _get_user_db(context)
    user_id = user_db.get_user_by_telegram_chat_id(update.effective_chat.id)
    target_ex = context.user_data["sw_exchange"]
    target_net = context.user_data["sw_network"]

    if save:
        account_address = context.user_data["account_address"]
        api_secret = context.user_data["api_secret"]
        api_wallet = context.user_data.get("api_wallet", "")
        passphrase = context.user_data.get("passphrase", "")
        await update.effective_chat.send_message("🔄 Validating credentials...")
        err = validate_exchange_credentials(
            target_ex, target_net, account_address, api_secret, passphrase,
        )
        if err is not None:
            await update.effective_chat.send_message(
                f"❌ *Validation failed:*\n{err}\n\nNo changes made — you're still "
                "on your current exchange. Try /menu → Config → Switch again.",
                parse_mode="Markdown",
            )
            _clear(context)
            return ConversationHandler.END
        user_db.save_credentials(
            user_id, target_ex, target_net,
            account_address=account_address, api_secret=api_secret,
            api_wallet=api_wallet, passphrase=passphrase,
        )

    # Flip the active pointer + (mainnet) tighten defaults, then reactivate.
    user_db.set_active_exchange_network(user_id, target_ex, target_net)
    if target_net == "mainnet":
        try:
            user_db.apply_mainnet_defaults(user_id)
        except Exception:
            logger.exception("apply_mainnet_defaults failed for %s", user_id)

    orch = _get_orchestrator(context)
    if orch:
        try:
            orch.deactivate_user(user_id)
            orch.activate_user(user_id)
        except Exception:
            logger.exception("Failed to reactivate pipeline for %s after switch", user_id)
            await update.effective_chat.send_message(
                "⚠️ Switched, but the pipeline failed to restart — check /menu, "
                "or contact admin.",
            )
            _clear(context)
            return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Menu", callback_data="menu:main")]])
    await update.effective_chat.send_message(
        f"✅ *Switched* to {format_exchange_badge(target_ex, target_net)}.\n\n"
        "Your other exchange's credentials are remembered — switch back anytime "
        "without re-entering them.",
        parse_mode="Markdown", reply_markup=keyboard,
    )
    logger.info("User %s switched to %s/%s", user_id, target_ex, target_net)
    _clear(context)
    return ConversationHandler.END


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("sw_exchange", "sw_network", "account_address", "api_wallet",
              "api_secret", "passphrase"):
        context.user_data.pop(k, None)


def build_switch_handler() -> ConversationHandler:
    """ConversationHandler for the Config → Switch exchange flow."""
    msg = lambda fn: MessageHandler(filters.TEXT & ~filters.COMMAND, fn)
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(switch_start, pattern=r"^switch:start$")],
        states={
            SW_EXCHANGE: [CallbackQueryHandler(sw_choose_exchange, pattern=r"^swex:")],
            SW_NETWORK: [CallbackQueryHandler(sw_choose_network, pattern=r"^swnet:")],
            SW_CONFIRM_SAVED: [CallbackQueryHandler(sw_confirm_saved, pattern=r"^swconf:")],
            SW_MAINNET: [msg(sw_mainnet_confirm)],
            SW_HL_ADDR: [msg(sw_hl_addr)],
            SW_HL_WALLET: [msg(sw_hl_wallet)],
            SW_HL_SECRET: [msg(sw_hl_secret)],
            SW_BF_KEY: [msg(sw_bf_key)],
            SW_BF_SECRET: [msg(sw_bf_secret)],
            SW_BF_PASS: [msg(sw_bf_pass)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

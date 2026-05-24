"""Registration flow — multi-step ConversationHandler.

States: ACCOUNT_ADDRESS → API_WALLET → API_SECRET → NETWORK

Each credential message is deleted immediately after reading.
DM-only check rejects registration in group chats.

Phase 2.1 / D4: the SaaS-era invite-code step was removed. This is a
private 3-user tool, no codes required.
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.exchange.hyperliquid import HyperliquidClient
from src.state.user_db import UserDatabase
from src.telegram.formatters import mask_address

logger = logging.getLogger(__name__)

# Conversation states
ACCOUNT_ADDRESS, API_WALLET, API_SECRET, NETWORK, MAINNET_CONFIRM = range(5)

# Phase 3.5 — mainnet selection requires typing this exact word (case-insensitive)
# as a small friction step. Inline-button-only would be too easy to fat-finger.
MAINNET_CONFIRM_TOKEN = "MAINNET"

# Regex for 0x-prefixed hex addresses/keys
_HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("orchestrator")


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /register — start the registration flow."""
    # DM-only check
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "🔒 Registration must be done in a private chat. Send me a DM."
        )
        return ConversationHandler.END

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id

    # Check if already registered
    existing_user = user_db.get_user_by_telegram_chat_id(chat_id)
    if existing_user:
        await update.message.reply_text("✅ You're already registered. Use /menu to get started!")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔑 *Registration*\n\n"
        "Let's get you set up — I'll need your Hyperliquid API credentials.\n"
        "Make sure you're in a private chat.\n\n"
        "📋 Send your *Account Address* (0x...):",
        parse_mode="Markdown",
    )
    return ACCOUNT_ADDRESS


async def receive_account_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the account address."""
    text = update.message.text.strip()

    # Delete the credential message
    try:
        await update.message.delete()
    except Exception:
        pass  # May fail if bot lacks delete permission

    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message(
            "❌ Invalid address format. Must be a 42-character 0x-prefixed hex address.\n\n"
            "📋 Send your *Account Address* (0x...):",
            parse_mode="Markdown",
        )
        return ACCOUNT_ADDRESS

    context.user_data["account_address"] = text
    masked = mask_address(text)
    await update.effective_chat.send_message(
        f"✅ Account Address saved (`{masked}`)\n\n"
        "🔑 Now send your *API Wallet Address* (0x...):",
        parse_mode="Markdown",
    )
    return API_WALLET


async def receive_api_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate the API wallet address."""
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    if not _HEX_PATTERN.match(text) or len(text) != 42:
        await update.effective_chat.send_message(
            "❌ Invalid address format. Must be a 42-character 0x-prefixed hex address.\n\n"
            "🔑 Send your *API Wallet Address* (0x...):",
            parse_mode="Markdown",
        )
        return API_WALLET

    context.user_data["api_wallet"] = text
    masked = mask_address(text)
    await update.effective_chat.send_message(
        f"✅ API Wallet saved (`{masked}`)\n\n"
        "🔒 Now send your *API Private Key* (0x...):",
        parse_mode="Markdown",
    )
    return API_SECRET


async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the API private key and prompt for network selection."""
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    if not _HEX_PATTERN.match(text) or len(text) != 66:
        await update.effective_chat.send_message(
            "❌ Invalid private key format. Must be a 66-character 0x-prefixed hex string.\n\n"
            "🔒 Send your *API Private Key* (0x...):",
            parse_mode="Markdown",
        )
        return API_SECRET

    context.user_data["api_secret"] = text

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧪 Testnet", callback_data="network:testnet"),
            InlineKeyboardButton("🌐 Mainnet", callback_data="network:mainnet"),
        ]
    ])
    await update.effective_chat.send_message(
        "🔐 Private Key encrypted.\n\n🌐 Select network:",
        reply_markup=keyboard,
    )
    return NETWORK


async def receive_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle network selection.

    Testnet completes registration immediately. Mainnet routes to a typed-
    confirmation step (Phase 3.5) before continuing — inline-button-only is
    too easy to fat-finger for a real-money switch.
    """
    query = update.callback_query
    await query.answer()

    network = query.data.replace("network:", "")
    if network not in ("testnet", "mainnet"):
        await query.edit_message_text("❌ Invalid network. Please select Testnet or Mainnet.")
        return NETWORK

    if network == "mainnet":
        context.user_data["network"] = "mainnet"
        await query.edit_message_text(
            "⚠️ *Mainnet registration — real-money trading.*\n\n"
            "Mainnet accounts get stricter defaults applied automatically:\n"
            f"• Position cap: *${UserDatabase.MAINNET_DEFAULT_POSITION_CAP_USD:.0f}* "
            "(testnet default: $500)\n"
            "• Trades above this size require explicit confirmation per signal "
            "(see Phase 3.5 mainnet promotion gate)\n"
            "• Auto-execute stays OFF until you turn it on\n\n"
            f"Type *{MAINNET_CONFIRM_TOKEN}* to confirm, or /cancel to abort.",
            parse_mode="Markdown",
        )
        return MAINNET_CONFIRM

    # Testnet — complete immediately
    context.user_data["network"] = "testnet"
    return await _complete_registration(update, context, network="testnet")


async def receive_mainnet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle typed MAINNET confirmation token (Phase 3.5)."""
    text = (update.message.text or "").strip()
    if text.upper() != MAINNET_CONFIRM_TOKEN:
        await update.effective_chat.send_message(
            f"❌ Confirmation token did not match.\n\n"
            f"Type *{MAINNET_CONFIRM_TOKEN}* exactly to confirm mainnet, "
            "or /cancel to abort.",
            parse_mode="Markdown",
        )
        return MAINNET_CONFIRM

    return await _complete_registration(update, context, network="mainnet")


async def _complete_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    network: str,
) -> int:
    """Shared completion: validate credentials, persist, activate.

    For ``network='mainnet'`` also applies the mainnet defaults (lower
    position cap) — see UserDatabase.apply_mainnet_defaults.
    """
    chat_id = update.effective_chat.id

    # Where to send the "validating..." progress message — callback_query for
    # the testnet button path, plain send_message for the mainnet typed path.
    query = update.callback_query
    if query is not None:
        progress = lambda text, **kw: query.edit_message_text(text, **kw)
    else:
        progress = lambda text, **kw: update.effective_chat.send_message(text, **kw)

    await progress("🔄 Validating credentials...")

    user_db = _get_user_db(context)
    account_address = context.user_data["account_address"]
    api_wallet = context.user_data["api_wallet"]
    api_secret = context.user_data["api_secret"]

    try:
        client = HyperliquidClient(
            account_address=account_address,
            private_key=api_secret,
            network=network,
        )
        client.get_account_state()
    except Exception as e:
        logger.warning("Credential validation failed for chat %d: %s", chat_id, e)
        await progress(
            f"❌ *Credential validation failed:*\n{e}\n\n"
            "Please check your credentials and try /register again.",
            parse_mode="Markdown",
        )
        _clear_user_data(context)
        return ConversationHandler.END

    user_id = str(chat_id)
    try:
        user_db.create_user(
            user_id=user_id,
            display_name=update.effective_user.full_name or user_id,
            credentials={
                "account_address": account_address,
                "api_wallet": api_wallet,
                "api_secret": api_secret,
                "network": network,
            },
        )
    except Exception as e:
        logger.error("Failed to create user %s: %s", user_id, e)
        await progress(
            "❌ Registration failed. You may already be registered. Contact admin."
        )
        _clear_user_data(context)
        return ConversationHandler.END

    # Mainnet: tighten position cap so the first real-money trade can't be huge.
    if network == "mainnet":
        try:
            user_db.apply_mainnet_defaults(user_id)
        except Exception:
            logger.exception("Failed to apply mainnet defaults for user %s", user_id)

    user_db.set_telegram_chat_id(user_id, chat_id)

    orchestrator = _get_orchestrator(context)
    if orchestrator:
        try:
            orchestrator.activate_user(user_id)
        except Exception as e:
            logger.error("Failed to activate pipeline for user %s: %s", user_id, e)

    cap = (
        UserDatabase.MAINNET_DEFAULT_POSITION_CAP_USD
        if network == "mainnet" else 500.0
    )
    network_label = "🌐 *Mainnet*" if network == "mainnet" else "🧪 *Testnet*"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Continue", callback_data="menu:main")],
    ])
    await progress(
        "🎉 *Registration Complete!*\n\n"
        f"Network: {network_label}\n"
        f"🎯 Strategy: `even_split` (33/33/34)\n"
        f"⚡ Auto-execute: OFF\n"
        f"📊 Max leverage: 20x\n"
        f"💰 Position cap: ${cap:.0f}\n\n"
        "Press Continue to open the main menu!",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    _clear_user_data(context)
    logger.info("User %s registered successfully (network=%s)", user_id, network)
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel — exit registration at any step."""
    _clear_user_data(context)
    await update.message.reply_text("❌ Registration cancelled.")
    return ConversationHandler.END


def _clear_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove sensitive data from user_data."""
    for key in ("account_address", "api_wallet", "api_secret", "network"):
        context.user_data.pop(key, None)


def build_registration_handler() -> ConversationHandler:
    """Build the ConversationHandler for the registration flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_command)],
        states={
            ACCOUNT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_address)],
            API_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_wallet)],
            API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
            NETWORK: [CallbackQueryHandler(receive_network, pattern=r"^network:")],
            MAINNET_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mainnet_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

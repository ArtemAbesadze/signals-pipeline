"""Registration flow — multi-step ConversationHandler.

States (Phase 6.9 — exchange choice first):
  EXCHANGE → (Hyperliquid)  ACCOUNT_ADDRESS → API_WALLET → API_SECRET → NETWORK
           → (Blofin)       BLOFIN_KEY → BLOFIN_SECRET → BLOFIN_PASSPHRASE → NETWORK
  NETWORK → [MAINNET_CONFIRM] → complete

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

from src.exchange.blofin import BlofinClient
from src.exchange.hyperliquid import HyperliquidClient
from src.state.user_db import UserDatabase
from src.telegram.formatters import mask_address

logger = logging.getLogger(__name__)

# Conversation states
(
    EXCHANGE,
    ACCOUNT_ADDRESS,
    API_WALLET,
    API_SECRET,
    BLOFIN_KEY,
    BLOFIN_SECRET,
    BLOFIN_PASSPHRASE,
    NETWORK,
    MAINNET_CONFIRM,
) = range(9)

# Phase 3.5 — mainnet selection requires typing this exact word (case-insensitive)
# as a small friction step. Inline-button-only would be too easy to fat-finger.
MAINNET_CONFIRM_TOKEN = "MAINNET"

# Regex for 0x-prefixed hex addresses/keys
_HEX_PATTERN = re.compile(r"^0x[0-9a-fA-F]+$")


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("orchestrator")


def validate_exchange_credentials(
    exchange: str, network: str, account_address: str, api_secret: str,
    passphrase: str = "",
) -> str | None:
    """Validate credentials against the exchange with a signed read. Returns
    None on success, or an error message string. Shared by /register and the
    exchange-switch flow. Blofin: testnet→demo, mainnet→production host."""
    try:
        if exchange == "blofin":
            blofin_network = "production" if network == "mainnet" else "demo"
            BlofinClient(
                api_key=account_address, api_secret=api_secret,
                passphrase=passphrase, network=blofin_network,
            ).get_balance()
        else:
            HyperliquidClient(
                account_address=account_address, private_key=api_secret,
                network=network,
            ).get_account_state()
        return None
    except Exception as e:
        return str(e)


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

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Hyperliquid", callback_data="exchange:hyperliquid"),
            InlineKeyboardButton("Blofin", callback_data="exchange:blofin"),
        ]
    ])
    await update.message.reply_text(
        "🔑 *Registration*\n\n"
        "Let's get you set up. First, which exchange is this account on?\n\n"
        "🏦 Select your *exchange*:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return EXCHANGE


async def receive_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle exchange selection — branches into the matching credential flow.

    Both exchanges store credentials in neutral user_data keys so
    ``_complete_registration`` is uniform: ``account_address`` (HL master
    address / Blofin API key), ``api_secret``, and — Blofin only —
    ``passphrase``. ``api_wallet`` is HL-only.
    """
    query = update.callback_query
    await query.answer()

    exchange = query.data.replace("exchange:", "")
    if exchange not in ("hyperliquid", "blofin"):
        await query.edit_message_text("❌ Invalid exchange. Please select Hyperliquid or Blofin.")
        return EXCHANGE

    context.user_data["exchange"] = exchange

    if exchange == "blofin":
        await query.edit_message_text(
            "🟦 *Blofin* selected.\n\n"
            "I'll need an *API Transaction* key (Read + Trade; **no** Withdraw).\n"
            "Make sure you're in a private chat — I delete each message after reading.\n\n"
            "🔑 Send your *API Key*:",
            parse_mode="Markdown",
        )
        return BLOFIN_KEY

    await query.edit_message_text(
        "🟩 *Hyperliquid* selected.\n\n"
        "I'll need your Hyperliquid API credentials.\n"
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
    await update.effective_chat.send_message("🔐 Private Key received.")
    return await _prompt_network(update, context)


async def _prompt_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the network/environment selection keyboard. Labels adapt to the
    exchange (HL: Testnet/Mainnet; Blofin: Demo/Live) but the callback values
    stay ``testnet``/``mainnet`` so completion is uniform — and
    ``build_exchange_client`` maps ``testnet→demo`` / ``mainnet→production``
    for Blofin."""
    exchange = context.user_data.get("exchange", "hyperliquid")
    if exchange == "blofin":
        buttons = [
            InlineKeyboardButton("🧪 Demo", callback_data="network:testnet"),
            InlineKeyboardButton("🌐 Live", callback_data="network:mainnet"),
        ]
        prompt = "🌐 Select environment:"
    else:
        buttons = [
            InlineKeyboardButton("🧪 Testnet", callback_data="network:testnet"),
            InlineKeyboardButton("🌐 Mainnet", callback_data="network:mainnet"),
        ]
        prompt = "🌐 Select network:"
    await update.effective_chat.send_message(
        prompt, reply_markup=InlineKeyboardMarkup([buttons]),
    )
    return NETWORK


# ------------------------------------------------------------------
# Blofin credential collection (Phase 6.9)
# ------------------------------------------------------------------

# Blofin keys/secrets are alphanumeric (not 0x hex). Light format check only —
# the authoritative validation is the live get_balance() call at completion.
def _is_plausible_blofin_secret(text: str) -> bool:
    return bool(text) and " " not in text and len(text) >= 8


async def receive_blofin_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the Blofin API key (stored in the neutral ``account_address``)."""
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if not _is_plausible_blofin_secret(text):
        await update.effective_chat.send_message(
            "❌ That doesn't look like an API key (no spaces, ≥8 chars).\n\n"
            "🔑 Send your *API Key*:",
            parse_mode="Markdown",
        )
        return BLOFIN_KEY

    context.user_data["account_address"] = text
    await update.effective_chat.send_message(
        "✅ API Key saved.\n\n🔒 Now send your *API Secret*:",
        parse_mode="Markdown",
    )
    return BLOFIN_SECRET


async def receive_blofin_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the Blofin API secret."""
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if not _is_plausible_blofin_secret(text):
        await update.effective_chat.send_message(
            "❌ That doesn't look like an API secret (no spaces, ≥8 chars).\n\n"
            "🔒 Send your *API Secret*:",
            parse_mode="Markdown",
        )
        return BLOFIN_SECRET

    context.user_data["api_secret"] = text
    await update.effective_chat.send_message(
        "✅ API Secret saved.\n\n"
        "🗝 Now send your *Passphrase* (the one you set when creating the key):",
        parse_mode="Markdown",
    )
    return BLOFIN_PASSPHRASE


async def receive_blofin_passphrase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the Blofin passphrase, then prompt for environment."""
    text = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if not text:
        await update.effective_chat.send_message(
            "❌ Passphrase can't be empty.\n\n🗝 Send your *Passphrase*:",
            parse_mode="Markdown",
        )
        return BLOFIN_PASSPHRASE

    context.user_data["passphrase"] = text
    await update.effective_chat.send_message("🔐 Passphrase received.")
    return await _prompt_network(update, context)


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
        await query.edit_message_text("❌ Invalid selection. Please choose again.")
        return NETWORK

    exchange = context.user_data.get("exchange", "hyperliquid")
    # mainnet == Blofin "production" (Live). Both are real money, so both go
    # through the typed-token friction step.
    env_word = "Live" if exchange == "blofin" else "Mainnet"

    if network == "mainnet":
        context.user_data["network"] = "mainnet"
        await query.edit_message_text(
            f"⚠️ *{env_word} registration — real-money trading.*\n\n"
            f"{env_word} accounts get stricter defaults applied automatically:\n"
            f"• Position cap: *${UserDatabase.MAINNET_DEFAULT_POSITION_CAP_USD:.0f}* "
            "(default otherwise: $500)\n"
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
    exchange = context.user_data.get("exchange", "hyperliquid")
    # Neutral credential keys (see receive_exchange): account_address holds the
    # HL master address OR the Blofin API key; api_wallet is HL-only (empty for
    # Blofin — the column is NOT NULL); passphrase is Blofin-only.
    account_address = context.user_data["account_address"]
    api_secret = context.user_data["api_secret"]
    api_wallet = context.user_data.get("api_wallet", "")
    passphrase = context.user_data.get("passphrase", "")

    err = validate_exchange_credentials(
        exchange, network, account_address, api_secret, passphrase,
    )
    if err is not None:
        logger.warning("Credential validation failed for chat %d: %s", chat_id, err)
        await progress(
            f"❌ *Credential validation failed:*\n{err}\n\n"
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
                "exchange": exchange,
                "passphrase": passphrase,
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
    if exchange == "blofin":
        exchange_label = "🟦 *Blofin*"
        network_label = "🌐 *Live*" if network == "mainnet" else "🧪 *Demo*"
    else:
        exchange_label = "🟩 *Hyperliquid*"
        network_label = "🌐 *Mainnet*" if network == "mainnet" else "🧪 *Testnet*"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Continue", callback_data="menu:main")],
    ])
    await progress(
        "🎉 *Registration Complete!*\n\n"
        f"Exchange: {exchange_label}\n"
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
    for key in (
        "account_address", "api_wallet", "api_secret",
        "passphrase", "network", "exchange",
    ):
        context.user_data.pop(key, None)


def build_registration_handler() -> ConversationHandler:
    """Build the ConversationHandler for the registration flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_command)],
        states={
            EXCHANGE: [CallbackQueryHandler(receive_exchange, pattern=r"^exchange:")],
            # Hyperliquid credential path
            ACCOUNT_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_address)],
            API_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_wallet)],
            API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
            # Blofin credential path
            BLOFIN_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_blofin_key)],
            BLOFIN_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_blofin_secret)],
            BLOFIN_PASSPHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_blofin_passphrase)],
            # Shared
            NETWORK: [CallbackQueryHandler(receive_network, pattern=r"^network:")],
            MAINNET_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mainnet_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

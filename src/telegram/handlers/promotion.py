"""`/promote_to_mainnet` — flip an existing user from testnet to mainnet.

Phase 3.5. For users who registered on testnet, validated, and now want to
trade real money without going through the full /register dance again.

Flow:
    /promote_to_mainnet
        → check user is registered + currently on testnet
        → warning + ask them to type MAINNET to confirm
        → flip user_credentials.network to 'mainnet'
        → apply mainnet defaults (tighter position cap)
        → re-activate the pipeline so the new network is picked up

Same MAINNET_CONFIRM_TOKEN convention as registration.py — typed token, not
inline buttons, because the consequence is real-money trades.
"""

import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.exchange.hyperliquid import HyperliquidClient
from src.state.user_db import UserDatabase
from src.telegram.handlers.registration import (
    MAINNET_CONFIRM_TOKEN,
    cancel_command,
)

logger = logging.getLogger(__name__)

# Single state — typed confirmation
PROMOTE_CONFIRM = 0


def _get_user_db(context: ContextTypes.DEFAULT_TYPE) -> UserDatabase:
    return context.bot_data["user_db"]


def _get_orchestrator(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.get("orchestrator")


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /promote_to_mainnet — entry point."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "🔒 Network changes must be done in a private chat. Send me a DM."
        )
        return ConversationHandler.END

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id

    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await update.message.reply_text(
            "You're not registered. Send /register first to set up your account."
        )
        return ConversationHandler.END

    creds = user_db.get_user_credentials_decrypted(user_id)
    if not creds:
        await update.message.reply_text(
            "Your account is missing credentials. Re-register with /register."
        )
        return ConversationHandler.END

    if creds["network"] == "mainnet":
        await update.message.reply_text(
            "✅ You're already on mainnet. No action needed."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "⚠️ *Promote to Mainnet — real-money trading.*\n\n"
        "This will flip your account from testnet to mainnet. The same API "
        "credentials must work on mainnet — if they're testnet-only keys, "
        "the promotion will fail validation and roll back.\n\n"
        "Mainnet defaults will be applied automatically:\n"
        f"• Position cap: *${UserDatabase.MAINNET_DEFAULT_POSITION_CAP_USD:.0f}* "
        "(was $500 on testnet)\n"
        "• Big-trade confirmation gate active above the cap (5-min timeout)\n\n"
        f"Type *{MAINNET_CONFIRM_TOKEN}* to confirm, or /cancel to abort.",
        parse_mode="Markdown",
    )
    return PROMOTE_CONFIRM


async def receive_promote_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Handle the typed confirmation token."""
    text = (update.message.text or "").strip()
    if text.upper() != MAINNET_CONFIRM_TOKEN:
        await update.message.reply_text(
            f"❌ Confirmation token did not match.\n\n"
            f"Type *{MAINNET_CONFIRM_TOKEN}* exactly to confirm, "
            "or /cancel to abort.",
            parse_mode="Markdown",
        )
        return PROMOTE_CONFIRM

    user_db = _get_user_db(context)
    chat_id = update.effective_chat.id
    user_id = user_db.get_user_by_telegram_chat_id(chat_id)
    if not user_id:
        await update.message.reply_text("You're not registered.")
        return ConversationHandler.END

    creds = user_db.get_user_credentials_decrypted(user_id)
    if not creds:
        await update.message.reply_text("Credentials missing — re-register.")
        return ConversationHandler.END

    # Validate the existing credentials against mainnet before flipping
    # anything in the DB — if validation fails the user stays on testnet.
    await update.message.reply_text("🔄 Validating credentials on mainnet...")
    try:
        client = HyperliquidClient(
            account_address=creds["account_address"],
            private_key=creds["api_secret"],
            network="mainnet",
        )
        client.get_account_state()
    except Exception as e:
        logger.warning("Mainnet credential validation failed for user %s: %s", user_id, e)
        await update.message.reply_text(
            f"❌ *Mainnet validation failed:*\n{e}\n\n"
            "Your testnet credentials may not work on mainnet. No changes made.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    try:
        user_db.update_user_credentials(user_id, network="mainnet")
        user_db.apply_mainnet_defaults(user_id)
    except Exception as e:
        logger.exception("Failed to promote user %s to mainnet", user_id)
        await update.message.reply_text(
            f"❌ Promotion failed: {e}\n\nPlease contact admin."
        )
        return ConversationHandler.END

    # Re-activate the pipeline so the new network is picked up (the orchestrator
    # holds a per-user Config, which is rebuilt from the DB on activation).
    orchestrator = _get_orchestrator(context)
    if orchestrator:
        try:
            orchestrator.deactivate_user(user_id)
            orchestrator.activate_user(user_id)
        except Exception:
            logger.exception("Failed to re-activate pipeline for user %s", user_id)

    await update.message.reply_text(
        "🎉 *Promoted to Mainnet*\n\n"
        f"🌐 Network: Mainnet\n"
        f"💰 Position cap: ${UserDatabase.MAINNET_DEFAULT_POSITION_CAP_USD:.0f}\n"
        "⚡ Auto-execute: unchanged (use /auto to toggle)\n\n"
        "From this point, real-money trades may execute on your account. "
        "Trades above the position cap require explicit per-signal confirmation.",
        parse_mode="Markdown",
    )
    logger.info("User %s promoted to mainnet", user_id)
    return ConversationHandler.END


def build_promotion_handler() -> ConversationHandler:
    """Build the ConversationHandler for /promote_to_mainnet."""
    return ConversationHandler(
        entry_points=[CommandHandler("promote_to_mainnet", promote_command)],
        states={
            PROMOTE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_promote_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

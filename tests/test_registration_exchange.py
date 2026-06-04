"""Phase 6.9 — /register exchange-choice flow.

Covers the new exchange-selection step and the Blofin credential path:
  - register_command offers an exchange choice
  - receive_exchange branches HL vs Blofin into the right credential state
  - Blofin key/secret/passphrase collection (deleted on receipt, neutral keys)
  - Blofin completion: validates via BlofinClient.get_balance, persists with
    exchange=blofin + passphrase + empty api_wallet, demo/production mapping
  - HL completion still works (regression)
  - validation failure aborts without creating a user
  - the ConversationHandler wires every new state
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from telegram.ext import ConversationHandler

from src.crypto import reset_fernet
from src.state.user_db import UserDatabase
from src.telegram.handlers import registration as reg


@pytest.fixture(autouse=True)
def _encryption_key():
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def user_db():
    with tempfile.TemporaryDirectory() as d:
        udb = UserDatabase(db_path=Path(d) / "test.db")
        yield udb
        udb.close()


def _msg_update(text: str, chat_id: int = 555):
    """An Update carrying a text message (credential-collection steps)."""
    update = MagicMock()
    update.message.text = text
    update.message.delete = AsyncMock()
    update.callback_query = None
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.effective_chat.send_message = AsyncMock()
    update.effective_user.full_name = "Tester"
    return update


def _cb_update(data: str, chat_id: int = 555):
    """An Update carrying a callback query (button steps)."""
    update = MagicMock()
    update.message = None
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.effective_chat.send_message = AsyncMock()
    update.effective_user.full_name = "Tester"
    return update


def _ctx(user_db, orchestrator=None):
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {"user_db": user_db, "orchestrator": orchestrator}
    return ctx


# ------------------------------------------------------------------
# Exchange selection
# ------------------------------------------------------------------
class TestExchangeChoice:
    @pytest.mark.asyncio
    async def test_register_offers_exchange_choice(self, user_db):
        update = _msg_update("/register")
        update.message.reply_text = AsyncMock()
        ctx = _ctx(user_db)

        state = await reg.register_command(update, ctx)

        assert state == reg.EXCHANGE
        # the keyboard offered both exchanges
        kb = update.message.reply_text.call_args.kwargs["reply_markup"]
        labels = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "exchange:hyperliquid" in labels
        assert "exchange:blofin" in labels

    @pytest.mark.asyncio
    async def test_choose_blofin_routes_to_blofin_key(self, user_db):
        update = _cb_update("exchange:blofin")
        ctx = _ctx(user_db)
        state = await reg.receive_exchange(update, ctx)
        assert state == reg.BLOFIN_KEY
        assert ctx.user_data["exchange"] == "blofin"

    @pytest.mark.asyncio
    async def test_choose_hyperliquid_routes_to_account_address(self, user_db):
        update = _cb_update("exchange:hyperliquid")
        ctx = _ctx(user_db)
        state = await reg.receive_exchange(update, ctx)
        assert state == reg.ACCOUNT_ADDRESS
        assert ctx.user_data["exchange"] == "hyperliquid"

    @pytest.mark.asyncio
    async def test_invalid_exchange_stays(self, user_db):
        update = _cb_update("exchange:ftx")
        ctx = _ctx(user_db)
        assert await reg.receive_exchange(update, ctx) == reg.EXCHANGE


# ------------------------------------------------------------------
# Blofin credential collection
# ------------------------------------------------------------------
class TestBlofinCredCollection:
    @pytest.mark.asyncio
    async def test_key_stored_and_message_deleted(self, user_db):
        update = _msg_update("f218a0a8b1f842c7b54ef0f53c75be3e")
        ctx = _ctx(user_db)
        state = await reg.receive_blofin_key(update, ctx)
        assert state == reg.BLOFIN_SECRET
        assert ctx.user_data["account_address"] == "f218a0a8b1f842c7b54ef0f53c75be3e"
        update.message.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_secret_stored(self, user_db):
        update = _msg_update("2cae78637cd74247915c75bb6da3207c")
        ctx = _ctx(user_db)
        state = await reg.receive_blofin_secret(update, ctx)
        assert state == reg.BLOFIN_PASSPHRASE
        assert ctx.user_data["api_secret"] == "2cae78637cd74247915c75bb6da3207c"

    @pytest.mark.asyncio
    async def test_passphrase_then_network_prompt(self, user_db):
        update = _msg_update("BlofinAPI_Key_2")
        ctx = _ctx(user_db)
        ctx.user_data["exchange"] = "blofin"
        state = await reg.receive_blofin_passphrase(update, ctx)
        assert state == reg.NETWORK
        assert ctx.user_data["passphrase"] == "BlofinAPI_Key_2"
        # Demo/Live labelling for Blofin
        kb = update.effective_chat.send_message.call_args.kwargs["reply_markup"]
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert "🧪 Demo" in texts and "🌐 Live" in texts

    @pytest.mark.asyncio
    async def test_bad_key_rejected(self, user_db):
        update = _msg_update("short")  # < 8 chars
        ctx = _ctx(user_db)
        assert await reg.receive_blofin_key(update, ctx) == reg.BLOFIN_KEY
        assert "account_address" not in ctx.user_data

    @pytest.mark.asyncio
    async def test_key_with_space_rejected(self, user_db):
        update = _msg_update("has space inside")
        ctx = _ctx(user_db)
        assert await reg.receive_blofin_key(update, ctx) == reg.BLOFIN_KEY


# ------------------------------------------------------------------
# Completion
# ------------------------------------------------------------------
class TestBlofinCompletion:
    def _seed_blofin(self, ctx):
        ctx.user_data.update({
            "exchange": "blofin",
            "account_address": "KEY123456",
            "api_secret": "SECRET78",
            "passphrase": "PASS",
        })

    @pytest.mark.asyncio
    async def test_demo_completion_persists_blofin_creds(self, user_db):
        update = _cb_update("network:testnet")
        ctx = _ctx(user_db)
        self._seed_blofin(ctx)

        with patch.object(reg, "BlofinClient") as MockBlofin:
            state = await reg._complete_registration(update, ctx, network="testnet")

        assert state == ConversationHandler.END
        # testnet maps to the Blofin demo environment
        assert MockBlofin.call_args.kwargs["network"] == "demo"
        MockBlofin.return_value.get_balance.assert_called_once()

        creds = user_db.get_user_credentials_decrypted("555")
        assert creds["exchange"] == "blofin"
        assert creds["account_address"] == "KEY123456"
        assert creds["passphrase"] == "PASS"
        assert creds["api_wallet"] == ""  # HL-only field, empty for Blofin

    @pytest.mark.asyncio
    async def test_live_maps_to_production(self, user_db):
        update = _msg_update(reg.MAINNET_CONFIRM_TOKEN)  # typed-token path
        ctx = _ctx(user_db)
        self._seed_blofin(ctx)

        with patch.object(reg, "BlofinClient") as MockBlofin:
            await reg._complete_registration(update, ctx, network="mainnet")

        assert MockBlofin.call_args.kwargs["network"] == "production"
        creds = user_db.get_user_credentials_decrypted("555")
        assert creds["network"] == "mainnet"

    @pytest.mark.asyncio
    async def test_validation_failure_aborts(self, user_db):
        update = _cb_update("network:testnet")
        ctx = _ctx(user_db)
        self._seed_blofin(ctx)

        with patch.object(reg, "BlofinClient") as MockBlofin:
            MockBlofin.return_value.get_balance.side_effect = RuntimeError("bad key")
            state = await reg._complete_registration(update, ctx, network="testnet")

        assert state == ConversationHandler.END
        assert user_db.get_user_credentials_decrypted("555") is None

    @pytest.mark.asyncio
    async def test_hyperliquid_completion_regression(self, user_db):
        update = _cb_update("network:testnet")
        ctx = _ctx(user_db)
        ctx.user_data.update({
            "exchange": "hyperliquid",
            "account_address": "0xMASTER",
            "api_wallet": "0xWALLET",
            "api_secret": "0xSECRET",
        })

        with patch.object(reg, "HyperliquidClient") as MockHL:
            state = await reg._complete_registration(update, ctx, network="testnet")

        assert state == ConversationHandler.END
        MockHL.return_value.get_account_state.assert_called_once()
        creds = user_db.get_user_credentials_decrypted("555")
        assert creds["exchange"] == "hyperliquid"
        assert creds["api_wallet"] == "0xWALLET"


# ------------------------------------------------------------------
# Handler wiring
# ------------------------------------------------------------------
class TestHandlerWiring:
    def test_all_states_registered(self):
        handler = reg.build_registration_handler()
        for state in (
            reg.EXCHANGE, reg.ACCOUNT_ADDRESS, reg.API_WALLET, reg.API_SECRET,
            reg.BLOFIN_KEY, reg.BLOFIN_SECRET, reg.BLOFIN_PASSPHRASE,
            reg.NETWORK, reg.MAINNET_CONFIRM,
        ):
            assert state in handler.states

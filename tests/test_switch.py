"""Phase 6.12 step 3 — exchange/network switch flow.

Real UserDatabase (for the active-pointer + per-combo creds), mocked Update/
context + orchestrator, and validate_exchange_credentials patched (no network)."""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from telegram.ext import ConversationHandler

from src.crypto import reset_fernet
from src.state.user_db import UserDatabase
from src.telegram.handlers import switch as sw

CHAT = 555
UID = "555"
HL_CREDS = {"account_address": "0xMASTER", "api_wallet": "0xWALLET",
            "api_secret": "0xSECRET", "network": "testnet", "exchange": "hyperliquid"}


@pytest.fixture(autouse=True)
def _key():
    k = Fernet.generate_key(); reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": k.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def user_db():
    with tempfile.TemporaryDirectory() as d:
        udb = UserDatabase(db_path=Path(d) / "t.db")
        udb.create_user(UID, "Tester", HL_CREDS)
        udb.set_telegram_chat_id(UID, CHAT)
        yield udb
        udb.close()


def _orch(open_trades=0):
    """Mock orchestrator with a pipeline ctx whose db reports open_trades."""
    ctx = SimpleNamespace(db=MagicMock())
    ctx.db.get_open_trades.return_value = list(range(open_trades))
    orch = MagicMock()
    orch.pipelines = {UID: ctx}
    return orch


def _ctx(user_db, orch):
    c = MagicMock()
    c.user_data = {}
    c.bot_data = {"user_db": user_db, "orchestrator": orch,
                  "config": SimpleNamespace(database=SimpleNamespace(path="data/trades.db"))}
    return c


def _cb(data):
    u = MagicMock()
    u.callback_query.data = data
    u.callback_query.answer = AsyncMock()
    u.callback_query.edit_message_text = AsyncMock()
    u.effective_chat.id = CHAT
    u.effective_chat.send_message = AsyncMock()
    u.message = None
    return u


def _msg(text):
    u = MagicMock()
    u.message.text = text
    u.message.delete = AsyncMock()
    u.callback_query = None
    u.effective_chat.id = CHAT
    u.effective_chat.send_message = AsyncMock()
    return u


class TestHardBlock:
    @pytest.mark.asyncio
    async def test_blocks_with_open_trades(self, user_db):
        u, c = _cb("switch:start"), _ctx(user_db, _orch(open_trades=2))
        assert await sw.switch_start(u, c) == ConversationHandler.END
        msg = u.effective_chat.send_message.call_args[0][0]
        assert "open/pending" in msg and "Settle" in msg

    @pytest.mark.asyncio
    async def test_proceeds_when_flat(self, user_db):
        u, c = _cb("switch:start"), _ctx(user_db, _orch(0))
        assert await sw.switch_start(u, c) == sw.SW_EXCHANGE


class TestSelection:
    @pytest.mark.asyncio
    async def test_blofin_no_saved_creds_prompts_key(self, user_db):
        c = _ctx(user_db, _orch(0))
        await sw.sw_choose_exchange(_cb("swex:blofin"), c)
        assert c.user_data["sw_exchange"] == "blofin"
        state = await sw.sw_choose_network(_cb("swnet:testnet"), c)
        assert state == sw.SW_BF_KEY  # no saved blofin creds yet → collect

    @pytest.mark.asyncio
    async def test_already_on_target_ends(self, user_db):
        c = _ctx(user_db, _orch(0))
        await sw.sw_choose_exchange(_cb("swex:hyperliquid"), c)
        u = _cb("swnet:testnet")
        assert await sw.sw_choose_network(u, c) == ConversationHandler.END
        assert "already on" in u.callback_query.edit_message_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_saved_creds_offer_reuse(self, user_db):
        user_db.save_credentials(UID, "blofin", "testnet",
                                 account_address="BKEY", api_secret="BSEC", passphrase="pp")
        c = _ctx(user_db, _orch(0))
        await sw.sw_choose_exchange(_cb("swex:blofin"), c)
        assert await sw.sw_choose_network(_cb("swnet:testnet"), c) == sw.SW_CONFIRM_SAVED

    @pytest.mark.asyncio
    async def test_mainnet_requires_token(self, user_db):
        c = _ctx(user_db, _orch(0))
        await sw.sw_choose_exchange(_cb("swex:hyperliquid"), c)
        assert await sw.sw_choose_network(_cb("swnet:mainnet"), c) == sw.SW_MAINNET


class TestCommit:
    @pytest.mark.asyncio
    async def test_saved_reuse_switches_active_and_reactivates(self, user_db):
        user_db.save_credentials(UID, "blofin", "testnet",
                                 account_address="BKEY", api_secret="BSEC", passphrase="pp")
        orch = _orch(0)
        c = _ctx(user_db, orch)
        c.user_data.update({"sw_exchange": "blofin", "sw_network": "testnet"})
        with patch.object(sw, "validate_exchange_credentials", return_value=None):
            state = await sw.sw_confirm_saved(_cb("swconf:yes"), c)
        assert state == ConversationHandler.END
        assert user_db.get_active_exchange_network(UID) == ("blofin", "testnet")
        orch.deactivate_user.assert_called_once_with(UID)
        orch.activate_user.assert_called_once_with(UID)

    @pytest.mark.asyncio
    async def test_new_blofin_creds_commit(self, user_db):
        orch = _orch(0)
        c = _ctx(user_db, orch)
        c.user_data.update({"sw_exchange": "blofin", "sw_network": "testnet"})
        await sw.sw_bf_key(_msg("f218a0a8b1f842c7b54ef0f53c75be3e"), c)
        await sw.sw_bf_secret(_msg("2cae78637cd74247915c75bb6da3207c"), c)
        with patch.object(sw, "validate_exchange_credentials", return_value=None):
            state = await sw.sw_bf_pass(_msg("BlofinAPI_Key_2"), c)
        assert state == ConversationHandler.END
        assert user_db.get_active_exchange_network(UID) == ("blofin", "testnet")
        assert user_db.has_credentials(UID, "blofin", "testnet")
        orch.activate_user.assert_called_once_with(UID)

    @pytest.mark.asyncio
    async def test_validation_failure_does_not_switch(self, user_db):
        orch = _orch(0)
        c = _ctx(user_db, orch)
        c.user_data.update({"sw_exchange": "blofin", "sw_network": "testnet",
                            "account_address": "BAD", "api_secret": "BAD", "passphrase": "x"})
        with patch.object(sw, "validate_exchange_credentials", return_value="bad key"):
            state = await sw._commit_switch(_msg("x"), c, save=True)
        assert state == ConversationHandler.END
        # active pointer unchanged, no creds saved, pipeline not touched
        assert user_db.get_active_exchange_network(UID) == ("hyperliquid", "testnet")
        assert not user_db.has_credentials(UID, "blofin", "testnet")
        orch.activate_user.assert_not_called()

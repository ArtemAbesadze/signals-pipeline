"""Tests for scripts/seed_blofin_demo_user.py — the 6.11 demo-user seeder.

Operational glue, so coverage is light: lock the one subtle thing — the
neutral credential mapping (Blofin API key → account_address, empty
api_wallet, exchange=blofin) and that the demo key is validated before any
DB write."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.seed_blofin_demo_user as seed


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("BLOFIN_API_KEY", "KEY123456")
    monkeypatch.setenv("BLOFIN_API_SECRET", "SECRET78")
    monkeypatch.setenv("BLOFIN_PASSPHRASE", "PASS")


def test_missing_creds_raises(monkeypatch):
    for k in ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    # _load_dotenv may repopulate from .env; force it to be a no-op
    monkeypatch.setattr(seed, "_load_dotenv", lambda: None)
    with pytest.raises(SystemExit, match="Missing demo creds"):
        seed._demo_creds()


def test_seed_creates_user_with_neutral_blofin_mapping(_env):
    db = MagicMock()
    db.get_user.return_value = None  # new user → create path
    with patch("src.state.user_db.UserDatabase", return_value=db), \
         patch("src.exchange.blofin.BlofinClient") as MockBlofin:
        MockBlofin.return_value.get_balance.return_value = {
            "available": "500000", "total_equity": "510000"}
        seed._seed("blofin_demo", 5000.0)

    # validated the demo key against the demo host before persisting
    assert MockBlofin.call_args.kwargs["network"] == "demo"
    MockBlofin.return_value.get_balance.assert_called_once()

    # created with the neutral credential mapping
    creds = db.create_user.call_args.args[2]
    assert creds["exchange"] == "blofin"
    assert creds["account_address"] == "KEY123456"   # API key in neutral field
    assert creds["api_secret"] == "SECRET78"
    assert creds["passphrase"] == "PASS"
    assert creds["api_wallet"] == ""                 # HL-only column, empty
    assert creds["network"] == "testnet"             # → demo via build_exchange_client
    # config: auto_execute ON, even_split
    config = db.create_user.call_args.args[3]
    assert config["auto_execute"] is True
    assert config["active_preset"] == "even_split"
    # port set
    db.set_port.assert_called_once()
    assert db.set_port.call_args.kwargs["port_usd"] == 5000.0


def test_seed_updates_existing_user(_env):
    db = MagicMock()
    db.get_user.return_value = object()  # exists → update path
    with patch("src.state.user_db.UserDatabase", return_value=db), \
         patch("src.exchange.blofin.BlofinClient") as MockBlofin:
        MockBlofin.return_value.get_balance.return_value = {"available": "1"}
        seed._seed("blofin_demo", 3000.0)

    db.create_user.assert_not_called()
    db.update_user_credentials.assert_called_once()
    assert db.update_user_credentials.call_args.kwargs["exchange"] == "blofin"
    db.set_port.assert_called_once()


def test_set_status_reversible(_env):
    db = MagicMock()
    db.get_user.return_value = object()
    with patch("src.state.user_db.UserDatabase", return_value=db):
        seed._set_status("7441245554", "inactive")
    db.set_user_status.assert_called_once_with("7441245554", "inactive")

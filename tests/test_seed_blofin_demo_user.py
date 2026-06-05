"""Tests for scripts/seed_blofin_demo_user.py — the 6.11 demo-user seeder.

Operational glue, so coverage is light: lock the subtle things — the neutral
credential mapping (Blofin API key → account_address, empty api_wallet,
exchange=blofin), that the demo key is validated before any DB write, and that
the .env FILE is authoritative for demo creds (a shell shadow can't win)."""

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


class TestDemoCreds:
    def test_missing_creds_raises(self, monkeypatch):
        for k in ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE"):
            monkeypatch.delenv(k, raising=False)
        # .env file lookup also returns nothing
        monkeypatch.setattr(seed, "_dotenv_value", lambda *a, **k: None)
        with pytest.raises(SystemExit, match="Missing demo creds"):
            seed._demo_creds()

    def test_dotenv_file_wins_over_shell_shadow(self, tmp_path, monkeypatch):
        # The exact bug from 2026-06-05: a stale exported key shadowed .env.
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# demo\nBLOFIN_API_KEY=f218a0a8\nBLOFIN_API_SECRET=2cae786\n"
            "BLOFIN_PASSPHRASE=BlofinAPI_Key_2\n"
        )
        monkeypatch.setattr(seed, "_REPO_ROOT", tmp_path)
        # shell shadow present — must NOT win
        monkeypatch.setenv("BLOFIN_API_KEY", "027551_SHADOW")
        assert seed._dotenv_value("BLOFIN_API_KEY") == "f218a0a8"
        creds = seed._demo_creds()
        assert creds["api_key"] == "f218a0a8"  # file, not the shadow

    def test_dotenv_value_falls_back_to_env_when_file_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(seed, "_REPO_ROOT", tmp_path)  # no .env here
        monkeypatch.setenv("BLOFIN_API_KEY", "FROM_ENV")
        monkeypatch.setenv("BLOFIN_API_SECRET", "S")
        monkeypatch.setenv("BLOFIN_PASSPHRASE", "P")
        assert seed._demo_creds()["api_key"] == "FROM_ENV"


class TestSeed:
    def test_seed_creates_user_with_neutral_blofin_mapping(self, _env, monkeypatch):
        monkeypatch.setattr(seed, "_dotenv_value", lambda *a, **k: None)  # use _env
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

        creds = db.create_user.call_args.args[2]
        assert creds["exchange"] == "blofin"
        assert creds["account_address"] == "KEY123456"   # API key in neutral field
        assert creds["api_secret"] == "SECRET78"
        assert creds["passphrase"] == "PASS"
        assert creds["api_wallet"] == ""                 # HL-only column, empty
        assert creds["network"] == "testnet"             # → demo via build_exchange_client
        config = db.create_user.call_args.args[3]
        assert config["auto_execute"] is True
        assert config["active_preset"] == "even_split"
        db.set_port.assert_called_once()
        assert db.set_port.call_args.kwargs["port_usd"] == 5000.0

    def test_seed_updates_existing_user(self, _env, monkeypatch):
        monkeypatch.setattr(seed, "_dotenv_value", lambda *a, **k: None)
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


def test_set_status_reversible():
    db = MagicMock()
    db.get_user.return_value = object()
    with patch("src.state.user_db.UserDatabase", return_value=db):
        seed._set_status("7441245554", "inactive")
    db.set_user_status.assert_called_once_with("7441245554", "inactive")

"""Tests for UserDatabase — CRUD, encryption, config merging."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

import sqlite3

from src.config.settings import Config
from src.crypto import encrypt, reset_fernet
from src.state.user_db import UserDatabase


@pytest.fixture(autouse=True)
def _encryption_key():
    """Set a deterministic encryption key for all tests."""
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode()}):
        yield
    reset_fernet()


@pytest.fixture
def db():
    """Create a temporary UserDatabase."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        udb = UserDatabase(db_path=db_path)
        yield udb
        udb.close()


SAMPLE_CREDS = {
    "account_address": "0xABC123",
    "api_wallet": "0xWALLET456",
    "api_secret": "0xSECRET789",
    "network": "testnet",
}

SAMPLE_CONFIG = {
    "active_preset": "tp1_only",
    "auto_execute": True,
    "max_leverage": 10,
    "max_open_positions": 5,
    "max_daily_loss_pct": 5.0,
    "max_position_size_usd": 200.0,
    "max_total_exposure_usd": 1000.0,
    "min_order_usd": 15.0,
}


class TestCreateUser:
    def test_create_user_basic(self, db):
        user = db.create_user("alice", "Alice", SAMPLE_CREDS)
        assert user.user_id == "alice"
        assert user.display_name == "Alice"
        assert user.status == "active"

    def test_create_user_with_config(self, db):
        db.create_user("bob", "Bob", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        cfg = db.get_user_config("bob")
        assert cfg["active_preset"] == "tp1_only"
        assert cfg["auto_execute"] is True
        assert cfg["max_leverage"] == 10

    def test_create_duplicate_raises(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        with pytest.raises(Exception):
            db.create_user("alice", "Alice2", SAMPLE_CREDS)


class TestGetUser:
    def test_get_existing_user(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        user = db.get_user("alice")
        assert user is not None
        assert user.display_name == "Alice"

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_user("nobody") is None


class TestListUsers:
    def test_list_all(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        users = db.list_users()
        assert len(users) == 2

    def test_list_by_status(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_user_status("bob", "inactive")

        active = db.list_users(status="active")
        assert len(active) == 1
        assert active[0].user_id == "alice"

        inactive = db.list_users(status="inactive")
        assert len(inactive) == 1
        assert inactive[0].user_id == "bob"

    def test_get_active_users(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.create_user("bob", "Bob", SAMPLE_CREDS)
        db.set_user_status("bob", "inactive")
        active = db.get_active_users()
        assert len(active) == 1
        assert active[0].user_id == "alice"


class TestSetUserStatus:
    def test_deactivate_and_reactivate(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_user_status("alice", "inactive")
        assert db.get_user("alice").status == "inactive"
        db.set_user_status("alice", "active")
        assert db.get_user("alice").status == "active"


class TestCredentials:
    def test_credentials_encrypted_at_rest(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        # Read raw from DB — should NOT be plaintext
        row = db._conn.execute(
            "SELECT account_address_enc FROM user_credentials WHERE user_id = 'alice'"
        ).fetchone()
        assert row["account_address_enc"] != "0xABC123"

    def test_credentials_decrypted_correctly(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["account_address"] == "0xABC123"
        assert creds["api_wallet"] == "0xWALLET456"
        assert creds["api_secret"] == "0xSECRET789"
        assert creds["network"] == "testnet"

    def test_nonexistent_credentials_returns_none(self, db):
        assert db.get_user_credentials_decrypted("nobody") is None

    def test_update_credentials(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.update_user_credentials("alice", account_address="0xNEW", network="mainnet")
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["account_address"] == "0xNEW"
        assert creds["network"] == "mainnet"
        # Unchanged fields stay the same
        assert creds["api_secret"] == "0xSECRET789"


BLOFIN_CREDS = {
    "account_address": "BLOFIN_API_KEY",   # repurposed: holds the API key
    "api_wallet": "",                       # unused on Blofin
    "api_secret": "blofin_secret",
    "network": "demo",
    "exchange": "blofin",
    "passphrase": "my-pass-phrase",
}


class TestExchangeCredentials:
    """Phase 6 Commit 1 — multi-exchange columns on user_credentials."""

    def test_default_exchange_is_hyperliquid(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["exchange"] == "hyperliquid"
        assert creds["passphrase"] == ""

    def test_create_blofin_user_with_passphrase(self, db):
        db.create_user("bob", "Bob", BLOFIN_CREDS)
        creds = db.get_user_credentials_decrypted("bob")
        assert creds["exchange"] == "blofin"
        assert creds["passphrase"] == "my-pass-phrase"
        assert creds["api_secret"] == "blofin_secret"

    def test_passphrase_encrypted_at_rest(self, db):
        db.create_user("bob", "Bob", BLOFIN_CREDS)
        row = db._conn.execute(
            "SELECT passphrase_enc FROM user_credentials WHERE user_id='bob'"
        ).fetchone()
        assert row["passphrase_enc"] not in (None, "my-pass-phrase")

    def test_hl_user_has_null_passphrase_at_rest(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        row = db._conn.execute(
            "SELECT passphrase_enc FROM user_credentials WHERE user_id='alice'"
        ).fetchone()
        assert row["passphrase_enc"] is None

    def test_update_exchange_and_passphrase(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.update_user_credentials("alice", exchange="blofin", passphrase="pp")
        creds = db.get_user_credentials_decrypted("alice")
        assert creds["exchange"] == "blofin"
        assert creds["passphrase"] == "pp"
        assert creds["api_secret"] == "0xSECRET789"  # untouched

    def test_config_build_carries_exchange_fields(self, db):
        db.create_user("bob", "Bob", BLOFIN_CREDS)
        cfg = db.get_user_config_as_config("bob", Config())
        assert cfg.exchange.exchange == "blofin"
        assert cfg.exchange.passphrase == "my-pass-phrase"
        assert cfg.exchange.network == "demo"

    def test_legacy_credentials_migration_defaults_to_hyperliquid(self, tmp_path):
        """A pre-Blofin user_credentials table (no exchange/passphrase_enc)
        gains the columns on open; existing rows default to hyperliquid."""
        db_path = tmp_path / "legacy_creds.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE user_credentials (user_id TEXT PRIMARY KEY REFERENCES users(user_id), "
            "account_address_enc TEXT NOT NULL, api_wallet_enc TEXT NOT NULL, "
            "api_secret_enc TEXT NOT NULL, network TEXT NOT NULL DEFAULT 'testnet', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users VALUES ('alice', 'Alice', 'active', 'x', 'x')"
        )
        conn.execute(
            "INSERT INTO user_credentials VALUES ('alice', ?, ?, ?, 'testnet', 'x', 'x')",
            (encrypt("0xMASTER"), encrypt("0xWALLET"), encrypt("0xSECRET")),
        )
        conn.commit()
        conn.close()

        udb = UserDatabase(db_path=db_path)
        cols = {r[1] for r in udb._conn.execute("PRAGMA table_info(user_credentials)").fetchall()}
        assert "exchange" in cols and "passphrase_enc" in cols
        creds = udb.get_user_credentials_decrypted("alice")
        assert creds["exchange"] == "hyperliquid"  # legacy default
        assert creds["passphrase"] == ""
        assert creds["account_address"] == "0xMASTER"  # existing creds still decrypt
        udb.close()


class TestUserConfig:
    def test_default_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "even_split"
        assert cfg["auto_execute"] is False
        assert cfg["max_leverage"] == 20
        assert cfg["max_open_positions"] == 10

    def test_custom_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "tp1_only"
        assert cfg["auto_execute"] is True
        assert cfg["max_leverage"] == 10
        assert cfg["max_position_size_usd"] == 200.0

    def test_update_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.update_user_config("alice", active_preset="tp2_be", max_leverage=15)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "tp2_be"
        assert cfg["max_leverage"] == 15

    def test_update_json_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        new_risk = {"LOW": 5.0, "MEDIUM": 3.0, "HIGH": 1.5}
        db.update_user_config("alice", size_by_risk=new_risk)
        cfg = db.get_user_config("alice")
        assert cfg["size_by_risk"] == new_risk

    def test_nonexistent_config_returns_none(self, db):
        assert db.get_user_config("nobody") is None


class TestGetUserConfigAsConfig:
    def test_builds_valid_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=SAMPLE_CONFIG)
        global_config = Config()
        cfg = db.get_user_config_as_config("alice", global_config)

        # Exchange should come from user credentials
        assert cfg.exchange.account_address == "0xABC123"
        assert cfg.exchange.api_secret == "0xSECRET789"
        assert cfg.exchange.network == "testnet"

        # Strategy from user config
        assert cfg.strategy.active_preset == "tp1_only"
        assert cfg.strategy.auto_execute is True
        assert cfg.strategy.max_leverage == 10

        # Risk from user config
        assert cfg.risk.max_open_positions == 5
        assert cfg.risk.max_daily_loss_pct == 5.0
        assert cfg.risk.max_position_size_usd == 200.0

        # Input/database/logging from global config
        assert cfg.input == global_config.input
        assert cfg.database == global_config.database

    def test_nonexistent_user_raises(self, db):
        global_config = Config()
        with pytest.raises(ValueError, match="not found"):
            db.get_user_config_as_config("nobody", global_config)

    def test_custom_presets_included(self, db):
        config_with_preset = {
            **SAMPLE_CONFIG,
            "custom_presets": {
                "my_preset": {
                    "tp_split": [0.5, 0.3, 0.2],
                    "move_sl_to_breakeven_after": "tp2",
                    "size_pct": 3.0,
                }
            },
        }
        db.create_user("alice", "Alice", SAMPLE_CREDS, config=config_with_preset)
        cfg = db.get_user_config_as_config("alice", Config())
        assert "my_preset" in cfg.strategy.presets
        assert cfg.strategy.presets["my_preset"].tp_split == [0.5, 0.3, 0.2]
        assert cfg.strategy.presets["my_preset"].size_pct == 3.0


# ====================================================================
# Port management (D1)
# ====================================================================


class TestPortDefaults:
    """A freshly created user has no port configured."""

    def test_new_user_has_no_port(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        state = db.get_port_state("alice")
        assert state == {"port_usd": None, "port_mode": "withdraw", "port_watermark": None}

    def test_unknown_user_returns_none(self, db):
        assert db.get_port_state("nobody") is None

    def test_port_fields_in_get_user_config(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        cfg = db.get_user_config("alice")
        assert cfg["port_usd"] is None
        assert cfg["port_mode"] == "withdraw"
        assert cfg["port_watermark"] is None

    def test_port_fields_in_config_dataclass(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        cfg = db.get_user_config_as_config("alice", Config())
        assert cfg.port.port_usd is None
        assert cfg.port.port_mode == "withdraw"
        assert cfg.port.port_watermark is None


class TestSetPort:
    """set_port stores the configuration and initializes the watermark."""

    def test_set_port_withdraw(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="withdraw")
        state = db.get_port_state("alice")
        assert state["port_usd"] == 1000.0
        assert state["port_mode"] == "withdraw"
        assert state["port_watermark"] is None

    def test_set_port_compound(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="compound")
        state = db.get_port_state("alice")
        assert state["port_mode"] == "compound"
        assert state["port_watermark"] is None

    def test_set_port_watermark_initializes_floor(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="watermark")
        state = db.get_port_state("alice")
        assert state["port_usd"] == 1000.0
        assert state["port_mode"] == "watermark"
        assert state["port_watermark"] == 1000.0

    def test_invalid_port_mode_rejected(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        with pytest.raises(ValueError, match="Invalid port_mode"):
            db.set_port("alice", port_usd=1000.0, port_mode="bogus")

    def test_non_positive_port_rejected(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        with pytest.raises(ValueError, match="must be positive"):
            db.set_port("alice", port_usd=0.0)
        with pytest.raises(ValueError, match="must be positive"):
            db.set_port("alice", port_usd=-100.0)

    def test_update_user_config_validates_port_mode(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        with pytest.raises(ValueError, match="Invalid port_mode"):
            db.update_user_config("alice", port_mode="bogus")


class TestApplyPnlToPort:
    """apply_pnl_to_port implements the three modes from D1."""

    def test_no_op_when_port_unconfigured(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        # No set_port call — port_usd is None
        result = db.apply_pnl_to_port("alice", pnl_usd=50.0)
        assert result["port_usd"] is None

    def test_withdraw_mode_ignores_profit(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="withdraw")
        result = db.apply_pnl_to_port("alice", pnl_usd=50.0)
        assert result["port_usd"] == 1000.0

    def test_withdraw_mode_ignores_loss(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="withdraw")
        result = db.apply_pnl_to_port("alice", pnl_usd=-50.0)
        assert result["port_usd"] == 1000.0

    def test_compound_mode_applies_profit(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="compound")
        result = db.apply_pnl_to_port("alice", pnl_usd=150.0)
        assert result["port_usd"] == 1150.0

    def test_compound_mode_applies_loss(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="compound")
        result = db.apply_pnl_to_port("alice", pnl_usd=-200.0)
        assert result["port_usd"] == 800.0

    def test_compound_loss_can_drive_port_below_initial(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="compound")
        db.apply_pnl_to_port("alice", pnl_usd=-300.0)
        result = db.apply_pnl_to_port("alice", pnl_usd=-200.0)
        assert result["port_usd"] == 500.0

    def test_watermark_profit_raises_both(self, db):
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="watermark")
        result = db.apply_pnl_to_port("alice", pnl_usd=150.0)
        assert result["port_usd"] == 1150.0
        assert result["port_watermark"] == 1150.0

    def test_watermark_loss_clamps_at_floor(self, db):
        """Loss can't drop port below the highest historical floor."""
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="watermark")
        # Win first to raise the floor
        db.apply_pnl_to_port("alice", pnl_usd=200.0)  # port=1200, wm=1200
        # Loss attempt — clamped to current watermark
        result = db.apply_pnl_to_port("alice", pnl_usd=-300.0)
        assert result["port_usd"] == 1200.0
        assert result["port_watermark"] == 1200.0

    def test_watermark_loss_from_initial_clamps_at_initial(self, db):
        """Initial port_usd is also the initial floor — first loss is clamped."""
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="watermark")
        result = db.apply_pnl_to_port("alice", pnl_usd=-100.0)
        assert result["port_usd"] == 1000.0  # clamped
        assert result["port_watermark"] == 1000.0

    def test_watermark_sequence(self, db):
        """Realistic sequence: win, loss (clamped), win raises floor, loss (clamped)."""
        db.create_user("alice", "Alice", SAMPLE_CREDS)
        db.set_port("alice", port_usd=1000.0, port_mode="watermark")

        r = db.apply_pnl_to_port("alice", pnl_usd=200.0)
        assert (r["port_usd"], r["port_watermark"]) == (1200.0, 1200.0)

        r = db.apply_pnl_to_port("alice", pnl_usd=-100.0)
        assert (r["port_usd"], r["port_watermark"]) == (1200.0, 1200.0)  # clamped

        r = db.apply_pnl_to_port("alice", pnl_usd=300.0)
        assert (r["port_usd"], r["port_watermark"]) == (1500.0, 1500.0)  # new high

        r = db.apply_pnl_to_port("alice", pnl_usd=-1000.0)
        assert (r["port_usd"], r["port_watermark"]) == (1500.0, 1500.0)  # clamped


class TestMigration:
    """An existing DB without port columns gets them added via migration."""

    def test_migration_adds_port_columns(self, tmp_path):
        """Simulate an older DB without port columns and verify migration adds them."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        # Create a minimal user_config table without the port columns
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE user_config (user_id TEXT PRIMARY KEY, "
            "active_preset TEXT NOT NULL DEFAULT 'runner', "
            "auto_execute INTEGER NOT NULL DEFAULT 0, "
            "max_leverage INTEGER NOT NULL DEFAULT 20, "
            "size_by_risk_json TEXT NOT NULL DEFAULT '{}', "
            "custom_presets_json TEXT NOT NULL DEFAULT '{}', "
            "max_open_positions INTEGER NOT NULL DEFAULT 10, "
            "max_daily_loss_pct REAL NOT NULL DEFAULT 10.0, "
            "max_position_size_usd REAL NOT NULL DEFAULT 500.0, "
            "max_total_exposure_usd REAL NOT NULL DEFAULT 2000.0, "
            "min_order_usd REAL NOT NULL DEFAULT 10.0, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

        # Opening via UserDatabase should run the migration
        db = UserDatabase(db_path=db_path)
        cursor = db._conn.execute("PRAGMA table_info(user_config)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "port_usd" in cols
        assert "port_mode" in cols
        assert "port_watermark" in cols
        db.close()


# ====================================================================
# D2 — Preset rename migration
# ====================================================================


class TestActivePresetMigration:
    """Old built-in preset names get rewritten to D2 equivalents on open."""

    def _seed_user_with_preset(self, db_path: Path, user_id: str, preset: str) -> None:
        """Insert a user_config row with the given active_preset bypassing the
        new validation — simulates a row written by a pre-D2 version."""
        import sqlite3
        now = "2026-01-01T00:00:00+00:00"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO users (user_id, display_name, status, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, ?)",
            (user_id, user_id, now, now),
        )
        conn.execute(
            "INSERT INTO user_config (user_id, active_preset, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, preset, now, now),
        )
        conn.commit()
        conn.close()

    @pytest.mark.parametrize("old,new", [
        ("runner", "even_split"),
        ("conservative", "tp1_only"),
        ("tp2_exit", "tp2_be"),
        ("tp3_hold", "tp3_be"),
        ("breakeven_filter", "even_split"),
        ("small_runner", "even_split"),
    ])
    def test_old_name_migrates_to_new(self, tmp_path, old, new):
        db_path = tmp_path / "legacy.db"
        # Open once to create the schema
        db = UserDatabase(db_path=db_path)
        db.close()
        # Seed a row with the old preset name
        self._seed_user_with_preset(db_path, "alice", old)
        # Re-open — _migrate_active_preset should rewrite alice's preset
        db = UserDatabase(db_path=db_path)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == new
        db.close()

    def test_custom_preset_name_untouched(self, tmp_path):
        """A user with a custom preset name (not in the rename map) is unaffected."""
        db_path = tmp_path / "legacy.db"
        db = UserDatabase(db_path=db_path)
        db.close()
        self._seed_user_with_preset(db_path, "alice", "my_custom_preset")
        db = UserDatabase(db_path=db_path)
        cfg = db.get_user_config("alice")
        assert cfg["active_preset"] == "my_custom_preset"
        db.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Running the migration twice doesn't double-process."""
        db_path = tmp_path / "legacy.db"
        db = UserDatabase(db_path=db_path)
        db.close()
        self._seed_user_with_preset(db_path, "alice", "runner")
        # First open migrates
        db = UserDatabase(db_path=db_path)
        assert db.get_user_config("alice")["active_preset"] == "even_split"
        db.close()
        # Second open is a no-op
        db = UserDatabase(db_path=db_path)
        assert db.get_user_config("alice")["active_preset"] == "even_split"
        db.close()

    def test_new_name_already_set_is_noop(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        db = UserDatabase(db_path=db_path)
        db.close()
        self._seed_user_with_preset(db_path, "alice", "hybrid")
        db = UserDatabase(db_path=db_path)
        assert db.get_user_config("alice")["active_preset"] == "hybrid"
        db.close()


# ====================================================================
# D2 — BUILTIN_PRESETS contents
# ====================================================================


class TestBuiltinPresets:
    """The 7 built-in presets match the D2 spec exactly."""

    def test_exactly_seven_presets(self):
        from src.config.settings import BUILTIN_PRESETS
        expected = {
            "tp1_only", "tp2_only", "tp3_only",
            "tp2_be", "tp3_be", "hybrid", "even_split",
        }
        assert set(BUILTIN_PRESETS.keys()) == expected

    def test_default_preset_is_even_split(self):
        from src.config.settings import DEFAULT_PRESET, StrategyConfig
        assert DEFAULT_PRESET == "even_split"
        assert StrategyConfig().active_preset == "even_split"

    @pytest.mark.parametrize("name,tp_split,be", [
        ("tp1_only", [1.0, 0.0, 0.0], "never"),
        ("tp2_only", [0.0, 1.0, 0.0], "never"),
        ("tp3_only", [0.0, 0.0, 1.0], "never"),
        ("tp2_be", [0.0, 1.0, 0.0], "tp1"),
        ("tp3_be", [0.0, 0.0, 1.0], "tp1"),
        ("hybrid", [0.1, 0.7, 0.2], "tp1"),
        ("even_split", [0.33, 0.33, 0.34], "tp1"),
    ])
    def test_preset_shape(self, name, tp_split, be):
        from src.config.settings import BUILTIN_PRESETS
        p = BUILTIN_PRESETS[name]
        assert p.tp_split == tp_split
        assert p.move_sl_to_breakeven_after == be

    def test_old_names_no_longer_exist(self):
        from src.config.settings import BUILTIN_PRESETS
        for old in ("runner", "conservative", "tp2_exit", "tp3_hold",
                     "breakeven_filter", "small_runner"):
            assert old not in BUILTIN_PRESETS

    def test_get_active_preset_resolves_each_builtin(self):
        from src.config.settings import BUILTIN_PRESETS, Config
        for name in BUILTIN_PRESETS:
            cfg = Config()
            cfg.strategy.active_preset = name
            assert cfg.get_active_preset().tp_split == BUILTIN_PRESETS[name].tp_split

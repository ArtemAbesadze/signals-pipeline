"""Per-user config and credential storage.

Manages three tables (users, user_credentials, user_config) and a
telegram_admins table in the same SQLite database as TradeDatabase.
Credentials are Fernet-encrypted at rest.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config.settings import (
    Config,
    ExchangeConfig,
    PortConfig,
    RiskConfig,
    StrategyConfig,
    StrategyPreset,
)
from src.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_USERS_DDL = """\
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

_USER_CREDENTIALS_DDL = """\
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
    account_address_enc TEXT NOT NULL,
    api_wallet_enc      TEXT NOT NULL,
    api_secret_enc      TEXT NOT NULL,
    network             TEXT NOT NULL DEFAULT 'testnet',
    exchange            TEXT NOT NULL DEFAULT 'hyperliquid',
    passphrase_enc      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

_USER_CONFIG_DDL = """\
CREATE TABLE IF NOT EXISTS user_config (
    user_id                TEXT PRIMARY KEY REFERENCES users(user_id),
    active_preset          TEXT NOT NULL DEFAULT 'even_split',
    auto_execute           INTEGER NOT NULL DEFAULT 0,
    max_leverage           INTEGER NOT NULL DEFAULT 20,
    size_by_risk_json      TEXT NOT NULL DEFAULT '{"LOW":4.0,"MEDIUM":2.0,"HIGH":1.0}',
    custom_presets_json    TEXT NOT NULL DEFAULT '{}',
    max_open_positions     INTEGER NOT NULL DEFAULT 10,
    max_daily_loss_pct     REAL NOT NULL DEFAULT 10.0,
    max_position_size_usd  REAL NOT NULL DEFAULT 500.0,
    max_total_exposure_usd REAL NOT NULL DEFAULT 2000.0,
    min_order_usd          REAL NOT NULL DEFAULT 10.0,
    telegram_chat_id       INTEGER,
    port_usd               REAL,
    port_mode              TEXT NOT NULL DEFAULT 'withdraw',
    port_watermark         REAL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
"""

VALID_PORT_MODES = ("withdraw", "compound", "watermark")

# D2 preset rename — applied once via _migrate_active_preset. User-defined
# custom presets (in custom_presets_json) are untouched; only references to
# the six built-in names that no longer exist get rewritten.
_PRESET_RENAMES: dict[str, str] = {
    "runner": "even_split",            # same tp_split + BE-after-TP1
    "conservative": "tp1_only",         # same tp_split, no BE
    "tp2_exit": "tp2_be",               # both exit fully by TP2
    "tp3_hold": "tp3_be",               # same tp_split + BE-after-TP1
    "breakeven_filter": "even_split",   # same shape; old size_pct deferred to size_by_risk
    "small_runner": "even_split",       # same shape; old size_pct deferred to size_by_risk
}

_TELEGRAM_ADMINS_DDL = """\
CREATE TABLE IF NOT EXISTS telegram_admins (
    telegram_id   INTEGER PRIMARY KEY,
    added_by      INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""

@dataclass
class UserRecord:
    user_id: str
    display_name: str
    status: str
    created_at: str
    updated_at: str


class UserDatabase:
    """Manages user registration, encrypted credentials, and per-user config.

    Uses the same SQLite file as TradeDatabase but operates on separate tables.
    """

    def __init__(self, db_path: str | Path = "data/trades.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._create_tables()
        logger.info("UserDatabase ready: path=%s", self._db_path)

    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(_USERS_DDL)
            self._conn.execute(_USER_CREDENTIALS_DDL)
            self._conn.execute(_USER_CONFIG_DDL)
            self._conn.execute(_TELEGRAM_ADMINS_DDL)
            self._migrate_user_config()
            self._migrate_drop_saas_columns()
            self._migrate_user_credentials()

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        display_name: str,
        credentials: dict[str, str],
        config: dict[str, Any] | None = None,
    ) -> UserRecord:
        """Create a user with credentials and optional config overrides.

        Args:
            user_id: Unique identifier for the user.
            display_name: Human-readable name.
            credentials: Must contain 'account_address', 'api_wallet', 'api_secret'.
                         Optional: 'network' (default 'testnet'),
                         'exchange' (default 'hyperliquid'),
                         'passphrase' (Blofin only; encrypted, NULL otherwise).
            config: Optional dict of config overrides (keys match user_config columns).

        Returns:
            The created UserRecord.
        """
        now = _now()
        config = config or {}
        passphrase = credentials.get("passphrase")

        with self._conn:
            # Insert user
            self._conn.execute(
                "INSERT INTO users (user_id, display_name, status, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, ?)",
                (user_id, display_name, now, now),
            )

            # Insert encrypted credentials
            self._conn.execute(
                "INSERT INTO user_credentials "
                "(user_id, account_address_enc, api_wallet_enc, api_secret_enc, "
                "network, exchange, passphrase_enc, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    encrypt(credentials["account_address"]),
                    encrypt(credentials["api_wallet"]),
                    encrypt(credentials["api_secret"]),
                    credentials.get("network", "testnet"),
                    credentials.get("exchange", "hyperliquid"),
                    encrypt(passphrase) if passphrase else None,
                    now,
                    now,
                ),
            )

            # Insert config (with defaults for missing keys)
            size_by_risk = config.get("size_by_risk", {"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0})
            custom_presets = config.get("custom_presets", {})
            self._conn.execute(
                "INSERT INTO user_config "
                "(user_id, active_preset, auto_execute, max_leverage, size_by_risk_json, "
                "custom_presets_json, max_open_positions, max_daily_loss_pct, "
                "max_position_size_usd, max_total_exposure_usd, min_order_usd, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    config.get("active_preset", "even_split"),
                    int(config.get("auto_execute", False)),
                    config.get("max_leverage", 20),
                    json.dumps(size_by_risk),
                    json.dumps(custom_presets),
                    config.get("max_open_positions", 10),
                    config.get("max_daily_loss_pct", 10.0),
                    config.get("max_position_size_usd", 500.0),
                    config.get("max_total_exposure_usd", 2000.0),
                    config.get("min_order_usd", 10.0),
                    now,
                    now,
                ),
            )

        logger.info("Created user: %s (%s)", user_id, display_name)
        return UserRecord(
            user_id=user_id,
            display_name=display_name,
            status="active",
            created_at=now,
            updated_at=now,
        )

    def get_user(self, user_id: str) -> UserRecord | None:
        """Get a user by ID."""
        row = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return UserRecord(
            user_id=row["user_id"],
            display_name=row["display_name"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_users(self, status: str | None = None) -> list[UserRecord]:
        """List users, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
        return [
            UserRecord(
                user_id=r["user_id"],
                display_name=r["display_name"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_active_users(self) -> list[UserRecord]:
        """Return all users with status='active'."""
        return self.list_users(status="active")

    def set_user_status(self, user_id: str, status: str) -> None:
        """Set user status to 'active' or 'inactive'."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
                (status, now, user_id),
            )
        logger.info("User %s status → %s", user_id, status)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def get_user_credentials_decrypted(self, user_id: str) -> dict[str, str] | None:
        """Decrypt and return user credentials."""
        row = self._conn.execute(
            "SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        keys = row.keys()
        passphrase_enc = row["passphrase_enc"] if "passphrase_enc" in keys else None
        return {
            "account_address": decrypt(row["account_address_enc"]),
            "api_wallet": decrypt(row["api_wallet_enc"]),
            "api_secret": decrypt(row["api_secret_enc"]),
            "network": row["network"],
            "exchange": row["exchange"] if "exchange" in keys else "hyperliquid",
            "passphrase": decrypt(passphrase_enc) if passphrase_enc else "",
        }

    def update_user_credentials(self, user_id: str, **kwargs: str) -> None:
        """Update specific credential fields. Values are encrypted before storage."""
        now = _now()
        updates = []
        params: list[Any] = []

        enc_fields = {"account_address": "account_address_enc",
                       "api_wallet": "api_wallet_enc",
                       "api_secret": "api_secret_enc",
                       "passphrase": "passphrase_enc"}

        for key, value in kwargs.items():
            if key in enc_fields:
                updates.append(f"{enc_fields[key]} = ?")
                params.append(encrypt(value))
            elif key in ("network", "exchange"):
                updates.append(f"{key} = ?")
                params.append(value)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(now)
        params.append(user_id)

        with self._conn:
            self._conn.execute(
                f"UPDATE user_credentials SET {', '.join(updates)} WHERE user_id = ?",
                params,
            )
        logger.info("Updated credentials for user %s", user_id)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_user_config(self, user_id: str) -> dict[str, Any] | None:
        """Return raw user config as a dict."""
        row = self._conn.execute(
            "SELECT * FROM user_config WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "active_preset": row["active_preset"],
            "auto_execute": bool(row["auto_execute"]),
            "max_leverage": row["max_leverage"],
            "size_by_risk": json.loads(row["size_by_risk_json"]),
            "custom_presets": json.loads(row["custom_presets_json"]),
            "max_open_positions": row["max_open_positions"],
            "max_daily_loss_pct": row["max_daily_loss_pct"],
            "max_position_size_usd": row["max_position_size_usd"],
            "max_total_exposure_usd": row["max_total_exposure_usd"],
            "min_order_usd": row["min_order_usd"],
            "port_usd": row["port_usd"],
            "port_mode": row["port_mode"],
            "port_watermark": row["port_watermark"],
        }

    def update_user_config(self, user_id: str, **kwargs: Any) -> None:
        """Update specific config fields."""
        now = _now()
        updates = []
        params: list[Any] = []

        # Map Python names to column names for JSON fields
        json_fields = {"size_by_risk": "size_by_risk_json", "custom_presets": "custom_presets_json"}
        direct_fields = {
            "active_preset", "auto_execute", "max_leverage",
            "max_open_positions", "max_daily_loss_pct",
            "max_position_size_usd", "max_total_exposure_usd", "min_order_usd",
            "port_usd", "port_mode", "port_watermark",
        }

        for key, value in kwargs.items():
            if key in json_fields:
                updates.append(f"{json_fields[key]} = ?")
                params.append(json.dumps(value))
            elif key in direct_fields:
                if key == "auto_execute":
                    value = int(value)
                if key == "port_mode" and value not in VALID_PORT_MODES:
                    raise ValueError(
                        f"Invalid port_mode '{value}'; expected one of {VALID_PORT_MODES}"
                    )
                updates.append(f"{key} = ?")
                params.append(value)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(now)
        params.append(user_id)

        with self._conn:
            self._conn.execute(
                f"UPDATE user_config SET {', '.join(updates)} WHERE user_id = ?",
                params,
            )
        logger.info("Updated config for user %s", user_id)

    # ------------------------------------------------------------------
    # Mainnet defaults (Phase 3.5)
    # ------------------------------------------------------------------

    # Position cap applied to users on mainnet — stricter than the testnet
    # default ($500) to slow real-money mistakes. Above this value, the
    # mainnet promotion gate also forces manual confirmation.
    MAINNET_DEFAULT_POSITION_CAP_USD = 100.0

    def apply_mainnet_defaults(self, user_id: str) -> None:
        """Tighten a user's per-user config to mainnet-appropriate defaults.

        Currently lowers max_position_size_usd from the testnet default of
        $500 to $100. ``auto_execute`` is left untouched — the default is
        already OFF, and if the user has explicitly turned it ON we don't
        silently flip it back (would surprise them). The big-trade
        confirmation gate handles the auto-execute-on case.

        Called by:
        - Registration mainnet path (registration.py).
        - /promote_to_mainnet conversation (promotion.py).
        """
        self.update_user_config(
            user_id,
            max_position_size_usd=self.MAINNET_DEFAULT_POSITION_CAP_USD,
        )
        logger.info(
            "Applied mainnet defaults for user %s (max_position_size_usd=%.2f)",
            user_id, self.MAINNET_DEFAULT_POSITION_CAP_USD,
        )

    # ------------------------------------------------------------------
    # Port management (D1 — port/wallet separation)
    # ------------------------------------------------------------------

    def set_port(
        self,
        user_id: str,
        port_usd: float,
        port_mode: str = "withdraw",
    ) -> None:
        """Configure a user's port — the trading capital subset their
        position sizing scales against.

        For ``watermark`` mode, the initial ``port_usd`` also becomes the
        initial ``port_watermark`` (floor). For ``withdraw`` and ``compound``,
        ``port_watermark`` is set to NULL (unused).

        Raises:
            ValueError: If port_mode is not one of VALID_PORT_MODES, or
                port_usd is not positive.
        """
        if port_mode not in VALID_PORT_MODES:
            raise ValueError(
                f"Invalid port_mode '{port_mode}'; expected one of {VALID_PORT_MODES}"
            )
        if port_usd <= 0:
            raise ValueError(f"port_usd must be positive, got {port_usd}")

        watermark = port_usd if port_mode == "watermark" else None
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET port_usd = ?, port_mode = ?, "
                "port_watermark = ?, updated_at = ? WHERE user_id = ?",
                (port_usd, port_mode, watermark, now, user_id),
            )
        logger.info(
            "Set port for user %s: usd=%.2f mode=%s watermark=%s",
            user_id, port_usd, port_mode, watermark,
        )

    def get_port_state(self, user_id: str) -> dict[str, Any] | None:
        """Return port_usd, port_mode, port_watermark for *user_id*.

        Returns:
            ``{"port_usd": float|None, "port_mode": str, "port_watermark": float|None}``
            or None if the user doesn't exist.
        """
        row = self._conn.execute(
            "SELECT port_usd, port_mode, port_watermark FROM user_config "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "port_usd": row["port_usd"],
            "port_mode": row["port_mode"],
            "port_watermark": row["port_watermark"],
        }

    def apply_pnl_to_port(self, user_id: str, pnl_usd: float) -> dict[str, float | None]:
        """Apply realized P&L to a user's port according to their mode.

        - ``withdraw``: no change to port_usd.
        - ``compound``: ``port_usd += pnl_usd`` (both signs).
        - ``watermark``: profits raise port + floor; losses clamp at floor
          (per D1 — "losses never below the highest floor reached").

        No-op if the user has no port_usd configured or doesn't exist.

        Returns:
            The new {"port_usd", "port_watermark"} after the update (or the
            existing values if no change was made).
        """
        state = self.get_port_state(user_id)
        if not state or state["port_usd"] is None:
            return state or {"port_usd": None, "port_watermark": None}

        port_usd = state["port_usd"]
        port_mode = state["port_mode"]
        port_watermark = state["port_watermark"]

        if port_mode == "withdraw":
            return {"port_usd": port_usd, "port_watermark": port_watermark}

        if port_mode == "compound":
            new_port = port_usd + pnl_usd
            self._write_port_values(user_id, new_port, port_watermark)
            return {"port_usd": new_port, "port_watermark": port_watermark}

        # watermark
        new_port = port_usd + pnl_usd
        new_watermark = port_watermark if port_watermark is not None else port_usd
        if pnl_usd >= 0:
            # Profit: raise floor if we crossed the old high.
            if new_port > new_watermark:
                new_watermark = new_port
        else:
            # Loss: clamp at the floor (D1 — "never below the highest floor reached").
            if new_port < new_watermark:
                new_port = new_watermark
        self._write_port_values(user_id, new_port, new_watermark)
        return {"port_usd": new_port, "port_watermark": new_watermark}

    def _write_port_values(
        self, user_id: str, port_usd: float, port_watermark: float | None
    ) -> None:
        """Persist port_usd + port_watermark for *user_id*."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET port_usd = ?, port_watermark = ?, "
                "updated_at = ? WHERE user_id = ?",
                (port_usd, port_watermark, now, user_id),
            )

    def get_user_config_as_config(self, user_id: str, global_config: Config) -> Config:
        """Build a per-user Config by merging DB values into the global config.

        Overrides exchange credentials, strategy settings, and risk limits
        from the user's DB records. Keeps input, database, logging, health,
        and discord settings from the global config.
        """
        user_creds = self.get_user_credentials_decrypted(user_id)
        user_cfg = self.get_user_config(user_id)

        if not user_creds or not user_cfg:
            raise ValueError(f"User {user_id} not found or incomplete data")

        # Build custom presets
        custom_presets: dict[str, StrategyPreset] = {}
        for name, preset_data in user_cfg.get("custom_presets", {}).items():
            if isinstance(preset_data, dict):
                custom_presets[name] = StrategyPreset(
                    tp_split=preset_data.get("tp_split", [0.33, 0.33, 0.34]),
                    move_sl_to_breakeven_after=preset_data.get("move_sl_to_breakeven_after", "tp1"),
                    size_pct=preset_data.get("size_pct", 2.0),
                )

        return Config(
            exchange=ExchangeConfig(
                network=user_creds["network"],
                account_address=user_creds["account_address"],
                api_wallet=user_creds["api_wallet"],
                api_secret=user_creds["api_secret"],
                exchange=user_creds.get("exchange", "hyperliquid"),
                passphrase=user_creds.get("passphrase", ""),
            ),
            input=global_config.input,
            strategy=StrategyConfig(
                active_preset=user_cfg["active_preset"],
                auto_execute=user_cfg["auto_execute"],
                max_leverage=user_cfg["max_leverage"],
                size_by_risk=user_cfg["size_by_risk"],
                presets=custom_presets,
            ),
            risk=RiskConfig(
                max_open_positions=user_cfg["max_open_positions"],
                max_daily_loss_pct=user_cfg["max_daily_loss_pct"],
                max_position_size_usd=user_cfg["max_position_size_usd"],
                max_total_exposure_usd=user_cfg["max_total_exposure_usd"],
                min_order_usd=user_cfg["min_order_usd"],
            ),
            port=PortConfig(
                port_usd=user_cfg.get("port_usd"),
                port_mode=user_cfg.get("port_mode", "withdraw"),
                port_watermark=user_cfg.get("port_watermark"),
            ),
            database=global_config.database,
            logging=global_config.logging,
            health=global_config.health,
            discord=global_config.discord,
        )

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def _migrate_user_config(self) -> None:
        """Add new columns to user_config if they don't exist (for existing DBs)."""
        cursor = self._conn.execute("PRAGMA table_info(user_config)")
        existing = {row[1] for row in cursor.fetchall()}
        migrations = {
            "telegram_chat_id": "ALTER TABLE user_config ADD COLUMN telegram_chat_id INTEGER",
            "port_usd": "ALTER TABLE user_config ADD COLUMN port_usd REAL",
            "port_mode": "ALTER TABLE user_config ADD COLUMN port_mode TEXT NOT NULL DEFAULT 'withdraw'",
            "port_watermark": "ALTER TABLE user_config ADD COLUMN port_watermark REAL",
        }
        for col, sql in migrations.items():
            if col not in existing:
                self._conn.execute(sql)
                logger.info("Migrated user_config: added column %s", col)

        self._migrate_active_preset()

    def _migrate_user_credentials(self) -> None:
        """Add multi-exchange columns to user_credentials for existing DBs.

        ``exchange`` discriminates the adapter (defaults to 'hyperliquid' so
        every pre-Blofin user keeps working). ``passphrase_enc`` holds the
        Fernet-encrypted Blofin passphrase (NULL for HL users). Idempotent
        ALTER-if-missing, matching ``_migrate_user_config``.
        """
        cursor = self._conn.execute("PRAGMA table_info(user_credentials)")
        existing = {row[1] for row in cursor.fetchall()}
        migrations = {
            "exchange": "ALTER TABLE user_credentials ADD COLUMN exchange TEXT NOT NULL DEFAULT 'hyperliquid'",
            "passphrase_enc": "ALTER TABLE user_credentials ADD COLUMN passphrase_enc TEXT",
        }
        for col, sql in migrations.items():
            if col not in existing:
                self._conn.execute(sql)
                logger.info("Migrated user_credentials: added column %s", col)

    def _migrate_drop_saas_columns(self) -> None:
        """One-shot D4 migration: drop the SaaS-era invite_codes table and
        the invite_code / access_expires_at columns from user_config.

        Requires SQLite >= 3.35 for DROP COLUMN; on older versions the
        ALTER will fail silently and the columns stick around as dead
        weight (harmless — we just don't read them anymore).
        """
        # Drop the invite_codes table outright
        try:
            self._conn.execute("DROP TABLE IF EXISTS invite_codes")
        except sqlite3.OperationalError:
            pass

        cursor = self._conn.execute("PRAGMA table_info(user_config)")
        existing = {row[1] for row in cursor.fetchall()}
        for col in ("invite_code", "access_expires_at"):
            if col in existing:
                try:
                    self._conn.execute(f"ALTER TABLE user_config DROP COLUMN {col}")
                    logger.info("Dropped user_config column: %s", col)
                except sqlite3.OperationalError as e:
                    logger.warning(
                        "Could not drop user_config.%s (SQLite < 3.35?): %s — "
                        "column will remain unused", col, e,
                    )

    def _migrate_active_preset(self) -> None:
        """Rename old built-in preset names to their D2 equivalents.

        Idempotent — re-running on an already-migrated DB is a no-op.
        Custom presets (defined in custom_presets_json) are untouched.
        """
        for old_name, new_name in _PRESET_RENAMES.items():
            cursor = self._conn.execute(
                "UPDATE user_config SET active_preset = ? WHERE active_preset = ?",
                (new_name, old_name),
            )
            if cursor.rowcount > 0:
                logger.info(
                    "Migrated active_preset for %d user(s): '%s' -> '%s'",
                    cursor.rowcount, old_name, new_name,
                )

    # ------------------------------------------------------------------
    # Telegram chat ID
    # ------------------------------------------------------------------

    def set_telegram_chat_id(self, user_id: str, chat_id: int) -> None:
        """Store the Telegram chat ID for a user."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE user_config SET telegram_chat_id = ?, updated_at = ? WHERE user_id = ?",
                (chat_id, now, user_id),
            )

    def get_telegram_chat_id(self, user_id: str) -> int | None:
        """Get the Telegram chat ID for a user."""
        row = self._conn.execute(
            "SELECT telegram_chat_id FROM user_config WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row or row["telegram_chat_id"] is None:
            return None
        return row["telegram_chat_id"]

    def get_user_by_telegram_chat_id(self, chat_id: int) -> str | None:
        """Look up user_id by Telegram chat ID. Returns None if not found."""
        row = self._conn.execute(
            "SELECT user_id FROM user_config WHERE telegram_chat_id = ?", (chat_id,)
        ).fetchone()
        return row["user_id"] if row else None

    def get_all_telegram_chat_ids(self) -> list[int]:
        """Get all Telegram chat IDs for active users."""
        rows = self._conn.execute(
            "SELECT uc.telegram_chat_id FROM user_config uc "
            "JOIN users u ON u.user_id = uc.user_id "
            "WHERE u.status = 'active' AND uc.telegram_chat_id IS NOT NULL"
        ).fetchall()
        return [row["telegram_chat_id"] for row in rows]

    # ------------------------------------------------------------------
    # Invite codes
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Telegram admin management
    # ------------------------------------------------------------------

    def add_telegram_admin(self, telegram_id: int, added_by: int) -> bool:
        """Add a Telegram admin. Returns False if already exists."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO telegram_admins (telegram_id, added_by, created_at) VALUES (?, ?, ?)",
                    (telegram_id, added_by, _now()),
                )
            logger.info("Added Telegram admin %d (by %d)", telegram_id, added_by)
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_telegram_admin(self, telegram_id: int) -> bool:
        """Remove a Telegram admin. Returns False if not found."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM telegram_admins WHERE telegram_id = ?", (telegram_id,)
            )
        if cursor.rowcount > 0:
            logger.info("Removed Telegram admin %d", telegram_id)
            return True
        return False

    def list_telegram_admins(self) -> list[int]:
        """Return all dynamically added Telegram admin IDs."""
        rows = self._conn.execute(
            "SELECT telegram_id FROM telegram_admins ORDER BY created_at"
        ).fetchall()
        return [row["telegram_id"] for row in rows]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

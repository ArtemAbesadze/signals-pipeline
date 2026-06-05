"""SQLite trade state persistence.

Each TradeDatabase instance is scoped to a single user_id.
Tables are auto-created on first connection.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.state.models import (
    EventType,
    OrderRecord,
    OrderStatus,
    OrderType,
    TradeEvent,
    TradeRecord,
    TradeStatus,
)

logger = logging.getLogger(__name__)

_TRADES_DDL = """\
CREATE TABLE IF NOT EXISTS trades (
    trade_id        INTEGER NOT NULL,
    user_id         TEXT    NOT NULL,
    pair            TEXT    NOT NULL,
    coin            TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    risk_level      TEXT    NOT NULL,
    trade_type      TEXT    NOT NULL,
    size_hint       TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    stop_loss       REAL    NOT NULL,
    tp1             REAL    NOT NULL,
    tp2             REAL    NOT NULL,
    tp3             REAL    NOT NULL,
    leverage        INTEGER NOT NULL,
    signal_leverage INTEGER NOT NULL,
    position_size_usd  REAL NOT NULL,
    position_size_coin REAL NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    closed_at       TEXT,
    close_reason    TEXT,
    pnl_pct         REAL,
    notes           TEXT,
    raw_signal_text TEXT,
    decision_snapshot TEXT,    -- JSON; see pipeline._build_decision_snapshot
    requires_confirmation INTEGER NOT NULL DEFAULT 0,  -- Phase 3.5 mainnet gate
    PRIMARY KEY (user_id, trade_id)
);
"""

_TRADE_EVENTS_DDL = """\
CREATE TABLE IF NOT EXISTS trade_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     INTEGER,                  -- nullable: parse errors may have no trade
    user_id      TEXT    NOT NULL,
    occurred_at  TEXT    NOT NULL,
    event_type   TEXT    NOT NULL,
    raw_text     TEXT,
    action_taken TEXT
);
"""

_ORDERS_DDL = """\
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    INTEGER NOT NULL,
    user_id     TEXT    NOT NULL,
    order_type  TEXT    NOT NULL,
    coin        TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    size        REAL    NOT NULL,
    price       REAL    NOT NULL,
    oid         TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    fill_price  REAL,
    fill_price_source TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    FOREIGN KEY (user_id, trade_id) REFERENCES trades (user_id, trade_id)
);
"""

_INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (user_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders (user_id, trade_id);",
    "CREATE INDEX IF NOT EXISTS idx_orders_oid ON orders (oid) WHERE oid IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events (user_id, trade_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON trade_events (user_id, event_type);",
    "CREATE INDEX IF NOT EXISTS idx_events_time ON trade_events (occurred_at);",
]


def _now() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


class TradeDatabase:
    """SQLite-backed trade state store, scoped to a single user.

    Args:
        user_id: Identifier for the user (used in all queries).
        db_path: Path to the SQLite database file.
            Parent directories are created automatically.
    """

    def __init__(self, user_id: str, db_path: str | Path = "data/trades.db"):
        self._user_id = user_id
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._create_tables()
        logger.info(
            "TradeDatabase ready: user=%s path=%s", user_id, self._db_path
        )

    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(_TRADES_DDL)
            self._conn.execute(_ORDERS_DDL)
            self._conn.execute(_TRADE_EVENTS_DDL)
            # Migrations for existing DBs — each ALTER is wrapped to ignore
            # "duplicate column" errors when the column already exists.
            for sql in (
                "ALTER TABLE trades ADD COLUMN notes TEXT",
                "ALTER TABLE trades ADD COLUMN raw_signal_text TEXT",
                "ALTER TABLE trades ADD COLUMN decision_snapshot TEXT",
                "ALTER TABLE trades ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0",
            ):
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists
            # orders.oid INTEGER → TEXT rebuild (Blofin order IDs are strings;
            # HL int oids round-trip as their string form). Runs before index
            # creation so the indexes always land on the final table shape.
            self._migrate_orders_oid_to_text()
            # Added after the oid rebuild so the rebuild's column copy can't
            # drop it. D11 (6.8b): records whether fill_price is the exchange's
            # real averagePrice ('exchange') or a CP target-price estimate
            # ('cp_estimate'); NULL for legacy rows.
            for sql in (
                "ALTER TABLE orders ADD COLUMN fill_price_source TEXT",
            ):
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists
            for idx in _INDEXES_DDL:
                self._conn.execute(idx)

    def _migrate_orders_oid_to_text(self) -> None:
        """Rebuild ``orders`` with ``oid TEXT`` if it's still ``INTEGER``.

        SQLite can't ALTER a column's type, so we rebuild the table. The
        column had INTEGER affinity, which silently stored HL's integer
        oids fine — but Blofin order IDs / clientOrderIds are strings, and
        INTEGER affinity would coerce a numeric-looking string back to an
        int. TEXT affinity stores everything verbatim. Idempotent: returns
        early once ``oid`` is already TEXT (or the table doesn't exist yet,
        i.e. a fresh DB created from the TEXT DDL above).

        Runs inside the caller's ``with self._conn`` transaction. No other
        table has a foreign key *to* ``orders``, so the drop/rename is safe.
        """
        cols = {
            row[1]: (row[2] or "").upper()
            for row in self._conn.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "oid" not in cols or cols["oid"] == "TEXT":
            return
        logger.info("Migrating orders.oid %s → TEXT", cols["oid"] or "INTEGER")
        self._conn.execute(
            """
            CREATE TABLE orders_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id    INTEGER NOT NULL,
                user_id     TEXT    NOT NULL,
                order_type  TEXT    NOT NULL,
                coin        TEXT    NOT NULL,
                side        TEXT    NOT NULL,
                size        REAL    NOT NULL,
                price       REAL    NOT NULL,
                oid         TEXT,
                status      TEXT    NOT NULL DEFAULT 'pending',
                fill_price  REAL,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                FOREIGN KEY (user_id, trade_id) REFERENCES trades (user_id, trade_id)
            )
            """
        )
        self._conn.execute(
            """
            INSERT INTO orders_new (
                id, trade_id, user_id, order_type, coin, side, size, price,
                oid, status, fill_price, created_at, updated_at
            )
            SELECT id, trade_id, user_id, order_type, coin, side, size, price,
                   CASE WHEN oid IS NULL THEN NULL ELSE CAST(oid AS TEXT) END,
                   status, fill_price, created_at, updated_at
            FROM orders
            """
        )
        self._conn.execute("DROP TABLE orders")
        self._conn.execute("ALTER TABLE orders_new RENAME TO orders")
        logger.info("orders.oid migration complete")

    @property
    def user_id(self) -> str:
        return self._user_id

    # ------------------------------------------------------------------
    # Trade operations
    # ------------------------------------------------------------------

    def create_trade(self, trade: TradeRecord) -> TradeRecord:
        """Insert a new trade. The trade's user_id is overridden by this instance's user_id."""
        now = _now()
        snapshot_json = (
            json.dumps(trade.decision_snapshot) if trade.decision_snapshot else None
        )
        with self._conn:
            self._conn.execute(
                """INSERT INTO trades (
                    trade_id, user_id, pair, coin, side, risk_level, trade_type,
                    size_hint, entry_price, stop_loss, tp1, tp2, tp3,
                    leverage, signal_leverage, position_size_usd, position_size_coin,
                    status, created_at, updated_at,
                    raw_signal_text, decision_snapshot, requires_confirmation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.trade_id, self._user_id, trade.pair, trade.coin,
                    trade.side, trade.risk_level, trade.trade_type, trade.size_hint,
                    trade.entry_price, trade.stop_loss, trade.tp1, trade.tp2, trade.tp3,
                    trade.leverage, trade.signal_leverage,
                    trade.position_size_usd, trade.position_size_coin,
                    trade.status.value, now, now,
                    trade.raw_signal_text, snapshot_json,
                    int(trade.requires_confirmation),
                ),
            )
        trade.user_id = self._user_id
        trade.created_at = datetime.fromisoformat(now)
        trade.updated_at = trade.created_at
        logger.info("Created trade #%d for user %s", trade.trade_id, self._user_id)
        return trade

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        """Look up a trade by its Potion Perps trade ID."""
        row = self._conn.execute(
            "SELECT * FROM trades WHERE user_id = ? AND trade_id = ?",
            (self._user_id, trade_id),
        ).fetchone()
        return self._row_to_trade(row) if row else None

    def get_open_trades(self) -> list[TradeRecord]:
        """Return all trades with status 'pending' or 'open'."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE user_id = ? AND status IN ('pending', 'open') "
            "ORDER BY created_at",
            (self._user_id,),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_trades_by_status(self, status: TradeStatus) -> list[TradeRecord]:
        """Return all trades with a specific status."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE user_id = ? AND status = ? ORDER BY created_at",
            (self._user_id, status.value),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def get_completed_trades(self) -> list[TradeRecord]:
        """Return all closed and canceled trades (for history view)."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE user_id = ? AND status IN ('closed', 'canceled') "
            "ORDER BY created_at",
            (self._user_id,),
        ).fetchall()
        return [self._row_to_trade(r) for r in rows]

    def update_trade_status(
        self,
        trade_id: int,
        status: TradeStatus,
        close_reason: str | None = None,
        pnl_pct: float | None = None,
    ) -> None:
        """Update a trade's status and optionally set close fields."""
        now = _now()
        closed_at = now if status in (TradeStatus.CLOSED, TradeStatus.CANCELED) else None
        with self._conn:
            self._conn.execute(
                """UPDATE trades
                   SET status = ?, updated_at = ?, closed_at = COALESCE(?, closed_at),
                       close_reason = COALESCE(?, close_reason),
                       pnl_pct = COALESCE(?, pnl_pct)
                   WHERE user_id = ? AND trade_id = ?""",
                (status.value, now, closed_at, close_reason, pnl_pct,
                 self._user_id, trade_id),
            )
        logger.info(
            "Trade #%d status -> %s (reason=%s)", trade_id, status.value, close_reason
        )

    def get_daily_closed_pnl(self) -> float:
        """Return the sum of pnl_pct for all trades closed today (UTC).

        Only includes trades with a non-null pnl_pct and closed_at today.
        Returns 0.0 if no trades were closed today.
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = self._conn.execute(
            """SELECT COALESCE(SUM(pnl_pct), 0.0) AS total
               FROM trades
               WHERE user_id = ?
                 AND status = 'closed'
                 AND closed_at IS NOT NULL
                 AND closed_at >= ?
                 AND pnl_pct IS NOT NULL""",
            (self._user_id, today),
        ).fetchone()
        return float(row["total"])

    def get_expired_confirmations(self, cutoff_iso: str) -> list[int]:
        """Return trade_ids of pending trades requiring confirmation whose
        created_at is older than ``cutoff_iso``. Phase 3.5 — the pipeline's
        sweep loop uses this to surface trades that need auto-decline.
        """
        rows = self._conn.execute(
            """SELECT trade_id FROM trades
               WHERE user_id = ?
                 AND status = 'pending'
                 AND requires_confirmation = 1
                 AND created_at < ?
               ORDER BY created_at""",
            (self._user_id, cutoff_iso),
        ).fetchall()
        return [row["trade_id"] for row in rows]

    def get_total_open_exposure_usd(self) -> float:
        """Return the total USD exposure across all open/pending trades."""
        row = self._conn.execute(
            """SELECT COALESCE(SUM(position_size_usd), 0.0) AS total
               FROM trades
               WHERE user_id = ?
                 AND status IN ('pending', 'open')""",
            (self._user_id,),
        ).fetchone()
        return float(row["total"])

    def update_trade_notes(self, trade_id: int, notes: str) -> None:
        """Update the notes field of a trade."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE trades SET notes = ?, updated_at = ? WHERE user_id = ? AND trade_id = ?",
                (notes, now, self._user_id, trade_id),
            )
        logger.info("Updated notes for trade #%d", trade_id)

    # ------------------------------------------------------------------
    # Order operations
    # ------------------------------------------------------------------

    def record_order(
        self,
        trade_id: int,
        order_type: OrderType,
        coin: str,
        side: str,
        size: float,
        price: float,
        oid: str | int | None = None,
        status: OrderStatus = OrderStatus.PENDING,
    ) -> int:
        """Insert an order record. Returns the auto-generated row ID.

        ``oid`` is the exchange order ID — an int on HL, a string
        (orderId / clientOrderId) on Blofin. Stored as TEXT so both fit;
        we normalize to ``str`` on write for consistent equality queries.
        """
        now = _now()
        with self._conn:
            cursor = self._conn.execute(
                """INSERT INTO orders (
                    trade_id, user_id, order_type, coin, side, size, price,
                    oid, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id, self._user_id, order_type.value, coin, side,
                    size, price, None if oid is None else str(oid),
                    status.value, now, now,
                ),
            )
        row_id = cursor.lastrowid
        logger.info(
            "Recorded order: trade #%d %s %s %s @ %s (oid=%s)",
            trade_id, order_type.value, side, size, price, oid,
        )
        return row_id

    def update_order_status(
        self,
        oid: str | int,
        status: OrderStatus,
        fill_price: float | None = None,
        fill_price_source: str | None = None,
    ) -> None:
        """Update an order's status by its exchange oid (HL int or Blofin str).

        ``fill_price_source`` (D11, 6.8b) records the provenance of
        ``fill_price``: 'exchange' (real averagePrice read back) or
        'cp_estimate' (CP target-price approximation). Both are COALESCE-guarded
        so passing None leaves the existing value untouched."""
        now = _now()
        with self._conn:
            self._conn.execute(
                """UPDATE orders
                   SET status = ?, updated_at = ?,
                       fill_price = COALESCE(?, fill_price),
                       fill_price_source = COALESCE(?, fill_price_source)
                   WHERE oid = ? AND user_id = ?""",
                (status.value, now, fill_price, fill_price_source, str(oid), self._user_id),
            )

    def set_order_oid(self, row_id: int, oid: str | int) -> None:
        """Set the exchange oid after an order is submitted (stored as TEXT)."""
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE orders SET oid = ?, status = 'submitted', updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (str(oid), now, row_id, self._user_id),
            )

    def get_orders_for_trade(self, trade_id: int) -> list[OrderRecord]:
        """Return all orders associated with a trade."""
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE user_id = ? AND trade_id = ? ORDER BY id",
            (self._user_id, trade_id),
        ).fetchall()
        return [self._row_to_order(r) for r in rows]

    # ------------------------------------------------------------------
    # Row mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
        snapshot_raw = row["decision_snapshot"]
        snapshot = json.loads(snapshot_raw) if snapshot_raw else None
        return TradeRecord(
            trade_id=row["trade_id"],
            user_id=row["user_id"],
            pair=row["pair"],
            coin=row["coin"],
            side=row["side"],
            risk_level=row["risk_level"],
            trade_type=row["trade_type"],
            size_hint=row["size_hint"],
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            tp1=row["tp1"],
            tp2=row["tp2"],
            tp3=row["tp3"],
            leverage=row["leverage"],
            signal_leverage=row["signal_leverage"],
            position_size_usd=row["position_size_usd"],
            position_size_coin=row["position_size_coin"],
            status=TradeStatus(row["status"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            closed_at=_parse_dt(row["closed_at"]),
            close_reason=row["close_reason"],
            pnl_pct=row["pnl_pct"],
            notes=row["notes"],
            raw_signal_text=row["raw_signal_text"],
            decision_snapshot=snapshot,
            requires_confirmation=bool(row["requires_confirmation"]) if "requires_confirmation" in row.keys() else False,
        )

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            trade_id=row["trade_id"],
            user_id=row["user_id"],
            order_type=OrderType(row["order_type"]),
            coin=row["coin"],
            side=row["side"],
            size=row["size"],
            price=row["price"],
            oid=row["oid"],
            status=OrderStatus(row["status"]),
            fill_price=row["fill_price"],
            fill_price_source=(
                row["fill_price_source"] if "fill_price_source" in row.keys() else None
            ),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    # ------------------------------------------------------------------
    # Audit-log events (D3)
    # ------------------------------------------------------------------

    def record_event(
        self,
        trade_id: int | None,
        event_type: EventType,
        raw_text: str | None,
        action_taken: str | None,
    ) -> int:
        """Insert a row into trade_events. user_id comes from this instance.

        ``trade_id`` can be None for orphan events (e.g. parse errors that
        couldn't extract a trade ID). Returns the auto-generated row id.
        """
        now = _now()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO trade_events (trade_id, user_id, occurred_at, "
                "event_type, raw_text, action_taken) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trade_id, self._user_id, now, event_type.value, raw_text, action_taken),
            )
        return cursor.lastrowid

    def get_events_for_trade(self, trade_id: int) -> list[TradeEvent]:
        """Return the chronological event log for a single trade."""
        rows = self._conn.execute(
            "SELECT * FROM trade_events WHERE user_id = ? AND trade_id = ? "
            "ORDER BY occurred_at, id",
            (self._user_id, trade_id),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_events_for_user(
        self,
        limit: int = 100,
        event_type: EventType | None = None,
    ) -> list[TradeEvent]:
        """Return the N most recent events for this user.

        If ``event_type`` is given, filter to that type (e.g. error feed).
        """
        if event_type is None:
            rows = self._conn.execute(
                "SELECT * FROM trade_events WHERE user_id = ? "
                "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (self._user_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trade_events WHERE user_id = ? AND event_type = ? "
                "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (self._user_id, event_type.value, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_port_changes(self, limit: int = 5) -> list[dict]:
        """Return the N most recently closed trades with realized P&L.

        Each entry includes ``delta_usd = position_size_usd * pnl_pct / 100``
        — the raw per-trade port-delta. Whether that delta actually applied
        depends on the user's ``port_mode`` (withdraw=ignored, compound=
        applied both directions, watermark=clamped at floor on losses).
        Display logic lives in the formatter; this method just returns
        the candidates.
        """
        rows = self._conn.execute(
            "SELECT trade_id, coin, close_reason, pnl_pct, position_size_usd, closed_at "
            "FROM trades WHERE user_id = ? AND status = 'closed' "
            "AND pnl_pct IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
            (self._user_id, limit),
        ).fetchall()
        return [
            {
                "trade_id": r["trade_id"],
                "coin": r["coin"],
                "close_reason": r["close_reason"],
                "pnl_pct": r["pnl_pct"],
                "delta_usd": float(r["position_size_usd"]) * float(r["pnl_pct"]) / 100.0,
                "closed_at": _parse_dt(r["closed_at"]),
            }
            for r in rows
        ]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> TradeEvent:
        return TradeEvent(
            id=row["id"],
            trade_id=row["trade_id"],
            user_id=row["user_id"],
            occurred_at=_parse_dt(row["occurred_at"]),
            event_type=EventType(row["event_type"]),
            raw_text=row["raw_text"],
            action_taken=row["action_taken"],
        )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

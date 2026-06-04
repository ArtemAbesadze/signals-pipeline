"""Data models for trade and order state persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TradeStatus(Enum):
    """Lifecycle status of a trade."""

    PREPARING = "preparing"  # Preparation message received, not yet actionable
    PENDING = "pending"      # Signal received, entry order not yet placed
    OPEN = "open"            # Entry filled, position is live
    CLOSED = "closed"        # All TPs hit, SL hit, or manually closed
    CANCELED = "canceled"    # Trade canceled before or after entry


class OrderType(Enum):
    """Which part of the trade this order represents."""

    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TP1 = "tp1"
    TP2 = "tp2"
    TP3 = "tp3"


class OrderStatus(Enum):
    """Lifecycle status of an individual order on the exchange."""

    PENDING = "pending"      # Built but not yet submitted
    SUBMITTED = "submitted"  # Sent to exchange, resting
    FILLED = "filled"        # Fully filled
    CANCELED = "canceled"    # Canceled (by us or exchange)
    REJECTED = "rejected"    # Exchange rejected the order


class EventType(Enum):
    """Audit-log event types (D3). Every signal and lifecycle event is
    persisted to trade_events with one of these values."""

    SIGNAL_ALERT = "signal_alert"       # New signal received (opened or skipped)
    ORDER_PENDING = "order_pending"     # CP says entry not yet filled
    TRADE_LIVE = "trade_live"           # CP confirms entry filled
    TP_HIT = "tp_hit"                   # Single TP hit (TP1/TP2/TP3)
    BREAKEVEN = "breakeven"             # SL moved to entry
    STOP_HIT = "stop_hit"               # Stop hit, trade closed
    SL_MOVE = "sl_move"                 # SL moved by manual update
    TRADE_CLOSED = "trade_closed"       # ALL_TP_HIT or explicit TRADE_CLOSED
    CANCEL = "cancel"                   # Trade canceled
    ERROR = "error"                     # Parse failure, exchange rejection, etc.
    # Mainnet promotion gate (Phase 3.5)
    CONFIRMATION_REQUESTED = "confirmation_requested"  # Big mainnet trade held for manual approval
    CONFIRMATION_APPROVED = "confirmation_approved"    # User approved via Telegram
    CONFIRMATION_DECLINED = "confirmation_declined"    # User rejected via Telegram
    CONFIRMATION_TIMEOUT = "confirmation_timeout"      # No response within mainnet_confirm_timeout_min


@dataclass
class TradeRecord:
    """One row per signal — tracks the full lifecycle of a trade."""

    trade_id: int            # Potion Perps trade ID (e.g. 1286)
    user_id: str
    pair: str                # e.g. "ZK/USDT"
    coin: str                # Hyperliquid coin name (e.g. "ZK")
    side: str                # "LONG" or "SHORT"
    risk_level: str          # "LOW", "MEDIUM", "HIGH"
    trade_type: str          # "SWING", "SCALP", or "POSITION"
    size_hint: str           # e.g. "1-4%"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    leverage: int            # Actual leverage used (after capping)
    signal_leverage: int     # Original leverage from signal
    position_size_usd: float # Actual USD position size
    position_size_coin: float  # Actual coin quantity
    status: TradeStatus = TradeStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None
    close_reason: str | None = None  # "all_tp_hit", "stop_hit", "manual", "canceled"
    pnl_pct: float | None = None     # Final P&L % (from signal provider or calculated)
    notes: str | None = None          # User-provided trade journal notes
    # D3 audit fields — verbatim signal text + decision snapshot captured at open
    raw_signal_text: str | None = None
    decision_snapshot: dict | None = None
    # Phase 3.5 — set when a mainnet trade above the confirm threshold is held
    # PENDING for the user to approve/reject via Telegram. Auto-declines on
    # the configured timeout via the pipeline's confirmation sweep.
    requires_confirmation: bool = False


@dataclass
class TradeEvent:
    """One row in the audit log (trade_events). Every signal and lifecycle
    event we observe is persisted here for post-mortem debugging + reporting."""

    id: int | None           # auto-increment
    trade_id: int | None     # nullable — parse errors may have no trade
    user_id: str
    occurred_at: datetime
    event_type: EventType
    raw_text: str | None     # verbatim message that triggered this event
    action_taken: str | None # human-readable description of what we did


@dataclass
class OrderRecord:
    """One row per order placed on the exchange."""

    id: int | None           # Auto-increment primary key
    trade_id: int            # FK to TradeRecord.trade_id
    user_id: str
    order_type: OrderType    # entry, stop_loss, tp1, tp2, tp3
    coin: str
    side: str                # "BUY" or "SELL"
    size: float              # Order quantity
    price: float             # Limit / trigger price
    oid: str | int | None = None  # Exchange order ID — HL int, or Blofin orderId/clientOrderId str (set after submission)
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

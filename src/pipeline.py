"""Signal processing pipeline — the core orchestrator.

Receives raw messages, classifies them, and dispatches to the appropriate
handler. For new signals: sizes the position, builds orders, submits to
the exchange, and records in the database. For lifecycle events: updates
the trade state accordingly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.telegram.notifications import TelegramNotifier

from src.config.settings import Config, StrategyPreset
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.order_builder import TradeOrderSet, build_orders
from src.utils.symbol_mapper import potion_to_hyperliquid
from src.exchange.position_manager import OrderSubmissionError, PositionManager
from src.parser.classifier import MessageType, classify
from src.parser.signal_parser import ParsedSignal, SignalParseError, parse_signal
from src.parser.update_parser import (
    UpdateParseError,
    parse_all_tp_hit,
    parse_breakeven,
    parse_canceled,
    parse_manual_update,
    parse_order_pending,
    parse_preparation,
    parse_sl_update,
    parse_stop_hit,
    parse_tp_hit,
    parse_trade_closed,
    parse_trade_live,
)
import re

from src.state.database import TradeDatabase
from src.state.models import EventType, TradeRecord, TradeStatus
from src.state.user_db import UserDatabase
from src.strategy.position_sizer import (
    PositionSizeError,
    RiskLimitBreached,
    calculate_position_size,
    check_risk_limits,
)

# Port-vs-wallet warning threshold (D1): warn but continue if port is more
# than this fraction of wallet, halt entirely if port > wallet outright.
_PORT_WARN_FRACTION = 0.95

_TRADE_ID_RE = re.compile(r"#(\d{3,})")

# Max length we'll store in trade_events.action_taken. Keeps the audit
# table compact when an upstream error includes a long stack trace or
# raw exchange-response blob (D6).
_MAX_ACTION_TAKEN_LEN = 1000


def _extract_trade_id_from_text(raw: str) -> int | None:
    """Best-effort extraction of a trade_id from any CP message.

    Used by the error-event path so a parse failure still ties to a trade
    when the message contained ``#NNNN`` — even if the structured parser
    couldn't get further. Returns ``None`` if no match.
    """
    if not isinstance(raw, str):
        return None
    m = _TRADE_ID_RE.search(raw)
    return int(m.group(1)) if m else None


def _truncate(s: str, limit: int = _MAX_ACTION_TAKEN_LEN) -> str:
    """Trim *s* to *limit* chars, appending an ellipsis marker if cut."""
    if len(s) <= limit:
        return s
    return s[: limit - len(" … (truncated)")] + " … (truncated)"

logger = logging.getLogger(__name__)


class Pipeline:
    """Processes raw signal messages end-to-end.

    Wires together: classifier → parser → position sizer → order builder
    → position manager → database.

    Each instance is scoped to a single user.
    """

    def __init__(
        self,
        config: Config,
        client: HyperliquidClient,
        db: TradeDatabase,
        user_db: UserDatabase | None = None,
        notifier: TelegramNotifier | None = None,
    ):
        self._config = config
        self._client = client
        self._db = db
        self._user_db = user_db
        self._pm = PositionManager(client, db)
        self._asset_meta = client.get_asset_meta()
        self._notifier = notifier

    def process_message(self, raw_message: str) -> None:
        """Classify and process a single raw message.

        This is the main entry point — call once per incoming message.
        """
        msg_type = classify(raw_message)
        logger.info("Classified message as: %s", msg_type.value)

        handlers = {
            MessageType.SIGNAL_ALERT: self._handle_signal,
            MessageType.ORDER_PENDING: self._handle_order_pending,
            MessageType.TRADE_LIVE: self._handle_trade_live,
            MessageType.TP_HIT: self._handle_tp_hit,
            MessageType.ALL_TP_HIT: self._handle_all_tp_hit,
            MessageType.BREAKEVEN: self._handle_breakeven,
            MessageType.STOP_HIT: self._handle_stop_hit,
            MessageType.CANCELED: self._handle_canceled,
            MessageType.TRADE_CLOSED: self._handle_trade_closed,
            MessageType.PREPARATION: self._handle_preparation,
            MessageType.MANUAL_UPDATE: self._handle_manual_update,
            MessageType.NOISE: self._handle_noise,
        }

        handler = handlers.get(msg_type, self._handle_noise)
        try:
            handler(raw_message)
        except (SignalParseError, UpdateParseError) as e:
            logger.error("Parse error for %s: %s", msg_type.value, e)
            self._record_event(
                trade_id=_extract_trade_id_from_text(raw_message),
                event_type=EventType.ERROR,
                raw_text=raw_message,
                action_taken=f"parse error ({msg_type.value}): {e}",
            )
        except Exception as e:
            logger.error("Unexpected error handling %s: %s", msg_type.value, e, exc_info=True)
            self._record_event(
                trade_id=_extract_trade_id_from_text(raw_message),
                event_type=EventType.ERROR,
                raw_text=raw_message,
                action_taken=f"unexpected error ({msg_type.value}): {e}",
            )

    # ------------------------------------------------------------------
    # Signal handling — new trade
    # ------------------------------------------------------------------

    def _handle_signal(self, raw: str) -> None:
        """Parse signal → port guardrails → size → build orders → submit."""
        signal = parse_signal(raw)
        preset = self._config.get_active_preset()
        auto_execute = self._config.strategy.auto_execute

        # Check if we already have this trade (any status)
        existing = self._db.get_trade(signal.trade_id)
        if existing:
            logger.warning("Trade #%d already exists (status=%s), skipping", signal.trade_id, existing.status.value)
            self._record_event(
                trade_id=signal.trade_id,
                event_type=EventType.SIGNAL_ALERT,
                raw_text=raw,
                action_taken=f"skipped: trade #{signal.trade_id} already exists (status={existing.status.value})",
            )
            return

        # --- Port / wallet guardrails (D1) ---
        port_state = self._get_port_state()
        port_usd = port_state["port_usd"]

        if port_usd is None:
            logger.warning(
                "Trade #%d skipped: port not configured for user %s",
                signal.trade_id, self._db.user_id,
            )
            self._record_event(
                trade_id=signal.trade_id,
                event_type=EventType.SIGNAL_ALERT,
                raw_text=raw,
                action_taken="skipped: port not configured",
            )
            if self._notifier:
                self._notify(self._notifier.notify_port_not_configured(
                    signal.trade_id, signal.pair,
                ))
            return

        wallet_usd = self._get_wallet_balance_usd()
        if port_usd > wallet_usd:
            logger.warning(
                "Trade #%d halted: port $%.2f exceeds wallet $%.2f",
                signal.trade_id, port_usd, wallet_usd,
            )
            self._record_event(
                trade_id=signal.trade_id,
                event_type=EventType.SIGNAL_ALERT,
                raw_text=raw,
                action_taken=f"skipped: port ${port_usd:.2f} exceeds wallet ${wallet_usd:.2f}",
            )
            if self._notifier:
                self._notify(self._notifier.notify_port_exceeds_wallet(
                    signal.trade_id, signal.pair, port_usd, wallet_usd,
                ))
            return

        if port_usd > wallet_usd * _PORT_WARN_FRACTION:
            logger.warning(
                "Trade #%d: port $%.2f > 95%% of wallet $%.2f — proceeding with warning",
                signal.trade_id, port_usd, wallet_usd,
            )
            if self._notifier:
                self._notify(self._notifier.notify_port_warning(
                    signal.trade_id, signal.pair, port_usd, wallet_usd,
                ))

        # --- Calculate position size from port (not wallet — D1) ---
        size_warning: str | None = None
        try:
            position_size_usd = calculate_position_size(
                port_usd, signal.risk_level.value, preset,
                self._config.strategy, self._config.risk,
            )
        except PositionSizeError as e:
            if auto_execute:
                logger.warning("Skipping trade #%d: %s", signal.trade_id, e)
                self._record_event(
                    trade_id=signal.trade_id,
                    event_type=EventType.SIGNAL_ALERT,
                    raw_text=raw,
                    action_taken=f"skipped: {e}",
                )
                if self._notifier:
                    self._notify(self._notifier.notify_signal_skipped(
                        signal.trade_id, signal.pair, str(e),
                    ))
                return
            # Manual mode: record anyway so user sees it in calls view
            logger.info("Trade #%d below size minimum but recording as PENDING (auto_execute=OFF): %s", signal.trade_id, e)
            position_size_usd = 0.0
            size_warning = str(e)

        # Risk gate — check all limits before proceeding
        try:
            check_risk_limits(
                risk_config=self._config.risk,
                open_trade_count=len(self._db.get_open_trades()),
                daily_pnl_pct=self._db.get_daily_closed_pnl(),
                total_exposure_usd=self._db.get_total_open_exposure_usd(),
                new_position_usd=position_size_usd,
            )
        except RiskLimitBreached as e:
            if auto_execute:
                logger.warning("Skipping trade #%d — risk limit: %s", signal.trade_id, e)
                self._record_event(
                    trade_id=signal.trade_id,
                    event_type=EventType.SIGNAL_ALERT,
                    raw_text=raw,
                    action_taken=f"skipped: risk limit breached — {e}",
                )
                if self._notifier:
                    self._notify(self._notifier.notify_signal_skipped(
                        signal.trade_id, signal.pair, str(e),
                    ))
                return
            logger.info("Trade #%d exceeds risk limit but recording as PENDING (auto_execute=OFF): %s", signal.trade_id, e)

        # Build orders (needed for coin name, leverage capping, etc.)
        trade_set = None
        try:
            trade_set = build_orders(
                signal, max(position_size_usd, 10.0), self._asset_meta,
                tp_split=preset.tp_split,
                max_leverage=self._config.strategy.max_leverage,
            )
        except (ValueError, KeyError) as e:
            if auto_execute:
                logger.error("Trade #%d order build failed: %s", signal.trade_id, e)
                self._record_event(
                    trade_id=signal.trade_id,
                    event_type=EventType.ERROR,
                    raw_text=raw,
                    action_taken=f"order build failed: {e}",
                )
                if self._notifier:
                    self._notify(self._notifier.notify_signal_skipped(
                        signal.trade_id, signal.pair, str(e),
                    ))
                return
            logger.info("Trade #%d order build failed but recording as PENDING (auto_execute=OFF): %s", signal.trade_id, e)

        # Derive fields — use trade_set if available, fall back to signal data
        coin = trade_set.coin if trade_set else potion_to_hyperliquid(signal.pair)
        leverage = trade_set.leverage if trade_set else min(signal.leverage, self._config.strategy.max_leverage)
        position_size_coin = trade_set.entry.sz if trade_set else 0.0

        # Build the decision snapshot (D3 — captures the inputs that
        # produced this trade for post-mortem auditing)
        decision_snapshot = self._build_decision_snapshot(
            signal=signal,
            preset=preset,
            port_state=port_state,
            wallet_usd=wallet_usd,
            position_size_usd=position_size_usd,
            leverage_applied=leverage,
        )

        # Record trade in DB (verbatim signal text + decision snapshot)
        trade_record = TradeRecord(
            trade_id=signal.trade_id,
            user_id=self._db.user_id,
            pair=signal.pair,
            coin=coin,
            side=signal.side.value,
            risk_level=signal.risk_level.value,
            trade_type=signal.trade_type,
            size_hint=signal.size,
            entry_price=signal.entry,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            tp3=signal.tp3,
            leverage=leverage,
            signal_leverage=signal.leverage,
            position_size_usd=position_size_usd,
            position_size_coin=position_size_coin,
            raw_signal_text=raw,
            decision_snapshot=decision_snapshot,
        )
        self._db.create_trade(trade_record)

        # Audit log: trade opened (D3)
        self._record_event(
            trade_id=signal.trade_id,
            event_type=EventType.SIGNAL_ALERT,
            raw_text=raw,
            action_taken=(
                f"opened #{signal.trade_id} {signal.pair} {signal.side.value} "
                f"size=${position_size_usd:.2f} lev={leverage}x"
                + (f" auto_execute=off" if not auto_execute else "")
            ),
        )

        # Notify new signal
        if self._notifier:
            self._notify(self._notifier.notify_new_signal(
                signal, trade_set or signal, position_size_usd,
                auto_execute=auto_execute,
                warning=size_warning,
            ))

        # Submit to exchange
        if auto_execute:
            try:
                self._pm.submit_trade(trade_set)
                logger.info(
                    "Trade #%d submitted: %s %s %s @ %s (lev=%dx, size=$%.2f)",
                    signal.trade_id, trade_set.coin, signal.side.value,
                    trade_set.entry.sz, signal.entry, trade_set.leverage, position_size_usd,
                )
                if self._notifier:
                    self._notify(self._notifier.notify_trade_opened(
                        signal.trade_id, trade_set.coin, signal.side.value,
                        signal.entry, position_size_usd,
                    ))
            except OrderSubmissionError as e:
                logger.error("Trade #%d submission failed: %s", signal.trade_id, e)
                self._db.update_trade_status(signal.trade_id, TradeStatus.CANCELED, close_reason="submission_failed")
                self._record_event(
                    trade_id=signal.trade_id,
                    event_type=EventType.ERROR,
                    raw_text=raw,
                    action_taken=f"submission failed: {e}",
                )
                if self._notifier:
                    self._notify(self._notifier.notify_trade_failed(
                        signal.trade_id, trade_set.coin, str(e),
                    ))
        else:
            logger.info(
                "Trade #%d ready (auto_execute=false): %s %s @ %s (lev=%dx, size=$%.2f)",
                signal.trade_id, coin, signal.side.value,
                signal.entry, leverage, position_size_usd,
            )

    # ------------------------------------------------------------------
    # Lifecycle events
    # ------------------------------------------------------------------

    def _handle_tp_hit(self, raw: str) -> None:
        """TP hit — update DB, optionally move SL to breakeven."""
        tp = parse_tp_hit(raw)
        trade = self._db.get_trade(tp.trade_id)
        if not trade:
            logger.warning("TP hit for unknown trade #%d", tp.trade_id)
            self._record_event(
                trade_id=tp.trade_id,
                event_type=EventType.TP_HIT,
                raw_text=raw,
                action_taken=f"TP{tp.tp_number} hit but trade is unknown locally",
            )
            return

        logger.info("TP%d hit for trade #%d %s (+%.2f%%)", tp.tp_number, tp.trade_id, tp.pair, tp.profit_pct)

        # Check if we should move SL to breakeven
        preset = self._config.get_active_preset()
        be_after = preset.move_sl_to_breakeven_after
        should_move = (
            (be_after == "tp1" and tp.tp_number == 1) or
            (be_after == "tp2" and tp.tp_number == 2)
        )
        be_moved = False
        if should_move and trade.status == TradeStatus.OPEN:
            be_moved = bool(self._pm.move_sl_to_breakeven(tp.trade_id, trade.coin, trade.entry_price))

        action = f"TP{tp.tp_number} hit at {tp.profit_pct:+.2f}%"
        if be_moved:
            action += f"; SL moved to entry ${trade.entry_price}"
        self._record_event(
            trade_id=tp.trade_id,
            event_type=EventType.TP_HIT,
            raw_text=raw,
            action_taken=action,
        )

        if self._notifier:
            self._notify(self._notifier.notify_tp_hit(
                tp.trade_id, trade.coin, tp.tp_number, tp.profit_pct,
            ))

    def _handle_all_tp_hit(self, raw: str) -> None:
        """All TPs hit — trade is fully closed."""
        atp = parse_all_tp_hit(raw)
        trade = self._db.get_trade(atp.trade_id)
        if not trade:
            logger.warning("All TP hit for unknown trade #%d", atp.trade_id)
            self._record_event(
                trade_id=atp.trade_id,
                event_type=EventType.TRADE_CLOSED,
                raw_text=raw,
                action_taken=f"all TPs hit at {atp.profit_pct:+.2f}% but trade is unknown locally",
            )
            return

        logger.info("All TPs hit for trade #%d %s (+%.2f%%)", atp.trade_id, atp.pair, atp.profit_pct)
        self._db.update_trade_status(
            atp.trade_id, TradeStatus.CLOSED,
            close_reason="all_tp_hit", pnl_pct=atp.profit_pct,
        )

        # Apply realized P&L to the user's port (D1 — withdraw/compound/watermark)
        self._apply_pnl_to_port(trade.position_size_usd, atp.profit_pct)

        self._record_event(
            trade_id=atp.trade_id,
            event_type=EventType.TRADE_CLOSED,
            raw_text=raw,
            action_taken=f"all TPs hit at {atp.profit_pct:+.2f}%, trade closed",
        )

        if self._notifier:
            self._notify(self._notifier.notify_all_tp_hit(
                atp.trade_id, trade.coin, atp.profit_pct,
            ))

    def _handle_breakeven(self, raw: str) -> None:
        """Breakeven — move SL to entry if not already done by TP-hit auto-move."""
        be = parse_breakeven(raw)
        trade = self._db.get_trade(be.trade_id)
        if not trade:
            logger.warning("Breakeven for unknown trade #%d", be.trade_id)
            self._record_event(
                trade_id=be.trade_id,
                event_type=EventType.BREAKEVEN,
                raw_text=raw,
                action_taken=f"BE after TP{be.tp_secured} for unknown trade",
            )
            return

        logger.info("Breakeven for trade #%d %s (TP%d secured)", be.trade_id, be.pair, be.tp_secured)

        moved = False
        if trade.status == TradeStatus.OPEN and self._config.strategy.auto_execute:
            moved = bool(self._pm.move_sl_to_breakeven(be.trade_id, trade.coin, trade.entry_price))
            if moved:
                logger.info("SL moved to breakeven for trade #%d via provider message", be.trade_id)
                if self._notifier:
                    self._notify(self._notifier.notify_breakeven(
                        be.trade_id, trade.coin, trade.entry_price,
                    ))
            # If move_sl_to_breakeven returns False it means no active SL was found
            # (likely already moved by the TP-hit auto-move) — that's fine.

        action = f"BE after TP{be.tp_secured}"
        if moved:
            action += f"; SL moved to entry ${trade.entry_price}"
        else:
            action += "; no SL move (already at BE or trade not open)"
        self._record_event(
            trade_id=be.trade_id,
            event_type=EventType.BREAKEVEN,
            raw_text=raw,
            action_taken=action,
        )

    def _handle_stop_hit(self, raw: str) -> None:
        """Stop hit — trade is closed at a loss."""
        sh = parse_stop_hit(raw)
        trade = self._db.get_trade(sh.trade_id)
        if not trade:
            logger.warning("Stop hit for unknown trade #%d", sh.trade_id)
            self._record_event(
                trade_id=sh.trade_id,
                event_type=EventType.STOP_HIT,
                raw_text=raw,
                action_taken=f"stop hit at {sh.loss_pct:+.2f}% but trade is unknown locally",
            )
            return

        logger.info("Stop hit for trade #%d %s (%.2f%%)", sh.trade_id, sh.pair, sh.loss_pct)
        self._db.update_trade_status(
            sh.trade_id, TradeStatus.CLOSED,
            close_reason="stop_hit", pnl_pct=sh.loss_pct,
        )

        # Apply realized P&L to the user's port (D1 — withdraw/compound/watermark)
        self._apply_pnl_to_port(trade.position_size_usd, sh.loss_pct)

        self._record_event(
            trade_id=sh.trade_id,
            event_type=EventType.STOP_HIT,
            raw_text=raw,
            action_taken=f"stop hit at {sh.loss_pct:+.2f}%, trade closed",
        )

        if self._notifier:
            self._notify(self._notifier.notify_stop_hit(
                sh.trade_id, trade.coin, sh.loss_pct,
            ))

    def _handle_canceled(self, raw: str) -> None:
        """Trade canceled — cancel all orders on exchange."""
        cancel = parse_canceled(raw)
        trade = self._db.get_trade(cancel.trade_id)
        if not trade:
            logger.warning("Cancel for unknown trade #%d", cancel.trade_id)
            self._record_event(
                trade_id=cancel.trade_id,
                event_type=EventType.CANCEL,
                raw_text=raw,
                action_taken=f"cancel for unknown trade — reason: {cancel.reason or 'n/a'}",
            )
            return

        logger.info("Trade #%d canceled: %s", cancel.trade_id, cancel.reason)
        if trade.status == TradeStatus.OPEN:
            self._pm.close_position(cancel.trade_id, trade.coin, reason="canceled")
            action = f"position closed; reason: {cancel.reason or 'n/a'}"
        else:
            self._pm.cancel_trade(cancel.trade_id)
            action = f"orders canceled; reason: {cancel.reason or 'n/a'}"

        self._record_event(
            trade_id=cancel.trade_id,
            event_type=EventType.CANCEL,
            raw_text=raw,
            action_taken=action,
        )

        if self._notifier:
            self._notify(self._notifier.notify_trade_canceled(
                cancel.trade_id, trade.coin, cancel.reason,
            ))

    def _handle_trade_closed(self, raw: str) -> None:
        """Trade manually closed — close position on exchange."""
        tc = parse_trade_closed(raw)
        trade = self._db.get_trade(tc.trade_id)
        if not trade:
            logger.warning("Trade closed for unknown trade #%d", tc.trade_id)
            self._record_event(
                trade_id=tc.trade_id,
                event_type=EventType.TRADE_CLOSED,
                raw_text=raw,
                action_taken=f"trade closed for unknown trade — detail: {tc.detail}",
            )
            return

        logger.info("Trade #%d closed: %s", tc.trade_id, tc.detail)
        if trade.status == TradeStatus.OPEN:
            self._pm.close_position(tc.trade_id, trade.coin, reason="manual_close")
        else:
            self._db.update_trade_status(tc.trade_id, TradeStatus.CLOSED, close_reason="manual_close")

        self._record_event(
            trade_id=tc.trade_id,
            event_type=EventType.TRADE_CLOSED,
            raw_text=raw,
            action_taken=f"closed: {tc.detail}",
        )

        if self._notifier:
            self._notify(self._notifier.notify_trade_closed(
                tc.trade_id, trade.coin, tc.detail,
            ))

    def _handle_order_pending(self, raw: str) -> None:
        """Order pending — log + notify; entry order stays resting on exchange.

        CP posts this when the entry price moved away before our limit
        filled. No exchange action — our order is already in place.
        """
        op = parse_order_pending(raw)
        trade = self._db.get_trade(op.trade_id)
        if not trade:
            logger.warning("ORDER_PENDING for unknown trade #%d", op.trade_id)
            self._record_event(
                trade_id=op.trade_id,
                event_type=EventType.ORDER_PENDING,
                raw_text=raw,
                action_taken="order pending for unknown trade",
            )
            return

        logger.info(
            "ORDER_PENDING: trade #%d %s — CP says price moved, waiting for pullback",
            op.trade_id, op.pair,
        )

        self._record_event(
            trade_id=op.trade_id,
            event_type=EventType.ORDER_PENDING,
            raw_text=raw,
            action_taken="CP says price moved, waiting for pullback (no exchange action)",
        )

        if self._notifier:
            self._notify(self._notifier.notify_order_pending(
                op.trade_id, trade.coin,
            ))

    def _handle_trade_live(self, raw: str) -> None:
        """Trade live — log + notify; exchange remains authoritative for status.

        CP confirms the entry has filled. We already know via the
        exchange order-fill event, so this is corroboration / audit only.
        """
        tl = parse_trade_live(raw)
        trade = self._db.get_trade(tl.trade_id)
        if not trade:
            logger.warning("TRADE_LIVE for unknown trade #%d", tl.trade_id)
            self._record_event(
                trade_id=tl.trade_id,
                event_type=EventType.TRADE_LIVE,
                raw_text=raw,
                action_taken="trade-live confirmation for unknown trade",
            )
            return

        logger.info(
            "TRADE_LIVE: trade #%d %s — CP confirms entry filled",
            tl.trade_id, tl.pair,
        )

        self._record_event(
            trade_id=tl.trade_id,
            event_type=EventType.TRADE_LIVE,
            raw_text=raw,
            action_taken="CP confirms entry filled (exchange is authoritative)",
        )

        if self._notifier:
            self._notify(self._notifier.notify_trade_live(
                tl.trade_id, trade.coin,
            ))

    def _handle_preparation(self, raw: str) -> None:
        """Preparation message — log but do not execute."""
        prep = parse_preparation(raw)
        logger.info(
            "Preparation: trade #%d %s %s (entry=%s, lev=%s) — NOT executing",
            prep.trade_id, prep.pair, prep.side, prep.entry, prep.leverage,
        )

    def _handle_manual_update(self, raw: str) -> None:
        """Manual update — try to detect actionable instructions (e.g. SL moves)."""
        # Try to detect an SL adjustment instruction first
        sl = parse_sl_update(raw)
        if sl:
            trade = self._db.get_trade(sl.trade_id)
            if not trade:
                logger.warning("SL update for unknown trade #%d", sl.trade_id)
                self._record_event(
                    trade_id=sl.trade_id,
                    event_type=EventType.SL_MOVE,
                    raw_text=raw,
                    action_taken=f"SL move to {sl.new_price} for unknown trade",
                )
                return
            if trade.status != TradeStatus.OPEN:
                logger.warning("SL update for non-open trade #%d (status=%s)", sl.trade_id, trade.status.value)
                self._record_event(
                    trade_id=sl.trade_id,
                    event_type=EventType.SL_MOVE,
                    raw_text=raw,
                    action_taken=f"SL move skipped — trade not open (status={trade.status.value})",
                )
                return

            logger.info("SL update detected: trade #%d → new SL %.6f", sl.trade_id, sl.new_price)
            if self._config.strategy.auto_execute:
                self._pm.move_stop_loss(sl.trade_id, trade.coin, sl.new_price)
                action = f"SL moved to {sl.new_price}"
                if self._notifier:
                    self._notify(self._notifier.notify_sl_moved(
                        sl.trade_id, trade.coin, sl.new_price,
                    ))
            else:
                logger.info("SL update ready (auto_execute=false) for trade #%d", sl.trade_id)
                action = f"SL move to {sl.new_price} ready (auto_execute=off)"
            self._record_event(
                trade_id=sl.trade_id,
                event_type=EventType.SL_MOVE,
                raw_text=raw,
                action_taken=action,
            )
            return

        # Not an SL instruction — log for human review
        mu = parse_manual_update(raw)
        logger.info(
            "Manual update: trade #%s %s — %s",
            mu.trade_id, mu.pair, mu.instruction,
        )

    def _handle_noise(self, raw: str) -> None:
        """Noise — ignore."""
        logger.debug("Noise message ignored")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify(self, coro) -> None:
        """Schedule an async notification without blocking the sync pipeline."""
        if self._notifier is None:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(coro)
        except RuntimeError:
            logger.debug("No event loop available for notification")

    def _get_wallet_balance_usd(self) -> float:
        """Get current USDC wallet balance (used for the port-vs-wallet guardrail)."""
        bal = self._client.get_balance()
        return float(bal["usdc_balance"])

    def _get_port_state(self) -> dict:
        """Return the current {port_usd, port_mode, port_watermark} for the user.

        Reads live from the UserDatabase when one is wired up. Falls back to
        the snapshot config when no user_db is attached (test paths, single-user
        ``.env`` fallback).
        """
        if self._user_db is not None:
            state = self._user_db.get_port_state(self._db.user_id)
            if state is not None:
                return state
        return {
            "port_usd": self._config.port.port_usd,
            "port_mode": self._config.port.port_mode,
            "port_watermark": self._config.port.port_watermark,
        }

    def _record_event(
        self,
        trade_id: int | None,
        event_type: EventType,
        raw_text: str,
        action_taken: str,
    ) -> None:
        """Persist a trade_events row. Failures are caught — audit
        logging must never crash the pipeline. ``action_taken`` is
        truncated to keep the audit table compact (D6)."""
        try:
            self._db.record_event(
                trade_id=trade_id,
                event_type=event_type,
                raw_text=raw_text,
                action_taken=_truncate(action_taken) if action_taken else action_taken,
            )
        except Exception:
            logger.exception("Failed to record trade event")

    def _build_decision_snapshot(
        self,
        signal: ParsedSignal,
        preset: StrategyPreset,
        port_state: dict,
        wallet_usd: float,
        position_size_usd: float,
        leverage_applied: int,
    ) -> dict:
        """Capture the inputs that produced this trade — preset, sizing,
        port state, leverage caps — for audit + post-mortem (D3)."""
        risk = signal.risk_level.value
        size_pct = self._config.strategy.size_by_risk.get(risk, preset.size_pct)
        port_usd = port_state["port_usd"] or 0.0
        target_usd = port_usd * size_pct / 100.0

        why_parts = [
            f"size_by_risk[{risk}]={size_pct}% of port ${port_usd:.2f} = ${target_usd:.2f}"
        ]
        if target_usd > position_size_usd:
            why_parts.append(
                f"clamped to max_position_size=${self._config.risk.max_position_size_usd:.2f}"
            )
        if leverage_applied < signal.leverage:
            why_parts.append(
                f"leverage capped from {signal.leverage}x to {leverage_applied}x"
            )

        exposure = (position_size_usd / port_usd * 100.0) if port_usd > 0 else 0.0
        return {
            "preset": self._config.strategy.active_preset,
            "size_pct_applied": size_pct,
            "port_usd_at_open": port_usd,
            "port_mode": port_state["port_mode"],
            "risk_level": risk,
            "leverage_applied": leverage_applied,
            "leverage_signal": signal.leverage,
            "position_size_usd": position_size_usd,
            "exposure_used_pct": exposure,
            "wallet_usd_at_open": wallet_usd,
            "why": "; ".join(why_parts),
        }

    def _apply_pnl_to_port(self, position_size_usd: float, pnl_pct: float) -> None:
        """Apply realized P&L (percent of collateral) to the user's port.

        No-op when there is no user_db wired up. Errors are caught and
        logged — port mis-updates must never crash the pipeline.
        """
        if self._user_db is None:
            return
        pnl_usd = position_size_usd * pnl_pct / 100.0
        try:
            self._user_db.apply_pnl_to_port(self._db.user_id, pnl_usd)
        except Exception:
            logger.exception(
                "Failed to apply port P&L for user %s (pnl_usd=%.2f)",
                self._db.user_id, pnl_usd,
            )

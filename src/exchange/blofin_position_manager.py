"""Blofin trade-lifecycle operations — parallel to ``PositionManager`` (HL).

Same public contract (``submit_trade`` / ``cancel_trade`` / ``close_position``
/ ``move_stop_loss`` / ``move_sl_to_breakeven`` / ``sync_positions``) so the
pipeline can use it polymorphically once dispatch is wired (next step).

Blofin specifics:
  - A trade = 1 regular entry order (``orderId``) + SL + 3 TPs as ``order-tpsl``
    conditionals (``tpslId``). Cancel routes by order type: entry →
    ``cancel-order``, SL/TPs → ``cancel-tpsl``. Both id kinds live in the
    ``orders.oid`` TEXT column (no schema change).
  - ``trade.coin`` is the base symbol (``BTC``) for D10 comparison parity;
    ``orders.coin`` and all API calls use the instId (``BTC-USDT``).
  - Close uses ``close-position`` (true market — no oracle-distance spread to
    tune, HL Bug #12 obviated).
"""

from __future__ import annotations

import logging

from src.exchange.blofin import (
    BlofinClient,
    CANCEL_GONE_CODE,
    first_item,
    order_id_of,
    response_error,
    response_ok,
    tpsl_id_of,
)
from src.exchange.blofin_order_builder import BlofinTpslParams, BlofinTradeOrderSet
from src.exchange.position_manager import OrderSubmissionError
from src.state.database import TradeDatabase
from src.state.models import OrderStatus, OrderType, TradeStatus
from src.utils.symbol_mapper import potion_to_blofin

logger = logging.getLogger(__name__)

_TP_TYPES = (OrderType.TP1, OrderType.TP2, OrderType.TP3)


def _result_error(result: dict) -> str | None:
    """Error message from a place response, or None. Checks the top-level
    code AND the per-item code (Blofin wraps results in data with its own
    code/msg)."""
    if not response_ok(result):
        return response_error(result)
    item = first_item(result.get("data"))
    if item and str(item.get("code", "0")) != "0":
        return item.get("msg") or f"code {item.get('code')}"
    return None


def _cancel_benign(result: dict) -> bool:
    """True if a cancel response means the order is already gone (benign).
    Blofin is NOT idempotent on cancel — ``102068`` = already filled/canceled/
    gone, which for our cleanup purposes is success."""
    if response_ok(result):
        return True
    if str(result.get("code")) == CANCEL_GONE_CODE:
        return True
    item = first_item(result.get("data"))
    return str(item.get("code")) == CANCEL_GONE_CODE


class BlofinPositionManager:
    """Manages a single user's trade lifecycle on Blofin, backed by the DB."""

    def __init__(self, client: BlofinClient, db: TradeDatabase):
        self._client = client
        self._db = db

    # ------------------------------------------------------------------
    # Startup sync — reconcile DB against the THREE real sources
    # ------------------------------------------------------------------
    def sync_positions(self) -> dict[str, list]:
        summary: dict[str, list] = {
            "closed": [], "canceled": [], "verified": [], "orphans": [],
        }
        try:
            positions = self._client.get_open_positions()
            orders = self._client.get_open_orders()
            tpsls = self._client.get_open_tpsl_orders()
        except Exception as e:
            logger.error("Failed to fetch Blofin state for sync: %s", e)
            return summary

        positions_by_inst = {p["inst_id"]: p for p in positions if p.get("inst_id")}
        resting_ids = (
            {str(o["orderId"]) for o in orders if o.get("orderId")}
            | {str(t["tpslId"]) for t in tpsls if t.get("tpslId")}
        )

        local_insts: set[str] = set()
        for trade in self._db.get_open_trades():
            inst = potion_to_blofin(trade.pair)
            local_insts.add(inst)

            if trade.status == TradeStatus.OPEN:
                if inst in positions_by_inst:
                    summary["verified"].append(trade.trade_id)
                else:
                    logger.warning(
                        "Sync: #%d %s was OPEN but no Blofin position — CLOSED",
                        trade.trade_id, inst,
                    )
                    self._db.update_trade_status(
                        trade.trade_id, TradeStatus.CLOSED,
                        close_reason="sync_no_position",
                    )
                    summary["closed"].append(trade.trade_id)

            elif trade.status == TradeStatus.PENDING:
                entry = next(
                    (o for o in self._db.get_orders_for_trade(trade.trade_id)
                     if o.order_type == OrderType.ENTRY), None,
                )
                if entry and entry.oid and str(entry.oid) in resting_ids:
                    summary["verified"].append(trade.trade_id)
                elif inst in positions_by_inst:
                    logger.info("Sync: #%d %s entry filled offline — OPEN", trade.trade_id, inst)
                    self._db.update_trade_status(trade.trade_id, TradeStatus.OPEN)
                    summary["verified"].append(trade.trade_id)
                else:
                    logger.warning(
                        "Sync: #%d %s PENDING but no resting order/position — CANCELED",
                        trade.trade_id, inst,
                    )
                    self._db.update_trade_status(
                        trade.trade_id, TradeStatus.CANCELED,
                        close_reason="sync_no_order",
                    )
                    summary["canceled"].append(trade.trade_id)

        for inst in positions_by_inst:
            if inst not in local_insts:
                summary["orphans"].append(inst)

        logger.info(
            "Blofin sync: %d verified, %d closed, %d canceled, %d orphans",
            len(summary["verified"]), len(summary["closed"]),
            len(summary["canceled"]), len(summary["orphans"]),
        )
        return summary

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------
    def submit_trade(self, trade_set: BlofinTradeOrderSet) -> bool:
        trade_id = trade_set.trade_id
        inst = trade_set.inst_id

        try:
            self._client.set_leverage(inst, trade_set.leverage, trade_set.margin_mode)
            logger.info("Set leverage: %s %dx %s", inst, trade_set.leverage, trade_set.margin_mode)
        except Exception as e:
            # Blofin refuses a leverage change while the instrument already has
            # an open position / pending orders ("You have pending cross orders.
            # Please cancel them before adjusting your leverage."). That happens
            # when a prior trade on the same coin is still live (or the user
            # holds a pre-existing position). Aborting the whole trade would
            # mean silently dropping the signal — worse than trading at the
            # leverage currently set on the instrument. So we log loudly (the
            # leverage may differ from the signal — captured for the audit) and
            # proceed. The 2026-06-05 demo soak surfaced this on overlapping
            # same-coin trades. Other set-leverage failures fall into the same
            # best-effort path; if the venue is truly unusable the entry below
            # will fail and raise its own OrderSubmissionError.
            logger.warning(
                "Could not set leverage for %s to %dx (%s) — proceeding at the "
                "leverage currently set on the instrument",
                inst, trade_set.leverage, e,
            )

        # --- Entry (regular order) ---
        e = trade_set.entry
        entry_row = self._db.record_order(
            trade_id, OrderType.ENTRY, inst, e.side, e.size, e.price or 0.0,
        )
        result = self._client.place_order(
            inst_id=inst, side=e.side, order_type=e.order_type, size=e.size,
            price=e.price, margin_mode=e.margin_mode, position_side=e.position_side,
            client_order_id=f"potion_{trade_id}_entry",
        )
        err = _result_error(result)
        if err:
            logger.error("Entry order rejected for #%d: %s", trade_id, err)
            raise OrderSubmissionError(f"Entry order rejected: {err}")
        oid = order_id_of(result)
        if oid:
            self._db.set_order_oid(entry_row, oid)
        logger.info("Entry placed: #%d %s orderId=%s", trade_id, inst, oid)

        # --- SL + TP conditionals (order-tpsl) ---
        self._submit_tpsl(trade_id, OrderType.STOP_LOSS, trade_set.stop_loss)
        for tp_type, tp in zip(_TP_TYPES, trade_set.take_profits):
            if tp.size > 0:
                self._submit_tpsl(trade_id, tp_type, tp)

        return True

    def _submit_tpsl(self, trade_id: int, order_type: OrderType, params: BlofinTpslParams) -> str | None:
        """Place one TP/SL conditional and record it. SL/TP rejection is
        logged but does not abort the trade (mirrors HL)."""
        price = params.tp_trigger_price if params.tp_trigger_price is not None else params.sl_trigger_price
        row = self._db.record_order(
            trade_id, order_type, params.inst_id, params.side, params.size, price or 0.0,
        )
        result = self._client.place_tpsl(
            inst_id=params.inst_id, side=params.side, size=params.size,
            tp_trigger_price=params.tp_trigger_price,
            sl_trigger_price=params.sl_trigger_price,
            margin_mode=params.margin_mode, position_side=params.position_side,
            reduce_only=params.reduce_only,
            client_order_id=f"potion_{trade_id}_{order_type.value}",
        )
        err = _result_error(result)
        if err:
            logger.error("TPSL %s rejected for #%d: %s", order_type.value, trade_id, err)
            self._db.set_order_status_by_row(row, OrderStatus.REJECTED)
            return None
        tid = tpsl_id_of(result)
        if tid:
            self._db.set_order_oid(row, tid)
        return tid

    # ------------------------------------------------------------------
    # Cancellation / close
    # ------------------------------------------------------------------
    def cancel_trade(self, trade_id: int) -> None:
        for order in self._db.get_orders_for_trade(trade_id):
            if order.status != OrderStatus.SUBMITTED or not order.oid:
                continue
            try:
                if order.order_type == OrderType.ENTRY:
                    res = self._client.cancel_order(order.coin, order_id=order.oid)
                else:
                    res = self._client.cancel_tpsl(order.coin, order.oid)
                if response_ok(res) or _cancel_benign(res):
                    self._db.update_order_status(order.oid, OrderStatus.CANCELED)
                    logger.info("Canceled %s oid=%s for #%d", order.order_type.value, order.oid, trade_id)
                else:
                    logger.error("Cancel failed oid=%s #%d: %s", order.oid, trade_id, response_error(res))
            except Exception as e:
                logger.error("Cancel error oid=%s: %s", order.oid, e)
        self._db.update_trade_status(trade_id, TradeStatus.CANCELED, close_reason="canceled")

    def close_position(self, trade_id: int, coin: str, reason: str = "manual") -> None:
        """Cancel resting orders/tpsl, then market-close any open position.

        Verifies Blofin accepted the close before marking CLOSED (Bug #13
        parity); raises ``OrderSubmissionError`` otherwise so the caller
        surfaces the truth."""
        trade = self._db.get_trade(trade_id)
        inst = self._inst_id(trade, coin)

        self.cancel_trade(trade_id)

        pos = next(
            (p for p in self._client.get_open_positions() if p.get("inst_id") == inst),
            None,
        )
        if not pos or float(pos.get("size", 0) or 0) == 0:
            self._db.update_trade_status(trade_id, TradeStatus.CLOSED, close_reason=reason)
            return

        result = self._client.close_position(
            inst, margin_mode=pos.get("margin_mode") or "cross",
        )
        if not response_ok(result):
            logger.error("Close rejected for %s #%d: %s", inst, trade_id, response_error(result))
            raise OrderSubmissionError(f"Close rejected: {response_error(result)}")

        logger.info("Closed position %s (#%d)", inst, trade_id)
        self._db.update_trade_status(trade_id, TradeStatus.CLOSED, close_reason=reason)

    def move_stop_loss(self, trade_id: int, coin: str, new_price: float) -> bool:
        """Move the SL conditional to *new_price* without ever leaving the
        position unprotected.

        Two things burned us on ADA #2262 (2026-06-19): a *duplicate* breakeven
        message re-moved an already-at-BE SL, and by then the market had crossed
        the level so the replacement was rejected — the old cancel-first ordering
        had already removed the existing SL, leaving the position with NO stop
        (and the re-instate, using the same now-invalid price, also failed).

        Guards:
          1. Idempotent — if the active SL is already at *new_price*, do nothing.
          2. Place the NEW SL first; only cancel the old one once the new is
             accepted. A rejected replacement leaves the existing SL untouched.
             During the brief overlap both SLs are reduce-only, so a trigger can
             never over-close the position.
        """
        sl = next(
            (o for o in self._db.get_orders_for_trade(trade_id)
             if o.order_type == OrderType.STOP_LOSS and o.status == OrderStatus.SUBMITTED),
            None,
        )
        if not sl or not sl.oid:
            logger.warning("No active SL conditional for #%d to move", trade_id)
            return False
        inst = sl.coin

        # (1) Idempotent — already at target → no-op. Kills the destructive
        #     re-move a duplicate breakeven message would otherwise trigger.
        if sl.price is not None and abs(sl.price - new_price) <= abs(new_price) * 1e-6:
            logger.info("SL for #%d already at %s — no move needed", trade_id, new_price)
            return True

        # (2) Fast skip if the new level is plainly on the wrong side of market
        #     (saves a doomed place + a rejected row). Best-effort.
        if not self._sl_price_valid(inst, sl.side, new_price):
            logger.warning(
                "SL move to %s for #%d skipped — Blofin would reject it (price "
                "already past that level for a %s-close); keeping existing SL",
                new_price, trade_id, sl.side,
            )
            return False

        # (3) Place the NEW SL FIRST. If rejected, the old SL is still live —
        #     the position is never left uncovered.
        params = BlofinTpslParams(
            inst_id=inst, side=sl.side, size=sl.size, sl_trigger_price=new_price,
        )
        new_tid = self._submit_tpsl(trade_id, OrderType.STOP_LOSS, params)
        if new_tid is None:
            logger.warning(
                "SL move to %s for #%d rejected — keeping existing SL @ %s "
                "(position still protected)", new_price, trade_id, sl.price,
            )
            return False

        # (4) New SL is live → cancel the old. If the cancel fails we briefly
        #     hold two reduce-only SLs (safe — neither can over-close); log it.
        try:
            res = self._client.cancel_tpsl(inst, sl.oid)
            if response_ok(res) or _cancel_benign(res):
                self._db.update_order_status(sl.oid, OrderStatus.CANCELED)
            else:
                logger.error(
                    "New SL %s placed for #%d but cancel of old SL %s failed: %s "
                    "(two reduce-only SLs live)",
                    new_tid, trade_id, sl.oid, response_error(res),
                )
        except Exception as e:
            logger.error(
                "New SL %s placed for #%d but error canceling old SL %s: %s "
                "(two reduce-only SLs live)", new_tid, trade_id, sl.oid, e,
            )

        logger.info("Moved SL to %s for #%d (tpslId=%s)", new_price, trade_id, new_tid)
        return True

    def _sl_price_valid(self, inst: str, side: str, new_price: float) -> bool:
        """True if an SL at *new_price* is on the valid side of the current
        market for a *side*-close (sell = long position → SL below market;
        buy = short → SL above). Best-effort: if the price can't be read, don't
        block the move (the re-instate guard in move_stop_loss is the backstop)."""
        try:
            current = self._client.get_all_mids().get(inst)
        except Exception:
            return True
        if not current or current <= 0:
            return True
        return new_price < current if side == "sell" else new_price > current

    def move_sl_to_breakeven(self, trade_id: int, coin: str, entry_price: float) -> bool:
        return self.move_stop_loss(trade_id, coin, entry_price)

    # ------------------------------------------------------------------
    def _inst_id(self, trade, coin: str | None = None) -> str:
        """Resolve the Blofin instId for API calls. Prefer the trade's pair
        (always present); fall back to the passed coin."""
        if trade is not None:
            return potion_to_blofin(trade.pair)
        if coin and "-" in coin:
            return coin
        return f"{coin}-USDT"

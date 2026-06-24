"""Order construction for Blofin, from a parsed CP signal.

Parallel to ``order_builder.py`` (Hyperliquid) but for Blofin's contract
model and native TP/SL. A CP signal maps to 5 orders, mirroring the HL
structure but on Blofin's endpoints (all demo-confirmed — see
``docs/BLOFIN_INTEGRATION.md`` § 0):

  - Entry   → ``POST /api/v1/trade/order`` (limit)
  - SL      → ``POST /api/v1/trade/order-tpsl`` (slTriggerPrice, full size)
  - TP1/2/3 → ``POST /api/v1/trade/order-tpsl`` (tpTriggerPrice, split size)

Blofin specifics this handles:
  - **Contract-value sizing**: 1 contract ≠ 1 unit. ``size_in_contracts =
    position_size_usd / (entry_price * contractValue)`` (BTC contractValue
    0.001). Forgetting the contractValue factor is a new failure mode — the
    BTC sentinel test pins it.
  - **lotSize / minSize** in contracts (replaces HL's per-asset szDecimals +
    global $10 min).
  - **tickSize** price rounding (replaces HL's empirical 5-sig-fig/6-decimal
    rule — Bug #18 goes away; Blofin publishes tickSize per instrument).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from src.exchange.errors import InstrumentNotAvailableError
from src.parser.signal_parser import ParsedSignal, Side
from src.utils.symbol_mapper import potion_to_blofin

logger = logging.getLogger(__name__)

# Market execution price for a TP/SL trigger (order-tpsl) — Blofin convention.
TPSL_MARKET_PRICE = "-1"


def _decimals(step_str: str) -> int:
    """Decimal places implied by a step string like "0.1" or "0.001"."""
    step_str = step_str.strip()
    if "." in step_str:
        return len(step_str.rstrip("0").split(".")[1])
    return 0


def _floor_to_step(value: float, step: float, decimals: int) -> float:
    """Floor ``value`` down to a multiple of ``step`` (e.g. lotSize)."""
    if step <= 0:
        return round(value, decimals)
    # +epsilon guards against fp dust making an exact multiple floor early
    n = math.floor(value / step + 1e-9)
    return round(n * step, decimals)


def _round_to_tick(price: float, tick: float, decimals: int) -> float:
    """Round ``price`` to the nearest ``tick`` multiple (e.g. tickSize)."""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, decimals)


@dataclass
class BlofinOrderParams:
    """A regular Blofin order (entry) for ``POST /api/v1/trade/order``."""

    inst_id: str
    side: str              # "buy" | "sell"
    order_type: str        # "limit" | "market"
    size: float            # in contracts
    price: float | None    # required for limit
    reduce_only: bool = False
    margin_mode: str = "cross"
    position_side: str = "net"


@dataclass
class BlofinTpslParams:
    """A TP or SL conditional for ``POST /api/v1/trade/order-tpsl``.

    Exactly one of ``tp_trigger_price`` / ``sl_trigger_price`` is set in our
    usage (TPs are tp-only, the stop is sl-only), both demo-confirmed as
    accepted. Trigger execution is market (``order_price = -1``).
    """

    inst_id: str
    side: str              # close side: "sell" to close a long, "buy" to close a short
    size: float            # in contracts (partial for TP splits)
    tp_trigger_price: float | None = None
    sl_trigger_price: float | None = None
    reduce_only: bool = True
    margin_mode: str = "cross"
    position_side: str = "net"


@dataclass
class BlofinTradeOrderSet:
    """Complete set of Blofin orders for one CP signal."""

    inst_id: str
    coin: str
    trade_id: int
    leverage: int
    margin_mode: str
    entry: BlofinOrderParams
    stop_loss: BlofinTpslParams
    take_profits: list[BlofinTpslParams] = field(default_factory=list)


def build_blofin_orders(
    signal: ParsedSignal,
    position_size_usd: float,
    instruments_meta: dict[str, Any],
    tp_split: list[float] | None = None,
    max_leverage: int | None = None,
) -> BlofinTradeOrderSet:
    """Convert a parsed signal into a complete Blofin order set.

    Args:
        signal: Parsed CP signal.
        position_size_usd: Total USD notional for the position.
        instruments_meta: ``BlofinClient.get_asset_meta()`` output, keyed by
            instId. Each value must carry ``contractValue``, ``lotSize``,
            ``minSize``, ``tickSize``, ``maxLeverage``.
        tp_split: Fraction of the position to close at each TP. Sums to 1.0.
            Defaults to ``[0.33, 0.33, 0.34]``.
        max_leverage: User cap; effective leverage is
            ``min(signal, user cap, instrument maxLeverage)``.

    Raises:
        ValueError: instrument missing, size below ``minSize``, bad tp_split.
    """
    if tp_split is None:
        tp_split = [0.33, 0.33, 0.34]
    if len(tp_split) != 3 or abs(sum(tp_split) - 1.0) > 0.01:
        raise ValueError(f"tp_split must have 3 values summing to 1.0, got {tp_split}")

    inst_id = potion_to_blofin(signal.pair)
    if inst_id not in instruments_meta:
        raise InstrumentNotAvailableError(
            f"Instrument '{inst_id}' not found in Blofin metadata"
        )

    meta = instruments_meta[inst_id]
    contract_value = float(meta["contractValue"])
    lot_size = float(meta["lotSize"])
    min_size = float(meta["minSize"])
    tick_size = float(meta["tickSize"])
    lot_decimals = _decimals(str(meta["lotSize"]))
    tick_decimals = _decimals(str(meta["tickSize"]))

    is_long = signal.side == Side.LONG
    entry_side = "buy" if is_long else "sell"
    close_side = "sell" if is_long else "buy"

    # Effective leverage = min(signal, user cap, instrument cap)
    exchange_max_lev = int(float(meta.get("maxLeverage", signal.leverage)))
    leverage = signal.leverage
    if max_leverage:
        leverage = min(leverage, max_leverage)
    leverage = min(leverage, exchange_max_lev)

    # --- Contract-value sizing ---
    # contracts = USD / (price * contractValue). The contractValue factor is
    # the Blofin-specific bit; the BTC sentinel test pins it.
    raw_contracts = position_size_usd / (signal.entry * contract_value)
    contracts = _floor_to_step(raw_contracts, lot_size, lot_decimals)
    if contracts < min_size:
        raise ValueError(
            f"Size {contracts} contracts ({inst_id}) is below Blofin minSize "
            f"{min_size} for ${position_size_usd:.2f} at ${signal.entry}"
        )

    entry_px = _round_to_tick(signal.entry, tick_size, tick_decimals)
    sl_px = _round_to_tick(signal.stop_loss, tick_size, tick_decimals)
    tp_prices = [
        _round_to_tick(signal.tp1, tick_size, tick_decimals),
        _round_to_tick(signal.tp2, tick_size, tick_decimals),
        _round_to_tick(signal.tp3, tick_size, tick_decimals),
    ]

    entry = BlofinOrderParams(
        inst_id=inst_id, side=entry_side, order_type="limit",
        size=contracts, price=entry_px, reduce_only=False,
    )

    # SL covers the full size (reduce-only conditional)
    stop_loss = BlofinTpslParams(
        inst_id=inst_id, side=close_side, size=contracts,
        sl_trigger_price=sl_px,
    )

    # TP splits — last TP gets the remainder so sizes sum exactly to entry
    take_profits: list[BlofinTpslParams] = []
    allocated = 0.0
    for i, (tp_price, fraction) in enumerate(zip(tp_prices, tp_split)):
        if i < len(tp_split) - 1:
            tp_size = _floor_to_step(contracts * fraction, lot_size, lot_decimals)
            allocated = round(allocated + tp_size, lot_decimals)
        else:
            tp_size = round(contracts - allocated, lot_decimals)
        take_profits.append(
            BlofinTpslParams(
                inst_id=inst_id, side=close_side, size=tp_size,
                tp_trigger_price=tp_price,
            )
        )

    logger.info(
        "Built Blofin order set: %s #%d %s %s %s contracts @ %s "
        "(lev=%dx, SL=%s, TP=[%s, %s, %s])",
        inst_id, signal.trade_id, signal.side.value, entry_side, contracts,
        entry_px, leverage, sl_px, tp_prices[0], tp_prices[1], tp_prices[2],
    )

    return BlofinTradeOrderSet(
        inst_id=inst_id, coin=inst_id.split("-")[0], trade_id=signal.trade_id,
        leverage=leverage, margin_mode="cross",
        entry=entry, stop_loss=stop_loss, take_profits=take_profits,
    )

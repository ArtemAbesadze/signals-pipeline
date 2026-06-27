"""Exchange adapter — the single seam that makes the pipeline exchange-agnostic.

Phase 6.8. ``Pipeline`` and ``Orchestrator`` are otherwise identical for HL
and Blofin; the only differences are *which* order builder, position manager,
balance field, symbol map, and minimum-size handling to use. This module
encapsulates exactly those divergences behind one small interface so the core
pipeline grows no ``if exchange == 'blofin'`` branches.

Design constraints:
  - The HL path must stay byte-for-byte identical to pre-6.8 behaviour (HL is
    the permanent fallback adapter — both exchanges coexist). ``build_trade_set``
    keeps the historical ``max(size, 10.0)`` HL minimum-notional bump; Blofin
    has no such bump (its builder enforces the instrument ``minSize`` directly).
  - ``trade_set.coin`` and ``trade_set.leverage`` exist on BOTH order sets, so
    the pipeline (and the notifier, which only reads ``.leverage``) uses them
    natively. The one field that diverges — entry size — goes through
    ``entry_size()`` (``.entry.sz`` on HL, ``.entry.size`` on Blofin).
  - Position managers already share a method contract
    (``submit_trade`` / ``cancel_trade`` / ``close_position`` /
    ``move_stop_loss`` / ``move_sl_to_breakeven`` / ``sync_positions``), so the
    adapter just hands back the right one.

D11 fill read-back (reading real executed fill prices from the exchange) is
deliberately NOT here yet — it lands as a focused follow-up once a real
triggered-conditional fill shape is captured on the Blofin demo. Until then
both exchanges keep CP's target-price approximation in ``_mark_order_filled``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from src.config.settings import StrategyPreset
from src.exchange.blofin import BlofinClient
from src.exchange.blofin_order_builder import build_blofin_orders
from src.exchange.blofin_position_manager import BlofinPositionManager
from src.exchange.hyperliquid import HyperliquidClient
from src.exchange.order_builder import build_orders
from src.exchange.position_manager import PositionManager
from src.parser.signal_parser import ParsedSignal
from src.state.database import TradeDatabase
from src.utils.symbol_mapper import potion_to_blofin, potion_to_hyperliquid

logger = logging.getLogger(__name__)

# Historical HL minimum-notional bump (see ``Pipeline._handle_signal`` pre-6.8).
# HL rejects sub-$10 orders and loses notional to ``szDecimals`` flooring, so
# the pipeline always sized at least $10 of notional into the HL builder.
_HL_MIN_NOTIONAL_USD = 10.0


class ExchangeAdapter(ABC):
    """Per-user, per-exchange bundle of the operations the pipeline needs.

    One instance wraps one client + DB. Built once at pipeline construction
    (and again, cheaply, by the orchestrator for startup sync / kill-switch).
    """

    name: str

    def __init__(self, client, db: TradeDatabase):
        self._client = client
        self._db = db
        self.position_manager = self._build_position_manager()
        # Cached at construction — same as the pre-6.8 ``Pipeline.__init__``,
        # which did ``self._asset_meta = client.get_asset_meta()`` once.
        self.asset_meta = client.get_asset_meta()

    @abstractmethod
    def _build_position_manager(self):
        ...

    @abstractmethod
    def build_trade_set(
        self,
        signal: ParsedSignal,
        position_size_usd: float,
        preset: StrategyPreset,
        max_leverage: int | None,
    ):
        """Build the exchange-native order set for *signal*. May raise the
        builder's own ``ValueError`` / ``KeyError`` — the pipeline catches
        those exactly as before."""
        ...

    @abstractmethod
    def entry_size(self, trade_set) -> float:
        """Entry size in the exchange's native unit (HL coins / Blofin
        contracts). Bridges ``.entry.sz`` vs ``.entry.size``."""
        ...

    @abstractmethod
    def wallet_balance_usd(self) -> float:
        """Spendable account balance in USD, for the port-vs-wallet guardrail."""
        ...

    @abstractmethod
    def map_symbol(self, pair: str) -> str:
        """Map a CP ``BASE/QUOTE`` pair to the exchange's coin/instId — the
        fallback used when an order set could not be built (auto_execute=off)."""
        ...

    def real_fill_price(self, coin: str, trade_id: int, order_type) -> float | None:
        """The exchange's REAL executed fill price for our order, or None.

        D11 (Phase 6.8b): a CP lifecycle event tells us an order *should* have
        filled, but CP doesn't carry the real fill price. When the exchange
        exposes executed-order history we read the true ``averagePrice`` and
        persist that; otherwise the caller falls back to the CP target price
        and marks the row as an estimate — never silently mixed.

        Default (Hyperliquid): None — HL has no equivalent endpoint, so the HL
        path keeps the CP target-price approximation it always used."""
        return None

    def realized_pnl(self, inst_id: str, trade_id: int) -> float | None:
        """The exchange's REAL realized PnL (quote ccy, USD) for this trade, or
        None.

        D11 6.8b-2: CP reports a leveraged % at the *signal* leverage, which
        diverges from reality (e.g. when leverage was capped). When the exchange
        exposes per-order realized PnL we sum it for the trade; otherwise the
        caller keeps CP's number and marks it an estimate.

        Default (Hyperliquid): None — no endpoint, HL keeps CP's pnl_pct."""
        return None


class HyperliquidAdapter(ExchangeAdapter):
    name = "hyperliquid"

    def _build_position_manager(self):
        return PositionManager(self._client, self._db)

    def build_trade_set(self, signal, position_size_usd, preset, max_leverage):
        return build_orders(
            signal,
            max(position_size_usd, _HL_MIN_NOTIONAL_USD),
            self.asset_meta,
            tp_split=preset.tp_split,
            max_leverage=max_leverage,
        )

    def entry_size(self, trade_set) -> float:
        return trade_set.entry.sz

    def wallet_balance_usd(self) -> float:
        return float(self._client.get_balance()["usdc_balance"])

    def map_symbol(self, pair: str) -> str:
        return potion_to_hyperliquid(pair)


class BlofinAdapter(ExchangeAdapter):
    name = "blofin"

    def _build_position_manager(self):
        return BlofinPositionManager(self._client, self._db)

    def build_trade_set(self, signal, position_size_usd, preset, max_leverage):
        # No $10 bump: the Blofin builder enforces the instrument ``minSize``
        # itself and contract-value math doesn't lose notional to flooring the
        # way HL's szDecimals did (Bug #18 class obviated).
        return build_blofin_orders(
            signal,
            position_size_usd,
            self.asset_meta,
            tp_split=preset.tp_split,
            max_leverage=max_leverage,
        )

    def entry_size(self, trade_set) -> float:
        return trade_set.entry.size

    def wallet_balance_usd(self) -> float:
        # Futures account ``available`` (USDT) is the buying power; total
        # equity includes unrealized PnL of open positions.
        return float(self._client.get_balance()["available"])

    def map_symbol(self, pair: str) -> str:
        return potion_to_blofin(pair)

    def real_fill_price(self, coin: str, trade_id: int, order_type) -> float | None:
        """Look up the real executed fill price from Blofin's orders-history,
        joined to our order by the ``potion_{trade_id}_{type}`` label we set at
        submit time. Entries carry it as ``clientOrderId``; triggered TP/SL
        executions carry it as ``algoClientOrderId`` (their execution orderId
        differs from the tpslId). Returns None on no-match or query failure so
        the caller falls back to the CP estimate. ``coin`` is the instId."""
        target = f"potion_{trade_id}_{order_type.value}"
        try:
            rows = self._client.get_orders_history(coin)
        except Exception:
            logger.exception(
                "orders-history query failed for #%s %s; using CP estimate",
                trade_id, coin,
            )
            return None
        for row in rows:
            if str(row.get("state")) != "filled":
                continue
            coid = row.get("clientOrderId") or row.get("algoClientOrderId") or ""
            # Exact, or a suffixed variant — a moved SL is placed as
            # ``potion_{id}_stop_loss_{n}`` (unique-clientOrderId fix), so its
            # real fill price still resolves here.
            if coid != target and not coid.startswith(target + "_"):
                continue
            try:
                px = float(row.get("averagePrice"))
            except (TypeError, ValueError):
                return None
            return px if px > 0 else None
        return None

    def realized_pnl(self, inst_id: str, trade_id: int) -> float | None:
        """Sum Blofin's per-order realized ``pnl`` over every executed order
        belonging to this trade (matched by our ``potion_{trade_id}_`` label on
        ``clientOrderId`` / ``algoClientOrderId``). The entry contributes 0; TP
        and SL fills contribute the real gain/loss. Returns None if no matching
        executed order exists (e.g. the close happened off-exchange or hasn't
        landed yet) or on query failure, so the caller falls back to CP."""
        prefix = f"potion_{trade_id}_"
        try:
            rows = self._client.get_orders_history(inst_id)
        except Exception:
            logger.exception(
                "orders-history query failed for #%s %s realized-PnL; using CP",
                trade_id, inst_id,
            )
            return None
        total = 0.0
        matched = False
        for row in rows:
            if str(row.get("state")) != "filled":
                continue
            coid = row.get("clientOrderId") or row.get("algoClientOrderId") or ""
            if not coid.startswith(prefix):
                continue
            try:
                total += float(row.get("pnl") or 0.0)
                matched = True
            except (TypeError, ValueError):
                continue
        return total if matched else None


_ADAPTERS: dict[str, type[ExchangeAdapter]] = {
    "hyperliquid": HyperliquidAdapter,
    "blofin": BlofinAdapter,
}


def build_adapter(exchange: str | None, client, db: TradeDatabase) -> ExchangeAdapter:
    """Construct the adapter for *exchange* (defaulting to Hyperliquid).

    The client must already be the matching kind — ``build_exchange_client``
    in the orchestrator is responsible for that pairing. Raises ``ValueError``
    on an unknown exchange rather than silently defaulting.
    """
    key = (exchange or "hyperliquid").lower()
    adapter_cls = _ADAPTERS.get(key)
    if adapter_cls is None:
        raise ValueError(f"Unknown exchange '{exchange}'")
    return adapter_cls(client, db)


def build_position_manager(exchange: str | None, client, db: TradeDatabase):
    """Just the position manager for *exchange* — for the orchestrator's
    startup sync and kill-switch, which don't need the full adapter (no order
    building / balance reads). Avoids an extra ``get_asset_meta`` round-trip
    that ``build_adapter`` would incur.
    """
    key = (exchange or "hyperliquid").lower()
    if key == "hyperliquid":
        return PositionManager(client, db)
    if key == "blofin":
        return BlofinPositionManager(client, db)
    raise ValueError(f"Unknown exchange '{exchange}'")

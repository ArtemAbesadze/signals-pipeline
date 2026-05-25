"""Position size calculator and risk gate.

Determines how much USD to allocate per trade based on:
  1. The user's configured port (a subset of their wallet — see D1)
  2. Strategy preset (size_pct)
  3. Risk level override (size_by_risk)
  4. Risk limits (max_position_size_usd, min_order_usd)

Also provides check_risk_limits() as a pre-trade gate that enforces:
  - Max open positions
  - Daily loss circuit breaker
  - Total exposure cap
"""

import logging

from src.config.settings import RiskConfig, StrategyConfig, StrategyPreset

logger = logging.getLogger(__name__)


class PositionSizeError(Exception):
    """Raised when a valid position size cannot be calculated."""


class RiskLimitBreached(Exception):
    """Raised when a risk guardrail blocks a new trade."""


def calculate_position_size(
    port_usd: float,
    risk_level: str,
    preset: StrategyPreset,
    strategy_config: StrategyConfig,
    risk_config: RiskConfig,
    network: str = "mainnet",
) -> float:
    """Calculate the USD position size for a trade.

    Sizing is a percentage of the user's port (their configured trading
    capital subset — D1), not their wallet balance.

    Resolution order for size_pct:
      1. size_by_risk[risk_level] if the risk level exists in the map
      2. preset.size_pct (the preset's default)

    The result is then clamped to risk limits. On testnet, if the
    calculated size would fall below the exchange minimum, the position
    is bumped UP to ``risk_config.testnet_position_floor_usd`` (Phase
    4.1) rather than skipped — lets us exercise the full pipeline
    without funding the wallet to the level the risk-level math would
    demand. Mainnet path is unchanged.

    Args:
        port_usd: User's currently allocated trading capital in USD.
        risk_level: Signal risk level ("LOW", "MEDIUM", "HIGH").
        preset: The active strategy preset.
        strategy_config: Strategy config (contains size_by_risk overrides).
        risk_config: Risk limits (max/min position sizes + testnet floor).
        network: Exchange network ("testnet" or "mainnet"). Default
            mainnet so callers who don't specify get the conservative
            "raise on below-min" behaviour.

    Returns:
        Position size in USD, ready to pass to build_orders().

    Raises:
        PositionSizeError: On mainnet only, if the calculated size is
            below the exchange minimum.
    """
    # Resolve size percentage
    size_pct = strategy_config.size_by_risk.get(risk_level, preset.size_pct)

    # Calculate raw USD size from port (not wallet — see D1)
    raw_size = port_usd * (size_pct / 100.0)

    # Clamp to max position size
    clamped_size = min(raw_size, risk_config.max_position_size_usd)

    # Testnet floor: bump up to ``testnet_position_floor_usd`` whenever the
    # calculated size is below that floor — not just below ``min_order_usd``.
    # That covers two cases at once:
    #   1. ``calc < min_order_usd`` — HL would reject outright (e.g. HIGH risk
    #      at small port).
    #   2. ``min_order_usd <= calc < floor`` — sizer passes, but after
    #      ``szDecimals`` flooring in order_builder the notional can drop
    #      back below HL's $10 min (e.g. MEDIUM risk on BTC: $10 → 0.00012
    #      BTC at $77K → notional $9.25 → rejected). The floor of $15 leaves
    #      enough headroom that szDecimals losses don't cross the boundary.
    if network == "testnet" and risk_config.testnet_position_floor_usd > 0:
        if clamped_size < risk_config.testnet_position_floor_usd:
            floored = min(
                risk_config.testnet_position_floor_usd,
                risk_config.max_position_size_usd,
            )
            logger.info(
                "Position size: $%.2f (%.1f%% of port $%.2f, risk=%s) bumped "
                "to testnet floor $%.2f",
                clamped_size, size_pct, port_usd, risk_level, floored,
            )
            return floored
        # calc already clears the floor — fall through, no bump, no mainnet
        # check. On testnet ``min_order_usd`` is never the controlling rule.
    elif clamped_size < risk_config.min_order_usd:
        # Mainnet (or testnet with floor disabled): raise so we never
        # silently trade larger than the user's risk math intended.
        raise PositionSizeError(
            f"Position size ${clamped_size:.2f} ({size_pct}% of port ${port_usd:.2f}) "
            f"is below minimum ${risk_config.min_order_usd:.2f}"
        )

    logger.info(
        "Position size: $%.2f (%.1f%% of port $%.2f, risk=%s, clamped to max=$%.2f)",
        clamped_size,
        size_pct,
        port_usd,
        risk_level,
        risk_config.max_position_size_usd,
    )

    return clamped_size


def check_risk_limits(
    risk_config: RiskConfig,
    open_trade_count: int,
    daily_pnl_pct: float,
    total_exposure_usd: float,
    new_position_usd: float,
) -> None:
    """Pre-trade risk gate — raises RiskLimitBreached if any limit is hit.

    Call this before opening any new trade. All checks are independent;
    the first breach raises immediately.

    Args:
        risk_config: Risk limits from config.
        open_trade_count: Current number of open/pending trades.
        daily_pnl_pct: Sum of pnl_pct for trades closed today (negative = losses).
        total_exposure_usd: Current total USD across all open/pending positions.
        new_position_usd: The proposed new position size in USD.

    Raises:
        RiskLimitBreached: With a human-readable reason.
    """
    # 1. Max open positions
    if open_trade_count >= risk_config.max_open_positions:
        raise RiskLimitBreached(
            f"Max open positions reached ({open_trade_count}/{risk_config.max_open_positions})"
        )

    # 2. Daily loss circuit breaker
    if daily_pnl_pct <= -risk_config.max_daily_loss_pct:
        raise RiskLimitBreached(
            f"Daily loss limit breached ({daily_pnl_pct:.1f}% vs "
            f"-{risk_config.max_daily_loss_pct:.1f}% max)"
        )

    # 3. Total exposure cap
    projected_exposure = total_exposure_usd + new_position_usd
    if projected_exposure > risk_config.max_total_exposure_usd:
        raise RiskLimitBreached(
            f"Total exposure would be ${projected_exposure:.2f} "
            f"(${total_exposure_usd:.2f} existing + ${new_position_usd:.2f} new), "
            f"exceeds max ${risk_config.max_total_exposure_usd:.2f}"
        )

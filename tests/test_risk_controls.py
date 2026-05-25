"""Tests for risk controls — position sizing and risk gate."""

import pytest

from src.config.settings import RiskConfig, StrategyConfig, StrategyPreset
from src.strategy.position_sizer import (
    PositionSizeError,
    RiskLimitBreached,
    calculate_position_size,
    check_risk_limits,
)


# ------------------------------------------------------------------
# Position sizing
# ------------------------------------------------------------------

class TestPositionSizing:

    def _default_preset(self, size_pct=2.0):
        return StrategyPreset(size_pct=size_pct)

    def _default_strategy(self, **overrides):
        kwargs = {"size_by_risk": {"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0}}
        kwargs.update(overrides)
        return StrategyConfig(**kwargs)

    def _default_risk(self, **overrides):
        kwargs = {"max_position_size_usd": 500.0, "min_order_usd": 10.0}
        kwargs.update(overrides)
        return RiskConfig(**kwargs)

    def test_basic_sizing(self):
        size = calculate_position_size(
            1000.0, "MEDIUM", self._default_preset(),
            self._default_strategy(), self._default_risk(),
        )
        assert size == 20.0  # 2% of 1000

    def test_risk_level_override(self):
        size = calculate_position_size(
            1000.0, "LOW", self._default_preset(),
            self._default_strategy(), self._default_risk(),
        )
        assert size == 40.0  # 4% of 1000 (LOW override)

    def test_clamped_to_max(self):
        size = calculate_position_size(
            100_000.0, "LOW", self._default_preset(),
            self._default_strategy(), self._default_risk(max_position_size_usd=500.0),
        )
        assert size == 500.0

    def test_below_minimum_raises(self):
        with pytest.raises(PositionSizeError, match="below minimum"):
            calculate_position_size(
                100.0, "HIGH", self._default_preset(),
                self._default_strategy(), self._default_risk(min_order_usd=10.0),
            )
            # 1% of 100 = $1, which is below $10

    def test_preset_size_pct_used_when_no_risk_override(self):
        strategy = StrategyConfig(size_by_risk={})  # no overrides
        size = calculate_position_size(
            1000.0, "MEDIUM", self._default_preset(size_pct=3.0),
            strategy, self._default_risk(),
        )
        assert size == 30.0  # 3% of 1000


# ------------------------------------------------------------------
# Testnet position floor (Phase 4.1)
# ------------------------------------------------------------------

class TestTestnetPositionFloor:
    """Testnet-only: bump sub-minimum sizes UP to ``testnet_position_floor_usd``
    rather than skipping the trade. Mainnet path stays the same — small
    sizes raise so we don't silently trade larger than the user configured."""

    def _preset(self):
        return StrategyPreset(size_pct=2.0)

    def _strategy(self):
        return StrategyConfig(size_by_risk={"LOW": 4.0, "MEDIUM": 2.0, "HIGH": 1.0})

    def _risk(self, **overrides):
        kwargs = {
            "max_position_size_usd": 500.0,
            "min_order_usd": 10.0,
            "testnet_position_floor_usd": 15.0,
        }
        kwargs.update(overrides)
        return RiskConfig(**kwargs)

    def test_testnet_bumps_below_min_up_to_floor(self):
        """1% of $500 = $5, below $10 min — bumps to floor $15."""
        size = calculate_position_size(
            500.0, "HIGH", self._preset(), self._strategy(), self._risk(),
            network="testnet",
        )
        assert size == 15.0

    def test_testnet_bumps_above_min_but_below_floor(self):
        """The gap case: 2% of $500 = $10 clears HL's min on its own, but
        after ``szDecimals`` flooring in order_builder, a BTC trade at this
        size produces a $9.25 notional and gets rejected. Floor must apply
        whenever calc is below the floor, not just below the HL min."""
        size = calculate_position_size(
            500.0, "MEDIUM", self._preset(), self._strategy(), self._risk(),
            network="testnet",
        )
        assert size == 15.0

    def test_testnet_no_bump_when_above_floor(self):
        """If math already clears the floor, leave the size alone — the
        user's sizing intent is respected once it's safe to fire as-is."""
        size = calculate_position_size(
            1000.0, "LOW", self._preset(), self._strategy(), self._risk(),
            network="testnet",
        )
        assert size == 40.0

    def test_mainnet_still_raises_below_min(self):
        """Mainnet contract unchanged — never silently trade larger than
        the user's risk-level math intended."""
        with pytest.raises(PositionSizeError, match="below minimum"):
            calculate_position_size(
                500.0, "HIGH", self._preset(), self._strategy(), self._risk(),
                network="mainnet",
            )

    def test_default_network_is_mainnet(self):
        """If a caller forgets to pass network, default to mainnet
        (conservative). The bug-prone direction would be defaulting to
        testnet and silently up-bumping in prod."""
        with pytest.raises(PositionSizeError, match="below minimum"):
            calculate_position_size(
                500.0, "HIGH", self._preset(), self._strategy(), self._risk(),
            )

    def test_testnet_floor_clamped_by_max_position(self):
        """Floor can't exceed the user's per-position cap. Edge case: if
        max < HL min, trade fires at the cap and fails downstream — but
        that's a user-config inconsistency surfacing where it should."""
        size = calculate_position_size(
            500.0, "HIGH", self._preset(), self._strategy(),
            self._risk(max_position_size_usd=12.0, testnet_position_floor_usd=15.0),
            network="testnet",
        )
        assert size == 12.0

    def test_testnet_floor_disabled_raises(self):
        """Setting floor to 0 disables the feature even on testnet — gives
        operators a way to opt out without changing network."""
        with pytest.raises(PositionSizeError, match="below minimum"):
            calculate_position_size(
                500.0, "HIGH", self._preset(), self._strategy(),
                self._risk(testnet_position_floor_usd=0.0),
                network="testnet",
            )

    def test_testnet_floor_default_value(self):
        """Default floor is $15 — chosen to clear HL's $10 min after
        szDecimals flooring for every CP-listed coin. If anyone bumps the
        default down, this test fails and they have to revisit the
        szDecimals analysis in the field docstring."""
        assert RiskConfig().testnet_position_floor_usd == 15.0


# ------------------------------------------------------------------
# Risk gate (check_risk_limits)
# ------------------------------------------------------------------

class TestCheckRiskLimits:

    def _default_risk(self, **overrides):
        kwargs = {
            "max_open_positions": 10,
            "max_daily_loss_pct": 10.0,
            "max_position_size_usd": 500.0,
            "max_total_exposure_usd": 2000.0,
            "min_order_usd": 10.0,
        }
        kwargs.update(overrides)
        return RiskConfig(**kwargs)

    def test_all_clear(self):
        """No limits breached — should pass silently."""
        check_risk_limits(
            risk_config=self._default_risk(),
            open_trade_count=3,
            daily_pnl_pct=-2.0,
            total_exposure_usd=500.0,
            new_position_usd=100.0,
        )

    def test_max_positions_breached(self):
        with pytest.raises(RiskLimitBreached, match="Max open positions"):
            check_risk_limits(
                risk_config=self._default_risk(max_open_positions=5),
                open_trade_count=5,
                daily_pnl_pct=0.0,
                total_exposure_usd=0.0,
                new_position_usd=100.0,
            )

    def test_max_positions_at_limit(self):
        """Exactly at limit should be breached (>=)."""
        with pytest.raises(RiskLimitBreached):
            check_risk_limits(
                risk_config=self._default_risk(max_open_positions=3),
                open_trade_count=3,
                daily_pnl_pct=0.0,
                total_exposure_usd=0.0,
                new_position_usd=100.0,
            )

    def test_max_positions_below_limit(self):
        """Below limit should pass."""
        check_risk_limits(
            risk_config=self._default_risk(max_open_positions=3),
            open_trade_count=2,
            daily_pnl_pct=0.0,
            total_exposure_usd=0.0,
            new_position_usd=100.0,
        )

    def test_daily_loss_breached(self):
        with pytest.raises(RiskLimitBreached, match="Daily loss limit"):
            check_risk_limits(
                risk_config=self._default_risk(max_daily_loss_pct=10.0),
                open_trade_count=0,
                daily_pnl_pct=-10.0,
                total_exposure_usd=0.0,
                new_position_usd=100.0,
            )

    def test_daily_loss_exactly_at_limit(self):
        """At exactly -max_daily_loss_pct should breach."""
        with pytest.raises(RiskLimitBreached):
            check_risk_limits(
                risk_config=self._default_risk(max_daily_loss_pct=5.0),
                open_trade_count=0,
                daily_pnl_pct=-5.0,
                total_exposure_usd=0.0,
                new_position_usd=100.0,
            )

    def test_daily_loss_within_limit(self):
        """Loss within limit should pass."""
        check_risk_limits(
            risk_config=self._default_risk(max_daily_loss_pct=10.0),
            open_trade_count=0,
            daily_pnl_pct=-9.9,
            total_exposure_usd=0.0,
            new_position_usd=100.0,
        )

    def test_daily_profit_always_passes(self):
        """Positive daily PnL should never trigger the loss breaker."""
        check_risk_limits(
            risk_config=self._default_risk(max_daily_loss_pct=1.0),
            open_trade_count=0,
            daily_pnl_pct=50.0,
            total_exposure_usd=0.0,
            new_position_usd=100.0,
        )

    def test_total_exposure_breached(self):
        with pytest.raises(RiskLimitBreached, match="Total exposure"):
            check_risk_limits(
                risk_config=self._default_risk(max_total_exposure_usd=1000.0),
                open_trade_count=0,
                daily_pnl_pct=0.0,
                total_exposure_usd=950.0,
                new_position_usd=100.0,
            )

    def test_total_exposure_exactly_at_limit(self):
        """New trade pushing exposure to exactly max should pass (> not >=)."""
        check_risk_limits(
            risk_config=self._default_risk(max_total_exposure_usd=1000.0),
            open_trade_count=0,
            daily_pnl_pct=0.0,
            total_exposure_usd=900.0,
            new_position_usd=100.0,
        )

    def test_total_exposure_over_limit(self):
        with pytest.raises(RiskLimitBreached):
            check_risk_limits(
                risk_config=self._default_risk(max_total_exposure_usd=1000.0),
                open_trade_count=0,
                daily_pnl_pct=0.0,
                total_exposure_usd=900.0,
                new_position_usd=101.0,
            )

    def test_multiple_limits_first_wins(self):
        """When multiple limits are breached, max positions is checked first."""
        with pytest.raises(RiskLimitBreached, match="Max open positions"):
            check_risk_limits(
                risk_config=self._default_risk(
                    max_open_positions=1,
                    max_daily_loss_pct=1.0,
                    max_total_exposure_usd=10.0,
                ),
                open_trade_count=5,
                daily_pnl_pct=-50.0,
                total_exposure_usd=9999.0,
                new_position_usd=100.0,
            )

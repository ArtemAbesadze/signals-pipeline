"""Tests for blofin_order_builder — contract-value sizing, tickSize/lotSize
rounding, leverage cap, TP split, native TP/SL structure (Phase 6.4)."""

import pytest

from src.exchange.blofin_order_builder import (
    BlofinTpslParams,
    build_blofin_orders,
)
from src.parser.signal_parser import ParsedSignal, RiskLevel, Side

# Real demo metadata shapes (BLOFIN_INTEGRATION.md § 0)
META = {
    "BTC-USDT": {"contractValue": "0.001", "lotSize": "0.1", "minSize": "0.1",
                 "tickSize": "0.1", "maxLeverage": "150"},
    "ETH-USDT": {"contractValue": "0.01", "lotSize": "0.01", "minSize": "0.01",
                 "tickSize": "0.01", "maxLeverage": "100"},
}


def _signal(pair="BTC/USDT", side=Side.LONG, entry=50000.0, sl=49000.0,
            tp1=51000.0, tp2=52000.0, tp3=55000.0, leverage=10, trade_id=1):
    return ParsedSignal(
        pair=pair, trade_id=trade_id, risk_level=RiskLevel.MEDIUM,
        trade_type="SWING", size="1-4%", side=side, entry=entry, stop_loss=sl,
        tp1=tp1, tp2=tp2, tp3=tp3, leverage=leverage,
    )


class TestContractValueSizing:
    def test_btc_sentinel(self):
        """Doc-pinned: $50 at $50,000 entry on BTC (contractValue 0.001)
        = exactly 1 contract (= 0.001 BTC). The contractValue factor is the
        Blofin-specific failure mode — this guards it."""
        s = build_blofin_orders(_signal(), 50.0, META)
        assert s.entry.size == 1.0

    def test_eth_contract_value(self):
        # ETH contractValue 0.01: $300 at $3000 = 300/(3000*0.01)=10 contracts
        s = build_blofin_orders(
            _signal(pair="ETH/USDT", entry=3000.0, sl=2900.0,
                    tp1=3100.0, tp2=3200.0, tp3=3300.0),
            300.0, META,
        )
        assert s.entry.size == 10.0

    def test_lot_size_flooring(self):
        # $189 at $50000 BTC → 3.78 contracts → floor to lotSize 0.1 → 3.7
        s = build_blofin_orders(_signal(), 189.0, META)
        assert s.entry.size == 3.7

    def test_below_min_size_raises(self):
        # $1 at $50000 BTC → 0.02 contracts < minSize 0.1
        with pytest.raises(ValueError, match="below Blofin minSize"):
            build_blofin_orders(_signal(), 1.0, META)


class TestPriceRounding:
    def test_tick_rounding(self):
        s = build_blofin_orders(
            _signal(entry=50000.07, sl=49000.04, tp1=51000.06,
                    tp2=52000.0, tp3=55000.0),
            500.0, META,
        )
        assert s.entry.price == 50000.1   # tickSize 0.1
        assert s.stop_loss.sl_trigger_price == 49000.0
        assert s.take_profits[0].tp_trigger_price == 51000.1


class TestLeverageCap:
    def test_capped_by_user(self):
        s = build_blofin_orders(_signal(leverage=200), 500.0, META, max_leverage=50)
        assert s.leverage == 50

    def test_capped_by_instrument(self):
        # BTC maxLeverage 150; signal 200, no user cap → 150
        s = build_blofin_orders(_signal(leverage=200), 500.0, META)
        assert s.leverage == 150

    def test_min_of_all_three(self):
        s = build_blofin_orders(_signal(leverage=200), 500.0, META, max_leverage=100)
        assert s.leverage == 100  # min(200, 100, 150)

    def test_signal_below_caps(self):
        s = build_blofin_orders(_signal(leverage=20), 500.0, META, max_leverage=100)
        assert s.leverage == 20

    def test_uncapped_zero_follows_cp_clamped_to_instrument(self):
        # 6.12 global policy: max_leverage=0 = uncapped → follow CP, clamp only
        # to the instrument max (BTC 150).
        assert build_blofin_orders(_signal(leverage=200), 500.0, META, max_leverage=0).leverage == 150
        assert build_blofin_orders(_signal(leverage=75), 500.0, META, max_leverage=0).leverage == 75


class TestSideMapping:
    def test_long(self):
        s = build_blofin_orders(_signal(side=Side.LONG), 500.0, META)
        assert s.entry.side == "buy"
        assert s.stop_loss.side == "sell"        # close a long = sell
        assert all(tp.side == "sell" for tp in s.take_profits)

    def test_short(self):
        s = build_blofin_orders(_signal(side=Side.SHORT), 500.0, META)
        assert s.entry.side == "sell"
        assert s.stop_loss.side == "buy"         # close a short = buy
        assert all(tp.side == "buy" for tp in s.take_profits)


class TestTpSlStructure:
    def test_sl_is_full_size_sl_only(self):
        s = build_blofin_orders(_signal(), 500.0, META)
        assert s.stop_loss.size == s.entry.size
        assert s.stop_loss.sl_trigger_price is not None
        assert s.stop_loss.tp_trigger_price is None
        assert s.stop_loss.reduce_only is True

    def test_tps_are_tp_only(self):
        s = build_blofin_orders(_signal(), 500.0, META)
        assert len(s.take_profits) == 3
        for tp in s.take_profits:
            assert tp.tp_trigger_price is not None
            assert tp.sl_trigger_price is None
            assert tp.reduce_only is True

    def test_tp_sizes_sum_to_entry(self):
        # contracts = 10 (200/(50000*0.001)*... let's use $500 -> 10 contracts)
        s = build_blofin_orders(_signal(), 500.0, META)
        total = round(sum(tp.size for tp in s.take_profits), 6)
        assert total == s.entry.size

    def test_even_split_remainder_to_last(self):
        # contracts = 1.0, even_split 0.33/0.33/0.34 -> 0.3/0.3/0.4
        s = build_blofin_orders(_signal(), 50.0, META)
        sizes = [tp.size for tp in s.take_profits]
        assert sizes == [0.3, 0.3, pytest.approx(0.4)]

    def test_custom_tp_split(self):
        # hybrid 10/70/20 on 10 contracts -> 1.0 / 7.0 / 2.0
        s = build_blofin_orders(_signal(), 500.0, META, tp_split=[0.1, 0.7, 0.2])
        sizes = [round(tp.size, 6) for tp in s.take_profits]
        assert sizes == [1.0, 7.0, 2.0]


class TestValidation:
    def test_bad_tp_split_raises(self):
        with pytest.raises(ValueError, match="tp_split"):
            build_blofin_orders(_signal(), 500.0, META, tp_split=[0.5, 0.5])

    def test_missing_instrument_raises(self):
        # Distinct type (Rec #3) so the pipeline can tell "coin not on this
        # venue" apart from a sizing bug — still a ValueError subclass, so the
        # existing build-failure handling keeps catching it.
        from src.exchange.errors import InstrumentNotAvailableError
        with pytest.raises(InstrumentNotAvailableError, match="not found in Blofin metadata"):
            build_blofin_orders(_signal(pair="FAKE/USDT"), 500.0, META)

    def test_inst_id_and_coin(self):
        s = build_blofin_orders(_signal(pair="ETH/USDT", entry=3000.0, sl=2900.0,
                                        tp1=3100.0, tp2=3200.0, tp3=3300.0),
                                300.0, META)
        assert s.inst_id == "ETH-USDT"
        assert s.coin == "ETH"

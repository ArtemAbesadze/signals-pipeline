"""Tests for scripts/test_driver.py — synthetic CP-format signal generator.

Critical invariants:

  - Every rendered template, fed back through the bot's classifier and
    the relevant parser, classifies as the expected MessageType and
    parses without error. If CP ever changes format and our templates
    drift, these tests catch it before a soak run wastes time.

  - LONG / SHORT price levels are sensible (SL on the right side of
    entry, TPs flipped).

  - Schedule generation honors the configured trade_count + duration
    and is deterministic with a seed.

  - Scenario assignment cycles through the configured names so all
    five lifecycle paths get exercised.

  - Cleanup deletes only the test trade_id range and leaves real
    CP trades alone.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.test_driver import (
    SCENARIOS,
    TEST_TRADE_ID_MAX,
    TEST_TRADE_ID_MIN,
    TradePlan,
    _format_price,
    assign_scenarios,
    cleanup_test_data,
    compute_price_levels,
    extract_pairs_from_samples,
    filter_to_hl_universe,
    generate_start_times,
    get_next_test_trade_id,
    render_event,
    render_signal_alert,
    sample_timing,
)
from src.parser.classifier import MessageType, classify
from src.parser.signal_parser import parse_signal
from src.parser.update_parser import (
    parse_all_tp_hit,
    parse_breakeven,
    parse_canceled,
    parse_stop_hit,
    parse_tp_hit,
    parse_trade_live,
)


def _make_plan(side: str = "LONG", trade_id: int = 7_000_001, coin: str = "DOT") -> TradePlan:
    entry = 4.50
    levels = compute_price_levels(side, entry)
    return TradePlan(
        trade_id=trade_id,
        coin=coin,
        pair=f"{coin}/USDT",
        side=side,
        risk="LOW",
        leverage=14,
        entry=entry,
        scenario=SCENARIOS["all_tp_hit"],
        start_offset_sec=0.0,
        **levels,
    )


class TestTemplateFidelity:
    """Every rendered template round-trips through classify + parser."""

    def test_signal_alert_long_round_trips(self):
        plan = _make_plan(side="LONG", trade_id=7_000_042)
        msg = render_signal_alert(plan)
        assert classify(msg) == MessageType.SIGNAL_ALERT
        parsed = parse_signal(msg)
        assert parsed.trade_id == 7_000_042
        assert parsed.pair == "DOT/USDT"
        assert parsed.side.value == "LONG"
        assert parsed.risk_level.value == "LOW"
        assert parsed.entry == plan.entry
        # LONG: SL below entry, TPs above
        assert parsed.stop_loss < parsed.entry < parsed.tp1 < parsed.tp2 < parsed.tp3

    def test_signal_alert_short_round_trips(self):
        plan = _make_plan(side="SHORT")
        plan.risk = "HIGH"
        msg = render_signal_alert(plan)
        assert classify(msg) == MessageType.SIGNAL_ALERT
        parsed = parse_signal(msg)
        assert parsed.side.value == "SHORT"
        assert parsed.risk_level.value == "HIGH"
        # SHORT: SL above entry, TPs below
        assert parsed.tp3 < parsed.tp2 < parsed.tp1 < parsed.entry < parsed.stop_loss

    def test_trade_live_round_trips(self):
        plan = _make_plan()
        msg = render_event("trade_live", plan, {}, random.Random(0))
        assert classify(msg) == MessageType.TRADE_LIVE
        parsed = parse_trade_live(msg)
        assert parsed.trade_id == plan.trade_id
        assert parsed.pair == plan.pair

    @pytest.mark.parametrize("tp_number", [1, 2, 3])
    def test_tp_hit_round_trips(self, tp_number):
        plan = _make_plan()
        msg = render_event("tp_hit", plan, {"tp_number": tp_number}, random.Random(0))
        assert classify(msg) == MessageType.TP_HIT
        parsed = parse_tp_hit(msg)
        assert parsed.tp_number == tp_number
        assert parsed.trade_id == plan.trade_id

    @pytest.mark.parametrize("tp_secured", [1, 2])
    def test_breakeven_round_trips(self, tp_secured):
        plan = _make_plan()
        msg = render_event("breakeven", plan, {"tp_secured": tp_secured}, random.Random(0))
        assert classify(msg) == MessageType.BREAKEVEN
        parsed = parse_breakeven(msg)
        assert parsed.tp_secured == tp_secured

    def test_all_tp_hit_round_trips(self):
        plan = _make_plan()
        msg = render_event("all_tp_hit", plan, {}, random.Random(0))
        assert classify(msg) == MessageType.ALL_TP_HIT
        parsed = parse_all_tp_hit(msg)
        assert parsed.trade_id == plan.trade_id
        assert parsed.profit_pct > 0

    def test_stop_hit_round_trips(self):
        plan = _make_plan()
        msg = render_event("stop_hit", plan, {}, random.Random(0))
        assert classify(msg) == MessageType.STOP_HIT
        parsed = parse_stop_hit(msg)
        assert parsed.trade_id == plan.trade_id
        assert parsed.loss_pct < 0

    def test_canceled_round_trips(self):
        plan = _make_plan()
        msg = render_event("canceled", plan, {}, random.Random(0))
        assert classify(msg) == MessageType.CANCELED
        parsed = parse_canceled(msg)
        assert parsed.trade_id == plan.trade_id
        # Reason text is one of our configured strings — not None / not empty
        assert parsed.reason


class TestPriceLevels:
    def test_long_levels_above_entry(self):
        levels = compute_price_levels("LONG", 100.0)
        assert levels["sl"] < 100.0 < levels["tp1"] < levels["tp2"] < levels["tp3"]

    def test_short_levels_below_entry(self):
        levels = compute_price_levels("SHORT", 100.0)
        assert levels["tp3"] < levels["tp2"] < levels["tp1"] < 100.0 < levels["sl"]

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError):
            compute_price_levels("DIAGONAL", 100.0)

    def test_format_price_precision_scales_with_magnitude(self):
        assert _format_price(1500.0) == "1500.00"
        assert _format_price(4.5).count(".") == 1 and len(_format_price(4.5).split(".")[1]) == 4
        assert _format_price(0.025).count(".") == 1 and len(_format_price(0.025).split(".")[1]) == 5
        assert _format_price(0.0001234).startswith("0.")


class TestScheduling:
    def test_generate_start_times_count_and_range(self):
        rng = random.Random(7)
        offsets = generate_start_times(rng, num_trades=5, duration_sec=3600, randomize=True)
        assert len(offsets) == 5
        assert offsets == sorted(offsets)
        assert all(0 <= t < 3600 for t in offsets)

    def test_generate_start_times_deterministic_with_seed(self):
        rng1 = random.Random(123)
        rng2 = random.Random(123)
        a = generate_start_times(rng1, 5, 3600, randomize=True)
        b = generate_start_times(rng2, 5, 3600, randomize=True)
        assert a == b

    def test_generate_start_times_evenly_spaced(self):
        offsets = generate_start_times(random.Random(0), 4, 3600, randomize=False)
        assert offsets == [0.0, 900.0, 1800.0, 2700.0]

    def test_assign_scenarios_uses_every_name(self):
        rng = random.Random(0)
        names = ["a", "b", "c", "d", "e"]
        assigned = assign_scenarios(rng, names, num_trades=5)
        assert sorted(assigned) == sorted(names)

    def test_assign_scenarios_more_trades_than_names_cycles(self):
        rng = random.Random(0)
        names = ["a", "b", "c", "d", "e"]
        assigned = assign_scenarios(rng, names, num_trades=12)
        # Each name appears at least twice (12 / 5 = 2.4)
        for n in names:
            assert assigned.count(n) >= 2

    def test_sample_timing_immediate_returns_zero(self):
        rng = random.Random(0)
        assert sample_timing(rng, {}, "_immediate") == 0.0

    def test_sample_timing_in_range(self):
        rng = random.Random(0)
        cfg = {"foo": [30, 120]}
        for _ in range(20):
            v = sample_timing(rng, cfg, "foo")
            assert 30 <= v <= 120


class TestCoinPool:
    def test_extract_pairs_from_samples(self):
        pairs = extract_pairs_from_samples(Path("signals/samples"))
        # Spot-check known pairs in the corpus
        assert "ETH" in pairs
        assert "ADA" in pairs
        assert "1000BONK" in pairs

    def test_filter_drops_unknown_coins(self):
        # Pretend HL only has ETH and ADA
        meta = {"ETH": {"szDecimals": 4}, "ADA": {"szDecimals": 0}}
        pool = filter_to_hl_universe(
            {"ETH", "ADA", "FAKECOIN", "XRP"}, meta, exclude={"XRP"},
        )
        names = {hl for _, hl in pool}
        assert names == {"ETH", "ADA"}

    def test_filter_drops_excluded(self):
        meta = {"ETH": {"szDecimals": 4}, "ADA": {"szDecimals": 0}}
        pool = filter_to_hl_universe({"ETH", "ADA"}, meta, exclude={"ETH"})
        names = {hl for _, hl in pool}
        assert names == {"ADA"}


class TestTradeIdAllocation:
    def test_next_id_when_no_db(self, tmp_path):
        next_id = get_next_test_trade_id(tmp_path / "missing.db")
        assert next_id == TEST_TRADE_ID_MIN + 1

    def test_next_id_resumes_after_previous_run(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (trade_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO trades VALUES (?)", (TEST_TRADE_ID_MIN + 5,))
        conn.execute("INSERT INTO trades VALUES (?)", (1234,))  # real CP trade
        conn.commit()
        conn.close()
        assert get_next_test_trade_id(db) == TEST_TRADE_ID_MIN + 6

    def test_test_id_range_is_documented_and_large(self):
        """The numeric range is the primary "obvious test trade" marker.
        Sanity-check it stays where the docs say it is."""
        assert TEST_TRADE_ID_MIN == 7_000_000
        assert TEST_TRADE_ID_MAX == 7_999_999
        assert TEST_TRADE_ID_MAX - TEST_TRADE_ID_MIN == 999_999


class TestCleanup:
    def test_cleanup_only_deletes_test_range(self, tmp_path):
        """The killer test — real CP trades MUST survive a cleanup."""
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE trades (trade_id INTEGER, user_id TEXT, PRIMARY KEY (user_id, trade_id));
            CREATE TABLE orders (trade_id INTEGER, oid INTEGER PRIMARY KEY);
            CREATE TABLE trade_events (id INTEGER PRIMARY KEY, trade_id INTEGER);
        """)
        # Real CP trades
        for tid in (1234, 2127, 2177):
            conn.execute("INSERT INTO trades VALUES (?, 'real_user')", (tid,))
            conn.execute("INSERT INTO orders VALUES (?, ?)", (tid, tid * 100))
            conn.execute("INSERT INTO trade_events VALUES (NULL, ?)", (tid,))
        # Test trades
        for tid in (TEST_TRADE_ID_MIN + 1, TEST_TRADE_ID_MIN + 2, TEST_TRADE_ID_MAX - 1):
            conn.execute("INSERT INTO trades VALUES (?, 'real_user')", (tid,))
            conn.execute("INSERT INTO orders VALUES (?, ?)", (tid, tid * 100))
            conn.execute("INSERT INTO trade_events VALUES (NULL, ?)", (tid,))
            conn.execute("INSERT INTO trade_events VALUES (NULL, ?)", (tid,))
        conn.commit()
        conn.close()

        result = cleanup_test_data(db)
        assert result == {"trades": 3, "orders": 3, "trade_events": 6}

        # Real CP trades survive
        conn = sqlite3.connect(db)
        remaining_trades = [r[0] for r in conn.execute("SELECT trade_id FROM trades")]
        remaining_orders = [r[0] for r in conn.execute("SELECT trade_id FROM orders")]
        remaining_events = [r[0] for r in conn.execute("SELECT trade_id FROM trade_events")]
        conn.close()

        assert sorted(remaining_trades) == [1234, 2127, 2177]
        assert sorted(remaining_orders) == [1234, 2127, 2177]
        assert sorted(remaining_events) == [1234, 2127, 2177]

    def test_cleanup_no_db_returns_zeros(self, tmp_path):
        result = cleanup_test_data(tmp_path / "missing.db")
        assert result == {"trades": 0, "orders": 0, "trade_events": 0}


class TestScenarios:
    def test_all_five_scenarios_defined(self):
        expected = {
            "all_tp_hit", "stop_hit", "tp1_then_stop",
            "cancel_pending", "cancel_after_fill",
        }
        assert set(SCENARIOS.keys()) == expected

    def test_each_scenario_starts_with_signal_alert(self):
        for name, scenario in SCENARIOS.items():
            assert scenario.events[0].event_type == "signal_alert", name

    def test_cancel_after_fill_includes_trade_live(self):
        """D10 case — entry must fill before the cancel for the bot's
        cancel handler to correctly route to market-close."""
        events = [e.event_type for e in SCENARIOS["cancel_after_fill"].events]
        assert events.index("trade_live") < events.index("canceled")

    def test_all_tp_hit_includes_breakeven_between_tps(self):
        events = [e.event_type for e in SCENARIOS["all_tp_hit"].events]
        idx_tp1 = events.index("tp_hit")
        idx_be = events.index("breakeven")
        # Find the next tp_hit after BE
        idx_tp2 = events.index("tp_hit", idx_be + 1)
        assert idx_tp1 < idx_be < idx_tp2

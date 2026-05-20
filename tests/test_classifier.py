"""Tests for message type classification against all real signal samples."""

from pathlib import Path

import pytest

from src.parser.classifier import MessageType, classify

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text().strip()


# ------------------------------------------------------------------
# Classifier: every sample file → correct MessageType
# ------------------------------------------------------------------

class TestClassifier:
    """Classify all 28 real samples and verify the correct MessageType."""

    # --- SIGNAL_ALERT ---

    def test_signal_alert_01(self):
        assert classify(_load("signal_alert_01.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_04_no_header(self):
        """signal_alert_04 has no 'TRADING SIGNAL ALERT' header — uses fallback."""
        assert classify(_load("signal_alert_04.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_05_kilo_prefix(self):
        assert classify(_load("signal_alert_05.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_06(self):
        assert classify(_load("signal_alert_06.txt")) == MessageType.SIGNAL_ALERT

    def test_signal_alert_07_bare_risk_format(self):
        """1000BONK — new CP format with bare `LOW RISK` (no parens) + ticker header."""
        assert classify(_load("signal_alert_07.txt")) == MessageType.SIGNAL_ALERT

    # --- ORDER_PENDING ---

    def test_order_pending_01(self):
        """ORDER_PENDING — wins over SIGNAL_ALERT despite 'Trading Signal Alert' header."""
        assert classify(_load("order_pending_01.txt")) == MessageType.ORDER_PENDING

    # --- TRADE_LIVE ---

    def test_trade_live_01(self):
        assert classify(_load("trade_live_01.txt")) == MessageType.TRADE_LIVE

    # --- TP_HIT ---

    def test_tp_hit_01(self):
        assert classify(_load("tp_hit_01.txt")) == MessageType.TP_HIT

    def test_tp_hit_02(self):
        assert classify(_load("tp_hit_02.txt")) == MessageType.TP_HIT

    def test_tp_hit_03(self):
        assert classify(_load("tp_hit_03.txt")) == MessageType.TP_HIT

    def test_tp_hit_04_signed_profit(self):
        """1000BONK — new CP format with +signed PROFIT and Discord mentions."""
        assert classify(_load("tp_hit_04.txt")) == MessageType.TP_HIT

    # --- ALL_TP_HIT ---

    def test_all_tp_hit_01(self):
        assert classify(_load("all_tp_hit_01.txt")) == MessageType.ALL_TP_HIT

    def test_all_tp_hit_02(self):
        assert classify(_load("all_tp_hit_02.txt")) == MessageType.ALL_TP_HIT

    def test_all_tp_hit_03(self):
        assert classify(_load("all_tp_hit_03.txt")) == MessageType.ALL_TP_HIT

    def test_all_tp_hit_04_pol(self):
        """POL — new CP format with +signed PROFIT."""
        assert classify(_load("all_tp_hit_04.txt")) == MessageType.ALL_TP_HIT

    # --- BREAKEVEN ---

    def test_breakeven_01(self):
        assert classify(_load("breakeven_01.txt")) == MessageType.BREAKEVEN

    def test_breakeven_02(self):
        assert classify(_load("breakeven_02.txt")) == MessageType.BREAKEVEN

    def test_breakeven_03_tp2(self):
        assert classify(_load("breakeven_03.txt")) == MessageType.BREAKEVEN

    def test_breakeven_04_tp2(self):
        assert classify(_load("breakeven_04.txt")) == MessageType.BREAKEVEN

    def test_breakeven_05_link_after_tp2(self):
        """LINK — new CP format with role mentions + Called-by footer."""
        assert classify(_load("breakeven_05.txt")) == MessageType.BREAKEVEN

    def test_breakeven_06_jup_after_tp1(self):
        assert classify(_load("breakeven_06.txt")) == MessageType.BREAKEVEN

    def test_breakeven_07_tao_after_spaced_tp(self):
        """TAO — 'TP 2' with space (between TP and digit) + (prev: <url>) reference."""
        assert classify(_load("breakeven_07.txt")) == MessageType.BREAKEVEN

    # --- STOP_HIT ---

    def test_stop_hit_01(self):
        assert classify(_load("stop_hit_01.txt")) == MessageType.STOP_HIT

    def test_stop_hit_02_trx_with_prev_link(self):
        """TRX — new CP format with stacked role mentions + bare prev URL."""
        assert classify(_load("stop_hit_02.txt")) == MessageType.STOP_HIT

    # --- CANCELED ---

    def test_canceled_01(self):
        assert classify(_load("canceled_01.txt")) == MessageType.CANCELED

    def test_canceled_02(self):
        assert classify(_load("canceled_02.txt")) == MessageType.CANCELED

    def test_canceled_03(self):
        assert classify(_load("canceled_03.txt")) == MessageType.CANCELED

    def test_canceled_04(self):
        assert classify(_load("canceled_04.txt")) == MessageType.CANCELED

    def test_canceled_05(self):
        assert classify(_load("canceled_05.txt")) == MessageType.CANCELED

    def test_canceled_06_sei_tp1_before_entry(self):
        """SEI — TP1 hit before our entry filled."""
        assert classify(_load("canceled_06.txt")) == MessageType.CANCELED

    def test_canceled_07_stx_better_entry(self):
        """STX — 'we will wait for better entry' reason."""
        assert classify(_load("canceled_07.txt")) == MessageType.CANCELED

    # --- TRADE_CLOSED ---

    def test_trade_closed_01(self):
        assert classify(_load("trade_closed_01.txt")) == MessageType.TRADE_CLOSED

    def test_trade_closed_02(self):
        assert classify(_load("trade_closed_02.txt")) == MessageType.TRADE_CLOSED

    def test_trade_closed_03_xrp_after_tp1(self):
        """XRP — new CP format with bracketed prev URL."""
        assert classify(_load("trade_closed_03.txt")) == MessageType.TRADE_CLOSED

    # --- PREPARATION ---

    def test_preparation_01(self):
        assert classify(_load("preparation_01.txt")) == MessageType.PREPARATION

    def test_preparation_02(self):
        assert classify(_load("preparation_02.txt")) == MessageType.PREPARATION

    def test_preparation_03(self):
        assert classify(_load("preparation_03.txt")) == MessageType.PREPARATION

    def test_preparation_04(self):
        assert classify(_load("preparation_04.txt")) == MessageType.PREPARATION

    # --- MANUAL_UPDATE ---

    def test_manual_update_01(self):
        assert classify(_load("manual_update_01.txt")) == MessageType.MANUAL_UPDATE

    # --- NOISE ---

    def test_noise_01(self):
        assert classify(_load("noise_01.txt")) == MessageType.NOISE


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------

class TestClassifierEdgeCases:

    def test_empty_string(self):
        assert classify("") == MessageType.NOISE

    def test_random_text(self):
        assert classify("hello world nothing here") == MessageType.NOISE

    def test_signal_without_header_has_fields(self):
        """A message with ENTRY, SL, and TP fields but no header → SIGNAL_ALERT."""
        msg = "PAIR: BTC/USDT #9999\nENTRY: 50000\nSL: 49000\nTP1: 51000\nTP2: 52000"
        assert classify(msg) == MessageType.SIGNAL_ALERT

    def test_cancel_keyword_variations(self):
        assert classify("Trade #1234 Canceled") == MessageType.CANCELED
        assert classify("CANCEL BTC/USDT #1234") == MessageType.CANCELED

    def test_all_tp_before_single_tp(self):
        """ALL_TP_HIT must match before TP_HIT to avoid false positives."""
        msg = "ALL TAKE-PROFIT TARGETS HIT\nPAIR: BTC/USDT #1234\nPROFIT: 100%"
        assert classify(msg) == MessageType.ALL_TP_HIT

    def test_order_pending_beats_signal_alert_header(self):
        """Regression: order-pending messages contain a 'Trading Signal Alert'
        header preamble. ORDER_PENDING must win the classifier race."""
        msg = (
            "Trading Signal Alert <@&123>\n"
            "**ORDER PENDING ⌛️**\n\n"
            "📝**PAIR:** BTC/USDT #1234\n\n"
            "Price moved, waiting for pullback..."
        )
        assert classify(msg) == MessageType.ORDER_PENDING

    def test_trade_live_distinct_from_signal_alert(self):
        """TRADE IS LIVE messages don't include 'Trading Signal Alert' but
        still need to be classified as TRADE_LIVE, not MANUAL_UPDATE."""
        msg = (
            "LIVE BTC <@&123>\n"
            "**TRADE IS LIVE** ✅\n\n"
            "**PAIR:** BTC/USDT #1234"
        )
        assert classify(msg) == MessageType.TRADE_LIVE

    def test_discord_mentions_stripped_for_classification(self):
        """Role/user mentions and Called-by footer don't disrupt classification."""
        msg = (
            "STOP HIT BTC <@&123>\n"
            "<@&456>\n"
            "**STOP TARGET HIT**\n\n"
            "PAIR: BTC/USDT #9999 (prev: <https://discord.com/channels/1/2/3>)\n\n"
            "LOSS: -10.5% 📉\n\n"
            "Called by <@1436550935743565884>"
        )
        assert classify(msg) == MessageType.STOP_HIT

    def test_bare_url_in_prev_ref_stripped(self):
        """`(prev: https://...)` without angle brackets is stripped too."""
        msg = (
            "STOP HIT BTC <@&123>\n"
            "**STOP TARGET HIT**\n\n"
            "PAIR: BTC/USDT #9999 (prev: https://discord.com/channels/1/2/3)\n\n"
            "LOSS: -10.5%"
        )
        assert classify(msg) == MessageType.STOP_HIT

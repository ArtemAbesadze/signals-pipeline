"""Tests for TRADING SIGNAL ALERT parsing — field-level assertions."""

from pathlib import Path

import pytest

from src.parser.signal_parser import ParsedSignal, RiskLevel, Side, SignalParseError, parse_signal

SAMPLES_DIR = Path("signals/samples")


def _load(filename: str) -> str:
    return (SAMPLES_DIR / filename).read_text().strip()


class TestSignalParser:
    """Parse real SIGNAL_ALERT samples and verify all extracted fields."""

    def test_signal_alert_01_zk(self):
        s = parse_signal(_load("signal_alert_01.txt"))
        assert s.pair == "ZK/USDT"
        assert s.trade_id == 1286
        assert s.risk_level == RiskLevel.MEDIUM
        assert s.trade_type == "SWING"
        assert s.size == "1-4%"
        assert s.side == Side.SHORT
        assert s.entry == 0.02153
        assert s.stop_loss == 0.02236
        assert s.tp1 == 0.02113
        assert s.tp2 == 0.02068
        assert s.tp3 == 0.01885
        assert s.leverage == 14

    def test_signal_alert_04_no_header_xrp(self):
        """signal_alert_04 has no 'TRADING SIGNAL ALERT' header."""
        s = parse_signal(_load("signal_alert_04.txt"))
        assert s.pair == "XRP/USDT"
        assert s.trade_id == 1282
        assert s.risk_level == RiskLevel.HIGH
        assert s.trade_type == "SWING"
        assert s.side == Side.SHORT
        assert s.entry == 1.4171
        assert s.stop_loss == 1.4996
        assert s.tp1 == 1.3741
        assert s.tp2 == 1.3293
        assert s.tp3 == 1.1604
        assert s.leverage == 27

    def test_signal_alert_05_kilo_bonk(self):
        """1000BONK/USDT — tests kilo-prefix pair naming."""
        s = parse_signal(_load("signal_alert_05.txt"))
        assert s.pair == "1000BONK/USDT"
        assert s.trade_id == 1269
        assert s.risk_level == RiskLevel.MEDIUM
        assert s.side == Side.LONG
        assert s.entry == 0.00738
        assert s.stop_loss == 0.006981
        assert s.tp1 == 0.007517
        assert s.tp2 == 0.007687
        assert s.tp3 == 0.008385
        assert s.leverage == 16

    def test_signal_alert_06_ada_low_risk(self):
        s = parse_signal(_load("signal_alert_06.txt"))
        assert s.pair == "ADA/USDT"
        assert s.trade_id == 1259
        assert s.risk_level == RiskLevel.LOW
        assert s.side == Side.LONG
        assert s.entry == 0.3026
        assert s.stop_loss == 0.2931
        assert s.tp1 == 0.3058
        assert s.tp2 == 0.3092
        assert s.tp3 == 0.3265
        assert s.leverage == 14

    def test_signal_alert_07_bare_risk_with_mentions(self):
        """New CP format: bare `LOW RISK` (no parens), ticker header,
        Discord mentions, Called-by footer — all must parse cleanly."""
        s = parse_signal(_load("signal_alert_07.txt"))
        assert s.pair == "1000BONK/USDT"
        assert s.trade_id == 2068
        assert s.risk_level == RiskLevel.LOW
        assert s.trade_type == "SWING"
        assert s.size == "1-4%"
        assert s.side == Side.SHORT
        assert s.entry == 0.00713
        assert s.stop_loss == 0.007268
        assert s.tp1 == 0.007061
        assert s.tp2 == 0.006961
        assert s.tp3 == 0.00663
        assert s.leverage == 16

    def test_signal_alert_08_position_type(self):
        """BCH — TYPE: POSITION (new type) + emoji-color risk indicator."""
        s = parse_signal(_load("signal_alert_08.txt"))
        assert s.pair == "BCH/USDT"
        assert s.trade_id == 2094
        assert s.risk_level == RiskLevel.HIGH
        assert s.trade_type == "POSITION"
        assert s.side == Side.SHORT
        assert s.entry == 371.8
        assert s.stop_loss == 395.83
        assert s.tp1 == 357.77
        assert s.tp2 == 359.52
        assert s.tp3 == 336.55
        assert s.leverage == 32

    def test_signal_alert_09_minimal_header_with_trade_on_link(self):
        """TRX — minimalist 'Trading Signal Alert' header + 🔗 Trade on link;
        URL must be stripped before parsing."""
        s = parse_signal(_load("signal_alert_09.txt"))
        assert s.pair == "TRX/USDT"
        assert s.trade_id == 2092
        assert s.risk_level == RiskLevel.LOW
        assert s.trade_type == "SWING"
        assert s.side == Side.SHORT
        assert s.entry == 0.3542
        assert s.stop_loss == 0.35758
        assert s.leverage == 27


class TestSignalParserRiskLevelFormat:
    """Both old (parenthesized) and new (bare) risk-level formats parse."""

    def _build_signal(self, risk_field: str) -> str:
        return (
            "TRADING SIGNAL ALERT\n\n"
            "PAIR: BTC/USDT #9999\n"
            f"{risk_field}\n\n"
            "TYPE: SWING\n"
            "SIZE: 1-4%\n"
            "SIDE: LONG\n\n"
            "ENTRY: 100\n"
            "SL: 90          (-10%)\n\n"
            "TP1: 110      (10%)\n"
            "TP2: 120      (20%)\n"
            "TP3: 130      (30%)\n\n"
            "LEVERAGE: 10x"
        )

    def test_parenthesized_risk_still_works(self):
        s = parse_signal(self._build_signal("(MEDIUM RISK)"))
        assert s.risk_level == RiskLevel.MEDIUM

    def test_bare_risk_works(self):
        s = parse_signal(self._build_signal("MEDIUM RISK"))
        assert s.risk_level == RiskLevel.MEDIUM

    def test_bare_low_risk_works(self):
        s = parse_signal(self._build_signal("LOW RISK"))
        assert s.risk_level == RiskLevel.LOW

    def test_bare_high_risk_works(self):
        s = parse_signal(self._build_signal("HIGH RISK"))
        assert s.risk_level == RiskLevel.HIGH


class TestSignalParserTradeType:
    """All three TYPE values — SWING, SCALP, POSITION — must parse."""

    def _build(self, type_value: str) -> str:
        return (
            "TRADING SIGNAL ALERT\n\n"
            "PAIR: BTC/USDT #9999\n"
            "LOW RISK\n\n"
            f"TYPE: {type_value}\n"
            "SIZE: 1-4%\n"
            "SIDE: LONG\n\n"
            "ENTRY: 100\n"
            "SL: 90          (-10%)\n\n"
            "TP1: 110      (10%)\n"
            "TP2: 120      (20%)\n"
            "TP3: 130      (30%)\n\n"
            "LEVERAGE: 10x"
        )

    def test_swing(self):
        assert parse_signal(self._build("SWING")).trade_type == "SWING"

    def test_scalp(self):
        assert parse_signal(self._build("SCALP")).trade_type == "SCALP"

    def test_position(self):
        """POSITION — new TYPE value used by CP for longer-term trades."""
        assert parse_signal(self._build("POSITION")).trade_type == "POSITION"


class TestSignalParserEdgeCases:

    def test_missing_pair_raises(self):
        with pytest.raises(SignalParseError, match="PAIR"):
            parse_signal("TRADING SIGNAL ALERT\nENTRY: 100\nSL: 90")

    def test_missing_entry_raises(self):
        with pytest.raises(SignalParseError, match="entry"):
            parse_signal("PAIR: BTC/USDT #1234\n(LOW RISK)\nTYPE: SWING\nSIZE: 1-4%\nSIDE: LONG\nSL: 90\nTP1: 110 (10%)\nTP2: 120 (20%)\nTP3: 130 (30%)\nLEVERAGE: 10x")

    def test_missing_tp_raises(self):
        with pytest.raises(SignalParseError, match="TP"):
            parse_signal("PAIR: BTC/USDT #1234\n(LOW RISK)\nTYPE: SWING\nSIZE: 1-4%\nSIDE: LONG\nENTRY: 100\nSL: 90\nTP1: 110 (10%)\nLEVERAGE: 10x")

    def test_discord_bold_markdown_stripped(self):
        """Bold markers (**) should be stripped without affecting field extraction."""
        msg = (
            "**TRADING SIGNAL ALERT**\n\n"
            "**PAIR:** BTC/USDT #9999\n"
            "**(LOW RISK)**\n\n"
            "**TYPE:** SWING\n"
            "**SIZE:** 1-4%\n"
            "**SIDE:** LONG\n\n"
            "**ENTRY:** 50000\n"
            "**SL:** 49000          (-2%)\n\n"
            "**TP1:** 51000      (2%)\n"
            "**TP2:** 52000      (4%)\n"
            "**TP3:** 55000      (10%)\n\n"
            "**LEVERAGE:** 10x\n"
        )
        s = parse_signal(msg)
        assert s.pair == "BTC/USDT"
        assert s.trade_id == 9999
        assert s.entry == 50000.0
        assert s.leverage == 10

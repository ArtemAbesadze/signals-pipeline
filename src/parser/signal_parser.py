"""Full signal field extraction from TRADING SIGNAL ALERT messages."""

import re
from dataclasses import dataclass
from enum import Enum


class RiskLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Side(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class ParsedSignal:
    """Structured representation of a TRADING SIGNAL ALERT."""

    pair: str            # e.g. "ZK/USDT"
    trade_id: int        # e.g. 1286
    risk_level: RiskLevel
    trade_type: str      # "SWING" or "SCALP"
    size: str            # e.g. "1-4%"
    side: Side
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    leverage: int


# Discord-specific noise patterns we always strip before parsing.
# Kept in sync with src/parser/classifier.py.
_DISCORD_MENTION = re.compile(r"<@[&!]?\d+>")
_PREV_REF = re.compile(r"\s*\(prev:[^)]*\)", re.IGNORECASE)
_BRACKETED_URL = re.compile(r"<https?://[^>]+>")
_BARE_URL = re.compile(r"https?://[^\s)<>]+")
_CALLED_BY_FOOTER = re.compile(r"(?im)^\s*Called by\s*<@\d+>\s*$")
_EMOJI = re.compile(
    r"[\U0001f300-\U0001f9ff\U00002600-\U000027bf\U0000fe00-\U0000fe0f"
    r"\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U0000200d]+",
)


def _clean(text: str) -> str:
    """Strip Discord markdown, mentions, URLs, and Called-by footer."""
    # Called-by footer must be stripped BEFORE general mention stripping —
    # the footer regex anchors on the trailing <@NNN> to identify the line.
    text = _CALLED_BY_FOOTER.sub("", text)
    text = _DISCORD_MENTION.sub("", text)
    text = _PREV_REF.sub("", text)
    text = _BRACKETED_URL.sub("", text)
    text = _BARE_URL.sub("", text)
    text = re.sub(r"\*+", "", text)
    text = text.replace("`", "")
    text = _EMOJI.sub("", text)
    return text


class SignalParseError(Exception):
    """Raised when a required field cannot be extracted."""


def _safe_float(value: str, field: str, error_cls=SignalParseError) -> float:
    """Convert *value* to float, raising a typed parse error on failure.

    The numeric regexes capture ``[\\d.]+`` / ``[+-]?[\\d.]+`` which can
    match malformed numbers like ``1.2.3.4``. ``float()`` on those raises
    ``ValueError`` — we want a parser-specific error class so the pipeline's
    audit hook routes it correctly.
    """
    try:
        return float(value)
    except (ValueError, TypeError) as e:
        raise error_cls(f"Could not parse {field} as a number: '{value}'") from e


def parse_signal(raw_message: str) -> ParsedSignal:
    """Extract all fields from a TRADING SIGNAL ALERT message.

    Args:
        raw_message: Raw message text (may contain Discord formatting).

    Returns:
        A ParsedSignal with all fields populated.

    Raises:
        SignalParseError: If any required field is missing or malformed.
    """
    text = _clean(raw_message)

    # --- PAIR and Trade ID ---
    m = re.search(r"PAIR[:\s]+(\S+/\S+)\s+#(\d+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract PAIR and trade ID")
    pair = m.group(1).upper()
    trade_id = int(m.group(2))

    # --- Risk Level ---
    # CP uses both `(LOW RISK)` (older format) and bare `LOW RISK` (current).
    m = re.search(r"\(?(LOW|MEDIUM|HIGH)\s+RISK\)?", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract risk level")
    risk_level = RiskLevel(m.group(1).upper())

    # --- Type (SWING / SCALP / POSITION) ---
    m = re.search(r"TYPE[:\s]+(SWING|SCALP|POSITION)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract trade type")
    trade_type = m.group(1).upper()

    # --- Size ---
    m = re.search(r"SIZE[:\s]+([\d]+-[\d]+%)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract size")
    size = m.group(1)

    # --- Side ---
    m = re.search(r"SIDE[:\s]+(LONG|SHORT)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract side")
    side = Side(m.group(1).upper())

    # --- Entry ---
    m = re.search(r"ENTRY[:\s]+([\d.]+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract entry price")
    entry = _safe_float(m.group(1), "entry")

    # --- Stop Loss ---
    m = re.search(r"SL[:\s]+([\d.]+)", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract stop loss")
    stop_loss = _safe_float(m.group(1), "stop_loss")

    # --- Take Profit targets (in the TAKE PROFIT TARGETS section) ---
    # Match TP lines that have percentages (to distinguish from R:R lines)
    tp_matches = re.findall(
        r"TP(\d)[:\s]+([\d.]+)\s+\([\d.]+%\)", text, re.IGNORECASE
    )
    tp_map: dict[int, float] = {}
    for tp_num, tp_val in tp_matches:
        tp_map[int(tp_num)] = _safe_float(tp_val, f"TP{tp_num}")

    if not all(k in tp_map for k in (1, 2, 3)):
        raise SignalParseError(
            f"Could not extract all TP targets (found: {sorted(tp_map.keys())})"
        )

    # --- Leverage ---
    m = re.search(r"LEVERAGE[:\s]+(\d+)x?", text, re.IGNORECASE)
    if not m:
        raise SignalParseError("Could not extract leverage")
    leverage = int(m.group(1))

    return ParsedSignal(
        pair=pair,
        trade_id=trade_id,
        risk_level=risk_level,
        trade_type=trade_type,
        size=size,
        side=side,
        entry=entry,
        stop_loss=stop_loss,
        tp1=tp_map[1],
        tp2=tp_map[2],
        tp3=tp_map[3],
        leverage=leverage,
    )

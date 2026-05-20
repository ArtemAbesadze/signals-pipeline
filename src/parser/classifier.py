"""Message type classification — signal, TP hit, prep, update, noise."""

import re
from enum import Enum


class MessageType(Enum):
    """All known CryptoPrinter message types."""

    SIGNAL_ALERT = "signal_alert"
    ORDER_PENDING = "order_pending"     # signal sent, entry resting, price moved away
    TRADE_LIVE = "trade_live"           # CP confirms entry has filled
    TP_HIT = "tp_hit"
    ALL_TP_HIT = "all_tp_hit"
    BREAKEVEN = "breakeven"
    STOP_HIT = "stop_hit"
    CANCELED = "canceled"
    TRADE_CLOSED = "trade_closed"
    PREPARATION = "preparation"
    MANUAL_UPDATE = "manual_update"
    NOISE = "noise"


# Discord-specific noise patterns we always strip before keyword matching.
# Kept in sync with src/parser/signal_parser.py::_clean.
_DISCORD_MENTION = re.compile(r"<@[&!]?\d+>")
_PREV_REF = re.compile(r"\s*\(prev:[^)]*\)", re.IGNORECASE)
_BRACKETED_URL = re.compile(r"<https?://[^>]+>")
_BARE_URL = re.compile(r"https?://[^\s)<>]+")
_CALLED_BY_FOOTER = re.compile(r"(?im)^\s*Called by\s*<@\d+>\s*$")
_EMOJI = re.compile(
    r"[\U0001f300-\U0001f9ff\U00002600-\U000027bf\U0000fe00-\U0000fe0f"
    r"\U0001fa00-\U0001fa6f\U0001fa70-\U0001faff\U0000200d]+",
)

# Phrases that mark CP commentary / recaps / market analysis. None of these
# trigger an action; all classify as NOISE. Match is case-insensitive because
# classify() upper-cases the cleaned text before comparison.
_NOISE_MARKERS = (
    "WEEKLY CRYPTO RESULTS",
    "WEEKLY STATS",
    "DAILY PERFORMANCE",
    "YESTERDAY'S RESULTS",
    "YESTERDAY COLLECTED",
    "NET RESULT:",
    "TOTAL COLLECTED:",
    "MARKET STRUCTURE UPDATE",
    "DATA RECAP",
)


def _strip_markdown(text: str) -> str:
    """Remove Discord markdown, mentions, URLs, and the Called-by footer."""
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


def classify(raw_message: str) -> MessageType:
    """Classify a raw signal message into its type.

    Uses keyword matching against the cleaned (markdown-stripped) text.
    Rules are ordered from most specific to least specific so that
    unambiguous patterns match first.

    Args:
        raw_message: The raw message text, potentially with Discord formatting.

    Returns:
        The identified MessageType.
    """
    text = _strip_markdown(raw_message).upper()

    # --- Noise (check first — fast reject) ---
    if "@PERP ALERT" in text:
        return MessageType.NOISE

    # CP commentary: weekly reports, daily recaps, market analysis, brief
    # comments. These are informational and must not trigger any action.
    # Checked BEFORE lifecycle keywords because CP sometimes wraps these
    # messages with ticker headers like "TRADE CANCELED <@&NNN>" that would
    # otherwise win the classification.
    if any(marker in text for marker in _NOISE_MARKERS):
        return MessageType.NOISE

    # --- Lifecycle events (specific keywords) ---
    # ORDER_PENDING and TRADE_LIVE must be checked BEFORE SIGNAL_ALERT —
    # their messages can include a "Trading Signal Alert" header preamble
    # that would otherwise win the classification.
    if "ORDER PENDING" in text:
        return MessageType.ORDER_PENDING

    if "TRADE IS LIVE" in text:
        return MessageType.TRADE_LIVE

    if "ALL TAKE-PROFIT TARGETS HIT" in text:
        return MessageType.ALL_TP_HIT

    if re.search(r"TP TARGET \d HIT", text):
        return MessageType.TP_HIT

    if "BREAK EVEN HIT" in text:
        return MessageType.BREAKEVEN

    if "STOP TARGET HIT" in text:
        return MessageType.STOP_HIT

    if "TRADE CLOSED OUT" in text:
        return MessageType.TRADE_CLOSED

    if re.search(r"\bCANCEL", text):
        return MessageType.CANCELED

    # --- Preparation (has "Incoming..." and "Prepare") ---
    if "INCOMING" in text and "PREPARE" in text:
        return MessageType.PREPARATION

    # --- Signal alert: must have structured fields, regardless of header ---
    # CP uses "Trading Signal Alert" as a generic header preamble for prose
    # too, so header presence alone is not enough. A real signal carries
    # ENTRY + SL + TP, full stop.
    has_entry = bool(re.search(r"\bENTRY[:\s]", text))
    has_sl = bool(re.search(r"\bSL[:\s]", text))
    has_tp = bool(re.search(r"\bTP\d", text))
    if has_entry and has_sl and has_tp:
        return MessageType.SIGNAL_ALERT

    # "Trading Signal Alert" header without structured fields is CP prose
    # (commentary, brief comment, market context) — treat as NOISE.
    if "TRADING SIGNAL ALERT" in text:
        return MessageType.NOISE

    # --- Manual update (has a pair/trade # but didn't match above) ---
    if re.search(r"PAIR[:\s]", text) or re.search(r"#\d{3,}", text):
        return MessageType.MANUAL_UPDATE

    return MessageType.NOISE

#!/usr/bin/env python3
"""Test driver — synthetic CP-format signal generator.

Replaces the real Telegram forwarder for the duration of a test run.
Generates N synthetic CP signals over a configurable wall-clock window
and walks each trade through its full lifecycle (signal → trade_live →
TP hits → BE → close / stop / cancel), posting CP-format messages to
the same mirror channel that the real forwarder uses.

The bot can't tell synthetic messages from real ones — they enter the
same channel in the same format. Everything downstream (parser,
pipeline, position manager, Hyperliquid) runs unmodified.

Test trade IDs are in the range [TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX]
(7_000_000 – 7_999_999) so they're trivially distinguishable from real
CP trade IDs (max ~4 digits). See ``--cleanup`` for bulk delete.

Run:

    python3 scripts/test_driver.py                  # uses config/test_driver.yaml
    python3 scripts/test_driver.py --config <path>  # alt config
    python3 scripts/test_driver.py --cleanup        # bulk delete test trades
    python3 scripts/test_driver.py --dry-run        # render scenarios, don't post
    python3 scripts/test_driver.py --top-up-demo    # (Blofin demo) request play money

Exchange-aware (Phase 6.10): the coin pool + prices are drawn from the target
exchange (config `exchange:` key, else config.yaml's exchange.exchange), so a
Blofin run generates Blofin-valid signals. Orphan-order cleanup routes each
user's cancels to their own exchange.

Foreground only — see config/test_driver.example.yaml for tunables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from telethon import TelegramClient

# Make src.* imports work when running as a top-level script
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

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
from src.utils.symbol_mapper import potion_to_blofin, potion_to_hyperliquid

logger = logging.getLogger("test_driver")


# ============================================================================
# Constants
# ============================================================================

# Test trade ID range. Real CP IDs are 4 digits (max observed ~3000), so any
# 7-digit trade_id is guaranteed synthetic. Bulk-delete with `--cleanup` or
# `WHERE trade_id BETWEEN 7_000_000 AND 7_999_999`.
TEST_TRADE_ID_MIN = 7_000_000
TEST_TRADE_ID_MAX = 7_999_999

HL_TESTNET_API = "https://api.hyperliquid-testnet.xyz"
HL_MAINNET_API = "https://api.hyperliquid.xyz"

# Blofin public market hosts. testnet→demo, mainnet→production (same mapping as
# src/orchestrator.py build_exchange_client). The /market/* endpoints are
# unauthenticated, so coin-pool + price planning needs no credentials.
BLOFIN_DEMO_API = "https://demo-trading-openapi.blofin.com"
BLOFIN_PROD_API = "https://openapi.blofin.com"

# Cancel reasons — pulled from real samples so the cancel parser handles them.
_CANCEL_REASONS = (
    "Trade got posted with significant delay in this fast moving market",
    "Didn't meet the requirements",
    "Price moved too fast",
    "TP1 target hit before reaching the entry",
    "Setup invalidated by market structure shift",
)


# ============================================================================
# Message templates
#
# Each template, when filled with valid params, must classify correctly and
# parse without error. Verified by tests/test_test_driver.py round-trips.
# ============================================================================

_SIGNAL_TEMPLATE = """TRADING SIGNAL ALERT

PAIR: {pair} #{trade_id}
({risk} RISK)

TYPE: SWING
SIZE: 1-4%
SIDE: {side}

ENTRY: {entry}
SL: {sl}          ({sl_pct:.2f}%)

TAKE PROFIT TARGETS:

TP1: {tp1}      ({tp1_pct:.2f}%)
TP2: {tp2}      ({tp2_pct:.2f}%)
TP3: {tp3}      ({tp3_pct:.2f}%)

LEVERAGE: {leverage}x

TP1: {tp1_rr:.2f} R:R
TP2: {tp2_rr:.2f} R:R
TP3: {tp3_rr:.2f} R:R

PROTECT YOUR CAPITAL, MANAGE RISK, LETS PRINT!"""

_TRADE_LIVE_TEMPLATE = """LIVE {coin}
**TRADE IS LIVE** ✅

**PAIR:** {pair} #{trade_id}"""

_TP_HIT_TEMPLATE = """**✅ TP TARGET {tp_number} HIT**

**📝PAIR:** {pair} #{trade_id}

**💰PROFIT:** {profit_pct:.2f}% 📈
**⏳PERIOD:** {period_minutes} Minutes"""

_BREAKEVEN_TEMPLATE = """**BREAK EVEN HIT AFTER TP{tp_secured}**

**📝PAIR:** {pair} #{trade_id}

Price has returned to entry after **TP{tp_secured}** was secured. Capital protected."""

_ALL_TP_HIT_TEMPLATE = """**🔥ALL TAKE-PROFIT TARGETS HIT**

**📝PAIR:** {pair} #{trade_id}

**💰PROFIT:** {profit_pct:.2f}% 📈
**⏳PERIOD:** {period_hours} Hours {period_minutes} Minutes"""

_STOP_HIT_TEMPLATE = """STOP TARGET HIT

PAIR: {pair} #{trade_id}

LOSS: -{loss_pct:.2f}%"""

_CANCELED_TEMPLATE = """PAIR: {pair} #{trade_id} CANCELED

{reason}"""


# ============================================================================
# Scenarios
# ============================================================================

@dataclass
class Event:
    """One step in a scenario.

    ``timing_key`` indexes into ``config.timing_sec`` for the delay range
    from the prior event. ``params`` carries event-specific fields like
    ``tp_number`` for tp_hit.
    """
    event_type: str
    timing_key: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    events: list[Event]


SCENARIOS: dict[str, Scenario] = {
    "all_tp_hit": Scenario(
        name="all_tp_hit",
        events=[
            Event("signal_alert",  "_immediate"),
            Event("trade_live",    "signal_to_live"),
            Event("tp_hit",        "live_to_first_tp", {"tp_number": 1}),
            Event("breakeven",     "first_tp_to_breakeven", {"tp_secured": 1}),
            Event("tp_hit",        "between_tps", {"tp_number": 2}),
            Event("all_tp_hit",    "final_event"),
        ],
    ),
    "stop_hit": Scenario(
        name="stop_hit",
        events=[
            Event("signal_alert",  "_immediate"),
            Event("trade_live",    "signal_to_live"),
            Event("stop_hit",      "final_event"),
        ],
    ),
    "tp1_then_stop": Scenario(
        name="tp1_then_stop",
        events=[
            Event("signal_alert",  "_immediate"),
            Event("trade_live",    "signal_to_live"),
            Event("tp_hit",        "live_to_first_tp", {"tp_number": 1}),
            Event("breakeven",     "first_tp_to_breakeven", {"tp_secured": 1}),
            Event("stop_hit",      "final_event"),
        ],
    ),
    "cancel_pending": Scenario(
        name="cancel_pending",
        events=[
            Event("signal_alert",  "_immediate"),
            Event("canceled",      "final_event"),
        ],
    ),
    "cancel_after_fill": Scenario(
        name="cancel_after_fill",
        events=[
            Event("signal_alert",  "_immediate"),
            Event("trade_live",    "signal_to_live"),
            Event("canceled",      "cancel_after_fill"),
        ],
    ),
}


# ============================================================================
# Per-trade context
# ============================================================================

@dataclass
class TradePlan:
    """All the parameterized data for one synthetic trade."""
    trade_id: int
    coin: str            # HL coin name (e.g. "kBONK")
    pair: str            # display pair (e.g. "1000BONK/USDT")
    side: str            # "LONG" or "SHORT"
    risk: str            # "LOW" / "MEDIUM" / "HIGH"
    leverage: int
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    scenario: Scenario
    start_offset_sec: float   # when in the run this trade fires


# ============================================================================
# Price math
# ============================================================================

# Distance-from-entry percentages used to derive SL / TPs. Arbitrary but
# realistic-looking; the bot doesn't validate market-meaningfulness.
_DELTA_SL_PCT = 2.0
_DELTA_TP1_PCT = 0.5
_DELTA_TP2_PCT = 1.0
_DELTA_TP3_PCT = 2.0


def compute_price_levels(side: str, entry: float) -> dict[str, float]:
    """Return SL/TP1/TP2/TP3 prices relative to *entry* for *side*.

    For LONG: SL below, TPs above. For SHORT: flipped.
    """
    if side == "LONG":
        sl = entry * (1 - _DELTA_SL_PCT / 100)
        tp1 = entry * (1 + _DELTA_TP1_PCT / 100)
        tp2 = entry * (1 + _DELTA_TP2_PCT / 100)
        tp3 = entry * (1 + _DELTA_TP3_PCT / 100)
    elif side == "SHORT":
        sl = entry * (1 + _DELTA_SL_PCT / 100)
        tp1 = entry * (1 - _DELTA_TP1_PCT / 100)
        tp2 = entry * (1 - _DELTA_TP2_PCT / 100)
        tp3 = entry * (1 - _DELTA_TP3_PCT / 100)
    else:
        raise ValueError(f"unknown side: {side}")
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}


def _format_price(p: float) -> str:
    """Format a price with enough decimals to be unambiguous, matching
    the precision style of real CP messages."""
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.7f}"


# ============================================================================
# Coin pool
# ============================================================================

_PAIR_RE = re.compile(r"PAIR:?\s*([A-Z0-9]+)/USDT", re.IGNORECASE)


def extract_pairs_from_samples(samples_dir: Path) -> set[str]:
    """Return the set of Potion-format base names seen in the sample corpus."""
    pairs: set[str] = set()
    if not samples_dir.is_dir():
        return pairs
    for f in samples_dir.iterdir():
        if f.suffix != ".txt":
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        for m in _PAIR_RE.finditer(text):
            pairs.add(m.group(1).upper())
    return pairs


def filter_to_hl_universe(
    potion_pairs: set[str],
    hl_asset_meta: dict[str, dict],
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Resolve each Potion pair to an HL coin and filter out unknowns.

    Returns a list of ``(pair_basename, hl_coin)`` tuples for the coins
    the test driver can use. Excluded coins are dropped silently.
    """
    exclude = exclude or set()
    out: list[tuple[str, str]] = []
    for base in sorted(potion_pairs):
        if base in exclude:
            continue
        try:
            hl_coin = potion_to_hyperliquid(f"{base}/USDT", available_coins=hl_asset_meta)
        except Exception:
            continue
        if hl_coin not in hl_asset_meta:
            continue
        out.append((base, hl_coin))
    return out


# ============================================================================
# Hyperliquid public API
# ============================================================================

def fetch_hl_asset_meta(network: str) -> dict[str, dict]:
    """Return the asset metadata dict {coin: {szDecimals, maxLeverage, ...}}."""
    base = HL_TESTNET_API if network == "testnet" else HL_MAINNET_API
    raw = _http_post_json(f"{base}/info", {"type": "meta"})
    universe = raw.get("universe", [])
    return {a["name"]: a for a in universe if "name" in a}


def fetch_hl_mids(network: str) -> dict[str, float]:
    """Return {coin: mid_price} for all HL coins."""
    base = HL_TESTNET_API if network == "testnet" else HL_MAINNET_API
    raw = _http_post_json(f"{base}/info", {"type": "allMids"})
    return {k: float(v) for k, v in raw.items()}


def _http_post_json(url: str, payload: dict) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============================================================================
# Blofin public API (market data — unauthenticated)
# ============================================================================

def _blofin_base(network: str) -> str:
    return BLOFIN_PROD_API if network == "mainnet" else BLOFIN_DEMO_API


def fetch_blofin_instruments(network: str) -> dict[str, dict]:
    """Return {instId: meta} from the public instruments endpoint. Each meta
    carries contractValue/lotSize/minSize/tickSize/maxLeverage."""
    raw = _http_get_json(f"{_blofin_base(network)}/api/v1/market/instruments")
    return {a["instId"]: a for a in (raw.get("data") or []) if a.get("instId")}


def fetch_blofin_mids(network: str) -> dict[str, float]:
    """Return {instId: last_price} from the public tickers endpoint."""
    raw = _http_get_json(f"{_blofin_base(network)}/api/v1/market/tickers")
    out: dict[str, float] = {}
    for t in raw.get("data") or []:
        inst = t.get("instId")
        last = t.get("last")
        if inst and last:
            try:
                out[inst] = float(last)
            except (TypeError, ValueError):
                continue
    return out


def filter_to_blofin_universe(
    potion_pairs: set[str],
    instruments: dict[str, dict],
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Resolve each Potion pair to a Blofin instId and drop unknowns.

    Returns ``(pair_basename, inst_id)`` tuples for instruments the driver
    can use. Mirrors ``filter_to_hl_universe`` for the Blofin universe."""
    exclude = exclude or set()
    out: list[tuple[str, str]] = []
    for base in sorted(potion_pairs):
        if base in exclude:
            continue
        try:
            inst = potion_to_blofin(f"{base}/USDT", available_instruments=instruments)
        except Exception:
            continue
        if inst not in instruments:
            continue
        out.append((base, inst))
    return out


# ============================================================================
# Market-source dispatch (Phase 6.10)
# ============================================================================

def build_market_pool(
    exchange: str,
    network: str,
    samples_dir: Path,
    exclude: set[str],
) -> tuple[list[tuple[str, str]], dict[str, float]]:
    """Return ``(pool, mids)`` for the target exchange.

    ``pool`` is a list of ``(pair_basename, resolved_symbol)`` where
    resolved_symbol is the HL coin or the Blofin instId; ``mids`` maps that
    same resolved_symbol to a current price. Selected by ``exchange`` so the
    synthetic signals reference coins the target user's exchange supports,
    priced from that exchange's own market data (Blofin planning is
    credential-free — the /market endpoints are public)."""
    sample_pairs = extract_pairs_from_samples(samples_dir)
    if exchange == "blofin":
        meta = fetch_blofin_instruments(network)
        mids = fetch_blofin_mids(network)
        pool = filter_to_blofin_universe(sample_pairs, meta, exclude)
    else:
        meta = fetch_hl_asset_meta(network)
        mids = fetch_hl_mids(network)
        pool = filter_to_hl_universe(sample_pairs, meta, exclude)
    pool = [(p, c) for (p, c) in pool if c in mids]
    return pool, mids


# ============================================================================
# Telegram posting
#
# We post via Telethon (Artem's user account), not via the bot's token.
# A bot does NOT receive ``channel_post`` updates for messages it itself
# sent to a channel — Telegram's API silently drops them since the bot
# already knows the message (it has the sendMessage response). Posting
# as a user account makes each test message look identical to a real CP
# message that the production Telethon forwarder delivers.
#
# The real forwarder is stopped during a test run (driver does this on
# startup), so we can safely reuse its existing session file.
# ============================================================================

async def post_to_channel(client: TelegramClient, channel_id: int, text: str):
    """Post *text* to the mirror channel as Artem's user account."""
    return await client.send_message(channel_id, text)


# ============================================================================
# Message rendering
# ============================================================================

def render_signal_alert(plan: TradePlan) -> str:
    """Render the SIGNAL_ALERT message for a trade plan."""
    sl_pct = abs((plan.sl - plan.entry) / plan.entry) * 100 * plan.leverage
    tp1_pct = abs((plan.tp1 - plan.entry) / plan.entry) * 100 * plan.leverage
    tp2_pct = abs((plan.tp2 - plan.entry) / plan.entry) * 100 * plan.leverage
    tp3_pct = abs((plan.tp3 - plan.entry) / plan.entry) * 100 * plan.leverage
    return _SIGNAL_TEMPLATE.format(
        pair=plan.pair,
        trade_id=plan.trade_id,
        risk=plan.risk,
        side=plan.side,
        entry=_format_price(plan.entry),
        sl=_format_price(plan.sl),
        sl_pct=-sl_pct,
        tp1=_format_price(plan.tp1),
        tp1_pct=tp1_pct,
        tp2=_format_price(plan.tp2),
        tp2_pct=tp2_pct,
        tp3=_format_price(plan.tp3),
        tp3_pct=tp3_pct,
        leverage=plan.leverage,
        tp1_rr=tp1_pct / max(sl_pct, 0.01),
        tp2_rr=tp2_pct / max(sl_pct, 0.01),
        tp3_rr=tp3_pct / max(sl_pct, 0.01),
    )


def _move_pct(reference: float, entry: float) -> float:
    """Absolute price-move percentage between *entry* and *reference*.
    The bot's downstream display uses this × leverage to render the
    profit/loss as a real CP would."""
    if entry == 0:
        return 0.0
    return abs(reference - entry) / entry * 100.0


def _tp_profit_pct(plan: "TradePlan", tp_number: int) -> float:
    """Realistic profit% for a TP hit — matches what real CP would show
    given the signal's prices and leverage. CP's percentages are the
    price-move-from-entry × the leverage (i.e. P&L on the collateral).

    Before this, the test driver used ``rng.uniform(10, 50) * tp_number``
    which surfaced as wildly inflated numbers in the bot's notifications
    (e.g. ``+37%`` profit on a $0.05 actual move). Tying the display
    percentage to the actual signal prices keeps the audit log honest
    and matches the user's real HL P&L."""
    tp_price = {1: plan.tp1, 2: plan.tp2, 3: plan.tp3}.get(tp_number, plan.tp1)
    return _move_pct(tp_price, plan.entry) * plan.leverage


def _all_tp_profit_pct(plan: "TradePlan") -> float:
    """Realistic profit% for ALL_TP_HIT — weighted average of the three
    TP profits by the preset's tp_split, then leveraged. The bot writes
    this into ``trades.pnl_pct`` so it has to make sense relative to the
    other test trades and to anything an operator sees in /stats."""
    split = (1 / 3.0, 1 / 3.0, 1 / 3.0)  # even split — close enough for test math
    move = (
        _move_pct(plan.tp1, plan.entry) * split[0]
        + _move_pct(plan.tp2, plan.entry) * split[1]
        + _move_pct(plan.tp3, plan.entry) * split[2]
    )
    return move * plan.leverage


def _stop_loss_pct(plan: "TradePlan") -> float:
    """Realistic loss% for a STOP_HIT — price-move from entry to SL ×
    leverage. Reported as a positive number (the template prefixes the
    minus sign)."""
    return _move_pct(plan.sl, plan.entry) * plan.leverage


def render_event(event_type: str, plan: TradePlan, params: dict, rng: random.Random) -> str:
    """Render a lifecycle event message for a trade plan."""
    if event_type == "signal_alert":
        return render_signal_alert(plan)
    if event_type == "trade_live":
        return _TRADE_LIVE_TEMPLATE.format(coin=plan.coin, pair=plan.pair, trade_id=plan.trade_id)
    if event_type == "tp_hit":
        tp_number = params.get("tp_number", 1)
        profit_pct = _tp_profit_pct(plan, tp_number)
        period = rng.randint(5, 90)
        return _TP_HIT_TEMPLATE.format(
            tp_number=tp_number, pair=plan.pair, trade_id=plan.trade_id,
            profit_pct=profit_pct, period_minutes=period,
        )
    if event_type == "breakeven":
        tp_secured = params.get("tp_secured", 1)
        return _BREAKEVEN_TEMPLATE.format(
            tp_secured=tp_secured, pair=plan.pair, trade_id=plan.trade_id,
        )
    if event_type == "all_tp_hit":
        profit_pct = _all_tp_profit_pct(plan)
        hours = rng.randint(1, 12)
        minutes = rng.randint(0, 59)
        return _ALL_TP_HIT_TEMPLATE.format(
            pair=plan.pair, trade_id=plan.trade_id, profit_pct=profit_pct,
            period_hours=hours, period_minutes=minutes,
        )
    if event_type == "stop_hit":
        loss_pct = _stop_loss_pct(plan)
        return _STOP_HIT_TEMPLATE.format(
            pair=plan.pair, trade_id=plan.trade_id, loss_pct=loss_pct,
        )
    if event_type == "canceled":
        reason = rng.choice(_CANCEL_REASONS)
        return _CANCELED_TEMPLATE.format(
            pair=plan.pair, trade_id=plan.trade_id, reason=reason,
        )
    raise ValueError(f"unknown event_type: {event_type}")


# ============================================================================
# Scenario / schedule planning
# ============================================================================

def generate_start_times(
    rng: random.Random,
    num_trades: int,
    duration_sec: int,
    randomize: bool,
) -> list[float]:
    """Return *num_trades* trade-start offsets (seconds from run start)."""
    if num_trades <= 0:
        return []
    if randomize:
        offsets = [rng.uniform(0, duration_sec) for _ in range(num_trades)]
        return sorted(offsets)
    # Evenly spaced — start at 0, last one just before duration ends
    step = duration_sec / max(num_trades, 1)
    return [step * i for i in range(num_trades)]


def assign_scenarios(
    rng: random.Random,
    scenario_names: list[str],
    num_trades: int,
) -> list[str]:
    """Assign one scenario per trade. Cycle through the list so each is
    used roughly equally; shuffle so order isn't predictable."""
    if not scenario_names:
        raise ValueError("scenarios list is empty")
    base = (scenario_names * ((num_trades // len(scenario_names)) + 1))[:num_trades]
    rng.shuffle(base)
    return base


def sample_timing(
    rng: random.Random,
    timing_cfg: dict[str, list[float]],
    key: str,
) -> float:
    """Sample a uniform delay from the configured [min, max] for *key*."""
    if key == "_immediate":
        return 0.0
    lo, hi = timing_cfg.get(key, [0, 0])
    return rng.uniform(lo, hi)


# ============================================================================
# Trade ID allocation + DB cleanup
# ============================================================================

def get_next_test_trade_id(db_path: Path) -> int:
    """Return the next test trade_id, picking up where prior runs left off."""
    if not db_path.exists():
        return TEST_TRADE_ID_MIN + 1
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT MAX(trade_id) FROM trades WHERE trade_id BETWEEN ? AND ?",
            (TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return TEST_TRADE_ID_MIN + 1
    if row and row[0]:
        return int(row[0]) + 1
    return TEST_TRADE_ID_MIN + 1


def cleanup_test_data(db_path: Path) -> dict[str, int]:
    """Delete all rows in the test trade ID range from trades/orders/trade_events.

    Refuses to run if any process is currently writing to the DB (basic
    safety — checks the WAL mode busy state).
    """
    if not db_path.exists():
        return {"trades": 0, "orders": 0, "trade_events": 0}

    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            r_events = conn.execute(
                "DELETE FROM trade_events WHERE trade_id BETWEEN ? AND ?",
                (TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX),
            )
            n_events = r_events.rowcount
            r_orders = conn.execute(
                "DELETE FROM orders WHERE trade_id BETWEEN ? AND ?",
                (TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX),
            )
            n_orders = r_orders.rowcount
            r_trades = conn.execute(
                "DELETE FROM trades WHERE trade_id BETWEEN ? AND ?",
                (TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX),
            )
            n_trades = r_trades.rowcount
        return {"trades": n_trades, "orders": n_orders, "trade_events": n_events}
    finally:
        conn.close()


def _list_test_orders_with_oid(db_path: Path) -> list[tuple[str, str, str, str]]:
    """Return ``(user_id, oid, coin, order_type)`` for every test order whose
    oid is set in the local DB. Used by ``cancel_orphan_orders`` to pair
    exchange cancel calls with the right user's credentials.

    ``oid`` is returned as a STRING — HL oids are numeric but Blofin
    orderIds/tpslIds are arbitrary strings (orders.oid is TEXT since the
    Phase 6.7 migration). The HL cancel path casts back to int. ``order_type``
    routes Blofin cancels (entry → cancel-order, SL/TP → cancel-tpsl).

    The bot's DB may say these orders are FILLED or CANCELED — that's the
    design-limitation ghost the test driver creates (synthetic CP events don't
    trigger real exchange state changes). They may still be resting on the
    exchange and need an actual cancel call.
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT user_id, oid, coin, order_type
               FROM orders
               WHERE trade_id BETWEEN ? AND ?
                 AND oid IS NOT NULL""",
            (TEST_TRADE_ID_MIN, TEST_TRADE_ID_MAX),
        ).fetchall()
        return [(r[0], str(r[1]), r[2], r[3]) for r in rows]
    finally:
        conn.close()


def cancel_orphan_orders(db_path: Path) -> dict[str, dict[str, int]]:
    """Cancel any test orders still resting on the exchange, per user.

    Walks every test-range order row with an oid (i.e. it reached the exchange
    at some point), groups by user_id, and dispatches the cancel on each user's
    own ``exchange`` (read from their credentials). HL uses
    ``exchange.cancel(coin, int(oid))``; Blofin routes entry orders to
    ``cancel-order`` and SL/TP conditionals to ``cancel-tpsl`` (the order_type
    column drives the split). Already-gone orders are idempotent/benign on both.
    Per-order errors are caught + logged so one failure doesn't block the rest.

    Returns ``{user_id: {"canceled": N, "errors": M}}``.
    """
    from src.state.user_db import UserDatabase

    rows = _list_test_orders_with_oid(db_path)
    if not rows:
        return {}

    by_user: dict[str, list[tuple[str, str, str]]] = {}
    for user_id, oid, coin, order_type in rows:
        by_user.setdefault(user_id, []).append((oid, coin, order_type))

    udb = UserDatabase()
    results: dict[str, dict[str, int]] = {}
    try:
        for user_id, items in by_user.items():
            creds = udb.get_user_credentials_decrypted(user_id)
            if not creds:
                logger.warning(
                    "Skipping cancel for user %s — no credentials in DB", user_id,
                )
                continue
            exchange = (creds.get("exchange") or "hyperliquid").lower()
            try:
                canceled, errors = _cancel_user_orders(exchange, creds, items)
            except Exception:
                logger.exception(
                    "Skipping cancel for user %s — client init failed", user_id,
                )
                continue
            results[user_id] = {"canceled": canceled, "errors": errors}
            logger.info(
                "User %s: canceled %d test orders on %s (%d errors)",
                user_id, canceled, exchange, errors,
            )
    finally:
        udb.close()
    return results


def _cancel_user_orders(
    exchange: str,
    creds: dict,
    items: list[tuple[str, str, str]],
) -> tuple[int, int]:
    """Cancel one user's orphan orders on their exchange. Returns
    ``(canceled, errors)``. Raises only on client construction failure."""
    from src.state.models import OrderType

    canceled = errors = 0
    if exchange == "blofin":
        from src.exchange.blofin import BlofinClient

        network = "production" if creds.get("network") == "mainnet" else "demo"
        client = BlofinClient(
            api_key=creds["account_address"],
            api_secret=creds["api_secret"],
            passphrase=creds.get("passphrase", ""),
            network=network,
        )
        for oid, coin, order_type in items:
            try:
                if order_type == OrderType.ENTRY.value:
                    client.cancel_order(coin, order_id=oid)
                else:
                    client.cancel_tpsl(coin, oid)
                canceled += 1  # 102068 (already gone) returns without raising
            except Exception as e:
                logger.warning(
                    "Blofin cancel failed for %s oid=%s: %s", coin, oid, e,
                )
                errors += 1
        return canceled, errors

    from src.exchange.hyperliquid import HyperliquidClient

    client = HyperliquidClient(
        account_address=creds["account_address"],
        private_key=creds["api_secret"],
        network=creds.get("network", "testnet"),
    )
    for oid, coin, _order_type in items:
        try:
            client.exchange.cancel(coin, int(oid))
            canceled += 1
        except Exception as e:
            # HL returns OK for already-canceled orders; this catch is for
            # real transport / signing failures.
            logger.warning("HL cancel failed for %s oid=%s: %s", coin, oid, e)
            errors += 1
    return canceled, errors


# ============================================================================
# Forwarder management
# ============================================================================

def stop_real_forwarder() -> bool:
    """Best-effort: stop the real Telethon forwarder via launchctl.

    Returns True if the bootout call exits 0 OR the agent wasn't loaded,
    which both mean "forwarder is not running" by the time this returns.
    """
    uid = os.getuid()
    label = f"gui/{uid}/local.potion-perps-forwarder"
    result = subprocess.run(
        ["launchctl", "bootout", label],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        logger.info("Stopped real forwarder (%s)", label)
        return True
    if "Could not find" in (result.stderr or "") or "No such" in (result.stderr or ""):
        logger.info("Real forwarder not currently loaded — nothing to stop")
        return True
    logger.warning(
        "launchctl bootout returned %d: %s", result.returncode, result.stderr.strip(),
    )
    return False


# ============================================================================
# Per-trade lifecycle execution
# ============================================================================

async def run_one_trade(
    plan: TradePlan,
    client: TelegramClient,
    channel_id: int,
    timing_cfg: dict[str, list[float]],
    rng: random.Random,
    dry_run: bool,
) -> None:
    """Walk one trade through its scenario, posting CP-format messages."""
    logger.info(
        "Trade #%d (%s %s %s, scenario=%s, start=+%.0fs)",
        plan.trade_id, plan.pair, plan.side, plan.risk,
        plan.scenario.name, plan.start_offset_sec,
    )
    for ev in plan.scenario.events:
        delay = sample_timing(rng, timing_cfg, ev.timing_key)
        if delay > 0:
            await asyncio.sleep(delay)
        text = render_event(ev.event_type, plan, ev.params, rng)
        if dry_run:
            logger.info(
                "Trade #%d: [DRY] would post %s (%d chars)",
                plan.trade_id, ev.event_type, len(text),
            )
            continue
        try:
            message = await post_to_channel(client, channel_id, text)
            logger.info(
                "Trade #%d: posted %s (msg_id=%s)",
                plan.trade_id, ev.event_type, getattr(message, "id", "?"),
            )
        except Exception as e:
            logger.error(
                "Trade #%d: FAILED to post %s — %s",
                plan.trade_id, ev.event_type, e,
            )
    logger.info("Trade #%d: scenario complete", plan.trade_id)


# ============================================================================
# Orchestration
# ============================================================================

async def run_test(config: dict, db_path: Path, dry_run: bool) -> None:
    cfg = config["test_driver"]
    seed = cfg.get("seed")
    rng = random.Random(seed)

    network = _read_network_from_config()
    # Target exchange: cfg override wins, else config.yaml's exchange.exchange.
    exchange = (cfg.get("exchange") or _read_exchange_from_config()).lower()
    channel_id = int(cfg["signals_channel_id"])

    logger.info(
        "Test run: %d trades over %d min on %s/%s (channel=%d, seed=%s, dry_run=%s)",
        cfg["trade_count"], cfg["duration_minutes"], exchange, network,
        channel_id, seed, dry_run,
    )

    if cfg.get("stop_real_forwarder_on_start", True) and not dry_run:
        stop_real_forwarder()

    # Build the coin pool: samples ∩ target-exchange universe \ excluded.
    logger.info("Fetching %s asset meta + mids (%s)...", exchange, network)
    samples_dir = _REPO_ROOT / cfg["coin_pool_from"]
    exclude = set(cfg.get("exclude_coins", []))
    pool, mids = build_market_pool(exchange, network, samples_dir, exclude)
    if not pool:
        raise SystemExit(
            f"Empty coin pool after filtering — check samples + {exchange} universe"
        )
    logger.info("Coin pool: %d coins (%s ...)", len(pool), [c for _, c in pool[:6]])

    # Schedule + assign scenarios
    num = cfg["trade_count"]
    duration_sec = int(cfg["duration_minutes"]) * 60
    start_times = generate_start_times(rng, num, duration_sec, cfg.get("randomize_start_times", True))
    scenario_names = assign_scenarios(rng, list(cfg["scenarios"]), num)

    side_dist = cfg.get("side_distribution", {"LONG": 0.5, "SHORT": 0.5})
    risk_dist = cfg.get("risk_distribution", {"LOW": 0.4, "MEDIUM": 0.4, "HIGH": 0.2})

    next_id = get_next_test_trade_id(db_path)
    logger.info("Starting trade_id at %d", next_id)

    plans: list[TradePlan] = []
    for i in range(num):
        pair_base, resolved = rng.choice(pool)
        side = _weighted_choice(rng, side_dist)
        risk = _weighted_choice(rng, risk_dist)
        entry = float(mids[resolved])
        levels = compute_price_levels(side, entry)
        # ``coin`` is the LIVE-header label: HL coin name for HL; for Blofin the
        # instId is "BTC-USDT", so use the base symbol there (the pipeline maps
        # the pair → instId itself via potion_to_blofin).
        coin_label = pair_base if exchange == "blofin" else resolved
        plan = TradePlan(
            trade_id=next_id + i,
            coin=coin_label,
            pair=f"{pair_base}/USDT",
            side=side,
            risk=risk,
            leverage=_default_leverage_for_risk(risk),
            entry=entry,
            sl=levels["sl"],
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            scenario=SCENARIOS[scenario_names[i]],
            start_offset_sec=start_times[i],
        )
        plans.append(plan)

    # Print the plan summary upfront
    logger.info("=== Trade plan ===")
    for p in plans:
        logger.info(
            "  +%5.0fs  #%d  %-13s %-5s %-7s lev=%dx scen=%s",
            p.start_offset_sec, p.trade_id, p.pair, p.side, p.risk,
            p.leverage, p.scenario.name,
        )

    if dry_run:
        logger.info("Dry run — not posting. Exiting.")
        return

    timing_cfg = cfg["timing_sec"]

    # Start a Telethon client signed in as the user account (same session
    # file as the real forwarder). Posts via this client look like real
    # CP messages relayed from Artem's TG account — the bot will receive
    # ``channel_post`` updates for them. Posting via the bot's own token
    # silently fails: Telegram doesn't generate channel_post events for a
    # bot's own outgoing messages.
    telethon_cfg = _read_telethon_config()
    session_path = Path(telethon_cfg["session_file"])
    if not session_path.is_absolute():
        session_path = _REPO_ROOT / session_path
    # Telethon appends ``.session`` to the session name internally —
    # the on-disk file is ``<session>.session``. Check for either.
    if not session_path.exists() and not session_path.with_suffix(".session").exists():
        raise SystemExit(
            f"Telethon session not found at {session_path} or "
            f"{session_path.with_suffix('.session')}. Run "
            "`python3 scripts/telethon_forwarder.py` once to log in, "
            "then retry the test driver."
        )

    client = TelegramClient(
        str(session_path),
        telethon_cfg["api_id"],
        telethon_cfg["api_hash"],
    )
    await client.start(phone=telethon_cfg["phone"])
    try:
        me = await client.get_me()
        logger.info(
            "Signed in as %s (id=%d) — posting as user account",
            getattr(me, "first_name", None) or getattr(me, "username", "?"),
            me.id,
        )

        async def _trade_task(plan: TradePlan) -> None:
            await asyncio.sleep(plan.start_offset_sec)
            await run_one_trade(
                plan, client, channel_id, timing_cfg, rng, dry_run=False,
            )

        tasks = [asyncio.create_task(_trade_task(p)) for p in plans]
        logger.info(
            "Run started. Waiting for all %d trades to complete...",
            len(tasks),
        )
        await asyncio.gather(*tasks, return_exceptions=False)
        logger.info("All test trades complete.")
    finally:
        await client.disconnect()
        logger.info("Telethon client disconnected.")


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights.keys())
    vals = list(weights.values())
    return rng.choices(keys, weights=vals, k=1)[0]


def _default_leverage_for_risk(risk: str) -> int:
    # Matches the rough leverage CP uses across the sample corpus
    return {"LOW": 14, "MEDIUM": 18, "HIGH": 25}.get(risk, 14)


# ============================================================================
# Env / config helpers
# ============================================================================

def _load_dotenv() -> None:
    """Populate ``os.environ`` from .env if not already set. Idempotent."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _read_telethon_config() -> dict:
    """Pull TG_API_ID/TG_API_HASH/TG_PHONE/TG_SESSION_FILE from .env or env.

    Mirrors the env contract used by ``scripts/telethon_forwarder.py`` so
    the same .env file works for both. Raises a clean error listing the
    missing vars rather than failing deep inside Telethon's client init.
    """
    _load_dotenv()
    required = ("TG_API_ID", "TG_API_HASH", "TG_PHONE")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"test_driver: missing env vars: {', '.join(missing)}. "
            "These are the same vars used by scripts/telethon_forwarder.py "
            "— see its module docstring for how to set them up."
        )
    try:
        api_id = int(os.environ["TG_API_ID"])
    except ValueError as e:
        raise SystemExit(f"test_driver: TG_API_ID must be an integer: {e}")
    return {
        "api_id": api_id,
        "api_hash": os.environ["TG_API_HASH"],
        "phone": os.environ["TG_PHONE"],
        "session_file": os.environ.get(
            "TG_SESSION_FILE", "data/.telethon_session",
        ),
    }


def _read_network_from_config() -> str:
    """Pull exchange.network from config/config.yaml (testnet/mainnet)."""
    cfg_path = _REPO_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        return "testnet"
    try:
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("exchange", {}).get("network", "testnet")
    except Exception:
        return "testnet"


def _read_exchange_from_config() -> str:
    """Pull exchange.exchange from config/config.yaml (hyperliquid/blofin)."""
    cfg_path = _REPO_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        return "hyperliquid"
    try:
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("exchange", {}).get("exchange", "hyperliquid")
    except Exception:
        return "hyperliquid"


# ============================================================================
# Demo top-up (Phase 6.10) — experimental
# ============================================================================

# Body-shape candidates for demo-apply-money. The public docs are truncated and
# the demo rejected the documented shape ("Parameter toAccount cannot be
# empty"). We try a small matrix and print each raw response so the working
# shape can be confirmed on the live demo. toAccount candidates cover the
# documented futures-account ids. See BLOFIN_INTEGRATION.md § "Demo facts".
_TOPUP_CANDIDATES = [
    {"adjust_type": "1", "to_account": "futures"},
    {"adjust_type": "1", "to_account": "3"},
    {"adjust_type": "1", "to_account": None},
    {"adjust_type": "2", "to_account": "futures"},
]


def top_up_demo(amount: str = "10000") -> None:
    """**Experimental** — request more demo play money and print balances.

    Builds a demo BlofinClient from BLOFIN_API_KEY/SECRET/PASSPHRASE in the
    env, prints the current balance (a confirmed signed read), then attempts
    ``demo-apply-money`` across the candidate body shapes, printing each raw
    response. The demo is pre-funded (~500k USDT) so this is usually
    unnecessary — its real value right now is resolving the exact body shape.
    """
    from src.exchange.blofin import BlofinClient

    _load_dotenv()
    key = os.environ.get("BLOFIN_API_KEY")
    secret = os.environ.get("BLOFIN_API_SECRET")
    passphrase = os.environ.get("BLOFIN_PASSPHRASE")
    if not (key and secret and passphrase):
        raise SystemExit(
            "top-up-demo: set BLOFIN_API_KEY / BLOFIN_API_SECRET / "
            "BLOFIN_PASSPHRASE (demo key) in .env or the environment."
        )

    client = BlofinClient(
        api_key=key, api_secret=secret, passphrase=passphrase, network="demo",
    )
    try:
        bal = client.get_balance()
        logger.info("Demo balance before: available=%s equity=%s",
                    bal.get("available"), bal.get("total_equity"))
    except Exception as e:
        raise SystemExit(f"top-up-demo: balance read failed (bad demo key?): {e}")

    for cand in _TOPUP_CANDIDATES:
        try:
            resp = client.demo_apply_money(
                amount=amount,
                adjust_type=cand["adjust_type"],
                to_account=cand["to_account"],
            )
            code = str(resp.get("code"))
            logger.info("  apply-money %s → code=%s msg=%s", cand, code, resp.get("msg"))
            if code == "0":
                logger.info("  ✓ accepted with %s", cand)
                break
        except Exception as e:
            logger.info("  apply-money %s → error %s", cand, e)
    else:
        logger.warning(
            "Demo top-up not confirmed — none of the candidate shapes returned "
            "code 0. Note the responses above to resolve the body shape.",
        )

    try:
        bal = client.get_balance()
        logger.info("Demo balance after:  available=%s equity=%s",
                    bal.get("available"), bal.get("total_equity"))
    except Exception:
        pass


# ============================================================================
# CLI entrypoint
# ============================================================================

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    )


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config", default="config/test_driver.yaml",
        help="Path to test driver YAML config (default: config/test_driver.yaml)",
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help=f"Delete all rows with trade_id in [{TEST_TRADE_ID_MIN}, {TEST_TRADE_ID_MAX}] and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan but don't post messages or touch the forwarder",
    )
    parser.add_argument(
        "--top-up-demo", action="store_true",
        help="(Blofin demo, experimental) request more demo play money and exit",
    )
    args = parser.parse_args()

    db_path = _REPO_ROOT / "data" / "trades.db"

    if args.top_up_demo:
        top_up_demo()
        return

    if args.cleanup:
        # Cancel orphan exchange orders FIRST — once we delete the DB rows
        # we lose the oid → user_id mapping needed to know which credentials
        # to use for the cancel call. Cancels are idempotent/benign on both
        # exchanges, and each user is routed to their own exchange.
        try:
            cancel_result = cancel_orphan_orders(db_path)
        except Exception:
            logger.exception(
                "Orphan order cleanup failed — proceeding with DB cleanup anyway",
            )
            cancel_result = {}
        for uid, counts in cancel_result.items():
            logger.info(
                "  exchange: user=%s canceled=%d errors=%d",
                uid, counts["canceled"], counts["errors"],
            )

        db_result = cleanup_test_data(db_path)
        logger.info(
            "DB cleanup — deleted %d trades, %d orders, %d events",
            db_result["trades"], db_result["orders"], db_result["trade_events"],
        )
        return

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = _REPO_ROOT / cfg_path
    if not cfg_path.exists():
        # Fall back to the example if config/test_driver.yaml is not set up
        example = _REPO_ROOT / "config" / "test_driver.example.yaml"
        if example.exists():
            logger.info("No %s — using %s", cfg_path, example)
            cfg_path = example
        else:
            raise SystemExit(f"Config not found: {cfg_path}")

    with open(cfg_path) as f:
        config = yaml.safe_load(f) or {}

    try:
        asyncio.run(run_test(config, db_path, dry_run=args.dry_run))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting.")


if __name__ == "__main__":
    main()

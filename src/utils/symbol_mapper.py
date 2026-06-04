"""Pair name to Hyperliquid symbol format mapping.

Potion Perps signals use pairs like 'ZK/USDT' or '1000BONK/USDT'.
Hyperliquid uses coin names like 'ZK' or 'kBONK'.

This module handles:
  1. Explicit overrides (rebrands, name mismatches)
  2. Kilo-prefix conversion (1000X → kX)
  3. Direct 1:1 mapping (ETH/USDT → ETH)
  4. Validation against live exchange metadata
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Potion Perps "1000X" prefix → Hyperliquid "kX" prefix
_KILO_PREFIX = "1000"

# ---------------------------------------------------------------------------
# Explicit overrides: Potion base name → Hyperliquid coin name
# Covers rebrands, naming differences, and special cases.
# ---------------------------------------------------------------------------
_OVERRIDES: dict[str, str] = {
    # Rebrands
    "MATIC": "POL",       # Polygon rebranded MATIC → POL
    "FTM": "S",           # Fantom rebranded to Sonic (S)
    "RNDR": "RENDER",     # Render rebranded RNDR → RENDER (HL uses RENDER)

    # Naming differences between exchanges
    "LUNA2": "LUNA",      # Some signals use LUNA2 for Terra 2.0
    "IOST": "IO",         # Name mismatch
    "SHIB": "kSHIB",      # Bare SHIB → kSHIB on Hyperliquid
    "PEPE": "kPEPE",      # Bare PEPE → kPEPE on Hyperliquid
    "BONK": "kBONK",      # Bare BONK → kBONK on Hyperliquid
    "LUNC": "kLUNC",      # Bare LUNC → kLUNC on Hyperliquid
    "NEIRO": "kNEIRO",    # Bare NEIRO → kNEIRO on Hyperliquid
    "DOGS": "kDOGS",      # Bare DOGS → kDOGS on Hyperliquid
    "JELLYJELLY": "JELLY",  # HL shortened name
}

# ---------------------------------------------------------------------------
# All Hyperliquid-listed coins as of 2025-05 (206 coins).
# Used as fallback validation when live metadata is unavailable.
# ---------------------------------------------------------------------------
HYPERLIQUID_COINS: set[str] = {
    "0G", "2Z", "AAVE", "ACE", "ADA", "AERO", "AI", "AI16Z", "AIXBT",
    "ALGO", "ALT", "ANIME", "APE", "APEX", "APT", "AR", "ARB", "ASTER",
    "ATOM", "AVAX", "AVNT", "AXL", "AXS", "BABY", "BADGER", "BANANA",
    "BERA", "BIGTIME", "BIO", "BLAST", "BLUR", "BLZ", "BNB", "BRETT",
    "BSV", "BTC", "CAKE", "CANTO", "CATI", "CC", "CELO", "CHILLGUY",
    "CHZ", "COMP", "DASH", "DOGE", "DOOD", "DYDX", "DYM", "EIGEN",
    "ENS", "ETC", "ETH", "FARTCOIN", "FET", "FIL", "FOGO", "FRIEND",
    "FTM", "FTT", "FXS", "GALA", "GAS", "GMT", "GOAT", "GRASS",
    "GRIFFAIN", "HBAR", "HEMI", "HMSTR", "HPOS", "HYPE", "HYPER",
    "ICP", "ILV", "IMX", "INIT", "INJ", "IO", "IOTA", "IP", "JELLY",
    "JELLYJELLY", "JOE", "JTO", "JUP", "KAITO", "KAS", "LAUNCHCOIN",
    "LAYER", "LDO", "LINEA", "LISTA", "LIT", "MANTA", "MATIC", "MAV",
    "MAVIA", "ME", "MEGA", "MELANIA", "MEME", "MERL", "MET", "MEW",
    "MINA", "MKR", "MON", "MOODENG", "MORPHO", "MOVE", "NEAR",
    "NEIROETH", "NEO", "NFTI", "NIL", "NOT", "NTRN", "NXPC", "OM",
    "OMNI", "ONDO", "OP", "ORDI", "OX", "PANDORA", "PAXG", "PENDLE",
    "PENGU", "PEOPLE", "PIXEL", "PNUT", "POL", "POLYX", "POPCAT",
    "PROMPT", "PROVE", "PUMP", "PURR", "PYTH", "RENDER", "REQ",
    "RESOLV", "REZ", "RLB", "RNDR", "RSR", "RUNE", "S", "SAGA",
    "SAND", "SCR", "SKR", "SKY", "SNX", "SOL", "SOPH", "SPX",
    "STABLE", "STBL", "STG", "STRAX", "STX", "SUI", "SUPER", "SUSHI",
    "SYRUP", "TAO", "TIA", "TNSR", "TON", "TRB", "TRUMP", "TST",
    "TURBO", "UMA", "UNIBOT", "USTC", "USUAL", "VET", "VINE",
    "VIRTUAL", "VVV", "W", "WCT", "WIF", "WLD", "WLFI", "XAI", "XLM",
    "XMR", "XPL", "YZY", "ZEC", "ZEN", "ZEREBRO", "ZETA", "ZK",
    "ZORA", "ZRO", "kBONK", "kDOGS", "kLUNC", "kNEIRO", "kPEPE",
    "kSHIB",
}

# ---------------------------------------------------------------------------
# Common leverage-trading pairs that Potion signals might reference.
# Maps Potion base name → Hyperliquid coin name for every known match.
# Pairs not on Hyperliquid are intentionally excluded.
# ---------------------------------------------------------------------------
COMMON_PAIRS: dict[str, str] = {
    # --- Top 20 by perps volume ---
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "BNB": "BNB",
    "XRP": "XRP",     # Not on HL currently — will fail validation
    "DOGE": "DOGE",
    "ADA": "ADA",
    "AVAX": "AVAX",
    "LINK": "LINK",   # Not on HL currently
    "DOT": "DOT",     # Not on HL currently
    "MATIC": "POL",   # Rebrand
    "FTM": "S",       # Rebrand → Sonic
    "NEAR": "NEAR",
    "ATOM": "ATOM",
    "APT": "APT",
    "SUI": "SUI",
    "OP": "OP",
    "ARB": "ARB",
    "INJ": "INJ",
    "TIA": "TIA",

    # --- Mid-cap perps (common on signal channels) ---
    "WIF": "WIF",
    "RENDER": "RENDER",
    "RNDR": "RENDER",
    "FET": "FET",
    "FIL": "FIL",
    "IMX": "IMX",
    "STX": "STX",
    "RUNE": "RUNE",
    "AAVE": "AAVE",
    "MKR": "MKR",
    "SNX": "SNX",
    "COMP": "COMP",
    "DYDX": "DYDX",
    "ENS": "ENS",
    "LDO": "LDO",
    "PENDLE": "PENDLE",
    "JTO": "JTO",
    "JUP": "JUP",
    "PYTH": "PYTH",
    "WLD": "WLD",
    "ORDI": "ORDI",
    "TAO": "TAO",
    "KAS": "KAS",
    "SEI": "SEI",     # Not on HL currently
    "ZK": "ZK",
    "ZRO": "ZRO",

    # --- Meme & micro-cap ---
    "BONK": "kBONK",
    "PEPE": "kPEPE",
    "SHIB": "kSHIB",
    "1000BONK": "kBONK",
    "1000PEPE": "kPEPE",
    "1000SHIB": "kSHIB",
    "1000LUNC": "kLUNC",
    "1000NEIRO": "kNEIRO",
    "1000DOGS": "kDOGS",
    "FLOKI": "FLOKI",  # Not on HL currently
    "TURBO": "TURBO",
    "BRETT": "BRETT",
    "POPCAT": "POPCAT",
    "PNUT": "PNUT",
    "FARTCOIN": "FARTCOIN",
    "TRUMP": "TRUMP",
    "MOODENG": "MOODENG",
    "GOAT": "GOAT",
    "MEW": "MEW",
    "NOT": "NOT",

    # --- L1/L2 & infra ---
    "HBAR": "HBAR",
    "VET": "VET",
    "ALGO": "ALGO",
    "ICP": "ICP",
    "XLM": "XLM",
    "ETC": "ETC",
    "IOTA": "IOTA",
    "TON": "TON",
    "MINA": "MINA",
    "CELO": "CELO",

    # --- DeFi / Gaming ---
    "APE": "APE",
    "AXS": "AXS",
    "GALA": "GALA",
    "SAND": "SAND",
    "BLUR": "BLUR",
    "SUSHI": "SUSHI",
    "CAKE": "CAKE",
    "GMX": "GMX",     # Not on HL currently
    "UMA": "UMA",
    "RSR": "RSR",
    "ONDO": "ONDO",

    # --- Hyperliquid-native & newer ---
    "HYPE": "HYPE",
    "PURR": "PURR",
    "ANIME": "ANIME",
    "BERA": "BERA",
    "IP": "IP",
    "KAITO": "KAITO",
    "VIRTUAL": "VIRTUAL",
    "AI16Z": "AI16Z",
    "AIXBT": "AIXBT",
    "GRASS": "GRASS",
    "EIGEN": "EIGEN",
    "MOVE": "MOVE",
    "USUAL": "USUAL",
    "PENGU": "PENGU",

    # --- From our signal samples ---
    "POL": "POL",
    "CRV": "CRV",     # Not on HL currently
    "BCH": "BCH",     # Not on HL currently
}


def potion_to_hyperliquid(pair: str, available_coins: dict[str, Any] | None = None) -> str:
    """Convert a Potion Perps pair to a Hyperliquid coin name.

    Resolution order:
      1. Explicit overrides (_OVERRIDES) — handles rebrands/special names
      2. COMMON_PAIRS lookup — comprehensive pair table
      3. Kilo-prefix rule (1000X → kX)
      4. Direct pass-through (strip /USDT, use base as coin)

    Args:
        pair: Pair string from signal, e.g. 'ZK/USDT', '1000BONK/USDT'.
        available_coins: Optional dict of available coins (from get_asset_meta()
            or get_all_mids()) used to validate the result.

    Returns:
        Hyperliquid coin name, e.g. 'ZK', 'kBONK'.

    Raises:
        ValueError: If the coin is not available on Hyperliquid (when validated).
    """
    base = pair.split("/")[0].strip().upper()

    # 1. Explicit overrides (rebrands, special cases)
    if base in _OVERRIDES:
        coin = _OVERRIDES[base]
    # 2. Common pairs table
    elif base in COMMON_PAIRS:
        coin = COMMON_PAIRS[base]
    # 3. Kilo-prefix: 1000X → kX
    elif base.startswith(_KILO_PREFIX):
        coin = "k" + base[len(_KILO_PREFIX):]
    # 4. Direct pass-through
    else:
        coin = base

    # Validate against live exchange data if provided
    if available_coins is not None and coin not in available_coins:
        raise ValueError(
            f"Coin '{coin}' (from pair '{pair}') not available on Hyperliquid. "
            f"The asset may not be listed or may use a different name."
        )

    return coin


def get_all_mappings() -> dict[str, str]:
    """Return the full mapping table (overrides + common pairs + kilo entries).

    Useful for debugging or displaying supported pairs.
    """
    mappings = dict(COMMON_PAIRS)
    mappings.update(_OVERRIDES)
    return mappings


# ---------------------------------------------------------------------------
# Blofin symbol mapping (Phase 6)
#
# Blofin uses Binance-style instrument IDs: "{BASE}-{QUOTE}" (e.g. BTC-USDT).
# Unlike Hyperliquid it does NOT use the kilo (k) prefix — CP's "1000BONK"
# stays "1000BONK" per Blofin's own listing; there is deliberately no kilo
# transform here. The exact form for each low-cap is verified against
# /market/instruments during the demo soak (Phase 6.10); any mismatch gets
# an override entry below.
#
# This table holds chain-level token REBRANDS only — facts independent of the
# exchange. Exchange-specific naming quirks are added empirically during demo
# validation, not assumed up front (D11: don't bake in unverified mappings).
# ---------------------------------------------------------------------------
_BLOFIN_OVERRIDES: dict[str, str] = {
    "MATIC": "POL",      # Polygon token migration MATIC → POL (chain-level)
    "RNDR": "RENDER",    # Render rebrand RNDR → RENDER (chain-level)
    "FTM": "S",          # Fantom → Sonic (chain-level)
}


def potion_to_blofin(
    pair: str, available_instruments: dict[str, Any] | None = None
) -> str:
    """Convert a Potion Perps pair to a Blofin instrument ID ("BASE-QUOTE").

    Resolution order:
      1. Chain-level rebrand overrides (``_BLOFIN_OVERRIDES``) on the base.
      2. Naive ``"{BASE}-{QUOTE}"`` — Blofin uses Binance-style symbols and
         does NOT apply HL's kilo prefix; ``1000BONK/USDT`` → ``1000BONK-USDT``.
      3. Validate against live exchange instruments if provided.

    Args:
        pair: Pair string from a signal, e.g. ``"BTC/USDT"``, ``"1000BONK/USDT"``.
            A bare base (no ``/``) defaults the quote to ``USDT``.
        available_instruments: Optional dict keyed by instId (from
            ``BlofinClient.get_instruments()``) used to validate the result.

    Returns:
        Blofin instrument ID, e.g. ``"BTC-USDT"``.

    Raises:
        ValueError: If the instrument is not listed on Blofin (when validated).

    The override table is seeded with chain-level rebrands only and is
    expanded empirically during the demo soak (Phase 6.10) — per D11 we
    don't bake in unverified exchange-naming assumptions.
    """
    parts = [p.strip().upper() for p in pair.split("/")]
    base = parts[0]
    quote = parts[1] if len(parts) > 1 and parts[1] else "USDT"

    base = _BLOFIN_OVERRIDES.get(base, base)
    inst_id = f"{base}-{quote}"

    if available_instruments is not None and inst_id not in available_instruments:
        raise ValueError(
            f"Instrument '{inst_id}' (from pair '{pair}') not listed on Blofin. "
            f"The asset may not be available or may use a different symbol "
            f"(add a _BLOFIN_OVERRIDES entry once confirmed on demo)."
        )

    return inst_id

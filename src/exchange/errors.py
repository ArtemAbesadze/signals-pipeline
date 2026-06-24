"""Shared exchange-layer exceptions.

Kept dependency-free (imports nothing from the package) so both the order
builders and the pipeline can import it without circular-import risk.
"""


class InstrumentNotAvailableError(ValueError):
    """The signal's coin isn't listed on the active exchange/venue.

    Subclasses ``ValueError`` so existing ``except (ValueError, KeyError)``
    build-failure handling still catches it — but the distinct type lets the
    pipeline tell "coin not on this venue" apart from a sizing/rounding bug and
    surface it clearly. Real case: a TON-USDT call on the Blofin *demo*, which
    only lists ~88 instruments (TON exists on prod) — #2273, 2026-06-22.
    """

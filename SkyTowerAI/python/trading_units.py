"""Canonical forex and broker unit conversions.

Price pips depend on the quote currency, while broker points depend on the
symbol's configured digits. Keeping those concepts separate prevents the
common 10x sizing/spread error on 2/4-digit feeds.
"""

from __future__ import annotations

import math

from instrument_profiles import profile_for


def normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol).upper() if ch.isalpha())[:6]


def forex_pip_size(symbol: str) -> float:
    """Return the pip price for a symbol.

    Non-forex CFDs (gold, indices) declare their own unit in
    ``instrument_profiles`` — this is the single choke point every server-side
    pip computation goes through (market_context, position_manager, exit
    engine, path recorder, zone/target via pips_to_price), so the profile hook
    lives HERE and nowhere else. Forex pairs have no profile and keep the
    conventional rule: 0.0001, or 0.01 for JPY quotes.
    """
    profile = profile_for(symbol)
    if profile is not None:
        return float(profile.pip_size)
    normalized = normalize_symbol(symbol)
    return 0.01 if len(normalized) >= 6 and normalized[3:6] == "JPY" else 0.0001


def pips_to_price(pips: float, symbol: str) -> float:
    pips = float(pips)
    if not math.isfinite(pips):
        raise ValueError("pips must be finite")
    return pips * forex_pip_size(symbol)


def price_to_pips(price_distance: float, symbol: str) -> float:
    price_distance = float(price_distance)
    if not math.isfinite(price_distance):
        raise ValueError("price distance must be finite")
    return price_distance / forex_pip_size(symbol)


def broker_pip_size(point: float, digits: int) -> float:
    """Return one pip in price units for a broker symbol specification."""
    point = float(point)
    digits = int(digits)
    if not math.isfinite(point) or point <= 0:
        raise ValueError("point must be a positive finite number")
    return point * (10.0 if digits in (3, 5) else 1.0)


def spread_price_to_pips(
    bid: float,
    ask: float,
    point: float,
    digits: int,
) -> float:
    """Convert a bid/ask price difference to pips without assuming digits."""
    bid = float(bid)
    ask = float(ask)
    if not math.isfinite(bid) or not math.isfinite(ask) or ask < bid:
        raise ValueError("bid/ask must be finite and ask must not be below bid")
    return (ask - bid) / broker_pip_size(point, digits)

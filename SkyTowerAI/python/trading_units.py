"""Canonical forex and broker unit conversions.

Price pips depend on the quote currency, while broker points depend on the
symbol's configured digits. Keeping those concepts separate prevents the
common 10x sizing/spread error on 2/4-digit feeds.
"""

from __future__ import annotations

import math


def normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol).upper() if ch.isalpha())[:6]


def forex_pip_size(symbol: str) -> float:
    """Return the conventional pip price for a six-letter FX symbol."""
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

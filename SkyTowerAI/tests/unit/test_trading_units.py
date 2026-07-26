import math

import pytest

from trading_units import (
    broker_pip_size,
    forex_pip_size,
    normalize_symbol,
    pips_to_price,
    price_to_pips,
    spread_price_to_pips,
)


@pytest.mark.parametrize(
    ("symbol", "normalized", "pip"),
    [
        ("EURUSD", "EURUSD", 0.0001),
        ("gbpjpy.pro", "GBPJPY", 0.01),
        ("USD/JPY", "USDJPY", 0.01),
        ("JPYUSD.a", "JPYUSD", 0.0001),
    ],
)
def test_forex_pip_size_uses_quote_currency(symbol, normalized, pip):
    assert normalize_symbol(symbol) == normalized
    assert forex_pip_size(symbol) == pip


@pytest.mark.parametrize(
    ("symbol", "pips"),
    [("EURUSD", 12.5), ("USDJPY", 12.5), ("GBPJPY.pro", -3.0)],
)
def test_price_pip_round_trip(symbol, pips):
    assert price_to_pips(pips_to_price(pips, symbol), symbol) == pytest.approx(
        pips
    )


@pytest.mark.parametrize(
    ("point", "digits", "expected"),
    [
        (0.00001, 5, 0.0001),
        (0.0001, 4, 0.0001),
        (0.001, 3, 0.01),
        (0.01, 2, 0.01),
    ],
)
def test_broker_pip_size_supports_all_fx_digit_formats(
    point, digits, expected
):
    assert broker_pip_size(point, digits) == expected


@pytest.mark.parametrize(
    ("bid", "ask", "point", "digits", "expected"),
    [
        (1.10000, 1.10025, 0.00001, 5, 2.5),
        (1.1000, 1.1002, 0.0001, 4, 2.0),
        (150.000, 150.025, 0.001, 3, 2.5),
        (150.00, 150.02, 0.01, 2, 2.0),
    ],
)
def test_spread_price_to_pips_does_not_assume_ten_points(
    bid, ask, point, digits, expected
):
    assert spread_price_to_pips(bid, ask, point, digits) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    "args",
    [
        (1.2, 1.1, 0.00001, 5),
        (math.nan, 1.1, 0.00001, 5),
        (1.1, math.inf, 0.00001, 5),
        (1.1, 1.2, 0.0, 5),
    ],
)
def test_invalid_quote_or_point_is_rejected(args):
    with pytest.raises(ValueError):
        spread_price_to_pips(*args)

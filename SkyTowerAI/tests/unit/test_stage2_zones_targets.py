import pytest

from target_calculator import TargetCalculator
from zone_analyzer import (
    Zone,
    ZoneAnalysisResult,
    ZoneStrength,
    ZoneType,
)


def test_jpy_zone_size_serializes_as_one_pip():
    zone = Zone(
        zone_type=ZoneType.LIQUIDITY_HIGH,
        price_high=150.01,
        price_low=150.00,
        creation_time=1,
        symbol="USDJPY",
    )
    assert zone.size_pips == pytest.approx(1.0)
    assert zone.to_dict()["size_pips"] == 1.0


@pytest.mark.parametrize(
    ("symbol", "entry", "pip"),
    [("EURUSD", 1.1000, 0.0001), ("USDJPY", 150.00, 0.01)],
)
def test_default_target_distances_match_across_quote_currencies(
    symbol, entry, pip
):
    analysis = ZoneAnalysisResult(symbol=symbol, current_price=entry)
    targets = TargetCalculator().calculate(analysis, "BUY")
    assert targets.tp1 == pytest.approx(entry + 40 * pip)
    assert targets.sl == pytest.approx(entry - 30 * pip)
    assert targets.tp1_pips == 40
    assert targets.sl_pips == 30


def test_jpy_zone_target_distance_uses_jpy_pips():
    zone = Zone(
        zone_type=ZoneType.LIQUIDITY_HIGH,
        price_high=150.21,
        price_low=150.19,
        creation_time=1,
        strength=ZoneStrength.STRONG,
        symbol="USDJPY",
    )
    analysis = ZoneAnalysisResult(
        symbol="USDJPY",
        current_price=150.00,
        liquidity_above=[zone],
    )
    targets = TargetCalculator().calculate(analysis, "BUY")
    assert targets.tp1 == pytest.approx(150.20)
    assert targets.tp1_pips == pytest.approx(20.0)

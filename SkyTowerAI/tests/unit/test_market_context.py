"""
Unit tests for market_context.py — trend/ATR/range math and zone summary.
"""

import pytest
from datetime import datetime, timedelta

from market_context import build_market_context, pip_size, normalize_pair, _trend, _atr_pips


def make_bars(closes, spread=0.0005):
    """Bars from a close series; high/low straddle the close."""
    bars = []
    for i, c in enumerate(closes):
        bars.append({
            "time": i,
            "open": c - 0.0001,
            "high": c + spread,
            "low": c - spread,
            "close": c,
        })
    return bars


def rising(n=30, start=1.0800, step=0.0005):
    return make_bars([start + i * step for i in range(n)])


def falling(n=30, start=1.0800, step=0.0005):
    return make_bars([start - i * step for i in range(n)])


class TestPipSize:
    def test_standard_pair(self):
        assert pip_size("EURUSD") == 0.0001

    def test_jpy_pair(self):
        assert pip_size("USDJPY") == 0.01
        assert pip_size("gbpjpy.pro") == 0.01


class TestNormalizePair:
    def test_broker_suffixes_stripped(self):
        assert normalize_pair("EURUSD.pro") == "EURUSD"
        assert normalize_pair("USDCAD_SB") == "USDCAD"
        assert normalize_pair("GBPUSD.a") == "GBPUSD"

    def test_slash_and_case(self):
        assert normalize_pair("usd/cad") == "USDCAD"
        assert normalize_pair("USD/CAD") == "USDCAD"

    def test_bare_pair_unchanged(self):
        assert normalize_pair("NZDUSD") == "NZDUSD"

    def test_empty(self):
        assert normalize_pair("") == ""
        assert normalize_pair(None) == ""


class TestZoneSanity:
    def test_cross_pair_levels_rejected(self):
        """Levels from a different price scale must not produce absurd pip distances."""
        bars = make_bars([1.2700 + i * 0.0002 for i in range(30)])
        zones = {"direction_bias": "bullish", "zone_bias": 0.5,
                 "nearest_resistance": 0.6200, "nearest_support": 0.6000}  # NZDUSD scale
        ctx = build_market_context({"M5": bars}, "GBPUSD", zones=zones)
        assert "resistance_distance_pips" not in ctx["zones"]
        assert "support_distance_pips" not in ctx["zones"]

    def test_plausible_levels_kept(self):
        bars = make_bars([1.0850] * 30)
        zones = {"direction_bias": "bullish", "zone_bias": 0.5,
                 "nearest_resistance": 1.0900, "nearest_support": 1.0800}
        ctx = build_market_context({"M5": bars}, "EURUSD", zones=zones)
        assert ctx["zones"]["resistance_distance_pips"] == pytest.approx(50, abs=6)
        assert ctx["zones"]["support_distance_pips"] == pytest.approx(50, abs=6)


class TestTrend:
    def test_uptrend(self):
        assert _trend(rising()) == "UP"

    def test_downtrend(self):
        assert _trend(falling()) == "DOWN"

    def test_flat_is_sideways_or_weak(self):
        flat = make_bars([1.0800] * 30)
        assert _trend(flat) in ("SIDEWAYS", "UP_WEAK", "DOWN_WEAK")

    def test_too_few_bars(self):
        assert _trend(rising(5)) == "UNKNOWN"


class TestATR:
    def test_atr_positive(self):
        atr = _atr_pips(rising(), "EURUSD")
        assert atr is not None and atr > 0

    def test_atr_needs_enough_bars(self):
        assert _atr_pips(rising(10), "EURUSD") is None

    def test_atr_scales_with_range(self):
        narrow = make_bars([1.08] * 30, spread=0.0002)
        wide = make_bars([1.08] * 30, spread=0.0010)
        assert _atr_pips(wide, "EURUSD") > _atr_pips(narrow, "EURUSD")


class TestEnrichedContext:
    def test_rsi_drift_candles_present(self):
        ctx = build_market_context({"M5": rising(60)}, "EURUSD", spread_points=42)
        assert 0 <= ctx["rsi14"]["M5"] <= 100
        assert ctx["rsi14"]["M5"] > 70  # steadily rising series is overbought
        assert ctx["drift_pips"]["last_30min"] > 0
        assert ctx["drift_pips"]["last_60min"] > ctx["drift_pips"]["last_30min"]
        assert len(ctx["candles"]["M5"]) == 36
        assert "/" in ctx["candles"]["M5"][0]  # O/H/L/C format
        assert ctx["current_spread_pips"] == 4.2

    def test_falling_market_rsi_low_drift_negative(self):
        ctx = build_market_context({"M5": falling(60)}, "EURUSD")
        assert ctx["rsi14"]["M5"] < 30
        assert ctx["drift_pips"]["last_30min"] < 0

    def test_short_series_omits_optional_fields(self):
        ctx = build_market_context({"H1": rising(5)}, "EURUSD")
        assert "drift_pips" not in ctx          # drift needs M5
        assert "current_spread_pips" not in ctx  # no spread given
        assert len(ctx["candles"]["H1"]) == 5


class TestBuildMarketContext:
    def test_none_when_no_data(self):
        assert build_market_context({}, "EURUSD") is None
        assert build_market_context({"M5": []}, "EURUSD") is None

    def test_full_context(self):
        ctx = build_market_context(
            {"M5": rising(60), "M15": rising(40), "H1": rising(48)},
            "EURUSD",
        )
        assert ctx["pair"] == "EURUSD"
        assert ctx["trend"]["H1"] == "UP"
        assert ctx["atr_pips"]["M5"] > 0
        assert ctx["bars_analyzed"] == {"M5": 60, "M15": 40, "H1": 48}
        assert 0 <= ctx["range_position_pct"] <= 100
        assert ctx["distance_to_recent_high_pips"] >= 0
        assert ctx["distance_to_recent_low_pips"] >= 0
        # rising market → price near top of range
        assert ctx["range_position_pct"] > 80

    def test_last_price_from_finest_timeframe(self):
        m5 = rising(30, start=1.0900)
        h1 = rising(30, start=1.0700)
        ctx = build_market_context({"M5": m5, "H1": h1}, "EURUSD")
        assert ctx["last_price"] == m5[-1]["close"]

    def test_partial_timeframes_ok(self):
        ctx = build_market_context({"H1": rising(48)}, "EURUSD")
        assert "H1" in ctx["trend"]
        assert "M5" not in ctx["trend"]

    def test_zones_summary(self):
        zones = {
            "direction_bias": "bullish",
            "zone_bias": 0.65,
            "nearest_resistance": 1.1000,
            "nearest_support": 1.0800,
        }
        ctx = build_market_context({"M5": rising(30, start=1.0850)}, "EURUSD", zones=zones)
        z = ctx["zones"]
        assert z["direction_bias"] == "bullish"
        assert z["resistance_distance_pips"] > 0
        assert z["support_distance_pips"] > 0

    def test_zones_only_no_ohlc(self):
        zones = {"direction_bias": "bearish", "zone_bias": -0.5, "current_price": 1.09,
                 "nearest_resistance": 1.10, "nearest_support": 1.08}
        ctx = build_market_context({}, "EURUSD", zones=zones)
        assert ctx is not None
        assert ctx["zones"]["direction_bias"] == "bearish"
        assert "trend" not in ctx

    def test_data_age(self):
        old = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        ctx = build_market_context({"M5": rising(30)}, "EURUSD", registered_at=old)
        assert 19 <= ctx["data_age_minutes"] <= 21

    def test_jpy_pip_math(self):
        bars = make_bars([150.00 + i * 0.05 for i in range(30)], spread=0.05)
        ctx = build_market_context({"M5": bars}, "USDJPY")
        # 30 bars * 0.05 range ≈ 145 pips of range, distances must be in pips not price
        assert ctx["distance_to_recent_low_pips"] > 100

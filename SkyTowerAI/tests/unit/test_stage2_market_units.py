import pytest

from market_context import build_market_context, summarize_pair_brief


def _bars(price=1.1, count=30):
    return [
        {
            "time": i,
            "open": price,
            "high": price + 0.0002,
            "low": price - 0.0002,
            "close": price,
        }
        for i in range(count)
    ]


def test_explicit_spread_pips_wins_over_legacy_points():
    context = build_market_context(
        {"M5": _bars()},
        "EURUSD",
        spread_points=42,
        spread_pips=2.0,
    )
    assert context["current_spread_pips"] == 2.0


def test_invalid_explicit_spread_does_not_masquerade_as_legacy_quote():
    context = build_market_context(
        {"M5": _bars()},
        "EURUSD",
        spread_points=42,
        spread_pips=float("nan"),
    )
    assert "current_spread_pips" not in context


def test_sibling_brief_uses_explicit_spread_pips():
    brief = summarize_pair_brief(
        {"M5": _bars()},
        "EURUSD",
        "EUR",
        spread_points=42,
        spread_pips=2.0,
    )
    assert "spread 2.0 pips" in brief
    assert "spread 4.2 pips" not in brief

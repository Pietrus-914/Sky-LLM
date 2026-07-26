"""Pins for the pre-live review fixes (2026-07-26).

Each test corresponds to a confirmed finding from the multi-agent review:
sentiment quote-side inversion, fabricated sentiment sources, inverse-event
forecast semantics, COT cross-contract contamination, TE naive datetimes,
broker-time open_time epochs, and guardrails surviving persist failures.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python")
)

from sentiment_analyzer import SentimentAggregator, SentimentData
from position_manager import PositionManager
from position_store import PositionStoreError
from timeutil import utcnow


def _sd(pair, signal):
    return SentimentData(
        pair=pair,
        source="myfxbook",
        timestamp=datetime.now(),
        long_percent=70.0,
        short_percent=30.0,
        net_sentiment=40.0,
        signal=signal,
        confidence=0.5,
    )


def _aggregator_with(cache):
    agg = SentimentAggregator()
    agg._cache = cache
    agg._cache_time = datetime.now()
    return agg


class TestSentimentQuoteSide:
    """Pair-level CONTRARIAN_LONG/SHORT must be inverted for currencies on
    the QUOTE side before they become a currency-level bias."""

    def test_long_usdcad_is_bearish_cad_and_bullish_usd(self):
        agg = _aggregator_with({"USDCAD": _sd("USDCAD", "CONTRARIAN_LONG")})
        assert agg.get_currency_sentiment("CAD")["signal"] == "BEARISH"
        assert agg.get_currency_sentiment("USD")["signal"] == "BULLISH"

    def test_short_nzdusd_is_bearish_nzd(self):
        agg = _aggregator_with({"NZDUSD": _sd("NZDUSD", "CONTRARIAN_SHORT")})
        assert agg.get_currency_sentiment("NZD")["signal"] == "BEARISH"
        assert agg.get_currency_sentiment("USD")["signal"] == "BULLISH"

    def test_cad_quote_side_votes_agree_across_pairs(self):
        # Retail long every CAD-quoted pair = retail SHORT CAD everywhere;
        # the contrarian currency read must be uniformly BEARISH CAD.
        agg = _aggregator_with({
            "USDCAD": _sd("USDCAD", "CONTRARIAN_LONG"),
            "AUDCAD": _sd("AUDCAD", "CONTRARIAN_LONG"),
            "GBPCAD": _sd("GBPCAD", "CONTRARIAN_LONG"),
        })
        result = agg.get_currency_sentiment("CAD")
        assert result["signal"] == "BEARISH"
        assert result["bearish_count"] == 3
        assert result["bullish_count"] == 0

    def test_unrelated_pairs_do_not_vote(self):
        agg = _aggregator_with({"EURJPY": _sd("EURJPY", "CONTRARIAN_LONG")})
        assert agg.get_currency_sentiment("CAD")["signal"] == "NO_DATA"


class TestSentimentHonestSources:
    """No fabricated inputs: no TradingView technicals dressed as retail,
    no hardcoded simulated fallback — empty sources mean NO_DATA."""

    def test_fabricated_sources_removed(self):
        names = {type(s).__name__ for s in SentimentAggregator().sources}
        assert "TradingViewTechnical" not in names
        assert "SimulatedSentiment" not in names

    def test_empty_sources_yield_no_data(self, monkeypatch):
        agg = SentimentAggregator()
        for source in agg.sources:
            monkeypatch.setattr(
                type(source), "fetch_sentiment", lambda self: {}
            )
        assert agg.get_all_sentiment() == {}
        result = agg.get_currency_sentiment("NZD")
        assert result["signal"] == "NO_DATA"
        assert result["pairs_analyzed"] == 0


class TestLowerIsBetterEvents:
    """Higher unemployment/claims is currency-NEGATIVE: the forecast
    comparison label must be swapped for inverse indicators."""

    @pytest.fixture
    def engine(self):
        from llm_decision_engine import LLMDecisionEngine
        return LLMDecisionEngine()

    def test_rising_claims_is_deterioration(self, engine):
        assert engine._compare_values(
            "235K", "220K", "Unemployment Claims") == "DETERIORATION"

    def test_falling_unemployment_rate_is_improvement(self, engine):
        assert engine._compare_values(
            "4.1%", "4.3%", "Unemployment Rate") == "IMPROVEMENT"

    def test_normal_event_keeps_plain_semantics(self, engine):
        assert engine._compare_values("0.4%", "0.2%", "CPI m/m") == "IMPROVEMENT"
        assert engine._compare_values("0.1%", "0.2%", "Retail Sales") == "DETERIORATION"

    def test_unchanged_and_unknown_paths(self, engine):
        assert engine._compare_values(
            "220K", "220K", "Unemployment Claims") == "UNCHANGED"
        assert engine._compare_values("", "220K", "Unemployment Claims") == "UNKNOWN"


class TestCotCrossContractFilter:
    """'%BRITISH POUND%' also matched the EUR/GBP cross-rate contract,
    interleaving sign-inverted rows into the GBP series."""

    def test_cross_contract_rows_are_dropped(self, monkeypatch):
        import cot_analyzer as cot

        rows = []
        for week in ("2026-07-21", "2026-07-14"):
            rows.append({
                "market_and_exchange_names":
                    "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
                "report_date_as_yyyy_mm_dd": week,
                "noncomm_positions_long_all": "64084",
                "noncomm_positions_short_all": "122135",
            })
            rows.append({
                "market_and_exchange_names":
                    "EURO FX/BRITISH POUND XRATE - CHICAGO MERCANTILE EXCHANGE",
                "report_date_as_yyyy_mm_dd": week,
                "noncomm_positions_long_all": "900",
                "noncomm_positions_short_all": "600",
            })

        captured = {}

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return rows

        def _fake_get(url, params=None, timeout=None):
            captured["where"] = params["$where"]
            return _FakeResponse()

        monkeypatch.setattr(cot.requests, "get", _fake_get)
        df = cot.COTDataFetcher().fetch_cot_data("GBP")

        assert "like 'BRITISH POUND - %'" in captured["where"]
        assert len(df) == 2
        assert all(df["market_and_exchange_names"].str.startswith(
            "BRITISH POUND -"))


class TestTradingEconomicsNaiveDatetimes:
    """A tz-less TE 'Date' must come back UTC-aware — a naive value raises
    TypeError in the aggregator and kills every calendar call."""

    def test_naive_te_date_is_localized(self, real_calendar_fetch, monkeypatch):
        import calendar_fetcher as cf

        class _FakeResponse:
            status_code = 200

            def json(self):
                return [{
                    "Date": "2026-07-27T13:30:00",
                    "Country": "United States",
                    "Importance": 3,
                    "Event": "CPI m/m",
                }]

        monkeypatch.setattr(
            cf.requests, "get",
            lambda *args, **kwargs: _FakeResponse(),
        )
        events = cf.TradingEconomicsCalendar().fetch_events()
        assert events, "TE parse should yield the fake event"
        assert events[0].datetime_utc.tzinfo is not None


class TestOpenTimeSanity:
    """A broker-time epoch mislabeled as UTC lands hours in the future and
    would make minutes_open negative for the whole trade."""

    def _snapshot(self, open_time):
        return {
            "reconcile": True,
            "ticket": "111",
            "position_id": "111",
            "symbol": "GBPUSD",
            "direction": "BUY",
            "entry_price": 1.28452,
            "lots": 0.5,
            "remaining_lots": 0.5,
            "max_loss_usd": 100.0,
            "open_time": open_time,
        }

    def test_future_epoch_falls_back_to_now(self):
        pm = PositionManager(exit_engine=None)
        broker_epoch = int(
            (datetime.now(timezone.utc) + timedelta(hours=3)).timestamp()
        )
        pm.update_position(self._snapshot(broker_epoch))
        minutes_open = (utcnow() - pm.position.open_time).total_seconds() / 60
        assert minutes_open >= 0
        assert minutes_open < 5

    def test_past_epoch_is_used_verbatim(self):
        pm = PositionManager(exit_engine=None)
        opened = datetime.now(timezone.utc) - timedelta(minutes=12)
        pm.update_position(self._snapshot(int(opened.timestamp())))
        minutes_open = (utcnow() - pm.position.open_time).total_seconds() / 60
        assert 11 <= minutes_open <= 13


class TestGuardrailsSurvivePersistFailure:
    """A failing snapshot store must not turn every report into HOLD —
    the in-memory guardrails still protect the live position."""

    def test_max_loss_guardrail_fires_with_broken_store(self, tmp_path):
        pm = PositionManager(
            exit_engine=None, state_file=str(tmp_path / "active.json")
        )
        report = {
            "reconcile": True,
            "ticket": "222",
            "position_id": "222",
            "symbol": "GBPUSD",
            "direction": "BUY",
            "entry_price": 1.28452,
            "lots": 0.5,
            "remaining_lots": 0.5,
            "max_loss_usd": 100.0,
            "profit_usd": -150.0,
        }

        def _broken_save(*args, **kwargs):
            raise PositionStoreError("disk full")

        pm.position_store.save = _broken_save
        result = pm.update_position(report)

        assert pm.position is not None
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"

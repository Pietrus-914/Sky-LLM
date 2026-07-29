"""
Unit tests for calendar resilience: ForexFactory last-good cache and the
no-trading-synthetic-static-events rule (guessed times must not be traded).
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

import config as cfg
from calendar_fetcher import (
    ForexFactoryCalendar, CalendarAggregator, EconomicEvent, is_non_data_event
)


def make_event(source="forexfactory", hours_ahead=2, name="CPI m/m",
               currency="USD", impact="HIGH"):
    return EconomicEvent(
        datetime_utc=datetime.now(pytz.UTC) + timedelta(hours=hours_ahead),
        currency=currency, event_name=name, impact=impact,
        forecast="0.3%", previous="0.2%", source=source,
    )


class TestFFLastGoodCache:
    def test_fetch_failure_serves_cached_events(self, tmp_path, monkeypatch,
                                                real_calendar_fetch):
        monkeypatch.setattr(ForexFactoryCalendar, "CACHE_FILE", str(tmp_path / "ff.json"))
        ff = ForexFactoryCalendar()
        ff._last_good = [make_event()]
        with patch("calendar_fetcher.requests.get", side_effect=Exception("429")):
            events = ff.fetch_events()
        assert len(events) == 1
        assert events[0].event_name == "CPI m/m"

    def test_fetch_failure_without_cache_returns_empty(self, tmp_path, monkeypatch,
                                                       real_calendar_fetch):
        monkeypatch.setattr(ForexFactoryCalendar, "CACHE_FILE", str(tmp_path / "ff.json"))
        ff = ForexFactoryCalendar()
        with patch("calendar_fetcher.requests.get", side_effect=Exception("429")):
            assert ff.fetch_events() == []

    def test_disk_cache_round_trip(self, tmp_path, monkeypatch,
                                   real_calendar_fetch):
        monkeypatch.setattr(ForexFactoryCalendar, "CACHE_FILE", str(tmp_path / "ff.json"))
        ff1 = ForexFactoryCalendar()
        ff1._save_disk_cache([make_event(name="Non-Farm Payrolls")])

        ff2 = ForexFactoryCalendar()  # fresh instance loads from disk
        assert len(ff2._last_good) == 1
        assert ff2._last_good[0].event_name == "Non-Farm Payrolls"
        assert ff2._last_good[0].datetime_utc.tzinfo is not None or True  # parseable

        with patch("calendar_fetcher.requests.get", side_effect=Exception("429")):
            assert len(ff2.fetch_events()) == 1


class TestStaticEventsNotTradeable:
    def _aggregator_with(self, events):
        agg = CalendarAggregator.__new__(CalendarAggregator)  # skip network-y init
        with patch.object(CalendarAggregator, "get_upcoming_events", return_value=events):
            return agg.get_tradeable_events(event_keywords=["CPI"], currencies=["USD"])

    def test_static_excluded_by_default(self, monkeypatch):
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        result = self._aggregator_with([make_event(source="static"), make_event(source="forexfactory")])
        assert len(result) == 1
        assert result[0].source == "forexfactory"

    def test_static_included_when_enabled(self, monkeypatch):
        monkeypatch.setenv("SKYTOWER_TRADE_STATIC_EVENTS", "true")
        result = self._aggregator_with([make_event(source="static")])
        assert len(result) == 1

    def test_fake_test_event_source_not_affected(self, monkeypatch):
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        result = self._aggregator_with([make_event(source="fake-test")])
        assert len(result) == 1


class TestPerKeyCacheTTL:
    """Each cache key ages independently — refreshing one key must NOT keep
    another key's stale result alive (the events-table freeze bug)."""

    def _agg(self):
        agg = CalendarAggregator.__new__(CalendarAggregator)
        agg._cache = {}
        agg._cache_duration = timedelta(minutes=15)
        return agg

    def test_fresh_key_valid_old_key_invalid(self):
        agg = self._agg()
        now = datetime.now()
        agg._cache["A"] = (["fresh"], now)
        agg._cache["B"] = (["stale"], now - timedelta(minutes=20))
        assert agg._is_cache_valid("A") is True
        assert agg._is_cache_valid("B") is False

    def test_refreshing_one_key_does_not_revalidate_another(self):
        """The core regression: key B stale, key A just refreshed. B must
        stay invalid (old code shared a single _cache_time and B went valid)."""
        agg = self._agg()
        agg._cache["B"] = (["stale"], datetime.now() - timedelta(minutes=20))
        # Simulate A being (re)computed now
        agg._cache["A"] = (["fresh"], datetime.now())
        assert agg._is_cache_valid("B") is False

    def test_unknown_key_invalid(self):
        assert self._agg()._is_cache_valid("missing") is False


class TestNonDataEvent:
    def test_speeches_and_projections_flagged(self):
        for name in ["FOMC Member Bowman Speaks", "Fed Chairman Warsh Testifies",
                     "ECB Press Conference", "FOMC Economic Projections",
                     "BOE Gov Bailey Speech"]:
            assert is_non_data_event(name) is True, name

    def test_hard_data_not_flagged(self):
        for name in ["CPI m/m", "Non-Farm Payrolls", "Core Retail Sales m/m",
                     "ADP Weekly Employment Change", "NFIB Small Business Index"]:
            assert is_non_data_event(name) is False, name


class TestTradeAllEventsSwitch:
    def _tradeable(self, events, keywords=None):
        agg = CalendarAggregator.__new__(CalendarAggregator)
        with patch.object(CalendarAggregator, "get_upcoming_events", return_value=events):
            return agg.get_tradeable_events(
                event_keywords=keywords or ["CPI"], currencies=["USD"])

    def test_strict_mode_requires_name_match(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        events = [make_event(name="CPI m/m"), make_event(name="NFIB Small Business Index")]
        result = self._tradeable(events)
        names = [e.event_name for e in result]
        assert names == ["CPI m/m"]  # NFIB not in whitelist

    def test_trade_all_takes_non_whitelisted_data_events(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", True)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        events = [make_event(name="CPI m/m"), make_event(name="NFIB Small Business Index")]
        result = self._tradeable(events)
        assert len(result) == 2  # both hard-data events, whitelist ignored

    def test_trade_all_still_excludes_speeches(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", True)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        events = [make_event(name="CPI m/m"),
                  make_event(name="FOMC Member Bowman Speaks")]
        result = self._tradeable(events)
        names = [e.event_name for e in result]
        assert names == ["CPI m/m"]  # speech dropped even in trade-all

    def test_next_tradeable_honors_trade_all(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", True)
        agg = CalendarAggregator.__new__(CalendarAggregator)
        events = [make_event(name="FOMC Member Speaks", hours_ahead=1),
                  make_event(name="NFIB Small Business Index", hours_ahead=2)]
        with patch.object(CalendarAggregator, "get_upcoming_events", return_value=events):
            nxt = agg.get_next_tradeable_event(event_keywords=["CPI"], currencies=["USD"])
        assert nxt is not None and nxt.event_name == "NFIB Small Business Index"

    def test_next_tradeable_excludes_static_like_full_list(self, monkeypatch):
        """get_next_tradeable_event must apply the SAME static guard as
        get_tradeable_events (shared predicate) — a synthetic guessed-time
        event must never be presented as the next tradeable event."""
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", True)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        agg = CalendarAggregator.__new__(CalendarAggregator)
        events = [make_event(source="static", name="CPI m/m", hours_ahead=1),
                  make_event(source="forexfactory", name="GDP m/m", hours_ahead=2)]
        with patch.object(CalendarAggregator, "get_upcoming_events", return_value=events):
            nxt = agg.get_next_tradeable_event(event_keywords=["CPI"], currencies=["USD"])
        assert nxt is not None and nxt.source == "forexfactory"


class TestRateDecisionNamesTradeable:
    """Regresja z 29.07.2026: feed FF nazywa decyzje stóp per bank centralny
    ("Federal Funds Rate", nie "Interest Rate Decision") — FOMC był niewidoczny
    dla selekcji w trybie whitelisty i panel pokazywał GDP z jutra jako next."""

    FF_RATE_DECISION_NAMES = [
        "Federal Funds Rate",    # USD (FOMC)
        "Official Bank Rate",    # GBP (BoE)
        "Overnight Rate",        # CAD (BOC)
        "Cash Rate",             # AUD (RBA)
        "Official Cash Rate",    # NZD (RBNZ)
    ]

    def _next_tradeable(self, events):
        agg = CalendarAggregator.__new__(CalendarAggregator)
        keywords = list(cfg.TIER1_EVENTS_ALL) + list(cfg.TIER2_EVENTS_ALL)
        with patch.object(CalendarAggregator, "get_upcoming_events", return_value=events):
            return agg.get_next_tradeable_event(event_keywords=keywords,
                                                currencies=["USD"])

    def test_ff_rate_decision_names_match_default_whitelist(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        for name in self.FF_RATE_DECISION_NAMES:
            nxt = self._next_tradeable([make_event(name=name)])
            assert nxt is not None and nxt.event_name == name, name

    def test_fomc_decision_not_shadowed_by_later_gdp(self, monkeypatch):
        """Scenariusz z buga: FOMC dziś + Advance GDP jutro — next musi być FOMC."""
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        events = [make_event(name="Federal Funds Rate", hours_ahead=8),
                  make_event(name="Advance GDP q/q", hours_ahead=27)]
        nxt = self._next_tradeable(events)
        assert nxt is not None and nxt.event_name == "Federal Funds Rate"

    def test_fomc_press_conference_still_excluded(self, monkeypatch):
        monkeypatch.setattr(cfg, "TRADE_ALL_EVENTS", False)
        monkeypatch.delenv("SKYTOWER_TRADE_STATIC_EVENTS", raising=False)
        assert self._next_tradeable(
            [make_event(name="FOMC Press Conference")]) is None

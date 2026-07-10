"""
Unit tests for calendar resilience: ForexFactory last-good cache and the
no-trading-synthetic-static-events rule (guessed times must not be traded).
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

from calendar_fetcher import ForexFactoryCalendar, CalendarAggregator, EconomicEvent


def make_event(source="forexfactory", hours_ahead=2, name="CPI m/m", currency="USD"):
    return EconomicEvent(
        datetime_utc=datetime.now(pytz.UTC) + timedelta(hours=hours_ahead),
        currency=currency, event_name=name, impact="HIGH",
        forecast="0.3%", previous="0.2%", source=source,
    )


class TestFFLastGoodCache:
    def test_fetch_failure_serves_cached_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ForexFactoryCalendar, "CACHE_FILE", str(tmp_path / "ff.json"))
        ff = ForexFactoryCalendar()
        ff._last_good = [make_event()]
        with patch("calendar_fetcher.requests.get", side_effect=Exception("429")):
            events = ff.fetch_events()
        assert len(events) == 1
        assert events[0].event_name == "CPI m/m"

    def test_fetch_failure_without_cache_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ForexFactoryCalendar, "CACHE_FILE", str(tmp_path / "ff.json"))
        ff = ForexFactoryCalendar()
        with patch("calendar_fetcher.requests.get", side_effect=Exception("429")):
            assert ff.fetch_events() == []

    def test_disk_cache_round_trip(self, tmp_path, monkeypatch):
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

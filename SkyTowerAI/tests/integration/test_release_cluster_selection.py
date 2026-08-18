"""
Server-level pin: one same-minute release costs ONE analysis.

Reproduces the 17.08.2026 CAD CPI release against the real selection code
(_get_next_unanalyzed_events -> _select_release_group -> _mark_event_analyzed)
and asserts the three properties the serial behaviour violated:
  * the dominant (HIGH headline) is what gets analyzed and labels the trade;
  * every sibling is retired in the same pass, so the next 15 s scan does NOT
    buy another panel for the same price path;
  * a SKIP on the headline ends the release (it used to just hand the budget
    to the next sibling, whose panel then ran against a 10 s deadline).
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import config
import server
from calendar_fetcher import EconomicEvent
from timeutil import utcnow


def cad_cpi_release(seconds_ahead=100):
    """The real cluster, in ForexFactory feed order (impact-descending)."""
    when = utcnow() + timedelta(seconds=seconds_ahead)
    when = when.replace(second=0, microsecond=0)
    spec = [("CPI m/m", "HIGH"), ("Median CPI y/y", "HIGH"),
            ("Trimmed CPI y/y", "HIGH"), ("Common CPI y/y", "MEDIUM")]
    return [EconomicEvent(datetime_utc=when, currency="CAD", event_name=name,
                          impact=impact, forecast="0.4%", previous="-0.4%",
                          source="ff")
            for name, impact in spec]


@pytest.fixture
def clean_state():
    saved = dict(server.analyzed_events)
    server.analyzed_events.clear()
    yield
    server.analyzed_events.clear()
    server.analyzed_events.update(saved)


def select_once(events):
    """The updater's PHASE 1 for one scan (mirrors background_decision_updater)."""
    preload = config.TRADING_CONFIG["preload_seconds"]
    candidates = [e for e in events if not server._is_event_analyzed(e)]
    for event in candidates:
        until = (event.datetime_utc - utcnow()).total_seconds()
        if until < -30:
            continue
        if 0 < until <= preload:
            dominant, shadowed = server._select_release_group(candidates, event)
            for sibling in shadowed:
                server._mark_event_analyzed(sibling, "co-released")
            return dominant, shadowed
        if until > preload:
            break
    return None, []


class TestOneAnalysisPerRelease:
    def test_dominant_is_analyzed_and_siblings_retired(self, clean_state):
        events = cad_cpi_release()

        dominant, shadowed = select_once(events)

        assert dominant.event_name == "CPI m/m"
        assert dominant.impact == "HIGH"
        assert sorted(s.event_name for s in shadowed) == [
            "Common CPI y/y", "Median CPI y/y", "Trimmed CPI y/y"]
        for sibling in shadowed:
            assert server._is_event_analyzed(sibling)
        # the dominant itself is NOT pre-marked — its own verdict decides
        assert not server._is_event_analyzed(dominant)

    def test_skip_on_the_headline_ends_the_release(self, clean_state):
        """The production failure mode: SKIP used to release the budget to the
        next sibling, so one CAD CPI print could buy four panels."""
        events = cad_cpi_release()

        dominant, _ = select_once(events)
        server._mark_event_analyzed(dominant)      # panel said SKIP

        again, _ = select_once(events)
        assert again is None, f"second panel would have run on {again}"

    def test_a_trade_on_the_headline_also_ends_the_release(self, clean_state):
        events = cad_cpi_release()
        dominant, _ = select_once(events)
        # trade opened -> the open handler marks the decision's event
        server.analyzed_events[server._analyzed_event_key(dominant)] = utcnow()

        again, _ = select_once(events)
        assert again is None

    def test_a_different_currency_in_the_same_minute_is_untouched(self, clean_state):
        events = cad_cpi_release()
        usd = EconomicEvent(datetime_utc=events[0].datetime_utc, currency="USD",
                            event_name="Retail Sales m/m", impact="HIGH",
                            forecast="0.3%", previous="0.1%", source="ff")

        dominant, shadowed = select_once(events + [usd])

        assert dominant.currency == "CAD"
        assert usd not in shadowed
        assert not server._is_event_analyzed(usd)

    def test_a_later_release_of_the_same_currency_is_untouched(self, clean_state):
        events = cad_cpi_release()
        later = EconomicEvent(
            datetime_utc=events[0].datetime_utc + timedelta(minutes=15),
            currency="CAD", event_name="Ivey PMI", impact="MEDIUM",
            forecast="55.0", previous="56.2", source="ff")

        _, shadowed = select_once(events + [later])

        assert later not in shadowed
        assert not server._is_event_analyzed(later)

    def test_solitary_event_behaves_exactly_as_before(self, clean_state):
        solo = EconomicEvent(
            datetime_utc=(utcnow() + timedelta(seconds=100)).replace(
                second=0, microsecond=0),
            currency="GBP", event_name="Official Bank Rate", impact="HIGH",
            forecast="3.75%", previous="3.75%", source="ff")

        dominant, shadowed = select_once([solo])

        assert dominant is solo
        assert shadowed == []
        assert not server._is_event_analyzed(solo)


class TestCoReleaseContext:
    def test_the_model_is_told_about_the_siblings(self):
        """A decision made on the headline alone cannot see a contradicting
        sibling — the tape prices the combined surprise."""
        import event_cluster
        events = cad_cpi_release()
        brief = event_cluster.co_release_brief(events[0], events)
        assert [b["name"] for b in brief] == [
            "Median CPI y/y", "Trimmed CPI y/y", "Common CPI y/y"]
        assert all(b["forecast"] and b["previous"] for b in brief)

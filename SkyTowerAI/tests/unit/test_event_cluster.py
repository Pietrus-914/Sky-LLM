"""
Same-minute release clusters: ONE analysis, ONE label, siblings in the prompt.

Production evidence (17.08.2026, CAD CPI at 12:30 UTC): CPI m/m (HIGH),
Median CPI y/y (HIGH), Trimmed CPI y/y (HIGH) and Common CPI y/y (MEDIUM)
printed together. The updater analyzed them SERIALLY — first-in-feed-order,
mark that one analyzed on SKIP, next scan takes the next sibling — so one
release bought up to four panels, each starting later against the panel's
max(T-20s, 10s) deadline, and the executed trade was filed under the weakest
member ("Common CPI y/y") whose playbook and reaction history nobody looks up.
The prompt never mentioned that the HIGH headline printed in the same second.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import event_cluster
from calendar_fetcher import EconomicEvent


def ev(name, impact="HIGH", currency="CAD", minute=30, forecast="0.4%",
       previous="-0.4%"):
    return EconomicEvent(
        datetime_utc=datetime(2026, 8, 17, 12, minute),
        currency=currency, event_name=name, impact=impact,
        forecast=forecast, previous=previous, source="ff")


# The real 17.08.2026 CAD release, in ForexFactory feed order
CAD_CPI_CLUSTER = [
    ev("CPI m/m", "HIGH"),
    ev("Median CPI y/y", "HIGH"),
    ev("Trimmed CPI y/y", "HIGH"),
    ev("Common CPI y/y", "MEDIUM"),
]


class TestDominance:
    def test_headline_wins_over_its_siblings(self):
        dominant, shadowed = event_cluster.pick_dominant(CAD_CPI_CLUSTER)
        assert dominant.event_name == "CPI m/m"
        assert [s.event_name for s in shadowed] == [
            "Median CPI y/y", "Trimmed CPI y/y", "Common CPI y/y"]

    @pytest.mark.parametrize("rotation", range(4))
    def test_feed_order_cannot_change_the_label(self, rotation):
        """The old bug was ordering-dependent: whichever sibling the feed
        listed first (and survived the SKIPs) became the trade's name."""
        rotated = CAD_CPI_CLUSTER[rotation:] + CAD_CPI_CLUSTER[:rotation]
        dominant, _ = event_cluster.pick_dominant(rotated)
        assert dominant.event_name == "CPI m/m"

    def test_impact_outranks_family(self):
        dominant, _ = event_cluster.pick_dominant(
            [ev("Retail Sales", "MEDIUM"), ev("Trade Balance", "HIGH")])
        assert dominant.event_name == "Trade Balance"

    def test_rate_decision_owns_its_minute(self):
        dominant, _ = event_cluster.pick_dominant(
            [ev("CPI m/m", "HIGH", currency="USD"),
             ev("Federal Funds Rate", "HIGH", currency="USD")])
        assert dominant.event_name == "Federal Funds Rate"

    def test_modified_variant_yields_to_the_plain_one(self):
        dominant, _ = event_cluster.pick_dominant(
            [ev("Core CPI m/m", "HIGH"), ev("CPI m/m", "HIGH")])
        assert dominant.event_name == "CPI m/m"

    def test_nfp_owns_the_payroll_minute(self):
        dominant, shadowed = event_cluster.pick_dominant([
            ev("Average Hourly Earnings m/m", "HIGH", currency="USD"),
            ev("Non-Farm Employment Change", "HIGH", currency="USD"),
            ev("Unemployment Rate", "HIGH", currency="USD"),
        ])
        assert dominant.event_name == "Non-Farm Employment Change"
        assert len(shadowed) == 2

    def test_empty_group(self):
        assert event_cluster.pick_dominant([]) == (None, [])


class TestGrouping:
    def test_only_same_currency_and_minute(self):
        events = CAD_CPI_CLUSTER + [
            ev("Housing Starts", "MEDIUM", currency="USD"),   # same minute
            ev("Retail Sales", "HIGH", minute=45),            # same currency
        ]
        group = event_cluster.same_release(events, CAD_CPI_CLUSTER[0])
        assert len(group) == 4
        assert all(e.currency == "CAD" for e in group)
        assert all(e.datetime_utc.minute == 30 for e in group)

    def test_reference_is_included_even_when_alone(self):
        solo = ev("GDP m/m", "HIGH")
        assert event_cluster.same_release([solo], solo) == [solo]

    def test_release_key_is_minute_precision(self):
        a = ev("CPI m/m")
        b = ev("Median CPI y/y")
        assert event_cluster.release_key(a) == event_cluster.release_key(b)
        assert event_cluster.release_key(a) != event_cluster.release_key(
            ev("CPI m/m", minute=31))


class TestCoReleaseBrief:
    def test_excludes_the_dominant_and_carries_the_numbers(self):
        brief = event_cluster.co_release_brief(CAD_CPI_CLUSTER[0], CAD_CPI_CLUSTER)
        assert [b["name"] for b in brief] == [
            "Median CPI y/y", "Trimmed CPI y/y", "Common CPI y/y"]
        assert brief[0]["impact"] == "HIGH"
        assert brief[0]["forecast"] == "0.4%"
        assert brief[0]["previous"] == "-0.4%"

    def test_ignores_other_minutes_and_currencies(self):
        candidates = CAD_CPI_CLUSTER + [
            ev("Housing Starts", "MEDIUM", currency="USD"),
            ev("Retail Sales", "HIGH", minute=45)]
        names = [b["name"] for b in
                 event_cluster.co_release_brief(CAD_CPI_CLUSTER[0], candidates)]
        assert "Housing Starts" not in names
        assert "Retail Sales" not in names

    def test_duplicate_names_are_collapsed(self):
        """Two feeds can report the same print; the model must see it once."""
        dupes = [CAD_CPI_CLUSTER[0], ev("Median CPI y/y", "HIGH"),
                 ev("Median CPI y/y", "HIGH")]
        brief = event_cluster.co_release_brief(dupes[0], dupes)
        assert len(brief) == 1

    def test_capped(self):
        many = [ev(f"Filler {i}", "MEDIUM") for i in range(20)]
        brief = event_cluster.co_release_brief(CAD_CPI_CLUSTER[0],
                                               CAD_CPI_CLUSTER + many)
        assert len(brief) == 6

    def test_no_dominant(self):
        assert event_cluster.co_release_brief(None, CAD_CPI_CLUSTER) == []


class TestPromptBlock:
    """The block must appear ONLY for a cluster — a solitary event's prompt
    stays byte-for-byte what it was (prompt_version pins the instructions,
    not which optional data sections happen to exist)."""

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SKYTOWER_OPENROUTER_KEY", "")
        from llm_decision_engine import LLMDecisionEngine
        eng = LLMDecisionEngine.__new__(LLMDecisionEngine)
        return eng

    def _ctx(self, co_released=None):
        ctx = {
            "event": {"name": "CPI m/m", "currency": "CAD",
                      "datetime": "2026-08-17T12:30:00", "impact": "HIGH",
                      "forecast": "0.4%", "previous": "-0.4%"},
            "cot_analysis": {}, "sentiment_analysis": {},
            "forecast_info": {"forecast_vs_previous": "HIGHER"},
            "suggested_pair": "USDCAD", "market_context": None,
        }
        if co_released:
            ctx["co_released"] = co_released
        return ctx

    def test_absent_without_siblings(self, engine):
        prompt = engine._entry_prompt(self._ctx())
        assert "CO-RELEASED" not in prompt

    def test_solitary_prompt_is_unchanged_by_the_feature(self, engine):
        """Byte-for-byte: the empty block must not even add whitespace."""
        with_key = engine._entry_prompt(self._ctx(co_released=[]))
        without_key = engine._entry_prompt(self._ctx())
        assert with_key == without_key

    def test_siblings_reach_the_model_with_their_numbers(self, engine):
        brief = event_cluster.co_release_brief(CAD_CPI_CLUSTER[0], CAD_CPI_CLUSTER)
        prompt = engine._entry_prompt(self._ctx(co_released=brief))
        assert "CO-RELEASED AT THE SAME MINUTE" in prompt
        assert "Median CPI y/y" in prompt
        assert "Common CPI y/y" in prompt
        # and it sits with the event, before the analysis inputs
        assert prompt.index("CO-RELEASED") < prompt.index("COT (INSTITUTIONAL)")

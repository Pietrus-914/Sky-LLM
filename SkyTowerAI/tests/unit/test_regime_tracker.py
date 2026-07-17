"""
Unit tests for the automatic monetary-policy regime tracker:
deterministic classification from recorded rate decisions, hold-streak and
stale-move decay, seeding semantics, manual override, scan dedup and the
LLM adjudication hook (mocked — no network in tests).
"""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from timeutil import utcnow
from regime_tracker import RegimeTracker, is_rate_decision


def path_record(currency="NZD", actual="2.50%", previous="2.25%",
                name="Official Cash Rate", when=None, **overrides):
    when = when or utcnow()
    rec = {
        "test": False,
        "event_key": f"{currency}|{name.lower()}|{when.isoformat()[:16]}",
        "event_name": name,
        "currency": currency,
        "event_time": when.isoformat(),
        "actual": actual,
        "forecast": previous,
        "previous": previous,
        "pair": f"{currency}USD",
        "move_1min_pips": 10.0,
        "move_5min_pips": 18.0,
        "move_30min_pips": 25.0,
    }
    rec.update(overrides)
    return rec


@pytest.fixture
def tracker(tmp_path):
    return RegimeTracker(state_file=str(tmp_path / "regimes.json"),
                         llm_enabled=False)


class TestRateDecisionDetection:
    def test_matches_rate_decision_names(self):
        for name in ("Official Cash Rate", "Federal Funds Rate",
                     "BoE Official Bank Rate", "Overnight Rate",
                     "Interest Rate Decision", "Main Refinancing Rate"):
            assert is_rate_decision(name), name

    def test_ignores_data_events(self):
        for name in ("CPI m/m", "Non-Farm Payrolls", "Employment Change",
                     "Fed Chair Speaks"):
            assert not is_rate_decision(name), name


class TestDeterministic:
    def test_hike_sets_hiking(self, tracker):
        assert tracker.scan([path_record(actual="2.50%", previous="2.25%")]) == 1
        assert tracker.get("NZD") == "hiking"

    def test_cut_sets_cutting(self, tracker):
        tracker.scan([path_record(actual="2.00%", previous="2.25%")])
        assert tracker.get("NZD") == "cutting"

    def test_single_hold_keeps_cycle(self, tracker):
        now = utcnow()
        tracker.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        tracker.scan([path_record(actual="2.50%", previous="2.50%",
                                  when=now + timedelta(days=42))])
        # LLM disabled -> deterministic: one pause within a live cycle
        assert tracker.get("NZD") == "hiking"

    def test_two_holds_decay_to_hold(self, tracker):
        now = utcnow()
        tracker.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        tracker.scan([path_record(actual="2.50%", previous="2.50%",
                                  when=now + timedelta(days=42))])
        tracker.scan([path_record(actual="2.50%", previous="2.50%",
                                  when=now + timedelta(days=84))])
        assert tracker.get("NZD") == "hold"

    def test_stale_move_decays_to_hold(self, tracker):
        old = utcnow() - timedelta(days=300)
        tracker.scan([path_record(actual="2.50%", previous="2.25%", when=old)])
        # hike observed but 300 days ago -> cycle expired
        assert tracker.get("NZD") == "hold"

    def test_missing_previous_uses_last_known_rate(self, tracker):
        now = utcnow()
        tracker.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        tracker.scan([path_record(actual="2.75%", previous=None,
                                  when=now + timedelta(days=42))])
        assert tracker.get("NZD") == "hiking"
        assert tracker.all()["NZD"]["rate"] == pytest.approx(2.75)


class TestScanHygiene:
    def test_dedup_by_event_key(self, tracker):
        rec = path_record()
        assert tracker.scan([rec]) == 1
        assert tracker.scan([rec]) == 0          # same event, e.g. second pair
        assert tracker.scan([dict(rec, pair="USDCAD")]) == 0

    def test_skips_test_and_unfilled_records(self, tracker):
        assert tracker.scan([path_record(test=True)]) == 0
        assert tracker.scan([path_record(actual=None)]) == 0
        assert tracker.scan([path_record(name="CPI m/m")]) == 0
        assert tracker.get("NZD") is None


class TestSeedAndOverride:
    def test_seed_applies_only_to_unknown(self, tmp_path):
        state = str(tmp_path / "regimes.json")
        t1 = RegimeTracker(state_file=state, seed={"USD": "hiking"},
                           llm_enabled=False)
        assert t1.get("USD") == "hiking"
        t1.scan([path_record(currency="USD", name="Federal Funds Rate",
                             actual="3.25%", previous="3.75%")])  # cut observed
        assert t1.get("USD") == "cutting"

        # Restart with the same seed: observed state must NOT be re-seeded
        t2 = RegimeTracker(state_file=state, seed={"USD": "hiking", "CHF": "hold"},
                           llm_enabled=False)
        assert t2.get("USD") == "cutting"
        assert t2.get("CHF") == "hold"   # new currency seeded

    def test_manual_override_until_next_decision(self, tracker):
        now = utcnow()
        tracker.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        assert tracker.get("NZD") == "hiking"

        tracker.set_manual("NZD", "hold")
        assert tracker.get("NZD") == "hold"
        assert tracker.all()["NZD"]["source"] == "manual"

        # Next observed decision recomputes and outranks the override
        tracker.scan([path_record(actual="2.75%", previous="2.50%",
                                  when=now + timedelta(days=42))])
        assert tracker.get("NZD") == "hiking"

    def test_manual_rejects_bad_value(self, tracker):
        with pytest.raises(ValueError):
            tracker.set_manual("NZD", "dovish")


class TestLlmAdjudication:
    def test_llm_resolves_ambiguous_hold(self, tmp_path, monkeypatch):
        t = RegimeTracker(state_file=str(tmp_path / "r.json"), llm_enabled=True)
        monkeypatch.setattr(t, "_llm_adjudicate",
                            lambda *a, **k: {"regime": "hold",
                                             "rationale": "end of cycle"})
        now = utcnow()
        t.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        assert t.get("NZD") == "hiking"           # a hike never asks the LLM
        t.scan([path_record(actual="2.50%", previous="2.50%",
                            when=now + timedelta(days=42))])
        assert t.get("NZD") == "hold"             # LLM verdict applied
        assert t.all()["NZD"]["source"] == "llm"

    def test_llm_cannot_invent_opposite_direction(self, tmp_path, monkeypatch):
        t = RegimeTracker(state_file=str(tmp_path / "r.json"), llm_enabled=True)
        monkeypatch.setattr(t, "_llm_adjudicate",
                            lambda *a, **k: {"regime": "cutting",
                                             "rationale": "nonsense"})
        now = utcnow()
        t.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        t.scan([path_record(actual="2.50%", previous="2.50%",
                            when=now + timedelta(days=42))])
        # verdict outside {deterministic, hold} is ignored -> deterministic
        assert t.get("NZD") == "hiking"
        assert t.all()["NZD"]["source"] == "rule"

    def test_llm_failure_falls_back_to_rule(self, tmp_path, monkeypatch):
        t = RegimeTracker(state_file=str(tmp_path / "r.json"), llm_enabled=True)
        monkeypatch.setattr(t, "_llm_adjudicate", lambda *a, **k: None)
        now = utcnow()
        t.scan([path_record(actual="2.50%", previous="2.25%", when=now)])
        t.scan([path_record(actual="2.50%", previous="2.50%",
                            when=now + timedelta(days=42))])
        assert t.get("NZD") == "hiking"

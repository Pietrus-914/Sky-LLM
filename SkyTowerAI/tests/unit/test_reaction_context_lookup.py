"""
Unit tests for the server-side forecast/previous resolution on event reactions.

Background: the EA has no calendar and never sends forecast/previous/actual, so
every reaction row ever written carried nulls and a permanent
"surprise: UNKNOWN". The server DOES know both numbers at decision time — these
tests pin the plumbing that carries them into the record, the attempt cap that
stops an unresolvable row from driving the calendar fetch loop forever, and the
numeric revision check that the newly-populated 'previous' switches on.
"""

import json

import pytest

from event_reaction_history import (
    BACKFILL_MAX_ATTEMPTS,
    EventReactionHistory,
    apply_release_to_record,
)
from calendar_fetcher import EconomicEvent


def make_reaction(**overrides):
    defaults = dict(
        event_name="Cash Rate",
        currency="AUD",
        event_time="2026-08-11T04:30:00",
        pair="AUDUSD",
        decision_id="dec-1",
        price_at_event=0.70604,
        price_after_1min=0.70500,
        price_after_5min=0.70453,
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture
def store(tmp_path):
    return EventReactionHistory(log_dir=str(tmp_path))


class TestRecordContextLookup:
    def test_forecast_previous_filled_from_lookup(self, store):
        store.set_event_lookup(lambda *_a: {
            "forecast": "4.35%", "previous": "4.35%", "source": "decision"})
        entry = store.record(make_reaction())
        assert entry["forecast"] == "4.35%"
        assert entry["previous"] == "4.35%"
        assert entry["context_source"] == "decision"

    def test_surprise_stays_unknown_without_actual(self, store):
        """The A/B boundary: forecast+previous alone must not change what the
        prompt shows, and the prompt's detail clause is gated on surprise."""
        store.set_event_lookup(lambda *_a: {"forecast": "0.5%", "source": "decision"})
        entry = store.record(make_reaction())
        assert entry["surprise"] == "UNKNOWN"
        assert entry["surprise_magnitude"] is None

    def test_surprise_computed_when_lookup_has_actual(self, store):
        store.set_event_lookup(lambda *_a: {
            "forecast": "0.3%", "actual": "0.5%", "source": "path_record"})
        entry = store.record(make_reaction())
        assert entry["surprise"] == "BEAT"
        assert entry["surprise_magnitude"] == pytest.approx(0.2)

    def test_ea_value_wins_over_lookup(self, store):
        store.set_event_lookup(lambda *_a: {"forecast": "server", "source": "decision"})
        entry = store.record(make_reaction(forecast="from-ea"))
        assert entry["forecast"] == "from-ea"
        assert entry["context_source"] == "ea"

    def test_unwired_lookup_records_nulls_not_an_error(self, store):
        entry = store.record(make_reaction())
        assert entry["forecast"] is None
        assert entry["context_source"] is None

    def test_raising_lookup_never_loses_the_reaction(self, store):
        def boom(*_a):
            raise RuntimeError("decision context unreadable")

        store.set_event_lookup(boom)
        entry = store.record(make_reaction())
        assert entry["move_5min_pips"] is not None
        assert entry["forecast"] is None

    def test_lookup_receives_normalized_arguments(self, store):
        seen = {}

        def spy(decision_id, currency, event_name, event_minute):
            seen.update(decision_id=decision_id, currency=currency,
                        event_name=event_name, event_minute=event_minute)
            return {}

        store.set_event_lookup(spy)
        store.record(make_reaction(currency="aud"))
        assert seen == {"decision_id": "dec-1", "currency": "AUD",
                        "event_name": "Cash Rate",
                        "event_minute": "2026-08-11T04:30"}

    def test_record_is_persisted_with_new_fields(self, store, tmp_path):
        store.set_event_lookup(lambda *_a: {"forecast": "1.0", "source": "schedule"})
        store.record(make_reaction())
        line = (tmp_path / "event_reactions.jsonl").read_text(encoding="utf-8").strip()
        assert json.loads(line)["context_source"] == "schedule"


class TestBackfillAttemptCap:
    def test_unmatched_row_accumulates_attempts(self, store):
        store.record(make_reaction())
        store.backfill_actuals([])
        assert store._records[0]["backfill_attempts"] == 1
        assert store.last_backfill_stats["unmatched"] == 1

    def test_attempts_persist_across_reload(self, store, tmp_path):
        store.record(make_reaction())
        store.backfill_actuals([])
        reloaded = EventReactionHistory(log_dir=str(tmp_path))
        assert reloaded._records[0]["backfill_attempts"] == 1

    def test_row_drops_out_after_cap(self, store):
        store.record(make_reaction())
        for _ in range(BACKFILL_MAX_ATTEMPTS):
            store.backfill_actuals([])
        assert store._records[0]["backfill_attempts"] == BACKFILL_MAX_ATTEMPTS
        store.backfill_actuals([])
        assert store.last_backfill_stats["examined"] == 0
        assert store._records[0]["backfill_attempts"] == BACKFILL_MAX_ATTEMPTS

    def test_returns_int_for_existing_callers(self, store):
        store.record(make_reaction())
        assert store.backfill_actuals([]) == 0


class TestEnrichMissingContext:
    def test_fills_historical_rows(self, store):
        store.record(make_reaction())
        assert store._records[0]["forecast"] is None
        filled = store.enrich_missing_context(
            lambda *_a: {"forecast": "4.35%", "previous": "4.35%",
                         "source": "decision"})
        assert filled == 1
        assert store._records[0]["forecast"] == "4.35%"
        assert store._records[0]["context_source"] == "decision"

    def test_rewrites_the_file(self, store, tmp_path):
        store.record(make_reaction())
        store.enrich_missing_context(lambda *_a: {"forecast": "9", "source": "decision"})
        reloaded = EventReactionHistory(log_dir=str(tmp_path))
        assert reloaded._records[0]["forecast"] == "9"

    def test_noop_without_a_lookup(self, store):
        store.record(make_reaction())
        assert store.enrich_missing_context() == 0

    def test_skips_test_rows(self, store):
        store.record(make_reaction(event_name="Cash Rate (FAKE TEST EVENT)"))
        assert store.enrich_missing_context(lambda *_a: {"forecast": "1"}) == 0

    def test_resolves_surprise_when_actual_arrives(self, store):
        store.record(make_reaction())
        store.enrich_missing_context(
            lambda *_a: {"forecast": "0.3", "actual": "0.1", "source": "path_record"})
        assert store._records[0]["surprise"] == "MISS"


class TestNumericRevisionDetection:
    def _event(self, previous, actual="0.5%"):
        return EconomicEvent(
            event_name="Cash Rate", currency="AUD",
            datetime_utc=None, impact="HIGH",
            forecast="0.3%", previous=previous, actual=actual)

    def test_formatting_difference_is_not_a_revision(self):
        record = {"previous": "0.30%", "forecast": "0.3%"}
        apply_release_to_record(record, self._event("0.3%"))
        assert "previous_revised" not in record

    def test_real_revision_is_flagged(self):
        record = {"previous": "0.3%", "forecast": "0.3%"}
        apply_release_to_record(record, self._event("0.4%"))
        assert record["previous_revised"] == "0.4%"
        assert record["revision_magnitude"] == pytest.approx(0.1)

    def test_non_numeric_falls_back_to_string_compare(self):
        record = {"previous": "n/a", "forecast": "0.3%"}
        apply_release_to_record(record, self._event("tentative"))
        assert record["previous_revised"] == "tentative"

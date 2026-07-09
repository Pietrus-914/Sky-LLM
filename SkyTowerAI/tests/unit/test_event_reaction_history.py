"""
Unit tests for event_reaction_history.py — recording, matching,
summarizing and backfilling 'actual' values.
"""

import pytest
from datetime import datetime

from event_reaction_history import (
    EventReactionHistory, normalize_event_name, classify_surprise, parse_numeric
)
from calendar_fetcher import EconomicEvent


def make_reaction(**overrides):
    defaults = dict(
        event_name="Non-Farm Payrolls",
        currency="USD",
        event_time="2026-06-05T13:30:00",
        pair="EURUSD",
        price_at_event=1.0842,
        price_after_1min=1.0830,
        price_after_5min=1.0801,
    )
    defaults.update(overrides)
    return defaults


class TestNormalize:
    def test_strips_parenthesized(self):
        assert normalize_event_name("CPI m/m (Jun)") == "cpi m/m"

    def test_strips_month_tokens(self):
        assert normalize_event_name("Retail Sales June") == "retail sales"

    def test_keeps_mm_yy_distinction(self):
        assert normalize_event_name("CPI m/m") != normalize_event_name("CPI y/y")

    def test_strips_quarter(self):
        assert normalize_event_name("GDP Q2") == "gdp"


class TestSurprise:
    def test_beat(self):
        assert classify_surprise("210K", "180K") == "BEAT"

    def test_miss(self):
        assert classify_surprise("3.1%", "3.4%") == "MISS"

    def test_inline(self):
        assert classify_surprise("4.25%", "4.25%") == "INLINE"

    def test_unknown_when_missing(self):
        assert classify_surprise(None, "180K") == "UNKNOWN"
        assert classify_surprise("210K", None) == "UNKNOWN"


class TestParseNumeric:
    def test_magnitude_suffixes(self):
        assert parse_numeric("210K") == 210_000
        assert parse_numeric("1.2M") == 1_200_000
        assert parse_numeric("0.5B") == 500_000_000

    def test_mixed_units_compare_correctly(self):
        # 1.2M actual vs 900K forecast is a BEAT, not a MISS
        assert classify_surprise("1.2M", "900K") == "BEAT"

    def test_percent_and_negative(self):
        assert parse_numeric("4.25%") == 4.25
        assert parse_numeric("-0.3%") == -0.3

    def test_garbage(self):
        assert parse_numeric("n/a") is None
        assert parse_numeric(None) is None

    def test_range_takes_first_number(self):
        assert parse_numeric("1.5-2.0%") == 1.5


class TestFakeEventIsolation:
    def test_fake_reactions_excluded_from_history(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction(event_name="CPI m/m (FAKE TEST EVENT)"))
        history.record(make_reaction(event_name="CPI m/m"))
        # Both normalize to 'cpi m/m', but only the real one may surface
        matches = history.get_matching("CPI m/m", "USD")
        assert len(matches) == 1
        assert matches[0]["test"] is False

    def test_fake_flag_persisted(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        entry = history.record(make_reaction(event_name="CPI m/m (FAKE TEST EVENT)"))
        assert entry["test"] is True


class TestLoadAndRewriteSafety:
    def test_load_reads_beyond_1000_records(self, tmp_path):
        """Backfill rewrites the file from memory — a capped load would
        permanently truncate the dataset after a restart."""
        h1 = EventReactionHistory(log_dir=str(tmp_path))
        for i in range(1200):
            h1.record(make_reaction(event_time=f"2026-01-01T{i % 24:02d}:00:00"))
        h2 = EventReactionHistory(log_dir=str(tmp_path))
        assert len(h2.get_recent(limit=2000)) == 1200

    def test_backfill_rewrite_preserves_all_records_after_reload(self, tmp_path):
        h1 = EventReactionHistory(log_dir=str(tmp_path))
        for i in range(1100):
            h1.record(make_reaction(event_time=f"2026-01-01T{i % 24:02d}:00:00"))
        h1.record(make_reaction(event_time="2026-06-05T13:30:00"))  # backfillable

        h2 = EventReactionHistory(log_dir=str(tmp_path))
        event = EconomicEvent(
            datetime_utc=datetime(2026, 6, 5, 13, 30), currency="USD",
            event_name="Non-Farm Payrolls", impact="HIGH",
            forecast="180K", previous="175K", actual="210K", source="ff")
        assert h2.backfill_actuals([event]) >= 1
        h3 = EventReactionHistory(log_dir=str(tmp_path))
        assert len(h3.get_recent(limit=2000)) == 1101  # nothing lost

    def test_backfill_fills_previous(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction())
        event = EconomicEvent(
            datetime_utc=datetime(2026, 6, 5, 13, 30), currency="USD",
            event_name="Non-Farm Payrolls", impact="HIGH",
            forecast="180K", previous="175K", actual="210K", source="ff")
        history.backfill_actuals([event])
        record = history.get_recent(1)[0]
        assert record["previous"] == "175K"
        assert record["forecast"] == "180K"


class TestRecordAndSummarize:
    def test_record_computes_pips(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        entry = history.record(make_reaction())
        assert entry["move_1min_pips"] == -12.0
        assert entry["move_5min_pips"] == -41.0
        assert entry["surprise"] == "UNKNOWN"  # no actual yet

    def test_jpy_pip_math(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        entry = history.record(make_reaction(
            pair="USDJPY", price_at_event=150.00,
            price_after_1min=150.25, price_after_5min=150.60))
        assert entry["move_1min_pips"] == 25.0
        assert entry["move_5min_pips"] == 60.0

    def test_persistence(self, tmp_path):
        h1 = EventReactionHistory(log_dir=str(tmp_path))
        h1.record(make_reaction())
        h2 = EventReactionHistory(log_dir=str(tmp_path))
        assert len(h2.get_recent()) == 1

    def test_summarize_matches_normalized_name(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction(event_name="Non-Farm Payrolls (Jun)"))
        summary = history.summarize("Non-Farm Payrolls", "USD")
        assert summary is not None
        assert "EURUSD" in summary
        assert "-41.0 pips/5min" in summary

    def test_summarize_none_without_history(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        assert history.summarize("CPI m/m", "USD") is None

    def test_summarize_filters_currency(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction(currency="USD"))
        assert history.summarize("Non-Farm Payrolls", "GBP") is None

    def test_summarize_limit(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        for i in range(6):
            history.record(make_reaction(event_time=f"2026-0{i+1}-05T13:30:00"))
        summary = history.summarize("Non-Farm Payrolls", "USD", limit=4)
        assert summary.count("EURUSD") == 4


class TestBackfill:
    def test_backfill_actual_and_surprise(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction(forecast="180K"))

        calendar_event = EconomicEvent(
            datetime_utc=datetime(2026, 6, 5, 13, 30),
            currency="USD",
            event_name="Non-Farm Payrolls",
            impact="HIGH",
            forecast="180K",
            previous="175K",
            actual="210K",
            source="forexfactory",
        )
        updated = history.backfill_actuals([calendar_event])
        assert updated == 1
        record = history.get_recent(1)[0]
        assert record["actual"] == "210K"
        assert record["surprise"] == "BEAT"

    def test_backfill_survives_reload(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction())
        event = EconomicEvent(
            datetime_utc=datetime(2026, 6, 5, 13, 30), currency="USD",
            event_name="Non-Farm Payrolls", impact="HIGH",
            forecast="180K", previous="175K", actual="210K", source="ff")
        history.backfill_actuals([event])

        reloaded = EventReactionHistory(log_dir=str(tmp_path))
        assert reloaded.get_recent(1)[0]["actual"] == "210K"

    def test_backfill_ignores_time_mismatch(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction())
        event = EconomicEvent(
            datetime_utc=datetime(2026, 6, 5, 15, 0), currency="USD",
            event_name="Non-Farm Payrolls", impact="HIGH",
            forecast="180K", previous="175K", actual="210K", source="ff")
        assert history.backfill_actuals([event]) == 0

    def test_backfill_noop_when_nothing_pending(self, tmp_path):
        history = EventReactionHistory(log_dir=str(tmp_path))
        history.record(make_reaction(actual="210K", forecast="180K"))
        assert history.backfill_actuals([]) == 0

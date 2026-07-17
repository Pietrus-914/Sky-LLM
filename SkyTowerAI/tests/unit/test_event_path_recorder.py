"""
Unit tests for the event path recorder (F1 of the learning loop):
server-side measurement of post-event M1 price paths for ALL monitored
events, broker-offset inference, spread snapshots, partial/no_data
degradation and the actual/revision backfill.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import event_path_recorder as epr
from event_path_recorder import EventPathRecorder
from market_context import infer_broker_offset_seconds

UTC_NOW = datetime(2026, 7, 16, 12, 0, 0)  # naive UTC reference
BROKER_OFFSET = 3 * 3600                   # UTC+3 broker (Purple-style)


def utc_epoch(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def make_bars(start_utc, minutes, base=1.0000, step=0.0002):
    """Chronological M1 bars in BROKER time covering [start, start+minutes).
    Price drifts upward `step` per minute; each bar has a 1-pip wick both ways."""
    bars = []
    for i in range(minutes):
        t_utc = start_utc + timedelta(minutes=i)
        o = base + i * step
        c = o + step
        bars.append({
            "time": utc_epoch(t_utc) + BROKER_OFFSET,
            "open": round(o, 5), "high": round(max(o, c) + 0.0001, 5),
            "low": round(min(o, c) - 0.0001, 5), "close": round(c, 5),
        })
    return bars


def make_event(name="CPI m/m", currency="USD", at=None, source="forexfactory",
               forecast="0.3%", previous="0.2%", actual=None):
    return SimpleNamespace(
        event_name=name, currency=currency,
        datetime_utc=(at or UTC_NOW), impact="HIGH",
        forecast=forecast, previous=previous, actual=actual,
        source=source,
    )


def market_entry(bars, pushed_at, spread=25):
    return {"pair": "USDCAD", "ohlc_multi": {"M1": bars},
            "spread_points": spread, "updated_at": pushed_at.isoformat()}


@pytest.fixture
def recorder(tmp_path):
    return EventPathRecorder(log_dir=str(tmp_path))


class TestMeasurePathDirect:
    """The extracted pure function, exercised the way the HISTORICAL builder
    uses it: UTC epochs, allow_partial=True, gap tolerance."""

    def _bar_map(self, start_utc, minutes, skip=()):
        bars = make_bars(start_utc, minutes)
        return {b["time"] - BROKER_OFFSET:
                {k: b[k] for k in ("open", "high", "low", "close")}
                for i, b in enumerate(bars) if i not in skip}

    def test_complete_window(self):
        t0 = utc_epoch(UTC_NOW)
        bm = self._bar_map(UTC_NOW - timedelta(minutes=5), 40)
        m = epr.measure_path(bm, t0, 0.0001, allow_partial=True)
        assert m["complete"] is True
        assert m["move_1min_pips"] == pytest.approx(2.0, abs=0.11)
        assert m["move_30min_pips"] == pytest.approx(60.0, abs=0.11)

    def test_partial_when_data_ends_early(self):
        t0 = utc_epoch(UTC_NOW)
        bm = self._bar_map(UTC_NOW - timedelta(minutes=5), 15)  # do T+9
        m = epr.measure_path(bm, t0, 0.0001, allow_partial=True)
        assert m is not None and m["complete"] is False
        assert m["move_5min_pips"] is not None
        assert m["move_30min_pips"] is None
        # strict mode defers instead
        assert epr.measure_path(bm, t0, 0.0001, allow_partial=False) is None

    def test_single_bar_gaps_tolerated(self):
        t0 = utc_epoch(UTC_NOW)
        bm = self._bar_map(UTC_NOW - timedelta(minutes=5), 40, skip={9, 21})
        m = epr.measure_path(bm, t0, 0.0001, allow_partial=True)
        assert m["complete"] is True

    def test_no_release_bar_means_none(self):
        t0 = utc_epoch(UTC_NOW)
        bm = self._bar_map(UTC_NOW + timedelta(minutes=3), 30)  # zaczyna po T0
        assert epr.measure_path(bm, t0, 0.0001, allow_partial=True) is None


class TestOffsetInference:
    def test_infers_broker_offset(self):
        pushed = UTC_NOW
        bars = make_bars(UTC_NOW - timedelta(minutes=60), 60)
        assert infer_broker_offset_seconds(bars, pushed) == BROKER_OFFSET

    def test_utc_broker_gives_zero(self):
        pushed = UTC_NOW
        bars = [{"time": utc_epoch(UTC_NOW - timedelta(minutes=1))}]
        assert infer_broker_offset_seconds(bars, pushed) == 0

    def test_garbage_times_give_none(self):
        assert infer_broker_offset_seconds([{"time": 0}], UTC_NOW) is None
        assert infer_broker_offset_seconds([], UTC_NOW) is None


class TestScheduleAndMeasure:
    def test_full_path_measured(self, recorder, monkeypatch):
        event_time = UTC_NOW
        event = make_event(at=event_time)

        # T-10min: schedule
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=10))
        recorder.tick([event], {})
        assert len(recorder._pending) == 1

        # T0: spread snapshot from a fresh push
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time)
        bars_t0 = make_bars(event_time - timedelta(minutes=59), 60)
        recorder.tick([], {"USDCAD": market_entry(bars_t0, event_time, spread=42)})

        # T+32min: measurement from a fresh push covering T-27..T+32
        now = event_time + timedelta(minutes=32)
        monkeypatch.setattr(epr, 'utcnow', lambda: now)
        bars = make_bars(event_time - timedelta(minutes=27), 60)
        recorder.tick([], {"USDCAD": market_entry(bars, now)})

        recs = recorder.get_recent()
        assert len(recs) == 1
        r = recs[0]
        assert r["data_status"] == "ok"
        assert r["pair"] == "USDCAD"
        assert r["broker_utc_offset_min"] == BROKER_OFFSET // 60
        assert r["spread_points_t0"] == 42
        assert r["non_data"] is False
        # 2 pips/min drift upward (step 0.0002, pip 0.0001 -> 2 pips/bar)
        assert r["move_1min_pips"] == pytest.approx(2.0, abs=0.11)
        assert r["move_5min_pips"] == pytest.approx(10.0, abs=0.11)
        assert r["move_30min_pips"] == pytest.approx(60.0, abs=0.11)
        assert r["first5_high_pips"] > 0
        assert r["first5_low_pips"] <= 0
        # persisted to JSONL
        with open(recorder.get_file_path(), encoding='utf-8') as f:
            assert len(f.readlines()) == 1

        # Event STAYS pending until give-up (a sibling pair whose push
        # hiccuped can still be measured), but the measured pair is not
        # measured twice on later ticks
        assert recorder._pending
        recorder.tick([], {"USDCAD": market_entry(bars, now)})
        with open(recorder.get_file_path(), encoding='utf-8') as f:
            assert len(f.readlines()) == 1

        # At give-up: retired, deduped, and NO spurious no_data record
        monkeypatch.setattr(epr, 'utcnow',
                            lambda: event_time + timedelta(minutes=51))
        recorder.tick([], {})
        assert not recorder._pending
        with open(recorder.get_file_path(), encoding='utf-8') as f:
            assert len(f.readlines()) == 1
        recorder.tick([event], {})
        assert not recorder._pending

    def test_late_sibling_pair_measured_before_give_up(self, recorder, monkeypatch):
        """A pair whose push was momentarily stale at T+31 gets measured on a
        later tick instead of being dropped forever."""
        event_time = UTC_NOW
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=10))
        recorder.tick([make_event(at=event_time)], {})

        # T+32: only USDCAD is fresh; NZDUSD's push is 4 min old (stale)
        now = event_time + timedelta(minutes=32)
        monkeypatch.setattr(epr, 'utcnow', lambda: now)
        bars = make_bars(event_time - timedelta(minutes=27), 60)
        nzd_bars = [dict(b, time=b["time"]) for b in
                    make_bars(event_time - timedelta(minutes=31), 60)]
        recorder.tick([], {
            "USDCAD": market_entry(bars, now),
            "NZDUSD": {"pair": "NZDUSD", "ohlc_multi": {"M1": nzd_bars},
                       "spread_points": 30,
                       "updated_at": (now - timedelta(minutes=4)).isoformat()},
        })
        assert len(recorder.get_recent()) == 1  # only USDCAD so far

        # T+35: NZDUSD push refreshed -> measured now
        now2 = event_time + timedelta(minutes=35)
        monkeypatch.setattr(epr, 'utcnow', lambda: now2)
        nzd_fresh = make_bars(event_time - timedelta(minutes=24), 60)
        recorder.tick([], {
            "NZDUSD": {"pair": "NZDUSD", "ohlc_multi": {"M1": nzd_fresh},
                       "spread_points": 30, "updated_at": now2.isoformat()},
        })
        recs = recorder.get_recent()
        assert len(recs) == 2
        assert {r["pair"] for r in recs} == {"USDCAD", "NZDUSD"}

    def test_static_events_ignored(self, recorder, monkeypatch):
        monkeypatch.setattr(epr, 'utcnow', lambda: UTC_NOW - timedelta(minutes=5))
        recorder.tick([make_event(source="static")], {})
        assert not recorder._pending

    def test_no_data_when_ea_never_pushed(self, recorder, monkeypatch):
        event_time = UTC_NOW
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=5))
        recorder.tick([make_event(at=event_time)], {})

        # past give-up with nothing pushed at all
        monkeypatch.setattr(epr, 'utcnow',
                            lambda: event_time + timedelta(minutes=51))
        recorder.tick([], {})
        recs = recorder.get_recent()
        assert len(recs) == 1
        assert recs[0]["data_status"] == "no_data"
        assert recs[0]["pair"] is None

    def test_partial_from_stale_push_at_give_up(self, recorder, monkeypatch):
        """MT5 died at T+12: at give-up time the stale push still covers a
        prefix of the window -> partial record, not silence."""
        event_time = UTC_NOW
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=5))
        recorder.tick([make_event(at=event_time)], {})

        pushed = event_time + timedelta(minutes=12)
        bars = make_bars(event_time - timedelta(minutes=48), 60)  # T-48..T+12
        snapshot = {"USDCAD": market_entry(bars, pushed)}

        # At T+32 the push is stale (20 min old) -> keep waiting
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time + timedelta(minutes=32))
        recorder.tick([], snapshot)
        assert not recorder.get_recent()

        # At T+50 give-up accepts the stale push
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time + timedelta(minutes=50))
        recorder.tick([], snapshot)
        recs = recorder.get_recent()
        assert len(recs) == 1
        r = recs[0]
        assert r["data_status"] == "partial"
        assert r["move_5min_pips"] is not None
        assert r["move_30min_pips"] is None  # beyond the stale push's coverage

    def test_restart_dedup_from_file(self, tmp_path, monkeypatch):
        rec1 = EventPathRecorder(log_dir=str(tmp_path))
        event_time = UTC_NOW
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=5))
        rec1.tick([make_event(at=event_time)], {})
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time + timedelta(minutes=51))
        rec1.tick([], {})
        assert len(rec1.get_recent()) == 1

        # New instance (server restart) must not re-schedule the recorded event
        rec2 = EventPathRecorder(log_dir=str(tmp_path))
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=1))
        rec2.tick([make_event(at=event_time)], {})
        assert not rec2._pending

    def test_fake_test_event_flagged(self, recorder, monkeypatch):
        event_time = UTC_NOW
        event = make_event(name="CPI m/m (FAKE TEST EVENT)", at=event_time,
                           source="fake-test")
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=2))
        recorder.tick([event], {})
        now = event_time + timedelta(minutes=32)
        monkeypatch.setattr(epr, 'utcnow', lambda: now)
        bars = make_bars(event_time - timedelta(minutes=27), 60)
        recorder.tick([], {"USDCAD": market_entry(bars, now)})
        assert recorder.get_recent()[0]["test"] is True


class TestBackfill:
    def _recorded(self, recorder, monkeypatch, event_time):
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=5))
        recorder.tick([make_event(at=event_time)], {})
        now = event_time + timedelta(minutes=32)
        monkeypatch.setattr(epr, 'utcnow', lambda: now)
        bars = make_bars(event_time - timedelta(minutes=27), 60)
        recorder.tick([], {"USDCAD": market_entry(bars, now)})

    def test_backfills_actual_surprise_and_revision(self, recorder, monkeypatch):
        event_time = UTC_NOW
        self._recorded(recorder, monkeypatch, event_time)
        r = recorder.get_recent()[0]
        assert r["actual"] is None and r["surprise"] == "UNKNOWN"

        released = make_event(at=event_time.replace(tzinfo=timezone.utc),
                              actual="0.5%", previous="0.1%")  # revised from 0.2%
        assert recorder.backfill_actuals([released]) == 1
        r = recorder.get_recent()[0]
        assert r["actual"] == "0.5%"
        assert r["surprise"] == "BEAT"
        assert r["surprise_magnitude"] == pytest.approx(0.2)
        assert r["previous_revised"] == "0.1%"
        assert r["revision_magnitude"] == pytest.approx(-0.1)
        # pending flag clears after backfill
        assert recorder.has_pending_actuals() is False

    def test_has_pending_actuals(self, recorder, monkeypatch):
        event_time = UTC_NOW
        self._recorded(recorder, monkeypatch, event_time)
        assert recorder.has_pending_actuals() is True

    def test_non_data_events_never_pend_for_actuals(self, recorder, monkeypatch):
        """Speeches publish no number: their records must not keep the
        15-min FF backfill fetch loop alive for days."""
        event_time = UTC_NOW
        event = make_event(name="Fed Chair Speaks", at=event_time,
                           forecast=None, previous=None)
        monkeypatch.setattr(epr, 'utcnow', lambda: event_time - timedelta(minutes=5))
        recorder.tick([event], {})
        now = event_time + timedelta(minutes=32)
        monkeypatch.setattr(epr, 'utcnow', lambda: now)
        bars = make_bars(event_time - timedelta(minutes=27), 60)
        recorder.tick([], {"USDCAD": market_entry(bars, now)})

        recs = recorder.get_recent()
        assert len(recs) == 1
        assert recs[0]["non_data"] is True
        assert recorder.has_pending_actuals() is False

    def test_backfill_attempts_cap_stops_retries(self, recorder, monkeypatch):
        """A record the feed can never match stops pending after the cap."""
        event_time = UTC_NOW
        self._recorded(recorder, monkeypatch, event_time)
        assert recorder.has_pending_actuals() is True
        for _ in range(12):  # BACKFILL_MAX_ATTEMPTS failed passes
            assert recorder.backfill_actuals([]) == 0
        assert recorder.has_pending_actuals() is False

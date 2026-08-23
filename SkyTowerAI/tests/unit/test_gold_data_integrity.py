"""
Gold audit fixes, 23.08.2026 — data-integrity half.

* broker clock offset: EA echo wins, inference refuses the 23-29-min stale
  band (gold daily break) instead of snapping 30 min short, bars that stop
  advancing make a chart non-fresh;
* episode verdicts are scored only against the decision's own instrument;
* statistics / calibration noise gates in the instrument's own pips, with
  forex untouched; unit-stamped records with the wrong pip are dropped;
* GET /api/targets uses the instrument pip; /api/position/opened stores the
  served exit horizon.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python', 'tools'))

import calibration
import episode_retrieval as er
import market_context as mc
from timeutil import utcnow, utc_epoch
import build_learned_stats as bls


TRUE_OFFSET = 3 * 3600   # broker UTC+3


def bars_ending(age_seconds: int, push: datetime, n: int = 5):
    """M1 bars whose newest bar opened `age_seconds` before the push, stamped
    in BROKER time (UTC+3)."""
    newest = utc_epoch(push) - age_seconds + TRUE_OFFSET
    return [{"time": newest - 60 * (n - 1 - i), "open": 1, "high": 1, "low": 1, "close": 1}
            for i in range(n)]


class TestBrokerOffset:
    def test_fresh_and_slightly_old_bars_infer_correctly(self):
        push = utcnow()
        for age in (0, 60, 120, 300, 480):
            assert mc.infer_broker_offset_seconds(bars_ending(age, push), push) == TRUE_OFFSET

    def test_stale_band_below_half_hour_refuses_instead_of_snapping_short(self):
        """A 23-29-min-old newest bar used to pass with offset-1800 (the bar
        would sit in the FUTURE of the push under that offset). Ages around
        30 / 60 min stay inherently ambiguous for pure inference — those are
        covered by the EA echo and the bars-stalled freshness gate."""
        push = utcnow()
        for age in (23 * 60, 25 * 60, 28 * 60):
            assert mc.infer_broker_offset_seconds(bars_ending(age, push), push) is None

    def test_echo_wins_over_inference(self):
        push = utcnow()
        stale = bars_ending(25 * 60, push)
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": TRUE_OFFSET}, stale, push) == TRUE_OFFSET
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": "10800"}, stale, push) == TRUE_OFFSET
        # TimeCurrent()-TimeGMT() drifts by seconds since the last tick:
        # the echo is snapped to the 15-min grid so minute-exact bar lookups work
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": 10743}, stale, push) == TRUE_OFFSET
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": 7198}, stale, push) == 7200
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": 19790}, stale, push) == 19800   # +5:30
        # garbage / out-of-range echo -> inference (None here)
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": "x"}, stale, push) is None
        assert mc.broker_offset_seconds({"broker_utc_offset_sec": 99 * 3600}, stale, push) is None
        assert mc.broker_offset_seconds(None, bars_ending(60, push), push) == TRUE_OFFSET

    def test_stalled_bars_age_the_entry(self):
        now = utcnow()
        entry = {"updated_at": (now - timedelta(seconds=30)).isoformat()}
        assert mc.entry_age_seconds(entry, now) == pytest.approx(30, abs=1)
        entry["bars_advanced_at"] = (now - timedelta(minutes=40)).isoformat()
        assert mc.entry_age_seconds(entry, now) == pytest.approx(2400, abs=1)
        entry["bars_advanced_at"] = "garbage"
        assert mc.entry_age_seconds(entry, now) == pytest.approx(30, abs=1)


class TestMarketDataStallTracking:
    @pytest.fixture
    def client(self, monkeypatch):
        import server
        monkeypatch.setattr(server, "ensure_services", lambda: None)
        monkeypatch.setattr(server, "market_data_reports", {})
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c, server

    def _push(self, client, newest_time, offset=None):
        payload = {"pair": "XAUUSD_raw", "current_price": 2400.0, "spread_points": 50,
                   "spread_pips": 5.0, "pip_size": 0.10,
                   "ohlc_multi": {"M1": [{"time": newest_time - 60, "open": 1, "high": 1, "low": 1, "close": 1},
                                         {"time": newest_time, "open": 1, "high": 1, "low": 1, "close": 1}]}}
        if offset is not None:
            payload["broker_utc_offset_sec"] = offset
        assert client.post('/api/market-data', json=payload).get_json()["status"] == "ok"

    def test_bars_advanced_at_only_moves_when_bars_move(self, client):
        c, server = client
        t = utc_epoch(utcnow())
        self._push(c, t, offset=10800)
        first = server.market_data_reports["XAUUSD"]
        assert first["broker_utc_offset_sec"] == 10800
        assert first["last_bar_time"] == t
        adv0 = first["bars_advanced_at"]
        self._push(c, t)                       # same newest bar -> stalled
        second = server.market_data_reports["XAUUSD"]
        assert second["bars_advanced_at"] == adv0
        assert second["broker_utc_offset_sec"] is None    # no echo this time
        self._push(c, t + 60)                  # bar advanced
        assert server.market_data_reports["XAUUSD"]["bars_advanced_at"] >= adv0

    def test_targets_get_uses_instrument_pip(self, client):
        c, _ = client
        data = c.get('/api/targets?symbol=XAUUSD&direction=BUY&entry_price=2400').get_json()
        assert data["targets"]["tp1"] == pytest.approx(2400 + 15 * 0.10) or \
            data.get("tp1") == pytest.approx(2400 + 15 * 0.10) or \
            any(abs(v - 2401.5) < 1e-6 for v in _flatten_numbers(data))


def _flatten_numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_numbers(v)
    elif isinstance(obj, (int, float)):
        yield obj


class TestEpisodeVerdictScoping:
    def _ep(self, pair="XAUUSD", move5=-85.0):
        return {"event_time": "2026-05-13T12:30:00", "event_name": "CPI m/m",
                "event_name_normalized": "cpi m/m", "currency": "USD", "pair": pair,
                "regime": "hold", "forecast": "0.3%", "previous": "0.2%", "actual": "0.4%",
                "surprise": "BEAT", "move_5min_pips": move5, "move_30min_pips": -60.0,
                "first5_high_pips": 5.0, "first5_low_pips": -90.0}

    def _dec(self, pair, direction="BUY"):
        return {"event_datetime": "2026-05-13T12:30:00", "event_name": "CPI m/m",
                "currency": "USD", "pair": pair, "direction": direction,
                "confidence": 0.7, "forced": False}

    def test_other_instrument_decision_is_not_scored(self):
        text = er.render_episodes([self._ep()], [self._dec("USDCAD")], [])
        assert "on USDCAD (different instrument, not scored)" in text
        assert "WRONG" not in text and "CORRECT" not in text

    def test_same_instrument_is_scored(self):
        text = er.render_episodes([self._ep()], [self._dec("XAUUSD_raw", "SELL")], [])
        assert "CORRECT" in text

    def test_legacy_decision_without_pair_keeps_verdict(self):
        d = self._dec("XAUUSD"); d.pop("pair")
        text = er.render_episodes([self._ep()], [d], [])
        assert "WRONG" in text


class TestStatsGates:
    def test_gates_forex_unchanged_gold_profiled(self):
        assert bls.gates_for("NZDUSD") == (2.0, 1.0)
        assert bls.gates_for("XAUUSD") == (10.0, 5.0)
        assert bls.gates_for("GOLD") == (10.0, 5.0)

    def test_currency_strength_sign_on_gold(self):
        assert bls._currency_strength_move({"move_5min_pips": -10.0, "pair": "XAUUSD", "currency": "USD"}) == 10.0
        assert bls._currency_strength_move({"move_5min_pips": -10.0, "pair": "GOLD", "currency": "USD"}) == 10.0
        assert bls._currency_strength_move({"move_5min_pips": -10.0, "pair": "XAUUSD", "currency": "EUR"}) is None
        assert bls._currency_strength_move({"move_5min_pips": 4.0, "pair": "USDCAD", "currency": "CAD"}) == -4.0

    def test_pair_stats_apply_gold_gate(self):
        def rec(m5, pre=0.0):
            return {"pair": "XAUUSD", "currency": "USD", "move_5min_pips": m5,
                    "move_30min_pips": m5, "first5_high_pips": max(m5, 0) + 1,
                    "first5_low_pips": min(m5, 0) - 1, "high_30min_pips": max(m5, 0) + 1,
                    "low_30min_pips": min(m5, 0) - 1, "pre_release_3m_pips": pre,
                    "surprise": "BEAT", "move_1min_pips": m5}
        recs = [rec(3.0), rec(-4.0), rec(40.0), rec(-60.0)]
        out = bls._pair_stats(recs)
        assert out["gates_pips"] == {"min_move": 10.0, "min_directional": 5.0}
        assert out["continuation_5to30"]["n"] == 2          # 3 / -4 pips are noise on gold
        fx = [dict(r, pair="USDCAD") for r in recs]
        assert bls._pair_stats(fx)["continuation_5to30"]["n"] == 4
        assert "gates_pips" not in bls._pair_stats(fx)

    def test_unit_stamp_mismatch_dropped(self, tmp_path):
        import gzip, json
        hist = tmp_path / "h.jsonl.gz"
        rows = [
            {"event_key": "USD|cpi m/m|2026-01-01T13:30", "pair": "XAUUSD", "currency": "USD",
             "event_name_normalized": "cpi m/m", "move_1min_pips": 80.0, "pip_size": 0.10},
            {"event_key": "USD|cpi m/m|2026-02-01T13:30", "pair": "XAUUSD", "currency": "USD",
             "event_name_normalized": "cpi m/m", "move_1min_pips": 80000.0, "pip_size": 0.0001},
            {"event_key": "USD|cpi m/m|2026-03-01T13:30", "pair": "XAUUSD", "currency": "USD",
             "event_name_normalized": "cpi m/m", "move_1min_pips": 70.0},
        ]
        with gzip.open(hist, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        records, counters = bls.load_records(str(hist), None)
        assert len(records) == 2
        assert counters["dropped"]["unit_mismatch"] == 1


class TestCalibrationGates:
    def test_score_uses_instrument_gate(self):
        base = {"event_name": "CPI m/m", "currency": "USD", "direction": "BUY",
                "confidence": 0.7, "model": "m", "prompt_version": "v", "forced": False,
                "event_datetime": "2026-05-13T12:30:00"}
        path_fx = {"event_name_normalized": "cpi m/m", "currency": "USD", "pair": "USDCAD",
                   "event_time": "2026-05-13T12:30:00", "move_5min_pips": 3.0,
                   "recorded_at": "2026-05-13T13:00:00Z"}
        path_gold = dict(path_fx, pair="XAUUSD")
        index = calibration.index_paths([path_fx, path_gold])
        row_fx = calibration.score_decision(dict(base, pair="USDCAD"), index)
        row_gold = calibration.score_decision(dict(base, pair="XAUUSD"), index)
        assert row_fx["correct"] is True           # 3 pips clears the 1-pip forex gate
        assert row_gold["correct"] is None         # 3 pips = $0.30 is noise on gold

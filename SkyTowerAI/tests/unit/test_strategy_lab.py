"""tools/strategy_lab.py — offline strategy evaluator on recorded paths."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python', 'tools'))

import strategy_lab as lab


def rec(**over):
    r = {"pair": "XAUUSD", "currency": "USD", "event_name_normalized": "cpi m/m",
         "surprise": "BEAT", "move_1min_pips": -80.0, "move_5min_pips": -78.0,
         "move_15min_pips": -90.0, "move_30min_pips": -60.0,
         "first5_high_pips": 6.0, "first5_low_pips": -95.0,
         "high_30min_pips": 10.0, "low_30min_pips": -120.0,
         "pre_release_3m_pips": 20.0}
    r.update(over)
    return r


class TestDirections:
    def test_oracle_maps_usd_beat_to_sell_gold(self):
        assert lab.oracle_direction(rec()) == -1
        assert lab.oracle_direction(rec(surprise="MISS")) == 1
        assert lab.oracle_direction(rec(surprise="INLINE")) == 0

    def test_oracle_inverse_sense_and_forex_quote(self):
        claims = rec(event_name_normalized="unemployment claims", pair="USDCAD")
        assert lab.oracle_direction(claims) == -1          # higher claims: USD weaker -> SELL USDCAD
        assert lab.oracle_direction(rec(pair="USDCAD")) == 1     # CPI beat: BUY USDCAD
        assert lab.oracle_direction(rec(pair="NZDUSD", currency="NZD")) == 1

    def test_entries(self):
        assert lab.strategy_entries(rec(), "pre_oracle", 20, 15) == (-1, 0.0, 0)
        assert lab.strategy_entries(rec(), "pre_fade_drift", 20, 15) == (-1, 0.0, 0)
        assert lab.strategy_entries(rec(), "post_confirm", 20, 15) == (-1, -80.0, 1)
        assert lab.strategy_entries(rec(), "post_fade", 20, 15) == (1, -80.0, 1)
        assert lab.strategy_entries(rec(move_1min_pips=-5.0), "post_confirm", 20, 15) == (0, 0.0, 0)
        assert lab.strategy_entries(rec(), "post_fade_5", 20, 15) == (1, -78.0, 5)


class TestOutcomeModel:
    def test_timed_exit_exact(self):
        pnl, how = lab.simulate(rec(), -1, 0.0, 0, 15, 0, 0, 12.0)
        assert (pnl, how) == (90.0 - 12.0, "time")

    def test_stop_before_target_is_conservative(self):
        # SELL at T0: adverse = high_30 (10), favorable = -low_30 (120)
        pnl, how = lab.simulate(rec(), -1, 0.0, 0, 30, 8, 100, 12.0)
        assert (pnl, how) == (-8 - 12.0, "sl")          # 10 >= 8 -> stop assumed first
        pnl, how = lab.simulate(rec(), -1, 0.0, 0, 30, 50, 100, 12.0)
        assert (pnl, how) == (100 - 12.0, "tp")

    def test_post_entry_uses_only_samples_after_entry(self):
        # post_fade: BUY at -80 after the first candle. Window extremes would
        # say favorable = high_30 - (-80) = 90, but only move_5/15/30 relative
        # to the entry count: max(-78+80, -90+80, -60+80) = 20.
        fav, adv = lab._excursions(rec(), 1, -80.0, 1, 30)
        assert fav == 20.0 and adv == -80.0 - (-120.0)
        pnl, how = lab.simulate(rec(), 1, -80.0, 1, 30, 0, 30, 12.0)
        assert how == "time" and pnl == (-60.0 + 80.0) - 12.0
        assert lab.simulate(rec(), 1, -80.0, 5, 5, 0, 0, 12.0) is None   # horizon <= entry


class TestGrid:
    def test_run_grid_and_best(self):
        recs = [rec(), rec(surprise="MISS", move_1min_pips=70.0, move_5min_pips=75.0,
                           move_15min_pips=85.0, move_30min_pips=50.0,
                           first5_high_pips=90.0, first5_low_pips=-4.0,
                           high_30min_pips=110.0, low_30min_pips=-9.0)]
        rows = lab.run_grid(recs, ["pre_oracle"], [0, 80], [0, 100], [15], 12.0, 20, 15, 1)
        assert {r["hold_min"] for r in rows} == {15}
        timed = [r for r in rows if r["sl"] == 0 and r["tp"] == 0][0]
        assert timed["n"] == 2 and timed["win_rate"] == 1.0
        assert timed["avg_pips"] == round(((90 - 12) + (85 - 12)) / 2, 1)
        best = lab.best_per_event(rows)
        assert "USD|cpi m/m::pre_oracle" in best

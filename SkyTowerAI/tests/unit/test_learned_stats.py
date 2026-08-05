"""
Unit tests for the F3 learning loop: learned-stats aggregation
(tools/build_learned_stats.py) and its prompt-side consumption
(LEARNED EVENT STATISTICS section + KEY BASE RATES recap).
"""
import gzip
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from tools.build_learned_stats import (build_stats, attribute_records,
                                       dominance_key, load_records,
                                       _currency_strength_move)
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


def path_rec(name="cpi m/m", currency="USD", pair="USDCAD",
             time="2024-01-01T13:30", impact="HIGH", move1=10.0, move5=20.0,
             move15=22.0, move30=25.0, hi=22.0, lo=-3.0, hi30=None, lo30=None,
             pre=-4.0,
             surprise="BEAT", mag=0.2, regime="hiking", source="historical",
             non_data=False, test=False, status="ok"):
    """One event-path record in the shared live/historical schema."""
    return {
        "source": source, "test": test,
        "event_key": f"{currency}|{name}|{time}",
        "event_name": name.title(), "event_name_normalized": name,
        "currency": currency, "event_time": time + ":00",
        "impact": impact, "surprise": surprise, "surprise_magnitude": mag,
        "non_data": non_data, "regime": regime, "pair": pair,
        "data_status": status,
        "move_1min_pips": move1, "move_5min_pips": move5,
        "move_15min_pips": move15, "move_30min_pips": move30,
        "first5_high_pips": hi, "first5_low_pips": lo,
        "high_30min_pips": hi30, "low_30min_pips": lo30,
        "pre_release_3m_pips": pre,
    }


def releases(n, name="cpi m/m", month_start=1, **overrides):
    """n same-name releases at distinct times (day bumps past December so
    wrapped months never collide with earlier ones)."""
    out = []
    for i in range(n):
        idx = month_start + i - 1
        t = f"2024-{idx % 12 + 1:02d}-{10 + idx // 12:02d}T13:30"
        out.append(path_rec(name=name, time=t, **overrides))
    return out


# ======================================================================
# Attribution: the dominant event claims the shared same-minute path
# ======================================================================

class TestAttribution:
    def test_solo_event_kept(self):
        recs = [path_rec()]
        attributed, bundled, info = attribute_records(recs)
        assert len(attributed) == 1
        assert not bundled

    def test_higher_impact_dominates(self):
        recs = [path_rec(name="cpi m/m", impact="HIGH"),
                path_rec(name="capacity utilization rate", impact="LOW")]
        attributed, bundled, info = attribute_records(recs)
        assert [r["event_name_normalized"] for r in attributed] == ["cpi m/m"]
        assert bundled["USD|capacity utilization rate"]["USD|cpi m/m"] == 1

    def test_rate_decision_beats_cpi(self):
        recs = [path_rec(name="official cash rate", currency="NZD", pair="NZDUSD"),
                path_rec(name="cpi q/q", currency="NZD", pair="NZDUSD")]
        attributed, _, _ = attribute_records(recs)
        assert [r["event_name_normalized"] for r in attributed] == ["official cash rate"]

    def test_nfp_beats_cad_employment_cross_currency(self):
        # The classic first-Friday USDCAD collision: both HIGH, family rank
        # resolves it (NFP owns the pair's path)
        recs = [path_rec(name="non-farm employment change", currency="USD"),
                path_rec(name="employment change", currency="CAD")]
        attributed, bundled, _ = attribute_records(recs)
        assert [r["currency"] for r in attributed] == ["USD"]
        assert bundled["CAD|employment change"]["USD|non-farm employment change"] == 1

    def test_plain_name_beats_core_variant(self):
        recs = [path_rec(name="core cpi m/m"), path_rec(name="cpi m/m")]
        attributed, _, _ = attribute_records(recs)
        assert [r["event_name_normalized"] for r in attributed] == ["cpi m/m"]

    def test_cross_currency_tie_is_ambiguous_and_dropped(self):
        recs = [path_rec(name="some usd thing", impact="LOW", currency="USD"),
                path_rec(name="other cad thing", impact="LOW", currency="CAD")]
        attributed, bundled, info = attribute_records(recs)
        assert attributed == []
        assert not bundled
        assert info["ambiguous_groups"] == 1
        assert info["ambiguous_records"] == 2

    def test_hidden_cross_currency_tie_still_ambiguous(self):
        # Two same-currency members sort ahead of the foreign tied member —
        # the foreign tie must still poison the group (review finding: the
        # old runner-up-only check attributed 6 real groups incorrectly)
        recs = [path_rec(name="building permits", impact="LOW", currency="USD"),
                path_rec(name="housing starts", impact="LOW", currency="USD"),
                path_rec(name="manufacturing sales m/m", impact="LOW",
                         currency="CAD")]
        attributed, bundled, info = attribute_records(recs)
        assert attributed == []
        assert not bundled
        assert info["ambiguous_groups"] == 1
        assert info["ambiguous_records"] == 3

    def test_alias_tiebreak_deterministic(self):
        # A member co-releasing equally often with two dominants must always
        # alias to the same one (sorted iteration -> alphabetically first)
        recs = []
        for i in range(5):
            t = f"2024-{i+1:02d}-10T13:30"
            recs.append(path_rec(name="cpi m/m", time=t))
            recs.append(path_rec(name="import prices m/m", time=t, impact="LOW"))
        for i in range(5):
            t = f"2024-{i+7:02d}-10T13:30"
            recs.append(path_rec(name="ppi m/m", time=t))
            recs.append(path_rec(name="import prices m/m", time=t, impact="LOW"))
        stats = build_stats(recs)
        assert stats["bundle_alias"]["USD|import prices m/m"]["to"] == "USD|cpi m/m"

    def test_same_event_different_minutes_not_grouped(self):
        recs = [path_rec(time="2024-01-01T13:30"),
                path_rec(name="ppi m/m", time="2024-01-01T13:31")]
        attributed, _, _ = attribute_records(recs)
        assert len(attributed) == 2

    def test_bundle_counts_are_release_level_not_record_level(self):
        # One CPI release measured on 3 pairs: the core-CPI member must count
        # ONE joint release, not three records
        recs = []
        for pair in ("USDCAD", "AUDUSD", "EURUSD"):
            recs.append(path_rec(name="cpi m/m", pair=pair))
            recs.append(path_rec(name="core cpi m/m", pair=pair))
        _, bundled, _ = attribute_records(recs)
        assert bundled["USD|core cpi m/m"]["USD|cpi m/m"] == 1


# ======================================================================
# Statistics math
# ======================================================================

class TestBuildStats:
    def test_median_iqr_and_n(self):
        recs = []
        for i, m5 in enumerate([10, 20, 30, 40, 50]):
            recs.append(path_rec(time=f"2024-{i+1:02d}-10T13:30", move5=float(m5)))
        stats = build_stats(recs)
        block = stats["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["n"] == 5
        assert block["abs_move_5min"]["median"] == 30
        assert block["abs_move_5min"]["p25"] == 20
        assert block["abs_move_5min"]["p75"] == 40
        assert block["abs_move_5min"]["n"] == 5

    def test_negative_moves_counted_as_absolute(self):
        recs = releases(4, move5=-30.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["abs_move_5min"]["median"] == 30

    def test_thin_pair_blocks_not_emitted(self):
        stats = build_stats(releases(2))
        assert "USD|cpi m/m" not in stats["events"]

    def test_currency_strength_flips_for_quote_currency(self):
        # USD event on EURUSD: pair DOWN means USD stronger
        rec = path_rec(pair="EURUSD", move5=-20.0)
        assert _currency_strength_move(rec) == 20.0
        rec = path_rec(pair="USDCAD", move5=-20.0)
        assert _currency_strength_move(rec) == -20.0

    def test_beat_miss_rates_in_currency_terms(self):
        # 4 BEATs on EURUSD where pair fell (USD stronger), 1 where it rose
        recs = []
        for i in range(4):
            recs.append(path_rec(pair="EURUSD", time=f"2024-{i+1:02d}-10T13:30",
                                 move5=-20.0, surprise="BEAT"))
        recs.append(path_rec(pair="EURUSD", time="2024-05-10T13:30",
                             move5=20.0, surprise="BEAT"))
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["EURUSD"]
        assert block["beat_currency_up_5min"] == {"rate": 0.8, "n": 5}

    def test_flat_moves_excluded_from_directional_rates(self):
        recs = releases(3, move5=0.5, surprise="BEAT")   # below 1-pip gate
        recs += releases(3, month_start=4, move5=25.0, surprise="BEAT")
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["beat_currency_up_5min"]["n"] == 3

    def test_fade_and_continuation_rates(self):
        recs = []
        # 3 releases: pre-drift -4 pips, release +20 (fade), 30min +25 (continues)
        recs += releases(3, pre=-4.0, move5=20.0, move30=25.0)
        # 1 release: pre-drift +4, release +20 (no fade), 30min -25 (reverses)
        recs += releases(1, month_start=4, pre=4.0, move5=20.0, move30=-25.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["fade_pre_drift"] == {"rate": 0.75, "n": 4}
        assert block["continuation_5to30"] == {"rate": 0.75, "n": 4}

    def test_adverse_wick_against_eventual_direction(self):
        # Up-moves with 3-pip downside wick: adverse = |first5_low|
        recs = releases(4, move5=20.0, hi=22.0, lo=-3.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["adverse_wick_5min"]["median"] == 3.0
        # Down-moves: adverse is the upside spike
        recs = releases(4, move5=-20.0, hi=7.0, lo=-25.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["adverse_wick_5min"]["median"] == 7.0

    def test_favorable_run_with_eventual_direction(self):
        # Up-moves: favorable = first5_high (what a BUY's TP could capture)
        recs = releases(4, move5=20.0, hi=22.0, lo=-3.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["favorable_run_5min"]["median"] == 22.0
        # Down-moves: favorable = |first5_low|
        recs = releases(4, move5=-20.0, hi=7.0, lo=-25.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["favorable_run_5min"]["median"] == 25.0

    def test_favorable_run_30min_uses_30min_extremes_and_direction(self):
        # 5-min window up, 30-min window reversed down: each window's
        # favorable side follows ITS OWN eventual direction
        recs = releases(4, move5=20.0, hi=22.0, lo=-3.0,
                        move30=-30.0, hi30=24.0, lo30=-38.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["favorable_run_5min"]["median"] == 22.0
        assert block["favorable_run_30min"]["median"] == 38.0

    def test_favorable_run_p80_present_with_enough_samples(self):
        recs = releases(12, move5=20.0, hi=22.0, lo=-3.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert "p80" in block["favorable_run_5min"]
        # 30-min extremes absent from these records -> stat not fabricated
        assert "favorable_run_30min" not in block

    def test_favorable_run_respects_move_gate(self):
        # Sub-2-pip window moves are direction noise: no favorable stat may
        # be fabricated from them (same gate as the adverse wick)
        recs = releases(4, move5=0.5, hi=22.0, lo=-3.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert "favorable_run_5min" not in block

    def test_regime_split(self):
        recs = releases(4, regime="hiking", move5=40.0)
        recs += releases(4, month_start=5, regime="cutting", move5=10.0)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["by_regime"]["hiking"]["abs_move_5min"]["median"] == 40
        assert block["by_regime"]["cutting"]["abs_move_5min"]["median"] == 10
        assert "hold" not in block["by_regime"]

    def test_surprise_sigma_deduped_across_pairs(self):
        # 12 releases, each measured on 2 pairs — sigma must use 12 unique
        # magnitudes, not 24 duplicated ones
        recs = []
        for i in range(12):
            t = f"2024-{i % 12 + 1:02d}-{10 + i // 12}T13:30"
            for pair in ("USDCAD", "AUDUSD"):
                recs.append(path_rec(time=t, pair=pair, mag=0.1 * (i % 3)))
        entry = build_stats(recs)["events"]["USD|cpi m/m"]
        assert entry["surprise_sigma_n"] == 12
        assert entry["surprise_sigma"] > 0

    def test_non_data_only_events_excluded(self):
        recs = releases(5, name="fed chair powell speaks", non_data=True,
                        surprise="UNKNOWN", mag=None)
        stats = build_stats(recs)
        assert "USD|fed chair powell speaks" not in stats["events"]

    def test_partial_records_contribute_available_horizons(self):
        recs = releases(5, move30=None, move15=None)
        block = build_stats(recs)["events"]["USD|cpi m/m"]["pairs"]["USDCAD"]
        assert block["abs_move_5min"]["n"] == 5
        assert "abs_move_30min" not in block

    def test_alias_for_thin_bundled_member(self):
        recs = []
        for i in range(6):   # 6 joint releases, member has no solo sample
            t = f"2024-{i+1:02d}-10T13:30"
            recs.append(path_rec(name="cpi m/m", time=t))
            recs.append(path_rec(name="core cpi m/m", time=t))
        stats = build_stats(recs)
        assert stats["bundle_alias"]["USD|core cpi m/m"] == \
            {"to": "USD|cpi m/m", "n": 6}

    def test_no_alias_when_member_has_own_sample(self):
        recs = []
        for i in range(6):   # 6 joint releases...
            t = f"2024-{i+1:02d}-10T13:30"
            recs.append(path_rec(name="cpi m/m", time=t))
            recs.append(path_rec(name="core cpi m/m", time=t))
        # ...but also 8 solo releases of the member -> its own stats suffice
        recs += releases(8, name="core cpi m/m", month_start=7)
        stats = build_stats(recs)
        assert "USD|core cpi m/m" not in stats["bundle_alias"]
        assert "USD|core cpi m/m" in stats["events"]


# ======================================================================
# Loading + dedup (files)
# ======================================================================

class TestLoadRecords:
    def _write(self, tmp_path, historical, live):
        hist = tmp_path / "historical.jsonl.gz"
        with gzip.open(hist, "wt", encoding="utf-8") as f:
            for r in historical:
                f.write(json.dumps(r) + "\n")
        live_path = tmp_path / "event_paths.jsonl"
        with open(live_path, "w", encoding="utf-8") as f:
            for r in live:
                f.write(json.dumps(r) + "\n")
        return str(hist), str(live_path)

    def test_live_wins_dedup(self, tmp_path):
        hist_rec = path_rec(move5=10.0)
        live_rec = path_rec(move5=50.0, source="server_m1")
        h, l = self._write(tmp_path, [hist_rec], [live_rec])
        records, info = load_records(h, l)
        assert len(records) == 1
        assert records[0]["move_5min_pips"] == 50.0
        assert info["sources"] == {"historical": 1, "live": 1}

    def test_junk_records_dropped(self, tmp_path):
        good = path_rec()
        h, l = self._write(tmp_path, [
            good,
            path_rec(time="2024-02-01T13:30", test=True),
            path_rec(time="2024-03-01T13:30", impact="HOLIDAY"),
            dict(path_rec(time="2024-04-01T13:30"), pair=None,
                 data_status="no_data", move_1min_pips=None),
        ], [])
        records, info = load_records(h, l)
        assert len(records) == 1
        assert info["dropped"]["test"] == 1
        assert info["dropped"]["holiday"] == 1
        assert info["dropped"]["no_data"] == 1

    def test_missing_files_tolerated(self, tmp_path):
        records, info = load_records(str(tmp_path / "nope.gz"),
                                     str(tmp_path / "nope.jsonl"))
        assert records == []


class TestCliGuards:
    def test_zero_events_never_clobbers_previous_output(self, tmp_path,
                                                        monkeypatch, capsys):
        """An input that loads records but emits no events (all below
        EMIT_MIN_N) must exit 1 and leave the previous good file untouched."""
        from tools import build_learned_stats as bls
        hist = tmp_path / "thin.jsonl.gz"
        with gzip.open(hist, "wt", encoding="utf-8") as f:
            f.write(json.dumps(path_rec()) + "\n")   # 1 record < EMIT_MIN_N
        out = tmp_path / "learned_stats.json"
        out.write_text('{"good": "previous"}', encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["build_learned_stats.py", "--historical", str(hist),
                             "--live", str(tmp_path / "none.jsonl"),
                             "--out", str(out)])
        with pytest.raises(SystemExit) as exc:
            bls.main()
        assert exc.value.code == 1
        assert out.read_text(encoding="utf-8") == '{"good": "previous"}'
        assert not os.path.exists(str(out) + ".tmp")


# ======================================================================
# Engine side: loader, gates, rendering, recap, hot-reload
# ======================================================================

def make_engine(tmp_path, regime_provider=None):
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trade_history.jsonl"),
        regime_provider=regime_provider,
    )
    engine.cot_analyzer = Mock()
    engine.sentiment = Mock()
    engine.playbooks_file = str(tmp_path / "event_playbooks.json")
    engine.learned_stats_file = str(tmp_path / "learned_stats.json")
    return engine


def write_stats(tmp_path, events=None, aliases=None):
    data = {
        "_meta": {"schema_version": 1},
        "events": events or {},
        "bundle_alias": aliases or {},
    }
    path = tmp_path / "learned_stats.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def cpi_entry(n=20, median5=18.2, beat_n=15, regime_buckets=True):
    entry = {
        "event_name": "CPI m/m", "currency": "USD", "n_releases": n,
        "span": ["2021-02", "2026-06"],
        "surprise_sigma": 0.15, "surprise_sigma_n": 40,
        "bundled_with": ["core cpi m/m", "cpi y/y"],
        "pairs": {
            "USDCAD": {
                "n": n,
                "abs_move_5min": {"median": median5, "p25": 9.1, "p75": 30.0, "n": n},
                "adverse_wick_5min": {"median": 1.5, "p25": 0.2, "p75": 3.8,
                                      "p90": 12.8, "n": n},
                "favorable_run_5min": {"median": 15.5, "p25": 7.3, "p75": 25.5,
                                       "p80": 25.7, "p90": 29.1, "n": 16},
                "favorable_run_30min": {"median": 18.3, "p25": 11.2,
                                        "p75": 25.9, "p80": 26.3, "n": 18},
                "continuation_5to30": {"rate": 0.86, "n": n},
                "fade_pre_drift": {"rate": 0.47, "n": n},
                "beat_currency_up_5min": {"rate": 0.68, "n": beat_n},
                "miss_currency_down_5min": {"rate": 0.92, "n": 13},
            },
        },
    }
    if regime_buckets:
        entry["pairs"]["USDCAD"]["by_regime"] = {
            "hiking": {"n": 15,
                       "abs_move_5min": {"median": 32.2, "p25": 12.5,
                                         "p75": 48.6, "n": 15}},
            "cutting": {"n": 10,
                        "abs_move_5min": {"median": 8.4, "p25": 4.4,
                                          "p75": 26.0, "n": 10}},
        }
    return entry


class TestLearnedStatsSection:
    def test_missing_file_yields_none(self, tmp_path):
        engine = make_engine(tmp_path)
        assert engine._learned_stats_section("CPI m/m", "USD", "USD/CAD") is None

    def test_broken_file_yields_none(self, tmp_path):
        engine = make_engine(tmp_path)
        (tmp_path / "learned_stats.json").write_text("{not json", encoding="utf-8")
        assert engine._learned_stats_section("CPI m/m", "USD", "USD/CAD") is None

    def test_foreign_schema_ignored(self, tmp_path):
        engine = make_engine(tmp_path)
        (tmp_path / "learned_stats.json").write_text(
            json.dumps({"_meta": {"schema_version": 99}, "events": {"x": {}}}),
            encoding="utf-8")
        assert engine._learned_stats_section("CPI m/m", "USD", "USD/CAD") is None

    def test_full_section_and_recap(self, tmp_path):
        engine = make_engine(tmp_path, regime_provider=lambda c: "hiking")
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()})
        full, recap = engine._learned_stats_section("CPI m/m", "USD", "USD/CAD")
        assert 'USD "CPI m/m" on USDCAD: n=20 measured releases' in full
        assert "median 18.2 (IQR 9.1-30, n=20)" in full
        assert "p90 12.8" in full
        assert ("favorable run" in full
                and "first 5min median 15.5, p75 25.5, p80 25.7 (n=16)" in full
                and "first 30min median 18.3, p75 25.9, p80 26.3 (n=18)" in full
                and "size take_profit_pips between the median and the p80" in full)
        assert "favorable-run median 15.5/p80 25.7p" in recap
        assert "86% (n=20)" in full          # continuation
        assert "BEAT -> USD stronger: 68% (n=15)" in full
        assert "current USD policy regime: hiking" in full
        assert "median 32.2" in full          # hiking bucket, not cutting
        assert "8.4" not in full
        assert "sigma(actual-forecast)=0.15" in full
        assert "co-releases with: core cpi m/m, cpi y/y" in full
        assert recap.startswith("USD CPI m/m on USDCAD: 5-min |move| median 18.2")
        assert "BEAT->USD stronger 68% (n=15)" in recap

    def test_regime_from_provider_beats_config_seed(self, tmp_path):
        engine = make_engine(tmp_path, regime_provider=lambda c: "cutting")
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()})
        full, _ = engine._learned_stats_section("CPI m/m", "USD", "USDCAD")
        assert "current USD policy regime: cutting" in full
        assert "median 8.4" in full

    def test_rate_below_gate_hidden(self, tmp_path):
        engine = make_engine(tmp_path)
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry(beat_n=9)})
        full, recap = engine._learned_stats_section("CPI m/m", "USD", "USDCAD")
        assert "BEAT -> USD stronger" not in full
        assert "MISS -> USD weaker: 92% (n=13)" in full
        assert "BEAT" not in (recap or "")

    def test_thin_pair_yields_none(self, tmp_path):
        engine = make_engine(tmp_path)
        entry = cpi_entry(n=4)
        entry["pairs"]["USDCAD"]["n"] = 4
        write_stats(tmp_path, {"USD|cpi m/m": entry})
        assert engine._learned_stats_section("CPI m/m", "USD", "USDCAD") is None

    def test_alias_fallback_with_note(self, tmp_path):
        engine = make_engine(tmp_path)
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()},
                    aliases={"USD|core cpi m/m": {"to": "USD|cpi m/m", "n": 63}})
        full, _ = engine._learned_stats_section("Core CPI m/m", "USD", "USDCAD")
        assert "No standalone sample" in full
        assert "co-released with USD \"CPI m/m\" 63x" in full

    def test_pair_fallback_with_note(self, tmp_path):
        engine = make_engine(tmp_path)
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()})
        full, _ = engine._learned_stats_section("CPI m/m", "USD", "GBPUSD")
        assert "No GBPUSD sample" in full
        assert "stats shown for USDCAD" in full

    def test_bom_file_tolerated(self, tmp_path):
        # A PowerShell/Notepad resave adds a BOM — the loader must still parse
        engine = make_engine(tmp_path)
        data = {"_meta": {"schema_version": 1},
                "events": {"USD|cpi m/m": cpi_entry()}, "bundle_alias": {}}
        (tmp_path / "learned_stats.json").write_bytes(
            b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
        full, _ = engine._learned_stats_section("CPI m/m", "USD", "USDCAD")
        assert "n=20 measured releases" in full

    def test_hot_reload_on_mtime_change(self, tmp_path):
        engine = make_engine(tmp_path)
        path = write_stats(tmp_path, {"USD|cpi m/m": cpi_entry(median5=18.2)})
        full, _ = engine._learned_stats_section("CPI m/m", "USD", "USDCAD")
        assert "median 18.2" in full
        entry = cpi_entry(median5=44.4)
        data = {"_meta": {"schema_version": 1}, "events": {"USD|cpi m/m": entry},
                "bundle_alias": {}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 10))
        full, _ = engine._learned_stats_section("CPI m/m", "USD", "USDCAD")
        assert "median 44.4" in full


class TestPromptIntegration:
    def _event(self):
        return SimpleNamespace(
            event_name="CPI m/m", currency="USD",
            datetime_utc=datetime.utcnow() + timedelta(minutes=30),
            impact="HIGH", forecast="0.3%", previous="0.2%")

    def _prepare(self, tmp_path, monkeypatch, with_stats=True):
        engine = make_engine(tmp_path, regime_provider=lambda c: "hiking")
        engine.cot_analyzer.analyze_currency.return_value = {"signal": "BULLISH"}
        engine.sentiment.get_currency_sentiment.return_value = {
            "signal": "SHORT", "pairs_analyzed": 1}
        engine.reaction_history = Mock()
        engine.reaction_history.summarize.return_value = None
        engine.reaction_history.summarize_currency_fallback.return_value = None
        engine.reaction_history.get_matching.return_value = []
        if with_stats:
            write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()})
        monkeypatch.setattr("llm_decision_engine.FORCE_DECISION", False)
        return engine

    def test_gather_data_carries_stats_and_recap(self, tmp_path, monkeypatch):
        engine = self._prepare(tmp_path, monkeypatch)
        ctx = engine._gather_data(self._event(), None)
        assert "n=20 measured releases" in ctx["learned_stats"]
        assert ctx["learned_recap"].startswith("USD CPI m/m on USDCAD")
        assert ctx["_source_status"]["learned_stats"] == "ok"

    def test_gather_data_without_stats(self, tmp_path, monkeypatch):
        engine = self._prepare(tmp_path, monkeypatch, with_stats=False)
        ctx = engine._gather_data(self._event(), None)
        assert ctx["learned_stats"] is None
        assert ctx["_source_status"]["learned_stats"] == "no_data"

    def test_render_exception_audited_as_error_not_no_data(self, tmp_path,
                                                           monkeypatch):
        """A bad regeneration that passes the schema gate but breaks rendering
        must be distinguishable from 'no stats exist' in the audit trail."""
        engine = self._prepare(tmp_path, monkeypatch, with_stats=False)
        entry = cpi_entry()
        entry["pairs"]["USDCAD"]["abs_move_5min"] = {"n": 20}   # no median
        write_stats(tmp_path, {"USD|cpi m/m": entry})
        ctx = engine._gather_data(self._event(), None)
        assert ctx["learned_stats"] is None
        assert ctx["_source_status"]["learned_stats"].startswith("error:")

    def test_prompt_places_stats_mid_and_recap_at_end(self, tmp_path, monkeypatch):
        engine = self._prepare(tmp_path, monkeypatch)
        engine.client = object()   # force the _llm_decision path
        engine.model = "test-model"
        captured = {}

        def fake_chat(prompt):
            captured["prompt"] = prompt
            return json.dumps({"reasoning": "x", "direction": "SKIP",
                               "confidence": 0.4})

        engine._chat = fake_chat
        decision = engine._llm_decision(self._event(),
                                        engine._gather_data(self._event(), None))
        prompt = captured["prompt"]
        assert "LEARNED EVENT STATISTICS" in prompt
        assert "KEY BASE RATES" in prompt
        # Section sits mid-prompt, recap after SUGGESTED PAIR near the end
        assert prompt.index("LEARNED EVENT STATISTICS") < prompt.index("YOUR TRACK RECORD")
        assert prompt.index("SUGGESTED PAIR:") < prompt.index("KEY BASE RATES")
        assert decision.direction == "SKIP"

    def test_prompt_omits_blocks_without_stats(self, tmp_path, monkeypatch):
        engine = self._prepare(tmp_path, monkeypatch, with_stats=False)
        engine.client = object()
        engine.model = "test-model"
        captured = {}

        def fake_chat(prompt):
            captured["prompt"] = prompt
            return json.dumps({"reasoning": "x", "direction": "SKIP",
                               "confidence": 0.4})

        engine._chat = fake_chat
        engine._llm_decision(self._event(), engine._gather_data(self._event(), None))
        assert "LEARNED EVENT STATISTICS" not in captured["prompt"]
        assert "KEY BASE RATES" not in captured["prompt"]

    def test_playbook_xref_only_when_stats_present(self, tmp_path, monkeypatch):
        """The playbook header must not point the model at a LEARNED EVENT
        STATISTICS section that is absent from the prompt."""
        engine = self._prepare(tmp_path, monkeypatch, with_stats=False)
        engine.client = object()
        engine.model = "test-model"
        (tmp_path / "event_playbooks.json").write_text(
            json.dumps({"CPI m/m": {"pattern": "fades the spike"}}),
            encoding="utf-8")
        captured = {}

        def fake_chat(prompt):
            captured["prompt"] = prompt
            return json.dumps({"reasoning": "x", "direction": "SKIP",
                               "confidence": 0.4})

        engine._chat = fake_chat
        engine._llm_decision(self._event(), engine._gather_data(self._event(), None))
        assert "EVENT PLAYBOOK" in captured["prompt"]
        assert "trust the statistics" not in captured["prompt"]
        # ...and WITH stats the cross-reference appears
        write_stats(tmp_path, {"USD|cpi m/m": cpi_entry()})
        engine._llm_decision(self._event(), engine._gather_data(self._event(), None))
        assert "trust the statistics" in captured["prompt"]

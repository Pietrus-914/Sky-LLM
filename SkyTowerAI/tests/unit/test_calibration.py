"""
Unit tests for the F4 calibration ledger (calibration.py) and its
prompt-line consumption in the decision engine.
"""
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from calibration import (index_paths, score_decision, build_summary,
                         prompt_line, _dedup_decisions)
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


def path_rec(name="cpi m/m", currency="USD", pair="USDCAD",
             time="2026-07-15T13:30", move5=20.0, test=False):
    return {
        "test": test, "currency": currency,
        "event_name": name.title(), "event_name_normalized": name,
        "event_time": time + ":00", "pair": pair,
        "move_5min_pips": move5,
    }


def decision(name="CPI m/m", currency="USD", pair="USDCAD",
             time="2026-07-15T13:30", direction="BUY", confidence=0.6,
             forced=False, ts="2026-07-15T13:28:00Z", decision_id="d1",
             model="test-model", prompt_version=None):
    """A recorded decision row. `model` and `prompt_version` matter to the
    ENGINE's prompt line, which is scoped to the running configuration so the
    second-person verdict cannot be computed from another model's history."""
    import llm_decision_engine as lde
    return {
        "timestamp": ts, "decision_id": decision_id, "event_name": name,
        "currency": currency, "pair": pair, "direction": direction,
        "confidence": confidence, "forced": forced,
        "event_datetime": time + ":00",
        "model": model,
        "prompt_version": (lde.ENTRY_PROMPT_VERSION if prompt_version is None
                           else prompt_version),
    }


class TestIndexPaths:
    def test_excludes_unusable(self):
        idx = index_paths([
            path_rec(test=True),
            dict(path_rec(time="2026-07-15T14:30"), pair=None),
            path_rec(time="2026-07-15T15:30", move5=None),
            path_rec(time="2026-07-15T16:30"),
        ])
        assert len(idx) == 1

    def test_newest_wins_on_duplicate_regardless_of_order(self):
        old = dict(path_rec(move5=10.0), recorded_at="2026-07-15T14:01:00Z")
        new = dict(path_rec(move5=30.0), recorded_at="2026-07-16T09:00:00Z")
        # get_recent() feeds newest FIRST — both orders must keep the newest
        for feed in ([new, old], [old, new]):
            idx = index_paths(feed)
            assert list(idx.values())[0]["move_5min_pips"] == 30.0


class TestScoreDecision:
    def _idx(self, **kw):
        return index_paths([path_rec(**kw)])

    def test_buy_correct_on_up_move(self):
        row = score_decision(decision(direction="BUY"), self._idx(move5=20.0))
        assert row["correct"] is True

    def test_buy_wrong_on_down_move(self):
        row = score_decision(decision(direction="BUY"), self._idx(move5=-20.0))
        assert row["correct"] is False

    def test_sell_correct_on_down_move(self):
        row = score_decision(decision(direction="SELL"), self._idx(move5=-20.0))
        assert row["correct"] is True

    def test_flat_move_not_scored(self):
        row = score_decision(decision(direction="BUY"), self._idx(move5=0.5))
        assert row is not None and row["correct"] is None

    def test_skip_gets_row_without_correctness(self):
        row = score_decision(decision(direction="SKIP"), self._idx(move5=25.0))
        assert row["direction"] == "SKIP" and row["correct"] is None

    def test_forced_excluded(self):
        assert score_decision(decision(forced=True), self._idx()) is None

    def test_unmeasured_event_returns_none(self):
        assert score_decision(decision(time="2026-07-16T13:30"),
                              self._idx()) is None

    def test_pair_slash_and_case_tolerated(self):
        row = score_decision(decision(pair="usd/cad"), self._idx())
        assert row is not None

    def test_garbage_confidence_coerced(self):
        row = score_decision(decision(confidence="high"), self._idx())
        assert row["confidence"] == 0.0


class TestDedup:
    def test_newest_decision_per_event_kept(self):
        older = decision(ts="2026-07-15T13:20:00Z", direction="SELL")
        newer = decision(ts="2026-07-15T13:28:00Z", direction="BUY")
        kept = _dedup_decisions([newer, older])
        assert len(kept) == 1 and kept[0]["direction"] == "BUY"

    def test_different_events_all_kept(self):
        a = decision()
        b = decision(name="NFP", time="2026-07-03T12:30")
        assert len(_dedup_decisions([a, b])) == 2


class TestBuildSummary:
    def test_brier_and_buckets(self):
        # 2 correct at 0.8, 1 wrong at 0.6, 1 flat, 1 skip
        paths, decisions = [], []
        specs = [("2026-07-01T13:30", 20.0, "BUY", 0.8),    # correct
                 ("2026-07-02T13:30", 15.0, "BUY", 0.8),    # correct
                 ("2026-07-03T13:30", -12.0, "BUY", 0.6),   # wrong
                 ("2026-07-04T13:30", 0.3, "BUY", 0.9),     # flat
                 ("2026-07-05T13:30", 30.0, "SKIP", 0.4)]   # skip
        for i, (t, m, direction, conf) in enumerate(specs):
            paths.append(path_rec(time=t, move5=m))
            decisions.append(decision(time=t, direction=direction,
                                      confidence=conf, decision_id=f"d{i}"))
        s = build_summary(decisions, paths)
        assert s["n_scored"] == 3
        assert s["n_flat_excluded"] == 1
        # Brier: ((0.8-1)^2 + (0.8-1)^2 + (0.6-0)^2) / 3 = 0.44/3
        assert s["brier"] == round(0.44 / 3, 3)
        assert s["hit_rate"] == round(2 / 3, 2)
        assert s["avg_confidence"] == round((0.8 + 0.8 + 0.6) / 3, 2)
        by_label = {b["label"]: b for b in s["buckets"]}
        assert by_label[">=70%"]["n"] == 2
        assert by_label[">=70%"]["hit_rate"] == 1.0
        assert by_label["55-70%"]["hit_rate"] == 0.0
        assert s["skips"] == {"n": 1, "median_abs_move_5min": 30.0,
                              "big_move_share": 1.0}

    def test_empty_inputs(self):
        s = build_summary([], [])
        assert s["n_scored"] == 0 and s["brier"] is None

    def test_duplicate_decisions_scored_once(self):
        paths = [path_rec()]
        dups = [decision(ts="2026-07-15T13:20:00Z"),
                decision(ts="2026-07-15T13:28:00Z")]
        assert build_summary(dups, paths)["n_scored"] == 1


def bulk_case(n=60, conf=0.7, hit_every=2):
    """n directional decisions with measured paths; every hit_every-th wrong."""
    paths, decisions = [], []
    for i in range(n):
        t = f"2026-{i % 12 + 1:02d}-{i // 12 + 1:02d}T13:30"
        move = 20.0 if i % hit_every else -20.0   # i%2==0 -> wrong
        paths.append(path_rec(time=t, move5=move))
        decisions.append(decision(time=t, confidence=conf,
                                  decision_id=f"d{i}", ts=f"2026-07-15T13:28:{i:02d}Z"))
    return decisions, paths


class TestPromptLine:
    def test_gated_below_50(self):
        decisions, paths = bulk_case(n=49)
        assert prompt_line(build_summary(decisions, paths)) is None

    def test_overconfident_wording(self):
        decisions, paths = bulk_case(n=60, conf=0.7, hit_every=2)  # 50% hit
        line = prompt_line(build_summary(decisions, paths))
        assert line.startswith("CALIBRATION (your last 60")
        assert "OVERCONFIDENT by 20 points" in line
        assert "Brier" in line

    def test_well_calibrated_wording(self):
        # 70% conf, 70% hit: wrong when i % 10 in (0,1,2) -> 30% wrong
        paths, decisions = [], []
        for i in range(60):
            t = f"2026-{i % 12 + 1:02d}-{i // 12 + 1:02d}T13:30"
            move = -20.0 if i % 10 < 3 else 20.0
            paths.append(path_rec(time=t, move5=move))
            decisions.append(decision(time=t, confidence=0.7,
                                      decision_id=f"d{i}",
                                      ts=f"2026-07-15T13:28:{i:02d}Z"))
        line = prompt_line(build_summary(decisions, paths))
        assert "well calibrated" in line


class TestEngineIntegration:
    def _engine(self, tmp_path, paths_provider):
        engine = LLMDecisionEngine(
            provider="rule-based",
            decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
            trade_history_file=str(tmp_path / "trades.jsonl"),
            paths_provider=paths_provider)
        engine.learned_stats_file = str(tmp_path / "learned_stats.json")
        engine.playbooks_file = str(tmp_path / "playbooks.json")
        return engine

    def test_no_provider_no_line(self, tmp_path):
        assert self._engine(tmp_path, None)._calibration_line() is None

    def test_line_from_provider_data(self, tmp_path):
        decisions, paths = bulk_case(n=60)
        engine = self._engine(tmp_path, lambda: paths)
        engine.model = "test-model"          # matches the recorded rows
        engine.decision_log = Mock()
        engine.decision_log.get_recent.return_value = decisions
        line = engine._calibration_line()
        assert line is not None and "CALIBRATION" in line

    def test_no_line_from_another_models_history(self, tmp_path):
        """The line says "you have been OVERCONFIDENT" — it must never be
        computed from a different model's or prompt version's calls."""
        decisions, paths = bulk_case(n=60)
        engine = self._engine(tmp_path, lambda: paths)
        engine.model = "some-other-model"
        engine.decision_log = Mock()
        engine.decision_log.get_recent.return_value = decisions
        assert engine._calibration_line() is None

    def test_provider_exception_contained(self, tmp_path):
        def boom():
            raise RuntimeError("recorder gone")
        engine = self._engine(tmp_path, boom)
        engine.cot_analyzer = Mock()
        engine.cot_analyzer.analyze_currency.return_value = {"signal": "X"}
        engine.sentiment = Mock()
        engine.sentiment.get_currency_sentiment.return_value = {
            "signal": "X", "pairs_analyzed": 1}
        engine.reaction_history = Mock()
        engine.reaction_history.summarize.return_value = None
        engine.reaction_history.summarize_currency_fallback.return_value = None
        from types import SimpleNamespace
        from datetime import datetime, timedelta
        event = SimpleNamespace(event_name="CPI m/m", currency="USD",
                                datetime_utc=datetime.utcnow() + timedelta(minutes=30),
                                impact="HIGH", forecast="0.3%", previous="0.2%")
        ctx = engine._gather_data(event, None)
        assert ctx["calibration_line"] is None
        assert ctx["_source_status"]["calibration"] == "no_data"


class TestSpreadAwareLedger:
    """2026-07-26: direction-hit alone flatters the model — the ledger now
    also reports net EV (5-min move minus news spread), playable hit rate
    and a per-model breakdown (decision rows carry `model`)."""

    def test_row_carries_net_pips_and_playable(self):
        idx = index_paths([path_rec(move5=20.0)])   # USDCAD, news spread 4.0
        row = score_decision(decision(direction="BUY", confidence=0.7), idx)
        assert row["correct"] is True
        assert row["spread_pips"] == 4.0
        assert row["net_pips"] == pytest.approx(16.0)
        assert row["playable"] is True

    def test_wrong_direction_pays_move_plus_spread(self):
        idx = index_paths([path_rec(move5=20.0)])
        row = score_decision(decision(direction="SELL"), idx)
        assert row["correct"] is False
        assert row["net_pips"] == pytest.approx(-24.0)

    def test_sub_spread_move_is_not_playable(self):
        idx = index_paths([path_rec(move5=2.0)])
        row = score_decision(decision(direction="BUY"), idx)
        assert row["correct"] is True
        assert row["playable"] is False

    def test_summary_net_ev_playable_and_by_model(self):
        paths, decisions = [], []
        for i in range(10):
            t = f"2026-07-15T{10 + i:02d}:30"
            paths.append(path_rec(time=t, move5=20.0))
            d = decision(time=t, ts=f"2026-07-15T{10 + i:02d}:28:00Z",
                         direction="BUY", confidence=0.6,
                         decision_id=f"d{i}")
            d["model"] = "anthropic/claude-fable-5"
            decisions.append(d)
        summary = build_summary(decisions, paths)
        assert summary["n_scored"] == 10
        assert summary["net_ev_pips"] == pytest.approx(16.0)
        assert summary["playable"] == {"n": 10, "hit_rate": 1.0}
        assert summary["by_model"] == [{
            "model": "anthropic/claude-fable-5", "n": 10,
            "hit_rate": 1.0, "avg_confidence": 0.6,
            "net_ev_pips": 16.0,
        }]


class TestModelAndPromptVersionRecording:
    """Calibration must be groupable per model and prompt version."""

    def test_decision_history_records_model_fields(self, tmp_path):
        from llm_decision_engine import (ENTRY_PROMPT_VERSION,
                                         TradingDecision)
        dh = DecisionHistory(log_dir=str(tmp_path))
        dh.record(TradingDecision(
            event="CPI m/m", currency="USD", pair="USD/CAD",
            direction="BUY", confidence=0.6, lot_percent=70,
            entry_seconds_before=15, exit_minutes_after=10,
            stop_loss_percent=40,
            model="panel:a,b,c", prompt_version=ENTRY_PROMPT_VERSION,
        ))
        row = dh.get_recent(1)[0]
        assert row["model"] == "panel:a,b,c"
        assert row["prompt_version"] == ENTRY_PROMPT_VERSION

    def test_rule_based_decision_labels_itself(self):
        from types import SimpleNamespace
        from datetime import datetime, timedelta
        engine = LLMDecisionEngine()
        event = SimpleNamespace(
            event_name="CPI m/m", currency="USD",
            datetime_utc=datetime.utcnow() + timedelta(minutes=30),
            impact="HIGH", forecast="0.3%", previous="0.2%", actual=None,
        )
        ctx = engine._gather_data(event, None)
        result = engine._rule_based_decision(event, ctx)
        assert result.model == "rule-based"

"""
Unit tests for the F4 K-call self-consistency ensemble
(SKYTOWER_ENSEMBLE_K >= 2): unanimity gates the trade, splits SKIP,
FORCE_DECISION uses majority, metadata is persisted.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import llm_decision_engine as lde
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


def make_engine(tmp_path):
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trades.jsonl"),
    )
    engine.playbooks_file = str(tmp_path / "playbooks.json")
    engine.learned_stats_file = str(tmp_path / "learned_stats.json")
    engine.client = object()
    engine.model = "test-model"
    engine.cot_analyzer = Mock()
    engine.cot_analyzer.analyze_currency.return_value = {"signal": "BULLISH"}
    engine.sentiment = Mock()
    engine.sentiment.get_currency_sentiment.return_value = {
        "signal": "SHORT", "pairs_analyzed": 1}
    engine.reaction_history = Mock()
    engine.reaction_history.summarize.return_value = None
    engine.reaction_history.summarize_currency_fallback.return_value = None
    engine.reaction_history.get_matching.return_value = []
    return engine


def make_event():
    return SimpleNamespace(
        event_name="CPI m/m", currency="USD",
        datetime_utc=datetime.utcnow() + timedelta(minutes=30),
        impact="HIGH", forecast="0.3%", previous="0.2%")


def reply(direction="BUY", confidence=0.7, lot=70, exit_m=10, sl=40, tp=60,
          reasoning="looks good"):
    return json.dumps({"reasoning": reasoning, "direction": direction,
                       "confidence": confidence, "lot_percent": lot,
                       "exit_minutes": exit_m, "stop_loss_pips": sl,
                       "take_profit_pips": tp, "stop_loss_percent": 40})


def run_ensemble(tmp_path, monkeypatch, replies, k=3, force=False):
    """Drive _llm_decision with ENSEMBLE_K=k and scripted _chat replies."""
    engine = make_engine(tmp_path)
    monkeypatch.setattr(lde, "ENSEMBLE_K", k)
    monkeypatch.setattr(lde, "FORCE_DECISION", force)
    calls = {"n": 0}

    def fake_chat(prompt):
        i = calls["n"]
        calls["n"] += 1
        r = replies[i % len(replies)]
        if isinstance(r, Exception):
            raise r
        return r

    engine._chat = fake_chat
    ctx = engine._gather_data(make_event(), None)
    decision = engine._llm_decision(make_event(), ctx)
    return decision, calls["n"], engine


class TestUnanimity:
    def test_unanimous_buy_trades(self, tmp_path, monkeypatch):
        d, n_calls, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.6), reply("BUY", 0.8), reply("BUY", 0.7)])
        assert d.direction == "BUY"
        assert n_calls == 3
        assert d.confidence == 0.7          # median of 0.6/0.7/0.8
        assert d.ensemble["k"] == 3 and d.ensemble["valid"] == 3
        assert "unanimous BUY" in d.reasoning
        assert d.raw_response.count("ENSEMBLE CALL BOUNDARY") == 2

    def test_unanimous_sell_trades(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("SELL", 0.6), reply("SELL", 0.6), reply("SELL", 0.9)])
        assert d.direction == "SELL"

    def test_numeric_fields_are_medians_with_final_clamp(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.7, lot=60, exit_m=5, sl=30, tp=40),
            reply("BUY", 0.7, lot=70, exit_m=10, sl=50, tp=60),
            reply("BUY", 0.7, lot=85, exit_m=15, sl=70, tp=110)])
        assert d.lot_percent == 70
        assert d.exit_minutes_after == 10
        assert d.stop_loss_pips == 50
        assert d.take_profit_pips == 60

    def test_sl_zero_sentinel_never_averaged(self, tmp_path, monkeypatch):
        # 0 = "not set" SENTINEL, not a magnitude. Zeros >= real votes -> 0.
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.7, sl=0), reply("BUY", 0.7, sl=0),
            reply("BUY", 0.7, sl=40)])
        assert d.stop_loss_pips == 0
        # Even split [0, 40]: tie goes to "not set" (EA fallback), NOT a
        # fabricated 20->25 stop nobody proposed (review finding)
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.7, sl=0), reply("BUY", 0.7, sl=40)], k=2)
        assert d.stop_loss_pips == 0
        # Real-vote majority: median over the REAL proposals only
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.7, sl=0), reply("BUY", 0.7, sl=40),
            reply("BUY", 0.7, sl=50)])
        assert d.stop_loss_pips == 45.0

    def test_null_reasoning_from_voter_does_not_crash(self, tmp_path,
                                                      monkeypatch):
        # JSON null reasoning: .get('reasoning', default) returns None when
        # the KEY EXISTS — must not TypeError the whole event (review finding)
        r = json.loads(reply("BUY", 0.7))
        r["reasoning"] = None
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [json.dumps(r)] * 3)
        assert d.direction == "BUY"
        assert "LLM decision" in d.reasoning


class TestSplits:
    def test_two_one_split_skips(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.8), reply("BUY", 0.9), reply("SELL", 0.6)])
        assert d.direction == "SKIP"
        assert "no unanimity" in d.reasoning
        assert d.ensemble["votes"][2]["direction"] == "SELL"

    def test_skip_vote_breaks_unanimity(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.8), reply("BUY", 0.9), reply("SKIP", 0.4)])
        assert d.direction == "SKIP"

    def test_unanimous_skip_skips(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("SKIP", 0.3), reply("SKIP", 0.4), reply("SKIP", 0.5)])
        assert d.direction == "SKIP"
        assert "all 3 votes SKIP" in d.reasoning
        assert "split" not in d.reasoning

    def test_lone_valid_vote_skips(self, tmp_path, monkeypatch):
        boom = RuntimeError("api down")
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            boom, boom, reply("BUY", 0.9)])
        assert d.direction == "SKIP"
        assert "only 1/3" in d.reasoning
        assert d.ensemble["valid"] == 1

    def test_all_calls_failed_falls_back_to_rules(self, tmp_path, monkeypatch):
        boom = RuntimeError("api down")
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [boom, boom, boom])
        assert d.direction in ("BUY", "SELL", "SKIP")
        assert d.ensemble is None           # rule-based fallback, no committee


class TestForceMode:
    def test_majority_wins_in_force_mode(self, tmp_path, monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.9), reply("BUY", 0.6), reply("SELL", 0.6)],
            force=True)
        assert d.direction == "BUY"
        assert d.forced is True
        # confidence = mean(0.9, 0.6) * agreement 2/3 = 0.75 * 0.667 = 0.5
        assert d.confidence == 0.5
        assert "majority BUY" in d.reasoning

    def test_all_skip_votes_remap_via_rules_in_force_mode(self, tmp_path,
                                                          monkeypatch):
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("SKIP", 0.4), reply("SKIP", 0.3), reply("SKIP", 0.5)],
            force=True)
        assert d.direction in ("BUY", "SELL")
        assert "all 3 votes SKIP" in d.reasoning

    def test_force_mode_dead_tie_resolved_by_rules_not_alphabet(self, tmp_path,
                                                                monkeypatch):
        # K=4, 2-2: must go through _forced_direction (rule scores), not
        # silently pick BUY (review finding)
        d, _, _ = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.8), reply("BUY", 0.7),
            reply("SELL", 0.8), reply("SELL", 0.7)], k=4, force=True)
        assert d.direction in ("BUY", "SELL")
        assert "tie" in d.reasoning


class TestSingleCallPathUntouched:
    def test_k1_makes_exactly_one_call(self, tmp_path, monkeypatch):
        d, n_calls, _ = run_ensemble(tmp_path, monkeypatch,
                                     [reply("BUY", 0.8)], k=1)
        assert n_calls == 1
        assert d.direction == "BUY"
        assert d.ensemble is None


class TestPersistence:
    def test_ensemble_metadata_recorded_in_history(self, tmp_path, monkeypatch):
        d, _, engine = run_ensemble(tmp_path, monkeypatch, [
            reply("BUY", 0.6), reply("BUY", 0.8), reply("BUY", 0.7)])
        engine.decision_log.record(d)
        rows = engine.decision_log.get_recent(5)
        assert rows[0]["ensemble"]["valid"] == 3
        assert len(rows[0]["ensemble"]["votes"]) == 3

    def test_single_call_rows_stay_slim(self, tmp_path, monkeypatch):
        d, _, engine = run_ensemble(tmp_path, monkeypatch,
                                    [reply("BUY", 0.8)], k=1)
        engine.decision_log.record(d)
        assert "ensemble" not in engine.decision_log.get_recent(5)[0]


# ============================================================================
# Mixed panel (F4c): SKYTOWER_ENSEMBLE_MODELS — one vote per model,
# anchor-gated aggregation (confirm trades, opposite vetoes, SKIP abstains)
# ============================================================================

PANEL = ["model-anchor", "model-b", "model-c"]


def run_panel(tmp_path, monkeypatch, script, force=False, models=None,
              provider="openrouter", k=1):
    """Drive _llm_decision with a heterogeneous panel. `script` maps model
    id -> reply string or Exception. Records which model each call used."""
    engine = make_engine(tmp_path)
    engine.provider = provider
    monkeypatch.setattr(lde, "ENSEMBLE_K", k)
    monkeypatch.setattr(lde, "ENSEMBLE_MODELS", list(models or PANEL))
    monkeypatch.setattr(lde, "FORCE_DECISION", force)
    routed = []

    def fake_chat(prompt, model=None):
        routed.append(model)
        r = script[model]
        if isinstance(r, Exception):
            raise r
        return r

    engine._chat = fake_chat
    ctx = engine._gather_data(make_event(), None)
    decision = engine._llm_decision(make_event(), ctx)
    return decision, routed, engine


class TestMixedPanel:
    def test_anchor_plus_confirmation_trades_with_anchor_confidence(
            self, tmp_path, monkeypatch):
        d, routed, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("BUY", 0.8, reasoning="anchor essay"),
            "model-b": reply("BUY", 0.4),      # cheaper model, own scale
            "model-c": reply("SKIP", 0.9)})    # abstains, does not block
        assert d.direction == "BUY"
        assert d.confidence == 0.8             # anchor's, never averaged
        assert "anchor essay" in d.reasoning
        assert "ENSEMBLE panel 2/3 BUY" in d.reasoning
        assert routed == PANEL                 # one call per listed model
        assert d.ensemble["anchor"] == "model-anchor"
        assert d.ensemble["panel"] == PANEL
        models_in_votes = [v["model"] for v in d.ensemble["votes"]]
        assert models_in_votes == PANEL

    def test_opposite_vote_vetoes(self, tmp_path, monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("BUY", 0.9),
            "model-b": reply("SELL", 0.6),
            "model-c": reply("BUY", 0.8)})
        assert d.direction == "SKIP"
        assert "veto" in d.reasoning
        assert "model-b" in d.reasoning

    def test_unconfirmed_anchor_skips(self, tmp_path, monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("BUY", 0.9),
            "model-b": reply("SKIP", 0.5),
            "model-c": reply("SKIP", 0.4)})
        assert d.direction == "SKIP"
        assert "unconfirmed" in d.reasoning

    def test_anchor_skip_gates_even_when_others_agree(self, tmp_path,
                                                      monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("SKIP", 0.4),
            "model-b": reply("BUY", 0.9),
            "model-c": reply("BUY", 0.9)})
        assert d.direction == "SKIP"
        assert "anchor voted SKIP" in d.reasoning

    def test_anchor_call_failure_skips(self, tmp_path, monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": RuntimeError("api down"),
            "model-b": reply("BUY", 0.9),
            "model-c": reply("BUY", 0.9)})
        assert d.direction == "SKIP"
        assert "call failed" in d.reasoning
        assert d.ensemble["valid"] == 2

    def test_panel_raw_dump_labels_models(self, tmp_path, monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("BUY", 0.8),
            "model-b": reply("BUY", 0.7),
            "model-c": reply("BUY", 0.6)})
        assert "[model-anchor]" in d.raw_response
        assert "[model-c]" in d.raw_response

    def test_force_mode_majority_still_works_with_panel(self, tmp_path,
                                                        monkeypatch):
        d, _, _ = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("SELL", 0.6),
            "model-b": reply("BUY", 0.8),
            "model-c": reply("BUY", 0.7)}, force=True)
        assert d.direction == "BUY"
        assert d.forced is True
        assert "majority BUY" in d.reasoning

    def test_non_openrouter_provider_falls_back_to_single_call(
            self, tmp_path, monkeypatch):
        """Models-only config on a single-vendor SDK must degrade to the
        classic single call, not a 1-vote committee that SKIPs everything."""
        engine = make_engine(tmp_path)
        engine.provider = "anthropic"
        monkeypatch.setattr(lde, "ENSEMBLE_K", 1)
        monkeypatch.setattr(lde, "ENSEMBLE_MODELS", list(PANEL))
        monkeypatch.setattr(lde, "FORCE_DECISION", False)
        calls = {"n": 0}

        def fake_chat(prompt, model=None):
            calls["n"] += 1
            return reply("BUY", 0.8)

        engine._chat = fake_chat
        ctx = engine._gather_data(make_event(), None)
        d = engine._llm_decision(make_event(), ctx)
        assert calls["n"] == 1
        assert d.direction == "BUY"
        assert d.ensemble is None

    def test_panel_metadata_persisted_in_history(self, tmp_path, monkeypatch):
        d, _, engine = run_panel(tmp_path, monkeypatch, {
            "model-anchor": reply("BUY", 0.8),
            "model-b": reply("BUY", 0.7),
            "model-c": reply("SELL", 0.6)})
        engine.decision_log.record(d)
        row = engine.decision_log.get_recent(5)[0]
        assert row["ensemble"]["anchor"] == "model-anchor"
        assert row["ensemble"]["votes"][1]["model"] == "model-b"

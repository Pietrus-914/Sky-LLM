"""
Unit tests for F5d: the replay harness's pure parts (split, episode
loading/join, scoring, prompt rebuild from a frozen dump). The LLM call
path is offline-only and not tested here.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from tools.replay_decisions import (split_of, load_episodes, score_rows,
                                    HOLDOUT_BUCKETS)


def write_dump(ctx_dir, decision_id, direction="BUY", confidence=0.7,
               forced=False, event="CPI m/m", currency="USD",
               pair="USDCAD", minute="2026-07-15T13:30", data_summary=None):
    os.makedirs(ctx_dir, exist_ok=True)
    dump = {
        "decision_id": decision_id, "event_name": event, "currency": currency,
        "pair": pair, "direction": direction, "confidence": confidence,
        "forced": forced, "event_datetime": minute + ":00",
        "data_summary": data_summary if data_summary is not None else {
            "event": {"name": event, "currency": currency},
            "suggested_pair": pair},
    }
    with open(os.path.join(ctx_dir, f"{decision_id}.json"), 'w',
              encoding='utf-8') as f:
        json.dump(dump, f)


def write_paths(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def path_row(event="cpi m/m", currency="USD", pair="USDCAD",
             minute="2026-07-15T13:30", move5=18.0):
    return {"test": False, "currency": currency, "event_name": event.title(),
            "event_name_normalized": event, "event_time": minute + ":00",
            "pair": pair, "move_5min_pips": move5}


class TestSplit:
    def test_deterministic(self):
        assert split_of("abc123") == split_of("abc123")

    def test_roughly_twenty_percent_holdout(self):
        ids = [f"id-{i}" for i in range(2000)]
        holdout = sum(1 for i in ids if split_of(i) == "holdout")
        assert 0.15 < holdout / len(ids) < 0.25

    def test_both_splits_reachable(self):
        splits = {split_of(f"id-{i}") for i in range(50)}
        assert splits == {"dev", "holdout"}


class TestLoadEpisodes:
    def test_join_and_skip_accounting(self, tmp_path):
        ctx = str(tmp_path / "ctx")
        write_dump(ctx, "d1")                                   # measured
        write_dump(ctx, "d2", minute="2026-07-16T13:30")        # NOT measured
        (tmp_path / "ctx" / "broken.json").write_text("{not json",
                                                      encoding="utf-8")
        paths_file = str(tmp_path / "paths.jsonl")
        write_paths(paths_file, [path_row()])
        episodes, skipped = load_episodes(ctx, paths_file)
        assert len(episodes) == 1 and episodes[0]["decision_id"] == "d1"
        assert episodes[0]["move_5min_pips"] == 18.0
        assert skipped == 2

    def test_pair_slash_tolerated(self, tmp_path):
        ctx = str(tmp_path / "ctx")
        write_dump(ctx, "d1", pair="USD/CAD")
        paths_file = str(tmp_path / "paths.jsonl")
        write_paths(paths_file, [path_row()])
        episodes, _ = load_episodes(ctx, paths_file)
        assert len(episodes) == 1

    def test_missing_paths_file(self, tmp_path):
        ctx = str(tmp_path / "ctx")
        write_dump(ctx, "d1")
        episodes, skipped = load_episodes(ctx, str(tmp_path / "nope.jsonl"))
        assert episodes == [] and skipped == 1


class TestScoreRows:
    def test_math_matches_calibration_conventions(self):
        rows = [
            {"direction": "BUY", "confidence": 0.8, "move_5min_pips": 20.0},
            {"direction": "BUY", "confidence": 0.6, "move_5min_pips": -20.0},
            {"direction": "SELL", "confidence": 0.7, "move_5min_pips": -5.0},
            {"direction": "BUY", "confidence": 0.9, "move_5min_pips": 0.5},
            {"direction": "SKIP", "confidence": 0.4, "move_5min_pips": 30.0},
        ]
        s = score_rows(rows)
        assert s["n"] == 3 and s["skips"] == 1 and s["flat"] == 1
        assert s["hit_rate"] == round(2 / 3, 3)
        expected_brier = round(((0.8 - 1) ** 2 + (0.6 - 0) ** 2
                                + (0.7 - 1) ** 2) / 3, 3)
        assert s["brier"] == expected_brier

    def test_empty(self):
        s = score_rows([])
        assert s["n"] == 0 and s["hit_rate"] is None


class TestPromptRebuild:
    def test_frozen_data_summary_rebuilds_prompt(self, tmp_path):
        """A real _gather_data context, JSON round-tripped (as the dump on
        disk is), must rebuild through the CURRENT _entry_prompt."""
        from unittest.mock import Mock
        from types import SimpleNamespace
        from datetime import datetime, timedelta
        from llm_decision_engine import LLMDecisionEngine
        from decision_history import DecisionHistory
        engine = LLMDecisionEngine(
            provider="rule-based",
            decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
            trade_history_file=str(tmp_path / "t.jsonl"))
        engine.playbooks_file = str(tmp_path / "p.json")
        engine.learned_stats_file = str(tmp_path / "l.json")
        from reflections import ReflectionStore
        engine.reflection_store = ReflectionStore(str(tmp_path / "r.jsonl"))
        engine.cot_analyzer = Mock()
        engine.cot_analyzer.analyze_currency.return_value = {"signal": "X"}
        engine.sentiment = Mock()
        engine.sentiment.get_currency_sentiment.return_value = {
            "signal": "X", "pairs_analyzed": 1}
        engine.reaction_history = Mock()
        engine.reaction_history.summarize.return_value = None
        engine.reaction_history.summarize_currency_fallback.return_value = None
        event = SimpleNamespace(event_name="CPI m/m", currency="USD",
                                datetime_utc=datetime.utcnow() + timedelta(minutes=30),
                                impact="HIGH", forecast="0.3%", previous="0.2%")
        ctx = engine._gather_data(event, None)
        frozen = json.loads(json.dumps(ctx, default=str))
        prompt = engine._entry_prompt(frozen)
        assert "EVENT DETAILS" in prompt
        assert "CPI m/m" in prompt
        assert prompt == engine._entry_prompt(frozen)   # deterministic

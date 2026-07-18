"""
Unit tests for F5a (similar-episode retrieval) and F5b (post-trade
reflections with n=1 quarantine).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from episode_retrieval import find_similar_episodes, render_episodes
from reflections import (ReflectionStore, render_for_prompt, trade_eligible,
                         generate_and_store, MAX_REFLECTION_CHARS)
from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


def ep(name="cpi m/m", currency="USD", pair="USDCAD", time_="2026-05-13T12:30",
       move5=18.2, move30=25.0, hi=20.0, lo=-3.1, regime="hiking",
       forecast="0.3%", previous="0.2%", actual="0.4%", surprise="BEAT",
       test=False):
    return {
        "test": test, "currency": currency,
        "event_name": name.title(), "event_name_normalized": name,
        "event_time": time_ + ":00", "pair": pair, "regime": regime,
        "forecast": forecast, "previous": previous, "actual": actual,
        "surprise": surprise,
        "move_5min_pips": move5, "move_30min_pips": move30,
        "first5_high_pips": hi, "first5_low_pips": lo,
    }


class TestFindSimilarEpisodes:
    def test_filters_name_currency_test_and_no_move(self):
        paths = [ep(),
                 ep(name="ppi m/m", time_="2026-05-14T12:30"),
                 ep(currency="CAD", time_="2026-05-15T12:30"),
                 ep(test=True, time_="2026-05-16T12:30"),
                 ep(move5=None, time_="2026-05-17T12:30")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking")
        assert len(out) == 1

    def test_one_episode_per_release_prefers_requested_pair(self):
        paths = [ep(pair="AUDUSD"), ep(pair="USDCAD")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking")
        assert len(out) == 1 and out[0]["pair"] == "USDCAD"

    def test_scoring_pair_beats_regime(self):
        paths = [ep(pair="AUDUSD", regime="hiking", time_="2026-06-10T12:30"),
                 ep(pair="USDCAD", regime="cutting", time_="2026-05-13T12:30")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking",
                                    limit=2)
        assert out[0]["pair"] == "USDCAD"     # +4 pair beats +2 regime

    def test_recency_breaks_ties(self):
        paths = [ep(time_="2026-03-10T12:30"), ep(time_="2026-06-10T12:30")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking",
                                    limit=2)
        assert out[0]["event_time"].startswith("2026-06-10")

    def test_forecast_direction_bonus(self):
        # Today: forecast > previous ("up"); episode A matches, B doesn't
        paths = [ep(time_="2026-05-10T12:30", forecast="0.1%", previous="0.4%"),
                 ep(time_="2026-04-10T12:30", forecast="0.5%", previous="0.2%")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking",
                                    forecast="0.3%", previous="0.2%", limit=2)
        assert out[0]["event_time"].startswith("2026-04-10")

    def test_limit(self):
        paths = [ep(time_=f"2026-{m:02d}-10T12:30") for m in range(1, 7)]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking")
        assert len(out) == 3


class TestRenderEpisodes:
    def test_line_contains_setup_path_and_decision(self):
        episode = ep()
        decision = {"forced": False, "currency": "USD", "event_name": "CPI m/m",
                    "event_datetime": "2026-05-13T12:30:00",
                    "direction": "BUY", "confidence": 0.65,
                    "decision_id": "d-1"}
        trade = {"decision_id": "d-1", "profit_usd": 42.1}
        text = render_episodes([episode], [decision], [trade])
        assert "2026-05-13 (hiking)" in text
        assert "actual 0.4% (BEAT)" in text
        assert "USDCAD 5min +18.2p" in text
        assert "you then chose BUY@65% -> CORRECT" in text
        assert "realized $+42.10" in text

    def test_wrong_call_and_no_decision(self):
        episode = ep(move5=-18.2)
        decision = {"forced": False, "currency": "USD", "event_name": "CPI m/m",
                    "event_datetime": "2026-05-13T12:30:00",
                    "direction": "BUY", "confidence": 0.65}
        text = render_episodes([episode], [decision], [])
        assert "WRONG" in text
        text2 = render_episodes([episode], [], [])
        assert "you then chose" not in text2

    def test_forced_decisions_not_joined(self):
        episode = ep()
        decision = {"forced": True, "currency": "USD", "event_name": "CPI m/m",
                    "event_datetime": "2026-05-13T12:30:00", "direction": "BUY"}
        assert "you then chose" not in render_episodes([episode], [decision], [])

    def test_empty(self):
        assert render_episodes([], [], []) is None


def make_engine(tmp_path, paths=None):
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trades.jsonl"),
        paths_provider=(lambda: paths) if paths is not None else None,
        regime_provider=lambda c: "hiking",
    )
    engine.playbooks_file = str(tmp_path / "playbooks.json")
    engine.learned_stats_file = str(tmp_path / "learned_stats.json")
    engine.reflection_store = ReflectionStore(str(tmp_path / "refl.jsonl"))
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
    return SimpleNamespace(event_name="CPI m/m", currency="USD",
                           datetime_utc=datetime.utcnow() + timedelta(minutes=30),
                           impact="HIGH", forecast="0.3%", previous="0.2%")


class TestEngineEpisodes:
    def test_no_provider_no_section(self, tmp_path):
        engine = make_engine(tmp_path, paths=None)
        assert engine._episodes_section(make_event(), "USD", "USDCAD") is None

    def test_section_from_paths(self, tmp_path):
        engine = make_engine(tmp_path, paths=[ep()])
        text = engine._episodes_section(make_event(), "USD", "USD/CAD")
        assert "2026-05-13" in text

    def test_gather_data_wires_episodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_decision_engine.FORCE_DECISION", False)
        engine = make_engine(tmp_path, paths=[ep()])
        ctx = engine._gather_data(make_event(), None)
        assert ctx["episodes"] is not None
        assert ctx["_source_status"]["episodes"] == "ok"


class TestReflectionStore:
    def test_roundtrip_and_tolerance(self, tmp_path):
        path = tmp_path / "refl.jsonl"
        path.write_text('{"currency": "USD", "event_name": "CPI m/m", "text": "a"}\n'
                        'BROKEN LINE\n', encoding="utf-8")
        store = ReflectionStore(str(path))
        assert len(store._load()) == 1
        store.append({"currency": "USD", "event_name": "CPI m/m", "text": "b"})
        assert len(store._load()) == 2

    def test_matching_exact_first_then_currency(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "refl.jsonl"))
        store.append({"currency": "USD", "event_name": "PPI m/m", "text": "old ppi"})
        store.append({"currency": "USD", "event_name": "CPI m/m", "text": "cpi note"})
        store.append({"currency": "CAD", "event_name": "CPI m/m", "text": "cad note"})
        rows = store.get_matching("CPI m/m", "USD")
        assert [r["text"] for r in rows] == ["cpi note", "old ppi"]


class TestEligibility:
    def test_gates(self):
        good = {"forced": False, "event_name": "CPI m/m", "direction": "BUY"}
        assert trade_eligible(good)
        assert not trade_eligible(dict(good, forced=True))
        assert not trade_eligible(dict(good, direction="?"))
        assert not trade_eligible(dict(good,
                                       event_name="(position lost in restart)"))


class TestGeneration:
    def _trade(self):
        return {"forced": False, "event_name": "CPI m/m", "direction": "BUY",
                "symbol": "USDCAD", "profit_usd": -67.0, "reason": "SL hit",
                "decision_id": "d-9", "entry_reasoning": "beat expected"}

    def test_record_shape_and_currency_override(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "r.jsonl"))
        rec = generate_and_store(lambda s, u: "Stop was inside the wick.",
                                 store, self._trade(), currency="CAD")
        assert rec["currency"] == "CAD"
        assert rec["n_label"] == "n=1"
        assert store.get_matching("CPI m/m", "CAD")[0]["text"] == \
            "Stop was inside the wick."

    def test_symbol_base_fallback(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "r.jsonl"))
        rec = generate_and_store(lambda s, u: "x", store, self._trade())
        assert rec["currency"] == "USD"

    def test_long_reply_trimmed(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "r.jsonl"))
        rec = generate_and_store(lambda s, u: "word " * 200, store, self._trade())
        assert len(rec["text"]) <= MAX_REFLECTION_CHARS + 3

    def test_chat_failure_returns_none(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "r.jsonl"))

        def boom(s, u):
            raise RuntimeError("api")
        assert generate_and_store(boom, store, self._trade()) is None
        assert generate_and_store(None, store, self._trade()) is None

    def test_forced_trade_never_generates(self, tmp_path):
        store = ReflectionStore(str(tmp_path / "r.jsonl"))
        calls = []
        rec = generate_and_store(lambda s, u: calls.append(1) or "x", store,
                                 dict(self._trade(), forced=True))
        assert rec is None and not calls


class TestPromptIntegration:
    def test_blocks_present_and_ordered(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_decision_engine.FORCE_DECISION", False)
        monkeypatch.setattr("llm_decision_engine.ENSEMBLE_K", 1)
        engine = make_engine(tmp_path, paths=[ep()])
        engine.reflection_store.append(
            {"currency": "USD", "event_name": "CPI m/m", "direction": "BUY",
             "pair": "USDCAD", "profit_usd": -67.0, "reason": "SL hit",
             "timestamp": "2026-07-14T12:20:00Z", "text": "note"})
        engine.client = object()
        engine.model = "test-model"
        captured = {}

        def fake_chat(prompt):
            captured["prompt"] = prompt
            return json.dumps({"reasoning": "x", "direction": "SKIP",
                               "confidence": 0.4})

        engine._chat = fake_chat
        engine._llm_decision(make_event(), engine._gather_data(make_event(), None))
        p = captured["prompt"]
        assert "SIMILAR PAST EPISODES" in p
        assert "POST-TRADE REFLECTIONS" in p
        assert "QUARANTINED n=1" in p
        assert p.index("SIMILAR PAST EPISODES") < p.index("YOUR TRACK RECORD")
        assert p.index("RECENT TRADE OUTCOMES") < p.index("POST-TRADE REFLECTIONS")

    def test_blocks_absent_without_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_decision_engine.FORCE_DECISION", False)
        monkeypatch.setattr("llm_decision_engine.ENSEMBLE_K", 1)
        engine = make_engine(tmp_path, paths=[])
        engine.client = object()
        engine.model = "test-model"
        captured = {}

        def fake_chat(prompt):
            captured["prompt"] = prompt
            return json.dumps({"reasoning": "x", "direction": "SKIP",
                               "confidence": 0.4})

        engine._chat = fake_chat
        engine._llm_decision(make_event(), engine._gather_data(make_event(), None))
        assert "SIMILAR PAST EPISODES" not in captured["prompt"]
        assert "POST-TRADE REFLECTIONS" not in captured["prompt"]


class TestServerSpawn:
    def test_reflection_spawn_appends_record(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "REFLECTIONS_ENABLED", True)
        import server
        orig = (server._reflection_chat_fn, server._reflection_chat_ready,
                server.decision_engine, server.decision_history)
        try:
            store = ReflectionStore(str(tmp_path / "r.jsonl"))
            server._reflection_chat_fn = lambda s, u: "quick note"
            server._reflection_chat_ready = True
            server.decision_engine = SimpleNamespace(reflection_store=store)
            server.decision_history = Mock()
            server.decision_history.get_recent.return_value = [
                {"decision_id": "d-9", "currency": "CAD"}]
            server._spawn_reflection({"forced": False, "event_name": "CPI m/m",
                                      "direction": "BUY", "symbol": "USDCAD",
                                      "profit_usd": 5.0, "reason": "AI close",
                                      "decision_id": "d-9"})
            deadline = time.time() + 3
            rows = []
            while time.time() < deadline:
                rows = store.get_matching("CPI m/m", "CAD")
                if rows:
                    break
                time.sleep(0.05)
            assert rows and rows[0]["text"] == "quick note"
            assert rows[0]["currency"] == "CAD"
        finally:
            (server._reflection_chat_fn, server._reflection_chat_ready,
             server.decision_engine, server.decision_history) = orig

    def test_forced_trade_not_spawned(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "REFLECTIONS_ENABLED", True)
        import server
        orig = (server._reflection_chat_fn, server._reflection_chat_ready,
                server.decision_engine)
        try:
            store = ReflectionStore(str(tmp_path / "r.jsonl"))
            calls = []
            server._reflection_chat_fn = lambda s, u: calls.append(1) or "x"
            server._reflection_chat_ready = True
            server.decision_engine = SimpleNamespace(reflection_store=store)
            server._spawn_reflection({"forced": True, "event_name": "CPI m/m",
                                      "direction": "BUY"})
            time.sleep(0.2)
            assert not calls
        finally:
            (server._reflection_chat_fn, server._reflection_chat_ready,
             server.decision_engine) = orig


class TestReviewRegressions:
    def test_one_malformed_episode_keeps_good_rows(self):
        """Review finding: a corrupted row must drop that ROW, not the section."""
        good = ep()
        bad = dict(ep(time_="2026-04-10T12:30"), forecast={"weird": 1})
        text = render_episodes([bad, good], [], [])
        assert text is not None and "2026-05-13" in text

    def test_string_move_filtered_at_selection(self):
        paths = [dict(ep(), move_5min_pips="18.2"),
                 ep(time_="2026-04-10T12:30")]
        out = find_similar_episodes(paths, "CPI m/m", "USD", "USDCAD", "hiking")
        assert len(out) == 1 and out[0]["event_time"].startswith("2026-04-10")

    def test_torn_line_append_recovers(self, tmp_path):
        """Review finding: append after a torn (no-newline) line must not
        fuse records."""
        path = tmp_path / "refl.jsonl"
        path.write_bytes(b'{"currency": "USD", "event_name": "CPI m/m", "text": "partial"')
        store = ReflectionStore(str(path))
        store.append({"currency": "USD", "event_name": "CPI m/m", "text": "new"})
        rows = store._load()
        assert any(r.get("text") == "new" for r in rows)

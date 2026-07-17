"""
Unit tests for the LLM knowledge additions:
- shared DecisionHistory instance (track record sees in-session decisions)
- realized trade outcomes from trade_history.jsonl in the prompt data
- event playbook loader (curated knowledge file)
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from llm_decision_engine import LLMDecisionEngine
from decision_history import DecisionHistory


def make_engine(tmp_path, decision_log=None):
    """Rule-based engine wired to tmp files only (never production logs)."""
    engine = LLMDecisionEngine(
        provider="rule-based",
        decision_log=decision_log or DecisionHistory(log_dir=str(tmp_path / "dh")),
        trade_history_file=str(tmp_path / "trade_history.jsonl"),
    )
    engine.cot_analyzer = Mock()
    engine.sentiment = Mock()
    engine.playbooks_file = str(tmp_path / "event_playbooks.json")
    return engine


def write_trades(path, records):
    with open(path, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def make_trade(**overrides):
    trade = {
        "ticket": 1,
        "symbol": "USDCAD",
        "direction": "BUY",
        "lots": 0.5,
        "event_name": "Core CPI m/m",
        "profit_usd": -67.02,
        "reason": "SL hit",
        "opened_at": "2026-07-14T12:14:45",
        "closed_at": "2026-07-14T12:16:00",
    }
    trade.update(overrides)
    return trade


class TestSharedDecisionLog:
    def test_track_record_sees_in_session_decision(self, tmp_path):
        """The engine must read the SAME DecisionHistory the server records
        into — a decision recorded mid-session appears in the track record."""
        shared = DecisionHistory(log_dir=str(tmp_path / "dh"))
        engine = make_engine(tmp_path, decision_log=shared)
        engine.reaction_history = Mock()
        engine.reaction_history.get_matching.return_value = []

        decision = SimpleNamespace(
            event="Core CPI m/m", currency="USD", pair="USDCAD",
            direction="BUY", confidence=0.65, forced=False,
            reasoning="test", data_summary=None,
        )
        shared.record(decision)

        track = engine._build_track_record("USD")
        assert track is not None
        assert "Core CPI m/m" in track
        assert "BUY USDCAD" in track

    def test_forced_decisions_excluded_from_track_record(self, tmp_path):
        """FORCE_DECISION test-mode rows are not the model's genuine calls —
        they must never masquerade as live track record."""
        shared = DecisionHistory(log_dir=str(tmp_path / "dh"))
        engine = make_engine(tmp_path, decision_log=shared)
        engine.reaction_history = Mock()
        engine.reaction_history.get_matching.return_value = []

        decision = SimpleNamespace(
            event="Core CPI m/m", currency="USD", pair="USDCAD",
            direction="BUY", confidence=0.65, forced=True,
            reasoning="test", data_summary=None,
        )
        shared.record(decision)

        assert engine._build_track_record("USD") is None


class TestTradeOutcomes:
    def test_filters_by_currency_and_formats(self, tmp_path):
        engine = make_engine(tmp_path)
        write_trades(engine.trade_history_file, [
            make_trade(profit_usd=-67.02),
            make_trade(symbol="EURJPY", profit_usd=10.0),   # no USD/CAD? EUR+JPY -> excluded for USD
            make_trade(symbol="NZDUSD", direction="SELL", profit_usd=42.0),
        ])
        section = engine._trade_outcomes_section("USD")
        assert section is not None
        assert "$-67.02" in section
        assert "$+42.00" in section
        assert "EURJPY" not in section
        assert "1 wins / 1 losses" in section

    def test_none_when_no_trades(self, tmp_path):
        engine = make_engine(tmp_path)
        assert engine._trade_outcomes_section("USD") is None

    def test_corrupt_lines_tolerated(self, tmp_path):
        engine = make_engine(tmp_path)
        with open(engine.trade_history_file, 'w', encoding='utf-8') as f:
            f.write("not json\n42\n")
            f.write(json.dumps(make_trade()) + "\n")
        section = engine._trade_outcomes_section("USD")
        assert section is not None and "$-67.02" in section

    def test_track_record_joins_realized_pnl(self, tmp_path):
        shared = DecisionHistory(log_dir=str(tmp_path / "dh"))
        engine = make_engine(tmp_path, decision_log=shared)
        engine.reaction_history = Mock()
        engine.reaction_history.get_matching.return_value = []

        write_trades(engine.trade_history_file, [make_trade()])
        decision = SimpleNamespace(
            event="Core CPI m/m", currency="USD", pair="USDCAD",
            direction="BUY", confidence=0.65, forced=False,
            reasoning="test", data_summary=None,
        )
        shared.record(decision)  # today's date == closed_at? no — match by event+date
        # Force the decision timestamp date to match the trade's closed_at date
        shared._recent[-1]["timestamp"] = "2026-07-14T12:13:20Z"

        track = engine._build_track_record("USD")
        assert track is not None
        assert "realized $-67.02" in track


class TestPlaybooks:
    def test_exact_event_match(self, tmp_path):
        engine = make_engine(tmp_path)
        with open(engine.playbooks_file, 'w', encoding='utf-8') as f:
            json.dump({"Core CPI m/m": {"pattern": "spike then partial retrace",
                                        "notes": "wide spread first 30s"}}, f)
        section = engine._playbook_section("Core CPI m/m (Jun)", "USD")
        assert section is not None
        assert "spike then partial retrace" in section
        assert "wide spread first 30s" in section

    def test_currency_fallback_key(self, tmp_path):
        engine = make_engine(tmp_path)
        with open(engine.playbooks_file, 'w', encoding='utf-8') as f:
            json.dump({"CURRENCY:NZD": {"pattern": "thin liquidity overshoots"}}, f)
        assert "thin liquidity" in engine._playbook_section("Official Cash Rate", "NZD")
        assert engine._playbook_section("Official Cash Rate", "USD") is None

    def test_missing_file_is_silent(self, tmp_path):
        engine = make_engine(tmp_path)
        assert engine._playbook_section("CPI m/m", "USD") is None

    def test_broken_file_is_silent(self, tmp_path):
        engine = make_engine(tmp_path)
        with open(engine.playbooks_file, 'w', encoding='utf-8') as f:
            f.write("{broken")
        assert engine._playbook_section("CPI m/m", "USD") is None

    def test_edit_picked_up_without_restart(self, tmp_path):
        engine = make_engine(tmp_path)
        with open(engine.playbooks_file, 'w', encoding='utf-8') as f:
            json.dump({"CPI m/m": {"pattern": "v1"}}, f)
        assert "v1" in engine._playbook_section("CPI m/m", "USD")
        with open(engine.playbooks_file, 'w', encoding='utf-8') as f:
            json.dump({"CPI m/m": {"pattern": "v2"}}, f)
        # Force an mtime that cannot collide with the cached one: rewriting
        # alone can land in the same filesystem timestamp tick as the v1 load
        # (observed flaky on Windows/NTFS), so stamp a distinct mtime AFTER
        # the write instead of before it.
        os.utime(engine.playbooks_file, (1, 1))
        assert "v2" in engine._playbook_section("CPI m/m", "USD")

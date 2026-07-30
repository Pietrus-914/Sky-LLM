"""
Thinking budget for reasoning models (30.07.2026).

The panel runs against a wall clock: analysis starts at PRELOAD_SECONDS and
the deadline is T-20s, so a model that thinks for two minutes casts NO vote.
Providers that count reasoning against max_tokens make it worse — a 200 with
an empty body. Both engines now send OpenRouter's unified reasoning field.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from llm_util import (reasoning_body, openrouter_headers, OPENROUTER_SITE,
                      REASONING_EFFORTS)


class TestChannelAttribution:
    """OpenRouter groups dashboard "App" rows by REFERER, not X-Title, so a
    shared referer collapsed entry/exit/aux into one row — on 30.07.2026 the
    entry panel's votes were listed under "Exit Manager", hiding per-channel
    spend and latency."""

    def test_each_channel_gets_its_own_referer(self):
        entry = openrouter_headers("entry", "SkyTower-AI Trading")
        exit_ = openrouter_headers("exit", "SkyTower-AI Exit Manager")
        aux = openrouter_headers("aux", "SkyTower-AI Aux")
        referers = {h["HTTP-Referer"] for h in (entry, exit_, aux)}
        assert len(referers) == 3, "channels must be separable in Generations"
        assert all(r.startswith(OPENROUTER_SITE + "/") for r in referers)

    def test_title_still_travels(self):
        assert openrouter_headers("entry", "SkyTower-AI Trading")["X-Title"] \
            == "SkyTower-AI Trading"

    def test_entry_and_exit_calls_use_distinct_referers(self, tmp_path):
        """The regression that mattered: both engines sending one referer."""
        from decision_history import DecisionHistory
        from llm_decision_engine import LLMDecisionEngine
        from exit_decision_engine import ExitDecisionEngine

        entry = LLMDecisionEngine(
            provider="rule-based",
            decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
            trade_history_file=str(tmp_path / "t.jsonl"))
        entry.provider = "openrouter"
        entry.model = "test/model"
        entry.client = MagicMock()
        entry.client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="{}"))])
        entry._chat("prompt")
        entry_ref = entry.client.chat.completions.create.call_args.kwargs[
            "extra_headers"]["HTTP-Referer"]

        ex = ExitDecisionEngine(provider="rule-based")
        ex.provider = "openrouter"
        ex.model = "test/exit"
        ex.client = MagicMock()
        ex.client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"reasoning": "ok", "action": "HOLD"}'))])
        ex._llm_decision(TestExitCallSendsTheBudget()._position())
        exit_ref = ex.client.chat.completions.create.call_args.kwargs[
            "extra_headers"]["HTTP-Referer"]

        assert entry_ref != exit_ref


class TestReasoningBody:
    def test_every_documented_level_is_accepted(self):
        for effort in REASONING_EFFORTS:
            assert reasoning_body(effort) == {"reasoning": {"effort": effort}}

    def test_uses_the_nested_field_not_the_openai_spelling(self):
        """OpenRouter reads {"reasoning": {"effort": ...}}; the flat
        `reasoning_effort` is the OpenAI-only spelling and is ignored."""
        body = reasoning_body("low")
        assert "reasoning_effort" not in body
        assert body["reasoning"]["effort"] == "low"

    def test_case_and_whitespace_tolerant(self):
        assert reasoning_body("  LOW ") == {"reasoning": {"effort": "low"}}

    def test_typo_falls_back_to_the_provider_default(self):
        """A bad value in .env must not become an API error at T-150s."""
        for junk in ("lowest", "verylow", "1", "", None, 3, object()):
            assert reasoning_body(junk) == {}


class TestEntryCallSendsTheBudget:
    def _engine(self, tmp_path):
        from decision_history import DecisionHistory
        from llm_decision_engine import LLMDecisionEngine
        engine = LLMDecisionEngine(
            provider="rule-based",
            decision_log=DecisionHistory(log_dir=str(tmp_path / "dh")),
            trade_history_file=str(tmp_path / "trades.jsonl"))
        engine.provider = "openrouter"
        engine.model = "test/model"
        engine.client = MagicMock()
        engine.client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="{}"))])
        return engine

    def test_effort_reaches_the_request(self, tmp_path, monkeypatch):
        import llm_decision_engine as lde
        monkeypatch.setitem(lde.LLM_CONFIG, "reasoning_effort", "minimal")
        engine = self._engine(tmp_path)

        engine._chat("prompt")

        kwargs = engine.client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {"reasoning": {"effort": "minimal"}}

    def test_unset_effort_sends_no_override(self, tmp_path, monkeypatch):
        import llm_decision_engine as lde
        monkeypatch.setitem(lde.LLM_CONFIG, "reasoning_effort", "")
        engine = self._engine(tmp_path)

        engine._chat("prompt")

        kwargs = engine.client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {}

    def test_panel_member_gets_the_budget_too(self, tmp_path, monkeypatch):
        """Every vote is a separate call — the cap must ride on all of them,
        not just the anchor's."""
        import llm_decision_engine as lde
        monkeypatch.setitem(lde.LLM_CONFIG, "reasoning_effort", "low")
        engine = self._engine(tmp_path)

        engine._chat("prompt", "moonshotai/kimi-k3")

        kwargs = engine.client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "moonshotai/kimi-k3"
        assert kwargs["extra_body"] == {"reasoning": {"effort": "low"}}


class TestExitCallSendsTheBudget:
    def _position(self):
        from datetime import datetime
        from position_manager import OpenPosition
        now = datetime.utcnow()
        return OpenPosition(
            ticket=1, symbol="USDCAD", direction="BUY", entry_price=1.37,
            current_price=1.3710, lots=0.5, remaining_lots=0.5, sl=1.3660,
            tp=0.0, profit_usd=10.0, max_profit_usd=10.0, max_drawdown_usd=0.0,
            tick_value=10.0, account_balance=5000.0, open_time=now,
            last_update=now, event_name="CPI m/m", entry_reasoning="x",
            max_loss_usd=100.0)

    def test_effort_reaches_the_request(self, monkeypatch):
        from exit_decision_engine import ExitDecisionEngine
        import exit_decision_engine as ede

        monkeypatch.setitem(ede.POSITION_MANAGEMENT_CONFIG,
                            "exit_reasoning_effort", "low")
        engine = ExitDecisionEngine(provider="rule-based")
        engine.provider = "openrouter"
        engine.model = "test/exit-model"
        engine.client = MagicMock()
        engine.client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"reasoning": "ok", "action": "HOLD"}'))])

        engine._llm_decision(self._position())

        kwargs = engine.client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {"reasoning": {"effort": "low"}}


class TestConfigDefaults:
    def test_both_engines_default_to_a_small_budget(self):
        """Defaults must be conservative: the deadline, not the model's
        appetite, decides how long a decision may take."""
        from config import LLM_CONFIG, POSITION_MANAGEMENT_CONFIG
        assert LLM_CONFIG["reasoning_effort"] in ("low", "minimal", "none")
        assert POSITION_MANAGEMENT_CONFIG["exit_reasoning_effort"] in (
            "low", "minimal", "none")

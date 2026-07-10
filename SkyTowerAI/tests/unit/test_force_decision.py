"""
Unit tests for FORCE_DECISION test mode (never SKIP).
Covers all three SKIP origins: LLM output, parse failure, rule-based tie.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

import llm_decision_engine
from llm_decision_engine import LLMDecisionEngine, TradingDecision
from calendar_fetcher import EconomicEvent


def make_event(**overrides):
    defaults = dict(
        event_name="Interest Rate Decision",
        currency="NZD",
        datetime_utc=datetime.utcnow() + timedelta(minutes=2),
        impact="HIGH",
        forecast="4.25%",
        previous="4.00%",
        actual=None,
        source="test",
    )
    defaults.update(overrides)
    return EconomicEvent(**defaults)


def make_engine():
    """Engine with no LLM client and mocked data sources."""
    engine = LLMDecisionEngine(provider="rule-based")
    engine.cot_analyzer = Mock()
    engine.sentiment = Mock()
    # Never touch the production logs/event_reactions.jsonl from unit tests
    engine.reaction_history = Mock()
    engine.reaction_history.summarize.return_value = None
    return engine


def neutral_context(engine):
    """Data sources return nothing useful — rule-based scores tie at 0."""
    engine.cot_analyzer.analyze_currency.return_value = {"error": "no data", "signal": "NEUTRAL"}
    engine.sentiment.get_currency_sentiment.return_value = {"signal": "NEUTRAL", "pairs_analyzed": 0}


class TestSystemPrompt:
    def test_normal_prompt_allows_skip(self):
        engine = make_engine()
        with patch.object(llm_decision_engine, "FORCE_DECISION", False):
            prompt = engine._system_prompt()
        assert '"BUY" or "SELL" or "SKIP"' in prompt
        assert "SKIP the trade if" in prompt
        assert "%SKIP_POLICY%" not in prompt
        assert "%DIRECTION_VALUES%" not in prompt

    def test_forced_prompt_bans_skip(self):
        engine = make_engine()
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            prompt = engine._system_prompt()
        assert '"BUY" or "SELL"' in prompt
        assert '"SKIP"' not in prompt
        assert "MUST choose BUY or SELL" in prompt
        assert "%SKIP_POLICY%" not in prompt


class TestRuleBasedForce:
    def test_tie_skips_when_flag_off(self):
        engine = make_engine()
        neutral_context(engine)
        # forecast == previous → no forecast score either
        event = make_event(forecast="4.00%", previous="4.00%")
        with patch.object(llm_decision_engine, "FORCE_DECISION", False):
            decision = engine.analyze_event(event)
        assert decision.direction == "SKIP"
        assert decision.forced is False

    def test_tie_forces_direction_when_flag_on(self):
        engine = make_engine()
        neutral_context(engine)
        event = make_event(forecast="4.00%", previous="4.00%")
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction in ("BUY", "SELL")
        assert decision.forced is True
        assert "forced" in decision.reasoning.lower()

    def test_weak_signal_follows_stronger_side(self):
        """Forecast improvement alone (+2 bullish) is below the +2 margin → SKIP
        normally, but force mode picks BUY."""
        engine = make_engine()
        neutral_context(engine)
        event = make_event(forecast="4.25%", previous="4.00%")
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction == "BUY"

    def test_deterioration_tie_break_sells(self):
        engine = make_engine()
        neutral_context(engine)
        event = make_event(forecast="3.75%", previous="4.00%")
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction == "SELL"


class TestLLMForce:
    def _engine_with_llm(self, responses):
        """Engine whose fake OpenRouter client returns the given texts in order."""
        engine = make_engine()
        engine.provider = "openrouter"
        engine.model = "test-model"
        engine.client = Mock()
        mocked = [
            Mock(choices=[Mock(message=Mock(content=text))]) for text in responses
        ]
        engine.client.chat.completions.create.side_effect = mocked
        return engine

    def test_llm_skip_remapped_to_rule_direction(self):
        engine = self._engine_with_llm(
            ['{"direction": "SKIP", "confidence": 0.35, "reasoning": "weak data"}']
        )
        neutral_context(engine)
        event = make_event()  # forecast improvement → forced BUY
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction == "BUY"
        assert decision.confidence == 0.35  # keeps the LLM's honest confidence
        assert "LLM chose SKIP" in decision.reasoning
        assert decision.forced is True

    def test_llm_skip_stays_skip_when_flag_off(self):
        engine = self._engine_with_llm(
            ['{"direction": "SKIP", "confidence": 0.35, "reasoning": "weak data"}']
        )
        neutral_context(engine)
        with patch.object(llm_decision_engine, "FORCE_DECISION", False):
            decision = engine.analyze_event(make_event())
        assert decision.direction == "SKIP"
        assert decision.forced is False

    def test_parse_failure_retries_then_uses_rule_fallback(self):
        engine = self._engine_with_llm(["not json at all", "still not json"])
        neutral_context(engine)
        event = make_event()
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        # two LLM calls (original + retry), then rule-based fallback
        assert engine.client.chat.completions.create.call_count == 2
        assert decision.direction in ("BUY", "SELL")
        assert decision.forced is True

    def test_parse_failure_retry_success(self):
        engine = self._engine_with_llm(
            ["garbage", '{"direction": "SELL", "confidence": 0.6, "reasoning": "retry ok"}']
        )
        neutral_context(engine)
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(make_event())
        assert decision.direction == "SELL"
        assert decision.confidence == 0.6

    def test_parse_failure_defaults_to_skip_when_flag_off(self):
        engine = self._engine_with_llm(["not json at all"])
        neutral_context(engine)
        with patch.object(llm_decision_engine, "FORCE_DECISION", False):
            decision = engine.analyze_event(make_event())
        assert engine.client.chat.completions.create.call_count == 1
        assert decision.direction == "SKIP"


class TestDirectionWhitelist:
    def test_hold_becomes_skip_then_forced(self):
        """LLM returning 'HOLD' must not slip through as an unexecutable direction."""
        engine = make_engine()
        engine.provider = "openrouter"
        engine.model = "test-model"
        engine.client = Mock()
        engine.client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content='{"direction": "HOLD", "confidence": 0.4, "reasoning": "unsure"}'))]
        )
        neutral_context(engine)
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(make_event())
        assert decision.direction in ("BUY", "SELL")

    def test_lowercase_buy_normalized(self):
        assert LLMDecisionEngine._normalize_direction(" buy ") == "BUY"

    def test_garbage_becomes_skip(self):
        assert LLMDecisionEngine._normalize_direction("NO_TRADE") == "SKIP"
        assert LLMDecisionEngine._normalize_direction(None) == "SKIP"
        assert LLMDecisionEngine._normalize_direction("skip") == "SKIP"


class TestForcedTieBreakConfidence:
    def test_zero_evidence_coin_flip_reports_low_confidence(self):
        engine = make_engine()
        neutral_context(engine)
        event = make_event(forecast=None, previous=None)
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction in ("BUY", "SELL")
        assert decision.confidence <= 0.35  # honest coin-flip, not the 0.5 baseline


class TestQuoteSideMapping:
    """Event currency is not always the pair's BASE: CAD -> USD/CAD.
    Bearish CAD must mean BUY USDCAD, not SELL (real bug from the first
    live event's fallback path, 10.07.2026)."""

    def test_bias_to_direction_base(self):
        f = LLMDecisionEngine._currency_bias_to_direction
        assert f("BULLISH", "NZD/USD", "NZD") == "BUY"
        assert f("BEARISH", "NZD/USD", "NZD") == "SELL"

    def test_bias_to_direction_quote(self):
        f = LLMDecisionEngine._currency_bias_to_direction
        assert f("BULLISH", "USD/CAD", "CAD") == "SELL"
        assert f("BEARISH", "USD/CAD", "CAD") == "BUY"
        assert f("BEARISH", "USDCAD.pro", "CAD") == "BUY"  # broker suffix

    def test_rule_based_cad_deterioration_buys_usdcad(self):
        """Bearish-CAD evidence on a CAD event must produce BUY USDCAD."""
        engine = make_engine()
        engine.cot_analyzer.analyze_currency.return_value = {
            "signal": "BEARISH", "confidence": 0.7}
        engine.sentiment.get_currency_sentiment.return_value = {
            "signal": "BEARISH", "confidence": 0.6, "pairs_analyzed": 3}
        event = make_event(currency="CAD", forecast="11.2K", previous="87.8K")
        with patch.object(llm_decision_engine, "FORCE_DECISION", False):
            decision = engine.analyze_event(event)
        assert decision.pair.replace("/", "") == "USDCAD"
        assert decision.direction == "BUY"  # weak CAD = USDCAD up

    def test_forced_tie_break_respects_quote_side(self):
        engine = make_engine()
        neutral_context(engine)
        # CAD deterioration tie-break: bearish CAD -> BUY USDCAD
        event = make_event(currency="CAD", forecast="10K", previous="80K")
        with patch.object(llm_decision_engine, "FORCE_DECISION", True):
            decision = engine.analyze_event(event)
        assert decision.direction == "BUY"


class TestForcedDirectionHelper:
    def test_cot_dominates(self):
        engine = make_engine()
        ctx = {
            "forecast_info": {"forecast_vs_previous": "IMPROVEMENT"},
            "cot_analysis": {"signal": "BEARISH"},
            "sentiment_analysis": {"signal": "NEUTRAL"},
        }
        direction, note = engine._forced_direction(ctx)
        assert direction == "SELL"  # COT 3 > forecast 2

    def test_full_tie_defaults_to_buy(self):
        engine = make_engine()
        ctx = {
            "forecast_info": {"forecast_vs_previous": "UNKNOWN"},
            "cot_analysis": {"signal": "NEUTRAL"},
            "sentiment_analysis": {"signal": "NEUTRAL"},
        }
        direction, note = engine._forced_direction(ctx)
        assert direction == "BUY"
        assert "tie-break" in note


class TestDecisionHistoryForcedFlag:
    def test_record_includes_forced(self, tmp_path):
        from decision_history import DecisionHistory
        history = DecisionHistory(log_dir=str(tmp_path))
        decision = TradingDecision(
            event="CPI", currency="USD", pair="USD/CAD", direction="BUY",
            confidence=0.4, lot_percent=60, entry_seconds_before=15,
            exit_minutes_after=10, stop_loss_percent=40,
            reasoning="test", data_summary={}, timestamp=datetime.utcnow(),
            forced=True,
        )
        history.record(decision)
        recent = history.get_recent(1)
        assert recent[0]["forced"] is True

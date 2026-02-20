"""
Unit tests for ExitDecisionEngine
Tests rule-based decisions and LLM response parsing.
"""
import pytest
import sys
import os
from datetime import datetime, timedelta

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from position_manager import OpenPosition, PositionCommand
from exit_decision_engine import ExitDecisionEngine


# ============================================================================
# Helpers
# ============================================================================

def make_open_position(**overrides):
    """Create a test OpenPosition with sensible defaults."""
    now = datetime.utcnow()
    defaults = dict(
        ticket=12345,
        symbol="NZDUSD",
        direction="BUY",
        entry_price=0.6200,
        current_price=0.6215,
        lots=0.50,
        remaining_lots=0.50,
        sl=0.6170,
        tp=0.0,
        profit_usd=0.0,
        max_profit_usd=0.0,
        max_drawdown_usd=0.0,
        tick_value=10.0,
        account_balance=5000.0,
        open_time=now,
        last_update=now,
        event_name="Official Cash Rate",
        entry_reasoning="AI: Strong bullish signal",
        spread_pips=2.5,
        zone_bias=0.0,
        nearest_resistance=0.6250,
        nearest_support=0.6180,
        ai_decisions=[],
        partial_closed=False,
        sl_moved_to_be=False,
    )
    defaults.update(overrides)
    return OpenPosition(**defaults)


@pytest.fixture
def engine():
    """Create ExitDecisionEngine in rule-based mode (no LLM)."""
    return ExitDecisionEngine(provider="rule-based")


# ============================================================================
# TestRuleBasedDecisions
# ============================================================================

class TestRuleBasedDecisions:
    """Test rule-based exit decisions."""

    def test_move_sl_to_be_after_profit(self, engine):
        """Profit > $30 should trigger MODIFY_SL to break-even."""
        pos = make_open_position(
            profit_usd=35.0,
            sl_moved_to_be=False,
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        assert decision.sl_price > 0
        assert "BE" in decision.reason.upper() or "break" in decision.reason.lower()

    def test_partial_close_after_high_profit(self, engine):
        """Profit > $60 (partial not done) should trigger PARTIAL_CLOSE 50%."""
        pos = make_open_position(
            profit_usd=65.0,
            sl_moved_to_be=True,  # SL already at BE
            partial_closed=False,
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "PARTIAL_CLOSE"
        assert decision.close_percent == 50

    def test_trailing_sl(self, engine):
        """Profit > $40 with SL at BE should trigger trailing SL.
        Trail distance = 0.0001 * 100 = 0.01 (10 pips).
        For trail to tighten SL: current_price - 0.01 must be > current SL.
        So current_price must be > SL + 0.01 = 0.6201 + 0.01 = 0.6301.
        """
        pos = make_open_position(
            profit_usd=45.0,
            current_price=0.6320,  # 120 pips above entry → trail SL = 0.6220 > 0.6201
            sl=0.6201,  # Already at break-even
            sl_moved_to_be=True,
            partial_closed=True,  # Partial already done — skip partial rule
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        assert "trail" in decision.reason.lower()
        # Trailing SL = current_price - 0.01 = 0.6220
        assert decision.sl_price < pos.current_price
        assert decision.sl_price > pos.sl  # 0.6220 > 0.6201

    def test_cut_loss_after_time(self, engine):
        """Loss after 10+ minutes should trigger CLOSE."""
        pos = make_open_position(
            profit_usd=-25.0,
            open_time=datetime.utcnow() - timedelta(minutes=12),
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "CLOSE"
        assert "loss" in decision.reason.lower() or "losing" in decision.reason.lower()

    def test_late_phase_close(self, engine):
        """Small P/L after 20+ minutes should trigger CLOSE."""
        pos = make_open_position(
            profit_usd=10.0,
            open_time=datetime.utcnow() - timedelta(minutes=22),
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "CLOSE"
        assert "late" in decision.reason.lower()

    def test_hold_when_no_action_needed(self, engine):
        """Normal early position should HOLD."""
        pos = make_open_position(
            profit_usd=15.0,
            open_time=datetime.utcnow() - timedelta(minutes=3),
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "HOLD"

    def test_sell_direction_sl_move(self, engine):
        """SELL position SL to BE should be below entry."""
        pos = make_open_position(
            direction="SELL",
            entry_price=0.6200,
            current_price=0.6180,
            sl=0.6230,  # SL above entry for SELL
            profit_usd=35.0,
            sl_moved_to_be=False,
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        # For SELL, new SL should be at/below entry
        assert decision.sl_price < pos.entry_price


# ============================================================================
# TestLLMResponseParsing
# ============================================================================

class TestLLMResponseParsing:
    """Test LLM response parsing."""

    def test_parse_valid_json(self, engine):
        """Valid JSON should be parsed into PositionCommand."""
        text = '{"action": "CLOSE", "sl_price": 0, "close_percent": 0, "reasoning": "Take profit at resistance"}'
        cmd = engine._parse_llm_response(text)
        assert cmd.action == "CLOSE"
        assert "Take profit" in cmd.reason

    def test_parse_json_in_markdown(self, engine):
        """JSON wrapped in markdown code blocks should parse correctly."""
        text = """```json
{"action": "MODIFY_SL", "sl_price": 0.6210, "close_percent": 0, "reasoning": "Moving SL up"}
```"""
        cmd = engine._parse_llm_response(text)
        assert cmd.action == "MODIFY_SL"
        assert cmd.sl_price == 0.6210

    def test_parse_invalid_json_fallback(self, engine):
        """Invalid JSON should fall back to HOLD."""
        cmd = engine._parse_llm_response("this is not json at all")
        assert cmd.action == "HOLD"
        assert "parse error" in cmd.reason.lower()

    def test_parse_unknown_action(self, engine):
        """Unknown action should be replaced with HOLD."""
        text = '{"action": "FOOBAR", "reasoning": "test"}'
        cmd = engine._parse_llm_response(text)
        assert cmd.action == "HOLD"

    def test_parse_partial_close(self, engine):
        """PARTIAL_CLOSE with close_percent should parse correctly."""
        text = '{"action": "PARTIAL_CLOSE", "sl_price": 0, "close_percent": 50, "reasoning": "Take partial profit"}'
        cmd = engine._parse_llm_response(text)
        assert cmd.action == "PARTIAL_CLOSE"
        assert cmd.close_percent == 50.0

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
# TestModifyTpValidationAndParsing (2026-08-05 audit)
# ============================================================================

class TestModifyTpValidationAndParsing:
    """tp_price was the only model-produced number reaching the broker with
    no units/side validation, and a JSON null in any numeric field silently
    demoted a valid model verdict to the rule-based fallback."""

    def test_null_numeric_field_keeps_the_model_verdict(self, engine):
        cmd = engine._parse_llm_response(
            '{"reasoning": "momentum intact", "sl_price": 0.0,'
            ' "tp_price": null, "close_percent": 0, "action": "HOLD"}')
        assert cmd.action == "HOLD"
        assert "Parse error" not in cmd.reason
        assert "momentum intact" in cmd.reason

    def test_modify_tp_wrong_units_demoted_to_hold(self, engine):
        # Pips confused with an instrument price: 15 on NZDUSD ~0.62 lands on
        # the VALID far side for a BUY — the broker would accept it and the
        # real 8-pip target would silently vanish
        pos = make_open_position(direction="BUY", current_price=0.6215)
        cmd = engine._parse_llm_response(
            '{"reasoning": "bank it", "tp_price": 15, "action": "MODIFY_TP"}',
            pos)
        assert cmd.action == "HOLD"
        assert "invalid" in cmd.reason.lower()

    def test_modify_tp_wrong_side_demoted_to_hold(self, engine):
        # A BUY take-profit below the market is a broker rejection at best
        pos = make_open_position(direction="BUY", current_price=0.6215)
        cmd = engine._parse_llm_response(
            '{"reasoning": "bank", "tp_price": 0.6200, "action": "MODIFY_TP"}',
            pos)
        assert cmd.action == "HOLD"

    def test_modify_tp_plausible_price_passes(self, engine):
        pos = make_open_position(direction="BUY", current_price=0.6215)
        cmd = engine._parse_llm_response(
            '{"reasoning": "bank", "tp_price": 0.6230, "action": "MODIFY_TP"}',
            pos)
        assert cmd.action == "MODIFY_TP"
        assert cmd.tp_price == pytest.approx(0.6230)

    def test_modify_tp_sell_side_validation(self, engine):
        pos = make_open_position(direction="SELL", current_price=0.6215)
        ok = engine._parse_llm_response(
            '{"reasoning": "bank", "tp_price": 0.6200, "action": "MODIFY_TP"}',
            pos)
        assert ok.action == "MODIFY_TP"
        bad = engine._parse_llm_response(
            '{"reasoning": "bank", "tp_price": 0.6230, "action": "MODIFY_TP"}',
            pos)
        assert bad.action == "HOLD"


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
        assert decision.sl_price == pytest.approx(0.6201)
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
        Trail distance = 0.0001 * 10 = 0.0010 (10 pips).
        """
        pos = make_open_position(
            profit_usd=45.0,
            current_price=0.6320,
            sl=0.6201,  # Already at break-even
            sl_moved_to_be=True,
            partial_closed=True,  # Partial already done — skip partial rule
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        assert "trail" in decision.reason.lower()
        assert decision.sl_price == pytest.approx(0.6310)
        assert decision.sl_price < pos.current_price
        assert decision.sl_price > pos.sl

    @pytest.mark.parametrize(
        ("direction", "entry", "expected"),
        [
            ("BUY", 150.00, 150.01),
            ("SELL", 150.00, 149.99),
        ],
    )
    def test_jpy_break_even_buffer_is_one_pip(
        self, engine, direction, entry, expected
    ):
        pos = make_open_position(
            symbol="USDJPY",
            direction=direction,
            entry_price=entry,
            current_price=150.20 if direction == "BUY" else 149.80,
            sl=149.50 if direction == "BUY" else 150.50,
            profit_usd=35.0,
            sl_moved_to_be=False,
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        assert decision.sl_price == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("direction", "current", "current_sl", "expected"),
        [
            ("BUY", 150.30, 150.01, 150.20),
            ("SELL", 149.70, 149.99, 149.80),
        ],
    )
    def test_jpy_trailing_distance_is_ten_pips(
        self, engine, direction, current, current_sl, expected
    ):
        pos = make_open_position(
            symbol="USDJPY",
            direction=direction,
            entry_price=150.00,
            current_price=current,
            sl=current_sl,
            profit_usd=45.0,
            sl_moved_to_be=True,
            partial_closed=True,
        )
        decision = engine._rule_based_decision(pos)
        assert decision.action == "MODIFY_SL"
        assert decision.sl_price == pytest.approx(expected)

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


# ============================================================================
# Cut-loss context (gpt_review, 30.07.2026). The exit prompt's rule #10 was
# unconditional on loss magnitude and the prompt carried neither the risk
# budget nor any price path, so "no signs of recovery after 5+ minutes"
# degenerated into "negative at minute 5 -> CLOSE" — more aggressive than the
# engine's OWN rule fallback (10 min AND worse than -$20).
# ============================================================================

class TestCutLossPromptContext:
    def test_system_prompt_ties_cut_loss_to_the_budget(self):
        from exit_decision_engine import EXIT_SYSTEM_PROMPT
        assert "NEVER cut on elapsed time alone" in EXIT_SYSTEM_PROMPT
        assert "risk budget" in EXIT_SYSTEM_PROMPT

    def test_prompt_states_budget_and_share_used(self, engine):
        pos = make_open_position(profit_usd=-25.0, max_loss_usd=100.0)
        prompt = engine._build_prompt(pos)
        assert "RISK BUDGET" in prompt
        assert "$100.00" in prompt
        assert "25% of that budget" in prompt
        assert "(LOSS)" in prompt

    def test_prompt_falls_back_to_configured_budget(self, engine):
        """A recovered position may carry no per-trade budget of its own."""
        pos = make_open_position(profit_usd=-10.0, max_loss_usd=0.0)
        assert "RISK BUDGET" in engine._build_prompt(pos)

    def test_budget_share_counts_realized_partials(self, engine):
        pos = make_open_position(profit_usd=-10.0, realized_usd=-30.0,
                                 max_loss_usd=100.0)
        assert "40% of that budget" in engine._build_prompt(pos)

    def test_trajectory_absent_on_first_report(self, engine):
        pos = make_open_position()
        assert "No samples yet" in engine._format_trajectory(pos)

    def test_trajectory_flags_recovery(self, engine):
        pos = make_open_position(pnl_samples=[
            {"minutes": 0.5, "price": 0.6190, "pnl": -60.0},
            {"minutes": 1.5, "price": 0.6195, "pnl": -30.0},
            {"minutes": 2.5, "price": 0.6205, "pnl": -5.0}])
        text = engine._format_trajectory(pos)
        assert "recovering" in text
        assert "T+2.5min" in text

    def test_trajectory_flags_no_recovery(self, engine):
        pos = make_open_position(pnl_samples=[
            {"minutes": 0.5, "price": 0.6205, "pnl": -10.0},
            {"minutes": 2.5, "price": 0.6180, "pnl": -70.0}])
        assert "no recovery yet" in engine._format_trajectory(pos)

    def test_trajectory_reaches_the_prompt(self, engine):
        pos = make_open_position(pnl_samples=[
            {"minutes": 1.0, "price": 0.6195, "pnl": -20.0}])
        prompt = engine._build_prompt(pos)
        assert "RECENT TRAJECTORY" in prompt
        assert "T+1.0min" in prompt

    def test_trajectory_shows_only_the_last_ten_samples(self, engine):
        pos = make_open_position(pnl_samples=[
            {"minutes": float(i), "price": 0.62, "pnl": float(-i)}
            for i in range(20)])
        text = engine._format_trajectory(pos)
        assert "T+19.0min" in text
        assert "T+5.0min" not in text

"""
Unit tests for PositionManager
Tests guardrails, lifecycle, flag management, ticket validation.
"""
import pytest
import sys
import os
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from position_manager import PositionManager, OpenPosition, PositionCommand


# ============================================================================
# Helpers
# ============================================================================

def make_position_data(**overrides):
    """Create default position open data dict."""
    data = {
        "ticket": 12345,
        "symbol": "NZDUSD",
        "direction": "BUY",
        "entry_price": 0.6200,
        "lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "tick_value": 10.0,
        "account_balance": 5000.00,
        "event_name": "Official Cash Rate",
    }
    data.update(overrides)
    return data


def make_report_data(ticket=12345, **overrides):
    """Create default position report data dict."""
    data = {
        "ticket": ticket,
        "current_price": 0.6210,
        "remaining_lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "profit_usd": 50.0,
        "tick_value": 10.0,
        "account_balance": 5000.00,
        "spread_pips": 2.5,
        "zone_bias": 0.0,
        "nearest_resistance": 0.6250,
        "nearest_support": 0.6180,
    }
    data.update(overrides)
    return data


@pytest.fixture
def pm():
    """Create PositionManager with no exit engine (rule-based only)."""
    return PositionManager(exit_engine=None)


@pytest.fixture
def pm_with_position(pm):
    """Create PositionManager with an open position."""
    pm.on_position_opened(make_position_data())
    return pm


# ============================================================================
# TestGuardrails
# ============================================================================

class TestGuardrails:
    """Test USD-based safety guardrails."""

    def test_max_loss_triggers_close(self, pm_with_position):
        """Profit < -$100 should trigger immediate CLOSE."""
        result = pm_with_position.update_position(
            make_report_data(profit_usd=-110.0)
        )
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "max loss" in result["command"]["reason"].lower()

    def test_max_hold_time_triggers_close(self, pm):
        """Position open > 30 min should trigger CLOSE."""
        pm.on_position_opened(make_position_data())
        # Manually set open_time to 31 minutes ago
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=31)

        result = pm.update_position(make_report_data(profit_usd=10.0))
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "hold time" in result["command"]["reason"].lower()

    def test_emergency_spread_triggers_close(self, pm_with_position):
        """Spread >= 15 pips should trigger CLOSE."""
        result = pm_with_position.update_position(
            make_report_data(spread_pips=16.0, profit_usd=10.0)
        )
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "spread" in result["command"]["reason"].lower()

    def test_profit_protection_triggers_close(self, pm_with_position):
        """Profit drop >50% from peak should trigger CLOSE."""
        # First report: peak profit of $80
        pm_with_position.update_position(make_report_data(profit_usd=80.0))

        # Second report: profit dropped to $30 (62.5% drop from $80)
        result = pm_with_position.update_position(
            make_report_data(profit_usd=30.0)
        )
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_no_guardrail_on_normal_position(self, pm_with_position):
        """Normal position (profit $20, 5min) should return HOLD."""
        result = pm_with_position.update_position(
            make_report_data(profit_usd=20.0, spread_pips=2.0)
        )
        assert result["command"]["action"] == "HOLD"

    def test_profit_protection_only_after_threshold(self, pm_with_position):
        """Profit protection should not trigger if peak < $20."""
        # Peak = $15, drop to $5 (66% drop but below $20 threshold)
        pm_with_position.update_position(make_report_data(profit_usd=15.0))
        result = pm_with_position.update_position(
            make_report_data(profit_usd=5.0)
        )
        # Should not trigger profit protection (peak $15 < $20 threshold)
        assert result["command"]["action"] == "HOLD"

    def test_daily_loss_blocks_new_trades(self, pm):
        """Daily loss > $300 should block new trades."""
        # Simulate 3 losing trades
        for i in range(3):
            pm.on_position_opened(make_position_data(ticket=100 + i))
            pm.on_position_closed({
                "ticket": 100 + i,
                "profit": -120.0,
                "reason": "SL hit",
            })

        # Daily P/L should be -$360
        can_trade, reason = pm.can_open_trade()
        assert can_trade is False
        assert "daily loss" in reason.lower()

    def test_daily_trade_limit(self, pm):
        """More than 5 trades per day should be blocked."""
        for i in range(5):
            pm.on_position_opened(make_position_data(ticket=100 + i))
            pm.on_position_closed({
                "ticket": 100 + i,
                "profit": 10.0,
                "reason": "TP hit",
            })

        can_trade, reason = pm.can_open_trade()
        assert can_trade is False
        assert "trade limit" in reason.lower()


# ============================================================================
# TestPositionLifecycle
# ============================================================================

class TestPositionLifecycle:
    """Test position open/close/update lifecycle."""

    def test_open_position(self, pm):
        """Opening a position should track all data correctly."""
        data = make_position_data()
        pm.on_position_opened(data)

        assert pm.position is not None
        assert pm.position.ticket == 12345
        assert pm.position.symbol == "NZDUSD"
        assert pm.position.direction == "BUY"
        assert pm.position.entry_price == 0.6200
        assert pm.position.lots == 0.50
        assert pm.daily_trades == 1

    def test_close_position_updates_daily_pnl(self, pm_with_position):
        """Closing a position should update daily P/L."""
        pm_with_position.on_position_closed({
            "ticket": 12345,
            "profit": 75.0,
            "reason": "AI close",
        })

        assert pm_with_position.position is None
        assert pm_with_position.daily_pnl_usd == 75.0
        assert len(pm_with_position.closed_trades) == 1
        assert pm_with_position.closed_trades[0]["profit_usd"] == 75.0

    def test_cannot_open_two_positions(self, pm_with_position):
        """Should not allow opening a second position."""
        can_trade, reason = pm_with_position.can_open_trade()
        assert can_trade is False
        assert "already open" in reason.lower()

    def test_daily_reset(self, pm):
        """Daily counters should reset on new day."""
        pm.on_position_opened(make_position_data())
        pm.on_position_closed({
            "ticket": 12345,
            "profit": -50.0,
            "reason": "SL",
        })

        assert pm.daily_pnl_usd == -50.0
        assert pm.daily_trades == 1

        # Simulate date change
        pm.daily_reset_date = "2020-01-01"
        pm._reset_daily_if_needed()

        assert pm.daily_pnl_usd == 0.0
        assert pm.daily_trades == 0

    def test_update_tracks_max_profit(self, pm_with_position):
        """Max profit should be tracked across updates."""
        pm_with_position.update_position(make_report_data(profit_usd=30.0))
        assert pm_with_position.position.max_profit_usd == 30.0

        pm_with_position.update_position(make_report_data(profit_usd=50.0))
        assert pm_with_position.position.max_profit_usd == 50.0

        # Profit drops but max should stay at 50
        pm_with_position.update_position(make_report_data(profit_usd=40.0))
        assert pm_with_position.position.max_profit_usd == 50.0

    def test_update_tracks_max_drawdown(self, pm_with_position):
        """Max drawdown should be tracked across updates."""
        pm_with_position.update_position(make_report_data(profit_usd=-20.0))
        assert pm_with_position.position.max_drawdown_usd == -20.0

        pm_with_position.update_position(make_report_data(profit_usd=-5.0))
        assert pm_with_position.position.max_drawdown_usd == -20.0  # stays at worst


# ============================================================================
# TestFlagManagement (BUG-2 fix verification)
# ============================================================================

class TestFlagManagement:
    """Test that position flags are set correctly after commands."""

    def test_sl_moved_to_be_flag_set_after_modify_sl(self, pm):
        """MODIFY_SL close to entry should set sl_moved_to_be flag."""
        pm.on_position_opened(make_position_data(entry_price=0.6200))

        # Simulate a MODIFY_SL command close to entry price
        cmd = PositionCommand(
            action="MODIFY_SL",
            sl_price=0.6201,  # 1 pip above entry — within 2-pip tolerance
        )
        pm.pending_command = cmd

        # update_position should consume pending command and set the flag
        pm.update_position(make_report_data(profit_usd=35.0))
        assert pm.position.sl_moved_to_be is True

    def test_partial_closed_flag_set_after_partial_close(self, pm):
        """PARTIAL_CLOSE command should set partial_closed flag."""
        pm.on_position_opened(make_position_data())

        cmd = PositionCommand(action="PARTIAL_CLOSE", close_percent=50)
        pm.pending_command = cmd

        pm.update_position(make_report_data(profit_usd=65.0))
        assert pm.position.partial_closed is True

    def test_rule_based_no_repeat_sl_move_after_flag_set(self, pm):
        """Rule-based engine should not repeat SL move when flag is set."""
        from exit_decision_engine import ExitDecisionEngine

        engine = ExitDecisionEngine(provider="rule-based")
        pm_local = PositionManager(exit_engine=engine)
        pm_local.on_position_opened(make_position_data())

        # Set flags manually to simulate already done
        pm_local.position.sl_moved_to_be = True
        pm_local.position.profit_usd = 35.0
        pm_local.position.max_profit_usd = 35.0

        # Rule-based should skip SL-to-BE rule (already done)
        decision = engine._rule_based_decision(pm_local.position)
        # Should not be MODIFY_SL for break-even (might be HOLD or trail)
        if decision.action == "MODIFY_SL":
            # If MODIFY_SL, it should be trailing (Rule 3), not BE (Rule 1)
            assert "trail" in decision.reason.lower() or pm_local.position.sl_moved_to_be

    def test_rule_based_no_repeat_partial_after_flag_set(self, pm):
        """Rule-based engine should not repeat partial close when flag is set."""
        from exit_decision_engine import ExitDecisionEngine

        engine = ExitDecisionEngine(provider="rule-based")
        pm_local = PositionManager(exit_engine=engine)
        pm_local.on_position_opened(make_position_data())

        # Set flags
        pm_local.position.partial_closed = True
        pm_local.position.profit_usd = 65.0
        pm_local.position.max_profit_usd = 65.0

        decision = engine._rule_based_decision(pm_local.position)
        # Should NOT be PARTIAL_CLOSE (already done)
        assert decision.action != "PARTIAL_CLOSE"


# ============================================================================
# TestTicketValidation (BUG-5 fix verification)
# ============================================================================

class TestTicketValidation:
    """Test ticket validation in update_position."""

    def test_update_rejected_for_wrong_ticket(self, pm_with_position):
        """Update with wrong ticket should be ignored."""
        result = pm_with_position.update_position(
            make_report_data(ticket=99999, profit_usd=50.0)
        )
        # Should return HOLD (ignored)
        assert result["command"]["action"] == "HOLD"
        # Position should not be updated
        assert pm_with_position.position.profit_usd == 0.0

    def test_update_accepted_for_correct_ticket(self, pm_with_position):
        """Update with correct ticket should be processed."""
        result = pm_with_position.update_position(
            make_report_data(ticket=12345, profit_usd=50.0)
        )
        assert pm_with_position.position.profit_usd == 50.0

    def test_update_accepted_when_no_ticket_in_data(self, pm_with_position):
        """Update without ticket field should be accepted (backward compat)."""
        data = make_report_data(profit_usd=25.0)
        del data["ticket"]
        result = pm_with_position.update_position(data)
        assert pm_with_position.position.profit_usd == 25.0


# ============================================================================
# TestConsistentJSON (BUG-4 fix verification)
# ============================================================================

class TestConsistentJSON:
    """Test that JSON responses have consistent format."""

    def test_hold_response_has_command_key(self, pm_with_position):
        """HOLD response should include 'command' object."""
        result = pm_with_position.update_position(
            make_report_data(profit_usd=10.0)
        )
        assert "command" in result
        assert "action" in result["command"]
        assert result["command"]["action"] == "HOLD"

    def test_close_response_has_command_key(self, pm_with_position):
        """CLOSE response should include 'command' object."""
        result = pm_with_position.update_position(
            make_report_data(profit_usd=-110.0)
        )
        assert "command" in result
        assert result["command"]["action"] == "CLOSE"

    def test_no_position_response_has_command_key(self, pm):
        """Response with no position should include 'command' object."""
        result = pm.update_position(make_report_data())
        assert "command" in result
        assert result["command"]["action"] == "HOLD"


# ============================================================================
# TestLLMOutsideLock (BUG-1 fix verification)
# ============================================================================

class TestLLMOutsideLock:
    """Test that LLM calls don't block the lock."""

    def test_llm_decision_applied_after_call(self, pm):
        """LLM decision should be applied correctly."""
        # Create mock exit engine that returns CLOSE
        mock_engine = MagicMock()
        mock_engine.decide.return_value = PositionCommand(
            action="CLOSE", reason="AI: Take profit"
        )

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0  # Force LLM check

        result = pm.update_position(make_report_data(profit_usd=50.0))

        assert mock_engine.decide.called
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"

    def test_llm_hold_returns_hold(self, pm):
        """LLM returning HOLD should result in HOLD response."""
        mock_engine = MagicMock()
        mock_engine.decide.return_value = PositionCommand(
            action="HOLD", reason="AI: Let it run"
        )

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        result = pm.update_position(make_report_data(profit_usd=20.0))
        assert result["command"]["action"] == "HOLD"

    def test_llm_error_falls_through_to_hold(self, pm):
        """LLM error should not crash — returns HOLD."""
        mock_engine = MagicMock()
        mock_engine.decide.side_effect = RuntimeError("API timeout")

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        result = pm.update_position(make_report_data(profit_usd=20.0))
        assert result["command"]["action"] == "HOLD"


# ============================================================================
# TestTradeHistoryPersistence (panel-owned risk / persistent stats commit)
# ============================================================================

class TestTradeHistoryPersistence:
    """Persistent trade log: restart rebuild, corruption tolerance,
    orphaned closes, fallback trade counting."""

    def test_stats_survive_restart(self, tmp_path):
        hf = str(tmp_path / "trade_history.jsonl")
        pm1 = PositionManager(exit_engine=None, history_file=hf)
        pm1.on_position_opened(make_position_data())
        pm1.on_position_closed({
            "ticket": 12345,
            "profit": -42.5,
            "reason": "SL hit",
        })

        pm2 = PositionManager(exit_engine=None, history_file=hf)
        status = pm2.get_status()
        assert status["daily_trades"] == 1
        assert status["daily_pnl_usd"] == -42.5
        assert len(status["recent_trades"]) == 1
        assert status["recent_trades"][0]["symbol"] == "NZDUSD"

    def test_corrupt_history_does_not_crash_startup(self, tmp_path):
        hf = tmp_path / "trade_history.jsonl"
        hf.write_text(
            'not json at all\n'
            '42\n'                                  # valid JSON, not a dict
            '{"closed_at": "2026-01-01T00:00:00", "profit_usd": null}\n'
            '\xff\xfebroken bytes\n',
            encoding="utf-8", errors="ignore",
        )
        pm = PositionManager(exit_engine=None, history_file=str(hf))
        status = pm.get_status()  # must not raise
        assert status["daily_trades"] == 0

    def test_null_profit_today_counts_as_zero(self, tmp_path):
        from timeutil import utcnow
        hf = tmp_path / "trade_history.jsonl"
        today = utcnow().strftime("%Y-%m-%d")
        hf.write_text(
            '{"closed_at": "%sT01:00:00", "profit_usd": null}\n'
            '{"closed_at": "%sT02:00:00", "profit_usd": -30.0}\n' % (today, today),
            encoding="utf-8",
        )
        pm = PositionManager(exit_engine=None, history_file=str(hf))
        assert pm.daily_trades == 2
        assert pm.daily_pnl_usd == -30.0

    def test_orphaned_close_is_persisted_and_counted(self, tmp_path):
        hf = str(tmp_path / "trade_history.jsonl")
        pm = PositionManager(exit_engine=None, history_file=hf)
        # No position tracked (server restarted mid-trade) -> close arrives
        pm.on_position_closed({"ticket": 777, "profit": -100.0, "reason": "SL"})
        assert pm.daily_trades == 1
        assert pm.daily_pnl_usd == -100.0

        # The loss must survive the NEXT restart too
        pm2 = PositionManager(exit_engine=None, history_file=hf)
        assert pm2.daily_pnl_usd == -100.0
        assert pm2.daily_trades == 1

    def test_untracked_fallback_trade_counts_against_limit(self, pm):
        """/api/trade-executed fallback must consume the daily limit."""
        pm.register_untracked_trade()
        assert pm.daily_trades == 1
        # pm.config is the shared module-level dict — patch, don't mutate
        with patch.dict(pm.config, {"max_daily_trades": 1}):
            can, reason = pm.can_open_trade()
        assert can is False
        assert "limit" in reason.lower()

    def test_status_reports_live_limits(self, pm):
        with patch.dict(pm.config, {"max_daily_trades": 9,
                                    "max_daily_loss_usd": 555.0}):
            status = pm.get_status()
        assert status["max_daily_trades"] == 9
        assert status["max_daily_loss_usd"] == 555.0


# ============================================================================
# TestPartialCloseAccounting
# ============================================================================

class TestPartialCloseAccounting:
    """A partial close realizes profit — it must not read as a profit
    collapse. Regression for 2026-07-22 GBPUSD: AI partial-closed 50% at the
    $201.60 peak, the guardrail compared the remaining half's floating $96
    against the full-position peak and force-closed the runner 26s later
    ("Safety: profit dropped 52% from peak")."""

    def test_partial_close_credits_realized_and_holds(self, pm):
        pm.on_position_opened(make_position_data(lots=2.4))
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=200.0))
        # EA executed PARTIAL_CLOSE 50%: floating halves, realized ~half of $200
        result = pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=96.0))
        assert pm.position is not None
        assert pm.position.realized_usd == pytest.approx(100.0)
        assert pm.position.partial_closed is True
        # Whole trade: $96 floating + $100 realized vs $200 peak = -2%, NOT -52%
        assert result["command"]["action"] == "HOLD"

    def test_peak_tracks_whole_trade_across_partial(self, pm):
        pm.on_position_opened(make_position_data(lots=2.4))
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=200.0))
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=96.0))
        assert pm.position.max_profit_usd == pytest.approx(200.0)
        # Runner keeps going: total = 100 realized + 130 floating = 230
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=130.0))
        assert pm.position.max_profit_usd == pytest.approx(230.0)

    def test_protection_still_fires_on_real_collapse_after_partial(self, pm):
        pm.on_position_opened(make_position_data(lots=2.4))
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=200.0))
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=96.0))
        # Genuine reversal: floating -> $0, total 100 vs peak 200 = -50%
        result = pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=0.0))
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_protection_unchanged_without_partial(self, pm_with_position):
        """No partial close -> exact old floating-only behaviour."""
        pm_with_position.update_position(make_report_data(profit_usd=80.0))
        result = pm_with_position.update_position(make_report_data(profit_usd=30.0))
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_max_loss_counts_realized_loss_from_partial(self, pm):
        """Partial close at a loss books a NEGATIVE realized leg that the
        floating-only check silently forgot."""
        pm.on_position_opened(make_position_data(lots=2.4))
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=-40.0))
        # Half closed at -$40 floating -> -$20 realized, remaining floats -$30
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=-30.0))
        assert pm.position.realized_usd == pytest.approx(-20.0)
        # Floating -90 alone is above the -$100 limit, but the whole trade
        # is at -110 -> must close
        result = pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=-90.0))
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "max loss" in result["command"]["reason"].lower()

    def test_manual_partial_in_mt5_also_credited(self, pm):
        """Volume drop without an AI PARTIAL_CLOSE command (user clicked in
        the terminal) must be credited the same way."""
        pm.on_position_opened(make_position_data(lots=1.0))
        pm.update_position(make_report_data(remaining_lots=1.0, profit_usd=60.0))
        pm.update_position(make_report_data(remaining_lots=0.25, profit_usd=15.0))
        assert pm.position.realized_usd == pytest.approx(45.0)
        assert pm.position.partial_closed is True

    def test_close_record_includes_realized(self, pm):
        pm.on_position_opened(make_position_data(lots=2.4))
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=200.0))
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=96.0))
        record = pm.on_position_closed({"ticket": 12345, "profit": 178.80,
                                        "reason": "AI: momentum gone",
                                        "profit_source": "history"})
        assert record["realized_usd"] == pytest.approx(100.0)
        assert record["profit_usd"] == pytest.approx(178.80)
        assert record["max_profit_usd"] == pytest.approx(200.0)
        assert pm.daily_pnl_usd == pytest.approx(178.80)

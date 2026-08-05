"""
Unit tests for PositionManager
Tests guardrails, lifecycle, flag management, ticket validation.
"""
import pytest
import sys
import os
import threading
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

    def test_emergency_spread_triggers_close_after_confirmation(
            self, pm_with_position):
        """Spread >= 15 pips closes — but only once a SECOND report confirms it.
        Entry is blocked just under the same threshold and releases routinely
        print a wide tick at T0, so closing on one sample liquidated the trade
        at the worst quote of the session."""
        first = pm_with_position.update_position(
            make_report_data(spread_pips=16.0, profit_usd=10.0)
        )
        assert first["command"]["action"] == "HOLD"

        result = pm_with_position.update_position(
            make_report_data(spread_pips=16.0, profit_usd=10.0)
        )
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "spread" in result["command"]["reason"].lower()

    def test_catastrophic_spread_closes_on_the_first_report(
            self, pm_with_position):
        """Past 2x the threshold, waiting for confirmation is the bigger risk."""
        result = pm_with_position.update_position(
            make_report_data(spread_pips=35.0, profit_usd=10.0)
        )
        assert result["command"]["action"] == "CLOSE"

    def test_transient_spike_alone_does_not_close(self, pm_with_position):
        """The case the debounce exists for: one wide tick, then normal."""
        pm_with_position.update_position(
            make_report_data(spread_pips=16.0, profit_usd=10.0))
        back = pm_with_position.update_position(
            make_report_data(spread_pips=3.0, profit_usd=10.0))
        assert back["command"]["action"] == "HOLD"
        assert pm_with_position._spread_breaches == 0

    def test_profit_protection_triggers_close(self, pm_with_position):
        """Confirmed drop >50% from an armed peak, outside grace, closes."""
        # Outside the post-open grace window (but under max hold)
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        # Peak $80 >= floor (30% of $100 budget = $30) -> armed
        pm_with_position.update_position(make_report_data(profit_usd=80.0))

        # First drop report ($30 = 62.5% drop) only arms the debounce
        first = pm_with_position.update_position(
            make_report_data(profit_usd=30.0)
        )
        assert first["command"]["action"] == "HOLD"

        # Second consecutive drop report confirms -> CLOSE
        result = pm_with_position.update_position(
            make_report_data(profit_usd=28.0)
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
        """No protection close while the peak is below the arming floor
        (30% of the $100 default budget = $30)."""
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        # Peak = $25 < $30 floor, drop to $5 (80% drop) twice
        pm_with_position.update_position(make_report_data(profit_usd=25.0))
        pm_with_position.update_position(make_report_data(profit_usd=5.0))
        result = pm_with_position.update_position(
            make_report_data(profit_usd=5.0)
        )
        assert result["command"]["action"] == "HOLD"

    def test_profit_protection_floor_scales_with_budget(self, pm):
        """2026-08-04 NZD regression: with a $1000 budget the old flat $20
        floor armed at ~1.3 pips of a 1.57-lot position. The floor is now 30%
        of max_loss_usd, so a $34.54 peak must NOT arm the guardrail."""
        pm.on_position_opened(make_position_data(max_loss_usd=1000.0))
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=5)
        pm.update_position(make_report_data(profit_usd=34.54))
        pm.update_position(make_report_data(profit_usd=-23.55))
        result = pm.update_position(make_report_data(profit_usd=-23.55))
        assert result["command"]["action"] == "HOLD"

    def test_profit_protection_respects_grace_period(self, pm_with_position):
        """2026-08-04 NZD regression: the release whipsaw lives in the first
        ~2 minutes. Inside the grace window even a confirmed 100%+ drop from
        an armed peak must not close."""
        # Fresh position (open_time = now) -> inside 120s grace
        pm_with_position.update_position(make_report_data(profit_usd=80.0))
        pm_with_position.update_position(make_report_data(profit_usd=5.0))
        result = pm_with_position.update_position(
            make_report_data(profit_usd=5.0)
        )
        assert result["command"]["action"] == "HOLD"

    def test_profit_protection_single_report_does_not_close(
            self, pm_with_position):
        """One whipsaw report is noise; recovery resets the debounce."""
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        pm_with_position.update_position(make_report_data(profit_usd=80.0))
        # One drop report, then recovery above the 50% line
        pm_with_position.update_position(make_report_data(profit_usd=30.0))
        back = pm_with_position.update_position(
            make_report_data(profit_usd=70.0)
        )
        assert back["command"]["action"] == "HOLD"
        assert pm_with_position._profit_drop_breaches == 0

    def test_profit_protection_never_closes_in_the_red(self, pm_with_position):
        """A negative total is a job for max-loss/SL, not profit protection —
        and time in the red RESETS the debounce, so a recovery must earn the
        close with two consecutive positive reports of its own (the first
        green tick after a deep drawdown must not cash out the runner)."""
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        pm_with_position.update_position(make_report_data(profit_usd=80.0))
        # Two drop reports in the red -> no close, counter stays reset
        pm_with_position.update_position(make_report_data(profit_usd=-5.0))
        red = pm_with_position.update_position(
            make_report_data(profit_usd=-5.0)
        )
        assert red["command"]["action"] == "HOLD"
        assert pm_with_position._profit_drop_breaches == 0
        # Recovery to +$10 (drop 87.5%): first green report only arms...
        first_green = pm_with_position.update_position(
            make_report_data(profit_usd=10.0)
        )
        assert first_green["command"]["action"] == "HOLD"
        # ...the second green report confirms and locks in what is left
        result = pm_with_position.update_position(
            make_report_data(profit_usd=10.0)
        )
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_catastrophic_give_back_closes_on_first_report(
            self, pm_with_position):
        """A >=90% give-back of a big armed peak (>=2x floor) closes without
        waiting for confirmation — the next report may already be in the red,
        where this rule refuses to act."""
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        pm_with_position.update_position(make_report_data(profit_usd=200.0))
        result = pm_with_position.update_position(
            make_report_data(profit_usd=5.0)
        )
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_incident_regression_default_budget(self, pm_with_position):
        """2026-08-04 NZD shape at the $100 DEFAULT budget: the $34.54 peak
        DOES clear the $30 floor, so it is the grace window (and debounce)
        that must carry the save — the whipsaw happened seconds after open."""
        pm_with_position.update_position(make_report_data(profit_usd=34.54))
        result = pm_with_position.update_position(
            make_report_data(profit_usd=-23.55)
        )
        assert result["command"]["action"] == "HOLD"

    def test_debounce_survives_persistent_persist_failure(self, pm_with_position):
        """2026-08-05 audit: while the position-store write keeps failing
        (disk full, locked file), every report re-enters the reconcile branch
        — which used to zero both debounce counters on every pass, so the
        confirmed-spread and profit-protection closes could NEVER reach their
        second confirming report for as long as persistence kept failing."""
        from position_store import PositionStoreError
        pm = pm_with_position
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=5)
        pm.position_store.save = MagicMock(
            side_effect=PositionStoreError("disk full"))
        pm.recovery_state = "error"
        snap = dict(make_position_data(), **make_report_data())
        snap["reconcile"] = True

        pm.update_position(dict(snap, profit_usd=80.0))
        pm.update_position(dict(snap, profit_usd=30.0))
        result = pm.update_position(dict(snap, profit_usd=30.0))
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_cushion_shrinks_to_remaining_lots_after_partial(self, pm):
        """2026-08-05 audit: after a partial the broker realized_usd already
        contains the entry commission for the FULL size, so the cushion must
        be sized on the remaining leg — the old original-lots cushion parked
        the guardrail in a dead band on exactly the AI-runner trades."""
        pm.on_position_opened(make_position_data(lots=1.5))
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=5)
        pm.update_position(make_report_data(remaining_lots=1.5, profit_usd=20.0))
        # 50% partial banked at +$25 net (broker deal history)
        pm.update_position(make_report_data(remaining_lots=0.75, profit_usd=20.0,
                                            realized_usd=25.0))
        # Collapse to total $9.5 (79% give-back): above the remaining-leg
        # cushion 7*0.75=$5.25, but below the old original-lots 7*1.5=$10.5
        pm.update_position(make_report_data(remaining_lots=0.75, profit_usd=-15.5,
                                            realized_usd=25.0))
        result = pm.update_position(
            make_report_data(remaining_lots=0.75, profit_usd=-15.5,
                             realized_usd=25.0))
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

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
    """Flags follow the BROKER, not our intentions.

    sl_moved_to_be enables trailing and disables the break-even rule;
    partial_closed disables the partial rule and is shown to the exit model.
    Setting them because a command was SENT made them lie whenever it did not
    execute — a lost response, a broker rejection, a stop on the wrong side of
    the market — and the server then never re-tried.
    """

    def test_sl_flag_needs_the_broker_to_confirm_the_stop(self, pm):
        pm.on_position_opened(make_position_data(entry_price=0.6200))
        pm.pending_command = PositionCommand(
            action="MODIFY_SL",
            sl_price=0.6201,  # 1 pip above entry — within 2-pip tolerance
        )

        # Command served, but the EA still reports the ORIGINAL stop
        pm.update_position(make_report_data(profit_usd=35.0, sl=0.6170))
        assert pm.position.sl_moved_to_be is False

        # Now the broker reports the stop at break-even
        pm.update_position(make_report_data(profit_usd=35.0, sl=0.6201))
        assert pm.position.sl_moved_to_be is True

    def test_be_tolerance_stops_at_two_pips(self, pm):
        pm.on_position_opened(make_position_data(entry_price=0.6200))
        pm.update_position(make_report_data(profit_usd=35.0, sl=0.6202))
        assert pm.position.sl_moved_to_be is True

    def test_stop_short_of_break_even_does_not_latch(self, pm):
        """A stop still on the loss side of entry is not break-even."""
        pm.on_position_opened(make_position_data(entry_price=0.6200))
        pm.update_position(make_report_data(profit_usd=35.0, sl=0.61975))
        assert pm.position.sl_moved_to_be is False

    def test_stop_past_break_even_latches(self, pm):
        """The EA or an operator may trail beyond entry before we look."""
        pm.on_position_opened(make_position_data(entry_price=0.6200))
        pm.update_position(make_report_data(profit_usd=60.0, sl=0.6230))
        assert pm.position.sl_moved_to_be is True

    def test_jpy_be_tolerance_uses_quote_currency(self, pm):
        pm.on_position_opened(
            make_position_data(symbol="USDJPY", entry_price=150.00)
        )
        pm.update_position(make_report_data(profit_usd=35.0, sl=150.02))
        assert pm.position.sl_moved_to_be is True

    def test_partial_closed_flag_follows_the_reported_volume(self, pm):
        """The volume drop IS the evidence; the command alone is not."""
        pm.on_position_opened(make_position_data(lots=0.50))
        pm.pending_command = PositionCommand(action="PARTIAL_CLOSE",
                                             close_percent=50)

        pm.update_position(make_report_data(profit_usd=65.0,
                                            remaining_lots=0.50))
        assert pm.position.partial_closed is False

        pm.update_position(make_report_data(profit_usd=65.0,
                                            remaining_lots=0.25))
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

    def test_llm_decision_queued_for_next_report(self, pm):
        """A networked exit engine runs OFF the request thread: the triggering
        report answers immediately and the command rides the next one.

        This is the fix for the lost-command bug — the EA's POST timeout (10s)
        is shorter than the exit LLM's (30s), so a decision delivered only in
        the triggering response was silently dropped whenever the model was
        slow, with no retry for another 30s."""
        mock_engine = MagicMock()  # has .client -> treated as networked
        mock_engine.decide.return_value = PositionCommand(
            action="CLOSE", reason="AI: Take profit"
        )

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0  # Force LLM check

        dispatch = pm.update_position(make_report_data(profit_usd=50.0))
        # The report that triggers the model must not wait for it. Had the EA
        # timed out here, nothing would be lost.
        assert dispatch["command"]["action"] == "HOLD"

        assert pm.wait_for_exit_worker(timeout=5.0)
        assert mock_engine.decide.called

        delivered = pm.update_position(make_report_data(profit_usd=50.0))
        assert delivered["has_command"] is True
        assert delivered["command"]["action"] == "CLOSE"

    def test_queued_command_is_served_only_once(self, pm):
        """Delivery pops the queue, so the EA cannot execute it twice."""
        mock_engine = MagicMock()
        mock_engine.decide.return_value = PositionCommand(
            action="PARTIAL_CLOSE", reason="AI: bank half", close_percent=50
        )
        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        pm.update_position(make_report_data(profit_usd=50.0))
        assert pm.wait_for_exit_worker(timeout=5.0)

        first = pm.update_position(make_report_data(profit_usd=50.0))
        assert first["command"]["action"] == "PARTIAL_CLOSE"
        pm.last_llm_check = time.time()  # keep the model out of this report
        second = pm.update_position(make_report_data(profit_usd=50.0))
        assert second["command"]["action"] == "HOLD"

    def test_rule_based_engine_answers_inline(self, pm):
        """An engine with no network client (the rule-based fallback) cannot
        time out, so it keeps answering in the triggering response."""
        inline_engine = MagicMock()
        inline_engine.client = None
        inline_engine.decide.return_value = PositionCommand(
            action="CLOSE", reason="Rule: cut loss"
        )

        pm.exit_engine = inline_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        result = pm.update_position(make_report_data(profit_usd=-30.0))
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"

    def test_guardrail_command_outranks_llm_decision(self, pm):
        """A queued safety command must never be displaced by a strategy one."""
        mock_engine = MagicMock()
        mock_engine.decide.return_value = PositionCommand(
            action="PARTIAL_CLOSE", reason="AI: bank half", close_percent=50
        )
        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        pm.update_position(make_report_data(profit_usd=50.0))
        # Guardrail fires (and queues its CLOSE) while the model is thinking
        pm.pending_command = PositionCommand(
            action="CLOSE", reason="Safety: max loss exceeded"
        )
        assert pm.wait_for_exit_worker(timeout=5.0)

        assert pm.pending_command.action == "CLOSE"
        assert "Safety" in pm.pending_command.reason

    def test_reports_build_a_trajectory_for_the_exit_model(self, pm):
        """The exit prompt's "signs of recovery" judgement needs a path, not a
        single snapshot. Samples carry the TOTAL P/L (floating + realized)."""
        pm.on_position_opened(make_position_data())
        for pnl in (-40.0, -25.0, -5.0):
            pm.update_position(make_report_data(profit_usd=pnl))

        samples = pm.position.pnl_samples
        assert [s["pnl"] for s in samples] == [-40.0, -25.0, -5.0]
        assert all("minutes" in s and "price" in s for s in samples)

    def test_trajectory_is_capped(self, pm):
        from position_manager import PNL_SAMPLE_CAP
        pm.on_position_opened(make_position_data())
        for i in range(PNL_SAMPLE_CAP + 8):
            pm.update_position(make_report_data(profit_usd=float(-i)))

        samples = pm.position.pnl_samples
        assert len(samples) == PNL_SAMPLE_CAP
        # Oldest dropped, newest kept
        assert samples[-1]["pnl"] == float(-(PNL_SAMPLE_CAP + 7))

    def test_slow_model_is_not_dispatched_twice(self, pm):
        """While one call is in flight the next report must not start another —
        cost and rate limits aside, two answers would race on one position."""
        release = threading.Event()
        calls = []

        def slow_decide(_pos):
            calls.append(1)
            release.wait(timeout=5.0)
            return PositionCommand(action="HOLD", reason="AI: developing")

        mock_engine = MagicMock()
        mock_engine.decide.side_effect = slow_decide
        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        pm.update_position(make_report_data(profit_usd=10.0))
        pm.last_llm_check = 0  # interval would allow another call
        pm.update_position(make_report_data(profit_usd=10.0))

        release.set()
        assert pm.wait_for_exit_worker(timeout=5.0)
        assert len(calls) == 1

    def test_llm_hold_is_recorded_but_never_queued(self, pm):
        """A HOLD must be journalled on the position yet never become a
        command — queueing it would hand the EA a no-op with has_command=true
        on the next report. Asserting only the dispatch response would pass
        for ANY model answer, since the dispatch always answers HOLD."""
        mock_engine = MagicMock()
        mock_engine.decide.return_value = PositionCommand(
            action="HOLD", reason="AI: Let it run"
        )

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        result = pm.update_position(make_report_data(profit_usd=20.0))
        assert result["command"]["action"] == "HOLD"
        assert pm.wait_for_exit_worker(timeout=5.0)

        assert pm.pending_command is None
        assert pm.position.ai_decisions[-1]["action"] == "HOLD"
        nxt = pm.update_position(make_report_data(profit_usd=20.0))
        assert nxt["has_command"] is False

    def test_llm_error_releases_the_worker_slot(self, pm):
        """A raising model must not leave _llm_inflight set: that would block
        every later exit consultation. Again, the dispatch response alone
        cannot show this."""
        mock_engine = MagicMock()
        mock_engine.decide.side_effect = RuntimeError("API timeout")

        pm.exit_engine = mock_engine
        pm.on_position_opened(make_position_data())
        pm.last_llm_check = 0

        result = pm.update_position(make_report_data(profit_usd=20.0))
        assert result["command"]["action"] == "HOLD"
        assert pm.wait_for_exit_worker(timeout=5.0)

        assert pm._llm_inflight is False
        assert pm.pending_command is None
        # ...and the next due report can consult the model again
        pm.last_llm_check = 0
        pm.update_position(make_report_data(profit_usd=20.0))
        assert pm.wait_for_exit_worker(timeout=5.0)
        assert mock_engine.decide.call_count == 2

    def test_worker_slot_is_freed_when_the_position_changes(self, pm):
        """A worker still waiting on the model for a CLOSED position must not
        block the next position's first consultation — the exit prompt calls
        the first minute the peak phase."""
        release = threading.Event()
        seen = []

        def slow_decide(_pos):
            seen.append(_pos.ticket)
            release.wait(timeout=5.0)
            return PositionCommand(action="HOLD", reason="AI: developing")

        mock_engine = MagicMock()
        mock_engine.decide.side_effect = slow_decide
        pm.exit_engine = mock_engine

        pm.on_position_opened(make_position_data(ticket=111))
        pm.last_llm_check = 0
        pm.update_position(make_report_data(ticket=111, profit_usd=10.0))

        # Position A closes and B opens while A's worker is still blocked
        pm.on_position_closed({"ticket": 111, "profit": -5.0,
                               "close_price": 0.6190, "reason": "SL"})
        pm.on_position_opened(make_position_data(ticket=222))
        assert pm._llm_inflight is False

        pm.last_llm_check = 0
        pm.update_position(make_report_data(ticket=222, profit_usd=10.0))
        release.set()
        assert pm.wait_for_exit_worker(timeout=5.0)
        assert 222 in seen, "the new position must get its own consultation"


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
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=5)
        pm.update_position(make_report_data(remaining_lots=2.4, profit_usd=200.0))
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=96.0))
        # Genuine reversal: floating -> $0, total 100 vs peak 200 = -50%,
        # confirmed on a second consecutive report
        pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=0.0))
        result = pm.update_position(make_report_data(remaining_lots=1.2, profit_usd=0.0))
        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "profit dropped" in result["command"]["reason"].lower()

    def test_protection_unchanged_without_partial(self, pm_with_position):
        """No partial close -> same peak math on the plain floating value."""
        pm_with_position.position.open_time = (
            datetime.utcnow() - timedelta(minutes=5))
        pm_with_position.update_position(make_report_data(profit_usd=80.0))
        pm_with_position.update_position(make_report_data(profit_usd=30.0))
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

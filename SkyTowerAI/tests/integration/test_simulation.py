"""
Full cycle simulation test for AI Position Management.
Simulates 30 position reports (2.5 minutes at 5s intervals)
with realistic profit curves. Tests rule-based exit logic.
"""
import pytest
import sys
import os
from datetime import datetime, timedelta

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from position_manager import PositionManager, PositionCommand
from exit_decision_engine import ExitDecisionEngine


def make_report(ticket, profit_usd, spread_pips=2.5, current_price=0.6210):
    """Create position report dict."""
    return {
        "ticket": ticket,
        "current_price": current_price,
        "remaining_lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "profit_usd": profit_usd,
        "tick_value": 10.0,
        "spread_pips": spread_pips,
        "account_balance": 5000.00,
        "zone_bias": 0.0,
        "nearest_resistance": 0.6250,
        "nearest_support": 0.6180,
    }


class TestWinningTradeSimulation:
    """
    Simulate a winning trade: profit rises from $0 to $80,
    then drops to $35. Tests SL-to-BE, partial close, and profit protection.
    """

    def test_winning_trade_full_cycle(self):
        """Simulate 30 reports of a winning trade with rule-based exits."""
        engine = ExitDecisionEngine(provider="rule-based")
        pm = PositionManager(exit_engine=engine)

        # Set LLM check interval to 0 so every call triggers rule-based
        pm.config = dict(pm.config)  # make a mutable copy
        pm.config["llm_check_interval_seconds"] = 0

        # Open position
        pm.on_position_opened({
            "ticket": 55555,
            "symbol": "NZDUSD",
            "direction": "BUY",
            "entry_price": 0.6200,
            "lots": 0.50,
            "sl": 0.6170,
            "tp": 0.0,
            "tick_value": 10.0,
            "account_balance": 5000.00,
            "event_name": "Official Cash Rate",
        })

        # Profit curve: rises to $80, then drops to $35
        profit_curve = [
            0, 5, 10, 12, 18, 22, 28,  # Reports 0-6: slow rise
            32, 38, 45, 52, 60, 68, 75, 80,  # Reports 7-14: strong rise
            78, 72, 65, 55, 48, 42, 38, 35,  # Reports 15-22: pullback
            33, 30, 28, 25, 22, 20, 18,      # Reports 23-29: fade
        ]

        actions_log = []
        sl_moved = False
        partial_done = False
        position_closed = False

        for i, profit in enumerate(profit_curve):
            if pm.position is None:
                position_closed = True
                break

            result = pm.update_position(make_report(
                ticket=55555,
                profit_usd=profit,
                current_price=0.6200 + profit * 0.0001,  # rough approximation
            ))

            action = result["command"]["action"]
            actions_log.append({
                "report": i,
                "profit": profit,
                "action": action,
                "reason": result["command"].get("reason", ""),
            })

            if action == "MODIFY_SL":
                sl_moved = True
            elif action == "PARTIAL_CLOSE":
                partial_done = True

        # Assertions: key behaviors should have happened
        assert sl_moved, "SL should have been moved (to BE or trailing)"
        assert partial_done, "Partial close should have been triggered at $60+ profit"

        # Verify the sequence makes sense
        modify_sl_reports = [a for a in actions_log if a["action"] == "MODIFY_SL"]
        partial_reports = [a for a in actions_log if a["action"] == "PARTIAL_CLOSE"]

        # SL move should happen around $30+ profit
        if modify_sl_reports:
            first_sl_move = modify_sl_reports[0]
            assert first_sl_move["profit"] >= 30, \
                f"SL move too early at ${first_sl_move['profit']}"

        # Partial close should happen around $60+ profit
        if partial_reports:
            first_partial = partial_reports[0]
            assert first_partial["profit"] >= 60, \
                f"Partial close too early at ${first_partial['profit']}"


class TestLosingTradeSimulation:
    """Simulate a losing trade that gets cut after time."""

    def test_losing_trade_cut(self):
        """Simulate a trade that goes negative — should be cut after 10 min."""
        engine = ExitDecisionEngine(provider="rule-based")
        pm = PositionManager(exit_engine=engine)
        pm.config = dict(pm.config)
        pm.config["llm_check_interval_seconds"] = 0

        # Open position
        pm.on_position_opened({
            "ticket": 66666,
            "symbol": "USDCAD",
            "direction": "SELL",
            "entry_price": 1.3500,
            "lots": 0.30,
            "sl": 1.3530,
            "tp": 0.0,
            "tick_value": 7.5,
            "account_balance": 3000.00,
            "event_name": "Employment Change",
        })

        # Simulate: price goes against us slowly
        profit_curve = [
            0, -3, -8, -12, -15, -18, -20, -22, -25, -28, -30
        ]

        # Advance time to 11+ minutes
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=11)

        close_triggered = False
        for i, profit in enumerate(profit_curve):
            if pm.position is None:
                close_triggered = True
                break

            result = pm.update_position(make_report(
                ticket=66666,
                profit_usd=profit,
                spread_pips=3.0,
            ))

            if result["command"]["action"] == "CLOSE":
                close_triggered = True
                break

        assert close_triggered, "Losing trade should be cut after 10+ minutes"


class TestMaxLossGuardrailSimulation:
    """Simulate a flash crash that triggers max loss guardrail."""

    def test_flash_crash_guardrail(self):
        """Sudden -$120 loss should trigger immediate CLOSE."""
        pm = PositionManager(exit_engine=None)

        pm.on_position_opened({
            "ticket": 77777,
            "symbol": "GBPUSD",
            "direction": "BUY",
            "entry_price": 1.2650,
            "lots": 1.0,
            "sl": 0.0,
            "tp": 0.0,
            "tick_value": 10.0,
            "account_balance": 10000.00,
            "event_name": "Interest Rate Decision",
        })

        # Normal report
        pm.update_position(make_report(ticket=77777, profit_usd=10.0))

        # Flash crash — instant loss
        result = pm.update_position(make_report(ticket=77777, profit_usd=-120.0))

        assert result["has_command"] is True
        assert result["command"]["action"] == "CLOSE"
        assert "max loss" in result["command"]["reason"].lower()


class TestEmergencySpreadSimulation:
    """Simulate spread spike during news."""

    def test_spread_spike_closes_position(self):
        """Spread spike to 16+ pips should close position."""
        pm = PositionManager(exit_engine=None)

        pm.on_position_opened({
            "ticket": 88888,
            "symbol": "NZDUSD",
            "direction": "BUY",
            "entry_price": 0.6200,
            "lots": 0.50,
            "sl": 0.6170,
            "tp": 0.0,
            "tick_value": 10.0,
            "account_balance": 5000.00,
            "event_name": "CPI",
        })

        # Normal spreads
        pm.update_position(make_report(ticket=88888, profit_usd=15.0, spread_pips=3.0))
        pm.update_position(make_report(ticket=88888, profit_usd=20.0, spread_pips=5.0))

        # Spread spikes — a single wide sample only arms the guardrail; the
        # exit needs confirmation on the next report (see the debounce in
        # PositionManager._check_guardrails)
        armed = pm.update_position(
            make_report(ticket=88888, profit_usd=18.0, spread_pips=16.0)
        )
        assert armed["command"]["action"] == "HOLD"

        result = pm.update_position(
            make_report(ticket=88888, profit_usd=18.0, spread_pips=17.0)
        )

        assert result["command"]["action"] == "CLOSE"
        assert "spread" in result["command"]["reason"].lower()


class TestDailyLimitsSimulation:
    """Simulate multiple trades hitting daily limits."""

    def test_daily_loss_limit_stops_trading(self):
        """After cumulative losses exceed $300, trading should stop."""
        pm = PositionManager(exit_engine=None)

        losses = [-80, -90, -85, -60]  # Total: -$315
        for i, loss in enumerate(losses):
            can_trade, _ = pm.can_open_trade()
            if not can_trade:
                # Should stop after 3rd trade (-$255 after 3, still ok)
                # Actually: -80 + -90 + -85 = -255, still under -300
                # After 4th: -315, over -300
                assert i >= 3, f"Stopped too early at trade {i}"
                break

            pm.on_position_opened({
                "ticket": 90000 + i,
                "symbol": "NZDUSD",
                "direction": "BUY",
                "entry_price": 0.6200,
                "lots": 0.30,
                "sl": 0.0,
                "tp": 0.0,
                "tick_value": 10.0,
                "account_balance": 5000.00,
                "event_name": f"Event {i}",
            })
            pm.on_position_closed({
                "ticket": 90000 + i,
                "profit": loss,
                "reason": "SL hit",
            })

        # After all 4 losses (total -$315), should not allow more trades
        can_trade, reason = pm.can_open_trade()
        assert can_trade is False
        assert "daily loss" in reason.lower()

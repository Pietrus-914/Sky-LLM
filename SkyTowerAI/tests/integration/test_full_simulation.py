"""
Full end-to-end integration simulation tests.
Simulates complete trading cycles through Flask HTTP endpoints,
multi-threaded concurrency, LLM mock integration, and edge cases.
"""
import pytest
import sys
import os
import time
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from position_manager import PositionManager, PositionCommand
from exit_decision_engine import ExitDecisionEngine
from server import app


# ============================================================================
# Helpers
# ============================================================================

def make_open_data(ticket=12345, symbol="NZDUSD", direction="BUY",
                   entry_price=0.6200, lots=0.50):
    return {
        "ticket": ticket,
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "lots": lots,
        "sl": 0.0,
        "tp": 0.0,
        "tick_value": 10.0,
        "account_balance": 5000.00,
        "event_name": "Official Cash Rate",
    }


def make_report(ticket=12345, profit_usd=0.0, spread_pips=2.5,
                current_price=0.6210, remaining_lots=0.50):
    return {
        "ticket": ticket,
        "current_price": current_price,
        "remaining_lots": remaining_lots,
        "sl": 0.0,
        "tp": 0.0,
        "profit_usd": profit_usd,
        "tick_value": 10.0,
        "spread_pips": spread_pips,
        "account_balance": 5000.00,
        "zone_bias": 0.0,
        "nearest_resistance": 0.6250,
        "nearest_support": 0.6180,
    }


def make_close_data(ticket=12345, profit=50.0, reason="Test close"):
    return {
        "ticket": ticket,
        "close_price": 0.6230,
        "profit": profit,
        "reason": reason,
    }


@pytest.fixture
def client():
    """Create test client with fresh position manager.
    Temporarily mocks decision_engine/calendar to prevent ensure_services()
    from calling init_services() and overwriting our test position_manager.
    Restores originals after test.
    """
    app.config['TESTING'] = True
    import server

    # Save originals
    orig_de = server.decision_engine
    orig_cal = server.calendar
    orig_pm = server.position_manager

    # Prevent ensure_services() from calling init_services()
    if server.decision_engine is None:
        server.decision_engine = MagicMock()
    if server.calendar is None:
        server.calendar = MagicMock()

    server.position_manager = PositionManager(exit_engine=None)

    with app.test_client() as c:
        yield c

    # Restore originals so other test files are not affected
    server.decision_engine = orig_de
    server.calendar = orig_cal
    server.position_manager = orig_pm


@pytest.fixture
def client_with_engine():
    """Create test client with mock LLM exit engine.
    Temporarily mocks decision_engine/calendar to prevent ensure_services().
    Restores originals after test.
    """
    app.config['TESTING'] = True
    import server

    # Save originals
    orig_de = server.decision_engine
    orig_cal = server.calendar
    orig_pm = server.position_manager

    # Prevent ensure_services() from calling init_services()
    if server.decision_engine is None:
        server.decision_engine = MagicMock()
    if server.calendar is None:
        server.calendar = MagicMock()

    mock_engine = MagicMock(spec=ExitDecisionEngine)
    mock_engine.decide.return_value = PositionCommand(action="HOLD", reason="AI: monitoring")

    server.position_manager = PositionManager(exit_engine=mock_engine)
    server.position_manager.config = dict(server.position_manager.config)
    server.position_manager.config["llm_check_interval_seconds"] = 0  # trigger every call

    with app.test_client() as c:
        yield c, mock_engine, server.position_manager

    # Restore originals
    server.decision_engine = orig_de
    server.calendar = orig_cal
    server.position_manager = orig_pm


# ============================================================================
# Test 1: Full HTTP cycle simulation (30 reports)
# ============================================================================

class TestFullHTTPCycleSimulation:
    """
    Simulate a complete winning trade through Flask endpoints.
    30 position reports with realistic profit curve.
    Verify SL-to-BE, partial close, and final closure via HTTP.
    """

    def test_winning_trade_30_reports_via_http(self, client):
        """Full winning trade lifecycle through HTTP endpoints."""
        import server

        # Use rule-based engine
        engine = ExitDecisionEngine(provider="rule-based")
        server.position_manager = PositionManager(exit_engine=engine)
        server.position_manager.config = dict(server.position_manager.config)
        server.position_manager.config["llm_check_interval_seconds"] = 0

        # 1. Open position via HTTP
        resp = client.post('/api/position/opened', json=make_open_data())
        assert resp.get_json()['status'] == 'ok'

        # 2. Verify status
        resp = client.get('/api/position/status')
        data = resp.get_json()
        assert data['has_position'] is True
        assert data['daily_trades'] == 1

        # 3. Send 30 reports with profit curve
        profit_curve = [
            0, 3, 8, 12, 18, 22, 28,           # 0-6: slow rise
            32, 38, 45, 52, 60, 68, 75, 80,     # 7-14: strong rise
            78, 72, 65, 55, 48, 42, 38, 35,     # 15-22: pullback
            33, 30, 28, 25, 22, 20, 18,         # 23-29: fade
        ]

        actions_seen = []
        for i, profit in enumerate(profit_curve):
            resp = client.post('/api/position/report', json=make_report(
                profit_usd=profit,
                current_price=0.6200 + profit * 0.0001,
            ))
            assert resp.status_code == 200
            data = resp.get_json()
            action = data['command']['action']
            actions_seen.append(action)

        # 4. Verify key actions occurred
        assert "MODIFY_SL" in actions_seen, "SL should have been modified"
        assert "PARTIAL_CLOSE" in actions_seen, "Partial close should have been triggered"

        # 5. Verify action order: SL move should come before partial close
        first_sl = actions_seen.index("MODIFY_SL")
        first_partial = actions_seen.index("PARTIAL_CLOSE")
        assert first_sl < first_partial, "SL move should happen before partial close"

        # 6. Close position
        resp = client.post('/api/position/closed', json=make_close_data(profit=35.0))
        assert resp.get_json()['status'] == 'ok'

        # 7. Verify daily P/L
        resp = client.get('/api/position/status')
        data = resp.get_json()
        assert data['has_position'] is False
        assert data['daily_pnl_usd'] == 35.0

    def test_losing_trade_guardrail_via_http(self, client):
        """Losing trade hits max loss guardrail through HTTP."""
        # Open
        client.post('/api/position/opened', json=make_open_data())

        # Reports with declining profit
        profits = [0, -10, -30, -50, -80, -110]
        close_triggered = False

        for profit in profits:
            resp = client.post('/api/position/report', json=make_report(
                profit_usd=profit
            ))
            data = resp.get_json()
            if data['command']['action'] == 'CLOSE':
                close_triggered = True
                assert "max loss" in data['command']['reason'].lower()
                break

        assert close_triggered, "Max loss guardrail should trigger CLOSE"


# ============================================================================
# Test 2: Multi-threaded concurrency test
# ============================================================================

class TestConcurrency:
    """Test thread safety of PositionManager under concurrent access."""

    def test_concurrent_reports_dont_corrupt_state(self):
        """Multiple threads sending reports should not corrupt position state."""
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(make_open_data())

        errors = []
        results = []

        def send_report(profit, thread_id):
            try:
                result = pm.update_position(make_report(profit_usd=profit))
                results.append({
                    "thread": thread_id,
                    "profit": profit,
                    "action": result["command"]["action"],
                })
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")

        # Launch 10 threads sending reports simultaneously
        threads = []
        for i in range(10):
            t = threading.Thread(target=send_report, args=(i * 5.0, i))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # No errors should have occurred
        assert len(errors) == 0, f"Thread errors: {errors}"
        # All threads should have gotten responses
        assert len(results) == 10

        # Position should still be valid
        assert pm.position is not None
        assert pm.position.ticket == 12345

    def test_concurrent_open_and_report(self):
        """Opening a position while reports are coming should be safe."""
        pm = PositionManager(exit_engine=None)
        errors = []

        def open_pos():
            try:
                pm.on_position_opened(make_open_data(ticket=11111))
            except Exception as e:
                errors.append(f"Open error: {e}")

        def send_report():
            try:
                # This may arrive before position is opened — should get HOLD
                pm.update_position(make_report(ticket=11111, profit_usd=10.0))
            except Exception as e:
                errors.append(f"Report error: {e}")

        # Launch both simultaneously
        t1 = threading.Thread(target=open_pos)
        t2 = threading.Thread(target=send_report)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0, f"Concurrent errors: {errors}"

    def test_can_open_trade_thread_safety(self):
        """can_open_trade() should be thread-safe (BUG-3 fix)."""
        pm = PositionManager(exit_engine=None)
        results = []

        def check_can_trade(thread_id):
            can, reason = pm.can_open_trade()
            results.append({"thread": thread_id, "can": can, "reason": reason})

        # 20 threads checking simultaneously
        threads = [
            threading.Thread(target=check_can_trade, args=(i,))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 20
        # All should agree since no position is open
        assert all(r["can"] is True for r in results)


# ============================================================================
# Test 3: LLM mock integration (simulated AI decisions)
# ============================================================================

class TestLLMMockIntegration:
    """Test position management with mocked LLM decisions."""

    def test_llm_close_decision_applied_via_http(self, client_with_engine):
        """When LLM says CLOSE, the HTTP response should contain CLOSE."""
        client, mock_engine, pm = client_with_engine

        # LLM will return CLOSE
        mock_engine.decide.return_value = PositionCommand(
            action="CLOSE", reason="AI: Target reached"
        )

        # Open position
        client.post('/api/position/opened', json=make_open_data())

        # Send report — should trigger LLM call and return CLOSE
        resp = client.post('/api/position/report', json=make_report(profit_usd=50.0))
        data = resp.get_json()

        assert data['has_command'] is True
        assert data['command']['action'] == 'CLOSE'
        assert "Target reached" in data['command']['reason']
        assert mock_engine.decide.called

    def test_llm_modify_sl_applied_via_http(self, client_with_engine):
        """When LLM says MODIFY_SL, response should contain the SL price."""
        client, mock_engine, pm = client_with_engine

        mock_engine.decide.return_value = PositionCommand(
            action="MODIFY_SL",
            sl_price=0.6210,
            reason="AI: Tighten stop loss"
        )

        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json=make_report(profit_usd=40.0))
        data = resp.get_json()

        assert data['command']['action'] == 'MODIFY_SL'
        assert data['command']['sl_price'] == 0.6210

    def test_llm_partial_close_sets_flag(self, client_with_engine):
        """After LLM returns PARTIAL_CLOSE, flag should be set."""
        client, mock_engine, pm = client_with_engine

        mock_engine.decide.return_value = PositionCommand(
            action="PARTIAL_CLOSE",
            close_percent=50,
            reason="AI: Take partial profit"
        )

        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json=make_report(profit_usd=65.0))
        data = resp.get_json()

        assert data['command']['action'] == 'PARTIAL_CLOSE'
        assert data['command']['close_percent'] == 50

        # Flag should be set (BUG-2 fix)
        assert pm.position.partial_closed is True

    def test_llm_hold_doesnt_trigger_action(self, client_with_engine):
        """When LLM says HOLD, no action should be returned."""
        client, mock_engine, pm = client_with_engine

        mock_engine.decide.return_value = PositionCommand(
            action="HOLD", reason="AI: Let it run"
        )

        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json=make_report(profit_usd=20.0))
        data = resp.get_json()

        assert data['command']['action'] == 'HOLD'

    def test_llm_error_returns_hold(self, client_with_engine):
        """When LLM throws error, should gracefully return HOLD."""
        client, mock_engine, pm = client_with_engine

        mock_engine.decide.side_effect = RuntimeError("API timeout")

        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json=make_report(profit_usd=20.0))
        data = resp.get_json()

        assert data['command']['action'] == 'HOLD'

    def test_llm_called_with_position_snapshot(self, client_with_engine):
        """LLM should receive a snapshot of position data (BUG-1 fix)."""
        client, mock_engine, pm = client_with_engine

        mock_engine.decide.return_value = PositionCommand(
            action="HOLD", reason="AI: monitoring"
        )

        client.post('/api/position/opened', json=make_open_data())
        client.post('/api/position/report', json=make_report(
            profit_usd=42.0,
            spread_pips=3.5,
            current_price=0.6242,
        ))

        # Verify LLM was called
        assert mock_engine.decide.called
        pos_snapshot = mock_engine.decide.call_args[0][0]

        # Snapshot should have the reported values
        assert pos_snapshot.profit_usd == 42.0
        assert pos_snapshot.spread_pips == 3.5
        assert pos_snapshot.current_price == 0.6242
        assert pos_snapshot.symbol == "NZDUSD"

    def test_guardrail_overrides_llm(self, client_with_engine):
        """Safety guardrails should override any LLM decision."""
        client, mock_engine, pm = client_with_engine

        # LLM says HOLD
        mock_engine.decide.return_value = PositionCommand(
            action="HOLD", reason="AI: Let it run"
        )

        client.post('/api/position/opened', json=make_open_data())

        # Report with -$120 loss — guardrail should override LLM
        resp = client.post('/api/position/report', json=make_report(
            profit_usd=-120.0
        ))
        data = resp.get_json()

        # Guardrail fires BEFORE LLM is even called
        assert data['command']['action'] == 'CLOSE'
        assert "max loss" in data['command']['reason'].lower()


# ============================================================================
# Test 4: Edge cases
# ============================================================================

class TestEdgeCases:
    """Test edge cases and error recovery."""

    def test_position_closed_during_llm_call(self):
        """If position is closed while LLM is processing, should handle gracefully."""
        slow_engine = MagicMock(spec=ExitDecisionEngine)

        def slow_decide(pos):
            time.sleep(0.1)  # Simulate slow LLM call
            return PositionCommand(action="CLOSE", reason="AI: close")

        slow_engine.decide.side_effect = slow_decide

        pm = PositionManager(exit_engine=slow_engine)
        pm.config = dict(pm.config)
        pm.config["llm_check_interval_seconds"] = 0

        pm.on_position_opened(make_open_data())

        # Start update in a thread (will call slow LLM)
        result_holder = [None]

        def do_update():
            result_holder[0] = pm.update_position(make_report(profit_usd=20.0))

        t = threading.Thread(target=do_update)
        t.start()

        # Close position while LLM is running
        time.sleep(0.05)
        pm.on_position_closed(make_close_data(profit=20.0))

        t.join(timeout=5)

        # Should not crash — returns HOLD because position is now None
        assert result_holder[0] is not None
        assert result_holder[0]["command"]["action"] == "HOLD"

    def test_double_close_no_crash(self, client):
        """Closing a position twice should not crash."""
        client.post('/api/position/opened', json=make_open_data())
        resp1 = client.post('/api/position/closed', json=make_close_data(profit=50.0))
        assert resp1.get_json()['status'] == 'ok'

        # Second close — no position to close, but should not error
        resp2 = client.post('/api/position/closed', json=make_close_data(profit=0.0))
        assert resp2.status_code == 200

    def test_report_without_open_no_crash(self, client):
        """Sending report without open position should not crash."""
        resp = client.post('/api/position/report', json=make_report())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['command']['action'] == 'HOLD'

    def test_empty_json_report(self, client):
        """Empty JSON body in report should be handled."""
        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json={})
        assert resp.status_code == 200

    def test_report_with_none_values(self, client):
        """Report with None values should not crash."""
        client.post('/api/position/opened', json=make_open_data())
        resp = client.post('/api/position/report', json={
            "ticket": 12345,
            "profit_usd": None,
            "spread_pips": None,
        })
        assert resp.status_code == 200

    def test_multiple_trade_cycles(self, client):
        """Multiple open/close cycles should track daily P/L correctly."""
        trades = [
            (50.0, "TP hit"),
            (-30.0, "SL hit"),
            (80.0, "AI close"),
        ]
        expected_pnl = 0.0

        for i, (profit, reason) in enumerate(trades):
            # Open
            resp = client.post('/api/position/opened',
                               json=make_open_data(ticket=1000 + i))
            assert resp.get_json()['status'] == 'ok'

            # Close
            resp = client.post('/api/position/closed',
                               json=make_close_data(ticket=1000 + i,
                                                    profit=profit,
                                                    reason=reason))
            assert resp.get_json()['status'] == 'ok'
            expected_pnl += profit

        # Verify cumulative daily P/L
        resp = client.get('/api/position/status')
        data = resp.get_json()
        assert data['daily_pnl_usd'] == expected_pnl  # 50 - 30 + 80 = 100
        assert data['daily_trades'] == 3
        assert data['closed_trades_today'] == 3

    def test_profit_protection_after_peak_then_recovery(self):
        """If profit peaks, drops 50%, but then recovers — should have closed at drop."""
        pm = PositionManager(exit_engine=None)
        pm.on_position_opened(make_open_data())

        # Rise to peak
        pm.update_position(make_report(profit_usd=50.0))
        pm.update_position(make_report(profit_usd=80.0))

        # Drop >50% from peak ($80 → $35 = 56% drop)
        result = pm.update_position(make_report(profit_usd=35.0))
        assert result['command']['action'] == 'CLOSE'
        assert "profit dropped" in result['command']['reason'].lower()


# ============================================================================
# Test 5: Full strategy simulation (rule-based, multi-scenario)
# ============================================================================

class TestStrategySimulation:
    """
    Simulate different market scenarios end-to-end:
    - V-shaped recovery
    - Slow bleed
    - Spike and fade
    - Choppy sideways
    """

    def _run_scenario(self, profit_curve, expect_close_action=None):
        """Helper: run a scenario and return all actions."""
        engine = ExitDecisionEngine(provider="rule-based")
        pm = PositionManager(exit_engine=engine)
        pm.config = dict(pm.config)
        pm.config["llm_check_interval_seconds"] = 0

        pm.on_position_opened(make_open_data())

        actions = []
        for i, profit in enumerate(profit_curve):
            if pm.position is None:
                break
            result = pm.update_position(make_report(
                profit_usd=profit,
                current_price=0.6200 + profit * 0.0001,
            ))
            actions.append({
                "step": i,
                "profit": profit,
                "action": result["command"]["action"],
                "reason": result["command"].get("reason", ""),
            })
        return actions

    def test_v_shaped_recovery(self):
        """Price drops initially but recovers strongly.
        Rule-based should hold through the dip (not deep enough to cut),
        then take action on the rise.
        """
        curve = [0, -5, -12, -15, -10, -5, 0, 8, 18, 25, 35, 45, 55, 65, 70]
        actions = self._run_scenario(curve)

        # Should have triggered SL-to-BE at profit > $30
        sl_actions = [a for a in actions if a["action"] == "MODIFY_SL"]
        assert len(sl_actions) > 0, "Should modify SL after recovery"

        # Should have triggered partial close at profit > $60
        partial_actions = [a for a in actions if a["action"] == "PARTIAL_CLOSE"]
        assert len(partial_actions) > 0, "Should partial close after strong recovery"

    def test_slow_bleed_long_hold(self):
        """Slow, consistent losses over 12 minutes.
        Should eventually cut at Rule 4 (loss after 10 min).
        """
        engine = ExitDecisionEngine(provider="rule-based")
        pm = PositionManager(exit_engine=engine)
        pm.config = dict(pm.config)
        pm.config["llm_check_interval_seconds"] = 0
        pm.on_position_opened(make_open_data())

        # Simulate 12 minutes passing
        pm.position.open_time = datetime.utcnow() - timedelta(minutes=11)

        curve = [-15, -18, -22, -25, -28]
        close_seen = False
        for profit in curve:
            if pm.position is None:
                break
            result = pm.update_position(make_report(profit_usd=profit))
            if result["command"]["action"] == "CLOSE":
                close_seen = True
                break

        assert close_seen, "Should cut slow bleed after 10+ minutes"

    def test_spike_and_fade(self):
        """Quick spike to $90 then gradual fade.
        Profit protection should trigger when profit drops >50% from peak.
        """
        curve = [0, 15, 35, 55, 75, 90, 85, 75, 60, 45, 40]
        actions = self._run_scenario(curve)

        # Should trigger profit protection around step 9-10
        # Peak was $90, drop to $40 = 55% drop > 50% threshold
        close_actions = [a for a in actions if a["action"] == "CLOSE"]
        assert len(close_actions) > 0, "Profit protection should trigger on fade"

    def test_choppy_sideways_no_overreaction(self):
        """Small oscillations around break-even.
        Rule-based should mostly HOLD — no unnecessary actions.
        """
        curve = [0, 5, -2, 8, 3, -5, 10, 2, -3, 7, 5]
        actions = self._run_scenario(curve)

        # Count non-HOLD actions — should be minimal (maybe 0)
        non_hold = [a for a in actions if a["action"] != "HOLD"]
        assert len(non_hold) <= 2, \
            f"Choppy market should not trigger many actions, got {len(non_hold)}: {non_hold}"

"""
Integration tests for AI Position Management endpoints.
Tests Flask endpoints for position lifecycle.

NOTE: These tests import the real server module with all its dependencies.
The python/ directory must be on the path and all dependencies installed.
"""
import pytest
import sys
import os

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from position_manager import PositionManager, PositionCommand
from server import app


@pytest.fixture
def client():
    """Create test client with isolated position manager.
    Prevents ensure_services() from overwriting our test PM.
    Restores originals after test.
    """
    from unittest.mock import MagicMock
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

    with app.test_client() as client:
        yield client

    # Restore originals
    server.decision_engine = orig_de
    server.calendar = orig_cal
    server.position_manager = orig_pm


def open_position(client, **overrides):
    """Helper to open a position via API."""
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
    return client.post('/api/position/opened', json=data)


def report_position(client, **overrides):
    """Helper to send position report via API."""
    data = {
        "ticket": 12345,
        "current_price": 0.6210,
        "remaining_lots": 0.50,
        "sl": 0.6170,
        "tp": 0.0,
        "profit_usd": 20.0,
        "tick_value": 10.0,
        "spread_pips": 2.5,
        "account_balance": 5000.00,
        "zone_bias": 0.0,
        "nearest_resistance": 0.6250,
        "nearest_support": 0.6180,
    }
    data.update(overrides)
    return client.post('/api/position/report', json=data)


def close_position(client, **overrides):
    """Helper to close a position via API."""
    data = {
        "ticket": 12345,
        "close_price": 0.6230,
        "profit": 150.0,
        "reason": "AI: TP reached",
    }
    data.update(overrides)
    return client.post('/api/position/closed', json=data)


# ============================================================================
# TestPositionEndpoints
# ============================================================================

class TestPositionEndpoints:
    """Test AI Position Management Flask endpoints."""

    def test_position_opened(self, client):
        """POST /api/position/opened should return status ok."""
        resp = open_position(client)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'

    def test_position_report_no_position(self, client):
        """Position report without open position should return HOLD."""
        resp = report_position(client)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['command']['action'] == 'HOLD'

    def test_position_report_with_position(self, client):
        """Position report with open position should return command."""
        open_position(client)
        resp = report_position(client, profit_usd=20.0)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'command' in data
        assert 'action' in data['command']

    def test_position_report_guardrail_triggers(self, client):
        """Position report with high loss should trigger CLOSE."""
        open_position(client)
        resp = report_position(client, profit_usd=-110.0)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['has_command'] is True
        assert data['command']['action'] == 'CLOSE'

    def test_position_closed(self, client):
        """POST /api/position/closed should return status ok."""
        open_position(client)
        resp = close_position(client, profit=75.0)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'

    def test_position_status(self, client):
        """GET /api/position/status should return status info."""
        resp = client.get('/api/position/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
        assert 'has_position' in data
        assert 'daily_pnl_usd' in data

    def test_position_status_with_open_position(self, client):
        """Status should show position details when open."""
        open_position(client)
        resp = client.get('/api/position/status')
        data = resp.get_json()
        assert data['has_position'] is True
        assert data['position']['ticket'] == 12345
        assert data['daily_trades'] == 1

    def test_full_lifecycle(self, client):
        """Full lifecycle: open → report → close → verify daily P/L."""
        # 1. Open position
        resp = open_position(client)
        assert resp.get_json()['status'] == 'ok'

        # 2. Send a few reports
        resp = report_position(client, profit_usd=20.0)
        assert resp.get_json()['command']['action'] == 'HOLD'

        resp = report_position(client, profit_usd=50.0)
        assert resp.status_code == 200

        # 3. Close position
        resp = close_position(client, profit=50.0)
        assert resp.get_json()['status'] == 'ok'

        # 4. Verify daily P/L
        resp = client.get('/api/position/status')
        data = resp.get_json()
        assert data['has_position'] is False
        assert data['daily_pnl_usd'] == 50.0
        assert data['daily_trades'] == 1
        assert data['closed_trades_today'] == 1


# ============================================================================
# TestDecisionIdEcho (F2): the EA echoes decision_id in its reports
# ============================================================================

class TestDecisionIdEcho:
    def _last_closed(self):
        import server
        return server.position_manager.closed_trades[-1]

    def test_orphaned_close_keeps_echoed_id(self, client):
        """Server restarted mid-trade (no tracked position): the EA echo is
        the only surviving lineage and must land in the trade record."""
        resp = close_position(client, decision_id="echo-abc123")
        assert resp.get_json()['status'] == 'ok'
        rec = self._last_closed()
        assert rec['decision_id'] == "echo-abc123"
        assert rec['event_name'] == "(position lost in restart)"

    def test_tracked_close_prefers_open_binding(self, client):
        """The opened-time binding wins; a (theoretical) divergent echo at
        close must not relabel the trade."""
        import server
        open_position(client)
        server.position_manager.position.decision_id = "bound-at-open"
        resp = close_position(client, decision_id="echo-at-close")
        assert resp.get_json()['status'] == 'ok'
        assert self._last_closed()['decision_id'] == "bound-at-open"

    def test_tracked_close_falls_back_to_echo(self, client):
        """Tracked position without a binding (old server at open time):
        the close echo fills the gap."""
        import server
        open_position(client)
        server.position_manager.position.decision_id = ""
        close_position(client, decision_id="echo-fills-gap")
        assert self._last_closed()['decision_id'] == "echo-fills-gap"

    def test_close_without_echo_still_works(self, client):
        """Old EA build sends no decision_id — everything degrades to ''."""
        close_position(client)
        assert self._last_closed()['decision_id'] == ""


# ============================================================================
# TestCalibrationEndpoint (F4)
# ============================================================================

class TestCalibrationEndpoint:
    def test_calibration_summary_served(self, client):
        import server
        from unittest.mock import Mock
        orig_dh, orig_pr = server.decision_history, server.path_recorder
        try:
            server.decision_history = Mock()
            server.decision_history.get_recent.return_value = [{
                "timestamp": "2026-07-15T13:28:00Z", "decision_id": "d1",
                "event_name": "CPI m/m", "currency": "USD", "pair": "USDCAD",
                "direction": "BUY", "confidence": 0.7, "forced": False,
                "event_datetime": "2026-07-15T13:30:00",
            }]
            server.path_recorder = Mock()
            server.path_recorder.get_recent.return_value = [{
                "test": False, "currency": "USD",
                "event_name": "CPI m/m", "event_name_normalized": "cpi m/m",
                "event_time": "2026-07-15T13:30:00", "pair": "USDCAD",
                "move_5min_pips": 18.0,
            }]
            resp = client.get('/api/calibration')
            data = resp.get_json()
            assert data['status'] == 'ok'
            assert data['calibration']['n_scored'] == 1
            assert data['calibration']['hit_rate'] == 1.0
        finally:
            server.decision_history = orig_dh
            server.path_recorder = orig_pr

    def test_calibration_tolerates_missing_services(self, client):
        import server
        orig_dh, orig_pr = server.decision_history, server.path_recorder
        try:
            server.decision_history = None
            server.path_recorder = None
            resp = client.get('/api/calibration')
            data = resp.get_json()
            assert data['status'] == 'ok'
            assert data['calibration']['n_scored'] == 0
        finally:
            server.decision_history = orig_dh
            server.path_recorder = orig_pr


# ============================================================================
# TestNoReanalysisAfterTradeOpen
# ============================================================================

def _make_fake_decision(evt_dt):
    """Minimal TradingDecision stand-in for the open/executed handlers."""
    from types import SimpleNamespace
    return SimpleNamespace(
        event="CPI y/y",
        decision_id="dup-test-1",
        reasoning="test reasoning",
        forced=False,
        direction="BUY",
        data_summary={"event": {"datetime": evt_dt.isoformat(),
                                "currency": "GBP"}},
    )


class TestNoReanalysisAfterTradeOpen:
    """Opening a trade clears next_decision while the event can still be
    seconds ahead — the event must be marked analyzed or the updater pays
    for a SECOND full LLM analysis (2026-07-22: 2x 3-call ensemble)."""

    def _setup_decision(self, server):
        from datetime import timedelta
        from timeutil import utcnow
        evt_dt = (utcnow() + timedelta(seconds=60)).replace(microsecond=0)
        decision = _make_fake_decision(evt_dt)
        with server.decision_lock:
            server.next_decision = decision
            server._last_served_signal = None
        server.analyzed_events.clear()
        # Event object as the updater's calendar scan would see it
        from types import SimpleNamespace
        fake_event = SimpleNamespace(datetime_utc=evt_dt,
                                     event_name="CPI y/y", currency="GBP")
        return decision, fake_event

    @staticmethod
    def _teardown(server):
        with server.decision_lock:
            server.next_decision = None
        server.analyzed_events.clear()

    def test_position_opened_marks_event_analyzed(self, client):
        import server
        decision, fake_event = self._setup_decision(server)
        try:
            resp = open_position(client, symbol="GBPUSD")
            assert resp.status_code == 200
            # The updater's scan predicate must now skip this event
            assert server._is_event_analyzed(fake_event) is True
            with server.decision_lock:
                assert server.next_decision is None
        finally:
            self._teardown(server)

    def test_trade_executed_fallback_marks_event_analyzed(self, client):
        import server
        decision, fake_event = self._setup_decision(server)
        try:
            resp = client.post('/api/trade-executed', json={"pair": "GBPUSD"})
            assert resp.status_code == 200
            assert server._is_event_analyzed(fake_event) is True
            with server.decision_lock:
                assert server.next_decision is None
        finally:
            self._teardown(server)

    def test_key_derivations_agree(self, client):
        """_analyzed_event_key (event object) and
        _analyzed_event_key_from_decision (decision dict) must produce the
        SAME key, otherwise the dedup marker silently misses."""
        import server
        decision, fake_event = self._setup_decision(server)
        try:
            assert (server._analyzed_event_key(fake_event)
                    == server._analyzed_event_key_from_decision(decision))
        finally:
            self._teardown(server)

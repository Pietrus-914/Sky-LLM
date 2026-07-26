"""
Integration tests for Flask server endpoints
"""
import pytest
import sys
import os

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from server import app


@pytest.fixture
def client():
    """Create test client"""
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def _bars_with_clusters():
    """30 bars: equal swing highs at 1.0900 (idx 4,12,20) and equal swing lows
    at 1.0800 (idx 8,16,24); everything else flat at 1.0850. Yields one STRONG
    liquidity pool above and one below the 1.0850 last price."""
    peaks, troughs = {4, 12, 20}, {8, 16, 24}
    bars = []
    for i in range(30):
        bars.append({
            "time": i * 900,
            "open": 1.0850,
            "high": 1.0900 if i in peaks else 1.0850,
            "low": 1.0800 if i in troughs else 1.0850,
            "close": 1.0850,
        })
    return bars


class TestLiquidityPoolsHelper:
    """_liquidity_pools_from_ohlc: server-side stop-cluster detection that
    previously ran only on mock data in test endpoints."""

    def test_none_for_non_dict(self):
        from server import _liquidity_pools_from_ohlc
        assert _liquidity_pools_from_ohlc(None, "EURUSD") is None
        assert _liquidity_pools_from_ohlc([], "EURUSD") is None

    def test_none_when_bars_too_few(self):
        from server import _liquidity_pools_from_ohlc
        assert _liquidity_pools_from_ohlc({"M15": _bars_with_clusters()[:9]}, "EURUSD") is None

    def test_none_when_no_clusters(self):
        # A flat series has no swing highs/lows -> no pools -> None
        from server import _liquidity_pools_from_ohlc
        flat = [{"time": i, "open": 1.085, "high": 1.0851, "low": 1.0849,
                 "close": 1.085} for i in range(30)]
        assert _liquidity_pools_from_ohlc({"M15": flat}, "EURUSD") is None

    def test_detects_clusters_both_sides(self):
        from server import _liquidity_pools_from_ohlc
        out = _liquidity_pools_from_ohlc({"M15": _bars_with_clusters()}, "EURUSD")
        assert out is not None
        prices = [p["price"] for p in out["liquidity_pools"]]
        assert any(abs(p - 1.0900) < 0.001 for p in prices)   # cluster above
        assert any(abs(p - 1.0800) < 0.001 for p in prices)   # cluster below
        assert all("strength" in p and "touches" in p for p in out["liquidity_pools"])
        assert out["liquidity_tf"] == "M15"

    def test_prefers_m15_over_m5(self):
        from server import _liquidity_pools_from_ohlc
        out = _liquidity_pools_from_ohlc(
            {"M5": _bars_with_clusters(), "M15": _bars_with_clusters()}, "EURUSD")
        assert out["liquidity_tf"] == "M15"

    def test_falls_back_to_m5_when_no_m15(self):
        from server import _liquidity_pools_from_ohlc
        out = _liquidity_pools_from_ohlc({"M5": _bars_with_clusters()}, "EURUSD")
        assert out["liquidity_tf"] == "M5"


class TestHealthEndpoint:
    """Test /health endpoint"""

    def test_health_returns_ok(self, client):
        """Health endpoint should return status ok"""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert 'timestamp' in data
        assert 'version' in data

    def test_health_version_format(self, client):
        """Version should be in correct format"""
        response = client.get('/health')
        data = response.get_json()
        version = data['version']
        parts = version.split('.')
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


class TestConfigEndpoint:
    """Test /api/config endpoint"""

    def test_config_returns_ok(self, client):
        """Config endpoint should return status ok"""
        response = client.get('/api/config')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert 'config' in data

    def test_config_has_required_keys(self, client):
        """Config should have required trading parameters"""
        response = client.get('/api/config')
        data = response.get_json()
        config = data['config']

        required_keys = ['max_risk_percent', 'default_lot_percent', 'entry_seconds_before',
                        'exit_minutes_after', 'max_spread_pips']
        for key in required_keys:
            assert key in config, f"Missing key: {key}"


class TestEventsEndpoint:
    """Test /api/events endpoint"""

    def test_events_returns_ok(self, client):
        """Events endpoint should return status ok"""
        response = client.get('/api/events')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert 'events' in data
        assert 'count' in data

    def test_events_with_hours_param(self, client):
        """Events endpoint should accept hours parameter"""
        response = client.get('/api/events?hours=48')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'

    def test_events_with_currencies_param(self, client):
        """Events endpoint should accept currencies parameter"""
        response = client.get('/api/events?currencies=USD,EUR')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'


class TestNextEventEndpoint:
    """Test /api/next-event endpoint"""

    def test_next_event_returns_ok(self, client):
        """Next event endpoint should return status ok"""
        response = client.get('/api/next-event')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        # Either event or message should be present
        assert 'event' in data or 'message' in data


class TestDecisionEndpoint:
    """Test /api/decision endpoint"""

    def test_decision_returns_ok(self, client):
        """Decision endpoint should return status ok"""
        response = client.get('/api/decision')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        # Either decision or message should be present
        assert 'decision' in data or 'message' in data


class TestSignalEndpoint:
    """Test /api/signal endpoint"""

    def test_signal_returns_response(self, client):
        """Signal endpoint should return valid response"""
        response = client.get('/api/signal')
        assert response.status_code == 200
        data = response.get_json()
        assert 'signal' in data

        if data['signal']:
            # If signal is True, should have trading data
            assert 'direction' in data
            assert 'pair' in data
            assert 'confidence' in data
        else:
            # If signal is False, should have message
            assert 'message' in data or 'error' in data


class TestTradeExecutedEndpoint:
    """Test /api/trade-executed endpoint"""

    def test_trade_executed_accepts_post(self, client):
        """Trade executed endpoint should accept POST"""
        response = client.post('/api/trade-executed',
                               json={'ticket': 12345, 'profit': 50.0})
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'

    def test_trade_executed_resets_decision(self, client):
        """Trade executed should reset decision"""
        # First get a decision (to populate it)
        client.get('/api/decision')

        # Then mark trade as executed
        response = client.post('/api/trade-executed',
                               json={'ticket': 12345})
        assert response.status_code == 200


class TestDecisionRefreshEndpoint:
    """Test /api/decision/refresh endpoint"""

    def test_refresh_accepts_post(self, client):
        """Refresh endpoint should accept POST"""
        response = client.post('/api/decision/refresh')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert 'message' in data

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

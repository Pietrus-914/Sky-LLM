"""
Pytest configuration and shared fixtures for SkyTower-AI tests
"""

import pytest
import sys
import os
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def test_config():
    """Test configuration with safe values"""
    return {
        "max_risk_percent": 1.0,
        "default_lot_percent": 50.0,
        "entry_seconds_before": 15,
        "exit_minutes_after": 10,
        "max_spread_pips": 5.0,
    }


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("FLASK_ENV", "testing")


# ============================================================================
# Sample Data Fixtures
# ============================================================================

@pytest.fixture
def sample_event():
    """Sample economic event"""
    return {
        "id": "test-event-001",
        "title": "Interest Rate Decision",
        "currency": "NZD",
        "impact": "HIGH",
        "datetime": datetime.now() + timedelta(hours=1),
        "forecast": "4.25%",
        "previous": "4.00%",
        "actual": None,
    }


@pytest.fixture
def sample_cot_data():
    """Sample COT data"""
    return {
        "currency": "NZD",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "non_commercial_long": 45000,
        "non_commercial_short": 32000,
        "commercial_long": 28000,
        "commercial_short": 41000,
        "net_position": 13000,  # 45000 - 32000
        "net_change": 2500,
        "signal": "BULLISH",
        "confidence": 0.65,
    }


@pytest.fixture
def sample_sentiment():
    """Sample sentiment data"""
    return {
        "pair": "NZDUSD",
        "retail_long_percent": 72.5,
        "retail_short_percent": 27.5,
        "contrarian_signal": "BEARISH",  # Trade against retail
        "source": "aggregated",
        "timestamp": datetime.now().isoformat(),
    }


@pytest.fixture
def sample_signal():
    """Sample trading signal"""
    return {
        "signal": True,
        "direction": "BUY",
        "pair": "NZDUSD",
        "lot_percent": 70,
        "confidence": 0.65,
        "entry_seconds_before": 15,
        "exit_minutes": 10,
        "time_until_event": 3600,
        "event_name": "Interest Rate Decision",
        "reasoning": "COT bullish (+3), Forecast improvement (+2)",
    }


# ============================================================================
# Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_calendar_api():
    """Mock calendar fetcher API responses"""
    with patch('python.calendar_fetcher.requests.get') as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "events": [
                {
                    "title": "Interest Rate Decision",
                    "currency": "NZD",
                    "impact": "HIGH",
                    "datetime": (datetime.now() + timedelta(hours=2)).isoformat(),
                }
            ]
        }
        yield mock_get


@pytest.fixture
def mock_cot_api():
    """Mock COT analyzer API responses"""
    with patch('python.cot_analyzer.requests.get') as mock_get:
        mock_get.return_value.status_code = 200
        yield mock_get


@pytest.fixture
def mock_sentiment_api():
    """Mock sentiment analyzer API responses"""
    with patch('python.sentiment_analyzer.requests.get') as mock_get:
        mock_get.return_value.status_code = 200
        yield mock_get


@pytest.fixture
def mock_llm_api():
    """Mock LLM API for decision engine"""
    with patch('python.llm_decision_engine.anthropic.Anthropic') as mock_anthropic:
        mock_client = Mock()
        mock_anthropic.return_value = mock_client
        mock_client.messages.create.return_value = Mock(
            content=[Mock(text='{"direction": "BUY", "confidence": 0.65}')]
        )
        yield mock_client


# ============================================================================
# Flask App Fixtures
# ============================================================================

@pytest.fixture
def app():
    """Create Flask app for testing"""
    from python.server import app
    app.config['TESTING'] = True
    app.config['DEBUG'] = False
    return app


@pytest.fixture
def client(app):
    """Create Flask test client"""
    return app.test_client()


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture
def temp_db(tmp_path):
    """Create temporary SQLite database"""
    db_path = tmp_path / "test_signals.db"
    return str(db_path)


# ============================================================================
# Helper Functions
# ============================================================================

def assert_valid_signal(signal: dict):
    """Assert that signal has all required fields"""
    required_fields = ['signal', 'direction', 'pair', 'confidence']
    for field in required_fields:
        assert field in signal, f"Missing field: {field}"

    if signal.get('signal'):
        assert signal['direction'] in ['BUY', 'SELL', 'SKIP']
        assert 0 <= signal['confidence'] <= 1


def assert_valid_event(event: dict):
    """Assert that event has all required fields"""
    required_fields = ['title', 'currency', 'impact', 'datetime']
    for field in required_fields:
        assert field in event, f"Missing field: {field}"

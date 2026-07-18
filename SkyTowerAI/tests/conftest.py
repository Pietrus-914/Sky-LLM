"""
Pytest configuration and shared fixtures for SkyTower-AI tests
"""

import pytest
import sys
import os

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))


@pytest.fixture(autouse=True)
def _pin_ensemble_k(monkeypatch):
    """The suite must not depend on the operator's .env: SKYTOWER_ENSEMBLE_K=3
    in production would silently reroute every single-call test through the
    ensemble path. Tests that exercise the ensemble monkeypatch K themselves
    (their setattr overrides this pin)."""
    import llm_decision_engine
    monkeypatch.setattr(llm_decision_engine, "ENSEMBLE_K", 1)


@pytest.fixture(autouse=True)
def _no_real_reflections(monkeypatch):
    """Tests must NEVER fire the real post-trade reflection worker: with the
    operator's .env key present, integration tests POSTing /api/position/
    closed were making PAID LLM calls and appending synthetic reflections to
    the PRODUCTION logs/trade_reflections.jsonl — 87 fake NZDUSD entries had
    poisoned the learning loop before this pin (quarantined 2026-07-18).
    Reflection tests re-enable explicitly with their own tmp stores + fake
    chat functions."""
    import config
    import server
    monkeypatch.setattr(config, "REFLECTIONS_ENABLED", False)
    # A fixture-scoped chat fn from an earlier test must not linger either
    monkeypatch.setattr(server, "_reflection_chat_fn", None)
    monkeypatch.setattr(server, "_reflection_chat_ready", True)


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

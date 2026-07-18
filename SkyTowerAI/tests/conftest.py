"""
Pytest configuration and shared fixtures for SkyTower-AI tests
"""

import pytest
import sys
import os

# Add python directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python'))

import calendar_fetcher as _cf

# Originals captured BEFORE the autouse offline pin below — the
# real_calendar_fetch fixture hands them back to the few tests that
# genuinely test fetching (with requests mocked by the test itself)
_REAL_CALENDAR_FETCHES = {
    "ff": _cf.ForexFactoryCalendar.fetch_events,
    "te": _cf.TradingEconomicsCalendar.fetch_events,
    "finnhub": _cf.FinnhubCalendar.fetch_events,
}


def pytest_configure(config):
    """Opt-in hard proof of network isolation: SKYTOWER_TEST_BLOCK_NET=1
    makes any non-loopback DNS lookup or connect raise. Used to verify the
    suite runs fully offline; harmless to leave on in CI."""
    if os.getenv("SKYTOWER_TEST_BLOCK_NET") != "1":
        return
    import socket
    real_gai = socket.getaddrinfo
    real_connect = socket.socket.connect
    LOOPBACK = ("127.0.0.1", "localhost", "::1", None, "")

    def guarded_gai(host, *args, **kwargs):
        if host not in LOOPBACK:
            raise RuntimeError(f"NETWORK BLOCKED in tests: DNS for {host!r}")
        return real_gai(host, *args, **kwargs)

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            raise RuntimeError(f"NETWORK BLOCKED in tests: connect to {address}")
        return real_connect(self, address, *args, **kwargs)

    socket.getaddrinfo = guarded_gai
    socket.socket.connect = guarded_connect


@pytest.fixture(autouse=True)
def _pin_ensemble_k(monkeypatch):
    """The suite must not depend on the operator's .env: SKYTOWER_ENSEMBLE_K=3
    in production would silently reroute every single-call test through the
    ensemble path. Tests that exercise the ensemble monkeypatch K themselves
    (their setattr overrides this pin)."""
    import llm_decision_engine
    monkeypatch.setattr(llm_decision_engine, "ENSEMBLE_K", 1)


@pytest.fixture(autouse=True)
def _no_calendar_network(monkeypatch):
    """Tests must NEVER hit the real calendar feeds: the FF feed 429s when
    hammered, and a successful fetch OVERWRITES the production
    logs/ff_calendar_cache.json (both observed when integration tests ran
    ensure_services() with live fetching — same failure class as the paid
    reflection calls fixed by _no_real_reflections). The pin serves the
    disk cache loaded at init (no fetch, no cache write); TE/Finnhub are
    flaky-broken sources and return []. Tests that genuinely test fetching
    request the real_calendar_fetch fixture and mock requests themselves."""

    def _ff_offline(self, days_ahead: int = 7):
        return list(self._last_good)

    monkeypatch.setattr(_cf.ForexFactoryCalendar, "fetch_events", _ff_offline)
    monkeypatch.setattr(_cf.TradingEconomicsCalendar, "fetch_events",
                        lambda self, days_ahead=7: [])
    monkeypatch.setattr(_cf.FinnhubCalendar, "fetch_events",
                        lambda self, days_ahead=7: [])


@pytest.fixture
def real_calendar_fetch(monkeypatch):
    """Restores the REAL fetch_events implementations for a test that
    exercises the fetch path itself (the test must mock requests)."""
    monkeypatch.setattr(_cf.ForexFactoryCalendar, "fetch_events",
                        _REAL_CALENDAR_FETCHES["ff"])
    monkeypatch.setattr(_cf.TradingEconomicsCalendar, "fetch_events",
                        _REAL_CALENDAR_FETCHES["te"])
    monkeypatch.setattr(_cf.FinnhubCalendar, "fetch_events",
                        _REAL_CALENDAR_FETCHES["finnhub"])


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

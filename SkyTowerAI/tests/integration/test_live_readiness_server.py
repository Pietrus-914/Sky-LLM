"""Server-level pins for the pre-live review fixes (2026-07-26)."""

import os
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python")
)

import config as cfg
import server
from timeutil import utcnow


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as test_client:
        yield test_client


class TestDashboardEventWhitelist:
    """The dashboard Save sends {events: [...]} — previously dropped
    silently, so panel checkbox edits never reached trade selection."""

    def test_events_key_filters_tiers_and_persists(self, client, monkeypatch):
        saved = {}
        monkeypatch.setattr(cfg, "save_runtime_overrides", saved.update)
        backup = (cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS)
        try:
            response = client.post(
                "/api/config/events", json={"events": ["CPI", "GDP"]}
            )
            assert response.status_code == 200
            assert cfg.TIER1_EVENTS == ["CPI"]
            assert cfg.TIER2_EVENTS == ["GDP"]
            assert cfg.HIGH_IMPACT_EVENTS == ["CPI", "GDP"]
            assert saved == {"enabled_events": ["CPI", "GDP"]}

            # Re-enabling everything restores the FULL rosters (the filter
            # must never shrink the immutable *_ALL baselines).
            response = client.post(
                "/api/config/events",
                json={"events": cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL},
            )
            assert response.status_code == 200
            assert cfg.TIER1_EVENTS == cfg.TIER1_EVENTS_ALL
            assert cfg.TIER2_EVENTS == cfg.TIER2_EVENTS_ALL
        finally:
            cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS = backup

    def test_unknown_names_are_ignored(self, client, monkeypatch):
        monkeypatch.setattr(cfg, "save_runtime_overrides", lambda _u: None)
        backup = (cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS)
        try:
            client.post(
                "/api/config/events",
                json={"events": ["CPI", "Totally Made Up Event"]},
            )
            assert cfg.HIGH_IMPACT_EVENTS == ["CPI"]
        finally:
            cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS = backup


class TestRefreshDecisionLockDiscipline:
    """The paid 20-60s analysis must run OUTSIDE decision_lock — /api/signal
    and /api/position/opened block on that lock during the entry window."""

    def test_llm_call_runs_without_holding_decision_lock(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(server, "ensure_services", lambda: None)

        event = SimpleNamespace(
            datetime_utc=utcnow() + timedelta(seconds=60)
        )
        calendar = MagicMock()
        calendar.get_tradeable_events.return_value = [event]
        monkeypatch.setattr(server, "calendar", calendar)

        observed = {}

        def _analyze():
            observed["lock_held"] = server.decision_lock.locked()
            return None

        engine = MagicMock()
        engine.get_next_trade_recommendation.side_effect = _analyze
        monkeypatch.setattr(server, "decision_engine", engine)
        monkeypatch.setattr(server, "next_decision", None)

        response = client.post("/api/decision/refresh")

        assert response.status_code == 200
        assert observed["lock_held"] is False
        # Live-config path: the calendar must read cfg.HIGH_IMPACT_EVENTS at
        # call time (event_keywords=None), not a stale import-time binding.
        assert (
            calendar.get_tradeable_events.call_args.kwargs["event_keywords"]
            is None
        )

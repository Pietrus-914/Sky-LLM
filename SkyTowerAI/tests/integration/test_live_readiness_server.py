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
    """The dashboard Save sends {events: [...checked...]} — the server turns
    that into the DISABLED complement (18.08.2026). Storing the enabled list
    instead let a dashboard whose hardcoded roster was older than config.py
    silently disable every newer name (Federal Funds Rate / Official Bank
    Rate / Overnight Rate — FOMC, BoE and BoC untradeable after any Save)."""

    def test_events_key_persists_the_disabled_complement(self, client, monkeypatch):
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
            # DISABLED names are persisted (and the legacy key retired)
            assert set(saved["disabled_events"]) == (
                set(cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL) - {"CPI", "GDP"}
            )
            assert saved["enabled_events"] is None

            # Re-enabling everything restores the FULL rosters (the filter
            # must never shrink the immutable *_ALL baselines).
            response = client.post(
                "/api/config/events",
                json={"events": cfg.TIER1_EVENTS_ALL + cfg.TIER2_EVENTS_ALL},
            )
            assert response.status_code == 200
            assert cfg.TIER1_EVENTS == cfg.TIER1_EVENTS_ALL
            assert cfg.TIER2_EVENTS == cfg.TIER2_EVENTS_ALL
            assert saved["disabled_events"] == []
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

    def test_get_serves_the_full_rosters_for_rendering(self, client):
        """The panel must render its checkboxes from the SERVER roster; a
        roster baked into dashboard.html is exactly how the bug happened."""
        body = client.get("/api/config/events").get_json()
        assert body["tier1_events_all"] == cfg.TIER1_EVENTS_ALL
        assert body["tier2_events_all"] == cfg.TIER2_EVENTS_ALL
        assert "disabled_events" in body
        # Every rate-decision alias the FF feed actually uses is offered
        for name in ("Federal Funds Rate", "Official Bank Rate", "Overnight Rate"):
            assert name in body["tier1_events_all"]

    def test_save_from_a_stale_panel_cannot_hide_a_roster_addition(
            self, client, monkeypatch):
        """A tab opened BEFORE a roster-changing restart still holds the old
        roster. It declares what it rendered, and the server only flips those
        names — otherwise its Save would re-create the original bug in
        miniature (silently disabling names that had no checkbox)."""
        monkeypatch.setattr(cfg, "save_runtime_overrides", lambda _u: None)
        backup = (cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS)
        try:
            legacy = list(cfg.LEGACY_PANEL_EVENT_ROSTER)
            resp = client.post("/api/config/events",
                               json={"events": legacy, "roster": legacy})
            assert resp.status_code == 200
            # Names the stale tab never displayed keep trading
            for name in ("Federal Funds Rate", "Official Bank Rate",
                         "Overnight Rate", "Consumer Price Index"):
                assert name in cfg.HIGH_IMPACT_EVENTS, name
            # ...and what it DID uncheck is still honoured
            client.post("/api/config/events",
                        json={"events": [n for n in legacy if n != "Retail Sales"],
                              "roster": legacy})
            assert "Retail Sales" not in cfg.HIGH_IMPACT_EVENTS
            assert "Federal Funds Rate" in cfg.HIGH_IMPACT_EVENTS
        finally:
            cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS = backup

    def test_without_a_roster_the_complement_is_the_full_rosters(
            self, client, monkeypatch):
        """Scripted POSTs (and the pre-18.08 panel) send no `roster` — the
        documented contract stays: everything not listed is disabled."""
        saved = {}
        monkeypatch.setattr(cfg, "save_runtime_overrides", saved.update)
        backup = (cfg.TIER1_EVENTS, cfg.TIER2_EVENTS, cfg.HIGH_IMPACT_EVENTS)
        try:
            client.post("/api/config/events", json={"events": ["CPI"]})
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

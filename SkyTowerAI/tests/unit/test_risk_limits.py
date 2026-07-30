"""
Risk-limit invariants (gpt_review, 30.07.2026).

Two gaps found after the 29.07.2026 FOMC trade:
  * every risk field was range-checked in isolation, so a per-trade budget
    ABOVE the daily budget passed validation — one trade could spend the whole
    day's loss allowance, because the daily limit only blocks the NEXT entry;
  * max_loss_usd is also what the EA uses to SIZE the lot, so an oversized
    value inflates the position and raises every guardrail meant to cap it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

import config as cfg
from calendar_fetcher import CalendarAggregator, is_non_data_event


class TestRiskLimitConflicts:
    def test_consistent_limits_report_nothing(self):
        assert cfg.risk_limit_conflicts(
            {"max_loss_usd": 100.0, "max_daily_loss_usd": 300.0}) == []

    def test_per_trade_equal_to_daily_is_allowed(self):
        """A single all-in trade is a legitimate (if aggressive) setup."""
        assert cfg.risk_limit_conflicts(
            {"max_loss_usd": 300.0, "max_daily_loss_usd": 300.0}) == []

    def test_per_trade_above_daily_is_a_conflict(self):
        conflicts = cfg.risk_limit_conflicts(
            {"max_loss_usd": 1000.0, "max_daily_loss_usd": 300.0})
        assert len(conflicts) == 1
        assert "max_loss_usd" in conflicts[0]
        assert "max_daily_loss_usd" in conflicts[0]

    def test_missing_or_junk_values_do_not_raise(self):
        assert cfg.risk_limit_conflicts({}) == []
        assert cfg.risk_limit_conflicts(
            {"max_loss_usd": None, "max_daily_loss_usd": "300"}) == []

    def test_live_config_is_self_consistent(self):
        """Whatever the operator's panel/env currently says, the shipped
        config must never arm a per-trade budget bigger than the day's."""
        assert cfg.risk_limit_conflicts(cfg.POSITION_MANAGEMENT_CONFIG) == []


class TestRiskEndpointRejectsConflicts:
    """Only the REJECTION path is exercised: it returns before touching the
    live config or writing logs/runtime_overrides.json, so the operator's
    real risk settings cannot be modified by the test run."""

    @pytest.fixture
    def client(self, monkeypatch):
        from server import app
        app.config['TESTING'] = True
        before = dict(cfg.POSITION_MANAGEMENT_CONFIG)
        saved = []
        monkeypatch.setattr(cfg, "save_runtime_overrides", saved.append)
        with app.test_client() as c:
            yield c, saved
        cfg.POSITION_MANAGEMENT_CONFIG.clear()
        cfg.POSITION_MANAGEMENT_CONFIG.update(before)

    def test_per_trade_above_daily_is_rejected(self, client):
        c, saved = client
        daily = cfg.POSITION_MANAGEMENT_CONFIG["max_daily_loss_usd"]
        before = cfg.POSITION_MANAGEMENT_CONFIG["max_loss_usd"]

        resp = c.post('/api/config/risk', json={"max_loss_usd": daily * 3})

        assert resp.status_code == 400
        assert "max_daily_loss_usd" in resp.get_json()["message"]
        # Nothing applied, nothing persisted
        assert cfg.POSITION_MANAGEMENT_CONFIG["max_loss_usd"] == before
        assert saved == []

    def test_lowering_daily_below_per_trade_is_rejected(self, client):
        c, saved = client
        per_trade = cfg.POSITION_MANAGEMENT_CONFIG["max_loss_usd"]

        resp = c.post('/api/config/risk',
                      json={"max_daily_loss_usd": max(per_trade - 1, 10)})

        assert resp.status_code == 400
        assert saved == []

    def test_out_of_range_still_rejected_first(self, client):
        c, _ = client
        resp = c.post('/api/config/risk', json={"max_loss_usd": 1})
        assert resp.status_code == 400
        assert "between" in resp.get_json()["message"]


class TestForcedFlagSurvivesReconcile:
    """A reconcile report REBUILDS server state after a restart. The EA now
    sends `forced`, but an older build omits it — and defaulting to False
    re-registered a FORCE_DECISION coin flip as a genuine trade, defeating
    every downstream forced filter (track record, calibration, reflections)."""

    def _pm(self, lookup=None):
        from position_manager import PositionManager
        return PositionManager(exit_engine=None, forced_lookup=lookup)

    def test_explicit_flag_is_honoured(self):
        pm = self._pm(lookup=lambda did: False)
        assert pm.resolve_forced({"forced": True, "decision_id": "x"}) is True
        assert pm.resolve_forced({"forced": False, "decision_id": "x"}) is False

    def test_missing_flag_falls_back_to_the_decision_row(self):
        pm = self._pm(lookup=lambda did: did == "forced-one")
        assert pm.resolve_forced({"decision_id": "forced-one"}) is True
        assert pm.resolve_forced({"decision_id": "genuine"}) is False

    def test_missing_flag_without_lineage_is_not_forced(self):
        pm = self._pm(lookup=lambda did: True)
        assert pm.resolve_forced({}) is False

    def test_broken_lookup_does_not_raise(self):
        def boom(_did):
            raise RuntimeError("history unreadable")
        pm = self._pm(lookup=boom)
        assert pm.resolve_forced({"decision_id": "x"}) is False

    def test_explicit_false_does_not_veto_the_decision_row(self):
        """An EA that adopted a position from version-1 recovery metadata has
        no stored marker and honestly reports False — decision_history still
        holds the truth, so the flag is a STICKY OR, not a short circuit."""
        pm = self._pm(lookup=lambda did: did == "was-forced")
        assert pm.resolve_forced(
            {"forced": False, "decision_id": "was-forced"}) is True
        assert pm.resolve_forced(
            {"forced": False, "decision_id": "genuine"}) is False

    def test_position_opened_endpoint_honours_the_ea_flag(self, monkeypatch,
                                                          tmp_path):
        """/api/position/opened is the endpoint the EA actually uses to
        re-adopt a position after a restart — the path where the served-signal
        lineage is already gone."""
        import server
        from position_manager import PositionManager
        # ensure_services() runs INSIDE the handler and rebuilds
        # server.position_manager when services were never initialised, which
        # would discard the patch below (and, before the conftest guard, wrote
        # the real logs/active_position.json). Initialise first, then patch.
        server.ensure_services()
        pm = PositionManager(exit_engine=None,
                             forced_lookup=lambda did: False,
                             state_file=str(tmp_path / "active.json"))
        monkeypatch.setattr(server, "position_manager", pm)
        monkeypatch.setattr(server, "_last_served_signal", None)
        monkeypatch.setattr(server, "next_decision", None)
        server.app.config['TESTING'] = True

        with server.app.test_client() as c:
            resp = c.post('/api/position/opened', json={
                "ticket": "555", "position_id": "555", "symbol": "USDCAD",
                "direction": "BUY", "entry_price": 1.3700, "lots": 0.5,
                "remaining_lots": 0.5, "max_loss_usd": 100.0,
                "tick_value": 10.0, "decision_id": "abc",
                "forced": True, "recovered": True,
            })
        assert resp.status_code == 200
        assert pm.position is not None
        assert pm.position.forced is True

    def test_position_opened_endpoint_falls_back_to_history(self, monkeypatch,
                                                            tmp_path):
        """Old EA build: no forced field in the payload at all."""
        import server
        from position_manager import PositionManager
        # ensure_services() runs INSIDE the handler and rebuilds
        # server.position_manager when services were never initialised, which
        # would discard the patch below (and, before the conftest guard, wrote
        # the real logs/active_position.json). Initialise first, then patch.
        server.ensure_services()
        pm = PositionManager(exit_engine=None,
                             forced_lookup=lambda did: did == "abc",
                             state_file=str(tmp_path / "active.json"))
        monkeypatch.setattr(server, "position_manager", pm)
        monkeypatch.setattr(server, "_last_served_signal", None)
        monkeypatch.setattr(server, "next_decision", None)
        server.app.config['TESTING'] = True

        with server.app.test_client() as c:
            resp = c.post('/api/position/opened', json={
                "ticket": "556", "position_id": "556", "symbol": "USDCAD",
                "direction": "BUY", "entry_price": 1.3700, "lots": 0.5,
                "remaining_lots": 0.5, "max_loss_usd": 100.0,
                "tick_value": 10.0, "decision_id": "abc", "recovered": True,
            })
        assert resp.status_code == 200
        assert pm.position.forced is True

    def test_reconcile_report_marks_the_position_forced(self):
        from datetime import datetime, timezone
        pm = self._pm()
        pm.update_position({
            "reconcile": True, "ticket": "777", "position_id": "777",
            "symbol": "USDCAD", "direction": "BUY", "entry_price": 1.3700,
            "lots": 0.5, "remaining_lots": 0.5, "max_loss_usd": 100.0,
            "forced": True, "decision_id": "abc",
            "open_time": int(datetime.now(timezone.utc).timestamp()) - 60,
        })
        assert pm.position is not None
        assert pm.position.forced is True


class TestVoteEventsAreNeverTradeable:
    """'MPC Official Bank Rate Votes' publishes a vote split ("0-0-9"), not a
    rate: parse_numeric reads it as 0 and fakes "no change". Its name contains
    the TIER1 substring "Official Bank Rate", so without a marker it was
    tradeable AND could shadow the real BoE decision in the same minute (the
    server holds a single next_decision slot). regime_tracker has excluded
    "votes" for the same reason since the historical replay corrupted GBP."""

    def test_votes_event_is_non_data(self):
        assert is_non_data_event("MPC Official Bank Rate Votes") is True

    def test_real_rate_decision_is_still_data(self):
        assert is_non_data_event("Official Bank Rate") is False
        assert is_non_data_event("Federal Funds Rate") is False

    def _event(self, name, currency="GBP"):
        from datetime import timedelta
        from calendar_fetcher import EconomicEvent
        from timeutil import utcnow
        return EconomicEvent(
            datetime_utc=utcnow() + timedelta(minutes=30),
            currency=currency, event_name=name, impact="HIGH",
            forecast="", previous="", actual="", source="forexfactory")

    def test_votes_event_is_not_tradeable_despite_tier1_substring(self):
        assert CalendarAggregator._event_is_tradeable(
            self._event("MPC Official Bank Rate Votes"),
            ["Official Bank Rate"], self._event("x").datetime_utc,
            trade_static=False, trade_all=False) is False

    def test_votes_event_is_not_tradeable_in_trade_all_mode(self):
        """TRADE_ALL_EVENTS ignores the name whitelist — the marker must
        still hold, exactly like speeches do."""
        assert CalendarAggregator._event_is_tradeable(
            self._event("MPC Official Bank Rate Votes"),
            [], self._event("x").datetime_utc,
            trade_static=False, trade_all=True) is False

    def test_real_boe_decision_remains_tradeable(self):
        assert CalendarAggregator._event_is_tradeable(
            self._event("Official Bank Rate"),
            ["Official Bank Rate"], self._event("x").datetime_utc,
            trade_static=False, trade_all=False) is True

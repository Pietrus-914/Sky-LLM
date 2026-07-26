from datetime import timedelta
from types import SimpleNamespace

import pytest

import server
from timeutil import utcnow


class _AvailablePositionManager:
    @staticmethod
    def can_open_trade():
        return True, "OK"


def _decision(lot_percent):
    event_time = utcnow() + timedelta(seconds=60)
    return SimpleNamespace(
        event="STAGE 2 CONTRACT TEST",
        currency="NZD",
        pair="NZDUSD",
        direction="BUY",
        confidence=0.8,
        lot_percent=lot_percent,
        entry_seconds_before=15,
        exit_minutes_after=5,
        stop_loss_percent=40,
        stop_loss_pips=25,
        take_profit_pips=40,
        reasoning="contract test",
        forced=False,
        decision_id="stage2-contract",
        data_summary={
            "event": {
                "datetime": event_time.isoformat(),
                "currency": "NZD",
            }
        },
    )


@pytest.fixture
def contract_client(monkeypatch):
    monkeypatch.setattr(server, "ensure_services", lambda: None)
    monkeypatch.setattr(
        server, "position_manager", _AvailablePositionManager()
    )
    monkeypatch.setattr(server, "executed_trades", set())
    monkeypatch.setattr(server, "_last_served_signal", None)
    monkeypatch.setattr(server, "_signal_served_log_key", None)
    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        yield client


@pytest.mark.parametrize(
    "lot_percent",
    [None, True, "bad", 0, -1, 100.1, float("nan"), float("inf")],
)
def test_signal_rejects_non_actionable_lot_without_served_lineage(
    contract_client, monkeypatch, lot_percent
):
    monkeypatch.setattr(server, "next_decision", _decision(lot_percent))
    response = contract_client.get("/api/signal?pair=NZDUSD")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["signal"] is False
    assert server._last_served_signal is None


@pytest.mark.parametrize("lot_percent", [1, 60, 100])
def test_signal_serves_only_finite_positive_risk(
    contract_client, monkeypatch, lot_percent
):
    monkeypatch.setattr(server, "next_decision", _decision(lot_percent))
    response = contract_client.get("/api/signal?pair=NZDUSD")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["signal"] is True
    assert payload["lot_percent"] == float(lot_percent)
    assert payload["max_loss_usd"] > 0
    assert b"NaN" not in response.data
    assert b"Infinity" not in response.data


def test_signal_rejects_unknown_direction_before_served_lineage(
    contract_client, monkeypatch
):
    decision = _decision(60)
    decision.direction = "HOLD"
    monkeypatch.setattr(server, "next_decision", decision)
    payload = contract_client.get("/api/signal?pair=NZDUSD").get_json()
    assert payload["signal"] is False
    assert server._last_served_signal is None


def test_signal_rejects_expired_decision_before_served_lineage(
    contract_client, monkeypatch
):
    decision = _decision(60)
    decision.data_summary["event"]["datetime"] = (
        utcnow() - timedelta(seconds=1)
    ).isoformat()
    monkeypatch.setattr(server, "next_decision", decision)
    payload = contract_client.get("/api/signal?pair=NZDUSD").get_json()
    assert payload["signal"] is False
    assert server._last_served_signal is None


@pytest.mark.parametrize("lot_percent", [0, -1, 101, "bad"])
def test_manual_test_signal_rejects_invalid_lot(
    contract_client, lot_percent
):
    response = contract_client.post(
        "/api/test-signal",
        json={
            "pair": "NZDUSD",
            "direction": "BUY",
            "lot_percent": lot_percent,
        },
    )
    assert response.status_code == 400

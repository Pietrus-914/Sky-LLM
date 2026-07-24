"""Lifecycle API contracts added by active-position recovery."""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python")
)

import server
from position_manager import PositionManager
from server import app


def snapshot(**overrides):
    value = {
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "magic": "20889916",
        "symbol": "GBPUSD",
        "direction": "BUY",
        "entry_price": 1.28452,
        "lots": 0.5,
        "remaining_lots": 0.5,
        "sl": 1.28152,
        "tp": 0.0,
        "max_loss_usd": 75.0,
        "event_name": "CPI y/y",
        "decision_id": "decision-recovery-api",
    }
    value.update(overrides)
    return value


@pytest.fixture
def client(monkeypatch, tmp_path):
    original = (
        server.decision_engine,
        server.calendar,
        server.position_manager,
    )
    server.decision_engine = MagicMock()
    server.calendar = MagicMock()
    server.position_manager = PositionManager(
        exit_engine=None,
        history_file=str(tmp_path / "history.jsonl"),
        state_file=str(tmp_path / "active.json"),
    )
    reflection = MagicMock()
    monkeypatch.setattr(server, "_spawn_reflection", reflection)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client, reflection
    (
        server.decision_engine,
        server.calendar,
        server.position_manager,
    ) = original


def test_duplicate_open_is_reconciled_without_second_trade(client):
    http, _ = client

    first = http.post("/api/position/opened", json=snapshot())
    second = http.post("/api/position/opened", json=snapshot())

    assert first.status_code == 200
    assert first.get_json()["registration"] == "opened"
    assert second.status_code == 200
    assert second.get_json()["registration"] == "reconciled"
    assert server.position_manager.daily_trades == 1


def test_duplicate_close_has_one_pnl_record_and_reflection(client):
    http, reflection = client
    http.post("/api/position/opened", json=snapshot())
    close = {
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": -30.0,
        "reason": "SL",
    }

    first = http.post("/api/position/closed", json=close)
    second = http.post("/api/position/closed", json=close)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True
    assert server.position_manager.daily_pnl_usd == -30.0
    assert len(server.position_manager.closed_trades) == 1
    reflection.assert_called_once()


def test_stale_close_returns_conflict_and_keeps_current_position(client):
    http, _ = client
    http.post("/api/position/opened", json=snapshot())
    http.post("/api/position/closed", json={
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": 10.0,
    })
    http.post("/api/position/opened", json=snapshot(
        ticket="222", position_id="position-B"
    ))

    response = http.post("/api/position/closed", json={
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": 10.0,
    })

    assert response.status_code == 409
    assert server.position_manager.position.position_id == "position-B"


def test_empty_reconcile_contract(client):
    http, _ = client

    response = http.post(
        "/api/position/reconcile", json={"has_position": False}
    )

    assert response.status_code == 200
    assert response.get_json()["reconciliation"] == "empty"
    assert response.get_json()["allow_new_trades"] is True


def test_invalid_open_snapshot_returns_422(client):
    http, _ = client

    response = http.post("/api/position/opened", json={"ticket": "1"})

    assert response.status_code == 422


def test_close_without_broker_identity_returns_422(client):
    http, reflection = client
    http.post("/api/position/opened", json=snapshot())

    response = http.post("/api/position/closed", json={
        "profit": 10.0,
        "reason": "missing identity",
    })

    assert response.status_code == 422
    assert server.position_manager.position is not None
    assert server.position_manager.daily_pnl_usd == 0.0
    reflection.assert_not_called()

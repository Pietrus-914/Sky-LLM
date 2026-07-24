"""Recovery and reconciliation tests for an active broker position."""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "python")
)

from position_manager import (
    PositionConflictError,
    PositionManager,
    PositionPersistenceError,
)
from position_store import PositionStore, PositionStoreError


def broker_snapshot(**overrides):
    data = {
        "reconcile": True,
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "magic": "20889916",
        "symbol": "GBPUSD",
        "direction": "BUY",
        "entry_price": 1.28452,
        "current_price": 1.28510,
        "lots": 0.50,
        "remaining_lots": 0.50,
        "sl": 1.28152,
        "tp": 0.0,
        "profit_usd": 18.40,
        "tick_value": 10.0,
        "account_balance": 10_000.0,
        "event_name": "CPI y/y",
        "decision_id": "decision-recovery-1",
        "max_loss_usd": 75.0,
        "open_time": int(datetime.now(timezone.utc).timestamp()),
    }
    data.update(overrides)
    return data


def test_repeated_open_is_idempotent_and_refreshes_broker_fields(tmp_path):
    state_file = str(tmp_path / "active.json")
    pm = PositionManager(exit_engine=None, state_file=state_file)

    assert pm.on_position_opened(broker_snapshot()) == "opened"
    assert pm.daily_trades == 1

    assert pm.on_position_opened(
        broker_snapshot(remaining_lots=0.25, sl=1.28452),
        recovered=True,
    ) == "reconciled"
    assert pm.daily_trades == 1
    assert pm.position.remaining_lots == 0.25
    assert pm.position.sl == 1.28452
    assert pm.position.decision_id == "decision-recovery-1"


def test_conflicting_position_is_not_allowed_to_replace_active_one():
    pm = PositionManager(exit_engine=None)
    pm.on_position_opened(broker_snapshot())

    result = pm.on_position_opened(
        broker_snapshot(ticket="222", position_id="different-position")
    )

    assert result == "conflict"
    assert pm.position.position_id == "18446744071234567890"
    assert pm.daily_trades == 1
    assert pm.can_open_trade() == (
        False,
        "Position recovery not resolved: conflict",
    )


def test_active_snapshot_survives_restart_and_blocks_until_live_match(tmp_path):
    state_file = str(tmp_path / "active.json")
    first = PositionManager(exit_engine=None, state_file=state_file)
    first.on_position_opened(broker_snapshot())

    restarted = PositionManager(exit_engine=None, state_file=state_file)

    assert restarted.position is not None
    assert restarted.position.position_id == "18446744071234567890"
    assert restarted.position.max_loss_usd == 75.0
    assert abs(
        (datetime.now(timezone.utc).replace(tzinfo=None)
         - restarted.position.open_time).total_seconds()
    ) < 5
    assert restarted.recovery_state == "pending"
    assert restarted.can_open_trade() == (
        False,
        "Position recovery not resolved: pending",
    )

    result = restarted.update_position(broker_snapshot(profit_usd=20.0))

    assert result["command"]["action"] == "HOLD"
    assert restarted.recovery_state == "ready"
    assert restarted.daily_trades == 1


def test_full_report_adopts_position_after_server_restart(tmp_path):
    pm = PositionManager(
        exit_engine=None,
        state_file=str(tmp_path / "active.json"),
    )

    result = pm.update_position(broker_snapshot(profit_usd=-80.0))

    assert pm.position is not None
    assert pm.position.recovered is True
    assert pm.daily_trades == 1
    assert result["has_command"] is True
    assert result["command"]["action"] == "CLOSE"
    assert "$75" in result["command"]["reason"]


def test_incomplete_legacy_report_does_not_create_position():
    pm = PositionManager(exit_engine=None)

    result = pm.update_position({
        "ticket": 12345,
        "profit_usd": 10.0,
    })

    assert result["command"]["action"] == "HOLD"
    assert pm.position is None
    assert pm.daily_trades == 0


def test_close_removes_active_snapshot(tmp_path):
    state_file = str(tmp_path / "active.json")
    pm = PositionManager(exit_engine=None, state_file=state_file)
    pm.on_position_opened(broker_snapshot())
    assert os.path.exists(state_file)

    pm.on_position_closed({
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": 25.0,
        "reason": "test close",
    })

    assert not os.path.exists(state_file)


def test_corrupt_active_snapshot_fails_closed(tmp_path):
    state_file = tmp_path / "active.json"
    state_file.write_text("{not-json", encoding="utf-8")

    pm = PositionManager(exit_engine=None, state_file=str(state_file))

    assert pm.position is None
    assert pm.recovery_state == "error"
    assert pm.can_open_trade() == (
        False,
        "Position recovery not resolved: error",
    )


def test_position_store_rejects_unsupported_schema(tmp_path):
    state_file = tmp_path / "active.json"
    state_file.write_text(json.dumps({
        "schema_version": 999,
        "position": {},
    }), encoding="utf-8")

    with pytest.raises(PositionStoreError, match="Unsupported"):
        PositionStore(str(state_file)).load()


def test_duplicate_close_is_durable_and_idempotent(tmp_path):
    history = str(tmp_path / "trades.jsonl")
    state = str(tmp_path / "active.json")
    pm = PositionManager(
        exit_engine=None, history_file=history, state_file=state
    )
    pm.on_position_opened(broker_snapshot())
    close = {
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": -25.0,
        "reason": "SL",
    }

    first = pm.on_position_closed(close)
    second = pm.on_position_closed(close)

    assert first is not None
    assert second is None
    assert pm.daily_pnl_usd == -25.0
    assert pm.daily_trades == 1
    assert len(pm.closed_trades) == 1
    assert len(open(history, encoding="utf-8").readlines()) == 1

    restarted = PositionManager(
        exit_engine=None, history_file=history, state_file=state
    )
    assert restarted.on_position_closed(close) is None
    assert restarted.daily_pnl_usd == -25.0


def test_ticket_only_close_retry_uses_both_identity_aliases(tmp_path):
    history = str(tmp_path / "trades.jsonl")
    pm = PositionManager(exit_engine=None, history_file=history)
    pm.on_position_opened(broker_snapshot())
    close = {
        "ticket": "9876543210",
        "profit": 14.0,
        "reason": "ticket-only retry",
    }

    assert pm.on_position_closed(close) is not None
    assert pm.on_position_closed(close) is None

    assert pm.daily_pnl_usd == 14.0
    assert pm.daily_trades == 1
    assert len(pm.closed_trades) == 1
    assert len(open(history, encoding="utf-8").readlines()) == 1


def test_stale_close_cannot_clear_newer_position():
    pm = PositionManager(exit_engine=None)
    pm.on_position_opened(broker_snapshot())
    pm.on_position_closed({
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "profit": 10.0,
    })
    pm.on_position_opened(
        broker_snapshot(ticket="222", position_id="position-B")
    )

    with pytest.raises(PositionConflictError):
        pm.on_position_closed({
            "ticket": "9876543210",
            "position_id": "18446744071234567890",
            "profit": 10.0,
        })

    assert pm.position.position_id == "position-B"
    assert pm.daily_pnl_usd == 10.0


def test_interrupted_close_tombstone_is_finalized_once(tmp_path):
    history = str(tmp_path / "trades.jsonl")
    state = str(tmp_path / "active.json")
    close_record = {
        "ticket": "9876543210",
        "position_id": "18446744071234567890",
        "symbol": "GBPUSD",
        "profit_usd": -40.0,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    PositionStore(state).save(
        {"ticket": "9876543210",
         "position_id": "18446744071234567890"},
        recovery_state="closing",
        close_record=close_record,
    )

    recovered = PositionManager(
        exit_engine=None, history_file=history, state_file=state
    )

    assert recovered.position is None
    assert recovered.recovery_state == "ready"
    assert recovered.daily_pnl_usd == -40.0
    assert len(open(history, encoding="utf-8").readlines()) == 1
    assert not os.path.exists(state)


def test_explicit_empty_reconcile_clears_corrupt_snapshot(tmp_path):
    state = tmp_path / "active.json"
    state.write_text("{corrupt", encoding="utf-8")
    pm = PositionManager(exit_engine=None, state_file=str(state))
    assert pm.recovery_state == "error"

    result = pm.reconcile_empty()

    assert result["reconciliation"] == "empty"
    assert result["allow_new_trades"] is True
    assert pm.recovery_state == "ready"
    assert not state.exists()


def test_empty_reconcile_does_not_discard_unreported_close(tmp_path):
    pm = PositionManager(
        exit_engine=None,
        state_file=str(tmp_path / "active.json"),
    )
    pm.on_position_opened(broker_snapshot())

    result = pm.reconcile_empty()

    assert result["reconciliation"] == "pending_close"
    assert result["position_id"] == "18446744071234567890"
    assert pm.position is not None
    assert pm.recovery_state == "pending_close"


def test_normal_open_retry_does_not_mark_trade_recovered():
    pm = PositionManager(exit_engine=None)
    pm.on_position_opened(broker_snapshot(reconcile=False))
    assert pm.position.recovered is False

    pm.on_position_opened(broker_snapshot(reconcile=False))

    assert pm.position.recovered is False


def test_report_state_is_persisted_after_partial_close(tmp_path):
    state = str(tmp_path / "active.json")
    pm = PositionManager(exit_engine=None, state_file=state)
    pm.on_position_opened(broker_snapshot())
    pm.update_position(broker_snapshot(
        remaining_lots=0.50,
        profit_usd=40.0,
    ))
    pm.update_position(broker_snapshot(
        remaining_lots=0.25,
        profit_usd=18.0,
    ))

    restarted = PositionManager(exit_engine=None, state_file=state)

    assert restarted.position.remaining_lots == 0.25
    assert restarted.position.partial_closed is True
    assert restarted.position.realized_usd == 20.0
    assert restarted.position.max_profit_usd == 40.0


def test_close_without_broker_identity_is_rejected():
    pm = PositionManager(exit_engine=None)
    pm.on_position_opened(broker_snapshot())

    with pytest.raises(ValueError, match="position_id or ticket"):
        pm.on_position_closed({"profit": 12.0, "reason": "invalid"})

    assert pm.position is not None
    assert pm.daily_pnl_usd == 0.0


def test_history_failure_keeps_closing_tombstone_until_restart(
        tmp_path, monkeypatch):
    history = str(tmp_path / "trades.jsonl")
    state = str(tmp_path / "active.json")
    pm = PositionManager(
        exit_engine=None,
        history_file=history,
        state_file=state,
    )
    pm.on_position_opened(broker_snapshot())
    monkeypatch.setattr(pm, "_write_history_line", lambda _record: False)

    with pytest.raises(PositionPersistenceError, match="history append"):
        pm.on_position_closed({
            "ticket": "9876543210",
            "position_id": "18446744071234567890",
            "profit": -22.0,
            "reason": "history temporarily unavailable",
        })

    assert pm.recovery_state == "closing"
    assert pm.can_open_trade() == (
        False,
        "Position recovery not resolved: closing",
    )
    assert pm.update_position(
        broker_snapshot(profit_usd=-25.0)
    )["command"]["action"] == "HOLD"
    assert pm.on_position_opened(broker_snapshot()) == "conflict"
    assert PositionStore(state).load()["recovery_state"] == "closing"

    restarted = PositionManager(
        exit_engine=None,
        history_file=history,
        state_file=state,
    )

    assert restarted.recovery_state == "ready"
    assert restarted.position is None
    assert restarted.daily_pnl_usd == -22.0
    assert len(open(history, encoding="utf-8").readlines()) == 1


def test_closing_tombstone_is_write_barrier_for_delayed_llm(
        tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingExit:
        def decide(self, _position):
            started.set()
            assert release.wait(timeout=3)
            return SimpleNamespace(action="HOLD", reason="late result")

    state = str(tmp_path / "active.json")
    pm = PositionManager(exit_engine=BlockingExit(), state_file=state)
    pm.on_position_opened(broker_snapshot())
    monkeypatch.setattr(pm, "_write_history_line", lambda _record: False)
    report_result = {}

    def run_report():
        report_result.update(pm.update_position(broker_snapshot(
            profit_usd=21.0
        )))

    worker = threading.Thread(target=run_report)
    worker.start()
    assert started.wait(timeout=3)

    with pytest.raises(PositionPersistenceError):
        pm.on_position_closed({
            "ticket": "9876543210",
            "position_id": "18446744071234567890",
            "profit": 20.0,
            "reason": "close during LLM",
        })

    empty = pm.reconcile_empty()
    assert empty["reconciliation"] == "closing"
    assert pm.recovery_state == "closing"

    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert report_result["command"]["action"] == "HOLD"
    assert PositionStore(state).load()["recovery_state"] == "closing"

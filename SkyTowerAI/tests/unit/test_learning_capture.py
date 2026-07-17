"""
Unit tests for F0 of the learning loop — "stop discarding experience":
- DecisionHistory persists the LLM's numeric outputs + decision_id and dumps
  the full context (data_summary + raw reply) to logs/decision_context/
- PositionManager's close record keeps MFE/MAE, prices, SL/TP and the exit trail
- EventReactionHistory stores numeric surprise magnitude
- TradingDecision carries a unique decision_id; candle labels are UTC-corrected
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python'))

from decision_history import DecisionHistory
from event_reaction_history import EventReactionHistory, surprise_magnitude
from llm_decision_engine import TradingDecision
from position_manager import PositionManager
from timeutil import utcnow


def make_decision(**overrides):
    fields = dict(
        event="CPI m/m", currency="USD", pair="USDCAD", direction="BUY",
        confidence=0.65, lot_percent=70, entry_seconds_before=15,
        exit_minutes_after=10, stop_loss_percent=40, stop_loss_pips=35.0,
        take_profit_pips=60.0, reasoning="test",
        data_summary={"event": {"datetime": "2026-07-16T12:00:00"}},
        timestamp=utcnow(), forced=False, raw_response='{"direction":"BUY"}',
    )
    fields.update(overrides)
    return TradingDecision(**fields)


class TestDecisionRecord:
    def test_numeric_outputs_and_id_persisted(self, tmp_path):
        dh = DecisionHistory(log_dir=str(tmp_path))
        decision = make_decision()
        dh.record(decision)

        with open(dh.get_file_path(), encoding='utf-8') as f:
            row = json.loads(f.readline())
        assert row["decision_id"] == decision.decision_id
        assert row["lot_percent"] == 70
        assert row["stop_loss_pips"] == 35.0
        assert row["take_profit_pips"] == 60.0
        assert row["exit_minutes"] == 10

    def test_context_dump_written(self, tmp_path):
        dh = DecisionHistory(log_dir=str(tmp_path))
        decision = make_decision()
        dh.record(decision)

        dump_path = os.path.join(str(tmp_path), 'decision_context',
                                 f"{decision.decision_id}.json")
        assert os.path.exists(dump_path)
        with open(dump_path, encoding='utf-8') as f:
            dump = json.load(f)
        assert dump["raw_response"] == '{"direction":"BUY"}'
        assert dump["data_summary"]["event"]["datetime"] == "2026-07-16T12:00:00"

    def test_decision_ids_are_unique(self):
        assert make_decision().decision_id != make_decision().decision_id

    def test_tolerates_legacy_decision_without_new_fields(self, tmp_path):
        """SimpleNamespace without decision_id/raw_response (old callers,
        tests) must still record cleanly."""
        dh = DecisionHistory(log_dir=str(tmp_path))
        legacy = SimpleNamespace(event="CPI m/m", currency="USD", pair="USDCAD",
                                 direction="SKIP", confidence=0.3, forced=False,
                                 reasoning="", data_summary=None)
        dh.record(legacy)
        assert len(dh.get_recent(5)) == 1


class TestCloseRecord:
    def _open_data(self):
        return {"ticket": 7, "symbol": "USDCAD", "direction": "BUY",
                "entry_price": 1.3700, "lots": 0.5, "sl": 1.3660, "tp": 1.3760,
                "tick_value": 10.0, "account_balance": 5000.0,
                "event_name": "CPI m/m"}

    def test_close_record_keeps_in_memory_fields(self, tmp_path):
        history = str(tmp_path / "trade_history.jsonl")
        pm = PositionManager(exit_engine=None, history_file=history)
        pm.on_position_opened(self._open_data(), entry_reasoning="beat expected",
                              decision_id="abc123")
        # Simulate what update_position tracks during the trade
        pm.position.max_profit_usd = 55.0
        pm.position.max_drawdown_usd = -12.0
        pm.position.spread_pips = 4.2
        pm.position.ai_decisions.append({"time": "t", "action": "HOLD",
                                         "reasoning": "developing"})
        pm.on_position_closed({"ticket": 7, "profit": 33.3, "reason": "AI exit",
                               "close_price": 1.3733, "profit_source": "history"})

        with open(history, encoding='utf-8') as f:
            rec = json.loads(f.readline())
        assert rec["decision_id"] == "abc123"
        assert rec["entry_price"] == 1.3700
        assert rec["close_price"] == 1.3733
        assert rec["sl"] == 1.3660 and rec["tp"] == 1.3760
        assert rec["max_profit_usd"] == 55.0
        assert rec["max_drawdown_usd"] == -12.0
        assert rec["spread_pips"] == 4.2
        assert rec["entry_reasoning"] == "beat expected"
        assert rec["ai_decisions"][0]["action"] == "HOLD"

    def test_orphaned_close_keeps_schema(self, tmp_path):
        history = str(tmp_path / "trade_history.jsonl")
        pm = PositionManager(exit_engine=None, history_file=history)
        pm.on_position_closed({"ticket": 9, "profit": -5.0,
                               "close_price": 1.1111, "reason": "SL"})
        with open(history, encoding='utf-8') as f:
            rec = json.loads(f.readline())
        assert rec["decision_id"] == ""
        assert rec["close_price"] == 1.1111
        assert rec["ai_decisions"] == []


class TestSurpriseMagnitude:
    def test_helper_parses_units(self):
        assert surprise_magnitude("0.5%", "0.3%") == pytest.approx(0.2)
        assert surprise_magnitude("210K", "180K") == pytest.approx(30000)
        assert surprise_magnitude(None, "0.3%") is None

    def test_recorded_with_reaction(self, tmp_path):
        h = EventReactionHistory(log_dir=str(tmp_path))
        entry = h.record({"event_name": "CPI m/m", "currency": "USD",
                          "event_time": "2026-07-16T12:00:00", "pair": "USDCAD",
                          "forecast": "0.3%", "actual": "0.5%",
                          "price_at_event": 1.37, "price_after_1min": 1.371,
                          "price_after_5min": 1.372})
        assert entry["surprise"] == "BEAT"
        assert entry["surprise_magnitude"] == pytest.approx(0.2)

    def test_backfill_fills_magnitude_and_revision(self, tmp_path):
        h = EventReactionHistory(log_dir=str(tmp_path))
        h.record({"event_name": "CPI m/m", "currency": "USD",
                  "event_time": "2026-07-16T12:00:00", "pair": "USDCAD",
                  "forecast": "0.3%", "previous": "0.2%",
                  "price_at_event": 1.37, "price_after_1min": 1.371,
                  "price_after_5min": 1.372})
        released = SimpleNamespace(
            event_name="CPI m/m", currency="USD",
            datetime_utc=datetime(2026, 7, 16, 12, 0, 0),
            actual="0.5%", forecast="0.3%", previous="0.1%")
        assert h.backfill_actuals([released]) == 1
        rec = h.get_recent(1)[0]
        assert rec["surprise_magnitude"] == pytest.approx(0.2)
        assert rec["previous_revised"] == "0.1%"
        assert rec["revision_magnitude"] == pytest.approx(-0.1)


class TestCandleUtcLabels:
    def test_labels_shifted_to_utc(self):
        from market_context import build_market_context

        now = datetime(2026, 7, 16, 12, 0, 0)
        offset = 3 * 3600  # UTC+3 broker
        bars = []
        for i in range(20):
            t_utc = now - timedelta(minutes=20 - i)
            bars.append({"time": int(t_utc.replace(tzinfo=timezone.utc).timestamp()) + offset,
                         "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005})
        ctx = build_market_context({"M1": bars}, "EURUSD",
                                   registered_at=now.isoformat())
        # Newest bar opened 11:59 UTC — the label must say so, not 14:59
        assert ctx["candles"]["M1"][-1].startswith("11:59")
